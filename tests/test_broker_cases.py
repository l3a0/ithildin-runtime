"""The simulated broker's cases, each order sent through `OrderPath`, gate and journal included.

A test's number is the item of #144 it shows: 1 to 8 of its acceptance, and from
9 on of its contract. No test here calls a broker's submit. `cancel`,
`find_order` and the reads are called on the simulator itself, as acceptance 1
says, so no case here runs on another broker as it stands.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    DATA_STAGE,
    SNAPSHOT_OUTPUT,
    BarStatus,
    BrokerState,
    BrokerUnavailable,
    FillBasis,
    Halt,
    OrderState,
    OrderType,
    Outcome,
    TimeInForce,
    output_key,
    result_key,
    sha256_of,
)
from ithildinruntime.execution.brokers import simulated
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
from ithildinruntime.execution.brokers.simulated import SimulatedBroker, refusal
from ithildinruntime.journal import AckEntry, InDoubtEntry, Journal, RejectedEntry, record_of
from tests.support import gates
from tests.support.fake_broker import FakeBroker
from tests.support.order_world import order
from tests.support.relay_broker import RelayBroker
from tests.support.sim_world import (
    ACCOUNT,
    CASH,
    DEPLOYMENT,
    EARLY,
    FULL,
    GLD,
    MICRO,
    TABLE,
    USO,
    Plain,
    SimWorld,
    after,
    close_of,
)

D = Decimal
DAYS = (FULL, EARLY)
START, PRICE, NEXT = D("10.25"), D("12.5"), D("99")
LONG_AGO = datetime.fromisoformat("2000-01-01T00:00:00+00:00")
# The side, the shares held before, the order, the shares after and the cash, by hand: the
# shares held before came at 10.25 and the order fills at 12.50.
SIDES = {
    "a-buy-opens": (0, 4, 4, "999950"),
    "a-sale-opens": (0, -4, -4, "1000050"),
    "a-buy-closes": (-4, 4, 0, "999991"),
    "a-sale-closes": (4, -4, 0, "1000009"),
    "a-buy-crosses": (-3, 7, 4, "999943.25"),
    "a-sale-crosses": (3, -7, -4, "1000056.75"),
    "a-buy-leaves-less": (-5, 2, -3, "1000026.25"),
    "a-sale-leaves-less": (5, -2, 3, "999973.75"),
}


def world(day=FULL, faults: Faults | None = None, **changes: object) -> SimWorld:
    """Four sessions: one before, the one whose close holds 10.25, `day` at 12.50, and the next."""
    prior = TABLE.before(day).day
    series = {TABLE.before(prior).day: D(1), prior: START, day: PRICE, after(day): NEXT}
    # The second leg's series differs, so a fill at the other leg's close answers otherwise.
    other = {d: p + 1000 for d, p in series.items()}
    given = {GLD: series, USO: other}
    if changes.pop("swapped", False):
        given = {USO: series, GLD: other}
    return SimWorld(given, faults or Faults(), **changes)  # type: ignore[arg-type]


def ids(day=FULL, early: int = 0, count: int = 1) -> list[str]:
    at = close_of(day) - timedelta(minutes=early)
    return [order(n, 1, at=at).client_order_id for n in range(1, count + 1)]


def sent(
    wanted=((4, GLD),), day=FULL, early: int = 0, faults: Faults | None = None
) -> tuple[SimWorld, list[OrderState]]:
    """One decision with its orders sent, the first of which is the process's first call."""
    made = world(day, faults)
    return made, made.day(day, wanted, early, recover=False)


def entries(made: SimWorld) -> list[object]:
    return [record_of(e) for e in Journal(Plain(made.store.blobs), ACCOUNT).read()]


def settled(made: SimWorld, day=FULL) -> None:
    """The decision of the session after, whose recover reads the fills."""
    made.decide(close_of(after(day)))


# 1. The relay


def test_1_a_relay_reaches_a_simulated_broker_alone() -> None:
    made, _ = sent(())
    fake = FakeBroker(lambda: made.store.blobs, ACCOUNT, made.clock.at)
    with pytest.raises(TypeError, match="simulated broker alone"):
        RelayBroker(fake, lambda: None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="simulated broker alone"):
        RelayBroker(RelayBroker(made.broker(), lambda: None), lambda: None)  # type: ignore[arg-type]

    class Mine(SimulatedBroker):
        pass

    mine = Mine(made.log, made.clock, object(), Faults())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="simulated broker alone"):
        RelayBroker(mine, lambda: None)


def test_1_a_relay_forwards_one_order_once_and_unchanged() -> None:
    made = world()
    first, second = made.decide(close_of(FULL), made.orders(close_of(FULL), [(4, GLD), (5, USO)]))
    relay = RelayBroker(made.broker(), lambda: None)
    assert made.send(first, relay) is OrderState.ACKNOWLEDGED
    assert made.send(second, relay) is OrderState.IN_DOUBT
    (held,) = made.broker().open_orders().orders
    assert (held.client_order_id, held.qty, held.instrument) == (first.client_order_id, 4, GLD)


# 2 and 7. The fill rule, on each side of each leg


@pytest.mark.parametrize("leg", [GLD, USO])
@pytest.mark.parametrize("early", [0, 20], ids=["at-the-close", "20-minutes-before"])
@pytest.mark.parametrize("day", DAYS, ids=["a-full-day", "an-early-close"])
@pytest.mark.parametrize("side", SIDES)
def test_2_each_side_fills_whole_at_its_sessions_close(side: str, day, early: int, leg) -> None:
    before, qty, end, cash = SIDES[side]
    made, prior = world(day, swapped=leg == USO), TABLE.before(day).day
    if before:
        assert made.day(prior, [(before, leg)]) == [OrderState.ACKNOWLEDGED]
    at, close = close_of(day) - timedelta(minutes=early), close_of(day)
    (intent,) = made.decide(at, made.orders(at, [(qty, leg)]))
    held = {leg: D(before)} if before else {}
    cash_before = CASH - before * START
    old = 1 if before else 0
    assert made.send(intent) is OrderState.ACKNOWLEDGED

    for now in sorted({at, max(at, close - MICRO), close}):
        made.clock.at = now
        broker = made.broker()
        assert len(broker.fills(LONG_AGO).fills) == old
        assert broker.positions().positions == held
        assert broker.account().cash == cash_before
        (copy,) = broker.open_orders().orders
        assert (copy.client_order_id, copy.state) == (intent.client_order_id, BrokerState.WORKING)
        assert (copy.qty, copy.filled, copy.submitted_at) == (qty, 0, at)

    # Past the close, the decision that read the close fills there, and one that had not waits.
    made.clock.at = received = close + MICRO
    waits = early > 0
    assert len(made.broker().fills(LONG_AGO).fills) == (old if waits else old + 1)
    if waits:
        assert made.broker().open_orders().orders[0].state is BrokerState.WORKING
        received = close_of(after(day))
    settled(made, day)

    broker = made.broker()
    fill = broker.fills(LONG_AGO).fills[-1]
    assert (fill.fill_id, fill.broker_order_id) == (f"sim-f-{old + 1}", f"sim-o-{old + 1}")
    assert (fill.client_order_id, fill.instrument) == (intent.client_order_id, leg)
    assert (fill.qty, fill.price) == (qty, PRICE)
    assert (fill.at, fill.received_at) == (close, received)
    assert fill.at_text == close.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    assert (fill.correction_id, fill.excess_places) == (None, ())
    assert fill.basis is (FillBasis.OFFICIAL_CLOSE if waits else FillBasis.SIGNAL_CLOSE)
    assert broker.positions().positions == ({leg: D(end)} if end else {})
    assert broker.account().cash == D(cash)
    assert broker.open_orders().orders == ()
    (closed,) = [o for o in broker.orders(at).orders]
    assert (closed.state, closed.filled) == (BrokerState.FILLED, qty)
    made.agree()
    # Nothing fills a second time.
    made.clock.at += timedelta(days=3)
    assert len(made.broker().fills(LONG_AGO).fills) == old + 1


