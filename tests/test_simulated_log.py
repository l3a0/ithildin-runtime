"""The simulated account's log: what it stores, and what each failed store leaves.

Each test's number is the acceptance item of #144 it shows. Every order goes in
through `OrderPath`.
"""

from __future__ import annotations

import json
import threading
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
    Halt,
    KeyExists,
    OrderState,
    StoreUnavailable,
    decode,
    encode,
    sim_account_key,
)
from ithildinruntime.execution.brokers.faults import (
    EditCash,
    EditPosition,
    Faults,
    FillPart,
    ForeignOrder,
    Late,
    OrderFault,
)
from ithildinruntime.execution.brokers.log import SimCall, SimState, SimulatedAccount
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
    made.sim.calls.clear()  # type: ignore[attr-defined]
    made.broker().positions()
    assert made.sim.calls == []  # type: ignore[attr-defined]
    made.decide(close_of(after(FULL)))
    made.sim.calls.clear()  # type: ignore[attr-defined]
    for _ in range(3):
        made.broker().positions()
    assert made.sim.calls == []  # type: ignore[attr-defined]
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
    made.sim.calls.clear()  # type: ignore[attr-defined]
    made.broker().positions()
    assert made.sim.calls == []  # type: ignore[attr-defined]


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


def test_4_a_create_that_lands_after_the_next_call_read_forward_is_taken_in() -> None:
    """The store answers neither, and the first lands between the two."""
    made = world()
    at = close_of(FULL) - timedelta(minutes=20)
    first, second = made.decide(at, made.orders(at, [(4, GLD), (5, USO)]))
    late: list[tuple[str, bytes]] = []
    made.sim.quiet = False  # type: ignore[attr-defined]

    class Slow:
        """Keeps the first create of call 2 and lets it land at the second."""

        def __init__(self, store: Wired) -> None:
            self.store = store

        def create(self, at_key: str, data: bytes, sha256: str) -> str:
            if at_key == key(2) and not late:
                late.append((at_key, data))
                raise StoreUnavailable(at_key, "no answer")
            if late and at_key == key(2):
                self.store.put(*late[0])
            return self.store.create(at_key, data, sha256)

        def get(self, at_key: str) -> bytes:
            return self.store.get(at_key)

        def list(self, prefix: str, newest: str | None) -> list[str]:
            return self.store.list(prefix, newest)

    made.log.store = Slow(made.sim)  # type: ignore[assignment,arg-type]
    assert made.send(first) is OrderState.IN_DOUBT
    assert made.broker().open_orders().orders == ()
    made.clock.at += timedelta(seconds=1)
    assert made.send(second) is OrderState.IN_DOUBT
    # The log is this writer's, so nothing blames a second process.
    held = made.broker().open_orders().orders
    assert [o.client_order_id for o in held] == [first.client_order_id]
    assert made.log.number == 2
    later = close_of(after(FULL))
    made.decide(later)
    assert made.broker().positions().positions == {GLD: 4}
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


def test_4_an_unsure_log_that_cannot_be_read_stays_unsure() -> None:
    made = world()
    made.day(FULL, [(4, GLD)])
    made.clock.at += MICRO
    made.sim.after = down("create", 3)  # type: ignore[attr-defined]
    with pytest.raises(BrokerUnavailable):
        made.broker().positions()
    quiet(made)
    made.sim.refuse(("list", "sim/"))  # type: ignore[attr-defined]
    for _ in range(2):
        with pytest.raises(BrokerUnavailable):
            made.broker().positions()
    quiet(made)
    assert made.broker().positions().positions == {GLD: 4}
    assert list(made.blobs()) == [key(n) for n in range(1, 4)]


# Kills


def ran(made: SimWorld, days: list[date]) -> None:
    for n, day in enumerate(days):
        made.day(day, [(2 + n, GLD), (-1 - n, USO)])


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("number", range(2, 8))
def test_4_a_kill_before_and_after_each_blob_leaves_one_log_that_goes_on(number, side) -> None:
    days = sessions(FULL, 3)
    made = world(6)

    def hook(count: int, op: str, at: str) -> None:
        if (op, at) == ("create", key(number)):
            raise Kill

    setattr(made.sim, side, hook)
    killed = 0
    for n, day in enumerate(days):
        at = close_of(day)
        try:
            planned = made.decide(at, made.orders(at, [(2 + n, GLD), (-1 - n, USO)]))
        except (Kill, BrokerUnavailable):
            killed += 1
            quiet(made)
            made.restart(f"{n + 1:032x}")
            made.recover()
            planned = ()
        for intent in planned:
            state = made.send(intent)
            if state is OrderState.IN_DOUBT:
                killed += 1
                quiet(made)
                made.restart(f"{n + 7:032x}")
    assert killed == 1
    made.decide(close_of(after(days[-1])))
    log = again(made)
    assert log.state == made.log.account_now
    assert log.state.open_orders == ()
    names = [o.client_order_id for o in log.orders.values()]
    assert len(set(names)) == len(names)
    assert [f.fill_id for f in log.fills] == [f"sim-f-{n}" for n in range(1, len(log.fills) + 1)]
    # Each order the journal holds acknowledged was filled, once, and so was one in doubt that
    # the log holds.
    doubtful = {o.client_order_id for o in _doubtful(made)}
    acked = {n for n, o in made.view().orders.items() if o.broker_order_id}
    assert acked <= set(names) <= acked | doubtful
    assert sorted(f.client_order_id for f in log.fills) == sorted(names)
    total = {i: sum((f.qty for f in log.fills if f.instrument == i), D(0)) for i in (GLD, USO)}
    assert dict(log.state.positions) == {i: q for i, q in total.items() if q}
    assert log.state.cash == CASH - sum((f.qty * f.price for f in log.fills), D(0))


