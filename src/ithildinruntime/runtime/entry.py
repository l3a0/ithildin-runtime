"""Each command's entry point: the supervisor's and every worker's.

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
   release, reads both every `PLAN_EVERY` seconds, beating its heartbeat, and
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
- 2: a setting is missing or malformed, or the credential file it names cannot
  be read, named in one line on standard error.
- 3: the supervisor halted the run, which stores no stop.
- 4: the supervisor found a result still missing an hour after the close.
- 5: the hold check after an unclean stop could not finish, so no worker starts.
- 6: the supervisor could not store its run's plan, or a worker found a plan it
  cannot read or one of another mode. Each alarms.
- 7: SIGTERM ended the supervisor before the run's stop was stored, so nothing
  watches the rest of the session.

#202's units restart a process that ends with 1 or 7, and give the supervisor's
unit `RestartPreventExitStatus=3 4 5 6`, because a restart into the same halt,
stall, failed hold or plan raises the same alarm again and spends the day's
restarts.
- 1, or any other: a failure, whose traceback goes to the host's journal.
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

from .alpha_worker import alpha_handlers
from .cost_worker import cost_handlers
from .data_worker import DataBuckets, data_buckets, data_handler
from .deployment import deployment_table
from .execution_worker import ExecutionWorker
from .loop import Handler, ReceiveLoop, Table
from .portfolio_worker import portfolio_handler
from .risk_worker import risk_handler
from .settings import SettingError, Settings, read_settings
from .supervisor import Supervisor

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
    "measures",
    "parts_for",
    "portfolio_main",
    "risk_main",
    "run_alpha",
    "run_cost",
    "run_data",
    "run_execution",
    "run_portfolio",
    "run_risk",
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


def wait_for_plan(worker: Worker) -> Plan | None:
    """The run's plan once it is stored, or `None` once SIGTERM asked the process to stop.

    Where the worker is handed the local release file, it also waits until that
    file names the plan's release. The supervisor writes it only after its hold
    check, so a worker started early, or after a lost machine whose plan is
    already stored, sends nothing before every strategy that needs one is held.
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
            if _released(worker.release_file, plan.release):
                return plan
        worker.stop.wait(worker.plan_every)
    return None


def _released(path: Path | None, release: str) -> bool:
    """Whether the local release file names `release`, or no file is handed in."""
    if path is None:
        return True
    try:
        return path.read_text().strip() == release
    except OSError:
        return False


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
        worker.alerts.alarm(f"{component}@{worker.deployment}: {halt.reason}, so it runs nothing")
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

    return _with_servers(
        servers, lambda: _serve(worker, "execution", handlers, lambda loop: made[0].run(loop))
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


def _settings() -> Settings:
    try:
        return read_settings()
    except SettingError as error:
        print(f"ithildin: {error}", file=sys.stderr)
        raise SystemExit(EXIT_SETTING) from None


def _clients(settings: Settings) -> AwsClients:
    """The clients over the settings' credential file, which must be readable at the start."""
    try:
        return AwsClients(settings.environment.region, settings.credentials)
    except CredentialFileError as error:
        print(f"ithildin: {error}", file=sys.stderr)
        raise SystemExit(EXIT_SETTING) from None


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
    clients = clients or _clients(settings)
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
    clients = clients or _clients(settings)
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


def data_main() -> None:
    settings, stop = _settings(), _stop_on_sigterm()
    parts = parts_for(settings, "data", stop)
    assert parts.lake is not None and parts.clock is not None
    raise SystemExit(
        run_data(
            parts.worker,
            clock=parts.clock,
            measured=parts.measured,
            lake=parts.lake,
            mirror=parts.mirror,
            servers=parts.servers,
        )
    )


def alpha_main() -> None:
    settings, stop = _settings(), _stop_on_sigterm()
    raise SystemExit(run_alpha(parts_for(settings, "alpha", stop).worker))


def cost_main() -> None:
    settings, stop = _settings(), _stop_on_sigterm()
    raise SystemExit(run_cost(parts_for(settings, "cost", stop).worker))


def risk_main() -> None:
    settings, stop = _settings(), _stop_on_sigterm()
    raise SystemExit(run_risk(parts_for(settings, "risk", stop).worker))


def portfolio_main() -> None:
    settings, stop = _settings(), _stop_on_sigterm()
    raise SystemExit(run_portfolio(parts_for(settings, "portfolio", stop).worker))


def execution_main() -> None:
    settings, stop = _settings(), _stop_on_sigterm()
    parts = parts_for(settings, "execution", stop)
    assert parts.clock is not None
    raise SystemExit(run_execution(parts.worker, clock=parts.clock, servers=parts.servers))


def supervisor_main() -> None:
    settings, stop, timer = _settings(), _stop_on_sigterm(), SystemTimer()
    try:
        supervisor = supervisor_for(settings, stop, timer=timer)
    except SettingError as error:
        print(f"ithildin: {error}", file=sys.stderr)
        raise SystemExit(EXIT_SETTING) from None
    outcome = supervisor.run(trading_day(timer.now()))
    print(f"ithildin-supervisor: {outcome.line}")
    raise SystemExit(outcome.code)
