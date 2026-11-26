"""The send stage's remaining cases of #146's acceptance, which #195 holds.

Every figure is made up. Each case runs the real send stage after the real recover in
`tests/support/send_world.py`, and reads what the journal and the simulated broker hold
afterwards. The cases sit apart from `tests/test_send.py` so the two files can change
without touching each other.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    DATA_STAGE,
    SEND_STAGE,
    SNAPSHOT_OUTPUT,
    Basket,
    DecisionResult,
    Forecast,
    Halt,
    HaltRecord,
    Limit,
    StrategyEnd,
    decode,
    encode,
    off_switch_key,
    order_id,
    output_key,
    result_key,
)
from ithildinruntime.execution.brokers.faults import Faults, OrderFault
from ithildinruntime.execution.inputs import read_inputs
from ithildinruntime.execution.order_path import OrderPath
from ithildinruntime.execution.send import OUTCOME_OUTPUT
from ithildinruntime.journal import Journal, VerdictEntry, record_of
from ithildinruntime.portfolio.sizing import size
from tests.support import gates
from tests.support.send_world import CHAN, EXECUTION, CheckedBroker, SendWorld, strategy
from tests.support.sim_world import ACCOUNT, DEPLOYMENT, FULL, GLD, USO, after, close_of

DAYS = [FULL, after(FULL), after(FULL, 2), after(FULL, 3), after(FULL, 4)]
CLOSES = {GLD: {day: 100 + n for n, day in enumerate(DAYS)}, USO: {day: 20 for day in DAYS}}
PAIR = {CHAN: {GLD: 3, USO: -5}}
TWO = "two"


def world(**changes) -> SendWorld:
    return SendWorld(CLOSES, **changes)


def sent(made: SendWorld, day: date, targets=None):
    made.ready(close_of(day), targets if targets is not None else PAIR)
    return made.send_stage()


def planned(made: SendWorld, count: int = 2, strategies=(CHAN, CHAN, TWO)) -> list[str]:
    """The ids `plan_orders` gives the newest decision's orders, numbered across the plan."""
    return [order_id(strategies[n], made.at, n + 1) for n in range(count)]


def verdicts(made: SendWorld) -> dict[str, list]:
    """Each order's journaled verdicts, oldest first."""
    found: dict[str, list] = {}
    for entry in Journal(made.store, ACCOUNT).read():  # type: ignore[arg-type]
        record = record_of(entry)
        if isinstance(record, VerdictEntry):
            found.setdefault(record.client_order_id, []).append(record.verdict)
    return found


# 1. The gate's answers through `ask`


@pytest.mark.parametrize(
    ("held", "refused", "value"),
    [({GLD: 3, USO: -5}, 1, "303"), ({GLD: 1, USO: -10}, 0, "200")],
    ids=["larger-leg-first", "larger-leg-second"],
)
def test_an_exit_while_net_is_past_its_cap_widens_net_on_one_leg_and_sends_nothing(
    held, refused, value
) -> None:
    made = world()
    sent(made, FULL, {CHAN: held})
    made.constraints = gates.constraints(max_net=Decimal(150))
    made.ready(close_of(DAYS[1]), {CHAN: {GLD: 0, USO: 0}})
    exits = planned(made)
    result = made.send_attempt()
    assert result.halt is not None and "nothing is sent" in result.halt.reason
    assert f"the gate refused {exits[refused]} on max_net, at {value}." in result.halt.reason
    states = made.states()
    assert states[exits[refused]] == "Refused"
    assert {states[n] for n in exits if n != exits[refused]} == {"Abandoned"}
    # Each exit only reduces, and the leg the gate refused is refused on net alone.
    said = verdicts(made)
    assert said[exits[refused]][-1].adding is False
    assert said[exits[refused]][-1].limit is Limit.MAX_NET
    assert len(made.submits()) == 2
    assert made.rebuilt() == len([v for vs in said.values() for v in vs])


def _three(made: SendWorld) -> None:
    made.ready(close_of(FULL), {**PAIR, TWO: {GLD: 2, USO: 0}})


def _two_strategies(**limits) -> SendWorld:
    return world(
        strategies=(strategy(), strategy(TWO)),
        others=(gates.constraints(strategy=TWO, **limits),),
    )


