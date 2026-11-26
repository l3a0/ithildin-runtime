"""Execution's send stage, run for real after the real recover, for #146's acceptance 2 to 5.

Every figure is made up. `tests/support/send_world.py` stores what stands before
the stage at each decision. Each case reads what the journal and the simulated
broker hold afterwards, never only what the stage returned.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    CAPS_OUTPUT,
    RECOVER_STAGE,
    Halt,
    OrderState,
    Stopped,
    StoreUnavailable,
    StrategyEnd,
    claim_key,
    kill_switch_key,
    off_switch_key,
    order_id,
    output_key,
)
from ithildinruntime.execution.account import Account
from ithildinruntime.execution.brokers.faults import EditCash, EditPosition, Faults, OrderFault
from ithildinruntime.execution.order_path import Asked, OrderPath
from ithildinruntime.journal import DecisionEntry, ReadEntry
from tests.support import gates
from tests.support.send_world import CHAN, SendWorld, strategy
from tests.support.sim_world import DEPLOYMENT, FULL, GLD, USO, after, close_of
from tests.support.watched_broker import watch_submits

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


def test_a_read_journaled_after_recovers_book_moves_nothing_and_lets_the_sync_on() -> None:
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


# Round 1's cases: each guard of the stage, held by a test that fails without it


@pytest.mark.parametrize(
    "limits",
    [{"max_open_orders": 1}, {"max_gross": Decimal(350)}],
    ids=["open-orders", "gross"],
)
def test_a_pair_whose_legs_fit_alone_and_not_together_sends_nothing(limits) -> None:
    made = world()
    made.constraints = gates.constraints(**limits)
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    result = made.send_attempt()
    assert result.halt is not None and "nothing is sent" in result.halt.reason
    assert made.submits() == []
    assert made.states() == {first: "Abandoned", second: "Refused"}


def _short_then(made: SendWorld, day2: dict) -> None:
    """Short GLD at the first close, which fills there, and a loss stop of 1 at the second."""
    sent(made, FULL, {CHAN: {GLD: -3, USO: 0}})
    made.constraints = gates.constraints(loss_stop=Decimal(1))
    made.ready(close_of(DAYS[1]), day2)


def test_a_loss_past_its_stop_halts_a_decision_that_plans_no_order() -> None:
    made = world()
    _short_then(made, {CHAN: {GLD: -3, USO: 0}})
    result = made.send_attempt()
    assert result.halt is not None and "past its stop" in result.halt.reason
    assert "decision" not in made.kinds(made.before)


def test_a_loss_past_its_stop_at_the_ask_sends_nothing() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    made.keeper.planted[first] = ["loss_past"]
    result = made.send_attempt()
    assert result.halt is not None and "past its stop" in result.halt.reason
    assert made.submits() == []


def test_the_ask_halts_on_an_order_the_journal_already_holds() -> None:
    made = world()
    sent(made, FULL)
    with pytest.raises(Halt, match="has an entry already"):
        made.path().ask(gates_names(made))


def test_another_writers_entry_after_recovers_book_halts_before_any_plan() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    view = made.view()
    read = max(view.reads)
    earlier = made.at - timedelta(minutes=1)
    made._record(
        DecisionEntry(
            deployment=DEPLOYMENT,
            run=made.run,
            decision_at=earlier,
            targets=(),
            orders=(),
            off=(),
            inputs={},
            read=read,
            synced=view.through.seq,
        )
    )
    result = made.send_attempt()
    assert result.halt is not None and "another writer moved the book" in result.halt.reason
    assert made.submits() == []


def test_another_writers_plan_for_this_decision_is_never_sent_as_this_ones() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    planned = world()
    planned.ready(close_of(FULL), PAIR)
    assert planned.send_attempt().halt is None
    (theirs,) = planned.view().decisions.values()
    done = []

    def before(number: int, op: str, key: str) -> None:
        # Once this stage's own read is journaled, another writer journals a plan for the
        # decision, at ten times the size.
        if done or op != "create" or not key.startswith("journal/"):
            return
        if made.kinds().count("read") >= 2:
            done.append(True)
            read = max(made.view().reads)
            gld, uso = theirs.orders
            bigger = (replace(gld, qty=gld.qty * 10), uso)
            made._record(replace(theirs, orders=bigger, read=read, synced=read))

    made.store.before = before
    result = made.send_attempt()
    assert done and result.halt is not None and "another plan" in result.halt.reason
    first, second = gates_names(made)
    # Both plans are named, theirs at ten times this one's first leg.
    assert f"it plans {first} for 30, {second} for -5" in result.halt.reason
    assert f"this stage planned {first} for 3, {second} for -5" in result.halt.reason
    assert any("another plan" in reason for reason in made.alarms.sent)
    assert made.submits() == []


def test_an_identical_plan_in_the_journal_is_this_stages_own_and_goes_out_once() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    planned = world()
    planned.ready(close_of(FULL), PAIR)
    assert planned.send_attempt().halt is None
    (same,) = planned.view().decisions.values()
    done = []

    def before(number: int, op: str, key: str) -> None:
        # The same plan, with this stage's own read, lands just before the stage records it,
        # as an append whose answer was lost would leave it.
        if done or op != "create" or not key.startswith("journal/"):
            return
        if made.kinds().count("read") >= 2:
            done.append(True)
            view = made.view()
            read = max(view.reads)
            made._record(replace(same, read=read, synced=view.through.seq))

    made.store.before = before
    result = made.send_attempt()
    assert done and result.halt is None
    assert made.kinds(made.before).count("decision") == 1
    assert made.submits() == gates_names(made)


def test_a_claim_alarms_on_a_leg_a_kill_left_sending() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)

    def before(number: int, op: str, key: str) -> None:
        # The broker has placed the first leg, and the process dies as the journal is to take
        # its answer.
        if op == "create" and key.startswith("journal/") and made.kinds()[-1:] == ["sending"]:
            if made.submits():
                raise _Killed

    made.store.before = before
    with pytest.raises(_Killed):
        made.send_attempt()
    made.store.before = lambda number, op, key: None
    first = gates_names(made)[0]
    assert made.states() == {first: "Sending"} and made.submits() == [first]
    made.alarms.sent.clear()
    made.restart()
    again = made.send_attempt(made.sender(writer="f" * 32))
    assert again.halt is not None and f"{first} is in doubt" in again.halt.reason
    assert f"{first} of {CHAN} went and its partner" in again.halt.reason
    assert any(first in reason and "in doubt" in reason for reason in made.alarms.sent)
    assert made.submits() == [first]


@pytest.mark.parametrize("why", ["after the close", "with the snapshot unreadable"])
def test_a_claim_names_every_planned_order_however_the_loss_reads(why) -> None:
    made = world()
    sent(made, FULL)
    if why == "after the close":
        made.clock.at = made.at + timedelta(hours=1)
    else:
        made.store.drop(output_key(DEPLOYMENT, made.run, made.at, "data", "snapshot"))
    again = made.send_attempt(made.sender(writer="f" * 32))
    assert again.halt is not None
    reason = again.halt.reason
    for name in gates_names(made):
        assert f"{name} is Acknowledged" in reason
    if why == "after the close":
        # The loss's own halt joins the reason after the orders, and skips neither.
        assert reason.index("Acknowledged") < reason.index("session's close at")


def test_a_run_with_no_broker_halts_the_send_stage_before_its_claim() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    result = made.send_attempt(made.sender(brokers=Halt("no broker is built on shadow")))
    assert result.halt is not None and result.halt.reason == "no broker is built on shadow"
    assert claim_key(DEPLOYMENT, made.run, made.at) not in made.store.blobs


def test_a_strategy_switch_that_cannot_be_read_halts() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    made.store.refuse(("get", off_switch_key("sim-1", CHAN)))
    result = made.send_attempt()
    assert result.halt is not None and "off switch unreadable" in result.halt.reason
    assert "decision" not in made.kinds(made.before)


def test_a_switch_cleared_between_the_legs_never_sends_the_second_alone() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    switch = off_switch_key("sim-1", CHAN)

    def before(number: int, op: str, key: str) -> None:
        kinds = made.kinds()
        if op == "create" and key.startswith("journal/"):
            if kinds[-1:] == ["decision"]:
                made.store.put(switch, b"off")
            elif kinds[-1:] == ["abandoned"] and switch in made.store.blobs:
                made.store.drop(switch)

    made.store.before = before
    outcome = made.outcome(made.send_attempt())
    assert outcome.strategies[0].end is StrategyEnd.PARTLY_SENT
    assert made.submits() == []


def test_a_send_that_raises_after_a_sending_abandons_the_rest_and_names_each_leg(
    monkeypatch,
) -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    real = OrderPath.send

    def send(self, name):
        if name == second:
            raise RuntimeError("the path broke")
        return real(self, name)

    monkeypatch.setattr(OrderPath, "send", send)
    result = made.send_attempt()
    assert result.halt is not None and "raised RuntimeError" in result.halt.reason
    assert made.states() == {first: "Acknowledged", second: "Abandoned"}
    assert f"{first} is Acknowledged" in made.alarms.sent[-1]


def test_a_failing_order_left_approved_is_abandoned_with_the_rest(monkeypatch) -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    real = OrderPath.send

    def send(self, name):
        if name == first:
            # The order is approved and journaled so, and then the path breaks.
            self.hold([name])
            raise RuntimeError("the path broke")
        return real(self, name)

    monkeypatch.setattr(OrderPath, "send", send)
    result = made.send_attempt()
    assert result.halt is not None and "raised RuntimeError" in result.halt.reason
    assert made.states() == {first: "Abandoned", second: "Abandoned"}
    assert made.submits() == []


def test_an_ended_plans_order_the_journal_will_not_take_still_alarms_on_every_leg() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    switch = off_switch_key("sim-1", CHAN)

    def before(number: int, op: str, key: str) -> None:
        kinds = made.kinds()
        if op == "create" and key.startswith("journal/"):
            if kinds[-1:] == ["decision"]:
                made.store.put(switch, b"off")
            elif kinds[-1:] == ["abandoned"]:
                # The store takes no more of the journal once the first leg is abandoned.
                raise StoreUnavailable(key, "the store's own text")

    made.store.before = before
    result = made.send_attempt()
    assert result.halt is not None
    assert "The plan's orders stand so" in result.halt.reason
    assert any("The plan's orders stand so" in reason for reason in made.alarms.sent)
    assert made.submits() == []


def test_a_failure_whose_journal_cannot_be_read_still_names_its_cause_and_every_leg(
    monkeypatch,
) -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    real = OrderPath.send

    def send(self, name):
        if name == second:
            made.store.refuse(("*", "journal/"))
            raise RuntimeError("the path broke")
        return real(self, name)

    monkeypatch.setattr(OrderPath, "send", send)
    alarms = len(made.alarms.sent)
    result = made.send_attempt()
    reason = result.halt.reason
    assert "raised RuntimeError" in reason and "could not be read" in reason
    # The leg this call saw acknowledged is named, and the one it cannot read back is not known.
    assert f"stand so: {first} acknowledged, {second} not known, since the journal" in reason
    # The leg whose call failed may be at the broker, so nothing says it was not sent.
    assert "none of them was sent" not in reason
    assert made.submits() == [first]
    # One alarm for the outage and one naming every leg, and no abandon is tried on it.
    sent = made.alarms.sent[alarms:]
    assert len(sent) == 2 and sent[0].startswith("account sim-1: the journal could not be read")
    assert "The plan's orders stand so" in sent[1]


def test_a_journal_that_turns_unreadable_after_the_abandons_names_the_legs_this_call_saw(
    monkeypatch,
) -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    real = OrderPath.send

    def send(self, name):
        if name == second:
            raise RuntimeError("the path broke")
        return real(self, name)

    def before(number: int, op: str, key: str) -> None:
        if op != "create" and key.startswith("journal/") and "abandoned" in made.kinds():
            raise StoreUnavailable(key, "the store's own text")

    monkeypatch.setattr(OrderPath, "send", send)
    made.store.before = before
    result = made.send_attempt()
    reason = result.halt.reason
    assert f"stand so: {first} acknowledged, {second} not known" in reason
    assert "the store's own text" not in reason
    # The second leg was abandoned before the journal went, so nothing says it was not.
    assert "not journaled abandoned" not in reason
    assert made.kinds()[-1] == "abandoned"


@pytest.mark.parametrize("leg", [0, 1], ids=["first", "second"])
@pytest.mark.parametrize(
    ("fault", "state"),
    [(OrderFault.REJECT, "Rejected"), (OrderFault.LOSE_ANSWER, "InDoubt")],
    ids=["rejected", "in-doubt"],
)
def test_a_leg_the_broker_rejects_or_leaves_in_doubt_ends_the_plan(leg, fault, state) -> None:
    planned = [order_id(CHAN, close_of(FULL), 1), order_id(CHAN, close_of(FULL), 2)]
    made = world(faults=Faults(orders={planned[leg]: fault}))
    made.ready(close_of(FULL), PAIR)
    result = made.send_attempt()
    assert result.halt is not None and f"the order {planned[leg]} is {state}" in result.halt.reason
    # The broker placed each leg before the one that failed, that one too where it took it
    # and its answer was lost, and none after it. A rejected leg is never placed.
    taken = [planned[leg]] if fault is OrderFault.LOSE_ANSWER else []
    assert made.submits() == planned[:leg] + taken
    states = made.states()
    assert states[planned[leg]] == state
    if leg == 0:
        assert states[planned[1]] == "Abandoned"


def test_a_kill_switch_pulled_before_the_second_leg_halts_with_the_first_at_the_broker() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)

    def before(number: int, op: str, key: str) -> None:
        if op == "create" and key.startswith("journal/") and made.kinds()[-1:] == ["ack"]:
            made.store.put(kill_switch_key("sim-1"), b"pulled")

    made.store.before = before
    result = made.send_attempt()
    assert result.halt is not None and "kill switch stands" in result.halt.reason
    assert made.submits() == [first]
    assert made.states()[second] == "Abandoned"


def test_a_portfolio_result_that_names_no_forecast_halts_before_any_plan() -> None:
    made = world()
    made.ready(close_of(FULL), PAIR)
    made.targets(made.at, PAIR, named=False)
    result = made.send_attempt()
    assert result.halt is not None and "names no" in result.halt.reason
    assert "decision" not in made.kinds(made.before)


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


def test_a_leg_the_broker_took_whose_ack_the_journal_lost_is_never_called_unsent(
    monkeypatch,
) -> None:
    """The journal goes once the broker has the second leg, so only the broker knows its fate."""
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)

    def refuse(intent) -> None:
        if intent.client_order_id == second:
            made.store.refuse(("*", "journal/"))

    watch_submits(monkeypatch, after=refuse)
    result = made.send_attempt()
    reason = result.halt.reason
    assert made.submits() == [first, second]
    assert "none of them was sent" not in reason
    assert f"stand so: {first} acknowledged, {second} not known, since the journal" in reason


def test_an_outage_the_path_raises_stops_every_write_and_pages_the_legs_once(
    monkeypatch,
) -> None:
    """The path raises an `Outage` before the broker's call, so the leg was not sent."""
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    real = OrderPath.send
    tried: list[str] = []

    def send(self, name):
        if name == second:
            made.store.refuse(("*", "journal/"))
            raise self._outage("the journal could not be read: StoreUnavailable")
        return real(self, name)

    def abandon(self, name, reason):
        tried.append(name)
        raise AssertionError("no abandon is tried once the store did not answer")

    monkeypatch.setattr(OrderPath, "send", send)
    monkeypatch.setattr(OrderPath, "abandon", abandon)
    alarms = len(made.alarms.sent)
    result = made.send_attempt()
    reason = result.halt.reason
    assert tried == [] and made.submits() == [first]
    assert f"{second} was not journaled abandoned, since the journal could not be read" in reason
    assert f"stand so: {first} acknowledged, {second} not sent, since the journal" in reason
    # The outage's own page and the one naming every leg.
    sent = made.alarms.sent[alarms:]
    assert len(sent) == 2 and "The plan's orders stand so" in sent[1]


