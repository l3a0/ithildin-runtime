"""The one path an order takes to a broker.

`OrderPath` is the only holder of a `Broker`. `send` takes a client order id
and nothing else, and everything about the order comes from the `decision`
entry the journal holds, so no caller can hand it an order that was not
planned, or a looser limit. docs/architecture/06-runtime.md's `#### Send, once
per order` gives the steps.

1. A view of the journal, the plan the order is in, and the hard caps.
2. The `intent` entry.
3. The controls, apart from the cutoff and the loss stop.
4. The inputs the gate is handed, and the cutoff.
5. A second view, the loss stop's state, and the gate.
6. The `verdict` entry.
7. The `sending` entry.
8. The private submit, which takes that entry's acknowledgement and nothing
   else. It views the journal again, refuses an order the journal does not show
   as just approved and just sending, reads every control again and calls the
   broker.
9. The entry that says what the broker answered.

An order reaches the broker at most once. Only the call whose `intent` the
journal took goes past step 2, and the account gives each `sending` entry's
acknowledgement to one submit. An order whose answer is unknown is `in_doubt`,
which a lookup settles (#147). Nothing here sends it again.

**What a person reads never holds what a broker said.** A reason, an alarm and a
halt name the order, the step and a type of error. The broker's own bytes reach
an entry's `raw` and a read's pages alone. A store's own words are left out the
same way, apart from a halt `core.Storage` raises on a decision's files.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from typing import NamedTuple

from ithildinruntime.core import (
    Ack,
    Alerts,
    BeforeClose,
    BlobRef,
    BlobStore,
    Broker,
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
)

from .controls import Controls, Stopping, Where
from .inputs import Inputs, read_inputs
from .patience import Patience

__all__ = ["OrderPath", "Shared", "Unstored", "quietly"]

# How many times a step that lost its number starts over.
_STARTS_OVER = 5
# How many numbers an answer may lose before it is kept for later. Each loss is another
# writer's entry.
_NUMBERS = 100
_BROKER_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}")
_NOT_SENT = frozenset({OrderState.INTENDED, OrderState.APPROVED})


def quietly(report: Callable[[], None]) -> Callable[[], None]:
    """`report`, which no error in it can turn into one of the order path's."""

    def call() -> None:
        try:
            report()
        except BaseException:
            return

    return call


def _alarm(alerts: Alerts, reason: str) -> str:
    """Send one alarm, and say what a halt adds where it could not be sent."""
    try:
        alerts.alarm(reason)
    except Exception as error:
        return f". The alarm could not be sent: {type(error).__name__}"
    return ""


