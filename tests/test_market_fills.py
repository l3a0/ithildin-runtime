"""The simulated broker's market order for the day, filled at the decision's quote.

A test's number is the item of #155 it shows: 1 the fill and its rejections, 2
the strategy's `[execution]` key, which `tests/test_orders.py` holds, and 3 what
the fill traces to. Every order goes in through `OrderPath`, gate and journal
included. A quote with a fault never reaches the broker that way before the
close, because the gate's mark judges it first at the gate's own clock, so those
cases store the faulted quote, or move the clock, through a relay that runs
after the gate and before the broker's call.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    DATA_STAGE,
    SNAPSHOT_OUTPUT,
    BrokerState,
    FillBasis,
    Halt,
    OrderIntent,
    OrderState,
    OrderType,
    Outcome,
    Snapshot,
    TimeInForce,
    decode_output,
    output_key,
    quote_faults,
    result_key,
    sha256_of,
)
from ithildinruntime.execution.brokers.faults import Faults, FillPart, Late, OrderFault
from ithildinruntime.execution.brokers.simulated import REASON_LIMIT, refusal
from ithildinruntime.journal import AckEntry, FillEntry, Journal, RejectedEntry, record_of
from tests.support.order_world import order
from tests.support.quotes import quote
from tests.support.relay_broker import RelayBroker
from tests.support.sim_world import (
    ACCOUNT,
    CASH,
    DEPLOYMENT,
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
LONG_AGO = datetime.fromisoformat("2000-01-01T00:00:00+00:00")
# Twenty minutes before the close, where the world's snapshot reads a quote of 3 bid and
# 3.01 asked for each instrument, stamped 59 seconds before the decision under a maximum of 90.
AT = close_of(FULL) - timedelta(minutes=20)
BID, ASK = D(3), D("3.01")


def world(faults: Faults | None = None) -> SimWorld:
    prior = TABLE.before(FULL).day
    series = {TABLE.before(prior).day: D(1), prior: D("10.25"), FULL: D("12.5"), after(FULL): D(9)}
    return SimWorld(
        {GLD: series, USO: {d: p + 1000 for d, p in series.items()}}, faults or Faults()
    )


def market(place: int, qty: object, instrument: str = GLD, at: datetime = AT) -> OrderIntent:
    return order(place, qty, instrument, at=at, kind=OrderType.MARKET, tif=TimeInForce.DAY)


def name(place: int = 1, at: datetime = AT) -> str:
    return market(place, 1, at=at).client_order_id


def entries(made: SimWorld) -> list[object]:
    return [record_of(e) for e in Journal(Plain(made.store.blobs), ACCOUNT).read()]


def call(made: SimWorld, number: int) -> dict:
    return json.loads(made.blobs()[f"sim/sim-1/{number:012d}.json"])


def rejected(made: SimWorld) -> str:
    """The reason of the one order the broker rejected."""
    (entry,) = [e for e in entries(made) if isinstance(e, RejectedEntry)]
    return json.loads(entry.raw)["reason"]


def past_the_gate(
    made: SimWorld,
    intent: OrderIntent,
    quotes: dict | None = None,
    later: datetime | None = None,
) -> OrderState:
    """Send one order, and store other quotes or move the clock once the gate has passed it."""
    snapshot = made.snapshot(AT)

    def first() -> None:
        if quotes is not None:
            made.stored(replace(snapshot, quotes=quotes))
        if later is not None:
            made.clock.at = later

    return made.send(intent, RelayBroker(made.broker(), first))


def settled(made: SimWorld) -> None:
    """The decision of the session after, whose recover reads the fills."""
    made.decide(close_of(after(FULL)))


# 1. The fill


@pytest.mark.parametrize(("qty", "price"), [(4, ASK), (-4, BID)], ids=["a-buy", "a-sale"])
def test_1_a_market_order_fills_whole_at_once_at_the_ask_or_the_bid(qty: int, price) -> None:
    made = world()
    (intent,) = made.decide(AT, (market(1, qty),))
    made.clock.at = sent = AT + timedelta(seconds=2)
    assert made.send(intent) is OrderState.ACKNOWLEDGED

    # The answer already holds the order filled, in the call that took it.
    (ack,) = [e for e in entries(made) if isinstance(e, AckEntry)]
    copy = json.loads(ack.raw)
    assert (copy["state"], copy["filled"], copy["type"]) == ("filled", str(qty), "market")
    assert made.log.number == 2
    broker = made.broker()
    (fill,) = broker.fills(LONG_AGO).fills
    assert (fill.fill_id, fill.broker_order_id) == ("sim-f-1", "sim-o-1")
    assert (fill.client_order_id, fill.instrument) == (intent.client_order_id, GLD)
    assert (fill.qty, fill.price, fill.basis) == (qty, price, FillBasis.QUOTE)
    assert (fill.at, fill.received_at) == (sent, sent)
    assert fill.at_text == sent.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert (fill.correction_id, fill.excess_places) == (None, ())
    assert broker.positions().positions == {GLD: qty}
    # Cash moves exactly: 1,000,000 less 4 at 3.01, or plus 4 at 3.
    assert broker.account().cash == CASH - qty * price
    assert broker.open_orders().orders == ()
    (ended,) = broker.orders(LONG_AGO).orders
    assert (ended.state, ended.filled) == (BrokerState.FILLED, qty)

    settled(made)
    (journaled,) = [e for e in entries(made) if isinstance(e, FillEntry)]
    assert journaled.fill == fill
    assert made.view().orders[intent.client_order_id].state is OrderState.FILLED
    made.agree()
    # Nothing fills a second time, at the close or after it.
    made.clock.at = close_of(after(FULL)) + timedelta(days=3)
    assert len(made.broker().fills(LONG_AGO).fills) == 1


def test_1_two_legs_each_fill_at_their_own_quote() -> None:
    made = world()
    snapshot = made.snapshot(AT)
    quotes = dict(snapshot.quotes) | {USO: quote(USO, "70.1", "70.2", AT)}
    made.decide(
        AT, (buy := market(1, 2), sale := market(2, -3, USO)), replace(snapshot, quotes=quotes)
    )
    assert [made.send(o) for o in (buy, sale)] == [OrderState.ACKNOWLEDGED] * 2
    fills = made.broker().fills(LONG_AGO).fills
    assert [(f.instrument, f.qty, f.price) for f in fills] == [(GLD, 2, ASK), (USO, -3, D("70.1"))]
    assert made.broker().account().cash == CASH - 2 * ASK + 3 * D("70.1")


def test_1_a_quote_the_snapshot_lacks_is_a_rejection_naming_it() -> None:
    made = world()
    (intent,) = made.decide(AT, (market(1, 4, USO),))
    only = {GLD: made.snapshot(AT).quotes[GLD]}
    assert past_the_gate(made, intent, quotes=only) is OrderState.REJECTED
    assert rejected(made) == f"the decision's snapshot holds no quote of {USO}"
    assert made.blobs().keys() == {"sim/sim-1/000000000001.json"}


def test_1_at_the_close_the_gate_marks_at_the_bar_and_the_broker_rejects_the_order() -> None:
    """A decision at the close reads no quote, so the gate marks at the day's bar."""
    made = world()
    close = close_of(FULL)
    (intent,) = made.decide(close, (market(1, 4, at=close),))
    assert made.snapshot(close).quotes == {}
    assert made.send(intent) is OrderState.REJECTED
    assert rejected(made) == f"the decision's snapshot holds no quote of {GLD}"
    assert made.broker().fills(LONG_AGO).fills == ()


