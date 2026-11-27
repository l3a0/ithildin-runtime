"""An order's states, the checks on each entry, and the view and the book they fold to."""

from __future__ import annotations

import dataclasses
import random
import threading
import time
import tracemalloc
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    Fill,
    Halt,
    OrderEntry,
    OrderState,
    OrderType,
    StoreUnavailable,
    TimeInForce,
    encode,
    journal_entry_key,
)
from ithildinruntime.journal import (
    FIRST_PREV_HASH,
    KINDS,
    Dispute,
    IntentEntry,
    Journal,
    NumberTaken,
    Record,
    SendingEntry,
    body_of,
    kind_of,
)
from ithildinruntime.journal.book import after
from ithildinruntime.journal.orders import DOUBTFUL, ENDED, MOVES, NOT_SENT, OPEN, Move
from tests.support.journals import (
    ACCOUNT,
    AT,
    CHAN,
    GLD,
    LATE,
    OTHER,
    USO,
    WRITER,
    Memory,
    abandoned,
    ack,
    add,
    decision,
    disputed,
    expired,
    fill,
    filled,
    in_doubt,
    lost,
    opening,
    placed,
    planned,
    read,
    rejected,
    route,
    shown,
    started,
    verdict,
)

DAY = timedelta(days=1)


def refuses(journal: Journal, record: Record, match: str) -> None:
    """`record` stores nothing and halts, and a replay of the same entry halts the same way."""
    record = placed(journal, record)
    view, before = journal.view(), dict(journal._store.blobs)
    was = shown(view) if view.through.seq and not (view.disputes or view.unmatched) else None
    with pytest.raises(Halt, match=match) as at_record:
        journal.record(view, record, AT)
    assert journal._store.blobs == before
    assert journal.view() is view
    if was is not None:
        assert shown(view) == was
    number = add(journal, record)
    for reader in (journal, journal, Journal(journal._store, ACCOUNT)):
        with pytest.raises(Halt, match=match) as at_replay:
            reader.view()
        assert at_replay.value.reason == at_record.value.reason
    assert f"entry {number}" in at_record.value.reason
    # `record`'s own reader of a bad entry, and the fold, name the account once.
    assert at_record.value.reason.startswith(f"journal {ACCOUNT}: entry {number}")


def accepts(journal: Journal, record: Record):
    """`record` stores the entry, and a replay of it shows what `record` gave."""
    before = len(journal._store.blobs)
    _, view = journal.record(journal.view(), placed(journal, record), AT)
    assert len(journal._store.blobs) == before + 1
    again = Journal(journal._store, ACCOUNT).view()
    assert again.through == view.through == journal.view().through
    if view.disputes or view.unmatched:
        held = ("orders", "disputes", "unmatched", "records", "_cash")
        assert [getattr(again, name) for name in held] == [getattr(view, name) for name in held]
    else:
        assert shown(again) == shown(view) == shown(journal.view())
    return view


def at(state: str, qty: object = 4, closing: bool | None = None) -> tuple[Journal, str]:
    """A journal holding one order brought to `state`, a closing-auction one where `closing`.

    Only a closing-auction order can be given up as lost, so a `Lost` route takes one.
    """
    order = planned(1, qty)
    if state == "Lost" if closing is None else closing:
        order = dataclasses.replace(
            order, type=OrderType.MARKET_ON_CLOSE, time_in_force=TimeInForce.CLS
        )
    journal = started(order)
    for record in route(state, order):
        add(journal, record)
    journal.view()
    return journal, order.client_order_id


# Acceptance 3: every state against every kind. The lists here are #142's item 8
# written out, and share nothing with the table in `journal/orders.py`.

ALLOWED = {
    ("intent", "none"): "Intended",
    ("approved", "Intended"): "Approved",
    ("refused", "Intended"): "Refused",
    ("abandoned", "Intended"): "Abandoned",
    ("abandoned", "Approved"): "Abandoned",
    ("abandoned", "Sending"): "Abandoned",
    ("sending", "Approved"): "Sending",
    ("ack", "Sending"): "Acknowledged",
    ("ack", "InDoubt"): "Acknowledged",
    ("rejected", "Sending"): "Rejected",
    ("rejected", "Acknowledged"): "Rejected",
    ("in_doubt", "Sending"): "InDoubt",
    ("in_doubt", "InDoubt"): "InDoubt",
    ("lost", "InDoubt"): "Lost",
    ("part", "Acknowledged"): "PartlyFilled",
    ("part", "PartlyFilled"): "PartlyFilled",
    ("whole", "Acknowledged"): "Filled",
    ("whole", "PartlyFilled"): "Filled",
    ("expired", "Acknowledged"): "Expired",
    ("expired", "PartlyFilled"): "Expired",
    # A dispute moves no state, and is allowed wherever its fill was applied.
    ("disputed_fill", "PartlyFilled"): "PartlyFilled",
    ("disputed_fill", "Filled"): "Filled",
    ("disputed_fill", "Expired"): "Expired",
}
ROUTES = (
    "none",
    "Intended",
    "Refused",
    "Approved",
    "Abandoned",
    "Abandoned late",
    "Sending",
    "InDoubt",
    "Lost",
    "Acknowledged",
    "PartlyFilled",
    "Filled",
    "Rejected",
    "Rejected late",
    "Expired",
    "Expired unfilled",
)
ACTS = (
    "intent",
    "approved",
    "refused",
    "abandoned",
    "sending",
    "ack",
    "rejected",
    "in_doubt",
    "lost",
    "part",
    "whole",
    "expired",
    "disputed_fill",
)
# The routes that gave the order a broker's id, which a fill names it by.
MAPPED = {"Acknowledged", "PartlyFilled", "Filled", "Rejected late", "Expired", "Expired unfilled"}


def test_3_the_list_names_every_state_and_every_kind_of_an_order() -> None:
    assert {route_.split()[0] for route_ in ROUTES} - {"none"} == {s.value for s in OrderState}
    assert {a for a, _ in ALLOWED} == set(ACTS)
    names = {"approved": "verdict", "refused": "verdict", "part": "fill", "whole": "fill"}
    # A broker's record and what the platform did not send move no order of the platform's.
    unowned = {"outside_record", "unmatched_record", "unmatched_fill", "unmatched_order"}
    # The loss stop's kinds move no order either, and tests/test_latch.py folds them.
    stop = {"stop_fired", "stop_stored", "stop_cleared"}
    # A takeover names late orders and moves none, and tests/test_order_lease.py folds it.
    assert {names.get(act, act) for act in ACTS} == set(KINDS) - {
        "opening",
        "read",
        "decision",
        "takeover",
        *unowned,
        *stop,
    }


@pytest.mark.parametrize("route_", ROUTES)
@pytest.mark.parametrize("act", ACTS)
def test_3_every_state_against_every_kind(route_: str, act: str) -> None:
    journal, name = at(route_, closing=route_ == "Lost" or act == "lost")
    state = route_.split()[0]
    # A `lost` names a list asked for once the order's time in force ran out.
    late = add(journal, read(LATE)) if act == "lost" else 0
    before = journal.view()
    done = before.orders[name].filled if name in before.orders else Decimal(0)
    record = {
        "intent": IntentEntry(3, planned(1, 4)),
        "approved": verdict(name),
        "refused": verdict(name, approved=False),
        "abandoned": abandoned(name),
        "sending": SendingEntry(name),
        "ack": ack(name, "b9"),
        "rejected": rejected(name),
        "in_doubt": in_doubt(name),
        "lost": lost(name, late),
        "part": filled(fill("new", "b1", 1)),
        "whole": filled(fill("new", "b1", 4 - done or 1)),
        "expired": expired(name, done),
        "disputed_fill": disputed(fill("b1-f1", "b1", 2, "10.26")),
    }[act]
    end = None if route_ == "Expired unfilled" and act == "disputed_fill" else ALLOWED.get(
        (act, state)
    )  # fmt: skip
    if end is None:
        if act == "disputed_fill":
            match = "disputes the fill b1-f1, which no entry applied"
        elif act in ("part", "whole") and route_ not in MAPPED:
            match = "of the broker's order b1, which no order was acknowledged under"
        else:
            stands = "has no entry" if state == "none" else f"is {state}"
            match = f"of the order {name}, which {stands}$"
        refuses(journal, record, match)
        return
    view = accepts(journal, record)
    assert view.orders[name].state.value == end
    kinds = [entry.kind for entry in view.orders[name].entries]
    moved = len(before.orders[name].entries) if name in before.orders else 0
    if act == "disputed_fill":
        assert view.orders[name] == before.orders[name]
        assert view.disputes == (Dispute(view.through.seq, record.fill),)
    else:
        kind = {"approved": "verdict", "refused": "verdict", "part": "fill", "whole": "fill"}
        assert kinds[moved:] == [kind.get(act, act)]
        assert view.orders[name].entries[-1].seq == view.through.seq


