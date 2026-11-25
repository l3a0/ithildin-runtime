"""How a target is sized, the `[portfolio]` section, the portfolio stage and its entry point.

#9's acceptance names these tests. Every figure is made up, and each hand
calculation is written out beside the figure it gives. The blobs the stage
reads come from `write_stage`, which stands in for the stages it reads.
"""

from __future__ import annotations

import random
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    BOOK_OUTPUT,
    CAPS_OUTPUT,
    CONSTRAINTS_OUTPUT,
    FORECAST_OUTPUT,
    PORTFOLIO_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SCHEDULE_OUTPUT,
    SIZING_OUTPUT,
    SNAPSHOT_OUTPUT,
    STRATEGIES,
    TARGET_OUTPUT,
    VALUATION_OUTPUT,
    Basket,
    BlobHeader,
    BlobRef,
    Book,
    CodecError,
    Constraints,
    CostKind,
    CostSchedule,
    CostSide,
    CostTerm,
    DecisionResult,
    Forecast,
    Halt,
    HardCaps,
    InstrumentId,
    Limit,
    Mode,
    OutputFile,
    Plan,
    PlanDecision,
    Sizing,
    Snapshot,
    StageKind,
    Storage,
    StrategyConfig,
    TargetPortfolio,
    Valuation,
    decode_output,
    encode,
    encode_output,
    held,
    load_strategies,
    output_key,
    parse_strategy,
    result_key,
    sha256_of,
)
from ithildinruntime.portfolio import SECTION as NAME
from ithildinruntime.portfolio import PortfolioStage, gross, size
from ithildinruntime.runtime.alpha_worker import alpha_rows
from ithildinruntime.runtime.cost_worker import cost_rows
from ithildinruntime.runtime.data_worker import DATA_ROW
from ithildinruntime.runtime.loop import Item, ReceiveLoop, Stage, Table, routes
from ithildinruntime.runtime.portfolio_worker import portfolio_handler, portfolio_row
from ithildinruntime.runtime.risk_worker import RISK_ROW
from tests.support.gates import (
    CLOSE,
    DECISION,
    GLD,
    LOOSE,
    OTHER,
    PRIOR,
    SPY,
    TABLE,
    TODAY,
    USO,
    World,
    book,
    caps,
    constraints,
    d,
    order,
    snapshot,
    valuation,
)
from tests.support.queues import Folder, SystemTimer, running
from tests.support.queues import storage as folder_storage
from tests.test_risk import (
    DEPLOYMENT,
    RECOVER_ROW,
    RELEASE,
    RUN,
    blobs,
    storage_over,
    store,
    tracked,
    write_stage,
)
from tests.test_storage_contract import Alarms

NINE = Decimal("1E-9")


def text(g: object = 900, instruments: str = '["GLD", "USO"]') -> str:
    return f"instruments = {instruments}\n[portfolio]\ngross = {g}\n"


def strategy(written: str | None = None, name: str = "chan") -> StrategyConfig:
    return parse_strategy(name, (text() if written is None else written).encode())


CHAN = strategy()
# GLD at 100 and USO at 50. One basket is a share of USO against 0.4 of GLD, which is
# 50 and 40 dollars, 90 in all, so a G of 900 is 10 baskets: 10 USO against 4 GLD.
MARKS = {GLD: d(100), USO: d(50)}
LEGS = {USO: d(1), GLD: d("-0.4")}
MARKED = snapshot(MARKS)
# One plus the stress move is 1.25, so a dollar limit is read as four fifths of itself.
CAPS = caps(stress_move=d("0.25"))
V = valuation()


def forecast(
    direction: int = 1,
    legs: Mapping[InstrumentId, object] = LEGS,
    name: str = "chan",
    **changes: object,
) -> Forecast:
    made = Forecast(name, Basket({i: d(q) for i, q in legs.items()}), direction, 1, Decimal(1))
    return replace(made, **changes)  # type: ignore[arg-type]


def sized(
    direction: int = 1,
    legs: Mapping[InstrumentId, object] = LEGS,
    *,
    config: StrategyConfig = CHAN,
    deployed: Iterable[str] = ("chan",),
    given: Forecast | None = None,
    marked: Snapshot = MARKED,
    holds: Book | None = None,
    limits: Constraints | None = None,
    owners: HardCaps = CAPS,
    v: Valuation = V,
    step: Decimal | None = None,
) -> tuple[TargetPortfolio, Sizing]:
    return size(
        config,
        tuple(deployed),
        given or forecast(direction, legs),
        marked,
        holds or book(),
        limits or constraints(),
        owners if step is None else replace(owners, share_step=step),
        v,
    )


def counts(target: TargetPortfolio) -> dict[str, str]:
    """Each leg's share count as it is written, which shows its places and a zero's sign."""
    return {str(i): format(q, "f") for i, q in target.shares.items()}


# Acceptance 1: sizing against hand figures

# Each case: the legs, the direction, G, then each leg's target at a step of 1, and at a
# step of 1E-9, which with no limit binding is what is wanted.
HAND: dict[str, tuple[dict[InstrumentId, object], int, object, tuple[str, str], tuple[str, str]]]
HAND = {
    # 900 ÷ 90 = 10 baskets.
    "long": (LEGS, 1, 900, ("-4", "10"), ("-4.000000000", "10.000000000")),
    "short": (LEGS, -1, 900, ("4", "-10"), ("4.000000000", "-10.000000000")),
    "flat": (LEGS, 0, 900, ("0", "0"), ("0.000000000", "0.000000000")),
    # 899 ÷ 90 = 9.98888… baskets, and 0.4 of that is 3.995555….
    "toward-zero": (LEGS, 1, 899, ("-3", "9"), ("-3.995555555", "9.988888888")),
    "toward-zero-short": (LEGS, -1, 899, ("3", "-9"), ("3.995555555", "-9.988888888")),
    # 50 and 30 dollars, 80 in all: 800 ÷ 80 = 10.
    "h-of-0.3": ({USO: 1, GLD: "-0.3"}, 1, 800, ("-3", "10"), ("-3.000000000", "10.000000000")),
    # A hedge ratio below zero puts both legs on one side.
    "h-of-minus-0.3": ({USO: 1, GLD: "0.3"}, 1, 800, ("3", "10"), ("3.000000000", "10.000000000")),
    "one-side-short": (
        {USO: 1, GLD: "0.3"},
        -1,
        800,
        ("-3", "-10"),
        ("-3.000000000", "-10.000000000"),
    ),
    # A leg of nothing: 800 ÷ 50 = 16, and the zero carries no sign either way.
    "h-of-zero": ({USO: 1, GLD: 0}, 1, 800, ("0", "16"), ("0.000000000", "16.000000000")),
    "h-of-zero-short": (
        {USO: 1, GLD: "-0"},
        -1,
        800,
        ("0", "-16"),
        ("0.000000000", "-16.000000000"),
    ),
    # 50 dollars of USO against 200 of GLD, and then 50 against 10.
    "gld-dearer": ({USO: 1, GLD: -2}, 1, 2500, ("-20", "10"), ("-20.000000000", "10.000000000")),
    "gld-cheaper": ({USO: 1, GLD: "-0.1"}, 1, 600, ("-1", "10"), ("-1.000000000", "10.000000000")),
    # One leg rounds to nothing beside one that does not: 110 ÷ 90 = 1.2222…, and 0.4888….
    "one-leg-to-zero": (LEGS, 1, 110, ("0", "1"), ("-0.488888888", "1.222222222")),
    "one-leg-to-zero-short": (LEGS, -1, 110, ("0", "-1"), ("0.488888888", "-1.222222222")),
    # A leg below a billionth of a share is wanted as nothing, unsigned.
    "a-leg-below-a-billionth": (
        {USO: 1, GLD: "-0.00000001"},
        1,
        "4.9999",
        ("0", "0"),
        ("0.000000000", "0.099997998"),
    ),
    # A factor common to every leg changes nothing.
    "a-basket-seven-times-larger": (
        {USO: 7, GLD: "-2.8"},
        1,
        900,
        ("-4", "10"),
        ("-4.000000000", "10.000000000"),
    ),
}


@pytest.mark.parametrize("case", [c for c in HAND if c != "a-leg-below-a-billionth"])
def test_1_a_target_at_a_step_of_one_is_the_hand_calculation(case) -> None:
    legs, direction, g, (gld, uso), (w_gld, w_uso) = HAND[case]
    target, record = sized(direction, legs, config=strategy(text(g)))
    assert counts(target) == {"GLD": gld, "USO": uso}
    assert (target.decision_at, target.strategy) == (DECISION, "chan")
    assert {i: format(q, "f") for i, q in record.wanted.items()} == {"GLD": w_gld, "USO": w_uso}
    assert (record.strategy, record.cut) == ("chan", ())


@pytest.mark.parametrize("case", HAND)
def test_1_a_target_at_a_step_of_a_billionth_is_what_is_wanted(case) -> None:
    legs, direction, g, _, (gld, uso) = HAND[case]
    target, record = sized(direction, legs, config=strategy(text(g)), step=NINE)
    assert counts(target) == {"GLD": gld, "USO": uso}
    assert record == Sizing("chan", target.shares, ())


def test_1_each_step_rounds_to_its_own_places() -> None:
    assert counts(sized(step=d("0.1"), config=strategy(text(899)))[0]) == {
        "GLD": "-3.9",
        "USO": "9.9",
    }
    assert counts(sized(0, step=d("0.001"))[0]) == {"GLD": "0.000", "USO": "0.000"}


