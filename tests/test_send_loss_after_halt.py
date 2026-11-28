"""The day's loss read on a halt of the send stage's steps 2 to 4, for #247.

Every figure is made up. A short GLD position of 3 shares, filled at the first
close of 100, reads a loss of 3 at the next decision, where GLD is marked at
101 against V. On the session after that the price is back at 100, the stop is
loosened to 1000 and the target covers the short. A case plants one fault on
the first of those two days. Where the stop latched, the next day halts on
"loss stop stands"; where it did not, the order to cover goes out, which is the
defect #247 closes.

Each firing asserts the reading itself, a loss of 3.0000 against a stop of
1.0000, the `stop_fired` reason, and exactly one alarm sent during the run
under test that names the loss and the stop. A count of orders alone passes a
fix that fires on every halt.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    PORTFOLIO_STAGE,
    RECOVER_STAGE,
    RISK_SECTION,
    RISK_STAGE,
    VALUATION_OUTPUT,
    DecisionResult,
    Halt,
    StopState,
    caps_key,
    claim_key,
    loss_stop_key,
    off_switch_key,
)
from ithildinruntime.execution.account import Account
from ithildinruntime.execution.brokers.faults import EditPosition, Faults
from ithildinruntime.execution.order_path import Unread
from ithildinruntime.journal import VerdictEntry
from tests.support import gates
from tests.support.order_world import CASH, RUN, World, caps_blob
from tests.support.order_world import DEPLOYMENT as PAPER
from tests.support.send_world import CHAN, SendWorld, risked, strategy
from tests.support.sim_world import ACCOUNT, DEPLOYMENT, FULL, GLD, USO, after, close_of

DAYS = [FULL, after(FULL), after(FULL, 2)]
CLOSES = {GLD: dict(zip(DAYS, (100, 101, 100), strict=True)), USO: dict.fromkeys(DAYS, 20)}
SHORT = {CHAN: {GLD: -3, USO: 0}}
COVER = {CHAN: {GLD: 0, USO: 0}}
TWO = "two"
FIRED = "the day's loss of 3.0000 is past its stop of 1.0000"
MISSING = "the forecast of chan is missing"


def short_then(stop: Decimal = Decimal(1), targets=SHORT, **changes) -> SendWorld:
    """The short sent and filled on the first day, and the second day made ready."""
    made = SendWorld(CLOSES, **changes)
    made.ready(close_of(DAYS[0]), targets)
    first = made.send_stage()
    assert first.halt is None, first.halt.reason
    made.constraints = replace(made.constraints, loss_stop=stop)
    made.ready(close_of(DAYS[1]), SHORT)
    made.keeper.losses.clear()
    return made


def day_two(made: SendWorld) -> DecisionResult:
    """The price back, the stop loosened and the target covering the short, with no fault."""
    made.constraints = replace(made.constraints, loss_stop=Decimal(1000))
    made.store.before = lambda number, op, key: None
    made.faults = Faults()
    made.ready(close_of(DAYS[2]), COVER)
    return made.send_stage()


def stop_kinds(made: SendWorld) -> list[str]:
    return [k for k in made.kinds() if k.startswith("stop_")]


def latched(made: SendWorld, result: DecisionResult, paged: int, upstream: str) -> None:
    """What a firing on a halt leaves: the reading, its reason, one page and no order."""
    assert result.halt is not None
    reason = result.halt.reason
    assert reason.startswith(upstream) and reason.count(upstream) == 1
    assert FIRED in reason
    entry = made.view().stop.standing.entry
    assert (entry.reading.loss, entry.reading.bound, entry.reading.past) == (
        Decimal("3.0000"),
        Decimal("1.0000"),
        True,
    )
    assert entry.reason.startswith("the day's loss at the decision's own time read past its stop")
    assert f"read after the decision halted: {upstream}" in entry.reason
    (page,) = made.alarms.sent[paged:]
    assert FIRED in page
    assert stop_kinds(made) == ["stop_fired", "stop_stored"]
    assert "decision" not in made.kinds(made.before) and "intent" not in made.kinds(made.before)
    assert len(made.submits()) == 1


def holds(made: SendWorld) -> None:
    """The next session halts on the standing stop, and covers nothing."""
    result = day_two(made)
    assert result.halt is not None and "loss stop stands" in result.halt.reason
    assert len(made.submits()) == 1


def control(made: SendWorld, result: DecisionResult, paged: int, upstream: str, stage: str) -> None:
    """A loss inside its stop of 10 on a halt: the halt as it was, with its cause, and no more."""
    assert result.halt is not None and result.halt.reason == upstream
    assert result.halt.cause == made.result_ref(stage)
    assert made.keeper.losses, "the loss was not read"
    assert stop_kinds(made) == [] and made.alarms.sent[paged:] == []
    assert "decision" not in made.kinds(made.before) and "intent" not in made.kinds(made.before)
    assert len(made.submits()) == 1


# 1 and 2. A caps blob rewritten after recover is read at step 5 for its loss_stop alone.


@pytest.mark.parametrize(
    "rewrite",
    [
        {"stress_move": gates.d("0.01"), "stress_low": gates.d("0.02")},
        {"stress_through": date(2026, 1, 2), "stress_max_age_days": 1},
    ],
    ids=["stress-move-below-its-low", "stress-through-past-its-age"],
)
def test_a_caps_blob_rewritten_after_recover_still_latches_a_loss_past_its_stop(rewrite) -> None:
    made = short_then()
    made.rewrite_caps(**rewrite)
    paged = len(made.alarms.sent)
    result = made.send_stage()
    assert result.halt is not None and FIRED in result.halt.reason
    entry = made.view().stop.standing.entry
    assert entry.reading.loss == Decimal("3.0000") and entry.reading.bound == 1
    # No step before 5 halted, so the firing is step 5's own.
    assert entry.reason == "the day's loss at the decision's own time read past its stop"
    (page,) = made.alarms.sent[paged:]
    assert FIRED in page
    holds(made)


@pytest.mark.parametrize("stop", [Decimal(1), Decimal(10)], ids=["past", "control"])
def test_a_stress_move_past_its_age_still_latches_a_loss_past_its_stop(stop: Decimal) -> None:
    """The risk stage halts first when the age passes on the calendar, and the loss is read."""
    made = short_then(stop)
    reason = "the hard caps' stress_through is 2026-01-02, past the 1 allowed"
    made.stage_halted(made.at, RISK_STAGE, reason)
    paged = len(made.alarms.sent)
    result = made.send_stage()
    upstream = f"{made.result_key(RISK_STAGE)} holds a halt: {reason}"
    if stop == 10:
        control(made, result, paged, upstream, RISK_STAGE)
        return
    latched(made, result, paged, upstream)
    holds(made)


# 3 to 6. A halt before step 5 reads the loss.


def test_a_missing_forecast_still_latches_a_loss_past_its_stop() -> None:
    made = short_then()
    made.portfolio_halted(made.at, MISSING)
    paged = len(made.alarms.sent)
    result = made.send_stage()
    latched(made, result, paged, f"{made.result_key(PORTFOLIO_STAGE)} holds a halt: {MISSING}")
    holds(made)


@pytest.mark.parametrize("stop", [Decimal(1), Decimal(10)], ids=["past", "control"])
def test_a_halted_risk_stage_latches_a_loss_past_its_stop(stop: Decimal) -> None:
    made = short_then(stop)
    reason = "the deployment runs 2 strategies"
    made.stage_halted(made.at, RISK_STAGE, reason)
    paged = len(made.alarms.sent)
    result = made.send_stage()
    upstream = f"{made.result_key(RISK_STAGE)} holds a halt: {reason}"
    if stop == 10:
        control(made, result, paged, upstream, RISK_STAGE)
        return
    latched(made, result, paged, upstream)
    holds(made)


def garbled(made: SendWorld, key: str) -> None:
    """A read of `key` that answers with something other than a blob or a refusal."""

    def before(number: int, op: str, at: str) -> None:
        if op == "get" and at == key:
            raise ValueError("a read that answers with no switch")

    made.store.before = before


@pytest.mark.parametrize("stop", [Decimal(1), Decimal(10)], ids=["past", "control"])
def test_an_unreadable_off_switch_still_latches_a_loss_past_its_stop(stop: Decimal) -> None:
    made = short_then(stop)
    garbled(made, off_switch_key(ACCOUNT, CHAN))
    paged = len(made.alarms.sent)
    result = made.send_stage()
    upstream = "strategy off switch unreadable: ValueError"
    if stop == 10:
        # The switch's halt is the stage's own, so it carries no cause.
        assert result.halt is not None and result.halt.reason == upstream
        assert result.halt.cause is None and made.keeper.losses
        assert stop_kinds(made) == [] and made.alarms.sent[paged:] == []
        assert "decision" not in made.kinds(made.before)
        return
    latched(made, result, paged, upstream)
    holds(made)


class _Down:
    """A broker of the account that answers no call."""

    alias = ACCOUNT

    def __getattr__(self, name: str):
        def call(*args, **kwargs):
            raise ConnectionError("no answer")

        return call


@pytest.mark.parametrize("fault", ["the broker flat", "a broker that does not answer"])
def test_a_sync_halt_fires_on_a_loss_past_its_stop(fault: str) -> None:
    """The journal's book is read, which the sync found differs, so this reading can only fire."""
    made = short_then()
    if fault == "the broker flat":
        # Someone covers the short by hand, at the send's first read of the broker.
        made.faults = Faults(events={made.log.calls + 1: EditPosition(GLD, Decimal(3))})
        stage = made.sender()
    else:
        stage = made.sender(brokers=lambda at: _Down())  # type: ignore[arg-type,return-value]
    paged = len(made.alarms.sent)
    result = made.send_attempt(stage)
    assert result.halt is not None
    upstream = result.halt.reason.split(f". {FIRED}")[0]
    latched(made, result, paged, upstream)
    if fault == "a broker that does not answer":
        # With the broker flat, the next recover halts on the difference first.
        holds(made)