def test_2_two_legs_each_fill_at_their_own_close() -> None:
    made, states = sent([(2, GLD), (-3, USO)])
    assert states == [OrderState.ACKNOWLEDGED] * 2
    settled(made)
    fills = made.broker().fills(LONG_AGO).fills
    assert [(f.instrument, f.qty, f.price) for f in fills] == [
        (GLD, 2, D("12.5")),
        (USO, -3, D("1012.5")),
    ]
    # 1,000,000 less 2 at 12.50, plus 3 at 1,012.50.
    assert made.broker().account().cash == D("1003012.5")
    made.agree()


@pytest.mark.parametrize("side", [1, -1])
def test_2_cash_and_shares_move_exactly_and_never_round(side: int) -> None:
    made = world(share_step=D("1E-9"))
    made.closes[GLD][FULL] = D("12.345678")  # type: ignore[index]
    assert made.day(FULL, [(side * D("1.000000001"), GLD)]) == [OrderState.ACKNOWLEDGED]
    settled(made)
    (fill,) = made.broker().fills(LONG_AGO).fills
    assert (fill.price, fill.excess_places) == (D("12.345678"), ("price",))
    assert made.broker().positions().positions == {GLD: side * D("1.000000001")}
    # 1,000,000 less, or plus, 1.000000001 at 12.345678.
    cash = {1: "999987.654321987654322", -1: "1000012.345678012345678"}[side]
    assert made.broker().account().cash == D(cash)
    made.agree()


def test_2_an_order_sent_at_the_close_and_filled_by_the_next_decision_is_signal_close() -> None:
    """The path test 1 of #4 takes: the next call is the next decision's recover."""
    made, _ = sent()
    settled(made)
    (fill,) = made.broker().fills(LONG_AGO).fills
    assert fill.basis is FillBasis.SIGNAL_CLOSE
    assert fill.received_at == close_of(after(FULL))


def test_2_a_fill_names_the_snapshot_that_gave_its_price() -> None:
    """A bar revised between two decisions gives the price the settling one holds."""
    for polled in (True, False):
        made, _ = sent()
        if polled:
            made.clock.at += MICRO
            assert len(made.broker().fills(LONG_AGO).fills) == 1
        made.closes[GLD][FULL] = D(13)  # type: ignore[index]
        settled(made)
        (fill,) = made.broker().fills(LONG_AGO).fills
        (close,) = json.loads(made.blobs()["sim/sim-1/000000000003.json"])["events"]["closed"]
        run, at = (FULL, close_of(FULL)) if polled else (after(FULL), close_of(after(FULL)))
        key = output_key(DEPLOYMENT, run.isoformat(), at, DATA_STAGE, SNAPSHOT_OUTPUT)
        assert close["priced_from"] == {"key": key, "sha256": sha256_of(made.store.blobs[key])}
        assert fill.price == (PRICE if polled else 13)
        assert fill.basis is FillBasis.SIGNAL_CLOSE


def test_2_a_fill_at_an_official_close_names_its_snapshot_too() -> None:
    made, _ = sent(early=20)
    settled(made)
    (close,) = json.loads(made.blobs()["sim/sim-1/000000000003.json"])["events"]["closed"]
    assert close["fill"]["basis"] == "official_close"
    at = close_of(after(FULL))
    key = output_key(DEPLOYMENT, after(FULL).isoformat(), at, DATA_STAGE, SNAPSHOT_OUTPUT)
    assert close["priced_from"] == {"key": key, "sha256": sha256_of(made.store.blobs[key])}


@pytest.mark.parametrize(
    ("moved", "state"),
    [
        (-MICRO, OrderState.ACKNOWLEDGED),
        (timedelta(0), OrderState.ACKNOWLEDGED),
        (MICRO, OrderState.REJECTED),
    ],
    ids=["a-microsecond-before", "at-the-close", "a-microsecond-after"],
)
@pytest.mark.parametrize("day", DAYS, ids=["a-full-day", "an-early-close"])
def test_2_an_order_rests_up_to_its_close_and_is_rejected_after_it(
    day, moved: timedelta, state: OrderState
) -> None:
    made = world(day)
    at, close = close_of(day) - timedelta(minutes=20), close_of(day)
    (intent,) = made.decide(at, made.orders(at, [(4, GLD)]))

    def moves() -> None:
        made.clock.at = close + moved

    assert made.send(intent, RelayBroker(made.broker(), moves)) is state
    held = made.broker().open_orders().orders
    if state is OrderState.REJECTED:
        assert held == ()
        (entry,) = [e for e in entries(made) if isinstance(e, RejectedEntry)]
        assert "after its session's close" in json.loads(entry.raw)["reason"]
        settled(made, day)
        assert made.broker().fills(LONG_AGO).fills == ()
    else:
        assert held[0].submitted_at == close + moved
        settled(made, day)
        assert made.broker().fills(LONG_AGO).fills[0].price == PRICE
    made.agree()


def test_2_an_order_sent_the_next_morning_does_not_rest_for_that_days_close() -> None:
    made = world()
    at = close_of(FULL) - timedelta(minutes=20)
    (intent,) = made.decide(at, made.orders(at, [(4, GLD)]))

    def moves() -> None:
        made.clock.at = TABLE.session(after(FULL)).open + timedelta(minutes=5)

    assert made.send(intent, RelayBroker(made.broker(), moves)) is OrderState.REJECTED


def test_1_each_way_an_order_is_refused_on_the_rule_itself() -> None:
    close = close_of(FULL)
    moc = order(1, 4, at=close)
    assert refusal(moc, close, close) is None
    assert refusal(replace(moc, qty=D("-0.000000001")), close, close) is None
    for changes, why in (
        ({"type": OrderType.MARKET}, "no market order is taken for cls"),
        ({"time_in_force": TimeInForce.DAY}, "no market_on_close order is taken for day"),
        ({"qty": D(0)}, "zero shares"),
        ({"qty": D("0.0000000001")}, "no number of shares"),
        ({"qty": D("NaN")}, "no number of shares"),
        ({"qty": D("Infinity")}, "no number of shares"),
    ):
        assert why in (refusal(replace(moc, **changes), close, close) or ""), changes
    assert "after its session's close" in (refusal(moc, close + MICRO, close) or "")
    # The day's market order is taken too, and #155's cases in tests/test_market_fills.py fill it.
    day = replace(moc, type=OrderType.MARKET, time_in_force=TimeInForce.DAY)
    assert refusal(day, close, close) is None


# 2 and 14. A cancel and the lookups


@pytest.mark.parametrize("early", [timedelta(minutes=5), MICRO, timedelta(0)])
def test_14_a_working_order_is_cancelled_up_to_the_close_itself(early: timedelta) -> None:
    made, _ = sent(early=20)
    made.clock.at = close_of(FULL) - early
    answer = made.broker().cancel("sim-o-1")
    assert (answer.outcome, answer.broker_order_id) == (Outcome.ACCEPTED, "sim-o-1")
    assert answer.order is not None and answer.order.state is BrokerState.CANCELLED
    assert json.loads(answer.raw)["state"] == "cancelled"
    assert made.broker().open_orders().orders == ()
    again = made.broker().cancel("sim-o-1")
    assert (again.outcome, again.order) == (Outcome.REJECTED, answer.order)
    made.decide(close_of(after(FULL)), recover=False)
    assert made.broker().fills(LONG_AGO).fills == ()
    assert made.broker().positions().positions == {}
    assert made.broker().lookup("sim-o-1").order.state is BrokerState.CANCELLED


def test_14_a_cancel_and_a_later_rejection_end_at_their_own_time() -> None:
    later = Faults({ids(early=20)[0]: OrderFault.REJECT_LATER})
    for faults, call in ((Faults(), "cancel"), (later, "positions")):
        made, _ = sent(early=20, faults=faults)
        made.clock.at += timedelta(seconds=5)
        READS[call](made.broker())
        (end,) = json.loads(made.blobs()["sim/sim-1/000000000003.json"])["events"]["closed"]
        assert end["at"] == "2026-11-03T20:40:05.000000Z"


