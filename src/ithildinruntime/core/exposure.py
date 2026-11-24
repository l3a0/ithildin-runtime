"""What a book is worth and how far it could move, counted one way for every reader.

The gate, portfolio construction and execution each need a price for an
instrument, the range a position could reach and whether an order only
reduces. No worker imports another, so the functions live here, once.
docs/design.md's `### Rule 9: how the gate counts open orders` says why an
open order is counted both filled and not.

Every sum and product is exact, as `core/exact.py` says. A book or an order
that holds a value that is not finite halts naming it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple

from .exact import exact_product, exact_total, finite, rounded
from .halt import Halt
from .records import (
    Bar,
    BarStatus,
    Book,
    InstrumentId,
    OrderIntent,
    Snapshot,
    Valuation,
)

__all__ = ["Effect", "Exposure", "classify", "exposure", "held", "mark", "position", "value_book"]

_DAILY = "1d"
_UNSTATED = "unstated"
_VALUE_PLACES = 4


class Effect(StrEnum):
    """What an order does to the positions it touches."""

    ADDS = "adds"
    REDUCES = "reduces"


class Exposure(NamedTuple):
    """The shares held if only the open sales fill, and if only the open buys do."""

    low: Decimal
    high: Decimal

    @property
    def size(self) -> Decimal:
        """The end further from zero, as a size."""
        return max(self.low.copy_abs(), self.high.copy_abs())


def mark(snapshot: Snapshot, instrument: InstrumentId, after: date) -> Decimal:
    """The one price an instrument is valued at: its newest daily bar's close.

    It halts unless that bar's session is later than `after`, and on a snapshot
    that holds a quote for the instrument, until #100 says which of a quote's
    prices is read.
    """
    if instrument in snapshot.quotes:
        raise Halt(f"{instrument} has a quote, and no rule says which of its prices is the mark")
    bars = _daily(snapshot, instrument)
    newest = max(bar.session for bar in bars)
    if newest <= after:
        raise Halt(
            f"{instrument} has no mark: its newest bar is for {newest.isoformat()}, "
            f"not later than {after.isoformat()}"
        )
    return _close(instrument, bars, newest)


def position(book: Book, instrument: InstrumentId, strategy: str | None = None) -> Decimal:
    """The shares held of one instrument, by the account or by one strategy."""
    return exact_total(
        f"the position in {instrument}",
        (
            finite(f"{name}'s position in {instrument}", positions[instrument])
            for name, positions in book.positions_by_strategy.items()
            if strategy in (None, name) and instrument in positions
        ),
    )


def exposure(
    book: Book,
    instrument: InstrumentId,
    intent: OrderIntent | None = None,
    strategy: str | None = None,
) -> Exposure:
    """The range of shares one instrument could reach, for the account or one strategy.

    The intent counts as one more order. An open order with the intent's
    client order id is the intent itself, and is never counted twice.
    """
    shares = position(book, instrument, strategy)
    orders = [
        finite(f"the open order {order.client_order_id}", order.qty)
        for order in _open(book, strategy)
        if order.instrument == instrument
        and (intent is None or order.client_order_id != intent.client_order_id)
    ]
    if (
        intent is not None
        and intent.instrument == instrument
        and strategy in (None, intent.strategy)
    ):
        orders.append(finite(f"the order {intent.client_order_id}", intent.qty))
    return Exposure(
        low=exact_total(f"{instrument}'s sales", (shares, *(q for q in orders if q < 0))),
        high=exact_total(f"{instrument}'s buys", (shares, *(q for q in orders if q > 0))),
    )


def classify(book: Book, intent: OrderIntent) -> Effect:
    """Whether an order only reduces, and otherwise it adds.

    It reduces only where it reduces both its strategy's position and the
    account's under every case of which open orders fill. An order with any
    part past zero in some case adds.
    """
    for strategy in (intent.strategy, None):
        reached = exposure(book, intent.instrument, intent, strategy)
        sale, buy = intent.qty < 0, intent.qty > 0
        if not (sale and reached.low >= 0 or buy and reached.high <= 0):
            return Effect.ADDS
    return Effect.REDUCES


def held(book: Book) -> tuple[InstrumentId, ...]:
    """Each instrument any strategy holds or has an open order in."""
    found = {
        instrument
        for positions in book.positions_by_strategy.values()
        for instrument, qty in positions.items()
        if qty != 0
    }
    found.update(order.instrument for order in _open(book, None))
    return tuple(sorted(found))


def value_book(book: Book, snapshot: Snapshot, session: date) -> Valuation:
    """V, the book at `session`'s official closes.

    Each position is marked at its bar of that session, and a newer one never
    serves, because a decision at the close would then value V at that close
    and read a loss of zero.
    """
    positions = {i: shares for i in held(book) if (shares := position(book, i)) != 0}
    marks = {i: _close(i, _daily(snapshot, i), session) for i in positions}
    value = exact_total(
        "the value of the book",
        (
            finite("the book's cash", book.cash),
            *(exact_product(f"the value of {i}", qty, marks[i]) for i, qty in positions.items()),
        ),
    )
    return Valuation(
        session=session,
        value=rounded("the value of the book", value, _VALUE_PLACES),
        cash=book.cash,
        positions=positions,
        marks=marks,
        replayed_through=book.replayed_through,
    )


def _open(book: Book, strategy: str | None) -> Iterator[OrderIntent]:
    for name, orders in book.open_orders_by_strategy.items():
        for order in orders:
            if order.strategy != name:
                raise Halt(
                    f"the book files {order.client_order_id}, an order of "
                    f"{order.strategy!r}, under {name!r}"
                )
            if strategy in (None, name):
                yield order


def _daily(snapshot: Snapshot, instrument: InstrumentId) -> tuple[Bar, ...]:
    status = snapshot.status.get(instrument)
    if status is None:
        raise Halt(f"{instrument} is not in the snapshot")
    if status != BarStatus.FRESH:
        raise Halt(f"{instrument} is {status} in the snapshot")
    bars = snapshot.bars.get(instrument, {}).get(_DAILY, ())
    if not bars:
        raise Halt(f"{instrument} has no daily bar")
    return bars


def _close(instrument: InstrumentId, bars: tuple[Bar, ...], session: date) -> Decimal:
    found = [bar for bar in bars if bar.session == session]
    if len(found) != 1:
        raise Halt(f"{instrument} has {len(found)} daily bars for {session.isoformat()}, not one")
    bar = found[0]
    kind = (bar.price_kind, bar.venues, bar.delay, bar.volume_counted)
    if _UNSTATED in kind:
        raise Halt(f"{instrument}'s bar for {session.isoformat()} is of unstated kind")
    price = finite(f"{instrument}'s close for {session.isoformat()}", bar.close)
    if price <= 0:
        raise Halt(f"{instrument}'s close for {session.isoformat()} is {price}, not above zero")
    return price
