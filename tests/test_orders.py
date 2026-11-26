"""The order manager by hand: `plan_orders` and the `[execution]` section, for #146's acceptance 1.

Every figure is made up. Each case builds a book and targets and reads back the
plan, so a rule of the contract that moved shows here as a wrong order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    Halt,
    OrderType,
    Remainder,
    TargetPortfolio,
    TimeInForce,
    order_id,
    parse_strategy,
)
from ithildinruntime.execution.orders import MOST_ORDERS, order_type, plan_orders
from tests.support import gates
from tests.support.gates import GLD, USO, d

AT = datetime(2026, 11, 3, 21, tzinfo=UTC)
MOC = (OrderType.MARKET_ON_CLOSE, TimeInForce.CLS)
CHAN, PAIR = gates.CHAN, "pair"
ONE, NINE = Decimal(1), Decimal("1E-9")


def target(shares: dict, strategy: str = CHAN, at: datetime = AT) -> TargetPortfolio:
    return TargetPortfolio(at, strategy, {i: d(q) for i, q in shares.items()})


def plan(targets, book=None, step=ONE, types=None, off=()):
    return plan_orders(
        targets,
        book if book is not None else gates.book(),
        types=types or {CHAN: MOC},
        share_step=step,
        decision_at=AT,
        off=off,
    )


def legs(made) -> list[tuple[str, str, Decimal]]:
    return [(o.strategy, o.instrument, o.qty) for o in made.orders]


@pytest.mark.parametrize(
    ("held", "wanted", "order"),
    [
        (0, 10, 10),  # opening long
        (0, -10, -10),  # opening short
        (10, 0, -10),  # closing a long
        (-10, 0, 10),  # closing a short
        (10, 15, 5),  # adding to a long
        (-10, -4, 6),  # cutting a short
        (10, -5, -15),  # crossing zero from long to short
        (-5, 10, 15),  # crossing zero from short to long
    ],
    ids=[
        "open-long",
        "open-short",
        "close-long",
        "close-short",
        "add",
        "cut",
        "cross-down",
        "cross-up",
    ],
)
def test_one_order_closes_the_gap(held, wanted, order) -> None:
    book = gates.book({CHAN: {GLD: held, USO: 0}})
    made = plan([target({GLD: wanted, USO: 0})], book)
    assert legs(made) == [(CHAN, GLD, d(order))]
    assert made.remainders == ()


def test_a_flat_target_on_a_flat_book_plans_nothing() -> None:
    made = plan([target({GLD: 0, USO: 0})])
    assert made.orders == () and made.remainders == ()


@pytest.mark.parametrize(
    ("step", "held", "wanted", "order", "left"),
    [
        (ONE, "0", "10.7", "10", "0.7"),
        (ONE, "0", "-10.7", "-10", "-0.7"),
        (ONE, "2.5", "0", "-2", "-0.5"),  # a split's fraction: the whole part is sold
        (NINE, "0", "10.7", "10.7", None),
        (NINE, "0", "0.0000000014", "0.000000001", "0.0000000004"),
        (NINE, "0", "-0.0000000014", "-0.000000001", "-0.0000000004"),
    ],
)
def test_a_remainder_rounds_toward_zero_and_is_named(step, held, wanted, order, left) -> None:
    book = gates.book({CHAN: {GLD: d(held), USO: 0}})
    made = plan([target({GLD: wanted, USO: 0})], book, step=step)
    assert legs(made) == [(CHAN, GLD, d(order))]
    expected = () if left is None else (Remainder(CHAN, GLD, d(left)),)
    assert made.remainders == expected


@pytest.mark.parametrize(
    ("wanted", "orders"),
    [("4", ["4"]), ("4.000000001", ["4"]), ("3.999999999", ["3"])],
    ids=["on-a-step", "one-past", "one-inside"],
)
def test_a_target_on_a_step_and_one_step_either_side(wanted, orders) -> None:
    made = plan([target({GLD: wanted, USO: 0})], step=ONE)
    assert [o.qty for o in made.orders] == [d(q) for q in orders]


def test_a_gap_below_one_step_plans_no_order_and_names_its_remainder() -> None:
    made = plan([target({GLD: "0.4", USO: 0})], step=ONE)
    assert made.orders == () and made.remainders == (Remainder(CHAN, GLD, d("0.4")),)


@pytest.mark.parametrize(("held", "open_qty", "wanted"), [(4, 6, 10), (4, -6, -2)])
def test_the_open_remainder_counts_toward_the_gap(held, open_qty, wanted) -> None:
    opened = gates.order(open_qty, GLD)
    book = gates.book({CHAN: {GLD: held, USO: 0}}, opens=[opened])
    # What is held and what is open together come to the target, so nothing more is needed.
    assert plan([target({GLD: wanted, USO: 0})], book).orders == ()


@pytest.mark.parametrize("open_qty", [6, -6], ids=["buy-open", "sell-open"])
def test_an_open_order_under_a_gap_halts(open_qty) -> None:
    opened = gates.order(open_qty, GLD)
    book = gates.book({CHAN: {GLD: 0, USO: 0}}, opens=[opened])
    with pytest.raises(Halt, match="has an open order in GLD and needs .* #149's"):
        plan([target({GLD: 1, USO: 0})], book)


def test_two_strategies_sort_by_strategy_then_instrument_and_number_from_01() -> None:
    types = {CHAN: MOC, PAIR: MOC}
    made = plan(
        [target({USO: 3, GLD: -2}, PAIR), target({USO: 1, GLD: 2})],
        types=types,
    )
    assert legs(made) == [
        (CHAN, GLD, d(2)),
        (CHAN, USO, d(1)),
        (PAIR, GLD, d(-2)),
        (PAIR, USO, d(3)),
    ]
    assert [o.client_order_id for o in made.orders] == [
        order_id(s, AT, n) for n, (s, _, _) in enumerate(legs(made), start=1)
    ]
    assert {(o.type, o.time_in_force) for o in made.orders} == {MOC}


def test_a_strategy_that_is_off_plans_nothing_and_its_target_is_still_checked() -> None:
    types = {CHAN: MOC, PAIR: MOC}
    made = plan([target({GLD: 2, USO: 0}), target({GLD: 5}, PAIR)], types=types, off={PAIR})
    assert legs(made) == [(CHAN, GLD, d(2))]
    with pytest.raises(Halt, match="hold none of pair"):
        plan([target({GLD: 2, USO: 0})], types=types, off={PAIR})


@pytest.mark.parametrize(
    ("targets", "said"),
    [
        ([], "hold none of chan"),
        ([target({GLD: 1}), target({GLD: 2})], "hold two of chan"),
        ([target({GLD: 1}, at=datetime(2026, 11, 2, 21, tzinfo=UTC))], "is of the decision at"),
        ([target({GLD: 1}), target({GLD: 1}, PAIR)], "pair, a strategy of no deployment"),
    ],
    ids=["none", "two", "another-decision", "another-strategy"],
)
def test_a_target_file_that_is_not_one_target_per_strategy_halts(targets, said) -> None:
    with pytest.raises(Halt, match=said):
        plan(targets)


@pytest.mark.parametrize(
    ("held", "opens"),
    [({GLD: 0, USO: 4}, []), ({GLD: 0}, [gates.order(2, USO)])],
    ids=["held", "open-order"],
)
def test_a_target_that_leaves_out_a_held_or_ordered_leg_halts(held, opens) -> None:
    book = gates.book({CHAN: held}, opens=opens)
    with pytest.raises(Halt, match="leaves out USO, .* never reads as zero"):
        plan([target({GLD: 1})], book)


def test_a_leg_held_at_zero_may_be_left_out() -> None:
    book = gates.book({CHAN: {GLD: 0, USO: 0}})
    assert legs(plan([target({GLD: 1})], book)) == [(CHAN, GLD, d(1))]


@pytest.mark.parametrize("count", [MOST_ORDERS - 1, MOST_ORDERS, MOST_ORDERS + 1])
def test_a_plan_of_more_than_99_orders_halts(count) -> None:
    names = [f"s{n:03d}" for n in range(count)]
    types = dict.fromkeys(names, MOC)
    targets = [target({GLD: 1}, name) for name in names]
    if count > MOST_ORDERS:
        with pytest.raises(Halt, match=f"holds {count} orders, past the 99"):
            plan(targets, types=types)
    else:
        assert len(plan(targets, types=types).orders) == count


def test_a_share_step_at_or_below_zero_halts() -> None:
    with pytest.raises(Halt, match="share step is 0"):
        plan([target({GLD: 1})], step=Decimal(0))


# The `[execution]` section


def config(execution: str | None):
    text = 'instruments = ["GLD", "USO"]\n'
    if execution is not None:
        text += f"[execution]\n{execution}\n"
    return parse_strategy(CHAN, text.encode())


def test_market_on_close_is_the_one_order_a_strategy_names() -> None:
    assert order_type(config('order = "market_on_close"')) == MOC


@pytest.mark.parametrize(
    ("section", "said"),
    [
        (None, r"has no \[execution\] section"),
        ("", r"has no \[execution\] section"),
        ('order = "market"', "only market_on_close is sent until #155"),
        ('order = "limit"', "names the order 'limit'"),
        ("order = 1", "names the order 1"),
        ('order = "market_on_close"\nstyle = "x"', r"holds style in \[execution\]"),
        ('kind = "market_on_close"', r"holds kind in \[execution\]"),
    ],
    ids=["absent", "empty", "market", "unknown", "not-text", "another-key", "only-another-key"],
)
def test_an_execution_section_it_cannot_read_halts_naming_the_strategy(section, said) -> None:
    with pytest.raises(Halt, match=said) as raised:
        order_type(config(section))
    assert "strategy chan" in raised.value.reason
