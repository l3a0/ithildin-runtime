"""#147: an order in doubt is looked up at the next recover, and never sent again.

Each order here is sent through the real order path to the simulated broker,
with a fault that leaves it in doubt. The next decision's recover finds it in
the broker's order list, looks it up by the broker's id, and journals what that
shows, or halts having settled nothing. Every figure is made up.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from ithildinruntime.core import (
    Ack,
    BlobRef,
    FoundOrder,
    OrderLookup,
    OrderState,
    Outcome,
    TimeInForce,
    decode_output,
)
from ithildinruntime.execution.brokers.faults import Faults, Late, OrderFault, ReadFault
from ithildinruntime.execution.order_path import OVERLAP
from ithildinruntime.journal import lost_from
from tests.support.gates import d as D
from tests.support.recover_world import RecoverWorld
from tests.support.sim_world import GLD, USO, close_of
from tests.test_recover import DAYS, doctored, flat_prices

_S = OrderState
D1, D2, D3 = (close_of(DAYS[n]) for n in (1, 2, 3))


def sent(faults: Faults, *legs: tuple[int, object], then=D2) -> tuple[RecoverWorld, list[str]]:
    """A plan of `legs` sent at the close of day 1 under `faults`, and the decision `then` ready."""
    world = RecoverWorld(flat_prices())
    made = world.decide(D1, world.orders(D1, list(legs) or [(10, GLD)]))
    world.faults = faults
    world.restart()
    for order in made:
        world.send(order)
    world.faults = Faults()
    world.restart()
    world.prepare(then)
    return world, [order.client_order_id for order in made]


def lookups(world: RecoverWorld) -> list[OrderLookup]:
    """Each stored answer of a lookup, in the order it was stored."""
    found = sorted(key for key in world.store.blobs if "/lookup-" in key)
    return [
        decode_output(world.store.blobs[key], OrderLookup).entries[0]
        for key in sorted(found, key=lambda k: int(k.rsplit("-", 1)[1].removesuffix(".json")))
    ]


def states(world: RecoverWorld, *names: str) -> list[OrderState]:
    orders = world.view().orders
    return [orders[name].state for name in names]


def test_an_answer_lost_after_the_broker_took_the_order_is_found_and_acknowledged() -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_ANSWER}))
    assert states(world, name) == [_S.IN_DOUBT]
    calls: list[str] = []
    result = world.attempt(world.stage(brokers=doctored(world, calls=calls)))
    assert result.halt is None, result.halt.reason
    assert calls[:3] == ["find_order", "lookup", "positions"]
    assert "submit" not in calls
    assert states(world, name) == [_S.FILLED]
    assert world.kinds(world.before) == ["read", "read", "ack", "read", "fill"]
    found, answer = lookups(world)
    sending = world.view().orders[name].sent_at
    assert (found.since, found.found.answer, len(found.found.rows)) == (
        sending - OVERLAP,
        Outcome.ACCEPTED,
        1,
    )
    assert answer.answer.broker_order_id == found.found.rows[0].broker_order_id
    # Each stored lookup is listed in the result ahead of the decision's files.
    keys = [ref.key for ref in result.outputs]
    assert [k.rsplit("/", 1)[1] for k in keys[:3]] == [
        "lookup-1.json",
        "lookup-2.json",
        "account.json",
    ]


def test_a_request_that_never_arrived_is_lost_and_the_book_is_flat() -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_REQUEST}))
    assert states(world, name) == [_S.IN_DOUBT]
    # The send alarmed once, when its answer was lost.
    sent_alarms = len(world.alarms.sent)
    result = world.attempt()
    assert result.halt is None, result.halt.reason
    assert states(world, name) == [_S.LOST]
    assert world.kinds(world.before) == ["read", "lost", "read"]
    assert world.entry("book").open_orders_by_strategy == {}
    assert world.entry("book").in_doubt == ()
    (alarm,) = world.alarms.sent[sent_alarms:]
    assert f"the order {name} is lost: the broker's whole order list" in alarm
    assert alarm.endswith("Whether it was sent is unproved")


def test_a_late_order_that_arrived_in_its_session_is_found_and_one_that_missed_it_is_lost() -> None:
    first, second = _name(D1, 1), _name(D1, 2)
    world, names = sent(Faults(orders={first: Late(1), second: Late(1000)}), (10, GLD), (-20, USO))
    assert names == [first, second]
    # A read at the close of day 1 brings the first in, ahead of its close.
    world.clock.at = D1
    world.restart()
    world.faults = Faults(orders={first: Late(1), second: Late(1000)})
    world.broker().open_orders()
    world.faults = Faults()
    world.restart()
    world.clock.at = D2
    result = world.attempt()
    assert result.halt is None, result.halt.reason
    assert states(world, first, second) == [_S.FILLED, _S.LOST]


def test_a_list_that_fails_settles_nothing_and_stores_nothing() -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_ANSWER}))
    world.faults = Faults(reads={("orders", 1): ReadFault.FAILS})
    world.restart()
    result = world.attempt()
    assert result.halt is not None
    assert result.halt.reason.startswith(
        f"account sim-1: the order list that maps {name} could not be read: the broker's "
        "find_order failed with TimeoutError, so nothing settles it"
    )
    assert world.kinds(world.before) == []
    assert lookups(world) == []
    assert states(world, name) == [_S.IN_DOUBT]
    # The alarm names the read.
    assert any("could not be read" in alarm for alarm in world.alarms.sent)
    # The next attempt reads the list whole and settles it.
    assert world.attempt().halt is None
    assert states(world, name) == [_S.FILLED]


def test_a_list_that_does_not_come_whole_is_stored_and_settles_nothing() -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_REQUEST}))
    world.faults = Faults(reads={("orders", 1): ReadFault.SHORT})
    world.restart()
    result = world.attempt()
    assert result.halt is not None and "did not come whole, as " in result.halt.reason
    assert world.kinds(world.before) == ["read"]
    ((found,),) = [lookups(world)]
    assert found.found.whole_list is False
    assert states(world, name) == [_S.IN_DOUBT]


def test_a_list_asked_for_before_the_order_could_no_longer_arrive_keeps_it_in_doubt() -> None:
    world, (name,) = sent(
        Faults(orders={_name(D1, 1): OrderFault.LOSE_REQUEST}), then=D1 + timedelta(seconds=1)
    )
    result = world.attempt()
    assert result.halt is not None
    assert f"the order {name} is in no row of the broker's whole order list" in result.halt.reason
    assert "and may still arrive until " in result.halt.reason
    assert states(world, name) == [_S.IN_DOUBT]


def _row(world: RecoverWorld, name: str):
    """The broker's row of an order a lost answer left in doubt."""
    since = world.view().orders[name].sent_at - OVERLAP
    (row,) = [o for o in world.broker().orders(since).orders if o.client_order_id == name]
    return row


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"qty": D("11")}, "differs in qty"),
        ({"instrument": USO}, "differs in instrument"),
        ({"client_order_id": None}, "differs in client_order_id"),
        ({"type": None}, "differs in type"),
        ({"time_in_force": TimeInForce.DAY}, "differs in time_in_force"),
    ],
    ids=["shares", "instrument", "no client order id", "type", "time in force"],
)
def test_a_row_that_differs_is_kept_as_unmatched_and_halts(change: dict, said: str) -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_ANSWER}))
    row = replace(_row(world, name), **change)
    answer = FoundOrder(Outcome.ACCEPTED, (row,), True, b"[]")
    result = world.attempt(world.stage(brokers=doctored(world, find_order=lambda *a: answer)))
    assert result.halt is not None and said in result.halt.reason
    assert world.kinds(world.before) == ["read", "unmatched_order"]
    assert states(world, name) == [_S.IN_DOUBT]
    # The account stays held, and nothing is looked up by the row's id.
    again = world.attempt(world.stage(brokers=doctored(world, find_order=lambda *a: answer)))
    assert again.halt is not None


