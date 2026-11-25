"""What an order is checked against, what it says, and the broker reads beside it.

Each test's number is the acceptance item of #143 it shows. Every input is
absent, stale, empty, malformed, of another decision, of other bytes and
unreadable, beside the same world unchanged, which sends.
"""

from __future__ import annotations

import traceback
from asyncio import CancelledError
from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    CONSTRAINTS_OUTPUT,
    DATA_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SNAPSHOT_OUTPUT,
    VALUATION_OUTPUT,
    BlobMissing,
    BlobRef,
    BrokerUnavailable,
    Constraints,
    Halt,
    HaltRecord,
    OrderState,
    Outcome,
    Snapshot,
    StopState,
    StoreUnavailable,
    Valuation,
    account_halts_prefix,
    caps_key,
    kill_switch_key,
    loss_stop_key,
    off_switch_key,
    result_key,
    run_halt_key,
    sha256_of,
    strategy_halts_prefix,
)
from ithildinruntime.core import journal_entry_key as entry_key
from ithildinruntime.journal import (
    InDoubtEntry,
    Journal,
    OpeningEntry,
    ReadEntry,
    VerdictEntry,
)
from ithildinruntime.risk import Gate
from ithildinruntime.runtime.loop.receive import _said as _loop_said
from tests.support import gates
from tests.support.fake_broker import raising, says
from tests.support.order_world import (
    ACCOUNT,
    AT,
    CASH,
    DEPLOYMENT,
    RUN,
    Wired,
    World,
    caps_blob,
    halted,
    last,
    order,
)

ID = order(1, 2).client_order_id
DAY = timedelta(days=1)
HALTED = HaltRecord("upstream stopped", None, (), (), ())


# 5. The share step


@pytest.mark.parametrize(("qty", "step"), [("2", "1"), ("2.000", "1"), ("1.5", "0.1"), ("-3", "1")])
def test_5_an_order_on_the_share_step_sends(qty: str, step: str) -> None:
    world = World(orders=(order(1, qty),), caps=gates.caps(share_step=Decimal(step)))
    assert world.send() is OrderState.ACKNOWLEDGED


@pytest.mark.parametrize(
    ("qty", "step"),
    [("1.5", "1"), ("2.000000001", "1"), ("-0.5", "1"), ("2.05", "0.1"), ("1e-9", "0.00000001")],
)
def test_5_an_order_off_the_share_step_halts_with_nothing_journaled(qty: str, step: str) -> None:
    world = World(orders=(order(1, qty),), caps=gates.caps(share_step=Decimal(step)))
    halted(world, "no multiple of the account's share step")
    assert world.kinds() == []


def test_5_a_step_rewritten_coarser_after_the_plan_halts() -> None:
    world = World(orders=(order(1, "1.5"),), caps=gates.caps(share_step=Decimal("0.1")))
    world.rewrite_caps(share_step=Decimal(1))
    halted(world, "no multiple")
    assert world.kinds() == []


# 5. Each input


Fault = Callable[[World], None]


def drop(key: str) -> Fault:
    return lambda world: world.store.drop(key)


def put(key: str, data: bytes) -> Fault:
    return lambda world: world.store.put(key, data)


def unreadable(op: str, key: str) -> Fault:
    return lambda world: world.store.refuse((op, key))


CAPS: dict[str, tuple[Fault, str]] = {
    "absent": (drop(caps_key(ACCOUNT)), "caps.json is missing"),
    "empty": (put(caps_key(ACCOUNT), b""), "not JSON"),
    "malformed": (put(caps_key(ACCOUNT), b"[]"), "not a JSON object"),
    "another account's": (
        put(caps_key(ACCOUNT), caps_blob(gates.caps(), "paper-2")),
        "does not name the account 'paper-1'",
    ),
    "unreadable": (unreadable("get", caps_key(ACCOUNT)), "S3 did not answer a read of accounts"),
    "stale": (
        lambda world: world.rewrite_caps(stress_through=date(2026, 10, 30), stress_max_age_days=1),
        "past the 1 allowed",
    ),
    "no step": (
        put(caps_key(ACCOUNT), caps_blob(gates.caps()).replace(b"share_step", b"step")),
        "holds no share_step",
    ),
}


@pytest.mark.parametrize("fault", CAPS)
def test_5_hard_caps_that_cannot_be_used_stop_the_order(fault: str) -> None:
    assert World().send() is OrderState.ACKNOWLEDGED
    spoil, why = CAPS[fault]
    world = World()
    spoil(world)
    halted(world, why)
    # Every fault but a stale one is found before anything is journaled.
    assert world.kinds() == (["intent", "abandoned"] if fault == "stale" else [])


