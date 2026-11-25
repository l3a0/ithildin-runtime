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
5. A second view, the loss stop's state, and the gate.
6. The `verdict` entry.
7. The `sending` entry.
8. The private submit, which takes that entry's acknowledgement and nothing
   else. It views the journal again, refuses an order the journal does not show
   as just approved and just sending, reads the hard caps and every control
   again and calls the broker.
9. The entry that says what the broker answered.

An order reaches the broker at most once. Only the call whose `intent` the
journal took goes past step 2, and the account gives each `sending` entry's
acknowledgement to one submit. An order whose answer is unknown is `in_doubt`,
which a lookup settles (#147). Nothing here sends it again.

**What a person reads holds none of a broker's text.** A reason, an alarm and a
halt name the order, the step and an error's type or a store's code. Two things
of a broker's pass: its order id, where it is one the journal takes, and the
reason of a `Halt` an adapter raises. The broker's own bytes reach an entry's
`raw` and a read's pages alone. A store's own words are left out the same way,
apart from a halt `core.Storage` raises on a decision's files or the hard caps.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import NamedTuple

from ithildinruntime.core import (
    Ack,
    Alerts,
    BeforeClose,
    BlobRef,
    BlobStore,
    Broker,
    BrokerState,
    BrokerUnavailable,
    CashRead,
    Clock,
    FillsRead,
    GateInputs,
    Halt,
    HardCaps,
    JournalAck,
    Limit,
    LossReading,
    OrderEntry,
    OrderIntent,
    OrderRecord,
    OrdersRead,
    OrderState,
    OrderType,
    Outcome,
    PositionsRead,
    RiskGate,
    StopState,
    Storage,
    StoreError,
    caps_key,
    encode,
    error_name,
    journal_entry_key,
    load_sessions,
    parse_caps,
    read_order_id,
    sha256_of,
    tighten,
    to_utc,
)
from ithildinruntime.journal import (
    RAW_LIMIT,
    AbandonedEntry,
    AckEntry,
    DecisionEntry,
    InDoubtEntry,
    IntentEntry,
    Journal,
    NumberTaken,
    Record,
    RejectedEntry,
    SendingEntry,
    VerdictEntry,
    View,
    kind_of,
    record_of,
)

from .controls import Controls, Stopping, Where
from .inputs import Inputs, read_inputs
from .patience import Patience, Tries, code_of

__all__ = ["OrderPath", "Shared", "Unstored", "quietly"]

# How many times a step that lost its number starts over.
_STARTS_OVER = 5
# How many numbers an answer may lose before it is kept for later. Each loss is another
# writer's entry.
_NUMBERS = 100
# How long one pass of a submit's checks may take, in seconds, for its first read to still hold
# at its last.
_PASS = 5.0
_BROKER_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}")
_NOT_SENT = frozenset({OrderState.INTENDED, OrderState.APPROVED})
_DOUBTFUL = frozenset({OrderState.SENDING, OrderState.IN_DOUBT})


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
    """An error in words of the platform's own: a halt's reason, or a code."""
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
    each `settle` tries once through, and halts nothing.
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
            if isinstance(own, RejectedEntry) and order.broker_order_id is not None:
                # The fold would take it, and the order another writer saw taken leaves the book.
                raise Halt(f"the journal shows it {_now(order)}")
            if any(
                tried(lambda s=e.seq: record_of(next(self._journal.read(after=s - 1)))) == doubt
                for e in order.entries
                if e.kind == "in_doubt"
            ):
                # An append whose answer was lost landed all the same.
                if own is not doubt:
                    self._in_doubt(kept, report)
                return self._done(kept, order.state)
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

    def _in_doubt(self, kept: _Kept, report: Callable[[], None]) -> None:
        self._say(
            kept,
            f"the order {kept.name} is in doubt, {kept.sent}: the journal refused its "
            f"{kind_of(kept.record)}",
            report,
        )

    def _say(self, kept: _Kept, reason: str, report: Callable[[], None]) -> None:
        if not self._alarm(reason, report):
            kept.added = ". An alarm about it could not be sent"

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
    # Each `sending` entry's acknowledgement that no submit has taken, by its number, with
    # the order's name, its last moment and the caps the gate was handed.
    cleared: dict[int, _Cleared] = field(default_factory=dict)
    # Each `sending` entry whose order the broker was called with, by its number.
    called: set[int] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


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
    def perhaps(self) -> str:
        return f"perhaps sent at {self.went.isoformat()}"


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

    # Orders

    def send(self, client_order_id: str) -> OrderState:
        """Send one planned order, and return the state the journal then shows it in.

        An order that already has an entry is not sent again. One left
        `Intended` or `Approved` halts, and the state of any other is returned.
        A send that journals an order `ABANDONED` and returns it says the
        strategy is off. One the gate refused, the broker rejected or that is in
        doubt is returned too, for the send stage to end its plan on. Anything
        else that stops an order raises `Halt`.
        """
        self._one_account()
        self._shared.unstored.settle(self._report)
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
        if added:
            raise Halt(f"the order {client_order_id} is {state}{added}")
        return state

    def hold(self, client_order_ids: tuple[str, ...] | list[str]) -> tuple[OrderState, ...]:
        """Ask the gate about each order in turn and journal it, and send nothing.

        It stops at the first order that is not approved, and returns the state
        of each it reached.
        """
        self._one_account()
        self._shared.unstored.settle(self._report)
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

    # The day's loss

    def loss(self, deployment: str, run: str, decision_at: datetime) -> tuple[LossReading, ...]:
        """The day's loss against each strategy's stop, with no order needed.

        A reading past its bound alarms and halts the decision, and so does a
        loss stop that has fired or cannot be read. The firing is recorded by
        #156.
        """
        self._one_account()
        view = self._view()
        book = view.book
        state, error = self._shared.controls.stop(self._shared.patience.tries(self._report))
        caps, _ = self._caps()
        inputs = read_inputs(self._shared.storage, deployment, run, decision_at, self._report)
        handed = GateInputs(caps=caps, valuation=inputs.valuation, stop=state, close_entry=None)
        now = self._shared.clock.now()
        readings = tuple(
            self._shared.gate.loss(book, inputs.snapshot, limits, handed, now)
            for limits in inputs.constraints
        )
        for reading in readings:
            if reading.past:
                raise self._past(reading)
        if state is not StopState.ABSENT:
            raise Halt(_stop_reason(state, error))
        return readings

    # Reads

    def positions(self) -> PositionsRead:
        return self._called("positions", PositionsRead, lambda b: b.positions())

    def open_orders(self) -> OrdersRead:
        return self._called("open_orders", OrdersRead, lambda b: b.open_orders())

    def account(self) -> CashRead:
        return self._called("account", CashRead, lambda b: b.account())

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
                raise Halt(f"the order {name} was left {found.state}, and nothing settles it yet")
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
            if not stopping.halts:
                return self._switched_off(name, stopping.reason)
            raise self._ended(name, stopping.reason)
        try:
            inputs = self._inputs(planned)
            limits = inputs.limits(order.strategy)
            last = _last(planned, tighten(limits, caps, inputs.valuation).moc_cutoff)
            self._before(name, last)
            view = self._view(last.at)
            state, error = shared.controls.stop(shared.patience.tries(self._report, last.at))
            handed = GateInputs(caps=caps, valuation=inputs.valuation, stop=state, close_entry=None)
            now = shared.clock.now()
            verdict = shared.gate.check(order, view.book, inputs.snapshot, limits, handed, now)
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
        _, view = self._recorded(name, view, entry, last.at)
        if verdict.loss.past:
            halt = self._past(verdict.loss)
            raise self._ended(name, halt.reason) if verdict.approved else halt
        if verdict.limit is Limit.STOP:
            raise Halt(f"the gate refused {name}: {_stop_reason(state, error)}")
        return _Approved(view, last, order, caps) if verdict.approved else OrderState.REFUSED

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
        except StoreError as error:
            raise Halt(f"the hard caps could not be read again: {code_of(error)}") from None
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
            ack, _ = self._record(approved.view, SendingEntry(name), approved.last.at)
        except NumberTaken:
            raise self._ended(name, _taken(name, "sending")) from None
        except Halt as halt:
            raise self._ended(name, halt.reason) from None
        except StoreError as error:
            # It may be stored, and only this call knows that nothing followed it.
            reason = f"the sending of {name} was not stored: {code_of(error)}"
            self._not_sent(name, reason, went)
            raise Halt(reason) from None
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
        order, went = self._checked(cleared)
        found: object = None
        error = halted = None
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

    def _checked(self, cleared: _Cleared) -> tuple[OrderRecord, datetime]:
        """The order with every check read again in one pass, and the time read last.

        A control that was read as absent may stand by the time the last is
        read. So a pass counts only where no read had to be tried again and it
        took no longer than `_PASS`. Any other is made again whole, until one
        counts or the deadline they share is over.
        """
        shared, name = self._shared, cleared.name
        tries = shared.patience.tries(self._report, cleared.last.at)
        while True:
            tries.waited, began = False, shared.clock.monotonic()
            view = self._view(tries=tries)
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
                raise _stopped(stopping)
            if not tries.waited and shared.clock.monotonic() - began <= _PASS:
                return order, self._before(name, cleared.last)
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
        except StoreError as error:
            raise Halt(f"entry {ack.seq} could not be read: {code_of(error)}") from None
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
        return self._doubt(answer, reason), answer.perhaps

    def _entry(self, answer: _Answer) -> tuple[Record, str]:
        name, ack = answer.order.client_order_id, answer.ack
        if answer.error is not None:
            return self._doubt(answer, f"the submit raised {answer.error}"), answer.perhaps
        if type(ack) is not Ack:
            return self._doubt(answer, f"the submit gave a {error_name(type(ack))}"), answer.perhaps
        raw, at = _plain(lambda: ack.raw, bytes), _utc(ack.received_at)
        broker = _plain(lambda: ack.broker_order_id, str)
        sent = answer.perhaps
        if broker is not None:
            sent = f"sent at {answer.went.isoformat()}, as the broker's {_shown(broker)}"
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
        elif ack.outcome == Outcome.ACCEPTED and not _BROKER_ID.fullmatch(broker or ""):
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
            same = bool(self._broker.alias == account)
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

    def _outage(self, reason: str) -> Halt:
        """A store that did not answer, alarmed here, since no halt blob could be stored either."""
        reason = f"{reason}. The owner's step: {self._shared.storage.owner_step}"
        return Halt(f"{reason}{_alarm(self._shared.alerts, reason)}")

    def _past(self, reading: LossReading) -> Halt:
        reason = (
            f"the day's loss of {reading.loss} is past its stop of {reading.bound}, measured from "
            f"the valuation of {reading.session.isoformat()}"
        )
        return Halt(f"{reason}{_alarm(self._shared.alerts, reason)}")


