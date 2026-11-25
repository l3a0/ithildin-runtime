"""The data types the workers pass to each other.

docs/architecture/05-building-blocks.md's `### The data the workers pass`
draws them. Each is a frozen dataclass, so a module cannot change a snapshot
or a book another module reads. A mapping field is typed `Mapping` and a list
field `tuple`, and each is made read-only when the record is built.

No field holds a float. Every price, quantity, amount and statistic is a
`Decimal` that declares its places through `Annotated[Decimal, Places(n)]`.
A field's places never shrink in a later release. A module issue may change a
field its own module owns, and where #22 and that issue disagree, the module
issue wins.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, NewType

from .codec import Places, encode
from .exact import rounded, worth
from .halt import Halt
from .names import is_name, is_sha256

__all__ = [
    "Ack",
    "Bar",
    "BarStatus",
    "Basket",
    "BlobHeader",
    "BlobRef",
    "Book",
    "BrokerAccount",
    "BrokerOrder",
    "BrokerState",
    "BucketObject",
    "CashRead",
    "Constraints",
    "Correction",
    "CostKind",
    "CostSchedule",
    "CostSide",
    "CostTerm",
    "DecisionResult",
    "Fill",
    "FillBasis",
    "FillsRead",
    "Forecast",
    "ForecastSet",
    "FoundOrder",
    "GateInputs",
    "HaltRecord",
    "HardCaps",
    "Heartbeat",
    "Instrument",
    "InstrumentId",
    "JournalAck",
    "JournalEntry",
    "Lease",
    "Limit",
    "LossReading",
    "Mode",
    "Money",
    "OrderEntry",
    "OrderIntent",
    "OrderRecord",
    "OrderState",
    "OrderType",
    "OrdersRead",
    "Outcome",
    "PastLimit",
    "Plan",
    "PlanDecision",
    "PositionsRead",
    "Price",
    "Quantity",
    "Quote",
    "READ_LIMIT",
    "Rate",
    "ReceivedPage",
    "RecordsRead",
    "RestartRecord",
    "SendOutcome",
    "Signal",
    "Sizing",
    "Snapshot",
    "SpikeFlag",
    "Statistic",
    "StopState",
    "StoredBlob",
    "TargetPortfolio",
    "TimeInForce",
    "Valuation",
    "Verdict",
]

InstrumentId = NewType("InstrumentId", str)

# The places each kind of decimal declares, which never shrink in a later
# release. tests/test_core_types.py pins each one.
# - A price carries four, the finest tick US equities quote below a dollar.
# - A quantity carries nine, because Alpaca takes fractional shares to nine.
# - An amount of money carries four, so a sub-cent fee survives.
# - A rate per share or per dollar and a statistic carry eight, for a hedge
#   ratio or a fee rate finer than a cent.
_MONEY_PLACES = 4
Price = Annotated[Decimal, Places(4)]
Quantity = Annotated[Decimal, Places(9)]
Money = Annotated[Decimal, Places(_MONEY_PLACES)]
Rate = Annotated[Decimal, Places(8)]
Statistic = Annotated[Decimal, Places(8)]


def _freeze(value: object) -> object:
    if isinstance(value, MappingProxyType):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze(v) for v in value)
    return value


class _Record:
    """Makes every mapping and list in a record read-only once it is built."""

    def __post_init__(self) -> None:
        for f in dataclasses.fields(self):  # type: ignore[arg-type]
            object.__setattr__(self, f.name, _freeze(getattr(self, f.name)))


class BarStatus(StrEnum):
    """What Data says of an instrument's bar.

    A bar is stale when it is any session behind the newest one ended by the
    decision, a written lag of 0 sessions. A decision at close−20 reads the bar
    of the session before and one at the close reads that session's own, so any
    lag is a session that should be there and is not.
    """

    FRESH = "fresh"
    MISSING = "missing"
    STALE = "stale"


class Mode(StrEnum):
    BACKTEST = "backtest"
    SHADOW = "shadow"
    PAPER = "paper"
    LIVE = "live"


@dataclass(frozen=True)
class Instrument(_Record):
    id: InstrumentId
    symbol: str


@dataclass(frozen=True)
class Bar(_Record):
    """A bar, with where it came from, when it closed and when it could be known.

    #5 owns this type. Each price stays as received, and `excess_places` names
    each price field whose received value carries more places than `Price`
    declares, which is kept rather than rounded.

    - `volume` is `None` where the source stated none, as on an imported vintage.
    - `fetched` and `fetched_end` are marketlake's `fetch_ts` and `fetch_end_ts`
      as received, each beside its UTC instant, and `None` where marketlake
      holds none, as on the Chan vintages.
    - `knowable_at` is the time the bar could first have been known. For a
      captured bar it is `fetched_at`. For an imported bar it is `end`, because
      a vintage records only `source_as_of`, the date its source recorded the
      whole series, so a bar of a vintage recorded after its sessions can carry
      corrections made up to that date.
    - `source` and `source_as_of` are marketlake's own fields for an imported
      row, and `None` on a captured one.
    - `price_kind`, `venues`, `delay` and `volume_counted` come from the data
      module's price-kind contract when the bar is read, and read `unstated`
      where the contract states nothing.
    - `vintage_id` names the partition the bar was read from by its path, its
      SHA-256 in marketlake's manifest and the bucket's version of it.
    - `first_seen` is the time the data worker read the bar.
    """

    instrument: InstrumentId
    session: date
    open: Price
    high: Price
    low: Price
    close: Price
    volume: int | None
    basis: str
    origin: str
    vintage_id: str
    end: datetime
    interval: str
    first_seen: datetime
    fetched: str | None
    fetched_at: datetime | None
    fetched_end: str | None
    fetched_end_at: datetime | None
    knowable_at: datetime
    source: str | None
    source_as_of: date | None
    price_kind: str
    venues: str
    delay: str
    volume_counted: str
    excess_places: tuple[str, ...]


@dataclass(frozen=True)
class SpikeFlag(_Record):
    """A price the data module flags as wrong, beside the price it was judged against.

    The price stays in its bar as received. `rule` names the check that
    flagged it, and `note` says what the check could not weigh, such as a
    volume the source never stated.
    """

    session: date
    field: str
    price: Price
    against: Price
    rule: str
    note: str


@dataclass(frozen=True)
class Quote(_Record):
    instrument: InstrumentId
    at: datetime
    bid: Price | None
    ask: Price | None
    last: Price | None
    vendor_stamp: str
    fetched_at: datetime
    realtime: bool
    volume: int | None


@dataclass(frozen=True)
class Snapshot(_Record):
    """The bars and quotes one decision read, with each instrument's status.

    `flags` holds each flagged instrument's spike flags, beside prices that
    stay as received. `ledgers` maps each marketlake ledger the read relied
    on, by its key in the lake, to the SHA-256 of the bytes read, and keeps no
    row of it.
    """

    decision_at: datetime
    bars: Mapping[InstrumentId, Mapping[str, tuple[Bar, ...]]]
    quotes: Mapping[InstrumentId, Quote]
    status: Mapping[InstrumentId, BarStatus]
    provisional: bool
    flags: Mapping[InstrumentId, tuple[SpikeFlag, ...]]
    segments: tuple[str, ...]
    ledgers: Mapping[str, str]

    def __post_init__(self) -> None:
        super().__post_init__()
        for instrument, quote in self.quotes.items():
            if quote.instrument != instrument:
                raise ValueError(f"a {quote.instrument} quote is filed under {instrument}")
        for instrument, by_interval in self.bars.items():
            for interval, bars in by_interval.items():
                for bar in bars:
                    if bar.instrument != instrument or bar.interval != interval:
                        raise ValueError(
                            f"a {bar.instrument} {bar.interval} bar is filed under "
                            f"{instrument} {interval}"
                        )

    def require_fresh(self, needed: Mapping[InstrumentId, Collection[str]]) -> None:
        """Raise `Halt` unless every bar the caller needs is present and fresh.

        `bars` maps each instrument to its bars by interval, so a daily bar and
        minute bars of one instrument sit side by side. The caller names each
        instrument it needs and the intervals it reads of it, such as
        `{USO: ["1d"]}`. A needed instrument or interval Data left out of the
        snapshot halts, rather than reading as nothing to trade. Every other
        instrument the snapshot holds must be fresh and hold a bar too.

        It judges no age. Data fills `status` under the written lag, and this
        only reads it. An empty snapshot, or a caller that names nothing it needs,
        halts, because an empty input counts as a missing one (hard limit 3).
        """
        if not isinstance(needed, Mapping) or any(
            isinstance(v, str) or not isinstance(v, Collection) for v in needed.values()
        ):
            raise TypeError("require_fresh takes a mapping of instruments to the intervals needed")
        if not needed or not all(needed.values()):
            raise Halt("the caller names no bar it needs from the snapshot")
        if not self.status and not self.bars:
            raise Halt(f"the snapshot for {self.decision_at.isoformat()} is empty")
        for instrument in sorted(set(needed) | set(self.status) | set(self.bars)):
            status = self.status.get(instrument)
            if status is None and instrument not in self.bars:
                raise Halt(f"{instrument} missing for the session")
            if status is None:
                raise Halt(f"{instrument} has a bar and no status")
            if status != BarStatus.FRESH:
                raise Halt(f"{instrument} {status} for the session")
            by_interval = self.bars.get(instrument, {})
            if not any(by_interval.values()):
                raise Halt(f"{instrument} is fresh and has no bar")
            for interval in sorted(needed.get(instrument, ())):
                if not by_interval.get(interval):
                    raise Halt(f"{instrument} has no {interval} bar for the session")


@dataclass(frozen=True)
class Basket(_Record):
    """Instruments in a fixed ratio, such as `{USO: 1, GLD: -h}` for a spread.

    A leg is a number of shares per unit of the basket, never an amount of
    dollars. A model that weighs its legs in dollars divides each weight by the
    price it decided on before it writes the leg. A basket names at least one
    leg.
    """

    legs: Mapping[InstrumentId, Statistic]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.legs:
            raise ValueError("a basket names at least one leg")


@dataclass(frozen=True)
class Forecast(_Record):
    """What one strategy expects of one basket.

    `direction` is 1 to hold the basket long, -1 to hold it short and 0 to hold
    none of it. A flat forecast names its legs too, so a target built from it
    writes each one as zero rather than leaving it out. `confidence` is between
    0 and 1.
    """

    strategy: str
    basket: Basket
    direction: int
    horizon_sessions: int
    confidence: Statistic

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.direction) is not int or self.direction not in (-1, 0, 1):
            raise ValueError(f"direction {self.direction!r} is not -1, 0 or 1")
        if type(self.horizon_sessions) is not int or self.horizon_sessions < 1:
            raise ValueError(f"horizon {self.horizon_sessions!r} is not one session or more")
        if not self.confidence.is_finite() or not 0 <= self.confidence <= 1:
            raise ValueError(f"confidence {self.confidence} is not between 0 and 1")


@dataclass(frozen=True)
class Signal(_Record):
    """One value an alpha model computed for a decision, and the sessions it read.

    `first_session` and `last_session` are the first and the last session whose
    prices the value read.
    """

    name: str
    value: Statistic
    first_session: date
    last_session: date


@dataclass(frozen=True)
class ForecastSet(_Record):
    """What an alpha model returns: its forecasts and the signals behind them.

    It holds at least one forecast and one signal, because an output file with
    no entries halts its reader, and a missing forecast never reads as flat.
    """

    forecasts: tuple[Forecast, ...]
    signals: tuple[Signal, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.forecasts:
            raise ValueError("a forecast set holds at least one forecast")
        if not self.signals:
            raise ValueError("a forecast set holds at least one signal")
        names = [signal.name for signal in self.signals]
        if repeated := sorted({n for n in names if names.count(n) > 1}):
            raise ValueError(f"a forecast set holds more than one signal named {repeated[0]}")


@dataclass(frozen=True)
class Constraints(_Record):
    """One strategy's limits for one decision, as the risk model writes them.

    `max_order_qty` and `max_position` hold one share count per instrument,
    because the legs of a spread differ in shares. `core.tighten` checks every
    field before anything reads one.
    """

    strategy: str
    max_order_qty: Mapping[InstrumentId, Quantity]
    max_position: Mapping[InstrumentId, Quantity]
    max_gross: Money
    max_open_orders: int
    max_net: Money
    loss_stop: Money
    moc_cutoff: int


@dataclass(frozen=True)
class HardCaps(_Record):
    """What only the owner writes, by hand, as `core.parse_caps` reads it.

    Every field but the last is a limit of the risk gate or a part of one. The
    last, `share_step`, is the broker account's rule rather than a limit: the
    step a share count moves in, which portfolio construction rounds a target
    to. The net cap is computed from `share`, V and `stress_move` rather than
    written. `stress_low` and `stress_high` are the sane range of the stress
    move, both ends allowed, `stress_vintage` the SHA-256 of the vintage it was
    measured over, `stress_through` that vintage's last session and
    `stress_max_age_days` the days after it that a decision may still use it.
    """

    max_order_qty: Mapping[InstrumentId, Quantity]
    max_position: Mapping[InstrumentId, Quantity]
    max_gross: Money
    max_open_orders: int
    loss_stop: Money
    moc_cutoff: int
    share: Statistic
    stress_move: Statistic
    stress_low: Statistic
    stress_high: Statistic
    stress_vintage: str
    stress_through: date
    stress_max_age_days: int
    share_step: Quantity


@dataclass(frozen=True)
class TargetPortfolio(_Record):
    decision_at: datetime
    strategy: str
    shares: Mapping[InstrumentId, Quantity]


@dataclass(frozen=True)
class Sizing(_Record):
    """How one strategy's target was sized.

    - `wanted` is each leg's shares before any limit, toward zero at the
      places a quantity declares.
    - `cut` names each limit that alone would make the target smaller before
      its rounding, in the order the gate reads them.
    """

    strategy: str
    wanted: Mapping[InstrumentId, Quantity]
    cut: tuple[Limit, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        if list(self.cut) != [limit for limit in _PASSABLE if limit in self.cut]:
            raise ValueError(
                f"cut names {', '.join(self.cut)}, which is out of order, twice, or not "
                "max_position, max_gross or max_net"
            )


class OrderType(StrEnum):
    MARKET = "market"
    MARKET_ON_CLOSE = "market_on_close"


class TimeInForce(StrEnum):
    """How long an order stands: the day, or the closing auction alone."""

    DAY = "day"
    CLS = "cls"


@dataclass(frozen=True)
class OrderIntent(_Record):
    """One order the platform means to send. `qty` is signed, negative for a sale.

    The pairs of `type` and `time_in_force` are a market order for the day and
    a market-on-close order at `cls`, and the gate halts on any other.
    """

    client_order_id: str
    strategy: str
    instrument: InstrumentId
    qty: Quantity
    type: OrderType
    time_in_force: TimeInForce


@dataclass(frozen=True)
class Correction(_Record):
    """One journal entry that moved cash or a position by anything but a fill or a listed record.

    `seq` is the entry's number, `at` the journal's stamp on it, `built_at`,
    and `repositions` whether it moved a position. A broker's correction or
    bust of a fill is such an entry. A fill of the platform's own order and a
    broker record on the closed list are not.
    """

    seq: int
    at: datetime
    repositions: bool


@dataclass(frozen=True)
class Book(_Record):
    """The positions, open orders and cash the journal's replay gives, and what qualifies them.

    - `open_orders_by_strategy` holds each order the gate approved that has
      not ended, at its unfilled remainder.
    - `in_doubt` names each instrument with a `sending` entry that no later
      entry settles. That order is open too, and every range and the count of
      open orders include it there.
    - `corrections` holds one `Correction` per entry that is one, in the
      journal's order.
    """

    positions_by_strategy: Mapping[str, Mapping[InstrumentId, Quantity]]
    open_orders_by_strategy: Mapping[str, tuple[OrderIntent, ...]]
    cash: Money
    replayed_through: int
    in_doubt: tuple[InstrumentId, ...]
    corrections: tuple[Correction, ...]


class OrderState(StrEnum):
    """Where an order stands. The table of moves between them is `journal/orders.py`'s."""

    INTENDED = "Intended"
    APPROVED = "Approved"
    REFUSED = "Refused"
    ABANDONED = "Abandoned"
    SENDING = "Sending"
    IN_DOUBT = "InDoubt"
    ACKNOWLEDGED = "Acknowledged"
    PARTLY_FILLED = "PartlyFilled"
    FILLED = "Filled"
    REJECTED = "Rejected"
    EXPIRED = "Expired"