# 7. A loss inside its stop reads and leaves the halt as it was.


@pytest.mark.parametrize(
    ("stop", "fires"),
    [(Decimal("3.0001"), False), (Decimal(3), False), (Decimal("2.9999"), True)],
    ids=["a-step-below-the-stop", "at-the-stop", "a-step-above-the-stop"],
)
def test_a_loss_inside_its_stop_during_a_halt_fires_nothing(stop: Decimal, fires: bool) -> None:
    made = short_then(stop)
    made.portfolio_halted(made.at, MISSING)
    upstream = f"{made.result_key(PORTFOLIO_STAGE)} holds a halt: {MISSING}"
    paged = len(made.alarms.sent)
    result = made.send_stage()
    assert result.halt is not None
    assert made.keeper.losses, "the loss was not read"
    if fires:
        assert "past its stop of 2.9999" in result.halt.reason and stop_kinds(made)
        return
    assert result.halt.reason == upstream
    assert result.halt.cause == made.result_ref(PORTFOLIO_STAGE)
    assert stop_kinds(made) == [] and made.alarms.sent[paged:] == []
    assert "decision" not in made.kinds(made.before) and "intent" not in made.kinds(made.before)
    assert len(made.submits()) == 1


# 8 to 10. What the reading still halts on, and what it no longer does.


