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

import io
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderStore, LogAlerts
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
    strategy_halt_key,
    strategy_halts_prefix,
    supervisor_start_key,
    to_utc,
)
from ithildinruntime.execution.controls import Controls
from ithildinruntime.journal import Journal, PauseEntry, record_of
from ithildinruntime.runtime.deployment import Deployment, deployment_table
from ithildinruntime.runtime.supervisor import (
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
    assert (
        CLOSE + STOP_AFTER_CLOSE
        <= stop.stopped_at
        < CLOSE + STOP_AFTER_CLOSE + timedelta(seconds=TICK)
    )
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
    assert world.timer.at - CLOSE < STALL_AFTER_CLOSE + timedelta(seconds=TICK)
    (alarm,) = world.alarms()
    assert "execution/send at 2026-10-02T19:40Z" in alarm and "an hour after the close" in alarm


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
    assert (world.root / "local" / "release").read_text() == f"{RELEASE}\n"


def test_another_plan_already_stored_ends_the_start_and_starts_no_worker(world: World) -> None:
    world.opened()
    world.ran()
    other = Plan(1, DEPLOYMENT, TODAY.isoformat(), Mode.PAPER, "sha256:other", ())
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(other))
    outcome = world.supervisor().run(TODAY)
    assert outcome.code == EXIT_PLAN and "sha256:other" in outcome.line
    assert not (world.root / "local" / "release").exists()


def test_a_stored_plan_of_another_mode_ends_the_start(world: World) -> None:
    world.opened()
    world.ran()
    other = Plan(1, DEPLOYMENT, TODAY.isoformat(), Mode.SHADOW, RELEASE, ())
    world.put(plan_key(DEPLOYMENT, TODAY.isoformat()), encode(other))
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
    return min(sent_at + IN_DOUBT_LIMIT, CLOSE - timedelta(minutes=10))


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
        # At the deadline the order is not yet past it.
        assert halt is None and outcome.code == EXIT_STOPPED, outcome.line
        return
    assert outcome.code == EXIT_HALTED, outcome.line
    record = decode(halt, RunHalt)
    assert held in record.reason and f"{deadline:%H:%M:%S}" in record.reason
    # A halt is every stage's result for the decision left, and execution's send is one.
    send = world.blob(result_key(DEPLOYMENT, TODAY.isoformat(), DECIDED[TODAY], "execution/send"))
    assert send is not None and b"the supervisor halted the run" in send
    (alarm,) = world.alarms()
    assert "halted 2026-10-02" in alarm and held in alarm


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
    assert outcome.code == EXIT_HALTED and "the journal cannot be read" in outcome.line
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
    pause = PauseEntry(DEPLOYMENT, "2026-10-01", AT, (CHAN,), "a test")
    _, view = world.journal.record(world.journal.view(), pause, AT)
    _, view = world.journal.record(view, pause, AT)
    after = Journal(world.store, ACCOUNT).view()
    assert after.book.positions_by_strategy == before.positions_by_strategy
    assert after.book.cash == before.cash
    # A second pause of one run keeps the first, and halts nothing.
    assert after.paused == {(DEPLOYMENT, "2026-10-01"): 4}
