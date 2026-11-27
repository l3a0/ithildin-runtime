"""The supervisor: the run's stop, the order-in-doubt deadline, the plan and the hold.

#200's plan names these tests. Each runs `Supervisor.run` over a folder with a
timer that moves only when the supervisor waits, so a test reads the time each
write was made at. The workers are left out: a stage's result is any blob at
its key, which is all the supervisor reads of it. Every id, price and quantity
is made up.

The decision is at close−20 of 2026-10-01, the moment tests/support/journals.py
journals its entries at, and today is the session after it.
"""

from __future__ import annotations

import dataclasses
import io
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderStore, LogAlerts, SystemTimer
from ithildinruntime.core import (
    RUN_RECORD_FORMAT,
    BeforeClose,
    Mode,
    OrderIntent,
    Plan,
    PlanDecision,
    Retry,
    RunHalt,
    RunStop,
    Storage,
    StoreUnavailable,
    StrategyHalt,
    SupervisorStart,
    decode,
    encode,
    journal_entry_key,
    load_sessions,
    plan_key,
    result_key,
    run_halt_key,
    run_stop_key,
    starts_prefix,
    strategy_halt_key,
    strategy_halts_prefix,
    supervisor_start_key,
    to_utc,
)
from ithildinruntime.core.strategy import STRATEGIES
from ithildinruntime.execution.controls import Controls
from ithildinruntime.journal import Journal, PauseEntry, record_of
from ithildinruntime.runtime.deployment import Deployment, deployment_table
from ithildinruntime.runtime.supervisor import (
    EXIT_ASKED,
    EXIT_FAILED,
    EXIT_HALTED,
    EXIT_HELD,
    EXIT_PLAN,
    EXIT_STALLED,
    EXIT_STOPPED,
    Supervisor,
)
from ithildinruntime.runtime.supervisor.deadline import (
    HEARTBEAT_GRACE,
    IN_DOUBT_LIMIT,
    STALL_AFTER_CLOSE,
    STOP_AFTER_CLOSE,
)
from ithildinruntime.runtime.supervisor.hold import CAUSE, COMPONENT
from tests.support.journals import (
    ACCOUNT,
    AT,
    CHAN,
    GLD,
    OTHER,
    WRITER,
    add,
    decision,
    opening,
    placed,
    planned,
    read,
    route,
)

SESSIONS = load_sessions()
DEPLOYMENT = "paper"
YESTERDAY = date(2026, 10, 1)
TODAY = date(2026, 10, 2)
RELEASE = "sha256:release"
# Each run's one decision, at close−20.
DECIDED = {d: to_utc(d, BeforeClose(20), SESSIONS) for d in (YESTERDAY, TODAY)}
CLOSE = SESSIONS.session(TODAY).close
OPEN = SESSIONS.session(TODAY).open
TICK = 10.0
TABLE = deployment_table(Deployment(DEPLOYMENT, ACCOUNT, {CHAN: (BeforeClose(20),)}))
STAGES = [stage.name for stage in TABLE.stages]


class Timer:
    """A timer that moves only when the supervisor waits."""

    def __init__(self, at: datetime) -> None:
        self.at = at
        self.mono = 0.0

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return self.mono

    def after(self, seconds: float) -> object:
        raise NotImplementedError


class World:
    def __init__(self, root: Path, at: datetime = CLOSE + STOP_AFTER_CLOSE) -> None:
        self.root = root
        self.store = FolderStore(root)
        self.timer = Timer(at)
        self.lines = io.StringIO()
        self.alerts = LogAlerts("supervisor", self.lines)
        self.storage = Storage(
            self.store,
            timer=self.timer,
            pause=lambda seconds: None,
            retry=Retry(timedelta(seconds=1), 0.01, 0.01),
            alerts=self.alerts,
            owner_step="nothing to do: a test",
        )
        self.journal = Journal(self.store, ACCOUNT, WRITER)
        self.ticks = 0
        self.most_ticks = 2000
        # Called at each wait, before the timer moves, so a test acts mid-run.
        self.on_tick: Callable[[World], None] = lambda world: None

    def wait(self, seconds: float) -> bool:
        self.on_tick(self)
        self.ticks += 1
        self.timer.at += timedelta(seconds=seconds)
        self.timer.mono += seconds
        return self.ticks >= self.most_ticks

    def supervisor(self, boot: str = "boot-a", mode: Mode = Mode.PAPER, **changes) -> Supervisor:
        made = {
            "mode": mode,
            "deployment": DEPLOYMENT,
            "account": ACCOUNT,
            "store": self.store,
            "storage": self.storage,
            "table": TABLE,
            "schedules": {CHAN: (BeforeClose(20),)},
            "release": RELEASE,
            "boot": boot,
            "timer": self.timer,
            "alerts": self.alerts,
            "release_file": self.root / "local" / "release",
            "wait": self.wait,
            "tick": TICK,
            "sessions": SESSIONS,
        }
        return Supervisor(**(made | changes))

    def alarms(self) -> list[str]:
        return [line for line in self.lines.getvalue().splitlines() if line.startswith("ALARM")]

    def results(self, day: date = TODAY, stages=STAGES, run: str | None = None) -> None:
        for stage in stages:
            key = result_key(DEPLOYMENT, run or day.isoformat(), DECIDED[day], stage)
            if not (self.root / key).exists():
                self.put(key, b"{}")

    def put(self, key: str, data: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def blob(self, key: str) -> bytes | None:
        path = self.root / key
        return path.read_bytes() if path.exists() else None

    def opened(self, *orders: OrderIntent, decided: bool = True) -> None:
        """An account opened at the decision before today's, with that decision journaled."""
        add(self.journal, opening())
        add(self.journal, read())
        if decided:
            add(self.journal, decision(*orders))

    def ran(self, day: date = YESTERDAY, stopped: bool = True) -> None:
        """A run's plan on `day`, as the supervisor stores one, and its stop if it stopped."""
        run = day.isoformat()
        self.put(plan_key(DEPLOYMENT, run), encode(_plan(day)))
        if stopped:
            stop = RunStop(RUN_RECORD_FORMAT, DEPLOYMENT, run, RELEASE, CLOSE)
            self.put(run_stop_key(DEPLOYMENT, run), encode(stop))

    def held(self, strategy: str = CHAN) -> list[str]:
        folder = self.root / strategy_halts_prefix(ACCOUNT, strategy)
        return sorted(p.name for p in folder.iterdir()) if folder.exists() else []

    def pauses(self) -> list[PauseEntry]:
        found = [record_of(entry) for entry in Journal(self.store, ACCOUNT).read()]
        return [entry for entry in found if isinstance(entry, PauseEntry)]


def _plan(day: date) -> Plan:
    at = to_utc(day, BeforeClose(20), SESSIONS)
    return Plan(1, DEPLOYMENT, day.isoformat(), Mode.PAPER, RELEASE, (PlanDecision(at, (CHAN,)),))


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path / "store")


def _stop_of(world: World, day: date = TODAY) -> RunStop | None:
    data = world.blob(run_stop_key(DEPLOYMENT, day.isoformat()))
    return None if data is None else decode(data, RunStop)


# The run's stop


def test_the_stop_waits_for_every_result_and_five_minutes_past_the_close(world: World) -> None:
    world.opened()
    world.ran()
    world.timer.at = CLOSE - timedelta(minutes=30)
    added = []

    def tick(w: World) -> None:
        # Every result but execution's send is stored at once, and send's two minutes in.
        if w.ticks == 0:
            w.results(stages=STAGES[:-1])
        if w.timer.at >= CLOSE - timedelta(minutes=28) and not added:
            w.results()
            added.append(w.timer.at)
        assert _stop_of(w) is None

    world.on_tick = tick
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_STOPPED, outcome.line
    stop = _stop_of(world)
    assert stop is not None and stop.run == TODAY.isoformat() and stop.release == RELEASE
    # No earlier than five minutes past the close, and within one tick of it.
    five = timedelta(minutes=5)
    assert CLOSE + five <= stop.stopped_at < CLOSE + five + timedelta(seconds=TICK)
    assert world.alarms() == []


def test_a_backtest_stores_its_stop_once_every_result_is_stored(world: World) -> None:
    world.timer.at = DECIDED[TODAY]
    world.results(run=f"{TODAY}_{TODAY}")
    outcome = world.supervisor(mode=Mode.BACKTEST, span=(TODAY, TODAY)).run(TODAY)
    assert outcome.code == EXIT_STOPPED, outcome.line
    stop = world.blob(run_stop_key(DEPLOYMENT, f"{TODAY}_{TODAY}"))
    assert stop is not None and decode(stop, RunStop).stopped_at == DECIDED[TODAY]
    # A backtest runs no hold, so no account was read and no start holds anything.
    assert world.held() == []