@dataclass(frozen=True)
class OrderEntry(_Record):
    """One journal entry that moved an order, by its number and the name of its kind."""

    seq: int
    kind: str


@dataclass(frozen=True)
class OrderRecord(_Record):
    """One order as the journal's entries leave it.

    - `decision` is the number of the `decision` entry that planned it.
    - `broker_order_id` is the broker's id, or `None` before an `ack`.
    - `filled` is the total of its fills, signed as the order's quantity is.
    - `entries` names each entry that moved it, oldest first. A `disputed_fill`
      and a repeated `fill` move no order, so neither is named.
    - `sent_at` is its `sending` entry's `built_at`, or `None` before one.
    """

    intent: OrderIntent
    decision: int
    state: OrderState
    broker_order_id: str | None
    filled: Quantity
    entries: tuple[OrderEntry, ...]
    sent_at: datetime | None


class FillBasis(StrEnum):
    """What price a simulated fill was given. A broker's own fill carries none."""

    SIGNAL_CLOSE = "signal_close"
    OFFICIAL_CLOSE = "official_close"
    QUOTE = "quote"


@dataclass(frozen=True)
class Fill(_Record):
    """One fill as the broker reported it. `qty` is signed, negative for a sale.

    - `at_text` is the time as the broker wrote it, beside `at` in UTC.
    - `excess_places` names each field whose received value carries more places
      than declared, which is kept rather than rounded.
    - `basis` is `None` on a broker's fill, so a simulated one can never be
      read as real or calibrate the cost model.
    """

    fill_id: str
    correction_id: str | None
    broker_order_id: str
    client_order_id: str | None
    instrument: InstrumentId
    qty: Quantity
    price: Price
    at_text: str
    at: datetime
    received_at: datetime
    excess_places: tuple[str, ...]
    basis: FillBasis | None


