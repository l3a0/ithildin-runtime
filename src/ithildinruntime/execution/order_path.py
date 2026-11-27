"""The one path an order takes to a broker.

`OrderPath` is the only holder of a `Broker`. `send` takes a client order id
and nothing else, and everything about the order comes from the `decision`
entry the journal holds, so no caller can hand it an order that was not
planned, or a looser limit. docs/architecture/06-runtime.md's `#### Send, once
per order` gives the steps.

1. A view of the journal, the plan the order is in, and the hard caps.
2. The `intent` entry.
3. The controls, apart from the order's last moment and the loss stop.
4. The inputs the gate is handed, and the order's last moment.
5. A second view, the loss stop's state from `Latch`, and the gate, whose quotes
   can end the order's last moment sooner.
6. The `verdict` entry, and the loss stop's firing where it read the day's loss
   past its stop.
7. The order's last moment again, and the `sending` entry.
8. The private submit, which takes that entry's acknowledgement and nothing
   else. It views the journal again, refuses an order the journal does not show
   as just approved and just sending, reads the hard caps, the loss stop and
   every control again, and calls the broker. A firing this process holds, and
   the count of its firings since the checks began, are read last, under the
   lock the call is marked under.
9. The entry that says what the broker answered.

The account's lease, which `execution/lease.py` holds, wraps those steps. `send`
reads the journal, then takes the lease, and journals a takeover where one is
owed, before step 1. The renewal of the lease is the fifth of step 8's checks,
and the release comes in a `finally`, whatever ends the send.
docs/architecture/06-runtime.md's `#### The account's lease, around each submit`
gives the steps.

An order reaches the broker at most once. Only the call whose `intent` the
journal took goes past step 2, and the account gives each `sending` entry's
acknowledgement to one submit. An order whose answer is unknown is `in_doubt`,
which `settle` settles by asking the broker. Nothing here sends it again.

`settle` takes an order of a plan whose send stage has ended, and moves it to
where only the broker moves it on, in this order.

1. An order with no entry, `Intended` or `Approved` is journaled `abandoned`,
   with an `intent` first where it has none.
2. A `Sending` order gets the crash half of `in_doubt`, since only its writer
   may journal that nothing left.
3. An `InDoubt` order is found through `Broker.find_order`, read from its
   `sending` less `OVERLAP`, and that answer is stored and named by a `read`
   entry before it is judged. A list that failed or did not come whole settles
   nothing. A whole list that omits a closing-auction order, asked for once its
   session closed, journals it `lost`, and one that omits an order of any other
   kind keeps it in doubt. One row that `copy_differs` finds no difference in is
   looked up by the broker's id, and that answer is stored and named the same
   way. A match journals the `ack`, unless a takeover named the order late
   under an older lease, whose `ack` the fold refuses, so it halts. Any other
   row, more than one, or a lookup that disagrees is journaled as an
   `unmatched_order` and halts.

**What a person reads holds none of a broker's text.** A reason, an alarm and a
halt name the order, the step and an error's type or a store's code. Two things
of a broker's pass: its order id, where it is one the journal takes, and the
reason of a `Halt` an adapter raises. The broker's own bytes reach an entry's
`raw` and a read's pages alone. A store's own words are left out the same way,
apart from a halt `core.Storage` raises on a decision's files or the hard caps.
"""

from __future__ import annotations

import secrets
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import NamedTuple

from ithildinruntime.core import (
    Ack,
    Alerts,
    BeforeClose,
    BlobMissing,
    BlobRef,
    BlobStore,
    Book,
    Broker,
    BrokerOrder,
    BrokerState,
    BrokerUnavailable,
    CashRead,
    Clock,
    FillsRead,
    FoundOrder,
    GateInputs,
    Halt,
    HardCaps,
    InstrumentId,
    JournalAck,
    KeyExists,
    Limit,
    LossReading,
    OrderEntry,
    OrderIntent,
    OrderLookup,
    OrderRecord,
    OrdersRead,
    OrderState,
    OrderType,
    Outage,
    Outcome,
    PositionsRead,
    RecordsRead,
    RiskGate,
    Snapshot,
    StopState,
    Storage,
    StoreError,
    Verdict,
    account_halt_key,
    caps_key,
    encode,
    error_name,
    held,
    is_broker_id,
    journal_entry_key,
    load_sessions,
    parse_caps,
    read_order_id,
    readable_until,
    sha256_of,
    takeover_key,
    tighten,
    to_utc,
)
from ithildinruntime.journal import (
    DOUBTFUL,
    NOT_SENT,
    RAW_LIMIT,
    AbandonedEntry,
    AckEntry,
    DecisionEntry,
    InDoubtEntry,
    IntentEntry,
    Journal,
    LostEntry,
    NumberTaken,
    ReadEntry,
    Record,
    RejectedEntry,
    Repeat,
    SendingEntry,
    TakeoverEntry,
    UnmatchedOrderEntry,
    VerdictEntry,
    View,
    kind_of,
    lost_from,
    may_be_lost,
    record_of,
)

from .controls import Controls, Stopping, Where
from .inputs import Inputs, read_inputs
from .latch import Latch
from .lease import (
    FORMAT,
    AbsentHalt,
    AccountLease,
    Held,
    Holder,
    LeaseLost,
    Taken,
    TakeoverRead,
)
from .patience import Patience, Tries, code_of

__all__ = [
    "OVERLAP",
    "Asked",
    "OrderPath",
    "Outage",
    "Settled",
    "Shared",
    "Unstored",
    "copy_differs",
    "quietly",
]

# How many times a step that lost its number starts over.
_STARTS_OVER = 5
# How many numbers an answer may lose before it is kept for later. Each loss is another
# writer's entry.
_NUMBERS = 100
# How long one pass of a submit's checks may take, in seconds, for its first read to still hold
# at its last.
_PASS = 5.0
_NOT_SENT = NOT_SENT
_DOUBTFUL = DOUBTFUL
# How many lists one takeover's time may hold before it halts.
_LISTS = 8
# The states a takeover finds an order late in, where a broker may still take it: a `sending`
# with no answer.
_LATE = frozenset({OrderState.SENDING, OrderState.IN_DOUBT})
# How far before an order's `sending` the list that finds it is read. It is larger than twice
# the clock's bound of 1 second, since the broker dates an order by a clock of its own.
OVERLAP = timedelta(seconds=60)

# Why a firing's entry says the stop fired, by the read that found the loss past its stop.
_DECIDED = "the day's loss at the decision's own time read past its stop"
_NOW = "the day's loss at the clock's time read past its stop"
_CHECKED = "an order's check read the day's loss past its stop"


def quietly(report: Callable[[], None]) -> Callable[[], None]:
    """`report`, which no error in it can turn into one of the order path's."""

    def call() -> None:
        try:
            report()
        except BaseException:
            return

    return call


def _alarm(alerts: Alerts, reason: str) -> str:
    """Send one alarm, and say what a halt adds where it could not be sent.

    Nothing an alert channel raises stops what follows, such as an answer's
    entry. A worker runs its handlers in threads, where no error ends the process.
    """
    try:
        alerts.alarm(reason)
    except BaseException as error:
        return f". The alarm could not be sent: {error_name(error)}"
    return ""


def _reason(halt: Halt) -> str:
    """A halt's reason where it is text. An adapter's own kind of halt may hold another."""
    reason = _plain(lambda: halt.reason, str)
    return error_name(halt) if reason is None else reason


def _plain[T](read: Callable[[], object], kind: type[T]) -> T | None:
    """What a read of an adapter's object gives, where it is exactly `kind`.

    A subclass is none, because its own code would run wherever it is used next.
    """
    try:
        found = read()
    except BaseException:
        return None
    return found if type(found) is kind else None


def _said(error: BaseException) -> str:
    """An error in words of the platform's own: a halt's reason, a broker call's, or a code.

    A `BrokerUnavailable` names only the call and an error's type.
    """
    if isinstance(error, BrokerUnavailable):
        return f"{error}"
    return _reason(error) if isinstance(error, Halt) else code_of(error)


class Named:
    """`Alerts` whose every alarm names the account first, by its alias."""

    def __init__(self, alerts: Alerts, account: str) -> None:
        self._alerts = alerts
        self._account = account

    def alarm(self, reason: str) -> None:
        self._alerts.alarm(f"account {self._account}: {reason}")

    def heartbeat(self) -> None:
        self._alerts.heartbeat()


@dataclass(eq=False)
class _Kept:
    """One entry the journal has not taken, about an order the broker may have."""

    name: str
    record: Record
    at: datetime
    sent: str
    # What a halt adds where an alarm about this entry could not be sent.
    added: str = ""

    @property
    def what(self) -> str:
        return f"the {kind_of(self.record)} of {self.name}, which was {self.sent}"