@pytest.mark.parametrize("which", ["sim-o-1", "sim-o-9"])
def test_14_a_cancel_after_the_close_is_refused_and_settles_nothing(which: str) -> None:
    made, _ = sent()
    made.clock.at += MICRO
    assert made.broker().cancel(which).outcome is Outcome.REJECTED
    assert made.log.number == 2
    assert made.broker().positions().positions == {GLD: 4}


def test_14_a_cancel_does_nothing_but_its_own_part() -> None:
    """What was due at its call comes at the next one."""
    made, _ = sent(early=20, faults=Faults(events={2: EditCash(D(5)), 3: EditCash(D(7))}))
    assert made.broker().cancel("sim-o-1").outcome is Outcome.ACCEPTED
    assert made.log.account_now.cash == CASH
    assert made.broker().account().cash == CASH + 12


def test_14_a_cancel_after_the_close_is_refused_while_no_prices_hold_it() -> None:
    made, _ = sent(early=20)
    made.clock.at = close_of(FULL) + MICRO
    answer = made.broker().cancel("sim-o-1")
    assert (answer.outcome, answer.order.state) == (Outcome.REJECTED, BrokerState.WORKING)
    settled(made)
    assert len(made.broker().fills(LONG_AGO).fills) == 1


@pytest.mark.parametrize(
    ("fault", "state", "filled"),
    [
        (None, BrokerState.FILLED, 4),
        (OrderFault.FILL_NONE, BrokerState.EXPIRED, 0),
        (FillPart(D(1)), BrokerState.EXPIRED, 1),
        (OrderFault.REJECT_LATER, BrokerState.REJECTED, 0),
    ],
)
def test_14_a_cancel_of_an_order_that_ended_is_refused(fault, state, filled) -> None:
    made, _ = sent(faults=Faults({ids()[0]: fault} if fault else {}))
    made.clock.at = close_of(FULL) + MICRO
    made.broker().positions()
    answer = made.broker().cancel("sim-o-1")
    assert (answer.outcome, answer.order.state) == (Outcome.REJECTED, state)
    assert answer.order.filled == filled
    assert made.broker().lookup("sim-o-1").order == answer.order


def test_14_an_unknown_id_is_not_found_by_a_lookup_and_refused_by_a_cancel() -> None:
    made, _ = sent()
    for unknown in ("sim-o-2", "", ids()[0]):
        found = made.broker().lookup(unknown)
        assert found.outcome is Outcome.NOT_FOUND
        assert (found.broker_order_id, found.order) == (None, None)
        refused = made.broker().cancel(unknown)
        assert (refused.outcome, refused.order) == (Outcome.REJECTED, None)
    assert made.broker().lookup("sim-o-1").order.state is BrokerState.WORKING


def test_14_a_search_finds_an_order_by_its_own_id_since_a_time() -> None:
    made, _ = sent([(4, GLD), (5, USO)])
    broker, at = made.broker(), close_of(FULL)
    intent = made.orders(at, [(4, GLD), (5, USO)])[1]
    found = broker.find_order(intent, at, {})
    assert (found.answer, found.whole_list) == (Outcome.ACCEPTED, True)
    assert [row.broker_order_id for row in found.rows] == ["sim-o-2"]
    late = broker.find_order(intent, at + MICRO, {})
    assert (late.answer, late.rows) == (Outcome.NOT_FOUND, ())
    other = broker.find_order(order(9, 1, at=at), at, {})
    assert (other.answer, other.rows) == (Outcome.NOT_FOUND, ())
    assert [o.broker_order_id for o in broker.orders(at).orders] == ["sim-o-1", "sim-o-2"]
    assert broker.orders(at + MICRO).orders == ()


# 6 and 12. What a broker is, and its clock


def test_6_a_read_names_its_bytes_apart_from_its_values() -> None:
    made, _ = sent([(4, GLD), (-5, USO)])
    settled(made)
    broker, now = made.broker(), made.clock.at
    assert broker.alias == ACCOUNT
    assert broker.clock() == now
    bodies = {
        "positions": broker.positions(),
        "open_orders": broker.open_orders(),
        "account": broker.account(),
        "fills": broker.fills(LONG_AGO),
        "orders": broker.orders(LONG_AGO),
        "activities": broker.activities(LONG_AGO),
    }
    for name, read in bodies.items():
        assert read.whole, name
        assert [page.at for page in read.received] == [now], name
    said = {name: json.loads(read.received[0].body) for name, read in bodies.items()}
    assert said["positions"] == {"GLD": "4", "USO": "-5"}
    assert said["open_orders"] == [] and said["activities"] == []
    assert D(said["account"]["cash"]) == bodies["account"].cash
    assert [f["fill_id"] for f in said["fills"]] == ["sim-f-1", "sim-f-2"]
    assert [o["broker_order_id"] for o in said["orders"]] == ["sim-o-1", "sim-o-2"]
    fills = broker.fills(close_of(FULL)).fills
    assert len(fills) == 2
    assert broker.fills(close_of(FULL) + MICRO).fills == ()


@pytest.mark.parametrize(
    "read", ["positions", "open_orders", "account", "fills", "orders", "activities"]
)
def test_6_a_read_past_the_limit_is_not_whole(read: str, monkeypatch) -> None:
    made, _ = sent([(4, GLD)])
    if read in ("positions", "fills"):
        made.clock.at += MICRO
    size = len(READS[read](made.broker()).received[0].body)
    monkeypatch.setattr(simulated, "READ_LIMIT", size)
    assert READS[read](made.broker()).whole
    monkeypatch.setattr(simulated, "READ_LIMIT", size - 1)
    assert not READS[read](made.broker()).whole


READS = {
    "positions": lambda b: b.positions(),
    "open_orders": lambda b: b.open_orders(),
    "account": lambda b: b.account(),
    "fills": lambda b: b.fills(LONG_AGO),
    "orders": lambda b: b.orders(LONG_AGO),
    "activities": lambda b: b.activities(LONG_AGO),
    "lookup": lambda b: b.lookup("sim-o-1"),
    "find_order": lambda b: b.find_order(order(1, 4, at=close_of(FULL)), LONG_AGO, {}),
    "cancel": lambda b: b.cancel("sim-o-1"),
}


OF_ORDERS = ["open_orders", "orders", "lookup", "find_order"]
HALTING = sorted(set(READS) - {"cancel", *OF_ORDERS})


@pytest.mark.parametrize("call", HALTING)
def test_12_a_clock_before_the_newest_call_halts(call: str) -> None:
    made, _ = sent()
    made.clock.at = close_of(FULL) - MICRO
    with pytest.raises(Halt) as raised:
        READS[call](made.broker())
    assert raised.value.reason == (
        "the clock reads 2026-11-03T20:59:59.999999Z, before the simulated account sim-1's newest "
        "call at 2026-11-03T21:00:00.000000Z"
    )
    made.clock.at = close_of(FULL)
    READS[call](made.broker())


@pytest.mark.parametrize("call", HALTING)
def test_12_a_clock_before_the_decision_of_its_prices_halts(call: str) -> None:
    made, _ = sent()
    made.decide(close_of(after(FULL)), recover=False)
    made.clock.at = close_of(FULL) + MICRO
    number = made.log.number
    with pytest.raises(Halt) as raised:
        READS[call](made.broker())
    assert raised.value.reason == (
        "the clock reads 2026-11-03T21:00:00.000001Z, before the simulated account sim-1's "
        "prices' decision at 2026-11-04T21:00:00.000000Z"
    )
    assert made.log.number == number


NEW_YORK = timezone(timedelta(hours=-5))
CLOCKS: dict[str, Callable[[datetime], object]] = {
    "an-hour-back": lambda at: at - timedelta(hours=1),
    "a-microsecond-back": lambda at: at - MICRO,
    "no-offset": lambda at: at.replace(tzinfo=None),
    "another-offset": lambda at: at.astimezone(NEW_YORK),
    "no-time": lambda at: "20:40",
    "a-time-of-day": lambda at: at.timetz(),
}


