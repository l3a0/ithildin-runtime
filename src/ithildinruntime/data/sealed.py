"""The read of one decision's quotes, from a day marketlake has sealed.

A decision reads this way where the manifest it downloaded lists the quotes
partition of any instrument its settings read, for the session's day.
docs/architecture/03-context-and-scope.md's `#### Which read a decision uses`
says why. How it goes, in order.

1. **Each instrument's partition is found by its ticker.** Its key is
   `LakePaths.quotes_partition_path` of the ticker the security master gives
   for the day, relative to the root. An instrument whose partition is not
   listed beside one whose is, is a fault, because a sealed read never opens a
   segment.
2. **Each listed partition is linked into the decision's lake root**, one
   `Mirror.partitions` call a key, after the bars are read. Every listed key is
   linked whatever faults before it.
3. **Each partition has to hold the session's equity close**, as a data row, or
   as a gap row tagged `spot_close` where the close's capture failed.
   marketlake seals at close+15, and its repair by hand can seal a day still
   open, which the gate would trade on as healthy. A gap row tagged `spot_close`
   is written only at or after the close, so a day that holds one was sealed
   after it. `NoSpotClose.tagged_gaps` counts those rows, and a count of zero
   is the fault. l3a0/marketlake#1050 asks marketlake to publish that the row
   is never written earlier, which its code holds and its README does not say.
4. **The walk of `cycles` reads five minutes of each leg at a time**, from the
   cut's own minute back to the minute the session opens. A minute is read by
   `lake.loader.load_quotes` at its New York `HH:MM`, then cut with
   `lake.unsealed.captured_by` at `cycles.CAPTURE_LAG` before the decision, the
   cut the read of segments takes too.

Every error but `MemoryError` that `load_quotes`, `captured_by`, the mirror or
this module's own checks meet is a fault, so no shape of answer takes the
snapshot's place. A stop leaves the read, since it is no `Exception`. The
table in docs/halts-and-drills.md's `### What the sealed read does with each
answer` lists every fault, and what ends the halt it causes.

A reason names a minute by its UTC instant. Only the call to `load_quotes`
takes New York's `HH:MM`.

`stated_close` reads one more thing from a sealed day, for the record of
splits rather than for a quote: every `close_price` its rows at the equity
close state, which is the previous session's close as Schwab states it.
`data.splits` compares it with a stored close, and nothing prices anything
with it. It links the partition and maps every error the same way, but stores
the fault in its answer, so the decision's snapshot holds it and a redelivery
halts on it too. A day with no data row at the equity close, including one
whose close capture failed and holds a gap row tagged `spot_close` there,
states no close, and the pair it would have covered is not covered.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
from lake.loader import NoSpotClose, PartitionQuarantined, SnapAbsent, load_quotes
from lake.paths import LakePaths
from lake.unsealed import captured_by

from ithildinruntime.core import Clock, Halt, InstrumentId, Session

from .cycles import CAPTURE_LAG, Fault, Leg, QuoteLimits, QuoteRead, Walk, Words
from .mirror import LakeRoot, Mirror, vintage_id
from .reference import QuoteKinds, SecurityMaster

__all__ = ["StatedClose", "is_listed", "partition_key", "read_sealed", "stated_close"]

log = logging.getLogger(__name__)

_MINUTE = timedelta(minutes=1)


def partition_key(ticker: str, day: date) -> str:
    """The lake-relative key of one ticker's sealed quotes for one day."""
    root = Path("/lake")
    return LakePaths(root).quotes_partition_path(ticker, day).relative_to(root).as_posix()


def is_listed(
    root: LakeRoot, day: date, instruments: Sequence[InstrumentId], master: SecurityMaster
) -> bool:
    """Whether the decision's manifest lists any instrument's sealed quotes for the day.

    An instrument the master gives no one ticker for counts for neither read,
    and is a fault of whichever the others choose.
    """
    for instrument in instruments:
        tickers = master.tickers(instrument, day)
        if len(tickers) == 1 and partition_key(tickers[0], day) in root.entries:
            return True
    return False