def test_three_orders_whose_third_is_refused_send_nothing() -> None:
    one = {GLD: Decimal(1), USO: gates.LOOSE}
    made = _two_strategies(max_order_qty=one, max_position=one)
    _three(made)
    first, second, third = planned(made, 3)
    result = made.send_attempt()
    assert result.halt is not None and "nothing is sent" in result.halt.reason
    said = f"the gate refused {third} on max_order_qty, at 2.000000000 against its bound of 1.0"
    assert said in result.halt.reason
    assert made.submits() == []
    assert made.states() == {first: "Abandoned", second: "Abandoned", third: "Refused"}
    assert made.rebuilt() == 3


def test_a_halt_on_the_middle_of_three_orders_sends_nothing() -> None:
    made = _two_strategies()
    _three(made)
    first, second, third = planned(made, 3)
    # The middle order halts at the ask and again when `hold` journals the plan's answers.
    made.keeper.planted[second] = [Halt("the gate cannot read the middle leg")] * 2
    result = made.send_attempt()
    assert result.halt is not None and "the gate cannot read the middle leg" in result.halt.reason
    assert made.submits() == []
    assert made.states() == {first: "Abandoned", second: "Abandoned", third: "Abandoned"}
    # The ask stopped at the middle order, and so did `hold`, so the third was never asked.
    assert made.keeper.asked == [first, second, first, second]


def test_a_second_leg_on_the_same_side_breaks_net_only_once_the_first_counts() -> None:
    made = world()
    made.constraints = gates.constraints(max_net=Decimal(150))
    made.ready(close_of(FULL), {CHAN: {GLD: 1, USO: 5}})
    first, second = planned(made)
    result = made.send_attempt()
    assert result.halt is not None
    said = f"the gate refused {second} on max_net, at 200.000000000 against its bound of 150.0"
    assert said in result.halt.reason
    assert made.submits() == []
    assert made.states() == {first: "Abandoned", second: "Refused"}
    (alone,) = verdicts(made)[first]
    assert alone.approved and alone.past == ()
    assert made.rebuilt() == 2


def test_a_second_leg_past_its_market_on_close_cutoff_at_the_ask_sends_nothing() -> None:
    made = world()
    made.constraints = gates.constraints(moc_cutoff=10)
    close = close_of(DAYS[1])
    # The quotes are stamped 59 seconds before the decision, and serve for 90.
    made.ready(close - timedelta(minutes=10, seconds=20), PAIR)
    made.clock.at = made.at
    first, second = planned(made)
    check = made.keeper.check

    def later(intent, *args):
        verdict = check(intent, *args)
        # The first leg is checked before the cutoff of ten minutes, and the clock then
        # reaches one second past it, 599 seconds before the close.
        if intent.client_order_id == first:
            made.clock.at = close - timedelta(seconds=599)
        return verdict

    made.keeper.check = later  # type: ignore[method-assign]
    result = made.send_attempt()
    assert result.halt is not None and "nothing is sent" in result.halt.reason
    assert f"the gate refused {second} on moc_cutoff, at 9.983333333 against its bound of 10" in (
        result.halt.reason
    )
    assert made.submits() == []
    # `hold` asks about the first leg again past its cutoff, which ends it before any verdict,
    # and the second has no entry, so both end abandoned.
    assert made.states() == {first: "Abandoned", second: "Abandoned"}
    assert made.rebuilt() == len([v for vs in verdicts(made).values() for v in vs])


# 2. Hard limit 2 again, through the stage


def test_no_order_of_a_day_reaches_the_broker_but_as_its_journal_and_plan_say() -> None:
    made = world()

    def checked(at):
        return CheckedBroker(lambda: made.store.blobs, ACCOUNT, at, to=made.broker())

    for day, target in ((FULL, PAIR), (DAYS[1], {CHAN: {GLD: -2, USO: 4}})):
        made.ready(close_of(day), target)
        result = made.send_attempt(made.sender(brokers=checked))
        assert result.halt is None, result.halt.reason
    # Each order of both days reached the simulator once, through the checks. The fixture in
    # tests/conftest.py fails this test on any refusal.
    assert len(made.submits()) == 4 and len(set(made.submits())) == 4