@pytest.mark.parametrize("clock", CLOCKS)
def test_12_no_clock_stops_a_cancel(clock: str) -> None:
    made, _ = sent(early=20)
    at = made.clock.at
    made.clock.at = found = CLOCKS[clock](at)  # type: ignore[assignment]
    answer = made.broker().cancel("sim-o-1")
    assert answer.outcome is Outcome.ACCEPTED
    assert answer.order.state is BrokerState.CANCELLED
    # It is stored at the newest call's time, so the log still goes forward.
    (end,) = json.loads(made.blobs()["sim/sim-1/000000000003.json"])["events"]["closed"]
    assert end["at"] == "2026-11-03T20:40:00.000000Z"
    blob = json.loads(made.blobs()["sim/sim-1/000000000003.json"])
    read = found.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if clock.endswith("back") else None
    assert (blob["at"], blob["clock"]) == ("2026-11-03T20:40:00.000000Z", read)
    made.clock.at = at
    assert made.broker().open_orders().orders == ()
    # A fresh process reads the blob back.
    made.restart("d" * 32)
    assert made.broker().lookup("sim-o-1").order.state is BrokerState.CANCELLED


def test_12_a_clock_that_cannot_be_read_stops_no_cancel() -> None:
    made, _ = sent(early=20)

    def broken() -> datetime:
        raise RuntimeError("the history clock reads a time only once this thread waited")

    made.clock.now = broken  # type: ignore[method-assign]
    assert made.broker().cancel("sim-o-1").outcome is Outcome.ACCEPTED
    with pytest.raises(Halt) as raised:
        made.broker().positions()
    # The type alone, and no word of the error's.
    assert raised.value.reason == "the simulated account sim-1's clock raised RuntimeError"


class NoZone(tzinfo):
    """A zone that raises when a time is asked its offset."""

    def utcoffset(self, at: datetime | None) -> timedelta | None:
        raise RuntimeError("no zone")


@pytest.mark.parametrize("call", ["cancel", "open_orders", "orders", "lookup"])
def test_12_a_clock_whose_offset_raises_stops_no_cancel_and_no_read_of_the_orders(call) -> None:
    made, _ = sent(early=20)
    made.clock.at = made.clock.at.replace(tzinfo=NoZone())
    with pytest.raises(Halt):
        made.broker().positions()
    READS[call](made.broker())


def test_12_a_read_of_the_orders_the_clock_stops_does_nothing_that_is_due() -> None:
    """An order on its way would otherwise be taken after its close, at the newest call's time."""
    made, _ = sent(early=20, faults=Faults({ids(early=20)[0]: Late(1)}))
    number = made.log.number
    made.clock.at = (close_of(FULL) + timedelta(minutes=5)).replace(tzinfo=None)
    assert made.broker().open_orders().orders == ()
    assert made.log.number == number
    made.clock.at = close_of(FULL) + timedelta(minutes=5)
    assert made.broker().orders(LONG_AGO).orders == ()
    assert made.log.account_now.delayed == ()


@pytest.mark.parametrize("call", ["open_orders", "orders", "lookup"])
def test_12_a_read_past_the_clock_is_dated_by_the_later_of_it_and_the_newest_call(call) -> None:
    made, _ = sent(early=20)
    newest = made.clock.at
    made.decide(close_of(after(FULL)), recover=False)
    for clock in (newest - timedelta(hours=1), newest + timedelta(hours=1)):
        made.clock.at = clock
        found = READS[call](made.broker())
        dated = found.received_at if call == "lookup" else found.received[0].at
        assert dated == max(clock, newest)


@pytest.mark.parametrize("call", ["open_orders", "orders", "lookup"])
def test_12_a_read_of_the_orders_ends_an_order_that_needs_no_price(call: str) -> None:
    made, _ = sent(early=20, faults=Faults({ids(early=20)[0]: OrderFault.FILL_NONE}))
    made.clock.at = close_of(FULL) + MICRO
    READS[call](made.broker())
    assert made.log.orders["sim-o-1"].state is BrokerState.EXPIRED


@pytest.mark.parametrize("call", OF_ORDERS)
def test_12_a_hand_placed_order_stops_no_read_of_the_orders_for_a_price(call: str) -> None:
    made, _ = sent(early=20, faults=Faults(events={2: ForeignOrder(USO, D(3))}))
    made.store.drop(result_key(DEPLOYMENT, made.run, made.at, DATA_STAGE))
    READS[call](made.broker())
    assert [o.id for o in made.log.account_now.open_orders] == ["sim-o-1", "sim-o-2"]


def test_2_a_send_that_waits_on_a_price_takes_the_clock_again_after_it() -> None:
    made = world()
    at = close_of(FULL) - timedelta(minutes=20)
    (intent,) = made.decide(at, made.orders(at, [(4, GLD)]))
    broker = made.broker()
    result = result_key(DEPLOYMENT, made.run, at, DATA_STAGE)

    def hook(count: int, op: str, key: str) -> None:
        if key == result:
            made.clock.at = close_of(FULL) + timedelta(seconds=1)

    def waits() -> None:
        made.store.before = hook

    assert made.send(intent, RelayBroker(broker, waits)) is OrderState.REJECTED
    (entry,) = journaled(made, RejectedEntry)
    assert "it came at 2026-11-03T21:00:01.000000Z" in json.loads(entry.raw)["reason"]


@pytest.mark.parametrize(
    ("moved", "outcome"),
    [
        (-MICRO, Outcome.ACCEPTED),
        (timedelta(0), Outcome.ACCEPTED),
        (MICRO, Outcome.REJECTED),
    ],
)
def test_12_a_cancel_whose_clock_gives_no_time_is_judged_by_the_newest_call(moved, outcome) -> None:
    made, _ = sent(early=20, faults=Faults(events={2: EditCash(D(1))}))
    made.clock.at = close_of(FULL) + moved
    made.broker().account()
    made.clock.at = made.clock.at.replace(tzinfo=None)
    assert made.broker().cancel("sim-o-1").outcome is outcome


def test_12_a_cancel_on_an_account_just_opened_needs_a_clock() -> None:
    made = world()
    made.run, made.at = FULL.isoformat(), close_of(FULL)
    made.clock.at = close_of(FULL)
    assert made.broker().cancel("sim-o-1").outcome is Outcome.REJECTED
    made.clock.at = close_of(FULL).replace(tzinfo=None)
    with pytest.raises(Halt, match="sim-1's clock reads .*, no UTC time"):
        made.broker().cancel("sim-o-1")
    made.clock.now = lambda: 1 / 0  # type: ignore[assignment,method-assign,return-value]
    with pytest.raises(Halt, match="sim-1's clock reads None, no UTC time"):
        made.broker().cancel("sim-o-1")


def test_12_a_fast_clock_that_was_set_right_refuses_no_cancel() -> None:
    """A cancel is judged by the clock it reads, whatever time an earlier call stored."""
    made, _ = sent(early=20, faults=Faults(events={2: EditCash(D(1))}))
    made.clock.at = close_of(FULL) + timedelta(minutes=30)
    made.broker().account()
    assert made.broker().cancel("sim-o-1").outcome is Outcome.REJECTED
    made.clock.at = close_of(FULL) - timedelta(minutes=5)
    assert made.broker().cancel("sim-o-1").outcome is Outcome.ACCEPTED
    blob = json.loads(made.blobs()["sim/sim-1/000000000004.json"])
    assert blob["at"] == "2026-11-03T21:30:00.000000Z"
    assert blob["clock"] == "2026-11-03T20:55:00.000000Z"


