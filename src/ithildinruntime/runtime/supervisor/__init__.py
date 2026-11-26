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
2. It reads today's start records, then writes its own, numbered one past the
   newest of its boot, so the hold can tell a lost machine from a restart.
3. Outside a backtest it runs the hold after an unclean stop, as `hold` says.
   A hold check that cannot finish writes no plan, so no worker starts.
4. It stores the run's plan, create-only. An equal plan already stored is
   done, and a different one ends the start.
5. It writes the local release file, naming the plan's release.
6. On each tick, outside a backtest, it checks the deadline of each order in
   doubt, as `deadline` says, and halts the run past it.
7. It stores the run's stop once every stage has a result for every decision
   and, outside a backtest, the close has passed by `STOP_AFTER_CLOSE`. #151
   adds a third condition, that the run has read the day's fills. Outside a
   backtest it first reads the run's halt, and stores the stop only where the
   store answers that there is none.
8. At `STALL_AFTER_CLOSE` past the close, a result still missing alarms and the
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
    CodecError,
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
    supervisor_start_key,
)
from ithildinruntime.journal import DOUBTFUL, Journal
from ithildinruntime.runtime.loop import Table

from .deadline import STALL_AFTER_CLOSE, STOP_AFTER_CLOSE, past_deadline
from .hold import HoldFailed, Starts, hold_after_unclean_stops

__all__ = [
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
        try:
            starts = self._start(run)
        except (Halt, StoreError, CodecError) as error:
            # The hold cannot tell a lost machine from a restart, so it holds.
            starts = Starts(plan_stored=True, other_boots=("unknown",), this_boot=False)
            self._alerts.alarm(
                f"{self._deployment}: the start records of {run} could not be "
                f"read or written: {_said(error)}"
            )
        if self._trading:
            try:
                hold_after_unclean_stops(
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
                )
            except HoldFailed as failed:
                return Outcome(EXIT_HELD, f"the hold check could not finish: {failed}")
        try:
            plan = self._store_plan(first, last)
        except Halt as halt:
            return Outcome(EXIT_PLAN, halt.reason)
        self._write_release_file(plan.release)
        return self._watch(plan)

    # Starting

    def _start(self, run: str) -> Starts:
        """Read today's start records, then write this one's."""
        prefix = starts_prefix(self._deployment, run)
        counts: dict[str, int] = {}
        for key in self._store.list(prefix, None):
            boot, _, name = key.removeprefix(prefix).partition("/")
            counts[boot] = max(counts.get(boot, 0), int(name.removesuffix(".json")))
        plan_stored = self._storage.exists(plan_key(self._deployment, run))
        count = counts.get(self._boot, 0) + 1
        record = SupervisorStart(
            format=RUN_RECORD_FORMAT,
            deployment=self._deployment,
            run=run,
            boot=self._boot,
            count=count,
            release=self._release,
            started_at=self._timer.now(),
        )
        self._storage.keep(
            supervisor_start_key(self._deployment, run, self._boot, count), encode(record)
        )
        others = tuple(sorted(b for b in counts if b != self._boot))
        return Starts(plan_stored, others, self._boot in counts)

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

    def _write_release_file(self, release: str) -> None:
        """Name the plan's release in the local release file, whole or not at all."""
        if self._release_file is None:
            return
        self._release_file.parent.mkdir(parents=True, exist_ok=True)
        partial = self._release_file.with_name(self._release_file.name + ".partial")
        partial.write_text(release + "\n")
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
                    "close, so it ends with no stop and its next start holds every strategy"
                )
                return Outcome(EXIT_STALLED, f"{plan.run} stalled with results missing: {said}")
            if self._wait(self._tick):
                return Outcome(EXIT_STOPPED, f"asked to stop before {plan.run} stopped")

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
        except (Halt, StoreError, CodecError, KeyError) as error:
            return [
                f"the journal cannot be read, so no order in doubt can be ruled out: {_said(error)}"
            ]
        return past_deadline(doubtful, now, self._sessions)

    def _halt_run(
        self, plan: Plan, stages: list[str], decisions: list[datetime], late: list[str]
    ) -> Outcome:
        """Halt the whole run: its halt first, then a halt as every result still missing."""
        reason = "; ".join(late)
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
        self._alerts.alarm(
            f"{self._deployment}: the supervisor halted {plan.run}, so no order is sent and the "
            f"run ends with no stop: {reason}"
        )
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