# 7. The halt's words when a call raises


def test_a_send_that_raises_is_named_by_the_order_and_the_errors_kind(monkeypatch) -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = planned(made)
    real = OrderPath.send

    def send(self, name):
        if name == second:
            raise RuntimeError("the vendor's own words")
        return real(self, name)

    monkeypatch.setattr(OrderPath, "send", send)
    result = made.send_attempt()
    assert result.halt is not None
    assert result.halt.reason.startswith(f"the call for {second} raised RuntimeError. ")
    assert "the vendor's own words" not in result.halt.reason


# 3. Each side, from a forecast through portfolio construction to the simulator

SIDES_CLOSES = {
    GLD: dict(zip(DAYS, (100, 101, 102, 200, 200), strict=True)),
    USO: dict.fromkeys(DAYS, 20),
}
BASKET = {GLD: Decimal(3), USO: Decimal(-5)}
# One basket is $400 gross at the first close, so a gross of 400 sizes one there.
SIZED = strategy(execution=EXECUTION + "[portfolio]\ngross = 400\n")


def _sized(made: SendWorld, day: date, direction: int):
    """Store the decision's files, then its target as portfolio construction sizes it."""
    at = close_of(day)
    made.ready(at, {CHAN: {GLD: 0, USO: 0}})
    inputs = read_inputs(made.storage(), DEPLOYMENT, made.run, at, lambda: None)
    forecast = Forecast(CHAN, Basket(BASKET), direction, 1, Decimal(1))
    target, _ = size(
        SIZED,
        (CHAN,),
        forecast,
        inputs.snapshot,
        made.view().book,
        inputs.limits(CHAN),
        made.caps,
        inputs.valuation,
        gates.fitting(made.constraints, made.caps),
    )
    made.targets(at, {CHAN: dict(target.shares)})
    return made.send_stage()


# Each decision's forecast, the orders it plans with whether the gate reads each as adding,
# and the position and cash once they fill, all worked by hand from the closes above.
WALK = [
    (1, {GLD: ("3", True), USO: ("-5", True)}, {GLD: 3, USO: -5}, "999800"),
    (0, {GLD: ("-3", False), USO: ("5", False)}, {GLD: 0, USO: 0}, "1000003"),
    # 400 / (3 × 102 + 5 × 20) baskets, each leg rounded toward zero.
    (-1, {GLD: ("-2", True), USO: ("4", True)}, {GLD: -2, USO: 4}, "1000127"),
    # 400 / (3 × 200 + 5 × 20) baskets. Each leg crosses zero and ends smaller than it began.
    (1, {GLD: ("3", True), USO: ("-6", True)}, {GLD: 1, USO: -2}, "999647"),
]


def test_each_side_opens_closes_and_crosses_as_the_forecast_asks() -> None:
    made = SendWorld(SIDES_CLOSES, strategies=(SIZED,))
    for n, (direction, orders, held, cash) in enumerate(WALK):
        result = _sized(made, DAYS[n], direction)
        assert result.halt is None, result.halt.reason
        (entry,) = [e for at, e in _decided(made) if at == made.at]
        assert {o.instrument: str(o.qty) for o in entry.orders} == {
            i: q for i, (q, _) in orders.items()
        }
        said = verdicts(made)
        for order in entry.orders:
            (verdict,) = said[order.client_order_id]
            assert verdict.approved and verdict.adding is orders[order.instrument][1], order
        # The fills land at the next decision's recover.
        made.ready(close_of(DAYS[n + 1]), {CHAN: held})
        book = made.view().book
        assert {i: q for i, q in book.positions_by_strategy.get(CHAN, {}).items() if q} == {
            i: Decimal(q) for i, q in held.items() if q
        }
        assert book.cash == Decimal(cash), n
        made.agree()
    assert made.rebuilt() == 8


def _decided(made: SendWorld):
    view = made.view()
    return [(at, view.decisions[seq]) for at, seq in view.decided.items()]


# 4. A wrong price


def _priced(leg: str, factor: Decimal, day: date) -> dict:
    """The sides' closes, with one leg's close on `day` wrong by `factor`."""
    closes = {i: dict(by) for i, by in SIDES_CLOSES.items()}
    closes[leg][day] = closes[leg][day] * factor
    return closes