def test_a_result_missing_an_hour_after_the_close_ends_the_run_with_no_stop(
    world: World,
) -> None:
    world.opened()
    world.ran()
    world.results(stages=STAGES[:-1])
    world.timer.at = CLOSE
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_STALLED, outcome.line
    assert _stop_of(world) is None
    # The alarm says an hour, so the stall comes at the close plus 60 minutes, within a tick.
    hour = timedelta(minutes=60)
    assert hour <= world.timer.at - CLOSE < hour + timedelta(seconds=TICK)
    (alarm,) = world.alarms()
    assert "execution/send at 2026-10-02T19:40Z" in alarm and "an hour after the close" in alarm
    assert "which stores the stop, or the run's halt where one is owed" in alarm


def test_a_halted_run_stores_no_stop_and_a_restart_after_its_halt_stores_none(
    world: World,
) -> None:
    world.opened()
    world.ran()
    world.results()
    halt = RunHalt(RUN_RECORD_FORMAT, DEPLOYMENT, TODAY.isoformat(), "a test", CLOSE)
    world.put(run_halt_key(DEPLOYMENT, TODAY.isoformat()), encode(halt))
    for _ in range(2):
        outcome = world.supervisor().run(TODAY)
        assert outcome.code == EXIT_HALTED, outcome.line
        assert _stop_of(world) is None


def test_a_second_start_after_the_stop_writes_nothing_new(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    first = world.supervisor().run(TODAY)
    stopped = world.blob(run_stop_key(DEPLOYMENT, TODAY.isoformat()))
    again = world.supervisor().run(TODAY)
    assert (first.code, again.code) == (EXIT_STOPPED, EXIT_STOPPED)
    assert world.blob(run_stop_key(DEPLOYMENT, TODAY.isoformat())) == stopped
    assert world.held() == []


def test_a_day_with_no_session_starts_no_run(world: World) -> None:
    outcome = world.supervisor().run(date(2026, 10, 3))
    assert outcome.code == EXIT_STOPPED and "no session on 2026-10-03" in outcome.line
    assert not (world.root / "runs").exists() and not (world.root / "local").exists()


# The plan and the release file


def test_the_plan_and_the_release_file_name_the_release(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    world.supervisor().run(TODAY)
    plan = decode(world.blob(plan_key(DEPLOYMENT, TODAY.isoformat())), Plan)
    assert (plan.mode, plan.release, [d.at for d in plan.decisions]) == (
        Mode.PAPER,
        RELEASE,
        [DECIDED[TODAY]],
    )
    assert (world.root / "local" / "release").read_text() == f"{RELEASE}\n{TODAY}\nboot-a\n"


def test_another_plan_already_stored_ends_the_start_and_starts_no_worker(world: World) -> None:
    world.opened()
    world.ran()
    other = Plan(1, DEPLOYMENT, TODAY.isoformat(), Mode.PAPER, "sha256:other", ())
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(other))
    # The start that stored that plan, on this boot, recorded itself.
    _start_on(world, "boot-a")
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_PLAN and "sha256:other" in outcome.line
    assert not (world.root / "local" / "release").exists()


def test_a_stored_plan_of_another_mode_ends_the_start(world: World) -> None:
    world.opened()
    world.ran()
    other = Plan(1, DEPLOYMENT, TODAY.isoformat(), Mode.SHADOW, RELEASE, ())
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(other))
    # The start that stored that plan, on this boot, recorded itself.
    _start_on(world, "boot-a")
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_PLAN and "is for shadow" in outcome.line