class Outcome(StrEnum):
    """What a broker's answer comes to, in one closed set.

    `not_sent` is for a proof that nothing left, such as a connection refused
    before any byte. `unknown` is for every answer an adapter cannot classify,
    and for a refusal that says the broker already holds the id. Only a lookup
    answers `not_found`, and only a search for an order answers `several`.
    """

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NOT_SENT = "not_sent"
    UNKNOWN = "unknown"
    NOT_FOUND = "not_found"
    SEVERAL = "several"


class BrokerState(StrEnum):
    """Where a broker says an order stands, in the platform's words."""

    WORKING = "working"
    PENDING_CANCEL = "pending_cancel"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BrokerOrder(_Record):
    """One order as the broker holds it, in the platform's types.

    `qty` and `filled` are signed, negative for a sale. `type` and
    `time_in_force` are `None` where the broker names one the platform does
    not send.
    """

    broker_order_id: str
    client_order_id: str | None
    instrument: InstrumentId
    qty: Quantity
    filled: Quantity
    type: OrderType | None
    time_in_force: TimeInForce | None
    state: BrokerState
    submitted_at: datetime


def _received_at(at: object) -> None:
    if not isinstance(at, datetime) or at.utcoffset() != timedelta(0):
        raise ValueError("an answer's time of receipt is not a UTC time")