def test_1_the_gate_halts_first_on_a_leg_with_no_quote_before_the_close() -> None:
    """Its newest bar is the session V was taken at, so nothing marks it."""
    made = world()
    snapshot = made.snapshot(AT)
    only = {GLD: snapshot.quotes[GLD]}
    (intent,) = made.decide(AT, (market(1, 4, USO),), replace(snapshot, quotes=only))
    with pytest.raises(Halt, match=f"{USO} has no mark"):
        made.send(intent)
    assert made.blobs().keys() == {"sim/sim-1/000000000001.json"}


def faulted(bid: object = BID, ask: object = ASK, **changes: object) -> object:
    """GLD's quote at the decision at `bid` and `ask`, with `changes` made after it is built."""
    if changes.pop("no_ask", False):
        changes["ask"] = None
    return replace(quote(GLD, bid, ask, AT), **changes)


@pytest.mark.parametrize(
    ("given", "said"),
    [
        (faulted(D("3.02")), "its ask of 3.01 is below its bid of 3.02"),
        (faulted(BID, D("3.5")), "further apart than the written 0.02"),
        (faulted(D(0)), "its bid is 0"),
        (faulted(no_ask=True), "its ask is None"),
        (faulted(status="Halted"), "its status is 'Halted'"),
        (faulted(realtime=False), "its realtime flag"),
        (faulted(price_kind="unstated"), "unstated price kind"),
        (faulted(max_age=None), "maximum age of None"),
        (quote(GLD, BID, ASK, AT, age=timedelta(seconds=90)), "past the written maximum of 90"),
    ],
    ids=["crossed", "wide", "zero", "no-ask", "status", "delayed", "kind", "no-maximum", "aged"],
)
def test_1_a_quote_with_a_fault_is_a_rejection_and_never_an_exception(given, said: str) -> None:
    made = world()
    (intent,) = made.decide(AT, (market(1, 4),))
    assert past_the_gate(made, intent, quotes={GLD: given}) is OrderState.REJECTED
    (entry,) = [e for e in entries(made) if isinstance(e, RejectedEntry)]
    copy = json.loads(entry.raw)
    assert copy.keys() == {"reason"}
    assert copy["reason"].startswith(f"the quote of {GLD} cannot be read: ")
    assert said in copy["reason"]
    assert made.blobs().keys() == {"sim/sim-1/000000000001.json"}
    # Its id was never taken, so the account holds no order under it.
    assert made.broker().orders(LONG_AGO).orders == ()