CUT = {
    # At 10 USO and 4 GLD wanted. One below, at and above what binds, each with the other
    # leg's loose.
    "position-uso-below": ({"max_position": {USO: 9}}, "-3", "9", (Limit.MAX_POSITION,)),
    "position-uso-at": ({"max_position": {USO: 10}}, "-4", "10", ()),
    "position-uso-above": ({"max_position": {USO: 11}}, "-4", "10", ()),
    # 3 ÷ 0.4 = 7.5 baskets.
    "position-gld-below": ({"max_position": {GLD: 3}}, "-3", "7", (Limit.MAX_POSITION,)),
    "position-gld-at": ({"max_position": {GLD: 4}}, "-4", "10", ()),
    "position-gld-above": ({"max_position": {GLD: 5}}, "-4", "10", ()),
    # 900 dollars with room for a move of 0.25 is 1125.
    "gross-below": ({"max_gross": "1124.9999"}, "-3", "9", (Limit.MAX_GROSS,)),
    "gross-at": ({"max_gross": 1125}, "-4", "10", ()),
    "gross-above": ({"max_gross": "1125.0001"}, "-4", "10", ()),
    # 720 of 900 dollars: 900 ÷ 1.25 ÷ 90 = 8 baskets.
    "gross-at-g": ({"max_gross": 900}, "-3", "8", (Limit.MAX_GROSS,)),
    # The long USO leg's 500 dollars, with room, is 625.
    "net-below": ({"max_net": "624.9999"}, "-3", "9", (Limit.MAX_NET,)),
    "net-at": ({"max_net": 625}, "-4", "10", ()),
    "net-above": ({"max_net": "625.0001"}, "-4", "10", ()),
    # 500 ÷ 1.25 ÷ 50 = 8 baskets.
    "net-at-the-legs-dollars": ({"max_net": 500}, "-3", "8", (Limit.MAX_NET,)),
    # The tighter of two is taken, whichever it is, and both are named.
    "position-tighter-than-net": (
        {"max_position": {USO: 7}, "max_net": 500},
        "-2",
        "7",
        (Limit.MAX_POSITION, Limit.MAX_NET),
    ),
    "net-tighter-than-position": (
        {"max_position": {USO: 9}, "max_net": 500},
        "-3",
        "8",
        (Limit.MAX_POSITION, Limit.MAX_NET),
    ),
    # 787.5 ÷ 1.25 ÷ 90 = 7 baskets.
    "gross-tighter-than-net": (
        {"max_gross": "787.5", "max_net": 500},
        "-2",
        "7",
        (Limit.MAX_GROSS, Limit.MAX_NET),
    ),
    # 375 ÷ 1.25 ÷ 50 = 6 baskets.
    "net-tightest-of-three": (
        {"max_position": {GLD: 3}, "max_gross": "787.5", "max_net": 375},
        "-2",
        "6",
        (Limit.MAX_POSITION, Limit.MAX_GROSS, Limit.MAX_NET),
    ),
    # 0.9 ÷ 0.4 = 2.25 baskets, which leaves no whole share of GLD beside two of USO.
    "position-gld-below-a-share": ({"max_position": {GLD: "0.9"}}, "0", "2", (Limit.MAX_POSITION,)),
    "both-legs-positions": (
        {"max_position": {GLD: 3, USO: 5}},
        "-2",
        "5",
        (Limit.MAX_POSITION,),
    ),
}


def held_to(**changes: object) -> Constraints:
    """Loose limits with these changed, and each leg's `max_position` loose unless it is named."""
    position = {GLD: LOOSE, USO: LOOSE} | {
        i: d(q)
        for i, q in changes.pop("max_position", {}).items()  # type: ignore[attr-defined]
    }
    return constraints(max_position=position, **{k: d(v) for k, v in changes.items()})


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("case", CUT)
def test_1_each_limit_below_at_and_above_what_binds(case, direction) -> None:
    changes, gld, uso, cut = CUT[case]
    limits = held_to(**changes)
    target, record = sized(direction, limits=limits)
    assert {i: q * direction for i, q in target.shares.items()} == {GLD: d(gld), USO: d(uso)}
    assert record.cut == cut
    # What is wanted does not move with a cap.
    assert {i: q * direction for i, q in record.wanted.items()} == {GLD: d(-4), USO: d(10)}
    # The hard caps bind as the strategy's own limits do, where `core.tighten` takes them.
    if "max_net" not in changes:
        owners = replace(CAPS, **{k: getattr(limits, k) for k in changes})
        assert sized(direction, owners=owners) == (target, record)


@pytest.mark.parametrize("direction", [1, -1])
def test_1_net_is_the_larger_side_and_not_the_difference(direction) -> None:
    """The gate counts each leg with the other unfilled, so either side alone has to fit."""
    # Two legs of 50 and 200 dollars, 10 baskets at a G of 2500. GLD's side is 2000, which
    # with room is 2500, and the difference of the sides is 1500.
    dearer = strategy(text(2500))
    legs = {USO: 1, GLD: -2}
    for net, gld, cut in ((2500, -20, ()), ("2499.9999", -19, (Limit.MAX_NET,))):
        target, record = sized(direction, legs, config=dearer, limits=held_to(max_net=net))
        assert (target.shares[GLD] * direction, record.cut) == (gld, cut)
    # Both legs on one side come to the whole 800, which with room is 1000.
    one_side = strategy(text(800))
    legs = {USO: 1, GLD: "0.3"}
    for net, uso, cut in ((1000, 10, ()), ("999.9999", 9, (Limit.MAX_NET,))):
        target, record = sized(direction, legs, config=one_side, limits=held_to(max_net=net))
        assert (target.shares[USO] * direction, record.cut) == (uso, cut)


def test_1_the_room_is_the_stress_move_and_not_its_range() -> None:
    owners = replace(CAPS, stress_low=d("0.1"), stress_move=d("0.25"), stress_high=d("0.6"))
    assert sized(owners=owners, limits=held_to(max_gross=1125))[1].cut == ()
    assert sized(owners=owners, limits=held_to(max_gross="1124.9999"))[1].cut == (Limit.MAX_GROSS,)
    # `max_position` counts shares and needs no room.
    assert sized(owners=owners, limits=held_to(max_position={USO: 10}))[1].cut == ()


def test_1_a_limit_applied_to_a_leg_of_nothing_bounds_nothing() -> None:
    limits = held_to(max_position={GLD: 0})
    target, record = sized(1, {USO: 1, GLD: 0}, limits=limits)
    assert (counts(target), record.cut) == ({"GLD": "0", "USO": "18"}, ())


def test_1_the_order_a_basket_is_written_in_changes_nothing() -> None:
    backward = forecast(legs=dict(reversed(LEGS.items())))
    one, other = sized(), sized(given=backward)
    assert encode(one[0]) == encode(other[0]) and encode(one[1]) == encode(other[1])
    assert list(one[0].shares) == list(one[1].wanted) == [GLD, USO]


def test_1_figures_of_the_codecs_every_digit_compare_exactly() -> None:
    """Two limits are compared across their divisors, four factors of a record's digits."""
    tail = "000000000000000000000000000000000001"
    leg, price, move = d(f"1.{tail}"), d(f"50.{tail}"), d(f"0.25{tail[2:]}")
    owners = replace(CAPS, stress_move=move, share_step=NINE)
    limits = held_to(max_gross=d(f"1124.9{tail[4:]}"), max_net=d(f"1124.{tail[4:]}"))
    marked = snapshot({GLD: 100, USO: price})
    legs = {USO: leg, GLD: 0}
    target, record = sized(1, legs, owners=owners, limits=limits, marked=marked)
    expected, cut = oracle(
        {i: d(q) for i, q in legs.items()}, {GLD: d(100), USO: price}, d(900), limits, move, NINE
    )
    assert {i: Fraction(q) for i, q in target.shares.items()} == expected
    assert list(record.cut) == cut == [Limit.MAX_GROSS, Limit.MAX_NET]


