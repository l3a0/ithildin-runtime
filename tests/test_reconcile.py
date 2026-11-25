"""`core.reconcile` by hand, as acceptance 4 of #145 names the cases.

Every figure is made up. Each case changes one side of a book and a broker that
agree, so a difference has the one cause the case gives it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    AccountRead,
    Book,
    BrokerOrder,
    BrokerState,
    CashRead,
    Compared,
    Difference,
    FillsRead,
    InstrumentId,
    OrderIntent,
    OrderRecord,
    OrdersRead,
    OrderState,
    OrderType,
    PositionsRead,
    ReceivedPage,
    TimeInForce,
    order_id,
    reconcile,
)
from tests.support.gates import DECISION, GLD, SPY, USO
from tests.support.gates import d as D

STEP = Decimal("0.000000001")
CENT = Decimal("0.0001")
PAGES = (ReceivedPage(b"[]", DECISION),)
SECRET = "PKFAKEKEY and more"


def record(place: int = 1, broker: str | None = "b-1", **changes: object) -> OrderRecord:
    intent = OrderIntent(
        order_id("chan", DECISION, place),
        "chan",
        GLD,
        D(10),
        OrderType.MARKET_ON_CLOSE,
        TimeInForce.CLS,
    )
    fields: dict[str, object] = {
        "intent": replace(intent, **changes.pop("intent", {})),  # type: ignore[arg-type]
        "decision": 3,
        "state": OrderState.ACKNOWLEDGED,
        "broker_order_id": broker,
        "filled": D(0),
        "entries": (),
        "sent_at": DECISION,
    }
    return OrderRecord(**fields | changes)  # type: ignore[arg-type]


def copy(order: OrderRecord, **changes: object) -> BrokerOrder:
    intent = order.intent
    fields: dict[str, object] = {
        "broker_order_id": order.broker_order_id,
        "client_order_id": intent.client_order_id,
        "instrument": intent.instrument,
        "qty": intent.qty,
        "filled": order.filled,
        "type": intent.type,
        "time_in_force": intent.time_in_force,
        "state": BrokerState.WORKING,
        "submitted_at": DECISION,
    }
    return BrokerOrder(**fields | changes)  # type: ignore[arg-type]


def book(
    held: Mapping[str, Mapping[InstrumentId, object]], orders: tuple[OrderRecord, ...] = ()
) -> Book:
    left = tuple(replace(o.intent, qty=o.intent.qty - o.filled) for o in orders)
    return Book(
        positions_by_strategy={s: {i: D(q) for i, q in by.items()} for s, by in held.items()},
        open_orders_by_strategy={"chan": left} if left else {},
        cash=D("1000.5"),
        replayed_through=9,
        in_doubt=(),
        corrections=(),
    )


def read(
    held: Mapping[InstrumentId, object],
    orders: tuple[BrokerOrder, ...] = (),
    cash: object = "1000.5",
) -> AccountRead:
    return AccountRead(
        positions=PositionsRead({i: D(q) for i, q in held.items()}, PAGES, True),
        open_orders=OrdersRead(orders, PAGES, True),
        cash=CashRead(D(cash), PAGES, True),
        since=DECISION - timedelta(days=1),
        fills=FillsRead((), PAGES, True),
    )


def found(difference: Difference) -> tuple:
    return (difference.of, difference.name, difference.fields)


def test_4_a_book_and_a_broker_that_agree_have_no_difference() -> None:
    order = record(filled=D(4), state=OrderState.PARTLY_FILLED)
    held = {"chan": {GLD: 14, USO: -3}}
    assert reconcile(book(held, (order,)), (order,), read({GLD: 14, USO: -3}, (copy(order),))) == ()
    assert reconcile(book({}), (), read({})) == ()


@pytest.mark.parametrize(
    ("held", "theirs", "expected"),
    [
        # One step either side of equal, long and short.
        ({"chan": {GLD: 5}}, {GLD: 5 + STEP}, [(GLD, D(5), 5 + STEP)]),
        ({"chan": {GLD: 5}}, {GLD: 5 - STEP}, [(GLD, D(5), 5 - STEP)]),
        ({"chan": {USO: -5}}, {USO: -5 - STEP}, [(USO, D(-5), -5 - STEP)]),
        ({"chan": {USO: -5}}, {USO: 5}, [(USO, D(-5), D(5))]),
        ({"chan": {USO: -5}}, {USO: -5}, []),
        # The same number written two ways.
        ({"chan": {GLD: "5.0"}}, {GLD: "5.000"}, []),
        # An instrument on one side alone.
        ({}, {SPY: 1}, [(SPY, D(0), D(1))]),
        ({"chan": {SPY: 1}}, {}, [(SPY, D(1), None)]),
        ({}, {SPY: -1}, [(SPY, D(0), D(-1))]),
        ({"chan": {SPY: STEP}}, {}, [(SPY, STEP, None)]),
        # A zero position, on either side and on both.
        ({"chan": {GLD: 0}}, {}, []),
        ({}, {GLD: 0}, []),
        ({"chan": {GLD: 0}}, {GLD: 0}, []),
        ({"chan": {GLD: 0}}, {GLD: 1}, [(GLD, D(0), D(1))]),
        # A strategy pair that nets to the account, and to nothing.
        ({"chan": {GLD: 5}, "other": {GLD: -3}}, {GLD: 2}, []),
        ({"chan": {GLD: 5}, "other": {GLD: -5}}, {}, []),
        ({"chan": {GLD: 5}, "other": {GLD: -3}}, {GLD: 5}, [(GLD, D(2), D(5))]),
        ({"chan": {GLD: 5}, "other": {GLD: -5}}, {GLD: 5}, [(GLD, D(0), D(5))]),
        # Sorted by instrument.
        (
            {"chan": {USO: 1, GLD: 1}},
            {SPY: 1},
            [(GLD, D(1), None), (SPY, D(0), D(1)), (USO, D(1), None)],
        ),
    ],
)
def test_4_positions(held, theirs, expected) -> None:
    given = reconcile(book(held), (), read(theirs))
    assert [(x.name, x.book, x.broker) for x in given] == expected
    assert {x.of for x in given} <= {Compared.POSITION}


@pytest.mark.parametrize("by", [CENT, -CENT])
def test_4_cash_one_step_either_side_of_equal(by: Decimal) -> None:
    (one,) = reconcile(book({}), (), read({}, cash=D("1000.5") + by))
    assert found(one) == (Compared.CASH, "", ())
    assert (one.book, one.broker) == (D("1000.5"), D("1000.5") + by)
    assert str(one) == f"cash is 1000.5 in the book and {D('1000.5') + by} at the broker"
    assert reconcile(book({}), (), read({}, cash="1000.5000")) == ()


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"instrument": USO}, "instrument"),
        ({"qty": D(10) + STEP}, "shares"),
        ({"qty": D(10) - STEP}, "shares"),
        ({"qty": D(-10)}, "shares"),
        ({"filled": D(4) + STEP}, "filled"),
        ({"filled": D(4) - STEP}, "filled"),
        ({"filled": D(0)}, "filled"),
        ({"client_order_id": order_id("chan", DECISION, 2)}, "client order id"),
        ({"client_order_id": None}, "client order id"),
    ],
)
def test_4_an_open_order_that_differs_in_one_field(changes: dict, field: str) -> None:
    order = record(filled=D(4), state=OrderState.PARTLY_FILLED)
    theirs = copy(order, **changes)
    (one,) = reconcile(book({}, (order,)), (order,), read({}, (theirs,)))
    assert found(one) == (Compared.OPEN_ORDER, "b-1", (field,))
    assert (one.book, one.broker) == (order, theirs)


def test_4_an_open_order_that_differs_in_every_field_names_each() -> None:
    order = record()
    theirs = copy(order, instrument=USO, qty=D(-1), filled=D(-1), client_order_id=None)
    (one,) = reconcile(book({}, (order,)), (order,), read({}, (theirs,)))
    assert one.fields == ("instrument", "shares", "filled", "client order id")
    assert str(one) == (
        f"the order {order.intent.client_order_id}, the broker's b-1, differs in instrument (GLD "
        "in the book and USO at the broker), shares (10 in the book and -1 at the broker), filled "
        f"(0 in the book and -1 at the broker), client order id ({order.intent.client_order_id} "
        "in the book and no client order id at the broker)"
    )


def test_4_an_open_order_on_one_side_alone() -> None:
    order = record()
    (ours,) = reconcile(book({}, (order,)), (order,), read({}))
    assert (ours.name, ours.book, ours.broker) == ("b-1", order, None)
    assert str(ours) == (
        f"the order {order.intent.client_order_id}, the broker's b-1, is open in the book alone, "
        "Acknowledged"
    )
    foreign = copy(order, client_order_id=None)
    (theirs,) = reconcile(book({}), (), read({}, (foreign,)))
    assert (theirs.name, theirs.book, theirs.broker) == ("b-1", None, foreign)
    assert str(theirs) == (
        "the broker's order b-1, 10 of GLD under no client order id, is open at the broker alone"
    )


def test_4_two_orders_that_net_are_each_compared() -> None:
    """A netted total agrees where each order does not."""
    buy, sell = record(1, "b-1"), record(2, "b-2", intent={"qty": D(-10)})
    swapped = (copy(buy, qty=D(-10)), copy(sell, qty=D(10)))
    given = reconcile(book({}, (buy, sell)), (buy, sell), read({}, swapped))
    assert [found(x) for x in given] == [
        (Compared.OPEN_ORDER, "b-1", ("shares",)),
        (Compared.OPEN_ORDER, "b-2", ("shares",)),
    ]
    gone = reconcile(book({}, (buy, sell)), (buy, sell), read({}))
    assert [x.name for x in gone] == ["b-1", "b-2"]


def test_4_an_order_the_broker_lists_twice() -> None:
    order = record()
    given = reconcile(book({}, (order,)), (order,), read({}, (copy(order), copy(order))))
    assert [found(x) for x in given] == [(Compared.OPEN_ORDER, "b-1", ("listed twice",))]
    assert str(given[0]) == "the broker lists its order b-1 more than once"
    (alone,) = reconcile(book({}), (), read({}, (copy(order), copy(order))))
    assert alone.fields == ("listed twice",)


@pytest.mark.parametrize("state", [OrderState.APPROVED, OrderState.SENDING, OrderState.IN_DOUBT])
def test_4_an_order_with_no_brokers_id_is_a_difference(state: OrderState) -> None:
    order = record(broker=None, state=state)
    (one,) = reconcile(book({}, (order,)), (order,), read({}))
    name = order.intent.client_order_id
    assert (one.name, one.book, one.broker) == (name, order, None)
    assert str(one) == f"the order {name} is open in the book alone, {state}"


def test_4_an_open_order_of_the_book_with_no_record_handed_in_is_a_difference() -> None:
    """A caller that hands in too few records never reads as agreement."""
    order = record()
    given = reconcile(book({}, (order,)), (), read({}))
    name = order.intent.client_order_id
    assert [found(x) for x in given] == [(Compared.OPEN_ORDER, name, ())]
    assert str(given[0]) == (
        f"the order {name} is open in the book and no record of it was handed in"
    )


def test_4_every_difference_at_once_sorted() -> None:
    kept, gone = record(1, "b-3"), record(2, "b-1")
    foreign = copy(record(3, "b-2"), client_order_id=SECRET)
    given = reconcile(
        book({"chan": {USO: 1, GLD: 2}}, (kept, gone)),
        (kept, gone),
        read({USO: 2, SPY: 1, GLD: 2}, (foreign, copy(kept, qty=D(9))), cash=0),
    )
    assert [found(x) for x in given] == [
        (Compared.POSITION, SPY, ()),
        (Compared.POSITION, USO, ()),
        (Compared.OPEN_ORDER, "b-1", ()),
        (Compared.OPEN_ORDER, "b-2", ()),
        (Compared.OPEN_ORDER, "b-3", ("shares",)),
        (Compared.CASH, "", ()),
    ]


def test_10_a_difference_holds_no_text_of_the_brokers() -> None:
    order = record()
    odd = copy(order, instrument=InstrumentId(SECRET), client_order_id=SECRET)
    alone = copy(record(2, SECRET), instrument=InstrumentId(SECRET), client_order_id=SECRET)
    given = reconcile(
        book({}, (order,)),
        (order,),
        read({InstrumentId(SECRET): 1, InstrumentId("X" * 41): 1}, (odd, alone)),
    )
    said = [str(x) for x in given]
    assert not [text for text in said if "PKFAKE" in text or "XX" in text]
    assert said == [
        "an instrument whose name is none is 0 in the book and 1 at the broker",
        "an instrument whose name is none is 0 in the book and 1 at the broker",
        "the broker's order whose id the journal refuses, 10 of an instrument whose name is none "
        "under a client order id that is no stamp of the platform's, is open at the broker alone",
        f"the order {order.intent.client_order_id}, the broker's b-1, differs in instrument (GLD "
        "in the book and an instrument whose name is none at the broker), client order id "
        f"({order.intent.client_order_id} in the book and a client order id that is no stamp of "
        "the platform's at the broker)",
    ]
