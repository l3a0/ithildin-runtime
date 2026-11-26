"""The fold of a broker's record and of what the platform did not send, #151.

`outside_record` moves the book's cash by its amount. The three unmatched kinds
move nothing and hold the account, so the book halts while any stands. Every
figure is made up.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    BrokerState,
    Halt,
    InstrumentId,
    OrderType,
    RecordKind,
    TimeInForce,
)
from ithildinruntime.journal import Journal, Repeat
from tests.support.journals import (
    AT,
    GLD,
    add,
    broker_record,
    disputed,
    fill,
    filled,
    foreign,
    outside,
    planned,
    route,
    started,
    unmatched_fill,
    unmatched_order,
    unmatched_record,
)
from tests.test_journal_book import accepts

ONE = planned(1, 4)
NAME = ONE.client_order_id
CASH = Decimal(100_000)


def journal(*records: object) -> Journal:
    made = started(ONE)
    for record in records:
        add(made, record)  # type: ignore[arg-type]
    return made


def test_an_outside_record_moves_the_cash_by_its_amount_and_nothing_else() -> None:
    fee = broker_record("r::1", "-1.25")
    dividend = broker_record("r::2", "3.5", RecordKind.DIVIDEND, instrument=GLD)
    view = journal(outside(fee), outside(dividend)).view()
    book = view.book
    assert book.cash == CASH - Decimal("1.25") + Decimal("3.5")
    assert book.positions_by_strategy == {}
    # A listed record is no correction.
    assert book.corrections == ()
    assert dict(view.records) == {"r::1": fee, "r::2": dividend}


def test_an_identical_repeat_of_an_outside_record_moves_nothing_more() -> None:
    fee = broker_record()
    later = dataclasses.replace(fee, received_at=AT.replace(hour=23))
    assert journal(outside(fee), outside(later)).view().book.cash == CASH - Decimal("1.25")


def test_an_outside_record_that_differs_from_the_one_applied_halts() -> None:
    fee = broker_record()
    with pytest.raises(Halt, match="entry 5's record r::1 holds -1.30 as a fee, dated"):
        journal(outside(fee), outside(dataclasses.replace(fee, amount=Decimal("-1.30")))).view()


def test_an_outside_record_of_an_id_kept_as_unmatched_halts() -> None:
    kept = broker_record(amount="-60")
    with pytest.raises(Halt, match="entry 5 applies the record r::1, which entry 4 keeps as"):
        journal(unmatched_record(kept), outside(broker_record())).view()


def test_an_outside_record_names_a_read_before_it() -> None:
    with pytest.raises(Halt, match="names entry 9, which is no read before it"):
        journal(outside(broker_record(), read=9)).view()


@pytest.mark.parametrize(
    ("entry", "said"),
    [
        (
            unmatched_record(broker_record(amount="5000", kind=RecordKind.DEPOSIT)),
            "entry 4 keeps an unmatched record",
        ),
        (unmatched_fill(fill("f1", "x1")), "entry 4 keeps an unmatched fill"),
        (unmatched_order(foreign()), "entry 4 keeps an unmatched order"),
    ],
)
def test_an_unmatched_entry_moves_nothing_and_the_book_halts_while_it_stands(
    entry: object, said: str
) -> None:
    view = journal(entry).view()
    assert view._cash == CASH and dict(view._positions) == {}
    with pytest.raises(Halt) as halted:
        _ = view.book
    assert said in halted.value.reason
    assert "which holds the account until a person releases it" in halted.value.reason
    # Every other entry can still be journaled beside it.
    after = journal(entry, *route("Sending", ONE))
    assert after.view().orders[NAME].state.value == "Sending"


def test_a_dispute_and_an_unmatched_entry_are_both_named() -> None:
    made = journal(*route("Acknowledged", ONE))
    part = fill("f1", "b1", 1)
    add(made, filled(part))
    add(made, disputed(dataclasses.replace(part, qty=Decimal(2))))
    add(made, unmatched_order(foreign()))
    with pytest.raises(Halt) as halted:
        _ = made.view().book
    reason = halted.value.reason
    assert "the fill f1 is in dispute" in reason and "keeps an unmatched order" in reason
    assert reason.index("in dispute") < reason.index("unmatched order")


def test_an_unmatched_repeat_with_the_content_held_halts() -> None:
    with pytest.raises(Halt, match="keeps the order x1 with the content entry 4 holds"):
        journal(unmatched_order(foreign()), unmatched_order(foreign(filled=Decimal(3)))).view()
    with pytest.raises(Halt, match="keeps the fill f1 with the content entry 4 holds"):
        journal(unmatched_fill(fill("f1", "x1")), unmatched_fill(fill("f1", "x1"))).view()


def test_an_unmatched_repeat_with_other_content_is_kept_beside_the_first() -> None:
    view = journal(
        unmatched_fill(fill("f1", "x1")), unmatched_fill(fill("f1", "x1", price="11"))
    ).view()
    assert [(one.seq, one.name) for one in view.unmatched] == [(4, "f1"), (5, "f1")]


def test_a_fill_kept_as_unmatched_is_never_applied_after() -> None:
    made = journal(*route("Acknowledged", ONE))
    add(made, unmatched_fill(fill("f1", "x1")))
    add(made, filled(fill("f1", "b1", 1)))
    with pytest.raises(Halt, match="fill f1 is one the journal keeps as unmatched"):
        made.view()


def test_a_fill_applied_is_never_kept_as_unmatched_after() -> None:
    made = journal(*route("Acknowledged", ONE))
    add(made, filled(fill("f1", "b1", 1)))
    add(made, unmatched_fill(fill("f1", "b1", 1)))
    with pytest.raises(Halt, match="keeps the fill f1 as unmatched, which an entry applied"):
        made.view()


def test_an_order_an_ack_mapped_is_never_kept_as_unmatched_and_the_reverse() -> None:
    made = journal(*route("Acknowledged", ONE, broker="x1"))
    add(made, unmatched_order(foreign("x1")))
    with pytest.raises(Halt, match="keeps the order x1 as unmatched, which an ack mapped"):
        made.view()
    made = journal(unmatched_order(foreign("x1")), *route("Acknowledged", ONE, broker="x1"))
    with pytest.raises(Halt, match="the broker's id x1, an order entry 4 keeps as one the"):
        made.view()


def test_a_record_applied_is_never_kept_as_unmatched_with_its_content() -> None:
    fee = broker_record()
    with pytest.raises(Halt, match="keeps the record r::1 as unmatched with the content applied"):
        journal(outside(fee), unmatched_record(fee)).view()


def test_a_fill_on_an_order_that_ended_is_no_orders_the_platform_sent() -> None:
    made = journal(*route("Expired unfilled", ONE))
    view = made.view()
    assert view.unowned(fill("f1", "b1", 1)) == (
        f"fill f1 is of the order {NAME}, which is Expired"
    )
    add(made, filled(fill("f1", "b1", 1)))
    with pytest.raises(Halt, match=f"entry \\d+'s fill f1 is of the order {NAME}, which is"):
        made.view()


def test_a_read_entry_is_needed_before_any_unmatched_entry() -> None:
    with pytest.raises(Halt, match="names entry 9, which is no read before it"):
        journal(unmatched_order(foreign(), read=9)).view()


# What a repeat must repeat: every field of its content, one at a time

RECORD_CHANGES = {
    "kind": RecordKind.INTEREST,
    "amount": Decimal("-1.26"),
    "instrument": GLD,
    "at": AT + timedelta(seconds=1),
}
ORDER_CHANGES = {
    "client_order_id": NAME,
    "instrument": InstrumentId("USO"),
    "qty": Decimal(8),
    "type": OrderType.MARKET,
    "time_in_force": TimeInForce.DAY,
    "submitted_at": AT + timedelta(seconds=1),
}
FILL_CHANGES = {
    "broker_order_id": "x2",
    "instrument": InstrumentId("USO"),
    "qty": Decimal(3),
    "price": Decimal("10.26"),
    "at": AT + timedelta(seconds=1),
}


@pytest.mark.parametrize("field", RECORD_CHANGES)
def test_a_record_that_repeats_an_id_with_any_field_of_its_content_changed_differs(
    field: str,
) -> None:
    fee = broker_record()
    other = dataclasses.replace(fee, **{field: RECORD_CHANGES[field]})
    assert journal(outside(fee)).view().record_repeats(other) is Repeat.DIFFERS
    assert journal(unmatched_record(fee)).view().record_repeats(other) is Repeat.DIFFERS


@pytest.mark.parametrize("field", ORDER_CHANGES)
def test_an_unmatched_order_listed_with_any_field_of_its_content_changed_differs(
    field: str,
) -> None:
    order = foreign()
    other = dataclasses.replace(order, **{field: ORDER_CHANGES[field]})
    view = journal(unmatched_order(order)).view()
    assert view.unmatched_repeats("order", "x1", other) is Repeat.DIFFERS


@pytest.mark.parametrize("field", FILL_CHANGES)
def test_an_unmatched_fill_listed_with_any_field_of_its_content_changed_differs(
    field: str,
) -> None:
    made = fill("f1", "x1")
    other = dataclasses.replace(made, **{field: FILL_CHANGES[field]})
    view = journal(unmatched_fill(made)).view()
    assert view.unmatched_repeats("fill", "f1", other) is Repeat.DIFFERS


def test_what_a_repeat_need_not_repeat_leaves_it_held() -> None:
    fee, order, made = broker_record(), foreign(), fill("f1", "x1")
    later = AT + timedelta(hours=1)
    view = journal(outside(fee), unmatched_order(order), unmatched_fill(made)).view()
    assert view.record_repeats(dataclasses.replace(fee, received_at=later)) is Repeat.HELD
    moved = dataclasses.replace(order, filled=Decimal(3), state=BrokerState.FILLED)
    assert view.unmatched_repeats("order", "x1", moved) is Repeat.HELD
    again = dataclasses.replace(made, received_at=later, client_order_id=NAME)
    assert view.unmatched_repeats("fill", "f1", again) is Repeat.HELD


def test_an_older_view_shows_no_record_applied_after_it() -> None:
    made = journal(outside(broker_record()))
    whole = made.view()
    older = made.view(through=whole.through.seq - 1)
    assert dict(older.records) == {} and dict(whole.records) == {"r::1": broker_record()}
    assert older.book.cash == CASH


def test_each_new_kind_replays_to_what_its_record_gave() -> None:
    made = journal()
    for record in (
        outside(broker_record()),
        unmatched_record(broker_record("r::2", 5000, RecordKind.DEPOSIT)),
        unmatched_fill(fill("f1", "x1")),
        unmatched_order(foreign()),
    ):
        accepts(made, record)