def test_each_start_writes_a_record_numbered_on_its_boot(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    for boot in ("boot-a", "boot-a", "boot-b"):
        world.supervisor(boot=boot).run(TODAY)
    run = TODAY.isoformat()
    for boot, count in (("boot-a", 1), ("boot-a", 2), ("boot-b", 1)):
        data = world.blob(supervisor_start_key(DEPLOYMENT, run, boot, count))
        record = decode(data, SupervisorStart)
        assert (record.boot, record.count, record.release) == (boot, count, RELEASE)


# The deadline of an order in doubt


def _doubtful(world: World, sent_at: datetime, state: str = "InDoubt") -> str:
    """An order of today's decision left in doubt, whose `sending` entry was built at `sent_at`."""
    order = planned(1, 5, at=DECIDED[TODAY])
    add(world.journal, decision(order, at=DECIDED[TODAY], read=2))
    for record in route(state, order, decided=world.journal.head().seq):
        world.journal.record(world.journal.view(), placed(world.journal, record), sent_at)
    return order.client_order_id


def _deadline(sent_at: datetime) -> datetime:
    # #147 item 6: the smaller of 30 minutes and the time left to the cancel cutoff, close−10.
    return min(sent_at + timedelta(minutes=30), CLOSE - timedelta(minutes=10))


@pytest.mark.parametrize(
    ("sent_at", "name"),
    [
        (DECIDED[TODAY], "thirty minutes come first"),
        (CLOSE - timedelta(minutes=15), "the cancel cutoff comes first"),
    ],
)
@pytest.mark.parametrize("step", [-1, 0, 1])
def test_an_order_in_doubt_halts_the_run_only_past_its_deadline(
    world: World, sent_at: datetime, name: str, step: int
) -> None:
    world.opened()
    world.ran()
    held = _doubtful(world, sent_at)
    deadline = _deadline(sent_at)
    world.timer.at = deadline + timedelta(microseconds=step)
    world.most_ticks = 1
    outcome = world.supervisor().run(TODAY)
    halt = world.blob(run_halt_key(DEPLOYMENT, TODAY.isoformat()))
    if step <= 0:
        # At the deadline the order is not yet past it, and the test's one tick ends the start.
        assert halt is None and outcome.code == EXIT_ASKED, outcome.line
        return
    assert outcome.code == EXIT_HALTED, outcome.line
    record = decode(halt, RunHalt)
    assert held in record.reason and f"{deadline:%H:%M:%S}" in record.reason
    # A halt is every stage's result for the decision left, and execution's send is one.
    send = world.blob(result_key(DEPLOYMENT, TODAY.isoformat(), DECIDED[TODAY], "execution/send"))
    assert send is not None and b"the supervisor halted the run" in send
    (alarm,) = world.alarms()
    assert "halts 2026-10-02" in alarm and held in alarm


def test_the_deadline_sits_inside_the_heartbeat_grace() -> None:
    assert IN_DOUBT_LIMIT < HEARTBEAT_GRACE


def test_an_order_left_in_doubt_by_an_earlier_session_halts_at_once(world: World) -> None:
    order = planned(1, 3)
    world.opened(order)
    world.ran()
    for record in route("Sending", order):
        add(world.journal, record)
    world.timer.at = OPEN
    world.most_ticks = 1
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HALTED and order.client_order_id in outcome.line


def test_a_journal_that_cannot_be_read_halts_the_run_on_the_first_tick(world: World) -> None:
    world.opened()
    world.ran()
    world.timer.at = OPEN

    def spoil(w: World) -> None:
        # The journal reads at the hold, and turns unreadable once the run starts.
        if w.ticks == 0:
            w.put(journal_entry_key(ACCOUNT, 4), b"not an entry")

    world.on_tick = spoil
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HALTED and "cannot be read, so no order in doubt" in outcome.line
    assert world.ticks == 1


# The hold after an unclean stop


def test_a_clean_previous_run_holds_nothing(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    assert world.supervisor().run(TODAY).code == EXIT_STOPPED
    assert world.held() == [] and world.pauses() == [] and world.alarms() == []


def test_a_first_run_holds_nothing(world: World) -> None:
    world.results()
    assert world.supervisor().run(TODAY).code == EXIT_STOPPED
    assert world.held() == [] and world.alarms() == []


def _hold_key(day: date = YESTERDAY, strategy: str = CHAN) -> str:
    return strategy_halt_key(ACCOUNT, strategy, COMPONENT, CAUSE, DECIDED[day], 1)


def test_a_previous_run_with_no_stop_holds_every_strategy_and_journals_one_pause(
    world: World,
) -> None:
    order = planned(1, 4, at=AT)
    world.opened(order)
    for record in route("Filled", order):
        add(world.journal, record)
    world.ran(stopped=False)
    world.results()
    outcome = world.supervisor().run(TODAY)
    # The run still goes on to its stop, and the strategy sends nothing.
    assert outcome.code == EXIT_STOPPED, outcome.line
    halt = decode(world.blob(_hold_key()), StrategyHalt)
    assert (halt.strategy, halt.cause, halt.run) == (CHAN, CAUSE, YESTERDAY.isoformat())
    stops = Controls(world.store, ACCOUNT).strategy(CHAN, lambda call: call())
    assert stops is not None and "strategy off: strategy halt stands" in stops.reason
    (pause,) = world.pauses()
    assert (pause.deployment, pause.run, pause.since, pause.strategies) == (
        DEPLOYMENT,
        YESTERDAY.isoformat(),
        AT,
        (CHAN,),
    )
    (alarm,) = world.alarms()
    assert "2026-10-01 ended uncleanly: it stored no stop" in alarm
    assert f"Open positions: {CHAN} GLD +4" in alarm
    assert "The operator clears it" in alarm


def test_a_live_hold_names_the_owner(world: World) -> None:
    world.opened()
    world.ran(stopped=False)
    world.results()
    world.supervisor(mode=Mode.LIVE).run(TODAY)
    # A live plan is a different plan, so the start ends there, after the hold.
    (alarm, *_) = world.alarms()
    assert "The owner clears it" in alarm and "operator" not in alarm


def test_every_strategy_the_book_holds_is_held_with_the_tables(world: World) -> None:
    order = planned(1, 2, GLD, OTHER, at=AT)
    world.opened(order)
    for record in route("Filled", order):
        add(world.journal, record)
    world.ran(stopped=False)
    world.results()
    world.supervisor().run(TODAY)
    assert world.held(CHAN) and world.held(OTHER)
    (pause,) = world.pauses()
    assert pause.strategies == tuple(sorted((CHAN, OTHER)))


def test_a_working_adding_order_is_named_and_nothing_new_is_sent(world: World) -> None:
    order = planned(1, 6, at=AT)
    world.opened(order)
    for record in route("Acknowledged", order, broker="bk-7"):
        add(world.journal, record)
    world.ran(stopped=False)
    world.results()
    world.supervisor().run(TODAY)
    (alarm,) = world.alarms()
    assert f"Working orders: {order.client_order_id} (the broker's bk-7)" in alarm
    assert "by 19:50 UTC today" in alarm
    assert Controls(world.store, ACCOUNT).strategy(CHAN, lambda call: call()) is not None


def test_an_order_in_doubt_at_the_stop_is_named_by_its_client_order_id(world: World) -> None:
    order = planned(1, 1, at=AT)
    world.opened(order)
    for record in route("InDoubt", order):
        add(world.journal, record)
    world.ran(stopped=False)
    world.timer.at = OPEN
    world.most_ticks = 1
    world.supervisor().run(TODAY)
    hold = next(a for a in world.alarms() if "ended uncleanly" in a)
    assert f"Orders in doubt, by client order id: {order.client_order_id}" in hold


@pytest.mark.parametrize(
    ("stop", "why"),
    [
        (b"", "its stop is not this run's stop record"),
        (b"{not json", "its stop is not this run's stop record"),
        (
            encode(RunStop(RUN_RECORD_FORMAT, DEPLOYMENT, "2026-09-30", RELEASE, CLOSE)),
            "its stop is not this run's stop record",
        ),
    ],
    ids=["empty", "malformed", "another run's"],
)
def test_a_stop_that_is_not_the_runs_own_holds(world: World, stop: bytes, why: str) -> None:
    world.opened()
    world.ran(stopped=False)
    world.put(run_stop_key(DEPLOYMENT, YESTERDAY.isoformat()), stop)
    world.results()
    world.supervisor().run(TODAY)
    assert world.held() and why in world.alarms()[0]


def test_a_previous_run_with_no_journaled_decision_holds_from_its_plans_first(
    world: World,
) -> None:
    # On shadow the send stage halts before it journals a decision, so none names the run.
    world.opened(decided=False)
    world.ran(stopped=False)
    world.results()
    world.supervisor().run(TODAY)
    (pause,) = world.pauses()
    assert pause.since is None
    first = _plan(YESTERDAY).decisions[0].at
    assert world.blob(strategy_halt_key(ACCOUNT, CHAN, COMPONENT, CAUSE, first, 1)) is not None


def test_a_restart_after_an_unclean_stop_leaves_each_strategy_off(world: World) -> None:
    world.opened()
    world.ran(stopped=False)
    world.results()
    for _ in range(3):
        # Each start is on one boot, so each is a restart the host supervised.
        world.supervisor(boot="boot-a").run(TODAY)
    assert world.held() == [_hold_key().rpartition("/")[2]]
    assert len(world.pauses()) == 1
    assert Controls(world.store, ACCOUNT).strategy(CHAN, lambda call: call()) is not None


def test_a_held_session_that_stops_cleanly_stays_held_at_its_next_start(world: World) -> None:
    world.opened()
    world.ran(stopped=False)
    world.results()
    assert world.supervisor().run(TODAY).code == EXIT_STOPPED
    assert _stop_of(world) is not None
    tomorrow = date(2026, 10, 5)
    world.timer.at = SESSIONS.session(tomorrow).close + STOP_AFTER_CLOSE
    DECIDED.setdefault(tomorrow, to_utc(tomorrow, BeforeClose(20), SESSIONS))
    world.results(day=tomorrow)
    world.supervisor().run(tomorrow)
    # Nothing clears a hold but a person, so the halt stands and no second pause is written.
    assert world.held() == [_hold_key().rpartition("/")[2]] and len(world.pauses()) == 1


def test_a_crash_between_the_halts_and_the_pause_journals_one_pause_on_the_rerun(
    world: World,
) -> None:
    world.opened()
    world.ran(stopped=False)
    world.results()
    halt = StrategyHalt(RUN_RECORD_FORMAT, ACCOUNT, CHAN, CAUSE, "a crash", "2026-10-01", CLOSE)
    world.put(_hold_key(), encode(halt))
    world.supervisor().run(TODAY)
    world.supervisor().run(TODAY)
    assert len(world.pauses()) == 1
    # The halt that stood was kept as it was.
    assert decode(world.blob(_hold_key()), StrategyHalt).reason == "a crash"


def test_a_hold_a_person_cleared_stays_cleared(world: World) -> None:
    world.opened()
    world.ran(stopped=False)
    world.results()
    world.supervisor().run(TODAY)
    (world.root / _hold_key()).unlink()
    world.supervisor(boot="boot-a").run(TODAY)
    assert world.held() == [] and len(world.pauses()) == 1


def test_the_walk_back_passes_a_session_with_no_plan(world: World) -> None:
    world.opened()
    # The account opened on 2026-10-01, and today is two sessions later.
    world.ran(stopped=False)
    later = date(2026, 10, 5)
    world.timer.at = SESSIONS.session(later).close + STOP_AFTER_CLOSE
    DECIDED.setdefault(later, to_utc(later, BeforeClose(20), SESSIONS))
    world.results(day=later)
    world.supervisor().run(later)
    (pause,) = world.pauses()
    assert pause.run == YESTERDAY.isoformat()


def test_a_lost_machine_holds_and_a_supervised_restart_does_not(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    run = TODAY.isoformat()
    record = SupervisorStart(RUN_RECORD_FORMAT, DEPLOYMENT, run, "boot-old", 1, RELEASE, OPEN)
    world.put(supervisor_start_key(DEPLOYMENT, run, "boot-old", 1), encode(record))
    world.put(plan_key(DEPLOYMENT, run), encode(_plan(TODAY)))
    # The same boot again is a restart the host supervised, which holds nothing.
    restart = World(world.root.with_name("restart"))
    restart.opened()
    restart.ran()
    restart.results()
    restart.put(supervisor_start_key(DEPLOYMENT, run, "boot-a", 1), encode(record))
    restart.put(plan_key(DEPLOYMENT, run), encode(_plan(TODAY)))
    restart.supervisor(boot="boot-a").run(TODAY)
    assert restart.held() == [] and restart.pauses() == []
    # Another boot, with none of this one's, is a lost machine.
    world.supervisor(boot="boot-new").run(TODAY)
    (pause,) = world.pauses()
    assert pause.run == run and "was lost" in pause.reason
    assert world.held(), "the strategy of a lost machine's run is held"


def test_a_journal_that_cannot_be_read_holds_every_strategy_and_starts_nothing(
    world: World,
) -> None:
    world.opened()
    world.ran()
    world.put(journal_entry_key(ACCOUNT, 4), b"not an entry")
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HELD, outcome.line
    held = strategy_halt_key(ACCOUNT, CHAN, COMPONENT, CAUSE, OPEN, 1)
    assert world.blob(held) is not None
    assert world.blob(plan_key(DEPLOYMENT, TODAY.isoformat())) is None
    assert not (world.root / "local" / "release").exists()
    (alarm,) = world.alarms()
    assert "could not finish" in alarm and "no worker starts" in alarm


def test_the_pause_record_reads_back_and_moves_nothing_in_the_book(world: World) -> None:
    world.opened()
    before = world.journal.view().book
    pause = PauseEntry(DEPLOYMENT, "2026-10-01", AT, (CHAN,), "a test", (), True)
    _, view = world.journal.record(world.journal.view(), pause, AT)
    _, view = world.journal.record(view, pause, AT)
    after = Journal(world.store, ACCOUNT).view()
    assert after.book.positions_by_strategy == before.positions_by_strategy
    assert after.book.cash == before.cash
    # A second pause of one run is kept beside the first, and halts nothing.
    assert list(after.pauses) == [4, 5]


# What a person reads after each way a start ends


def test_a_plan_that_cannot_be_stored_alarms(world: World) -> None:
    world.opened()
    world.ran()
    other = Plan(1, DEPLOYMENT, TODAY.isoformat(), Mode.PAPER, "sha256:other", ())
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(other))
    # The start that stored that plan, on this boot, recorded itself.
    _start_on(world, "boot-a")
    world.supervisor().run(TODAY)
    (alarm,) = world.alarms()
    assert "could not store the plan of 2026-10-02" in alarm and "sha256:other" in alarm
    assert "Otherwise start the supervisor's unit again once the store answers" in alarm


def test_a_hold_a_failed_check_left_is_named_at_a_later_clean_start(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    bad = journal_entry_key(ACCOUNT, 4)
    world.put(bad, b"not an entry")
    assert world.supervisor().run(TODAY).code == EXIT_HELD
    (world.root / bad).unlink()
    world.lines.truncate(0)
    world.lines.seek(0)
    assert world.supervisor().run(TODAY).code == EXIT_STOPPED
    (alarm,) = world.alarms()
    assert f"{CHAN} is still held by a halt nobody has cleared" in alarm


def test_a_hold_check_that_fails_mid_session_says_the_workers_run_on(world: World) -> None:
    world.opened()
    world.ran()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    # This machine released its workers into today's run before.
    (world.root / "local").mkdir(parents=True)
    (world.root / "local" / "release").write_text(f"{RELEASE}\n{TODAY}\nboot-a\n")
    _start_on(world, "boot-a")
    world.put(journal_entry_key(ACCOUNT, 4), b"not an entry")
    assert world.supervisor().run(TODAY).code == EXIT_HELD
    (alarm,) = world.alarms()
    assert "the workers run today's plan and no supervisor watches it" in alarm
    assert "no worker starts" not in alarm and "stays held" in alarm
    assert "No halt of" not in alarm, "every strategy was held, so none is named as free"


def test_a_release_file_of_yesterdays_run_on_this_boot_released_nothing_today(
    world: World,
) -> None:
    world.opened()
    world.ran()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    (world.root / "local").mkdir(parents=True)
    (world.root / "local" / "release").write_text(f"{RELEASE}\n{YESTERDAY}\nboot-a\n")
    world.put(journal_entry_key(ACCOUNT, 4), b"not an entry")
    assert world.supervisor().run(TODAY).code == EXIT_HELD
    (alarm,) = world.alarms()
    assert "no worker starts" in alarm


def test_a_hold_check_that_fails_before_this_machine_released_says_none_starts(
    world: World,
) -> None:
    world.opened()
    world.ran()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    # A release file of another boot, such as one left from before a reboot, released none.
    (world.root / "local").mkdir(parents=True)
    (world.root / "local" / "release").write_text(f"{RELEASE}\n{TODAY}\nboot-old\n")
    world.put(journal_entry_key(ACCOUNT, 4), b"not an entry")
    assert world.supervisor().run(TODAY).code == EXIT_HELD
    (alarm,) = world.alarms()
    assert "no worker starts" in alarm


def test_a_hold_check_that_stores_no_halt_says_nothing_is_held(world: World) -> None:
    world.opened()
    world.ran()
    world.put(journal_entry_key(ACCOUNT, 4), b"not an entry")
    _refusing(world, lambda call, key: call == "create" and "/halts/" in key)
    assert world.supervisor().run(TODAY).code == EXIT_HELD
    (alarm,) = [a for a in world.alarms() if "unclean stop" in a]
    assert "stored no halt of its own, so it holds no strategy" in alarm
    assert "A halt written earlier may still stand" in alarm
    assert "Pull the kill switch" in alarm and "stays held" not in alarm


def test_a_first_run_says_so_in_a_line(world: World) -> None:
    said: list[str] = []
    world.results()
    world.supervisor(say=said.append).run(TODAY)
    assert said == [
        f"{DEPLOYMENT}: account {ACCOUNT} has no journal entry, so this first run holds nothing"
    ]


def test_a_halted_runs_alarm_says_what_to_do_and_who_clears_it(world: World) -> None:
    world.opened()
    world.ran()
    _doubtful(world, DECIDED[TODAY])
    world.timer.at = _deadline(DECIDED[TODAY]) + timedelta(seconds=1)
    world.supervisor().run(TODAY)
    (alarm,) = world.alarms()
    assert "Settle each order named at the broker" in alarm
    assert "The next session's start holds every strategy" in alarm
    assert "The operator clears it" in alarm


def test_a_stall_alarm_says_a_later_restart_can_still_store_the_stop(world: World) -> None:
    world.opened()
    world.ran()
    world.results(stages=STAGES[:-1])
    world.timer.at = CLOSE
    world.supervisor().run(TODAY)
    (alarm,) = world.alarms()
    assert "unless the missing result lands and a person starts the supervisor's unit" in alarm


def test_a_stop_asked_before_the_runs_stop_ends_with_its_own_code(world: World) -> None:
    world.opened()
    world.ran()
    world.timer.at = OPEN
    world.most_ticks = 1
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_ASKED and "asked to stop before" in outcome.line
    assert _stop_of(world) is None
    (alarm,) = world.alarms()
    assert "asked to stop before 2026-10-02's stop was stored" in alarm
    assert "If its unit shows stopped, start the supervisor's unit again" in alarm


# A pause covers only what it names


def _start_on(world: World, boot: str, count: int = 1, day: date = TODAY) -> None:
    run = day.isoformat()
    record = SupervisorStart(RUN_RECORD_FORMAT, DEPLOYMENT, run, boot, count, RELEASE, OPEN)
    world.put(supervisor_start_key(DEPLOYMENT, run, boot, count), encode(record))


def _clear(world: World) -> None:
    for strategy in (CHAN, OTHER):
        folder = world.root / strategy_halts_prefix(ACCOUNT, strategy)
        for path in folder.glob("*.json") if folder.exists() else ():
            path.unlink()


def test_a_second_lost_machine_after_a_cleared_hold_holds_again(world: World) -> None:
    world.opened()
    world.ran()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    _start_on(world, "boot-a")
    world.timer.at = OPEN
    world.most_ticks = 1
    # B finds A lost, holds and pauses. A person clears the hold.
    world.supervisor(boot="boot-b").run(TODAY)
    assert len(world.pauses()) == 1 and world.held()
    _clear(world)
    # Then B is lost too, and C starts on a third boot.
    world.supervisor(boot="boot-c").run(TODAY)
    pauses = world.pauses()
    assert [p.boots for p in pauses] == [("boot-a",), ("boot-b",)]
    assert world.held(), "the second lost machine holds again"


def test_a_run_paused_in_its_session_that_then_ends_uncleanly_holds_at_the_next(
    world: World,
) -> None:
    world.opened()
    world.ran(stopped=False)
    _start_on(world, "boot-a", day=YESTERDAY)
    # Yesterday's run was paused for a lost machine, mid-session, and the hold cleared.
    from ithildinruntime.journal import PauseEntry as Pause

    mid = Pause(DEPLOYMENT, YESTERDAY.isoformat(), AT, (CHAN,), "lost", ("boot-a",), False)
    world.journal.record(world.journal.view(), mid, AT)
    world.results()
    world.supervisor().run(TODAY)
    pauses = world.pauses()
    assert [(p.run, p.ended) for p in pauses] == [
        (YESTERDAY.isoformat(), False),
        (YESTERDAY.isoformat(), True),
    ]
    assert world.held()
    # Each pause's halts sit at keys of their own, so a cleared first one is never reused.
    second = strategy_halt_key(ACCOUNT, CHAN, COMPONENT, CAUSE, AT, 2)
    assert world.blob(second) is not None


def test_an_error_nobody_foresaw_in_the_hold_check_holds(world: World, monkeypatch) -> None:
    world.opened()
    world.ran()

    def broken(self):
        raise RuntimeError("a bug")

    monkeypatch.setattr(Journal, "view", broken)
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HELD and "RuntimeError: a bug" in outcome.line
    assert world.blob(strategy_halt_key(ACCOUNT, CHAN, COMPONENT, CAUSE, OPEN, 1)) is not None


def test_a_stray_start_record_key_reads_as_a_lost_machine_and_holds(world: World) -> None:
    world.opened()
    world.ran()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    world.put(f"runs/{DEPLOYMENT}/{TODAY}/starts/boot-a/notes.json", b"{}")
    world.timer.at = OPEN
    world.most_ticks = 1
    world.supervisor().run(TODAY)
    assert world.held()
    assert any("start records of 2026-10-02 could not be read" in a for a in world.alarms())


def test_a_deadline_that_cannot_be_computed_halts_the_run(world: World, monkeypatch) -> None:
    from ithildinruntime.runtime import supervisor as module

    world.opened()
    world.ran()
    _doubtful(world, DECIDED[TODAY])
    world.timer.at = OPEN

    def broken(*args, **kwargs):
        raise module.Halt("the session table ends")

    monkeypatch.setattr(module, "past_deadline", broken)
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HALTED and "the session table ends" in outcome.line
    assert world.blob(run_halt_key(DEPLOYMENT, TODAY.isoformat())) is not None


def test_the_release_file_is_never_written_through_a_planted_link(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    target = world.root.parent / "elsewhere"
    target.write_text("untouched\n")
    local = world.root / "local"
    local.mkdir(parents=True)
    (local / "release.partial").symlink_to(target)
    world.supervisor().run(TODAY)
    assert target.read_text() == "untouched\n"
    assert (local / "release").read_text() == f"{RELEASE}\n{TODAY}\nboot-a\n"
    assert not (local / "release").is_symlink()


def test_the_deadlines_are_the_values_the_issues_wrote() -> None:
    # #147 item 6, and the two margins past the close #200's plan wrote.
    assert IN_DOUBT_LIMIT == timedelta(minutes=30)
    assert STOP_AFTER_CLOSE == timedelta(minutes=5)
    assert STALL_AFTER_CLOSE == timedelta(minutes=60)


# The walk back and the lost machine, each condition on its own


def _opened_on(world: World, at: datetime) -> None:
    """An account opened at `at`, with no decision journaled."""
    world.journal.record(world.journal.view(), opening(), at)


def test_an_unclean_run_behind_a_clean_one_is_not_reached(world: World) -> None:
    # Opened well before, so only the clean run, and not the opening, ends the walk.
    _opened_on(world, datetime(2026, 9, 25, 19, 40, tzinfo=UTC))
    world.ran(day=date(2026, 9, 29), stopped=False)
    world.ran(day=YESTERDAY)
    world.results()
    world.supervisor().run(TODAY)
    assert world.held() == [] and world.pauses() == []


def test_an_unclean_run_behind_one_whose_end_a_pause_covers_is_not_reached(world: World) -> None:
    _opened_on(world, datetime(2026, 9, 25, 19, 40, tzinfo=UTC))
    world.ran(day=date(2026, 9, 29), stopped=False)
    world.ran(stopped=False)
    end = PauseEntry(DEPLOYMENT, YESTERDAY.isoformat(), AT, (CHAN,), "ended", (), True)
    world.journal.record(world.journal.view(), end, AT)
    world.results()
    world.supervisor().run(TODAY)
    assert len(world.pauses()) == 1 and world.held() == []


def test_a_run_before_the_accounts_opening_is_not_reached(world: World) -> None:
    # The account opened at 2026-10-01's decision, which ran no plan, and a plan of the
    # session before it stored no stop.
    world.opened()
    world.put(plan_key(DEPLOYMENT, "2026-09-30"), encode(_plan(date(2026, 9, 30))))
    world.results()
    world.supervisor().run(TODAY)
    assert world.held() == []


def test_a_start_on_this_boot_beside_another_boots_is_a_supervised_restart(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    _start_on(world, "boot-old")
    _start_on(world, "boot-a")
    world.supervisor(boot="boot-a").run(TODAY)
    assert world.held() == [] and world.pauses() == []


def test_another_boots_start_with_no_plan_stored_is_no_lost_machine(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    _start_on(world, "boot-old")
    world.supervisor(boot="boot-new").run(TODAY)
    assert world.held() == [] and world.pauses() == []


def test_the_halt_names_the_runs_last_journaled_decision(world: World) -> None:
    world.opened()
    later = AT + timedelta(minutes=1)
    add(world.journal, read(later))
    add(world.journal, decision(at=later, read=4))
    other = decision(at=AT + timedelta(minutes=2), read=4)
    add(world.journal, dataclasses.replace(other, run="2026-09-29"))
    world.ran(stopped=False)
    world.results()
    world.supervisor().run(TODAY)
    (pause,) = world.pauses()
    assert pause.since == later
    assert world.blob(strategy_halt_key(ACCOUNT, CHAN, COMPONENT, CAUSE, later, 1)) is not None


def test_a_lost_machine_with_no_journaled_decision_holds_from_its_plans_first(
    world: World,
) -> None:
    world.opened(decided=False)
    world.ran()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    _start_on(world, "boot-old")
    world.timer.at = OPEN
    world.most_ticks = 1
    world.supervisor(boot="boot-new").run(TODAY)
    (pause,) = world.pauses()
    assert pause.since is None and pause.boots == ("boot-old",)
    first = _plan(TODAY).decisions[0].at
    assert world.blob(strategy_halt_key(ACCOUNT, CHAN, COMPONENT, CAUSE, first, 1)) is not None


@pytest.mark.parametrize("state", ["Intended", "Acknowledged", "InDoubt"])
def test_a_working_or_doubtful_order_of_a_strategy_the_table_lacks_is_held(
    world: World, state: str
) -> None:
    order = planned(1, 2, GLD, OTHER, at=AT)
    world.opened(order)
    for record in route(state, order):
        add(world.journal, record)
    world.ran(stopped=False)
    world.timer.at = OPEN
    world.most_ticks = 1
    world.supervisor().run(TODAY)
    assert world.held(OTHER)
    (pause,) = world.pauses()
    assert OTHER in pause.strategies


# Store faults on the supervisor's path


class Refusing(FolderStore):
    """A folder that refuses each call on one key, as S3 not answering does."""

    def __init__(self, root: Path, refused: Callable[[str, str], bool]) -> None:
        super().__init__(root)
        self.refused = refused

    def get(self, key: str) -> bytes:
        if self.refused("get", key):
            raise StoreUnavailable(key, "503: no answer")
        return super().get(key)

    def create(self, key: str, data: bytes, sha256: str) -> str:
        if self.refused("create", key):
            raise StoreUnavailable(key, "503: no answer")
        return super().create(key, data, sha256)

    def list(self, prefix: str, after: str | None):
        if self.refused("list", prefix):
            raise StoreUnavailable(prefix, "503: no answer")
        return super().list(prefix, after)


def _refusing(world: World, refused: Callable[[str, str], bool]) -> None:
    world.store = Refusing(world.root, refused)
    # The retries' deadline runs on the machine's clock, since the test's moves only on a wait.
    world.storage = Storage(
        world.store,
        timer=SystemTimer(),
        pause=lambda seconds: None,
        retry=Retry(timedelta(seconds=1), 0.01, 0.01),
        alerts=world.alerts,
        owner_step="nothing to do: a test",
    )


def test_a_result_read_that_does_not_answer_waits_for_the_next_tick(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    send = result_key(DEPLOYMENT, TODAY.isoformat(), DECIDED[TODAY], "execution/send")
    # Refused past Storage's own retries until the supervisor's first tick.
    _refusing(world, lambda call, key: call == "get" and key == send and world.ticks == 0)
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_STOPPED, outcome.line
    assert world.ticks == 1 and _stop_of(world) is not None


def test_a_start_record_that_cannot_be_written_releases_no_worker(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    _refusing(world, lambda call, key: call == "create" and "/starts/" in key)
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HELD and "no start record" in outcome.line
    assert world.blob(plan_key(DEPLOYMENT, TODAY.isoformat())) is None
    assert not (world.root / "local" / "release").exists()
    assert any("no worker of it starts" in a for a in world.alarms())


def test_a_stored_plan_with_no_start_record_holds(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    # A plan stands with no start recorded, as when the records were lost.
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    world.supervisor().run(TODAY)
    assert world.held() and [p.boots for p in world.pauses()] == [("unrecorded-1",)]


def test_a_runs_halt_that_cannot_be_read_stores_no_stop(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    halt = run_halt_key(DEPLOYMENT, TODAY.isoformat())
    _refusing(world, lambda call, key: call == "get" and key == halt)
    outcome = world.supervisor().run(TODAY)
    # No run was halted, so it ends with the code the host restarts, and alarms.
    assert outcome.code == EXIT_FAILED and "could not be read" in outcome.line
    assert _stop_of(world) is None
    assert any("The host restarts the supervisor" in a for a in world.alarms())


def test_an_unclean_run_whose_plan_holds_no_decision_holds_everything(world: World) -> None:
    world.opened(decided=False)
    empty = Plan(1, DEPLOYMENT, YESTERDAY.isoformat(), Mode.PAPER, RELEASE, ())
    world.put(plan_key(DEPLOYMENT, YESTERDAY.isoformat()), encode(empty))
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HELD and "holds no decision" in outcome.line
    assert world.blob(strategy_halt_key(ACCOUNT, CHAN, COMPONENT, CAUSE, OPEN, 1)) is not None


def test_a_plan_with_no_decision_is_not_stored(world: World) -> None:
    from datetime import time

    from ithildinruntime.core import ExchangeTime

    # 2026-11-27 closes at 13:00 New York time, so a decision at 15:40 there is dropped.
    half = date(2026, 11, 27)
    world.timer.at = SESSIONS.session(half).open
    late = {CHAN: (ExchangeTime(time(15, 40)),)}
    outcome = world.supervisor(schedules=late).run(half)
    assert outcome.code == EXIT_PLAN and "holds no decision" in outcome.line
    assert world.blob(plan_key(DEPLOYMENT, half.isoformat())) is None


# Round 2: what a start that never finished its hold leaves


def test_a_start_whose_hold_failed_leaves_no_record_so_its_restart_holds(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    _start_on(world, "boot-a")
    bad = journal_entry_key(ACCOUNT, 4)
    world.put(bad, b"not an entry")
    # B finds A lost, and its hold fails and writes no start record of B.
    assert world.supervisor(boot="boot-b").run(TODAY).code == EXIT_HELD
    assert not (world.root / f"runs/{DEPLOYMENT}/{TODAY}/starts/boot-b").exists()
    (world.root / bad).unlink()
    _clear(world)
    # B's restart on the same boot still finds A lost, and holds.
    world.supervisor(boot="boot-b").run(TODAY)
    (pause,) = world.pauses()
    assert pause.boots == ("boot-a",) and world.held()


def test_a_start_that_dies_mid_hold_leaves_its_restart_holding(world: World, monkeypatch) -> None:
    from ithildinruntime.runtime import supervisor as module

    world.opened()
    world.ran()
    world.results()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    _start_on(world, "boot-a")

    class Killed(BaseException):
        pass

    def killed(**kwargs):
        raise Killed

    real = module.hold_after_unclean_stops
    monkeypatch.setattr(module, "hold_after_unclean_stops", killed)
    with pytest.raises(Killed):
        world.supervisor(boot="boot-b").run(TODAY)
    monkeypatch.setattr(module, "hold_after_unclean_stops", real)
    world.supervisor(boot="boot-b").run(TODAY)
    assert world.held() and [p.boots for p in world.pauses()] == [("boot-a",)]


def test_unreadable_start_records_each_hold_with_a_boot_of_their_own(
    world: World,
) -> None:
    world.opened()
    world.ran()
    world.results()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    _refusing(world, lambda call, key: call == "list" and "/starts/" in key)
    world.timer.at = OPEN
    world.most_ticks = 1
    world.supervisor().run(TODAY)
    _clear(world)
    world.supervisor().run(TODAY)
    pauses = world.pauses()
    assert len(pauses) == 2 and pauses[0].boots != pauses[1].boots
    assert all(p.boots[0].startswith("unknown-") for p in pauses)
    assert world.held(), "a second failure to read the starts holds again"


def test_two_unclean_runs_have_their_halts_stored_before_either_pause_oldest_first(
    world: World,
) -> None:
    world.journal.record(world.journal.view(), opening(), datetime(2026, 9, 25, 19, 40, tzinfo=UTC))
    world.ran(day=date(2026, 9, 30), stopped=False)
    world.ran(stopped=False)
    world.results()
    world.supervisor().run(TODAY)
    assert [p.run for p in world.pauses()] == ["2026-09-30", "2026-10-01"]


def test_a_standing_hold_on_live_names_the_owner(world: World) -> None:
    world.opened()
    world.put(_hold_key(), b"{}")
    world.supervisor(mode=Mode.LIVE).run(TODAY)
    standing = [a for a in world.alarms() if "still held" in a]
    assert standing and "The owner clears it" in standing[0]


# Round 2 of the review: what no test reached


def test_a_result_missing_at_the_second_decision_stores_no_stop(world: World) -> None:
    world.opened()
    world.ran()
    first, second = to_utc(TODAY, BeforeClose(60), SESSIONS), DECIDED[TODAY]
    twice = {CHAN: (BeforeClose(60), BeforeClose(20))}
    for stage in STAGES:
        world.put(result_key(DEPLOYMENT, TODAY.isoformat(), first, stage), b"{}")
    world.timer.at = CLOSE
    outcome = world.supervisor(schedules=twice).run(TODAY)
    assert outcome.code == EXIT_STALLED and _stop_of(world) is None
    (alarm,) = world.alarms()
    assert f"at {second:%Y-%m-%dT%H:%M}Z" in alarm and f"{first:%H:%M}Z" not in alarm


def test_halts_that_cannot_be_listed_are_named_and_halt_the_run(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    folder = strategy_halts_prefix(ACCOUNT, CHAN)
    _refusing(world, lambda call, key: call == "list" and key == folder)
    # A halt there may stand in for the run's own, so the run halts, and stores no stop.
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HALTED and _stop_of(world) is None
    (alarm,) = [a for a in world.alarms() if "still held" in a]
    assert f"{CHAN} (whose halts could not be read:" in alarm
    stored = decode(world.blob(run_halt_key(DEPLOYMENT, TODAY.isoformat())), RunHalt)
    assert "a halt still owed cannot be ruled out" in stored.reason


def test_halts_that_cannot_be_listed_at_the_stop_store_no_stop(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    folder = strategy_halts_prefix(ACCOUNT, CHAN)
    lists = 0

    def refused(call: str, key: str) -> bool:
        # The hold's read and the watch's read are answered, and the stop's read is refused.
        nonlocal lists
        if call == "list" and key == folder:
            lists += 1
            return lists > 2
        return False

    _refusing(world, refused)
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_FAILED and _stop_of(world) is None
    assert any("or a halt of a strategy standing in for it" in a for a in world.alarms())
    _refusing(world, lambda call, key: False)
    assert world.supervisor().run(TODAY).code == EXIT_STOPPED


def test_a_second_lost_machines_halts_take_the_next_count(world: World) -> None:
    world.opened()
    world.ran()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    _start_on(world, "boot-a")
    world.timer.at = OPEN
    world.most_ticks = 1
    world.supervisor(boot="boot-b").run(TODAY)
    _clear(world)
    world.supervisor(boot="boot-c").run(TODAY)
    second = strategy_halt_key(ACCOUNT, CHAN, COMPONENT, CAUSE, DECIDED[TODAY], 2)
    assert world.held() == [second.rpartition("/")[2]]


def test_a_link_planted_after_the_unlink_is_never_written_through(
    world: World, monkeypatch
) -> None:
    world.opened()
    world.ran()
    world.results()
    target = world.root.parent / "elsewhere"
    target.write_text("untouched\n")
    partial = world.root / "local" / "release.partial"
    real = Path.unlink

    def unlink(self, missing_ok=False):
        real(self, missing_ok=missing_ok)
        if self == partial:
            # Someone plants a link between the unlink and the open.
            self.symlink_to(target)

    monkeypatch.setattr(Path, "unlink", unlink)
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_PLAN and target.read_text() == "untouched\n"
    (alarm,) = [a for a in world.alarms() if "release file could not be written" in a]
    assert "Start the supervisor's unit again once its directory is writable" in alarm
    assert "no worker on this machine starts" in alarm and "run on" not in alarm


def test_a_release_file_that_cannot_be_rewritten_says_the_workers_released_earlier_run_on(
    world: World, monkeypatch
) -> None:
    world.opened()
    world.ran()
    world.results()
    _start_on(world, "boot-a")
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    (world.root / "local").mkdir(parents=True)
    (world.root / "local" / "release").write_text(f"{RELEASE}\n{TODAY}\nboot-a\n")

    def refused(*args, **kwargs):
        raise PermissionError("the directory is read-only")

    monkeypatch.setattr(Supervisor, "_write_release_file", refused)
    outcome = world.supervisor(boot="boot-a").run(TODAY)
    assert outcome.code == EXIT_PLAN
    (alarm,) = [a for a in world.alarms() if "release file could not be written" in a]
    assert "the workers it released earlier run on with no supervisor" in alarm
    assert "no worker on this machine starts" not in alarm


def test_a_release_file_that_is_not_utf_8_reads_as_not_released(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    (world.root / "local").mkdir(parents=True)
    (world.root / "local" / "release").write_bytes(b"\xff\n")
    assert world.supervisor()._released_here(TODAY.isoformat()) is False
    assert world.supervisor().run(TODAY).code == EXIT_STOPPED


def test_a_second_stop_with_no_start_record_holds_again(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(_plan(TODAY)))
    # Y finds the plan with no start record and holds, and its own record cannot be written.
    _refusing(world, lambda call, key: call == "create" and "/starts/" in key)
    assert world.supervisor(boot="boot-y").run(TODAY).code == EXIT_HELD
    (failed,) = [a for a in world.alarms() if "the start record of" in a]
    # A first start has no earlier record of its boot to stand in, so it does not watch on.
    assert "could not be written" in failed and "watches on" not in failed
    _clear(world)
    # Z still finds no start record, and holds again under a name of its own.
    world.supervisor(boot="boot-z").run(TODAY)
    assert [p.boots for p in world.pauses()] == [("unrecorded-1",), ("unrecorded-2",)]
    assert world.held()


def test_a_runs_halt_that_cannot_be_stored_says_to_pull_the_kill_switch(world: World) -> None:
    world.opened()
    world.ran()
    held = _doubtful(world, DECIDED[TODAY])
    world.timer.at = _deadline(DECIDED[TODAY]) + timedelta(seconds=1)
    halt = run_halt_key(DEPLOYMENT, TODAY.isoformat())
    _refusing(world, lambda call, key: call == "create" and key == halt)
    outcome = world.supervisor().run(TODAY)
    # Ended with the code the host restarts, so the halt is tried again, unless that ends 5 or 6.
    assert outcome.code == EXIT_FAILED and "could not be stored" in outcome.line
    assert any(held in a for a in world.alarms())
    assert any("Pull the account's kill switch" in a for a in world.alarms())
    assert any(
        "which tries the halt again once the store answers" in a
        and "A restart that ends with 5 or 6 does not" in a
        and "Nothing records the halt" not in a
        for a in world.alarms()
    )
    assert _stop_of(world) is None
    # A halt of each strategy stands in its place, which execution's controls refuse orders on.
    assert world.held() and "supervisor-run_halt-" in world.held()[0]
    assert Controls(world.store, ACCOUNT).strategy(CHAN, lambda call: call()) is not None
    _refusing(world, lambda call, key: False)
    assert world.supervisor().run(TODAY).code == EXIT_HALTED
    assert world.blob(halt) is not None


def test_a_runs_halt_that_cannot_be_stored_halts_each_strategy_of_the_table(world: World) -> None:
    world.opened()
    world.ran()
    _doubtful(world, DECIDED[TODAY])
    world.timer.at = _deadline(DECIDED[TODAY]) + timedelta(seconds=1)
    halt = run_halt_key(DEPLOYMENT, TODAY.isoformat())
    _refusing(world, lambda call, key: call == "create" and key == halt)
    both = {CHAN: (BeforeClose(20),), OTHER: (BeforeClose(20),)}
    # The release tracks one strategy's file, so the second is a copy of it under another name.
    folder = world.root / "strategies"
    folder.mkdir()
    for strategy in (CHAN, OTHER):
        shutil.copyfile(STRATEGIES / f"{CHAN}.toml", folder / f"{strategy}.toml")
    table = deployment_table(Deployment(DEPLOYMENT, ACCOUNT, both), folder)
    assert world.supervisor(table=table, schedules=both).run(TODAY).code == EXIT_FAILED
    # Every strategy the table names is held, not only the first.
    for strategy in (CHAN, OTHER):
        assert [k for k in world.held(strategy) if k.startswith("supervisor-run_halt-")]
    assert any(f"a halt holds {CHAN}, {OTHER}" in a for a in world.alarms())


def test_two_decisions_whose_results_are_all_stored_stop_the_run(world: World) -> None:
    world.opened()
    world.ran()
    first = to_utc(TODAY, BeforeClose(60), SESSIONS)
    for at in (first, DECIDED[TODAY]):
        for stage in STAGES:
            world.put(result_key(DEPLOYMENT, TODAY.isoformat(), at, stage), b"{}")
    twice = {CHAN: (BeforeClose(60), BeforeClose(20))}
    assert world.supervisor(schedules=twice).run(TODAY).code == EXIT_STOPPED
    assert _stop_of(world) is not None


def test_a_newer_runs_halt_that_cannot_be_stored_stores_no_pause_at_all(world: World) -> None:
    world.journal.record(world.journal.view(), opening(), datetime(2026, 9, 25, 19, 40, tzinfo=UTC))
    world.ran(day=date(2026, 9, 30), stopped=False)
    world.ran(stopped=False)
    newer = strategy_halt_key(ACCOUNT, CHAN, COMPONENT, CAUSE, DECIDED[YESTERDAY], 1)
    _refusing(world, lambda call, key: call == "create" and key == newer)
    assert world.supervisor().run(TODAY).code == EXIT_HELD
    # Every halt is stored before any pause, so the older run's pause never lands alone.
    assert world.pauses() == []


def test_unreadable_start_records_before_any_plan_hold_nothing_of_today(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    _refusing(world, lambda call, key: call == "list" and "/starts/" in key)
    outcome = world.supervisor().run(TODAY)
    # With no plan stored there is no run of today to hold, and the start releases no worker.
    assert outcome.code == EXIT_HELD, outcome.line
    assert world.held() == [] and world.pauses() == []
    assert not (world.root / "local" / "release").exists()


def test_the_exit_codes_are_the_numbers_the_hosts_units_read() -> None:
    from ithildinruntime.runtime import entry, supervisor

    # #202's units restart on 1 and 7 and never on 2 to 6, so these numbers are a contract.
    assert (supervisor.EXIT_STOPPED, supervisor.EXIT_FAILED) == (0, 1)
    assert (entry.EXIT_SETTING, supervisor.EXIT_HALTED, supervisor.EXIT_STALLED) == (2, 3, 4)
    assert (supervisor.EXIT_HELD, supervisor.EXIT_PLAN, entry.EXIT_PLAN) == (5, 6, 6)
    assert supervisor.EXIT_ASKED == 7


def test_a_hold_that_stores_some_halts_names_the_strategy_it_left_free(world: World) -> None:
    from ithildinruntime.runtime.supervisor.hold import HoldFailed, Starts, hold_after_unclean_stops

    world.opened()
    world.ran()
    world.put(journal_entry_key(ACCOUNT, 4), b"not an entry")
    _refusing(world, lambda call, key: call == "create" and f"/{CHAN}/halts/" in key)
    with pytest.raises(HoldFailed):
        hold_after_unclean_stops(
            storage=world.storage,
            journal=Journal(world.store, ACCOUNT, WRITER),
            mode=Mode.PAPER,
            deployment=DEPLOYMENT,
            account=ACCOUNT,
            strategies=[CHAN, OTHER],
            today=TODAY,
            starts=Starts(False, (), False),
            sessions=SESSIONS,
            now=world.timer.now,
            alarm=world.alerts.alarm,
        )
    (alarm,) = [a for a in world.alarms() if "unclean stop" in a]
    assert f"{OTHER} stays held" in alarm
    assert f"No halt of {CHAN} could be stored, so it is not held: pull the kill switch" in alarm


def test_a_start_whose_records_cannot_be_read_releases_no_worker(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    _refusing(world, lambda call, key: call == "list" and "/starts/" in key)
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HELD
    assert not (world.root / "local" / "release").exists()


def test_a_supervised_restart_whose_record_fails_watches_on(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    assert world.supervisor(boot="boot-b").run(TODAY).code == EXIT_STOPPED
    # The restart on the same boot cannot record itself, and its boot's first record stands.
    world.put(run_stop_key(DEPLOYMENT, TODAY.isoformat()) + ".gone", b"")
    (world.root / run_stop_key(DEPLOYMENT, TODAY.isoformat())).unlink()
    _refusing(world, lambda call, key: call == "create" and "/starts/" in key)
    outcome = world.supervisor(boot="boot-b").run(TODAY)
    assert outcome.code == EXIT_STOPPED and _stop_of(world) is not None
    assert not any("no worker of it starts" in a for a in world.alarms())
    (failed,) = [a for a in world.alarms() if "the start record of" in a]
    assert "could not be written" in failed
    assert "This boot's first record stands, so it watches on" in failed


def test_an_unrecorded_start_after_this_machine_released_says_its_workers_run_on(
    world: World,
) -> None:
    world.opened()
    world.ran()
    world.results()
    (world.root / "local").mkdir(parents=True)
    (world.root / "local" / "release").write_text(f"{RELEASE}\n{TODAY}\nboot-a\n")
    _refusing(world, lambda call, key: call == "list" and "/starts/" in key)
    world.timer.at = OPEN
    world.supervisor().run(TODAY)
    (alarm,) = [a for a in world.alarms() if "no start record" in a]
    assert "the workers it released earlier run on" in alarm and "no worker" not in alarm
    assert "Start the supervisor's unit again once the store answers" in alarm
    assert "that names this machine could be read or written" in alarm


def test_a_plan_that_cannot_be_read_with_unreadable_records_holds(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    plan = plan_key(DEPLOYMENT, TODAY.isoformat())
    _refusing(
        world,
        lambda call, key: (call == "list" and "/starts/" in key) or (call == "get" and key == plan),
    )
    outcome = world.supervisor().run(TODAY)
    # A plan that cannot be read may stand, so the hold tries today's run and holds.
    assert outcome.code == EXIT_HELD and world.held()


def test_a_restart_that_finds_a_plan_the_codec_cannot_read_ends_with_six(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), b"{not a plan")
    _start_on(world, "boot-a")
    outcome = world.supervisor(boot="boot-a").run(TODAY)
    assert outcome.code == EXIT_PLAN and "holds no plan the codec reads" in outcome.line
    assert not (world.root / "local" / "release").exists()


def test_a_first_start_that_finds_a_plan_the_codec_cannot_read_holds(world: World) -> None:
    world.opened()
    world.ran()
    world.results()
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), b"{not a plan")
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HELD and world.held()


def test_a_restart_after_the_runs_halt_could_not_be_stored_stores_the_halt_not_a_stop(
    world: World,
) -> None:
    world.opened()
    world.ran()
    world.results()
    halt = run_halt_key(DEPLOYMENT, TODAY.isoformat())
    starts = world.root / starts_prefix(DEPLOYMENT, TODAY.isoformat())

    def refused(call: str, key: str) -> bool:
        # Once its start is recorded, the journal cannot be read and the run's halt not stored.
        armed = starts.exists() and any(starts.rglob("*.json"))
        return (call == "list" and key.startswith("journal/") and armed) or (
            call == "create" and key == halt
        )

    _refusing(world, refused)
    world.timer.at = OPEN
    assert world.supervisor().run(TODAY).code == EXIT_FAILED
    (name,) = world.held()
    # The stand-in says it stands in for the run's halt, and carries that halt's reason.
    stand_in = decode(world.blob(strategy_halts_prefix(ACCOUNT, CHAN) + name), StrategyHalt)
    assert stand_in.reason.startswith("the run's halt could not be stored: ")
    assert "the journal or an order's deadline cannot be read" in stand_in.reason
    # The store answers again and the journal reads, so nothing is late on the restart.
    _refusing(world, lambda call, key: False)
    world.timer.at = CLOSE + timedelta(minutes=10)
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_HALTED and _stop_of(world) is None
    stored = decode(world.blob(halt), RunHalt)
    assert "the journal or an order's deadline cannot be read" in stored.reason
    assert not stored.reason.startswith("the run's halt could not be stored")


def test_a_runs_halt_and_strategy_halts_that_cannot_be_stored_name_what_is_not_held(
    world: World,
) -> None:
    world.opened()
    world.ran()
    _doubtful(world, DECIDED[TODAY])
    world.timer.at = _deadline(DECIDED[TODAY]) + timedelta(seconds=1)
    halt = run_halt_key(DEPLOYMENT, TODAY.isoformat())
    _refusing(world, lambda call, key: call == "create" and (key == halt or "/halts/" in key))
    assert world.supervisor().run(TODAY).code == EXIT_FAILED
    (alarm,) = [a for a in world.alarms() if "could not be stored" in a and "In its place" in a]
    assert "no halt holds any strategy" in alarm and "Pull the account's kill switch" in alarm
    assert f"no halt of {CHAN}" in alarm and "so it is not held" in alarm
    assert "Nothing records the halt" in alarm and "tries the halt again" not in alarm
    assert not world.held()


def _stand_in_owed(world: World) -> str:
    """A first start whose run's halt is refused, so a halt of each strategy stands in for it."""
    world.opened()
    world.ran()
    world.results()
    halt = run_halt_key(DEPLOYMENT, TODAY.isoformat())
    starts = world.root / starts_prefix(DEPLOYMENT, TODAY.isoformat())

    def refused(call: str, key: str) -> bool:
        armed = starts.exists() and any(starts.rglob("*.json"))
        return (call == "list" and key.startswith("journal/") and armed) or (
            call == "create" and key == halt
        )

    _refusing(world, refused)
    world.timer.at = OPEN
    assert world.supervisor().run(TODAY).code == EXIT_FAILED
    assert world.held()
    return halt


def test_a_restart_in_the_session_stores_the_halt_owed_at_once(world: World) -> None:
    halt = _stand_in_owed(world)
    _refusing(world, lambda call, key: False)
    world.timer.at = OPEN + timedelta(minutes=30)
    ticks = world.ticks
    assert world.supervisor().run(TODAY).code == EXIT_HALTED and _stop_of(world) is None
    # Stored before the first tick, not at the stop after the close.
    assert world.ticks == ticks
    assert decode(world.blob(halt), RunHalt).halted_at == OPEN + timedelta(minutes=30)


def test_a_stand_in_written_while_the_run_is_watched_halts_it_at_the_stop(world: World) -> None:
    from ithildinruntime.runtime.supervisor import OWED, RUN_HALT_CAUSE

    world.opened()
    world.ran()
    world.results()
    world.timer.at = CLOSE
    stand_in = StrategyHalt(
        RUN_RECORD_FORMAT, ACCOUNT, CHAN, RUN_HALT_CAUSE, f"{OWED}late", TODAY.isoformat(), CLOSE
    )
    key = strategy_halt_key(ACCOUNT, CHAN, COMPONENT, RUN_HALT_CAUSE, CLOSE, 1)

    def write_once(world: World) -> None:
        if world.blob(key) is None:
            world.put(key, encode(stand_in))

    # It lands after the watch's first read, so only the stop's read finds it.
    world.on_tick = write_once
    assert world.supervisor().run(TODAY).code == EXIT_HALTED and _stop_of(world) is None
    stored = decode(world.blob(run_halt_key(DEPLOYMENT, TODAY.isoformat())), RunHalt)
    assert stored.reason == "late"


def test_a_stand_in_that_cannot_be_read_halts_the_run_naming_its_key(world: World) -> None:
    from ithildinruntime.runtime.supervisor import RUN_HALT_CAUSE

    world.opened()
    world.ran()
    world.results()
    key = strategy_halt_key(ACCOUNT, CHAN, COMPONENT, RUN_HALT_CAUSE, OPEN, 1)
    world.put(key, b"not json")
    assert world.supervisor().run(TODAY).code == EXIT_HALTED
    assert _stop_of(world) is None
    stored = decode(world.blob(run_halt_key(DEPLOYMENT, TODAY.isoformat())), RunHalt)
    assert f"the halt at {key} cannot be read" in stored.reason


def test_a_stand_in_for_an_earlier_runs_halt_leaves_todays_stop_alone(world: World) -> None:
    from ithildinruntime.runtime.supervisor import OWED, RUN_HALT_CAUSE

    world.opened()
    world.ran()
    world.results()
    earlier = StrategyHalt(
        RUN_RECORD_FORMAT, ACCOUNT, CHAN, RUN_HALT_CAUSE, f"{OWED}late", "2026-10-01", CLOSE
    )
    key = strategy_halt_key(ACCOUNT, CHAN, COMPONENT, RUN_HALT_CAUSE, OPEN, 1)
    world.put(key, encode(earlier))
    # Today's run owes no halt, so it stores its stop, and the earlier halt stays named.
    assert world.supervisor().run(TODAY).code == EXIT_STOPPED and _stop_of(world) is not None
    assert world.blob(run_halt_key(DEPLOYMENT, TODAY.isoformat())) is None
    assert any("still held" in a for a in world.alarms())