@pytest.mark.parametrize("call", OF_ORDERS)
@pytest.mark.parametrize("why", ["clock", "decision", "price", "no-time", "raises"])
def test_12_no_clock_and_no_price_stops_a_read_of_the_orders(why: str, call: str) -> None:
    """A cancel is looked up first, so what stops a lookup stops the cancel."""
    made, _ = sent(early=20)
    at = made.clock.at
    if why == "clock":
        made.clock.at = at - MICRO
    elif why == "no-time":
        made.clock.at = at.replace(tzinfo=None)
    elif why == "raises":
        made.clock.now = lambda: 1 / 0  # type: ignore[assignment,method-assign,return-value]
    else:
        made.decide(close_of(after(FULL)), recover=False)
        if why == "decision":
            made.clock.at = at
        else:
            made.store.drop(result_key(DEPLOYMENT, made.run, made.at, DATA_STAGE))
    with pytest.raises(Halt):
        made.broker().positions()
    number, broker = made.log.number, made.broker()
    if call == "find_order":
        found = broker.find_order(order(1, 4, at=at), LONG_AGO, {})
        held, whole = found.rows, found.whole_list
    elif call == "lookup":
        held, whole = (broker.lookup("sim-o-1"),), True
    else:
        found = READS[call](broker)
        held, whole = found.orders, found.whole
    assert whole
    (one,) = [getattr(o, "order", o) for o in held]
    assert (one.broker_order_id, one.state) == ("sim-o-1", BrokerState.WORKING)
    assert made.log.number == number
    if call == "find_order":
        # What it finds carries no time of its own.
        return
    came = found.received[0].at if call != "lookup" else held[0].received_at
    # A good clock dates the answer, and the log's newest call does where the clock would halt.
    assert came == (close_of(after(FULL)) if why == "price" else at)
    assert made.log.at == at


def test_3_an_order_whose_prices_cannot_be_read_is_in_doubt_and_not_held() -> None:
    made = world()
    at = close_of(FULL) - timedelta(minutes=20)
    (intent,) = made.decide(at, made.orders(at, [(4, GLD)]))

    def lost() -> None:
        made.store.drop(result_key(DEPLOYMENT, made.run, at, DATA_STAGE))

    why = "in doubt: the simulated account sim-1 cannot read the prices of its order: .* not stored"
    with pytest.raises(Halt, match=why):
        made.send(intent, RelayBroker(made.broker(), lost))
    assert made.broker().orders(LONG_AGO).orders == ()


@pytest.mark.parametrize("call", OF_ORDERS)
def test_12_a_read_of_the_orders_asks_for_no_price(call: str) -> None:
    made, _ = sent(early=20)
    made.decide(close_of(after(FULL)), recover=False)
    made.store.calls.clear()
    READS[call](made.broker())
    assert all(key.startswith("sim/") for _, key in made.store.calls)
    # The close comes at the next call that reads a price.
    assert made.broker().positions().positions == {GLD: 4}
    assert made.broker().lookup("sim-o-1").order.state is BrokerState.FILLED


def test_12_a_read_of_the_orders_under_a_clock_that_went_back_is_cut_short_as_asked() -> None:
    made, _ = sent([(4, GLD), (5, USO)], faults=Faults(reads={("orders", 1): ReadFault.SHORT}))
    made.clock.at -= MICRO
    found = made.broker().orders(LONG_AGO)
    assert (len(found.orders), found.whole) == (1, False)


@pytest.mark.parametrize("call", OF_ORDERS)
def test_12_a_fault_that_cannot_happen_halts_a_read_of_the_orders_too(call: str) -> None:
    made, _ = sent(early=20, faults=Faults(events={2: EditOrder("sim-o-9", D(9))}))
    with pytest.raises(Halt, match="sim-o-9, which is not open"):
        READS[call](made.broker())


def test_12_a_read_of_the_orders_of_an_account_just_opened_needs_a_clock() -> None:
    made = world()
    made.run, made.at = FULL.isoformat(), close_of(FULL)
    made.clock.at = close_of(FULL).replace(tzinfo=None)
    with pytest.raises(Halt, match="sim-1's clock reads .*, no UTC time"):
        made.broker().open_orders()
    made.clock.now = lambda: 1 / 0  # type: ignore[assignment,method-assign,return-value]
    with pytest.raises(Halt, match="sim-1's clock reads None, no UTC time"):
        made.broker().open_orders()


def test_2_two_sessions_settled_in_one_call_each_fill_at_their_own_close() -> None:
    made = world()
    made.closes[GLD][after(FULL, 2)] = D(120)  # type: ignore[index]
    made.closes[USO][after(FULL, 2)] = D(1120)  # type: ignore[index]
    bars = made.closes[GLD].pop(FULL), made.closes[USO].pop(FULL)  # type: ignore[attr-defined]
    made.day(FULL, [(4, GLD)], early=20)
    made.day(after(FULL), [(3, GLD)])
    # The first session's bar comes late, so one call settles both.
    made.closes[GLD][FULL], made.closes[USO][FULL] = bars  # type: ignore[index]
    made.decide(close_of(after(FULL, 2)), recover=False)
    assert [(f.qty, f.price) for f in made.broker().fills(LONG_AGO).fills] == [
        (4, PRICE),
        (3, NEXT),
    ]


def test_2_an_order_sent_before_its_close_after_the_last_one_settled_is_official_close() -> None:
    """The close of the session before is read first, and says nothing of this session's."""
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    made.day(after(FULL), [(3, GLD)], early=20)
    assert [o.read_close for o in made.log.account_now.open_orders] == [False]
    made.closes[GLD][after(FULL, 2)] = D(120)  # type: ignore[index]
    made.closes[USO][after(FULL, 2)] = D(1120)  # type: ignore[index]
    made.decide(close_of(after(FULL, 2)), recover=False)
    assert made.broker().fills(LONG_AGO).fills[-1].basis is FillBasis.OFFICIAL_CLOSE


def test_6_an_order_edited_in_the_call_that_fills_it_fills_at_its_new_size() -> None:
    made, _ = sent(faults=Faults(events={2: EditOrder("sim-o-1", D(9))}))
    made.clock.at += MICRO
    assert made.broker().positions().positions == {GLD: 9}
    made.restart("d" * 32)
    assert made.broker().positions().positions == {GLD: 9}


@pytest.mark.parametrize("why", ["no-result", "stale", "lost"])
def test_12_no_price_stops_a_cancel_of_another_order(why: str) -> None:
    made = world()
    del made.closes[GLD][FULL]  # type: ignore[attr-defined]
    made.day(FULL, [(4, GLD)], early=20)
    at = close_of(after(FULL)) - timedelta(minutes=20)
    (other,) = made.decide(at, made.orders(at, [(5, USO)]))
    assert made.send(other) is OrderState.ACKNOWLEDGED
    # The first order is past its close, and its price can no longer be read.
    key = result_key(DEPLOYMENT, made.run, at, DATA_STAGE)
    if why == "no-result":
        made.store.drop(key)
    elif why == "stale":
        made.stored(made.snapshot(at, status={GLD: BarStatus.STALE, USO: BarStatus.FRESH}))
    else:
        bars = {USO: {"1d": made.bars(USO, at)}}
        made.stored(made.snapshot(at, bars=bars, status={USO: BarStatus.FRESH}))
    with pytest.raises(Halt, match="sim-1's order sim-o-1 is past its close and has no price: "):
        made.broker().positions()
    made.store.calls.clear()
    answer = made.broker().cancel("sim-o-2")
    assert (answer.outcome, answer.order.state) == (Outcome.ACCEPTED, BrokerState.CANCELLED)
    # It asks for no price at all.
    assert all(key.startswith("sim/") for _, key in made.store.calls)
    assert [o.id for o in made.log.account_now.open_orders] == ["sim-o-1"]
    # The order past its own close is refused, as no cancel can undo a close.
    assert made.broker().cancel("sim-o-1").outcome is Outcome.REJECTED


def test_12_a_clock_that_reads_no_utc_time_halts() -> None:
    made, _ = sent()
    made.clock.at = close_of(FULL).replace(tzinfo=None)
    with pytest.raises(Halt, match="the simulated account sim-1's clock is .*, not a UTC time"):
        made.broker().positions()