class Unstored:
    """The entries an account owes its journal, kept in one place for every path.

    An order's fate is known here alone once the broker has answered, so an
    entry stays until the journal takes it or shows that it already knows. An
    alarm about one that could not be sent waits in a queue of its own, which
    each `settle` tries once through, and halts nothing. So does one sent through
    `alarm`, as `Account.alarm` sends a stage's and the worker's.
    """

    def __init__(
        self, journal: Journal, alerts: Alerts, patience: Patience, owner_step: str
    ) -> None:
        self._journal = journal
        self._alerts = alerts
        self._patience = patience
        self._owner_step = owner_step
        self._kept: list[_Kept] = []
        self._alarms: deque[str] = deque()
        self._lock = threading.Lock()

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(kept.what for kept in self._kept)

    def put(
        self, name: str, record: Record, at: datetime, sent: str, report: Callable[[], None]
    ) -> tuple[OrderState, str]:
        """Store what became of an order, and return its state and what a halt adds.

        It is kept before the first try, so no error can lose it. An order in
        doubt alarms at once, ahead of its entry.
        """
        kept = _Kept(name, record, at, sent)
        with self._lock:
            self._kept.append(kept)
        if isinstance(record, InDoubtEntry):
            self._say(kept, f"the order {name} is in doubt, {sent}: {record.reason}", report)
        try:
            return self._store(kept, report, once=True), kept.added
        except Exception as error:
            failed = _said(error)
            self._say(
                kept,
                f"the journal did not take {kept.what}: {failed}. The owner's step: "
                f"{self._owner_step}",
                report,
            )
            if isinstance(error, StoreError):
                try:
                    return self._store(kept, report), kept.added
                except Exception as again:
                    failed = _said(again)
        raise Halt(
            f"the journal did not take {kept.what}: {failed}. It is kept and tried again before "
            f"the next send{kept.added}"
        )

    def hold(
        self, name: str, record: Record, at: datetime, sent: str, report: Callable[[], None]
    ) -> OrderState | None:
        """Keep an entry and try it once, for a store that has just failed and paged.

        The one try sends no alarm about the journal's outage, since that paged
        already, and returns the state the order is in where it lands. Where the
        store does not answer it, the entry stays, and each `settle` tries it, as
        it tries every kept entry. An outage of another blob, such as the hard
        caps, leaves the journal answering, so the entry lands at once. A journal
        whose fold refuses the entry and takes the `in_doubt` that stands in for
        it pages that, as `put` does, and keeps nothing. One that refuses both
        raises its halt with the entry kept, and the caller's halt names it.
        """
        kept = _Kept(name, record, at, sent)
        with self._lock:
            self._kept.append(kept)
        try:
            return self._store(kept, report, once=True)
        except StoreError:
            return None

    def settle(self, report: Callable[[], None]) -> None:
        """Try each unsent alarm and each kept entry again, and halt naming each entry left."""
        with self._lock:
            waiting, owed = list(self._kept), len(self._alarms)
        for _ in range(owed):
            # Taken one at a time, so another thread that settles sends none of them twice.
            with self._lock:
                reason = self._alarms.popleft() if self._alarms else None
            if reason is not None:
                self._alarm(reason, report)
        failing = []
        for kept in waiting:
            try:
                self._store(kept, report)
            except Exception as error:
                failing.append(f"the journal still does not take {kept.what}: {_said(error)}")
        if failing:
            raise Halt(". ".join(failing))

    def _store(self, kept: _Kept, report: Callable[[], None], once: bool = False) -> OrderState:
        """Append the entry until it lands, and return the state the order is in."""
        tries = None if once else self._patience.tries(report)

        def tried[R](call: Callable[[], R]) -> R:
            if tries is not None:
                return tries(call)
            try:
                return call()
            finally:
                report()

        own = kept.record
        for _ in range(_NUMBERS):
            view = tried(self._journal.view)
            order = view.orders.get(kept.name)
            if order is None:
                raise Halt(f"the journal holds no order {kept.name}")
            doubt = _doubted(kept)
            if _holds(order, own):
                return self._done(kept, order.state)
            if any(
                tried(lambda s=e.seq: record_of(next(self._journal.read(after=s - 1)))) == doubt
                for e in order.entries
                if e.kind == "in_doubt"
            ):
                # An append whose answer was lost landed all the same.
                if own is not doubt:
                    self._in_doubt(kept, report, f", and the journal shows it {_now(order)}")
                return self._done(kept, order.state)
            if isinstance(own, RejectedEntry) and order.broker_order_id is not None:
                # The fold would take it, and the order another writer saw taken leaves the book.
                raise Halt(f"the journal shows it {_now(order)}")
            if own is doubt and order.state not in _DOUBTFUL:
                self._say(
                    kept,
                    f"the order {kept.name} was kept as in doubt, {kept.sent}, and the journal "
                    f"shows it {_now(order)}",
                    report,
                )
                return self._done(kept, order.state)
            refused: list[str] = []
            for record in (own,) if own is doubt else (own, doubt):
                try:
                    _, after = tried(lambda v=view, r=record: self._journal.record(v, r, kept.at))
                except Halt as halt:
                    refused.append(halt.reason)
                    continue
                except NumberTaken:
                    break
                if record is not own:
                    self._in_doubt(kept, report)
                return self._done(kept, after.orders[kept.name].state)
            else:
                raise Halt(refused[0])
        raise Halt(f"other writers took {_NUMBERS} numbers in a row")

    def _in_doubt(self, kept: _Kept, report: Callable[[], None], shows: str = "") -> None:
        self._say(
            kept,
            f"the order {kept.name} is in doubt, {kept.sent}: the journal refused its "
            f"{kind_of(kept.record)}{shows}",
            report,
        )

    def _say(self, kept: _Kept, reason: str, report: Callable[[], None]) -> None:
        if not self._alarm(reason, report):
            kept.added = ". An alarm about it could not be sent"

    def alarm(self, reason: str, report: Callable[[], None]) -> bool:
        """Send one alarm, or queue it to be tried at each later settle, and say whether it went."""
        return self._alarm(reason, report)

    def unsent(self) -> tuple[str, ...]:
        """Each alarm this process still owes, in the order it was raised."""
        with self._lock:
            return tuple(self._alarms)

    def _alarm(self, reason: str, report: Callable[[], None]) -> bool:
        """Send one alarm, or queue it, and say whether it was sent."""
        sent = not _alarm(self._alerts, reason)
        if not sent:
            with self._lock:
                self._alarms.append(reason)
        report()
        return sent

    def _done(self, kept: _Kept, state: OrderState) -> OrderState:
        with self._lock:
            self._kept = [k for k in self._kept if k is not kept]
        return state


def _now(order: OrderRecord) -> str:
    """An order's state, with the broker's id where the journal holds one."""
    broker = order.broker_order_id
    return f"{order.state}" if broker is None else f"{order.state}, as the broker's {broker}"


def _holds(order: OrderRecord, record: Record) -> bool:
    """Whether the order's state already says what an entry would."""
    match record:
        case AckEntry():
            return order.broker_order_id == record.broker_order_id
        case RejectedEntry():
            return order.state == OrderState.REJECTED
        case AbandonedEntry():
            return order.state == OrderState.ABANDONED
        case InDoubtEntry():
            return False
        case _:
            raise Halt(f"a {kind_of(record)} is never kept")


def _doubted(kept: _Kept) -> InDoubtEntry:
    """What stands in for an entry the fold refuses, with the bytes it held."""
    record, raw, at = kept.record, None, kept.at
    if isinstance(record, InDoubtEntry):
        return record
    if isinstance(record, AckEntry | RejectedEntry):
        raw, at = record.raw, record.received_at
    reason = f"the journal refused the {kind_of(record)} of an order that was {kept.sent}"
    return InDoubtEntry(kept.name, reason, raw, False, at)


@dataclass(frozen=True)
class Shared:
    """What one account's paths share, built once by `Account`."""

    account: str
    store: BlobStore
    storage: Storage
    journal: Journal
    gate: RiskGate
    clock: Clock
    alerts: Alerts
    patience: Patience
    controls: Controls
    unstored: Unstored
    latch: Latch
    lease: AccountLease
    # Who takes the account's lease. Each take names a handler of its own under this one.
    holder: Holder
    # Each `sending` entry's acknowledgement that no submit has taken, by its number, with
    # the order's name, its last moment and the caps the gate was handed.
    cleared: dict[int, _Cleared] = field(default_factory=dict)
    # Each `sending` entry whose order the broker was called with, by its number.
    called: set[int] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def alarm(self, reason: str) -> str:
        """Send one alarm, and say what a halt adds where it could not be sent."""
        return _alarm(self.alerts, reason)

    def outage(self, reason: str) -> Outage:
        """A store that did not answer, alarmed here, since no halt blob could be stored either."""
        reason = f"{reason}. The owner's step: {self.storage.owner_step}"
        return Outage(f"{reason}{self.alarm(reason)}")

    def settle(self, report: Callable[[], None]) -> None:
        """Store each entry the journal has not taken, and complete the loss stop's firing.

        The kept broker answers come first, and a failure of the latch joins
        theirs rather than replacing it.
        """
        failing = []
        try:
            self.unstored.settle(report)
        except Halt as halt:
            failing.append(halt.reason)
        try:
            self.latch.complete(report)
        except Halt as halt:
            failing.append(halt.reason)
        except Exception as error:
            failing.append(f"completing the loss stop's firing raised {type(error).__name__}")
        if failing:
            raise Halt(". ".join(failing))


class _Last(NamedTuple):
    """The last moment an order may go out, and what sets it."""

    at: datetime
    what: str


class _Cleared(NamedTuple):
    ack: JournalAck
    name: str
    last: _Last
    caps: HardCaps


class _Planned(NamedTuple):
    """An order as its `decision` entry, number `seq`, plans it."""

    seq: int
    entry: DecisionEntry
    order: OrderIntent

    @property
    def where(self) -> Where:
        return Where(
            self.order.strategy, self.entry.deployment, self.entry.run, self.entry.decision_at
        )


class _Approved(NamedTuple):
    """An order the gate approved, on the view its `verdict` left."""

    view: View
    last: _Last
    order: OrderIntent
    caps: HardCaps


class _Answer(NamedTuple):
    """What a submit of `order` came to: the broker's answer, or the type of what it raised.

    `went` is when the call went out, and `at` when its answer came, which is `went` where the
    clock could not be read after it. Both are when the `sending` was built, where an error of the
    path's own hid them.
    """

    order: OrderIntent
    went: datetime
    at: datetime
    ack: object
    error: str | None
    halt: str | None

    @property
    def broker(self) -> str | None:
        """The broker's id, where the answer is an `Ack` that holds one as plain text."""
        ack = self.ack
        return _plain(lambda: ack.broker_order_id, str) if type(ack) is Ack else None

    @property
    def sent(self) -> str:
        """Whether the order went out, in words, whatever else of the answer can be read."""
        if self.broker is None:
            return f"perhaps sent at {self.went.isoformat()}"
        return f"sent at {self.went.isoformat()}, as the broker's {_shown(self.broker)}"