@pytest.mark.parametrize(
    ("blob", "why"),
    [
        (
            lambda: caps_blob(gates.caps(), ACCOUNT).replace(b'"loss_stop"', b'"stop"'),
            "holds no loss_stop",
        ),
        (
            lambda: caps_blob(gates.caps(loss_stop=Decimal(0)), ACCOUNT),
            "loss_stop is 0, not above zero",
        ),
        (lambda: caps_blob(gates.caps(), "paper-2"), "does not name the account"),
        (lambda: b"[]", "not a JSON object"),
    ],
    ids=["no-loss-stop", "a-stop-of-zero", "another-account", "no-object"],
)
def test_the_loss_reading_still_halts_on_a_caps_blob_that_is_not_whole(blob, why) -> None:
    made = short_then()
    made.portfolio_halted(made.at, MISSING)
    made.store.put(caps_key(ACCOUNT), blob())
    result = made.send_stage()
    assert result.halt is not None and "The day's loss could not be read: " in result.halt.reason
    assert why in result.halt.reason and stop_kinds(made) == []
    assert result.halt.cause == made.result_ref(PORTFOLIO_STAGE)


def reading(world: World) -> tuple:
    """The halt path's reading, on an order path's world."""
    where = (PAPER, RUN, gates.DECISION)
    return world.account().loss(where, world.strategies(), world.report, after="a halt")


def test_the_loss_reading_still_halts_on_a_valuation_of_another_session() -> None:
    world = World(valuation=gates.valuation(10**9, replayed_through=1, session=gates.TODAY))
    with pytest.raises(Unread, match="the valuation is of .*, the session before the decision's"):
        reading(world)
    assert world.every_kind().count("stop_fired") == 0


