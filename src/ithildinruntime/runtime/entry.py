"""Each command's entry point: the supervisor's, every worker's and the host's forwarder's.

`pyproject.toml`'s `[project.scripts]` names one command per process, each a
`main` here. A `main` reads the machine's settings once, as `runtime/settings.py`
says, builds the adapters the environment's row in `runtime/deployment.py`
names, and hands plain values to a core, `run_<worker>` or `run_supervisor`.
CI starts the same cores with a folder, named pipes and stand-in time servers,
and a backtest runs through them with a folder, pipes and the history clock, so
what CI tests is what a VM runs.

How a worker runs, in order.

1. **It waits for its run's plan.** A worker that starts before the supervisor
   has stored the plan, or before the local release file names the plan's
   release, its run and this boot, reads both every `PLAN_EVERY` seconds, beating its heartbeat, and
   never exits for want of them, as design.md's `### Rule 7: how a run starts`
   says. The supervisor writes that file only after its hold check.
   `ReceiveLoop` and every handler take the plan's run and release when they
   are built, so they are built once it is read.
2. **It builds its handlers and its loop.** Data's and execution's entry points
   start the time servers first and stop them after, and data's measures its
   clock outside a backtest, so no trading decision halts on "no time server
   was asked".
3. **It runs the loop to the run's stop.** SIGTERM asks the loop to stop, and
   the process exits once the loop has drained.

Each command ends with one of these codes, which #202's units read.

- 0: the run's stop is stored, or SIGTERM ended a worker, or a day with no
  session started nothing.
- 1, or any other: a failure, whose traceback goes to the host's journal, and
  so does a crash by a signal. The supervisor also ends with 1, and alarms, where
  it cannot read the run's halt, or store it, so the host restarts it to try
  again, unless that restart ends with 5 or 6, after which a person starts it.
  A restart that finds a halt of a strategy standing in for the run's own
  stores the run's halt from it rather than a stop. Where the halt cannot be
  stored, each strategy is halted in its place and the alarm says to pull the
  kill switch. Where none of those can be stored either, the supervisor does not
  end, and tries the halt again on each tick until one lands, unless it is asked
  to stop, when it ends with 3.
- 2: a setting is missing or malformed, the credential file it names cannot be
  read, or the release pin file is missing or empty, named in one line on
  standard error and in an alarm line.
- 3: the supervisor halted the run, which stores no stop, or was asked to stop
  before a run's halt it could not store, or any halt in its place, had landed,
  and alarms that nothing records the halt.
- 4: the supervisor found a result still missing an hour after the close.
- 5: the hold check after an unclean stop could not finish, or the start could
  not be recorded or its records read, so the supervisor writes no plan and no release file.
  Workers this machine released before run on with no supervisor, and no
  other worker starts.
- 6: the supervisor could not store its run's plan or write the local release
  file, or a worker found a plan it cannot read or one of another mode. Each
  alarms.
- 7: SIGTERM ended the supervisor before the run's stop was stored, so nothing
  watches the rest of the session. It alarms.

The forwarder, `forwarder_main`, ends with 2 on a setting it cannot use, and
otherwise only with an error, when the journal ends or gives an entry it cannot
read. Its unit restarts it from its saved cursor, as `runtime/forwarder.py` says.

#202's units restart a process that ends with 1 or 7 or by a signal. The
supervisor's unit carries `RestartPreventExitStatus=2 3 4 5 6` and each worker's
but execution's `RestartPreventExitStatus=2 6`, because a restart into the same
setting, halt, stall, failed hold or plan raises the same alarm again and spends
the day's restarts. Execution's units have no start limit and restart on any
exit while a session is open, so a cancel and the kill switch are served again as
soon as the cause of an exit is fixed, as design.md's `#### Automatic restarts:
the two components that keep on past their cap` says. After 4, 5 or 6 a person
starts the supervisor's unit again once the cause is fixed.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

from ithildinruntime.adapters import (
    TIME_SERVERS,
    AwsClients,
    CredentialFileError,
    LogAlerts,
    S3Store,
    SqsQueue,
    SystemTimer,
    TimeServers,
    WallClock,
)
from ithildinruntime.adapters.ntfy import Load, Pager, Post, Repeats, https_post
from ithildinruntime.core import (
    DATA_STAGE,
    PORTFOLIO_STAGE,
    RISK_STAGE,
    STRESS_MOVES,
    Alerts,
    BlobStore,
    Clock,
    CodecError,
    Halt,
    Mode,
    Plan,
    Queue,
    Retry,
    Storage,
    Timer,
    decode,
    plan_key,
    run_part,
    run_stop_key,
)
from ithildinruntime.data.reference import QuoteKinds
from ithildinruntime.data.splits import SplitRecord

from .alarms import PLAN_UNRUN, SETTING
from .alpha_worker import alpha_handlers
from .cost_worker import cost_handlers
from .data_worker import DataBuckets, data_buckets, data_handler
from .deployment import deployment_table
from .execution_worker import ExecutionWorker
from .forwarder import FileDayCount, Forwarder, ForwarderState, Journalctl
from .loop import Handler, ReceiveLoop, Table
from .portfolio_worker import portfolio_handler
from .risk_worker import risk_handler
from .settings import (
    ForwarderSettings,
    SettingError,
    Settings,
    read_forwarder_settings,
    read_settings,
)
from .supervisor import Outcome, Supervisor, read_release_file
from .supervisor.deadline import (
    CAUSE_ENDS_AFTER,
    PAGE_AGAIN_FIRST,
    PAGE_AGAIN_FIRST_FOR,
    PAGE_AGAIN_LATER,
    PAGES_A_DAY,
    PAGES_WARN_AT,
)

__all__ = [
    "EXIT_PLAN",
    "EXIT_SETTING",
    "PLAN_EVERY",
    "Parts",
    "RETRY",
    "Worker",
    "alpha_main",
    "cost_main",
    "data_main",
    "execution_main",
    "forwarder_for",
    "forwarder_main",
    "measures",
    "one_storage",
    "parts_for",
    "portfolio_main",
    "risk_main",
    "run_alpha",
    "run_cost",
    "run_data",
    "run_execution",
    "run_portfolio",
    "run_risk",
    "serve",
    "supervise",
    "supervisor_for",
    "supervisor_main",
    "trading_day",
]

EXIT_SETTING = 2
EXIT_PLAN = 6
PLAN_EVERY = 5.0
# Below the loop's 30-second heartbeat limit, since `core.Storage` reports
# nothing while it tries a read again, as runtime/execution_worker.py says.
RETRY = Retry(timedelta(seconds=20), 0.5, 4.0)
OWNER_STEP = "check the account's orders and positions on the broker's own site"
_NEW_YORK = ZoneInfo("America/New_York")


def measures(mode: Mode) -> bool:
    """Whether a run judges its clock: every mode but a backtest, whose clock is history's."""
    return Mode(mode) is not Mode.BACKTEST


