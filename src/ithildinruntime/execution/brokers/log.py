"""A simulated account as a log of calls, one blob for each call that changed it.

docs/architecture/08-crosscutting-concepts.md's `#### The simulated broker's
account` says what a blob holds. In short:

- A blob holds its call's events and the account after them. An order that ended
  is in the event that ended it and in no later blob, so the log of a long run
  does not grow with its square.
- Only a create the store acknowledged moves what this process remembers. A
  create with no answer leaves the log unsure, and the next call first reads
  forward and takes in what is there, where it is this writer's.
- A log is one process's. A call that finds its number taken by another writer
  halts, because two processes are writing one account.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType

from ithildinruntime.core import (
    BlobMissing,
    BlobStore,
    BrokerOrder,
    BrokerState,
    BrokerUnavailable,
    CodecError,
    Fill,
    Halt,
    InstrumentId,
    KeyExists,
    Money,
    OrderType,
    Quantity,
    StoreError,
    TimeInForce,
    decode,
    encode,
    exact_product,
    exact_total,
    finite,
    sha256_of,
    sim_account_key,
)

__all__ = [
    "SimCall",
    "SimClose",
    "SimEdit",
    "SimEvents",
    "SimOrder",
    "SimState",
    "SimulatedAccount",
    "after",
    "ended",
]

# The format this release writes, and every one it reads. tests/fixtures/simulated holds a
# committed log, because a shadow account's log outlives every release.
SIM_FORMAT = 1
_OPENING = 1
_WRITER = re.compile(r"[0-9a-f]{32}")
_ZERO = Decimal(0)
_UNREACHED = "account_log"


@dataclass(frozen=True)
class SimOrder:
    """One order as the account holds it, or as it is on its way.

    - `id` is the broker's, and `None` for one that has not arrived.
    - `session` is the session whose close it fills at, and `read_close` whether
      the decision that sent it had read that close.
    """

    id: str | None
    client_order_id: str | None
    instrument: InstrumentId
    qty: Quantity
    filled: Quantity
    type: OrderType
    time_in_force: TimeInForce
    state: BrokerState
    submitted_at: datetime
    session: date
    read_close: bool

    def copy(self) -> BrokerOrder:
        """The order as a broker's answer gives it."""
        if self.id is None:
            raise Halt(f"the order {self.client_order_id} has not arrived")
        return BrokerOrder(
            broker_order_id=self.id,
            client_order_id=self.client_order_id,
            instrument=self.instrument,
            qty=self.qty,
            filled=self.filled,
            type=self.type,
            time_in_force=self.time_in_force,
            state=self.state,
            submitted_at=self.submitted_at,
        )


@dataclass(frozen=True)
class SimEdit:
    """What someone did to the account by hand, as the part it left.

    `count` is the call the fault named. One of the rest is set: the cash, one
    instrument's shares, or an order, changed or new.
    """

    count: int
    cash: Money | None
    instrument: InstrumentId | None
    shares: Quantity | None
    order: SimOrder | None


@dataclass(frozen=True)
class SimClose:
    """An order's end: its id, the state it ended in, when, and its fill where it had one."""

    id: str
    state: BrokerState
    at: datetime
    fill: Fill | None


@dataclass(frozen=True)
class SimEvents:
    """What one call did, in the order it is applied.

    - `edits` came by hand.
    - `placed` are the orders the account took, and `missed` the client order
      ids of orders that arrived and were not taken.
    - `delayed` are the orders now on their way.
    - `closed` are the orders that ended.
    """

    edits: tuple[SimEdit, ...] = ()
    placed: tuple[SimOrder, ...] = ()
    missed: tuple[str, ...] = ()
    delayed: tuple[SimOrder, ...] = ()
    closed: tuple[SimClose, ...] = ()


@dataclass(frozen=True)
class SimState:
    """The account after a call: its cash, its positions, and each order open or on its way."""

    cash: Money
    positions: Mapping[InstrumentId, Quantity]
    open_orders: tuple[SimOrder, ...]
    delayed: tuple[SimOrder, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))


@dataclass(frozen=True)
class SimCall:
    """One blob of the log. Call 1 opens the account, so it names no writer and no time."""

    format: int
    account: str
    call: int
    writer: str | None
    at: datetime | None
    events: SimEvents
    after: SimState