@dataclass(frozen=True)
class Ack(_Record):
    """A broker's answer about one order, with the bytes it sent beside the converted value.

    `raw` is a body and never a header. An `accepted` with no broker's order
    id, and a time that is not UTC, raise.
    """

    outcome: Outcome
    broker_order_id: str | None
    order: BrokerOrder | None
    raw: bytes = dataclasses.field(repr=False)
    received_at: datetime

    def __post_init__(self) -> None:
        super().__post_init__()
        _received_at(self.received_at)
        if self.outcome == Outcome.ACCEPTED and not self.broker_order_id:
            raise ValueError("an accepted order carries no broker's order id")


@dataclass(frozen=True)
class FoundOrder(_Record):
    answer: Outcome
    rows: tuple[BrokerOrder, ...]
    whole_list: bool
    raw: bytes = dataclasses.field(repr=False)


# The most bytes a broker read holds in all its pages. Alpaca's order list gives at most 500
# orders a page, and an order is about a kilobyte, so this is several thousand orders.
READ_LIMIT = 4 * 1024 * 1024


@dataclass(frozen=True)
class ReceivedPage(_Record):
    """One page of a broker read as it came: its body, never a header, and when it came."""

    body: bytes = dataclasses.field(repr=False)
    at: datetime

    def __post_init__(self) -> None:
        super().__post_init__()
        _received_at(self.at)


