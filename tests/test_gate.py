"""The risk gate: each limit at its boundary, every halt, and orders that only reduce.

docs/architecture/05-building-blocks.md's `#### The risk gate holds no state of
its own` says what the gate reads, and `##### What each limit compares` how each
limit counts. Figures here are made up and small enough to check by hand.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal, localcontext

import pytest

from ithildinruntime import core
from ithildinruntime.core import (
    CLOCK_BOUND,
    Bar,
    BarStatus,
    Book,
    Correction,
    Halt,
    InstrumentId,
    JournalEntry,
    Limit,
    OrderType,
    Snapshot,
    StopState,
    TimeInForce,
    mark,
    quote_faults,
    value_book,
)
from ithildinruntime.risk import Gate
from tests.support.gates import (
    CENT,
    CHAN,
    CLOSE,
    DECISION,
    GLD,
    LOOSE,
    OTHER,
    PRIOR,
    QTY,
    SPY,
    TABLE,
    TODAY,
    USO,
    World,
    book,
    caps,
    d,
    inputs,
    moc,
    order,
    quoted,
    snapshot,
    stress_record,
    utc,
    valuation,
)
from tests.support.quotes import MAX_AGE, quote

TICK = timedelta(microseconds=1)
ENTRY = JournalEntry(seq=1, format=1, built_at=DECISION, body={"kind": "close"}, prev_hash="0" * 64)


def refusal(world: World) -> tuple[Limit | None, Decimal | None, Decimal | None]:
    verdict = world.check()
    return verdict.limit, verdict.value, verdict.bound


def test_the_gate_is_a_risk_gate_that_keeps_its_records_alone() -> None:
    records = (stress_record(),)
    gate: core.RiskGate = Gate(records)
    assert vars(gate) == {"_records": records}
    world = World()
    assert world.check() == world.check()


# 1. The seven limits, one step below, at and above each, from either source.

BOTH = {GLD: LOOSE, USO: LOOSE}


def _order_qty(world: World, off: int) -> World:
    return world.at(intent=order(10 + off * QTY))


def _position(world: World, off: int) -> World:
    return world.at(book=book({CHAN: {GLD: 40}}), intent=order(10 + off * QTY))


def _gross(world: World, off: int) -> World:
    return world.at(book=book({CHAN: {USO: -40}}), intent=order(10 + off * CENT))


def _net(world: World, off: int) -> World:
    return world.at(book=book({CHAN: {USO: 40}}), intent=order(10 + off * CENT))


def _count(world: World, off: int) -> World:
    return world.at(book=book(opens=[order(1, name=f"old-{n}") for n in range(3 + off)]))


def _loss(world: World, off: int) -> World:
    return world.losing(50 + off * CENT)


def _late(world: World, off: int) -> World:
    return world.at(intent=moc(1), now=CLOSE - timedelta(minutes=12) + off * TICK)


type Move = Callable[[World, int], World]
SEVEN: dict[str, tuple[Move, object, object, Decimal, Decimal]] = {
    # The move, the tight value, the loose value, then the figure and bound a refusal names.
    "max_order_qty": (_order_qty, {GLD: d(10), USO: d(10)}, BOTH, 10 + QTY, d(10)),
    "max_position": (_position, {GLD: d(50), USO: d(50)}, BOTH, 50 + QTY, d(50)),
    "max_gross": (_gross, d(50), LOOSE, 50 + CENT, d(50)),
    "max_open_orders": (_count, 4, 100, d(5), d(4)),
    "max_net": (_net, d(50), LOOSE, 50 + CENT, d(50)),
    "loss_stop": (_loss, d(50), LOOSE, 50 + CENT, d(50)),
    # A microsecond short of twelve minutes, to nine places.
    "moc_cutoff": (_late, 12, 0, d("11.999999983"), d(12)),
}


def _held_to(world: World, source: str, limit: str, value: object) -> World:
    if source == "caps":
        world = world.capped(**{limit: value})
    else:
        world = world.held_to(**{limit: value})
    if limit == "max_order_qty":
        # No source may cap an order below a position, so the two move together.
        return _held_to(world, source, "max_position", value)
    return world


@pytest.mark.parametrize("source", ["caps", "constraints"])
@pytest.mark.parametrize("limit", [name for name in SEVEN if name != "max_net"])
def test_1_each_limit_one_step_below_at_and_above(limit: str, source: str) -> None:
    move, tight, loose, value, bound = SEVEN[limit]
    for off, named in ((-1, (None,) * 3), (0, (None,) * 3), (1, (Limit(limit), value, bound))):
        world = move(World(), off)
        assert refusal(_held_to(world, source, limit, tight)) == named, off
        assert _held_to(world, source, limit, loose).check().approved, off


def test_1_the_net_cap_one_step_below_at_and_above_as_a_limit_and_from_v() -> None:
    move, tight, _, value, bound = SEVEN["max_net"]
    for off, named in ((-1, (None,) * 3), (0, (None,) * 3), (1, (Limit.MAX_NET, value, bound))):
        world = move(World(), off)
        assert refusal(world.held_to(max_net=tight)) == named, off
        # A tenth of a V of 100, over a stress move of a fifth, is 50.
        small = world.capped(share=d("0.1")).given(valuation=valuation(100))
        assert refusal(small) == named, off
        assert world.check().approved and small.capped(share=d("0.5")).check().approved


def test_1_the_figure_is_compared_exactly_and_recorded_rounded() -> None:
    """A gross a hair past its bound is refused though its record reads equal to the bound."""
    world = World(
        intent=order("0.000000001"),
        snapshot=snapshot({GLD: "0.0001", USO: 1}),
        book=book({CHAN: {USO: 50}}),
    ).held_to(max_gross=d(50))
    verdict = world.check()
    assert (verdict.limit, verdict.value, verdict.bound) == (Limit.MAX_GROSS, d(50), d(50))


def test_1_a_pairs_second_leg_alone_breaks_the_net_cap() -> None:
    pair = World(
        book=book(opens=[order(40, GLD, name="leg-1")]), intent=order(-60, USO, name="leg-2")
    ).held_to(max_net=d(50))
    assert refusal(pair) == (Limit.MAX_NET, d(60), d(50))
    # Both legs filled come to a net of 20, and a leg of 50 fits alone.
    assert pair.at(intent=order(-50, USO)).check().approved


def test_1_an_order_beside_one_in_doubt_is_refused_whichever_strategy_sent_it() -> None:
    doubtful = order(1, strategy=OTHER, name="old-1")
    world = World(book=book(opens=[doubtful], in_doubt=(GLD,)))
    assert refusal(world) == (Limit.IN_DOUBT, None, None)
    assert world.at(intent=order(-1)).check().limit is Limit.IN_DOUBT
    assert world.at(intent=order(1, USO)).check().approved
    assert world.at(book=book(opens=[doubtful])).check().approved
    # A pair whose two sends both got no answer leaves both legs in doubt.
    both = book(opens=[doubtful, order(-1, USO, name="old-2")], in_doubt=(USO, GLD))
    for leg in (GLD, USO):
        assert world.at(book=both, intent=order(1, leg)).check().limit is Limit.IN_DOUBT


def test_1_an_order_naming_no_strategy_is_refused_for_its_position() -> None:
    world = World(intent=order(1, strategy=""))
    verdict = world.check()
    assert (verdict.limit, verdict.missing_term) == (Limit.POSITION, "position")
    assert (verdict.value, verdict.bound, verdict.worst_loss) == (None, None, None)
    assert verdict.adding
    assert World(intent=order(1)).check().approved
    # It adds even where the account's position shrinks, because no strategy's does.
    assert world.at(book=book({CHAN: {GLD: 5}}), intent=order(-1, strategy="")).check().adding


def test_1_an_order_with_a_close_entry_is_refused_until_117_reads_one() -> None:
    world = World().given(close_entry=ENTRY)
    assert refusal(world) == (Limit.CLOSE_ENTRY, None, None)
    flat = world.at(book=book({CHAN: {GLD: 1}}), intent=order(-1))
    assert flat.check().limit is Limit.CLOSE_ENTRY
    assert World().check().approved


def test_1_a_replacement_counts_the_order_it_replaces_until_its_cancel_settles() -> None:
    old = order(30, name="old-1")
    world = World(book=book(opens=[old]), intent=order(30, name="new-1")).held_to(
        max_position={GLD: d(50), USO: d(50)}
    )
    assert refusal(world) == (Limit.MAX_POSITION, d(60), d(50))
    # Part filled during its cancel: what filled is held, and the rest stays open.
    part = book({CHAN: {GLD: 12}}, opens=[order(18, name="old-1")])
    assert refusal(world.at(book=part)) == (Limit.MAX_POSITION, d(60), d(50))
    assert world.at(book=book()).check().approved
    # Filled whole during its cancel, all of it is held.
    whole = world.at(book=book({CHAN: {GLD: 30}}))
    assert refusal(whole) == (Limit.MAX_POSITION, d(60), d(50))


EARLY = date(2026, 11, 27)


def test_1_the_cutoff_follows_an_early_close() -> None:
    close = TABLE.session(EARLY).close
    assert close == utc(2026, 11, 27, 18, 0)
    at = close - timedelta(minutes=20)
    world = World(
        intent=moc(1),
        snapshot=snapshot({GLD: 1, USO: 1}, at=at, day=EARLY),
        now=close - timedelta(minutes=12),
    ).capped(moc_cutoff=12)
    world = world.given(valuation=valuation(session=TABLE.before(EARLY).day))
    assert world.check().approved
    assert world.at(now=world.now + TICK).check().limit is Limit.MOC_CUTOFF


def test_1_a_day_order_passes_after_the_cutoff() -> None:
    world = World(intent=moc(1), now=CLOSE - timedelta(minutes=5)).capped(moc_cutoff=12)
    assert refusal(world) == (Limit.MOC_CUTOFF, d(5), d(12))
    assert world.at(intent=order(1)).check().approved


def test_1_a_cutoff_is_one_instant_and_one_that_is_no_time_halts() -> None:
    late = World(intent=moc(1)).held_to(moc_cutoff=10**6)
    assert late.check().limit is Limit.MOC_CUTOFF
    # It halts whatever the order is, so that a halt comes before every refusal.
    for intent in (moc(1), order(1), order(LOOSE + 1)):
        with pytest.raises(Halt, match="1000000000000000 minutes before the close is no time"):
            World(intent=intent).held_to(moc_cutoff=10**15).check()


def test_1_a_wide_book_names_the_first_limit_in_the_tables_order() -> None:
    """Breaking every limit at once names each in turn as the one before is loosened."""
    world = (
        World(
            intent=moc(100),
            book=book(opens=[order(1, name="old-1")]),
            now=CLOSE - timedelta(minutes=1),
        )
        .losing(50)
        .held_to(
            max_order_qty={GLD: d(10), USO: d(10)},
            max_position={GLD: d(10), USO: d(10)},
            max_gross=d(10),
            max_open_orders=1,
            max_net=d(10),
            loss_stop=d(10),
            moc_cutoff=12,
        )
    )
    world = world.given(close_entry=ENTRY, stop=StopState.FIRED)
    world = world.at(
        book=replace(world.book, in_doubt=(GLD,)), intent=replace(world.intent, strategy="")
    )
    steps: list[tuple[Limit, Callable[[World], World]]] = [
        (Limit.STOP, lambda w: w.given(stop=StopState.ABSENT)),
        (Limit.CLOSE_ENTRY, lambda w: w.given(close_entry=None)),
        (Limit.IN_DOUBT, lambda w: w.at(book=replace(w.book, in_doubt=()))),
        (Limit.POSITION, lambda w: w.at(intent=replace(w.intent, strategy=CHAN))),
        (Limit.MAX_ORDER_QTY, lambda w: w.held_to(max_order_qty=BOTH)),
        (Limit.MAX_POSITION, lambda w: w.held_to(max_position=BOTH)),
        (Limit.MAX_GROSS, lambda w: w.held_to(max_gross=LOOSE)),
        (Limit.MAX_OPEN_ORDERS, lambda w: w.held_to(max_open_orders=100)),
        (Limit.MAX_NET, lambda w: w.held_to(max_net=LOOSE)),
        (Limit.LOSS_STOP, lambda w: w.held_to(loss_stop=LOOSE)),
        (Limit.MOC_CUTOFF, lambda w: w.held_to(moc_cutoff=0)),
    ]
    assert [limit for limit, _ in steps[4:]] == list(Limit)[:7]
    for limit, loosen in steps:
        verdict = world.check()
        assert verdict.limit is limit
        # 6. Whichever limit is named, the verdict still carries the loss past its bound.
        assert verdict.loss.past is (world.constraints.loss_stop == d(10))
        world = loosen(world)
    assert world.check().approved


def test_1_a_holding_the_constraints_do_not_limit_still_counts() -> None:
    """A strategy's constraints need not name another's instruments, and the account holds both."""
    marks = snapshot({GLD: 1, SPY: 2})
    other = World(book=book({OTHER: {SPY: 20}}), snapshot=marks, intent=order(10))
    for change, limit, figure in (
        ({"max_gross": d(49)}, Limit.MAX_GROSS, 50),
        ({"max_net": d(49)}, Limit.MAX_NET, 50),
    ):
        assert refusal(other.held_to(**change)) == (limit, figure, 49)
    assert other.check().worst_loss == 2
    mine = other.at(book=book({CHAN: {SPY: 20}}))
    assert mine.check().worst_loss == 10
    # An open order alone counts too.
    resting = other.at(book=book(opens=[order(-20, SPY, OTHER, name="old-1")]))
    assert refusal(resting.held_to(max_gross=d(49))) == (Limit.MAX_GROSS, 50, 49)
    fallen = other.losing(0).at(snapshot=snapshot({GLD: 1, SPY: 1}))
    assert fallen.loss().loss == fallen.check().loss.loss == 20


# 2. Orders that only reduce.


@dataclasses.dataclass(frozen=True)
class Row:
    """One row of #7's table: the holdings, the working order, the order and the answer."""

    limit: Limit
    held: dict[InstrumentId, int]
    working: tuple[InstrumentId, int] | None
    intent: tuple[InstrumentId, int]
    adds: bool
    figure: int
    refused: bool


