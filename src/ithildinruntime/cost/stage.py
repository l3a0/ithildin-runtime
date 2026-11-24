"""The cost stage: read data's result, then store the strategy's schedule for the decision.

Each strategy has a cost stage of its own, named `cost/<strategy>`, so one
strategy's terms and configuration hash are all a schedule's header names. The
header's release and that hash are the model's version. The stage is
deterministic: its bytes come from the snapshot's key and SHA-256, the release
and the strategy's file, and it reads no clock and no mode.

Its handler does three things, in order.

1. **It reads data's result and the snapshot it lists**, through
   `core.StageDecision`. A result that holds a halt is passed on, as
   docs/design.md's `### Rule 7: a halt is passed on` says: the stage's own
   result is a halt naming data's result as its cause, and no schedule is
   stored.
2. **It asks the model for the schedule.** A configuration the model refuses
   becomes the stage's own halt, whose `cause` is empty because nothing
   upstream halted.
3. **It stores the schedule, then returns the result**, which the loop stores
   last.

A halt at any of the three becomes the stage's result, whose header names
each blob read by then.

The entry point hands in the stage's name, its kind and the stage it reads, as
the stage table declares them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol

from ithildinruntime.core import (
    CostModel,
    CostSchedule,
    DecisionResult,
    Halt,
    StageKind,
    StageRun,
    Storage,
)

__all__ = ["COMPONENT", "SCHEDULE", "CostStage"]

COMPONENT = "cost"
SCHEDULE = "schedule"
# The format of the schedule's file and the stage's result header.
_FORMAT = 1


class _Item(Protocol):
    """The receive loop's item, of which cost reads only the decision time."""

    decision_at: datetime


class CostStage:
    """The handler the receive loop calls for one strategy's cost stage."""

    def __init__(
        self,
        model: CostModel,
        storage: Storage,
        *,
        stage: str,
        kind: StageKind,
        data_stage: str,
        deployment: str,
        run: str,
        release: str,
        config_hashes: Mapping[str, str],
    ) -> None:
        self._model = model
        self._data_stage = data_stage
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
        decision = self._run.decision(item.decision_at)
        try:
            snapshot = decision.snapshot(self._data_stage)
            report()
            schedule = self._model.schedule(snapshot)
            stored = decision.put(SCHEDULE, CostSchedule, (schedule,))
            report()
        except Halt as halt:
            return decision.halted(halt)
        return decision.done(stored)