def test_12_an_order_under_a_clock_that_went_back_is_in_doubt_and_not_held() -> None:
    made, _ = sent()
    at = close_of(after(FULL))
    (intent,) = made.decide(at, made.orders(at, [(5, USO)]))
    held = made.log.number

    def moves() -> None:
        made.clock.at = at - MICRO

    with pytest.raises(Halt, match="in doubt: the clock reads"):
        made.send(intent, RelayBroker(made.broker(), moves))
    made.clock.at = at
    assert made.broker().open_orders().orders == ()
    assert made.log.number == held


def test_6_an_order_of_another_decision_than_the_prices_halts() -> None:
    made = world()
    at = close_of(FULL) - timedelta(minutes=20)
    (intent,) = made.decide(at, made.orders(at, [(4, GLD)]))
    made.at = at - timedelta(minutes=1)
    with pytest.raises(Halt) as raised:
        made.send(intent)
    assert raised.value.reason == (
        f"the order {intent.client_order_id} is in doubt: the order {intent.client_order_id} is of "
        "the decision at 2026-11-03T20:40:00.000000Z, and the prices of account sim-1 are of "
        "2026-11-03T20:39:00.000000Z"
    )
    made.at = at
    assert made.broker().orders(LONG_AGO).orders == ()


# 6. Each fault, with what the caller sees and what the account does


def journaled(made: SimWorld, kind: type) -> list:
    return [e for e in entries(made) if isinstance(e, kind)]


def holds(made: SimWorld, count: int) -> None:
    broker = made.broker()
    assert len(broker.open_orders().orders) == len(broker.orders(LONG_AGO).orders) == count
    assert broker.positions().positions == {}
    assert broker.account().cash == CASH
    assert broker.fills(LONG_AGO).fills == ()


def bought(made: SimWorld) -> None:
    """The order for 4 GLD filled whole at 12.50 and nothing else did."""
    broker = made.broker()
    assert broker.positions().positions == {GLD: 4}
    # 1,000,000 less 4 at 12.50.
    assert broker.account().cash == D("999950")
    assert broker.open_orders().orders == ()
    assert [(f.qty, f.price) for f in broker.fills(LONG_AGO).fills] == [(4, PRICE)]
    (order_,) = broker.orders(LONG_AGO).orders
    assert (order_.state, order_.filled) == (BrokerState.FILLED, 4)


def test_6_reject() -> None:
    made, states = sent(faults=Faults({ids()[0]: OrderFault.REJECT}))
    assert states == [OrderState.REJECTED]
    holds(made, 0)
    assert made.log.number == 1


def test_6_a_rejected_order_stores_what_was_due_with_it() -> None:
    faults = Faults({ids()[0]: OrderFault.REJECT}, events={1: EditCash(D(5))})
    made, states = sent(faults=faults)
    assert states == [OrderState.REJECTED]
    assert (made.log.number, made.log.account_now.cash) == (2, CASH + 5)


@pytest.mark.parametrize("fault", [OrderFault.BAD_TYPE, OrderFault.BAD_QTY])
def test_6_an_order_changed_as_it_enters_is_rejected_by_the_accounts_own_rule(fault) -> None:
    made, states = sent(faults=Faults({ids()[0]: fault}))
    assert states == [OrderState.REJECTED]
    (entry,) = journaled(made, RejectedEntry)
    why = {OrderFault.BAD_TYPE: "taken for cls", OrderFault.BAD_QTY: "zero shares"}[fault]
    assert why in json.loads(entry.raw)["reason"]
    holds(made, 0)


def test_6_lose_request() -> None:
    made, states = sent(faults=Faults({ids()[0]: OrderFault.LOSE_REQUEST}))
    assert states == [OrderState.IN_DOUBT]
    (entry,) = journaled(made, InDoubtEntry)
    assert (entry.reason, entry.raw) == ("the submit raised BrokerUnavailable", None)
    holds(made, 0)
    settled(made)
    assert made.broker().fills(LONG_AGO).fills == ()


def test_6_lose_answer() -> None:
    made, states = sent(faults=Faults({ids()[0]: OrderFault.LOSE_ANSWER}))
    assert states == [OrderState.IN_DOUBT]
    (entry,) = journaled(made, InDoubtEntry)
    assert entry.raw is None
    holds(made, 1)
    settled(made)
    bought(made)


def test_6_lose_answer_of_an_order_the_account_refuses() -> None:
    made = world(faults=Faults({ids(early=20)[0]: OrderFault.LOSE_ANSWER}))
    at = close_of(FULL) - timedelta(minutes=20)
    (intent,) = made.decide(at, made.orders(at, [(4, GLD)]))

    def moves() -> None:
        made.clock.at = close_of(FULL) + MICRO

    assert made.send(intent, RelayBroker(made.broker(), moves)) is OrderState.IN_DOUBT
    holds(made, 0)


@pytest.mark.parametrize("calls", [1, 2, 5])
def test_6_late_holds_the_order_from_its_call_on(calls: int) -> None:
    made, states = sent(early=20, faults=Faults({ids(early=20)[0]: Late(calls)}))
    assert states == [OrderState.IN_DOUBT]
    (entry,) = journaled(made, InDoubtEntry)
    assert entry.reason == "the submit raised BrokerUnavailable"
    broker = made.broker()
    made.clock.at += timedelta(seconds=3)
    seen = [len(broker.open_orders().orders) for _ in range(calls + 1)]
    assert seen == [0] * (calls - 1) + [1, 1]
    (held,) = broker.open_orders().orders
    assert (held.submitted_at, held.qty) == (made.clock.at, 4)
    made.decide(close_of(after(FULL)), recover=False)
    (fill,) = made.broker().fills(LONG_AGO).fills
    assert fill.basis is FillBasis.OFFICIAL_CLOSE
    bought(made)


def test_6_late_sent_at_the_close_is_signal_close_once_it_arrives() -> None:
    made, _ = sent(faults=Faults({ids()[0]: Late(1)}))
    assert len(made.broker().open_orders().orders) == 1
    settled(made)
    (fill,) = made.broker().fills(LONG_AGO).fills
    assert fill.basis is FillBasis.SIGNAL_CLOSE
    bought(made)


def test_6_the_id_of_an_order_that_arrived_too_late_is_refused_again() -> None:
    made, _ = sent(early=20, faults=Faults({ids(early=20)[0]: Late(1)}))
    made.clock.at = close_of(FULL) + MICRO
    holds(made, 0)
    second = world(sim_store=Plain(made.store.blobs), sim_writer="d" * 32)
    planned = second.decide(
        close_of(FULL) - timedelta(minutes=20),
        second.orders(close_of(FULL) - timedelta(minutes=20), [(7, GLD)]),
        recover=False,
    )
    to = second.broker()

    def moves() -> None:
        second.clock.at = close_of(FULL) + MICRO

    assert second.send(planned[0], RelayBroker(to, moves)) is OrderState.IN_DOUBT
    (entry,) = journaled(second, InDoubtEntry)
    assert "already holds an order under the id" in json.loads(entry.raw)["reason"]


def test_6_late_past_its_close_is_never_held() -> None:
    made, _ = sent(early=20, faults=Faults({ids(early=20)[0]: Late(1)}))
    made.clock.at = close_of(FULL) + MICRO
    holds(made, 0)
    number = made.log.number
    settled(made)
    holds(made, 0)
    # It is missed once, and is on its way no more.
    assert (made.log.number, made.log.account_now.delayed) == (number, ())


def test_6_late_counts_a_call_that_failed() -> None:
    faults = Faults({ids(early=20)[0]: Late(2)}, {("positions", 1): ReadFault.FAILS})
    made, _ = sent(early=20, faults=faults)
    with pytest.raises(BrokerUnavailable):
        made.broker().positions()
    assert len(made.broker().open_orders().orders) == 1


