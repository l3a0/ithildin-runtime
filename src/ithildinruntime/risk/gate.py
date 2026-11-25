"""The risk gate: the one check on the one order path.

`check` answers one order and `loss` reads the day's loss with none. Both are
pure functions of what they are handed and of the stress records the gate was
built with, with no store, no clock and no mode, so one order checked twice on
the same inputs gets one answer, in a backtest and live alike.
docs/architecture/05-building-blocks.md's `#### The risk gate holds no state of
its own` draws the inputs.

Two outcomes are kept apart.

1. **A halt** says an input is absent, stale or invalid, and raises `Halt`
   (hard limit 3). Every halt comes before every refusal, so a refusal never
   hides one.
2. **A refusal** says a limit worked, and is a `Verdict` naming it. The one
   state read as a refusal is a stop that could not be read.

A figure is compared exactly and rounded only to be recorded. The worst loss
rounds up, because a loss recorded smaller than it is would understate it, and
every other figure rounds half to even.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from typing import NamedTuple

from ithildinruntime.core import (
    BeforeClose,
    Book,
    Constraints,
    Correction,
    Effect,
    Exposure,
    GateInputs,
    Halt,
    InstrumentId,
    Limit,
    LossReading,
    OrderIntent,
    OrderType,
    PastLimit,
    Session,
    Snapshot,
    StopState,
    StressRecords,
    TimeInForce,
    Valuation,
    Verdict,
    classify,
    exact_product,
    exact_total,
    exposure,
    finite,
    held,
    hold_to_record,
    load_sessions,
    mark,
    places,
    position,
    quotient,
    require_current,
    require_utc,
    rounded,
    tighten,
    to_utc,
    worth,
)

__all__ = ["Gate"]

_PAIRS = frozenset(
    {(OrderType.MARKET, TimeInForce.DAY), (OrderType.MARKET_ON_CLOSE, TimeInForce.CLS)}
)
_VALUE = places(Verdict, "value")
_MONEY = places(Verdict, "worst_loss")
_MINUTE = 60_000_000


@dataclass(frozen=True)
class _Read:
    """What a check read once no input halted it.

    `stress_move` is the one the hold left, and the caps' own where nothing held them.
    """

    limits: Constraints
    stress_move: Decimal
    session: Session
    cutoff: datetime
    marks: Mapping[InstrumentId, Decimal]
    loss: Decimal
    reading: LossReading


class _Figure(NamedTuple):
    """One limit with the figure it read and its bound, both `None` for a state that refuses."""

    limit: Limit
    value: Decimal | None = None
    bound: Decimal | None = None


class Gate:
    """`core.RiskGate`. It keeps the stress records, and nothing from one call to the next."""

    def __init__(self, records: StressRecords) -> None:
        self._records = records

    def check(
        self,
        intent: OrderIntent,
        book: Book,
        snapshot: Snapshot,
        constraints: Constraints,
        inputs: GateInputs,
        now: datetime,
    ) -> Verdict:
        """Approve one order, or refuse it naming the first limit that does."""
        if not isinstance(intent, OrderIntent):
            raise Halt("the order is missing")
        read = _read(book, snapshot, constraints, inputs, now, intent, self._records)
        adding = classify(book, intent) is Effect.ADDS
        reached = {i: exposure(book, i, intent) for i in read.marks}
        past = tuple(f for f in _three(intent, reached, read) if f.value > f.bound)
        first = next(_refusals(intent, book, inputs, read, now, adding, reached, past), None)
        return Verdict(
            approved=first is None,
            limit=first.limit if first else None,
            value=_recorded(first.limit, first.value) if first else None,
            bound=_recorded(first.limit, first.bound) if first else None,
            missing_term="position" if first and first.limit is Limit.POSITION else None,
            adding=adding,
            worst_loss=_worst_loss(intent, book, read),
            stress_move=read.stress_move,
            loss=read.reading,
            past=tuple(
                PastLimit(f.limit, _recorded(f.limit, f.value), _recorded(f.limit, f.bound))
                for f in past
            ),
        )

    def loss(
        self,
        book: Book,
        snapshot: Snapshot,
        constraints: Constraints,
        inputs: GateInputs,
        now: datetime,
    ) -> LossReading:
        """The day's loss, which the send stage reads on every decision with no order.

        It reads no stress move, so it holds the caps to no record. A record
        lost after the risk stage ran would otherwise stop the loss stop from firing.
        """
        return _read(book, snapshot, constraints, inputs, now, None, ()).reading


def _read(
    book: Book,
    snapshot: Snapshot,
    constraints: Constraints,
    inputs: GateInputs,
    now: datetime,
    intent: OrderIntent | None,
    records: StressRecords,
) -> _Read:
    """Every input checked, or a halt naming the first one that is absent, stale or invalid.

    An order's caps are held to the stress records, and a loss reading's are not.
    """
    for what, part, kind in (
        ("the book is", book, Book),
        ("the snapshot is", snapshot, Snapshot),
        ("the gate's inputs are", inputs, GateInputs),
    ):
        if not isinstance(part, kind):
            raise Halt(f"{what} missing")
    for what, part in (
        ("the book's positions", book.positions_by_strategy),
        ("the book's open orders", book.open_orders_by_strategy),
        ("the snapshot's status", snapshot.status),
    ):
        if not isinstance(part, Mapping):
            raise Halt(f"{what} are {part!r}, not a mapping")
    valuation = inputs.valuation
    if not isinstance(valuation, Valuation):
        raise Halt("the valuation is missing")
    caps = inputs.caps
    if intent is not None:
        caps = hold_to_record(constraints, caps, records)
    limits = tighten(constraints, caps, valuation)
    if not isinstance(inputs.stop, StopState):
        raise Halt(f"the loss stop's state is {inputs.stop!r}, which names none")
    table = load_sessions()
    decision = require_utc("the decision", snapshot.decision_at)
    session = table.opened_by(decision)
    if not decision <= require_utc("now", now) <= session.close:
        raise Halt(
            f"now is {now.isoformat()}, not from the decision at {decision.isoformat()} to its "
            f"session's close at {session.close.isoformat()}"
        )
    cutoff = to_utc(session.day, BeforeClose(limits.moc_cutoff))
    require_current(caps, valuation, decision)
    _check_book(book, valuation, table.before(session.day))
    if intent is not None:
        _check_intent(intent, book, limits)
    instruments = {*held(book), *([intent.instrument] if intent is not None else [])}
    marks = {i: mark(snapshot, i, valuation.session) for i in sorted(instruments)}
    loss = _day_loss(book, valuation, marks)
    return _Read(
        limits=limits,
        stress_move=caps.stress_move,
        session=session,
        cutoff=cutoff,
        marks=marks,
        loss=loss,
        reading=LossReading(
            loss=rounded("the day's loss", loss, _MONEY),
            bound=rounded("the loss stop", limits.loss_stop, _MONEY),
            past=loss > limits.loss_stop,
            session=valuation.session,
        ),
    )


def _names(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _check_book(book: Book, valuation: Valuation, prior: Session) -> None:
    """Halt on a book that cannot be read, or that moved since V by anything but its own fills.

    A deposit, or the restatement a split brings, would otherwise hide a loss.
    The price is no trading for the rest of that session.
    """
    finite("the book's cash", book.cash)
    corrections = book.corrections
    if not isinstance(corrections, tuple) or not all(
        isinstance(c, Correction) for c in corrections
    ):
        raise Halt(f"the book's corrections are {corrections!r}, not a list of corrections")
    # A journal's first entry is number 1, and one with no entry is replayed through 0.
    for what, number, least in (
        ("the valuation's replayed_through", valuation.replayed_through, 0),
        ("the book's replayed_through", book.replayed_through, 0),
        *(("a correction's seq", c.seq, 1) for c in corrections),
    ):
        if type(number) is not int or number < least:
            raise Halt(f"{what} is {number!r}, not an entry's number")
    if not isinstance(book.in_doubt, tuple) or not all(_names(i) for i in book.in_doubt):
        raise Halt(f"the book's in_doubt is {book.in_doubt!r}, not a list of instruments")
    for name, positions in book.positions_by_strategy.items():
        if not isinstance(positions, Mapping):
            raise Halt(f"{name}'s positions are {positions!r}, not a mapping")
        for instrument, qty in positions.items():
            if not _names(instrument):
                raise Halt(f"{name} holds {instrument!r}, which names no instrument")
            finite(f"{name}'s position in {instrument}", qty)
    seen: set[str] = set()
    working: set[str] = set()
    for name, orders in book.open_orders_by_strategy.items():
        if not isinstance(orders, tuple) or not all(isinstance(o, OrderIntent) for o in orders):
            raise Halt(f"{name}'s open orders are {orders!r}, not a list of orders")
        for order in orders:
            if not _names(order.client_order_id) or order.client_order_id in seen:
                raise Halt(
                    f"{name} has an open order whose id is {order.client_order_id!r}, which names "
                    "none or another"
                )
            if not _names(order.instrument):
                raise Halt(f"{name} has an open order in {order.instrument!r}, no instrument")
            if finite(f"{name}'s open order {order.client_order_id}", order.qty) == 0:
                raise Halt(f"{name}'s open order {order.client_order_id} has nothing left")
            seen.add(order.client_order_id)
            working.add(order.instrument)
    # Every range, and the count of open orders, include an order in doubt through the
    # open order it is.
    if uncounted := sorted(set(book.in_doubt) - working):
        raise Halt(f"{uncounted[0]} is in doubt and the book holds no open order in it")
    if valuation.replayed_through > book.replayed_through:
        raise Halt(
            f"the valuation was replayed through entry {valuation.replayed_through}, past the "
            f"book's {book.replayed_through}"
        )
    for c in corrections:
        if c.seq > book.replayed_through:
            raise Halt(f"a correction's seq is {c.seq}, past the book's {book.replayed_through}")
        require_utc(f"when entry {c.seq} corrected the book", c.at)
        if type(c.repositions) is not bool:
            raise Halt(f"the correction at entry {c.seq} repositions {c.repositions!r}, no bool")
    corrected = max((c.seq for c in corrections), default=None)
    if corrected is not None and corrected > valuation.replayed_through:
        raise Halt(
            f"entry {corrected} corrected the book after the valuation, which was replayed "
            f"through entry {valuation.replayed_through}"
        )
    at = max((c.at for c in corrections if c.repositions), default=None)
    if at is not None and at > prior.close:
        raise Halt(
            f"a position was corrected at {at.isoformat()}, after the valuation's session "
            f"closed at {prior.close.isoformat()}"
        )


def _check_intent(intent: OrderIntent, book: Book, limits: Constraints) -> None:
    if not _names(intent.client_order_id):
        raise Halt(f"an order's client order id is {intent.client_order_id!r}, which names none")
    if finite(f"the order {intent.client_order_id}", intent.qty) == 0:
        raise Halt(f"the order {intent.client_order_id} is for zero shares")
    if (intent.type, intent.time_in_force) not in _PAIRS:
        raise Halt(
            f"the order {intent.client_order_id} is a {intent.type!r} order for "
            f"{intent.time_in_force!r}, a pair the gate does not know"
        )
    if intent.strategy not in ("", limits.strategy):
        raise Halt(
            f"the order {intent.client_order_id} is {intent.strategy!r}'s, and the constraints "
            f"are {limits.strategy!r}'s"
        )
    if intent.instrument not in limits.max_position:
        raise Halt(f"the constraints and the hard caps do not both limit {intent.instrument}")
    for orders in book.open_orders_by_strategy.values():
        if intent.client_order_id in {order.client_order_id for order in orders}:
            raise Halt(f"the order {intent.client_order_id} is already open, and this resends it")


def _day_loss(book: Book, valuation: Valuation, marks: Mapping[InstrumentId, Decimal]) -> Decimal:
    """V less the book's cash and each position at its mark, so a fee counts as a loss."""
    now_worth = worth("the book", book.cash, {i: position(book, i) for i in marks}, marks)
    return exact_total("the day's loss", (valuation.value, now_worth.copy_negate()))