class _BrokerRead(_Record):
    """What every broker read checks of its pages.

    - A read holds at least one page, because an empty answer is one the
      broker gave, and a read that failed raises instead.
    - One whose pages are past `READ_LIMIT` is not `whole`.
    """

    received: tuple[ReceivedPage, ...]
    whole: bool

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.received:
            raise ValueError("a broker read holds no page the broker sent")
        size = sum(len(page.body) for page in self.received)
        if self.whole and size > READ_LIMIT:
            raise ValueError(f"a read of {size} bytes, past {READ_LIMIT}, is not whole")


@dataclass(frozen=True)
class PositionsRead(_BrokerRead):
    """The positions the broker holds. `whole` says that every page came."""

    positions: Mapping[InstrumentId, Quantity]
    received: tuple[ReceivedPage, ...]
    whole: bool


@dataclass(frozen=True)
class OrdersRead(_BrokerRead):
    """The broker's orders, the open ones or every one since a time."""

    orders: tuple[BrokerOrder, ...]
    received: tuple[ReceivedPage, ...]
    whole: bool


@dataclass(frozen=True)
class FillsRead(_BrokerRead):
    fills: tuple[Fill, ...]
    received: tuple[ReceivedPage, ...]
    whole: bool


@dataclass(frozen=True)
class CashRead(_BrokerRead):
    """The account's cash, which `Broker.account` gives."""

    cash: Money
    received: tuple[ReceivedPage, ...]
    whole: bool


@dataclass(frozen=True)
class RecordsRead(_BrokerRead):
    """The broker's records, whose own type #151 adds."""

    received: tuple[ReceivedPage, ...]
    whole: bool


@dataclass(frozen=True)
class JournalAck(_Record):
    seq: int
    entry_hash: str


