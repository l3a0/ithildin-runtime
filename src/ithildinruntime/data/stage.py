"""Data's stage: wait for the decision time, read the snapshot, store it, then judge it.

`data` is a capturing stage with no inputs, so the receive loop hands it every
decision the moment the plan exists, side by side. Its handler does five
things, in order.

1. **It waits for the decision time** through `Clock.wait_until`, handing the
   loop's `report` along, so a wall clock keeps the heartbeat beating while
   it waits and a history clock returns at once. Data holds no branch on the
   mode. A stop does not reach a handler waiting here, because the receive
   loop hands a handler no stop signal, so the process stays up until the
   wait ends. The wait has written nothing, so a decision cut off by the
   host's restart runs again safely, but a stop that should end the process
   at once cannot yet. #107 holds the fix, because a stop that leaves the
   decision for redelivery needs the loop to say so.
2. **It reads the snapshot** through `LakeSource`. A read that cannot give a
   snapshot raises `Halt`, which the loop stores as data's result.
3. **It stores the snapshot** through `core.Storage`'s rule for a capturing
   stage. The first capture wins, and a later read that differs, even only in
   its read time, is stored under its own attempt number, which alarms. The
   alarm's reason says whether any bar, status, flag or ledger differs, or
   only the read time, so a restatement by marketlake reads apart from a
   redelivery.
4. **It judges the stored snapshot.** A missing or stale instrument, or a bar
   of unstated kind, or a bar whose prices contradict each other, ends the
   decision with a halt as data's result that names the stored snapshot, as
   hard limit 3 asks. A close past the written multiple halts nothing. Its
   flag stays in the stored snapshot beside the price, monitoring alerts the
   owner, which #11 builds, and the decision trades on the price as it
   arrived.
5. **It returns the result**, which the loop stores last. The result lists
   the snapshot that counts first, and the attempt that differed after it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol

from ithildinruntime.core import (
    BlobHeader,
    Clock,
    DecisionResult,
    Halt,
    OutputFile,
    Snapshot,
    StageKind,
    Storage,
    attempt_key,
    decode_output,
    encode_output,
    output_key,
)

from .source import LakeSource

__all__ = ["SNAPSHOT", "STAGE", "DataStage", "differences"]

STAGE = "data"
SNAPSHOT = "snapshot"
# The format of data's snapshot file and result header.
_FORMAT = 1
_READ_TIME = datetime(2000, 1, 1, tzinfo=UTC)


class _Item(Protocol):
    """The receive loop's item, of which data reads only the decision time."""

    decision_at: datetime


class DataStage:
    """The handler the receive loop calls for data's stage, one decision at a time."""

    def __init__(
        self,
        source: LakeSource,
        storage: Storage,
        clock: Clock,
        *,
        deployment: str,
        run: str,
        release: str,
        config_hashes: Mapping[str, str],
    ) -> None:
        self._source = source
        self._storage = storage
        self._clock = clock
        self._deployment = deployment
        self._run = run
        self._release = release
        self._config_hashes = dict(config_hashes)

    def __call__(self, item: _Item, report: Callable[[], None]) -> DecisionResult:
        at = item.decision_at
        self._clock.wait_until(at, report)
        reading = self._source.read(at)
        report()
        header = BlobHeader(
            format=_FORMAT,
            deployment=self._deployment,
            run=self._run,
            stage=STAGE,
            kind=str(StageKind.CAPTURING),
            decision_at=at,
            inputs={},
            release=self._release,
            config_hashes=self._config_hashes,
        )
        data = encode_output(Snapshot, OutputFile(header, (reading.snapshot,), False))
        stored = self._storage.put(
            StageKind.CAPTURING,
            output_key(self._deployment, self._run, at, STAGE, SNAPSHOT),
            data,
            attempt=lambda n: attempt_key(self._deployment, self._run, at, STAGE, SNAPSHOT, n),
            explain=lambda first: differences(first, data),
        )
        report()
        outputs = (stored.counts,) if stored.ref == stored.counts else (stored.counts, stored.ref)
        halt = None
        if reading.halts:
            halt = self._storage.halt_record(Halt("; ".join(reading.halts)), cause=stored.counts)
        return DecisionResult(header=header, outputs=outputs, halt=halt)


def differences(first: bytes, later: bytes) -> str:
    """What differs between two captures of one decision's snapshot, for the alarm."""
    try:
        a = decode_output(first, Snapshot).entries[0]
        b = decode_output(later, Snapshot).entries[0]
    except Exception as error:
        return f"the first capture is no snapshot the codec reads: {type(error).__name__}: {error}"
    differ = []
    if _without_read_time(a.bars) != _without_read_time(b.bars):
        differ.append("bars")
    for name in ("status", "flags", "ledgers", "quotes", "segments", "provisional"):
        if getattr(a, name) != getattr(b, name):
            differ.append(name)
    if not differ:
        return "Only the read time differs, as on a redelivery after a crash"
    return f"These differ, as on a restatement by marketlake: {', '.join(differ)}"


def _without_read_time(bars: object) -> object:
    return {
        instrument: {
            interval: tuple(dataclasses.replace(bar, first_seen=_READ_TIME) for bar in series)
            for interval, series in by_interval.items()
        }
        for instrument, by_interval in bars.items()  # type: ignore[attr-defined]
    }
