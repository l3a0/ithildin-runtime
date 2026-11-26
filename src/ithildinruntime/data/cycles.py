"""The one walk over a day's capture cycles, the pick of one, and its conversion to quotes.

Two reads of a decision's quotes share it. `segments` reads the capture
segments of a day marketlake has not sealed, and `sealed` the minutes of a day
it has. docs/architecture/03-context-and-scope.md's `#### Which read a decision
uses` says which one a decision takes. Each read gives every instrument a leg,
which is a list of parts newest first, and a function that reads some of them.
For segments a part is a key, and for a sealed day it is a minute.

How the walk goes, in order.

1. **Every read cuts at `CAPTURE_LAG` before its decision.** A row whose
   capture landed after the cut is read by neither read, and is stored nowhere.
   The judge, the stop and the ages below still read the decision time.
2. **The newest five parts of each leg are read, and five more at a time**,
   until some instant is the capture of a row of every instrument. A leg has no
   more once its parts end, or once five of its own gave at least one row and
   every one was captured before the decision by more than its maximum age,
   since the judge refuses such a quote and any older one. Each leg stops alone,
   because stopping them all at the first one can lose a cycle they share.
3. **The cycle is the newest instant at which every instrument has a row.** The
   rows of one cycle share one `fetch_end_ts`, the instant the response landed.
   An older cycle they all have is taken over a newer one only some have.
4. **A fault in a round ends the walk once every leg's turn in it is done**, so
   one instrument's refusal loses none of another's rows.

Reading five parts at a time, and not one, is what makes the two reads agree.
A cycle that landed after a later minute's sits in the newest five segments,
and a read a minute at a time would miss it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pyarrow as pa

from ithildinruntime.core import (
    CodecError,
    Halt,
    InstrumentId,
    Quote,
    Segment,
    exceeds_places,
    from_received,
    maximum_faults,
    places,
)
from ithildinruntime.core.codec import INT_LIMIT

from .reference import QuoteKind, QuoteKinds

__all__ = [
    "BATCH",
    "CAPTURE_LAG",
    "COLUMNS",
    "Fault",
    "Leg",
    "QuoteLimits",
    "QuoteRead",
    "Walk",
    "Words",
]

# How many parts of one instrument each round reads.
BATCH = 5

# How long a capture can take to reach the bucket. Every read of quotes keeps
# only rows whose capture landed by this long before its decision, sealed or of
# segments, in every mode, so a backtest reads what live reads. It is a fact
# about the capture, so no strategy writes its own. A change to it is reviewed as
# a raised maximum age is. docs/architecture/03-context-and-scope.md's `##### Why
# every read cuts 15 seconds before its decision` says what bounds it and what it
# costs.
CAPTURE_LAG = timedelta(seconds=15)

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
    - `sealed` says which read it was.
    - `segments` names each segment opened, by key, and `partitions` each
      partition linked, by its vintage id.
    - `rows` holds every row kept that is not a quote's own, in the order read.
    - `faults` holds each reason the read failed, in order.
    - `read_at` is the clock after the last answer.
    - `listed` counts the segments listed and `calls` the calls of `load_quotes`.
    - `cycle_age` is how long before the decision the newest capture every
      instrument has was made, or `None` where the read found none.
    """

    quotes: Mapping[InstrumentId, Quote]
    sealed: bool
    segments: tuple[Segment, ...]
    partitions: tuple[str, ...]
    rows: Mapping[InstrumentId, tuple[Mapping[str, str | None], ...]]
    faults: tuple[str, ...]
    read_at: datetime
    listed: int
    calls: int
    cycle_age: timedelta | None


class Fault(Exception):
    """A reason the read fails. It never leaves the read."""


@dataclass
class Leg:
    """One instrument's parts, newest first, and the rows read of it so far."""

    instrument: InstrumentId
    ticker: str
    # What its parts are of, such as a segment prefix or a partition's key.
    source: str
    parts: Sequence[str] = ()
    opened: int = 0
    # No more of its parts are read.
    done: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)
    # Each capture instant, with the places of its rows in `rows`.
    captured: dict[datetime, list[int]] = field(default_factory=dict)

    @property
    def left(self) -> int:
        return 0 if self.done else len(self.parts) - self.opened


@dataclass(frozen=True)
class Words:
    """How one read names what it read in a reason."""

    # The latest capture the read keeps.
    by: datetime
    # What stands before a leg's source.
    where: str
    # One leg's parts, and how many were read.
    parts: Callable[[Leg], str]


class Walk:
    """One decision's walk back over its legs' parts.

    `read` gives the rows of some parts of one leg as tables, and raises `Fault`.
    The rows of a table it gave before it raised are kept.
    """

    def __init__(
        self,
        day: date,
        at: datetime,
        limits: Mapping[InstrumentId, QuoteLimits],
        read: Callable[[Leg, Sequence[str]], Iterable[pa.Table]],
        words: Words,
    ) -> None:
        self._day = day
        self._at = at
        self._limits = limits
        self._read = read
        self._words = words
        self.legs: list[Leg] = []
        self.faults: list[str] = []

    def find(self) -> datetime | None:
        """Read parts until every instrument has a row at one instant, the newest such."""
        if not all(leg.parts for leg in self.legs):
            self.faults.append(self._no_cycle())
            return None
        while True:
            for leg in self.legs:
                try:
                    self._open(leg)
                except Fault as fault:
                    self.faults.append(str(fault))
            if self.faults:
                return None
            shared = set(self.legs[0].captured).intersection(*(leg.captured for leg in self.legs))
            if shared:
                return max(shared)
            if not any(leg.left for leg in self.legs):
                self.faults.append(self._no_cycle())
                return None

    def _open(self, leg: Leg) -> None:
        """The newest five parts of one instrument that are still to read."""
        if not leg.left:
            return
        parts = leg.parts[leg.opened : leg.opened + BATCH]
        leg.opened += len(parts)
        captured: list[datetime] = []
        for table in self._read(leg, parts):
            captured.extend(self._take(leg, table))
        age = self._limits[leg.instrument].max_age * _SECOND
        if captured and all((self._at - when) // _MICROSECOND > age for when in captured):
            leg.done = True

    def _take(self, leg: Leg, table: pa.Table) -> list[datetime]:
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
                raise Fault(f"{where} has {name} as {found}, and marketlake publishes {kind}")
        captured = []
        for n, row in enumerate(rows, first):
            when = _instant(row["fetch_end_ts"])
            if when is None:
                raise Fault(f"{where} has a fetch_end_ts {row['fetch_end_ts']!r} that is no time")
            leg.captured.setdefault(when, []).append(n)
            captured.append(when)
        return captured

    def quotes(self, cycle: datetime, kinds: QuoteKinds) -> dict[InstrumentId, Quote]:
        """One quote of every instrument at `cycle`, or none and a fault."""
        for leg in self.legs:
            if len(leg.captured[cycle]) > 1:
                self.faults.append(
                    f"{leg.instrument} has {len(leg.captured[cycle])} rows captured at "
                    f"{cycle.isoformat()}, {self._words.where} {leg.source}"
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
        """Why no quote was read: each leg's parts, and how far each instrument's rows go."""
        each = []
        for leg in self.legs:
            newest = max(leg.captured, default=None)
            each.append(
                f"{leg.instrument} has {self._words.parts(leg)}, and "
                + (f"its newest row captured at {newest.isoformat()}" if newest else "no row read")
            )
        return (
            f"no capture by {self._words.by.isoformat()} holds a row of every instrument: "
            + "; ".join(each)
        )


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