def trading_day(now: datetime) -> date:
    """The exchange's date at `now`, which names the day's run."""
    return now.astimezone(_NEW_YORK).date()


@dataclass(frozen=True)
class Worker:
    """What every worker's core is handed, whatever its component."""

    mode: Mode
    deployment: str
    account: str
    run: str
    store: BlobStore
    storage: Storage
    queue: Queue
    bucket: str
    table: Table
    timer: Timer
    alerts: Alerts
    beat: Callable[[], None]
    stop: threading.Event
    pause: Callable[[float], None] = time.sleep
    stress_moves: Path = STRESS_MOVES
    plan_every: float = PLAN_EVERY
    # The local release file, which the supervisor writes once its hold check is done.
    release_file: Path | None = None
    # This machine's boot, which the local release file must name.
    boot: str | None = None
    # How many of the worker's decisions its loop runs side by side. A test sets 1 for
    # data until #215 fixes the mirror's race between decisions read side by side.
    pool_size: int = 4


def wait_for_plan(worker: Worker) -> Plan | None:
    """The run's plan once it is stored, or `None` once SIGTERM asked the process to stop.

    Where the worker is handed the local release file, it also waits until that
    file names the plan's release, its run and this boot. The supervisor writes
    it only after its hold check, so a worker started early, or after a lost
    machine whose plan is already stored, sends nothing before every strategy
    that needs one is held.
    """
    key = plan_key(worker.deployment, worker.run)
    while not worker.stop.is_set():
        worker.beat()
        try:
            data = worker.storage.find_blob(key)
        except Halt:
            # A store that does not answer says nothing yet. Storage alarmed on it.
            data = None
        if data is not None:
            try:
                plan = decode(data, Plan)
            except CodecError as error:
                raise Halt(f"{key} holds no plan the codec reads: {error}") from None
            if plan.mode is not worker.mode:
                raise Halt(
                    f"{key} is a plan for {plan.mode.value}, and this worker runs "
                    f"{worker.mode.value}"
                )
            if _released(worker.release_file, plan.release, plan.run, worker.boot):
                return plan
        worker.stop.wait(worker.plan_every)
    return None


