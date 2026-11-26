"""The read of one decision's quotes, from the capture segments of a day marketlake has not sealed.

A segment is the file one capture cycle writes for one ticker, and marketlake's
README publishes how to read one in its `## Read an unsealed day`.
docs/architecture/03-context-and-scope.md's `### Reading the day before it is
sealed` says why the read is the shape it is. How it goes, in order.

1. **Each instrument's segments are listed once**, under the prefix
   `lake.unsealed.segment_prefix` gives for its ticker on the session's day. A
   listing the bucket refuses or does not answer is tried once more. Where an
   instrument lists nothing, nothing is opened.
2. **The walk of `cycles` takes the listing as a leg, newest first**, and asks
   for five segments at a time. Each five are downloaded oldest first.
3. **marketlake's reader is called once for each instrument and five segments**,
   with the SHA-256 the bucket stored with each, and its verdict is the check of
   the bytes. It gives the rows captured by `cycles.CAPTURE_LAG` before the
   decision, the cut the sealed read takes too.

Nothing here raises for what a call to the bucket meets, or marketlake's reader
or a row holds.
Each fault becomes a reason beside what was read, so the snapshot that is
stored before the halt holds every row a call to the reader returned. A fault
ends the read once every instrument's turn in that round is done, so one
instrument's refusal loses none of another's rows.

A segment marketlake's reader or the bucket cannot vouch for is downloaded once
more, at the same version where the first answer named one, and a second fault
of it is a fault of the read. The table in that page's `##### What the read does
with each answer` lists every fault, and what ends the halt it causes.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime
from pathlib import Path

import pyarrow as pa
from lake.unsealed import (
    SchemaVersionUnknown,
    SegmentAbsent,
    SegmentMismatch,
    UnsealedRefused,
    load_unsealed_quotes,
    segment_prefix,
)

from ithildinruntime.core import (
    BlobMissing,
    Bucket,
    BucketObject,
    Clock,
    Halt,
    InstrumentId,
    Segment,
    Session,
    StoreError,
)

from .cycles import BATCH, CAPTURE_LAG, COLUMNS, Fault, Leg, QuoteLimits, QuoteRead, Walk, Words
from .reference import QuoteKinds, SecurityMaster

__all__ = ["BATCH", "COLUMNS", "QuoteLimits", "QuoteRead", "read_quotes"]

log = logging.getLogger(__name__)


def read_quotes(
    live: Bucket,
    root: Path,
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

    The cut is `CAPTURE_LAG` before `at`.

    `root` is the decision's lake root, which already holds the reader files the
    mirror checked. `report` is called after each call to the bucket and to
    marketlake's reader, so a long read keeps its worker's heartbeat.
    """
    opener = _Opener(live, root, session, at, report)
    words = Words(
        at - CAPTURE_LAG,
        "under",
        lambda leg: f"{len(leg.parts)} segments under {leg.source}, {leg.opened} of them opened",
    )
    walk = Walk(session.day, at, limits, opener.read, words)
    opener.list_all(walk, limits, master)
    cycle = walk.find() if not walk.faults else None
    read_at = clock.now()
    quotes = walk.quotes(cycle, kinds) if cycle is not None else {}
    return QuoteRead(
        quotes=quotes,
        sealed=False,
        segments=tuple(sorted(opener.segments.values(), key=lambda s: s.key)),
        partitions=(),
        rows=walk.rows(cycle if quotes else None),
        faults=tuple(walk.faults),
        read_at=read_at,
        listed=sum(len(leg.parts) for leg in walk.legs),
        calls=0,
        cycle_age=None if cycle is None else at - cycle,
    )


