"""The cost stage: read data's result, then store the strategy's schedule for the decision.

Each strategy has a cost stage of its own, named `cost/<strategy>`, so one
strategy's terms and configuration hash are all a schedule's header names. The
header's release and that hash are the model's version. The stage is
deterministic: its bytes come from the snapshot's key and SHA-256, the release
and the strategy's file, and it reads no clock and no mode.

Its handler does three things, in order.

1. **It reads data's result and the snapshot it lists**, through
   `core.read_snapshot`. A result that holds a halt is passed on, as
   docs/design.md's `### Rule 7: a halt is passed on` says: the stage's own
   result is a halt naming data's result as its cause, and no schedule is
   stored.
2. **It asks the model for the schedule.** A configuration the model refuses
   becomes the stage's own halt, whose header names data's result and the
   snapshot, and whose `cause` is empty because nothing upstream halted.
3. **It stores the schedule, then returns the result**, which the loop stores
   last.

The entry point hands in the stage's name, its kind and the stage it reads, as
the stage table declares them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Protocol

from ithildinruntime.core import (
    BlobHeader,
    BlobRef,
    CostModel,
    CostSchedule,
    DecisionResult,
    Halt,
    HaltRecord,
    OutputFile,
    StageKind,
    Storage,
    encode_output,
    output_key,
    read_snapshot,
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
        self._storage = storage
        self._stage = stage
        self._kind = StageKind(kind)
        self._data_stage = data_stage
        self._deployment = deployment
        self._run = run
        self._release = release
        self._config_hashes = dict(config_hashes)

    def __call__(self, item: _Item, report: Callable[[], None]) -> DecisionResult:
        at = item.decision_at
        read = read_snapshot(
            self._storage,
            deployment=self._deployment,
            run=self._run,
            decision_at=at,
            stage=self._stage,
            data_stage=self._data_stage,
        )
        if isinstance(read, HaltRecord):
            return DecisionResult(header=self._header(at, read.cause), outputs=(), halt=read)
        report()
        header = self._header(at, read.result, read.file)
        try:
            schedule = self._model.schedule(read.snapshot)
        except Halt as halt:
            return DecisionResult(header=header, outputs=(), halt=self._storage.halt_record(halt))
        stored = self._storage.put(
            self._kind,
            output_key(self._deployment, self._run, at, self._stage, SCHEDULE),
            encode_output(CostSchedule, OutputFile(header, (schedule,), False)),
        )
        report()
        return DecisionResult(header=header, outputs=(stored.ref,), halt=None)

    def _header(self, at: datetime, *read: BlobRef) -> BlobHeader:
        return BlobHeader(
            format=_FORMAT,
            deployment=self._deployment,
            run=self._run,
            stage=self._stage,
            kind=str(self._kind),
            decision_at=at,
            inputs={ref.key: ref.sha256 for ref in read},
            release=self._release,
            config_hashes=self._config_hashes,
        )
