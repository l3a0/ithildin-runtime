"""The simulated account's log: what it stores, and what each failed store leaves.

A test's number is the item of #144 it shows: 2 and 6 of its contract where a
test opens or loads an account, and otherwise of its acceptance. Every order goes
in through `OrderPath`.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.core import (
    BrokerState,
    BrokerUnavailable,
    CodecError,
    Conflict,
    Halt,
    KeyExists,
    OrderState,
    Outcome,
    StoreUnavailable,
    decode,
    encode,
    sha256_of,
    sim_account_key,
)
from ithildinruntime.execution.brokers.faults import (
    EditCash,
    EditOrder,
    EditPosition,
    Faults,
    FillPart,
    ForeignOrder,
    Late,
    OrderFault,
    ReadFault,
)
from ithildinruntime.execution.brokers.log import (
    SimCall,
    SimClose,
    SimEdit,
    SimEvents,
    SimState,
    SimulatedAccount,
)
from ithildinruntime.execution.brokers.log import after as applied
from ithildinruntime.execution.brokers.simulated import SimulatedBroker
from tests.support.order_world import Kill, Wired, order
from tests.support.sim_world import (
    ACCOUNT,
    CASH,
    FULL,
    GLD,
    MICRO,
    SIM_WRITER,
    TABLE,
    USO,
    SimWorld,
    after,
    close_of,
)

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures" / "simulated"
OTHER = "d" * 32


READS = {
    "positions": lambda b: b.positions(),
    "account": lambda b: b.account(),
    "fills": lambda b: b.fills(close_of(FULL)),
}


def sessions(first: date, count: int) -> list[date]:
    return [d for d in sorted(TABLE.sessions) if d >= first][:count]


def world(count: int = 4, first: date = FULL, **changes: object) -> SimWorld:
    """`count` sessions of closes from the one before `first`, and its own store for the log."""
    days = sessions(TABLE.before(first).day, count)
    closes = {
        GLD: {d: D(100) + n for n, d in enumerate(days)},
        USO: {d: D(50) - D(n) / 4 for n, d in enumerate(days)},
    }
    given: dict[str, object] = {"sim_store": Wired()}
    return SimWorld(closes, **given | changes)  # type: ignore[arg-type]


def key(number: int) -> str:
    return sim_account_key(ACCOUNT, number)


def down(op: str, number: int) -> Callable[[int, str, str], None]:
    def hook(count: int, found: str, at: str) -> None:
        if (found, at) == (op, key(number)):
            raise StoreUnavailable(at, "503 SlowDown: the store's own words")

    return hook


def quiet(made: SimWorld) -> None:
    made.sim.before = made.sim.after = lambda n, op, at: None  # type: ignore[attr-defined]


def call(made: SimWorld, number: int) -> SimCall:
    return decode(made.blobs()[key(number)], SimCall)


def again(made: SimWorld) -> SimulatedAccount:
    """A second process that reads the whole log."""
    log = SimulatedAccount.load(made.sim, ACCOUNT, OTHER)
    log.catch_up()
    return log


# What a blob holds


def test_4_a_call_that_changes_the_account_stores_one_blob_and_a_read_none() -> None:
    made = world()
    made.day(FULL, [(4, GLD), (-5, USO)])
    made.decide(close_of(after(FULL)))
    made.sim.calls.clear()  # type: ignore[attr-defined]
    for _ in range(3):
        made.broker().positions()
    # Each call asks for the next number, finds none and lists nothing.
    assert made.sim.calls == [("get", key(5))] * 3  # type: ignore[attr-defined]
    assert list(made.blobs()) == [key(n) for n in range(1, 5)]
    opened, first, second, fills = (call(made, n) for n in range(1, 5))
    assert (opened.writer, opened.at, opened.after) == (None, None, SimState(CASH, {}, (), ()))
    assert [o.id for o in first.events.placed] == ["sim-o-1"]
    assert [o.id for o in second.events.placed] == ["sim-o-2"]
    assert [o.id for o in second.after.open_orders] == ["sim-o-1", "sim-o-2"]
    assert [c.id for c in fills.events.closed] == ["sim-o-1", "sim-o-2"]
    # An order that ended is in the event that ended it and in no state.
    assert fills.after.open_orders == ()
    assert fills.after.positions == {GLD: 4, USO: -5}
    assert fills.writer == SIM_WRITER
    assert again(made).state == fills.after


def test_4_an_order_records_its_session_and_whether_its_decision_read_that_close() -> None:
    at_close, early = world(), world()
    at_close.day(FULL, [(4, GLD)])
    early.day(FULL, [(4, GLD)], early=20)
    for made, read in ((at_close, True), (early, False)):
        (order,) = call(made, 2).events.placed
        assert (order.session, order.read_close) == (FULL, read)


# Opening


def test_2_open_is_safe_to_repeat_and_nothing_else_opens_an_account() -> None:
    store = Wired()
    SimulatedBroker.open(store, ACCOUNT, CASH)
    held = dict(store.blobs)
    SimulatedBroker.open(store, ACCOUNT, CASH)
    assert store.blobs == held
    with pytest.raises(Halt, match="holds another opening"):
        SimulatedBroker.open(store, ACCOUNT, CASH + 1)
    assert store.blobs == held


@pytest.mark.parametrize("cash", [D(-1), D("NaN"), D("Infinity"), D("1E+40")])
def test_2_a_cash_no_account_opens_with_halts(cash: Decimal) -> None:
    store = Wired()
    with pytest.raises(Halt):
        SimulatedBroker.open(store, ACCOUNT, cash)
    assert store.blobs == {}


def test_2_an_opening_of_nothing_is_one() -> None:
    store = Wired()
    SimulatedBroker.open(store, ACCOUNT, D(0))
    assert list(store.blobs) == [key(1)]


def test_2_an_opening_refused_as_existing_and_then_missing_halts() -> None:
    class Gone(Wired):
        def create(self, key: str, data: bytes, sha256: str) -> str:
            raise KeyExists(key, "the key already holds a blob")

    with pytest.raises(Halt, match="was there and then missing"):
        SimulatedBroker.open(Gone(), ACCOUNT, CASH)


def test_2_an_opening_the_store_does_not_take_raises_the_stores_own_refusal() -> None:
    store = Wired()
    store.refuse(("create", "sim/"))
    with pytest.raises(StoreUnavailable):
        SimulatedBroker.open(store, ACCOUNT, CASH)
    assert store.blobs == {}


def test_6_load_reads_nothing_and_the_first_call_halts_on_an_account_never_opened() -> None:
    made = world(opened=False)
    made.at, made.clock.at = close_of(FULL), close_of(FULL)
    assert made.sim.calls == []  # type: ignore[attr-defined]
    for read in READS:
        with pytest.raises(Halt, match="never opened, or its log is lost"):
            READS[read](made.broker())
    # Nothing a call does opens it.
    assert made.blobs() == {}


@pytest.mark.parametrize(
    ("account", "writer", "reason"),
    [("a/b", SIM_WRITER, "has no key"), (ACCOUNT, "host-7", "no writer"), (ACCOUNT, "", "writer")],
)
def test_6_a_bad_alias_or_writer_halts_the_first_call_and_no_start(account, writer, reason) -> None:
    log = SimulatedAccount.load(Wired(), account, writer)
    with pytest.raises(Halt, match=reason):
        log.catch_up()


# A create that fails


@pytest.mark.parametrize("restart", [False, True], ids=["the-next-call", "a-restart"])
@pytest.mark.parametrize("landed", [False, True], ids=["refused", "landed"])
def test_4_a_fill_whose_create_failed_comes_once(landed: bool, restart: bool) -> None:
    made = world()
    made.day(FULL, [(4, GLD), (-5, USO)])
    at = close_of(after(FULL))
    made.decide(at, recover=False)
    made.sim.before = down("create", 4) if not landed else made.sim.before  # type: ignore[attr-defined]
    made.sim.after = down("create", 4) if landed else made.sim.after  # type: ignore[attr-defined]
    before = (made.log.number, made.log.state, list(made.log.fills), dict(made.log.orders))
    with pytest.raises(BrokerUnavailable) as raised:
        made.path().positions()
    assert str(raised.value) == "the broker's positions failed with StoreUnavailable"
    # The call that failed changed nothing this process remembers.
    log = made.log
    assert (log.number, log.state, list(log.fills), dict(log.orders)) == before
    quiet(made)
    made.clock.at = at + timedelta(seconds=9)
    if restart:
        made.restart(OTHER)
    made.recover()
    made.agree()
    fills = made.broker().fills(close_of(FULL)).fills
    assert [f.fill_id for f in fills] == ["sim-f-1", "sim-f-2"]
    # The blob that landed keeps the time it was made.
    assert {f.received_at for f in fills} == {at if landed else made.clock.at}
    assert made.broker().positions().positions == {GLD: 4, USO: -5}
    assert list(made.blobs()) == [key(n) for n in range(1, 5)]
    assert again(made).state == made.log.state


@pytest.mark.parametrize("restart", [False, True], ids=["the-next-call", "a-restart"])
@pytest.mark.parametrize("landed", [False, True], ids=["refused", "landed"])
def test_4_an_order_whose_create_failed_is_in_doubt_and_held_where_it_landed(
    landed: bool, restart: bool
) -> None:
    made = world()
    at = close_of(FULL) - timedelta(minutes=20)
    (intent,) = made.decide(at, made.orders(at, [(4, GLD)]))
    hook = down("create", 2)
    if landed:
        made.sim.after = hook  # type: ignore[attr-defined]
    else:
        made.sim.before = hook  # type: ignore[attr-defined]
    assert made.send(intent) is OrderState.IN_DOUBT
    assert made.log.number == 1
    quiet(made)
    made.clock.at = at + timedelta(seconds=9)
    if restart:
        made.restart(OTHER)
    held = made.broker().open_orders().orders
    assert [o.client_order_id for o in held] == ([intent.client_order_id] if landed else [])
    assert len(made.broker().orders(at).orders) == len(held)
    # The next order goes on from there.
    later = close_of(after(FULL))
    (other,) = made.decide(later, made.orders(later, [(5, USO)]))
    assert made.send(other) is OrderState.ACKNOWLEDGED
    assert again(made).state == made.log.state
    assert made.broker().positions().positions == ({GLD: 4} if landed else {})


class Slow:
    """Keeps the first create of call 2 with no answer, and lets a test land it later."""

    def __init__(self, store: Wired) -> None:
        self.store = store
        self.late: list[tuple[str, bytes]] = []
        self.lands = False

    def land(self) -> None:
        self.store.put(*self.late[0])

    def create(self, at: str, data: bytes, sha256: str) -> str:
        if at == key(2) and not self.late:
            self.late.append((at, data))
            raise StoreUnavailable(at, "no answer")
        if at == key(2) and self.lands:
            self.land()
        return self.store.create(at, data, sha256)

    def get(self, at: str) -> bytes:
        return self.store.get(at)

    def list(self, prefix: str, newest: str | None) -> list[str]:
        return self.store.list(prefix, newest)


def slow() -> tuple[SimWorld, Slow, list]:
    made = world()
    at = close_of(FULL) - timedelta(minutes=20)
    planned = made.decide(at, made.orders(at, [(4, GLD), (5, USO)]))
    store = Slow(made.sim)  # type: ignore[arg-type]
    made.log.store = store  # type: ignore[assignment]
    assert made.send(planned[0]) is OrderState.IN_DOUBT
    assert made.broker().open_orders().orders == ()
    return made, store, list(planned)


def test_4_a_create_that_lands_after_a_call_read_forward_is_in_the_next_read() -> None:
    made, store, (first, _) = slow()
    intent = made.orders(made.at, [(4, GLD)])[0]
    assert made.broker().orders(made.at).orders == ()
    store.land()
    (held,) = made.broker().orders(made.at).orders
    assert held.client_order_id == first.client_order_id == intent.client_order_id
    made.decide(close_of(after(FULL)))
    assert made.broker().positions().positions == {GLD: 4}
    assert again(made).state == made.log.state


def test_4_a_create_that_lands_after_the_close_is_taken_and_fills() -> None:
    """The account took it in time, and no call could know, so recover keeps its fill unmatched."""
    made, store, (first, _) = slow()
    made.clock.at = close_of(FULL) + timedelta(minutes=5)
    found = made.broker().find_order(first, made.at, {})
    assert (found.answer, found.whole_list) == (Outcome.NOT_FOUND, True)
    store.land()
    assert len(made.broker().find_order(first, made.at, {}).rows) == 1
    made.decide(close_of(after(FULL)))
    assert made.broker().positions().positions == {GLD: 4}


def test_4_a_create_that_lands_under_the_next_one_starts_that_call_over() -> None:
    made, store, (first, second) = slow()
    store.lands = True
    assert made.send(second) is OrderState.ACKNOWLEDGED
    held = made.broker().open_orders().orders
    assert [(o.broker_order_id, o.client_order_id) for o in held] == [
        ("sim-o-1", first.client_order_id),
        ("sim-o-2", second.client_order_id),
    ]
    assert again(made).state == made.log.state


def test_4_the_same_bytes_at_the_key_are_this_calls_own() -> None:
    made = world()

    class Twice(Wired):
        """A client that sends each create twice and gives the second answer."""

        def create(self, at_key: str, data: bytes, sha256: str) -> str:
            super().create(at_key, data, sha256)
            return super().create(at_key, data, sha256)

    twice = Twice(made.sim.blobs)  # type: ignore[attr-defined]
    made.sim_store = twice
    made.restart()
    assert made.day(FULL, [(4, GLD)]) == [OrderState.ACKNOWLEDGED]
    assert made.log.number == 2


@pytest.mark.parametrize("op", ["list", "get"])
def test_4_a_log_that_cannot_be_read_fails_its_call_and_names_no_word_of_the_stores(op) -> None:
    made = world()
    made.day(FULL, [(4, GLD)])
    made.restart(OTHER)
    made.sim.refuse((op, "sim/"))  # type: ignore[attr-defined]
    with pytest.raises(BrokerUnavailable) as raised:
        made.broker().positions()
    assert str(raised.value) == "the broker's account_log failed with StoreUnavailable"
    quiet(made)
    assert len(made.broker().open_orders().orders) == 1


def test_4_a_log_that_could_not_be_read_forward_is_read_at_the_next_call() -> None:
    made = world()
    made.day(FULL, [(4, GLD)])
    made.clock.at += MICRO
    made.sim.after = down("create", 3)  # type: ignore[attr-defined]
    with pytest.raises(BrokerUnavailable):
        made.broker().positions()
    quiet(made)
    made.sim.refuse(("get", "sim/"))  # type: ignore[attr-defined]
    for _ in range(2):
        with pytest.raises(BrokerUnavailable):
            made.broker().positions()
    quiet(made)
    assert made.broker().positions().positions == {GLD: 4}
    assert list(made.blobs()) == [key(n) for n in range(1, 4)]


@pytest.mark.parametrize("landed", [False, True], ids=["refused", "landed"])
def test_4_a_cancel_whose_create_failed_raises_and_stands_where_it_landed(landed: bool) -> None:
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    setattr(made.sim, "after" if landed else "before", down("create", 3))
    with pytest.raises(BrokerUnavailable) as raised:
        made.broker().cancel("sim-o-1")
    assert str(raised.value) == "the broker's account_log failed with StoreUnavailable"
    quiet(made)
    state = made.broker().lookup("sim-o-1").order.state
    assert state is (BrokerState.CANCELLED if landed else BrokerState.WORKING)
    made.decide(close_of(after(FULL)), recover=False)
    assert made.broker().positions().positions == ({} if landed else {GLD: 4})


@pytest.mark.parametrize("landed", [False, True], ids=["before", "after"])
def test_4_whatever_a_create_raises_the_next_call_reads_what_landed(landed: bool) -> None:
    made = world()
    at = close_of(FULL) - timedelta(minutes=20)
    (intent,) = made.decide(at, made.orders(at, [(4, GLD)]))

    def hook(count: int, op: str, found: str) -> None:
        if (op, found) == ("create", key(2)):
            raise Kill

    setattr(made.sim, "after" if landed else "before", hook)
    assert made.send(intent) is OrderState.IN_DOUBT
    quiet(made)
    assert len(made.broker().open_orders().orders) == (1 if landed else 0)


# Kills


def ran(made: SimWorld, days: list[date]) -> None:
    for n, day in enumerate(days):
        made.day(day, [(2 + n, GLD), (-1 - n, USO)])


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("number", range(2, 8))
def test_4_a_kill_before_and_after_each_blob_leaves_one_log_that_goes_on(number, side) -> None:
    """The process ends at the blob, so no store call of it goes through after that."""
    days = sessions(FULL, 3)
    made = world(6)
    dead: list[int] = []

    def hook(count: int, op: str, at: str) -> None:
        if (op, at) == ("create", key(number)) and not dead:
            dead.append(number)
            raise Kill

    def nothing(count: int, op: str, at: str) -> None:
        if len(dead) == 1:
            raise Kill

    setattr(made.sim, side, hook)
    made.store.before = nothing
    ends: list[str] = []
    for n, day in enumerate(days):
        at = close_of(day)
        planned: tuple = ()
        try:
            planned = made.decide(at, made.orders(at, [(2 + n, GLD), (-1 - n, USO)]))
            for intent in planned:
                made.send(intent)
        except (Kill, BrokerUnavailable):
            dead.append(0)
            quiet(made)
            made.restart(f"{n + 1:032x}")
            ends.extend(o.state for o in made.view().live.values())
    assert dead == [number, 0]
    made.decide(close_of(after(days[-1])))
    log = again(made)
    assert log.state == made.log.account_now
    assert log.state.open_orders == ()
    names = [o.client_order_id for o in log.orders.values()]
    assert len(set(names)) == len(names)
    assert [f.fill_id for f in log.fills] == [f"sim-f-{n}" for n in range(1, len(log.fills) + 1)]
    # An order killed at its blob is left `Sending`, and the log holds it where the blob landed.
    doubtful = {o.client_order_id for o in _doubtful(made)}
    acked = {n for n, o in made.view().orders.items() if o.broker_order_id}
    assert acked <= set(names) <= acked | doubtful
    if number not in (4, 7):
        assert OrderState.SENDING in ends and doubtful
    if side == "after" and doubtful:
        assert doubtful & set(names)
    assert sorted(f.client_order_id for f in log.fills) == sorted(names)
    total = {i: sum((f.qty for f in log.fills if f.instrument == i), D(0)) for i in (GLD, USO)}
    assert dict(log.state.positions) == {i: q for i, q in total.items() if q}
    assert log.state.cash == CASH - sum((f.qty * f.price for f in log.fills), D(0))


def _doubtful(made: SimWorld) -> list:
    unknown = (OrderState.IN_DOUBT, OrderState.SENDING)
    return [o.intent for o in made.view().live.values() if o.state in unknown]


def test_4_an_order_rests_across_two_processes_and_fills_in_the_second() -> None:
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    made.restart(OTHER)
    (held,) = made.broker().open_orders().orders
    assert (held.broker_order_id, held.qty) == ("sim-o-1", 4)
    made.decide(close_of(after(FULL)))
    made.agree()
    (fill,) = made.broker().fills(close_of(FULL)).fills
    assert (fill.price, fill.qty, fill.broker_order_id) == (D(101), 4, "sim-o-1")
    assert call(made, 3).writer == OTHER


def two() -> tuple[SimWorld, SimulatedAccount, SimulatedAccount]:
    """Two processes over one account, both of which have read it."""
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    first = made.log
    made.restart(OTHER)
    made.broker().positions()
    return made, first, made.log


def test_4_two_processes_take_turns_on_one_account() -> None:
    made, first, second = two()
    assert made.broker().cancel("sim-o-1").outcome is Outcome.ACCEPTED
    # The first one's next read holds what the second did, and so does its next cancel.
    made.log = first
    assert made.broker().open_orders().orders == ()
    assert made.broker().lookup("sim-o-1").order.state is BrokerState.CANCELLED
    at = close_of(FULL) - timedelta(minutes=10)
    (intent,) = made.decide(at, made.orders(at, [(5, USO)]))
    assert made.send(intent) is OrderState.ACKNOWLEDGED
    made.log = second
    assert made.broker().cancel("sim-o-2").outcome is Outcome.ACCEPTED
    assert [call(made, n).writer for n in (2, 3, 4, 5)] == [SIM_WRITER, OTHER, SIM_WRITER, OTHER]
    assert again(made).state == made.log.state


def test_4_a_read_holds_every_call_another_process_stored_since() -> None:
    made, first, second = two()
    made.faults = Faults(events={n: EditCash(D(1)) for n in range(1, 4)})
    for _ in range(3):
        made.broker().account()
        second.fired.clear()
    made.faults = Faults()
    assert second.number > 4
    made.log = first
    assert made.broker().account().cash == second.account_now.cash > CASH
    assert first.number == second.number


def test_4_a_call_whose_number_another_process_takes_starts_over_on_what_it_did() -> None:
    made, first, second = two()
    made.decide(close_of(after(FULL)), recover=False)
    taken: list[int] = []

    def hook(count: int, op: str, at: str) -> None:
        if (op, at) == ("create", key(3)) and not taken:
            taken.append(1)
            made.log = first
            assert made.broker().positions().positions == {GLD: 4}

    made.sim.before = hook  # type: ignore[attr-defined]
    # Both settle the one fill, and it is stored once.
    assert made.broker().positions().positions == {GLD: 4}
    assert [len(log.fills) for log in (first, second)] == [1, 1]
    assert list(made.blobs()) == [key(n) for n in range(1, 4)]
    assert call(made, 3).writer == SIM_WRITER


@pytest.mark.parametrize(("times", "goes"), [(100, True), (101, False)])
def test_4_a_cancel_whose_number_another_process_keeps_taking(times: int, goes: bool) -> None:
    made, first, second = two()
    made.faults = Faults(events={n: EditCash(D(1)) for n in range(1, first.calls + 120)})
    made.log = first
    other = made.broker()
    made.faults, made.log = Faults(), second
    lost: list[str] = []

    def hook(count: int, op: str, at: str) -> None:
        if op == "create" and len(lost) < times:
            lost.append(at)
            made.sim.quiet = True  # type: ignore[attr-defined]
            first.fired.update(range(first.calls + 1))
            other.account()
            made.sim.quiet = False  # type: ignore[attr-defined]

    made.sim.before = hook  # type: ignore[attr-defined]
    if goes:
        assert made.broker().cancel("sim-o-1").outcome is Outcome.ACCEPTED
    else:
        with pytest.raises(BrokerUnavailable, match="contended_log failed with KeyExists"):
            made.broker().cancel("sim-o-1")
    quiet(made)
    state = made.broker().lookup("sim-o-1").order.state
    assert state is (BrokerState.CANCELLED if goes else BrokerState.WORKING)
    assert made.broker().account().cash == CASH + times


@pytest.mark.parametrize("what", ["cancel", "read", "send"])
@pytest.mark.parametrize(("times", "goes"), [(100, True), (101, False)])
def test_4_a_call_that_loses_its_number_too_often_has_no_answer(times, goes, what) -> None:
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    at = close_of(FULL) - timedelta(minutes=10)
    (intent,) = made.decide(at, made.orders(at, [(5, USO)]))
    made.faults = Faults(events={made.log.calls + 1: EditCash(D(7))} if what == "read" else {})
    lost: list[str] = []

    def hook(count: int, op: str, found: str) -> None:
        if op == "create" and len(lost) < times:
            lost.append(found)
            raise Conflict(found, "409 ConditionalRequestConflict")

    made.sim.before = hook  # type: ignore[attr-defined]
    number, state = made.log.number, made.log.state
    if what == "read" and not goes:
        # As the order path gives it, with the call's name and the store's error.
        with pytest.raises(BrokerUnavailable) as raised:
            made.path().account()
        assert str(raised.value) == "the broker's account failed with Conflict"
        lost.clear()
    if what == "send":
        found = made.send(intent)
        assert found is (OrderState.ACKNOWLEDGED if goes else OrderState.IN_DOUBT)
    elif goes:
        assert made.broker().account().cash == CASH + 7 if what == "read" else True
        if what == "cancel":
            assert made.broker().cancel("sim-o-1").outcome is Outcome.ACCEPTED
    else:
        name = "account" if what == "read" else "cancel"
        with pytest.raises(BrokerUnavailable, match="contended_log failed with Conflict"):
            made.broker().account() if name == "account" else made.broker().cancel("sim-o-1")
    quiet(made)
    if goes:
        assert made.log.number == number + 1
    else:
        # Nothing changed, in this process or in the log.
        assert (made.log.number, made.log.state) == (number, state)
        assert len(made.blobs()) == number


def test_4_a_create_that_raced_and_did_not_land_starts_over() -> None:
    """S3's 409, which says that a conditional write did not land."""
    made = world()
    at = close_of(FULL) - timedelta(minutes=20)
    (intent,) = made.decide(at, made.orders(at, [(4, GLD)]))
    raced: list[str] = []

    def hook(count: int, op: str, found: str) -> None:
        if (op, found) == ("create", key(2)) and not raced:
            raced.append(found)
            raise Conflict(found, "409 ConditionalRequestConflict")

    made.sim.before = hook  # type: ignore[attr-defined]
    assert made.send(intent) is OrderState.ACKNOWLEDGED
    assert list(made.blobs()) == [key(1), key(2)]


