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
    PORTFOLIO_STAGE,
    SEND_STAGE,
    SNAPSHOT_OUTPUT,
    TARGET_OUTPUT,
    Basket,
    BlobRef,
    DecisionResult,
    Forecast,
    Halt,
    HaltRecord,
    Limit,
    OutputFile,
    Snapshot,
    StrategyEnd,
    TargetPortfolio,
    decode,
    decode_output,
    encode,
    encode_output,
    off_switch_key,
    order_id,
    output_key,
    result_key,
    sha256_of,
)
from ithildinruntime.execution.brokers.faults import Faults, OrderFault
from ithildinruntime.execution.brokers.simulated import SimulatedBroker
from ithildinruntime.execution.inputs import read_inputs
from ithildinruntime.execution.order_path import OrderPath
from ithildinruntime.execution.send import OUTCOME_OUTPUT
from ithildinruntime.journal import Journal, VerdictEntry, record_of
from ithildinruntime.portfolio.sizing import size
from tests.support import fake_broker, gates
from tests.support.fake_broker import FakeBroker
from tests.support.relay_broker import CheckedBroker, RelayBroker
from tests.support.send_world import CHAN, EXECUTION, SendWorld, strategy
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
    # At the second close net is 3 × 101 − 5 × 20 = 203, and 1 × 101 − 15 × 20 = −199, both past
    # the cap of 150. The leg refused widens net with the other leg unfilled: buying USO back
    # leaves 3 × 101 = 303, and selling GLD leaves 15 × 20 = 300.
    [({GLD: 3, USO: -5}, 1, "303"), ({GLD: 1, USO: -15}, 0, "300")],
    ids=["larger-leg-first", "larger-leg-second"],
)
def test_an_exit_while_net_is_past_its_cap_widens_net_on_one_leg_and_sends_nothing(
    held, refused, value
) -> None:
    made = world()
    sent(made, FULL, {CHAN: held})
    made.constraints = gates.constraints(max_net=Decimal(150))
    made.ready(close_of(DAYS[1]), {CHAN: {GLD: 0, USO: 0}})
    book = made.view().book
    net = sum(q * CLOSES[i][DAYS[1]] for i, q in book.positions_by_strategy[CHAN].items())
    assert abs(net) > 150, net
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
    assert verdicts(made) == {}


# 2. Hard limit 2 again, through the stage


def checked(made: SendWorld, writer: str = "e" * 32):
    """A send stage whose broker is the simulator behind the fake broker's four refusals."""
    return made.sender(
        brokers=lambda at: CheckedBroker(lambda: made.store.blobs, ACCOUNT, at, to=made.broker()),
        writer=writer,
    )


def test_a_checked_broker_reaches_a_simulated_broker_alone() -> None:
    # The order sweep exempts tests/support/relay_broker.py's submits on this promise alone.
    made = world()
    made.ready(close_of(FULL), PAIR)

    class Mine(SimulatedBroker):
        pass

    mine = Mine(made.log, made.clock, object(), Faults())  # type: ignore[arg-type]
    fake = FakeBroker(lambda: made.store.blobs, ACCOUNT, made.at)
    for wrong in (fake, RelayBroker(made.broker(), lambda: None), mine):
        with pytest.raises(TypeError, match="simulated broker alone"):
            CheckedBroker(lambda: made.store.blobs, ACCOUNT, made.at, to=wrong)  # type: ignore[arg-type]
    fake_broker.made.clear()


def test_no_order_of_a_day_reaches_the_broker_but_as_its_journal_and_plan_say() -> None:
    made = world()
    for day, target in ((FULL, PAIR), (DAYS[1], {CHAN: {GLD: -2, USO: 4}})):
        made.ready(close_of(day), target)
        result = made.send_attempt(checked(made))
        assert result.halt is None, result.halt.reason
    # Each order of both days reached the simulator once, through the checks. The fixture in
    # tests/conftest.py fails this test on any refusal.
    assert len(made.submits()) == 4 and len(set(made.submits())) == 4
    assert made.rebuilt() == 4


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
    # 400 / (3 × 102 + 5 × 20) baskets, each leg rounded to the nearest share.
    (-1, {GLD: ("-3", True), USO: ("5", True)}, {GLD: -3, USO: 5}, "1000209"),
    # 400 / (3 × 200 + 5 × 20) baskets. Each leg crosses zero and ends smaller than it began.
    (1, {GLD: ("5", True), USO: ("-8", True)}, {GLD: 2, USO: -3}, "999369"),
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
    assert made.rebuilt() == len([v for vs in verdicts(made).values() for v in vs])


