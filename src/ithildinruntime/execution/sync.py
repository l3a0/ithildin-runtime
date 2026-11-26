"""One read of the broker and what the journal takes from it, which both execution stages run.

Recover reads the broker before any model runs, and the send stage reads it
again right before it plans. Each does the same four things, from here, so the
two never drift apart.

1. `read_broker` asks for the positions, the open orders, the cash, the records
   the broker booked on its own and the fills since a time, in that order, so a
   record or a fill that lands between two reads is a difference and never a match.
2. `journal_read` records the `read` entry that names the stored capture, before
   any value in it is used, and halts on a read that did not come whole.
3. It records an entry for each new record, fill and open order the broker
   lists, as `execution/outside.py` sorts them, and halts on a fill in dispute
   or anything kept as unmatched, ahead of any lookup.
4. It looks up each order the journal holds at the broker that the broker no
   longer lists, and records the entry of its end, or halts. An answer that
   halts is not stored, which #211 changes.

What a person reads shows a broker's order or fill id where it is one the
journal takes, and no other text of the broker's.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from ithildinruntime.core import (
    AccountRead,
    Ack,
    BlobRef,
    BrokerState,
    BrokerUnavailable,
    Clock,
    CodecError,
    Halt,
    OrderLookup,
    OrderRecord,
    OrderState,
    Outcome,
    StageDecision,
    lookup_key,
)
from ithildinruntime.journal import (
    DOUBTFUL,
    ENDED,
    NOT_SENT,
    OPEN,
    RAW_LIMIT,
    ExpiredEntry,
    NumberTaken,
    ReadEntry,
    RejectedEntry,
    View,
)

from .account import Account, StageRecord
from .order_path import OrderPath
from .outside import fill_entry, load_ranges, order_entry, record_entry

__all__ = [
    "AT_THE_BROKER",
    "UNSETTLED",
    "Keep",
    "journal_read",
    "keeper",
    "read_broker",
    "recorded",
]

# What stores one answer of a broker's about one order, as it came, and gives the stored file.
type Keep = Callable[[OrderLookup], BlobRef]

# How many numbers one entry may lose to another writer.
_NUMBERS = 100
_ZERO = Decimal(0)
# An order the broker has, as the journal's own groups of states give it, and every other
# state that has not ended, which only a lookup, a person or recover's settling moves on.
# A state no group names is unsettled, so it halts rather than being looked up.
AT_THE_BROKER = OPEN - NOT_SENT - DOUBTFUL
UNSETTLED = frozenset(OrderState) - ENDED - AT_THE_BROKER


def keeper(decision: StageDecision, deployment: str, run: str, stage: str, at: datetime) -> Keep:
    """Store each lookup of a decision afresh, at `lookup-<n>.json` under the stage's prefix."""

    def keep(found: OrderLookup) -> BlobRef:
        def key(n: int) -> str:
            return lookup_key(deployment, run, at, stage, n)

        try:
            return decision.fresh(key, OrderLookup, found).ref
        except CodecError:
            # Its text quotes a field, and the field came from the broker.
            raise Halt(
                f"what the broker answered of {found.client_order_id} is no stored lookup, so it "
                "is not stored and nothing settles the order"
            ) from None

    return keep


def read_broker(path: OrderPath, since: datetime) -> AccountRead:
    """The five reads of the broker, the fills last, each failure a halt naming the call."""
    # In this order, so a fill that lands between two reads is a difference and never a match.
    return AccountRead(
        positions=_asked(path.positions),
        open_orders=_asked(path.open_orders),
        cash=_asked(path.account),
        since=since,
        records=_asked(lambda: path.activities(since)),
        fills=_asked(lambda: path.fills(since)),
    )