TABLE_15 = {
    "a-sale-of-30": Row(Limit.MAX_POSITION, {GLD: 100}, None, (GLD, -30), False, 100, False),
    "a-sale-of-100": Row(Limit.MAX_POSITION, {GLD: 100}, None, (GLD, -100), False, 100, False),
    "a-sale-of-101": Row(Limit.MAX_POSITION, {GLD: 100}, None, (GLD, -101), True, 100, True),
    "gross-one-leg-out": Row(
        Limit.MAX_GROSS, {GLD: 100, USO: 60}, None, (GLD, -100), False, 160, False
    ),
    "net-shrinks": Row(Limit.MAX_NET, {GLD: 100, USO: -30}, None, (GLD, -100), False, 70, False),
    "net-shrinks-and-turns": Row(
        Limit.MAX_NET, {GLD: 120, USO: -55}, None, (GLD, -120), False, 65, False
    ),
    "a-hedge-out-first": Row(
        Limit.MAX_NET, {GLD: 100, USO: -30}, (GLD, -100), (USO, 30), False, 100, True
    ),
    "net-grows": Row(Limit.MAX_NET, {GLD: 100, USO: -60}, None, (GLD, -100), False, 60, True),
    "a-larger-breach-hides-it": Row(
        Limit.MAX_NET, {GLD: 60, USO: -60}, (GLD, 100), (GLD, -60), False, 60, True
    ),
    "a-missing-leg": Row(Limit.MAX_NET, {GLD: 100}, None, (USO, -100), True, 100, True),
}


def _row(row: Row, flip: int, limit: Decimal) -> World:
    opens = [order(flip * row.working[1], row.working[0], name="old-1")] if row.working else []
    world = World(
        book=book({CHAN: {i: flip * q for i, q in row.held.items()}}, opens=opens),
        intent=order(flip * row.intent[1], row.intent[0]),
    )
    name = str(row.limit)
    if row.limit is Limit.MAX_POSITION:
        return world.held_to(**{name: {GLD: limit, USO: limit}})
    return world.held_to(**{name: limit})


@pytest.mark.parametrize("flip", [1, -1], ids=["as-written", "mirrored"])
@pytest.mark.parametrize("name", TABLE_15)
def test_2_each_row_of_the_table_under_a_limit_of_50(name: str, flip: int) -> None:
    row = TABLE_15[name]
    verdict = _row(row, flip, d(50)).check()
    assert verdict.adding is row.adds
    assert verdict.approved is not row.refused
    if row.refused:
        assert (verdict.limit, verdict.value, verdict.bound) == (row.limit, row.figure, 50)
    else:
        # It goes out while the limit is past, and the verdict says so.
        assert [(p.limit, p.value, p.bound) for p in verdict.past] == [(row.limit, row.figure, 50)]


@pytest.mark.parametrize("flip", [1, -1], ids=["as-written", "mirrored"])
@pytest.mark.parametrize("name", TABLE_15)
def test_2_each_row_one_step_either_side_of_its_limit(name: str, flip: int) -> None:
    """At the figure nothing is past. One step below it, the row's answer is the table's."""
    row = TABLE_15[name]
    at = _row(row, flip, d(row.figure)).check()
    assert at.approved
    below = _row(row, flip, row.figure - CENT).check()
    assert below.approved is not row.refused
    assert row.limit in [p.limit for p in below.past]
    above = _row(row, flip, row.figure + CENT).check()
    assert above.approved
    if name != "a-larger-breach-hides-it":
        assert (at.past, above.past) == ((), ())
    else:
        # The working buy filled would still come to 100, which this order leaves alone.
        assert [(p.limit, p.value) for p in at.past] == [(Limit.MAX_NET, 100)]


@pytest.mark.parametrize("flip", [1, -1], ids=["as-written", "mirrored"])
def test_2_an_order_that_leaves_net_the_same_size_is_not_larger(flip: int) -> None:
    """Long 100 against 50 short, and the 100 sold: net goes from 50 to −50, both past 40."""
    holding = book({CHAN: {GLD: flip * 100, USO: flip * -50}})
    world = World(book=holding, intent=order(flip * -100)).held_to(max_net=d(40))
    assert world.check().approved
    wider = world.at(book=book({CHAN: {GLD: flip * 100, USO: flip * (-50 - QTY)}}))
    assert refusal(wider) == (Limit.MAX_NET, 50 + QTY, d(40))