def test_4_a_call_a_listing_misses_is_asked_for_before_its_gap_halts() -> None:
    made = full()

    class Racing(Wired):
        def list(self, prefix: str, newest: str | None) -> list[str]:
            return [k for k in super().list(prefix, newest) if k != key(3)]

    made.sim_store = Racing(made.sim.blobs)  # type: ignore[attr-defined]
    assert again(made).state == made.log.state


def test_4_the_clock_is_read_after_the_log() -> None:
    """Another process can store a call while this one reads, at a later time than its clock."""
    made, first, second = two()
    made.decide(close_of(after(FULL)), recover=False)
    moved: list[int] = []

    def hook(count: int, op: str, at: str) -> None:
        if (op, at) == ("get", key(3)) and not moved:
            moved.append(1)
            made.clock.at += timedelta(seconds=1)
            made.log = first
            made.broker().positions()

    made.sim.before = hook  # type: ignore[attr-defined]
    made.log = second
    assert made.broker().positions().positions == {GLD: 4}
    assert moved == [1]


def test_4_two_processes_never_hold_one_order_as_their_own() -> None:
    """Both send one id at one time, and only the one whose blob landed owns the order.

    Their two blobs differ in the writer alone, so the one that lost its number
    starts over and finds the id held.
    """
    made = world()
    other = SimWorld(made.closes, sim_store=made.sim, sim_writer="e" * 32)
    at = close_of(FULL) - timedelta(minutes=20)
    (ours,) = made.decide(at, made.orders(at, [(4, GLD)]))
    (theirs,) = other.decide(at, other.orders(at, [(4, GLD)]))
    assert ours == theirs
    states: list[OrderState] = []

    def hook(count: int, op: str, found: str) -> None:
        if (op, found) == ("create", key(2)) and not states:
            states.append(OrderState.SENDING)
            states.append(other.send(theirs))

    made.sim.before = hook  # type: ignore[attr-defined]
    assert made.send(ours) is OrderState.IN_DOUBT
    assert states[1] is OrderState.ACKNOWLEDGED
    (held,) = made.broker().orders(at).orders
    assert held.client_order_id == ours.client_order_id
    assert call(made, 2).writer == "e" * 32