def test_3_the_table_holds_no_other_move() -> None:
    """The table's own size, so a move added with no case above is seen."""
    assert len(MOVES) == len([pair for pair in ALLOWED if pair[0] != "disputed_fill"])
    assert {move for move, _ in MOVES} == set(Move)
    assert OPEN == {
        OrderState.APPROVED,
        OrderState.SENDING,
        OrderState.IN_DOUBT,
        OrderState.ACKNOWLEDGED,
        OrderState.PARTLY_FILLED,
    }
    assert DOUBTFUL == {OrderState.SENDING, OrderState.IN_DOUBT}
    assert NOT_SENT == {OrderState.INTENDED, OrderState.APPROVED}
    assert ENDED == set(OrderState) - OPEN - {OrderState.INTENDED}
    for move, start in MOVES:
        assert start not in ENDED, (move, start)


@pytest.mark.parametrize("state", [r for r in ROUTES if r != "none"])
def test_an_order_is_in_the_book_at_its_remainder_in_the_five_open_states_alone(state: str) -> None:
    journal, name = at(state)
    book = journal.view().book
    left = {"Approved": 4, "Sending": 4, "InDoubt": 4, "Acknowledged": 4, "PartlyFilled": 2}
    if state in left:
        ((strategy, (order,)),) = book.open_orders_by_strategy.items()
        assert strategy == CHAN
        assert order == dataclasses.replace(planned(1, 4), qty=Decimal(left[state]))
    else:
        assert book.open_orders_by_strategy == {}
    assert book.in_doubt == ((GLD,) if state in ("Sending", "InDoubt") else ())
    doubted = journal.in_doubt()
    assert [o.intent.client_order_id for o in doubted] == ([name] if book.in_doubt else [])
    assert journal.replay() == book


@pytest.mark.parametrize("qty", [4, -4])
def test_an_order_record_shows_what_its_entries_say(qty: int) -> None:
    journal, name = at("Expired", qty)
    order = journal.view().orders[name]
    assert order.intent == planned(1, qty)
    assert (order.decision, order.state, order.broker_order_id) == (3, OrderState.EXPIRED, "b1")
    assert order.filled == qty / 2
    kinds = ["intent", "verdict", "sending", "ack", "fill", "expired"]
    assert order.entries == tuple(OrderEntry(n, kind) for n, kind in enumerate(kinds, start=4))
    assert order.sent_at == AT
    journal, name = at("Approved")
    order = journal.view().orders[name]
    assert (order.broker_order_id, order.sent_at, order.filled) == (None, None, 0)


# Acceptance 4: the checks of item 9, one step inside and outside.


def decides(*orders, journal: Journal | None = None, **changes: object) -> tuple[Journal, Record]:
    if journal is None:
        journal = Journal(Memory(), ACCOUNT, WRITER)
        add(journal, opening())
        add(journal, read())
    return journal, dataclasses.replace(decision(*orders), **changes)


ONE, TWO = planned(1, 4), planned(2, -3, USO, OTHER)


def test_4_a_decision_is_accepted_with_the_stamps_of_its_own_orders() -> None:
    assert accepts(*decides()).decisions.keys() == {3}
    view = accepts(*decides(ONE, TWO, off=("idle", "chan")))
    assert view.decided == {AT: 3}
    assert view.decisions[3].orders == (ONE, TWO)
    many = [planned(n, 1) for n in range(1, 100)]
    assert len(accepts(*decides(*many)).decisions[3].orders) == 99


@pytest.mark.parametrize(
    "orders",
    [
        (ONE, ONE),
        (planned(2, 4),),
        (TWO, ONE),
        (planned(1, 4, at=AT + DAY),),
        (planned(1, 4, at=AT + timedelta(seconds=1)),),
        (dataclasses.replace(ONE, strategy=OTHER),),
    ],
)
def test_4_a_decisions_order_has_the_stamp_of_its_place_strategy_and_time(orders) -> None:
    refuses(*decides(*orders), "entry 3 plans .*, which is not what the id says")


@pytest.mark.parametrize(
    "name", ["mine", "", "ith-chan-20261001T194000Z-1", ONE.client_order_id + " "]
)
def test_4_a_decisions_order_with_no_stamp_halts(name: str) -> None:
    order = dataclasses.replace(ONE, client_order_id=name)
    refuses(*decides(order), "entry 3 plans an order whose id is no stamp")


def test_4_a_hundredth_order_halts() -> None:
    many = [planned(n, 1) for n in range(1, 100)]
    past = dataclasses.replace(ONE, client_order_id=ONE.client_order_id[:-2] + "100")
    refuses(*decides(*many, past), "entry 3 plans an order whose id is no stamp")


def test_4_a_decision_plans_no_order_of_a_strategy_that_is_off() -> None:
    refuses(*decides(ONE, TWO, off=(OTHER,)), "an order of 'other-one.2', which was off")


def test_4_one_decision_entry_per_decision_time() -> None:
    refuses(started(ONE), decision(), "entry 4 is a second decision at .*, after entry 3")
    accepts(started(ONE), decision(at=AT + timedelta(seconds=1)))


@pytest.mark.parametrize("number", [0, 1, 3, 4, 9, -2])
def test_4_a_decisions_read_is_an_earlier_read_entry(number: int) -> None:
    later = decision(at=AT + DAY, read=number, synced=1)
    refuses(started(ONE), later, f"entry 4 names entry {number}, which is no read before it")


@pytest.mark.parametrize(("number", "fine"), [(0, False), (1, True), (3, True), (4, False)])
def test_4_a_decision_was_synced_through_any_earlier_entry(number: int, fine: bool) -> None:
    later = decision(at=AT + DAY, synced=number)
    if fine:
        assert accepts(started(ONE), later).decisions[4].synced == number
    else:
        refuses(started(ONE), later, f"entry 4 was synced through entry {number}")


def test_4_an_intents_order_equals_the_one_its_decision_plans() -> None:
    accepts(started(ONE, TWO), IntentEntry(3, TWO))
    for changed in (
        dataclasses.replace(ONE, qty=Decimal(5)),
        dataclasses.replace(ONE, qty=Decimal("4.000000001")),
        dataclasses.replace(ONE, qty=Decimal(-4)),
        dataclasses.replace(ONE, instrument=USO),
        dataclasses.replace(ONE, strategy=OTHER),
        dataclasses.replace(ONE, type=OrderType.MARKET_ON_CLOSE),
        dataclasses.replace(ONE, client_order_id=TWO.client_order_id),
        planned(3, 4),
    ):
        refuses(started(ONE, TWO), IntentEntry(3, changed), "is not one entry 3 plans")
    for number in (0, 2, 4, 9):
        refuses(started(ONE), IntentEntry(number, ONE), f"entry {number}, which is no decision")
    journal = started(ONE)
    add(journal, decision(planned(1, 4, at=AT + DAY), at=AT + DAY))
    refuses(journal, IntentEntry(4, ONE), "is not one entry 4 plans")


@pytest.mark.parametrize(("number", "fine"), [(0, False), (3, False), (4, True), (5, False)])
def test_4_a_verdicts_book_is_the_entry_before_it(number: int, fine: bool) -> None:
    journal, name = at("Intended")
    record = verdict(name, book=number)
    if fine:
        accepts(journal, record)
    else:
        refuses(journal, record, f"on the book through entry {number}, not through the entry")


