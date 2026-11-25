"""The read of one decision's quotes, from the capture segments of a day marketlake has not sealed.

A segment is the file one capture cycle writes for one ticker, and marketlake's
README publishes how to read one in its `## Read an unsealed day`.
docs/architecture/03-context-and-scope.md's `### Reading the day before it is
sealed` says why the read is the shape it is. How it goes, in order.

1. **Each instrument's segments are listed once**, under the prefix
   `lake.unsealed.segment_prefix` gives for its ticker on the session's day. A
   listing the bucket does not answer is tried once more. Where an instrument
   lists nothing, nothing is opened.
2. **The newest five of each are opened, and five more at a time**, until some
   instant is found at which every instrument has a row. An instrument has no
   more to open once its listing ends, or once five of its own returned at least
   one row and every one was captured before the decision by more than its
   maximum age, since the judge refuses such a quote and any older one. Each
   instrument stops alone, because stopping them all at the first one can lose a
   cycle they share.
3. **marketlake's reader is called once for each instrument and five segments**,
   with the SHA-256 the bucket stored with each, and its verdict is the check of
   the bytes. It gives the rows captured by the decision time.
4. **The cycle is the newest instant at which every instrument has a row.** The
   rows of one cycle share one `fetch_end_ts`, the instant the response landed.
   An older cycle they all have is taken over a newer one only some have.

Nothing here raises for what the bucket, marketlake's reader or a row holds.
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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

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
    CodecError,
    Halt,
    InstrumentId,
    Quote,
    Segment,
    Session,
    StoreError,
    exceeds_places,
    from_received,
    maximum_faults,
    places,
)
from ithildinruntime.core.codec import INT_LIMIT

from .reference import QuoteKind, QuoteKinds, SecurityMaster

__all__ = ["BATCH", "COLUMNS", "QuoteLimits", "QuoteRead", "read_quotes"]

log = logging.getLogger(__name__)

# How many segments of one instrument each round opens.
BATCH = 5

# The columns marketlake's README publishes, at the types it publishes. Its
# reader checks five of them and gives the other sixteen as a segment holds them.
COLUMNS: Mapping[str, pa.DataType] = {
    "snap_ts": pa.string(),
    "fetch_ts": pa.string(),
    "fetch_end_ts": pa.string(),
    "vendor_quote_ts": pa.string(),
    "bid_time": pa.int64(),
    "ask_time": pa.int64(),
    "trade_time": pa.int64(),
    "ticker": pa.string(),
    "bid": pa.float64(),
    "ask": pa.float64(),
    "last": pa.float64(),
    "bid_size": pa.int64(),
    "ask_size": pa.int64(),
    "last_size": pa.int64(),
    "total_volume": pa.int64(),
    "security_status": pa.string(),
    "realtime": pa.bool_(),
    "close_tag": pa.string(),
    "session_phase": pa.string(),
    "row_kind": pa.string(),
    "schema_version": pa.int64(),
}

_PRICES = ("bid", "ask", "last")
_PLACES = places(Quote, "bid")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_SECOND = 1_000_000
# What pyarrow raises for a value Python has no form of.
_UNREADABLE = (ValueError, OverflowError, pa.ArrowException)


@dataclass(frozen=True)
class QuoteLimits:
    """One instrument's written maxima: an age in seconds, and a share of the midpoint."""

    max_age: int
    max_spread_share: Decimal

    def __post_init__(self) -> None:
        if faults := maximum_faults(self.max_age, self.max_spread_share):
            raise ValueError("; ".join(faults))

    def __str__(self) -> str:
        return f"{self.max_age} seconds and {self.max_spread_share}"