# A log that cannot be read back


def full() -> SimWorld:
    made = world()
    made.day(FULL, [(4, GLD), (-5, USO)])
    made.decide(close_of(after(FULL)))
    return made


def halts(made: SimWorld, reason: str) -> None:
    """A fresh process halts at each call, a send included, and stores nothing in the log."""
    made.restart(OTHER)
    held = made.blobs()
    for read in READS:
        with pytest.raises(Halt) as raised:
            READS[read](made.broker())
        assert reason in raised.value.reason
        assert ACCOUNT in raised.value.reason
    with pytest.raises(Halt) as raised:
        made.broker().cancel("sim-o-1")
    assert reason in raised.value.reason
    at = close_of(after(FULL, 2))
    (intent,) = made.decide(at, made.orders(at, [(1, GLD)]), recover=False)
    with pytest.raises(Halt) as raised:
        made.send(intent)
    doubt = f"the order {intent.client_order_id} is in doubt: "
    assert raised.value.reason.startswith(doubt) and reason in raised.value.reason
    assert made.blobs() == held


@pytest.mark.parametrize("number", [1, 2, 3])
def test_4_a_lost_call_before_the_newest_halts(number: int) -> None:
    made = full()
    made.sim.drop(key(number))  # type: ignore[attr-defined]
    halts(
        made,
        f"the simulated account sim-1's call {number} is missing, and the next key is "
        f"{key(number + 1)}",
    )