def test_a_first_leg_whose_call_failed_with_the_journal_gone_leaves_the_second_unsent(
    monkeypatch,
) -> None:
    """The second leg was never tried, so it alone is named not sent, and never submitted."""
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)

    def send(self, name):
        made.store.refuse(("*", "journal/"))
        raise RuntimeError("the path broke")

    monkeypatch.setattr(OrderPath, "send", send)
    result = made.send_attempt()
    reason = result.halt.reason
    assert made.submits() == []
    assert f"{second} was not journaled abandoned, since the journal could not be read" in reason
    assert f"stand so: {first} not known, {second} not sent, since the journal" in reason


@pytest.mark.parametrize("at", ["hold", "abandon"], ids=["at the hold", "at the first abandon"])
def test_a_refused_plan_stops_its_writes_at_the_first_outage(monkeypatch, at: str) -> None:
    """A store that stopped answering paged once, and each later write would page it again."""
    made = world()
    made.ready(close_of(FULL), PAIR)
    names = [f"ith-{CHAN}-x-{n}" for n in (1, 2, 3)]
    tried: list[str] = []

    def outage(self) -> Halt:
        return self._outage("the journal could not be read: StoreUnavailable")

    def hold(self, client_order_ids):
        if at == "hold":
            raise outage(self)
        return ()

    def abandon(self, name, reason):
        tried.append(name)
        raise outage(self)

    monkeypatch.setattr(OrderPath, "ask", lambda self, n: (Asked(n[0], None, "refused"),))
    monkeypatch.setattr(OrderPath, "hold", hold)
    monkeypatch.setattr(OrderPath, "abandon", abandon)
    stage = made.sender()
    path = made.account.path(made.broker(), lambda: None)
    alarms = len(made.alarms.sent)
    with pytest.raises(Halt) as halted:
        stage._asked(path, names, lambda: None)
    assert tried == ([] if at == "hold" else names[:1])
    assert halted.value.reason.count("the journal could not be read") == 1
    # The outage's own page and the one that says nothing is sent.
    sent = made.alarms.sent[alarms:]
    assert len(sent) == 2 and sent[1].startswith("account sim-1: nothing is sent: refused")


def test_a_journal_whose_fold_refuses_an_entry_names_its_cause_and_every_leg_in_order(
    monkeypatch,
) -> None:
    """The journal answers and cannot be read, which is no outage, and nothing is abandoned."""
    made = world()
    made.ready(close_of(FULL), PAIR)
    first, second = gates_names(made)
    real = Account.view

    def send(self, name):
        monkeypatch.setattr(
            Account, "view", lambda *a: (_ for _ in ()).throw(Halt("entry 9 is refused"))
        )
        raise RuntimeError("the path broke")

    monkeypatch.setattr(OrderPath, "send", send)
    result = made.send_attempt()
    monkeypatch.setattr(Account, "view", real)
    reason = result.halt.reason
    assert "entry 9 is refused" in reason and "did not answer" not in reason
    assert f"stand so: {first} not known, {second} not sent, since the journal" in reason
    assert made.submits() == []
