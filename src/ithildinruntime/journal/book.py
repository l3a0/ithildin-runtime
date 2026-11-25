"""The fold from a journal's entries to the book, the orders and the fills.

A `View` is one reading of the journal, through one entry, and never changes.
`after` works out the view one entry on, and raises `Halt` where a check on
that entry fails, having changed nothing. docs/architecture/05-building-blocks.md's
`#### The book comes from the journal` says what each part is.

A view copies what does not grow: the positions, the cash and every order not
in an end state. Orders that ended, decisions, broker reads, fills and the
broker's ids are kept once, in maps every view of one `Journal` reads, and a
view shows none whose entry is past its own.
"""

from __future__ import annotations

import threading
from bisect import bisect_right
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from itertools import islice
from types import MappingProxyType
from typing import NamedTuple

from ithildinruntime.core import (
    Book,
    CodecError,
    Fill,
    Halt,
    InstrumentId,
    JournalAck,
    JournalEntry,
    OrderEntry,
    OrderIntent,
    OrderRecord,
    OrderState,
    exact_product,
    exact_total,
    from_received,
    read_order_id,
)

from .entries import (
    AbandonedEntry,
    AckEntry,
    DecisionEntry,
    DisputedFillEntry,
    ExpiredEntry,
    FillEntry,
    InDoubtEntry,
    IntentEntry,
    OpeningEntry,
    ReadEntry,
    RejectedEntry,
    SendingEntry,
    VerdictEntry,
    kind_of,
    record_of,
)
from .orders import DOUBTFUL, ENDED, OPEN, Move, moved

__all__ = ["Dispute", "View", "after", "empty"]

_ZERO = Decimal(0)
# What a fill read twice must repeat. Every other field is one a second
# endpoint may leave out or write another way.
_CONTENT = ("fill_id", "broker_order_id", "instrument", "qty", "price", "at")


class Dispute(NamedTuple):
    """A repeat of a fill whose content differs, under its entry's number."""

    seq: int
    fill: Fill


class _Kept[K, V]:
    """Values that are only ever added, each under the number of the entry that gave it."""

    def __init__(self) -> None:
        self.found: dict[K, tuple[int, V]] = {}
        self.keys: list[K] = []
        self.seqs: list[int] = []

    def add(self, seq: int, key: K, value: V) -> None:
        # An entry folded twice, such as by an append and by a read forward on
        # another thread, gives the same value.
        if key not in self.found:
            self.found[key] = (seq, value)
            self.keys.append(key)
            self.seqs.append(seq)


class _Upto[K, V](Mapping[K, V]):
    """What `_Kept` holds from the entries up to one, in their order."""

    def __init__(self, kept: _Kept[K, V], through: int) -> None:
        self._kept = kept
        self._through = through

    def __getitem__(self, key: K) -> V:
        seq, value = self._kept.found[key]
        if seq > self._through:
            raise KeyError(key)
        return value

    def __iter__(self) -> Iterator[K]:
        return islice(self._kept.keys, len(self))

    def __len__(self) -> int:
        return bisect_right(self._kept.seqs, self._through)


class _Orders(Mapping[str, OrderRecord]):
    """Every order by client order id: the ended ones as they ended, then the rest as planned."""

    def __init__(self, live: Mapping[str, OrderRecord], ended: Mapping[str, OrderRecord]) -> None:
        self._live = live
        self._ended = ended

    def __getitem__(self, key: str) -> OrderRecord:
        found = self._live.get(key)
        return self._ended[key] if found is None else found

    def __iter__(self) -> Iterator[str]:
        yield from self._ended
        yield from self._live

    def __len__(self) -> int:
        return len(self._ended) + len(self._live)