def _released(path: Path | None, release: str, run: str, boot: str | None) -> bool:
    """Whether the local release file names `release` for `run` on this boot, or none is handed in.

    A file left from another run, or from before a reboot, releases nothing, so a
    worker never starts ahead of this boot's hold. A worker handed a file and no
    boot is released by nothing, since every command names its boot.
    """
    if path is None:
        return True
    return boot is not None and read_release_file(path) == (release, run, boot)


def _serve(
    worker: Worker,
    component: str,
    handlers_for: Callable[[Plan], Mapping[str, Handler]],
    run: Callable[[ReceiveLoop], None] | None = None,
) -> int:
    try:
        plan = wait_for_plan(worker)
    except Halt as halt:
        # A plan this worker cannot run is no reason to restart into the same plan.
        worker.alerts.alarm(
            PLAN_UNRUN(
                f"{component}@{worker.deployment}: {halt.reason}, so it runs nothing. Correct the "
                "plan or the settings' environment, then start the unit again"
                + _serves_no_cancel(component),
                scope=f"{component}@{worker.deployment}",
            )
        )
        return EXIT_PLAN
    if plan is None:
        return 0
    loop = ReceiveLoop(
        worker=f"{component}@{worker.deployment}",
        component=component,
        table=worker.table,
        plan=plan,
        queue=worker.queue,
        storage=worker.storage,
        bucket=worker.bucket,
        handlers=handlers_for(plan),
        alerts=worker.alerts,
        timer=worker.timer,
        beat=worker.beat,
        pool_size=worker.pool_size,
        stop_key=run_stop_key(worker.deployment, plan.run),
    )
    done = threading.Event()

    def watch() -> None:
        # SIGTERM sets `stop`, and the loop drains as its own stop says.
        while not done.is_set():
            if worker.stop.wait(0.5):
                loop.stop()
                return

    watcher = threading.Thread(target=watch, name=f"{component}-stop", daemon=True)
    watcher.start()
    try:
        (run or (lambda made: made.run()))(loop)
    finally:
        done.set()
    return 0


def run_data(
    worker: Worker,
    *,
    clock: Clock,
    measured: bool,
    lake: DataBuckets,
    mirror: Path,
    servers: TimeServers | None,
    quote_kinds: QuoteKinds | None = None,
    splits: SplitRecord | None = None,
) -> int:
    """Data's worker. `servers` is started before the loop and stopped after it."""

    def handlers(plan: Plan) -> Mapping[str, Handler]:
        return {
            DATA_STAGE: data_handler(
                bucket=lake.lake,
                live=lake.live,
                mirror=mirror,
                storage=worker.storage,
                clock=clock,
                timer=worker.timer,
                measured=measured,
                deployment=worker.deployment,
                run=plan.run,
                release=plan.release,
                table=worker.table,
                quote_kinds=quote_kinds,
                splits=splits,
            )
        }

    return _with_servers(servers, lambda: _serve(worker, "data", handlers))


def run_alpha(worker: Worker) -> int:
    return _serve(worker, "alpha", lambda plan: alpha_handlers(**_common(worker, plan)))


def run_cost(worker: Worker) -> int:
    return _serve(worker, "cost", lambda plan: cost_handlers(**_common(worker, plan)))


def run_risk(worker: Worker) -> int:
    return _serve(
        worker,
        "risk",
        lambda plan: {
            RISK_STAGE: risk_handler(**_common(worker, plan), stress_moves=worker.stress_moves)
        },
    )


def run_portfolio(worker: Worker) -> int:
    return _serve(
        worker,
        "portfolio",
        lambda plan: {
            PORTFOLIO_STAGE: portfolio_handler(
                **_common(worker, plan), stress_moves=worker.stress_moves
            )
        },
    )


def run_execution(worker: Worker, *, clock: Clock, servers: TimeServers | None) -> int:
    """Execution's worker, which settles its account once the loop has ended."""
    made: list[ExecutionWorker] = []

    def handlers(plan: Plan) -> Mapping[str, Handler]:
        made.append(
            ExecutionWorker(
                mode=worker.mode,
                store=worker.store,
                storage=worker.storage,
                account=worker.account,
                clock=clock,
                alerts=worker.alerts,
                retry=RETRY,
                pause=worker.pause,
                deployment=worker.deployment,
                run=plan.run,
                release=plan.release,
                table=worker.table,
                stress_moves=worker.stress_moves,
            )
        )
        return made[0].handlers

    def run(loop: ReceiveLoop) -> None:
        one_storage(made[0], loop)
        made[0].run(loop)

    return _with_servers(servers, lambda: _serve(worker, "execution", handlers, run))