FILES: dict[str, tuple[str, str, type]] = {
    "snapshot": (DATA_STAGE, SNAPSHOT_OUTPUT, Snapshot),
    "constraints": (RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints),
    "valuation": (RECOVER_STAGE, VALUATION_OUTPUT, Valuation),
}


def entries(world: World, name: str) -> tuple:
    return {
        "snapshot": (world.snapshot,),
        "constraints": world.constraints,
        "valuation": (world.valuation,),
    }[name]


def refiled(world: World, name: str, made: tuple | None = None, **how: object) -> None:
    """Store a file of the decision again, around every rule, with what a test changes."""
    stage, output, kind = FILES[name]
    found = entries(world, name) if made is None else made
    world.file(stage, output, kind, found, **how)  # type: ignore[arg-type]


def stage_key(name: str) -> str:
    return result_key(DEPLOYMENT, RUN, AT, FILES[name][0])


FILE_FAULTS: dict[str, tuple[Callable[[World, str], None], str]] = {
    "result absent": (lambda w, n: w.store.drop(stage_key(n)), "is not stored"),
    "result empty": (lambda w, n: w.store.put(stage_key(n), b""), "holds no decision result"),
    "result malformed": (lambda w, n: w.store.put(stage_key(n), b"{}"), "holds no decision result"),
    "result unreadable": (
        lambda w, n: w.store.refuse(("get", stage_key(n))),
        "S3 did not answer a read",
    ),
    "result a halt": (lambda w, n: refiled(w, n, halt=HALTED), "holds a halt: upstream stopped"),
    "result of another decision": (
        lambda w, n: w.result(FILES[n][0], (w.refs[n],), at=AT - DAY),
        "so it is stale",
    ),
    "listed twice": (lambda w, n: refiled(w, n, listed=2), "2 times, not once"),
    "not listed": (lambda w, n: refiled(w, n, listed=0), "0 times, not once"),
    "file absent": (lambda w, n: w.store.drop(w.refs[n].key), "is missing"),
    "file of other bytes": (
        lambda w, n: w.store.put(w.refs[n].key, w.store.blobs[w.refs[n].key] + b" "),
        "has SHA-256",
    ),
    "file unreadable": (
        lambda w, n: w.store.refuse(("get", w.refs[n].key)),
        "S3 did not answer a read",
    ),
    "file empty": (lambda w, n: refiled(w, n, ()), 'no "trade nothing" mark'),
    "file that trades nothing": (
        lambda w, n: refiled(w, n, (), trade_nothing=True),
        "holds (0 entries, not one|no constraints)",
    ),
    "file of another decision": (lambda w, n: refiled(w, n, at=AT - DAY), "so it is stale"),
    "file of other hashes": (
        lambda w, n: _other_hashes(w, n),
        "names the configuration hashes",
    ),
    "file rewritten since the plan": (
        lambda w, n: refiled(w, n, _changed(w, n)),
        "planned on runs/d-7",
    ),
    "file of two entries": (
        lambda w, n: refiled(w, n, entries(w, n) * 2),
        "holds 2 (entries, not one|constraints of 'chan')|planned on",
    ),
}


def _other_hashes(world: World, name: str) -> None:
    ref = world.refs[name]
    refiled(world, name, hashes={gates.CHAN: "d" * 64})
    # The result is stored again under its own hashes, and lists the file of the others.
    made = BlobRef(ref.key, _sha(world, ref.key))
    world.result(FILES[name][0], (made,))


def _sha(world: World, key: str) -> str:
    return sha256_of(world.store.blobs[key])


def _changed(world: World, name: str) -> tuple:
    """The same file with one figure changed, as a stage rerun into the folder would leave it."""
    return {
        "snapshot": (replace(world.snapshot, provisional=True),),
        "constraints": (gates.constraints(max_open_orders=99),),
        "valuation": (gates.valuation(CASH, replayed_through=0),),
    }[name]


@pytest.mark.parametrize("name", FILES)
@pytest.mark.parametrize("fault", FILE_FAULTS)
def test_5_a_file_that_cannot_be_used_ends_the_order_abandoned(fault: str, name: str) -> None:
    spoil, why = FILE_FAULTS[fault]
    world = World()
    spoil(world, name)
    halted(world, why)
    assert world.kinds() == ["intent", "abandoned"]


@pytest.mark.parametrize("name", FILES)
def test_5_a_plan_that_names_no_file_or_another_ends_the_order_abandoned(name: str) -> None:
    world = World(inputs={name: None})
    halted(world, f"entry 3, a decision, names no {name}")
    assert world.kinds() == ["intent", "abandoned"]
    right = World().refs[name]
    for other in (BlobRef(right.key, "e" * 64), BlobRef("runs/other.json", right.sha256)):
        world = World(inputs={name: other})
        halted(world, "planned on")
        assert world.kinds() == ["intent", "abandoned"]