@pytest.mark.parametrize("value", [Decimal("0.0001"), Decimal(0), Decimal(-5)])
def test_the_loss_reading_fires_on_a_valuation_at_or_below_zero(value: Decimal) -> None:
    """`tighten` halts there, where the loss is largest, and the halt path's reading fires."""
    # Short 1,000 GLD sold at 1 and marked at 1,002 leaves the account worth -1,000.
    world = World(
        position=Decimal(-1000),
        snapshot=gates.snapshot({gates.GLD: 1002, gates.USO: 1}),
        valuation=gates.valuation(value, replayed_through=1),
        constraints=(gates.constraints(loss_stop=Decimal(1)),),
    )
    loss = (1000 + value).quantize(Decimal("0.0001"))
    with pytest.raises(Halt, match=f"the day's loss of {loss} is past its stop of 1"):
        reading(world)
    assert world.every_kind().count("stop_fired") == 1


def test_the_gate_reads_a_valuation_at_or_below_zero_that_its_check_halts_on() -> None:
    """`tighten` halts there, where the loss is largest, and the reading measures it."""
    for value in (0, -5):
        world = gates.World(book=gates.book(cash=-10)).given(valuation=gates.valuation(value))
        world = world.held_to(loss_stop=Decimal(1))
        with pytest.raises(Halt, match="not above zero"):
            world.check()
        read = world.loss()
        assert read.past and read.loss == value + 10 and read.bound == 1
        stop = world.constraints.loss_stop
        again = world.gate().loss_against(
            world.book, world.snapshot, stop, world.inputs.valuation, StopState.ABSENT, world.now
        )
        assert again == read


# 11 to 14. The halt's reason, its cause, and an outage.


def test_a_halt_joined_to_its_loss_reading_names_each_once() -> None:
    made = short_then()
    reason = "recover could not read the broker"
    made.stage_halted(made.at, RECOVER_STAGE, reason)
    result = made.send_stage()
    upstream = f"{made.result_key(RECOVER_STAGE)} holds a halt: {reason}"
    assert result.halt is not None
    assert result.halt.reason == (
        f"{upstream}. The day's loss could not be read, for that same reason"
    )
    assert stop_kinds(made) == []


@pytest.mark.parametrize("stop", [Decimal(10), Decimal(1)], ids=["inside", "past"])
def test_an_upstream_halt_keeps_its_cause_after_the_loss_is_read(stop: Decimal) -> None:
    made = short_then(stop)
    made.portfolio_halted(made.at, MISSING)
    result = made.send_stage()
    assert result.halt is not None and made.keeper.losses
    assert result.halt.cause == made.result_ref(PORTFOLIO_STAGE)
    assert (FIRED in result.halt.reason) is (stop == 1)


def test_an_upstream_outage_passes_through_with_no_reading() -> None:
    made = short_then()
    made.store.refuse(("get", made.result_key(PORTFOLIO_STAGE)))
    paged = len(made.alarms.sent)
    result = made.send_attempt()
    assert result.halt is not None and "did not answer" in result.halt.reason
    assert made.keeper.losses == [] and stop_kinds(made) == []
    assert FIRED not in " ".join(made.alarms.sent[paged:])


@pytest.mark.parametrize("path", ["a-halt", "a-claim", "the-off-switch"])
def test_an_outage_during_the_reading_fires_only_on_a_claim_with_no_plan(path: str) -> None:
    """An outage fires nothing, but on the claim's halt of a decision with no plan.

    The call that claimed it may have read the loss past its stop and died, and the latch holds
    a firing whose writes fail, so there the stop fires with no reading, as before #247.
    """
    made = short_then(Decimal(1000) if path == "a-claim" else Decimal(1))
    valuation = made.key(VALUATION_OUTPUT)
    if path == "a-halt":
        made.portfolio_halted(made.at, MISSING)
        made.store.refuse(("get", valuation))
    elif path == "a-claim":
        key = claim_key(DEPLOYMENT, made.run, made.at)
        made.store.put(key, b'{"token":"dead","writer":"' + b"f" * 32 + b'"}')
        made.store.refuse(("get", valuation))
    else:
        made.store.refuse(("get", off_switch_key(ACCOUNT, CHAN)))
    paged = len(made.alarms.sent)
    result = made.send_attempt()
    assert result.halt is not None
    sent = made.alarms.sent[paged:]
    if path == "a-claim":
        assert stop_kinds(made) == ["stop_fired", "stop_stored"]
        assert made.view().stop.standing.entry.reading is None
        assert "the loss stop fired with no reading" in result.halt.reason
        # The outage pages, and so does the firing.
        assert len(sent) == 2 and "did not answer" in sent[0]
        assert "the loss stop fired with no reading" in sent[1]
        return
    assert stop_kinds(made) == []
    assert not any("loss stop fired" in s or FIRED in s for s in sent)
    if path == "a-halt":
        assert "The day's loss could not be read" in result.halt.reason
        # The outage alone pages, once.
        assert len(sent) == 1 and "did not answer" in sent[0]


