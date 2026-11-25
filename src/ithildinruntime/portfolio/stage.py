"""The portfolio stage: read every model's result, then store each strategy's target.

One stage serves every strategy of the deployment. It is deterministic: its
bytes come from the blobs its header names, the release and the strategies'
files, and it reads no clock, no mode, no queue and nothing under `accounts/`.

Its handler does four things, in order, and the first fault decides.

1. **It reads each input's result**, in the order the stage table lists them.
   Each is stored, then holds no halt, then carries the configuration the stage
   table gives that input, so a file an older configuration wrote is never sized
   with a newer one. A halt is passed on naming that result as its cause, as
   docs/design.md's `### Rule 7: a halt is passed on` says.
2. **It reads each file**: data's snapshot, recover's book, valuation and caps,
   risk's constraints with one entry per strategy, and each strategy's forecast
   and then its cost schedule. The schedule is read and weighs nothing, because
   every estimate is zero in slice 1.
3. **It sizes each strategy in turn**, through `size`. Each one's target is
   sized as if the account held nothing else.
4. **It stores the targets, and then how each was sized**, one entry per
   strategy in each file, both computed before either is stored.

A halt at any of the four becomes the stage's result, whose header names each
blob read and accepted by then. `TargetPortfolio` has no form for the "trade
nothing" mark, so an input that carries it halts rather than reading as a
target.

The entry point hands in the stress records, which the release carries, the
stage's name, its kind, and each stage it reads with the configuration hashes
the stage table gives it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from typing import Protocol

from ithildinruntime.core import (
    ALPHA_COMPONENT,
    BOOK_OUTPUT,
    CAPS_OUTPUT,
    CONSTRAINTS_OUTPUT,
    COST_COMPONENT,
    DATA_STAGE,
    FORECAST_OUTPUT,
    RECOVER_STAGE,
    RISK_STAGE,
    SCHEDULE_OUTPUT,
    SIZING_OUTPUT,
    TARGET_OUTPUT,
    VALUATION_OUTPUT,
    Book,
    Constraints,
    CostSchedule,
    DecisionResult,
    Forecast,
    Halt,
    HardCaps,
    Sizing,
    StageDecision,
    StageKind,
    StageRun,
    Storage,
    StrategyConfig,
    StressRecords,
    TargetPortfolio,
    Valuation,
)

from .sizing import size

__all__ = ["PortfolioStage"]

# The format of both files and of the stage's result header.
_FORMAT = 1


class _Item(Protocol):
    """The receive loop's item, of which portfolio construction reads only the decision time."""

    decision_at: datetime


class PortfolioStage:
    """The handler the receive loop calls for the portfolio stage."""

    def __init__(
        self,
        strategies: Iterable[StrategyConfig],
        storage: Storage,
        *,
        records: StressRecords,
        stage: str,
        kind: StageKind,
        inputs: Mapping[str, Mapping[str, str]],
        deployment: str,
        run: str,
        release: str,
        config_hashes: Mapping[str, str],
    ) -> None:
        self._strategies = tuple(sorted(strategies, key=lambda s: s.name))
        self._names = tuple(s.name for s in self._strategies)
        self._records = records
        self._inputs = {name: dict(hashes) for name, hashes in inputs.items()}
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
            # Every result first, so a halt in one is passed on whatever another's files hold.
            for upstream, hashes in self._inputs.items():
                decision.result(upstream, hashes)
            made = self._sized(decision)
            targets = decision.put(TARGET_OUTPUT, TargetPortfolio, tuple(t for t, _ in made))
            sizings = decision.put(SIZING_OUTPUT, Sizing, tuple(s for _, s in made))
        except Halt as halt:
            return decision.halted(halt)
        return decision.done(targets, sizings)

    def _sized(self, decision: StageDecision) -> list[tuple[TargetPortfolio, Sizing]]:
        snapshot = decision.snapshot(DATA_STAGE)
        book = decision.entry(RECOVER_STAGE, BOOK_OUTPUT, Book)
        valuation = decision.entry(RECOVER_STAGE, VALUATION_OUTPUT, Valuation)
        caps = decision.entry(RECOVER_STAGE, CAPS_OUTPUT, HardCaps)
        limits = self._limits(decision)
        forecasts = {}
        for name in self._names:
            forecasts[name] = decision.entry(f"{ALPHA_COMPONENT}/{name}", FORECAST_OUTPUT, Forecast)
            decision.entry(f"{COST_COMPONENT}/{name}", SCHEDULE_OUTPUT, CostSchedule)
        return [
            size(
                strategy,
                self._names,
                forecasts[strategy.name],
                snapshot,
                book,
                limits[strategy.name],
                caps,
                valuation,
                self._records,
            )
            for strategy in self._strategies
        ]

    def _limits(self, decision: StageDecision) -> Mapping[str, Constraints]:
        """Each strategy's limits, which risk's file holds once for each and for no other."""
        found = decision.entries(RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints)
        listed = sorted(entry.strategy for entry in found)
        if listed != list(self._names):
            raise Halt(
                f"risk's constraints are of {', '.join(listed)}, not of "
                f"{', '.join(self._names)} once each"
            )
        return {entry.strategy: entry for entry in found}
