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
from enum import StrEnum
from functools import cached_property
from itertools import islice
from types import MappingProxyType
from typing import NamedTuple

from ithildinruntime.core import (
    Book,
    BrokerOrder,
    BrokerRecord,
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
    broker_id_shown,
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
    LostEntry,
    OpeningEntry,
    OutsideRecordEntry,
    ReadEntry,
    Record,
    RejectedEntry,
    SendingEntry,
    StopClearedEntry,
    StopFiredEntry,
    StopStoredEntry,
    UnmatchedFillEntry,
    UnmatchedOrderEntry,
    UnmatchedRecordEntry,
    VerdictEntry,
    kind_of,
    named,
    record_of,
)
from .orders import DOUBTFUL, ENDED, MOVES, OPEN, Move, lost_from, may_be_lost, moved
from .stop import Latched, is_latch_entry, latched, refusal

__all__ = ["Dispute", "Repeat", "Unmatched", "View", "after", "empty"]

_ZERO = Decimal(0)
# What a fill read twice must repeat. Every other field is one a second
# endpoint may leave out or write another way.
_CONTENT = ("fill_id", "broker_order_id", "instrument", "qty", "price", "at")
# What a broker's record read twice must repeat.
_RECORD = ("record_id", "kind", "amount", "instrument", "at")
# What an order the platform did not send, listed twice, must repeat. Its fills and state may
# move between two reads.
_ORDER = (
    "broker_order_id",
    "client_order_id",
    "instrument",
    "qty",
    "type",
    "time_in_force",
    "submitted_at",
)


class Repeat(StrEnum):
    """What a fill the broker reports is to a view, going by its content."""

    NEW = "new"
    HELD = "held"
    DIFFERS = "differs"


class Dispute(NamedTuple):
    """A repeat of a fill whose content differs, under its entry's number."""

    seq: int
    fill: Fill


class Unmatched(NamedTuple):
    """What the journal keeps and gives to no book, under its entry's number.

    `kind` is `record`, `fill` or `order`, `name` the broker's id for it, and
    `reason` why no rule let it reach a book.
    """

    seq: int
    kind: str
    name: str
    held: BrokerRecord | Fill | BrokerOrder
    reason: str


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
        self.records: _Kept[str, BrokerRecord] = _Kept()
        self.brokers: _Kept[str, str] = _Kept()
        # Each entry the loss stop's state is worked out from, by its number.
        self.stops: _Kept[int, Record] = _Kept()


