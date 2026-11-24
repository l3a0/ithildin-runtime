"""The cost stage: read data's result, then store the strategy's schedule for the decision.

Each strategy has a cost stage of its own, named `cost/<strategy>`, so one
strategy's terms and configuration hash are all a schedule's header names. The
header's release and that hash are the model's version. The stage is
deterministic: its bytes come from the snapshot's key and SHA-256, the release
and the strategy's file, and it reads no clock and no mode.

Its handler does three things, in order.

1. **It reads data's result.** A result that holds a halt is passed on, as
   docs/design.md's `### Rule 7: a halt is passed on` says: the stage's own
   result is a halt naming data's result as its cause, and no schedule is
   stored.
2. **It reads the snapshot data's result lists, and asks the model for the
   schedule.** A configuration the model refuses raises `Halt`, which the
   loop stores as the stage's result.
3. **It stores the schedule, then returns the result**, which the loop stores
   last.

The entry point hands in the names of data's stage and of its snapshot,
because `cost` imports only `core`.
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
    OutputFile,
    Snapshot,
    StageKind,
    Storage,
    encode,
    encode_output,
    output_key,
    result_key,
    sha256_of,
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
        data_stage: str,
        snapshot: str,
        deployment: str,
        run: str,
        release: str,
        config_hashes: Mapping[str, str],
    ) -> None:
        self._model = model
        self._storage = storage
        self._stage = stage
        self._data_stage = data_stage
        self._snapshot = snapshot
        self._deployment = deployment
        self._run = run
        self._release = release
        self._config_hashes = dict(config_hashes)

    def __call__(self, item: _Item, report: Callable[[], None]) -> DecisionResult:
        at = item.decision_at
        data_key = result_key(self._deployment, self._run, at, self._data_stage)
        data = self._storage.read_result(data_key, at)
        if data is None:
            raise Halt(f"{data_key} is not stored, and stage {self._stage} reads it")
        # The codec reads only canonical bytes, so this is the stored blob's SHA-256.
        found = BlobRef(data_key, sha256_of(encode(data)))
        if data.halt is not None:
            halt = Halt(f"{data_key} holds a halt: {data.halt.reason}")
            return DecisionResult(
                header=self._header(at, found),
                outputs=(),
                halt=self._storage.halt_record(halt, cause=found),
            )
        # Data lists the capture that counts under its own key, and any later
        # capture that differed under an attempt key.
        counts = output_key(self._deployment, self._run, at, self._data_stage, self._snapshot)
        listed = [ref for ref in data.outputs if ref.key == counts]
        if len(listed) != 1:
            raise Halt(f"{data_key} lists {counts} {len(listed)} times, not once")
        file = self._storage.read_output(data_key, data, listed[0], at, Snapshot)
        if len(file.entries) != 1:
            raise Halt(f"{counts} holds {len(file.entries)} snapshots, not one")
        report()
        schedule = self._model.schedule(file.entries[0])
        header = self._header(at, found, listed[0])
        stored = self._storage.put(
            StageKind.DETERMINISTIC,
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
            kind=str(StageKind.DETERMINISTIC),
            decision_at=at,
            inputs={ref.key: ref.sha256 for ref in read},
            release=self._release,
            config_hashes=self._config_hashes,
        )