def oracle(
    legs: Mapping[InstrumentId, Decimal],
    marks: Mapping[InstrumentId, Decimal],
    g: Decimal,
    limits: Constraints,
    move: Decimal,
    step: Decimal,
) -> tuple[dict[InstrumentId, Fraction], list[Limit]]:
    """The shares and the limits that cut them, in exact fractions, sharing no code with `size`.

    `legs` are already signed by the direction.
    """
    x = {i: Fraction(q) for i, q in legs.items()}
    worth = {i: x[i] * Fraction(marks[i]) for i in x}
    whole = sum(map(abs, worth.values()), Fraction(0))
    longs = sum((w for w in worth.values() if w > 0), Fraction(0))
    side = max(longs, whole - longs)
    room = 1 + Fraction(move)
    asked = Fraction(g) / whole
    bounds = [
        *((Limit.MAX_POSITION, Fraction(limits.max_position[i]) / abs(x[i])) for i in x if x[i]),
        (Limit.MAX_GROSS, Fraction(limits.max_gross) / room / whole),
        (Limit.MAX_NET, Fraction(limits.max_net) / room / side),
    ]
    u = min([asked, *(bound for _, bound in bounds)])
    cut = [n for n in Limit if any(limit is n and bound < asked for limit, bound in bounds)]
    shares = {
        i: (1 if q > 0 else -1) * (abs(u * q) // Fraction(step)) * Fraction(step)
        for i, q in x.items()
    }
    return shares, cut


def test_1_the_sizing_agrees_with_an_oracle_in_exact_fractions() -> None:
    draw = random.Random(9)
    seen = {"sized": 0, "cut": 0, "by two": 0, "zero": 0}
    for _ in range(1500):
        names = [InstrumentId(f"I{n}") for n in range(draw.choice([1, 2, 2, 2, 3, 5, 10]))]
        legs = {
            i: d(draw.choice([1, "0.3", round(draw.uniform(0.001, 3), 8), 0]))
            * draw.choice([1, -1])
            for i in names
        }
        if not any(legs.values()):
            continue
        marks = {i: d(round(draw.uniform(0.01, 900), 4)) for i in names}
        g = d(round(draw.choice([draw.uniform(1, 2e3), draw.uniform(50, 2e5)]), 2))
        move = d(round(draw.uniform(0.02, 1.5), 8))
        step = d(draw.choice(["1", "0.1", "0.000000001"]))
        loose = dict.fromkeys(names, LOOSE)
        position = {i: d(draw.choice([10**8, round(draw.uniform(0, 500))])) for i in names}
        limits = constraints(
            max_order_qty=loose,
            max_position=position,
            max_gross=d(draw.choice([10**8, round(draw.uniform(0, 3e5), 4)])),
            max_net=d(draw.choice([10**8, round(draw.uniform(0, 3e5), 4)])),
        )
        owners = caps(
            max_order_qty=loose,
            max_position=loose,
            stress_move=move,
            stress_low=d("0.01"),
            stress_high=d(2),
        )
        direction = draw.choice([1, -1])
        config = strategy(text(g, str([str(i) for i in names])))
        turned = {i: q * direction for i, q in legs.items()}
        expected, cut = oracle(turned, marks, g, limits, move, step)
        try:
            target, record = sized(
                direction,
                legs,
                config=config,
                marked=snapshot(marks),
                limits=limits,
                owners=owners,
                step=step,
            )
        except Halt as halt:
            assert not any(expected.values()), halt.reason
            seen["zero"] += 1
            continue
        # A held direction that comes to nothing halts, so it is never a target.
        assert any(expected.values())
        assert {i: Fraction(q) for i, q in target.shares.items()} == expected
        assert list(record.cut) == cut
        wanted, _ = oracle(turned, marks, g, constraints(max_position=loose), move, NINE)
        assert {i: Fraction(q) for i, q in record.wanted.items()} == wanted
        seen["sized"] += 1
        seen["cut"] += bool(cut)
        seen["by two"] += len(cut) > 1
    assert min(seen.values()) > 5 and seen["sized"] > 1000


# Acceptance 2: the target reads nothing else

BOOKS = {
    "flat": book(),
    "long": book({"chan": {USO: 10, GLD: -4}}),
    "short": book({"chan": {USO: -20, GLD: 8}}),
    "zeros": book({"chan": {USO: 0, GLD: 0}}),
    "open-orders": book(
        {"chan": {USO: 3}}, [order(-1, USO, "chan"), order(2, GLD, "chan", "new-2")]
    ),
    "other-cash": book({"chan": {USO: 10, GLD: -4}}, cash=-5),
    "a-win": book({"chan": {USO: 10}}, platform_realised_pnl=d(10**6)),
    "a-loss": book({"chan": {USO: 10}}, platform_realised_pnl=d(-(10**6))),
}


@pytest.mark.parametrize("direction", [1, 0, -1])
@pytest.mark.parametrize("name", BOOKS)
def test_2_the_target_is_the_same_whatever_is_held(name, direction) -> None:
    assert sized(direction, holds=BOOKS[name]) == sized(direction)
    cut = held_to(max_net=500)
    assert sized(direction, holds=BOOKS[name], limits=cut) == sized(direction, limits=cut)


@pytest.mark.parametrize("value", [10**6, 5 * 10**8, 10**10])
def test_2_with_no_limit_binding_the_accounts_value_changes_nothing(value) -> None:
    # The net cap is 0.5 × V ÷ 0.25, which is 2,000,000 at the smallest.
    assert sized(v=valuation(value)) == sized()


def test_2_confidence_and_horizon_scale_nothing() -> None:
    other = forecast(confidence=d("0.25"), horizon_sessions=20)
    assert sized(given=other) == sized()


@pytest.mark.parametrize("step", [d(1), NINE], ids=["whole", "fractional"])
def test_2_a_larger_v_alone_grows_the_target_to_the_new_cap_less_room(step) -> None:
    """#33's test: a flat book, the net cap binding, and the account's value alone raised."""
    owners = replace(CAPS, share=d("0.02"), share_step=step)
    # 0.02 × 5,000 ÷ 0.25 = 400, less room 320, over 50 dollars is 6.4 baskets.
    small, record = sized(owners=owners, v=valuation(5000))
    assert record.cut == (Limit.MAX_NET,)
    assert small.shares[USO] == (6 if step == 1 else d("6.4"))
    assert small.shares[GLD] == (-2 if step == 1 else d("-2.56"))
    # 0.02 × 6,250 ÷ 0.25 = 500, less room 400, is 8 baskets.
    large, record = sized(owners=owners, v=valuation(6250))
    assert record.cut == (Limit.MAX_NET,)
    assert large.shares == {GLD: d(-3) if step == 1 else d("-3.2"), USO: d(8)}


def test_2_a_lower_stress_move_alone_grows_the_target() -> None:
    owners = replace(CAPS, share=d("0.02"), share_step=NINE)
    v = valuation(5000)
    assert sized(owners=owners, v=v)[0].shares[USO] == d("6.4")
    # 0.02 × 5,000 ÷ 0.2 = 500, and 500 ÷ 1.2 ÷ 50 = 8.3333….
    lower, record = sized(owners=replace(owners, stress_move=d("0.2")), v=v)
    assert lower.shares == {GLD: d("-3.333333333"), USO: d("8.333333333")}
    assert record.cut == (Limit.MAX_NET,)


# Acceptance 5 and 6, on the function: what must agree before it sizes


def refused(**changes: object) -> str:
    with pytest.raises(Halt) as caught:
        sized(**changes)  # type: ignore[arg-type]
    return caught.value.reason


DIRECTIONS = pytest.mark.parametrize("direction", [1, 0, -1])
NO_SECTION = strategy('instruments = ["GLD", "USO"]\n')


@DIRECTIONS
def test_5_no_section_halts_at_every_direction_before_anything_else(direction) -> None:
    # The book holds another strategy's shares too, which is the next check.
    reason = refused(direction=direction, config=NO_SECTION, holds=book({OTHER: {USO: 1}}))
    assert reason == (
        "strategy chan's configuration chan.toml has no [portfolio] section, or an empty one"
    )


@DIRECTIONS
@pytest.mark.parametrize(
    ("holds", "what"),
    [
        (book({OTHER: {USO: 1}}), "USO"),
        (book({OTHER: {USO: -1}, "chan": {USO: 1}}), "USO"),
        (book(opens=[order(1, GLD, OTHER)]), "GLD"),
        (book({"": {GLD: "0.000000001"}}), "GLD"),
    ],
    ids=["shares", "shares-that-offset", "an-open-order", "no-strategys"],
)
def test_5_a_book_held_under_a_name_the_deployment_does_not_run_halts(
    direction, holds, what
) -> None:
    (name,) = {*holds.positions_by_strategy, *holds.open_orders_by_strategy} - {"chan"}
    assert refused(direction=direction, holds=holds) == (
        f"the book holds {what}, or an open order in it, under {name!r}, which the deployment does "
        "not run"
    )
    # A deployment that runs it sizes as before.
    assert sized(direction, deployed=("chan", name), holds=holds) == sized(direction)


@DIRECTIONS
def test_5_a_position_of_zero_under_another_name_passes(direction) -> None:
    holds = book({OTHER: {USO: 0, SPY: "0.000"}})
    assert sized(direction, holds=holds) == sized(direction)


@DIRECTIONS
def test_5_a_forecast_of_another_strategy_halts(direction) -> None:
    assert refused(given=forecast(direction, name="reversal")) == (
        "chan's forecast is of strategy 'reversal'"
    )


@DIRECTIONS
@pytest.mark.parametrize(
    ("legs", "odd"),
    [
        ({USO: 1}, "GLD"),
        ({GLD: 1}, "USO"),
        ({USO: 1, GLD: 1, SPY: 1}, "SPY"),
        ({USO: 1, SPY: 1}, "GLD"),
        ({USO: 1, SPY: 0, GLD: 1}, "SPY"),
    ],
    ids=["no-gld", "no-uso", "one-more", "another", "one-more-of-zero"],
)
def test_5_a_basket_of_other_instruments_than_the_strategys_halts(direction, legs, odd) -> None:
    reason = refused(given=forecast(direction, legs), marked=snapshot({GLD: 1, USO: 1, SPY: 1}))
    assert reason == f"chan's basket and the instruments it trades do not both name {odd}"


@DIRECTIONS
@pytest.mark.parametrize(
    "holds",
    [
        book({"chan": {USO: 10, SPY: 1}}),
        book({"chan": {SPY: "-0.000000001"}}),
        book({"chan": {USO: 10}}, [order(1, SPY, "chan")]),
    ],
    ids=["shares", "a-sliver", "an-open-order"],
)
def test_5_a_held_instrument_the_forecast_does_not_name_halts(direction, holds) -> None:
    assert refused(direction=direction, holds=holds) == (
        "chan holds SPY, or an open order in it, and its forecast does not name it"
    )


@DIRECTIONS
def test_5_a_position_of_zero_the_forecast_does_not_name_passes(direction) -> None:
    holds = book({"chan": {USO: 10, SPY: 0}})
    assert sized(direction, holds=holds) == sized(direction)


@DIRECTIONS
@pytest.mark.parametrize("bad", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("leg", [GLD, USO])
def test_5_a_leg_that_is_not_finite_halts(direction, bad, leg) -> None:
    """#67: a fitted ratio that is no number sizes nothing, whatever the direction."""
    legs = {**LEGS, leg: d(bad)}
    reason = refused(given=forecast(direction, legs))
    assert reason.startswith(f"chan's leg in {leg} is ") and "not a finite decimal" in reason


@pytest.mark.parametrize("bad", ["NaN", "Infinity"])
def test_5_the_codec_and_the_gate_refuse_such_a_leg_too(bad) -> None:
    with pytest.raises(CodecError, match="not finite"):
        encode(forecast(legs={**LEGS, GLD: d(bad)}))
    with pytest.raises(Halt, match="the order new-1 is .*, not a finite decimal"):
        World(intent=order(bad)).check()


@DIRECTIONS
@pytest.mark.parametrize("zero", ["0", "-0", "0E-8"])
def test_5_a_basket_of_nothing_halts(direction, zero) -> None:
    assert refused(direction=direction, legs={USO: zero, GLD: zero}) == (
        "every leg of chan's basket is zero"
    )


@DIRECTIONS
def test_5_a_leg_of_zero_is_held_to_a_limit_and_a_price_too(direction) -> None:
    legs = {USO: 1, GLD: 0}
    of_uso = {"max_order_qty": {USO: LOOSE}, "max_position": {USO: LOOSE}}
    assert refused(direction=direction, legs=legs, limits=constraints(**of_uso)) == (
        "no limit names GLD, a leg of chan's basket"
    )
    assert refused(direction=direction, legs=legs, marked=snapshot({USO: 50})) == (
        "GLD missing for the session"
    )


def test_5_the_first_fault_in_order_decides() -> None:
    """Every fault of items 6 to 11 and the zero target at once, each cleared in turn."""
    faults: list[tuple[dict[str, object], str]] = [
        ({"config": CHAN}, "has no [portfolio] section"),
        ({"holds": book({"chan": {SPY: 1}})}, "under 'other', which the deployment does not run"),
        ({"name": "chan"}, "chan's forecast is of strategy 'reversal'"),
        (
            {"legs": {USO: "NaN", GLD: 0}},
            "chan's basket and the instruments it trades do not both name SPY",
        ),
        ({"holds": book()}, "chan holds SPY, or an open order in it"),
        ({"legs": {USO: 0, GLD: 0}}, "chan's leg in USO is"),
        ({"legs": {USO: 1, GLD: 0}}, "every leg of chan's basket is zero"),
        ({"v": valuation(0)}, "the valuation is of 2026-11-03"),
        ({"v": valuation(10)}, "the valuation's value is 0"),
        ({"limits": held_to(max_gross=1)}, "no limit names GLD"),
        ({"marked": MARKED}, "GLD missing for the session"),
        ({"limits": held_to()}, "cut by max_gross and max_net"),
    ]
    given: dict[str, object] = {
        "config": NO_SECTION,
        "holds": book({"chan": {SPY: 1}, OTHER: {USO: 1}}),
        "name": "reversal",
        "legs": {USO: "NaN", GLD: 0, SPY: 1},
        "v": valuation(0, session=TODAY),
        "limits": constraints(max_order_qty={USO: LOOSE}, max_position={USO: LOOSE}),
        "marked": snapshot({USO: 50}),
    }

    def asked() -> dict[str, object]:
        rest = {k: v for k, v in given.items() if k not in ("name", "legs")}
        return rest | {"given": forecast(1, given["legs"], given["name"])}  # type: ignore[arg-type]

    for cleared, reason in faults:
        assert reason in refused(**asked())
        given |= cleared
    # With a net cap of 0.5 × 10 ÷ 0.25 = 20 dollars, less than a share of USO is left.
    assert refused(**asked()).endswith("cut by max_net")
    given["v"] = V
    assert sized(**asked())[0].shares == {GLD: d(0), USO: d(18)}  # type: ignore[arg-type]


def test_5_a_count_past_what_a_record_holds_halts() -> None:
    """The reader takes a G of 1e30, and at a cent a share that is a count of 33 digits."""
    vast = strategy(text("1e30"))
    legs = {USO: "0.0001", GLD: 0}
    reason = refused(config=vast, legs=legs, marked=snapshot({GLD: 100, USO: "0.01"}))
    assert reason.startswith("chan's target in USO cannot be recorded at ")


@DIRECTIONS
@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        (
            {"v": valuation(session=TABLE.before(PRIOR).day)},
            "the valuation is of 2026-10-30, not of 2026-11-02, the session before the decision's",
        ),
        (
            {"v": valuation(session=TODAY)},
            "the valuation is of 2026-11-03, not of 2026-11-02, the session before the decision's",
        ),
        (
            {"owners": replace(CAPS, stress_max_age_days=3)},
            "the hard caps' stress_through is 2026-10-30, 4 days before the decision and past the 3"
            " allowed",
        ),
        (
            {"owners": replace(CAPS, stress_move=d("0.7"))},
            "stress_move is 0.7, not from 0.1 to 0.6",
        ),
        ({"v": valuation(0)}, "the valuation's value is 0, not above zero"),
        ({"limits": constraints(max_net=d(-1))}, "the constraints' max_net is -1, below zero"),
        (
            {"owners": replace(CAPS, max_order_qty={GLD: d(1)}, max_position={GLD: d(1)})},
            "the constraints limit USO, which the hard caps do not",
        ),
        (
            {
                "limits": constraints(
                    max_order_qty={GLD: LOOSE}, max_position={GLD: LOOSE}, strategy="chan"
                )
            },
            "no limit names USO, a leg of chan's basket",
        ),
    ],
    ids=[
        "an-older-v",
        "todays-v",
        "an-old-stress-move",
        "a-stress-move-out-of-range",
        "a-v-of-zero",
        "a-negative-limit",
        "caps-without-a-leg",
        "no-limit-on-a-leg",
    ],
)
def test_5_limits_no_decision_may_use_halt(direction, changes, reason) -> None:
    assert reason in refused(direction=direction, **changes)