def test_4_a_lost_newest_call_reads_as_the_account_before_it() -> None:
    """The log cannot see it. Recover's comparison does, in tests/test_recover.py."""
    made = full()
    made.sim.drop(key(4))  # type: ignore[attr-defined]
    made.restart(OTHER)
    broker = made.broker()
    # The fills come again, under their ids and from what the snapshot holds.
    assert [f.fill_id for f in broker.fills(close_of(FULL)).fills] == ["sim-f-1", "sim-f-2"]
    made.agree()
    made = full()
    for number in (4, 3):
        made.sim.drop(key(number))  # type: ignore[attr-defined]
    made.restart(OTHER)
    assert made.broker().positions().positions == {GLD: 4}
    with pytest.raises(AssertionError):
        made.agree()


def test_4_a_call_lost_under_a_running_process_halts_the_next_process() -> None:
    """A running process asks for the next number alone, so only a first read sees the gap."""
    made, first, second = two()
    made.faults = Faults(events={n: EditCash(D(1)) for n in range(1, 3)})
    for _ in range(2):
        second.fired.clear()
        made.broker().account()
    made.faults = Faults()
    made.sim.drop(key(3))  # type: ignore[attr-defined]
    made.log = first
    assert made.broker().account().cash == CASH
    halts(made, "call 3 is missing, and the next key is")