def test_2_a_limit_put_past_by_a_lowered_cap_a_price_or_a_fall_in_v() -> None:
    held = World(book=book({CHAN: {GLD: 40}}, cash=1000), intent=order(-10)).losing(0)
    more = held.at(intent=order(10))
    assert held.check().past == ()
    # A lowered cap, for each of the three. A share of V of 0.1 over 0.2 is half of V.
    half = {"share": d("0.005")}
    for change, limit in (
        ({"max_position": {GLD: d(30), USO: d(30)}}, Limit.MAX_POSITION),
        ({"max_gross": d(30)}, Limit.MAX_GROSS),
        (half, Limit.MAX_NET),
    ):
        out = held.capped(**change).check()
        assert out.approved and [p.limit for p in out.past] == [limit]
        assert more.capped(**change).check().limit is limit
    # A price that doubled, for gross and net.
    for change, limit in (
        ({"max_gross": d(60)}, Limit.MAX_GROSS),
        ({"max_net": d(60)}, Limit.MAX_NET),
    ):
        calm = held.held_to(**change)
        assert calm.check().past == ()
        dear = calm.at(snapshot=snapshot({GLD: 2, USO: 1}))
        out = dear.check()
        assert out.approved and [(p.limit, p.value) for p in out.past] == [(limit, 80)]
        assert dear.at(intent=order(10)).check().limit is limit
    # A V that fell, for net: half of V is the cap here.
    rich = held.capped(share=d("0.1")).given(valuation=valuation(100))
    assert rich.check().past == ()
    poor = rich.given(valuation=valuation(78))
    assert [(p.limit, p.bound) for p in poor.check().past] == [(Limit.MAX_NET, 39)]
    assert poor.check().approved
    assert poor.at(intent=order(1)).check().limit is Limit.MAX_NET


def test_2_past_names_each_limit_over_its_bound_in_the_tables_order() -> None:
    world = World(book=book({CHAN: {GLD: 40}}), intent=order(-10)).held_to(
        max_position={GLD: d(30), USO: d(30)}, max_gross=d(20), max_net=d(10)
    )
    verdict = world.check()
    assert verdict.approved and not verdict.adding
    assert [(p.limit, p.value, p.bound) for p in verdict.past] == [
        (Limit.MAX_POSITION, 40, 30),
        (Limit.MAX_GROSS, 40, 20),
        (Limit.MAX_NET, 40, 10),
    ]
    # At each bound it is empty, and it is on a refusal and on a stop's refusal too.
    level = world.held_to(
        max_position={GLD: d(40), USO: d(40)}, max_gross=d(40), max_net=d(40)
    ).check()
    assert level.approved and level.past == ()
    assert world.given(stop=StopState.FIRED).check().past == verdict.past
    adding = world.at(intent=order(QTY))
    for limit in (Limit.MAX_POSITION, Limit.MAX_GROSS, Limit.MAX_NET):
        refused = adding.check()
        assert (refused.limit, refused.value) == (limit, 40 + QTY)
        assert refused.past[0].limit is limit
        assert [p.value for p in refused.past] == [40 + QTY] * len(refused.past)
        loose = BOTH if limit is Limit.MAX_POSITION else LOOSE
        adding = adding.held_to(**{str(limit): loose})
    assert adding.check().approved and adding.check().past == ()


def test_2_past_counts_the_order_being_checked() -> None:
    """An order that adds is past with itself counted, though the book alone is not."""
    world = World(book=book({CHAN: {GLD: 40}}), intent=order(20)).held_to(max_gross=d(50))
    assert [(p.limit, p.value) for p in world.check().past] == [(Limit.MAX_GROSS, 60)]


def test_2_a_position_limit_of_zero_lets_an_exit_to_flat_out_and_no_further() -> None:
    world = World(book=book({CHAN: {GLD: 100}}), intent=order(-100)).held_to(
        max_position={GLD: d(0), USO: d(0)}
    )
    assert world.check().approved
    over = world.at(intent=order(-100 - QTY)).check()
    assert (over.limit, over.adding) == (Limit.MAX_POSITION, True)


@pytest.mark.parametrize("limit", ["max_order_qty", "max_open_orders", "loss_stop", "moc_cutoff"])
def test_2_the_other_four_limits_hold_an_order_that_only_reduces(limit: str) -> None:
    out = World(book=book({CHAN: {GLD: 100}}), intent=moc(-30)).losing(50)
    out = out.at(now=CLOSE - timedelta(minutes=5))
    assert out.check().approved and not out.check().adding
    tight: dict[str, object] = {
        "max_order_qty": {GLD: 30 - QTY, USO: d(1)},
        "max_open_orders": 0,
        "loss_stop": 50 - CENT,
        "moc_cutoff": 6,
    }
    world = out.held_to(**{limit: tight[limit]})
    if limit == "max_order_qty":
        world = world.held_to(max_position={GLD: d(0), USO: d(0)})
    assert world.check().limit is Limit(limit)
    assert not world.check().adding


@pytest.mark.parametrize("state", [StopState.FIRED, StopState.UNREADABLE])
@pytest.mark.parametrize("qty", [-5, 5], ids=["reduces", "adds"])
def test_2_any_stop_but_an_absent_one_refuses_every_order(state: StopState, qty: int) -> None:
    world = World(book=book({CHAN: {GLD: 5}}), intent=order(qty))
    assert world.check().approved
    assert refusal(world.given(stop=state)) == (Limit.STOP, None, None)
    by_hand = world.given(stop=state).at(intent=order(qty, strategy=""))
    assert by_hand.check().limit is Limit.STOP


def test_2_an_order_that_only_reduces_halts_with_no_valid_price() -> None:
    pair = book({CHAN: {GLD: 100, USO: -60}})
    world = World(book=pair, intent=order(-100))
    assert world.check().approved
    for instrument in (GLD, USO):
        # The order's own leg, and the pair's other leg, whose price net reads.
        unpriced = snapshot(
            {GLD: 1, USO: 1},
            status={i: BarStatus.STALE if i == instrument else BarStatus.FRESH for i in (GLD, USO)},
        )
        with pytest.raises(Halt, match=f"{instrument} stale for the session"):
            world.at(snapshot=unpriced).check()


# 3. The worst loss, and what counts as reducing.


def test_3_the_worst_loss_against_hand_figures() -> None:
    marks = snapshot({GLD: 100, USO: 10})
    one = World(book=book({CHAN: {GLD: 10}}), intent=order(5), snapshot=marks)
    # 15 shares at 100 dollars, and a move of a fifth.
    assert one.check().worst_loss == 300
    two = one.at(book=book({CHAN: {GLD: 10, USO: -20}}))
    assert two.check().worst_loss == 340
    # A working sale of 5 USO adds to the other leg, so both legs count it.
    both = one.at(book=book({CHAN: {GLD: 10, USO: -20}}, opens=[order(-5, USO, name="old-1")]))
    assert both.check().worst_loss == 350
    # A sale that only reduces counts as unfilled, and another strategy's leg not at all.
    out = two.at(intent=order(-5))
    assert out.check().worst_loss == 240
    other = two.at(book=book({CHAN: {GLD: 10, USO: -20}, OTHER: {GLD: 1000}}))
    assert other.check().worst_loss == 340
    assert two.capped(stress_move=d("0.4")).check().worst_loss == 680


def test_3_a_worst_loss_is_never_recorded_smaller_than_it_is() -> None:
    world = World(intent=order(QTY))
    # A billionth of a share at a dollar, times a fifth, still reads above zero.
    assert world.check().worst_loss == CENT
    assert World(intent=order("1.00001")).check().worst_loss == d("0.2001")


def test_3_a_lower_stress_move_raises_the_net_cap() -> None:
    world = World(intent=order(60)).capped(share=d("0.01")).given(valuation=valuation(1000))
    world = world.at(book=book(cash=1000))
    assert refusal(world) == (Limit.MAX_NET, d(60), d(50))
    assert world.capped(stress_move=d("0.1")).check().approved


# 5. Every halt of the gate's own inputs. The caps', the limits' and the mark's are in
# tests/test_limits.py and tests/test_valuing.py.


def _dated(**changes: object) -> Callable[[World], World]:
    return lambda w: w.given(valuation=replace(w.inputs.valuation, **changes))


def _leg(snap: Snapshot, *bars: Bar) -> Snapshot:
    return replace(snap, bars={**snap.bars, USO: {"1d": bars}})


def _newest(snap: Snapshot, **changes: object) -> Snapshot:
    """The snapshot with fields of USO's newest bar replaced."""
    old, new = snap.bars[USO]["1d"]
    return _leg(snap, old, replace(new, **changes))


def _late_leg(snap: Snapshot) -> Snapshot:
    """USO's newest bar is V's session's own, and GLD's is today's."""
    return _leg(snap, snap.bars[USO]["1d"][0])


def _early_leg(snap: Snapshot) -> Snapshot:
    return _newest(snap, session=date(2026, 11, 4))


def _quoted(snap: Snapshot) -> Snapshot:
    return replace(snap, quotes={USO: quote(USO, 1, 1, DECISION)})