def test_5_a_snapshot_whose_own_decision_is_another_ends_the_order_abandoned() -> None:
    made = gates.snapshot({gates.GLD: 1, gates.USO: 1}, at=AT - DAY, day=gates.PRIOR)
    halted(World(snapshot=made), "holds a snapshot of")


def test_5_a_stale_valuation_or_price_ends_the_order_abandoned() -> None:
    stale = gates.valuation(CASH, replayed_through=1, session=date(2026, 10, 30))
    halted(World(valuation=stale), "the valuation is of 2026-10-30")
    old = gates.snapshot({gates.GLD: 1, gates.USO: 1}, day=gates.PRIOR)
    halted(World(snapshot=old), "has no mark")
    halted(World(valuation=gates.valuation(0, replayed_through=1)), "not above zero")


def test_5_the_constraints_read_are_the_orders_own_strategys() -> None:
    other = gates.constraints(
        strategy=gates.OTHER,
        max_position={gates.GLD: gates.d(0)},
        max_order_qty={gates.GLD: gates.d(0)},
    )
    assert World(constraints=(other, gates.constraints())).send() is OrderState.ACKNOWLEDGED
    halted(World(constraints=(other,)), "holds 0 constraints of 'chan', not one")


def test_5_a_wrong_run_reads_its_own_files_and_never_a_callers() -> None:
    """A `decision` entry of a run nothing is stored for halts on its own absent results."""
    assert World().send() is OrderState.ACKNOWLEDGED
    world = World(plan=False)
    world.record(OpeningEntry(ACCOUNT, CASH))
    world.record(ReadEntry("execution/send", AT, BlobRef("r", "a" * 64), AT))
    world.decide(world.orders, run="2026-11-03")
    reason = halted(world, "data/result.json is not stored")
    assert "runs/d-7/2026-11-03/" in reason


@pytest.mark.parametrize("read", [0, 1])
def test_5_a_wrong_runs_halt_is_the_one_that_stops_its_order(read: int) -> None:
    """The run's halt is read under the run its `decision` names, whatever the date says."""
    at = control_reads("run halt")[read]
    for run, stops in ((RUN, True), ("2026-11-03", False)):
        world = World()
        key = run_halt_key(DEPLOYMENT, run)
        world.store.before = lambda n, op, k, w=world, put=key: (
            w.store.put(put, b"{}") if n == at else None
        )
        if stops:
            halted(world, "run halt stands")
        else:
            assert world.send() is OrderState.ACKNOWLEDGED


def test_5_two_constraints_of_one_strategy_halt() -> None:
    world = World(constraints=(gates.constraints(), gates.constraints()))
    halted(world, "holds 2 constraints of 'chan', not one")


def test_5_a_gate_that_raises_ends_the_order_abandoned_and_raises() -> None:
    class Broken(Gate):
        def check(self, *given):  # type: ignore[no-untyped-def]
            raise ZeroDivisionError("a fault")

    world = World(gate=Broken())
    with pytest.raises(ZeroDivisionError):
        world.send()
    assert world.state() is OrderState.ABANDONED and world.submits == 0


# 5. The journal


def test_5_a_journal_that_cannot_be_used_stops_the_order() -> None:
    absent = World(plan=False)
    halted(absent, "is in no decision the journal holds")
    halted(World(store=_refusing("list", "journal/")), "the journal could not be read")
    world = World()
    world.store.put(entry_key(ACCOUNT, 3), b"{}")
    halted(world, "entry 3 is not a canonical journal entry")
    world = World()
    world.store.drop(entry_key(ACCOUNT, 2))
    halted(world, "entry 2 is missing")
    for name, why in (
        ("made-up", "not an order id"),
        (order(2, 1).client_order_id, "in no decision"),
        (order(1, 2, at=AT - DAY).client_order_id, "in no decision"),
    ):
        world = World()
        halted(world, why, lambda w=world, n=name: w.path().send(n))


def _refusing(op: str, key: str):
    store = Wired()
    store.refuse((op, key))
    return store


# 5. The controls


CONTROLS: dict[str, tuple[str, str, bool]] = {
    "kill switch": ("get", kill_switch_key(ACCOUNT), True),
    "account halt": ("list", account_halts_prefix(ACCOUNT), True),
    "run halt": ("get", run_halt_key(DEPLOYMENT, RUN), True),
    "this decision's send result": (
        "get",
        result_key(DEPLOYMENT, RUN, AT, "execution/send"),
        True,
    ),
    "loss stop": ("get", loss_stop_key(ACCOUNT), True),
    "strategy off switch": ("get", off_switch_key(ACCOUNT, gates.CHAN), False),
    "strategy halt": ("list", strategy_halts_prefix(ACCOUNT, gates.CHAN), False),
}