class _Opener:
    """Lists, downloads and reads one decision's segments."""

    def __init__(
        self,
        live: Bucket,
        root: Path,
        session: Session,
        at: datetime,
        report: Callable[[], None],
    ) -> None:
        self._live = live
        self._root = root
        self._day = session.day
        self._at = at
        self._report = report
        # Each segment that was downloaded once more, which is the most any is, and why.
        self._again: dict[str, str] = {}
        self.segments: dict[str, Segment] = {}

    # Listing

    def list_all(
        self, walk: Walk, limits: Mapping[InstrumentId, QuoteLimits], master: SecurityMaster
    ) -> None:
        for instrument in limits:
            try:
                ticker = master.ticker(instrument, self._day)
                leg = Leg(instrument, ticker, segment_prefix(ticker, self._day))
                walk.legs.append(leg)
                leg.parts = tuple(reversed(self._listed(leg.source)))
            except Halt as halt:
                walk.faults.append(halt.reason)
            except UnsealedRefused as error:
                walk.faults.append(f"{instrument} has no segments to list: {error}")
            except Fault as fault:
                walk.faults.append(str(fault))

    def _listed(self, prefix: str) -> Sequence[str]:
        for last in (False, True):
            try:
                return self._live.list(prefix)
            except StoreError as error:
                if last:
                    raise Fault(f"{prefix} could not be listed: {_said(error)}") from None
                log.warning("data lists %s once more, because: %s", prefix, _said(error))
            except Exception as error:
                raise Fault(f"{prefix} could not be listed: {_named(prefix, error)}") from None
            finally:
                self._report()
        raise AssertionError("the second try returns or raises")

    # Opening

    def read(self, leg: Leg, parts: Sequence[str]) -> Iterator[pa.Table]:
        """One table of some segments of one instrument, downloaded oldest first."""
        keys = parts[::-1]
        for key in keys:
            self._download(key)
        yield self._door(leg, keys)

    def _download(self, key: str) -> None:
        """Put one segment in the lake root, keeping the version and SHA-256 the bucket gave it."""
        got = self._got(key, None)
        for what, value in (("version", got.version), ("stored SHA-256", got.sha256)):
            if value is None:
                raise self._fault(key, f"has no {what} in the bucket")
        self._write(key, got.data)
        self.segments[key] = Segment(key, got.version, got.sha256)  # type: ignore[arg-type]

    def _got(self, key: str, version: str | None) -> BucketObject:
        while True:
            try:
                return self._live.read(key, version)
            except BlobMissing as error:
                said = f"is listed and the bucket holds none: {error.answer}"
                raise self._fault(key, said) from None
            except StoreError as error:
                self._once_more(key, f"could not be downloaded: {_said(error)}")
            except Exception as error:
                raise self._fault(key, f"could not be downloaded: {_named(key, error)}") from None
            finally:
                self._report()

    def _once_more(self, key: str, said: str) -> None:
        """Let one segment be downloaded again, once."""
        if key in self._again:
            raise self._fault(key, said)
        self._again[key] = said
        log.warning("data downloads %s once more, because it %s", key, said)

    def _fault(self, key: str, said: str) -> Fault:
        """What ends the read at one segment, with what its first try met where it had two."""
        if (first := self._again.get(key)) is None:
            return Fault(f"{key} {said}")
        return Fault(f"{key} failed twice. First it {first}, and then it {said}")

    def _write(self, key: str, data: bytes) -> None:
        path = self._root.joinpath(*key.split("/"))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except (OSError, ValueError) as error:
            raise self._fault(key, f"could not be written: {type(error).__name__}") from None

    def _door(self, leg: Leg, keys: Sequence[str]) -> pa.Table:
        """marketlake's reader over these segments, with a second download of one it refuses."""
        where = f"{leg.ticker} on {self._day.isoformat()}"
        while True:
            try:
                return load_unsealed_quotes(
                    leg.ticker,
                    self._day,
                    self._at - CAPTURE_LAG,
                    segments={key: self.segments[key].sha256 for key in keys},
                    lake_root=self._root,
                )
            except SchemaVersionUnknown as error:
                raise self._fault(
                    error.segment,
                    f"holds rows at schema version {', '.join(map(str, error.versions))}, which "
                    "the pinned marketlake does not read, so the marketlake pin is raised",
                ) from None
            except SegmentMismatch as error:
                key = error.segment
                said = f"hashed to {error.actual}, where the bucket stored {error.expected}"
            except UnsealedRefused as error:
                key = error.segment
                said = f"was refused by marketlake's reader: {_said(error)}"
                if key not in self.segments:
                    raise Fault(f"{key or where} {said}") from None
                if not isinstance(error, SegmentAbsent):
                    raise self._fault(key, said) from None
            except OSError as error:
                # Its text can hold an absolute path, and this goes into a stored reason.
                raise Fault(f"{where} could not be read: {type(error).__name__}") from None
            finally:
                self._report()
            self._once_more(key, said)
            self._write(key, self._got(key, self.segments[key].version).data)


def _said(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _named(where: str, error: Exception) -> str:
    """An error that is no answer of the bucket's, by its type, with its words in the log alone.

    Such an error can be the client's, such as refreshed credentials that are
    still expired, and its words can hold a path of the machine's.
    """
    log.error("data's call to the bucket for %s failed", where, exc_info=error)
    return type(error).__name__