def _unfrozen(made: Book, corrections: object) -> Book:
    """The book with `corrections` as they are, where building one makes a list a tuple."""
    object.__setattr__(made, "corrections", corrections)
    return made


def corrected(
    seq: object, at: datetime = DECISION - timedelta(days=30), repositions: object = False
) -> Correction:
    return Correction(seq, at, repositions)  # type: ignore[arg-type]


HALTS: dict[str, tuple[Callable[[World], World], str]] = {
    "now-before-the-decision": (
        lambda w: w.at(now=DECISION - TICK),
        "now is 2026-11-03T20:39:59.999999.*, not from the decision",
    ),
    "now-past-the-close": (lambda w: w.at(now=CLOSE + TICK), "now is 2026-11-03T21:00:00.000001"),
    "now-three-days-on": (lambda w: w.at(now=DECISION + timedelta(days=3)), "now is 2026-11-06"),
    "now-in-another-zone": (
        lambda w: w.at(now=DECISION.astimezone(timezone(timedelta(hours=-5)))),
        "now is .*, not a UTC time",
    ),
    "now-with-no-zone": (lambda w: w.at(now=DECISION.replace(tzinfo=None)), "now is .*, not a UTC"),
    "a-decision-with-no-zone": (
        lambda w: w.at(snapshot=replace(w.snapshot, decision_at=DECISION.replace(tzinfo=None))),
        "the decision is .*, not a UTC time",
    ),
    "a-decision-after-its-close": (
        lambda w: w.at(snapshot=replace(w.snapshot, decision_at=CLOSE + TICK), now=CLOSE + TICK),
        "now is .*, not from the decision",
    ),
    "a-zero-quantity": (lambda w: w.at(intent=order(0)), "new-1 is for zero shares"),
    "a-quantity-not-finite": (
        lambda w: w.at(intent=order("NaN")),
        "the order new-1 is Decimal\\('NaN'\\), not a finite decimal",
    ),
    "an-infinite-quantity": (lambda w: w.at(intent=order("Infinity")), "order new-1 is .*Infinity"),
    "a-float-quantity": (
        lambda w: w.at(intent=replace(order(1), qty=1.0)),
        "the order new-1 is 1.0, not a finite decimal",
    ),
    "a-market-order-at-the-close": (
        lambda w: w.at(intent=order(1, tif=TimeInForce.CLS)),
        "a pair the gate does not know",
    ),
    "a-market-on-close-order-for-the-day": (
        lambda w: w.at(intent=order(1, kind=OrderType.MARKET_ON_CLOSE)),
        "a pair the gate does not know",
    ),
    "a-misspelled-type": (
        lambda w: w.at(intent=replace(moc(1), type="market_on_cloze")),
        "a 'market_on_cloze' order for",
    ),
    "an-instrument-the-two-do-not-both-limit": (
        lambda w: w.at(intent=order(1, SPY), snapshot=snapshot({GLD: 1, USO: 1, SPY: 1})),
        "the constraints and the hard caps do not both limit SPY",
    ),
    "an-instrument-the-snapshot-lacks": (
        lambda w: w.at(snapshot=snapshot({USO: 1})),
        "GLD missing for the session",
    ),
    "a-held-instrument-the-snapshot-lacks": (
        lambda w: w.at(book=book({OTHER: {SPY: 1}})),
        "SPY missing for the session",
    ),
    "an-open-order-in-an-instrument-the-snapshot-lacks": (
        lambda w: w.at(book=book(opens=[order(1, SPY, name="old-1")])),
        "SPY missing for the session",
    ),
    "a-held-instrument-flat-across-strategies": (
        lambda w: w.at(book=book({CHAN: {SPY: 1}, OTHER: {SPY: -1}})),
        "SPY missing for the session",
    ),
    "another-strategy": (
        lambda w: w.at(intent=order(1, strategy=OTHER)),
        "new-1 is 'other''s, and the constraints are 'chan''s",
    ),
    "an-id-already-open": (
        lambda w: w.at(book=book(opens=[order(1, strategy=OTHER)])),
        "new-1 is already open, and this resends it",
    ),
    # A strategy's own retry, which is the usual resend.
    "its-own-id-already-open": (
        lambda w: w.at(book=book(opens=[order(1)])),
        "new-1 is already open, and this resends it",
    ),
    "an-open-order-filed-under-another-strategy": (
        lambda w: w.at(
            book=replace(book(), open_orders_by_strategy={OTHER: (order(1, name="o"),)})
        ),
        "the book files o, an order of 'chan', under 'other'",
    ),
    "no-valuation": (lambda w: w.given(valuation=None), "the valuation is missing"),
    "an-older-valuation": (_dated(session=date(2026, 10, 30)), "the valuation is of 2026-10-30"),
    "a-valuation-of-today": (_dated(session=TODAY), "the valuation is of 2026-11-03"),
    "a-valuation-replayed-past-the-book": (
        _dated(replayed_through=8),
        "replayed through entry 8, past the book's 7",
    ),
    "a-mark-no-later-than-v": (
        lambda w: w.at(snapshot=snapshot({GLD: 1, USO: 1}, day=PRIOR)),
        "GLD has no mark: its newest bar is for 2026-11-02",
    ),
    "a-held-legs-mark-no-later-than-v": (
        lambda w: w.at(book=book({CHAN: {USO: 10}}), snapshot=_late_leg(w.snapshot)),
        "USO has no mark: its newest bar is for 2026-11-02",
    ),
    "a-working-legs-mark-no-later-than-v": (
        lambda w: w.at(
            book=book(opens=[order(1, USO, OTHER, name="old-1")]), snapshot=_late_leg(w.snapshot)
        ),
        "USO has no mark: its newest bar is for 2026-11-02",
    ),
    "a-held-leg-with-a-quote": (
        lambda w: w.at(book=book({CHAN: {USO: 10}}), snapshot=_quoted(w.snapshot)),
        "USO has a quote and a bar for 2026-11-03",
    ),
    "a-bar-after-the-decision": (
        lambda w: w.at(book=book({CHAN: {USO: 10}}), snapshot=_early_leg(w.snapshot)),
        "USO has a bar for 2026-11-04, after the decision's session 2026-11-03",
    ),
    "a-held-leg-of-no-stated-kind": (
        lambda w: w.at(book=book({CHAN: {USO: 10}}), snapshot=_newest(w.snapshot, venues="")),
        "USO's bar for 2026-11-03 has unstated venues",
    ),
    "a-stress-move-past-its-age": (
        lambda w: w.capped(stress_max_age_days=3),
        "stress_through is 2026-10-30, 4 days before the decision",
    ),
    "a-correction-after-v": (
        lambda w: w.at(book=book(corrections=(corrected(8),), replayed_through=9)),
        "entry 8 corrected the book after the valuation",
    ),
    "an-older-correction-before-v-and-a-newer-after-it": (
        lambda w: w.at(book=book(corrections=(corrected(3), corrected(8)), replayed_through=9)),
        "entry 8 corrected the book after the valuation",
    ),
    "corrections-that-are-no-list": (
        lambda w: w.at(book=replace(book(), corrections=None)),
        "the book's corrections are None, not a list of corrections",
    ),
    "corrections-in-a-list-that-can-change": (
        lambda w: w.at(book=_unfrozen(book(), [corrected(1)])),
        "the book's corrections are .*, not a list of corrections",
    ),
    "a-correction-that-is-none": (
        lambda w: w.at(book=book(corrections=(None,))),
        "the book's corrections are .*, not a list of corrections",
    ),
    "a-correction-past-the-book": (
        lambda w: w.at(book=book(corrections=(corrected(9),), replayed_through=8)),
        "a correction's seq is 9, past the book's 8",
    ),
    "a-correction-that-repositions-0": (
        lambda w: w.at(book=book(corrections=(corrected(1, repositions=0),))),
        "the correction at entry 1 repositions 0, no bool",
    ),
    "a-correction-that-says-no-reposition": (
        lambda w: w.at(book=book(corrections=(corrected(1, repositions=None),))),
        "the correction at entry 1 repositions None, no bool",
    ),
    # The newest entry moved cash alone, and the one behind it moved a position.
    "two-corrections-where-only-the-older-repositions": (
        lambda w: w.at(
            book=book(
                corrections=(
                    corrected(6, TABLE.session(PRIOR).close + TICK, True),
                    corrected(7, TABLE.session(PRIOR).close + 2 * TICK),
                )
            )
        ),
        "a position was corrected at 2026-11-02T21:00:00.000001",
    ),
    "an-older-reposition-before-the-close-and-a-newer-after-it": (
        lambda w: w.at(
            book=book(
                corrections=(
                    corrected(3, TABLE.session(PRIOR).close, True),
                    corrected(6, TABLE.session(PRIOR).close + TICK, True),
                )
            )
        ),
        "a position was corrected at 2026-11-02T21:00:00.000001",
    ),
    "a-reposition-after-vs-session": (
        lambda w: w.at(
            book=book(corrections=(corrected(7, TABLE.session(PRIOR).close + TICK, True),))
        ),
        "a position was corrected at 2026-11-02T21:00:00.000001",
    ),
    # A split's restatement, or a broker's trade, journaled before V was built.
    "a-reposition-v-was-replayed-past": (
        lambda w: w.at(
            book=book(corrections=(corrected(6, TABLE.session(PRIOR).close + TICK, True),))
        ),
        "a position was corrected at 2026-11-02T21:00:00.000001",
    ),
    "a-reposition-with-no-zone": (
        lambda w: w.at(book=book(corrections=(corrected(1, datetime(2026, 1, 1), True),))),
        "when entry 1 corrected the book is .*, not a UTC time",
    ),
    "a-correction-of-cash-with-no-zone": (
        lambda w: w.at(book=book(corrections=(corrected(1, datetime(2026, 1, 1)),))),
        "when entry 1 corrected the book is .*, not a UTC time",
    ),
    "a-correction-of-cash-in-another-zone": (
        lambda w: w.at(
            book=book(
                corrections=(
                    corrected(1, datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))),
                )
            )
        ),
        "when entry 1 corrected the book is .*, not a UTC time",
    ),
    "cash-not-finite": (lambda w: w.at(book=book(cash="NaN")), "the book's cash is"),
    "a-position-not-finite": (
        lambda w: w.at(book=book({OTHER: {USO: "Infinity"}})),
        "other's position in USO is",
    ),
    "an-open-order-not-finite": (
        lambda w: w.at(book=book(opens=[order("NaN", USO, name="old-1")])),
        "open order old-1 is",
    ),
    "a-position-past-the-arithmetics-digits": (
        lambda w: w.at(book=book({CHAN: {GLD: "1E+200"}, OTHER: {GLD: 1}})),
        "the position in GLD has no exact sum: Inexact",
    ),
    **{
        f"{name}-of-{number!r}": (change, f"{shown} is .*, not an entry's number")
        for number in (d("NaN"), True, False, "", "7", 7.0, -1)
        for name, shown, change in (
            (
                "corrected-through",
                "a correction's seq",
                lambda w, n=number: w.at(book=book(corrections=(corrected(n),))),
            ),
            (
                "a-book-replayed-through",
                "the book's replayed_through",
                lambda w, n=number: w.at(book=book(replayed_through=n)),
            ),
            (
                "a-valuation-replayed-through",
                "the valuation's replayed_through",
                lambda w, n=number: w.given(valuation=valuation(replayed_through=n)),
            ),
        )
    },
    "corrected-through-no-entry": (
        lambda w: w.at(book=book(corrections=(corrected(0),))),
        "a correction's seq is 0, not an entry's number",
    ),
    "a-book-replayed-through-nothing": (
        lambda w: w.at(book=book(replayed_through=None)),
        "the book's replayed_through is None, not an entry's number",
    ),
    **{
        f"in-doubt-of-{doubt!r}": (
            lambda w, n=doubt: w.at(book=replace(book(), in_doubt=n)),
            "the book's in_doubt is .*, not a list of instruments",
        )
        for doubt in ("GLDM", None, (None,), ("",), (1,), frozenset({GLD}))
    },
    "no-positions": (
        lambda w: w.at(book=replace(book(), positions_by_strategy=None)),
        "the book's positions are None, not a mapping",
    ),
    "no-open-orders": (
        lambda w: w.at(book=replace(book(), open_orders_by_strategy=None)),
        "the book's open orders are None, not a mapping",
    ),
    "positions-that-are-no-mapping": (
        lambda w: w.at(book=replace(book(), positions_by_strategy={CHAN: None})),
        "chan's positions are None, not a mapping",
    ),
    **{
        f"open-orders-of-{orders!r}": (
            lambda w, n=orders: w.at(book=replace(book(), open_orders_by_strategy={CHAN: n})),
            "chan's open orders are .*, not a list of orders",
        )
        for orders in (None, 5, ("new-1",))
    },
    **{
        f"an-open-order-with-an-id-of-{name!r}": (
            lambda w, n=name: w.at(book=book(opens=[order(1, USO, name=n)])),
            "chan has an open order whose id is",
        )
        for name in ("", None, 5)
    },
    "two-open-orders-of-one-id": (
        lambda w: w.at(book=book(opens=[order(1, USO, name="a"), order(1, USO, OTHER, name="a")])),
        "other has an open order whose id is 'a', which names none or another",
    ),
    "an-open-order-of-another-strategy-with-no-id": (
        lambda w: w.at(book=book(opens=[order(1, USO, name="a"), order(1, USO, OTHER, name="")])),
        "other has an open order whose id is ''",
    ),
    "another-strategys-orders-that-are-no-list": (
        lambda w: w.at(book=replace(book(), open_orders_by_strategy={CHAN: (), OTHER: None})),
        "other's open orders are None, not a list of orders",
    ),
    "two-open-orders-of-one-id-in-one-strategy": (
        lambda w: w.at(book=book(opens=[order(1, USO, name="a"), order(2, USO, name="a")])),
        "chan has an open order whose id is 'a', which names none or another",
    ),
    **{
        f"an-open-order-of-{name}-with-nothing-left": (
            lambda w, n=name: w.at(book=book(opens=[order(0, USO, n, name="old-1")])),
            f"{name}'s open order old-1 has nothing left",
        )
        for name in (CHAN, OTHER)
    },
    **{
        f"an-open-order-in-{name!r}": (
            lambda w, n=name: w.at(book=book(opens=[order(1, n, OTHER, name="old-1")])),
            f"other has an open order in {name!r}, no instrument",
        )
        for name in ("", None, 5)
    },
    **{
        f"a-position-in-{name!r}": (
            lambda w, n=name: w.at(book=book({OTHER: {n: 1, GLD: 1}})),
            f"other holds {name!r}, which names no instrument",
        )
        for name in ("", None, 5)
    },
    "in-doubt-and-held-with-no-open-order": (
        lambda w: w.at(book=book({CHAN: {GLD: 5}}, in_doubt=(GLD,))),
        "GLD is in doubt and the book holds no open order in it",
    ),
    "in-doubt-with-no-open-order": (
        lambda w: w.at(book=book(opens=[order(1, USO, name="old-1")], in_doubt=(USO, GLD))),
        "GLD is in doubt and the book holds no open order in it",
    ),
    "no-status": (
        lambda w: w.at(snapshot=replace(w.snapshot, status=None)),
        "the snapshot's status are None, not a mapping",
    ),
    "a-valuation-of-no-session": (
        lambda w: w.given(valuation=valuation(session=None)),
        "the valuation's session is None, not a date",
    ),
    "a-valuation-of-a-time": (
        lambda w: w.given(valuation=valuation(session=DECISION)),
        "the valuation's session is .*, not a date",
    ),
    "no-order": (lambda w: w.at(intent=None), "the order is missing"),
    **{
        f"an-order-with-an-id-of-{name!r}": (
            lambda w, n=name: w.at(intent=replace(w.intent, client_order_id=n)),
            "an order's client order id is .*, which names none",
        )
        for name in ("", None, 5)
    },
    "a-stop-that-names-no-state": (lambda w: w.given(stop="absent"), "the loss stop's state is"),
    "no-caps": (lambda w: w.given(caps=None), "the hard caps are missing"),
    "no-constraints": (lambda w: w.at(constraints=None), "the constraints are missing"),
    "no-book": (lambda w: w.at(book=None), "the book is missing"),
    "no-snapshot": (lambda w: w.at(snapshot=None), "the snapshot is missing"),
    "no-inputs": (lambda w: w.at(inputs=None), "the gate's inputs are missing"),
}