def control_reads(name: str) -> list[int]:
    op, key, _ = CONTROLS[name]
    world = World()
    world.send()
    found = [n for n, call in enumerate(world.store.calls, 1) if call == (op, key)]
    assert len(found) == 2
    return found


# A folder that lists nothing at all answers with an empty list, so `None` is no answer.
@pytest.mark.parametrize(
    ("name", "error"),
    [
        *((name, error) for name in CONTROLS for error in ("StoreUnavailable", "PermissionError")),
        *(
            (name, error)
            for name, (op, _, _) in CONTROLS.items()
            for error in ("None", "BlobMissing")
            if op == "list"
        ),
    ],
)
@pytest.mark.parametrize("read", [0, 1])
def test_5_a_control_that_cannot_be_read_halts(name: str, read: int, error: str) -> None:
    op, key, _ = CONTROLS[name]
    world = World()
    at = control_reads(name)[read]
    plain = world.store.list

    def before(number: int, o: str, k: str) -> None:
        if (o, k) == (op, key) and number >= at:
            if error == "PermissionError":
                raise PermissionError("the store's own text")
            if error == "StoreUnavailable":
                raise StoreUnavailable(k, "the store's own text")

    world.store.before = before
    if error in ("None", "BlobMissing"):

        def listed(prefix: str, after: str | None):  # type: ignore[no-untyped-def]
            found = plain(prefix, after)
            if prefix != key or len(world.store.calls) < at:
                return found
            if error == "BlobMissing":
                raise BlobMissing(prefix, "no such folder")
            return None

        world.store.list = listed  # type: ignore[method-assign]
    shown = "NoneType" if error == "None" else error
    reason = halted(world, f"{name} unreadable: {shown}")
    assert "own text" not in reason + str(world.records()) + str(world.alarms.sent)
    late = read == 1
    assert ("sending" in world.kinds()) == late
    refused = name == "loss stop" and not late
    assert world.state() is (OrderState.REFUSED if refused else OrderState.ABANDONED)


@pytest.mark.parametrize("name", CONTROLS)
def test_5_a_control_refused_once_is_read_again(name: str) -> None:
    op, key, _ = CONTROLS[name]
    for at in control_reads(name):
        world = World()

        def before(number: int, o: str, k: str, at: int = at) -> None:
            if number == at:
                raise StoreUnavailable(k, "once")

        world.store.before = before
        assert world.send() is OrderState.ACKNOWLEDGED


def test_5_controls_of_another_account_run_and_strategy_stop_nothing() -> None:
    world = World()
    for key in (
        kill_switch_key("paper-2"),
        account_halts_prefix("paper-2") + "a.json",
        run_halt_key(DEPLOYMENT, "2026-11-02"),
        run_halt_key("other", RUN),
        loss_stop_key("paper-2"),
        off_switch_key(ACCOUNT, gates.OTHER),
        strategy_halts_prefix(ACCOUNT, gates.OTHER) + "a.json",
        result_key(DEPLOYMENT, RUN, AT - DAY, "execution/send"),
        result_key(DEPLOYMENT, RUN, AT, "execution/recover"),
    ):
        if key not in world.store.blobs:
            world.store.put(key, b"{}")
    assert world.send() is OrderState.ACKNOWLEDGED


def test_5_a_control_names_the_halt_it_found() -> None:
    world = World()
    world.store.put(account_halts_prefix(ACCOUNT) + "execution-cancel-1-1.json", b"{}")
    assert halted(world, "account") == "account halt stands: execution-cancel-1-1.json"
    off = World()
    off.store.put(strategy_halts_prefix(ACCOUNT, gates.CHAN) + "a-b-1-1.json", b"{}")
    assert off.send() is OrderState.ABANDONED
    assert last(off).reason == "strategy off: strategy halt stands: a-b-1-1.json"


@pytest.mark.parametrize("read", [0, 1])
def test_5_a_strategy_that_is_off_is_abandoned_with_no_halt(read: int) -> None:
    world = World()
    at = control_reads("strategy off switch")[read]
    key = off_switch_key(ACCOUNT, gates.CHAN)
    world.store.before = lambda n, op, k: world.store.put(key, b"") if n == at else None
    assert world.send() is OrderState.ABANDONED
    assert world.kinds() == ["intent", *["verdict", "sending"][: 2 * read], "abandoned"]
    assert world.submits == 0 and world.alarms.sent == []


def test_5_a_strategy_off_whose_abandoned_is_not_stored_halts() -> None:
    world = World()
    world.store.put(off_switch_key(ACCOUNT, gates.CHAN), b"")
    world.store.refuse(("create", entry_key(ACCOUNT, 5)))
    halted(world, "strategy off: .* was not stored: StoreUnavailable")


