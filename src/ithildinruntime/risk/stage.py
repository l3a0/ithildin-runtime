"""The risk stage: read data's snapshot and recover's files, then store the decision's limits.

The stage is shared by every strategy of the deployment, because it reads the
account's book. It is deterministic: its bytes come from the blobs its header
names, the release and the strategies' files, and it reads no clock and no
mode. It never reads the owner's caps blob, which the owner can rewrite
between two attempts. It reads the caps as execution's recover stage parsed
and stored them, so a second attempt writes the same bytes. The order path
reads the blob itself as each order's send begins, and hands it to the gate.

Its handler does four things, in order.

1. **It reads data's result and then recover's**, through
   `core.StageDecision`. A halt in either is passed on, as docs/design.md's
   `### Rule 7: a halt is passed on` says, naming that result as its cause,
   before any file or any strategy's section is read.
2. **It reads four files**, the snapshot, the book, the hard caps and the
   session's valuation, each holding one entry.
3. **It asks the model for the strategy's limits**, tightened by the caps. A
   deployment of two strategies halts, because the scope each one's limits
   take is not decided.
4. **It stores one `Constraints` per strategy, then returns the result**, which
   the loop stores last.

A halt at any of the four becomes the stage's result, whose header names each
blob read and accepted by then, and whose `cause` is empty unless it passes one
on.

The entry point hands in the stress records, which the release carries, and the
stage's name, its kind and the stages it reads, as the stage table declares
them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Protocol

from ithildinruntime.core import (
    BOOK_OUTPUT,
    CAPS_OUTPUT,
    CONSTRAINTS_OUTPUT,
    VALUATION_OUTPUT,
    Book,
    Constraints,
    DecisionResult,
    Halt,
    HardCaps,
    StageKind,
    StageRun,
    Storage,
    StrategyConfig,
    StressRecords,
    Valuation,
)

from .model import LimitRisk

__all__ = ["RiskStage"]

# The format of the constraints file and the stage's result header.
_FORMAT = 1


class _Item(Protocol):
    """The receive loop's item, of which risk reads only the decision time."""

    decision_at: datetime


class RiskStage:
    """The handler the receive loop calls for the risk stage."""

    def __init__(
        self,
        strategies: Iterable[StrategyConfig],
        storage: Storage,
        *,
        records: StressRecords,
        stage: str,
        kind: StageKind,
        data_stage: str,
        recover_stage: str,
        deployment: str,
        run: str,
        release: str,
        config_hashes: Mapping[str, str],
    ) -> None:
        self._strategies = tuple(strategies)
        self._records = records
        self._data_stage = data_stage
        self._recover_stage = recover_stage
        self._run = StageRun(
            storage,
            format=_FORMAT,
            stage=stage,
            kind=kind,
            deployment=deployment,
            run=run,
            release=release,
            config_hashes=config_hashes,
        )

    def __call__(self, item: _Item, report: Callable[[], None]) -> DecisionResult:
        decision = self._run.decision(item.decision_at, report)
        try:
            # Both results first, so a halt in either is passed on whatever the
            # other's files hold.
            decision.result(self._data_stage)
            decision.result(self._recover_stage)
            snapshot = decision.snapshot(self._data_stage)
            book = decision.entry(self._recover_stage, BOOK_OUTPUT, Book)
            caps = decision.entry(self._recover_stage, CAPS_OUTPUT, HardCaps)
            valuation = decision.entry(self._recover_stage, VALUATION_OUTPUT, Valuation)
            limits = self._model().constraints(book, snapshot, caps, valuation)
            stored = decision.put(CONSTRAINTS_OUTPUT, Constraints, (limits,))
        except Halt as halt:
            return decision.halted(halt)
        return decision.done(stored)

    def _model(self) -> LimitRisk:
        # The model is built at each decision, so it keeps nothing from the one before.
        if len(self._strategies) != 1:
            names = ", ".join(strategy.name for strategy in self._strategies)
            raise Halt(
                f"the deployment runs {len(self._strategies)} strategies ({names}), and the scope "
                "each one's limits take is decided for one alone"
            )
        return LimitRisk(self._strategies[0], self._records)
