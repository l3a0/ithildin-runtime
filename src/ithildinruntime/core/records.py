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
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, NewType

from .codec import Places, encode
from .halt import Halt

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
    "BrokerRead",
    "Constraints",
    "CostSchedule",
    "DecisionResult",
    "Fill",
    "Forecast",
    "FoundOrder",
    "GateInputs",
    "HaltRecord",
    "Heartbeat",
    "Instrument",
    "InstrumentId",
    "JournalAck",
    "JournalEntry",
    "Lease",
    "Mode",
    "Money",
    "OrderIntent",
    "Plan",
    "PlanDecision",
    "Price",
    "Quantity",
    "Quote",
    "Rate",
    "RestartRecord",
    "SendOutcome",
    "Snapshot",
    "Statistic",
    "StoredBlob",
    "TargetPortfolio",
    "Valuation",
    "Verdict",
]

InstrumentId = NewType("InstrumentId", str)

# The places each kind of decimal declares, which never shrink in a later
# release. tests/test_core_types.py pins each one.
# - A price carries four, the finest tick US equities quote below a dollar.
# - A quantity carries nine, because Alpaca takes fractional shares to nine.
# - An amount of money carries four, so a sub-cent fee survives.
# - A per-share rate and a statistic carry eight, for a hedge ratio or a fee
#   rate finer than a cent.
Price = Annotated[Decimal, Places(4)]
Quantity = Annotated[Decimal, Places(9)]
Money = Annotated[Decimal, Places(4)]
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
    """What Data says of an instrument's bar. #7 sets the age that makes one stale."""

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
    """A bar, with where it came from and when it closed.

    Each price's kind, venues and delay, and each volume's counting, join this
    type when #50 names them and #5 adds them.
    """

    instrument: InstrumentId
    session: date
    open: Price
    high: Price
    low: Price
    close: Price
    volume: int
    basis: str
    origin: str
    vintage_id: str
    end: datetime
    interval: str
    first_seen: datetime


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
    """The bars and quotes one decision read, with each instrument's status."""

    decision_at: datetime
    bars: Mapping[InstrumentId, Mapping[str, tuple[Bar, ...]]]
    quotes: Mapping[InstrumentId, Quote]
    status: Mapping[InstrumentId, BarStatus]
    provisional: bool
    flags: Mapping[str, object]
    segments: tuple[str, ...]
    ledgers: Mapping[str, str]

    def __post_init__(self) -> None:
        super().__post_init__()
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

        It judges no age. Data fills `status` under #7's rule, and this only
        reads it. An empty snapshot, or a caller that names nothing it needs,
        halts, because an empty input counts as a missing one (hard limit 3).
        """
        if not isinstance(needed, Mapping) or any(isinstance(v, str) for v in needed.values()):
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
    """Instruments in a fixed ratio, such as `{USO: 1, GLD: -h}` for a spread."""

    legs: Mapping[InstrumentId, Statistic]


@dataclass(frozen=True)
class Forecast(_Record):
    strategy: str
    basket: Basket
    direction: int
    horizon_sessions: int
    confidence: Statistic


@dataclass(frozen=True)
class Constraints(_Record):
    max_order_qty: Quantity
    max_position: Quantity
    max_gross: Money
    max_open_orders: int
    max_net: Money
    loss_stop: Money
    moc_cutoff: int


@dataclass(frozen=True)
class TargetPortfolio(_Record):
    decision_at: datetime
    strategy: str
    shares: Mapping[InstrumentId, Quantity]


@dataclass(frozen=True)
class OrderIntent(_Record):
    client_order_id: str
    strategy: str
    instrument: InstrumentId
    qty: Quantity
    type: str
    time_in_force: str


@dataclass(frozen=True)
class Book(_Record):
    positions_by_strategy: Mapping[str, Mapping[InstrumentId, Quantity]]
    open_orders_by_strategy: Mapping[str, tuple[OrderIntent, ...]]
    cash: Money
    platform_realised_pnl: Money
    replayed_through: int


@dataclass(frozen=True)
class Fill(_Record):
    fill_id: str
    correction_id: str | None
    broker_order_id: str
    client_order_id: str | None
    qty: Quantity
    price: Price
    at: datetime
    received_at: datetime


@dataclass(frozen=True)
class BrokerOrder(_Record):
    broker_order_id: str
    client_order_id: str | None
    instrument: InstrumentId
    side: str
    qty: Quantity
    type: str


@dataclass(frozen=True)
class Ack(_Record):
    """A broker's answer, with the bytes it sent beside the converted value."""

    client_order_id: str | None
    broker_order_id: str | None
    order: BrokerOrder | None
    state: str
    outcome: str
    raw: bytes


@dataclass(frozen=True)
class FoundOrder(_Record):
    answer: str
    rows: tuple[BrokerOrder, ...]
    whole_list: bool
    raw: bytes


@dataclass(frozen=True)
class BrokerRead(_Record):
    """A broker read's received bytes, kept apart from the value converted from them.

    `value` is typed `object` until #10 gives each call's converted value a
    type. Until then the codec writes only plain JSON values there, so a read
    whose converted value is a record or a decimal is stored by its `raw`
    bytes alone.
    """

    raw: bytes
    value: object


@dataclass(frozen=True)
class JournalAck(_Record):
    seq: int
    entry_hash: str


@dataclass(frozen=True)
class Verdict(_Record):
    approved: bool
    limit: str | None
    value: Quantity | None
    missing_term: str | None


@dataclass(frozen=True)
class CostSchedule(_Record):
    """A fixed and a per-share cost for each instrument, written once per decision."""

    per_share: Mapping[InstrumentId, Rate]
    fixed: Mapping[InstrumentId, Money]

    def estimate(self, instrument: InstrumentId, qty: Decimal) -> Decimal:
        """The cost of trading `qty` shares, unrounded. A missing instrument halts.

        The formula, a fixed cost plus a per-share cost, follows 05's proposal
        for #8, which owns it and may change it.
        """
        if instrument not in self.per_share or instrument not in self.fixed:
            raise Halt(f"{instrument} has no cost in the schedule")
        return self.fixed[instrument] + self.per_share[instrument] * abs(qty)


@dataclass(frozen=True)
class BrokerAccount(_Record):
    positions: Mapping[InstrumentId, Quantity]
    open_orders: tuple[BrokerOrder, ...]
    cash: Money
    read_at: datetime


@dataclass(frozen=True)
class Valuation(_Record):
    session: date
    value: Money
    cash: Money
    marks: Mapping[InstrumentId, Price]
    replayed_through: int


@dataclass(frozen=True)
class GateInputs(_Record):
    share: Statistic
    stress_move: Statistic
    vintage_hash: str
    value: Money
    stop_state: str
    close_entry: JournalEntry | None


@dataclass(frozen=True)
class SendOutcome(_Record):
    decision_at: datetime
    target_hash: str
    orders: tuple[Ack, ...]


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
    """The header every output file begins with. `inputs` maps each input's key to its SHA-256."""

    format: int
    deployment: str
    run: str
    stage: str
    kind: str
    decision_at: datetime
    inputs: Mapping[str, str]
    release: str
    config_hash: str


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
    reason: str
    cause: BlobRef | None
    restart_records: tuple[RestartRecord, ...]
    found_outputs: tuple[BlobRef, ...]


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