@dataclass(frozen=True, eq=False)
class View:
    """One consistent reading of a journal, through one entry's number and hash.

    - `orders` holds each order by its client order id, and `live` those that
      have not ended, in the order their intents were journaled.
    - `decisions` holds each `decision` entry by its number, and `decided` the
      number by the decision time. `reads` holds each `read` entry by its number.
    - `fills` holds each fill as first applied, by its id, and `disputes` each
      repeat whose content differs. `records` holds each broker's record that
      moved the cash, by its id.
    - `unmatched` holds each record, fill and order the journal keeps and gives
      to no book, which holds the account.
    - `opened_at` is the `opening` entry's `built_at`, and `sent_at` the newest
      `sending` entry's, or `None` where there is none.
    """

    account: str
    through: JournalAck
    opened_at: datetime | None
    sent_at: datetime | None
    live: Mapping[str, OrderRecord]
    disputes: tuple[Dispute, ...]
    unmatched: tuple[Unmatched, ...]
    # The `Journal` that gave this view, so no other one builds an entry on it.
    _owner: object = field(repr=False)
    _cash: Decimal = field(repr=False)
    _positions: Mapping[str, Mapping[InstrumentId, Decimal]] = field(repr=False)
    # Who wrote the `sending` entry of each order that is still `Sending`.
    _senders: Mapping[str, object] = field(repr=False)
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
    def records(self) -> Mapping[str, BrokerRecord]:
        return _Upto(self._shared.records, self.through.seq)

    @cached_property
    def stop(self) -> Latched:
        """The loss stop's state as the journal holds it, which `journal.stop` works out."""
        kept = _Upto(self._shared.stops, self.through.seq)
        return latched((seq, kept[seq]) for seq in kept)

    def record_repeats(self, record: BrokerRecord) -> Repeat:
        """Whether a broker's record is new, held as applied or as unmatched, or differs from each.

        Its content decides, as `_RECORD` lists it, and not when it was received.
        """
        first = self.records.get(record.record_id)
        held = [*([first] if first is not None else []), *self._kept("record", record.record_id)]
        if not held:
            return Repeat.NEW
        if any(_alike(record, one, _RECORD) for one in held):
            return Repeat.HELD
        return Repeat.DIFFERS

    def unmatched_repeats(self, kind: str, name: str, held: Fill | BrokerOrder) -> Repeat:
        """Whether a fill or an order is kept as unmatched already, with this content or another."""
        kept = self._kept(kind, name)
        if not kept:
            return Repeat.NEW
        fields = _CONTENT if kind == "fill" else _ORDER
        return Repeat.HELD if any(_alike(held, one, fields) for one in kept) else Repeat.DIFFERS

    def unowned(self, fill: Fill) -> str | None:
        """Why a new fill cannot be any order's the platform sent, or `None` where it is one's.

        A fill past its kind's own bounds, such as one for zero shares, at a
        price not above zero or carrying a correction's id, is not refused here
        for that reason and may get `None`, because `entries.past_bounds`
        refuses it, so a caller asks both.

        The words name the fill and the order and none of the broker's text, so
        a halt and an unmatched entry can both hold them.
        """
        return _unowned(self, fill)

    def _kept(self, kind: str, name: str) -> list[BrokerRecord | Fill | BrokerOrder]:
        return [one.held for one in self.unmatched if (one.kind, one.name) == (kind, name)]

    def repeats(self, fill: Fill) -> Repeat:
        """Whether a fill is new, or one held, as applied or as a dispute, or differs from each.

        Its content decides, as `_CONTENT` lists it. When it was received and
        what a second endpoint leaves out do not.
        """
        first = self.fills.get(fill.fill_id)
        if first is None:
            return Repeat.NEW
        if _same(fill, first) or any(_same(fill, dispute.fill) for dispute in self.disputes):
            return Repeat.HELD
        return Repeat.DIFFERS

    def acknowledged(self, broker_order_id: str) -> OrderRecord | None:
        """The order acknowledged under a broker's id, or `None` where there is none."""
        name = _Upto(self._shared.brokers, self.through.seq).get(broker_order_id)
        return None if name is None else self.orders[name]

    @property
    def book(self) -> Book:
        """The book, or a halt on an empty journal, a fill in dispute or anything unmatched.

        An empty journal reads the same as a wrong account or a lost store, so
        it never reads as a flat book. An unmatched entry holds the account
        until a person releases it, and no entry releases one yet.
        """
        if self.through.seq == 0:
            raise Halt(f"journal {self.account}: it holds no entry, so there is no book")
        if self.disputes or self.unmatched:
            said = [*map(self._disputed, self.disputes), *map(_held, self.unmatched)]
            raise Halt(f"journal {self.account}: {'; '.join(said)}")
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
        unmatched=(),
        _owner=owner,
        _cash=_ZERO,
        _positions=MappingProxyType({}),
        _senders=MappingProxyType({}),
        _shared=_Shared(),
    )