def test_two_rows_for_one_order_are_each_kept_as_unmatched_and_halt() -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_ANSWER}))
    row = _row(world, name)
    other = replace(row, broker_order_id="sim-o-99")
    answer = FoundOrder(Outcome.ACCEPTED, (row, other), True, b"[]")
    result = world.attempt(world.stage(brokers=doctored(world, find_order=lambda *a: answer)))
    assert result.halt is not None and "answers accepted with 2 rows" in result.halt.reason
    assert world.kinds(world.before) == ["read", "unmatched_order", "unmatched_order"]


def test_a_lookup_that_fails_or_disagrees_keeps_the_order_in_doubt() -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_ANSWER}))

    def failing(broker_order_id: str) -> Ack:
        raise TimeoutError("PKFAKEKEY")

    result = world.attempt(world.stage(brokers=doctored(world, lookup=failing)))
    assert result.halt is not None
    assert "by the broker's sim-o-1 failed: the broker's lookup failed with TimeoutError" in (
        result.halt.reason
    )
    assert "PKFAKEKEY" not in result.halt.reason
    assert states(world, name) == [_S.IN_DOUBT]
    real = world.broker().lookup("sim-o-1")
    bigger = replace(real, order=replace(real.order, qty=D("11")))
    result = world.attempt(world.stage(brokers=doctored(world, lookup=lambda b: bigger)))
    assert result.halt is not None and "differs in qty" in result.halt.reason
    assert world.kinds(world.before)[-1] == "unmatched_order"
    assert states(world, name) == [_S.IN_DOUBT]


def test_a_fill_on_an_order_given_up_as_lost_is_kept_as_unmatched_and_halts() -> None:
    """The list omitted the order, and the broker had it all the same."""
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_ANSWER}))
    nothing = FoundOrder(Outcome.NOT_FOUND, (), True, b"[]")
    result = world.attempt(world.stage(brokers=doctored(world, find_order=lambda *a: nothing)))
    assert states(world, name) == [_S.LOST]
    assert result.halt is not None
    assert "which no order was acknowledged under" in result.halt.reason
    assert "unmatched_fill" in world.kinds(world.before)