def test_5_an_off_switch_found_at_the_submit_with_the_store_down_is_kept() -> None:
    world = World()
    account = world.account()
    key = off_switch_key(ACCOUNT, gates.CHAN)
    at = control_reads("strategy off switch")[1]

    def before(number: int, op: str, k: str) -> None:
        if number == at:
            world.store.put(key, b"")
        if number > at and op == "create":
            raise StoreUnavailable(k, "down")

    world.store.before = before
    halted(world, "did not take the abandoned", lambda: world.send(account=account))
    assert account.unstored() == (f"the abandoned of {ID}, which was not sent",)
    world.store.before = lambda n, op, k: None
    account.settle(world.report)
    assert world.state() is OrderState.ABANDONED


# 9. The day's loss


STEP = Decimal("0.0001")


def losing(loss: Decimal, **changes: object) -> World:
    return World(
        valuation=gates.valuation(CASH + loss, replayed_through=1),
        constraints=(gates.constraints(loss_stop=gates.d(100)),),
        **changes,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize("loss", [100 - STEP, Decimal(100)])
def test_9_a_loss_inside_its_bound_sends(loss: Decimal) -> None:
    world = losing(loss)
    (reading,) = world.path().loss(DEPLOYMENT, RUN, AT)
    assert (reading.loss, reading.bound, reading.past) == (loss, Decimal(100), False)
    assert world.kinds() == [] and world.alarms.sent == []
    assert world.send() is OrderState.ACKNOWLEDGED


def test_9_a_loss_past_its_bound_halts_there_and_on_a_verdict() -> None:
    world = losing(100 + STEP)
    said = (
        "the day's loss of 100.0001 is past its stop of 100.0000, measured from the valuation of "
        "2026-11-02"
    )
    alarm = f"account {ACCOUNT}: {said}"
    halted(world, said, lambda: world.path().loss(DEPLOYMENT, RUN, AT))
    assert world.alarms.sent == [alarm] and world.kinds() == []
    halted(world, said)
    assert world.alarms.sent == [alarm, alarm]
    assert world.kinds() == ["intent", "verdict"]
    assert world.state() is OrderState.REFUSED


def test_9_a_refusal_for_another_limit_that_read_a_loss_past_its_bound_halts_too() -> None:
    world = losing(101 * Decimal(1), caps=gates.caps(max_gross=gates.d(1)))
    halted(world, "the day's loss of 101")
    (entry,) = [r for r in world.records() if isinstance(r, VerdictEntry)]
    assert entry.verdict.limit.value == "max_gross"


def test_9_an_approval_that_read_a_loss_past_its_bound_is_never_sent() -> None:
    """The gate never answers so, and the path holds the halt whatever the gate answers."""

    class Yes(Gate):
        def check(self, *given):  # type: ignore[no-untyped-def]
            said = super().check(*given)
            return replace(said, approved=True, limit=None, value=None, bound=None)

    world = losing(101 * Decimal(1), gate=Yes())
    halted(world, "the day's loss of 101")
    assert world.state() is OrderState.ABANDONED


def test_9_every_strategys_stop_is_read() -> None:
    tight = gates.constraints(strategy=gates.OTHER, loss_stop=gates.d(50))
    world = losing(Decimal(60))
    world.constraints = (gates.constraints(loss_stop=gates.d(100)), tight)
    refiled(world, "constraints")
    halted(world, "past its stop of 50", lambda: world.path().loss(DEPLOYMENT, RUN, AT))
    one = losing(Decimal(40))
    one.constraints = (gates.constraints(loss_stop=gates.d(100)), tight)
    refiled(one, "constraints")
    assert [r.bound for r in one.path().loss(DEPLOYMENT, RUN, AT)] == [100, 50]


def test_9_a_stop_that_stands_or_cannot_be_read_halts_at_each_read() -> None:
    for spoil, why in (
        (put(loss_stop_key(ACCOUNT), b"{}"), "loss stop stands"),
        (unreadable("get", loss_stop_key(ACCOUNT)), "loss stop unreadable: StoreUnavailable"),
    ):
        world = World()
        spoil(world)
        halted(world, why, lambda world=world: world.path().loss(DEPLOYMENT, RUN, AT))
        halted(world, why)
        (entry,) = [r for r in world.records() if isinstance(r, VerdictEntry)]
        assert entry.verdict.limit.value == "stop"
        assert entry.stop is (StopState.FIRED if "stands" in why else StopState.UNREADABLE)


def test_9_a_loss_past_its_bound_is_said_even_where_the_stop_stands() -> None:
    world = losing(Decimal(101))
    world.store.put(loss_stop_key(ACCOUNT), b"{}")
    halted(world, "the day's loss", lambda: world.path().loss(DEPLOYMENT, RUN, AT))


# Two strategies' constraints are read in turn, and a file is compared with no plan.
@pytest.mark.parametrize(
    ("fault", "name"),
    [
        (fault, name)
        for fault in FILE_FAULTS
        for name in FILES
        if fault != "file rewritten since the plan"
        and (fault, name) != ("file of two entries", "constraints")
    ],
)
def test_9_the_loss_halts_on_a_file_it_cannot_use(fault: str, name: str) -> None:
    spoil, why = FILE_FAULTS[fault]
    world = World()
    spoil(world, name)
    halted(world, why, lambda: world.path().loss(DEPLOYMENT, RUN, AT))
    assert world.kinds() == []


@pytest.mark.parametrize("fault", CAPS)
def test_9_the_loss_halts_on_hard_caps_it_cannot_use(fault: str) -> None:
    spoil, why = CAPS[fault]
    world = World()
    spoil(world)
    halted(world, why, lambda: world.path().loss(DEPLOYMENT, RUN, AT))


def test_9_a_wrong_run_or_deployment_halts() -> None:
    for named in (("other", RUN, AT), (DEPLOYMENT, "2026-11-02", AT), (DEPLOYMENT, RUN, AT - DAY)):
        world = World()
        halted(world, "is not stored", lambda w=world, n=named: w.path().loss(*n))


# 10. The gate's answer, rebuilt


PAIR = (order(1, 2), order(2, -3, gates.USO))


def test_10_the_gates_answer_rebuilds_after_the_caps_are_rewritten_and_deleted() -> None:
    world = World(orders=PAIR, caps=gates.caps(max_gross=gates.d(50)))
    account = world.account()
    for place in (1, 2):
        assert world.send(place, account) is OrderState.ACKNOWLEDGED
    assert world.rebuilt() == 2
    world.rewrite_caps(max_gross=gates.d(1), stress_move=gates.d("0.5"))
    assert world.rebuilt() == 2
    world.store.drop(caps_key(ACCOUNT))
    assert world.rebuilt() == 2


class AsksAnother(Gate):
    """A gate asked about another size than the order planned."""

    def __init__(self, qty: int, *given: object) -> None:
        super().__init__(*given)
        self.qty = Decimal(qty)

    def check(self, intent, *rest):  # type: ignore[no-untyped-def]
        return super().check(replace(intent, qty=self.qty), *rest)


def test_10_an_order_asked_about_at_another_size_does_not_rebuild() -> None:
    adding = World(gate=AsksAnother(1))
    assert adding.send() is OrderState.ACKNOWLEDGED
    with pytest.raises(AssertionError):
        adding.rebuilt()
    # A sale of 4 that only reduces is past the 3 an order may hold, and one of 1 is not.
    limits = dict.fromkeys((gates.GLD, gates.USO), gates.d(3))
    tight = (gates.constraints(max_order_qty=limits, max_position=limits),)
    held: dict = {"orders": (order(1, -4),), "position": Decimal(5), "constraints": tight}
    right = World(**held)
    assert right.send() is OrderState.REFUSED
    wrong = World(**held, gate=AsksAnother(-1))
    assert wrong.send() is OrderState.ACKNOWLEDGED
    with pytest.raises(AssertionError):
        wrong.rebuilt()


def test_10_an_order_that_only_reduces_rebuilds() -> None:
    world = World(orders=(order(1, -4),), position=Decimal(5))
    assert world.send() is OrderState.ACKNOWLEDGED
    verdict = [r for r in world.records() if isinstance(r, VerdictEntry)][-1]
    assert not verdict.verdict.adding
    assert world.rebuilt() == 1


# 11. Credentials, and the reads


KEY, NUMBER = "PKFAKEKEY0123456789", "PA3FAKE98765"


def vendor(kind: type[BaseException] = RuntimeError) -> BaseException:
    """An error as a broker's library raises one, with a cause and a context that say too much."""
    try:
        try:
            raise ValueError(f"header APCA-API-KEY-ID: {KEY}")
        except ValueError as cause:
            raise kind(f"account {NUMBER} of {KEY}") from cause
    except BaseException as error:
        return error


def leaks(*texts: object) -> list[str]:
    return [str(t) for t in texts for secret in (KEY, NUMBER) if secret in str(t)]


def everything_said(world: World) -> list[str]:
    """Every alarm, and every field of every entry but `raw`."""
    bodies = [{k: v for k, v in e.body.items() if k != "raw"} for e in world.reader().read()]
    return [*world.alarms.sent, *map(str, bodies)]


def told(error: BaseException | None) -> list[str]:
    """Everything an error, its causes and its contexts would print."""
    if error is None:
        return []
    said = traceback.format_exception(error)
    while error is not None:
        said += [repr(error), str(error)]
        error = error.__cause__ or error.__context__
    return said


@pytest.mark.parametrize("store_down", [False, True])
@pytest.mark.parametrize(
    "kind",
    [RuntimeError, SystemExit, CancelledError, Halt, BrokerUnavailable, KeyboardInterrupt],
)
def test_11_a_submit_that_raises_says_nothing_of_the_vendors(
    kind: type[BaseException], store_down: bool, capfd, caplog
) -> None:
    caplog.set_level(0)
    world = World()
    error: BaseException
    if kind is BrokerUnavailable:
        error = BrokerUnavailable(f"{NUMBER} ", vendor())
    elif kind is Halt:
        error = Halt("the adapter's own halt")
        error.__cause__ = vendor()
    else:
        error = vendor(kind)
    world.broker.answer = raising(error)
    if store_down:
        world.store.refuse(("create", entry_key(ACCOUNT, 7)))
    raised: BaseException | None = None
    try:
        world.send()
    except (Halt, KeyboardInterrupt) as error:
        raised = error
    assert (raised is not None) == (store_down or kind in (Halt, KeyboardInterrupt))
    captured = capfd.readouterr()
    said = [*everything_said(world), *told(raised), captured.out, captured.err, caplog.text]
    assert not leaks(*said)


@pytest.mark.parametrize("outcome", list(Outcome))
def test_11_a_brokers_bytes_reach_raw_alone(outcome: Outcome, capfd, caplog) -> None:
    caplog.set_level(0)
    world = World()
    raw = f"{KEY} {NUMBER}".encode()
    world.broker.answer = says(outcome, f"id {NUMBER}", raw, client_order_id=KEY)
    assert world.send() is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry) and found.raw == raw
    captured = capfd.readouterr()
    assert not leaks(*everything_said(world), captured.out, captured.err, caplog.text)