class _Shared:
    """What grows with the journal, kept once for every view of one fold."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.ended: _Kept[str, OrderRecord] = _Kept()
        self.decisions: _Kept[int, DecisionEntry] = _Kept()
        self.decided: _Kept[datetime, int] = _Kept()
        self.reads: _Kept[int, ReadEntry] = _Kept()
        self.fills: _Kept[str, Fill] = _Kept()
        self.brokers: _Kept[str, str] = _Kept()


@dataclass(frozen=True, eq=False)
class View:
    """One consistent reading of a journal, through one entry's number and hash.

    - `orders` holds each order by its client order id, and `live` those that
      have not ended, in the order their intents were journaled.
    - `decisions` holds each `decision` entry by its number, and `decided` the
      number by the decision time. `reads` holds each `read` entry by its number.
    - `fills` holds each fill as first applied, by its id, and `disputes` each
      repeat whose content differs.
    - `opened_at` is the `opening` entry's `built_at`, and `sent_at` the newest
      `sending` entry's, or `None` where there is none.
    """

    account: str
    through: JournalAck
    opened_at: datetime | None
    sent_at: datetime | None
    live: Mapping[str, OrderRecord]
    disputes: tuple[Dispute, ...]
    # The `Journal` that gave this view, so no other one builds an entry on it.
    _owner: object = field(repr=False)
    _cash: Decimal = field(repr=False)
    _positions: Mapping[str, Mapping[InstrumentId, Decimal]] = field(repr=False)
    _shared: _Shared = field(repr=False)

    @property
    def orders(self) -> Mapping[str, OrderRecord]:
        return _Orders(self.live, _Upto(self._shared.ended, self.through.seq))

    @property
    def decisions(self) -> Mapping[int, DecisionEntry]:
        return _Upto(self._shared.decisions, self.through.seq)

    @property
    def decided(self) -> Mapping[datetime, int]:
        return _Upto(self._shared.decided, self.through.seq)

    @property
    def reads(self) -> Mapping[int, ReadEntry]:
        return _Upto(self._shared.reads, self.through.seq)

    @property
    def fills(self) -> Mapping[str, Fill]:
        return _Upto(self._shared.fills, self.through.seq)

    @property
    def book(self) -> Book:
        """The book, or a halt on a journal with no entry and on a fill in dispute.

        An empty journal reads the same as a wrong account or a lost store, so
        it never reads as a flat book.
        """
        if self.through.seq == 0:
            raise Halt(f"journal {self.account}: it holds no entry, so there is no book")
        if self.disputes:
            raise Halt(f"journal {self.account}: {'; '.join(map(self._disputed, self.disputes))}")
        filed: dict[str, tuple[OrderIntent, ...]] = {}
        for order in self.live.values():
            if order.state in OPEN:
                strategy = order.intent.strategy
                rest = replace(order.intent, qty=_left(order.intent, order.filled))
                filed[strategy] = (*filed.get(strategy, ()), rest)
        doubtful = {o.intent.instrument for o in self.live.values() if o.state in DOUBTFUL}
        return Book(
            positions_by_strategy=self._positions,
            open_orders_by_strategy=filed,
            cash=self._cash,
            replayed_through=self.through.seq,
            in_doubt=tuple(sorted(doubtful)),
            corrections=(),
        )

    def _disputed(self, dispute: Dispute) -> str:
        seq, _ = self._shared.fills.found[dispute.fill.fill_id]
        first = self.fills[dispute.fill.fill_id]
        return (
            f"the fill {first.fill_id} is in dispute: entry {seq} holds {_said(first)}, and "
            f"entry {dispute.seq} holds {_said(dispute.fill)}"
        )


def empty(account: str, owner: object, entry_hash: str) -> View:
    """The view of a journal with no entry, whose book halts."""
    return View(
        account=account,
        through=JournalAck(0, entry_hash),
        opened_at=None,
        sent_at=None,
        live=MappingProxyType({}),
        disputes=(),
        _owner=owner,
        _cash=_ZERO,
        _positions=MappingProxyType({}),
        _shared=_Shared(),
    )


class Step:
    """One entry worked out on a view, with nothing kept until `keep`."""

    def __init__(self, entry: JournalEntry, view: View) -> None:
        self.seq = entry.seq
        self.at = entry.built_at
        self.view = view
        self._changes: dict[str, object] = {}
        self._news: list[tuple[_Kept, object, object]] = []

    def change(self, **changes: object) -> None:
        self._changes |= changes

    def add[K, V](self, kept: _Kept[K, V], key: K, value: V) -> None:
        # Every value here is added once, so a second one this view already
        # shows would be dropped without a word.
        held = kept.found.get(key)
        if held is not None and held[0] <= self.view.through.seq:
            raise Halt(f"entry {self.seq} gives {key!r} a second value, after entry {held[0]}'s")
        self._news.append((kept, key, value))

    def order(self, kind: str, order: OrderRecord, state: OrderState, **changes: object) -> None:
        """The order as this entry leaves it, kept with the live ones until it ends."""
        made = replace(
            order,
            state=state,
            entries=(*order.entries, OrderEntry(self.seq, kind)),
            **changes,  # type: ignore[arg-type]
        )
        name = made.intent.client_order_id
        live = dict(self.view.live)
        if state in ENDED:
            live.pop(name, None)
            self.add(self.view._shared.ended, name, made)
        else:
            live[name] = made
        self.change(live=MappingProxyType(live))

    def keep(self, entry_hash: str) -> View:
        """The view through this entry, once the entry is known to be stored."""
        shared = self.view._shared
        with shared.lock:
            for kept, key, value in self._news:
                kept.add(self.seq, key, value)
        return replace(
            self.view,
            through=JournalAck(self.seq, entry_hash),
            **self._changes,  # type: ignore[arg-type]
        )


def after(view: View, entry: JournalEntry) -> Step:
    """One entry on from `view`, or a halt naming the account and entry where a check fails."""
    try:
        return _worked_out(view, entry)
    except Halt as halt:
        raise Halt(f"journal {view.account}: {halt.reason}") from None


def _worked_out(view: View, entry: JournalEntry) -> Step:
    record = record_of(entry)
    seq, kind = entry.seq, kind_of(record)
    if seq != view.through.seq + 1:
        raise Halt(f"entry {seq} is folded after entry {view.through.seq}")
    if isinstance(record, OpeningEntry) != (seq == 1):
        raise Halt(
            f"entry {seq} is of the kind {kind}, and an account's entry 1 is its opening and no "
            "other is"
        )
    step = Step(entry, view)
    match record:
        case OpeningEntry():
            _opening(step, record)
        case ReadEntry():
            step.add(view._shared.reads, seq, record)
        case DecisionEntry():
            _decision(step, record)
        case IntentEntry():
            _intent(step, record)
        case VerdictEntry():
            order, state = _next(
                step,
                record.client_order_id,
                Move.APPROVED if record.verdict.approved else Move.REFUSED,
            )
            if record.book != seq - 1:
                raise Halt(
                    f"entry {seq} is a verdict on the book through entry {record.book}, not "
                    "through the entry before it"
                )
            step.order(kind, order, state)
        case AbandonedEntry():
            step.order(kind, *_next(step, record.client_order_id, Move.ABANDONED))
        case SendingEntry():
            order, state = _next(step, record.client_order_id, Move.SENDING)
            step.order(kind, order, state, sent_at=step.at)
            step.change(sent_at=step.at if view.sent_at is None else max(view.sent_at, step.at))
        case AckEntry():
            _ack(step, record)
        case RejectedEntry():
            step.order(kind, *_next(step, record.client_order_id, Move.REJECTED))
        case InDoubtEntry():
            step.order(kind, *_next(step, record.client_order_id, Move.IN_DOUBT))
        case FillEntry():
            _fill(step, record)
        case DisputedFillEntry():
            _disputed_fill(step, record)
        case ExpiredEntry():
            order, state = _next(step, record.client_order_id, Move.EXPIRED)
            if record.filled != order.filled:
                raise Halt(
                    f"entry {seq} says {record.client_order_id} expired with {record.filled} "
                    f"filled, and its fills come to {order.filled}"
                )
            step.order(kind, order, state)
        case _:
            raise Halt(f"entry {seq} is of the kind {kind}, which the book does not read")
    return step


def _next(step: Step, name: str, move: Move) -> tuple[OrderRecord, OrderState]:
    """The order an entry names, and the state the entry leaves it in."""
    order = step.view.orders.get(name)
    state = moved(step.seq, name, move, None if order is None else order.state)
    # Only an intent moves an order that has no entry, so the move above halted on one.
    assert order is not None
    return order, state


def _opening(step: Step, record: OpeningEntry) -> None:
    if record.account != step.view.account:
        raise Halt(
            f"entry {step.seq} opens the account {record.account!r}, not {step.view.account!r}"
        )
    if record.cash < 0:
        raise Halt(f"entry {step.seq} opens the account with {record.cash}, below zero")
    step.change(_cash=record.cash, opened_at=step.at)


def _decision(step: Step, record: DecisionEntry) -> None:
    seq, at = step.seq, record.decision_at
    if (other := step.view.decided.get(at)) is not None:
        raise Halt(f"entry {seq} is a second decision at {at.isoformat()}, after entry {other}")
    _require_read(step, record.read)
    if not 1 <= record.synced < seq:
        raise Halt(f"entry {seq} was synced through entry {record.synced}, no entry before it")
    for place, order in enumerate(record.orders, start=1):
        if order.strategy in record.off:
            raise Halt(
                f"entry {seq} plans {order.client_order_id}, an order of {order.strategy!r}, "
                "which was off"
            )
        try:
            said = read_order_id(order.client_order_id)
        except Halt as halt:
            raise Halt(f"entry {seq} plans an order whose id is no stamp: {halt.reason}") from None
        if said != (order.strategy, at, place):
            raise Halt(
                f"entry {seq} plans {order.client_order_id} as order {place} of "
                f"{order.strategy!r} at {at.isoformat()}, which is not what the id says"
            )
    step.add(step.view._shared.decisions, seq, record)
    step.add(step.view._shared.decided, at, seq)


def _intent(step: Step, record: IntentEntry) -> None:
    seq, order = step.seq, record.order
    name = order.client_order_id
    known = step.view.orders.get(name)
    state = moved(seq, name, Move.INTENT, None if known is None else known.state)
    planned = step.view.decisions.get(record.decision)
    if planned is None:
        raise Halt(f"entry {seq} names entry {record.decision}, which is no decision before it")
    if order not in planned.orders:
        raise Halt(f"entry {seq}'s order {name} is not one entry {record.decision} plans")
    step.order("intent", OrderRecord(order, record.decision, state, None, _ZERO, (), None), state)


def _ack(step: Step, record: AckEntry) -> None:
    order, state = _next(step, record.client_order_id, Move.ACK)
    broker = record.broker_order_id
    brokers = _Upto(step.view._shared.brokers, step.view.through.seq)
    if (other := brokers.get(broker)) is not None:
        raise Halt(
            f"entry {step.seq} gives {record.client_order_id} the broker's id {broker}, which "
            f"is {other}'s"
        )
    step.add(step.view._shared.brokers, broker, record.client_order_id)
    step.order("ack", order, state, broker_order_id=broker)


def _fill(step: Step, record: FillEntry) -> None:
    seq, view, fill = step.seq, step.view, record.fill
    _require_read(step, record.read)
    first = view.fills.get(fill.fill_id)
    if first is not None:
        # A repeat of the first, or of a dispute already held, changes nothing.
        if _same(fill, first) or any(_same(fill, dispute.fill) for dispute in view.disputes):
            return
        raise Halt(
            f"entry {seq}'s fill {fill.fill_id} holds {_said(fill)}, and the one applied holds "
            f"{_said(first)}"
        )
    name = _Upto(view._shared.brokers, view.through.seq).get(fill.broker_order_id)
    if name is None:
        raise Halt(
            f"entry {seq}'s fill {fill.fill_id} is of the broker's order {fill.broker_order_id}, "
            "which no order was acknowledged under"
        )
    order = view.orders[name]
    intent = order.intent
    filled = exact_total(f"the shares filled of {name}", (order.filled, fill.qty))
    move = Move.LAST_FILL if filled == intent.qty else Move.PART_FILL
    state = moved(seq, name, move, order.state)
    if fill.client_order_id not in (None, name):
        raise Halt(
            f"entry {seq}'s fill {fill.fill_id} names {fill.client_order_id!r}, and its broker's "
            f"order is {name}'s"
        )
    if fill.instrument != intent.instrument:
        raise Halt(
            f"entry {seq}'s fill {fill.fill_id} is in {fill.instrument}, and {name} is in "
            f"{intent.instrument}"
        )
    if (fill.qty > 0) != (intent.qty > 0):
        raise Halt(f"entry {seq}'s fill {fill.fill_id} of {fill.qty} is not on {name}'s side")
    if filled.copy_abs() > intent.qty.copy_abs():
        raise Halt(
            f"entry {seq}'s fill {fill.fill_id} takes {name} to {filled} filled of {intent.qty}"
        )
    paid = exact_product(f"the cost of the fill {fill.fill_id}", fill.qty, fill.price)
    cash = exact_total("the cash", (view._cash, paid.copy_negate()))
    held = dict(view._positions.get(intent.strategy, {}))
    shares = exact_total(
        f"the position in {intent.instrument}", (held.get(intent.instrument, _ZERO), fill.qty)
    )
    for what, value in (
        ("the cash", cash),
        (f"the position in {intent.instrument}", shares),
        (f"what is left of {name}", _left(intent, filled)),
    ):
        try:
            from_received(value)
        except CodecError as error:
            raise Halt(f"entry {seq} leaves {what} where no record holds it: {error}") from None
    if shares == 0:
        held.pop(intent.instrument, None)
    else:
        held[intent.instrument] = shares
    positions = {**view._positions, intent.strategy: MappingProxyType(held)}
    if not held:
        del positions[intent.strategy]
    step.change(_cash=cash, _positions=MappingProxyType(positions))
    step.add(view._shared.fills, fill.fill_id, fill)
    step.order("fill", order, state, filled=filled)


def _disputed_fill(step: Step, record: DisputedFillEntry) -> None:
    seq, view, fill = step.seq, step.view, record.fill
    _require_read(step, record.read)
    first = view.fills.get(fill.fill_id)
    if first is None:
        raise Halt(f"entry {seq} disputes the fill {fill.fill_id}, which no entry applied")
    if _same(fill, first):
        raise Halt(f"entry {seq} disputes the fill {fill.fill_id} with the content it has")
    if held := [dispute.seq for dispute in view.disputes if _same(fill, dispute.fill)]:
        raise Halt(
            f"entry {seq} disputes the fill {fill.fill_id} with the content entry {held[0]} holds"
        )
    step.change(disputes=(*view.disputes, Dispute(seq, fill)))


def _require_read(step: Step, read: int) -> None:
    if read not in step.view.reads:
        raise Halt(f"entry {step.seq} names entry {read}, which is no read before it")


def _left(intent: OrderIntent, filled: Decimal) -> Decimal:
    name = intent.client_order_id
    return exact_total(f"what is left of {name}", (intent.qty, filled.copy_negate()))


def _same(one: Fill, other: Fill) -> bool:
    return all(getattr(one, name) == getattr(other, name) for name in _CONTENT)


def _said(fill: Fill) -> str:
    return (
        f"{fill.qty} {fill.instrument} at {fill.price} on the order {fill.broker_order_id}, "
        f"filled {fill.at.isoformat()}"
    )
