"""Execution's recover stage: what the account holds, said before any model runs.

Every decision starts here. The stage reads the broker, brings the journal up to
what the broker did since, and compares the two. A difference halts. Agreement
gives the risk and portfolio stages the three files they read: the book, the hard
caps as this stage read them, and the session's valuation.

Its handler waits for the decision's time, having written nothing, and then works
in this order. docs/architecture/06-runtime.md's `#### What recover does, step by
step` says why each comes where it does.

1. Data's result and its snapshot.
2. The answers this process still owes the journal, and a view of the journal. An
   order nothing has settled halts here with an alarm.
3. Four reads of the broker, the fills last.
4. The capture, `account.json`, stored before anything in it is compared.
5. A `read` entry that names the capture, and then whether each read is whole.
6. A `fill` entry for each new fill, and a `disputed_fill` for one that differs.
7. A lookup of each order the broker no longer lists, and the entry of its end.
8. A second view, and `core.reconcile`.
9. The hard caps.
10. `caps.json`, `book.json`, `valuation.json` and the result.

A second attempt at one decision reads the broker again, and goes on from the
first attempt's capture and from each file it stored. So the owner can rewrite the
caps and the journal can grow between two attempts, and both end in the same files.
Where the account itself moved between the two reads, the attempt halts.

What a person reads shows a broker's order or fill id where it is one the journal
takes, an instrument where it is a name, and a client order id where it is a stamp
of the platform's, and no other text of the broker's. The broker's own words are in
`account.json`. An alarm goes once in a process, and again after a restart.
Monitoring alarms on a halt as design.md's `### Rule 7: monitoring tells the owner
once` says, once for a run of halts with one reason. Until #11 builds it, this
stage's alarm is the only page about an order a crash left.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from ithildinruntime.core import (
    ACCOUNT_OUTPUT,
    BOOK_OUTPUT,
    CAPS_OUTPUT,
    RECOVER_STAGE,
    VALUATION_OUTPUT,
    AccountRead,
    Ack,
    BlobRef,
    Book,
    Broker,
    BrokerOrder,
    BrokerState,
    BrokerUnavailable,
    Clock,
    DecisionResult,
    Fill,
    Halt,
    HardCaps,
    OrderRecord,
    OrderState,
    Outcome,
    Progress,
    Snapshot,
    StageDecision,
    StageKind,
    StageRun,
    Storage,
    Valuation,
    broker_id_shown,
    caps_key,
    load_sessions,
    parse_caps,
    read_order_id,
    reconcile,
    value_book,
)
from ithildinruntime.journal import (
    DOUBTFUL,
    ENDED,
    NOT_SENT,
    OPEN,
    RAW_LIMIT,
    DisputedFillEntry,
    ExpiredEntry,
    FillEntry,
    NumberTaken,
    ReadEntry,
    RejectedEntry,
    Repeat,
    View,
)

from .account import Account, StageRecord
from .order_path import OrderPath

__all__ = ["RecoverStage", "differences", "fills_since"]

# The format of the stage's four files and of its result header.
_FORMAT = 1
# How many numbers one entry may lose to another writer.
_NUMBERS = 100
# How long before the bound's own time the fills are asked from, so one the broker posts late
# is still fetched.
_LATE = timedelta(days=1)
# The issue that copies the first decision's valuation to a later one of its session.
_SECOND = "#183"
_ZERO = Decimal(0)
_RECEIVED = datetime(2000, 1, 1, tzinfo=UTC)
# An order the broker has, as the journal's own groups of states give it, and every other
# state that has not ended, which only a lookup, a person or #147 settles.
# A state no group names is unsettled, so it halts rather than being looked up.
_AT_THE_BROKER = OPEN - NOT_SENT - DOUBTFUL
_UNSETTLED = frozenset(OrderState) - ENDED - _AT_THE_BROKER


class _Item(Protocol):
    """The receive loop's item, of which recover reads only the decision time."""

    decision_at: datetime