@dataclass(frozen=True)
class QuoteRead:
    """What one decision's read of its quotes gave, and how much it read.

    - `quotes` holds one quote of every instrument, or none at all.
    - `segments` names each segment opened, by key.
    - `rows` holds every row marketlake's reader returned that is not a
      quote's own, in the order it was read.
    - `faults` holds each reason the read failed, in order.
    - `read_at` is the clock after the bucket's last answer.
    - `listed` counts the segments listed, and `cycle_age` is how long before
      the decision the newest capture every instrument has was made, or `None`
      where the read found none.
    """

    quotes: Mapping[InstrumentId, Quote]
    segments: tuple[Segment, ...]
    rows: Mapping[InstrumentId, tuple[Mapping[str, str | None], ...]]
    faults: tuple[str, ...]
    read_at: datetime
    listed: int
    cycle_age: timedelta | None


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
    """One quote of each instrument in `limits`, all of one capture cycle by `at`.

    `root` is the decision's lake root, which already holds the reader files the
    mirror checked. `report` is called after each call to the bucket and to
    marketlake's reader, so a long read keeps its worker's heartbeat.
    """
    walk = _Walk(live, root, session, at, limits, report)
    walk.list_all(master)
    cycle = walk.find() if not walk.faults else None
    read_at = clock.now()
    quotes = walk.quotes(cycle, kinds) if cycle is not None else {}
    return QuoteRead(
        quotes=quotes,
        segments=tuple(sorted(walk.segments.values(), key=lambda s: s.key)),
        rows=walk.rows(cycle if quotes else None),
        faults=tuple(walk.faults),
        read_at=read_at,
        listed=sum(len(leg.keys) for leg in walk.legs),
        cycle_age=None if cycle is None else at - cycle,
    )


class _Fault(Exception):
    """A reason the read fails. It never leaves this module."""


@dataclass
class _Leg:
    """One instrument's listing, and the rows read of it so far."""

    instrument: InstrumentId
    ticker: str
    prefix: str
    keys: Sequence[str] = ()
    # How many of the oldest keys are still to open.
    left: int = 0
    opened: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    # Each capture instant, with the places of its rows in `rows`.
    captured: dict[datetime, list[int]] = field(default_factory=dict)