def test_1_the_quote_is_judged_at_the_decision_time_and_not_at_the_clock() -> None:
    """Stamped 59 seconds before the decision, under a maximum of 90."""
    made = world()
    (intent,) = made.decide(AT, (market(1, 4),))
    # By the clock the quote is 59 + 600 seconds old, and the decision's time still judges it.
    later = AT + timedelta(minutes=10)
    assert past_the_gate(made, intent, later=later) is OrderState.ACKNOWLEDGED
    (fill,) = made.broker().fills(LONG_AGO).fills
    assert (fill.price, fill.at) == (ASK, AT + timedelta(minutes=10))


def test_1_a_long_reason_is_cut_to_its_bound() -> None:
    made = world()
    (intent,) = made.decide(AT, (market(1, 4),))
    long = faulted(status="x" * (3 * REASON_LIMIT))
    assert past_the_gate(made, intent, quotes={GLD: long}) is OrderState.REJECTED
    said = rejected(made)
    assert len(said) == REASON_LIMIT
    assert said.endswith(" (cut)")
    assert said.startswith(f"the quote of {GLD} cannot be read: its status is 'xxx")


@pytest.mark.parametrize("over", [0, 1], ids=["at-the-limit", "one-past-it"])
def test_1_a_reason_is_cut_only_past_its_limit(over: int) -> None:
    prefix = f"the quote of {GLD} cannot be read: "
    one = prefix + "; ".join(quote_faults(faulted(status="x"), AT, AT))
    status = "x" * (1 + REASON_LIMIT - len(one) + over)
    made = world()
    (intent,) = made.decide(AT, (market(1, 4),))
    assert past_the_gate(made, intent, quotes={GLD: faulted(status=status)}) is OrderState.REJECTED
    said = rejected(made)
    assert len(said) == REASON_LIMIT
    assert said.endswith(" (cut)") is bool(over)