def after(state: SimState, events: SimEvents) -> SimState:
    """The account once `events` are applied, or a halt where one does not fit it."""
    cash, positions = state.cash, dict(state.positions)
    open_orders = {order.id: order for order in state.open_orders}
    delayed = list(state.delayed)

    def moved(instrument: InstrumentId, shares: Decimal) -> None:
        if shares == 0:
            positions.pop(instrument, None)
        else:
            positions[instrument] = shares

    for edit in events.edits:
        if edit.cash is not None:
            cash = edit.cash
        if edit.instrument is not None and edit.shares is not None:
            moved(edit.instrument, edit.shares)
        if edit.order is not None:
            open_orders[edit.order.id] = edit.order
    for order in events.placed:
        delayed = [d for d in delayed if d.client_order_id != order.client_order_id]
        open_orders[order.id] = order
    delayed = [d for d in delayed if d.client_order_id not in events.missed]
    delayed.extend(events.delayed)
    for close in events.closed:
        order = open_orders.pop(close.id, None)
        if order is None:
            raise Halt(f"the simulated order {close.id} ended and was not open")
        if close.fill is not None:
            fill = close.fill
            # As `Book.cash` moves, so the journal and this log agree on every fill.
            paid = exact_product(f"the cost of the fill {fill.fill_id}", fill.qty, fill.price)
            cash = exact_total("the simulated cash", (cash, paid.copy_negate()))
            what = f"the simulated position in {fill.instrument}"
            moved(
                fill.instrument,
                exact_total(what, (positions.get(fill.instrument, _ZERO), fill.qty)),
            )
    return SimState(cash, positions, tuple(open_orders.values()), tuple(delayed))


def ended(order: SimOrder, close: SimClose) -> SimOrder:
    """An order as it ended."""
    filled = _ZERO if close.fill is None else close.fill.qty
    return replace(order, state=close.state, filled=filled)