class _Walk:
    def __init__(
        self,
        live: Bucket,
        root: Path,
        session: Session,
        at: datetime,
        limits: Mapping[InstrumentId, QuoteLimits],
        report: Callable[[], None],
    ) -> None:
        self._live = live
        self._root = root
        self._day = session.day
        self._at = at
        self._limits = limits
        self._report = report
        # Each segment that was downloaded once more, which is the most any is, and why.
        self._again: dict[str, str] = {}
        self.legs: list[_Leg] = []
        self.segments: dict[str, Segment] = {}
        self.faults: list[str] = []

    # Listing

    def list_all(self, master: SecurityMaster) -> None:
        for instrument in self._limits:
            try:
                ticker = master.ticker(instrument, self._day)
                leg = _Leg(instrument, ticker, segment_prefix(ticker, self._day))
                self.legs.append(leg)
                leg.keys = self._listed(leg.prefix)
                leg.left = len(leg.keys)
            except Halt as halt:
                self.faults.append(halt.reason)
            except UnsealedRefused as error:
                self.faults.append(f"{instrument} has no segments to list: {error}")
            except _Fault as fault:
                self.faults.append(str(fault))
        if not self.faults and not all(leg.keys for leg in self.legs):
            self.faults.append(self._no_cycle())

    def _listed(self, prefix: str) -> Sequence[str]:
        for last in (False, True):
            try:
                return self._live.list(prefix)
            except StoreError as error:
                if last:
                    raise _Fault(f"{prefix} could not be listed: {_said(error)}") from None
                log.warning("data lists %s once more, because: %s", prefix, _said(error))
            except ValueError as error:
                raise _Fault(f"{prefix} could not be listed: {_said(error)}") from None
            finally:
                self._report()
        raise AssertionError("the second try returns or raises")

    # Opening

    def find(self) -> datetime | None:
        """Open segments until every instrument has a row at one instant, the newest such."""
        while True:
            for leg in self.legs:
                try:
                    self._open(leg)
                except _Fault as fault:
                    self.faults.append(str(fault))
            if self.faults:
                return None
            shared = set(self.legs[0].captured).intersection(*(leg.captured for leg in self.legs))
            if shared:
                return max(shared)
            if not any(leg.left for leg in self.legs):
                self.faults.append(self._no_cycle())
                return None

    def _open(self, leg: _Leg) -> None:
        """The newest five segments of one instrument that are still to open."""
        keys = leg.keys[max(0, leg.left - BATCH) : leg.left]
        if not keys:
            return
        leg.left -= len(keys)
        leg.opened += len(keys)
        for key in keys:
            self._download(key)
        table = self._door(leg, keys)
        captured = self._take(leg, table)
        age = self._limits[leg.instrument].max_age * _SECOND
        if captured and all((self._at - when) // _MICROSECOND > age for when in captured):
            leg.left = 0

    def _take(self, leg: _Leg, table: pa.Table) -> list[datetime]:
        """Keep every row of one answer, then check its columns and read its capture instants."""
        where = f"{leg.ticker} on {self._day.isoformat()}"
        names = table.schema.names
        rows = [
            dict(zip(names, values, strict=True))
            for values in zip(*(_values(column) for column in table.columns), strict=True)
        ]
        first = len(leg.rows)
        leg.rows.extend(rows)
        for name, kind in COLUMNS.items():
            place = table.schema.get_field_index(name)
            if place >= 0 and (found := table.schema.field(place).type) != kind:
                raise _Fault(f"{where} has {name} as {found}, and marketlake publishes {kind}")
        captured = []
        for n, row in enumerate(rows, first):
            when = _instant(row["fetch_end_ts"])
            if when is None:
                raise _Fault(f"{where} has a fetch_end_ts {row['fetch_end_ts']!r} that is no time")
            leg.captured.setdefault(when, []).append(n)
            captured.append(when)
        return captured

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
            except ValueError as error:
                raise _Fault(f"{key} could not be downloaded: {_said(error)}") from None
            finally:
                self._report()

    def _once_more(self, key: str, said: str) -> None:
        """Let one segment be downloaded again, once."""
        if key in self._again:
            raise self._fault(key, said)
        self._again[key] = said
        log.warning("data downloads %s once more, because it %s", key, said)

    def _fault(self, key: str, said: str) -> _Fault:
        """What ends the read at one segment, with what its first try met where it had two."""
        if (first := self._again.get(key)) is None:
            return _Fault(f"{key} {said}")
        return _Fault(f"{key} failed twice. First it {first}, and then it {said}")

    def _write(self, key: str, data: bytes) -> None:
        path = self._root.joinpath(*key.split("/"))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except (OSError, ValueError) as error:
            raise _Fault(f"{key} could not be written: {type(error).__name__}") from None

    def _door(self, leg: _Leg, keys: Sequence[str]) -> pa.Table:
        """marketlake's reader over these segments, with a second download of one it refuses."""
        where = f"{leg.ticker} on {self._day.isoformat()}"
        while True:
            try:
                return load_unsealed_quotes(
                    leg.ticker,
                    self._day,
                    self._at,
                    segments={key: self.segments[key].sha256 for key in keys},
                    lake_root=self._root,
                )
            except SchemaVersionUnknown as error:
                raise _Fault(
                    f"{error.segment} holds rows at schema version "
                    f"{', '.join(map(str, error.versions))}, which the pinned marketlake does not "
                    "read, so the marketlake pin is raised"
                ) from None
            except SegmentMismatch as error:
                key = error.segment
                said = f"hashed to {error.actual}, where the bucket stored {error.expected}"
            except UnsealedRefused as error:
                key = error.segment
                said = f"was refused by marketlake's reader: {_said(error)}"
                if key not in self.segments:
                    raise _Fault(f"{key or where} {said}") from None
                if not isinstance(error, SegmentAbsent):
                    raise self._fault(key, said) from None
            except OSError as error:
                # Its text can hold an absolute path, and this goes into a stored reason.
                raise _Fault(f"{where} could not be read: {type(error).__name__}") from None
            finally:
                self._report()
            self._once_more(key, said)
            self._write(key, self._got(key, self.segments[key].version).data)

    # Picking

    def quotes(self, cycle: datetime, kinds: QuoteKinds) -> dict[InstrumentId, Quote]:
        """One quote of every instrument at `cycle`, or none and a fault."""
        for leg in self.legs:
            if len(leg.captured[cycle]) > 1:
                self.faults.append(
                    f"{leg.instrument} has {len(leg.captured[cycle])} rows captured at "
                    f"{cycle.isoformat()}, under {leg.prefix}"
                )
        try:
            kind = kinds.kind(self._day)
        except Halt as halt:
            self.faults.append(halt.reason)
        if self.faults:
            return {}
        return {
            leg.instrument: _quote(
                leg.instrument,
                leg.rows[leg.captured[cycle][0]],
                cycle,
                kind,
                self._limits[leg.instrument],
            )
            for leg in self.legs
        }

    def rows(
        self, cycle: datetime | None
    ) -> dict[InstrumentId, tuple[Mapping[str, str | None], ...]]:
        """Every row returned but the ones the quotes of `cycle` hold."""
        out = {}
        for leg in self.legs:
            own = [] if cycle is None else leg.captured[cycle]
            kept = tuple(_received(row) for n, row in enumerate(leg.rows) if n not in own)
            if kept:
                out[leg.instrument] = kept
        return out

    def _no_cycle(self) -> str:
        """Why no quote was read: what each prefix listed, and how far each instrument's rows go."""
        each = []
        for leg in self.legs:
            newest = max(leg.captured, default=None)
            each.append(
                f"{leg.instrument} has {len(leg.keys)} segments under {leg.prefix}, "
                f"{leg.opened} of them opened, and "
                + (f"its newest row captured at {newest.isoformat()}" if newest else "no row read")
            )
        return (
            f"no capture by {self._at.isoformat()} holds a row of every instrument: "
            + "; ".join(each)
        )


def _said(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"


def _values(column: pa.ChunkedArray) -> list[Any]:
    """One column's values, with a note in place of one Python cannot hold, so no row is lost.

    marketlake's reader leaves most columns' types alone, and a retyped one can
    hold a value past any of Python's, such as a time past its last year. The
    segment it came from is named by its version and SHA-256.
    """
    try:
        return column.to_pylist()
    except _UNREADABLE:
        return [_value(scalar) for scalar in column]


def _value(scalar: pa.Scalar) -> Any:
    try:
        return scalar.as_py()
    except _UNREADABLE:
        return f"a {scalar.type} that cannot be read"


def _instant(text: object) -> datetime | None:
    """The UTC instant a stamp names, or `None` where it names none."""
    if not isinstance(text, str):
        return None
    try:
        stamped = datetime.fromisoformat(text)
        return None if stamped.utcoffset() is None else stamped.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _milliseconds(value: int | None) -> datetime | None:
    """The UTC instant of epoch milliseconds, or `None` where they are null or past any date."""
    if value is None:
        return None
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except OverflowError:
        return None


def _price(value: float | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return from_received(value)
    except CodecError:
        return None


def _received(row: Mapping[str, Any]) -> dict[str, str | None]:
    """A row as text, by column."""
    return {
        name: None if value is None else repr(value) if isinstance(value, float) else str(value)
        for name, value in row.items()
    }


def _quote(
    instrument: InstrumentId,
    row: Mapping[str, Any],
    captured_at: datetime,
    kind: QuoteKind,
    limits: QuoteLimits,
) -> Quote:
    prices = {name: _price(row.get(name)) for name in _PRICES}
    volume = row.get("total_volume")
    return Quote(
        instrument=instrument,
        **prices,
        volume=volume if volume is not None and -INT_LIMIT < volume < INT_LIMIT else None,
        bid_at=_milliseconds(row.get("bid_time")),
        ask_at=_milliseconds(row.get("ask_time")),
        last_at=_milliseconds(row.get("trade_time")),
        fetched_at=_instant(row.get("fetch_ts")),
        captured_at=captured_at,
        realtime=row.get("realtime"),
        status=row.get("security_status"),
        price_kind=kind.price_kind,
        last_kind=kind.last_kind,
        venues=kind.venues,
        volume_counted=kind.volume_counted,
        normal_status=kind.normal_status,
        max_age=limits.max_age,
        max_spread_share=limits.max_spread_share,
        received=_received(row),
        excess_places=tuple(
            name
            for name, price in prices.items()
            if price is not None and exceeds_places(price, _PLACES)
        ),
    )