class Limit(StrEnum):
    """What a refusal names: one of the seven limits, or one of four states that refuse.

    The seven come in the order the gate reads them.
    """

    MAX_ORDER_QTY = "max_order_qty"
    MAX_POSITION = "max_position"
    MAX_GROSS = "max_gross"
    MAX_OPEN_ORDERS = "max_open_orders"
    MAX_NET = "max_net"
    LOSS_STOP = "loss_stop"
    MOC_CUTOFF = "moc_cutoff"
    STOP = "stop"
    CLOSE_ENTRY = "close_entry"
    IN_DOUBT = "in_doubt"
    POSITION = "position"


# The four states carry no figure, because nothing was measured against a bound.
_STATES = frozenset({Limit.STOP, Limit.CLOSE_ENTRY, Limit.IN_DOUBT, Limit.POSITION})
# The three limits an order that only reduces may go out past, in the order the gate reads
# them. They are the three a target is sized within, too.
_PASSABLE = (Limit.MAX_POSITION, Limit.MAX_GROSS, Limit.MAX_NET)


@dataclass(frozen=True)
class PastLimit(_Record):
    """One of `max_position`, `max_gross` and `max_net` that is past with an order counted.

    `value` is the size of the figure at the end of its range further from
    zero. The gate compares the exact figure, and this one is rounded for the
    record, so it can equal `bound`.
    """

    limit: Limit
    value: Quantity
    bound: Quantity

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.limit not in _PASSABLE:
            raise ValueError(f"{self.limit!r} is not max_position, max_gross or max_net")
        if not self.value.is_finite() or not self.bound.is_finite() or self.value < self.bound:
            raise ValueError(f"{self.limit} at {self.value} is not past {self.bound}")


@dataclass(frozen=True)
class LossReading(_Record):
    """The day's loss against its stop, and the session V was valued at.

    The gate compares the exact figures, and `loss` and `bound` are rounded for
    the record, so a loss past its bound can read as equal to it.
    """

    loss: Money
    bound: Money
    past: bool
    session: date

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.loss.is_finite() or not self.bound.is_finite():
            raise ValueError(f"a loss of {self.loss} against a stop of {self.bound} is not finite")
        if not (self.loss >= self.bound if self.past else self.loss <= self.bound):
            raise ValueError(
                f"a loss of {self.loss} against a stop of {self.bound} does not read "
                f"past as {self.past}"
            )


@dataclass(frozen=True)
class Verdict(_Record):
    """What the gate answered for one order, and what it read to answer.

    - `limit` names what refused the order, with the figure and its `bound`
      where one of the seven limits did.
    - `missing_term` is `position` for an order that names no strategy, whose
      position nothing can be measured against.
    - `adding` is false only for an order `core.classify` says reduces.
    - `worst_loss` is the strategy's loss under the stress move with the order
      counted, or `None` where the order names no strategy.
    - `loss` is the day's loss the check read.
    - `past` names each of the three limits an order that only reduces may go
      out past, where the figure with this order counted is over its bound.

    A verdict that contradicts itself raises, which `decode` gives as a
    `CodecError`.
    """

    approved: bool
    limit: Limit | None
    value: Quantity | None
    bound: Quantity | None
    missing_term: str | None
    adding: bool
    worst_loss: Money | None
    loss: LossReading
    past: tuple[PastLimit, ...]

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.limit is not None and not isinstance(self.limit, Limit):
            raise ValueError(f"limit {self.limit!r} is not a limit")
        if self.approved and self.limit is not None:
            raise ValueError(f"an approval names {self.limit}")
        if not self.approved and self.limit is None:
            raise ValueError("a refusal names no limit")
        measured = self.limit is not None and self.limit not in _STATES
        if (self.value is not None, self.bound is not None) != (measured, measured):
            raise ValueError(
                f"a verdict on {self.limit} carries a value of {self.value} and a bound of "
                f"{self.bound}"
            )
        if self.missing_term != ("position" if self.limit == Limit.POSITION else None):
            raise ValueError(f"a verdict on {self.limit} lacks {self.missing_term!r}")
        named = [entry.limit for entry in self.past]
        if named != [limit for limit in _PASSABLE if limit in named]:
            raise ValueError(f"past names {', '.join(named)}, out of order or twice")
        if self.limit in _PASSABLE and self.limit not in named:
            raise ValueError(f"a refusal on {self.limit} leaves it out of past")
        if self.approved and self.adding and named:
            raise ValueError(f"an approval that adds is past {', '.join(named)}")
        if self.limit == Limit.LOSS_STOP and not self.loss.past:
            raise ValueError("a refusal on loss_stop reads a loss inside the stop")
        if self.approved and (self.worst_loss is None or not self.worst_loss.is_finite()):
            raise ValueError(f"an approval carries a worst loss of {self.worst_loss}")
        if self.approved and self.adding and self.worst_loss <= 0:
            raise ValueError(f"an approval that adds carries a worst loss of {self.worst_loss}")