# 15 and 16. A wrong send row builds no broker, and the loss is read all the same.


def test_a_wrong_send_row_with_a_position_held_still_reads_the_loss() -> None:
    made = short_then(Decimal(10))
    result = made.send_attempt(made.sender(brokers=Halt("the send row names another stage")))
    assert result.halt is not None and result.halt.reason == "the send row names another stage"
    assert made.keeper.losses and stop_kinds(made) == []
    assert claim_key(DEPLOYMENT, made.run, made.at) not in made.store.blobs


def test_a_wrong_send_row_with_a_loss_past_its_stop_fires_and_writes_no_claim() -> None:
    made = short_then()
    paged = len(made.alarms.sent)
    result = made.send_stage(made.sender(brokers=Halt("the send row names another stage")))
    latched(made, result, paged, "the send row names another stage")
    assert claim_key(DEPLOYMENT, made.run, made.at) not in made.store.blobs
    holds(made)


# 17 to 19. Settling first, the close, and one reading.


class _Killed(BaseException):
    """The process dies."""


def test_a_firing_journaled_without_its_blob_is_completed_before_the_reading() -> None:
    made = short_then()
    made.portfolio_halted(made.at, MISSING)

    def before(number: int, op: str, key: str) -> None:
        # The process dies once the firing is journaled, before its blob is stored.
        if op == "create" and key == loss_stop_key(ACCOUNT):
            raise _Killed

    made.store.before = before
    with pytest.raises(_Killed):
        made.send_attempt()
    made.store.before = lambda number, op, key: None
    assert stop_kinds(made) == ["stop_fired"] and loss_stop_key(ACCOUNT) not in made.store.blobs
    made.restart()
    made.keeper.losses.clear()
    # The stop is loosened, so no firing of this reading can complete the old one.
    made.constraints = replace(made.constraints, loss_stop=Decimal(1000))
    again = made.send_attempt()
    assert stop_kinds(made) == ["stop_fired", "stop_stored"]
    assert loss_stop_key(ACCOUNT) in made.store.blobs
    # The completed firing stands, which the reading after the claim's halt says.
    assert again.halt is not None and "loss stop stands" in again.halt.reason
    assert FIRED not in again.halt.reason and made.keeper.losses


def test_a_reading_at_the_decision_inside_the_stop_counts_as_read_past_the_close() -> None:
    made = short_then(Decimal(10))
    made.portfolio_halted(made.at, MISSING)
    made.clock.at = gates.TABLE.opened_by(made.at).close + timedelta(hours=1)
    result = made.send_stage()
    upstream = f"{made.result_key(PORTFOLIO_STAGE)} holds a halt: {MISSING}"
    assert result.halt is not None and result.halt.reason == upstream
    assert made.keeper.losses[0] == made.at and len(made.keeper.losses) == 2
    assert stop_kinds(made) == []


def test_step_5_and_the_halt_path_return_the_same_reading() -> None:
    for loss in (Decimal(40), Decimal(60)):
        world = World(
            valuation=gates.valuation(CASH + loss, replayed_through=1),
            constraints=(gates.constraints(loss_stop=Decimal(100)),),
        )
        step_5 = world.path().loss(PAPER, RUN, gates.DECISION, world.strategies())
        assert step_5 == reading(world) and step_5[0].loss == loss
        # The order's own check reads the same figure against the same stop.
        world.send()
        (entry,) = [e for e in world.entries() if isinstance(e, VerdictEntry)]
        assert entry.verdict.loss == step_5[0]