@DIRECTIONS
def test_5_the_current_check_comes_before_the_limits(direction) -> None:
    both = {"v": valuation(0, session=TODAY), "owners": replace(CAPS, share=d(2))}
    assert refused(direction=direction, **both).startswith("the valuation is of 2026-11-03")


STEPS = [
    ("0", "0"),
    ("-1", "-1"),
    ("0.3", "0.3"),
    ("10", "10"),
    ("1E+1", "1E+1"),
    ("2", "2"),
    ("0.0000000001", "1E-10"),
    ("1.0", "1.0"),
    ("0.10", "0.10"),
    ("0.01000", "0.01000"),
    ("NaN", "Decimal('NaN')"),
    ("Infinity", "Decimal('Infinity')"),
]


@DIRECTIONS
@pytest.mark.parametrize(("written", "shown"), STEPS)
def test_5_a_share_step_of_another_form_halts(direction, written, shown) -> None:
    reason = refused(direction=direction, step=d(written))
    assert reason.startswith(f"the hard caps' share_step is {shown}, not ")


@DIRECTIONS
@pytest.mark.parametrize(
    ("marked", "reason"),
    [
        # Before #100 every close−20 decision halts here, even from flat.
        (
            snapshot(MARKS, day=PRIOR),
            "GLD has no mark: its newest bar is for 2026-11-02, not later than 2026-11-02",
        ),
        (snapshot({GLD: 100}), "USO missing for the session"),
        (
            snapshot({GLD: 0, USO: 50}, prior={GLD: 1}),
            "GLD's close for 2026-11-03 is 0, not above zero",
        ),
    ],
    ids=["no-mark", "no-bar", "a-price-of-zero"],
)
def test_5_a_leg_with_no_price_halts(direction, marked, reason) -> None:
    assert refused(direction=direction, marked=marked) == reason


def test_5_a_decision_at_the_close_is_sized_as_one_before_it() -> None:
    made, record = sized(marked=snapshot(MARKS, at=CLOSE))
    assert made == replace(sized()[0], decision_at=CLOSE) and record == sized()[1]


ZEROS = {
    # A G below one share of either leg: 49 ÷ 90 = 0.5444… of USO.
    "a-g-below-a-share": ({"config": strategy(text(49))}, 49, "that no limit cut"),
    "with-a-cap-binding-too": (
        {"config": strategy(text(49)), "limits": held_to(max_net=20)},
        49,
        "cut by max_net",
    ),
    "a-cap-below-a-share": (
        {"limits": held_to(max_position={USO: "0.9"}, max_gross=50)},
        900,
        "cut by max_position and max_gross",
    ),
    "a-cap-of-zero": ({"limits": held_to(max_gross=0)}, 900, "cut by max_gross"),
    "a-step-above-the-target": (
        {"config": strategy(text("0.0001")), "step": d("0.001")},
        "0.0001",
        "that no limit cut",
    ),
    # A true V of 5,000 read as 50: 0.02 × 50 ÷ 0.25 = 4, and 4 ÷ 1.25 ÷ 50 is 0.064 baskets.
    "a-v-100-times-low": (
        {"owners": replace(CAPS, share=d("0.02")), "v": valuation(50)},
        900,
        "cut by max_net",
    ),
}


@pytest.mark.parametrize("direction", [1, -1])
def test_5_a_v_read_100_times_low_halts_a_book_on_one_side(direction) -> None:
    """The gate approves every sale there, so this halt is what stops them."""
    owners = replace(CAPS, share=d("0.02"))
    holds = book({"chan": {USO: 10 * direction, GLD: 4 * direction}})
    # A true V of 100,000 read as 1,000: 0.02 × 1,000 ÷ 0.25 = 80, less room 64, under 90.
    given = {"legs": {USO: 1, GLD: "0.4"}, "owners": owners, "holds": holds}
    assert sized(direction, v=valuation(100_000), **given)[1].cut == ()  # type: ignore[arg-type]
    assert refused(direction=direction, v=valuation(1000), **given) == (
        f"chan forecasts {direction} and its target is zero shares in every leg, at a gross of 900 "
        "cut by max_net"
    )


def test_5_a_cap_below_one_share_of_one_leg_sells_down_to_the_other() -> None:
    """Left standing: the gate approves both orders, and #11's alert reports the target."""
    holds = book({"chan": {USO: 10, GLD: -4}})
    for limits, uso in ((held_to(max_position={GLD: "0.9"}), 2), (held_to(max_gross=120), 1)):
        target, record = sized(limits=limits, holds=holds)
        assert target.shares == {GLD: d(0), USO: d(uso)} and record.cut
        first, second = order(4, GLD), order(uso - 10, USO, name="new-2")
        world = World(first, holds, MARKED, limits).given(caps=CAPS)
        sent = replace(holds, open_orders_by_strategy={"chan": (first,)})
        for verdict in (world.check(), world.at(intent=second, book=sent).check()):
            assert verdict.approved and not verdict.adding


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("case", ZEROS)
def test_5_a_held_direction_sized_to_nothing_halts_naming_g_and_what_cut_it(
    case, direction
) -> None:
    changes, g, why = ZEROS[case]
    for holds in (book(), book({"chan": {USO: 10 * direction, GLD: -4 * direction}})):
        assert refused(direction=direction, holds=holds, **changes) == (
            f"chan forecasts {direction} and its target is zero shares in every leg, at a gross "
            f"of {g} {why}"
        )
    # A flat forecast is an explicit zero, at any G and any limit.
    assert not any(sized(0, **changes)[0].shares.values())