class RecoverStage:
    """The handler the receive loop calls for the recover stage.

    `brokers` gives one decision's broker, or is the `Halt` that says why this
    run has none, which every decision then ends in.
    """

    def __init__(
        self,
        account: Account,
        brokers: Callable[[datetime], Broker] | Halt,
        storage: Storage,
        clock: Clock,
        *,
        stage: str,
        kind: StageKind,
        data_stage: str,
        data_hashes: Mapping[str, str],
        deployment: str,
        run: str,
        release: str,
        config_hashes: Mapping[str, str],
    ) -> None:
        if not release:
            raise ValueError("recover names its release, which a second attempt checks")
        self._account = account
        self._brokers = brokers
        # Each alarm this process has sent, so a cause that stands pages once and not at
        # every decision. Monitoring alarms on the halts themselves, by its own rule.
        self._said: set[str] = set()
        self._clock = clock
        self._data_stage = data_stage
        self._data_hashes = dict(data_hashes)
        self._run = StageRun(
            storage,
            format=_FORMAT,
            stage=stage,
            kind=kind,
            deployment=deployment,
            run=run,
            release=release,
            config_hashes=config_hashes,
        )

    def __call__(self, item: _Item, report: Progress) -> DecisionResult:
        # Nothing is written yet, so a stop may end this wait.
        self._clock.wait_until(item.decision_at, report)
        decision = self._run.decision(item.decision_at, report)
        try:
            files = self._recovered(decision, item.decision_at, report)
        except Halt as halt:
            return decision.halted(halt)
        return decision.done(*files)

    def _recovered(
        self, decision: StageDecision, at: datetime, report: Callable[[], None]
    ) -> tuple[BlobRef, ...]:
        account, brokers = self._account, self._brokers
        if isinstance(brokers, Halt):
            # Decisions follow each other, and a raised exception gains its own traceback.
            raise Halt(brokers.reason)
        decision.result(self._data_stage, self._data_hashes)
        snapshot = decision.snapshot(self._data_stage)

        account.settle(report)
        view = account.view(report)
        if view.opened_at is None:
            # A journal with no entry has no book, whose own halt says so.
            _ = view.book
        self._require_settled(view, report)

        path = account.path(brokers(at), report)
        made = _read_broker(path, fills_since(view))
        capture = decision.capture(ACCOUNT_OUTPUT, AccountRead, made, differences)
        # The capture that counts, which is an earlier attempt's where there was one.
        read = capture.entry
        if read != made and (moved := _moved(read, made)):
            # The two reads differ, so the account may have moved and its files would be stale.
            raise Halt(
                f"{capture.ref.key} and this attempt's read of the broker differ in "
                f"{', '.join(moved)}, so the account may have moved between two attempts at "
                "one decision"
            )
        view = self._record(view, lambda v: self._read_entry(v, capture.ref, read, at), report)
        seq = next(n for n, entry in view.reads.items() if entry.ref == capture.ref)
        if short := read.short:
            raise Halt(f"the broker's {', '.join(short)} did not come whole, as {capture.ref.key}")

        for fill in read.fills.fills:
            view = self._record(view, lambda v, f=fill: _fill_entry(v, f, seq), report)
        # A fill in dispute halts here, ahead of any lookup, naming it.
        _ = view.book
        listed = {order.broker_order_id for order in read.open_orders.orders}
        for name, order in view.live.items():
            if order.state in _AT_THE_BROKER and order.broker_order_id not in listed:
                view = self._ended(view, name, order, path, report)

        view = account.view(report)
        book = view.book
        if found := reconcile(book, view.live.values(), read):
            said = "; ".join(str(difference) for difference in found)
            raise Halt(f"the book and the broker differ, as {capture.ref.key} holds it: {said}")
        self._past_their_session([o for o in view.live.values() if o.state in OPEN], at, report)

        caps = self._caps(decision)
        return (caps, *self._files(decision, book, view, snapshot, at))

    # Step 2

    def _require_settled(self, view: View, report: Callable[[], None]) -> None:
        """Halt on a plan a crash left unfinished, with an alarm for each of its orders.

        A crash writes no `in_doubt` entry, so this can be the first a person
        hears of the order. #147 settles one.
        """
        orders = view.orders
        said: list[str] = []
        for entry in view.decisions.values():
            planned = {o.client_order_id: orders.get(o.client_order_id) for o in entry.orders}
            if _finished(planned.values()):
                continue
            stands = [_stands(name, order) for name, order in planned.items()]
            when = f"the decision at {entry.decision_at.isoformat()}"
            added = ""
            for one in stands:
                why = f"{one}, in {when}, whose orders are not settled"
                added = self._alarm(why, report) or added
            said.append(f"{when} is not settled: {'; '.join(stands)}{added}")
        if said:
            raise Halt(f"account {self._account.alias}: {'. '.join(said)}")

    def _alarm(self, reason: str, report: Callable[[], None]) -> str:
        """Send an alarm once in this process, and say what a halt adds where it went nowhere."""
        if reason in self._said:
            return ""
        self._said.add(reason)
        return self._account.alarm(reason, report)

    # Steps 5 to 7

    def _read_entry(
        self, view: View, ref: BlobRef, read: AccountRead, at: datetime
    ) -> ReadEntry | None:
        if any(entry.ref == ref for entry in view.reads.values()):
            return None
        # Never later than now, whatever clock an adapter dates its pages by, since the next
        # recover asks for the fills from this time.
        came = [page.at for part in read.reads.values() for page in part.received]
        return ReadEntry(self._run.stage, at, ref, min(self._clock.now(), *came))

    def _record(
        self,
        view: View,
        decide: Callable[[View], StageRecord | None],
        report: Callable[[], None],
    ) -> View:
        """Record what `decide` says on a view, deciding again on a new one each time a number goes.

        It says nothing where the view already holds it, so a fill another
        instance recorded is seen and not repeated.
        """
        for _ in range(_NUMBERS):
            record = decide(view)
            if record is None:
                return view
            try:
                return self._account.record(view, record, report)
            except NumberTaken:
                view = self._account.view(report)
        raise Halt(f"other writers took {_NUMBERS} numbers of the journal in a row")

    def _ended(
        self,
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

        return self._record(view, decide, report)

    # Step 8

    def _past_their_session(
        self, orders: list[OrderRecord], at: datetime, report: Callable[[], None]
    ) -> None:
        """Alarm on each order still open past the close it was to end by, which halts nothing.

        Both sides hold it open, so the comparison agrees. A day with no bar
        for its instrument leaves a simulated order so. It goes once in a
        process, and a restart sends it again.
        """
        for order in orders:
            name = order.intent.client_order_id
            _, sent_for, _ = read_order_id(name)
            close = load_sessions().opened_by(sent_for).close
            if close < at:
                self._alarm(
                    f"the order {name} is still open after its session's close at "
                    f"{close.isoformat()}",
                    report,
                )

    # Steps 9 and 10

    def _caps(self, decision: StageDecision) -> BlobRef:
        """The stored caps file, whose header names the owner's blob it was parsed from."""
        key = caps_key(self._account.alias)
        stored = decision.own(CAPS_OUTPUT, HardCaps)
        if stored is None:
            parsed = parse_caps(decision.blob(key), self._account.alias)
            stored = decision.keep(CAPS_OUTPUT, HardCaps, parsed)
        sha256 = stored.header.inputs.get(key)
        if sha256 is None:
            raise Halt(f"{stored.ref.key} does not name {key}, which its caps came from")
        # The blob the stored file came from, whatever this attempt read.
        decision.names(BlobRef(key, sha256))
        return stored.ref

    def _files(
        self, decision: StageDecision, book: Book, view: View, snapshot: Snapshot, at: datetime
    ) -> tuple[BlobRef, BlobRef]:
        """`book.json` and `valuation.json`, each as first stored, and V from the stored book."""
        # Worked out ahead of the book's own file, so a book nothing can value is not stored.
        made = _value(book, view, snapshot, at)
        kept = decision.keep(BOOK_OUTPUT, Book, book)
        if kept.entry != book:
            # An earlier attempt's, which the journal may have grown past and nothing else.
            if replace(kept.entry, replayed_through=book.replayed_through) != book:
                raise Halt(f"{kept.ref.key} holds another book than the journal's")
            made = _value(kept.entry, view, snapshot, at)
        value = decision.keep(VALUATION_OUTPUT, Valuation, made)
        if value.entry != made:
            raise Halt(f"{value.ref.key} holds another valuation than its book's")
        return kept.ref, value.ref


def fills_since(view: View) -> datetime:
    """The time the fills are asked from, before any fill the journal may not hold.

    It is one day before the earlier of two times, and never before the account
    was opened, whose opening cash holds every fill before it.

    1. The newest recover read whose decision has a `decision` entry after it. A
       read alone proves nothing, because a recover that stored one and then
       halted journaled no fill of it.
    2. The `sending` of the oldest order that has not ended.
    """
    opened = view.opened_at
    if opened is None:
        raise Halt(f"the journal of {view.account} holds no opening, so no fill can be dated")
    decided = view.decided
    done = [
        entry.at
        for seq, entry in view.reads.items()
        if entry.stage == RECOVER_STAGE and decided.get(entry.decision_at, 0) > seq
    ]
    sent = [order.sent_at for order in view.live.values() if order.sent_at is not None]
    times = ([max(done)] if done else []) + ([min(sent)] if sent else [])
    return max(opened, min(times) - _LATE) if times else opened


def differences(first: AccountRead, later: AccountRead) -> str:
    """What differs between two captures of one decision's account, for the alarm."""
    one, two = _undated(first), _undated(later)
    named = [name for name, part in one.items() if part != two[name]]
    if not named:
        return "Only the times the pages came differ, as on a second attempt after a crash"
    return f"These differ: {', '.join(named)}"


def _undated(read: AccountRead) -> dict[str, object]:
    """Each part of a capture without the times it came."""
    fills = tuple(replace(fill, received_at=_RECEIVED) for fill in read.fills.fills)
    return {
        "positions": dict(read.positions.positions),
        "open orders": read.open_orders.orders,
        "cash": read.cash.cash,
        "fills": fills,
        "the time the fills were asked from": read.since,
        "whether each came whole": [part.whole for part in read.reads.values()],
        "the pages' bytes": [[p.body for p in part.received] for part in read.reads.values()],
    }


# The parts of two reads that may differ while the account stands still: when the pages
# came, and their bytes. A later read that did not come whole could hide a fill, so it
# counts as a move.
_UNMOVED = frozenset({"the time the fills were asked from", "the pages' bytes"})


def _moved(first: AccountRead, later: AccountRead) -> list[str]:
    """What differs between two reads in a way that may be a move.

    A fill only the first holds counts as no move where the later read asked from a
    later time than that fill's, as a bound moved by the first attempt's own fills
    does. Open orders are compared by the broker's id, so a list in another order is
    the same list, and one that holds an order twice is another. A fill listed twice
    is no move, since each is applied once by its id.
    """
    one, two = _undated(first), _undated(later)
    named = [p for p in one if p not in _UNMOVED and p != "fills" and one[p] != two[p]]
    if "open orders" in named and _by_id(first) == _by_id(later):
        named.remove("open orders")
    gone = [f for f in one["fills"] if f not in two["fills"]]  # type: ignore[attr-defined]
    new = [f for f in two["fills"] if f not in one["fills"]]  # type: ignore[attr-defined]
    if new or any(f.at >= later.since for f in gone):
        named.append("fills")
    return named


def _by_id(read: AccountRead) -> list[BrokerOrder]:
    """The open orders sorted by the broker's id, so one listed twice stays twice."""
    return sorted(read.open_orders.orders, key=lambda order: order.broker_order_id)


def _asked[R](call: Callable[[], R]) -> R:
    """A read of the broker, whose failure halts naming the call and the kind of error alone."""
    try:
        return call()
    except BrokerUnavailable as error:
        reason = f"{error}"
    raise Halt(reason)


def _read_broker(path: OrderPath, since: datetime) -> AccountRead:
    # In this order, so a fill that lands between two reads is a difference and never a match.
    return AccountRead(
        positions=_asked(path.positions),
        open_orders=_asked(path.open_orders),
        cash=_asked(path.account),
        since=since,
        fills=_asked(lambda: path.fills(since)),
    )


def _finished(orders: Iterable[OrderRecord | None]) -> bool:
    """Whether a plan's orders each stand where a lookup or a fill alone moves them on."""
    found = list(orders)
    if any(order is not None and order.state in _UNSETTLED for order in found):
        return False
    missing = found.count(None)
    return missing in (0, len(found))


def _stands(name: str, order: OrderRecord | None) -> str:
    if order is None:
        return f"the order {name} has no entry"
    when = "never" if order.sent_at is None else f"at {order.sent_at.isoformat()}"
    return f"the order {name} is {order.state}, its sending stored {when}"


def _fill_entry(view: View, fill: Fill, read: int) -> FillEntry | DisputedFillEntry | None:
    repeat = view.repeats(fill)
    if repeat is Repeat.HELD:
        return None
    if repeat is Repeat.DIFFERS:
        return DisputedFillEntry(fill, read)
    if view.acknowledged(fill.broker_order_id) is None:
        raise Halt(
            f"the fill {broker_id_shown(fill.fill_id)} is of the broker's order "
            f"{broker_id_shown(fill.broker_order_id)}, which no order of the platform's was "
            "acknowledged under"
        )
    return FillEntry(fill, read)


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


def _value(book: Book, view: View, snapshot: Snapshot, at: datetime) -> Valuation:
    """V at the close of the session before the decision's, built by the session's first recover.

    A recover is the first where no order was sent strictly after that close. One sent
    at the close itself belongs to the session that closed.
    """
    table = load_sessions()
    session = table.opened_by(at)
    before = table.before(session.day)
    if view.sent_at is not None and view.sent_at > before.close:
        raise Halt(
            f"an order was sent at {view.sent_at.isoformat()}, after the close of "
            f"{before.day.isoformat()}, so this is a second decision in the session of "
            f"{session.day.isoformat()}, whose valuation {_SECOND} copies from the first"
        )
    return value_book(book, snapshot, before.day)