@pytest.mark.parametrize("start", ["flat", "held"])
def test_a_wrong_priced_target_past_the_share_caps_is_refused_by_the_gate(start) -> None:
    # The target sizing would give at GLD's price 100 times low, had no cap cut it: 400 /
    # (3 × 1.01 + 5 × 20) baskets, each leg rounded to the nearest share. The gross cap reads
    # the price, so it is fooled: 12 × 1.01 + 19 × 20 = 392.12 is inside 1,000, where 12 × 101
    # alone is past it. The gate's share caps, which read no price, are what stop it.
    made = SendWorld(_priced(GLD, Decimal("0.01"), DAYS[1]), strategies=(SIZED,))
    made.constraints = gates.constraints(
        max_order_qty=TRUE, max_position=TRUE, max_gross=Decimal(1000)
    )
    if start == "held":
        result, _ = _tried(made, DAYS[0], 1)
        assert result.halt is None, result.halt.reason
    else:
        made.ready(close_of(DAYS[0]), {CHAN: {GLD: 0, USO: 0}})
        assert made.send_stage().halt is None
    before = len(made.submits())
    made.ready(close_of(DAYS[1]), {CHAN: {GLD: 12, USO: -19}})
    first, second = planned(made)
    result = made.send_attempt()
    assert result.halt is not None and "nothing is sent" in result.halt.reason
    assert f"the gate refused {first} on max_order_qty" in result.halt.reason
    assert made.submits()[before:] == []
    assert made.states()[first] == "Refused" and made.states()[second] == "Abandoned"
    # The gate read GLD at its wrong price, so its gross figure is inside the cap.
    refusal = verdicts(made)[first][-1]
    assert refusal.limit is Limit.MAX_ORDER_QTY
    assert Limit.MAX_GROSS not in [p.limit for p in refusal.past]
    assert made.rebuilt() == len([v for vs in verdicts(made).values() for v in vs])


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


def _twice(made: SendWorld) -> None:
    _, key = _snapshot_ref(made)
    stored = decode(made.store.blobs[key], DecisionResult)
    made.store.put(key, encode(replace(stored, outputs=stored.outputs * 2)))


def _relisted(made: SendWorld) -> None:
    """Another snapshot of the decision, stored whole and listed by data's result as its own.

    The store's own checks pass, so only the `decision` entry's SHA-256 tells it apart.
    """
    key, result = _snapshot_ref(made)
    stored = decode_output(made.store.blobs[key], Snapshot)
    (snapshot,) = stored.entries
    # GLD's newest close doubled, which still reads as a valid bar of its session.
    *older, newest = snapshot.bars[GLD]["1d"]
    doubled = replace(newest, high=newest.high * 2, close=newest.close * 2)
    bars = {**snapshot.bars, GLD: {"1d": (*older, doubled)}}
    other = replace(snapshot, bars=bars)
    data = encode_output(Snapshot, OutputFile(stored.header, (other,), False))
    made.store.put(key, data)
    held = decode(made.store.blobs[result], DecisionResult)
    made.store.put(result, encode(replace(held, outputs=(BlobRef(key, sha256_of(data)),))))