@pytest.mark.parametrize(
    ("number", "fine"), [(-1, False), (0, False), (1, True), (4, True), (5, False), (99, False)]
)
def test_4_a_verdicts_close_entry_is_an_earlier_one(number: int, fine: bool) -> None:
    journal, name = at("Intended")
    record = dataclasses.replace(verdict(name, book=4), close_entry=number)
    if fine:
        accepts(journal, record)
    else:
        refuses(journal, record, f"entry 5 names entry {number}, no entry before it")


def test_4_a_brokers_id_is_one_orders() -> None:
    one, two = planned(1, 4), planned(2, 4)
    journal = started(one, two)
    for record in [*route("Acknowledged", one), *route("Sending", two)]:
        add(journal, record)
    refuses(journal, ack(two.client_order_id, "b1"), f"which is {one.client_order_id}'s")
    journal = started(one, two)
    for record in [*route("Filled", one), *route("Sending", two)]:
        add(journal, record)
    refuses(journal, ack(two.client_order_id, "b1"), f"which is {one.client_order_id}'s")
    journal = started(one, two)
    for record in [*route("Filled", one), *route("Sending", two)]:
        add(journal, record)
    assert accepts(journal, ack(two.client_order_id, "b2")).orders[
        two.client_order_id
    ].broker_order_id == ("b2")


NAME = ONE.client_order_id


@pytest.mark.parametrize(
    ("made", "read_", "match"),
    [
        (fill("new", "b1", 2), 2, None),
        (fill("new", "b1", "1.999999999"), 2, None),
        (fill("new", "b1", "0.000000001"), 2, None),
        (fill("new", "b1", 1, client_order_id=NAME), 2, None),
        (fill("new", "b1", "2.000000001"), 2, "takes .* to 4.000000001 filled of 4"),
        (fill("new", "b1", 3), 2, "takes .* to 5 filled of 4"),
        (fill("new", "b1", -1), 2, "of -1 is not on .*'s side"),
        (fill("new", "b1", 1, instrument=USO), 2, "is in 'USO', and .* is in GLD"),
        (
            fill("new", "b1", 1, client_order_id="ith-x"),
            2,
            "names another client order id than",
        ),
        (fill("new", "b1", 1, client_order_id=""), 2, "names another client order id than"),
        (fill("new", "b2", 1), 2, "of the broker's order b2, which no order was"),
        (fill("new", "b1", 1), 3, "names entry 3, which is no read before it"),
        (fill("new", "b1", 1), 9, "names entry 9, which is no read before it"),
        (fill("new", "b1", 1), 0, "names entry 0, which is no read before it"),
    ],
)
def test_4_each_check_on_a_fill(made: Fill, read_: int, match: str | None) -> None:
    journal, name = at("PartlyFilled")
    if match is not None:
        refuses(journal, filled(made, read_), match)
        return
    view = accepts(journal, filled(made, read_))
    assert view.orders[name].filled == 2 + made.qty
    assert view.fills["new"] == made
    assert view.book.cash == 100_000 - (2 + made.qty) * Decimal("10.25")
    assert view.book.positions_by_strategy == {CHAN: {GLD: 2 + made.qty}}


def test_4_an_unmapped_fill_halts_with_the_brokers_id_and_no_orders() -> None:
    journal, name = at("Sending")
    with pytest.raises(Halt) as halted:
        journal.record(journal.view(), filled(fill("new", "b7", 1)), AT)
    assert "b7" in halted.value.reason and name not in halted.value.reason


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"received_at": AT + DAY},
        {"client_order_id": NAME},
        {"at_text": "2026-10-01 19:40Z"},
        {"excess_places": ("price",)},
        {"price": "10.2500"},
        {"received_at": AT + DAY, "client_order_id": NAME, "at_text": "x"},
    ],
)
@pytest.mark.parametrize("state", ["PartlyFilled", "Filled", "Expired"])
def test_4_a_repeat_of_a_fill_is_applied_once(state: str, changes: dict) -> None:
    journal, name = at(state)
    before = shown(journal.view())
    view = accepts(journal, filled(fill("b1-f1", "b1", 2, **changes)))
    after = shown(view)
    assert after["fills"] == before["fills"]
    assert view.orders[name] == before["orders"][name]
    assert after["book"] == dataclasses.replace(
        before["book"], replayed_through=before["through"].seq + 1
    )


DIFFERING = {
    "price": {"price": "10.26"},
    "qty": {"qty": 1},
    "broker_order_id": {"broker_order_id": "b2"},
    "instrument": {"instrument": USO},
    "at": {"at": AT + timedelta(microseconds=1)},
    # A bust sent again under its own id, for no shares.
    "no shares": {"qty": 0},
}


def differing(field: str, **more: object) -> Fill:
    return fill("b1-f1", **{"broker_order_id": "b1", "qty": 2} | DIFFERING[field] | more)


@pytest.mark.parametrize("field", DIFFERING)
@pytest.mark.parametrize("state", ["PartlyFilled", "Filled", "Expired"])
def test_4_a_repeat_that_differs_is_a_dispute_and_the_book_halts(state: str, field: str) -> None:
    journal, name = at(state)
    before, repeat = journal.view(), differing(field)
    copy = Journal(Memory(journal._store.blobs), ACCOUNT, WRITER)
    no_fill = "is for zero shares|entry .*'s fill b1-f1 holds .*, and the one applied holds"
    refuses(copy, filled(repeat), no_fill)
    view = accepts(journal, disputed(repeat))
    number = view.through.seq
    assert view.disputes == (Dispute(number, repeat),)
    assert view.orders == before.orders and view.fills == before.fills
    first = next(e.seq for e in view.orders[name].entries if e.kind == "fill")
    for reader in (view, journal.view(), Journal(journal._store, ACCOUNT).view()):
        with pytest.raises(Halt) as halted:
            _ = reader.book
        for said in (
            "the fill b1-f1 is in dispute",
            f"entry {first} holds 2 of 'GLD' at 10.25 on the order b1, filled "
            "2026-10-01T19:40:00+00:00",
            f"entry {number} holds {repeat.qty} of '{repeat.instrument}' at {repeat.price} on the "
            f"order {repeat.broker_order_id}, filled {repeat.at.isoformat()}",
        ):
            assert said in halted.value.reason
    with pytest.raises(Halt, match="is in dispute"):
        journal.replay()
    # A view before the dispute still has a book.
    assert journal.view(through=number - 1).book == before.book


def test_4_a_dispute_is_refused_with_no_first_with_its_content_and_where_it_is_held() -> None:
    for name in ("none", "Acknowledged"):
        journal, _ = at(name)
        refuses(journal, disputed(fill("b1-f1", "b1", 2)), "which no entry applied")
    journal, _ = at("PartlyFilled")
    refuses(
        journal,
        disputed(fill("b1-f1", "b1", 2, received_at=AT + DAY)),
        "disputes the fill b1-f1 with the content it has",
    )
    for number in (1, 3, 8, 9, 99):
        journal, _ = at("PartlyFilled")
        refuses(journal, disputed(differing("price"), number), "which is no read before it")
    journal, _ = at("PartlyFilled")
    accepts(journal, disputed(differing("price")))
    again = disputed(differing("price", received_at=AT + DAY, at_text="x"))
    refuses(journal, again, "with the content entry 9 holds")


def test_4_one_dispute_offered_twice_and_from_two_journals_leaves_one_entry() -> None:
    journal, _ = at("PartlyFilled")
    other = Journal(journal._store, ACCOUNT, "1" * 32)
    views = journal.view(), other.view()
    record = disputed(differing("price"))
    journal.record(views[0], record, AT)
    with pytest.raises(NumberTaken):
        other.record(views[1], record, AT)
    for again in (journal, other):
        with pytest.raises(Halt, match="with the content entry 9 holds"):
            again.record(again.view(), record, AT)
        # A recover that reads the repeat again as a plain fill is not halted by it.
        again.record(again.view(), filled(differing("price")), AT)
    kinds = [entry.body["kind"] for entry in Journal(journal._store, ACCOUNT).read()]
    assert kinds.count("disputed_fill") == 1
    assert len(journal.view().disputes) == 1