def test_6_late_up_to_its_close_is_held_and_survives_a_restart() -> None:
    made, _ = sent(early=20, faults=Faults({ids(early=20)[0]: Late(2)}))
    made.restart("d" * 32)
    made.clock.at = close_of(FULL)
    broker = made.broker()
    # The count starts again in the second process.
    assert [len(broker.open_orders().orders) for _ in range(3)] == [0, 1, 1]
    made.restart("e" * 32)
    assert len(made.broker().open_orders().orders) == 1


def test_6_late_with_no_fault_in_the_next_process_arrives_at_its_first_call() -> None:
    made, _ = sent(early=20, faults=Faults({ids(early=20)[0]: Late(9)}))
    made.faults = Faults()
    made.restart("d" * 32)
    assert len(made.broker().open_orders().orders) == 1


def test_6_an_order_after_one_on_its_way_takes_the_next_id() -> None:
    names = ids(early=20, count=2)
    made, states = sent([(4, GLD), (5, USO)], early=20, faults=Faults({names[0]: Late(2)}))
    assert states == [OrderState.IN_DOUBT, OrderState.ACKNOWLEDGED]
    broker = made.broker()
    assert [o.client_order_id for o in broker.orders(LONG_AGO).orders] == [names[1], names[0]]
    assert [o.broker_order_id for o in broker.orders(LONG_AGO).orders] == ["sim-o-1", "sim-o-2"]


@pytest.mark.parametrize("qty", [4, -4])
@pytest.mark.parametrize("part", [1, 3, 4, 9])
def test_6_fill_part_fills_that_much_and_expires_the_rest(qty: int, part: int) -> None:
    made, states = sent([(qty, GLD)], faults=Faults({ids()[0]: FillPart(D(part))}))
    assert states == [OrderState.ACKNOWLEDGED]
    holds(made, 1)
    settled(made)
    size = min(part, 4) * (1 if qty > 0 else -1)
    broker = made.broker()
    (fill,) = broker.fills(LONG_AGO).fills
    assert (fill.qty, fill.price) == (size, PRICE)
    (ended,) = broker.orders(LONG_AGO).orders
    assert ended.state is (BrokerState.FILLED if part >= 4 else BrokerState.EXPIRED)
    assert (ended.filled, ended.qty) == (size, qty)
    assert broker.positions().positions == {GLD: size}
    assert broker.account().cash == CASH - size * PRICE
    assert broker.open_orders().orders == ()
    made.agree()
    made.clock.at += timedelta(days=9)
    assert len(made.broker().fills(LONG_AGO).fills) == 1


def test_6_fill_none_expires_at_the_close() -> None:
    made, states = sent(faults=Faults({ids()[0]: OrderFault.FILL_NONE}))
    assert states == [OrderState.ACKNOWLEDGED]
    holds(made, 1)
    settled(made)
    broker = made.broker()
    assert broker.open_orders().orders == ()
    assert broker.fills(LONG_AGO).fills == ()
    (ended,) = broker.orders(LONG_AGO).orders
    assert (ended.state, ended.filled) == (BrokerState.EXPIRED, 0)
    assert json.loads(made.blobs()["sim/sim-1/000000000003.json"])["events"]["closed"] == [
        {
            "at": "2026-11-03T21:00:00.000000Z",
            "fill": None,
            "id": "sim-o-1",
            "priced_from": None,
            "state": "expired",
        }
    ]
    made.agree()


def test_6_reject_later_rejects_it_at_the_next_call() -> None:
    made, states = sent(early=20, faults=Faults({ids(early=20)[0]: OrderFault.REJECT_LATER}))
    assert states == [OrderState.ACKNOWLEDGED]
    assert made.log.account_now.open_orders[0].state is BrokerState.WORKING
    assert made.broker().open_orders().orders == ()
    (ended,) = made.broker().orders(LONG_AGO).orders
    assert (ended.state, ended.filled) == (BrokerState.REJECTED, 0)
    settled(made)
    assert made.broker().fills(LONG_AGO).fills == ()
    made.agree()


def test_6_duplicate_is_refused_by_the_accounts_own_check_and_the_first_held() -> None:
    made, states = sent(faults=Faults({ids()[0]: OrderFault.DUPLICATE}))
    assert states == [OrderState.IN_DOUBT]
    (entry,) = journaled(made, InDoubtEntry)
    assert entry.reason == "the broker's answer is unknown"
    assert "already holds an order" in json.loads(entry.raw)["reason"]
    holds(made, 1)
    settled(made)
    bought(made)


@pytest.mark.parametrize("fault", [None, Late(9)], ids=["held", "on-its-way"])
def test_6_an_id_the_account_has_had_is_refused_from_another_journal_too(fault) -> None:
    """Two journals over one account, which is the one way an id comes twice."""
    name = ids(early=20)[0]
    made, _ = sent(early=20, faults=Faults({name: fault} if fault else {}))
    second = world(faults=made.faults, sim_store=Plain(made.store.blobs), sim_writer="d" * 32)
    assert second.day(FULL, [(7, GLD)], early=20, recover=False) == [OrderState.IN_DOUBT]
    (entry,) = journaled(second, InDoubtEntry)
    assert "already holds an order under the id" in json.loads(entry.raw)["reason"]
    found = [*second.log.account_now.open_orders, *second.log.account_now.delayed]
    assert [(o.client_order_id, o.qty) for o in found if o.qty == 7] == []


@pytest.mark.parametrize(
    "read", ["positions", "open_orders", "account", "fills", "orders", "activities"]
)
def test_6_read_fails_at_its_count_alone_and_changes_nothing(read: str) -> None:
    made, _ = sent(faults=Faults(reads={(read, 2): ReadFault.FAILS}))
    made.clock.at += MICRO
    READS[read](made.broker())
    number = made.log.number
    with pytest.raises(BrokerUnavailable, match=f"the broker's {read} failed with TimeoutError"):
        READS[read](made.broker())
    assert made.log.number == number
    READS[read](made.broker())


def test_6_a_failed_read_settles_nothing() -> None:
    made, _ = sent(faults=Faults(reads={("positions", 1): ReadFault.FAILS}))
    made.clock.at += MICRO
    with pytest.raises(BrokerUnavailable):
        made.broker().positions()
    assert made.log.number == 2
    assert made.broker().positions().positions == {GLD: 4}


@pytest.mark.parametrize(
    "read", ["positions", "open_orders", "account", "fills", "orders", "activities"]
)
def test_6_read_short_is_not_whole_and_leaves_out_its_last_row(read: str) -> None:
    made, _ = sent([(4, GLD), (5, USO)], faults=Faults(reads={(read, 2): ReadFault.SHORT}))
    if read in ("positions", "fills"):
        made.clock.at += MICRO
    whole, short = READS[read](made.broker()), READS[read](made.broker())
    assert (whole.whole, short.whole) == (True, False)
    if read not in ("account", "activities"):
        rows = {"open_orders": "orders"}.get(read, read)
        assert len(_rows(whole, rows)) == 2
        assert _rows(short, rows) == _rows(whole, rows)[:1]
    assert READS[read](made.broker()) == whole


def _rows(read: object, name: str) -> list:
    held = {
        "positions": lambda r: sorted(r.positions.items()),
        "orders": lambda r: r.orders,
        "fills": lambda r: r.fills,
    }
    return list(held[name](read))


def test_6_a_short_list_makes_a_search_not_whole() -> None:
    made, _ = sent(faults=Faults(reads={("orders", 1): ReadFault.SHORT}))
    intent = order(1, 4, at=close_of(FULL))
    found = made.broker().find_order(intent, LONG_AGO, {})
    assert (found.answer, found.rows, found.whole_list) == (Outcome.NOT_FOUND, (), False)
    assert made.broker().find_order(intent, LONG_AGO, {}).whole_list


def test_6_a_lookup_can_fail() -> None:
    made, _ = sent(faults=Faults(reads={("lookup", 1): ReadFault.FAILS}))
    with pytest.raises(BrokerUnavailable, match="the broker's lookup failed"):
        made.broker().lookup("sim-o-1")
    assert made.broker().lookup("sim-o-1").outcome is Outcome.ACCEPTED