def _tried(made: SendWorld, day: date, direction: int):
    """The decision's target as portfolio construction sizes it, or its halt, then the stage."""
    at = close_of(day)
    made.ready(at, {CHAN: {GLD: 0, USO: 0}})
    inputs = read_inputs(made.storage(), DEPLOYMENT, made.run, at, lambda: None)
    forecast = Forecast(CHAN, Basket(BASKET), direction, 1, Decimal(1))
    try:
        target, sizing = size(
            SIZED,
            (CHAN,),
            forecast,
            inputs.snapshot,
            made.view().book,
            inputs.limits(CHAN),
            made.caps,
            inputs.valuation,
            gates.fitting(made.constraints, made.caps),
        )
    except Halt as halt:
        made.portfolio_halted(at, halt.reason)
        return made.send_stage(), None
    made.targets(at, {CHAN: dict(target.shares)})
    return made.send_stage(), sizing


# The true sizes, which the share caps are set to.
TRUE = {GLD: Decimal(3), USO: Decimal(5)}
WRONG = [Decimal(100), Decimal("0.01")]


@pytest.mark.parametrize("start", ["flat", "held"])
@pytest.mark.parametrize("factor", WRONG, ids=["100-times-high", "100-times-low"])
@pytest.mark.parametrize("leg", [GLD, USO])
def test_a_price_wrong_100_times_sends_no_order_past_a_share_cap(leg, factor, start) -> None:
    day = DAYS[0] if start == "flat" else DAYS[1]
    made = SendWorld(_priced(leg, factor, day), strategies=(SIZED,))
    made.constraints = gates.constraints(max_order_qty=TRUE, max_position=TRUE)
    if start == "held":
        result, _ = _tried(made, DAYS[0], 1)
        assert result.halt is None, result.halt.reason
        before = len(made.submits())
        assert before == 2
    else:
        before = 0
    result, sizing = _tried(made, day, 1)
    placed = made.submits()[before:]
    orders = made.view().orders
    for name in placed:
        assert orders[name].intent.qty.copy_abs() <= TRUE[orders[name].intent.instrument], name
    if factor > 1:
        # Every leg sizes to zero shares, which portfolio construction halts on, and the send
        # stage halts on its result by name.
        assert sizing is None and result.halt is not None
        assert "its target is zero shares in every leg" in result.halt.reason
        assert placed == []
    else:
        # A price 100 times low wants more baskets, and the share cap on the position cuts the
        # target back to the true size. That is the one case no refusal stops: from flat the
        # true orders go out, and from held no order is needed.
        assert sizing is not None and Limit.MAX_POSITION in sizing.cut
        assert result.halt is None, result.halt.reason
        assert len(placed) == (2 if start == "flat" else 0)


# 5. The trace's halt: a plan whose snapshot is not the one it names


def _snapshot_ref(made: SendWorld):
    key = output_key(DEPLOYMENT, made.run, made.at, DATA_STAGE, SNAPSHOT_OUTPUT)
    return key, result_key(DEPLOYMENT, made.run, made.at, DATA_STAGE)


def _missing(made: SendWorld) -> None:
    made.store.drop(_snapshot_ref(made)[0])


def _unlisted(made: SendWorld) -> None:
    _, key = _snapshot_ref(made)
    stored = decode(made.store.blobs[key], DecisionResult)
    made.store.put(key, encode(replace(stored, outputs=())))


def _halted(made: SendWorld) -> None:
    _, key = _snapshot_ref(made)
    stored = decode(made.store.blobs[key], DecisionResult)
    halt = HaltRecord("data stopped", None, (), (), ())
    made.store.put(key, encode(replace(stored, outputs=(), halt=halt)))


def _other_bytes(made: SendWorld) -> None:
    key, _ = _snapshot_ref(made)
    made.store.put(key, made.store.blobs[key] + b" ")