@pytest.mark.parametrize("name", HALTS)
def test_5_each_halt_names_what_it_read(name: str) -> None:
    broken, reason = HALTS[name]
    world = World()
    assert world.check().approved
    with pytest.raises(Halt, match=reason):
        broken(world).check()


ORDERS_ALONE = {
    "a-zero-quantity",
    "a-quantity-not-finite",
    "an-infinite-quantity",
    "a-float-quantity",
    "a-market-order-at-the-close",
    "a-market-on-close-order-for-the-day",
    "a-misspelled-type",
    "an-instrument-the-two-do-not-both-limit",
    "an-instrument-the-snapshot-lacks",
    "another-strategy",
    "an-id-already-open",
    "its-own-id-already-open",
    "a-mark-no-later-than-v",
    "no-order",
    *(name for name in HALTS if name.startswith("an-order-with-an-id-of-")),
}


@pytest.mark.parametrize("name", sorted(set(HALTS) - ORDERS_ALONE))
def test_5_the_loss_reading_halts_on_the_same_inputs(name: str) -> None:
    broken, reason = HALTS[name]
    with pytest.raises(Halt, match=reason):
        broken(World()).loss()


LEG_HALTS: dict[str, tuple[Callable[[Snapshot], Snapshot], str]] = {
    "older": (lambda s: _late_leg(s), "USO has no mark"),
    "later": (lambda s: _early_leg(s), "USO has a bar for 2026-11-04"),
    "unended": (
        lambda s: _newest(s, end=DECISION + TICK),
        "USO's bar for 2026-11-03 ends at 2026-11-03T20:40:00.000001",
    ),
    "unknown": (
        lambda s: _newest(s, knowable_at=DECISION + TICK),
        "USO's bar for 2026-11-03 was knowable at 2026-11-03T20:40:00.000001",
    ),
    "quoted": (lambda s: _quoted(s), "USO has a quote and a bar for 2026-11-03"),
    "stale": (
        lambda s: replace(s, status={**s.status, USO: BarStatus.STALE}),
        "USO stale for the session",
    ),
    "unstated": (lambda s: _newest(s, delay="unstated"), "has unstated delay"),
    "free": (lambda s: _newest(s, close=d(0)), "USO's close for 2026-11-03 is 0"),
    "two": (lambda s: _leg(s, *s.bars[USO]["1d"], s.bars[USO]["1d"][1]), "USO has 2 daily bars"),
}