def test_6_the_hand_edits_move_the_account_at_their_call_and_once() -> None:
    edits = {
        2: EditCash(D("-7.5")),
        3: EditPosition(GLD, D(-3)),
        4: EditOrder("sim-o-1", D(9)),
    }
    made, _ = sent(early=20, faults=Faults(events=edits))
    assert made.log.calls == 1
    broker = made.broker()
    assert broker.account().cash == CASH - D("7.5")
    assert broker.positions().positions == {GLD: -3}
    assert broker.open_orders().orders[0].qty == 9
    # The journal's book knows nothing of them.
    assert made.view().book.cash == CASH
    made.decide(close_of(after(FULL)), recover=False)
    broker = made.broker()
    assert broker.positions().positions == {GLD: 6}
    # 1,000,000 less 7.50, less 9 at 12.50.
    assert broker.account().cash == D("999880")


def test_6_a_second_process_makes_the_edits_it_is_handed() -> None:
    made, _ = sent(early=20, faults=Faults(events={2: EditCash(D(5))}))
    assert made.broker().account().cash == CASH + 5
    made.restart("d" * 32)
    assert [made.broker().account().cash for _ in range(3)] == [CASH + 5, CASH + 10, CASH + 10]


def test_6_an_edit_between_two_reads() -> None:
    made, _ = sent(early=20, faults=Faults(events={4: EditCash(D(5))}))
    broker = made.broker()
    seen = [broker.account().cash for _ in range(4)]
    assert seen == [CASH, CASH, CASH + 5, CASH + 5]


def test_6_an_edit_whose_call_failed_comes_at_the_next() -> None:
    reads = {("account", 1): ReadFault.FAILS}
    made, _ = sent(early=20, faults=Faults(reads=reads, events={2: EditCash(D(5))}))
    broker = made.broker()
    with pytest.raises(BrokerUnavailable):
        broker.account()
    assert [broker.account().cash for _ in range(2)] == [CASH + 5] * 2


def test_6_an_edit_of_an_open_order_is_what_a_lookup_gives() -> None:
    made, _ = sent(early=20, faults=Faults(events={3: EditOrder("sim-o-1", D(9))}))
    broker = made.broker()
    assert [broker.open_orders().orders[0].qty for _ in range(3)] == [4, 9, 9]
    assert broker.lookup("sim-o-1").order.qty == 9


def test_6_an_edit_of_a_position_to_nothing_takes_it_off() -> None:
    made, _ = sent(faults=Faults(events={3: EditPosition(GLD, D(-4))}))
    made.clock.at += MICRO
    broker = made.broker()
    assert broker.positions().positions == {GLD: 4}
    assert broker.positions().positions == {}


def test_6_an_edit_of_an_order_that_is_not_open_halts() -> None:
    made, _ = sent(early=20, faults=Faults(events={2: EditOrder("sim-o-9", D(9))}))
    with pytest.raises(Halt, match="sim-o-9, which is not open"):
        made.broker().positions()


def test_6_a_foreign_order_carries_no_stamp_and_fills_as_any_other() -> None:
    made, _ = sent(early=20, faults=Faults(events={2: ForeignOrder(USO, D(-2))}))
    broker = made.broker()
    ours, theirs = broker.open_orders().orders
    assert (theirs.broker_order_id, theirs.client_order_id) == ("sim-o-2", None)
    assert (theirs.instrument, theirs.qty, theirs.state) == (USO, -2, BrokerState.WORKING)
    assert (theirs.type, theirs.time_in_force) == (OrderType.MARKET_ON_CLOSE, TimeInForce.CLS)
    assert ours.client_order_id == ids(early=20)[0]
    made.decide(close_of(after(FULL)), recover=False)
    fills = made.broker().fills(LONG_AGO).fills
    assert [(f.client_order_id, f.qty) for f in fills] == [(ids(early=20)[0], 4), (None, -2)]
    assert [f.basis for f in fills] == [FillBasis.OFFICIAL_CLOSE] * 2
    assert made.broker().positions().positions == {GLD: 4, USO: -2}
    # 1,000,000 less 4 at 12.50, plus 2 at 1,012.50.
    assert made.broker().account().cash == D("1001975")


def test_6_a_foreign_order_under_a_decision_at_the_close_reads_no_signal() -> None:
    made, _ = sent(faults=Faults(events={2: ForeignOrder(USO, D(-2))}))
    made.broker().positions()
    settled(made)
    fills = made.broker().fills(LONG_AGO).fills
    assert [f.basis for f in fills] == [FillBasis.SIGNAL_CLOSE, FillBasis.OFFICIAL_CLOSE]


def test_6_a_foreign_order_the_rule_refuses_halts() -> None:
    made, _ = sent(early=20, faults=Faults(events={2: ForeignOrder(USO, D(0))}))
    with pytest.raises(Halt, match="places an order that is refused: the order is for zero"):
        made.broker().positions()


def test_6_orders_that_come_in_one_call_take_ids_in_turn() -> None:
    names = ids(early=20, count=2)
    faults = Faults(
        orders={names[0]: Late(1)},
        events={2: ForeignOrder(USO, D(2)), 3: ForeignOrder(GLD, D(3))},
    )
    made, states = sent([(4, GLD), (5, USO)], early=20, faults=faults)
    assert states == [OrderState.IN_DOUBT, OrderState.ACKNOWLEDGED]
    found = made.broker().orders(LONG_AGO).orders
    assert [o.broker_order_id for o in found] == [f"sim-o-{n}" for n in range(1, 5)]
    assert [o.client_order_id for o in found] == [None, names[0], names[1], None]


def test_13_a_fault_that_means_nothing_is_refused_as_it_is_built() -> None:
    for bad in (
        lambda: Late(0),
        lambda: Late(True),
        lambda: FillPart(D(0)),
        lambda: FillPart(D(-1)),
        lambda: FillPart(D("NaN")),
        lambda: Faults(reads={("positions", 0): ReadFault.FAILS}),
        lambda: Faults(reads={("nothing", 1): ReadFault.FAILS}),
        lambda: Faults(reads={("lookup", 1): ReadFault.SHORT}),
        lambda: Faults(events={0: EditCash(D(1))}),
        lambda: Faults({"a": "reject"}),
        lambda: Faults({"a": ReadFault.FAILS}),
        lambda: Faults(reads={("positions", 1): "read_fails"}),
        lambda: Faults(events={1: "edit"}),
        lambda: EditCash(D("NaN")),
        lambda: EditPosition(GLD, D("Infinity")),
        lambda: EditOrder("sim-o-1", 3.5),
        lambda: EditOrder("sim-o-1", D(0)),
        lambda: ForeignOrder(GLD, D("NaN")),
    ):
        with pytest.raises(ValueError):
            bad()


def test_13_faults_are_read_only() -> None:
    orders, reads, events = {"a": OrderFault.REJECT}, {}, {1: EditCash(D(1))}
    faults = Faults(orders, reads, events)
    orders["b"], reads[("fills", 1)] = OrderFault.REJECT, ReadFault.FAILS
    events[2] = EditCash(D(2))
    assert (dict(faults.orders), dict(faults.reads)) == ({"a": OrderFault.REJECT}, {})
    assert dict(faults.events) == {1: EditCash(D(1))}
    for held in (faults.orders, faults.reads, faults.events):
        with pytest.raises(TypeError):
            held["b"] = None  # type: ignore[index]


def test_1_the_account_takes_no_order_around_the_gate() -> None:
    """A refused order never reaches the simulator, whose log stays as it was."""
    made = world()
    small = {GLD: D(3), USO: D(3)}
    made.constraints = gates.constraints(max_order_qty=small, max_position=small)
    made.restart()
    assert made.day(FULL, [(4, GLD)]) == [OrderState.REFUSED]
    assert made.log.number in (0, 1)
    assert journaled(made, AckEntry) == []