def test_1_a_rejection_names_every_fault_of_the_quote() -> None:
    made = world()
    (intent,) = made.decide(AT, (market(1, 4),))
    two = faulted(D("3.02"), status="Halted")
    assert past_the_gate(made, intent, quotes={GLD: two}) is OrderState.REJECTED
    said = rejected(made)
    assert "its ask of 3.01 is below its bid of 3.02" in said
    assert "its status is 'Halted'" in said


def test_1_a_market_order_after_its_sessions_close_is_rejected() -> None:
    close = close_of(FULL)
    assert refusal(market(1, 4, at=close), close, close) is None
    assert "after its session's close" in (
        refusal(market(1, 4, at=close), close + MICRO, close) or ""
    )


def test_1_a_quote_with_more_places_than_a_fill_holds_is_kept_and_named() -> None:
    made = world()
    snapshot = made.snapshot(AT)
    fine = faulted(D("3.0000000001"), D("3.0100000001"))
    (intent,) = made.decide(AT, (market(1, 4),), replace(snapshot, quotes={GLD: fine}))
    assert made.send(intent) is OrderState.ACKNOWLEDGED
    (fill,) = made.broker().fills(LONG_AGO).fills
    assert (fill.price, fill.excess_places) == (D("3.0100000001"), ("price",))


# 1. The faults a market order meets


def test_1_fill_part_fills_that_much_at_once_and_expires_the_rest() -> None:
    made = world(Faults({name(): FillPart(D(1))}))
    (intent,) = made.decide(AT, (market(1, -4),))
    assert made.send(intent) is OrderState.ACKNOWLEDGED
    broker = made.broker()
    (fill,) = broker.fills(LONG_AGO).fills
    assert (fill.qty, fill.price) == (-1, BID)
    (ended,) = broker.orders(LONG_AGO).orders
    assert (ended.state, ended.filled) == (BrokerState.EXPIRED, -1)
    settled(made)
    assert made.view().orders[intent.client_order_id].state is OrderState.EXPIRED
    made.agree()


def test_1_fill_none_rests_and_expires_unfilled_at_the_close() -> None:
    made = world(Faults({name(): OrderFault.FILL_NONE}))
    (intent,) = made.decide(AT, (market(1, 4),))
    assert made.send(intent) is OrderState.ACKNOWLEDGED
    made.clock.at = close_of(FULL)
    (held,) = made.broker().open_orders().orders
    assert held.state is BrokerState.WORKING
    settled(made)
    broker = made.broker()
    assert (broker.open_orders().orders, broker.fills(LONG_AGO).fills) == ((), ())
    (ended,) = broker.orders(LONG_AGO).orders
    assert (ended.state, ended.filled) == (BrokerState.EXPIRED, 0)
    made.agree()


def test_1_reject_later_rests_and_is_rejected_at_the_next_call() -> None:
    made = world(Faults({name(): OrderFault.REJECT_LATER}))
    (intent,) = made.decide(AT, (market(1, 4),))
    assert made.send(intent) is OrderState.ACKNOWLEDGED
    assert made.log.account_now.open_orders[0].state is BrokerState.WORKING
    assert made.broker().open_orders().orders == ()
    (ended,) = made.broker().orders(LONG_AGO).orders
    assert (ended.state, ended.filled) == (BrokerState.REJECTED, 0)
    assert made.broker().fills(LONG_AGO).fills == ()


def test_1_bad_type_turns_a_market_order_into_a_pair_the_account_refuses() -> None:
    made = world(Faults({name(): OrderFault.BAD_TYPE}))
    (intent,) = made.decide(AT, (market(1, 4),))
    assert made.send(intent) is OrderState.REJECTED
    assert rejected(made) == "no market order is taken for cls"