@dataclass(eq=False)
class SimulatedAccount:
    """One account's log, held by one process for its whole life.

    `load` reads nothing, so no worker fails to start on a log that cannot be
    read. The log is read once, at the first call of a broker over it, and an
    account that was never opened, a gap and a bad blob halt there.
    """

    account: str
    store: BlobStore = field(repr=False)
    writer: str = field(repr=False)
    # Held through a whole call, so two threads never build on one number.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # The newest call this process knows is stored, and the account after it.
    number: int = 0
    at: datetime | None = None
    state: SimState | None = None
    # Every order ever placed, as it stands, every client order id, and every fill.
    orders: dict[str, SimOrder] = field(default_factory=dict, repr=False)
    names: set[str] = field(default_factory=set, repr=False)
    fills: list[Fill] = field(default_factory=list, repr=False)
    # What the faults count: the calls of this process, in all and by each read's name.
    calls: int = 0
    reads: dict[str, int] = field(default_factory=dict, repr=False)
    # The account events this process made, and the call each order on its way arrives at.
    fired: set[int] = field(default_factory=set, repr=False)
    due: dict[str, int] = field(default_factory=dict, repr=False)
    _loaded: bool = field(default=False, repr=False)
    _unsure: bool = field(default=False, repr=False)

    @classmethod
    def load(cls, store: BlobStore, account: str, writer: str) -> SimulatedAccount:
        """The log of `account`, unread. `writer` is drawn afresh for each, 32 hex characters."""
        return cls(account, store, writer)

    @staticmethod
    def open(store: BlobStore, account: str, cash: Decimal) -> None:
        """Write call 1, which opens the account flat with `cash`. One already there is done.

        It halts on another opening. A store's refusal is raised as it came.
        """
        if finite("the simulated account's opening cash", cash) < 0:
            raise Halt(f"a simulated account opens with {cash}, below zero")
        state = SimState(cash, {}, (), ())
        key = _key(account, _OPENING)
        data = _encoded(
            SimCall(SIM_FORMAT, account, _OPENING, None, None, SimEvents(), state),
        )
        try:
            store.create(key, data, sha256_of(data))
        except KeyExists:
            if _get(store, key) != data:
                raise Halt(f"{key} holds another opening of the account") from None

    def catch_up(self) -> None:
        """Read the log, the whole of it the first time and what is unsure after that."""
        if self._loaded and not self._unsure:
            return
        if not _WRITER.fullmatch(self.writer):
            raise Halt(f"the simulated account {self.account}'s writer is no writer")
        prefix = _key(self.account, _OPENING).rpartition("/")[0] + "/"
        newest = _key(self.account, self.number) if self.number else None
        try:
            keys = self.store.list(prefix, newest)
            for key in keys:
                number = self.number + 1
                if key != _key(self.account, number):
                    raise Halt(
                        f"the simulated account {self.account}'s call {number} is missing, and "
                        f"the next key is {key}"
                    )
                self._take(self._checked(number, _get(self.store, key)))
        except StoreError as error:
            raise BrokerUnavailable(_UNREACHED, error) from None
        if not self.number:
            raise Halt(
                f"the simulated account {self.account} has no call: it was never opened, or its "
                "log is lost"
            )
        self._loaded, self._unsure = True, False

    def append(self, events: SimEvents, now: datetime) -> None:
        """Store one call's events, and remember them once the store has acknowledged them.

        A store that does not answer raises `BrokerUnavailable`, with nothing
        changed and the log unsure.
        """
        state, number = self.account_now, self.number + 1
        call = SimCall(
            SIM_FORMAT, self.account, number, self.writer, now, events, after(state, events)
        )
        key, data = _key(self.account, number), _encoded(call)
        # Before the create, so whatever it raises leaves the log unsure.
        self._unsure = True
        try:
            try:
                self.store.create(key, data, sha256_of(data))
            except KeyExists as error:
                # These same bytes are this create's own, whose first answer was lost.
                stored = _get(self.store, key)
                if stored != data:
                    self._mine(self._checked(number, stored))
                    # An earlier create of this process that landed late. The next call takes it in.
                    raise error
        except StoreError as error:
            raise BrokerUnavailable(_UNREACHED, error) from None
        self._unsure = False
        self._take(call)

    @property
    def account_now(self) -> SimState:
        if self.state is None:
            raise Halt(f"the simulated account {self.account} has not been read")
        return self.state

    def _mine(self, call: SimCall) -> None:
        if call.writer != self.writer:
            raise Halt(
                f"the simulated account {self.account}'s call {call.call} is another writer's, so "
                "two processes are writing one account"
            )

    def _checked(self, number: int, data: bytes) -> SimCall:
        what = f"the simulated account {self.account}'s call {number}"
        try:
            call = decode(data, SimCall)
        except CodecError as error:
            raise Halt(f"{what} is not a canonical call: {error}") from None
        if call.format != SIM_FORMAT:
            raise Halt(f"{what} has format {call.format}, which this release does not read")
        if (call.account, call.call) != (self.account, number):
            raise Halt(f"{what} holds call {call.call} of {call.account}")
        opening = number == _OPENING
        if (call.writer is None, call.at is None) != (opening, opening):
            raise Halt(f"{what} names the writer {call.writer!r} and the time {call.at}")
        return call

    def _take(self, call: SimCall) -> None:
        """Remember one stored call, or halt where it does not follow from the one before."""
        what, events = f"the simulated account {self.account}'s call {call.call}", call.events
        if call.call == _OPENING:
            opened = SimState(call.after.cash, {}, (), ())
            if call.after != opened or events != SimEvents():
                raise Halt(f"{what} opens an account that is not flat")
        else:
            if self._loaded and call.writer != self.writer:
                self._mine(call)
            if call.after != after(self.account_now, events):
                raise Halt(f"{what} is not the call before it with its events applied")
            if self.at is not None and call.at is not None and call.at < self.at:
                raise Halt(f"{what} is at {call.at.isoformat()}, before the call before it")
        placed = [e.order for e in events.edits if e.order and e.order.id not in self.orders]
        placed.extend(events.placed)
        for at, order in enumerate(placed, start=len(self.orders) + 1):
            if order.id != f"sim-o-{at}":
                raise Halt(f"{what} places {order.id}, not sim-o-{at}")
        fills = [close.fill for close in events.closed if close.fill is not None]
        for at, fill in enumerate(fills, start=len(self.fills) + 1):
            if fill.fill_id != f"sim-f-{at}":
                raise Halt(f"{what} holds the fill {fill.fill_id}, not sim-f-{at}")
        for edit in events.edits:
            if edit.order is not None and edit.order.id is not None:
                self.orders[edit.order.id] = edit.order
            if call.writer == self.writer:
                self.fired.add(edit.count)
        for order in placed:
            self.orders[order.id] = order  # type: ignore[index]
        for order in (*placed, *events.delayed):
            if order.client_order_id is not None:
                self.names.add(order.client_order_id)
        for close in events.closed:
            self.orders[close.id] = ended(self.orders[close.id], close)
        self.fills.extend(fills)
        self.number, self.at, self.state = call.call, call.at, call.after


def _key(account: str, number: int) -> str:
    try:
        return sim_account_key(account, number)
    except ValueError as error:
        raise Halt(f"a simulated account's call has no key: {error}") from None


def _encoded(call: SimCall) -> bytes:
    """The call's bytes, or a halt where a record could not hold a figure of it."""
    try:
        return encode(call)
    except CodecError as error:
        raise Halt(
            f"the simulated account {call.account}'s call {call.call} cannot be written: {error}"
        ) from None


def _get(store: BlobStore, key: str) -> bytes:
    try:
        return store.get(key)
    except BlobMissing:
        raise Halt(f"{key} was there and then missing") from None