@pytest.mark.parametrize("read", ["check", "loss"])
@pytest.mark.parametrize("resting", [False, True], ids=["held", "working"])
@pytest.mark.parametrize("name", LEG_HALTS)
def test_5_a_leg_beside_the_order_halts_on_its_price_as_the_orders_own_does(
    name: str, resting: bool, read: str
) -> None:
    """A book holds it or has an order in it, and neither read can value it."""
    broken, reason = LEG_HALTS[name]
    beside = [order(1, USO, OTHER, name="old-1")] if resting else []
    world = World(book=book({} if resting else {CHAN: {USO: 10}}, opens=beside))
    assert getattr(world, read)() is not None
    with pytest.raises(Halt, match=reason):
        getattr(world.at(snapshot=broken(world.snapshot)), read)()


@pytest.mark.parametrize("name", sorted(ORDERS_ALONE - {"another-strategy", "no-order"}))
def test_5_an_order_that_names_no_strategy_meets_the_same_halts(name: str) -> None:
    """It would be refused for its position, and every halt comes before that."""
    broken, reason = HALTS[name]
    world = broken(World())
    with pytest.raises(Halt, match=reason):
        world.at(intent=replace(world.intent, strategy="")).check()


def test_5_a_halt_comes_before_a_refusal_it_collides_with() -> None:
    fired = World().given(stop=StopState.FIRED)
    assert fired.check().limit is Limit.STOP
    with pytest.raises(Halt, match="GLD missing for the session"):
        fired.at(snapshot=snapshot({USO: 1})).check()
    large = World(intent=order(LOOSE + 1))
    assert large.check().limit is Limit.MAX_ORDER_QTY
    with pytest.raises(Halt, match="entry 8 corrected the book"):
        large.at(book=book(corrections=(corrected(8),), replayed_through=9)).check()


def test_5_a_journal_with_no_entry_is_replayed_through_0() -> None:
    world = World(book=book(replayed_through=0))
    assert world.given(valuation=valuation(replayed_through=0)).check().approved


def test_5_a_correction_at_vs_own_entry_and_a_reposition_at_its_close_pass() -> None:
    world = World(book=book(corrections=(corrected(7, TABLE.session(PRIOR).close, True),)))
    assert world.check().approved
    late = TABLE.session(PRIOR).close + TICK
    cash_alone = book(corrections=(corrected(6, utc(2026, 10, 1, 12), True), corrected(7, late)))
    assert world.at(book=cash_alone).check().approved
    earlier = book(corrections=(corrected(1, utc(2026, 10, 1, 12), True),))
    assert world.at(book=earlier).check().approved


def test_5_the_stress_moves_age_inside_at_and_past_its_bound() -> None:
    through = date(2026, 10, 30)
    days = (TODAY - through).days
    world = World()
    assert world.capped(stress_max_age_days=days + 1).check().approved
    assert world.capped(stress_max_age_days=days).check().approved
    with pytest.raises(Halt, match="stress_through is 2026-10-30, 4 days before the decision"):
        world.capped(stress_max_age_days=days - 1).check()
    # A vintage measured after an old decision is no older for it.
    assert world.capped(stress_through=date(2030, 1, 1)).check().approved


# 6. The loss reading.


def test_6_the_loss_at_one_step_below_and_past_its_stop() -> None:
    world = World(book=book({CHAN: {GLD: 10}}, cash=1000)).held_to(loss_stop=d(50))
    for off, past in ((-1, False), (0, False), (1, True)):
        reading = world.losing(50 + off * CENT).loss()
        assert reading == core.LossReading(50 + off * CENT, d(50), past, PRIOR)
    # A gain reads as a negative loss.
    assert world.losing(-3).loss().loss == -3


def test_6_the_loss_needs_no_order_and_no_position() -> None:
    flat = World(book=book(cash=940), snapshot=snapshot({})).given(valuation=valuation(1000))
    reading = flat.held_to(loss_stop=d(50)).loss()
    assert (reading.loss, reading.past) == (60, True)


def test_6_a_fill_between_two_checks_moves_the_loss_by_its_price() -> None:
    """Ten shares bought at 12 and marked at 10 have lost 20, and a fee is lost whole."""
    marks = snapshot({GLD: 10, USO: 1})
    before = World(book=book(cash=1000), snapshot=marks).given(valuation=valuation(1000))
    assert before.loss().loss == 0
    bought = book({CHAN: {GLD: 10}}, cash=d(1000) - 120 - d("0.5"))
    after = before.at(book=bought)
    assert after.loss().loss == d("20.5")
    assert after.at(intent=order(1, name="new-2")).check().loss.loss == d("20.5")


SOLD = {
    # What V held of GLD at 10, what is left, and the price now. The loss is hand made.
    "part-of-a-long-leg": (100, 40, 5, 500),
    "all-of-a-long-leg": (100, 0, 5, 500),
    "part-of-a-short-leg": (-100, -40, 15, 500),
    "all-of-a-short-leg": (-100, 0, 15, 500),
    "more-of-a-long-leg": (40, 100, 5, 500),
}


@pytest.mark.parametrize("name", SOLD)
def test_6_the_loss_counts_what_is_held_now_and_what_a_trade_left(name: str) -> None:
    """V held the leg at 10. What was traded went at the price now, so it lost as much as the rest.

    The one that bought 60 more at 10 lost on all 100.
    """
    held, left, price, lost = SOLD[name]
    marks = snapshot({GLD: price, USO: 1}, prior={GLD: 10, USO: 1})
    v = value_book(book({CHAN: {GLD: held}}, cash=5000), marks, PRIOR)
    assert v.positions == {GLD: held}
    traded = held - left
    cash = 5000 + traded * (10 if traded < 0 < held else price)
    world = World(
        book=book({CHAN: {GLD: left, USO: 3}}, cash=cash - 3), snapshot=marks, intent=order(1, USO)
    ).given(valuation=v)
    assert world.loss().loss == lost
    assert world.held_to(loss_stop=d(lost)).check().approved
    tight = world.held_to(loss_stop=lost - CENT)
    assert tight.loss().past
    assert refusal(tight) == (Limit.LOSS_STOP, lost, lost - CENT)


def test_6_a_decision_at_the_close_reads_todays_bar_and_never_the_priors() -> None:
    """V is at the prior session's close, and both sessions' bars are in the snapshot."""
    marks = snapshot({GLD: 90, USO: 1}, at=CLOSE, prior={GLD: 100})
    holding = book({CHAN: {GLD: 10}}, cash=0)
    v = value_book(holding, marks, PRIOR)
    assert v.value == 1000
    world = World(book=holding, snapshot=marks, now=CLOSE).given(valuation=v)
    world = world.held_to(loss_stop=d(50))
    assert (world.loss().loss, world.loss().past) == (100, True)
    assert world.check().limit is Limit.LOSS_STOP


def test_6_a_product_past_twenty_eight_digits_is_exact() -> None:
    """The loss is refused a step past its stop, which a product rounded at 28 digits would hide."""
    qty, price = d("123456789123456789.123456789"), d("65.12345678901234")
    worth = _exact(qty, price)
    assert Decimal(qty * price) != worth
    v = core.to_decimal(worth.quantize(Decimal(1)), 4) + 100
    world = World(
        book=book({CHAN: {GLD: qty}}, cash=0), snapshot=snapshot({GLD: price, USO: 1})
    ).given(valuation=valuation(v))
    with localcontext() as context:
        context.prec = 60
        exact = v - worth
        one = world.held_to(loss_stop=exact)
        assert not one.loss().past
        assert world.held_to(loss_stop=exact - Decimal("1E-23")).loss().past


def _exact(qty: Decimal, price: Decimal) -> Decimal:
    """The product in integers, which no decimal context rounds."""
    a, b = int(qty.scaleb(9)), int(price.scaleb(14))
    digits = str(a * b)
    return Decimal(f"{digits[:-23]}.{digits[-23:]}")


def test_6_the_answer_does_not_depend_on_the_order_of_a_mapping() -> None:
    forward = World(
        book=book({CHAN: {GLD: "1.1", USO: "-2.3"}, OTHER: {USO: "0.7"}}),
        snapshot=snapshot({GLD: "0.1", USO: "0.3"}),
    )
    v = core.Valuation(
        PRIOR, d("2.3"), d("1"), {GLD: d(1), USO: d(1)}, {GLD: d(1), USO: d("0.3")}, 7
    )
    back = core.Valuation(
        PRIOR, d("2.3"), d("1"), {USO: d(1), GLD: d(1)}, {USO: d("0.3"), GLD: d(1)}, 7
    )
    assert core.encode(v) == core.encode(back)
    turned = World(
        book=book({OTHER: {USO: "0.7"}, CHAN: {USO: "-2.3", GLD: "1.1"}}),
        snapshot=snapshot({USO: "0.3", GLD: "0.1"}),
    )
    one = forward.given(valuation=core.decode(core.encode(v), core.Valuation)).check()
    two = turned.given(valuation=core.decode(core.encode(back), core.Valuation)).check()
    assert core.encode(one) == core.encode(two)