def test_1_late_fills_as_it_arrives_under_its_own_decisions_prices() -> None:
    made = world(Faults({name(): Late(2)}))
    (intent,) = made.decide(AT, (market(1, 4),))
    assert made.send(intent) is OrderState.IN_DOUBT
    broker = made.broker()
    made.clock.at = arrived = AT + timedelta(seconds=3)
    assert [len(broker.fills(LONG_AGO).fills) for _ in range(2)] == [0, 1]
    (fill,) = broker.fills(LONG_AGO).fills
    assert (fill.price, fill.at, fill.basis) == (ASK, arrived, FillBasis.QUOTE)
    (ended,) = broker.orders(LONG_AGO).orders
    assert (ended.state, ended.submitted_at) == (BrokerState.FILLED, arrived)


def test_1_two_late_market_orders_arriving_in_one_call_take_their_own_fill_ids() -> None:
    made = world(Faults({name(1): Late(3), name(2): Late(2)}))
    buy, sale = made.decide(AT, (market(1, 4), market(2, -2, USO)))
    assert [made.send(o) for o in (buy, sale)] == [OrderState.IN_DOUBT] * 2
    broker = made.broker()
    # Both are due at the second call, so both arrive and fill in it.
    assert [len(broker.fills(LONG_AGO).fills) for _ in range(3)] == [0, 2, 2]
    fills = broker.fills(LONG_AGO).fills
    assert [(f.fill_id, f.instrument, f.qty) for f in fills] == [
        ("sim-f-1", GLD, 4),
        ("sim-f-2", USO, -2),
    ]


def test_1_late_arriving_under_another_decisions_prices_is_missed() -> None:
    made = world(Faults({name(): Late(3)}))
    (intent,) = made.decide(AT, (market(1, 4),))
    assert made.send(intent) is OrderState.IN_DOUBT
    # The next decision's broker is the one it arrives at, whose quote is of another snapshot.
    later = AT + timedelta(minutes=5)
    made.decide(later, recover=False)
    broker = made.broker()
    for _ in range(3):
        assert broker.orders(LONG_AGO).orders == ()
    assert made.log.account_now.delayed == ()
    assert broker.fills(LONG_AGO).fills == ()


def test_1_late_whose_quote_has_a_fault_is_missed() -> None:
    made = world(Faults({name(): Late(1)}))
    (intent,) = made.decide(AT, (market(1, 4),))
    halted = faulted(status="Halted")
    assert past_the_gate(made, intent, quotes={GLD: halted}) is OrderState.IN_DOUBT
    assert made.log.account_now.delayed != ()
    broker = made.broker()
    assert broker.fills(LONG_AGO).fills == ()
    assert broker.orders(LONG_AGO).orders == ()
    assert made.log.account_now.delayed == ()


def test_1_late_whose_snapshot_holds_no_quote_of_it_is_missed() -> None:
    made = world(Faults({name(): Late(1)}))
    (intent,) = made.decide(AT, (market(1, 4),))
    assert past_the_gate(made, intent, quotes={}) is OrderState.IN_DOUBT
    broker = made.broker()
    assert broker.fills(LONG_AGO).fills == ()
    assert broker.orders(LONG_AGO).orders == ()
    assert made.log.account_now.delayed == ()


def test_1_a_read_of_the_orders_misses_a_late_market_order_of_another_decision() -> None:
    """It reads no price to do so, and an order of another decision is never placed."""
    made = world(Faults({name(): Late(1)}))
    (intent,) = made.decide(AT, (market(1, 4),))
    assert made.send(intent) is OrderState.IN_DOUBT
    made.decide(AT + timedelta(minutes=5), recover=False)
    assert made.log.account_now.delayed != ()
    assert made.broker().open_orders().orders == ()
    assert made.log.account_now.delayed == ()


