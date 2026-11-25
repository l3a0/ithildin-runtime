"""The simulated broker a backtest and shadow trade through, written to `core.Broker`.

docs/architecture/05-building-blocks.md's `### One simulator serves backtest and
shadow` says what it is for. It is an adapter of the platform's own, with one
fill rule by order type and no branch on a mode or a test.

- **A market-on-close order at `cls`** rests when it comes at or before its
  session's close, and is rejected after it. It fills whole at that session's
  official close, at the first call after the close whose prices hold it. Until
  then it is working, and it can be cancelled up to the close itself.
- **Any other order is rejected.** The day's market order gets its fill with
  #155, which reads the decision's quote.
- **It moves only forward.** A call whose clock reads before the log's newest
  call, or before the decision its prices are of, halts.
- **No check on an input stops a cancel**, as the charter's risk engineer vetoes.
  A cancel reads no price. It is judged at the account's time, which is the
  latest time any call has shown it, so a clock that went back moves nothing.

Every call reads the log forward. Every call but a cancel then does what is due:
what a fault has someone do to the account, an order that arrives late, a
rejection that comes later, and the closes. A call then does its own part, stores
one blob where anything changed, and answers. A call that fails has changed
nothing, and one whose number another call took starts over on what it then reads.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from ithildinruntime.core import (
    READ_LIMIT,
    Ack,
    BlobStore,
    BrokerOrder,
    BrokerState,
    BrokerUnavailable,
    CashRead,
    Clock,
    Fill,
    FillBasis,
    FillsRead,
    FoundOrder,
    Halt,
    KeyExists,
    OrderIntent,
    OrdersRead,
    OrderType,
    Outcome,
    PositionsRead,
    ReceivedPage,
    RecordsRead,
    Session,
    TimeInForce,
    encode,
    exact_total,
    exceeds_places,
    load_sessions,
    places,
    read_order_id,
    require_utc,
)

from .faults import (
    EditCash,
    EditOrder,
    EditPosition,
    Faults,
    FillPart,
    ForeignOrder,
    Late,
    OrderFault,
    ReadFault,
)
from .log import SimClose, SimEdit, SimEvents, SimOrder, SimState, SimulatedAccount, after
from .prices import Prices

__all__ = ["SimulatedBroker", "refusal"]

_ZERO = Decimal(0)
_SHARE_PLACES = places(OrderIntent, "qty")
_PRICE_PLACES = places(Fill, "price")
_CONTENDED = "contended_log"
# How many times a call starts over on a number another call took.
_STARTS_OVER = 100


def refusal(order: OrderIntent | SimOrder, at: datetime, close: datetime) -> str | None:
    """Why the account takes no such order at `at`, or `None` for one that rests until `close`."""
    if (order.type, order.time_in_force) != (OrderType.MARKET_ON_CLOSE, TimeInForce.CLS):
        # The day's market order among them, whose fill is #155's.
        return f"no {order.type} order is taken for {order.time_in_force}"
    if not order.qty.is_finite() or exceeds_places(order.qty, _SHARE_PLACES):
        return f"{order.qty} is no number of shares at {_SHARE_PLACES} places"
    if order.qty == 0:
        return "the order is for zero shares"
    if at > close:
        return f"it came at {_time(at)}, after its session's close at {_time(close)}"
    return None


# A call's events, and what reads its answer once they are stored.
type _Done[R] = tuple[SimEvents, Callable[[], R]]


class SimulatedBroker:
    """One decision's broker over one account's log.

    `log` is the worker's own, kept across decisions, and `prices` are the
    decision's. So no method of `Broker` gains an argument.
    """

    def __init__(self, log: SimulatedAccount, clock: Clock, prices: Prices, faults: Faults) -> None:
        self._log = log
        self._clock = clock
        self._prices = prices
        self._faults = faults

    @staticmethod
    def open(store: BlobStore, account: str, cash: Decimal) -> None:
        """Open an account flat with `cash`, which no other call does. It is safe to repeat."""
        SimulatedAccount.open(store, account, cash)

    @property
    def alias(self) -> str:
        return self._log.account

    # The calls that move the account

    def submit(self, intent: OrderIntent) -> Ack:
        fault = self._faults.orders.get(intent.client_order_id)

        def enter(now: datetime, events: SimEvents) -> _Done[Ack]:
            if fault is OrderFault.REJECT:
                return events, lambda: _rejected("the account takes no order under this name", now)
            entered = _changed(intent, fault)
            events, answer = self._enter(entered, events, now, fault)
            if fault is OrderFault.DUPLICATE:
                events, answer = self._enter(entered, events, now, fault)
            return events, lambda: self._answered(intent, fault, answer)

        with self._log.lock:
            self._log.calls += 1
            if fault is OrderFault.LOSE_REQUEST:
                raise BrokerUnavailable("request", TimeoutError)
            _, sent_at, _ = read_order_id(intent.client_order_id)
            if sent_at != self._prices.decision_at:
                raise Halt(
                    f"the order {intent.client_order_id} is of the decision at {_time(sent_at)}, "
                    f"and the prices of account {self.alias} are of "
                    f"{_time(self._prices.decision_at)}"
                )
            return self._call(enter)

    def cancel(self, broker_order_id: str) -> Ack:
        with self._log.lock:
            self._log.calls += 1
            for _ in range(_STARTS_OVER + 1):
                self._log.catch_up()
                at, clock = self._account_time()
                order = self._log.orders.get(broker_order_id)
                if order is None:
                    return _rejected("the account holds no such order", at)
                if order.state is not BrokerState.WORKING or at > _close_of(order):
                    return _found(order, at, Outcome.REJECTED)
                close = SimClose(broker_order_id, BrokerState.CANCELLED, at, None, None)
                if self._log.append(SimEvents(closed=(close,)), at, clock):
                    return _found(self._log.orders[broker_order_id], at)
            raise BrokerUnavailable(_CONTENDED, KeyExists)

    def _account_time(self) -> tuple[datetime, datetime | None]:
        """The later of the clock and the log's newest call, and the clock's own reading."""
        try:
            read: object = self._clock.now()
        except Exception:
            read = None
        clock = read if isinstance(read, datetime) and read.utcoffset() == timedelta(0) else None
        seen = [at for at in (clock, self._log.at) if at is not None]
        if not seen:
            # An account with no call but its opening holds nothing to cancel.
            raise Halt(f"the simulated broker's clock reads {read!r}, not a UTC time")
        return max(seen), clock

    # The reads

    def positions(self) -> PositionsRead:
        with self._log.lock:
            now, state, short = self._read("positions")
            held = _cut(sorted(state.positions.items()), short)
            body = encode({instrument: f"{shares:f}" for instrument, shares in held})
            return PositionsRead(dict(held), *_page(body, now, short))

    def open_orders(self) -> OrdersRead:
        with self._log.lock:
            now, state, short = self._read("open_orders")
            return _orders([order.copy() for order in state.open_orders], now, short)

    def account(self) -> CashRead:
        with self._log.lock:
            now, state, short = self._read("account")
            return CashRead(state.cash, *_page(encode({"cash": f"{state.cash:f}"}), now, short))

    def fills(self, since: datetime) -> FillsRead:
        with self._log.lock:
            now, _, short = self._read("fills")
            rows = _cut([fill for fill in self._log.fills if fill.at >= since], short)
            return FillsRead(tuple(rows), *_page(_listed(rows), now, short))

    def orders(self, since: datetime) -> OrdersRead:
        with self._log.lock:
            return self._orders_since(since)

    def find_order(self, intent: OrderIntent, since: datetime, mapped: object) -> FoundOrder:
        """The order under the intent's own id, which the account takes once."""
        with self._log.lock:
            found = self._orders_since(since)
            rows = tuple(o for o in found.orders if o.client_order_id == intent.client_order_id)
            answer = Outcome.ACCEPTED if rows else Outcome.NOT_FOUND
            return FoundOrder(answer, rows, found.whole, found.received[0].body)

    def lookup(self, broker_order_id: str) -> Ack:
        """The order as it stands, `accepted` with its state on the copy, or `not_found`."""
        with self._log.lock:
            now, _, _ = self._read("lookup")
            order = self._log.orders.get(broker_order_id)
            if order is None:
                return Ack(Outcome.NOT_FOUND, None, None, _reason("no such order"), now)
            return _found(order, now)

    def activities(self, since: datetime) -> RecordsRead:
        """No record, until #151 gives a record a type."""
        with self._log.lock:
            now, _, short = self._read("activities")
            return RecordsRead(*_page(b"[]", now, short))

    def clock(self) -> datetime:
        return self._clock.now()

    def _orders_since(self, since: datetime) -> OrdersRead:
        now, _, short = self._read("orders")
        placed = [o.copy() for o in self._log.orders.values() if o.submitted_at >= since]
        return _orders(placed, now, short)

    def _read(self, name: str) -> tuple[datetime, SimState, bool]:
        """Do what is due and give the account, and whether a fault cuts this read short."""
        log = self._log
        log.calls += 1
        log.reads[name] = log.reads.get(name, 0) + 1
        fault = self._faults.reads.get((name, log.reads[name]))
        if fault is ReadFault.FAILS:
            raise BrokerUnavailable(name, TimeoutError)
        now = self._call(lambda now, events: (events, lambda: now))
        return now, log.account_now, fault is ReadFault.SHORT

    # What every call does around its own part

    def _call[R](self, work: Callable[[datetime, SimEvents], _Done[R]]) -> R:
        """Do what is due and then `work`, store both as one call, and answer.

        `work` gives every event of the call and what reads its answer once they
        are stored.
        """
        for _ in range(_STARTS_OVER + 1):
            now, due = self._due()
            events, answer = work(now, due)
            if events == SimEvents() or self._log.append(events, now, now):
                return answer()
        raise BrokerUnavailable(_CONTENDED, KeyExists)

    def _due(self) -> tuple[datetime, SimEvents]:
        """The call's time, and every event that comes before the call's own."""
        log = self._log
        # The log first, since another process can store a call while the clock is read.
        log.catch_up()
        now = require_utc("the simulated broker's clock", self._clock.now())
        for what, at in (("newest call", log.at), ("prices' decision", self._prices.decision_at)):
            if at is not None and now < at:
                raise Halt(
                    f"the clock reads {_time(now)}, before the simulated account {self.alias}'s "
                    f"{what} at {_time(at)}"
                )
        events = SimEvents()
        for count in sorted(self._faults.events):
            if count <= log.calls and count not in log.fired:
                edit = self._edit(count, after(log.account_now, events), events, now)
                events = replace(events, edits=(*events.edits, edit))
        events = self._arrivals(events, now)
        return now, self._closes(events, now)

    def _edit(self, count: int, state: SimState, events: SimEvents, now: datetime) -> SimEdit:
        """What someone did to the account by hand, as the part it left."""
        event = self._faults.events[count]
        match event:
            case EditCash(by):
                cash = exact_total("the simulated cash", (state.cash, by))
                return SimEdit(count, cash, None, None, None)
            case EditPosition(instrument, by):
                held = state.positions.get(instrument, _ZERO)
                shares = exact_total(f"the simulated position in {instrument}", (held, by))
                return SimEdit(count, None, instrument, shares, None)
            case EditOrder(broker_order_id, qty):
                for order in state.open_orders:
                    if order.id == broker_order_id:
                        return SimEdit(count, None, None, None, replace(order, qty=qty))
                raise Halt(f"the fault at call {count} edits {broker_order_id}, which is not open")
            case ForeignOrder(instrument, qty):
                intent = OrderIntent(
                    "", "", instrument, qty, OrderType.MARKET_ON_CLOSE, TimeInForce.CLS
                )
                if why := refusal(intent, now, self._session.close):
                    raise Halt(f"the fault at call {count} places an order that is refused: {why}")
                order = replace(self._order_of(intent, now), client_order_id=None)
                return SimEdit(count, None, None, None, replace(order, id=self._next_id(events)))

    def _arrivals(self, events: SimEvents, now: datetime) -> SimEvents:
        """Each order on its way whose call has come, placed or missed."""
        log = self._log
        for order in log.account_now.delayed:
            name = order.client_order_id or ""
            fault = self._faults.orders.get(name)
            later = fault.calls if isinstance(fault, Late) else 0
            # One read back from the log counts from this process's first call.
            if log.calls < log.due.setdefault(name, log.calls - 1 + later):
                continue
            if refusal(order, now, _close_of(order)) is not None:
                events = replace(events, missed=(*events.missed, name))
                continue
            placed = replace(order, id=self._next_id(events), submitted_at=now)
            events = replace(events, placed=(*events.placed, placed))
        return events

    def _closes(self, events: SimEvents, now: datetime) -> SimEvents:
        """Each open order that ends now: rejected later, filled at its close, or expired."""
        for order in after(self._log.account_now, events).open_orders:
            try:
                close = self._close(order, events, now)
            except Halt as halt:
                raise Halt(
                    f"the simulated account {self.alias}'s order {order.id} is past its close and "
                    f"has no price: {halt.reason}"
                ) from None
            if close is not None:
                events = replace(events, closed=(*events.closed, close))
        return events

    def _close(self, order: SimOrder, events: SimEvents, now: datetime) -> SimClose | None:
        closed = _close_of(order)
        fault = self._faults.orders.get(order.client_order_id or "")
        if fault is OrderFault.REJECT_LATER:
            return SimClose(order.id or "", BrokerState.REJECTED, now, None, None)
        if now <= closed:
            return None
        if fault is OrderFault.FILL_NONE:
            return SimClose(order.id or "", BrokerState.EXPIRED, closed, None, None)
        price = self._prices.close(order.instrument, order.session)
        if price is None:
            return None
        qty = order.qty
        if isinstance(fault, FillPart) and fault.qty < qty.copy_abs():
            qty = fault.qty.copy_sign(qty)
        number = len(self._log.fills) + sum(c.fill is not None for c in events.closed) + 1
        fill = Fill(
            fill_id=f"sim-f-{number}",
            correction_id=None,
            broker_order_id=order.id or "",
            client_order_id=order.client_order_id,
            instrument=order.instrument,
            qty=qty,
            price=price,
            at_text=_time(closed),
            at=closed,
            received_at=now,
            excess_places=("price",) if exceeds_places(price, _PRICE_PLACES) else (),
            basis=FillBasis.SIGNAL_CLOSE if order.read_close else FillBasis.OFFICIAL_CLOSE,
        )
        state = BrokerState.FILLED if qty == order.qty else BrokerState.EXPIRED
        return SimClose(order.id or "", state, closed, fill, self._prices.snapshot())

    # One order as it enters

    def _enter(
        self, intent: OrderIntent, events: SimEvents, now: datetime, fault: object
    ) -> tuple[SimEvents, Ack | None]:
        """The account's own answer to one order, and what it did. A late one has no answer."""
        name = intent.client_order_id
        entering = {o.client_order_id for o in (*events.placed, *events.delayed)}
        if name in self._log.names or name in entering:
            why = f"the account already holds an order under the id {name}"
            return events, Ack(Outcome.UNKNOWN, None, None, _reason(why), now)
        if isinstance(fault, Late):
            return replace(events, delayed=(*events.delayed, self._order_of(intent, now))), None
        if why := refusal(intent, now, self._session.close):
            return events, _rejected(why, now)
        placed = replace(self._order_of(intent, now), id=self._next_id(events))
        return replace(events, placed=(*events.placed, placed)), _found(placed, now)

    @property
    def _session(self) -> Session:
        """The session of the decision these prices are for."""
        return load_sessions().opened_by(self._prices.decision_at)

    def _order_of(self, intent: OrderIntent, now: datetime) -> SimOrder:
        session = self._session
        return SimOrder(
            id=None,
            client_order_id=intent.client_order_id,
            instrument=intent.instrument,
            qty=intent.qty,
            filled=_ZERO,
            type=intent.type,
            time_in_force=intent.time_in_force,
            state=BrokerState.WORKING,
            submitted_at=now,
            session=session.day,
            read_close=self._prices.close(intent.instrument, session.day) is not None,
        )

    def _answered(self, intent: OrderIntent, fault: object, answer: Ack | None) -> Ack:
        """What the caller hears of an order the account has dealt with."""
        if isinstance(fault, Late) and answer is None:
            self._log.due[intent.client_order_id] = self._log.calls + fault.calls
        if answer is None or fault is OrderFault.LOSE_ANSWER:
            raise BrokerUnavailable("answer", TimeoutError)
        return answer

    def _next_id(self, events: SimEvents) -> str:
        return f"sim-o-{len(self._log.orders) + len(self._log.new(events)) + 1}"