def journal_read(
    account: Account,
    path: OrderPath,
    view: View,
    ref: BlobRef,
    read: AccountRead,
    *,
    stage: str,
    at: datetime,
    clock: Clock,
    report: Callable[[], None],
) -> tuple[View, int]:
    """Journal one stored read of the broker, and return the view after it and its entry's number.

    `ref` is the stored capture that holds `read`. A read that did not come
    whole halts once its `read` entry is stored, and so does a fill in
    dispute or anything kept as unmatched, ahead of any lookup.
    """
    view = recorded(account, view, lambda v: _read_entry(v, stage, ref, read, at, clock), report)
    seq = next(n for n, entry in view.reads.items() if entry.ref == ref)
    if short := read.short:
        raise Halt(f"the broker's {', '.join(short)} did not come whole, as {ref.key}")
    ranges = load_ranges()
    for one in read.records.records:
        view = recorded(account, view, lambda v, r=one: record_entry(v, r, seq, ranges), report)
    for fill in read.fills.fills:
        view = recorded(account, view, lambda v, f=fill: fill_entry(v, f, seq), report)
    for listed_order in read.open_orders.orders:
        view = recorded(account, view, lambda v, o=listed_order: order_entry(v, o, seq), report)
    # A fill in dispute, and anything kept as unmatched, halts here, ahead of any lookup.
    _ = view.book
    listed = {order.broker_order_id for order in read.open_orders.orders}
    for name, order in view.live.items():
        if order.state in AT_THE_BROKER and order.broker_order_id not in listed:
            view = _ended(account, view, name, order, path, report)
    return view, seq


def recorded(
    account: Account,
    view: View,
    decide: Callable[[View], StageRecord | None],
    report: Callable[[], None],
) -> View:
    """Record what `decide` says on a view, deciding again on a new one each time a number goes.

    It says nothing where the view already holds it, so an entry another
    instance recorded is seen and not repeated.
    """
    for _ in range(_NUMBERS):
        record = decide(view)
        if record is None:
            return view
        try:
            return account.record(view, record, report)
        except NumberTaken:
            view = account.view(report)
    raise Halt(f"other writers took {_NUMBERS} numbers of the journal in a row")


def _read_entry(
    view: View, stage: str, ref: BlobRef, read: AccountRead, at: datetime, clock: Clock
) -> ReadEntry | None:
    if any(entry.ref == ref for entry in view.reads.values()):
        return None
    # Never later than now, whatever clock an adapter dates its pages by, since the next
    # recover asks for the fills from this time.
    came = [page.at for part in read.reads.values() for page in part.received]
    return ReadEntry(stage, at, ref, min(clock.now(), *came))


def _ended(
    account: Account,
    view: View,
    name: str,
    order: OrderRecord,
    path: OrderPath,
    report: Callable[[], None],
) -> View:
    """Journal how an order the broker no longer lists ended, or halt."""
    broker = order.broker_order_id
    if broker is None:
        raise Halt(f"the order {name} is {order.state} under no broker's id")
    ack = _asked(lambda: path.lookup(broker))
    record = _end_entry(name, order, ack)

    def decide(v: View) -> StageRecord | None:
        return record if name in v.live else None

    return recorded(account, view, decide, report)


def _asked[R](call: Callable[[], R]) -> R:
    """A read of the broker, whose failure halts naming the call and the kind of error alone."""
    try:
        return call()
    except BrokerUnavailable as error:
        reason = f"{error}"
    raise Halt(reason)


def _end_entry(name: str, order: OrderRecord, ack: Ack) -> ExpiredEntry | RejectedEntry:
    """The entry a lookup of an order the broker no longer lists comes to, or a halt."""
    what = f"the order {name}, the broker's {order.broker_order_id}, is not listed open"
    copy = ack.order
    if ack.outcome is not Outcome.ACCEPTED or copy is None:
        raise Halt(f"{what}, and its lookup found no such order")
    found = (ack.broker_order_id, copy.broker_order_id, copy.client_order_id)
    if (*found, copy.instrument, copy.qty) != (
        order.broker_order_id,
        order.broker_order_id,
        name,
        order.intent.instrument,
        order.intent.qty,
    ):
        raise Halt(f"{what}, and its lookup found another order")
    raw, cut = ack.raw[:RAW_LIMIT], len(ack.raw) > RAW_LIMIT
    if copy.state is BrokerState.EXPIRED:
        return ExpiredEntry(name, copy.filled, raw, cut, ack.received_at)
    if copy.state is BrokerState.REJECTED and copy.filled == _ZERO:
        reason = "the broker rejected it after it had taken it"
        return RejectedEntry(name, reason, raw, cut, ack.received_at)
    state = copy.state.value if isinstance(copy.state, BrokerState) else "in no state"
    raise Halt(f"{what}, and its lookup says {state} with {copy.filled} filled")