def test_a_reading_past_its_stop_is_the_same_on_both_paths() -> None:
    world = World(
        valuation=gates.valuation(CASH + Decimal(101), replayed_through=1),
        constraints=(gates.constraints(loss_stop=Decimal(100)),),
    )
    with pytest.raises(Halt) as halt:
        reading(world)
    with pytest.raises(Halt) as step_5:
        world.path().loss(PAPER, RUN, gates.DECISION, world.strategies())
    assert halt.value.reason.split(". entry")[0] == step_5.value.reason.split(". entry")[0]


# 20 and 21. Whose stop is read.


def test_a_strategy_that_holds_nothing_fires_on_the_accounts_loss_past_its_stop() -> None:
    made = short_then(
        Decimal(10),
        {**SHORT, TWO: {GLD: 0, USO: 0}},
        strategies=(strategy(), strategy(TWO)),
        others=(gates.constraints(strategy=TWO, loss_stop=Decimal(1)),),
    )
    made.stage_halted(made.at, RISK_STAGE, "the deployment runs 2 strategies")
    paged = len(made.alarms.sent)
    result = made.send_stage()
    upstream = f"{made.result_key(RISK_STAGE)} holds a halt: the deployment runs 2 strategies"
    latched(made, result, paged, upstream)
    made.others = (gates.constraints(strategy=TWO, loss_stop=Decimal(1000)),)
    holds(made)


@pytest.mark.parametrize("stop", [1, 10], ids=["past", "control"])
def test_a_risk_section_with_a_bad_field_the_loss_never_uses_still_reads_the_loss(stop) -> None:
    bad = risked(strategy(), Decimal(stop))
    sections = {**bad.sections, RISK_SECTION: {"loss_stop": stop, "max_net": -1}}
    made = short_then(strategies=(replace(bad, sections=sections),))
    made.stage_halted(made.at, RISK_STAGE, "max_net is -1, below zero")
    paged = len(made.alarms.sent)
    result = made.send_stage()
    upstream = f"{made.result_key(RISK_STAGE)} holds a halt: max_net is -1, below zero"
    if stop == 10:
        control(made, result, paged, upstream, RISK_STAGE)
        return
    latched(made, result, paged, upstream)
    # The section keeps its stop of 1 the next session too, so the firing stands either way.
    holds(made)


# What round 1 of the review asked for.


@pytest.mark.parametrize("where", ["step-5", "a-halt"])
def test_the_caps_own_loss_stop_binds_where_it_is_the_tighter(where: str) -> None:
    """The caps blob is the one place the owner's stop enters the reading, read alone."""
    made = short_then(Decimal(10))
    # The owner tightens the caps' own stop, which recover's copy of them does not hold yet.
    made.rewrite_caps(loss_stop=Decimal(1))
    if where == "a-halt":
        made.portfolio_halted(made.at, MISSING)
    paged = len(made.alarms.sent)
    result = made.send_stage()
    assert result.halt is not None and FIRED in result.halt.reason
    entry = made.view().stop.standing.entry
    assert (entry.reading.loss, entry.reading.bound) == (Decimal("3.0000"), Decimal("1.0000"))
    (page,) = made.alarms.sent[paged:]
    assert FIRED in page
    holds(made)


def losing_world(**changes) -> World:
    """An order path's world whose loss of 101 is past its stop of 100."""
    return World(
        valuation=gates.valuation(CASH + Decimal(101), replayed_through=1),
        constraints=(gates.constraints(loss_stop=Decimal(100)),),
        **changes,
    )


@pytest.mark.parametrize(
    ("section", "why"),
    [
        (None, r"has no \[risk\] section"),
        ({"max_net": 1}, "states no loss_stop"),
        ({"loss_stop": True}, "core refuses: the strategy's loss_stop is True"),
        ({"loss_stop": "1"}, "core refuses: the strategy's loss_stop is '1'"),
        ({"loss_stop": 0}, "core refuses: the strategy's loss_stop is 0, not above zero"),
        ({"loss_stop": -1}, "core refuses: the strategy's loss_stop is -1, not above zero"),
        ({"loss_stop": Decimal("NaN")}, "core refuses: the strategy's loss_stop"),
    ],
    ids=["no-section", "no-stop", "a-bool", "text", "zero", "negative", "nan"],
)
def test_a_stop_the_risk_section_holds_wrongly_reads_as_unread(section, why) -> None:
    """Such a decision cannot read its loss, which is #264's, so nothing fires here."""
    world = losing_world()
    (made,) = world.strategies()
    sections = {k: v for k, v in made.sections.items() if k != RISK_SECTION}
    if section is not None:
        sections[RISK_SECTION] = section
    strategies = (replace(made, sections=sections),)
    where = (PAPER, RUN, gates.DECISION)
    with pytest.raises(Unread, match=why) as halt:
        world.account().loss(where, strategies, world.report, after="a halt")
    assert gates.CHAN in halt.value.reason
    assert world.every_kind().count("stop_fired") == 0 and world.alarms.sent == []


