"""Execution's recover stage: what the account holds, said before any model runs.

Every decision starts here. The stage reads the broker, brings the journal up to
what the broker did since, and compares the two. A difference halts. Agreement
gives the risk and portfolio stages the three files they read: the book, the hard
caps as this stage read them, and the session's valuation.

Its handler waits for the decision's time, having written nothing, and then works
in this order. docs/architecture/06-runtime.md's `#### What recover does, step by
step` says why each comes where it does.

1. Data's result and its snapshot.
2. The answers this process still owes the journal, a view of the journal, and
   each plan a send stage left unfinished, settled by `OrderPath.settle`, which
   sends none of its orders. An order that stays unsettled halts here with an
   alarm.
3. Five reads of the broker, the records after the cash and the fills last.
4. The capture, `account.json`, stored before anything in it is compared.
5. A `read` entry that names the capture, and then whether each read is whole.
6. An entry for each new record, fill and open order the broker lists, as
   `execution/outside.py` sorts them: a record on the closed list moves the
   cash, a fill of an order the platform sent moves its book, a repeat that
   differs is a dispute, and anything else is kept as unmatched and holds the
   account. A dispute or an unmatched entry halts here, ahead of any lookup.
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

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ithildinruntime.core import (
    ACCOUNT_OUTPUT,
    BOOK_OUTPUT,
    CAPS_OUTPUT,
    RECOVER_STAGE,
    VALUATION_OUTPUT,
    AccountRead,
    BlobRef,
    Book,
    Broker,
    BrokerOrder,
    Clock,
    DecisionResult,
    Halt,
    HardCaps,
    OrderRecord,
    Progress,
    Snapshot,
    StageDecision,
    StageKind,
    StageRun,
    Storage,
    Valuation,
    caps_key,
    load_sessions,
    parse_caps,
    read_order_id,
    reconcile,
    value_book,
)
from ithildinruntime.journal import (
    OPEN,
    DecisionEntry,
    View,
)

from .account import Account
from .order_path import OrderPath, Outage
from .sync import UNSETTLED, journal_read, keeper, read_broker

__all__ = ["RecoverStage", "differences", "fills_since"]

# The format of the stage's four files and of its result header. Format 2's capture holds the
# records read, which format 1's does not.
_FORMAT = 2
# How long before the bound's own time the records and the fills are asked from, so one the
# broker posts late is still fetched.
_LATE = timedelta(days=1)
# The issue that copies the first decision's valuation to a later one of its session.
_SECOND = "#183"
_RECEIVED = datetime(2000, 1, 1, tzinfo=UTC)
# A stored lookup's key, which names its decision.
_STORED = re.compile(r"runs/\S+?/lookup-[0-9]+\.json")


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
        path = account.path(brokers(at), report)
        view = self._settled(decision, path, view, at, report)

        made = read_broker(path, fills_since(view))
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
        journal_read(
            account,
            path,
            view,
            capture.ref,
            read,
            stage=self._run.stage,
            at=at,
            clock=self._clock,
            report=report,
        )

        view = account.view(report)
        book = view.book
        if found := reconcile(book, view.live.values(), read):
            said = "; ".join(str(difference) for difference in found)
            raise Halt(f"the book and the broker differ, as {capture.ref.key} holds it: {said}")
        self._past_their_session([o for o in view.live.values() if o.state in OPEN], at, report)

        caps = self._caps(decision)
        return (caps, *self._files(decision, book, view, snapshot, at))

    # Step 2

    def _settled(
        self,
        decision: StageDecision,
        path: OrderPath,
        view: View,
        at: datetime,
        report: Callable[[], None],
    ) -> View:
        """Settle each plan a send stage left unfinished, and halt on any order it cannot.

        The receive loop runs recover only once the last decision's send stage
        has a result, so each such plan's stage has ended. Each order of it is
        settled by `OrderPath.settle`, which sends nothing. An order in doubt or
        lost alarms, since a crash writes no entry and this can be the first a
        person hears of it.
        """
        plans = [entry for entry in view.decisions.values() if not _settled_plan(entry, view)]
        if not plans:
            return view
        stage = self._run.stage

        keep = keeper(decision, self._run.deployment, self._run.run, stage, at)
        failed: list[str] = []
        added = ""
        for planned in [o for entry in plans for o in entry.orders]:
            name = planned.client_order_id
            try:
                settled = path.settle(name, keep, stage, at)
            except Outage as outage:
                # Every later order would meet the same outage, so nothing more is tried. Its
                # own alarm went, and this one names each order, which nothing else does.
                names = ", ".join(o.client_order_id for e in plans for o in e.orders)
                when = ", ".join(e.decision_at.isoformat() for e in plans)
                left = (
                    f"nothing more is settled of the decisions at {when}, whose orders are {names}"
                )
                step = "Look each up on the broker's site"
                page = f"the journal stopped answering, so {left}. {step}"
                added = self._alarm(page, report) or added
                said = [*failed, outage.reason, left[0].upper() + left[1:]]
                raise Halt(f"account {self._account.alias}: {'. '.join(said)}{added}") from None
            except Halt as halt:
                failed.append(halt.reason)
                added = self._alarm(halt.reason, report) or added
                continue
            if settled.said:
                added = self._alarm(settled.said, report) or added
        view = self._account.view(report)
        said: list[str] = []
        for entry in plans:
            if _settled_plan(entry, view):
                continue
            stands = [
                _stands(o.client_order_id, view.orders.get(o.client_order_id)) for o in entry.orders
            ]
            when = entry.decision_at.isoformat()
            said.append(f"the decision at {when} is not settled: {'; '.join(stands)}")
        if said:
            raise Halt(f"account {self._account.alias}: {'. '.join([*failed, *said])}{added}")
        return view

    def _alarm(self, reason: str, report: Callable[[], None]) -> str:
        """Send an alarm once in this process, and say what a halt adds where it went nowhere.

        A stored lookup's key is under each decision's own prefix, so it is left out of
        what makes two alarms the same cause.
        """
        cause = _STORED.sub("a stored lookup", reason)
        if cause in self._said:
            return ""
        self._said.add(cause)
        return self._account.alarm(reason, report)

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
    """The time the records and the fills are asked from, before any the journal may not hold.

    A record the broker dates earlier than this is never fetched, so the cash it
    moved shows as a difference and halts.

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
    records = tuple(replace(one, received_at=_RECEIVED) for one in read.records.records)
    return {
        "positions": dict(read.positions.positions),
        "open orders": read.open_orders.orders,
        "cash": read.cash.cash,
        "records": records,
        "fills": fills,
        "the time the records and fills were asked from": read.since,
        "whether each came whole": [part.whole for part in read.reads.values()],
        "the pages' bytes": [[p.body for p in part.received] for part in read.reads.values()],
    }