class Talks:
    """An answer whose every form says too much."""

    def __repr__(self) -> str:
        return f"{KEY} of {NUMBER}"

    __str__ = __repr__


def test_11_an_answer_of_no_kind_is_never_printed(capfd, caplog) -> None:
    caplog.set_level(0)
    world = World()
    world.broker.answer = lambda intent, number: Talks()
    assert world.send() is OrderState.IN_DOUBT
    world.broker.reads["positions"] = Talks
    with pytest.raises(BrokerUnavailable, match="failed with Talks") as raised:
        world.path().positions()
    captured = capfd.readouterr()
    said = [*everything_said(world), *told(raised.value), captured.out, captured.err, caplog.text]
    assert not leaks(*said)


BAD_ID = f"acct {NUMBER} {KEY}"


@pytest.mark.parametrize("store", ["answers", "lost answer", "down", "another writer"])
def test_11_an_id_the_journal_refuses_is_in_no_alarm_and_no_halt(store: str) -> None:
    world = World()
    account = world.account()

    def answer(intent: object, number: int) -> object:
        if store == "another writer":
            world.record(InDoubtEntry(ID, "taken over", None, False, AT), other)
        return says(Outcome.ACCEPTED, BAD_ID)(intent, number)  # type: ignore[arg-type]

    def after(number: int, op: str, key: str) -> None:
        if store == "lost answer" and key == entry_key(ACCOUNT, 7) and op == "create":
            world.store.after = lambda n, o, k: None
            raise StoreUnavailable(key, "timeout")

    other = Journal(world.store, ACCOUNT, "2" * 32)
    world.broker.answer, world.store.after = answer, after
    if store == "down":
        world.store.refuse(("create", entry_key(ACCOUNT, 7)))
    raised = None
    try:
        assert world.send(account=account) is OrderState.IN_DOUBT
    except Halt as halt:
        raised = halt
    assert (raised is not None) == (store == "down")
    world.store.before = lambda n, op, key: None
    account.settle(world.report)
    assert account.unstored() == () and world.state() is OrderState.IN_DOUBT
    assert last(world).raw == b"{}"
    assert not leaks(*everything_said(world), *told(raised))


