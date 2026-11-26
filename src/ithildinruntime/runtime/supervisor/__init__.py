"""The supervisor: it writes the run's plan, watches its results, and writes the run's stop.

design.md's `### Rule 7: how a run starts` and `### Rule 7: how a run stops`
give the rules. The supervisor starts and stops no process: the host's units
keep every worker running, and the host's path unit starts them once the
supervisor writes the local release file. It holds nothing in memory that a
restart would need, because it reads the plan, the results and the journal
from storage each time.

It imports `core`, `journal`, `adapters` and `runtime.loop` alone, as
05-building-blocks.md's import rule says, so `runtime/entry.py` builds the
table, the settings and the adapters and hands them in. It runs no receive
loop, because `ReceiveLoop` refuses a component with no stage. It reads
storage on its own tick instead, which its deadline needs in any case, and it
never waits on `Clock.wait_until`.

One start runs in this order.

1. A day with no session ends at once, with no plan and no release file.
2. It reads today's start records, so the hold can tell a lost machine from a
   restart.
3. Outside a backtest it runs the hold after an unclean stop, as `hold` says,
   and alarms on each strategy a halt still holds from before. A hold check
   that cannot finish writes no plan and no release file.
4. It records its own start, numbered one past the newest of its boot, only now
   that the hold has finished, so a start that died mid-hold leaves none.
5. It stores the run's plan, create-only. An equal plan already stored is
   done, and a different one ends the start.
6. It writes the local release file, naming the plan's release, the run and
   this boot.
7. On each tick, outside a backtest, it checks the deadline of each order in
   doubt, as `deadline` says, and halts the run past it.
8. It stores the run's stop once every stage has a result for every decision
   and, outside a backtest, the close has passed by `STOP_AFTER_CLOSE`. #151
   adds a third condition, that the run has read the day's fills. Outside a
   backtest it first reads the run's halt, and stores the stop only where the
   store answers that there is none.
9. At `STALL_AFTER_CLOSE` past the close, a result still missing alarms and the
   start ends with no stop.

It is safe as one instance, because each write it makes is create-only. A
second instance acting on the same read of the run needs the plan's lease,
which #210 builds.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ithildinruntime.core import (
    RUN_RECORD_FORMAT,
    Alerts,
    BlobHeader,
    BlobStore,
    DecisionResult,
    Halt,
    Mode,
    Plan,
    RunHalt,
    RunStop,
    ScheduleTime,
    SessionTable,
    Storage,
    StoreError,
    SupervisorStart,
    Timer,
    build_plan,
    decode,
    encode,
    load_sessions,
    plan_key,
    result_key,
    run_halt_key,
    run_part,
    run_stop_key,
    starts_prefix,
    strategy_halts_prefix,
    supervisor_start_key,
)
from ithildinruntime.journal import DOUBTFUL, Journal
from ithildinruntime.runtime.loop import Table

from .deadline import STALL_AFTER_CLOSE, STOP_AFTER_CLOSE, past_deadline
from .hold import HoldFailed, Starts, hold_after_unclean_stops, who

__all__ = [
    "EXIT_ASKED",
    "EXIT_HALTED",
    "EXIT_HELD",
    "EXIT_PLAN",
    "EXIT_STALLED",
    "EXIT_STOPPED",
    "Outcome",
    "Supervisor",
]

# What a start ends with, for the host's unit. 0 is a run stopped, or nothing to do.
EXIT_STOPPED = 0
EXIT_HALTED = 3
EXIT_STALLED = 4
EXIT_HELD = 5
EXIT_PLAN = 6
EXIT_ASKED = 7
_COMPONENT = "supervisor"


@dataclass(frozen=True)
class Outcome:
    """How one start ended: the exit code, and the one line that says why."""

    code: int
    line: str


class Supervisor:
    """One deployment's supervisor, for one run."""

    def __init__(
        self,
        *,
        mode: Mode,
        deployment: str,
        account: str,
        store: BlobStore,
        storage: Storage,
        table: Table,
        schedules: Mapping[str, Sequence[ScheduleTime]],
        release: str,
        boot: str,
        timer: Timer,
        alerts: Alerts,
        release_file: Path | None,
        wait: Callable[[float], bool],
        tick: float = 10.0,
        sessions: SessionTable | None = None,
        span: tuple[date, date] | None = None,
        say: Callable[[str], None] = print,
    ) -> None:
        self._mode = Mode(mode)
        if (self._mode is Mode.BACKTEST) != (span is not None):
            raise ValueError("a backtest's supervisor is handed its span, and only a backtest's")
        self._deployment = deployment
        self._account = account
        self._store = store
        self._storage = storage
        self._table = table
        self._schedules = schedules
        self._release = release
        self._boot = boot
        self._timer = timer
        self._alerts = alerts
        self._release_file = release_file
        self._wait = wait
        self._tick = tick
        self._sessions = sessions or load_sessions()
        self._span = span
        self._say = say

    @property
    def _trading(self) -> bool:
        return self._mode is not Mode.BACKTEST

    def run(self, today: date) -> Outcome:
        """One start of the supervisor on `today`, to the run's stop or the reason it ends."""
        if self._span is not None:
            first, last = self._span
        elif today not in self._sessions.sessions:
            return Outcome(EXIT_STOPPED, f"no session on {today.isoformat()}, so no run starts")
        else:
            first = last = today
        run = run_part(self._mode, first, last)
        count: int | None = None
        try:
            starts, count = self._read_starts(run)
        except Exception as error:
            # The hold cannot tell a lost machine from a restart, so it holds, and a boot
            # of its own for this start keeps a pause of it from covering any other.
            unknown = f"unknown-{secrets.token_hex(4)}"
            starts = Starts(True, (unknown,), False, self._released_here(run))
            self._alerts.alarm(
                f"{self._deployment}: the start records of {run} could not be read: {_said(error)}"
            )
        if self._trading:
            try:
                found = hold_after_unclean_stops(
                    storage=self._storage,
                    journal=Journal(self._store, self._account, secrets.token_hex(16)),
                    mode=self._mode,
                    deployment=self._deployment,
                    account=self._account,
                    strategies=[s.name for s in self._table.strategies],
                    today=today,
                    starts=starts,
                    sessions=self._sessions,
                    now=self._timer.now,
                    alarm=self._alerts.alarm,
                    say=self._say,
                )
            except HoldFailed as failed:
                return Outcome(EXIT_HELD, f"the hold check could not finish: {failed}")
            if not found:
                # The hold's own alarm already names what it held on this start.
                self._name_standing_halts(today)
        if count is not None:
            # Recorded only once the hold has finished, so a start that died mid-hold, or
            # whose hold failed, never makes its restart read as one the host supervised.
            self._record_start(run, count)
        try:
            plan = self._store_plan(first, last)
        except Halt as halt:
            self._alerts.alarm(
                f"{self._deployment}: the supervisor could not store the plan of {run}, so "
                f"nothing watches its orders in doubt and nothing writes its stop: {halt.reason}. "
                "A plan of another release means the pin moved during the session, and the "
                "next session runs the new release"
            )
            return Outcome(EXIT_PLAN, halt.reason)
        try:
            self._write_release_file(plan.release, plan.run)
        except OSError as error:
            # No worker starts without the file, so this fails closed, and says so.
            self._alerts.alarm(
                f"{self._deployment}: the local release file could not be written, so no worker "
                f"on this machine starts: {type(error).__name__}: {error}"
            )
            return Outcome(EXIT_PLAN, f"the local release file could not be written: {error}")
        return self._watch(plan)

    def _name_standing_halts(self, today: date) -> None:
        """Alarm on each strategy a halt still holds, which a later clean start never clears."""
        held = []
        for strategy in sorted(s.name for s in self._table.strategies):
            try:
                if self._store.list(strategy_halts_prefix(self._account, strategy), None):
                    held.append(strategy)
            except StoreError as error:
                held.append(f"{strategy}, whose halts could not be read ({_said(error)}),")
        if held:
            self._alerts.alarm(
                f"{self._deployment}: on {today.isoformat()} {', '.join(held)} is still held by a "
                f"halt nobody has cleared, so it sends no new order. {who(self._mode)}, with "
                "#203's `ithildin halt clear` once it is built"
            )

    # Starting

    def _read_starts(self, run: str) -> tuple[Starts, int]:
        """What today's start records say, and the count this start's record will take."""
        prefix = starts_prefix(self._deployment, run)
        counts: dict[str, int] = {}
        for key in self._store.list(prefix, None):
            boot, _, name = key.removeprefix(prefix).partition("/")
            counts[boot] = max(counts.get(boot, 0), int(name.removesuffix(".json")))
        plan_stored = self._storage.exists(plan_key(self._deployment, run))
        others = tuple(sorted(b for b in counts if b != self._boot))
        starts = Starts(plan_stored, others, self._boot in counts, self._released_here(run))
        return starts, counts.get(self._boot, 0) + 1

    def _released_here(self, run: str) -> bool:
        """Whether this machine's local release file already released its workers into `run`."""
        if self._release_file is None:
            return False
        try:
            lines = self._release_file.read_text().split("\n")
        except OSError:
            return False
        return lines[1:3] == [run, self._boot]

    def _record_start(self, run: str, count: int) -> None:
        """Record this start on its boot, which a later start reads."""
        record = SupervisorStart(
            format=RUN_RECORD_FORMAT,
            deployment=self._deployment,
            run=run,
            boot=self._boot,
            count=count,
            release=self._release,
            started_at=self._timer.now(),
        )
        try:
            self._storage.keep(
                supervisor_start_key(self._deployment, run, self._boot, count), encode(record)
            )
        except Exception as error:
            # A restart then reads as a lost machine and holds, which errs toward stopping.
            self._alerts.alarm(
                f"{self._deployment}: the start record of {run} could not be written: "
                f"{_said(error)}. A restart on this machine holds every strategy"
            )

    def _store_plan(self, first: date, last: date) -> Plan:
        built = build_plan(
            deployment=self._deployment,
            mode=self._mode,
            release=self._release,
            schedules=self._schedules,
            first=first,
            last=last,
            table=self._sessions,
        )
        plan, key = built.plan, plan_key(self._deployment, built.plan.run)
        if not plan.decisions:
            raise Halt(f"the plan of {plan.run} holds no decision")
        if self._storage.claim(key, encode(plan)):
            return plan
        stored = decode(self._storage.read_blob(key), Plan)
        if stored.mode is not self._mode:
            raise Halt(
                f"the stored plan of {plan.run} is for {stored.mode.value}, and this "
                f"environment runs {self._mode.value}"
            )
        raise Halt(
            f"another plan of {plan.run} is stored, on release {stored.release}, so this start, "
            f"on {plan.release}, writes none"
        )

    def _write_release_file(self, release: str, run: str) -> None:
        """Name the plan's release, its run and this boot in the local release file, whole.

        Its first line is the release the host's path unit reads. The run and the
        boot follow, so a file left from another run or from before a reboot
        releases no worker.
        """
        if self._release_file is None:
            return
        self._release_file.parent.mkdir(parents=True, exist_ok=True)
        partial = self._release_file.with_name(self._release_file.name + ".partial")
        partial.unlink(missing_ok=True)
        # Created afresh and never through a link someone planted at its name.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(partial, flags, 0o644)
        with os.fdopen(descriptor, "w") as file:
            file.write(f"{release}\n{run}\n{self._boot}\n")
        os.replace(partial, self._release_file)

    # Watching

    def _watch(self, plan: Plan) -> Outcome:
        stages = [stage.name for stage in self._table.stages]
        decisions = [d.at for d in plan.decisions]
        close = self._sessions.session(self._sessions.opened_by(decisions[-1]).day).close
        first_open = dict.fromkeys(stages, 0)
        journal = Journal(self._store, self._account)
        while True:
            now = self._timer.now()
            if self._trading:
                late = self._late(journal, now)
                if late:
                    return self._halt_run(plan, stages, decisions, late)
            missing = self._missing(plan.run, stages, decisions, first_open)
            if not missing and (not self._trading or now >= close + STOP_AFTER_CLOSE):
                return self._stop(plan)
            if self._trading and missing and now >= close + STALL_AFTER_CLOSE:
                said = "; ".join(f"{stage} at {at:%Y-%m-%dT%H:%M}Z" for stage, at in missing)
                self._alerts.alarm(
                    f"{self._deployment}: {plan.run} has no result for {said} an hour after the "
                    "close, so the supervisor ends with no stop. The next session's start holds "
                    "every strategy, unless the missing result lands and a person starts the "
                    "supervisor's unit again before then, which stores the stop"
                )
                return Outcome(EXIT_STALLED, f"{plan.run} stalled with results missing: {said}")
            if self._wait(self._tick):
                self._alerts.alarm(
                    f"{self._deployment}: the supervisor was asked to stop before {plan.run}'s "
                    "stop was stored, so nothing watches the rest of the session until it starts "
                    "again"
                )
                return Outcome(EXIT_ASKED, f"asked to stop before {plan.run} stopped")

    def _missing(
        self, run: str, stages: list[str], decisions: list[datetime], first_open: dict[str, int]
    ) -> list[tuple[str, datetime]]:
        """Each stage and decision with no result yet, reading only past what is known stored."""
        missing = []
        for stage in stages:
            n = first_open[stage]
            try:
                while n < len(decisions) and self._storage.exists(
                    result_key(self._deployment, run, decisions[n], stage)
                ):
                    n += 1
            except Halt:
                # A store that does not answer says nothing yet, and the next tick reads again.
                pass
            first_open[stage] = n
            missing += [(stage, at) for at in decisions[n:]]
        return missing

    def _late(self, journal: Journal, now: datetime) -> list[str]:
        """Each order in doubt past its deadline, or the reason the journal cannot rule one out."""
        try:
            view = journal.view()
            doubtful = [
                (order, view.decisions[order.decision].decision_at)
                for order in view.live.values()
                if order.state in DOUBTFUL
            ]
            return past_deadline(doubtful, now, self._sessions)
        except Exception as error:
            # Whatever fails, no order in doubt can be ruled out, so the run halts.
            return [
                "the journal or an order's deadline cannot be read, so no order in doubt can be "
                f"ruled out: {_said(error)}"
            ]

    def _halt_run(
        self, plan: Plan, stages: list[str], decisions: list[datetime], late: list[str]
    ) -> Outcome:
        """Halt the whole run: its halt first, then a halt as every result still missing."""
        reason = "; ".join(late)
        # Alarmed first, so the orders are named even where the halt cannot be stored.
        self._alerts.alarm(
            f"{self._deployment}: the supervisor halts {plan.run}, so no order is sent and the "
            f"run ends with no stop: {reason}. Settle each order named at the broker. The next "
            f"session's start holds every strategy. {who(self._mode)}"
        )
        record = RunHalt(
            format=RUN_RECORD_FORMAT,
            deployment=self._deployment,
            run=plan.run,
            reason=reason,
            halted_at=self._timer.now(),
        )
        self._storage.keep(run_halt_key(self._deployment, plan.run), encode(record))
        halt = Halt(f"the supervisor halted the run: {reason}")
        for at in decisions:
            for stage in stages:
                key = result_key(self._deployment, plan.run, at, stage)
                if self._storage.exists(key):
                    continue
                self._storage.put_result(key, self._halt_result(plan, stage, at, halt))
        return Outcome(EXIT_HALTED, f"halted {plan.run}: {reason}")

    def _halt_result(self, plan: Plan, stage: str, at: datetime, halt: Halt) -> DecisionResult:
        row = self._table.stage(stage)
        header = BlobHeader(
            format=1,
            deployment=self._deployment,
            run=plan.run,
            stage=stage,
            kind=str(row.kind),
            decision_at=at,
            inputs={},
            release=plan.release,
            config_hashes=self._table.hashes_for(stage),
        )
        return DecisionResult(header=header, outputs=(), halt=self._storage.halt_record(halt))

    def _stop(self, plan: Plan) -> Outcome:
        if self._trading:
            try:
                halted = self._storage.find_blob(run_halt_key(self._deployment, plan.run))
            except Halt as halt:
                return Outcome(
                    EXIT_HALTED, f"the run's halt could not be read, so no stop: {halt.reason}"
                )
            if halted is not None:
                return Outcome(EXIT_HALTED, f"{plan.run} is halted, so it stores no stop")
        record = RunStop(
            format=RUN_RECORD_FORMAT,
            deployment=self._deployment,
            run=plan.run,
            release=plan.release,
            stopped_at=self._timer.now(),
        )
        self._storage.keep(run_stop_key(self._deployment, plan.run), encode(record))
        return Outcome(EXIT_STOPPED, f"{plan.run} stopped cleanly")


def _said(error: BaseException) -> str:
    return error.reason if isinstance(error, Halt) else f"{type(error).__name__}: {error}"