def test_4_a_process_that_stores_a_call_in_a_gap_halts_its_own_next_call() -> None:
    made, first, second = two()
    made.faults = Faults(events={n: EditCash(D(1)) for n in range(1, 3)})
    for _ in range(2):
        second.fired.clear()
        made.broker().account()
    made.faults = Faults()
    made.sim.drop(key(3))  # type: ignore[attr-defined]
    made.log = first
    assert made.broker().cancel("sim-o-1").outcome is Outcome.ACCEPTED
    with pytest.raises(Halt, match="call 4 is not the call before it with its events applied"):
        made.broker().open_orders()


def test_4_a_newest_call_lost_under_the_process_that_made_it_leaves_a_gap() -> None:
    made = full()
    made.sim.drop(key(4))  # type: ignore[attr-defined]
    made.faults = Faults(events={made.log.calls + 1: EditCash(D(1))})
    assert made.broker().account().cash == made.log.account_now.cash
    assert key(5) in made.blobs() and key(4) not in made.blobs()
    halts(made, "call 4 is missing")


def test_4_a_deleted_log_halts_and_never_reads_as_a_new_account() -> None:
    made = full()
    for at in list(made.blobs()):
        made.sim.drop(at)  # type: ignore[attr-defined]
    halts(
        made,
        "the simulated account sim-1 has no call: it was never opened, or its log is lost",
    )
    assert made.blobs() == {}


def test_4_a_newest_call_that_is_no_call_halts() -> None:
    made = full()
    made.sim.put(key(5), b"{}")  # type: ignore[attr-defined]
    halts(made, "call 5 is not a canonical call")


def test_4_a_key_that_is_no_calls_halts() -> None:
    made = full()
    made.sim.put(key(4).replace(".json", ".txt"), b"{}")  # type: ignore[attr-defined]
    halts(made, "call 5 is missing, and the next key is")


BAD: dict[str, tuple[Callable[[SimCall], object], str]] = {
    "not-canonical": (lambda c: b" " + encode(c), "not a canonical call"),
    "not-json": (lambda c: b"\xff", "not a canonical call"),
    "another-account": (lambda c: replace(c, account="sim-2"), "holds call 3 of sim-2"),
    "another-call": (lambda c: replace(c, call=2), "holds call 2 of sim-1"),
    "another-format": (lambda c: replace(c, format=2), "has format 2"),
    "a-later-format": (
        lambda c: encode(c).replace(b'"format":1', b'"format":2,"later":null'),
        "has format 2, which this release does not read",
    ),
    "no-writer": (lambda c: replace(c, writer=None), "names the writer None"),
    "no-time": (lambda c: replace(c, at=None), "and the time None"),
    "other-cash": (
        lambda c: replace(c, after=replace(c.after, cash=c.after.cash + 1)),
        "is not the call before it with its events applied",
    ),
    "an-earlier-time": (lambda c: replace(c, at=c.at - MICRO), "before the call before it"),
    "another-id": (
        lambda c: replace(
            c,
            events=replace(c.events, placed=(replace(c.events.placed[0], id="sim-o-9"),)),
            after=replace(
                c.after,
                open_orders=(c.after.open_orders[0], replace(c.after.open_orders[1], id="sim-o-9")),
            ),
        ),
        "places sim-o-9, not sim-o-2",
    ),
}


def _at(c: SimCall, **events: object) -> SimCall:
    changed = replace(c.events, **events)  # type: ignore[arg-type]
    return replace(c, events=changed)


def _state(c: SimCall, **after_: object) -> SimCall:
    return replace(c, after=replace(c.after, **after_))  # type: ignore[arg-type]


def _order(c: SimCall, **changes: object) -> SimCall:
    made = replace(c.events.placed[0], **changes)  # type: ignore[arg-type]
    held = tuple(made if o.id == c.events.placed[0].id else o for o in c.after.open_orders)
    return _state(_at(c, placed=(made,)), open_orders=held)


BAD["a-repeated-id"] = (
    lambda c: _order(c, client_order_id=c.after.open_orders[0].client_order_id),
    "a second time",
)
BAD["a-held-order"] = (
    lambda c: _state(
        _at(c, placed=(replace(c.events.placed[0], id="sim-o-1"),)),
        open_orders=(replace(c.events.placed[0], id="sim-o-1"),),
    ),
    "places sim-o-1, which it holds already",
)
BAD["a-repeated-id-on-its-way"] = (
    lambda c: _state(
        _at(c, placed=(), delayed=(replace(c.after.open_orders[0], id=None),)),
        open_orders=c.after.open_orders[:1],
        delayed=(replace(c.after.open_orders[0], id=None),),
    ),
    "a second time",
)


def _expired(end: SimClose, qty: Decimal) -> SimClose:
    return replace(end, state=BrokerState.EXPIRED, fill=replace(end.fill, qty=qty))


# A fill that is not of its order, by what it changes of call 4's first end.
FILLS: dict[str, Callable[[SimClose], SimClose]] = {
    "no-snapshot": lambda e: replace(e, priced_from=None),
    "no-fill": lambda e: replace(e, fill=None),
    "filled-with-no-fill": lambda e: replace(e, fill=None, priced_from=None),
    **{
        f"{state}-with-no-fill": lambda e, state=state: replace(
            e, state=state, fill=None, priced_from=None
        )
        for state in (BrokerState.WORKING, BrokerState.PENDING_CANCEL, BrokerState.UNKNOWN)
    },
    **{
        f"{state}-in-part": lambda e, state=state: replace(_expired(e, D(3)), state=state)
        for state in BrokerState
        if state is not BrokerState.EXPIRED
    },
    "another-order": lambda e: replace(e, fill=replace(e.fill, broker_order_id="sim-o-2")),
    "another-name": lambda e: replace(e, fill=replace(e.fill, client_order_id="x")),
    "another-instrument": lambda e: replace(e, fill=replace(e.fill, instrument=USO)),
    "another-side": lambda e: replace(e, fill=replace(e.fill, qty=D(-4))),
    "too-many": lambda e: replace(e, fill=replace(e.fill, qty=D(400))),
    "none": lambda e: replace(e, fill=replace(e.fill, qty=D(0))),
    "filled-in-part": lambda e: replace(e, fill=replace(e.fill, qty=D(3))),
    "expired-on-another-side": lambda e: _expired(e, D(-1)),
    "expired-with-too-many": lambda e: _expired(e, D(5)),
    "expired-with-none": lambda e: _expired(e, D(0)),
    "expired-in-part-with-no-snapshot": lambda e: replace(_expired(e, D(3)), priced_from=None),
    "expired-with-a-snapshot-and-no-fill": lambda e: replace(_expired(e, D(3)), fill=None),
    **{
        f"{state}-whole": lambda e, state=state: replace(e, state=state)
        for state in BrokerState
        if state is not BrokerState.FILLED
    },
}