class Step:
    """One entry worked out on a view, with nothing kept until `keep`."""

    def __init__(self, entry: JournalEntry, view: View) -> None:
        self.seq = entry.seq
        self.at = entry.built_at
        self.writer = entry.body["writer"]
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
        senders = {n: w for n, w in self.view._senders.items() if n != name}
        if state is OrderState.SENDING:
            senders[name] = self.writer
        self.change(_senders=MappingProxyType(senders))
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
            if record.close_entry is not None and not 1 <= record.close_entry < seq:
                raise Halt(f"entry {seq} names entry {record.close_entry}, no entry before it")
            step.order(kind, order, state)
            if is_latch_entry(record):
                step.add(view._shared.stops, seq, record)
        case StopFiredEntry() | StopStoredEntry() | StopClearedEntry():
            refused = refusal(view.stop, seq, record)
            if refused is not None:
                raise Halt(refused)
            step.add(view._shared.stops, seq, record)
        case AbandonedEntry():
            order, state = _next(step, record.client_order_id, Move.ABANDONED)
            # No other writer can know that an order whose `sending` is stored never left.
            if order.state is OrderState.SENDING and (
                view._senders[record.client_order_id] != step.writer
            ):
                raise Halt(
                    f"entry {seq} abandons {record.client_order_id}, whose sending another "
                    "writer stored"
                )
            step.order(kind, order, state)
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
        case LostEntry():
            _lost(step, record)
        case FillEntry():
            _fill(step, record)
        case DisputedFillEntry():
            _disputed_fill(step, record)
        case OutsideRecordEntry():
            _outside(step, record)
        case UnmatchedRecordEntry():
            _unmatched(
                step, record.read, "record", record.record.record_id, record.record, record.reason
            )
        case UnmatchedFillEntry():
            _unmatched(step, record.read, "fill", record.fill.fill_id, record.fill, record.reason)
        case UnmatchedOrderEntry():
            order = record.order
            _unmatched(step, record.read, "order", order.broker_order_id, order, record.reason)
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
    if kept := [
        one.seq for one in step.view.unmatched if (one.kind, one.name) == ("order", broker)
    ]:
        raise Halt(
            f"entry {step.seq} gives {record.client_order_id} the broker's id {broker}, an order "
            f"entry {kept[0]} keeps as one the platform did not send"
        )
    brokers = _Upto(step.view._shared.brokers, step.view.through.seq)
    if (other := brokers.get(broker)) is not None:
        raise Halt(
            f"entry {step.seq} gives {record.client_order_id} the broker's id {broker}, which "
            f"is {other}'s"
        )
    step.add(step.view._shared.brokers, broker, record.client_order_id)
    step.order("ack", order, state, broker_order_id=broker)


def _lost(step: Step, record: LostEntry) -> None:
    """A closing-auction order in doubt that a whole list, asked for after its close, omitted."""
    order, state = _next(step, record.client_order_id, Move.LOST)
    _require_read(step, record.read)
    if not may_be_lost(order.intent):
        raise Halt(
            f"entry {step.seq} gives up on {record.client_order_id}, which is no closing-auction "
            "order, and a broker may hold one of another kind for the next session's open"
        )
    asked, after = step.view.reads[record.read].at, lost_from(record.client_order_id)
    if asked < after:
        raise Halt(
            f"entry {step.seq} gives up on {record.client_order_id} on the list entry "
            f"{record.read} names, asked for at {asked.isoformat()}, before {after.isoformat()}, "
            "when the order could still arrive"
        )
    step.order("lost", order, state)


def _fill(step: Step, record: FillEntry) -> None:
    seq, view, fill = step.seq, step.view, record.fill
    _require_read(step, record.read)
    repeat = view.repeats(fill)
    if repeat is Repeat.HELD:
        # A repeat changes nothing. One of a dispute that is past a fill's bounds
        # halted before it came here.
        return
    if repeat is Repeat.DIFFERS:
        raise Halt(
            f"entry {seq}'s fill {fill.fill_id} holds {_said(fill)}, and the one applied holds "
            f"{_said(view.fills[fill.fill_id])}"
        )
    if view._kept("fill", fill.fill_id):
        raise Halt(f"entry {seq}'s fill {fill.fill_id} is one the journal keeps as unmatched")
    if (why := _unowned(view, fill)) is not None:
        raise Halt(f"entry {seq}'s {why}")
    order = view.acknowledged(fill.broker_order_id)
    assert order is not None
    intent, name = order.intent, order.intent.client_order_id
    filled = exact_total(f"the shares filled of {name}", (order.filled, fill.qty))
    move = Move.LAST_FILL if filled == intent.qty else Move.PART_FILL
    state = moved(seq, name, move, order.state)
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