@pytest.mark.parametrize(
    ("spoil", "why"),
    [
        (_missing, "is missing"),
        (_unlisted, "0 times, not once"),
        (_halted, "holds a halt"),
        (_other_bytes, "has SHA-256"),
        (_twice, "2 times, not once"),
        (_relisted, "planned on .* with SHA-256"),
    ],
    ids=[
        "missing",
        "unconfirmed-unlisted",
        "unconfirmed-halted",
        "other-bytes",
        "unconfirmed-listed-twice",
        "other-snapshot-listed",
    ],
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
    # The second leg sells USO from flat, which opens a short. The simulator has no refusal of a
    # short as such, so the fault refuses this one id, at the submit or after acknowledging it.
    # The broker sits behind the fake broker's checks, which refuse an id sent twice.
    short = order_id(CHAN, close_of(FULL), 2)
    made = world(faults=Faults(orders={short: fault}))
    made.ready(close_of(FULL), PAIR)
    first = planned(made)[0]
    result = made.send_attempt(checked(made))
    if fault is OrderFault.REJECT:
        assert result.halt is not None and f"the order {short} is Rejected" in result.halt.reason
        # The first leg is at the broker alone, and the alarm says so.
        assert f"{first} is Acknowledged" in made.alarms.sent[-1]
    else:
        assert result.halt is None, result.halt.reason
    # A rerun at the same decision, by another process, sends nothing, and the claim is what
    # stops it.
    made.restart()
    again = made.send_attempt(checked(made, writer="f" * 32))
    assert again.halt is not None and "holds another call's claim" in again.halt.reason
    # The next decision's recover settles the refusal, and its plan asks for the short again
    # under that decision's own id, a new order rather than a resend.
    made.ready(close_of(DAYS[1]), PAIR)
    result = made.send_attempt(checked(made))
    assert result.halt is None, result.halt.reason
    states = made.states()
    assert states[short] == "Rejected" and states[first] == "Filled"
    # The journal holds one `sending` of it, and the checks behind each stage's broker refuse a
    # second submit of the id. Across stages, a resend with its own `sending` breaks the count,
    # and one without it fails the check that a `sending` is the order's newest entry. The
    # simulator places an order it rejects later, and never one it rejects at the submit.
    kinds = [e.kind for e in made.view().orders[short].entries]
    assert kinds.count("sending") == 1 and kinds[-1] == "rejected", kinds
    assert made.submits().count(short) == (fault is OrderFault.REJECT_LATER)
    renewed = order_id(CHAN, close_of(DAYS[1]), 1)
    assert made.view().orders[renewed].intent.qty == Decimal(-5)
    assert made.submits()[-1] == renewed
    assert made.rebuilt() == len([v for vs in verdicts(made).values() for v in vs])


def test_a_strategys_part_ends_in_one_of_four_ways_and_none_is_a_skip() -> None:
    # #33 asks that a skip come from a closed list in the strategy's specification. No
    # strategy names one yet, so the list of ends holds none.
    assert [end.name for end in StrategyEnd] == ["SENT", "NO_ORDER_NEEDED", "PARTLY_SENT", "OFF"]


def _of_the_day_before(made: SendWorld) -> dict:
    """The targets stored again under this decision's key, each of the decision before it."""
    key = output_key(DEPLOYMENT, made.run, made.at, PORTFOLIO_STAGE, TARGET_OUTPUT)
    made.targets(made.at, PAIR)
    stored = decode_output(made.store.blobs[key], TargetPortfolio)
    earlier = tuple(replace(t, decision_at=close_of(FULL)) for t in stored.entries)
    return {"stored": earlier}


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
        (_of_the_day_before, "is of the decision at"),
    ],
    ids=[
        "leaves-out-a-leg",
        "no-target-at-all",
        "target-of-no-strategy",
        "switch-unreadable",
        "target-of-another-decision",
    ],
)
def test_bad_input_halts_and_never_ends_a_strategys_part_with_no_order(bad, why) -> None:
    made = world()
    sent(made, FULL)
    made.ready(close_of(DAYS[1]), PAIR)
    wanted = bad(made)
    if "stored" in wanted:
        _store_targets(made, wanted["stored"])
    else:
        made.targets(made.at, wanted)
    result = made.send_attempt()
    assert result.halt is not None and why in result.halt.reason
    outcome = output_key(DEPLOYMENT, made.run, made.at, SEND_STAGE, OUTCOME_OUTPUT)
    assert outcome not in made.store.blobs
    assert "decision" not in made.kinds(made.before)


def _store_targets(made: SendWorld, entries: tuple) -> None:
    """Store target entries as they are, under this decision's file and its result."""
    key = output_key(DEPLOYMENT, made.run, made.at, PORTFOLIO_STAGE, TARGET_OUTPUT)
    header = decode_output(made.store.blobs[key], TargetPortfolio).header
    data = encode_output(TargetPortfolio, OutputFile(header, entries, False))
    made.store.put(key, data)
    result = result_key(DEPLOYMENT, made.run, made.at, PORTFOLIO_STAGE)
    held = decode(made.store.blobs[result], DecisionResult)
    made.store.put(result, encode(replace(held, outputs=(BlobRef(key, sha256_of(data)),))))