def _last(planned: _Planned, minutes: int) -> _Last:
    """A market-on-close order's cutoff, and any other order's session close.

    The gate takes an order up to the close of its decision's session, and a
    broker may hold one sent after it for the next session's open.
    """
    session = load_sessions().opened_by(planned.entry.decision_at)
    if planned.order.type == OrderType.MARKET_ON_CLOSE:
        return _Last(to_utc(session.day, BeforeClose(minutes)), "its market-on-close cutoff")
    return _Last(session.close, "its session's close")


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
    if found is None:
        return []
    pairs = (
        ("client_order_id", found.client_order_id, order.client_order_id),
        ("instrument", found.instrument, order.instrument),
        ("qty", found.qty, order.qty),
        ("type", found.type, order.type),
        ("time_in_force", found.time_in_force, order.time_in_force),
        ("broker_order_id", found.broker_order_id, ack.broker_order_id),
    )
    return [name for name, one, other in pairs if one != other]


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


def _stop_reason(state: StopState, error: str) -> str:
    return "loss stop stands" if state is StopState.FIRED else f"loss stop unreadable: {error}"


def _shown(broker_order_id: str) -> str:
    """A broker's id as an alarm shows it, where it is one the journal would take."""
    return broker_order_id if _BROKER_ID.fullmatch(broker_order_id) else "id the journal refuses"


def _text(reason: str) -> str:
    """A reason the codec can write. A lone surrogate has no form, so it is escaped."""
    return reason.encode("utf-8", "backslashreplace").decode()