class Asked(NamedTuple):
    """What the gate answered for one planned order before anything was journaled.

    `verdict` is `None` where the check raised `Halt`, and `reason` is empty
    for an approval and otherwise says, in words, what stopped the order.
    """

    client_order_id: str
    verdict: Verdict | None
    reason: str

    @property
    def approved(self) -> bool:
        return self.verdict is not None and self.verdict.approved


class Settled(NamedTuple):
    """The state `settle` left an order in, and what a person is to be told of it, or nothing."""

    state: OrderState
    said: str


class _Off(Exception):
    """A strategy that is off stops the order and not the decision."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Unjournaled(Exception):
    """An `abandoned` the store did not take, with what the journal last showed."""


class _StartOver(Exception):
    """An `intent` lost its number, so the order is decided again."""


class OrderPath:
    """The order path for one decision, and the only holder of its `Broker`.

    `report` tells the receive loop the handler is not hung. It is called after
    each store call made here, each broker call and each alarm about an answer,
    and nothing it raises reaches an order. `core.Storage` reports nothing while
    it tries a read again, so its retry's deadline has to be below the loop's
    heartbeat limit.
    No call here waits on a clock, so a stop cannot end one between the
    broker's answer and its entry.
    """

    def __init__(self, shared: Shared | Halt, broker: Broker, report: Callable[[], None]) -> None:
        self._held = shared
        self._broker = broker
        self._report = quietly(report)
        # The account's lease as this path last wrote it, from a take until its release.
        self._lease: Held | None = None

    # Orders

    def send(self, client_order_id: str) -> OrderState:
        """Send one planned order, and return the state the journal then shows it in.

        An order that already has an entry is not sent again. One left
        `Intended` or `Approved` halts, and the state of any other is returned.
        A send that journals an order `ABANDONED` and returns it says the
        strategy is off. One the gate refused, the broker rejected or that is in
        doubt is returned too, for the send stage to end its plan on. Anything
        else that stops an order raises `Halt`, or the error itself where the
        path does not expect it.
        """
        self._one_account()
        self._shared.settle(self._report)
        try:
            self._take(client_order_id)
            return self._sent(client_order_id)
        except Outage as outage:
            kept = self._let_go(paged=True)
            if kept:
                raise Outage(f"{outage.reason}. {kept}") from None
            raise
        finally:
            self._let_go()

    def _sent(self, client_order_id: str) -> OrderState:
        found = self._approve(client_order_id)
        if not isinstance(found, _Approved):
            return found
        went = self._shared.clock.now()
        ack = self._sending(client_order_id, found)
        try:
            answer = self._submit(ack)
        except _Off as off:
            return self._not_sent(client_order_id, off.reason, went)
        except Exception as error:
            if ack.seq not in self._shared.called:
                if isinstance(error, Outage):
                    # It paged, and each write the store would refuse would page it again.
                    raise self._held_unsent(client_order_id, error, went) from None
                self._not_sent(client_order_id, _said(error), went)
                raise
            # The broker may have the order, whatever went wrong after its call.
            answer = _Answer(found.order, went, went, None, error_name(error), None)
        record, sent = self._read(answer)
        try:
            state, added = self._shared.unstored.put(
                client_order_id, record, answer.at, sent, self._report
            )
        except Halt as halt:
            if answer.halt is None:
                raise
            raise Halt(f"{_doubted_by(answer)}. {halt.reason}") from None
        if answer.halt is not None:
            raise Halt(f"{_doubted_by(answer)}{added}")
        if state is OrderState.ABANDONED:
            raise Halt(f"the order {client_order_id} could not be sent to the broker{added}")
        if isinstance(record, AckEntry) and state is not OrderState.ACKNOWLEDGED:
            self._late(client_order_id, state, sent, added)
        if added:
            raise Halt(f"the order {client_order_id} is {state}{added}")
        return state

    def _late(self, name: str, state: OrderState, sent: str, added: str) -> None:
        """Halt where the broker took an order a takeover found late, which the fold never acks."""
        try:
            late = self._view().late.get(name)
        except Halt:
            return
        if late is not None:
            raise Halt(
                f"the order {name} is a late order under an older lease, {sent}: the takeover at "
                f"entry {late} found it sent with no answer, and the broker's order list it "
                f"stored omits it, so it is {state} and never acknowledged. It may be working at "
                f"the broker, so a person looks it up there{added}"
            )

    def hold(self, client_order_ids: tuple[str, ...] | list[str]) -> tuple[OrderState, ...]:
        """Ask the gate about each order in turn and journal it, and send nothing.

        It stops at the first order that is not approved, and returns the state
        of each it reached.
        """
        self._one_account()
        self._shared.settle(self._report)
        states: list[OrderState] = []
        for name in client_order_ids:
            found = self._approve(name)
            states.append(OrderState.APPROVED if isinstance(found, _Approved) else found)
            if not isinstance(found, _Approved):
                break
        return tuple(states)

    def abandon(self, client_order_id: str, reason: str) -> OrderState:
        """Journal that a planned order was never sent, and why.

        It records the `intent` first where there is none, and halts, storing
        nothing, on an order that has a `sending` entry or has ended.
        """
        self._one_account()
        order = self._view().orders.get(client_order_id)
        state = None if order is None else order.state
        if state is None or state in _NOT_SENT:
            try:
                state = self._abandoned(client_order_id, reason)
            except _Unjournaled as failed:
                what = f"the abandoned of {client_order_id} was not stored: {failed}"
                raise self._outage(what) from None
            if state is OrderState.ABANDONED:
                return state
        raise Halt(f"the order {client_order_id} is {state}, so it is not abandoned")

    def ask(self, client_order_ids: tuple[str, ...] | list[str]) -> tuple[Asked, ...]:
        """Ask the gate about each planned order in turn, and store nothing.

        Each order approved is counted open in the book the next is asked on, as
        a journal holding its `verdict` would count it. It stops at the first
        order the gate refuses or that raises `Halt`, and returns the answer of
        each it reached. An `Outage` is raised as it is, since it paged and is no
        refusal. An order the journal already holds an entry of halts,
        because a plan is asked about before any of it is journaled. It reads
        no control but the loss stop, which the gate is handed, and `send`
        reads every one again. It journals no order. It settles the account
        first, and a reading past the loss stop pages and fires the stop, as
        `Latch.fire` stores it.
        """
        self._one_account()
        shared = self._shared
        shared.settle(self._report)
        view = self._view()
        book = view.book
        caps, _ = self._caps()
        answers: list[Asked] = []
        read: dict[tuple[str, str, datetime], Inputs] = {}
        for name in client_order_ids:
            if name in view.orders:
                raise Halt(f"the order {name} has an entry already, so its plan is not asked again")
            planned = self._planned(view, name)
            order = planned.order
            try:
                where = planned.where
                key = (where.deployment, where.run, where.decision_at)
                if key not in read:
                    read[key] = self._inputs(planned)
                inputs = read[key]
                limits = inputs.limits(order.strategy)
                state, error = shared.latch.state(view, shared.patience.tries(self._report))
                handed = GateInputs(
                    caps=caps, valuation=inputs.valuation, stop=state, close_entry=None
                )
                now = shared.clock.now()
                verdict = shared.gate.check(order, book, inputs.snapshot, limits, handed, now)
            except Outage:
                # It paged, and is no refusal, so the stage asks and journals nothing more.
                raise
            except Halt as halt:
                answers.append(Asked(name, None, halt.reason))
                break
            if verdict.loss.past:
                # `loss` halts the decision on this before a plan is made. The gate read it
                # again here, and a loss past its stop sends nothing, pages and fires the stop.
                halt = self._fired(verdict.loss, where[1:], _CHECKED)
                answers.append(Asked(name, None, halt.reason))
                break
            if not verdict.approved:
                stop = shared.latch.said(view, state, error)
                answers.append(Asked(name, verdict, _refused(name, verdict, stop)))
                break
            answers.append(Asked(name, verdict, ""))
            book = _counted(book, order)
        return tuple(answers)

    def off(self, strategy: str) -> str | None:
        """Why a strategy is off, by its off switch or its halts, or `None` where neither stands.

        One that cannot be read halts the decision.
        """
        self._one_account()
        shared = self._shared
        stopping = shared.controls.strategy(strategy, shared.patience.tries(self._report))
        if stopping is None:
            return None
        if stopping.down:
            raise self._outage(stopping.reason)
        if stopping.halts:
            raise Halt(stopping.reason)
        return stopping.reason

    def settle(
        self, name: str, keep: Callable[[OrderLookup], BlobRef], stage: str, at: datetime
    ) -> Settled:
        """Settle one order of a plan whose send stage has ended, and never send it.

        `keep` stores a broker's answer as it came and gives the stored file,
        which a `read` entry of `stage` at the decision `at` names before
        anything judges it. An order that already stands where only the broker
        moves it on is returned as it stands. Anything that leaves the order
        unsettled raises `Halt`.
        """
        self._one_account()
        view = self._view()
        order = view.orders.get(name)
        state = None if order is None else order.state
        if state is None or state in _NOT_SENT:
            state = self.abandon(name, "its send stage ended before it was sent")
            return Settled(state, "")
        said = ""
        if state is OrderState.SENDING:
            reason = "its send stage ended with no answer journaled"
            state = self._journaled(
                name, lambda v: InDoubtEntry(name, reason, None, False, self._shared.clock.now())
            )
            said = (
                f"the order {name} is in doubt: {reason}, so it is looked up and never sent again"
            )
        if state is not OrderState.IN_DOUBT:
            return Settled(state, said)
        state, lost = self._looked_up(name, keep, stage, at)
        return Settled(state, lost or said)

    def _looked_up(
        self, name: str, keep: Callable[[OrderLookup], BlobRef], stage: str, at: datetime
    ) -> tuple[OrderState, str]:
        """Find an order in doubt in the broker's list, then look it up by the broker's id."""
        view = self._view()
        order = view.orders[name]
        sent = order.sent_at
        if sent is None:
            raise Halt(f"the order {name} is in doubt with no sending")
        since = sent - OVERLAP
        mapped = frozenset(
            o.broker_order_id for o in view.orders.values() if o.broker_order_id is not None
        )
        asked = self._shared.clock.now()
        try:
            found = self._called(
                "find_order", FoundOrder, lambda b: b.find_order(order.intent, since, mapped)
            )
        except (Halt, BrokerUnavailable) as failed:
            raise Halt(
                f"the order list that maps {name} could not be read: {_said(failed)}, so nothing "
                "settles it"
            ) from None
        ref, seq = self._kept(keep, OrderLookup(name, since, asked, found, None), stage, at)
        if found.whole_list is not True:
            raise Halt(
                f"the order list that maps {name} did not come whole, as {ref.key}, so nothing "
                "settles it"
            )
        rows = tuple(found.rows)
        if found.answer is Outcome.NOT_FOUND and not rows:
            if not may_be_lost(order.intent):
                # A day order a broker takes after its close may wait for the next open.
                raise Halt(
                    f"the order {name} is in no row of the broker's whole order list, as "
                    f"{ref.key}, and only a closing-auction order is given up as lost until a "
                    "broker's contract test shows what a late order of another kind does"
                )
            after = lost_from(name)
            if asked < after:
                raise Halt(
                    f"the order {name} is in no row of the broker's whole order list, as "
                    f"{ref.key}, and may still arrive until {after.isoformat()}"
                )
            reason = (
                f"the broker's whole order list, asked for at {asked.isoformat()} once its time "
                f"in force ran out, omits it, as {ref.key}"
            )
            self._journaled(name, lambda v: LostEntry(name, seq, reason))
            return (
                OrderState.LOST,
                f"the order {name} is lost: {reason}. Whether it was sent is unproved",
            )
        if found.answer is not Outcome.ACCEPTED or len(rows) != 1:
            many = f"{len(rows)} rows" if rows else "no row"
            what = (
                f"the broker's order list answers {_outcome(found.answer)} with {many} for {name}"
            )
            raise self._unmatched(rows, seq, f"{what}, as {ref.key}")
        (row,) = rows
        if differs := copy_differs(order.intent, row, None, known=False):
            what = f"the broker's row for {name} differs in {', '.join(differs)}, as {ref.key}"
            raise self._unmatched(rows, seq, what)
        broker = row.broker_order_id
        asked = self._shared.clock.now()
        try:
            ack = self.lookup(broker)
        except (Halt, BrokerUnavailable) as failed:
            raise Halt(
                f"the lookup of {name} by the broker's {broker} failed: {_said(failed)}, so it "
                "stays in doubt"
            ) from None
        ref, seq = self._kept(keep, OrderLookup(name, None, asked, None, ack), stage, at)
        copy = ack.order
        raw, came = _plain(lambda: ack.raw, bytes), _utc(ack.received_at)
        if ack.outcome is not Outcome.ACCEPTED or copy is None or ack.broker_order_id != broker:
            raise Halt(
                f"the lookup of {name} by the broker's {broker} answers {_outcome(ack.outcome)}"
                f"{'' if copy is not None else ' with no copy of the order'}, as {ref.key}, so it "
                "stays in doubt"
            )
        if differs := copy_differs(order.intent, copy, broker):
            what = f"the lookup of {name} by the broker's {broker} differs in {', '.join(differs)}"
            raise self._unmatched((copy,), seq, f"{what}, as {ref.key}")
        if raw is None or came is None:
            raise Halt(f"the lookup of {name} is not plain bytes at a UTC time, as {ref.key}")
        entry = AckEntry(name, broker, raw[:RAW_LIMIT], len(raw) > RAW_LIMIT, came)
        return self._journaled(name, lambda v: entry), ""

    def _kept(
        self, keep: Callable[[OrderLookup], BlobRef], found: OrderLookup, stage: str, at: datetime
    ) -> tuple[BlobRef, int]:
        """Store a broker's answer, and journal the `read` entry naming it before any judging."""
        ref = keep(found)
        self._report()
        read = ReadEntry(stage, at, ref, found.asked_at)
        for _ in range(_NUMBERS):
            view = self._view()
            held = [n for n, entry in view.reads.items() if entry.ref == ref]
            if held:
                return ref, held[0]
            try:
                _, view = self._record(view, read)
            except NumberTaken:
                continue
            except StoreError as error:
                what = f"the read of {ref.key} was not stored in the journal"
                raise self._outage(f"{what}: {code_of(error)}") from None
            return ref, max(view.reads)
        raise Halt(f"other writers took {_NUMBERS} numbers in a row")

    def _journaled(self, name: str, made: Callable[[View], Record | None]) -> OrderState:
        """Journal what `settle` found of an order, and return the state it then shows."""
        return self._appended(name, made).orders[name].state

    def _appended(self, name: str, made: Callable[[View], Record | None]) -> View:
        """Journal one entry `settle` makes, on a new view each time its number goes.

        `made` gives `None` where the view already holds what it would say.
        """
        for _ in range(_NUMBERS):
            view = self._view()
            record = made(view)
            if record is None:
                return view
            try:
                _, view = self._record(view, record)
            except NumberTaken:
                continue
            except StoreError as error:
                what = f"the {kind_of(record)} of {name} was not stored in the journal"
                raise self._outage(f"{what}: {code_of(error)}") from None
            return view
        raise Halt(f"other writers took {_NUMBERS} numbers in a row")

    def _unmatched(self, rows: tuple[BrokerOrder, ...], seq: int, reason: str) -> Halt:
        """Keep each row as an order that traces to nothing, and give the halt that says why.

        A row the journal keeps already with this content is not kept twice, so a
        later decision's halt says the same. A store that does not answer is raised
        as an `Outage` that names the row's reason first, since every later write
        would meet it too.
        """
        said = [reason]
        for row in rows:
            if not is_broker_id(row.broker_order_id):
                said.append(
                    "a row's broker id is one the journal refuses, so it is kept in the read alone"
                )
                continue

            def made(view: View, row: BrokerOrder = row) -> Record | None:
                held = view.unmatched_repeats("order", row.broker_order_id, row)
                return None if held is Repeat.HELD else UnmatchedOrderEntry(row, seq, _text(reason))

            try:
                self._appended(row.broker_order_id, made)
            except Outage as outage:
                # It paged already, and the halt still says why the row was kept.
                raise Outage(f"{reason}. {outage.reason}") from None
            except Halt as halt:
                said.append(halt.reason)
        return Halt(". ".join(said))

    # The day's loss

    def loss(
        self, deployment: str, run: str, decision_at: datetime, *, unread: bool = False
    ) -> tuple[LossReading, ...]:
        """The day's loss against each strategy's stop, with no order needed.

        It settles the account first, which completes a firing a past write
        left part stored. Each strategy's loss is read twice: at the decision's
        own time, from its stored inputs, and then at the clock's time. A
        reading past its bound at either pages, fires the stop and halts the
        decision, and so does a loss stop that stands or cannot be read. A rerun
        after the close so fires on the decision's own loss, where the gate's
        check of the clock against the session's close halts the second read.

        `unread` fires the stop with no reading where the loss cannot be read,
        for the claim halt of a decision the journal holds no plan of, since
        only that plan proves the loss was read inside its bound.
        """
        self._one_account()
        shared = self._shared
        shared.settle(self._report)
        where = (deployment, run, decision_at)
        try:
            view = self._view()
            book = view.book
            state, error = shared.latch.state(view, shared.patience.tries(self._report))
            caps, _ = self._caps()
            inputs = read_inputs(shared.storage, deployment, run, decision_at, self._report)
            handed = GateInputs(caps=caps, valuation=inputs.valuation, stop=state, close_entry=None)
            decided = tuple(
                shared.gate.loss(book, inputs.snapshot, limits, handed, decision_at)
                for limits in inputs.constraints
            )
        except Halt as halt:
            if not unread:
                raise
            said = (
                f"the loss stop fired with no reading, because the day's loss of the decision at "
                f"{decision_at.isoformat()} could not be read and the journal holds no plan of "
                f"it: {halt.reason}"
            )
            alarmed = shared.alarm(said)
            added = shared.latch.fire(None, where, said, self._report)
            raise Halt(f"{said}{alarmed}{added}") from None
        for reading in decided:
            if reading.past:
                raise self._fired(reading, where, _DECIDED)
        now = shared.clock.now()
        readings = tuple(
            shared.gate.loss(book, inputs.snapshot, limits, handed, now)
            for limits in inputs.constraints
        )
        for reading in readings:
            if reading.past:
                raise self._fired(reading, where, _NOW)
        if state is not StopState.ABSENT:
            raise Halt(shared.latch.said(view, state, error))
        return readings

    # Reads

    def positions(self) -> PositionsRead:
        return self._called("positions", PositionsRead, lambda b: b.positions())

    def open_orders(self) -> OrdersRead:
        return self._called("open_orders", OrdersRead, lambda b: b.open_orders())

    def account(self) -> CashRead:
        return self._called("account", CashRead, lambda b: b.account())

    def activities(self, since: datetime) -> RecordsRead:
        return self._called("activities", RecordsRead, lambda b: b.activities(since))

    def fills(self, since: datetime) -> FillsRead:
        return self._called("fills", FillsRead, lambda b: b.fills(since))

    def lookup(self, broker_order_id: str) -> Ack:
        return self._called("lookup", Ack, lambda b: b.lookup(broker_order_id))

    def _called[R](self, name: str, kind: type[R], call: Callable[[Broker], R]) -> R:
        self._one_account()
        refused: BaseException
        try:
            found = call(self._broker)
        except Halt as halt:
            refused = Halt(_reason(halt))
        except BaseException as error:
            refused = BrokerUnavailable(name, error)
        else:
            self._report()
            # The type alone is read, since an answer of another kind may say anything.
            if type(found) is kind:
                return found
            refused = BrokerUnavailable(name, type(found))
        # Raised outside the handler, so no broker's error is in its context.
        self._report()
        raise refused

    # The account's lease

    def _take(self, name: str) -> None:
        """Take the account's lease for one order, and journal its takeover where one is owed.

        The journal is read before the lease, so a first use is told from a lost
        lease without racing another first use. A lease another handler holds
        ends the order with nothing journaled, and a failed takeover halts.
        """
        shared = self._shared
        tries = shared.patience.tries(self._report)
        view = self._view(tries=tries)
        holder = replace(shared.holder, handler=f"{shared.holder.handler}-{secrets.token_hex(8)}")
        try:
            taken = shared.lease.take(
                holder, tries, view.sent_at is not None, lambda: self._absent(view, tries)
            )
        except StoreError as error:
            raise self._outage(
                f"the account's lease could not be read or taken: {code_of(error)}. Whether a "
                "take landed is unknown, so this handler does not act, and a take that landed "
                "expires into a takeover"
            ) from None
        self._lease = taken.held
        if taken.took is not None:
            self._takeover(name, taken)

    def _takeover(self, name: str, taken: Taken) -> None:
        """Store the broker's order list and journal the takeover, before this handler acts.

        Any step that fails halts new orders, and the lease keeps the takeover
        owed, or expires into one, so the next take journals it. The one exception
        is the rewrite that clears `owed` finding the lease gone: the entry stands
        journaled, and the next take halts on the lease as absent where the
        journal holds a `sending`, and otherwise starts it anew.
        """
        shared, took = self._shared, taken.took
        assert took is not None and taken.tag is not None
        view = self._view()
        where = self._planned(view, name).where
        unsettled = [o for o in view.live.values() if o.state in _LATE and o.sent_at is not None]
        asked = shared.clock.now()
        since = min((o.sent_at for o in unsettled if o.sent_at is not None), default=asked)
        since -= OVERLAP
        try:
            read = self._called("orders", OrdersRead, lambda b: b.orders(since))
        except (Halt, BrokerUnavailable) as failed:
            raise self._failed("the broker's order list could not be read", _said(failed)) from None
        count, mine = taken.held.lease.count, taken.held.lease
        assert mine.handler is not None
        stored = TakeoverRead(
            FORMAT, shared.account, mine.handler, mine.release, count, since, asked, read
        )
        # Stored before it is judged, a list that did not come whole too, since the broker's
        # answer cannot be had again.
        ref = self._stored_list(where, stored)
        if read.whole is not True:
            raise self._failed(f"the broker's order list did not come whole, as {ref.key}", "")
        listed = {row.client_order_id for row in read.orders}
        owed_from, expired = taken.held.lease.owed_from, taken.held.lease.owed_expired
        for _ in range(_STARTS_OVER + 1):
            late = tuple(
                sorted(
                    order.intent.client_order_id
                    for order in view.live.values()
                    if order.state in _LATE and order.intent.client_order_id not in listed
                )
            )
            entry = TakeoverEntry(taken.tag, count, owed_from, expired, since, ref, late)
            try:
                _, view = self._record(view, entry)
                break
            except NumberTaken:
                view = self._view()
            except StoreError as error:
                what = f"the takeover of the list at {ref.key} was not journaled"
                raise self._failed(what, code_of(error), down=True) from None
            except Outage as outage:
                # It paged, and a second page would name the same outage.
                raise Outage(
                    f"the takeover of the account's lease failed: the takeover of the list at "
                    f"{ref.key} was not journaled. No new order goes out until a takeover stores "
                    f"the list and journals its entry. {outage.reason}"
                ) from None
            except Halt as halt:
                what = f"the journal refused the takeover of the list at {ref.key}"
                raise self._failed(what, halt.reason) from None
        else:
            what = f"the takeover of the list at {ref.key} lost its number {_STARTS_OVER + 1} times"
            raise self._failed(what, "")
        late_said = f", and finds {', '.join(late)} late" if late else ""
        if owed_from is None:
            whose = "a lease a person cleared"
        else:
            run = f" of the run {took.run}" if took.handler == owed_from and took.run else ""
            ran_out = f", whose lease ran out at {expired.isoformat()}" if expired else ""
            whose = f"{owed_from}{run}{ran_out}"
        if took.handler is not None and took.handler != owed_from:
            # A taker that held the lease last with the takeover still owed: one whose takeover
            # failed, or whose entry stands journaled but whose rewrite clearing `owed` did not.
            whose = (
                f"{whose}, after {took.handler}, which held it last with the takeover still owed"
            )
        # The alarm goes before the rewrite that clears `owed`, so a journaled takeover alarms
        # even where that rewrite fails.
        shared.alarm(
            f"the account's lease was taken over from {whose}, and the broker's order list is "
            f"stored at {ref.key}{late_said}"
        )
        held = self._lease
        assert held is not None
        try:
            self._lease = shared.lease.settled(held, shared.patience.tries(self._report))
        except LeaseLost as lost:
            if lost.unanswered:
                raise self._outage(lost.reason) from None
            if lost.gone:
                # The entry stands journaled, so the takeover is owed no more. The next take halts
                # on the lease as absent where the journal holds a sending, and otherwise starts it
                # anew. Nothing is left to release, so this is the one page.
                self._lease = None
                raise Halt(f"{lost.reason}{shared.alarm(lost.reason)}") from None
            # `settled` reads back a write whose answer was lost, so a refusal here is another
            # handler's rewrite. A release on this handler's tag is sure to be refused, so this
            # halt is the one page, and it says the entry stands rather than the takeover failed.
            self._lease = None
            reason = (
                "the account's lease now carries another handler's rewrite, so this handler's "
                "rewrite after its takeover, which marks the takeover done, did not stand. This "
                "handler's takeover entry stands journaled. A handler took the lease over after "
                "this handler's ran out, and it journals a takeover of its own before it acts, if "
                "it has not already. This send halts, and the next order's take reads the lease "
                "again"
            )
            raise Halt(f"{reason}{shared.alarm(reason)}") from None

    def _stored_list(self, where: Where, read: TakeoverRead) -> BlobRef:
        """Store a takeover's list create-only, at the first key its time leaves free.

        A backtest's clock reads one time for a whole decision, so a takeover tried
        again in that decision finds its earlier list there, and stores a later one.
        """
        shared, data = self._shared, encode(read)
        sha, tries = sha256_of(data), shared.patience.tries(self._report)
        for attempt in range(1, _LISTS + 1):
            key = takeover_key(where.deployment, where.run, read.asked_at, attempt)
            try:
                tries(lambda k=key: shared.store.create(k, data, sha))
            except KeyExists:
                try:
                    stands = tries(lambda k=key: shared.store.head(k)).ref.sha256
                except StoreError as error:
                    raise self._failed(
                        f"{key} could not be read", code_of(error), down=True
                    ) from None
                if stands == sha:
                    return BlobRef(key, sha)
                continue
            except StoreError as error:
                raise self._failed(
                    f"the order list was not stored at {key}", code_of(error), down=True
                ) from None
            return BlobRef(key, sha)
        raise self._failed(f"{_LISTS} keys of the takeover's list at its time are each taken", "")

    def _failed(self, what: str, why: str, down: bool = False) -> Halt:
        """A takeover that failed: new orders halt, and the lease owes it or expires into one.

        A store that refused or did not answer is an `Outage`, which pages once
        with the owner's step. The send then tries only the release of the lease.
        """
        reason = (
            f"the takeover of the account's lease failed: {what}{': ' if why else ''}{why}. No new "
            "order goes out until a takeover stores the list and journals its entry"
        )
        if down:
            return self._shared.outage(reason)
        return Halt(f"{reason}{self._shared.alarm(reason)}")

    def _absent(self, view: View, tries: Tries) -> Halt:
        """Raise the absent lease's one halt, at a key two finders of one journal share."""
        shared = self._shared
        assert view.sent_at is not None
        key = account_halt_key(
            shared.account, "execution", "lease_absent", view.sent_at, view.through.seq
        )
        reason = (
            f"the account's lease is absent at {shared.lease.key}, and the journal holds a sending "
            f"through entry {view.through.seq}, so the lease was lost. New orders halt, and the "
            f"halt stands at {key} until a person releases it"
        )
        held = AbsentHalt(
            FORMAT,
            shared.account,
            view.through.seq,
            view.sent_at,
            shared.storage.halt_record(Halt(reason)),
        )
        data = encode(held)
        try:
            tries(lambda: shared.store.create(key, data, sha256_of(data)))
        except KeyExists:
            pass
        except StoreError as error:
            # A store that did not take the halt is an outage, paged once with the owner's step.
            return shared.outage(f"{reason}. Its halt was not stored at {key}: {code_of(error)}")
        return Halt(f"{reason}{shared.alarm(reason)}")

    def _renew(self, tries: Tries) -> None:
        held = self._lease
        if held is None:
            raise Halt("no order goes out without the account's lease")
        try:
            self._lease = self._shared.lease.renew(held, tries)
        except LeaseLost as lost:
            if lost.unanswered:
                raise self._outage(lost.reason) from None
            raise

    def _let_go(self, paged: bool = False) -> str:
        """Release the lease this send holds, paging a release that did not land.

        Where the send is raising an `Outage`, which paged, the release is still
        tried, since the lease is its own key and a store that refused only another
        frees the lease at once. A release the store does not answer follows an
        outage that already paged, so it sends no second page and gives the words
        the halt adds.
        """
        held, self._lease = self._lease, None
        if held is None:
            return ""
        refused = self._shared.lease.release(held, self._shared.patience.tries(self._report))
        if refused is None:
            return ""
        # The lease expires, and the next order of this process or any other halts on it as held
        # until then, so the cause is paged here, or after an outage that paged, said in its
        # halt. An exception that cut short the answer to this handler's own last rewrite leaves
        # that write unknown here, so the page can then name it as another handler's.
        ends = held.lease.expires
        reason = refused.reason
        if ends is not None and not refused.gone:
            # A write of this handler's own that landed unanswered moved the expiry further on.
            reason = f"{reason}. It expires no earlier than {ends.isoformat()}"
        if refused.unanswered and paged:
            # The release may have landed with its answer lost, so the halt does not promise an
            # alarm that a free lease would never send.
            return (
                f"{reason[:1].upper()}{reason[1:]}. Whether it landed is unknown. If it did not, "
                "the next take after that takes it over and alarms"
            )
        if refused.unanswered:
            self._shared.outage(reason)
        else:
            self._shared.alarm(reason)
        return ""

    # Steps 1 to 6

    def _approve(self, name: str) -> _Approved | OrderState:
        for _ in range(_STARTS_OVER + 1):
            try:
                return self._asked(name)
            except _StartOver:
                continue
        raise Halt(f"the order {name} lost its intent's number {_STARTS_OVER + 1} times")

    def _asked(self, name: str) -> _Approved | OrderState:
        shared = self._shared
        view = self._view()
        planned = self._planned(view, name)
        found = view.orders.get(name)
        if found is not None:
            if found.state in _NOT_SENT:
                raise Halt(
                    f"the order {name} was left {found.state}, and the next recover settles it"
                )
            return found.state
        self._newest(view, planned)
        caps, caps_sha256 = self._caps()
        order = planned.order
        if Fraction(order.qty) % Fraction(caps.share_step) != 0:
            raise Halt(
                f"the order {name} is for {order.qty} shares, no multiple of the account's "
                f"share step of {caps.share_step}"
            )
        # The strategy's own cutoff is not read yet, and is never later than this one.
        until = _last(planned, caps.moc_cutoff).at
        try:
            _, view = self._record(view, IntentEntry(planned.seq, order), until)
        except NumberTaken:
            raise _StartOver from None
        except StoreError as error:
            raise self._ended(name, f"the intent of {name} was not stored", error) from None
        tries = shared.patience.tries(self._report, until)
        stopping = shared.controls.stopping(planned.where, tries, with_stop=False)
        if stopping is not None:
            if stopping.down:
                # Its `intent` stands, so the next recover journals it abandoned.
                raise self._outage(stopping.reason)
            if not stopping.halts:
                return self._switched_off(name, stopping.reason)
            raise self._ended(name, stopping.reason)
        try:
            inputs = self._inputs(planned)
            limits = inputs.limits(order.strategy)
            last = _last(planned, tighten(limits, caps, inputs.valuation).moc_cutoff)
            self._before(name, last)
            view = self._view(last.at)
            state, error = shared.latch.state(view, shared.patience.tries(self._report, last.at))
            handed = GateInputs(caps=caps, valuation=inputs.valuation, stop=state, close_entry=None)
            now, book = shared.clock.now(), view.book
            verdict = shared.gate.check(order, book, inputs.snapshot, limits, handed, now)
            # The gate read each quote's age once, and the call comes after two appends. The
            # verdict is no send, so its own append is not cut short.
            ends = min([last, *_aged(inputs.snapshot, {*held(book), order.instrument})])
        except Outage:
            # It paged, and an `abandoned` would page it again. Its `intent` stands, so the next
            # recover journals it abandoned.
            raise
        except Halt as halt:
            raise self._ended(name, halt.reason) from None
        except Exception as error:
            self._ended(name, f"the check of {name} raised {type(error).__name__}")
            raise
        entry = VerdictEntry(
            client_order_id=name,
            verdict=verdict,
            now=now,
            stop=state,
            close_entry=None,
            book=view.through.seq,
            caps=encode(caps),
            caps_sha256=caps_sha256,
            snapshot=inputs.refs["snapshot"],
            constraints=inputs.refs["constraints"],
            valuation=inputs.refs["valuation"],
        )
        stop = shared.latch.said(view, state, error)
        _, view = self._recorded(name, view, entry, last.at)
        if verdict.loss.past:
            where = planned.where
            halt = self._fired(
                verdict.loss, (where.deployment, where.run, where.decision_at), _CHECKED
            )
            raise self._ended(name, halt.reason) if verdict.approved else halt
        if verdict.limit is Limit.STOP:
            raise Halt(f"the gate refused {name}: {stop}")
        return _Approved(view, ends, order, caps) if verdict.approved else OrderState.REFUSED

    def _planned(self, view: View, name: str) -> _Planned:
        """The order as the journal's `decision` entry holds it, which halts on one in no plan."""
        _, at, place = read_order_id(name)
        seq = view.decided.get(at)
        if seq is not None:
            entry = view.decisions[seq]
            if place <= len(entry.orders) and entry.orders[place - 1].client_order_id == name:
                return _Planned(seq, entry, entry.orders[place - 1])
        raise Halt(f"the order {name} is in no decision the journal holds")

    def _newest(self, view: View, planned: _Planned) -> None:
        """Halt on an order of any decision but the newest, since decisions send in order."""
        at = planned.entry.decision_at
        if later := [other for other in view.decided if other > at]:
            raise Halt(
                f"the order {planned.order.client_order_id} is of the decision at "
                f"{at.isoformat()}, and the journal holds a later one at {max(later).isoformat()}"
            )

    def _inputs(self, planned: _Planned) -> Inputs:
        """The decision's files, each the one the plan was made from."""
        entry, (_, deployment, run, at) = planned.entry, planned.where
        for name in ("snapshot", "constraints", "valuation"):
            if name not in entry.inputs:
                raise Halt(f"entry {planned.seq}, a decision, names no {name}")
        inputs = read_inputs(self._shared.storage, deployment, run, at, self._report)
        for name, ref in inputs.refs.items():
            named: BlobRef = entry.inputs[name]
            if named != ref:
                raise Halt(
                    f"entry {planned.seq} planned on {named.key} with SHA-256 {named.sha256}, and "
                    f"the {name} stored is {ref.key} with {ref.sha256}"
                )
        return inputs

    def _caps(self) -> tuple[HardCaps, str]:
        """The hard caps, read and parsed afresh, with their blob's SHA-256."""
        account = self._shared.account
        data = self._shared.storage.read_blob(caps_key(account))
        self._report()
        return parse_caps(data, account), sha256_of(data)

    def _caps_now(self, tries: Tries) -> HardCaps:
        """The hard caps, read among a submit's checks, whose tries they share."""
        account = self._shared.account
        try:
            data = tries(lambda: self._shared.store.get(caps_key(account)))
        except BlobMissing as error:
            # The store answered, so this is no outage.
            raise Halt(f"the hard caps could not be read again: {code_of(error)}") from None
        except StoreError as error:
            raise self._outage(f"the hard caps could not be read again: {code_of(error)}") from None
        try:
            return parse_caps(data, account)
        except Halt as halt:
            raise Halt(f"the hard caps read again are not whole: {halt.reason}") from None

    def _before(self, name: str, last: _Last) -> datetime:
        """The clock's time, which halts past the order's last moment."""
        now = self._shared.clock.now()
        if now > last.at:
            raise Halt(
                f"the order {name} may go out until {last.at.isoformat()}, {last.what}, and it is "
                f"{now.isoformat()}"
            )
        return now

    # Steps 7 and 8

    def _sending(self, name: str, approved: _Approved) -> JournalAck:
        went = self._shared.clock.now()
        try:
            # The verdict may have waited past it, and a `sending` on its own reads as in doubt.
            self._before(name, approved.last)
            ack, _ = self._record(approved.view, SendingEntry(name), approved.last.at)
        except NumberTaken:
            raise self._ended(name, _taken(name, "sending")) from None
        except Halt as halt:
            raise self._ended(name, halt.reason) from None
        except StoreError as error:
            # It may be stored, and only this call knows that nothing followed it. The journal
            # has stopped answering, so it pages once here and the `abandoned` is kept.
            outage = self._outage(f"the sending of {name} was not stored: {code_of(error)}")
            raise self._held_unsent(name, outage, went) from None
        with self._shared.lock:
            self._shared.cleared[ack.seq] = _Cleared(ack, name, approved.last, approved.caps)
        return ack

    def _submit(self, ack: JournalAck) -> _Answer:
        """Call the broker for the order whose `sending` entry `ack` acknowledges.

        What it raises after the broker's call is told apart by the mark in
        `called`, which stands before that call. An acknowledgement is good for
        one call, so one that was used, swapped or made up halts.
        """
        shared = self._shared
        with shared.lock:
            cleared = shared.cleared.pop(ack.seq, None)
            if cleared is not None and cleared.ack is not ack:
                shared.cleared[ack.seq] = cleared
                cleared = None
        if cleared is None:
            raise Halt("no order goes out on an acknowledgement this account is not holding")
        order, went, fired = self._checked(cleared)
        found: object = None
        error = halted = None
        with shared.lock:
            # A firing counts itself under this lock as it is held, so one that another thread
            # held, or held and stored whole, since the checks began is seen here.
            if shared.latch.holding() or shared.latch.count() != fired:
                raise Halt(f"the order {cleared.name} is not sent: the loss stop fired")
            shared.called.add(ack.seq)
        try:
            found = self._broker.submit(order.intent)
        except Halt as halt:
            error, halted = error_name(halt), _reason(halt)
        except BaseException as raised:
            error = error_name(raised)
        self._report()
        try:
            came = shared.clock.now()
        except BaseException:
            # The answer is the one thing that cannot be had again, so no clock can lose it.
            came = went
        return _Answer(order.intent, went, came, found, error, halted)

    def _checked(self, cleared: _Cleared) -> tuple[OrderRecord, datetime, int]:
        """The order with every check read again in one pass, the time read last, and a count.

        A control that was read as absent may stand by the time the last is
        read. So a pass counts only where no read had to be tried again and it
        took no longer than `_PASS`. Any other is made again whole, until one
        counts or the deadline they share is over. The count is of the firings
        this process had made when the pass began, which the call compares.

        A firing stops every order here, a close after a broker's trade too,
        until #117 says what the submit does with a valid close entry.
        """
        shared, name = self._shared, cleared.name
        tries = shared.patience.tries(self._report, cleared.last.at)
        while True:
            tries.waited, began = False, shared.clock.monotonic()
            with shared.lock:
                # Read with the count, since a firing leaves memory only once the journal holds it,
                # so one counted before this pass is in memory here or in the view below.
                fired, holding = shared.latch.count(), shared.latch.holding()
            view = self._view(tries=tries)
            if holding:
                raise Halt(shared.latch.said(view, StopState.FIRED, ""))
            if shared.latch.held() or view.stop.fired:
                raise Halt(shared.latch.said(view, StopState.FIRED, ""))
            order = self._just_sending(view, cleared, tries)
            planned = _Planned(order.decision, view.decisions[order.decision], order.intent)
            self._newest(view, planned)
            # A book in dispute halts here.
            _ = view.book
            if self._caps_now(tries) != cleared.caps:
                raise Halt(f"the hard caps' limits changed since the send of {name} began")
            # The strategy's two are the last blobs read, so a switch hides no other blob's halt.
            stopping = shared.controls.stopping(planned.where, tries, with_stop=True)
            if stopping is not None:
                raise self._outage(stopping.reason) if stopping.down else _stopped(stopping)
            if not tries.waited and shared.clock.monotonic() - began <= _PASS:
                # The fifth check, on a pass that counts so far: the account's lease still
                # carries this path's own tag, which the renewal proves by rewriting it on that
                # tag. A renewal that waited leaves the pass behind as a read that waited does.
                self._renew(tries)
                if not tries.waited and shared.clock.monotonic() - began <= _PASS:
                    return order, self._before(name, cleared.last), fired
            if tries.over:
                raise Halt(
                    f"the checks of {name} were not all read in one pass by the deadline: in every "
                    f"pass a read was tried again, or the pass took longer than {_PASS:g} seconds"
                )

    def _just_sending(self, view: View, cleared: _Cleared, tries: Tries) -> OrderRecord:
        """The order, which halts unless its newest entry is this `sending`, right after a yes.

        The fold lets only an approved verdict lead to a `sending`, and a
        `sending` may follow it at any distance. One that does not follow at
        once was approved on a book that has since moved.
        """
        ack, name = cleared.ack, cleared.name
        order = view.live.get(name)
        if order is None or order.state is not OrderState.SENDING:
            raise Halt(f"the order {name} is not sending")
        if order.entries[1:] != (
            OrderEntry(ack.seq - 1, "verdict"),
            OrderEntry(ack.seq, "sending"),
        ):
            raise Halt(f"the order {name} has no verdict right before entry {ack.seq}")
        key = journal_entry_key(self._shared.account, ack.seq)
        try:
            stored = tries(lambda: self._shared.store.get(key))
        except BlobMissing as error:
            # The store answered, so this is no outage.
            raise Halt(f"entry {ack.seq} could not be read: {code_of(error)}") from None
        except StoreError as error:
            raise self._outage(f"entry {ack.seq} could not be read: {code_of(error)}") from None
        if sha256_of(stored) != ack.entry_hash:
            raise Halt(f"entry {ack.seq} is not stored as it was acknowledged")
        return order

    # Step 9

    def _read(self, answer: _Answer) -> tuple[Record, str]:
        """The entry a broker's answer comes to, and whether the order was sent, in words.

        Whatever an answer does as it is read ends in doubt, because the order
        may be at the broker. An entry holds only values of the platform's own
        types, so nothing of an adapter's runs once it is made.
        """
        try:
            return self._entry(answer)
        except BaseException as error:
            reason = f"its answer could not be read: {error_name(error)}"
        return self._doubt(answer, reason), answer.sent

    def _entry(self, answer: _Answer) -> tuple[Record, str]:
        name, ack = answer.order.client_order_id, answer.ack
        if answer.error is not None:
            return self._doubt(answer, f"the submit raised {answer.error}"), answer.sent
        if type(ack) is not Ack:
            return self._doubt(answer, f"the submit gave a {error_name(type(ack))}"), answer.sent
        raw, at = _plain(lambda: ack.raw, bytes), _utc(ack.received_at)
        broker, sent = answer.broker, answer.sent
        if broker is None and ack.broker_order_id is not None:
            reason = f"its broker's id is a {error_name(type(ack.broker_order_id))}"
        elif raw is None:
            reason = f"its bytes are a {error_name(type(ack.raw))}"
        elif at is None:
            reason = "its time is not a UTC datetime"
        elif len(raw) > RAW_LIMIT:
            reason = f"its answer is longer than {RAW_LIMIT} bytes"
        elif differs := _differs(answer.order, ack):
            reason = f"the broker's copy differs in {', '.join(differs)}"
        elif ack.outcome == Outcome.ACCEPTED and not is_broker_id(broker):
            reason = "the broker's id is one the journal refuses"
        elif contradicts := _contradicts(ack):
            reason = contradicts
        elif ack.outcome == Outcome.ACCEPTED and broker is not None:
            return AckEntry(name, broker, raw, False, at), sent
        elif ack.outcome == Outcome.REJECTED:
            said = sent if broker is not None else f"sent at {answer.went.isoformat()}"
            entry = RejectedEntry(name, "the broker rejected it", raw, False, at)
            return entry, f"{said}, and rejected"
        elif ack.outcome == Outcome.NOT_SENT:
            return AbandonedEntry(name, "the adapter says nothing was sent"), "not sent"
        else:
            reason = f"the broker's answer is {Outcome(ack.outcome).value}"
        return self._doubt(answer, reason), sent

    def _doubt(self, answer: _Answer, reason: str) -> InDoubtEntry:
        """An order in doubt, with the bytes the broker sent where they are plain bytes."""
        ack = answer.ack
        raw = at = None
        if issubclass(type(ack), Ack):
            raw = _plain(lambda: ack.raw, bytes)
            at = _plain(lambda: _utc(ack.received_at), datetime)
        cut = raw is not None and len(raw) > RAW_LIMIT
        kept = raw[:RAW_LIMIT] if raw is not None else None
        return InDoubtEntry(answer.order.client_order_id, reason, kept, cut, at or answer.at)

    def _held_unsent(self, name: str, outage: Outage, at: datetime) -> Halt:
        """Keep the `abandoned` of an order with a `sending` that never left, and try it once.

        The store has just failed and paged, so the one try sends no page about it.
        Where it lands, or the store does not answer it, this gives an `Outage`, and
        a kept entry waits for the next settle. Where the journal's fold refuses
        it, and takes its `in_doubt` or refuses that too, this gives a plain
        `Halt`, so the send stage reads the journal, where another writer may have
        sent the order. A process that dies first
        leaves the `sending` with no answer, which the next recover looks up.
        """
        try:
            at = self._shared.clock.now()
        except Exception:
            pass
        reason = outage.reason
        record = AbandonedEntry(name, _text(reason))
        try:
            state = self._shared.unstored.hold(name, record, at, "not sent", self._report)
        except Exception as error:
            # The journal answered with a refusal, so this is a plain halt and the send stage
            # reads what the journal holds, where another writer may have sent the order.
            return Halt(
                f"{reason}. The journal did not take the abandoned of {name}: {_said(error)}. "
                "It is kept and tried again before the next send"
            )
        if state is OrderState.ABANDONED:
            return Outage(f"{reason}. The journal shows {name} {state}")
        if state is not None:
            # The journal's fold refused the entry and took the `in_doubt` that stands in for it,
            # which `Unstored` paged, so this is a plain halt and the send stage reads the journal.
            return Halt(
                f"{reason}. The journal refused the abandoned of {name} and shows it {state}"
            )
        return Outage(
            f"{reason}. The abandoned of {name} is kept and tried again before the next send"
        )

    def _not_sent(self, name: str, reason: str, at: datetime) -> OrderState:
        """Journal that an order with a `sending` entry never left, which only this call knows.

        `at` is the time its `sending` was built, for a clock that cannot be read now.
        """
        try:
            at = self._shared.clock.now()
        except Exception:
            pass
        record = AbandonedEntry(name, _text(reason))
        try:
            state, added = self._shared.unstored.put(name, record, at, "not sent", self._report)
        except Halt as halt:
            raise Halt(f"{reason}. {halt.reason}") from None
        if added:
            raise Halt(f"{reason}. The order {name} is {state}{added}")
        return state

    # The journal

    @property
    def _shared(self) -> Shared:
        if isinstance(self._held, Halt):
            raise Halt(self._held.reason)
        return self._held

    def _one_account(self) -> None:
        """Halt unless the journal, the controls and the broker name one account.

        `Account` builds the first two from one alias. What the broker names is
        never shown, since it could be a broker's number.
        """
        account, error = self._shared.account, None
        try:
            alias = self._broker.alias
            same = type(alias) is str and alias == account
        except BaseException as raised:
            same, error = False, error_name(raised)
        if error is not None:
            raise Halt(f"the broker's alias could not be read: {error}")
        if not same:
            raise Halt(f"the broker trades another account than {account!r}")

    def _view(self, until: datetime | None = None, tries: Tries | None = None) -> View:
        tries = tries or self._shared.patience.tries(self._report, until)
        try:
            return tries(self._shared.journal.view)
        except StoreError as error:
            raise self._outage(f"the journal could not be read: {code_of(error)}") from None

    def _record(
        self, view: View, record: Record, until: datetime | None = None
    ) -> tuple[JournalAck, View]:
        """Append one entry, built once and resent unchanged."""
        at, tries = self._shared.clock.now(), self._shared.patience.tries(self._report, until)
        return tries(lambda: self._shared.journal.record(view, record, at))

    def _recorded(
        self,
        name: str,
        view: View,
        record: Record,
        until: datetime | None,
    ) -> tuple[JournalAck, View]:
        """Append the `verdict` of an order, or end the order and halt."""
        kind = kind_of(record)
        try:
            return self._record(view, record, until)
        except NumberTaken:
            # The book has moved, or another call holds this entry. Nothing is asked twice.
            raise self._ended(name, _taken(name, kind)) from None
        except StoreError as error:
            raise self._ended(name, f"the {kind} of {name} was not stored", error) from None
        except Halt as halt:
            raise self._ended(name, halt.reason) from None

    def _switched_off(self, name: str, reason: str) -> OrderState:
        halt = self._ended(name, reason)
        if halt.reason != reason:
            raise halt
        return OrderState.ABANDONED

    def _ended(self, name: str, reason: str, error: StoreError | None = None) -> Halt:
        """Journal `abandoned` for an order with no `sending`, and give the halt that says why.

        An order the journal shows further on has been moved by another writer,
        which may have it at the broker, so it alarms and is left as it is.
        """
        if error is not None:
            reason = f"{reason}: {code_of(error)}"
        try:
            state = self._abandoned(name, reason)
        except _Unjournaled as failed:
            return self._outage(f"{reason}. The abandoned of {name} was not stored: {failed}")
        except Halt as halt:
            return Halt(f"{reason}. {name} was not journaled abandoned: {halt.reason}")
        if state is OrderState.ABANDONED:
            return Halt(reason)
        what = f"{reason}. The journal shows {name} {state}, which this call did not do"
        return Halt(f"{what}{_alarm(self._shared.alerts, what)}")

    def _abandoned(self, name: str, reason: str) -> OrderState:
        """Journal `abandoned` for an order with no `sending`, and return the state it is in."""
        journal, tries = self._shared.journal, self._shared.patience.tries(self._report)
        shows = "the journal could not be read"
        for _ in range(_STARTS_OVER + 1):
            try:
                view = tries(journal.view)
                order = view.orders.get(name)
                if order is None:
                    shows = f"the journal showed no entry of {name}"
                    planned = self._planned(view, name)
                    _, view = self._record(view, IntentEntry(planned.seq, planned.order))
                    order = view.orders[name]
                shows = f"the journal showed {name} {order.state}"
                if order.state not in _NOT_SENT:
                    return order.state
                self._record(view, AbandonedEntry(name, _text(reason)))
                return OrderState.ABANDONED
            except NumberTaken:
                continue
            except StoreError as error:
                raise _Unjournaled(f"{code_of(error)}, and {shows}") from None
        raise Halt(f"{name} lost its number {_STARTS_OVER + 1} times")

    def _outage(self, reason: str) -> Outage:
        return self._shared.outage(reason)

    def _past(self, reading: LossReading) -> Halt:
        """The halt on a reading past its stop, alarmed before the firing's writes are tried."""
        reason = (
            f"the day's loss of {reading.loss} is past its stop of {reading.bound}, measured from "
            f"the valuation of {reading.session.isoformat()}"
        )
        return Halt(f"{reason}{_alarm(self._shared.alerts, reason)}")

    def _fired(self, reading: LossReading, where: tuple[str, str, datetime], reason: str) -> Halt:
        """Alarm on a reading past its stop, fire the stop, and give the halt with both."""
        halt = self._past(reading)
        added = self._shared.latch.fire(reading, where, reason, self._report)
        return Halt(f"{halt.reason}{added}")