def test_4_a_third_content_is_a_second_dispute_and_the_book_names_both() -> None:
    journal, _ = at("PartlyFilled")
    accepts(journal, disputed(differing("price")))
    view = accepts(journal, disputed(differing("qty")))
    assert [dispute.seq for dispute in view.disputes] == [9, 10]
    with pytest.raises(
        Halt, match="entry 9 holds 2 of 'GLD' at 10.26 .*entry 10 holds 1 of 'GLD' at 10.25"
    ):
        _ = view.book


def test_4_a_dispute_stops_no_record() -> None:
    orders = [planned(n, 4) for n in range(1, 5)]
    journal = started(*orders)
    for record in route("PartlyFilled", orders[0]):
        add(journal, record)
    for order in orders[1:]:
        for record in route("Sending", order):
            add(journal, record)
    accepts(journal, disputed(differing("price")))
    names = [order.client_order_id for order in orders]
    accepts(journal, ack(names[1], "b5"))
    accepts(journal, in_doubt(names[2]))
    accepts(journal, abandoned(names[3]))
    accepts(journal, filled(fill("f-other", "b5", 1)))
    view = accepts(journal, filled(fill("b1-f2", "b1", 2)))
    assert [view.orders[name].state.value for name in names] == [
        "Filled",
        "PartlyFilled",
        "InDoubt",
        "Abandoned",
    ]
    with pytest.raises(Halt, match="is in dispute"):
        _ = view.book


@pytest.mark.parametrize(
    ("said", "fine"),
    [("2", True), ("2.000", True), ("2.000000001", False), ("1.999999999", False), ("-2", False)],
)
def test_4_an_expiry_says_what_filled(said: str, fine: bool) -> None:
    journal, name = at("PartlyFilled")
    if fine:
        accepts(journal, expired(name, said))
    else:
        refuses(
            journal, expired(name, said), f"expired with {said} filled, and its fills come to 2"
        )


def test_4_an_expiry_of_a_sale_is_signed_as_its_fills_are() -> None:
    journal, name = at("PartlyFilled", -4)
    assert accepts(journal, expired(name, -2)).orders[name].filled == -2
    journal, name = at("PartlyFilled", -4)
    refuses(journal, expired(name, 2), "expired with 2 filled, and its fills come to -2")


# Acceptance 4 and items 12 to 15: the opening, and a journal with no entry.


def test_4_the_opening_names_the_journals_own_account_and_cash_of_zero_or_more() -> None:
    for cash in ("0", "0.0001", "100000"):
        view = accepts(Journal(Memory(), ACCOUNT, WRITER), opening(cash))
        assert view.book.cash == Decimal(cash)
        assert view.book.positions_by_strategy == view.book.open_orders_by_strategy == {}
        assert (view.book.replayed_through, view.book.corrections) == (1, ())
        assert (view.opened_at, view.sent_at) == (AT, None)
    refuses(Journal(Memory(), ACCOUNT, WRITER), opening("-0.0001"), "with -0.0001, below zero")
    refuses(
        Journal(Memory(), ACCOUNT, WRITER),
        opening(account="paper-2"),
        "opens the account 'paper-2'",
    )


def test_4_entry_1_is_the_opening_and_no_other_is() -> None:
    for record in (read(), decision(), IntentEntry(1, ONE), abandoned(NAME), filled(fill("f"))):
        refuses(Journal(Memory(), ACCOUNT, WRITER), record, "entry 1 is of the kind .* opening")
    refuses(started(), opening(), "entry 4 is of the kind opening")


def test_a_journal_with_no_entry_has_a_view_and_no_book() -> None:
    journal = Journal(Memory(), ACCOUNT, WRITER)
    for view in (journal.view(), journal.view(through=0)):
        assert (view.through.seq, view.through.entry_hash) == (0, FIRST_PREV_HASH)
        assert (dict(view.orders), dict(view.fills), view.opened_at) == ({}, {}, None)
        with pytest.raises(Halt, match="holds no entry, so there is no book"):
            _ = view.book
    with pytest.raises(Halt, match="holds no entry"):
        journal.replay()
    assert journal.in_doubt() == ()
    # `record` on it accepts an opening alone, and the view before it stays empty.
    before = journal.view()
    _, after = journal.record(before, opening(), AT)
    assert after.book.cash == 100_000
    with pytest.raises(Halt, match="holds no entry"):
        _ = before.book


def test_12_cash_is_exact_and_a_shorts_cash_comes_in() -> None:
    one, two = planned(1, "0.333333333"), planned(2, "-0.333333333", USO, OTHER)
    journal = started(one, two, cash="19.0412")
    for order, broker in ((one, "b1"), (two, "b2")):
        for record in route("Acknowledged", order, broker=broker):
            add(journal, record)
    add(journal, filled(fill("f1", "b1", "0.333333333", "57.1237")))
    view = accepts(journal, filled(fill("f2", "b2", "-0.333333333", "71.0001", USO)))
    bought = Decimal("19.0412") - Decimal("0.333333333") * Decimal("57.1237")
    assert view.book.cash == bought + Decimal("0.333333333") * Decimal("71.0001")
    assert view.book.positions_by_strategy == {
        CHAN: {GLD: Decimal("0.333333333")},
        OTHER: {USO: Decimal("-0.333333333")},
    }


def test_12_cash_is_exact_past_the_default_contexts_digits() -> None:
    one = planned(1, "1.000000001")
    journal = started(one, cash="9" * 24 + ".9999")
    for record in route("Acknowledged", one):
        add(journal, record)
    view = accepts(journal, filled(fill("f1", "b1", "1.000000001", "0.0001")))
    assert str(view.book.cash) == "9" * 24 + ".9997999999999"


def test_12_a_closed_position_leaves_the_book_and_two_strategies_stay_apart() -> None:
    one, two = planned(1, 4), planned(2, -4, strategy=OTHER)
    journal = started(one, two)
    for order, broker in ((one, "b1"), (two, "b2")):
        for record in route("Filled", order, broker=broker):
            add(journal, record)
    assert journal.view().book.positions_by_strategy == {CHAN: {GLD: 4}, OTHER: {GLD: -4}}
    close = planned(1, -4, at=AT + DAY)
    add(journal, decision(close, at=AT + DAY))
    for record in route("Filled", close, decided=journal.head().seq, broker="b3"):
        add(journal, record)
    view = journal.view()
    assert view.book.positions_by_strategy == {OTHER: {GLD: -4}}
    assert view.book.cash == 100_000 - Decimal("10.25") * -4


@pytest.mark.parametrize(
    ("qty", "price", "what"),
    [
        ("1.123456789", "10." + "1" * 34, "the cash"),
        ("1E+29", "1", None),
        ("1E+30", "100", "the cash"),
    ],
)
def test_11_a_book_that_no_record_could_hold_is_never_stored(
    qty: str, price: str, what: str | None
) -> None:
    order = planned(1, Decimal(qty))
    journal = started(order)
    for record in route("Acknowledged", order):
        add(journal, record)
    record = filled(fill("f1", "b1", qty, price))
    if what is None:
        accepts(journal, record)
    else:
        refuses(journal, record, f"entry 8 leaves {what} where no record holds it")


def test_11_a_position_and_a_remainder_that_no_record_could_hold_are_never_stored() -> None:
    order = planned(1, Decimal("1E+29"))
    journal = started(order, cash=0)
    for record in route("Acknowledged", order):
        add(journal, record)
    small = filled(fill("f1", "b1", "0.0000000001", "1"))
    refuses(journal, small, "leaves what is left of .* where no record holds it")
    one, two = planned(1, Decimal("1E+29")), planned(2, Decimal("0.0000000001"))
    journal = started(one, two, cash="0")
    for record in route("Acknowledged", one):
        add(journal, record)
    add(journal, filled(fill("f1", "b1", "1E+29", "0.0000000001")))
    for record in route("Acknowledged", two, broker="b2"):
        add(journal, record)
    refuses(
        journal,
        filled(fill("f2", "b2", "0.0000000001", "1")),
        "leaves (the cash|the position in GLD) where no record holds it",
    )