@pytest.mark.parametrize("bad", FILLS)
def test_4_an_end_that_does_not_fit_its_order_halts(bad: str) -> None:
    made = full()
    found, before = call(made, 4), call(made, 3).after
    wrong = FILLS[bad](found.events.closed[0])
    events = replace(found.events, closed=(wrong, found.events.closed[1]))
    after_ = applied(before, events)
    made.sim.put(key(4), encode(replace(found, events=events, after=after_)))  # type: ignore[attr-defined]
    halts(made, "call 4 ends sim-o-1 in a way it cannot end")


def test_4_a_sale_that_ends_with_a_fill_of_nothing_halts() -> None:
    made = full()
    found, before = call(made, 4), call(made, 3).after
    wrong = _expired(found.events.closed[1], D(0))
    events = replace(found.events, closed=(found.events.closed[0], wrong))
    after_ = applied(before, events)
    made.sim.put(key(4), encode(replace(found, events=events, after=after_)))  # type: ignore[attr-defined]
    halts(made, "call 4 ends sim-o-2 in a way it cannot end")


@pytest.mark.parametrize("bad", BAD)
def test_4_a_call_that_is_not_one_halts(bad: str) -> None:
    made = full()
    change, reason = BAD[bad]
    found = change(call(made, 3))
    made.sim.put(key(3), found if isinstance(found, bytes) else encode(found))  # type: ignore[attr-defined]
    halts(made, reason)


def test_4_a_swapped_call_halts() -> None:
    made = full()
    blobs = made.blobs()
    made.sim.put(key(2), blobs[key(3)])  # type: ignore[attr-defined]
    made.sim.put(key(3), blobs[key(2)])  # type: ignore[attr-defined]
    halts(made, "call 2 holds call 3 of sim-1")


@pytest.mark.parametrize(
    "read", ["positions", "open_orders", "orders", "lookup", "find_order", "cancel"]
)
def test_4_a_bad_call_stored_under_a_running_process_halts_its_next_call(read: str) -> None:
    made = full()
    made.sim.put(key(5), b"{}")  # type: ignore[attr-defined]
    for _ in range(2):
        with pytest.raises(Halt, match="sim-1's call 5 is not a canonical call"):
            CALLS[read](made.broker())


def planted(made: SimWorld, number: int, bad: SimCall) -> None:
    """Plant a call whose account follows from its events, so only a check of them can halt."""
    before = call(made, number - 1).after
    made.sim.put(key(number), encode(replace(bad, after=applied(before, bad.events))))  # type: ignore[attr-defined]


def test_4_one_id_placed_twice_in_one_call_halts() -> None:
    made = full()
    found = call(made, 3)
    (first,) = found.events.placed
    for events, reason in (
        (
            replace(found.events, placed=(first, replace(first, id="sim-o-3"))),
            f"places {first.client_order_id} a second time",
        ),
        (
            replace(found.events, delayed=(replace(first, id=None),)),
            f"places {first.client_order_id} a second time",
        ),
        (
            replace(found.events, placed=(first, replace(first, client_order_id="x"))),
            "places sim-o-2, which it holds already",
        ),
    ):
        planted(made, 3, replace(found, events=events))
        made.restart(OTHER)
        with pytest.raises(Halt, match=reason):
            made.broker().positions()


def test_4_an_edit_of_an_order_that_ended_halts() -> None:
    made = full()
    ended_ = made.log.orders["sim-o-1"]
    again_ = replace(ended_, state=BrokerState.WORKING, filled=D(0))
    found = call(made, 4)
    edit = SimEdit(9, None, None, None, again_)
    planted(made, 5, replace(found, call=5, events=SimEvents(edits=(edit,))))
    made.restart(OTHER)
    with pytest.raises(Halt, match="call 5 edits sim-o-1, which is not open"):
        made.broker().positions()


def test_4_an_order_on_its_way_twice_under_one_id_halts() -> None:
    made = world()
    at = close_of(FULL) - timedelta(minutes=20)
    planned = made.decide(at, made.orders(at, [(4, GLD)]))
    made.faults = Faults({planned[0].client_order_id: Late(9)})
    made.send(planned[0])
    found = call(made, 2)
    twice = replace(
        found,
        call=3,
        after=replace(found.after, delayed=(*found.after.delayed, *found.after.delayed)),
    )
    made.sim.put(key(3), encode(twice))  # type: ignore[attr-defined]
    made.restart(OTHER)
    with pytest.raises(Halt, match="call 3 places ith-chan-.* a second time"):
        made.broker().positions()


def test_4_a_call_that_could_not_be_read_back_is_never_stored() -> None:
    """A fault's order and an edit of it in one call are one new order, not two."""
    reads = {("positions", 1): ReadFault.FAILS}
    events = {2: ForeignOrder(USO, D(2)), 3: EditOrder("sim-o-2", D(9))}
    made = world(faults=Faults(reads=reads, events=events))
    made.day(FULL, [(4, GLD)], early=20, recover=False)
    with pytest.raises(BrokerUnavailable):
        made.broker().positions()
    found = made.broker().open_orders().orders
    assert [(o.broker_order_id, o.qty) for o in found] == [("sim-o-1", 4), ("sim-o-2", 9)]
    assert again(made).state == made.log.state


def test_4_a_call_that_does_not_follow_halts_before_it_is_stored(monkeypatch) -> None:
    """It would otherwise halt the account for good, since no blob here can be deleted."""
    monkeypatch.setattr(SimulatedBroker, "_next_id", lambda self, events: "sim-o-7")
    made = world()
    with pytest.raises(Halt, match="sim-1's call 2 places sim-o-7, not sim-o-1"):
        made.day(FULL, [(4, GLD)])
    assert list(made.blobs()) == [key(1)]


def test_4_a_fill_under_another_id_and_an_end_of_an_order_not_open_halt() -> None:
    made = full()
    found = call(made, 4)
    first, second = found.events.closed
    wrong = replace(first, fill=replace(first.fill, fill_id="sim-f-7"))
    made.sim.put(  # type: ignore[attr-defined]
        key(4), encode(replace(found, events=replace(found.events, closed=(wrong, second))))
    )
    halts(made, "holds the fill sim-f-7, not sim-f-1")
    made = full()
    twice = replace(found, events=replace(found.events, closed=(first, first, second)))
    made.sim.put(key(4), encode(twice))  # type: ignore[attr-defined]
    halts(made, "call 4: the simulated order sim-o-1 ended and was not open")