def test_5_a_v_read_low_that_leaves_one_share_is_sized_to_it() -> None:
    """Left standing: the gate approves both sales this asks for, and #11's alert reports it.

    A true V of 250,000 read as 2,500 caps net at 0.02 × 2,500 ÷ 0.25 = 200. Both legs are on
    one side, at 90 dollars a basket, and 200 ÷ 1.25 ÷ 90 is 1.7777… baskets.
    """
    owners = replace(CAPS, share=d("0.02"))
    legs = {USO: 1, GLD: "0.4"}
    holds = book({"chan": {USO: 10, GLD: 4}}, cash=249_100)
    true, record = sized(1, legs, owners=owners, v=valuation(250_000), holds=holds)
    assert (counts(true), record.cut) == ({"GLD": "4", "USO": "10"}, ())
    target, record = sized(1, legs, owners=owners, v=valuation(2500), holds=holds)
    assert (counts(target), record.cut) == ({"GLD": "0", "USO": "1"}, (Limit.MAX_NET,))
    first, second = order(-4, GLD), order(-9, USO, name="new-2")
    world = World(first, holds, MARKED).given(caps=owners, valuation=valuation(2500))
    sent = replace(holds, open_orders_by_strategy={"chan": (first,)})
    for verdict in (world.check(), world.at(intent=second, book=sent).check()):
        assert verdict.approved and not verdict.adding


# Acceptance 8: the [portfolio] section

BAD_SECTIONS = {
    "absent": ("", "has no [portfolio] section, or an empty one"),
    "empty": ("[portfolio]\n", "has no [portfolio] section, or an empty one"),
    "a-blank-table": ("[portfolio.gross]\n", "has no [portfolio] section, or an empty one"),
    "another-key-alone": (
        "[portfolio]\nnet = 1\n",
        "has a [portfolio] section that states no gross",
    ),
    "an-unknown-key": (
        "[portfolio]\ngross = 1\nnet = 1\nalpha = 2\n",
        "has a [portfolio] section that holds alpha, net, which this form does not have",
    ),
    **{
        name: (
            f"[portfolio]\ngross = {written}\n",
            f"has a [portfolio] section that has gross = {said}",
        )
        for name, written, said in (
            ("a-bool", "true", "True, which is not a number"),
            ("text", '"900"', "'900', which is not a number"),
            ("a-list", "[900]", "(900,), which is not a number"),
            ("a-date", "2026-11-03", "datetime.date(2026, 11, 3), which is not a number"),
            ("zero", "0", "0, which is not a finite number above zero"),
            ("zero-written-long", "0.0000", "0.0000, which is not a finite number above zero"),
            ("negative", "-900", "-900, which is not a finite number above zero"),
            ("just-negative", "-0.0001", "-0.0001, which is not a finite number above zero"),
            ("five-places", "900.00001", "900.00001, which carries more than 4 places"),
            ("a-spare-place", "900.00000", "900.00000, which carries more than 4 places"),
        )
    },
}


@pytest.mark.parametrize("case", BAD_SECTIONS)
def test_8_a_section_the_form_refuses_halts_naming_the_strategy_and_the_key(case) -> None:
    written, reason = BAD_SECTIONS[case]
    config = strategy(f'instruments = ["GLD", "USO"]\n{written}', name="reversal")
    with pytest.raises(Halt) as caught:
        gross(config)
    assert caught.value.reason == f"strategy reversal's configuration reversal.toml {reason}"


@pytest.mark.parametrize("written", ["inf", "-inf", "nan", "1e400"])
def test_8_a_g_the_strategy_reader_refuses_fails_the_load(written) -> None:
    with pytest.raises(Halt, match="strategy chan's configuration chan.toml holds portfolio.gross"):
        strategy(text(written))


@pytest.mark.parametrize(
    "written", ["1", "900", "900.0000", "0.0001", "1e4", "12345678901234567890"]
)
def test_8_a_section_is_read_exactly(written) -> None:
    assert str(gross(strategy(text(written)))) == str(Decimal(written))


@pytest.mark.parametrize("bad", ["Infinity", "NaN"])
def test_8_a_g_that_is_not_finite_halts_where_a_config_is_built_in_memory(bad) -> None:
    config = replace(CHAN, sections={NAME: {"gross": d(bad)}})
    with pytest.raises(Halt, match="gross = .*, which is not a finite number above zero"):
        gross(config)


# Each tracked strategy with no G written yet, and the issue that writes it.
WITHOUT_G = {"chan_bollinger": "#129"}


def readable(folder: Path, names: list[str]) -> list[str]:
    """Each strategy whose `[portfolio]` section is read. One with no section is left out."""
    found = []
    for config in load_strategies(folder, names):
        if NAME in config.sections:
            assert gross(config) > 0
            found.append(config.name)
    return found


def test_8_every_tracked_section_is_one_the_reader_reads(tmp_path) -> None:
    names = tracked()
    assert names, "no strategy file is tracked"
    read = readable(STRATEGIES, names)
    assert set(names) - set(read) == set(WITHOUT_G)
    # No tracked file holds a section until #129, so a fixture runs the same check on one.
    for name in names:
        (tmp_path / f"{name}.toml").write_bytes((STRATEGIES / f"{name}.toml").read_bytes())
    (tmp_path / "fixture.toml").write_text(text())
    assert readable(tmp_path, [*names, "fixture"]) == sorted([*read, "fixture"])
    (tmp_path / "fixture.toml").write_text(text(0))
    with pytest.raises(Halt, match="strategy fixture's configuration .* gross = 0"):
        readable(tmp_path, [*names, "fixture"])


@pytest.mark.parametrize("name", WITHOUT_G)
def test_8_a_tracked_strategy_with_no_section_halts(name) -> None:
    (config,) = load_strategies(STRATEGIES, [name])
    with pytest.raises(Halt, match=f"{name}.toml has no \\[portfolio\\] section, or an empty one"):
        sized(config=config, given=forecast(0, name=name), deployed=[name])


# What `core` gained for this stage


def test_held_takes_one_strategy_as_position_does() -> None:
    both = book({"chan": {USO: 1, GLD: 0}, OTHER: {SPY: -1}}, [order(1, GLD, OTHER, "new-2")])
    assert held(both) == (GLD, SPY, USO)
    assert held(both, "chan") == (USO,)
    assert held(both, OTHER) == (GLD, SPY)
    assert held(both, "none") == ()


@pytest.mark.parametrize(
    "cut",
    [
        (Limit.MAX_NET, Limit.MAX_GROSS),
        (Limit.MAX_GROSS, Limit.MAX_GROSS),
        (Limit.LOSS_STOP,),
        (Limit.MAX_POSITION, Limit.MAX_ORDER_QTY),
    ],
)
def test_a_sizing_names_the_three_limits_in_order_and_once(cut) -> None:
    with pytest.raises(ValueError, match="cut names"):
        Sizing("chan", {USO: d(1)}, cut)
    Sizing("chan", {}, (Limit.MAX_POSITION, Limit.MAX_GROSS, Limit.MAX_NET))


# The stage


def table_of(*strategies: StrategyConfig) -> Table:
    return Table(
        stages=(
            DATA_ROW,
            RECOVER_ROW,
            RISK_ROW,
            *alpha_rows(strategies),
            *cost_rows(strategies),
            portfolio_row(strategies),
        ),
        strategies=strategies,
    )


ONE = table_of(CHAN)
FREE = CostTerm(CostKind.CHARGE, CostSide.BOTH, d(0), d(0), d(0), "placeholder: a test")
SCHEDULE = CostSchedule({GLD: (FREE,), USO: (FREE,)})
LIMITS = constraints(max_gross=d(900))
HELD = book({"chan": {USO: 3, GLD: -1}})
# 900 ÷ 1.25 ÷ 90 = 8 baskets.
TARGET = TargetPortfolio(DECISION, "chan", {GLD: d(-3), USO: d(8)})
SIZED = Sizing("chan", {GLD: d("-4.000000000"), USO: d("10.000000000")}, (Limit.MAX_GROSS,))

ALPHA, COST = "alpha/chan", "cost/chan"
# Each input in the row's order, with its files as the stage reads them.
Written = dict[str, dict[str, tuple[type, tuple]]]
INPUTS: Written = {
    "data": {SNAPSHOT_OUTPUT: (Snapshot, (MARKED,))},
    RECOVER_STAGE: {
        BOOK_OUTPUT: (Book, (HELD,)),
        VALUATION_OUTPUT: (Valuation, (V,)),
        CAPS_OUTPUT: (HardCaps, (CAPS,)),
    },
    RISK_STAGE: {CONSTRAINTS_OUTPUT: (Constraints, (LIMITS,))},
    ALPHA: {FORECAST_OUTPUT: (Forecast, (forecast(),))},
    COST: {SCHEDULE_OUTPUT: (CostSchedule, (SCHEDULE,))},
}


def key_of(stage: str, name: str | None = None) -> str:
    if name is None:
        return result_key(DEPLOYMENT, RUN, DECISION, stage)
    return output_key(DEPLOYMENT, RUN, DECISION, stage, name)


FILES = [(stage, name) for stage, files in INPUTS.items() for name in files]
RESULTS = [key_of(stage) for stage in INPUTS]
# Each key a decision reads, in the order it reads them.
ORDER = [*RESULTS, *(key_of(stage, name) for stage, name in FILES)]
RESULT = key_of(PORTFOLIO_STAGE)
STORED = {name: key_of(PORTFOLIO_STAGE, name) for name in (TARGET_OUTPUT, SIZING_OUTPUT)}


def with_file(**changes: object) -> Written:
    """The inputs with these files' one entry another, by the file's name."""
    return {
        stage: {
            name: (kind, (changes[name],)) if name in changes else (kind, entries)
            for name, (kind, entries) in files.items()
        }
        for stage, files in INPUTS.items()
    }