# 7. A wrong price, 100 times either way on each leg, flat and held.


def _wrong(prices: dict[InstrumentId, object], held: bool) -> World:
    """GLD at 100 and USO at 50, or long 5 GLD and short 10 USO, with V at those prices."""
    positions = {CHAN: {GLD: 5, USO: -10}} if held else {}
    v = valuation(10_000)
    world = World(
        book=book(positions, cash=10_000),
        snapshot=snapshot({GLD: 100, USO: 50} | prices, prior={GLD: 100, USO: 50}),
    ).given(valuation=v)
    return world.held_to(
        max_order_qty={GLD: d(10), USO: d(20)},
        max_position={GLD: d(10), USO: d(20)},
        max_gross=d(3_000),
        max_net=d(1_500),
        loss_stop=d(100),
    )


# A buy of 5 GLD and a sale of 10 USO, each checked twice: as the limits stand, and with
# gross loosened, so that net decides. Then the day's loss read.
type Answered = tuple[Limit, object, object] | None
WRONG: dict[tuple[str, bool], tuple[dict, Answered, Answered, Answered, Answered, object]] = {
    ("right", False): ({}, None, None, None, None, 0),
    ("GLD-high", False): (
        {GLD: 10_000},
        (Limit.MAX_GROSS, 50_000, 3_000),
        (Limit.MAX_NET, 50_000, 1_500),
        None,
        None,
        0,
    ),
    ("GLD-low", False): ({GLD: 1}, None, None, None, None, 0),
    ("USO-high", False): (
        {USO: 5_000},
        None,
        None,
        (Limit.MAX_GROSS, 50_000, 3_000),
        (Limit.MAX_NET, 50_000, 1_500),
        0,
    ),
    ("USO-low", False): ({USO: d("0.5")}, None, None, None, None, 0),
    # Held, the book is worth 10,000 in cash, 500 of GLD and −500 of USO.
    ("right", True): ({}, None, None, None, None, 0),
    ("GLD-high", True): (
        {GLD: 10_000},
        (Limit.MAX_GROSS, 100_500, 3_000),
        (Limit.MAX_NET, 99_500, 1_500),
        (Limit.MAX_GROSS, 51_000, 3_000),
        (Limit.MAX_NET, 49_500, 1_500),
        -49_500,
    ),
    ("GLD-low", True): (
        {GLD: 1},
        (Limit.LOSS_STOP, 495, 100),
        (Limit.LOSS_STOP, 495, 100),
        (Limit.LOSS_STOP, 495, 100),
        (Limit.LOSS_STOP, 495, 100),
        495,
    ),
    ("USO-high", True): (
        {USO: 5_000},
        (Limit.MAX_GROSS, 51_000, 3_000),
        (Limit.MAX_NET, 49_500, 1_500),
        (Limit.MAX_GROSS, 100_500, 3_000),
        (Limit.MAX_NET, 99_500, 1_500),
        49_500,
    ),
    ("USO-low", True): ({USO: d("0.5")}, None, None, None, None, -495),
}
# What no cap stops, because the wrong price reads every dollar figure small and the
# share caps read no price: an order on a leg read 100 times low, one beside it, and,
# held, any order while a short leg reads low, which is a gain to the loss stop.
NO_CAP_STOPS = {
    ("GLD-low", False, GLD),
    ("GLD-low", False, USO),
    ("GLD-high", False, USO),
    ("USO-low", False, USO),
    ("USO-low", False, GLD),
    ("USO-high", False, GLD),
    ("USO-low", True, GLD),
    ("USO-low", True, USO),
}


@pytest.mark.parametrize(("leg", "held"), WRONG)
def test_7_a_wrong_price_against_the_dollar_limits(leg: str, held: bool) -> None:
    prices, buy, buy_on_net, sell, sell_on_net, loss = WRONG[(leg, held)]
    world = _wrong(prices, held)
    assert world.loss().loss == loss
    for intent, stands, on_net in (
        (order(5), buy, buy_on_net),
        (order(-10, USO), sell, sell_on_net),
    ):
        asked = world.at(intent=intent)
        for answered, expected in ((asked, stands), (asked.held_to(max_gross=LOOSE), on_net)):
            assert refusal(answered) == (expected or (None, None, None))


def test_7_the_cases_no_cap_stops_are_listed() -> None:
    passed = {
        (leg, held, intent.instrument)
        for (leg, held), (prices, *_) in WRONG.items()
        for intent in (order(5), order(-10, USO))
        if leg != "right" and _wrong(prices, held).at(intent=intent).check().approved
    }
    assert passed == NO_CAP_STOPS


def test_7_a_long_leg_read_high_lets_its_exit_out_to_flat_and_no_further() -> None:
    world = _wrong({GLD: 10_000}, held=True)
    out = world.at(intent=order(-5, GLD)).check()
    assert out.approved and not out.loss.past
    assert [p.limit for p in out.past] == [Limit.MAX_GROSS, Limit.MAX_NET]
    past_flat = world.at(intent=order(-5 - QTY, GLD)).check()
    assert past_flat.limit is Limit.MAX_GROSS


def test_7_a_short_leg_read_high_has_its_exit_refused_on_the_loss_stop() -> None:
    world = _wrong({USO: 5_000}, held=True)
    assert world.at(intent=order(10, USO)).check().limit is Limit.LOSS_STOP


# 8. Test 1's calendar.


def test_8_test_1s_first_and_last_decisions_pass_on_one_caps_blob() -> None:
    """Test 1 decides and sends at the close, on an account whose cutoff is the close."""
    one = caps(stress_through=date(2012, 4, 9), moc_cutoff=0)
    for day in (date(2006, 4, 26), date(2006, 6, 21), date(2012, 4, 9)):
        close = TABLE.session(day).close
        world = World(
            intent=moc(1),
            snapshot=snapshot({GLD: 1, USO: 1}, at=close, day=day),
            inputs=inputs(caps=one, valuation=valuation(session=TABLE.before(day).day)),
            now=close,
        )
        assert world.check().approved, day


def test_a_verdict_reads_back_from_its_bytes() -> None:
    for world in (
        World(),
        World(intent=order(1, strategy="")),
        World(intent=moc(1), now=CLOSE).capped(moc_cutoff=12),
        World(book=book({CHAN: {GLD: 40}}), intent=order(-10)).held_to(max_gross=d(20)),
    ):
        verdict = world.check()
        assert core.decode(core.encode(verdict), core.Verdict) == verdict


# The hold to the stress records.

M = d("0.2")
OWN = stress_record(stress_move=M)
RAISED = stress_record(vintage="c" * 64, stress_move=d("0.4"))
NOT_HELD: dict[str, tuple[object, str]] = {
    "the-readers-halt": (Halt("stress_moves.toml is missing"), "^stress_moves.toml is missing$"),
    "no-records": ((), "there is no stress record"),
    "another-vintage": (
        (stress_record(vintage="c" * 64),),
        "stress_vintage is a+, of no record of chan",
    ),
    "withdrawn": ((stress_record(withdrawn="mistyped"),), "withdrawn: mistyped"),
    "other-legs": ((stress_record(instruments=(GLD,)),), "do not both name USO"),
    "another-last-session": (
        (stress_record(through=date(2026, 10, 29)),),
        "stress_through is 2026-10-30",
    ),
    "a-move-below": ((stress_record(stress_move=d("0.20000001")),), "stress_move is 0.2, below"),
    "past-stress-high": ((OWN, replace(RAISED, stress_move=d("0.7"))), "stress_high is 0.6"),
}


@pytest.mark.parametrize("name", NOT_HELD)
def test_the_hold_halts_each_order_and_never_the_loss(name: str) -> None:
    records, why = NOT_HELD[name]
    world = World(book=book({CHAN: {GLD: 10}}, cash=1000)).held_to(loss_stop=d(50)).losing(51)
    assert world.at(records=(OWN,)).check().limit is Limit.LOSS_STOP
    broken = world.at(records=records)
    with pytest.raises(Halt, match=why):
        broken.check()
    # An order that names no strategy would be refused, and the halt comes first.
    with pytest.raises(Halt, match=why):
        broken.at(intent=order(1, strategy="")).check()
    assert broken.loss() == world.at(records=(OWN,)).loss()
    assert (broken.loss().loss, broken.loss().past) == (51, True)


def test_the_loss_says_past_under_caps_that_name_no_record() -> None:
    world = World(book=book(cash=940)).given(valuation=valuation(1000)).held_to(loss_stop=d(50))
    unnamed = world.at(records=(OWN,)).capped(stress_vintage="f" * 64)
    assert unnamed.loss() == world.loss() and unnamed.loss().past
    with pytest.raises(Halt, match="stress_vintage is f+, of no record"):
        unnamed.check()


