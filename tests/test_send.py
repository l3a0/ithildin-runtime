"""Execution's send stage, run for real after the real recover, for #146's acceptance 2 to 5.

Every figure is made up. `tests/support/send_world.py` stores what stands before
the stage at each decision. Each case reads what the journal and the simulated
broker hold afterwards, never only what the stage returned.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    CAPS_OUTPUT,
    RECOVER_STAGE,
    Halt,
    OrderState,
    Stopped,
    StrategyEnd,
    claim_key,
    off_switch_key,
    order_id,
    output_key,
)
from ithildinruntime.execution.brokers.faults import EditCash, EditPosition, Faults, OrderFault
from ithildinruntime.journal import ReadEntry
from tests.support import gates
from tests.support.send_world import CHAN, SendWorld, strategy
from tests.support.sim_world import DEPLOYMENT, FULL, GLD, USO, after, close_of

DAYS = [FULL, after(FULL), after(FULL, 2)]
CLOSES = {GLD: {day: 100 + n for n, day in enumerate(DAYS)}, USO: {day: 20 for day in DAYS}}
PAIR = {CHAN: {GLD: 3, USO: -5}}


def world(**changes) -> SendWorld:
    return SendWorld(CLOSES, **changes)


def sent(made: SendWorld, day: date, targets=None):
    made.ready(close_of(day), targets if targets is not None else PAIR)
    return made.send_stage()


def names(made: SendWorld) -> list[str]:
    return sorted(made.states())


# A day that sends


def test_a_pair_goes_out_in_the_plans_order_and_the_outcome_names_each() -> None:
    made = world()
    result = sent(made, FULL)
    outcome = made.outcome(result)
    first, second = names(made)
    assert [(o.client_order_id, o.state) for o in outcome.orders] == [
        (first, OrderState.ACKNOWLEDGED),
        (second, OrderState.ACKNOWLEDGED),
    ]
    assert [(s.strategy, s.end) for s in outcome.strategies] == [(CHAN, StrategyEnd.SENT)]
    assert made.submits() == [first, second]
    assert made.kinds(made.before) == [
        "read",
        "read",
        "decision",
        "intent",
        "verdict",
        "sending",
        "ack",
        "intent",
        "verdict",
        "sending",
        "ack",
    ]


def test_the_fills_land_at_the_next_decision_and_the_book_holds_the_target() -> None:
    made = world()
    sent(made, FULL)
    result = sent(made, DAYS[1], PAIR)
    assert made.outcome(result).strategies[0].end is StrategyEnd.NO_ORDER_NEEDED
    book = made.view().book
    assert dict(book.positions_by_strategy[CHAN]) == {GLD: 3, USO: -5}
    made.agree()


def test_a_decision_with_no_order_needed_journals_its_plan_and_says_so() -> None:
    made = world()
    result = sent(made, FULL, {CHAN: {GLD: 0, USO: 0}})
    outcome = made.outcome(result)
    assert outcome.orders == () and outcome.strategies[0].end is StrategyEnd.NO_ORDER_NEEDED
    assert made.kinds(made.before)[-1] == "decision"
    assert made.submits() == []


def test_the_decision_entry_names_every_file_the_plan_was_made_from() -> None:
    made = world()
    sent(made, FULL)
    (entry,) = made.view().decisions.values()
    assert sorted(entry.inputs) == [
        "constraints",
        f"forecast/{CHAN}",
        f"schedule/{CHAN}",
        "snapshot",
        "targets",
        "valuation",
    ]


def test_a_stop_while_the_stage_waits_ends_it_before_it_writes_anything() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    before = dict(made.store.blobs)

    def stopped(at, report) -> None:
        raise Stopped

    made.clock.wait_until = stopped
    with pytest.raises(Stopped):
        made.send_attempt()
    assert made.store.blobs == before


# Halts before anything is planned


def test_a_strategy_with_no_execution_section_halts_before_any_entry() -> None:
    made = world(strategies=(strategy(execution=""),))
    result = sent(made, FULL)
    assert result.halt is not None and "[execution]" in result.halt.reason
    assert "decision" not in made.kinds(made.before)


def test_a_strategy_that_is_off_gets_no_order_and_is_named() -> None:
    made = world()
    made.store.put(off_switch_key("sim-1", CHAN), b"off")
    result = sent(made, FULL)
    outcome = made.outcome(result)
    assert outcome.orders == ()
    ((line),) = outcome.strategies
    assert line.end is StrategyEnd.OFF and "off switch stands" in line.reason
    (entry,) = made.view().decisions.values()
    assert entry.off == (CHAN,) and entry.orders == ()


def test_an_entry_another_writer_journaled_after_recovers_book_halts_the_sync() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    made._record(ReadEntry(RECOVER_STAGE, made.at, made.ref(RECOVER_STAGE, CAPS_OUTPUT), made.at))
    # A read alone moves no book, so it is let through.
    assert made.send_attempt().halt is None


@pytest.mark.parametrize(
    "event",
    [EditCash(Decimal(5)), EditPosition(GLD, Decimal(2))],
    ids=["cash", "position"],
)
def test_the_broker_moving_between_recover_and_send_halts_before_any_plan(event) -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    # The simulator counts every call of the account, so this edit lands at the send's first read.
    made.faults = Faults(events={made.log.calls + 1: event})
    result = made.send_attempt()
    assert result.halt is not None and "the book and the broker differ" in result.halt.reason
    assert "decision" not in made.kinds(made.before)
    assert made.submits() == []


def test_a_claim_another_call_holds_halts_naming_every_planned_order() -> None:
    made = world()
    sent(made, FULL)
    again = made.send_attempt(made.sender(writer="f" * 32))
    assert again.halt is not None
    reason = again.halt.reason
    assert "holds another call's claim" in reason
    for name in names(made):
        assert f"{name} is Acknowledged" in reason
    assert len(made.submits()) == 2


def test_a_second_call_in_one_process_does_not_read_its_own_claim() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    stage = made.sender()
    assert stage(*_item(made)).halt is None
    again = stage(*_item(made))
    assert again.halt is not None and "holds another call's claim" in again.halt.reason
    assert len(made.submits()) == 2


def test_a_claim_names_an_order_in_doubt_and_alarms_on_it() -> None:
    first = order_id(CHAN, close_of(FULL), 1)
    made = world(faults=Faults(orders={first: OrderFault.LOSE_ANSWER}))
    made.ready(close_of(FULL), PAIR)
    result = made.send_attempt()
    assert result.halt is not None
    made.alarms.sent.clear()
    again = made.send_attempt(made.sender(writer="f" * 32))
    assert again.halt is not None and "in doubt" in again.halt.reason
    assert any("in doubt" in reason for reason in made.alarms.sent)


# Asking about the whole plan


def test_a_second_leg_the_gate_refuses_sends_nothing_and_ends_every_order() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    made.keeper.planted[second] = ["refuse", "refuse"]
    result = made.send_attempt()
    assert result.halt is not None and "nothing is sent" in result.halt.reason
    assert "max_order_qty" in result.halt.reason
    assert made.submits() == []
    assert made.states() == {first: "Abandoned", second: "Refused"}
    assert any("nothing is sent" in reason for reason in made.alarms.sent)


@pytest.mark.parametrize(
    ("limits", "refused"),
    [
        (
            {
                "max_order_qty": {GLD: gates.LOOSE, USO: Decimal(4)},
                "max_position": {GLD: gates.LOOSE, USO: Decimal(4)},
            },
            1,
        ),
        (
            {
                "max_order_qty": {GLD: Decimal(2), USO: gates.LOOSE},
                "max_position": {GLD: Decimal(2), USO: gates.LOOSE},
            },
            0,
        ),
        ({"max_position": {GLD: gates.LOOSE, USO: Decimal(4)}}, 1),
    ],
    ids=["second-leg-order-size", "first-leg-order-size", "second-leg-position"],
)
def test_a_real_limit_on_either_leg_sends_nothing_with_its_figure_and_bound(
    limits, refused
) -> None:
    made = world()
    made.constraints = gates.constraints(**limits)
    made.ready(close_of(FULL), PAIR)
    planned = gates_names(made)
    result = made.send_attempt()
    assert result.halt is not None and "nothing is sent" in result.halt.reason
    limit = next(iter(limits))
    assert f"on {limit}, at " in result.halt.reason and "against its bound of" in result.halt.reason
    assert made.submits() == []
    states = made.states()
    assert states[planned[refused]] == "Refused"
    assert {states[n] for n in planned if n != planned[refused]} == {"Abandoned"}


def test_a_first_leg_the_gate_refuses_leaves_the_second_with_an_intent_and_abandoned() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    made.keeper.planted[first] = ["refuse", "refuse"]
    assert made.send_attempt().halt is not None
    assert made.submits() == []
    assert made.states() == {first: "Refused", second: "Abandoned"}


@pytest.mark.parametrize("place", [0, 1], ids=["first", "last"])
def test_a_check_that_raises_sends_nothing(place) -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    planned = gates_names(made)
    made.keeper.planted[planned[place]] = [Halt("the gate cannot read a leg")]
    result = made.send_attempt()
    assert result.halt is not None and "the gate cannot read a leg" in result.halt.reason
    assert made.submits() == []
    assert set(made.states().values()) <= {"Abandoned"}
    assert set(made.states()) == set(planned)


def test_a_leg_refused_at_the_ask_and_approved_after_is_abandoned_and_never_sent() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    # Refused at the ask, approved when `hold` asks again.
    made.keeper.planted[second] = ["refuse", None]
    assert made.send_attempt().halt is not None
    assert made.submits() == []
    view = made.view()
    assert made.states() == {first: "Abandoned", second: "Abandoned"}
    kinds = [e.kind for e in view.orders[second].entries]
    assert kinds == ["intent", "verdict", "abandoned"]


# What a send answers


def test_a_second_leg_refused_at_its_send_halts_with_the_first_at_the_broker() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    # Approved at the ask, refused at the send's own check.
    made.keeper.planted[second] = [None, "refuse"]
    result = made.send_attempt()
    assert result.halt is not None
    assert f"the order {second} is Refused" in result.halt.reason
    assert made.submits() == [first]
    assert f"{first} is Acknowledged" in made.alarms.sent[-1]


def test_a_first_leg_refused_at_its_send_abandons_the_second() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    made.keeper.planted[first] = [None, "refuse"]
    result = made.send_attempt()
    assert result.halt is not None and made.submits() == []
    assert made.states() == {first: "Refused", second: "Abandoned"}


def test_an_off_switch_before_the_first_leg_ends_in_partly_sent() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    switch = off_switch_key("sim-1", CHAN)

    def before(number: int, op: str, key: str) -> None:
        # Switched off once the plan is journaled, before the first leg's own check.
        if op == "create" and key.startswith("journal/") and made.kinds()[-1:] == ["decision"]:
            made.store.put(switch, b"off")

    made.store.before = before
    outcome = made.outcome(made.send_attempt())
    assert outcome.strategies[0].end is StrategyEnd.PARTLY_SENT
    assert made.submits() == []
    assert made.states() == {first: "Abandoned", second: "Abandoned"}


def test_an_off_switch_after_the_first_leg_went_halts() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    switch = off_switch_key("sim-1", CHAN)

    def before(number: int, op: str, key: str) -> None:
        if op == "create" and key.startswith("journal/") and made.kinds()[-1:] == ["ack"]:
            made.store.put(switch, b"off")

    made.store.before = before
    result = made.send_attempt()
    assert result.halt is not None and f"the order {second} is Abandoned" in result.halt.reason
    assert made.submits() == [first]


# Hard limit 2, through the stage


def test_no_order_reaches_the_broker_around_the_gate() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    sent_before = len(made.keeper.asked)
    made.send_attempt()
    planned = gates_names(made)
    # Each order is asked at the plan's ask and again at its send.
    assert made.keeper.asked[sent_before:] == [planned[0], planned[1], planned[0], planned[1]]


# A kill after every write


def _creates(made: SendWorld) -> int:
    """How many blobs one send stage creates, counted on a clean run."""
    made.ready(close_of(FULL), PAIR)
    made.store.calls.clear()
    assert made.send_attempt().halt is None
    return len(_ours(made))


def _ours(made: SendWorld) -> list[str]:
    """Each blob the platform created, less the simulator's, whose writes are the broker's."""
    return [key for op, key in made.store.calls if op == "create" and not key.startswith("sim/")]


class _Killed(BaseException):
    pass


def test_a_kill_after_any_write_and_a_rerun_send_each_order_once_at_most() -> None:
    count = _creates(world())
    assert count >= 12
    for n in range(1, count + 1):
        made = world()
        made.ready(close_of(FULL), PAIR)
        made.store.calls.clear()

        def after(number: int, op: str, key: str, n=n, made=made) -> None:
            if op == "create" and not key.startswith("sim/") and len(_ours(made)) == n:
                raise _Killed

        made.store.after = after
        with pytest.raises(_Killed):
            made.send_attempt()
        made.store.after = lambda number, op, key: None
        made.restart()
        again = made.send_attempt(made.sender(writer="f" * 32))
        placed = made.submits()
        assert len(placed) == len(set(placed)) <= 2, n
        assert again.halt is not None, n
        if claim_key(DEPLOYMENT, made.run, made.at) in made.store.blobs:
            assert "claim" in again.halt.reason, n
            for name in gates_names(made, planned_only=True):
                assert name in again.halt.reason, n
        # The rerun sent nothing of its own.
        assert made.submits() == placed, n


# Helpers


def _item(made: SendWorld):
    from ithildinruntime.runtime.loop import Item
    from tests.support.recover_world import Waits

    return Item(made.at, "execution/send"), Waits()


def gates_names(made: SendWorld, planned_only: bool = False) -> list[str]:
    """The ids the plan at the newest decision gives its orders, as `plan_orders` numbers them."""
    from ithildinruntime.core import order_id

    if planned_only:
        seq = made.view().decided.get(made.at)
        if seq is None:
            return []
        return [o.client_order_id for o in made.view().decisions[seq].orders]
    return [order_id(CHAN, made.at, 1), order_id(CHAN, made.at, 2)]


_ = (output_key, gates)