def _said(error: BaseException) -> str:
    """An error in words of the platform's own: a halt's reason, or a type."""
    return error.reason if isinstance(error, Halt) else type(error).__name__


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
    entry stays until the journal takes it or shows that it already knows.
    """

    def __init__(self, journal: Journal, alerts: Alerts, patience: Patience) -> None:
        self._journal = journal
        self._alerts = alerts
        self._patience = patience
        self._kept: list[_Kept] = []
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
            self._say(kept, f"the order {name} is in doubt, {sent}: {record.reason}")
        try:
            return self._store(kept, report, once=True), kept.added
        except Exception as error:
            failed = _said(error)
            self._say(kept, f"the journal did not take {kept.what}: {failed}")
            if isinstance(error, StoreError):
                try:
                    return self._store(kept, report), kept.added
                except Exception as again:
                    failed = _said(again)
        raise Halt(
            f"the journal did not take {kept.what}: {failed}. It is kept and tried again before "
            f"the next send{kept.added}"
        )

    def keep(self, name: str, record: Record, at: datetime, sent: str) -> None:
        """Keep an entry the store has just refused, with no try of it here."""
        with self._lock:
            self._kept.append(_Kept(name, record, at, sent))

    def settle(self, report: Callable[[], None]) -> None:
        """Try each kept entry again, and halt naming each one the journal still does not take."""
        with self._lock:
            waiting = list(self._kept)
        failing = []
        for kept in waiting:
            try:
                self._store(kept, report)
            except Exception as error:
                failing.append(f"{kept.what} ({_said(error)})")
        if failing:
            raise Halt(f"the journal still does not take {'; '.join(failing)}")

    def _store(self, kept: _Kept, report: Callable[[], None], once: bool = False) -> OrderState:
        """Append the entry until it lands, and return the state the order is in."""

        def tried[R](call: Callable[[], R]) -> R:
            found = call() if once else self._patience.tried(call, report)
            report()
            return found

        for _ in range(_NUMBERS):
            view = tried(self._journal.view)
            order = view.orders.get(kept.name)
            if order is None:
                raise Halt(f"the journal holds no order {kept.name}")
            if _holds(order, kept.record):
                self._done(kept)
                if isinstance(kept.record, InDoubtEntry) and order.state != OrderState.IN_DOUBT:
                    self._say(
                        kept,
                        f"the order {kept.name} was kept as in doubt, and the journal shows it "
                        f"{order.state}",
                    )
                return order.state
            refused: Halt | None = None
            for record in (kept.record, _doubted(kept)):
                try:
                    _, after = tried(lambda v=view, r=record: self._journal.record(v, r, kept.at))
                except Halt as halt:
                    refused = refused or halt
                    continue
                except NumberTaken:
                    break
                self._done(kept)
                if record is not kept.record:
                    self._say(
                        kept,
                        f"the order {kept.name} is in doubt, {kept.sent}: the journal refused its "
                        f"{kind_of(kept.record)}",
                    )
                return after.orders[kept.name].state
            else:
                assert refused is not None
                raise Halt(refused.reason)
        raise Halt(f"other writers took {_NUMBERS} numbers in a row")

    def _say(self, kept: _Kept, reason: str) -> None:
        kept.added += _alarm(self._alerts, reason)

    def _done(self, kept: _Kept) -> None:
        with self._lock:
            self._kept = [k for k in self._kept if k is not kept]


def _holds(order: OrderRecord, record: Record) -> bool:
    """Whether the journal already shows what an entry would say."""
    match record:
        case AckEntry():
            return order.broker_order_id == record.broker_order_id
        case RejectedEntry():
            return order.state == OrderState.REJECTED
        case AbandonedEntry():
            return order.state == OrderState.ABANDONED
        case _:
            return order.state != OrderState.SENDING


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
    # the order's name and its cutoff.
    cleared: dict[int, _Cleared] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


class _Cleared(NamedTuple):
    ack: JournalAck
    name: str
    cutoff: datetime | None


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
    cutoff: datetime | None


class _Answer(NamedTuple):
    """What a submit of `order` came to: the broker's answer, or the type of what it raised.

    `at` is when it came, or when the call went out where the clock could not be read after it.
    """

    order: OrderIntent
    at: datetime
    ack: object
    error: str | None
    halt: str | None


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
    each append and each broker call, and nothing it raises reaches an order.
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

        An order that already has an entry is not sent again, and its state is
        returned. A halt says why nothing more may go out in this decision.
        """
        self._one_account()
        self._shared.unstored.settle(self._report)
        found = self._approve(client_order_id)
        if not isinstance(found, _Approved):
            return found
        ack = self._sending(client_order_id, found)
        try:
            answer = self._submit(ack)
        except _Off as off:
            return self._not_sent(client_order_id, off.reason)
        except Exception as error:
            # Everything the submit raises, it raises before the broker's call.
            try:
                self._not_sent(client_order_id, _said(error))
            except Halt as halt:
                raise Halt(f"{_said(error)}. {halt.reason}") from None
            raise
        record, sent = self._read(answer)
        state, added = self._shared.unstored.put(
            client_order_id, record, answer.at, sent, self._report
        )
        if answer.halt is not None:
            raise Halt(f"the order {client_order_id} is in doubt: {answer.halt}{added}")
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
                state = self._abandoned(client_order_id, reason, _NOT_SENT)
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
        state, error = self._shared.controls.stop(self._report)
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
        refused: Exception
        try:
            found = call(self._broker)
        except KeyboardInterrupt:
            raise
        except Halt as halt:
            refused = Halt(halt.reason)
        except BrokerUnavailable as error:
            refused = BrokerUnavailable(name, error.error)
        except BaseException as error:
            refused = BrokerUnavailable(name, type(error).__name__)
        else:
            self._report()
            if isinstance(found, kind):
                return found
            refused = BrokerUnavailable(name, type(found).__name__)
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
        caps, caps_sha256 = self._caps()
        order = planned.order
        if Fraction(order.qty) % Fraction(caps.share_step) != 0:
            raise Halt(
                f"the order {name} is for {order.qty} shares, no multiple of the account's "
                f"share step of {caps.share_step}"
            )
        # The strategy's own cutoff is not read yet, and is never later than this one.
        until = _cutoff(planned, caps.moc_cutoff)
        try:
            _, view = self._record(view, IntentEntry(planned.seq, order), until)
        except NumberTaken:
            raise _StartOver from None
        except StoreError as error:
            raise self._ended(name, f"the intent of {name} was not stored", error) from None
        stopping = shared.controls.stopping(planned.where, self._report, until, with_stop=False)
        if stopping is not None:
            if not stopping.halts:
                return self._switched_off(name, stopping.reason)
            raise self._ended(name, stopping.reason)
        try:
            inputs = self._inputs(planned)
            limits = inputs.limits(order.strategy)
            cutoff = _cutoff(planned, tighten(limits, caps, inputs.valuation).moc_cutoff)
            self._before(cutoff)
            view = self._view(cutoff)
            state, error = shared.controls.stop(self._report, cutoff)
            self._intended(view, name)
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
        _, view = self._recorded(name, view, entry, cutoff, _NOT_SENT)
        if verdict.loss.past:
            halt = self._past(verdict.loss)
            raise self._ended(name, halt.reason) if verdict.approved else halt
        if verdict.limit is Limit.STOP:
            raise Halt(f"the gate refused {name}: {_stop_reason(state, error)}")
        return _Approved(view, cutoff) if verdict.approved else OrderState.REFUSED

    def _planned(self, view: View, name: str) -> _Planned:
        """The order as the journal's `decision` entry holds it, which halts on one in no plan."""
        _, at, place = read_order_id(name)
        seq = view.decided.get(at)
        if seq is not None:
            entry = view.decisions[seq]
            if place <= len(entry.orders) and entry.orders[place - 1].client_order_id == name:
                return _Planned(seq, entry, entry.orders[place - 1])
        raise Halt(f"the order {name} is in no decision the journal holds")

    def _intended(self, view: View, name: str) -> None:
        order = view.orders.get(name)
        if order is None or order.state is not OrderState.INTENDED:
            state = "has no entry" if order is None else f"is {order.state}"
            raise Halt(f"another writer moved the order {name}, which {state}")

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

    def _before(self, cutoff: datetime | None) -> datetime:
        """The clock's time, which halts past a market-on-close order's cutoff."""
        now = self._shared.clock.now()
        if cutoff is not None and now > cutoff:
            raise Halt(
                f"the market-on-close cutoff of {cutoff.isoformat()} has passed, and it is "
                f"{now.isoformat()}"
            )
        return now

    # Steps 7 and 8

    def _sending(self, name: str, approved: _Approved) -> JournalAck:
        ack, _ = self._recorded(
            name,
            approved.view,
            SendingEntry(name),
            approved.cutoff,
            # Only this call can have stored a `sending` the store did not answer for.
            _NOT_SENT | {OrderState.SENDING},
        )
        with self._shared.lock:
            self._shared.cleared[ack.seq] = _Cleared(ack, name, approved.cutoff)
        return ack

    def _submit(self, ack: JournalAck) -> _Answer:
        """Call the broker for the order whose `sending` entry `ack` acknowledges.

        It raises before the broker's call or not at all. An acknowledgement is
        good for one call, so one that was used, swapped or made up halts. An
        error that ends the process leaves `sending` alone, which replays as an
        order in doubt.
        """
        shared = self._shared
        with shared.lock:
            cleared = shared.cleared.pop(ack.seq, None)
            if cleared is not None and cleared.ack is not ack:
                shared.cleared[ack.seq] = cleared
                cleared = None
        if cleared is None:
            raise Halt("no order goes out on an acknowledgement this account is not holding")
        view = self._view(cleared.cutoff)
        order = self._just_sending(view, cleared)
        planned = _Planned(order.decision, view.decisions[order.decision], order.intent)
        _ = view.book
        stopping = shared.controls.stopping(
            planned.where, self._report, cleared.cutoff, with_stop=True
        )
        if stopping is not None:
            raise _stopped(stopping)
        went = self._before(cleared.cutoff)
        ack: object = None
        error = halted = None
        try:
            ack = self._broker.submit(order.intent)
        except KeyboardInterrupt:
            raise
        except Halt as halt:
            error, halted = type(halt).__name__, halt.reason
        except BaseException as raised:
            error = type(raised).__name__
        self._report()
        try:
            went = shared.clock.now()
        except Exception:
            # The answer is the one thing that cannot be had again, so no clock can lose it.
            pass
        return _Answer(order.intent, went, ack, error, halted)

    def _just_sending(self, view: View, cleared: _Cleared) -> OrderRecord:
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
            stored = self._shared.patience.tried(
                lambda: self._shared.store.get(key), self._report, cleared.cutoff
            )
        except StoreError as error:
            raise Halt(f"entry {ack.seq} could not be read: {type(error).__name__}") from None
        if sha256_of(stored) != ack.entry_hash:
            raise Halt(f"entry {ack.seq} is not stored as it was acknowledged")
        return order

    # Step 9

    def _read(self, answer: _Answer) -> tuple[Record, str]:
        """The entry a broker's answer comes to, and whether the order was sent, in words."""
        try:
            return self._entry(answer)
        except Exception as error:
            reason = f"its answer could not be read: {type(error).__name__}"
            return self._doubt(answer, reason), "perhaps sent"

    def _entry(self, answer: _Answer) -> tuple[Record, str]:
        name, ack = answer.order.client_order_id, answer.ack
        if answer.error is not None:
            return self._doubt(answer, f"the submit raised {answer.error}"), "perhaps sent"
        if not isinstance(ack, Ack):
            return self._doubt(answer, f"the submit gave a {type(ack).__name__}"), "perhaps sent"
        raw, at, broker = ack.raw, ack.received_at, ack.broker_order_id
        sent = "perhaps sent" if broker is None else f"sent, as the broker's {_shown(broker)}"
        if not isinstance(raw, bytes):
            reason = "its answer holds no bytes"
        elif not isinstance(at, datetime) or at.utcoffset() != timedelta(0):
            reason = "its answer's time is not UTC"
        elif len(raw) > RAW_LIMIT:
            reason = f"its answer is longer than {RAW_LIMIT} bytes"
        elif differs := _differs(answer.order, ack):
            reason = f"the broker's copy differs in {', '.join(differs)}"
        elif ack.outcome == Outcome.ACCEPTED and broker is not None:
            return AckEntry(name, broker, raw, False, at), sent
        elif ack.outcome == Outcome.REJECTED:
            return RejectedEntry(
                name, "the broker rejected it", raw, False, at
            ), "sent and rejected"
        elif ack.outcome == Outcome.NOT_SENT:
            return AbandonedEntry(name, "the adapter says nothing was sent"), "not sent"
        else:
            reason = f"the broker's answer is {Outcome(ack.outcome).value}"
        return self._doubt(answer, reason), sent

    def _doubt(self, answer: _Answer, reason: str) -> InDoubtEntry:
        """An order in doubt, with the bytes the broker sent where it sent any."""
        ack = answer.ack
        raw = ack.raw if isinstance(ack, Ack) and isinstance(ack.raw, bytes) else None
        at = ack.received_at if isinstance(ack, Ack) else None
        if not isinstance(at, datetime) or at.utcoffset() != timedelta(0):
            at = answer.at
        cut = raw is not None and len(raw) > RAW_LIMIT
        kept = raw[:RAW_LIMIT] if raw is not None else None
        return InDoubtEntry(answer.order.client_order_id, reason, kept, cut, at)

    def _not_sent(self, name: str, reason: str) -> OrderState:
        """Journal that an order with a `sending` entry never left, which only this call knows."""
        record, at = AbandonedEntry(name, _text(reason)), self._shared.clock.now()
        state, added = self._shared.unstored.put(name, record, at, "not sent", self._report)
        if added:
            raise Halt(f"the order {name} is {state}{added}")
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
            same = self._broker.alias == account
        except Exception as raised:
            same, error = False, type(raised).__name__
        if error is not None:
            raise Halt(f"the broker's alias could not be read: {error}")
        if not same:
            raise Halt(f"the broker trades another account than {account!r}")

    def _view(self, until: datetime | None = None) -> View:
        try:
            view = self._shared.patience.tried(self._shared.journal.view, self._report, until)
        except StoreError as error:
            raise self._outage(f"the journal could not be read: {type(error).__name__}") from None
        self._report()
        return view

    def _record(
        self, view: View, record: Record, until: datetime | None = None
    ) -> tuple[JournalAck, View]:
        """Append one entry, built once and resent unchanged."""
        at = self._shared.clock.now()
        stored = self._shared.patience.tried(
            lambda: self._shared.journal.record(view, record, at), self._report, until
        )
        self._report()
        return stored

    def _recorded(
        self,
        name: str,
        view: View,
        record: Record,
        until: datetime | None,
        left: frozenset[OrderState],
    ) -> tuple[JournalAck, View]:
        """Append the `verdict` or the `sending` of an order, or end the order and halt."""
        kind = kind_of(record)
        try:
            return self._record(view, record, until)
        except NumberTaken:
            # The book has moved, or another call holds this entry. Nothing is asked twice.
            raise self._ended(name, f"another writer took the number of {name}'s {kind}") from None
        except StoreError as error:
            raise self._ended(name, f"the {kind} of {name} was not stored", error, left) from None
        except Halt as halt:
            raise self._ended(name, halt.reason) from None

    def _switched_off(self, name: str, reason: str) -> OrderState:
        halt = self._ended(name, reason)
        if halt.reason != reason:
            raise halt
        return OrderState.ABANDONED

    def _ended(
        self,
        name: str,
        reason: str,
        error: StoreError | None = None,
        left: frozenset[OrderState] = _NOT_SENT,
    ) -> Halt:
        """Journal `abandoned` for an order that goes no further, and give the halt that says why.

        `left` holds the states this call knows nothing was sent in. An order in
        another has been moved by another writer, which may have it at the
        broker, so it alarms and is left as it is.
        """
        if error is not None:
            reason = f"{reason}: {type(error).__name__}"
        try:
            state = self._abandoned(name, reason, left)
        except _Unjournaled as failed:
            if OrderState.SENDING in left:
                # A `sending` may be stored, and only this process knows nothing followed it.
                record, at = AbandonedEntry(name, _text(reason)), self._shared.clock.now()
                self._shared.unstored.keep(name, record, at, "not sent")
            return self._outage(f"{reason}. The abandoned of {name} was not stored: {failed}")
        except Halt as halt:
            return Halt(f"{reason}. {name} was not journaled abandoned: {halt.reason}")
        if state is OrderState.ABANDONED:
            return Halt(reason)
        what = f"{reason}. The journal shows {name} {state}, which this call did not do"
        return Halt(f"{what}{_alarm(self._shared.alerts, what)}")

    def _abandoned(self, name: str, reason: str, left: frozenset[OrderState]) -> OrderState:
        """Journal `abandoned` for an order in one of `left`, and return the state it is in."""
        journal, patience = self._shared.journal, self._shared.patience
        shows = "the journal could not be read"
        for _ in range(_STARTS_OVER + 1):
            try:
                view = patience.tried(journal.view, self._report)
                order = view.orders.get(name)
                if order is None:
                    shows = f"the journal showed no entry of {name}"
                    planned = self._planned(view, name)
                    _, view = self._record(view, IntentEntry(planned.seq, planned.order))
                    order = view.orders[name]
                shows = f"the journal showed {name} {order.state}"
                if order.state not in left:
                    return order.state
                self._record(view, AbandonedEntry(name, _text(reason)))
                return OrderState.ABANDONED
            except NumberTaken:
                continue
            except StoreError as error:
                raise _Unjournaled(f"{type(error).__name__}, and {shows}") from None
        raise Halt(f"{name} lost its number {_STARTS_OVER + 1} times")

    def _outage(self, reason: str) -> Halt:
        """A store that did not answer, alarmed here, since no halt blob could be stored either."""
        return Halt(f"{reason}{_alarm(self._shared.alerts, reason)}")

    def _past(self, reading: LossReading) -> Halt:
        reason = (
            f"the day's loss of {reading.loss} is past its stop of {reading.bound}, measured from "
            f"the valuation of {reading.session.isoformat()}"
        )
        return Halt(f"{reason}{_alarm(self._shared.alerts, reason)}")


def _cutoff(planned: _Planned, minutes: int) -> datetime | None:
    """The last moment a market-on-close order may go out, and `None` for any other order."""
    if planned.order.type != OrderType.MARKET_ON_CLOSE:
        return None
    session = load_sessions().opened_by(planned.entry.decision_at)
    return to_utc(session.day, BeforeClose(minutes))


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
