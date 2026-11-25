"""What a book is worth and how far it could move, counted one way for every reader.

The alpha model, the gate, the risk model, portfolio construction and execution
each need a price for an instrument, and each but the alpha model what is held.
The gate and execution also need the range a position could reach and whether an
order only reduces. No worker imports another, so the functions live here, once.
docs/design.md's `### Rule 9: how the gate counts open orders` says why an open
order is counted both filled and not.

Every sum and product is exact, as `core/exact.py` says. A book or an order
that holds a value that is not finite halts naming it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import NamedTuple

from .codec import places
from .exact import exact_total, finite, rounded, worth
from .halt import Halt
from .names import DAILY, is_unstated
from .quotes import midpoint, require_quote
from .records import (
    Bar,
    Book,
    InstrumentId,
    OrderIntent,
    Snapshot,
    Valuation,
)
from .sessions import load_sessions, require_utc

__all__ = [
    "Effect",
    "Exposure",
    "Series",
    "classify",
    "exposure",
    "held",
    "mark",
    "position",
    "series_of",
    "session_close",
    "unstated_fields",
    "value_book",
]

_VALUE_PLACES = places(Valuation, "value")


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


def mark(snapshot: Snapshot, instrument: InstrumentId, after: date, now: datetime) -> Decimal:
    """An instrument's one price at a decision: its quote's midpoint, or its newest bar's close.

    It is a signal's newest price, and what a position is sized and valued at.

    It is the midpoint where the snapshot holds a quote for the instrument,
    which `core.require_quote` judges as of `now`, and the close where it holds
    none. A stage that reads no clock passes the decision time as `now`, and the
    gate passes its own. It halts unless the price is of a session later than
    `after` and no later than the decision's own, and unless the quote's older
    stamp is later than `after`'s close. It halts on a quote beside a bar of the
    decision's session, because nothing says which of the two is the mark.
    The bars are checked either way, and a bar's kind and its close only where
    the close is the mark.
    """
    bars = _daily(snapshot, instrument)
    newest = max(bar.session for bar in bars)
    if instrument in snapshot.quotes:
        quote = require_quote(snapshot, instrument, now)
        # The quote is of the decision's session, or the judge refused it.
        today = load_sessions().opened_by(snapshot.decision_at).day
        if newest == today:
            raise Halt(
                f"{instrument} has a quote and a bar for {today.isoformat()}, and nothing says "
                "which is the mark"
            )
        if today <= after:
            raise Halt(
                f"{instrument} has no mark: its quote is for {today.isoformat()}, not later than "
                f"{after.isoformat()}"
            )
        # A quote captured today can carry a stamp at or before V's close, under a maximum age
        # longer than the night, and a book marked there would read no loss. A stamp after that
        # close passes, and how old one may be is its strategy's written maximum.
        stamp, closed = min(quote.bid_at, quote.ask_at), load_sessions().session(after).close  # type: ignore[type-var]
        if stamp <= closed:
            raise Halt(
                f"{instrument} has no mark: its quote's older stamp is {stamp.isoformat()}, not "
                f"later than the close of {after.isoformat()} at {closed.isoformat()}"
            )
        return midpoint(quote)
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

    The intent counts as one more order. An open order that carries its client
    order id is the intent itself, already sent, so the book's unfilled
    remainder stands for it and the intent is not counted again. An open order
    that carries the id and differs in anything but size halts. An intent that
    was sent and is no longer open has no remainder to stand for it, so a caller
    never hands one in.
    """
    orders = [order for order in _open(book, strategy) if order.instrument == instrument]
    if intent is not None and _sent(book, intent) is None:
        if intent.instrument == instrument and strategy in (None, intent.strategy):
            orders.append(intent)
    sizes = [finite(f"the order {order.client_order_id}", order.qty) for order in orders]
    shares = position(book, instrument, strategy)
    return Exposure(
        low=exact_total(f"{instrument}'s sales", (shares, *(q for q in sizes if q < 0))),
        high=exact_total(f"{instrument}'s buys", (shares, *(q for q in sizes if q > 0))),
    )


def classify(book: Book, intent: OrderIntent) -> Effect:
    """Whether an order only reduces, and otherwise it adds.

    It reduces only where it reduces both its strategy's position and the
    account's under every case of which open orders fill. An order with any
    part past zero in some case adds.
    """
    finite(f"the order {intent.client_order_id}", intent.qty)
    sale, buy = intent.qty < 0, intent.qty > 0
    for strategy in (intent.strategy, None):
        reached = exposure(book, intent.instrument, intent, strategy)
        if not (sale and reached.low >= 0 or buy and reached.high <= 0):
            return Effect.ADDS
    return Effect.REDUCES


def held(book: Book, strategy: str | None = None) -> tuple[InstrumentId, ...]:
    """Each instrument the account, or one strategy, holds or has an open order in."""
    found = {
        instrument
        for name, positions in book.positions_by_strategy.items()
        if strategy in (None, name)
        for instrument, qty in positions.items()
        if qty != 0
    }
    found.update(order.instrument for order in _open(book, strategy))
    return tuple(sorted(found))