def _doubtful(made: SimWorld) -> list:
    return [o.intent for o in made.view().live.values() if o.state is OrderState.IN_DOUBT]


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


def test_4_a_second_process_on_one_account_halts_the_first() -> None:
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    first = made.log
    made.restart(OTHER)
    at = close_of(FULL) - timedelta(minutes=10)
    (intent,) = made.decide(at, made.orders(at, [(5, USO)]))
    assert made.send(intent) is OrderState.ACKNOWLEDGED
    made.log = first
    made.decide(close_of(after(FULL)), recover=False)
    for _ in range(2):
        with pytest.raises(Halt, match="call 3 is another writer's, so two processes"):
            made.broker().positions()
    assert made.log.number == 2
    # The second one's log is whole and goes on.
    assert again(made).number == 3


def test_4_a_second_process_whose_blob_is_read_forward_halts_too() -> None:
    made = world()
    made.day(FULL, [(4, GLD)])
    made.clock.at += MICRO
    made.sim.before = down("create", 3)  # type: ignore[attr-defined]
    with pytest.raises(BrokerUnavailable):
        made.broker().positions()
    quiet(made)
    first = made.log
    made.restart(OTHER)
    made.broker().positions()
    made.log = first
    with pytest.raises(Halt, match="call 3 is another writer's"):
        made.broker().positions()


# A log that cannot be read back


def full() -> SimWorld:
    made = world()
    made.day(FULL, [(4, GLD), (-5, USO)])
    made.decide(close_of(after(FULL)))
    return made


def halts(made: SimWorld, reason: str) -> None:
    made.restart(OTHER)
    for _ in range(2):
        with pytest.raises(Halt, match=reason):
            made.path().positions()


def test_4_a_gap_halts() -> None:
    made = full()
    made.sim.drop(key(3))  # type: ignore[attr-defined]
    halts(made, "call 3 is missing, and the next key is sim/sim-1/000000000004.json")


def test_4_a_lost_first_call_halts() -> None:
    made = full()
    made.sim.drop(key(1))  # type: ignore[attr-defined]
    halts(made, "call 1 is missing")


def test_4_a_deleted_log_halts_and_never_reads_as_a_new_account() -> None:
    made = full()
    for at in list(made.blobs()):
        made.sim.drop(at)  # type: ignore[attr-defined]
    halts(made, "never opened, or its log is lost")
    assert made.blobs() == {}


def test_4_a_lost_newest_call_is_met_by_the_next_create_of_the_process_that_made_it() -> None:
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


def test_4_a_fill_under_another_id_and_an_end_of_an_order_not_open_halt() -> None:
    made = full()
    found = call(made, 4)
    first, second = found.events.closed
    wrong = replace(first, fill=replace(first.fill, fill_id="sim-f-7"))
    made.sim.put(  # type: ignore[attr-defined]
        key(4), encode(replace(found, events=replace(found.events, closed=(wrong, second))))
    )
    halts(made, "holds the fill sim-f-7, not sim-f-1")
    twice = replace(found, events=replace(found.events, closed=(first, first, second)))
    made.sim.put(key(4), encode(twice))  # type: ignore[attr-defined]
    halts(made, "the simulated order sim-o-1 ended and was not open")


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
}


@pytest.mark.parametrize("read", CALLS)
def test_4_two_threads_never_build_on_one_number(read: str) -> None:
    made = world()
    made.day(FULL, [(4, GLD)])
    made.clock.at += MICRO
    entered, done = threading.Event(), threading.Event()

    def other() -> None:
        CALLS[read](made.broker())
        done.set()

    early: list[bool] = []

    def hook(count: int, op: str, at: str) -> None:
        if op == "create" and not entered.is_set():
            entered.set()
            threading.Thread(target=other, daemon=True).start()
            early.append(done.wait(0.1))

    made.sim.before = hook  # type: ignore[attr-defined]
    assert made.broker().positions().positions == {GLD: 4}
    assert done.wait(5)
    # The second call waits for the first's create, and stores nothing of its own.
    assert early == [False]
    assert made.log.number == len(made.blobs()) == 3


# 5. Determinism