def _close_of(order: SimOrder) -> datetime:
    return load_sessions().session(order.session).close


def _changed(intent: OrderIntent, fault: object) -> OrderIntent:
    """The order as a fault changes it on its way in, which the account's own rule then answers."""
    if fault is OrderFault.BAD_TYPE:
        return replace(intent, type=OrderType.MARKET)
    if fault is OrderFault.BAD_QTY:
        return replace(intent, qty=_ZERO)
    return intent


def _time(at: datetime) -> str:
    """A UTC time as the codec writes one."""
    return at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _reason(text: str) -> bytes:
    return encode({"reason": text})


def _rejected(why: str, now: datetime) -> Ack:
    return Ack(Outcome.REJECTED, None, None, _reason(why), now)


def _found(order: SimOrder, now: datetime, outcome: Outcome = Outcome.ACCEPTED) -> Ack:
    copy = order.copy()
    return Ack(outcome, copy.broker_order_id, copy, encode(copy), now)


def _cut[T](rows: Sequence[T], short: bool) -> Sequence[T]:
    return rows[:-1] if short else rows


def _listed(rows: Sequence[object]) -> bytes:
    return b"[" + b",".join(encode(row) for row in rows) + b"]"


def _page(body: bytes, now: datetime, short: bool) -> tuple[tuple[ReceivedPage, ...], bool]:
    """A read's one page, and whether the read is whole."""
    return (ReceivedPage(body, now),), not short and len(body) <= READ_LIMIT


def _orders(rows: Sequence[BrokerOrder], now: datetime, short: bool) -> OrdersRead:
    kept = _cut(rows, short)
    return OrdersRead(tuple(kept), *_page(_listed(kept), now, short))