# Item 11: the one way a writer adds an entry.


def test_11_record_stores_the_entry_after_its_view_and_no_other() -> None:
    journal, name = at("Approved")
    view = journal.view()
    ack_, after = journal.record(view, SendingEntry(name), AT + DAY)
    (entry,) = list(journal.read(after=view.through.seq))
    assert (entry.seq, entry.prev_hash) == (view.through.seq + 1, view.through.entry_hash)
    assert entry.built_at == AT + DAY and entry.body["writer"] == WRITER
    assert (ack_.seq, ack_.entry_hash) == (entry.seq, entry.hash())
    assert after.orders[name].sent_at == after.sent_at == AT + DAY


def test_11_record_on_a_view_the_journal_moved_past_is_refused() -> None:
    """It chains to the view it was handed, never to what the `Journal` remembers."""
    journal, name = at("Approved")
    old = journal.view()
    journal.record(old, SendingEntry(name), AT)
    before = dict(journal._store.blobs)
    for record in (read(), abandoned(name)):
        with pytest.raises(NumberTaken) as taken:
            journal.record(old, record, AT)
        assert (taken.value.account, taken.value.seq) == (ACCOUNT, old.through.seq + 1)
    assert journal._store.blobs == before
    # A step the old view would refuse halts on it, and one the new view refuses halts there.
    with pytest.raises(Halt, match="which is Sending"):
        journal.record(journal.view(), SendingEntry(name), AT)


def test_11_another_writers_entry_at_the_number_is_taken_and_viewed_again() -> None:
    journal, name = at("Approved")
    other = Journal(journal._store, ACCOUNT, "1" * 32)
    mine, theirs = journal.view(), other.view()
    other.record(theirs, abandoned(name), AT)
    with pytest.raises(NumberTaken):
        journal.record(mine, SendingEntry(name), AT)
    assert mine.orders[name].state is OrderState.APPROVED
    again = journal.view()
    assert again.orders[name].state is OrderState.ABANDONED
    with pytest.raises(Halt, match="which is Abandoned"):
        journal.record(again, SendingEntry(name), AT)


def test_7_two_writers_never_hold_one_acknowledgement_under_one_clock() -> None:
    journal, name = at("Approved")
    other = Journal(journal._store, ACCOUNT, "1" * 32)
    views, held = (journal.view(), other.view()), []
    for writer, view in zip((journal, other), views, strict=True):
        try:
            held.append(writer.record(view, SendingEntry(name), AT)[0])
        except NumberTaken:
            pass
    assert len(held) == 1


