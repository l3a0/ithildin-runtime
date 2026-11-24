"""Data's stage: wait for the decision time, read the snapshot, store it, then judge it.

`data` is a capturing stage with no inputs, so the receive loop hands it every
decision the moment the plan exists, side by side. Its handler does five
things, in order.

1. **It waits for the decision time** through `Clock.wait_until`, handing the
   loop's `report` along, so a wall clock keeps the heartbeat beating while
   it waits and a history clock returns at once. Data holds no branch on the
   mode. A stop ends a wall clock's wait at once with `Stopped`, which this
   handler lets through. The loop then stores nothing for the decision, and
   nothing is written before the wait, so a later check starts the decision
   again safely.
2. **It reads the snapshot** through `LakeSource`. A read that cannot give a
   snapshot raises `Halt`, which the loop stores as data's result. The read
   comes before the store, so it halts this way even when a redelivery finds
   a first capture already stored. Settings that could not be built from the
   strategies' files halt here too, at each decision, so the reason is stored
   as the decision's result rather than lost with a worker that fails to start.
3. **It stores the snapshot** through `core.Storage`'s rule for a capturing
   stage. The first capture wins, and a later read that differs, even only in
   its read time, is stored under its own attempt number, which alarms. The
   alarm's reason names what differs: the header's format, release or
   strategies' configuration hashes, and any bar, status, flag, ledger,
   quote, segment or provisional mark, or says that a capture holds other
   than one snapshot, that the first capture is no snapshot the codec reads,
   or that only the read time differs, as on a
   redelivery after a crash. It names no cause beyond that, because the
   same difference can come from a restatement by marketlake, a nightly
   upload or a redeploy.
4. **It judges the snapshot that counts.** That is the read just made, unless
   an earlier capture of the decision is stored, in which case it is that
   first capture, because every later stage reads it. A first capture that
   cannot be read to judge it halts, and so does one that holds other than
   one snapshot or was written under another release or other strategy
   configuration hashes.
   That read is the alarm's single try, so one failed read of the first
   capture halts its decision. A missing or stale instrument, or a bar
   of unstated kind, or a bar whose prices contradict each other, and on a
   redelivery each case `data.source.judge` lists, ends the decision with a
   halt as data's result that names the stored snapshot, as
   hard limit 3 asks. A close past the written multiple, or below its bar's
   low, halts nothing. Its flag stays in the stored snapshot beside the
   price, monitoring alerts the owner, which #11 builds, and the decision
   trades on the price as it arrived.
5. **It returns the result**, which the loop stores last. The result lists
   the snapshot that counts first, and the attempt that differed after it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol

from ithildinruntime.core import (
    DATA_STAGE,
    SNAPSHOT_OUTPUT,
    BlobHeader,
    BlobRef,
    Clock,
    DecisionResult,
    Halt,
    InstrumentId,
    OutputFile,
    Progress,
    Snapshot,
    StageKind,
    Storage,
    attempt_key,
    decode_output,
    encode_output,
    output_key,
    sha256_of,
)

from .source import LakeSource, judge

__all__ = ["SNAPSHOT", "STAGE", "DataStage", "differences"]

STAGE = DATA_STAGE
SNAPSHOT = SNAPSHOT_OUTPUT
# The format of data's snapshot file and result header.
_FORMAT = 1
_READ_TIME = datetime(2000, 1, 1, tzinfo=UTC)


class _Item(Protocol):
    """The receive loop's item, of which data reads only the decision time."""

    decision_at: datetime