def _unowned(view: View, fill: Fill) -> str | None:
    """Why a fill cannot be applied to any order the journal sent, from `the fill`, or `None`.

    A fill past its kind's own bounds, such as one for zero shares, is refused
    by `entries.past_bounds`, which `record_of` runs before the fold.
    """
    what = f"fill {broker_id_shown(fill.fill_id)}"
    order = view.acknowledged(fill.broker_order_id)
    if order is None:
        return (
            f"{what} is of the broker's order {broker_id_shown(fill.broker_order_id)}, which no "
            "order was acknowledged under"
        )
    intent, name = order.intent, order.intent.client_order_id
    if (Move.PART_FILL, order.state) not in MOVES:
        return f"{what} is of the order {name}, which is {order.state.value}"
    if fill.client_order_id not in (None, name):
        return f"{what} names another client order id than {name}, which its broker's order is"
    if fill.instrument != intent.instrument:
        return f"{what} is in {named(fill.instrument)}, and {name} is in {intent.instrument}"
    if (fill.qty > 0) != (intent.qty > 0):
        return f"{what} of {fill.qty} is not on {name}'s side"
    filled = exact_total(f"the shares filled of {name}", (order.filled, fill.qty))
    if filled.copy_abs() > intent.qty.copy_abs():
        return f"{what} takes {name} to {filled} filled of {intent.qty}"
    return None


def _outside(step: Step, record: OutsideRecordEntry) -> None:
    seq, view, kept = step.seq, step.view, record.record
    name = kept.record_id
    _require_read(step, record.read)
    if held := [one.seq for one in view.unmatched if (one.kind, one.name) == ("record", name)]:
        raise Halt(
            f"entry {seq} applies the record {name}, which entry {held[0]} keeps as unmatched"
        )
    first = view.records.get(name)
    if first is not None:
        if _alike(kept, first, _RECORD):
            # A repeat changes nothing.
            return
        raise Halt(
            f"entry {seq}'s record {name} holds {_record_said(kept)}, and the one applied holds "
            f"{_record_said(first)}"
        )
    cash = exact_total("the cash", (view._cash, kept.amount))
    try:
        from_received(cash)
    except CodecError as error:
        raise Halt(f"entry {seq} leaves the cash where no record holds it: {error}") from None
    step.change(_cash=cash)
    step.add(view._shared.records, name, kept)


def _unmatched(
    step: Step,
    read: int,
    kind: str,
    name: str,
    held: BrokerRecord | Fill | BrokerOrder,
    reason: str,
) -> None:
    """Keep what no book takes, after the checks that it was not one a book already took."""
    seq, view = step.seq, step.view
    _require_read(step, read)
    fields = {"record": _RECORD, "fill": _CONTENT, "order": _ORDER}[kind]
    applied = {"record": view.records, "fill": view.fills}.get(kind, {}).get(name)
    if kind == "fill" and applied is not None:
        raise Halt(f"entry {seq} keeps the fill {name} as unmatched, which an entry applied")
    if applied is not None and _alike(held, applied, fields):
        raise Halt(f"entry {seq} keeps the {kind} {name} as unmatched with the content applied")
    if kind == "order" and view.acknowledged(name) is not None:
        raise Halt(f"entry {seq} keeps the order {name} as unmatched, which an ack mapped")
    if same := [
        one.seq
        for one in view.unmatched
        if (one.kind, one.name) == (kind, name) and _alike(held, one.held, fields)
    ]:
        raise Halt(f"entry {seq} keeps the {kind} {name} with the content entry {same[0]} holds")
    step.change(unmatched=(*view.unmatched, Unmatched(seq, kind, name, held, reason)))


def _held(one: Unmatched) -> str:
    return (
        f"entry {one.seq} keeps an unmatched {one.kind}, which holds the account until a person "
        f"releases it, and nothing releases one yet: {one.reason}"
    )


def _alike(one: object, other: object, fields: tuple[str, ...]) -> bool:
    return all(getattr(one, name) == getattr(other, name) for name in fields)


def _record_said(record: BrokerRecord) -> str:
    on = "" if record.instrument is None else f" on {named(record.instrument)}"
    return f"{record.amount} as a {record.kind.value}{on}, dated {record.at.isoformat()}"


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
        f"{fill.qty} of {named(fill.instrument)} at {fill.price} on the order "
        f"{fill.broker_order_id}, filled {fill.at.isoformat()}"
    )