def long_run(count: int, faults: bool) -> SimWorld:
    days, priced = sessions(date(2026, 3, 3), count + 1), sessions(date(2026, 3, 2), count + 2)
    closes = {
        GLD: {d: D(100) + D(n % 7) / 4 for n, d in enumerate(priced)},
        USO: {d: D(50) - D(n % 5) / 8 for n, d in enumerate(priced)},
    }
    made = SimWorld(closes, sim_store=Wired())
    if faults:

        def name(n: int, place: int = 1) -> str:
            return order(place, 1, at=close_of(days[n])).client_order_id

        made.faults = Faults(
            orders={
                name(3): OrderFault.REJECT,
                name(5): FillPart(D(1)),
                name(7): OrderFault.FILL_NONE,
                name(9): OrderFault.REJECT_LATER,
                name(11): OrderFault.DUPLICATE,
                name(13): Late(1),
                name(15): OrderFault.LOSE_ANSWER,
            },
            events={40: EditPosition(GLD, D(1)), 60: EditCash(D(-3)), 82: ForeignOrder(USO, D(2))},
        )
    for n, day in enumerate(days[:count]):
        made.day(day, [(1 + n % 4, GLD), (-1 - n % 3, USO)] if n % 2 else [(-2, GLD), (1, USO)])
    made.decide(close_of(days[count]))
    return made


@pytest.mark.parametrize("faults", [False, True], ids=["as-it-is", "with-faults"])
def test_5_fifty_decisions_twice_give_the_same_bytes(faults: bool) -> None:
    one, two = long_run(50, faults).blobs(), long_run(50, faults).blobs()
    assert one == two
    assert len(one) > 100
    states = {c["state"] for b in one.values() for c in json.loads(b)["events"]["closed"]}
    assert states == ({"filled", "expired", "rejected"} if faults else {"filled"})


def test_5_this_release_reads_and_writes_the_committed_log() -> None:
    """A shadow account's log outlives a release, so its bytes never change."""
    made = world(faults=Faults(events={4: EditPosition(GLD, D(1)), 6: EditCash(D("-2.5"))}))
    made.faults = replace(
        made.faults,
        orders={made.orders(close_of(FULL), [(1, GLD)])[0].client_order_id: FillPart(D("1.5"))},
    )
    made.day(FULL, [(4, GLD), (-5, USO)], recover=False)
    for _ in range(4):
        made.broker().positions()
    made.decide(close_of(after(FULL)), recover=False)
    made.broker().positions()
    stored = {p.name: p.read_bytes() for p in sorted(FIXTURES.glob("*.json"))}
    assert {k.rpartition("/")[2]: v for k, v in made.blobs().items()} == stored
    store = Wired({f"sim/{ACCOUNT}/{name}": data for name, data in stored.items()})
    log = SimulatedAccount.load(store, ACCOUNT, OTHER)
    log.catch_up()
    assert log.state == made.log.state
    states = [o.state for o in log.orders.values()]
    assert states == [BrokerState.EXPIRED, BrokerState.FILLED]


# 8. Cost


def test_8_a_hundred_decisions_of_two_orders_cost_one_blob_per_call_that_changed_it() -> None:
    made = long_run(100, False)
    # One to open, one for each order, and one for each decision's fills.
    assert made.log.number == len(made.blobs()) == 1 + 200 + 100
    store = made.sim
    assert len(made.log.orders) == len(made.log.fills) == 200
    # A read of the whole log, once, and none after it.
    made.restart(OTHER)
    store.calls.clear()  # type: ignore[attr-defined]
    made.broker().positions()
    ops = [op for op, _ in store.calls]  # type: ignore[attr-defined]
    assert (ops.count("get"), ops.count("list")) == (301, 1)
    made.broker().fills(close_of(FULL))
    assert len(store.calls) == 302  # type: ignore[attr-defined]


def test_8_a_run_resumed_halfway_gives_the_account_of_one_that_was_not() -> None:
    whole = long_run(20, False)
    days = sessions(date(2026, 3, 3), 21)
    closes = whole.closes
    made = SimWorld(closes, sim_store=Wired())
    for n, day in enumerate(days[:20]):
        if n == 10:
            made.restart(OTHER)
            made.sim.calls.clear()  # type: ignore[attr-defined]
        made.day(day, [(1 + n % 4, GLD), (-1 - n % 3, USO)] if n % 2 else [(-2, GLD), (1, USO)])
        if n == 10:
            assert [op for op, _ in made.sim.calls].count("get") == 1 + 20 + 9  # type: ignore[attr-defined]
    made.decide(close_of(days[20]))
    made.agree()
    assert made.log.state == whole.log.state
    assert [f.fill_id for f in made.log.fills] == [f.fill_id for f in whole.log.fills]


def test_8_a_state_holds_no_order_that_ended() -> None:
    made = long_run(30, False)
    sizes = [len(b) for k, b in made.blobs().items()]
    assert max(sizes[-6:]) <= max(sizes[2:8]) + 40
