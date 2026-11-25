"""The simulated broker a backtest and shadow trade through, written to `core.Broker`.

docs/architecture/05-building-blocks.md's `### One simulator serves backtest and
shadow` says what it is for. It is an adapter of the platform's own, with one
fill rule by order type and no branch on a mode or a test.

- **A market-on-close order at `cls`** rests when it comes at or before its
  session's close, and is rejected after it. It fills whole at that session's
  official close, at the first call after it that reads a price and finds that
  one. Until then it is working, and it can be cancelled up to the close itself.
- **Any other order is rejected.** The day's market order gets its fill with
  #155, which reads the decision's quote.
- **It moves only forward.** A call whose clock reads before the log's newest
  call, or before the decision its prices are of, halts.
- **No clock and no price stops a cancel, or a read of the orders**, as the
  charter's risk engineer vetoes a check on input that can stop a cancel. Neither
  reads a price. A cancel is judged by the clock, or by the log's newest call
  where the clock gives no UTC time. `open_orders`, `orders`, `lookup` and
  `find_order` give the log as it stands where the clock would halt them.

Every call reads the log forward. Every call but a cancel then does what is due:
what a fault has someone do to the account, an order that arrives late, a
rejection that comes later, and the closes. A read of the orders leaves each close
that needs a price to the next call that reads one. A call then does its own part, stores
one blob where anything changed, and answers. Where its create fails, nothing this
process remembers has changed, and the next call finds whether the blob landed.
One whose number another call took starts over on what it then reads.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
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
    InstrumentId,
    OrderIntent,
    OrdersRead,
    OrderType,
    Outcome,
    PositionsRead,
    ReceivedPage,
    RecordsRead,
    Session,
    StoreError,
    TimeInForce,
    encode,
    error_name,
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
# The reads that tell a cancel what there is to cancel.
_OF_ORDERS = frozenset({"open_orders", "orders", "lookup"})


class _Unpriced(Exception):
    """A close this call needs and has not read, which it reads with the log let go."""

    def __init__(self, instrument: InstrumentId, session: date, why: str) -> None:
        super().__init__(why)
        self.asked = instrument, session
        self.why = why


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
        # Each close this decision's prices gave, so none is read while the log is held.
        self._read_closes: dict[tuple[InstrumentId, date], Decimal | None] = {}

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

        self._counted()
        if fault is OrderFault.LOSE_REQUEST:
            raise BrokerUnavailable("request", TimeoutError)
        _, sent_at, _ = read_order_id(intent.client_order_id)
        if sent_at != self._prices.decision_at:
            raise Halt(
                f"the order {intent.client_order_id} is of the decision at {_time(sent_at)}, and "
                f"the prices of account {self.alias} are of {_time(self._prices.decision_at)}"
            )
        return self._call(enter)

    def cancel(self, broker_order_id: str) -> Ack:
        self._counted()
        lost: list[type[StoreError]] = []
        while len(lost) <= _STARTS_OVER:
            with self._log.lock:
                self._log.catch_up()
                now, clock = self._stamp()
                order = self._log.orders.get(broker_order_id)
                if order is None:
                    return _rejected("the account holds no such order", now)
                if order.state is not BrokerState.WORKING or (clock or now) > _close_of(order):
                    return _found(order, now, Outcome.REJECTED)
                close = SimClose(broker_order_id, BrokerState.CANCELLED, now, None, None)
                taken = self._log.append(SimEvents(closed=(close,)), now, clock)
                if taken is None:
                    return _found(self._log.orders[broker_order_id], now)
            lost.append(taken)
        raise BrokerUnavailable(_CONTENDED, lost[-1])

    def _stamp(self) -> tuple[datetime, datetime | None]:
        """The later of the clock and the log's newest call, and the clock's own reading.

        It is the time of a call the clock does not stop, and `None` stands for a
        clock that gives no UTC time.
        """
        clock: datetime | None = None
        try:
            read: object = self._clock.now()
            if isinstance(read, datetime) and read.utcoffset() == timedelta(0):
                clock = read
        except Exception:
            # A clock that gives no time stops no cancel and no read of the orders.
            read = None
        seen = [at for at in (clock, self._log.at) if at is not None]
        if not seen:
            # An account with no call but its opening holds no order.
            raise Halt(f"the simulated account {self.alias}'s clock reads {read!r}, no UTC time")
        return max(seen), clock

    # The reads

    def positions(self) -> PositionsRead:
        now, state, short = self._read("positions", lambda log: log.account_now)
        held = _cut(sorted(state.positions.items()), short)
        body = encode({instrument: f"{shares:f}" for instrument, shares in held})
        return PositionsRead(dict(held), *_page(body, now, short))

    def open_orders(self) -> OrdersRead:
        now, state, short = self._read("open_orders", lambda log: log.account_now)
        return _orders([order.copy() for order in state.open_orders], now, short)

    def account(self) -> CashRead:
        now, state, short = self._read("account", lambda log: log.account_now)
        return CashRead(state.cash, *_page(encode({"cash": f"{state.cash:f}"}), now, short))

    def fills(self, since: datetime) -> FillsRead:
        now, fills, short = self._read("fills", lambda log: tuple(log.fills))
        rows = _cut([fill for fill in fills if fill.at >= since], short)
        return FillsRead(tuple(rows), *_page(_listed(rows), now, short))

    def orders(self, since: datetime) -> OrdersRead:
        now, held, short = self._read("orders", lambda log: tuple(log.orders.values()))
        return _orders([o.copy() for o in held if o.submitted_at >= since], now, short)

    def find_order(self, intent: OrderIntent, since: datetime, mapped: object) -> FoundOrder:
        """The order under the intent's own id, which the account takes once."""
        found = self.orders(since)
        rows = tuple(o for o in found.orders if o.client_order_id == intent.client_order_id)
        answer = Outcome.ACCEPTED if rows else Outcome.NOT_FOUND
        return FoundOrder(answer, rows, found.whole, found.received[0].body)

    def lookup(self, broker_order_id: str) -> Ack:
        """The order as it stands, `accepted` with its state on the copy, or `not_found`."""
        now, order, _ = self._read("lookup", lambda log: log.orders.get(broker_order_id))
        if order is None:
            return Ack(Outcome.NOT_FOUND, None, None, _reason("no such order"), now)
        return _found(order, now)

    def activities(self, since: datetime) -> RecordsRead:
        """No record, until #151 gives a record a type."""
        now, _, short = self._read("activities", lambda log: None)
        return RecordsRead(*_page(b"[]", now, short))

    def clock(self) -> datetime:
        return self._clock.now()

    def _read[R](
        self, name: str, part: Callable[[SimulatedAccount], R]
    ) -> tuple[datetime, R, bool]:
        """Do what is due and give a part of the account, and whether a fault cuts it short."""
        log = self._log
        self._counted()
        with log.lock:
            log.reads[name] = log.reads.get(name, 0) + 1
            fault = self._faults.reads.get((name, log.reads[name]))
        if fault is ReadFault.FAILS:
            raise BrokerUnavailable(name, TimeoutError)
        now, found = self._call(
            lambda now, events: (events, lambda: (now, part(log))), name in _OF_ORDERS
        )
        return now, found, fault is ReadFault.SHORT

    # What every call does around its own part

    def _counted(self) -> None:
        with self._log.lock:
            self._log.calls += 1

    def _call[R](
        self, work: Callable[[datetime, SimEvents], _Done[R]], of_orders: bool = False
    ) -> R:
        """Do what is due and then `work`, store both as one call, and answer.

        `work` gives every event of the call and what reads its answer once they
        are stored. A close it needs is read with the log let go, so a price that
        is slow holds no other call.
        """
        lost: list[type[StoreError]] = []
        while len(lost) <= _STARTS_OVER:
            try:
                with self._log.lock:
                    now, due = self._due(of_orders)
                    events, answer = work(now, due)
                    changed = events != SimEvents()
                    taken = self._log.append(events, now, now) if changed else None
                    if taken is None:
                        return answer()
                lost.append(taken)
            except _Unpriced as unpriced:
                try:
                    found = self._prices.close(*unpriced.asked)
                except Halt as halt:
                    raise Halt(f"{unpriced.why}: {halt.reason}") from None
                self._read_closes[unpriced.asked] = found
        raise BrokerUnavailable(_CONTENDED, lost[-1])

    def _close_price(self, instrument: InstrumentId, session: date, why: str) -> Decimal | None:
        """A session's close as this decision's prices give it, read ahead of the log's lock."""
        if (instrument, session) not in self._read_closes:
            raise _Unpriced(instrument, session, why)
        return self._read_closes[instrument, session]

    def _now(self) -> datetime:
        """The clock's time, or a halt where it is none this account can go on from."""
        log = self._log
        try:
            now = require_utc(f"the simulated account {self.alias}'s clock", self._clock.now())
        except Halt:
            raise
        except Exception as error:
            raise Halt(
                f"the simulated account {self.alias}'s clock raised {error_name(error)}"
            ) from None
        for what, at in (("newest call", log.at), ("prices' decision", self._prices.decision_at)):
            if at is not None and now < at:
                raise Halt(
                    f"the clock reads {_time(now)}, before the simulated account {self.alias}'s "
                    f"{what} at {_time(at)}"
                )
        return now

    def _due(self, of_orders: bool) -> tuple[datetime, SimEvents]:
        """The call's time, and every event that comes before the call's own."""
        log = self._log
        # The log first, since another process can store a call while the clock is read.
        log.catch_up()
        try:
            now = self._now()
        except Halt:
            if not of_orders:
                raise
            return self._stamp()[0], SimEvents()
        events = SimEvents()
        for count in sorted(self._faults.events):
            if count <= log.calls and count not in log.fired:
                edit = self._edit(count, after(log.account_now, events), events, now)
                events = replace(events, edits=(*events.edits, edit))
        events = self._arrivals(events, now)
        return now, self._closes(events, now, of_orders)

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
                # No decision sent it, so no price says what one had read.
                order = replace(self._order_of(intent, now, False), client_order_id=None)
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

    def _closes(self, events: SimEvents, now: datetime, of_orders: bool) -> SimEvents:
        """Each open order that ends now: rejected later, filled at its close, or expired."""
        for order in after(self._log.account_now, events).open_orders:
            close = self._close(order, events, now, of_orders)
            if close is not None:
                events = replace(events, closed=(*events.closed, close))
        return events

    def _close(
        self, order: SimOrder, events: SimEvents, now: datetime, of_orders: bool
    ) -> SimClose | None:
        closed = _close_of(order)
        fault = self._faults.orders.get(order.client_order_id or "")
        if fault is OrderFault.REJECT_LATER:
            return SimClose(order.id or "", BrokerState.REJECTED, now, None, None)
        if now <= closed:
            return None
        if fault is OrderFault.FILL_NONE:
            return SimClose(order.id or "", BrokerState.EXPIRED, closed, None, None)
        if of_orders:
            return None
        why = (
            f"the simulated account {self.alias}'s order {order.id} is past its close and has no "
            "price"
        )
        price = self._close_price(order.instrument, order.session, why)
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
            order = self._order_of(intent, now, self._had_close(intent.instrument))
            return replace(events, delayed=(*events.delayed, order)), None
        if why := refusal(intent, now, self._session.close):
            return events, _rejected(why, now)
        order = self._order_of(intent, now, self._had_close(intent.instrument))
        placed = replace(order, id=self._next_id(events))
        return replace(events, placed=(*events.placed, placed)), _found(placed, now)

    @property
    def _session(self) -> Session:
        """The session of the decision these prices are for."""
        return load_sessions().opened_by(self._prices.decision_at)

    def _order_of(self, intent: OrderIntent, now: datetime, read_close: bool) -> SimOrder:
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
            read_close=read_close,
        )

    def _had_close(self, instrument: InstrumentId) -> bool:
        """Whether the decision had read a close of its own session."""
        why = f"the simulated account {self.alias} cannot read the prices of its order"
        return self._close_price(instrument, self._session.day, why) is not None

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