def test_a_raised_record_binds_the_net_cap_and_the_worst_loss() -> None:
    """The share is 0.5 and V 1,000, so the net cap is 2,500 at 0.2 and 1,250 at 0.4."""
    world = World(intent=order(1000), book=book(cash=1000), snapshot=snapshot({GLD: 2, USO: 1}))
    world = world.given(valuation=valuation(1000))
    for records in ((OWN, RAISED), (RAISED, OWN)):
        verdict = world.at(records=records).check()
        assert (verdict.limit, verdict.value, verdict.bound) == (Limit.MAX_NET, 2000, 1250)
        assert (verdict.worst_loss, verdict.stress_move) == (800, d("0.4"))
    for records in (
        (OWN,),
        (OWN, replace(RAISED, withdrawn="x")),
        (OWN, replace(RAISED, strategy=OTHER)),
    ):
        verdict = world.at(records=records).check()
        assert verdict.approved and verdict.worst_loss == 400
        assert verdict.stress_move == d("0.2")


def test_a_third_leg_no_measurement_read_halts() -> None:
    three = {GLD: LOOSE, USO: LOOSE, SPY: LOOSE}
    world = World(
        intent=order(49, SPY), snapshot=snapshot({GLD: 1, USO: 1, SPY: 1}), records=(OWN,)
    )
    world = world.capped(max_position=three, max_order_qty=three)
    with pytest.raises(Halt, match="do not both name SPY"):
        world.held_to(max_position=three, max_order_qty=three).check()


def test_the_caps_own_halts_come_before_the_records() -> None:
    with pytest.raises(Halt, match="the hard caps' share is"):
        World(records=()).capped(share=d(2)).check()
    with pytest.raises(Halt, match="the valuation is missing"):
        World(records=()).given(valuation=None).check()
    # A V that is not above zero comes after the records.
    with pytest.raises(Halt, match="there is no stress record"):
        World(records=()).given(valuation=valuation(0)).check()


# A quote as the mark. Every figure is made up.

AGED = DECISION - timedelta(seconds=59) + timedelta(seconds=MAX_AGE) - CLOCK_BOUND


def legs(
    gld: tuple[object, object] = (100, 100),
    uso: tuple[object, object] = (10, 10),
    **changes: object,
) -> Snapshot:
    """Close−20 with a quote of each leg. `changes` are GLD's quote's own."""
    made = quoted({GLD: gld, USO: uso})
    return replace(made, quotes={**made.quotes, GLD: replace(made.quotes[GLD], **changes)})


def quoted_world(**changes: object) -> World:
    return World(book=book({CHAN: {GLD: 10}}), snapshot=legs(**changes))  # type: ignore[arg-type]


ORDERS = {
    "adding": order(1),
    "reducing": order(-1),
    "a person's, adding": order(1, strategy=""),
    "a person's, reducing": order(-1, strategy=""),
}


def test_4_a_held_leg_is_valued_at_its_midpoint() -> None:
    world = quoted_world(gld=("99.9999", "100.0002"))
    middle = d("100.00005")
    valued = world.given(valuation=valuation(world.book.cash + 10 * middle + 3))
    assert valued.loss().loss == 3
    assert world.check().worst_loss == rounded_up(11 * middle * d("0.2"))


def rounded_up(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_CEILING)


@pytest.mark.parametrize("name", ORDERS)
@pytest.mark.parametrize(("step", "halts"), [(-TICK, False), (timedelta(0), False), (TICK, True)])
def test_4_a_quote_past_its_age_when_its_order_is_checked_halts_it(
    name: str, step: timedelta, halts: bool
) -> None:
    """Good at the decision, and the gate measures the age at its own `now`."""
    world = quoted_world().at(intent=ORDERS[name])
    assert mark(world.snapshot, GLD, PRIOR, DECISION) == 100
    world.check()
    late = world.at(now=AGED + step)
    if not halts:
        late.check()
        late.loss()
        return
    for read in (late.check, late.loss):
        with pytest.raises(Halt, match="GLD's quote cannot be read: its ask has the stamp"):
            read()


@pytest.mark.parametrize("name", ORDERS)
@pytest.mark.parametrize(
    ("change", "said"),
    [
        ({"price_kind": "unstated"}, "GLD's quote is of unstated price kind"),
        ({"realtime": False}, "its realtime flag"),
        ({"status": "Halted"}, "its status is 'Halted'"),
        ({"max_age": None}, "a maximum age of None"),
        ({"bid": None}, "its bid is None"),
        ({"captured_at": DECISION + TICK}, "it was captured at"),
    ],
)
def test_4_a_quote_with_a_fault_halts_every_order_and_the_loss_reading(
    name: str, change: dict, said: str
) -> None:
    # A snapshot's quotes share one capture, so the other leg has no quote here.
    made = legs()
    made = replace(made, quotes={GLD: replace(made.quotes[GLD], **change)})
    world = World(book=book({CHAN: {GLD: 10}}), snapshot=made, intent=ORDERS[name])
    for read in (world.check, world.loss):
        with pytest.raises(Halt, match=re.escape(said)):
            read()


@pytest.mark.parametrize(
    ("ask", "halts"), [("119.9999", False), ("120", False), ("120.0001", True)]
)
def test_4_a_spread_past_its_written_share_halts(ask: str, halts: bool) -> None:
    """Of 80 by 120, the spread of 40 is 0.4 of the midpoint exactly."""
    world = quoted_world(gld=("80", ask), max_spread_share=d("0.4"))
    if not halts:
        world.check()
        world.loss()
        return
    for intent in ORDERS.values():
        with pytest.raises(Halt, match="further apart than the written 0.4 of their midpoint"):
            world.at(intent=intent).check()
    with pytest.raises(Halt, match="further apart"):
        world.loss()


def test_4_a_wide_quote_halts_rather_than_hide_a_loss_past_the_stop() -> None:
    """Long 5, in a market that fell to 60 by 60.1 and is shown as 60 by 140."""
    long = book({CHAN: {GLD: 5}}, cash=500)
    narrow = World(book=long, snapshot=legs(gld=(60, "60.1")), intent=order(1))
    narrow = narrow.held_to(loss_stop=d(100)).given(valuation=valuation("1000"))
    assert narrow.loss().loss == d("199.75")
    assert narrow.check().limit is Limit.LOSS_STOP
    wide = narrow.at(snapshot=legs(gld=(60, 140)))
    for read in (wide.check, wide.loss):
        with pytest.raises(Halt, match="further apart"):
            read()
    # With no bound on the spread the midpoint of 100 reads no loss, and the order goes out.
    blind = narrow.at(snapshot=legs(gld=(60, 140), max_spread_share=d("0.999")))
    assert blind.loss().loss == 0 and blind.check().approved


@pytest.mark.parametrize("name", ORDERS)
def test_4_a_quote_captured_after_the_decision_halts_however_late_the_gate_reads_it(
    name: str,
) -> None:
    """The judge is handed the decision time beside `now`, and not `now` for both."""
    after = DECISION + timedelta(seconds=5)
    made = quoted({GLD: (100, 100), USO: (10, 10)}, bid_at=after, ask_at=after, captured_at=after)
    world = World(book=book({CHAN: {GLD: 10}}), snapshot=made, intent=ORDERS[name])
    late = world.at(now=DECISION + timedelta(seconds=10))
    for read in (late.check, late.loss):
        with pytest.raises(Halt, match="it was captured at 2026-11-03T20:40:05"):
            read()


@pytest.mark.parametrize("name", ORDERS)
def test_4_a_quote_stamped_at_or_before_the_close_v_was_valued_at_halts(name: str) -> None:
    """A book marked there would read no loss, however far the market has moved since."""
    closed = TABLE.session(PRIOR).close
    old = legs(bid_at=closed, ask_at=closed, max_age=86_400)
    world = World(book=book({CHAN: {GLD: 10}}), snapshot=old, intent=ORDERS[name])
    for read in (world.check, world.loss):
        with pytest.raises(Halt, match="GLD has no mark: its quote's older stamp is 2026-11-02T21"):
            read()
    after = closed + TICK
    later = world.at(snapshot=legs(bid_at=after, ask_at=after, max_age=86_400))
    assert later.loss().loss is not None and later.check() is not None


def test_4_from_flat_one_aged_leg_halts_its_own_order_and_not_its_partners() -> None:
    made = legs()
    old = replace(made.quotes[USO], bid_at=made.quotes[USO].bid_at - TICK)
    world = World(snapshot=replace(made, quotes={**made.quotes, USO: old}), now=AGED)
    assert world.at(intent=order(1, GLD)).check().approved
    with pytest.raises(Halt, match="USO's quote cannot be read: its bid has the stamp"):
        world.at(intent=order(1, USO)).check()


@pytest.mark.parametrize("factor", [d(100), d("0.01")])
def test_7_a_price_wrong_by_100_times_on_both_sides_is_refused_on_a_share_cap(
    factor: Decimal,
) -> None:
    """It passes data's check, since the two sides agree, so a cap in shares is what holds."""
    wrong = (100 * factor, d("100.0001") * factor)
    made = legs(gld=wrong)
    assert quote_faults(made.quotes[GLD], DECISION, DECISION) == ()
    world = World(snapshot=made, intent=order(6)).capped(max_position={GLD: d(5), USO: d(5)})
    verdict = world.check()
    assert (verdict.approved, verdict.adding, verdict.limit) == (False, True, Limit.MAX_POSITION)
    assert world.at(intent=order(5)).check().approved
    one_side = legs(gld=(100, 100 * factor))
    with pytest.raises(Halt, match="further apart|below its bid"):
        world.at(snapshot=one_side).check()