def test_11_a_key_shaped_word_is_no_type_of_error() -> None:
    """`BrokerUnavailable` takes an error or its type, so no word an adapter writes is shown."""
    world = World()

    def raises() -> object:
        raise BrokerUnavailable("positions", KEY)  # type: ignore[arg-type]

    world.broker.reads["positions"] = raises
    with pytest.raises(BrokerUnavailable, match="failed with str") as raised:
        world.path().positions()
    assert not leaks(*told(raised.value))
    world.broker.answer = raising(type(f"APIError account {NUMBER}", (Exception,), {})())
    assert world.send() is OrderState.IN_DOUBT
    assert not leaks(*everything_said(world))


def test_11_a_brokers_error_reaches_no_failure_of_the_receive_loop() -> None:
    """What a handler raises is what the loop alarms on, stores and prints as its worker ends."""
    world = World()

    def raises() -> object:
        raise vendor()

    world.broker.reads["positions"] = raises
    try:
        world.path().positions()
    except BrokerUnavailable as error:
        assert not leaks(_loop_said(error))


READS: dict[str, Callable] = {
    "positions": lambda path: path.positions(),
    "open_orders": lambda path: path.open_orders(),
    "account": lambda path: path.account(),
    "fills": lambda path: path.fills(AT),
    "lookup": lambda path: path.lookup("b-1"),
}