def write(
    storage: Storage,
    inputs: Written = INPUTS,
    table: Table = ONE,
    halts: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Store each input's files and result, and return each blob's key and SHA-256."""
    wrote: dict[str, str] = {}
    for stage, files in inputs.items():
        halt = (halts or {}).get(stage)
        wrote |= write_stage(storage, stage, {} if halt else files, table, halt)
    return wrote


def handler(storage: Storage, table: Table = ONE) -> PortfolioStage:
    return portfolio_handler(
        storage=storage, deployment=DEPLOYMENT, run=RUN, release=RELEASE, table=table
    )


def run(stage: PortfolioStage, report: Callable[[], None] = lambda: None) -> DecisionResult:
    return stage(Item(DECISION, PORTFOLIO_STAGE), report)


def own_header(inputs: Mapping[str, str], table: Table = ONE) -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        stage="portfolio",
        kind="deterministic",
        decision_at=DECISION,
        inputs=inputs,
        release=RELEASE,
        config_hashes=table.hashes_for(PORTFOLIO_STAGE),
    )


def stored(root: Path) -> tuple[tuple[TargetPortfolio, ...], tuple[Sizing, ...]]:
    return (
        decode_output((root / STORED[TARGET_OUTPUT]).read_bytes(), TargetPortfolio).entries,
        decode_output((root / STORED[SIZING_OUTPUT]).read_bytes(), Sizing).entries,
    )


def test_7_the_stage_stores_the_target_and_the_record_a_hand_calculation_gives(tmp_path) -> None:
    storage = store(tmp_path)
    inputs = write(storage)
    result = run(handler(storage))
    assert result.halt is None
    assert [ref.key for ref in result.outputs] == [STORED[TARGET_OUTPUT], STORED[SIZING_OUTPUT]]
    target = decode_output((tmp_path / STORED[TARGET_OUTPUT]).read_bytes(), TargetPortfolio)
    record = decode_output((tmp_path / STORED[SIZING_OUTPUT]).read_bytes(), Sizing)
    assert target == OutputFile(own_header(inputs), (TARGET,), False)
    assert record == OutputFile(own_header(inputs), (SIZED,), False)
    assert result.header == own_header(inputs)
    assert set(inputs) == set(ORDER)
    assert STORED[TARGET_OUTPUT].endswith("/20261103T204000Z/portfolio/target.json")
    assert STORED[SIZING_OUTPUT].endswith("/20261103T204000Z/portfolio/sizing.json")


def test_7_a_flat_forecast_stores_an_explicit_zero_for_every_leg(tmp_path) -> None:
    storage = store(tmp_path)
    write(storage, with_file(forecast=forecast(0)))
    assert run(handler(storage)).halt is None
    data = (tmp_path / STORED[TARGET_OUTPUT]).read_bytes()
    assert b'"shares":{"GLD":"0","USO":"0"}' in data
    assert stored(tmp_path)[1] == (Sizing("chan", {GLD: d("0E-9"), USO: d("0E-9")}, ()),)


def test_7_a_second_run_stores_the_same_bytes_and_the_same_result(tmp_path) -> None:
    storage = store(tmp_path)
    write(storage)
    first = run(handler(storage))
    kept = blobs(tmp_path)
    # Storage halts a deterministic stage whose second run writes other bytes.
    assert run(handler(store(tmp_path))) == first
    assert blobs(tmp_path) == kept
    # A crash between the two files leaves the first, which a second attempt stores again.
    (tmp_path / STORED[SIZING_OUTPUT]).unlink()
    assert run(handler(store(tmp_path))) == first
    assert blobs(tmp_path) == kept


def test_7_mappings_in_another_order_in_memory_give_the_same_bytes() -> None:
    """Storage hands back sorted keys, so the function is asked directly."""
    turned = {
        "given": forecast(legs=dict(reversed(LEGS.items()))),
        "marked": snapshot(dict(reversed(MARKS.items()))),
        "holds": book({"chan": {USO: 3, GLD: -1}}),
        "limits": constraints(max_gross=d(900), max_position={USO: LOOSE, GLD: LOOSE}),
        "owners": replace(CAPS, max_position={USO: LOOSE, GLD: LOOSE}),
    }
    for one, other in zip(sized(limits=LIMITS), sized(**turned), strict=True):  # type: ignore[arg-type]
        assert encode(one) == encode(other)
        for name in ("shares", "wanted"):
            if hasattr(one, name):
                assert list(getattr(other, name)) == [GLD, USO]


def test_7_blobs_built_from_mappings_in_another_order_store_the_same_bytes(tmp_path) -> None:
    def turned(mapping: Mapping) -> dict:
        return dict(reversed(mapping.items()))

    backward = with_file(
        snapshot=snapshot(turned(MARKS)),
        book=book({"chan": turned({USO: 3, GLD: -1})}),
        caps=replace(
            CAPS,
            max_order_qty=turned(CAPS.max_order_qty),
            max_position=turned(CAPS.max_position),
        ),
        constraints=replace(LIMITS, max_position=turned(LIMITS.max_position)),
        forecast=forecast(legs=turned(LEGS)),
    )
    made = []
    for name, inputs in (("one", INPUTS), ("other", backward)):
        storage = store(tmp_path / name)
        write(storage, inputs)
        assert run(handler(storage)).halt is None
        made.append({key: (tmp_path / name / key).read_bytes() for key in STORED.values()})
    assert made[0] == made[1]


@pytest.mark.parametrize("direction", [1, 0, -1])
def test_2_the_stored_body_is_the_same_whatever_the_book_holds(tmp_path, direction) -> None:
    bodies = []
    for name, holds in BOOKS.items():
        storage = store(tmp_path / name)
        write(storage, with_file(book=holds, forecast=forecast(direction)))
        assert run(handler(storage)).halt is None
        bodies.append(stored(tmp_path / name))
    assert all(body == bodies[0] for body in bodies)


def test_the_stage_reports_after_each_read_and_each_write(tmp_path) -> None:
    storage = store(tmp_path)
    write(storage)
    seen: list[int] = []
    run(
        handler(storage), lambda: seen.append(sum((tmp_path / k).exists() for k in STORED.values()))
    )
    # Five results and seven files, then each file it stores.
    assert seen == [0] * 12 + [1, 2]


def test_the_stage_reads_its_inputs_in_order_and_nothing_else(tmp_path) -> None:
    write(store(tmp_path))
    read: list[str] = []

    class Watched(FolderStore):
        def get(self, key: str) -> bytes:
            read.append(key)
            return super().get(key)

    assert run(handler(storage_over(Watched(tmp_path)))).halt is None
    assert read == ORDER
    assert not any("accounts/" in key for key in read)


def test_the_stage_reads_each_strategys_forecast_and_then_its_schedule(tmp_path) -> None:
    write(store(tmp_path), PAIR, TWO)
    read: list[str] = []

    class Watched(FolderStore):
        def get(self, key: str) -> bytes:
            read.append(key)
            return super().get(key)

    assert run(handler(storage_over(Watched(tmp_path)), TWO)).halt is None
    files = [
        key_of(f"{component}/{name}", output)
        for name in ("chan", "reversal")
        for component, output in (("alpha", FORECAST_OUTPUT), ("cost", SCHEDULE_OUTPUT))
    ]
    assert read[: len(PAIR)] == [key_of(stage) for stage in TWO.stage("portfolio").inputs]
    assert read[-4:] == files


def test_a_second_decision_of_one_handler_names_only_its_own_inputs(tmp_path) -> None:
    storage = store(tmp_path)
    write(storage)
    stage = handler(storage)
    assert run(stage).halt is None
    second = stage(Item(DECISION + timedelta(minutes=1), PORTFOLIO_STAGE), lambda: None)
    assert second.halt is not None and dict(second.header.inputs) == {}


# Acceptance 5 and 7, on the stage: each halt, with what its header names


def own_halt(
    root: Path, storage: Storage, inputs: Mapping[str, str], table: Table = ONE
) -> tuple[str, DecisionResult]:
    """The reason the stage halts on its own, leaving no blob, and naming these blobs."""
    kept = blobs(root)
    result = run(handler(storage, table))
    assert result.halt is not None and result.outputs == ()
    # Nothing upstream halted, so the halt names no cause, and monitoring alarms on it.
    assert result.halt.cause is None
    assert result.header == own_header(inputs, table)
    assert blobs(root) == kept
    return result.halt.reason, result


def up_to(wrote: Mapping[str, str], key: str, *, read: bool) -> dict[str, str]:
    """What the header names where the read of `key` halted, with `key` itself where it was read."""
    return {k: wrote[k] for k in ORDER[: ORDER.index(key) + read]}


@pytest.mark.parametrize("stage", INPUTS)
def test_5_a_result_that_is_not_stored_halts(tmp_path, stage) -> None:
    storage = store(tmp_path)
    wrote = write(storage, {s: files for s, files in INPUTS.items() if s != stage})
    named = up_to(wrote, key_of(stage), read=False)
    reason, _ = own_halt(tmp_path, storage, named)
    assert reason == f"{key_of(stage)} is not stored, and stage portfolio reads it"


@pytest.mark.parametrize("stage", INPUTS)
def test_5_a_result_storage_refuses_halts_and_is_not_named(tmp_path, stage) -> None:
    storage = store(tmp_path)
    wrote = write(storage)
    (tmp_path / key_of(stage)).write_bytes(b"{}")
    reason, _ = own_halt(tmp_path, storage, up_to(wrote, key_of(stage), read=False))
    assert reason.startswith(f"{key_of(stage)} holds no decision result the codec reads")


def passed_on(root: Path, storage: Storage, stage: str, wrote: Mapping[str, str]) -> None:
    kept = blobs(root)
    result = run(handler(storage))
    key = key_of(stage)
    assert result.halt is not None and result.halt.cause == BlobRef(key, wrote[key])
    assert result.halt.reason == f"{key} holds a halt: why"
    assert result.outputs == ()
    # Every result read by then, and no file.
    assert result.header == own_header({k: wrote[k] for k in RESULTS[: RESULTS.index(key) + 1]})
    assert blobs(root) == kept


@pytest.mark.parametrize("stage", INPUTS)
def test_5_a_halt_in_any_input_is_passed_on_naming_it(tmp_path, stage) -> None:
    storage = store(tmp_path)
    passed_on(tmp_path, storage, stage, write(storage, halts={stage: "why"}))


@pytest.mark.parametrize(
    ("first", "second"),
    [(a, b) for n, a in enumerate(INPUTS) for b in list(INPUTS)[n + 1 :]],
)
def test_5_of_two_halts_the_first_in_the_rows_order_is_passed_on(tmp_path, first, second) -> None:
    storage = store(tmp_path)
    passed_on(tmp_path, storage, first, write(storage, halts={first: "why", second: "other"}))


@pytest.mark.parametrize(("stage", "name"), FILES)
def test_5_a_halt_is_passed_on_before_another_inputs_file_is_opened(tmp_path, stage, name) -> None:
    halted = COST if stage != COST else ALPHA
    storage = store(tmp_path)
    wrote = write(storage, halts={halted: "why"})
    (tmp_path / key_of(stage, name)).write_bytes(b"corrupt")
    passed_on(tmp_path, storage, halted, wrote)


OLDER = strategy(text(901))


@pytest.mark.parametrize("stage", INPUTS)
def test_5_a_result_of_another_configuration_halts(tmp_path, stage) -> None:
    """A backtest rerun into one folder, after G moved, leaves the older run's blobs behind."""
    storage = store(tmp_path)
    wrote: dict[str, str] = {}
    for one, files in INPUTS.items():
        wrote |= write_stage(storage, one, files, table_of(OLDER) if one == stage else ONE)
    named = up_to(wrote, key_of(stage), read=True)
    reason, _ = own_halt(tmp_path, storage, named)
    assert reason == (
        f"{key_of(stage)} does not carry the configuration of chan that stage portfolio runs"
    )


def test_5_a_result_of_another_set_of_strategies_halts(tmp_path) -> None:
    storage = store(tmp_path)
    table = table_of(CHAN, strategy(name="reversal"))
    wrote = write(storage, {"data": INPUTS["data"]}, table)
    reason, _ = own_halt(tmp_path, storage, {key_of("data"): wrote[key_of("data")]})
    assert reason.startswith(f"{key_of('data')} does not carry the configuration of reversal that")


def test_5_a_result_that_lacks_a_strategys_configuration_halts(tmp_path) -> None:
    """Data's result of a run before the deployment gained a strategy."""
    storage = store(tmp_path)
    wrote = write_stage(storage, "data", PAIR["data"], ONE)
    reason, _ = own_halt(tmp_path, storage, {key_of("data"): wrote[key_of("data")]}, TWO)
    assert reason.startswith(f"{key_of('data')} does not carry the configuration of reversal that")


def test_5_the_configuration_is_checked_result_by_result(tmp_path) -> None:
    # An older configuration in an earlier result decides over a later halt, and a halt
    # in one result is passed on whatever configuration that result carries.
    storage = store(tmp_path / "older-first")
    wrote = write_stage(storage, "data", INPUTS["data"], table_of(OLDER))
    write_stage(storage, RECOVER_STAGE, {}, ONE, "why")
    reason, _ = own_halt(tmp_path / "older-first", storage, {key_of("data"): wrote[key_of("data")]})
    assert "does not carry the configuration of chan" in reason
    storage = store(tmp_path / "halted")
    wrote = write_stage(storage, "data", {}, table_of(OLDER), "why")
    passed_on(tmp_path / "halted", storage, "data", wrote)


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize(("stage", "name"), FILES)
def test_5_a_result_that_lists_a_file_other_than_once_halts(tmp_path, stage, name, count) -> None:
    storage = store(tmp_path)
    wrote = write(storage, {s: f for s, f in INPUTS.items() if s != stage})
    made = replace(
        own_header({}, ONE),
        stage=stage,
        kind="capturing",
        config_hashes=ONE.hashes_for(stage),
    )
    refs = []
    for one, (kind, entries) in INPUTS[stage].items():
        data = encode_output(kind, OutputFile(made, entries, False))
        ref = storage.put(StageKind.CAPTURING, key_of(stage, one), data).ref
        wrote[ref.key] = ref.sha256
        refs += [ref] * (count if one == name else 1)
    result = DecisionResult(made, tuple(refs), None)
    storage.put_result(key_of(stage), result)
    wrote[key_of(stage)] = sha256_of(encode(result))
    named = up_to(wrote, key_of(stage, name), read=False)
    reason, _ = own_halt(tmp_path, storage, named)
    assert reason == f"{key_of(stage)} lists {key_of(stage, name)} {count} times, not once"


@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize(("stage", "name"), [f for f in FILES if f[0] != RISK_STAGE])
def test_5_a_file_of_other_than_one_entry_halts(tmp_path, stage, name, count) -> None:
    """One of no entries carries the "trade nothing" mark, which halts as the rest do."""
    kind, entries = INPUTS[stage][name]
    inputs = {s: dict(f) for s, f in INPUTS.items()}
    inputs[stage][name] = (kind, entries * count)
    storage = store(tmp_path)
    wrote = write(storage, inputs)
    reason, _ = own_halt(tmp_path, storage, up_to(wrote, key_of(stage, name), read=True))
    assert reason == f"{key_of(stage, name)} holds {count} entries, not one"


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        ((), f"{key_of(RISK_STAGE, CONSTRAINTS_OUTPUT)} holds no entries"),
        ((LIMITS, LIMITS), "risk's constraints are of chan, chan, not of chan once each"),
        (
            (replace(LIMITS, strategy="reversal"),),
            "risk's constraints are of reversal, not of chan once each",
        ),
        (
            (LIMITS, replace(LIMITS, strategy="reversal")),
            "risk's constraints are of chan, reversal, not of chan once each",
        ),
    ],
    ids=["none", "twice", "anothers", "one-more"],
)
def test_5_constraints_of_other_strategies_than_the_deployments_halt(
    tmp_path, entries, reason
) -> None:
    storage = store(tmp_path)
    inputs = {**INPUTS, RISK_STAGE: {CONSTRAINTS_OUTPUT: (Constraints, entries)}}
    wrote = write(storage, inputs)
    key = key_of(RISK_STAGE, CONSTRAINTS_OUTPUT)
    assert own_halt(tmp_path, storage, up_to(wrote, key, read=True))[0] == reason