def read_sealed(
    mirror: Mirror,
    root: LakeRoot,
    session: Session,
    at: datetime,
    limits: Mapping[InstrumentId, QuoteLimits],
    *,
    master: SecurityMaster,
    kinds: QuoteKinds,
    clock: Clock,
    report: Callable[[], None],
) -> QuoteRead:
    """One quote of each instrument in `limits`, all of one capture cycle by the cut.

    The cut is `CAPTURE_LAG` before `at`. `report` is called after each call of
    `load_quotes` and of the mirror's, so a long read keeps its worker's
    heartbeat.
    """
    cut = at - CAPTURE_LAG
    reader = _Reader(mirror, root, session, cut, report)
    walk = Walk(session.day, at, limits, reader.read, Words(cut, "in", _parts))
    reader.prepare(walk, limits, master, minutes(session, cut))
    cycle = walk.find() if not walk.faults else None
    read_at = clock.now()
    quotes = walk.quotes(cycle, kinds) if cycle is not None else {}
    return QuoteRead(
        quotes=quotes,
        sealed=True,
        segments=(),
        partitions=tuple(sorted(reader.partitions)),
        rows=walk.rows(cycle if quotes else None),
        faults=tuple(walk.faults),
        read_at=read_at,
        listed=0,
        calls=reader.calls,
        cycle_age=None if cycle is None else at - cycle,
    )


@dataclass(frozen=True)
class StatedClose:
    """What one sealed day states of the session before's close.

    `values` holds each distinct `close_price` of its data rows at the equity
    close, as received and sorted, and is empty where it holds no such row.
    `fault` is why the day could not be read, and then `values` is empty.
    """

    key: str
    sha256: str | None
    values: tuple[str, ...]
    fault: str | None


def stated_close(mirror: Mirror, root: LakeRoot, ticker: str, day: date) -> StatedClose | None:
    """Every `close_price` the sealed day of `ticker` on `day` states at its equity close.

    `None` is a day whose partition the manifest does not list. The caller
    resolves the ticker through the security master.
    """
    key = partition_key(ticker, day)
    if key not in root.entries:
        return None
    where = f"{key}, which the record of splits reads for the close of the session before,"
    try:
        mirror.partitions(root, [key])
    except Halt as halt:
        return StatedClose(key, None, (), f"{where} could not be linked: {halt.reason}")
    except MemoryError:
        raise
    except Exception as error:
        log.error("data's link of %s failed", key, exc_info=error)
        return StatedClose(key, None, (), f"{where} could not be linked: {type(error).__name__}")
    sha = root.files[key].sha256
    try:
        table = load_quotes(ticker, day, None, lake_root=root.path)
        held = table.column("close_price").to_pylist()
    except SnapAbsent as error:
        if error.unreadable:
            return StatedClose(
                key, sha, (), f"{where} has {len(error.unreadable)} snap_ts values it cannot read"
            )
        return StatedClose(key, sha, (), None)
    except NoSpotClose:
        # No data row at the equity close, which a failed capture's gap row records too.
        return StatedClose(key, sha, (), None)
    except PartitionQuarantined:
        return StatedClose(key, sha, (), f"{where} is quarantined in marketlake")
    except MemoryError:
        raise
    except Exception as error:
        log.error("data's read of %s at the equity close failed", key, exc_info=error)
        return StatedClose(key, sha, (), f"{where} could not be read: {type(error).__name__}")
    values = sorted({repr(v) if isinstance(v, float) else str(v) for v in held if v is not None})
    return StatedClose(key, sha, tuple(values), None)


def minutes(session: Session, cut: datetime) -> tuple[str, ...]:
    """Each minute from the cut's own back to the one the session opens, as UTC instants.

    The minutes are counted in New York time, from the session's own
    `utc_offset`, so a winter day and a half day count the way marketlake
    stamps them.
    """
    local, opened = cut + session.utc_offset, session.open + session.utc_offset
    minute = local.replace(second=0, microsecond=0)
    out = []
    while minute >= opened:
        out.append((minute - session.utc_offset).astimezone(UTC).isoformat())
        minute -= _MINUTE
    return tuple(out)


def _parts(leg: Leg) -> str:
    said = f"{len(leg.parts)} minutes to read in {leg.source}, {leg.opened} of them read"
    if leg.opened:
        said += f", from {leg.parts[0]} back to {leg.parts[leg.opened - 1]}"
    return said