# The parts of two reads that may differ while the account stands still: when the pages
# came, and their bytes. A later read that did not come whole could hide a fill, so it
# counts as a move.
_UNMOVED = frozenset({"the time the records and fills were asked from", "the pages' bytes"})


def _moved(first: AccountRead, later: AccountRead) -> list[str]:
    """What differs between two reads in a way that may be a move.

    A fill or a record only the first holds counts as no move where the later read
    asked from a later time than its own, as a bound moved by the first attempt's
    own fills does. Open orders are compared by the broker's id, so a list in another order is
    the same list, and one that holds an order twice is another. A fill listed twice
    is no move, since each is applied once by its id.
    """
    one, two = _undated(first), _undated(later)
    dated = ("records", "fills")
    named = [p for p in one if p not in _UNMOVED and p not in dated and one[p] != two[p]]
    if "open orders" in named and _by_id(first) == _by_id(later):
        named.remove("open orders")
    for part in dated:
        gone = [f for f in one[part] if f not in two[part]]  # type: ignore[attr-defined]
        new = [f for f in two[part] if f not in one[part]]  # type: ignore[attr-defined]
        if new or any(f.at >= later.since for f in gone):
            named.append(part)
    return named


def _by_id(read: AccountRead) -> list[BrokerOrder]:
    """The open orders sorted by the broker's id, so one listed twice stays twice."""
    return sorted(read.open_orders.orders, key=lambda order: order.broker_order_id)


def _settled_plan(entry: DecisionEntry, view: View) -> bool:
    """Whether each order of a plan stands where a lookup or a fill alone moves it on."""
    return _finished(view.orders.get(o.client_order_id) for o in entry.orders)


def _finished(orders: Iterable[OrderRecord | None]) -> bool:
    """Whether a plan's orders each stand where a lookup or a fill alone moves them on."""
    found = list(orders)
    if any(order is not None and order.state in UNSETTLED for order in found):
        return False
    missing = found.count(None)
    return missing in (0, len(found))


def _stands(name: str, order: OrderRecord | None) -> str:
    if order is None:
        return f"the order {name} has no entry"
    when = "never" if order.sent_at is None else f"at {order.sent_at.isoformat()}"
    return f"the order {name} is {order.state}, its sending stored {when}"


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