def session_close(snapshot: Snapshot, instrument: InstrumentId, session: date) -> Decimal | None:
    """The official close of one session, or `None` where the snapshot holds no bar of it.

    The bar is checked as a mark's is, so one that ends or was knowable after
    the decision halts, and so does a close of unstated kind.
    """
    bars = _daily(snapshot, instrument)
    if all(bar.session != session for bar in bars):
        return None
    return _close(instrument, bars, session)


def value_book(book: Book, snapshot: Snapshot, session: date) -> Valuation:
    """V, the book at `session`'s official closes.

    Each position is marked at its bar of that session, and a newer one never
    serves, because a decision at the close would then value V at that close
    and read a loss of zero.
    """
    positions = {i: shares for i in held(book) if (shares := position(book, i)) != 0}
    marks = {i: _close(i, _daily(snapshot, i), session) for i in positions}
    cash = finite("the book's cash", book.cash)
    return Valuation(
        session=session,
        value=rounded(
            "the value of the book",
            worth("the value of the book", cash, positions, marks),
            _VALUE_PLACES,
        ),
        cash=cash,
        positions=positions,
        marks=marks,
        replayed_through=book.replayed_through,
    )


class Series(NamedTuple):
    """One recording of an instrument's history: which price, from which source, as of when.

    A captured bar's is `raw` with no source and no date, so every captured bar
    of an instrument is of one series.
    """

    basis: str
    source: str | None
    source_as_of: date | None

    def __str__(self) -> str:
        source = "no source" if self.source is None else self.source
        dated = "no date" if self.source_as_of is None else self.source_as_of.isoformat()
        return f"{self.basis} from {source} as of {dated}"


def series_of(bar: Bar) -> Series:
    """The series a bar is of. Two series of one instrument can be on two scales."""
    return Series(bar.basis, bar.source, bar.source_as_of)


def unstated_fields(bar: Bar) -> tuple[str, ...]:
    """Which of a bar's four price-kind fields its source states nothing for.

    A field that holds no word states nothing either, so a bar built or stored
    without one never reads as stated.
    """
    fields = {
        "price kind": bar.price_kind,
        "venues": bar.venues,
        "delay": bar.delay,
        "volume counting": bar.volume_counted,
    }
    return tuple(name for name, said in fields.items() if is_unstated(said))


def _sent(book: Book, intent: OrderIntent) -> OrderIntent | None:
    """The open order that carries the intent's id, which halts unless it is the same order.

    It reads the whole book, since one filed under another strategy or
    instrument is the case to catch. What is left open is never more than the
    intent, and never nothing.
    """
    for order in _open(book, None):
        if order.client_order_id != intent.client_order_id:
            continue
        left = finite(f"the order {order.client_order_id}", order.qty)
        whole = finite(f"the order {intent.client_order_id}", intent.qty)
        same = ("strategy", "instrument", "type", "time_in_force")
        if any(getattr(order, f) != getattr(intent, f) for f in same) or not (
            0 < left <= whole or whole <= left < 0
        ):
            raise Halt(
                f"the book holds another order under the id {intent.client_order_id} than the "
                "one handed in"
            )
        return order
    return None


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
    """The instrument's daily bars, each ended and knowable by the decision.

    `require_fresh` is the one test of a snapshot every module runs. Data reads
    no bar that ends or was knowable later than the decision, and a book valued
    at one would hide a loss, so this refuses what data would never return.
    """
    snapshot.require_fresh({instrument: [DAILY]})
    bars = snapshot.bars[instrument][DAILY]
    at = require_utc("the snapshot's decision_at", snapshot.decision_at)
    for bar in bars:
        if type(bar.session) is not date:
            raise Halt(f"{instrument} has a bar for {bar.session!r}, not a date")
        for what, when in (("ends", bar.end), ("was knowable", bar.knowable_at)):
            if require_utc(f"when {instrument}'s bar {what}", when) > at:
                raise Halt(
                    f"{instrument}'s bar for {bar.session.isoformat()} {what} at "
                    f"{when.isoformat()}, after the decision at {at.isoformat()}"
                )
    table = load_sessions()
    today = table.opened_by(at).day
    newest = max(bar.session for bar in bars)
    if newest > today:
        raise Halt(
            f"{instrument} has a bar for {newest.isoformat()}, after the decision's session "
            f"{today.isoformat()}"
        )
    table.session(newest)
    return bars


def _close(instrument: InstrumentId, bars: tuple[Bar, ...], session: date) -> Decimal:
    found = [bar for bar in bars if bar.session == session]
    if len(found) != 1:
        raise Halt(f"{instrument} has {len(found)} daily bars for {session.isoformat()}, not one")
    (bar,) = found
    if silent := unstated_fields(bar):
        raise Halt(f"{instrument}'s bar for {session.isoformat()} has unstated {', '.join(silent)}")
    price = finite(f"{instrument}'s close for {session.isoformat()}", bar.close)
    if price <= 0:
        raise Halt(f"{instrument}'s close for {session.isoformat()} is {price}, not above zero")
    return price