def _name(at, place: int) -> str:
    return f"ith-chan-{at.strftime('%Y%m%dT%H%M%SZ')}-{place:02d}"


@pytest.mark.parametrize(
    ("answer", "said", "kinds"),
    [
        (lambda row: FoundOrder(Outcome.ACCEPTED, (), True, b"[]"), "accepted with no row", []),
        (
            lambda row: FoundOrder(Outcome.NOT_FOUND, (row,), True, b"[]"),
            "not_found with 1 rows",
            ["unmatched_order"],
        ),
        (lambda row: FoundOrder(Outcome.UNKNOWN, (), True, b"[]"), "unknown with no row", []),
        (
            lambda row: FoundOrder(Outcome.ACCEPTED, (replace(row, qty=-row.qty),), True, b"[]"),
            "differs in qty",
            ["unmatched_order"],
        ),
        (
            lambda row: FoundOrder(
                Outcome.ACCEPTED, (replace(row, broker_order_id="no id here"),), True, b"[]"
            ),
            "a row's broker id is one the journal refuses",
            [],
        ),
    ],
    ids=[
        "accepted with no row",
        "not found with a row",
        "unknown with no row",
        "the other side",
        "a broker id the journal refuses",
    ],
)
def test_a_list_answer_of_any_other_shape_settles_nothing(answer, said: str, kinds: list) -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_ANSWER}))
    found = answer(_row(world, name))
    result = world.attempt(world.stage(brokers=doctored(world, find_order=lambda *a: found)))
    assert result.halt is not None and said in result.halt.reason
    assert world.kinds(world.before) == ["read", *kinds]
    assert states(world, name) == [_S.IN_DOUBT]


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (
            lambda ack: replace(ack, outcome=Outcome.REJECTED),
            "answers rejected, as ",
        ),
        (
            lambda ack: replace(ack, broker_order_id="sim-o-99", order=replace(ack.order)),
            "answers accepted, as ",
        ),
        (
            lambda ack: replace(ack, order=replace(ack.order, broker_order_id="sim-o-99")),
            "differs in broker_order_id",
        ),
    ],
    ids=["rejected with a copy", "under another id", "a copy under another id"],
)
def test_a_lookup_by_the_broker_id_that_answers_otherwise_keeps_the_order_in_doubt(
    change, said: str
) -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_ANSWER}))
    answer = change(world.broker().lookup("sim-o-1"))
    result = world.attempt(world.stage(brokers=doctored(world, lookup=lambda b: answer)))
    assert result.halt is not None and said in result.halt.reason, result.halt.reason
    assert "ack" not in world.kinds(world.before)
    assert states(world, name) == [_S.IN_DOUBT]


@pytest.mark.parametrize(
    ("after", "state"),
    [(timedelta(0), _S.LOST), (-timedelta(microseconds=1), _S.IN_DOUBT)],
    ids=["at the first moment the order cannot arrive", "a microsecond before it"],
)
def test_the_list_gives_an_order_up_from_its_close_plus_twice_the_clock_bound(after, state) -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_REQUEST}))
    # A decision time is whole seconds, and the list is asked for at the clock's time.
    world.prepare(lost_from(name) - timedelta(seconds=1))
    world.clock.at = lost_from(name) + after
    world.attempt()
    assert states(world, name) == [state]


def test_the_list_is_read_from_a_minute_before_the_sending() -> None:
    """The broker dates an order by a clock of its own, which can run behind the platform's."""
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_ANSWER}))
    world.attempt()
    found, _ = lookups(world)
    assert found.since == world.view().orders[name].sent_at - timedelta(seconds=60)


def test_a_list_that_never_says_it_is_whole_is_not_stored_and_settles_nothing() -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_REQUEST}))
    found = FoundOrder(Outcome.NOT_FOUND, (), None, b"[]")  # type: ignore[arg-type]
    result = world.attempt(world.stage(brokers=doctored(world, find_order=lambda *a: found)))
    assert result.halt is not None and "is no stored lookup" in result.halt.reason
    assert world.kinds(world.before) == []
    assert states(world, name) == [_S.IN_DOUBT]


def test_a_stored_answer_is_named_by_one_read_entry_however_often_it_is_kept() -> None:
    world, (name,) = sent(Faults(orders={_name(D1, 1): OrderFault.LOSE_ANSWER}))
    ref = BlobRef("runs/a/lookup-1.json", "a" * 64)
    found = OrderLookup(name, None, D2, None, world.broker().lookup("sim-o-1"))
    path = world.path()
    first = path._kept(lambda f: ref, found, "execution/recover", D2)
    again = path._kept(lambda f: ref, found, "execution/recover", D2)
    assert first == again
    assert [e.ref for e in world.view().reads.values()].count(ref) == 1
