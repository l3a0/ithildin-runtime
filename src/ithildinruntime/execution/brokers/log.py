"""A simulated account as a log of calls, one blob for each call that changed it.

docs/architecture/08-crosscutting-concepts.md's `#### The simulated broker's
account` says what a blob holds. In short:

- A blob holds its call's events and the account after them. An order that ended
  is in the event that ended it and in no later blob, so the log of a long run
  does not grow with its square.
- Every call reads the log forward first, and takes in each blob that follows
  from the one before it, whoever wrote it. So an answer the store lost, and a
  call of another process that takes turns on the account, are both in the next
  answer.
- A create moves what this process remembers only once the store acknowledged it.
  One that finds its number taken has changed nothing, and its call starts over.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType

from ithildinruntime.core import (
    BlobMissing,
    BlobRef,
    BlobStore,
    BrokerOrder,
    BrokerState,
    BrokerUnavailable,
    CodecError,
    Conflict,
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
      the decision that sent it had read a close of that session.
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
    """An order's end: its id, the state it ended in, when, and its fill where it had one.

    `priced_from` is the stored snapshot that gave the fill its price, and `None`
    where there is no fill.
    """

    id: str
    state: BrokerState
    at: datetime
    fill: Fill | None
    priced_from: BlobRef | None


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
    """One blob of the log. Call 1 opens the account, so it names no writer and no time.

    `at` is the account's time, which never goes back, and `clock` the clock's
    own reading, `None` where that was no UTC time. They differ only on a cancel
    under a clock that reads before the account's time, or no UTC time.
    """

    format: int
    account: str
    call: int
    writer: str | None
    at: datetime | None
    clock: datetime | None
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
    """One account's log, as one process holds it for its whole life.

    `load` reads nothing, so no worker fails to start on a log that cannot be
    read. The whole log is read at the first call of a broker over it, and an
    account that was never opened, a gap and a bad blob halt there.
    """

    account: str
    store: BlobStore = field(repr=False)
    writer: str = field(repr=False)
    # Held through a call's own part, because the receive loop runs its handlers in threads. No
    # price is read while it is held.
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # The newest call this process has taken in, and the account after it.
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
    _listed: bool = field(default=False, repr=False)

    @classmethod
    def load(cls, store: BlobStore, account: str, writer: str) -> SimulatedAccount:
        """The log of `account`, unread. `writer` is drawn afresh for each, 32 hex characters.

        Two processes that share one build the same bytes under one clock, and
        each would read one order's answer as its own.
        """
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
            SimCall(SIM_FORMAT, account, _OPENING, None, None, None, SimEvents(), state),
        )
        try:
            store.create(key, data, sha256_of(data))
        except KeyExists:
            if _get(store, key) != data:
                raise Halt(f"{key} holds another opening of the account") from None

    def catch_up(self) -> None:
        """Take in every call stored since the newest one this process knows.

        The first time lists the whole log, so a gap is seen. After that it asks
        for the next number until there is none, which costs one read each time.
        """
        if not _WRITER.fullmatch(self.writer):
            raise Halt(f"the simulated account {self.account}'s writer is no writer")
        try:
            if not self._listed:
                self._read_whole()
            while (data := _there(self.store, _key(self.account, self.number + 1))) is not None:
                self._keep(self._checked(self.number + 1, data))
        except StoreError as error:
            raise BrokerUnavailable(_UNREACHED, error) from None

    def _read_whole(self) -> None:
        prefix = _key(self.account, _OPENING).rpartition("/")[0] + "/"
        newest = _key(self.account, self.number) if self.number else None
        for key in self.store.list(prefix, newest):
            # A folder listed while another process adds to it can show a call and miss the one
            # before it, so the missing one is asked for by its own key before its gap halts.
            while key != (wanted := _key(self.account, self.number + 1)):
                data = _there(self.store, wanted)
                if data is None:
                    raise Halt(
                        f"the simulated account {self.account}'s call {self.number + 1} is "
                        f"missing, and the next key is {key}"
                    )
                self._keep(self._checked(self.number + 1, data))
            self._keep(self._checked(self.number + 1, _get(self.store, key)))
        if not self.number:
            raise Halt(
                f"the simulated account {self.account} has no call: it was never opened, or its "
                "log is lost"
            )
        self._listed = True

    def append(
        self, events: SimEvents, now: datetime, clock: datetime | None
    ) -> type[StoreError] | None:
        """Store one call's events, and give what took its number, or `None` once it is stored.

        One whose number is taken changed nothing, and its caller reads forward
        and starts over. A store that does not answer raises
        `BrokerUnavailable`, with nothing this process remembers changed, and
        the next call finds whether the blob landed.
        """
        call = SimCall(
            SIM_FORMAT,
            self.account,
            self.number + 1,
            self.writer,
            now,
            clock,
            events,
            after(self.account_now, events),
        )
        key, data = _key(self.account, call.call), _encoded(call)
        # Before the create, so a call that could not be read back is never stored.
        self._follows(call)
        try:
            try:
                self.store.create(key, data, sha256_of(data))
            except KeyExists:
                # These same bytes are this create's own, whose first answer was lost.
                if _get(self.store, key) != data:
                    return KeyExists
        except Conflict:
            # S3 says it raced another write and did not land.
            return Conflict
        except StoreError as error:
            raise BrokerUnavailable(_UNREACHED, error) from None
        self._keep(call)
        return None

    @property
    def account_now(self) -> SimState:
        if self.state is None:
            raise Halt(f"the simulated account {self.account} has not been read")
        return self.state

    def _checked(self, number: int, data: bytes) -> SimCall:
        what = f"the simulated account {self.account}'s call {number}"
        # A blob a later release wrote may hold fields this one cannot decode, and its halt
        # should say so.
        written = _written_format(data)
        if written not in (None, SIM_FORMAT):
            raise Halt(f"{what} has format {written}, which this release does not read")
        try:
            call = decode(data, SimCall)
        except CodecError as error:
            raise Halt(f"{what} is not a canonical call: {error}") from None
        if (call.account, call.call) != (self.account, number):
            raise Halt(f"{what} holds call {call.call} of {call.account}")
        opening = number == _OPENING
        if (call.writer is None, call.at is None) != (opening, opening):
            raise Halt(f"{what} names the writer {call.writer!r} and the time {call.at}")
        self._follows(call)
        return call

    def _follows(self, call: SimCall) -> None:
        """Halt on a call that does not follow from the one before it."""
        what, events = f"the simulated account {self.account}'s call {call.call}", call.events
        if call.call == _OPENING:
            if call.after != SimState(call.after.cash, {}, (), ()) or events != SimEvents():
                raise Halt(f"{what} opens an account that is not flat")
            return
        try:
            applied = after(self.account_now, events)
            unended = after(self.account_now, replace(events, closed=()))
        except Halt as halt:
            raise Halt(f"{what}: {halt.reason}") from None
        if call.after != applied:
            raise Halt(f"{what} is not the call before it with its events applied")
        if self.at is not None and call.at is not None and call.at < self.at:
            raise Halt(f"{what} is at {call.at.isoformat()}, before the call before it")
        open_now = {order.id for order in self.account_now.open_orders}
        for edit in events.edits:
            if (
                edit.order is not None
                and edit.order.id in self.orders
                and (edit.order.id not in open_now)
            ):
                raise Halt(f"{what} edits {edit.order.id}, which is not open")
        waiting = {order.client_order_id for order in self.account_now.delayed}
        seen: set[str | None] = set()
        for order in (*events.placed, *events.delayed):
            name = order.client_order_id
            if order.id in self.orders or order.id in seen and order.id is not None:
                raise Halt(f"{what} places {order.id}, which it holds already")
            # An order on its way comes once more, as it arrives.
            arrives = name in waiting and order.id is not None
            if name in seen or name in self.names and not arrives:
                raise Halt(f"{what} places {name} a second time")
            seen |= {name, order.id}
        opened = {order.id: order for order in unended.open_orders}
        for close in events.closed:
            if not _fits(opened[close.id], close):
                raise Halt(f"{what} ends {close.id} in a way it cannot end")
        for at, name in enumerate(self.new(events), start=len(self.orders) + 1):
            if name != f"sim-o-{at}":
                raise Halt(f"{what} places {name}, not sim-o-{at}")
        for at, fill in enumerate(_fills(events), start=len(self.fills) + 1):
            if fill.fill_id != f"sim-f-{at}":
                raise Halt(f"{what} holds the fill {fill.fill_id}, not sim-f-{at}")

    def new(self, events: SimEvents) -> list[str]:
        """The id of each order `events` place, in turn, and none twice."""
        placed = [e.order.id for e in events.edits if e.order is not None]
        placed += [order.id for order in events.placed]
        return [name for name in dict.fromkeys(placed) if name and name not in self.orders]

    def _keep(self, call: SimCall) -> None:
        """Remember one stored call."""
        events = call.events
        for edit in events.edits:
            if edit.order is not None and edit.order.id is not None:
                self.orders[edit.order.id] = edit.order
            if call.writer == self.writer:
                self.fired.add(edit.count)
        for order in events.placed:
            self.orders[order.id] = order  # type: ignore[index]
        for order in (*events.placed, *events.delayed):
            if order.client_order_id is not None:
                self.names.add(order.client_order_id)
        for close in events.closed:
            self.orders[close.id] = ended(self.orders[close.id], close)
        self.fills.extend(_fills(events))
        self.number, self.at, self.state = call.call, call.at, call.after


def _fills(events: SimEvents) -> list[Fill]:
    return [close.fill for close in events.closed if close.fill is not None]


def _fits(order: SimOrder, close: SimClose) -> bool:
    """Whether an end is one its order can have, and its fill, if any, is of it."""
    fill = close.fill
    if fill is None or close.priced_from is None:
        unfilled = (BrokerState.CANCELLED, BrokerState.EXPIRED, BrokerState.REJECTED)
        return fill is None and close.priced_from is None and close.state in unfilled
    whole = fill.qty == order.qty
    return (
        (fill.broker_order_id, fill.client_order_id, fill.instrument)
        == (order.id, order.client_order_id, order.instrument)
        and fill.qty != 0
        and (fill.qty > 0) == (order.qty > 0)
        and fill.qty.copy_abs() <= order.qty.copy_abs()
        and close.state is (BrokerState.FILLED if whole else BrokerState.EXPIRED)
    )


def _written_format(data: bytes) -> int | None:
    """The `format` a blob names, read before it is decoded."""
    try:
        parsed = json.loads(data)
    except (ValueError, RecursionError):
        return None
    found = parsed.get("format") if isinstance(parsed, dict) else None
    return found if type(found) is int else None


def _there(store: BlobStore, key: str) -> bytes | None:
    try:
        return store.get(key)
    except BlobMissing:
        return None


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