class DataStage:
    """The handler the receive loop calls for data's stage, for any number of decisions at once.

    `source` is the read, or the `Halt` that refused its settings, which every
    decision then raises.
    """

    def __init__(
        self,
        source: LakeSource | Halt,
        storage: Storage,
        clock: Clock,
        *,
        deployment: str,
        run: str,
        release: str,
        config_hashes: Mapping[str, str],
    ) -> None:
        if not release:
            raise ValueError("data's stage names its release, which a redelivery checks")
        self._source = source
        self._storage = storage
        self._clock = clock
        self._deployment = deployment
        self._run = run
        self._release = release
        self._config_hashes = dict(config_hashes)

    def __call__(self, item: _Item, report: Progress) -> DecisionResult:
        at = item.decision_at
        self._clock.wait_until(at, report)
        source = self._source
        if isinstance(source, Halt):
            # Decisions run side by side, and a raised exception gains its own traceback.
            raise Halt(source.reason)
        reading = source.read(at)
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
        first: list[bytes] = []

        def explain(held: bytes) -> str:
            first.append(held)
            return differences(held, data)

        stored = self._storage.put(
            StageKind.CAPTURING,
            output_key(self._deployment, self._run, at, STAGE, SNAPSHOT),
            data,
            attempt=lambda n: attempt_key(self._deployment, self._run, at, STAGE, SNAPSHOT, n),
            explain=explain,
        )
        report()
        outputs = (stored.counts,) if stored.ref == stored.counts else (stored.counts, stored.ref)
        if stored.ref == stored.counts:
            reasons = reading.halts
        else:
            reasons = _counted(
                stored.counts, first, source.instruments, self._release, self._config_hashes
            )
        halt = None
        if reasons:
            halt = self._storage.halt_record(Halt("; ".join(reasons)), cause=stored.counts)
        return DecisionResult(header=header, outputs=outputs, halt=halt)


def _counted(
    counts: BlobRef,
    first: Sequence[bytes],
    instruments: Sequence[InstrumentId],
    release: str,
    config_hashes: Mapping[str, str],
) -> tuple[str, ...]:
    """The halts of the capture that counts, which every later stage reads.

    A later read that differs judges nothing, because the decision trades from
    the first capture. A first capture that cannot be read, does not match its
    stored SHA-256, cannot be decoded or holds other than one snapshot halts,
    since it cannot be judged. So does one written under another release or
    other strategy configuration hashes: only this release's code, under the
    settings those hashes name, reads its fields and status as it wrote them,
    so `judge` repeats this release's read and no other's. Data's own settings
    come from the files those hashes name, so a change to one moves a hash.
    """
    if not first:
        return (f"{counts.key} counts and could not be read to judge it",)
    if sha256_of(first[0]) != counts.sha256:
        return (
            f"{counts.key} counts and was read back with SHA-256 {sha256_of(first[0])}, "
            f"not the stored {counts.sha256}",
        )
    try:
        stored = decode_output(first[0], Snapshot)
    except Exception as error:
        return (f"{counts.key} counts and is no snapshot the codec reads: {error}",)
    if stored.header.release != release:
        return (
            f"{counts.key} counts and was written by release {stored.header.release!r}, "
            f"not this release {release!r}, so it cannot be judged",
        )
    if dict(stored.header.config_hashes) != dict(config_hashes):
        return (
            f"{counts.key} counts and was written under configuration hashes "
            f"{dict(stored.header.config_hashes)}, not this stage's {dict(config_hashes)}, "
            "so it cannot be judged",
        )
    if len(stored.entries) != 1:
        return (f"{counts.key} counts and holds {len(stored.entries)} snapshots, not one",)
    snapshot = stored.entries[0]
    return tuple(f"{counts.key}, which counts: {r}" for r in judge(snapshot, instruments))


def differences(first: bytes, later: bytes) -> str:
    """What differs between two captures of one decision's snapshot, for the alarm."""
    try:
        one = decode_output(first, Snapshot)
        two = decode_output(later, Snapshot)
    except Exception as error:
        return f"the first capture is no snapshot the codec reads: {type(error).__name__}: {error}"
    if len(one.entries) != 1 or len(two.entries) != 1:
        return (
            f"The captures hold {len(one.entries)} and {len(two.entries)} snapshots, "
            "where each should hold one"
        )
    a, b = one.entries[0], two.entries[0]
    header = []
    if one.header.format != two.header.format:
        header.append("the format")
    if one.header.release != two.header.release:
        header.append("the release")
    if dict(one.header.config_hashes) != dict(two.header.config_hashes):
        header.append("the strategies' configuration hashes")
    snapshot = []
    if _without_read_time(a.bars) != _without_read_time(b.bars):
        snapshot.append("bars")
    for name in ("status", "flags", "ledgers", "quotes", "segments", "provisional"):
        if getattr(a, name) != getattr(b, name):
            snapshot.append(name)
    named = ", ".join(header + snapshot)
    if not named:
        return "Only the read time differs, as on a redelivery after a crash"
    return f"These differ: {named}"


def _without_read_time(bars: object) -> object:
    return {
        instrument: {
            interval: tuple(dataclasses.replace(bar, first_seen=_READ_TIME) for bar in series)
            for interval, series in by_interval.items()
        }
        for instrument, by_interval in bars.items()  # type: ignore[attr-defined]
    }