def _counted(book: Book, order: OrderIntent) -> Book:
    """The book with one more open order, as an approved `verdict` would leave it."""
    orders = dict(book.open_orders_by_strategy)
    orders[order.strategy] = (*orders.get(order.strategy, ()), order)
    return replace(book, open_orders_by_strategy=orders)


def _refused(name: str, verdict: Verdict, stop: str) -> str:
    """A refusal in words: the limit, and the figure and its bound where it has them.

    `stop` is the loss stop's state in words, which a refusal on it names.
    """
    if verdict.limit is Limit.STOP:
        return f"the gate refused {name}: {stop}"
    said = f"the gate refused {name} on {verdict.limit}"
    if verdict.value is not None:
        said = f"{said}, at {verdict.value} against its bound of {verdict.bound}"
    return said


def _last(planned: _Planned, minutes: int) -> _Last:
    """A market-on-close order's cutoff, and any other order's session close.

    The gate takes an order up to the close of its decision's session, and a
    broker may hold one sent after it for the next session's open. A quote the
    gate marked at ends it sooner, as `_aged` says.
    """
    session = load_sessions().opened_by(planned.entry.decision_at)
    if planned.order.type == OrderType.MARKET_ON_CLOSE:
        return _Last(to_utc(session.day, BeforeClose(minutes)), "its market-on-close cutoff")
    return _Last(session.close, "its session's close")