@pytest.mark.parametrize(("stage", "name"), FILES)
def test_5_a_file_that_is_missing_or_of_another_run_halts(tmp_path, stage, name) -> None:
    key = key_of(stage, name)
    storage = store(tmp_path / "missing")
    wrote = write(storage)
    (tmp_path / "missing" / key).unlink()
    named = up_to(wrote, key, read=False)
    reason, _ = own_halt(tmp_path / "missing", storage, named)
    assert reason == f"output file {key}, listed by {key_of(stage)}, is missing"
    # The bytes are another file's than the ones the result names.
    storage = store(tmp_path / "other")
    wrote = write(storage)
    (tmp_path / "other" / key).write_bytes(b"{}")
    reason, _ = own_halt(tmp_path / "other", storage, up_to(wrote, key, read=False))
    assert key in reason


@pytest.mark.parametrize(("stage", "name"), FILES)
@pytest.mark.parametrize(
    "changes",
    [
        {"decision_at": DECISION - timedelta(minutes=1)},
        {"config_hashes": table_of(OLDER).hashes_for(PORTFOLIO_STAGE)},
    ],
    ids=["another-decision", "another-configuration"],
)
def test_7_a_file_whose_header_is_not_its_results_halts(tmp_path, stage, name, changes) -> None:
    storage = store(tmp_path)
    wrote = write(storage, {s: f for s, f in INPUTS.items() if s != stage})
    made = replace(
        own_header({}, ONE),
        stage=stage,
        kind="capturing",
        config_hashes=ONE.hashes_for(stage),
    )
    refs = []
    for one, (kind, entries) in INPUTS[stage].items():
        header = replace(made, **changes) if one == name else made
        data = encode_output(kind, OutputFile(header, entries, False))
        refs.append(storage.put(StageKind.CAPTURING, key_of(stage, one), data).ref)
        wrote[refs[-1].key] = refs[-1].sha256
    result = DecisionResult(made, tuple(refs), None)
    storage.put_result(key_of(stage), result)
    wrote[key_of(stage)] = sha256_of(encode(result))
    named = up_to(wrote, key_of(stage, name), read=False)
    assert key_of(stage, name) in own_halt(tmp_path, storage, named)[0]


def test_5_a_snapshot_of_another_decision_halts(tmp_path) -> None:
    storage = store(tmp_path)
    # The file's own header names this decision, so storage's check passes.
    older = replace(MARKED, decision_at=DECISION - timedelta(minutes=1))
    wrote = write(storage, with_file(snapshot=older))
    key = key_of("data", SNAPSHOT_OUTPUT)
    reason, _ = own_halt(tmp_path, storage, up_to(wrote, key, read=True))
    assert reason.startswith(f"{key} holds a snapshot of")


FAULTS: dict[str, tuple[dict[str, object], str]] = {
    "another-strategys-shares": (
        {"book": book({OTHER: {USO: 1}})},
        "under 'other', which the deployment does not run",
    ),
    "another-strategys-order": (
        {"book": book(opens=[order(1, USO, OTHER)])},
        "under 'other', which the deployment does not run",
    ),
    "a-forecast-of-another": (
        {"forecast": forecast(name="reversal")},
        "chan's forecast is of strategy 'reversal'",
    ),
    "a-basket-of-one-leg": (
        {"forecast": forecast(legs={USO: 1})},
        "chan's basket and the instruments it trades do not both name GLD",
    ),
    "a-held-instrument-unnamed": (
        {"book": book({"chan": {SPY: 1}})},
        "chan holds SPY, or an open order in it, and its forecast does not name it",
    ),
    "an-ordered-instrument-unnamed": (
        {"book": book(opens=[order(1, SPY, "chan")])},
        "chan holds SPY, or an open order in it, and its forecast does not name it",
    ),
    "a-basket-of-nothing": (
        {"forecast": forecast(legs={USO: 0, GLD: 0})},
        "every leg of chan's basket is zero",
    ),
    "an-older-v": (
        {"valuation": valuation(session=TABLE.before(PRIOR).day)},
        "the valuation is of 2026-10-30, not of 2026-11-02",
    ),
    "an-old-stress-move": (
        {"caps": replace(CAPS, stress_max_age_days=3)},
        "stress_through is 2026-10-30, 4 days before the decision",
    ),
    "a-limit-missing": (
        {"constraints": replace(LIMITS, max_order_qty={GLD: LOOSE}, max_position={GLD: LOOSE})},
        "no limit names USO, a leg of chan's basket",
    ),
    **{
        f"a-step-of-{step}": (
            {"caps": replace(CAPS, share_step=d(step))},
            f"the hard caps' share_step is {shown}, not written as 1, 0.1",
        )
        for step, shown in (
            ("0", "0"),
            ("-1", "-1"),
            ("0.3", "0.3"),
            ("10", "10"),
            ("0.0000000001", "1E-10"),
            ("1.0", "1.0"),
            ("0.10", "0.10"),
        )
    },
    "no-mark": (
        {"snapshot": snapshot(MARKS, day=PRIOR)},
        "GLD has no mark: its newest bar is for 2026-11-02, not later than 2026-11-02",
    ),
}