class CostKind(StrEnum):
    """What a cost term estimates.

    A `charge` is a commission or a fee the broker books. A `gap` is the gap
    from the quote's midpoint at the decision to the fill. The two stay apart,
    as docs/design.md's commission line asks, so a broker that charges no
    commission never reads as costing nothing.
    """

    CHARGE = "charge"
    GAP = "gap"


class CostSide(StrEnum):
    """The side of a trade a cost term applies to: either one, or a sale alone."""

    BOTH = "both"
    SELL = "sell"


@dataclass(frozen=True)
class CostTerm(_Record):
    """One figure of an instrument's cost, and where it came from.

    `fixed` is charged once per trade, `per_share` on each share, and
    `per_dollar` on each dollar traded. `source` is a dated published text, or
    `placeholder:` and the reason no such text was read.
    """

    kind: CostKind
    side: CostSide
    fixed: Money
    per_share: Rate
    per_dollar: Rate
    source: str


@dataclass(frozen=True)
class CostSchedule(_Record):
    """Each instrument's cost terms, written once per strategy and decision."""

    terms: Mapping[InstrumentId, tuple[CostTerm, ...]]

    def estimate(
        self,
        instrument: InstrumentId,
        qty: Decimal,
        price: Decimal,
        kind: CostKind | None = None,
    ) -> Decimal:
        """What trading `qty` shares at `price` would cost, rounded to no field's places.

        `qty` is negative for a sale, and `price` is the one the caller sizes
        the trade at. `kind` picks the charge or the gap alone, and `None` asks
        for both and adds them. Zero shares cost zero, at any price and with any terms, so a leg
        that does not trade needs none.

        A trade in an instrument with no term of a kind asked for halts,
        because an absent term never reads as a cost of zero (hard limit 3).
        So does a quantity that is not finite, and a price that is not above
        zero, since a price of zero would charge nothing per dollar.
        """
        if not qty.is_finite():
            raise Halt(f"{instrument} has a trade of {qty} shares, which has no cost")
        if qty == 0:
            return Decimal(0)
        asked = tuple(CostKind) if kind is None else (kind,)
        terms = [t for t in self.terms.get(instrument, ()) if t.kind in asked]
        if absent := [k for k in asked if k not in {t.kind for t in terms}]:
            raise Halt(f"{instrument} has no {' and no '.join(absent)} term in the schedule")
        if not price.is_finite() or price <= 0:
            raise Halt(f"{instrument} has a trade priced at {price}, which has no cost")
        shares = abs(qty)
        return sum(
            (
                t.fixed + t.per_share * shares + t.per_dollar * shares * price
                for t in terms
                if t.side == CostSide.BOTH or qty < 0
            ),
            Decimal(0),
        )


@dataclass(frozen=True)
class BrokerAccount(_Record):
    positions: Mapping[InstrumentId, Quantity]
    open_orders: tuple[BrokerOrder, ...]
    cash: Money
    read_at: datetime


@dataclass(frozen=True)
class Valuation(_Record):
    """V, the account's value at one session's official closes, and its parts.

    `value` is `cash` plus each position at its mark, rounded to the places
    `Money` declares, and a valuation that says otherwise raises.
    """

    session: date
    value: Money
    cash: Money
    positions: Mapping[InstrumentId, Quantity]
    marks: Mapping[InstrumentId, Price]
    replayed_through: int

    def __post_init__(self) -> None:
        super().__post_init__()
        if unmarked := sorted(set(self.positions) - set(self.marks)):
            raise ValueError(f"a valuation holds {unmarked[0]} with no mark")
        try:
            whole = worth("the valuation's value", self.cash, self.positions, self.marks)
            # A change of `Money`'s places would refuse the valuations already
            # stored, and a release rolled back would refuse the new ones. So a
            # release that reads both ships before one that writes the new.
            parts = rounded("the valuation's value", whole, _MONEY_PLACES)
        except Halt as error:
            raise ValueError(error.reason) from error
        if self.value != parts:
            raise ValueError(
                f"a valuation of {self.value} is not its cash and positions, which come to {parts}"
            )


class StopState(StrEnum):
    """What the order path found of the loss stop: no firing, one that stands, or no answer."""

    ABSENT = "absent"
    FIRED = "fired"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class GateInputs(_Record):
    """What the order path reads for one check, beside the book, the snapshot and the limits.

    `caps` are the hard caps as they read just before the check, `valuation`
    the session's V or `None` where none was found, and `close_entry` the
    journal's entry of a close after a broker's trade.
    """

    caps: HardCaps
    valuation: Valuation | None
    stop: StopState
    close_entry: JournalEntry | None