@pytest.mark.parametrize("read", ["open_orders", "orders", "lookup"])
def test_1_a_read_of_the_orders_reads_no_price_and_leaves_a_late_market_order_on_its_way(
    read: str,
) -> None:
    """A cancel is looked up first, so a price that stops a lookup would stop the cancel."""
    made = world(Faults({name(): Late(1)}))
    (intent,) = made.decide(AT, (market(1, 4),))
    assert made.send(intent) is OrderState.IN_DOUBT
    made.store.drop(result_key(DEPLOYMENT, made.run, AT, DATA_STAGE))
    made.store.calls.clear()
    broker = made.broker()
    reads = {
        "open_orders": lambda: broker.open_orders().orders,
        "orders": lambda: broker.orders(LONG_AGO).orders,
        "lookup": lambda: broker.lookup("sim-o-1").outcome,
    }
    assert reads[read]() in ((), Outcome.NOT_FOUND)
    assert all(key.startswith("sim/") for _, key in made.store.calls)
    assert [o.client_order_id for o in made.log.account_now.delayed] == [name()]
    # The next call that reads a price needs the quote, and halts while it cannot be read.
    with pytest.raises(Halt, match="cannot read the quote of its order: .* not stored"):
        broker.positions()


def test_1_a_snapshot_that_cannot_be_read_puts_the_order_in_doubt_and_holds_none() -> None:
    made = world()
    (intent,) = made.decide(AT, (market(1, 4),))

    def lost() -> None:
        made.store.drop(result_key(DEPLOYMENT, made.run, AT, DATA_STAGE))

    why = "in doubt: the simulated account sim-1 cannot read the quote of its order: .* not stored"
    with pytest.raises(Halt, match=why):
        made.send(intent, RelayBroker(made.broker(), lost))
    assert made.log.number == 1


def test_1_a_market_order_still_working_at_its_close_never_fills_there() -> None:
    """Under `fill_none` it rests, and a process with no fault then reads the close."""
    made = world(Faults({name(): OrderFault.FILL_NONE}))
    (intent,) = made.decide(AT, (market(1, 4),))
    assert made.send(intent) is OrderState.ACKNOWLEDGED
    made.faults = Faults()
    made.restart()
    settled(made)
    broker = made.broker()
    assert broker.fills(LONG_AGO).fills == ()
    (ended,) = broker.orders(LONG_AGO).orders
    assert (ended.state, ended.filled) == (BrokerState.EXPIRED, 0)
    assert broker.positions().positions == {}
    # The log records its end at the session's close, not at the call that saw it.
    closes = [
        c
        for number in range(2, made.log.number + 1)
        for c in call(made, number)["events"]["closed"]
    ]
    assert [(c["id"], c["state"], c["at"], c["fill"]) for c in closes] == [
        ("sim-o-1", "expired", "2026-11-03T21:00:00.000000Z", None)
    ]


# 3. What the fill traces to


def test_3_a_fill_names_the_decisions_snapshot_whose_quote_gave_its_price() -> None:
    made = world()
    (intent,) = made.decide(AT, (market(1, 4),))
    assert made.send(intent) is OrderState.ACKNOWLEDGED
    (close,) = call(made, 2)["events"]["closed"]
    key = output_key(DEPLOYMENT, FULL.isoformat(), AT, DATA_STAGE, SNAPSHOT_OUTPUT)
    data = made.store.blobs[key]
    assert close["priced_from"] == {"key": key, "sha256": sha256_of(data)}
    assert close["fill"]["basis"] == "quote"
    (snapshot,) = decode_output(data, Snapshot).entries
    held = snapshot.quotes[GLD]
    assert D(close["fill"]["price"]) == held.ask
    # Everything #46 traces a quote by is in the snapshot the fill names.
    assert snapshot.quotes_read_at == AT
    for field in ("bid_at", "ask_at", "fetched_at", "captured_at"):
        assert getattr(held, field) is not None, field
    assert held.received["ask"] == "3.01"