@pytest.mark.parametrize("change", ["positions", "open_orders", "events"])
def test_4_an_opening_that_is_not_flat_halts(change: str) -> None:
    made = full()
    opened, third = call(made, 1), call(made, 3)
    if change == "events":
        bad = replace(opened, events=third.events)
    elif change == "positions":
        bad = replace(opened, after=replace(opened.after, positions={GLD: D(1)}))
    else:
        bad = replace(opened, after=replace(opened.after, open_orders=third.after.open_orders))
    made.sim.put(key(1), encode(bad))  # type: ignore[attr-defined]
    halts(made, "opens an account that is not flat")


def test_4_an_account_a_record_cannot_hold_halts_with_nothing_stored() -> None:
    made = world(faults=Faults(events={2: EditCash(D("1E-40"))}))
    made.day(FULL, [(4, GLD)], recover=False)
    with pytest.raises(Halt, match="call 3 cannot be written"):
        made.broker().account()
    assert len(made.blobs()) == 2
    with pytest.raises(CodecError):
        encode(replace(call(made, 2), after=replace(call(made, 2).after, cash=D("1E-40"))))


# Threads


CALLS: dict[str, Callable[[SimulatedBroker], object]] = {
    **READS,
    "open_orders": lambda b: b.open_orders(),
    "orders": lambda b: b.orders(close_of(FULL)),
    "activities": lambda b: b.activities(close_of(FULL)),
    "lookup": lambda b: b.lookup("sim-o-1"),
    "find_order": lambda b: b.find_order(order(1, 4, at=close_of(FULL)), close_of(FULL), {}),
    "cancel": lambda b: b.cancel("sim-o-1"),
}