def _three(
    intent: OrderIntent, reached: Mapping[InstrumentId, Exposure], read: _Read
) -> tuple[_Figure, ...]:
    """The three limits an order that only reduces may go out past, each at its further end."""
    gross = exact_total(
        "the gross exposure",
        (
            exact_product(f"the gross exposure in {i}", reached[i].size, price)
            for i, price in read.marks.items()
        ),
    )
    limits = read.limits
    return (
        _Figure(
            Limit.MAX_POSITION,
            reached[intent.instrument].size,
            limits.max_position[intent.instrument],
        ),
        _Figure(Limit.MAX_GROSS, gross, limits.max_gross),
        _Figure(Limit.MAX_NET, _net(reached, read).size, limits.max_net),
    )


def _net(reached: Mapping[InstrumentId, Exposure], read: _Read) -> Exposure:
    """The account's dollars, signed across instruments, at every low end and at every high end."""
    return Exposure(
        *(
            exact_total(
                "the net exposure",
                (
                    exact_product(f"the net exposure in {i}", ends[side], read.marks[i])
                    for i, ends in reached.items()
                ),
            )
            for side in (0, 1)
        )
    )


def _refusals(
    intent: OrderIntent,
    book: Book,
    inputs: GateInputs,
    read: _Read,
    now: datetime,
    adding: bool,
    reached: Mapping[InstrumentId, Exposure],
    past: tuple[_Figure, ...],
) -> Iterator[_Figure]:
    """Every refusal of one order, in the order that fixes which limit is named."""
    if inputs.stop is not StopState.ABSENT:
        yield _Figure(Limit.STOP)
    # #117 reads a close entry. Until it does, any close entry refuses its order.
    if inputs.close_entry is not None:
        yield _Figure(Limit.CLOSE_ENTRY)
    if intent.instrument in book.in_doubt:
        yield _Figure(Limit.IN_DOUBT)
    if not intent.strategy:
        yield _Figure(Limit.POSITION)
    limits = read.limits
    qty, most = intent.qty.copy_abs(), limits.max_order_qty[intent.instrument]
    if qty > most:
        yield _Figure(Limit.MAX_ORDER_QTY, qty, most)
    # An order that adds is refused where one of the three is past with it
    # counted, an existing breach included. One that only reduces shrinks its
    # instrument's position in every case, so it can grow neither
    # `max_position` nor `max_gross`.
    if adding:
        yield from (f for f in past if f.limit is not Limit.MAX_NET)
    open_orders = Decimal(1 + sum(len(o) for o in book.open_orders_by_strategy.values()))
    if open_orders > limits.max_open_orders:
        yield _Figure(Limit.MAX_OPEN_ORDERS, open_orders, Decimal(limits.max_open_orders))
    if adding:
        yield from (f for f in past if f.limit is Limit.MAX_NET)
    else:
        yield from _widened_net(intent, book, read, _net(reached, read))
    if read.reading.past:
        yield _Figure(Limit.LOSS_STOP, read.loss, limits.loss_stop)
    if intent.type == OrderType.MARKET_ON_CLOSE and now > read.cutoff:
        left = Decimal((read.session.close - now) // timedelta(microseconds=1))
        minutes = quotient("the minutes to the close", left, Decimal(_MINUTE), _VALUE)
        yield _Figure(Limit.MOC_CUTOFF, minutes, Decimal(limits.moc_cutoff))


def _widened_net(
    intent: OrderIntent, book: Book, read: _Read, after: Exposure
) -> Iterator[_Figure]:
    """`max_net`, where an order that only reduces makes net larger and past its limit.

    Net is signed across instruments, so a sale that shrinks one leg can widen
    the account. The order moves the sum of the low ends for a sale and of the
    high ends for a buy, and only that sum is read, because a larger breach at
    the other end would hide the change.
    """
    before = _net({i: exposure(book, i) for i in read.marks}, read)
    end, start = (after.low, before.low) if intent.qty < 0 else (after.high, before.high)
    size = end.copy_abs()
    if size > read.limits.max_net and size > start.copy_abs():
        yield _Figure(Limit.MAX_NET, size, read.limits.max_net)


def _worst_loss(intent: OrderIntent, book: Book, read: _Read) -> Decimal | None:
    """The strategy's loss under the stress move, every leg moving against it, the order counted.

    `None` for an order that names no strategy, whose legs nothing lists.
    """
    if not intent.strategy:
        return None
    worst = exact_total(
        "the worst loss",
        (
            exact_product(
                f"the worst loss in {i}",
                exposure(book, i, intent, intent.strategy).size,
                price,
                read.stress_move,
            )
            for i, price in read.marks.items()
        ),
    )
    return rounded("the worst loss", worst, _MONEY, ROUND_CEILING)


def _recorded(limit: Limit, value: Decimal | None) -> Decimal | None:
    return None if value is None else rounded(f"{limit}", value, _VALUE)