def _aged(snapshot: Snapshot, marked: set[InstrumentId]) -> list[_Last]:
    """When each quote the gate marked an instrument at is past its written maximum age."""
    return [
        _Last(readable_until(quote), f"when the quote of {name} reaches its maximum age")
        for name, quote in snapshot.quotes.items()
        if name in marked
    ]


def _utc(at: object) -> datetime | None:
    """A time in UTC, or `None` for one that is not and for no plain time."""
    if type(at) is not datetime or at.utcoffset() != timedelta(0):
        return None
    return at.astimezone(UTC)


def _doubted_by(answer: _Answer) -> str:
    return f"the order {answer.order.client_order_id} is in doubt: {answer.halt}"


def _differs(order: OrderIntent, ack: Ack) -> list[str]:
    """Each field the broker's copy of the order gives another value, where it sent a copy."""
    found = ack.order
    return [] if found is None else copy_differs(order, found, ack.broker_order_id)


def _contradicts(ack: Ack) -> str | None:
    """How an answer says two things of one order, where it does.

    An order journaled as never sent or as rejected leaves the book, so the next
    order is checked as if the broker had none of it.
    """
    copy = ack.order
    if ack.outcome == Outcome.NOT_SENT:
        if ack.broker_order_id is not None or copy is not None or ack.raw:
            return "its answer says nothing was sent and holds something the broker sent"
    elif ack.outcome == Outcome.REJECTED and copy is not None:
        if copy.state != BrokerState.REJECTED or copy.filled != 0:
            return "its answer says it was rejected and its copy does not"
    elif ack.outcome == Outcome.ACCEPTED and copy is not None:
        if copy.state == BrokerState.REJECTED:
            return "its answer says it was accepted and its copy that it was rejected"
    return None