def test_11_two_calls_that_build_the_same_bytes_hold_one_acknowledgement() -> None:
    """Two threads of one process share a writer, and under a history clock a time too."""
    journal, name = at("Approved")
    view = journal.view()
    start, held, taken = threading.Barrier(8), [], []

    def send() -> None:
        start.wait()
        try:
            held.append(journal.record(view, SendingEntry(name), AT))
        except NumberTaken as refused:
            taken.append(refused.seq)

    threads = [threading.Thread(target=send) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(held) == 1 and len(taken) == 7
    assert journal.view().orders[name].state is OrderState.SENDING
    with pytest.raises(NumberTaken):
        journal.record(view, SendingEntry(name), AT)


class Slow(set):
    """A set whose check and whose add each leave time for another thread's check."""

    def __contains__(self, item: object) -> bool:
        found = super().__contains__(item)
        time.sleep(0.05)
        return found

    def add(self, item: object) -> None:
        time.sleep(0.05)
        super().add(item)


def test_11_no_two_calls_pass_the_one_check_that_hands_a_number_out() -> None:
    journal, name = at("Approved")
    view = journal.view()
    journal._acknowledged = Slow()
    held = _together(8, lambda n: journal.record(view, SendingEntry(name), AT))
    assert len(held) == 1


def _together(threads: int, call: Callable[[int], object]) -> list[object]:
    """What each call that raised no `NumberTaken` gave, all started at once."""
    start, given, failures = threading.Barrier(threads), [], []

    def run(n: int) -> None:
        start.wait()
        try:
            given.append(call(n))
        except NumberTaken:
            pass
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    made = [threading.Thread(target=run, args=(n,), daemon=True) for n in range(threads)]
    for thread in made:
        thread.start()
    for thread in made:
        thread.join(60)
        assert not thread.is_alive()
    assert not failures, failures
    return given


class SlowDict(dict):
    def __contains__(self, item: object) -> bool:
        found = super().__contains__(item)
        time.sleep(0.05)
        return found


def test_7_an_entry_folded_on_two_threads_is_kept_once() -> None:
    journal, _ = at("Acknowledged")
    view = journal.view()
    entry = journal._build(body_of(filled(fill("f1")), WRITER), AT, view.through)
    steps = [after(view, entry) for _ in range(4)]
    view._shared.fills.found = SlowDict()
    views = _together(4, lambda n: steps[n].keep(entry.hash()))
    assert len(views) == 4
    for made in views:
        assert list(made.fills) == ["f1"] and len(made.fills) == 1


def test_11_a_fill_whose_answer_was_lost_is_kept_once() -> None:
    made, _ = at("Acknowledged")
    store = Failing(made._store.blobs)
    journal = Journal(store, ACCOUNT, WRITER)
    view = journal.view()
    store.fail = "landed"
    with pytest.raises(StoreUnavailable):
        journal.record(view, filled(fill("f1")), AT)
    assert list(journal.view().fills) == ["f1"]
    _, after_ = journal.record(view, filled(fill("f1")), AT)
    assert list(after_.fills) == ["f1"]
    assert shown(after_) == shown(journal.view()) == shown(Journal(store, ACCOUNT).view())


def test_after_refuses_a_second_value_for_what_a_view_shows() -> None:
    """Every kept value is added once, so a second one would be dropped without a word."""
    journal, _ = at("PartlyFilled")
    view = journal.view()
    step = after(view, journal._build(body_of(read(), WRITER), AT, view.through))
    with pytest.raises(Halt, match="gives 'b1-f1' a second value, after entry 8's"):
        step.add(view._shared.fills, "b1-f1", fill("b1-f1", qty=1))
    # One entry's own value, already kept by a newer view of the same maps, is not a second.
    _, newer = journal.record(view, read(), AT)
    assert view._shared.reads.found[newer.through.seq] == (newer.through.seq, read())
    step.add(view._shared.reads, newer.through.seq, read())


class Failing(Memory):
    """A store whose next create fails, having landed or not."""

    fail: str | None = None

    def create(self, key: str, data: bytes, sha256: str) -> str:
        fail, self.fail = self.fail, None
        if fail == "refused":
            raise StoreUnavailable(key, "no answer")
        tag = super().create(key, data, sha256)
        if fail == "landed":
            raise StoreUnavailable(key, "the answer was lost")
        return tag


@pytest.mark.parametrize("looked", [False, True])
@pytest.mark.parametrize("fail", ["refused", "landed"])
def test_11_only_an_append_the_store_acknowledged_moves_the_fold(fail: str, looked: bool) -> None:
    made, name = at("Approved")
    store = Failing(made._store.blobs)
    journal = Journal(store, ACCOUNT, WRITER)
    view = journal.view()
    was = shown(view)
    store.fail = fail
    with pytest.raises(StoreUnavailable):
        journal.record(view, SendingEntry(name), AT)
    assert shown(view) == was
    if fail == "refused" or looked:
        # What the store holds is what a view shows, whatever the caller heard.
        state = OrderState.APPROVED if fail == "refused" else OrderState.SENDING
        assert journal.view().orders[name].state is state
    # The same call builds the same bytes, so it is safe to repeat.
    ack_, after = journal.record(view, SendingEntry(name), AT)
    assert (ack_.seq, after.through.seq) == (6, 6)
    assert shown(after) == shown(journal.view()) == shown(Journal(store, ACCOUNT).view())
    with pytest.raises(NumberTaken):
        journal.record(view, SendingEntry(name), AT)


def test_11_a_late_answer_moves_the_fold_no_way_back() -> None:
    made, name = at("Approved")
    store = Failing(made._store.blobs)
    journal = Journal(store, ACCOUNT, WRITER)
    view = journal.view()
    store.fail = "landed"
    with pytest.raises(StoreUnavailable):
        journal.record(view, SendingEntry(name), AT)
    _, newest = journal.record(journal.view(), read(AT + DAY), AT)
    _, late = journal.record(view, SendingEntry(name), AT)
    assert (late.through.seq, newest.through.seq) == (6, 7)
    store.gets = 0
    assert journal.view() is newest
    assert store.gets == 0


def test_11_an_own_entry_is_never_read_back() -> None:
    journal, name = at("Approved")
    view = journal.view()
    store = journal._store
    store.gets = store.lists = 0
    for record in (SendingEntry(name), ack(name), filled(fill("f1", "b1", 1))):
        _, view = journal.record(view, record, AT)
    assert (store.gets, store.lists) == (0, 0)
    assert journal.view() is view
    assert (store.gets, store.lists) == (0, 1)


def test_11_a_view_another_journal_gave_is_refused() -> None:
    journal, name = at("Approved")
    store = journal._store
    for view in (
        Journal(store, ACCOUNT, WRITER).view(),
        Journal(store, "paper-2", WRITER).view(),
        None,
        journal.view().book,
    ):
        with pytest.raises(Halt, match="the view is not one this journal gave"):
            journal.record(view, SendingEntry(name), AT)
    assert len(store.blobs) == 5
    # One folded from the first by this journal is.
    _, after = journal.record(journal.view(through=5), SendingEntry(name), AT)
    assert shown(after) == shown(journal.view()) == shown(Journal(store, ACCOUNT).view())


# Item 18: a view.


def test_18_a_view_through_an_earlier_entry_reads_nothing_past_it() -> None:
    journal, _ = at("Expired")
    end = journal.view().through.seq
    prefixes = {}
    grown = Journal(store := Memory(), ACCOUNT, WRITER)
    for number in range(1, end + 1):
        key = journal_entry_key(ACCOUNT, number)
        store.create(key, journal._store.get(key), "")
        prefixes[number] = shown(grown.view())
    store.gets = 0
    for number, was in prefixes.items():
        store.gets = 0
        assert shown(grown.view(through=number)) == was
        assert store.gets == number
    with pytest.raises(Halt, match=f"entry {end + 1} is missing"):
        grown.view(through=end + 1)
    for bad in (-1, True, "1", 1.0):
        with pytest.raises(ValueError, match="is not an entry number or 0"):
            grown.view(through=bad)
    assert shown(grown.view()) == prefixes[end]


def test_18_a_halt_past_an_earlier_entry_does_not_stop_its_view() -> None:
    journal, name = at("Approved")
    good = shown(journal.view())
    add(journal, verdict(name))
    with pytest.raises(Halt):
        journal.view()
    assert shown(journal.view(through=5)) == good
    with pytest.raises(Halt):
        journal.view(through=6)


def test_18_a_view_is_acted_on_only_once_it_was_read_to_the_end() -> None:
    journal, name = at("Approved")
    reader = Journal(journal._store, ACCOUNT, WRITER)
    old = reader.view()
    add(journal, SendingEntry(name))
    add(journal, SendingEntry(name))
    for _ in range(2):
        with pytest.raises(Halt, match="entry 7 is a sending of the order"):
            reader.view()
    assert old.orders[name].state is OrderState.APPROVED
    # The entry that halted is not folded, so what comes after it is not read either.
    add(journal, ack(name))
    with pytest.raises(Halt, match="entry 7 is a sending of the order"):
        reader.view()


def test_18_a_view_shows_each_decision_read_and_time() -> None:
    journal, name = at("Sending")
    add(journal, read(AT + DAY))
    add(journal, decision(at=AT + DAY, read=7))
    view = journal.view()
    assert list(view.decisions) == [3, 8] and list(view.reads) == [2, 7]
    assert view.decided == {AT: 3, AT + DAY: 8}
    assert view.reads[7] == read(AT + DAY)
    assert view.reads.get(8) is None and view.decisions.get(7) is None
    assert (len(view.decisions), len(view.reads), len(view.orders)) == (2, 2, 1)
    assert (view.account, view.opened_at, view.sent_at) == (ACCOUNT, AT, AT)
    with pytest.raises(TypeError):
        view.reads[7] = None  # type: ignore[index]
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.account = "x"  # type: ignore[misc]
    for changed in (view.live, view.orders, view.fills, view.decisions, view.decided):
        with pytest.raises(TypeError):
            changed["x"] = None  # type: ignore[index]


def test_18_live_orders_keep_the_order_their_intents_were_journaled_in() -> None:
    one, two, three = (planned(n, 4) for n in (1, 2, 3))
    journal = started(one, two, three)
    for order in (two, one, three):
        add(journal, IntentEntry(3, order))
    names = [order.client_order_id for order in (two, one, three)]
    for name in (names[2], names[0], names[1]):
        add(journal, verdict(name))
        add(journal, SendingEntry(name))
        assert list(journal.view().live) == names
    assert [o.intent.client_order_id for o in journal.in_doubt()] == names


def test_18_a_view_held_before_an_entry_shows_nothing_of_it_in_any_map() -> None:
    journal, name = at("Sending")
    held, was = journal.view(), shown(journal.view())
    _, newer = journal.record(held, ack(name), AT)
    _, newer = journal.record(newer, read(AT + DAY), AT)
    _, newer = journal.record(newer, decision(at=AT + DAY, read=8), AT)
    _, newer = journal.record(newer, filled(fill("f1", "b1", 4)), AT)
    assert (len(newer.reads), len(newer.decisions), len(newer.decided)) == (2, 2, 2)
    assert shown(held) == was
    for shown_, key in (
        (held.reads, 8),
        (held.decisions, 9),
        (held.decided, AT + DAY),
        (held.fills, "f1"),
    ):
        assert key not in shown_ and len(shown_) == len(list(shown_))
        with pytest.raises(KeyError):
            shown_[key]
    # The broker's id is known from entry 7 on, and the held view is through 6.
    entry = journal._build(body_of(filled(fill("f1", "b1", 4)), WRITER), AT, held.through)
    with pytest.raises(Halt, match="b1, which no order was acknowledged under"):
        after(held, entry)


def test_18_the_newest_sent_at_is_the_latest_of_any_order() -> None:
    one, two = planned(1, 4), planned(2, 4)
    journal = started(one, two)
    for record in [*route("Approved", one), *route("Approved", two)]:
        add(journal, record)
    _, view = journal.record(journal.view(), SendingEntry(one.client_order_id), AT + DAY)
    assert view.sent_at == AT + DAY
    # A clock set back between two sends leaves the later time.
    _, view = journal.record(view, SendingEntry(two.client_order_id), AT)
    assert view.sent_at == AT + DAY
    assert view.orders[two.client_order_id].sent_at == AT
    assert [o.intent for o in journal.in_doubt()] == [one, two]


# Acceptance 7: a view is incremental, and one held is never changed.


def test_7_a_view_reads_one_more_blob_after_another_writers_one_more_entry() -> None:
    journal = Journal(store := Memory(), ACCOUNT)
    writer = Journal(store, ACCOUNT, WRITER)
    _, at_end = writer.record(writer.view(), opening(), AT)
    for n in range(1999):
        _, at_end = writer.record(at_end, read(AT + n * DAY), AT)
    store.gets = store.lists = 0
    first = journal.view()
    assert (store.gets, store.lists) == (2000, 1)
    store.gets = store.lists = 0
    assert journal.view() is first
    assert (store.gets, store.lists) == (0, 1)
    writer.record(at_end, read(), AT)
    store.gets = store.lists = 0
    again = journal.view()
    assert (store.gets, store.lists) == (1, 1)
    assert (first.through.seq, again.through.seq) == (2000, 2001)
    assert shown(again) == shown(Journal(store, ACCOUNT).view()) == shown(writer.view())


def _lives(journal: Journal, orders: int, start: datetime = AT) -> list:
    """`orders` whole lives recorded one after another, each with its own read and decision."""
    view = journal.view()
    if view.through.seq == 0:
        _, view = journal.record(view, opening(), AT)
    views = []
    for n in range(1, orders + 1):
        when = start + n * timedelta(minutes=1)
        _, view = journal.record(view, read(when), when)
        order = planned(1, 4 if n % 2 else -4, at=when)
        _, view = journal.record(
            view, decision(order, at=when, read=view.through.seq), when
        )  # fmt: skip
        broker = f"b{when.timestamp():.0f}"
        for record in route("Filled", order, decided=view.through.seq, broker=broker):
            if getattr(record, "book", 0) is None:
                record = dataclasses.replace(record, book=view.through.seq)
            _, view = journal.record(view, record, when)
            views.append(view)
    return views


def test_7_a_view_held_while_a_hundred_more_entries_are_recorded_is_unchanged() -> None:
    journal = Journal(Memory(), ACCOUNT, WRITER)
    held = _lives(journal, 5)
    was = [shown(view) for view in held]
    thread = threading.Thread(target=_lives, args=(journal, 14, AT + DAY), daemon=True)
    thread.start()
    thread.join(60)
    assert not thread.is_alive()
    assert journal.view().through.seq - held[-1].through.seq > 100
    assert [shown(view) for view in held] == was
    last = held[-1]
    assert len(last.fills) == len(list(last.fills)) == 10
    assert len(last.orders) == len(list(last.orders)) == 5
    newer = set(journal.view().fills) - set(last.fills)
    assert newer and all(last.fills.get(name) is None for name in newer)
    assert all(name not in last.fills for name in newer)


def test_7_two_readers_see_prefixes_while_a_third_thread_records(tmp_path: Path) -> None:
    journal = Journal(FolderStore(tmp_path), ACCOUNT, WRITER)
    journal.record(journal.view(), opening(), AT)
    failures: list[BaseException] = []
    seen: list[list] = [[], []]
    done = threading.Event()

    def look(n: int) -> None:
        try:
            while not done.is_set():
                seen[n].append(journal.view())
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    threads = [threading.Thread(target=look, args=(n,), daemon=True) for n in (0, 1)]
    for thread in threads:
        thread.start()
    try:
        _lives(journal, 12)
    finally:
        done.set()
    for thread in threads:
        thread.join(60)
        assert not thread.is_alive()
    assert not failures, failures
    prefixes = {}
    for views in seen:
        numbers = [view.through.seq for view in views]
        assert numbers == sorted(numbers)
        for view in {v.through.seq: v for v in views}.values():
            number = view.through.seq
            if number not in prefixes:
                prefixes[number] = shown(journal.view(through=number))
            assert shown(view) == prefixes[number]
    assert journal.view().through.seq == 97


class Torn(Memory):
    """A store whose listings miss entries, as a folder read beside a writer can.

    `missed` holds the numbers each listing in turn leaves out, and a listing
    past its end leaves out none.
    """

    missed: list[tuple[int, ...]] = []

    def list(self, prefix: str, after: str | None) -> list[str]:
        gone = [journal_entry_key(ACCOUNT, n) for n in (self.missed.pop(0) if self.missed else ())]
        return [key for key in super().list(prefix, after) if key not in gone]


def test_7_an_entry_a_listing_missed_is_read_by_its_own_key() -> None:
    made, _ = at("Acknowledged")
    good = shown(made.view())
    for missed in [(4,), (1,), (6,), (2, 3), (1, 2, 4, 6), (1, 2, 3, 4, 5, 6)]:
        store = Torn(made._store.blobs)
        store.missed = [missed]
        assert shown(Journal(store, ACCOUNT).view()) == good
        assert (store.lists, store.gets) == (1, 7)
    # One missed at the end is read by the next view, which lists it.
    store.missed = [(7,)]
    journal = Journal(store, ACCOUNT)
    assert journal.view().through.seq == 6
    assert shown(journal.view()) == good


@pytest.mark.parametrize("gone", [(1,), (4,), (6,), (4, 5), (2, 6)])
def test_7_an_entry_that_is_neither_listed_nor_stored_is_a_gap(gone: tuple) -> None:
    made, _ = at("Acknowledged")
    store = Torn(made._store.blobs)
    for number in gone:
        del store.blobs[journal_entry_key(ACCOUNT, number)]
    store.missed = [gone]
    with pytest.raises(Halt, match=f"entry {gone[0]} is missing, and the next key is entry"):
        Journal(store, ACCOUNT).view()


@pytest.mark.parametrize("extra", [0, 1, 3, 7])
def test_7_a_key_that_comes_after_its_own_number_halts_at_every_view(extra: int) -> None:
    made, _ = at("Acknowledged")

    class Extra(Memory):
        def list(self, prefix: str, after: str | None) -> list[str]:
            return [*super().list(prefix, after), journal_entry_key(ACCOUNT, extra)]

    store = Extra(made._store.blobs)
    journal = Journal(store, ACCOUNT)
    for _ in range(3):
        with pytest.raises(Halt, match=f"the listing returned .*{extra}.json after entry 7"):
            journal.view()


def test_7_a_key_numbered_zero_halts_before_any_entry_is_read() -> None:
    made, _ = at("Acknowledged")
    store = Memory(made._store.blobs)
    store.create(journal_entry_key(ACCOUNT, 0), b"{}", "")
    journal = Journal(store, ACCOUNT)
    for _ in range(3):
        with pytest.raises(Halt, match="the listing returned .*000000.json after entry 0"):
            journal.view()
    with pytest.raises(Halt, match="000000.json after entry 0"):
        journal.view(through=3)
    assert store.gets == 0


def test_7_an_entry_read_by_its_own_key_is_checked_as_a_listed_one_is() -> None:
    made, _ = at("Acknowledged")
    key = journal_entry_key(ACCOUNT, 4)
    good = made._store.blobs[key]
    for bad, match in (
        (good.replace(b'"seq":4', b'"seq": 4'), "not a canonical"),
        (good.replace(b'"seq":4', b'"seq":5'), "key of entry 4 holds seq 5"),
        (good.replace(b'"format":1', b'"format":2'), "format 2"),
        (encode(dataclasses.replace(next(made.read(after=3)), prev_hash="f" * 64)), "prev_hash"),
        (made._store.blobs[journal_entry_key(ACCOUNT, 3)], "key of entry 4 holds seq 3"),
    ):
        store = Torn(made._store.blobs | {key: bad})
        store.missed = [(4,)]
        with pytest.raises(Halt, match=match):
            Journal(store, ACCOUNT).view()


def test_7_each_entry_of_a_gap_of_two_is_checked_against_the_one_before_it() -> None:
    made, _ = at("Acknowledged")
    for number in (2, 3):
        entry = next(made.read(after=number - 1))
        bad = encode(dataclasses.replace(entry, prev_hash="f" * 64))
        store = Torn(made._store.blobs | {journal_entry_key(ACCOUNT, number): bad})
        store.missed = [(2, 3)]
        with pytest.raises(Halt, match=f"entry {number}'s prev_hash does not match"):
            Journal(store, ACCOUNT).view()


def test_7_a_store_that_does_not_answer_for_a_missed_entry_is_no_gap() -> None:
    made, _ = at("Acknowledged")

    class Quiet(Torn):
        quiet = 1

        def get(self, key: str) -> bytes:
            if self.quiet and key == journal_entry_key(ACCOUNT, 4):
                self.quiet = 0
                raise StoreUnavailable(key, "no answer")
            return super().get(key)

    store = Quiet(made._store.blobs)
    store.missed = [(4,)]
    journal = Journal(store, ACCOUNT)
    with pytest.raises(StoreUnavailable, match="no answer"):
        journal.view()
    assert shown(journal.view()) == shown(made.view())


def test_7_no_view_ends_short_of_an_entry_its_listing_showed() -> None:
    made, _ = at("Expired")
    end = made.view().through.seq
    draw = random.Random(142)
    for _ in range(500):
        store = Torn(made._store.blobs)
        store.missed = [tuple(n for n in range(1, end + 1) if draw.random() < 0.3)]
        listed = max((n for n in range(1, end + 1) if n not in store.missed[0]), default=0)
        assert Journal(store, ACCOUNT).view().through.seq == listed


def test_a_disputes_text_from_outside_is_cut_where_a_halt_shows_it() -> None:
    journal, _ = at("PartlyFilled")
    long = "USO\n" + "x" * 10_000
    view = accepts(journal, disputed(fill("b1-f1", "b1", 2, instrument=long)))
    with pytest.raises(Halt) as halted:
        _ = view.book
    assert len(halted.value.reason) < 500 and "\n" not in halted.value.reason
    assert view.disputes[0].fill.instrument == long
    for changes in ({"instrument": long}, {"client_order_id": long}):
        journal, _ = at("PartlyFilled")
        with pytest.raises(Halt) as halted:
            journal.record(journal.view(), filled(fill("f2", "b1", 1, **changes)), AT)
        assert len(halted.value.reason) < 500 and "\n" not in halted.value.reason


def test_7_an_append_does_not_wait_on_a_storage_read() -> None:
    """A view stuck in a storage read holds up no entry."""
    reading, go = threading.Event(), threading.Event()

    class Slow(Memory):
        slow = False

        def list(self, prefix: str, after: str | None) -> list[str]:
            if self.slow and threading.current_thread() is stuck:
                reading.set()
                assert go.wait(30)
            return super().list(prefix, after)

    made, name = at("Approved")
    journal = Journal(store := Slow(made._store.blobs), ACCOUNT, WRITER)
    view = journal.view()
    found: list = []
    stuck = threading.Thread(target=lambda: found.append(journal.view()), daemon=True)
    store.slow = True
    stuck.start()
    assert reading.wait(30)
    _, after = journal.record(view, SendingEntry(name), AT)
    assert not found
    go.set()
    stuck.join(60)
    assert found[0].through == after.through


def test_7_a_record_costs_the_same_at_entry_17000_as_at_entry_1000() -> None:
    """By the memory it allocates, since a wall clock on a busy runner says little."""
    journal = Journal(Memory(), ACCOUNT, WRITER)
    _, view = journal.record(journal.view(), opening(), AT)
    peaks: dict[int, tuple[str, int]] = {}

    def put(record: Record, when: datetime) -> None:
        nonlocal view
        # A whole life of nine entries from each number, so the two sets hold the same kinds.
        measured = any(start <= view.through.seq + 1 < start + 9 for start in (1_000, 16_991))
        if getattr(record, "book", 0) is None:
            record = dataclasses.replace(record, book=view.through.seq)
        if measured:
            tracemalloc.start()
        _, view = journal.record(view, record, when)
        if measured:
            peaks[view.through.seq] = (kind_of(record), tracemalloc.get_traced_memory()[1])
            tracemalloc.stop()

    number = 0
    while view.through.seq < 17_000:
        number += 1
        when = AT + number * timedelta(minutes=1)
        order = planned(1, 4 if number % 2 else -4, at=when)
        put(read(when), when)
        put(decision(order, at=when, read=view.through.seq), when)
        decided = view.through.seq
        for record in route("Filled", order, decided=decided, broker=f"b{number}"):
            put(record, when)
    early, late = (
        [peak for seq, peak in peaks.items() if (seq < 2_000) is first] for first in (True, False)
    )
    assert len(early) == len(late) == 9
    print(f"entry 1,000 on: {early}, entry 16,991 on: {late}")
    # By kind, so a verdict, the largest, hides no growth in a smaller one.
    for kind in {kind for kind, _ in early}:
        was, now = (max(p for k, p in found if k == kind) for found in (early, late))
        assert now <= 2 * was, (kind, was, now)


OTHER_WRITER = "f" * 32


@pytest.mark.parametrize("state", ["Intended", "Approved"])
def test_an_order_that_was_never_sending_is_abandoned_by_any_writer(state: str) -> None:
    order = planned(1, 4)
    journal = started(order)
    for record in route(state, order):
        add(journal, record)
    other = Journal(journal._store, ACCOUNT, OTHER_WRITER)
    _, view = other.record(other.view(), abandoned(order.client_order_id), AT)
    assert view.orders[order.client_order_id].state is OrderState.ABANDONED


def test_an_order_that_is_sending_is_abandoned_by_the_writer_of_its_sending_alone() -> None:
    """Nobody else can know that nothing left, and the order would be out of the book."""
    order, after = planned(1, 4), planned(2, 4)
    name = order.client_order_id
    journal = started(order, after)
    for record in route("Sending", order):
        add(journal, record)
    other = Journal(journal._store, ACCOUNT, OTHER_WRITER)
    stored = len(journal._store.blobs)
    with pytest.raises(Halt, match=f"abandons {name}, whose sending another writer stored"):
        other.record(other.view(), abandoned(name), AT)
    assert len(journal._store.blobs) == stored
    # Another writer's entry on another order changes nothing of whose `sending` it was.
    other.record(other.view(), IntentEntry(3, after), AT)
    with pytest.raises(Halt, match="another writer"):
        other.record(other.view(), abandoned(name), AT)
    _, view = journal.record(journal.view(), abandoned(name), AT)
    assert view.orders[name].state is OrderState.ABANDONED
    # A journal read from its first entry gives the same answer, on the stored bytes alone.
    assert Journal(journal._store, ACCOUNT).view().orders[name].state is OrderState.ABANDONED


def test_the_writer_of_each_sending_is_kept_apart() -> None:
    one, two = planned(1, 4), planned(2, 4, USO)
    journal = started(one, two)
    for order in (one, two):
        for record in route("Sending", order):
            add(journal, record)
    for order in (one, two):
        _, view = journal.record(journal.view(), abandoned(order.client_order_id), AT)
        assert view.orders[order.client_order_id].state is OrderState.ABANDONED


def test_an_order_in_doubt_takes_another_answers_bytes_and_stays_in_doubt() -> None:
    order = planned(1, 4)
    journal = started(order)
    for record in route("InDoubt", order):
        add(journal, record)
    _, view = journal.record(journal.view(), in_doubt(order.client_order_id, b"late"), AT)
    found = view.orders[order.client_order_id]
    assert found.state is OrderState.IN_DOUBT and found.entries[-1].kind == "in_doubt"
    assert view.book.in_doubt == (GLD,)


def test_a_stored_abandonment_by_another_writer_halts_the_replay() -> None:
    order = planned(1, 4)
    journal = started(order)
    for record in route("Sending", order):
        add(journal, record)
    body = body_of(abandoned(order.client_order_id), OTHER_WRITER)
    journal._append(journal._build(body, AT))
    with pytest.raises(Halt, match="another writer stored"):
        Journal(journal._store, ACCOUNT).view()


@pytest.mark.parametrize(
    ("asked", "takes"),
    [
        (timedelta(minutes=20, seconds=1), False),
        (timedelta(minutes=20, seconds=2), True),
        (DAY, True),
    ],
    ids=["a second short of twice the clock's bound", "at it", "a day later"],
)
def test_a_lost_names_a_list_asked_for_once_the_time_in_force_ran_out(
    asked: timedelta, takes: bool
) -> None:
    """The order is planned 20 minutes before its session's close, so it may arrive until then."""
    journal, name = at("InDoubt", closing=True)
    seq = add(journal, read(AT + asked))
    if takes:
        assert accepts(journal, lost(name, seq)).orders[name].state is OrderState.LOST
    else:
        refuses(
            journal, lost(name, seq), "before 2026-10-01T20:00:02.+, when the order could still"
        )


def test_a_day_order_is_never_given_up_as_lost() -> None:
    """A broker may hold a day order that came after its close for the next session's open."""
    journal, name = at("InDoubt")
    seq = add(journal, read(AT + DAY))
    refuses(journal, lost(name, seq), "which is no closing-auction order")


def test_a_lost_names_a_read_before_it() -> None:
    journal, name = at("InDoubt", closing=True)
    refuses(journal, lost(name, 99), "names entry 99, which is no read before it")