class _Reader:
    """Links one decision's partitions, and reads their minutes."""

    def __init__(
        self,
        mirror: Mirror,
        root: LakeRoot,
        session: Session,
        cut: datetime,
        report: Callable[[], None],
    ) -> None:
        self._mirror = mirror
        self._root = root
        self._session = session
        self._day = session.day
        self._cut = cut
        self._report = report
        self.partitions: set[str] = set()
        self.calls = 0

    def prepare(
        self,
        walk: Walk,
        limits: Mapping[InstrumentId, QuoteLimits],
        master: SecurityMaster,
        parts: Sequence[str],
    ) -> None:
        """Give each instrument its leg, link each listed partition, and check each close."""
        for instrument in limits:
            try:
                ticker = master.ticker(instrument, self._day)
            except Halt as halt:
                walk.faults.append(halt.reason)
                continue
            walk.legs.append(Leg(instrument, ticker, partition_key(ticker, self._day), parts))
        listed = [leg.source for leg in walk.legs if leg.source in self._root.entries]
        if unlisted := [leg.source for leg in walk.legs if leg.source not in listed]:
            walk.faults.append(
                f"the manifest lists {', '.join(listed)} and not {', '.join(unlisted)}, and a "
                "sealed read never opens a segment"
            )
        for key in listed:
            try:
                self._link(key)
            except Fault as fault:
                walk.faults.append(str(fault))
        if walk.faults:
            return
        for leg in walk.legs:
            try:
                self._close(leg)
            except Fault as fault:
                walk.faults.append(str(fault))

    def _link(self, key: str) -> None:
        try:
            self._mirror.partitions(self._root, [key])
        except Halt as halt:
            raise Fault(halt.reason) from None
        except MemoryError:
            raise
        except Exception as error:
            log.error("data's link of %s failed", key, exc_info=error)
            raise Fault(f"{key} could not be linked: {type(error).__name__}") from None
        finally:
            self._report()
        self.partitions.add(vintage_id(self._root.files[key]))

    def _close(self, leg: Leg) -> None:
        """Fault unless the partition holds a row of the session's equity close.

        That row is a data row, or a gap row tagged `spot_close`, which records
        a close whose capture failed. The runtime substitutes no close price for
        it, and reads no price at the close anyway.
        """
        none = f"{leg.source} holds no row of the session's equity close"
        try:
            found = self._load(leg, None, "at the equity close")
        except _NoClose as missing:
            if not missing.tagged_gaps:
                raise Fault(none) from None
            log.info(
                "%s's equity close is %d gap rows tagged spot_close, so it was sealed after "
                "the close",
                leg.source,
                missing.tagged_gaps,
            )
            return
        if found is None:
            raise Fault(none)

    def read(self, leg: Leg, parts: Sequence[str]) -> Iterator[pa.Table]:
        """The rows of each minute captured by the cut, newest minute first.

        The rows a minute kept are given before its ticker is checked, so a
        fault there still stores them.
        """
        for minute in parts:
            where = f"at {minute}"
            table = self._load(leg, minute, where)
            if table is None:
                continue
            try:
                kept = captured_by(table, self._cut)
            except MemoryError:
                raise
            except Exception as error:
                raise _failed(leg, where, error) from None
            yield kept
            try:
                held = kept.column("ticker").to_pylist()
            except MemoryError:
                raise
            except Exception as error:
                raise _failed(leg, where, error) from None
            if wrong := sorted({repr(t) for t in held if t != leg.ticker}):
                raise Fault(f"{leg.source} holds a row of ticker {', '.join(wrong)} {where}")

    def _load(self, leg: Leg, minute: str | None, where: str) -> pa.Table | None:
        """marketlake's answer at a minute, or at the equity close.

        `None` is no row at the minute. No data row at the equity close raises
        `_NoClose`, with marketlake's count of gap rows tagged `spot_close`.
        """
        snap = None if minute is None else self._hhmm(minute)
        self.calls += 1
        try:
            return load_quotes(leg.ticker, self._day, snap, lake_root=self._root.path)
        except SnapAbsent as error:
            if error.unreadable:
                raise Fault(
                    f"{leg.source} has no row {where}, and {len(error.unreadable)} snap_ts "
                    "values in it cannot be read"
                ) from None
            return None
        except NoSpotClose as error:
            gaps = getattr(error, "tagged_gaps", None)
            if minute is None and type(gaps) is int and gaps >= 0:
                raise _NoClose(gaps) from None
            raise _failed(leg, where, error) from None
        except PartitionQuarantined:
            raise Fault(f"{leg.source} is quarantined in marketlake") from None
        except MemoryError:
            raise
        except Exception as error:
            raise _failed(leg, where, error) from None
        finally:
            self._report()

    def _hhmm(self, minute: str) -> str:
        local = datetime.fromisoformat(minute) + self._session.utc_offset
        return local.strftime("%H:%M")


class _NoClose(Exception):
    """No data row at the equity close, with how many gap rows tagged `spot_close` record it."""

    def __init__(self, tagged_gaps: int) -> None:
        super().__init__(tagged_gaps)
        self.tagged_gaps = tagged_gaps


def _failed(leg: Leg, where: str, error: Exception) -> Fault:
    """An error of the read, by its type, with its words in the log alone.

    Several of marketlake's errors hold the lake root's path, which is a path of
    the machine's, and a reason is stored.
    """
    log.error("data's read of %s %s failed", leg.source, where, exc_info=error)
    return Fault(f"{leg.source} could not be read {where}: {type(error).__name__}")