def one_storage(execution: ExecutionWorker, loop: ReceiveLoop) -> None:
    """Refuse an execution worker whose account and loop hold blobs through two `Storage`s.

    A fired loss stop's blob S3 has not stored is held by the account's
    `Storage`, and the loop keeps the process alive only while its own holds
    something, so over two a clean end in an S3 outage would drop the blob.
    """
    if execution.storage is not loop.storage:
        raise RuntimeError(
            "execution's account and its loop are built over two Storages, so a blob held "
            "unstored by one could be dropped when the other ends"
        )


def _common(worker: Worker, plan: Plan) -> dict[str, object]:
    return {
        "storage": worker.storage,
        "deployment": worker.deployment,
        "run": plan.run,
        "release": plan.release,
        "table": worker.table,
    }


def _with_servers(servers: TimeServers | None, serve: Callable[[], int]) -> int:
    if servers is None:
        return serve()
    servers.start()
    try:
        return serve()
    finally:
        servers.stop()


# The commands


def _serves_no_cancel(component: str) -> str:
    """What execution's alarm adds where it ends before its loop: nothing serves a cancel."""
    if component != "execution":
        return ""
    return (
        ". Execution serves no cancel and no kill switch on this machine until the cause is "
        f"fixed, though its unit restarts on its own, so {OWNER_STEP}"
    )


def _refuse(component: str, error: Exception) -> NoReturn:
    """End a command on a setting it cannot use: one line, and an alarm line beside it."""
    print(f"ithildin: {error}", file=sys.stderr)
    LogAlerts(component).alarm(
        SETTING(
            f"{error}. Fix it, then start the unit again{_serves_no_cancel(component)}",
            scope=component,
        )
    )
    raise SystemExit(EXIT_SETTING) from None


def _settings(component: str = "ithildin") -> Settings:
    try:
        return read_settings()
    except SettingError as error:
        _refuse(component, error)


def _clients(settings: Settings, component: str = "ithildin") -> AwsClients:
    """The clients over the settings' credential file, which must be readable at the start."""
    try:
        return AwsClients(settings.environment.region, settings.credentials)
    except CredentialFileError as error:
        _refuse(component, error)


def _stop_on_sigterm() -> threading.Event:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return stop


def _beat(path: Path | None) -> Callable[[], None]:
    if path is None:
        return lambda: None

    def beat() -> None:
        path.touch()

    return beat


@dataclass(frozen=True)
class Parts:
    """What one worker's core is handed in its environment, as `parts_for` builds it."""

    worker: Worker
    clock: Clock | None
    servers: TimeServers | None
    lake: DataBuckets | None
    mirror: Path
    measured: bool


# The workers whose clock is the machine's, judged against the time servers.
_TIMED = ("data", "execution")


def parts_for(
    settings: Settings,
    component: str,
    stop: threading.Event,
    clients: AwsClients | None = None,
    timer: Timer | None = None,
) -> Parts:
    """Every adapter `component`'s core takes in the environment `settings` names.

    It is the one place a command wires its adapters, so a test that hands it
    stand-in clients checks the wiring a VM runs.
    """
    place = settings.environment
    clients = clients or _clients(settings, component)
    timer = timer or SystemTimer()
    alerts = LogAlerts(component)
    store = S3Store(clients.s3(), place.bucket)
    sqs = clients.sqs()
    queue = SqsQueue(
        sqs,
        clients.queue_url(sqs, place.queue_name(component)),
        visibility_timeout=place.visibility,
    )
    storage = Storage(
        store, timer=timer, pause=time.sleep, retry=RETRY, alerts=alerts, owner_step=OWNER_STEP
    )
    day = trading_day(timer.now())
    worker = Worker(
        mode=place.mode,
        deployment=place.deployment.name,
        account=place.deployment.account,
        run=run_part(place.mode, day, day),
        store=store,
        storage=storage,
        queue=queue,
        bucket=place.bucket,
        table=deployment_table(place.deployment),
        timer=timer,
        alerts=alerts,
        beat=_beat(settings.heartbeat),
        stop=stop,
        release_file=settings.release_file,
        boot=settings.boot,
    )
    servers = TimeServers(TIME_SERVERS, now=timer.now) if component in _TIMED else None
    clock = WallClock(timer, servers=servers) if servers is not None else None
    lake = None
    if component == "data":
        lake = data_buckets(
            place.mode,
            client=clients.s3(),
            bucket=settings.lake_bucket,
            prefix=settings.lake_prefix,
        )
    return Parts(worker, clock, servers, lake, settings.mirror, measures(place.mode))