def test_a_stage_that_runs_no_strategy_cannot_read_its_loss() -> None:
    world = losing_world()
    where = (PAPER, RUN, gates.DECISION)
    with pytest.raises(Unread, match="runs no strategy"):
        world.account().loss(where, (), world.report, after="a halt")
    assert world.every_kind().count("stop_fired") == 0
    # The claim's halt with no plan fires with no reading on it, as on any input it cannot read.
    with pytest.raises(Halt, match="^the loss stop fired with no reading.*runs no strategy"):
        world.account().loss(where, (), world.report, after="a claim", unread=True)


def test_an_account_that_cannot_be_used_reads_no_loss_and_says_so() -> None:
    made = short_then()
    made.portfolio_halted(made.at, MISSING)
    broken = Account(
        store=made.store,  # type: ignore[arg-type]
        storage=made.storage(),
        account="a/b",
        writer="d" * 32,
        gate=made.keeper,  # type: ignore[arg-type]
        clock=made.clock,  # type: ignore[arg-type]
        alerts=made.alarms,
        retry=made.retry,
        pause=made.clock.wait,
    )
    result = made.send_attempt(made.sender(account=broken, brokers=Halt("no broker")))
    assert result.halt is not None
    assert result.halt.reason == (
        "no broker. The day's loss could not be read: the account 'a/b' is no alias a journal "
        "is kept under"
    )
    assert stop_kinds(made) == [] and made.keeper.losses == []


@pytest.mark.parametrize("path", ["a-halt", "a-claim"])
def test_a_settle_that_halts_reads_as_unread_and_fires_nothing(path: str) -> None:
    """On the claim's halt with no plan too, where an input that cannot be read fires the stop."""
    made = short_then()
    if path == "a-halt":
        made.portfolio_halted(made.at, MISSING)
        upstream = f"{made.result_key(PORTFOLIO_STAGE)} holds a halt: {MISSING}"
    else:
        key = claim_key(DEPLOYMENT, made.run, made.at)
        made.store.put(key, b'{"token":"dead","writer":"' + b"f" * 32 + b'"}')
        upstream = f"{key} holds another call's claim, so this one sends nothing"
    calls: list[int] = []

    def refused(report) -> None:
        # The claim's own settle passes, and the reading's settle halts.
        calls.append(1)
        if path == "a-halt" or len(calls) > 1:
            raise Halt("the journal still does not take the answer of order X")

    made.account._shared.unstored.settle = refused  # type: ignore[method-assign]
    result = made.send_attempt()
    assert result.halt is not None and result.halt.reason.startswith(upstream)
    # Step 2 halts on the upstream result, or the claim halts, and the reading's own settle halts.
    assert result.halt.reason.endswith(
        ". The day's loss could not be read: the journal still does not take the answer of order X"
    )
    assert "fired with no reading" not in result.halt.reason
    assert stop_kinds(made) == []


@pytest.mark.parametrize(
    "rewrite",
    [
        {"stress_move": gates.d("0.01"), "stress_low": gates.d("0.02")},
        {"stress_through": date(2026, 1, 2), "stress_max_age_days": 1},
        None,
    ],
    ids=["stress-move-below-its-low", "stress-through-past-its-age", "no-share-step"],
)
def test_step_5_still_halts_on_caps_an_order_could_not_use(rewrite) -> None:
    """The loss reads inside its stop, and a decision that plans no order halts before its plan."""
    made = short_then(Decimal(1000))
    if rewrite is None:
        blob = caps_blob(made.caps, ACCOUNT).replace(b'"share_step"', b'"step"')
        made.store.put(caps_key(ACCOUNT), blob)
    else:
        made.rewrite_caps(**rewrite)
    result = made.send_stage()
    assert result.halt is not None and FIRED not in result.halt.reason
    assert "the hard caps'" in result.halt.reason or "the caps blob holds no" in result.halt.reason
    assert "decision" not in made.kinds(made.before) and stop_kinds(made) == []