def _taken(name: str, kind: str) -> str:
    return f"another writer took the number of {name}'s {kind}"


def _stopped(stopping: Stopping) -> Exception:
    return Halt(stopping.reason) if stopping.halts else _Off(stopping.reason)


def copy_differs(
    intent: OrderIntent, copy: BrokerOrder, broker: str | None, known: bool = True
) -> list[str]:
    """Each detail a broker's copy of an order gives another value than its `sending` entry's.

    It is the order path's one comparison of a single copy the broker returns
    for an order against the order the platform sent, which a submit's answer, a
    settling list's row and lookup, and recover's lookup of an order the broker
    no longer lists each run. `core.reconcile` compares the open orders a broker
    lists by fields of its own, because `core` imports nothing of `execution`. It reads the client
    order id, the instrument, the quantity, whose sign is the side, the type, the
    time in force and the broker's id, which is `broker` where `known`, and any
    id the journal takes otherwise. A copy of a broker that keeps a client order
    id and gives none is no match.
    """
    pairs = [
        ("client_order_id", copy.client_order_id, intent.client_order_id),
        ("instrument", copy.instrument, intent.instrument),
        ("qty", copy.qty, intent.qty),
        ("type", copy.type, intent.type),
        ("time_in_force", copy.time_in_force, intent.time_in_force),
    ]
    if known:
        pairs.append(("broker_order_id", copy.broker_order_id, broker))
    else:
        pairs.append(("broker_order_id", is_broker_id(copy.broker_order_id), True))
    return [name for name, one, other in pairs if one != other]


def _outcome(outcome: object) -> str:
    return outcome.value if isinstance(outcome, Outcome) else "no outcome"


def _shown(broker_order_id: str) -> str:
    """A broker's id as an alarm shows it, where it is one the journal would take."""
    return broker_order_id if is_broker_id(broker_order_id) else "id the journal refuses"


def _text(reason: str) -> str:
    """A reason the codec can write. A lone surrogate has no form, so it is escaped."""
    return reason.encode("utf-8", "backslashreplace").decode()