@dataclass(frozen=True)
class SendOutcome(_Record):
    decision_at: datetime
    target_hash: str
    orders: tuple[Ack, ...]


@dataclass(frozen=True)
class BucketObject(_Record):
    """An object read from a bucket the platform may only read, such as marketlake's.

    `version` is the bucket's version of the object, and `sha256` the
    whole-object SHA-256 the bucket stored with it. Either is `None` where the
    bucket holds none, which the reader refuses.
    """

    key: str
    data: bytes
    version: str | None
    sha256: str | None


@dataclass(frozen=True)
class BlobRef(_Record):
    key: str
    sha256: str


@dataclass(frozen=True)
class StoredBlob(_Record):
    ref: BlobRef
    etag: str
    stored_at: datetime


@dataclass(frozen=True)
class BlobHeader(_Record):
    """The header every output file and decision result begins with.

    `inputs` maps each input's key to its SHA-256. `config_hashes` maps each
    strategy the blob concerns to the SHA-256 of that strategy's tracked
    configuration file. A stage that belongs to one strategy records that
    strategy alone, and a stage the deployment's strategies share records
    every one of them, so a change to one strategy's file leaves every other
    strategy's blobs naming the same version. The codec writes the map with
    its keys sorted, so its bytes do not depend on the order it was built in.
    """

    format: int
    deployment: str
    run: str
    stage: str
    kind: str
    decision_at: datetime
    inputs: Mapping[str, str]
    release: str
    config_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.config_hashes, Mapping) or not self.config_hashes:
            raise ValueError("a header names the configuration hash of at least one strategy")
        for strategy, digest in self.config_hashes.items():
            if not is_name(strategy):
                raise ValueError(f"strategy {strategy!r} cannot be part of a key")
            if not is_sha256(digest):
                raise ValueError(
                    f"strategy {strategy}'s configuration hash {digest!r} is not a lower case "
                    "hex SHA-256"
                )


@dataclass(frozen=True)
class RestartRecord(_Record):
    format: int
    worker: str
    restart_count: int
    exit_status: int | None
    signal: str | None
    error_tail: str


@dataclass(frozen=True)
class HaltRecord(_Record):
    """Why a decision halted, and what it leaves behind.

    `unstored` lists each capture the process still held that is not stored,
    for any reason, such as S3 not answering or its key holding other bytes, by
    the key it was trying and its SHA-256. design.md's `### Rule 1:
    a write S3 refuses or does not answer` refuses a `clear` of the halt until
    each is stored or journaled as lost, and #11 builds that refusal.
    """

    reason: str
    cause: BlobRef | None
    restart_records: tuple[RestartRecord, ...]
    found_outputs: tuple[BlobRef, ...]
    unstored: tuple[BlobRef, ...]


@dataclass(frozen=True)
class DecisionResult(_Record):
    header: BlobHeader
    outputs: tuple[BlobRef, ...]
    halt: HaltRecord | None


@dataclass(frozen=True)
class Heartbeat(_Record):
    format: int
    writer: str
    at: datetime


@dataclass(frozen=True)
class Lease(_Record):
    """A lease's holder, down to the handler, and when it expires.

    The holder's fields are `None` while the lease is free. `status` is set
    only on a plan's lease, where it holds the run's status.
    """

    format: int
    vm: str | None
    boot: str | None
    container: str | None
    restart_count: int | None
    handler: str | None
    release: str | None
    run: str | None
    taken: datetime | None
    expires: datetime | None
    status: str | None


@dataclass(frozen=True)
class PlanDecision(_Record):
    """One decision of a plan: its UTC time and the strategies deciding at it."""

    at: datetime
    strategies: tuple[str, ...]


@dataclass(frozen=True)
class Plan(_Record):
    format: int
    deployment: str
    run: str
    mode: Mode
    release: str
    decisions: tuple[PlanDecision, ...]


@dataclass(frozen=True)
class JournalEntry(_Record):
    """The one numbered record, chained to the entry before it.

    `body` holds a `kind` and that kind's fields, as plain JSON values: the
    codec refuses a decimal, a time or a record there, because it could not
    read one back as what it was.
    """

    seq: int
    format: int
    built_at: datetime
    body: Mapping[str, object]
    prev_hash: str

    def hash(self) -> str:
        """The SHA-256 of the entry's canonical bytes, which the next entry carries.

        This is the one place the entry's hash is computed. `decode` refuses
        bytes that are not the canonical encoding of what it parsed, so for an
        entry read back this equals the SHA-256 of the bytes S3 returned.
        """
        return hashlib.sha256(encode(self)).hexdigest()