class Watched:
    """The log's lock, which says when a thread has asked for it while another holds it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.asked = threading.Event()

    def __enter__(self) -> None:
        if not self._lock.acquire(blocking=False):
            self.asked.set()
            self._lock.acquire()

    def __exit__(self, *given: object) -> None:
        self._lock.release()


def either(*events: threading.Event) -> None:
    """Wait until one of the events is set."""
    for _ in range(5000):
        if any(event.is_set() for event in events):
            return
        time.sleep(0.001)
    raise AssertionError("neither came")


@pytest.mark.parametrize("read", CALLS)
def test_4_two_threads_never_build_on_one_number(read: str) -> None:
    made = world()
    made.day(FULL, [(4, GLD)])
    made.clock.at += MICRO
    made.log.lock = lock = Watched()  # type: ignore[assignment]
    entered, done = threading.Event(), threading.Event()
    early: list[bool] = []

    def other() -> None:
        CALLS[read](made.broker())
        done.set()

    def hook(count: int, op: str, at: str) -> None:
        if op == "create" and not entered.is_set():
            entered.set()
            threading.Thread(target=other, daemon=True).start()
            either(lock.asked, done)
            early.append(done.is_set())

    made.sim.before = hook  # type: ignore[attr-defined]
    assert made.broker().positions().positions == {GLD: 4}
    assert done.wait(5)
    # The second call waits for the first's create, and stores nothing of its own.
    assert early == [False]
    assert made.log.number == len(made.blobs()) == 3


def test_4_a_cancel_holds_the_log_while_it_stores() -> None:
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    made.log.lock = lock = Watched()  # type: ignore[assignment]
    done, early = threading.Event(), []

    def other() -> None:
        made.broker().open_orders()
        done.set()

    def hook(count: int, op: str, at: str) -> None:
        if op == "create":
            threading.Thread(target=other, daemon=True).start()
            either(lock.asked, done)
            early.append(done.is_set())

    made.sim.before = hook  # type: ignore[attr-defined]
    assert made.broker().cancel("sim-o-1").outcome is Outcome.ACCEPTED
    assert done.wait(5)
    assert early == [False]


@pytest.mark.parametrize("what", ["read", "send"])
def test_4_a_price_that_is_slow_holds_no_cancel(what: str) -> None:
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    at = close_of(FULL) - timedelta(minutes=10)
    (intent,) = made.decide(at, made.orders(at, [(5, USO)]), recover=False)
    broker, path = made.broker(), None
    waits, goes, done = threading.Event(), threading.Event(), threading.Event()
    if what == "read":
        # A read waits on the close of an order past it.
        made.clock.at = at = close_of(after(FULL)) - timedelta(minutes=20)
        made.decide(at, (), recover=False)
        broker = made.broker()
    stored = type(broker._prices).close

    class Slow:
        decision_at = broker._prices.decision_at
        snapshot = broker._prices.snapshot

        def close(self, instrument, session):
            waits.set()
            assert goes.wait(5)
            return stored(made.broker()._prices, instrument, session)

    broker._prices = Slow()  # type: ignore[assignment]
    path = made.account.path(broker, lambda: None)

    def work() -> None:
        path.positions() if what == "read" else path.send(intent.client_order_id)
        done.set()

    threading.Thread(target=work, daemon=True).start()
    assert waits.wait(5)
    if what == "send":
        assert made.broker().cancel("sim-o-1").outcome is Outcome.ACCEPTED
    else:
        assert made.broker().cancel("sim-o-1").outcome is Outcome.REJECTED
    assert not done.is_set()
    goes.set()
    assert done.wait(5)
    held = {} if what == "send" else {GLD: 4}
    assert made.broker().positions().positions == held


def test_4_two_orders_sent_at_once_take_two_numbers() -> None:
    made = world()
    at = close_of(FULL)
    made.decide(at, ())
    made.faults = Faults()
    one = SimWorld(made.closes, sim_store=made.sim)
    one.log = made.log
    first, second = one.decide(at, one.orders(at, [(4, GLD), (5, USO)]), recover=False)
    one.log.lock = lock = Watched()  # type: ignore[assignment]
    entered, done = threading.Event(), threading.Event()
    early: list[bool] = []

    def other() -> None:
        # A second broker over the same log, so nothing but the log's lock keeps them apart.
        # It calls the broker itself, since the account's lease keeps two order paths of one
        # account from sending at once.
        two = SimWorld(made.closes, sim_store=made.sim, opened=False)
        two.store.blobs, two.store._keys = one.store.blobs, one.store._keys
        two.store.quiet = True
        two.log, two.run, two.at, two.clock.at = one.log, one.run, one.at, at
        two.broker().submit(second)
        done.set()

    def hook(count: int, op: str, found: str) -> None:
        if op == "create" and not entered.is_set():
            entered.set()
            threading.Thread(target=other, daemon=True).start()
            either(lock.asked, done)
            early.append(done.is_set())

    made.sim.before = hook  # type: ignore[attr-defined]
    one.send(first)
    assert done.wait(5)
    assert early == [False]
    assert [o.id for o in made.log.account_now.open_orders] == ["sim-o-1", "sim-o-2"]


# 5. Determinism


def long_run(count: int, faults: str = "") -> SimWorld:
    days, priced = sessions(date(2026, 3, 3), count + 1), sessions(date(2026, 3, 2), count + 2)
    closes = {
        GLD: {d: D(100) + D(n % 7) / 4 for n, d in enumerate(priced)},
        USO: {d: D(50) - D(n % 5) / 8 for n, d in enumerate(priced)},
    }
    made = SimWorld(closes, sim_store=Wired())
    if faults:

        def name(n: int, place: int = 1) -> str:
            return order(place, 1, at=close_of(days[n])).client_order_id

        # An order in doubt ends its leg, so those come last.
        last = {
            "late": {name(45): Late(1), name(45, 2): OrderFault.LOSE_ANSWER},
            "lost": {name(45): OrderFault.DUPLICATE, name(45, 2): OrderFault.LOSE_REQUEST},
        }[faults]
        made.faults = Faults(
            orders={
                name(3): OrderFault.REJECT,
                name(5): FillPart(D(1)),
                name(7): OrderFault.FILL_NONE,
                name(9): OrderFault.REJECT_LATER,
                name(11): OrderFault.BAD_TYPE,
                name(13): OrderFault.BAD_QTY,
                **last,
            },
            events={40: EditPosition(GLD, D(1)), 60: EditCash(D(-3)), 82: ForeignOrder(USO, D(2))},
        )
    for n, day in enumerate(days[:count]):
        made.day(day, [(1 + n % 4, GLD), (-1 - n % 3, USO)] if n % 2 else [(-2, GLD), (1, USO)])
    made.decide(close_of(days[count]))
    return made


@pytest.mark.parametrize("faults", ["", "late", "lost"])
def test_5_fifty_decisions_twice_give_the_same_bytes(faults: str) -> None:
    one, two = long_run(50, faults), long_run(50, faults)
    assert one.blobs() == two.blobs()
    # Each fault was reached, so the bytes are of a run that met it.
    states = Counter(o.state for o in one.view().orders.values())
    calls = [call(one, n).events for n in range(1, one.log.number + 1)]
    ended = {close.state for events in calls for close in events.closed}
    if not faults:
        assert states == {OrderState.FILLED: 100}
        assert ended == {BrokerState.FILLED}
        return
    assert (states[OrderState.REJECTED], states[OrderState.EXPIRED]) == (4, 2)
    assert states[OrderState.IN_DOUBT] == 2
    assert ended == {BrokerState.FILLED, BrokerState.EXPIRED, BrokerState.REJECTED}
    assert sum(len(events.edits) for events in calls) == 3
    assert sum(len(events.delayed) for events in calls) == (1 if faults == "late" else 0)


def committed() -> SimWorld:
    """A run that writes each kind of event, whose log tests/fixtures/simulated holds."""
    made = world(6)
    at = close_of(FULL) - timedelta(minutes=20)
    names = [o.client_order_id for o in made.orders(at, [(1, GLD)] * 5)]
    made.faults = Faults(
        orders={
            names[0]: FillPart(D("1.5")),
            names[1]: Late(1),
            names[2]: OrderFault.REJECT_LATER,
            # It arrives after its close, so it is missed.
            names[3]: Late(9),
        },
        events={
            7: EditPosition(GLD, D(1)),
            8: EditCash(D("-2.5")),
            9: EditOrder("sim-o-1", D(6)),
            10: ForeignOrder(USO, D(2)),
        },
    )
    legs = [(4, GLD), (-5, USO), (3, GLD), (7, GLD)]
    planned = made.decide(at, made.orders(at, legs), recover=False)
    for intent in planned:
        made.send(intent)
    for _ in range(6):
        made.broker().positions()
    # A cancel under a clock that went back, so the blob's time and the clock's differ.
    made.clock.at -= timedelta(seconds=30)
    made.broker().cancel("sim-o-2")
    made.decide(close_of(after(FULL)), recover=False)
    for _ in range(2):
        made.broker().positions()
    return made


# The digest, when the log was committed, of the snapshot that priced its fills.
PRICED_FROM = "9910432dd2bbd1174e8982d18d1063b88c8911a86b6d4fb307c7d798928fbbd7"


def test_5_this_release_reads_and_writes_the_committed_log() -> None:
    """A shadow account's log outlives a release, so its bytes never change.

    The digest of the snapshot that priced a fill moves with `Snapshot`'s fields, so it
    is compared with that snapshot's own bytes, and left out of the comparison.
    """
    made = committed()
    stored = {p.name: p.read_bytes() for p in sorted(FIXTURES.glob("*.json"))}
    written = {k.rpartition("/")[2]: v for k, v in made.blobs().items()}
    ends = [end for data in written.values() for end in decode(data, SimCall).events.closed]
    # One snapshot priced both fills.
    (ref,) = {end.priced_from for end in ends if end.priced_from is not None}
    assert ref.sha256 == sha256_of(made.store.blobs[ref.key])
    now, then = ref.sha256.encode(), PRICED_FROM.encode()
    assert {name: data.replace(now, then) for name, data in written.items()} == stored
    store = Wired({f"sim/{ACCOUNT}/{name}": data for name, data in stored.items()})
    log = SimulatedAccount.load(store, ACCOUNT, OTHER)
    log.catch_up()
    assert log.state == made.log.state
    states = {o.state for o in log.orders.values()}
    assert states == {
        BrokerState.EXPIRED,
        BrokerState.FILLED,
        BrokerState.CANCELLED,
        BrokerState.REJECTED,
    }
    calls = [decode(data, SimCall) for data in stored.values()]
    assert any(c.clock != c.at for c in calls)
    for kind in ("edits", "placed", "missed", "delayed", "closed"):
        assert any(getattr(c.events, kind) for c in calls), kind
    assert any(e.order is not None for c in calls for e in c.events.edits)


# 8. Cost


def test_8_a_hundred_decisions_of_two_orders_cost_one_blob_per_call_that_changed_it() -> None:
    made = long_run(100)
    # One to open, one for each order, and one for each decision's fills.
    assert made.log.number == len(made.blobs()) == 1 + 200 + 100
    store = made.sim
    assert len(made.log.orders) == len(made.log.fills) == 200
    # A read of the whole log, once, and none after it.
    made.restart(OTHER)
    store.calls.clear()  # type: ignore[attr-defined]
    made.broker().positions()
    ops = [op for op, _ in store.calls]  # type: ignore[attr-defined]
    # The whole log once, and one read for the next number, which each later call costs.
    assert (ops.count("get"), ops.count("list")) == (302, 1)
    made.broker().fills(close_of(FULL))
    assert len(store.calls) == 304  # type: ignore[attr-defined]


def test_8_a_run_resumed_halfway_gives_the_account_of_one_that_was_not() -> None:
    whole = long_run(100)
    days = sessions(date(2026, 3, 3), 101)
    made = SimWorld(whole.closes, sim_store=Wired())
    for n, day in enumerate(days[:100]):
        if n == 50:
            made.restart(OTHER)
            made.sim.calls.clear()  # type: ignore[attr-defined]
            made.broker().clock()
        made.day(day, [(1 + n % 4, GLD), (-1 - n % 3, USO)] if n % 2 else [(-2, GLD), (1, USO)])
        if n == 50:
            # Call 1, the orders and fills of 49 decisions, the orders of the 50th, and then one
            # read for each of this decision's four calls and each of the four closes it asked for.
            gets = [op for op, _ in made.sim.calls].count("get")  # type: ignore[attr-defined]
            assert gets == 1 + 49 * 3 + 2 + 4 + 4
    made.decide(close_of(days[100]))
    made.agree()
    assert made.log.state == whole.log.state
    assert [f.fill_id for f in made.log.fills] == [f.fill_id for f in whole.log.fills]


def test_8_a_state_holds_no_order_that_ended() -> None:
    made = long_run(30)
    sizes = [len(b) for k, b in made.blobs().items()]
    assert max(sizes[-6:]) <= max(sizes[2:8]) + 40