@DIRECTIONS
@pytest.mark.parametrize("fault", FAULTS)
def test_5_each_disagreement_halts_the_stage_naming_every_input(tmp_path, fault, direction) -> None:
    changes, reason = FAULTS[fault]
    given = replace(changes.get("forecast", forecast()), direction=direction)
    storage = store(tmp_path)
    wrote = write(storage, with_file(**changes | {"forecast": given}))
    assert reason in own_halt(tmp_path, storage, wrote)[0]


@DIRECTIONS
def test_5_a_strategy_with_no_section_halts_the_stage_and_not_the_workers_start(
    tmp_path, direction
) -> None:
    table = table_of(NO_SECTION)
    storage = store(tmp_path)
    wrote = write(storage, with_file(forecast=forecast(direction)), table)
    reason, _ = own_halt(tmp_path, storage, wrote, table)
    assert reason == (
        "strategy chan's configuration chan.toml has no [portfolio] section, or an empty one"
    )


@pytest.mark.parametrize("direction", [1, -1])
def test_5_a_held_direction_sized_to_nothing_halts_the_stage(tmp_path, direction) -> None:
    storage = store(tmp_path)
    wrote = write(
        storage,
        with_file(
            forecast=forecast(direction), constraints=replace(LIMITS, max_net=d(1), max_gross=d(1))
        ),
    )
    reason, _ = own_halt(tmp_path, storage, wrote)
    assert reason == (
        f"chan forecasts {direction} and its target is zero shares in every leg, at a gross of 900 "
        "cut by max_gross and max_net"
    )


@pytest.mark.parametrize("name", STORED)
def test_5_a_file_already_stored_with_other_bytes_halts(tmp_path, name) -> None:
    storage = store(tmp_path)
    wrote = write(storage)
    storage.put(StageKind.DETERMINISTIC, STORED[name], b"{}")
    result = run(handler(storage))
    assert "so the step is not deterministic" in result.halt.reason
    assert (result.halt.cause, result.outputs) == (None, ())
    assert result.header == own_header(wrote)
    # Both are computed before either is stored, and the targets go first.
    assert (tmp_path / STORED[SIZING_OUTPUT]).exists() == (name == SIZING_OUTPUT)
    assert (tmp_path / STORED[TARGET_OUTPUT]).exists()


# Two strategies, which risk's stage halts on, so this shows the files' form alone

SECOND = strategy(text(800, '["SPY", "USO"]'), name="reversal")
# Declared out of order, to show the stage sorts them by name.
TWO = table_of(SECOND, CHAN)
PAIR: Written = {
    **with_file(snapshot=snapshot({**MARKS, SPY: 20}), constraints=LIMITS, caps=CAPS),
    RECOVER_STAGE: {
        **INPUTS[RECOVER_STAGE],
        BOOK_OUTPUT: (Book, (book({"reversal": {USO: 2, SPY: -5}, "chan": {USO: 3}}),)),
        CAPS_OUTPUT: (
            HardCaps,
            (
                replace(
                    CAPS,
                    max_order_qty=dict.fromkeys((GLD, SPY, USO), LOOSE),
                    max_position=dict.fromkeys((GLD, SPY, USO), LOOSE),
                ),
            ),
        ),
    },
    RISK_STAGE: {
        CONSTRAINTS_OUTPUT: (
            Constraints,
            (
                constraints(
                    strategy="reversal",
                    max_order_qty={SPY: LOOSE, USO: LOOSE},
                    max_position={SPY: LOOSE, USO: LOOSE},
                ),
                LIMITS,
            ),
        )
    },
    "alpha/reversal": {
        FORECAST_OUTPUT: (Forecast, (forecast(-1, {SPY: 1, USO: "-0.4"}, "reversal"),))
    },
    "cost/reversal": {SCHEDULE_OUTPUT: (CostSchedule, (SCHEDULE,))},
}
# Two legs of 20 dollars each, 40 in all: 800 ÷ 40 = 20 baskets.
OF_SECOND = (
    TargetPortfolio(DECISION, "reversal", {SPY: d(-20), USO: d(8)}),
    Sizing("reversal", {SPY: d("-20.000000000"), USO: d("8.000000000")}, ()),
)


def test_7_two_strategies_each_get_one_target_sized_as_if_alone(tmp_path) -> None:
    storage = store(tmp_path)
    wrote = write(storage, PAIR, TWO)
    result = run(handler(storage, TWO))
    assert result.halt is None
    assert stored(tmp_path) == ((TARGET, OF_SECOND[0]), (SIZED, OF_SECOND[1]))
    assert result.header == own_header(wrote, TWO)
    assert set(result.header.config_hashes) == {"chan", "reversal"}


def test_7_one_strategys_halt_stores_nothing_for_the_other(tmp_path) -> None:
    storage = store(tmp_path)
    alpha = {FORECAST_OUTPUT: (Forecast, (forecast(0, {SPY: 0, USO: 0}, "reversal"),))}
    wrote = write(storage, {**PAIR, "alpha/reversal": alpha}, TWO)
    reason, _ = own_halt(tmp_path, storage, wrote, TWO)
    assert reason == "every leg of reversal's basket is zero"


def test_7_strategies_are_judged_one_at_a_time_by_name(tmp_path) -> None:
    """The first strategy's last check comes before the second's first."""
    bare = strategy('instruments = ["SPY", "USO"]\n', name="reversal")
    table = table_of(bare, CHAN)
    storage = store(tmp_path)
    other = PAIR[RISK_STAGE][CONSTRAINTS_OUTPUT][1][0]
    zero = {CONSTRAINTS_OUTPUT: (Constraints, (other, replace(LIMITS, max_gross=d(0))))}
    wrote = write(storage, {**PAIR, RISK_STAGE: zero}, table)
    reason, _ = own_halt(tmp_path, storage, wrote, table)
    assert reason.startswith("chan forecasts 1 and its target is zero shares")


# The entry point


def test_the_row_is_shared_and_reads_every_model_in_order() -> None:
    assert ONE.stage("portfolio") == Stage(
        "portfolio",
        StageKind.DETERMINISTIC,
        inputs=("data", "execution/recover", "risk", "alpha/chan", "cost/chan"),
        strategy=None,
    )
    assert TWO.stage("portfolio").inputs == (
        "data",
        "execution/recover",
        "risk",
        "alpha/chan",
        "cost/chan",
        "alpha/reversal",
        "cost/reversal",
    )
    heard = {r.key for r in routes(ONE, DEPLOYMENT) if r.reader == "portfolio"}
    assert heard == {
        f"runs/{DEPLOYMENT}/*/{stage}/result.json" for stage in ONE.stage("portfolio").inputs
    }
    assert dict(TWO.hashes_for("portfolio")) == {
        "chan": CHAN.config_hash,
        "reversal": SECOND.config_hash,
    }


def test_the_handler_takes_its_kind_and_its_inputs_from_the_table(tmp_path) -> None:
    row = ONE.stage("portfolio")
    turned = replace(row, kind=StageKind.CAPTURING, inputs=tuple(reversed(row.inputs)))
    table = Table(
        stages=tuple(turned if s.name == "portfolio" else s for s in ONE.stages),
        strategies=(CHAN,),
    )
    result = run(handler(store(tmp_path), table))
    assert result.halt.reason.startswith(f"{key_of(COST)} is not stored")
    assert result.header.kind == "capturing"


def test_11_the_portfolio_worker_loads_no_other_module_and_no_reader() -> None:
    code = (
        "import sys, ithildinruntime.runtime.portfolio_worker; "
        "print(sorted({'lake', 'pandas', 'pyarrow', 'numpy'} & set(sys.modules)), "
        "sorted(m for m in sys.modules if m.split('.')[:2] in "
        "[['ithildinruntime', w] for w in ('data', 'alpha', 'risk', 'cost', 'execution')]))"
    )
    ran = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert ran.stdout.strip() == "[] []"


def looped(tmp_path: Path, inputs: Written, table: Table, halts=None) -> tuple:
    """The portfolio stage's result from the receive loop, beside stand-ins for what it reads."""
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    alarms = Alarms()
    storage = folder_storage(folder.store(), alarms)
    # The loop finds these in its check as it starts.
    wrote = write(storage, inputs, table, halts)
    plan = Plan(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        mode=Mode.PAPER,
        release=RELEASE,
        decisions=(PlanDecision(DECISION, tuple(sorted(s.name for s in table.strategies))),),
    )
    loop = ReceiveLoop(
        worker="portfolio-1",
        component="portfolio",
        table=table,
        plan=plan,
        queue=folder.queue("portfolio"),
        storage=storage,
        bucket=folder.bucket,
        handlers={PORTFOLIO_STAGE: handler(storage, table)},
        alerts=alarms,
        timer=SystemTimer(),
        beat=lambda: None,
        tick=0.05,
        stop_timeout=5.0,
    )
    with running({"portfolio": loop}) as started:
        assert started.wait_for(lambda: (folder.root / RESULT).exists(), 20)
    result = storage.read_result(RESULT, DECISION)
    assert result is not None
    return folder.root, result, wrote


@pytest.mark.parametrize("both", [False, True], ids=["one", "two"])
def test_7_the_loop_stores_each_target_beside_stand_in_rows(tmp_path, both) -> None:
    inputs, table = (PAIR, TWO) if both else (INPUTS, ONE)
    root, result, wrote = looped(tmp_path, inputs, table)
    assert result.halt is None and result.header == own_header(wrote, table)
    second = OF_SECOND if both else ()
    assert stored(root) == ((TARGET, *second[:1]), (SIZED, *second[1:]))


@pytest.mark.parametrize("stage", INPUTS)
def test_7_the_loop_stores_each_halt_passed_on(tmp_path, stage) -> None:
    _, result, wrote = looped(tmp_path, INPUTS, ONE, {stage: "why"})
    assert result.halt.cause == BlobRef(key_of(stage), wrote[key_of(stage)])