@pytest.mark.parametrize(
    "kind", [RuntimeError, SystemExit, CancelledError, KeyError, KeyboardInterrupt]
)
@pytest.mark.parametrize("name", READS)
def test_11_a_read_that_raises_says_nothing_of_the_vendors(
    name: str, kind: type[BaseException], capfd, caplog
) -> None:
    caplog.set_level(0)
    world = World()

    def raises() -> object:
        raise vendor(kind)

    world.broker.reads[name] = raises
    ends = KeyboardInterrupt if kind is KeyboardInterrupt else BrokerUnavailable
    with pytest.raises(ends) as raised:
        READS[name](world.path())
    said = "" if kind is KeyboardInterrupt else f"the broker's {name} failed with {kind.__name__}"
    assert str(raised.value) == said
    captured = capfd.readouterr()
    every = [*told(raised.value), captured.out, captured.err, caplog.text, *world.alarms.sent]
    assert not leaks(*every)


@pytest.mark.parametrize("name", READS)
def test_5_a_read_answers_with_its_own_type_or_raises(name: str) -> None:
    world = World()
    found = READS[name](world.path())
    assert found is not None and world.broker.calls == [name] and world.reports > 0
    for wrong in (None, [], {}, b"", object()):
        world.broker.reads[name] = lambda wrong=wrong: wrong
        with pytest.raises(BrokerUnavailable, match=f"{name} failed with {type(wrong).__name__}"):
            READS[name](world.path())

    def halts() -> object:
        raise Halt("the simulated log has a gap") from vendor()

    world.broker.reads[name] = halts
    with pytest.raises(Halt, match="has a gap") as raised:
        READS[name](world.path())
    assert not leaks(*told(raised.value))

    def unavailable() -> object:
        raise BrokerUnavailable("get_all_positions", TimeoutError())

    world.broker.reads[name] = unavailable
    with pytest.raises(BrokerUnavailable) as read:
        READS[name](world.path())
    assert (read.value.call, read.value.error) == (name, "TimeoutError")

    def stops() -> object:
        raise KeyboardInterrupt

    world.broker.reads[name] = stops
    with pytest.raises(KeyboardInterrupt):
        READS[name](world.path())
    del world.broker.reads[name]
    world.report_error = SystemExit("x")
    assert READS[name](world.path()) == found


def test_11_an_alias_that_cannot_be_read_says_nothing_of_the_vendors() -> None:
    class NoAlias:
        @property
        def alias(self) -> str:
            raise vendor()

    world = World()
    for name, read in READS.items():
        with pytest.raises(Halt, match="alias could not be read: RuntimeError") as raised:
            read(world.path(broker=NoAlias()))
        assert not leaks(*told(raised.value)), name


EVERY_CALL: dict[str, Callable] = {
    **READS,
    "send": lambda path: path.send(ID),
    "hold": lambda path: path.hold([ID]),
    "abandon": lambda path: path.abandon(ID, "no"),
    "loss": lambda path: path.loss(DEPLOYMENT, RUN, AT),
}


@pytest.mark.parametrize("name", EVERY_CALL)
def test_5_every_call_halts_unless_the_broker_trades_the_journals_account(name: str) -> None:
    world = World()
    stored = dict(world.store.blobs)
    for alias in (NUMBER, "", None, "paper-2"):
        world.broker.alias = alias  # type: ignore[assignment]
        with pytest.raises(Halt, match="the broker trades another account than 'paper-1'") as no:
            EVERY_CALL[name](world.path())
        assert not leaks(no.value.reason)
    assert world.store.blobs == stored and world.broker.calls == []
    world.broker.alias = ACCOUNT
    EVERY_CALL[name](world.path())


@pytest.mark.parametrize("name", EVERY_CALL)
def test_5_an_account_that_names_none_halts_each_call_and_never_the_start(name: str) -> None:
    world = World()
    for alias in ("", "a/b", "..", None):
        account = world.account(account=alias)
        with pytest.raises(Halt, match="is no alias a journal is kept under"):
            EVERY_CALL[name](account.path(world.broker, world.report))
        with pytest.raises(Halt, match="is no alias"):
            account.settle(world.report)
        assert account.unstored() == ()
    assert world.broker.calls == []


def test_5_a_writer_that_is_none_halts_each_order_and_sends_nothing() -> None:
    world = World()
    for writer in (None, "", "host-1", "0" * 31):
        account = world.account(writer=writer)
        halted(world, "no writer", lambda account=account: world.send(account=account))
    assert world.kinds() == []