@pytest.mark.parametrize(
    ("spoil", "why"),
    [
        (_missing, "is missing"),
        (_unlisted, "0 times, not once"),
        (_halted, "holds a halt"),
        (_other_bytes, "has SHA-256"),
    ],
    ids=["missing", "unconfirmed-unlisted", "unconfirmed-halted", "other-bytes"],
)
def test_a_plan_whose_snapshot_is_not_the_one_it_names_sends_nothing(spoil, why) -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = planned(made)
    done = []

    def after(number: int, op: str, key: str) -> None:
        # The plan is journaled, naming the snapshot it was made from, and then the snapshot
        # changes before the gate is asked.
        if not done and op == "create" and key.startswith("journal/"):
            if made.kinds()[-1:] == ["decision"]:
                done.append(True)
                spoil(made)

    made.store.after = after
    result = made.send_attempt()
    assert done and result.halt is not None and "nothing is sent" in result.halt.reason
    assert re.search(why, result.halt.reason), result.halt.reason
    assert made.submits() == []
    assert made.states() == {first: "Abandoned", second: "Abandoned"}


# 6. A refused short, and a strategy's part that ends with no order


@pytest.mark.parametrize("fault", [OrderFault.REJECT, OrderFault.REJECT_LATER])
def test_a_refused_short_settles_on_the_brokers_refusal_and_is_not_resent(fault) -> None:
    # The second leg sells USO from flat, which opens the short the broker refuses.
    short = order_id(CHAN, close_of(FULL), 2)
    made = world(faults=Faults(orders={short: fault}))
    made.ready(close_of(FULL), PAIR)
    first = planned(made)[0]
    result = made.send_attempt()
    if fault is OrderFault.REJECT:
        assert result.halt is not None and f"the order {short} is Rejected" in result.halt.reason
    else:
        assert result.halt is None, result.halt.reason
    # A rerun at the same decision, by another process, sends nothing.
    made.restart()
    again = made.send_attempt(made.sender(writer="f" * 32))
    assert again.halt is not None and "holds another call's claim" in again.halt.reason
    # The next decision's recover settles the refusal, and its plan asks for the short again
    # under that decision's own id, a new order rather than a resend.
    result = sent(made, DAYS[1])
    assert result.halt is None, result.halt.reason
    states = made.states()
    assert states[short] == "Rejected" and states[first] == "Filled"
    # The journal holds one `sending` of it, so the broker was called with it once. The
    # simulator places an order it rejects later, and never one it rejects at the submit.
    kinds = [e.kind for e in made.view().orders[short].entries]
    assert kinds.count("sending") == 1 and kinds[-1] == "rejected", kinds
    assert made.submits().count(short) == (fault is OrderFault.REJECT_LATER)
    renewed = order_id(CHAN, close_of(DAYS[1]), 1)
    assert made.view().orders[renewed].intent.qty == Decimal(-5)
    assert made.submits()[-1] == renewed


def test_a_strategys_part_ends_in_one_of_four_ways_and_none_is_a_skip() -> None:
    # #33 asks that a skip come from a closed list in the strategy's specification. No
    # strategy names one yet, so the list of ends holds none.
    assert [end.value for end in StrategyEnd] == ["sent", "no order needed", "partly sent", "off"]


def _switch_unreadable(made: SendWorld) -> dict:
    made.store.refuse(("get", off_switch_key(ACCOUNT, CHAN)))
    return PAIR


@pytest.mark.parametrize(
    ("bad", "why"),
    [
        (lambda made: {CHAN: {GLD: 0}}, "leaves out USO, which its book holds"),
        (lambda made: {}, 'no entries and no "trade nothing" mark'),
        (lambda made: {**PAIR, TWO: {GLD: 1}}, "a strategy of no deployment"),
        (_switch_unreadable, "off switch unreadable"),
    ],
    ids=["leaves-out-a-leg", "no-target-at-all", "target-of-no-strategy", "switch-unreadable"],
)
def test_bad_input_halts_and_never_ends_a_strategys_part_with_no_order(bad, why) -> None:
    made = world()
    sent(made, FULL)
    made.ready(close_of(DAYS[1]), PAIR)
    made.targets(made.at, bad(made))
    result = made.send_attempt()
    assert result.halt is not None and why in result.halt.reason
    outcome = output_key(DEPLOYMENT, made.run, made.at, SEND_STAGE, OUTCOME_OUTPUT)
    assert outcome not in made.store.blobs
    assert "decision" not in made.kinds(made.before)