def supervisor_for(
    settings: Settings,
    stop: threading.Event,
    clients: AwsClients | None = None,
    timer: Timer | None = None,
) -> Supervisor:
    """The supervisor of the environment `settings` names, reading its release from the pin."""
    place = settings.environment
    clients = clients or _clients(settings, "supervisor")
    timer = timer or SystemTimer()
    alerts = LogAlerts("supervisor")
    store = S3Store(clients.s3(), place.bucket)
    storage = Storage(
        store, timer=timer, pause=time.sleep, retry=RETRY, alerts=alerts, owner_step=OWNER_STEP
    )
    return Supervisor(
        mode=place.mode,
        deployment=place.deployment.name,
        account=place.deployment.account,
        store=store,
        storage=storage,
        table=deployment_table(place.deployment),
        schedules=place.deployment.schedules,
        release=settings.release(),
        boot=settings.boot,
        timer=timer,
        alerts=alerts,
        release_file=settings.release_file,
        wait=stop.wait,
    )


def serve(
    settings: Settings,
    component: str,
    stop: threading.Event,
    clients: AwsClients | None = None,
    timer: Timer | None = None,
) -> int:
    """Run `component`'s worker in the environment `settings` names, and return its exit code."""
    parts = parts_for(settings, component, stop, clients, timer)
    worker = parts.worker
    if component == "data":
        if parts.lake is None or parts.clock is None:
            raise RuntimeError("data's parts hold no lake or no clock")
        return run_data(
            worker,
            clock=parts.clock,
            measured=parts.measured,
            lake=parts.lake,
            mirror=parts.mirror,
            servers=parts.servers,
        )
    if component == "execution":
        if parts.clock is None:
            raise RuntimeError("execution's parts hold no clock")
        return run_execution(worker, clock=parts.clock, servers=parts.servers)
    cores = {"alpha": run_alpha, "cost": run_cost, "risk": run_risk, "portfolio": run_portfolio}
    return cores[component](worker)


def supervise(
    settings: Settings,
    stop: threading.Event,
    clients: AwsClients | None = None,
    timer: Timer | None = None,
) -> Outcome:
    """Run the supervisor of the environment `settings` names, for today's run."""
    timer = timer or SystemTimer()
    supervisor = supervisor_for(settings, stop, clients, timer)
    return supervisor.run(trading_day(timer.now()))


def _main(component: str) -> None:
    settings, stop = _settings(component), _stop_on_sigterm()
    raise SystemExit(serve(settings, component, stop))


def data_main() -> None:
    _main("data")


def alpha_main() -> None:
    _main("alpha")


def cost_main() -> None:
    _main("cost")


def risk_main() -> None:
    _main("risk")


def portfolio_main() -> None:
    _main("portfolio")


def execution_main() -> None:
    _main("execution")


def forwarder_for(settings: ForwarderSettings, post: Post = https_post) -> Forwarder:
    """The host's forwarder for the environment `settings` names, posting through `post`."""
    state = ForwarderState(settings.state)
    pager = Pager(
        environment=settings.environment.name,
        address=settings.address,
        post=post,
        repeats=Repeats(PAGE_AGAIN_FIRST, PAGE_AGAIN_FIRST_FOR, PAGE_AGAIN_LATER, CAUSE_ENDS_AFTER),
        load=Load(PAGES_A_DAY, PAGES_WARN_AT),
        count=FileDayCount(state.count_file),
        refused=state.mark_refused,
    )
    return Forwarder(
        journal=Journalctl(directory=settings.journal).entries, pager=pager, state=state
    )


def forwarder_main() -> None:
    """Forward the runtime's alarm lines until the journal ends, which ends with an error."""
    try:
        settings = read_forwarder_settings()
    except SettingError as error:
        _refuse("forwarder", error)
    forwarder_for(settings).run()


def supervisor_main() -> None:
    settings, stop = _settings("supervisor"), _stop_on_sigterm()
    try:
        outcome = supervise(settings, stop)
    except SettingError as error:
        _refuse("supervisor", error)
    print(f"ithildin-supervisor: {outcome.line}")
    raise SystemExit(outcome.code)
