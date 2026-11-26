"""Each command as #202's units start it: its settings, its exit codes and its end on SIGTERM.

The commands are the `[project.scripts]` entries, each a `main` in
runtime/entry.py. A setting that is missing or malformed ends a command with the
exit code 2 and one line naming it, before anything reaches AWS, so each is run
here as a subprocess. SIGTERM is sent to a worker's core waiting for its plan,
in a subprocess of its own, and the test waits on the process rather than on a
fixed sleep.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from ithildinruntime.core import Mode
from ithildinruntime.runtime import entry
from ithildinruntime.runtime.deployment import ENVIRONMENTS, WORKERS
from ithildinruntime.runtime.settings import SettingError, read_settings

COMMANDS = {f"ithildin-{w}": f"{w}_main" for w in (*WORKERS, "supervisor")}
BIN = Path(sys.executable).parent


def test_every_command_resolves_to_its_main() -> None:
    found = {e.name: e for e in entry_points(group="console_scripts") if e.name in COMMANDS}
    assert set(found) == set(COMMANDS)
    for name, made in found.items():
        assert made.value == f"ithildinruntime.runtime.entry:{COMMANDS[name]}"
        assert made.load() is getattr(entry, COMMANDS[name])


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_a_command_with_no_settings_exits_2_naming_them(command: str) -> None:
    env = {k: v for k, v in os.environ.items() if k != "ITHILDIN_SETTINGS"}
    done = subprocess.run(
        [BIN / command], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == entry.EXIT_SETTING
    worker = command.removeprefix("ithildin-")
    assert done.stderr.splitlines() == [
        "ithildin: ITHILDIN_SETTINGS names no settings file",
        f"ALARM {worker}: ITHILDIN_SETTINGS names no settings file. Fix it, then start the "
        "unit again",
    ]


GOOD = {
    "environment": "shadow",
    "mirror": "/tmp/mirror",
    "lake_bucket": "lake-bucket",
    "lake_prefix": "lake",
    "pin": "PIN",
    "release_file": "/tmp/release",
    "credentials": "/tmp/credentials",
}


def _write(tmp_path: Path, values: dict[str, object], raw: str | None = None) -> dict[str, str]:
    path = tmp_path / "settings.toml"
    if raw is None or raw == "WRONG_TYPE":
        lines = [f"{k} = {v!r}\n".replace("'", '"') for k, v in values.items()]
        if raw == "WRONG_TYPE":
            lines = [
                "environment = 7\n" if line.startswith("environment") else line for line in lines
            ]
        raw = "".join(lines)
    path.write_text(raw)
    return {"ITHILDIN_SETTINGS": str(path)}


@pytest.fixture
def boot(tmp_path: Path) -> Path:
    made = tmp_path / "boot_id"
    made.write_text("0f3c9b8e-1d2a-4c5b-9e6f-7a8b9c0d1e2f\n")
    return made


def test_good_settings_read_the_environment_from_the_release_table(
    tmp_path: Path, boot: Path
) -> None:
    pin = tmp_path / "pin"
    pin.write_text("sha256:abc\n")
    settings = read_settings(_write(tmp_path, GOOD | {"pin": str(pin)}), boot)
    assert settings.environment is ENVIRONMENTS["shadow"]
    assert settings.environment.mode is Mode.SHADOW
    assert settings.boot == "0f3c9b8e-1d2a-4c5b-9e6f-7a8b9c0d1e2f"
    assert settings.release() == "sha256:abc"
    assert (settings.credentials, settings.heartbeat) == (Path("/tmp/credentials"), None)


@pytest.mark.parametrize(
    ("values", "raw", "said"),
    [
        ({k: v for k, v in GOOD.items() if k != "mirror"}, None, "has no mirror"),
        ({k: v for k, v in GOOD.items() if k != "credentials"}, None, "has no credentials"),
        (GOOD | {"mirror": ""}, None, "gives mirror no text"),
        (GOOD | {"mirror": "  "}, None, "gives mirror no text"),
        (GOOD, "WRONG_TYPE", "gives environment no text"),
        (GOOD | {"heartbeat": ""}, None, "gives heartbeat no text"),
        (GOOD | {"extra": "x"}, None, "holds keys no reader knows: ['extra']"),
        (GOOD | {"environment": "live"}, None, "knows no environment 'live', only shadow"),
        (GOOD, "environment = [", "is not TOML"),
    ],
    ids=[
        "missing",
        "no credentials",
        "empty",
        "blank",
        "wrong type",
        "empty optional",
        "extra",
        "unknown",
        "toml",
    ],
)
def test_a_setting_that_is_missing_or_malformed_is_named(
    tmp_path: Path, boot: Path, values, raw, said: str
) -> None:
    with pytest.raises(SettingError, match=None) as raised:
        read_settings(_write(tmp_path, values, raw), boot)
    assert said in str(raised.value)


def test_a_settings_file_that_cannot_be_read_is_named(tmp_path: Path, boot: Path) -> None:
    with pytest.raises(SettingError, match="cannot be read"):
        read_settings({"ITHILDIN_SETTINGS": str(tmp_path / "none.toml")}, boot)


@pytest.mark.parametrize("text", ["", "a/b\n", "..\n"])
def test_a_boot_id_that_cannot_name_a_key_is_refused(tmp_path: Path, text: str) -> None:
    made = tmp_path / "boot_id"
    made.write_text(text)
    with pytest.raises(SettingError, match="boot id"):
        read_settings(_write(tmp_path, GOOD), made)


def test_a_missing_boot_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SettingError, match="cannot be read"):
        read_settings(_write(tmp_path, GOOD), tmp_path / "no-boot")


@pytest.mark.parametrize("pin", [None, "", "two words\n"])
def test_a_release_pin_that_names_no_release_is_refused(
    tmp_path: Path, boot: Path, pin: str | None
) -> None:
    path = tmp_path / "pin"
    if pin is not None:
        path.write_text(pin)
    settings = read_settings(_write(tmp_path, GOOD | {"pin": str(path)}), boot)
    with pytest.raises(SettingError, match="release pin"):
        settings.release()


@pytest.mark.parametrize(
    ("mode", "measured"),
    [(Mode.BACKTEST, False), (Mode.SHADOW, True), (Mode.PAPER, True), (Mode.LIVE, True)],
)
def test_every_mode_but_a_backtest_measures_its_clock(mode: Mode, measured: bool) -> None:
    assert entry.measures(mode) is measured


class Servers:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")


@pytest.mark.parametrize("fails", [False, True])
def test_the_time_servers_start_before_the_loop_and_stop_after_it(fails: bool) -> None:
    calls: list[str] = []

    def serve() -> int:
        calls.append("loop")
        if fails:
            raise RuntimeError("the loop failed")
        return 0

    if fails:
        with pytest.raises(RuntimeError):
            entry._with_servers(Servers(calls), serve)  # type: ignore[arg-type]
    else:
        assert entry._with_servers(Servers(calls), serve) == 0  # type: ignore[arg-type]
    assert calls == ["start", "loop", "stop"]


CHILD = textwrap.dedent(
    """
    import sys, threading, time
    from pathlib import Path
    from ithildinruntime.adapters import FolderStore, PipeQueue, SystemTimer
    from ithildinruntime.core import Mode, Retry, Storage
    from ithildinruntime.runtime import entry
    from ithildinruntime.runtime.deployment import Deployment, deployment_table
    from ithildinruntime.core import BeforeClose
    from datetime import timedelta

    root = Path(sys.argv[1])

    class Alarms:
        def alarm(self, reason): print("ALARM", reason, flush=True)
        def heartbeat(self): pass

    store = FolderStore(root / "store")
    storage = Storage(store, timer=SystemTimer(), pause=time.sleep,
                      retry=Retry(timedelta(seconds=1), 0.05, 0.1), alerts=Alarms(), owner_step="x")
    stop = entry._stop_on_sigterm()
    beats = []

    def beat():
        beats.append(1)
        if len(beats) == 2:
            print("waiting", flush=True)

    worker = entry.Worker(
        mode=Mode.SHADOW, deployment="ci", account="acct1", run="2026-11-30", store=store,
        storage=storage, queue=PipeQueue(root / "pipes", "alpha", wait_seconds=0.2),
        bucket="folder",
        table=deployment_table(Deployment("ci", "acct1", {"chan_bollinger": (BeforeClose(20),)})),
        timer=SystemTimer(), alerts=Alarms(), beat=beat, stop=stop, plan_every=0.05)
    code = entry.run_alpha(worker)
    print("ended", code, len(beats) > 1, flush=True)
    sys.exit(code)
    """
)


def test_sigterm_ends_a_worker_waiting_for_its_plan_with_code_0(tmp_path: Path) -> None:
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    child = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "waiting"
        child.send_signal(signal.SIGTERM)
        out, err = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
    assert child.returncode == 0, err
    # It beat more than once while it waited, and ran no loop.
    assert out.strip() == "ended 0 True"


def _idle_worker(root: Path, mode: Mode) -> entry.Worker:
    """An alpha worker over a folder, whose plan's decisions never get data's result."""
    import threading
    import time
    from datetime import timedelta

    from ithildinruntime.adapters import FolderStore, PipeQueue, SystemTimer
    from ithildinruntime.core import BeforeClose, Retry, Storage
    from ithildinruntime.runtime.deployment import Deployment, deployment_table

    class Alarms:
        def __init__(self) -> None:
            self.reasons: list[str] = []

        def alarm(self, reason: str) -> None:
            self.reasons.append(reason)

        def heartbeat(self) -> None:
            pass

    store = FolderStore(root / "store")
    alarms = Alarms()
    storage = Storage(
        store,
        timer=SystemTimer(),
        pause=time.sleep,
        retry=Retry(timedelta(seconds=1), 0.05, 0.1),
        alerts=alarms,
        owner_step="x",
    )
    return entry.Worker(
        mode=mode,
        deployment="ci",
        account="acct1",
        run="2026-11-30",
        store=store,
        storage=storage,
        queue=PipeQueue(root / "pipes", "alpha", wait_seconds=0.2),
        bucket="folder",
        table=deployment_table(Deployment("ci", "acct1", {"chan_bollinger": (BeforeClose(20),)})),
        timer=SystemTimer(),
        alerts=alarms,
        beat=lambda: None,
        stop=threading.Event(),
        plan_every=0.05,
    )


def _store_plan(worker: entry.Worker, mode: Mode) -> None:
    from datetime import date

    from ithildinruntime.core import (
        BeforeClose,
        Plan,
        PlanDecision,
        encode,
        load_sessions,
        plan_key,
        sha256_of,
        to_utc,
    )

    at = to_utc(date(2026, 11, 30), BeforeClose(20), load_sessions())
    plan = Plan(1, "ci", "2026-11-30", mode, "sha256:r", (PlanDecision(at, ("chan_bollinger",)),))
    data = encode(plan)
    worker.store.create(plan_key("ci", "2026-11-30"), data, sha256_of(data))


def test_a_plan_of_another_mode_halts_the_worker(tmp_path: Path) -> None:
    from ithildinruntime.core import Halt

    worker = _idle_worker(tmp_path, Mode.SHADOW)
    _store_plan(worker, Mode.PAPER)
    with pytest.raises(Halt, match="a plan for paper, and this worker runs shadow"):
        entry.wait_for_plan(worker)
    # Its command alarms and ends with its own code rather than a traceback.
    assert entry.run_alpha(worker) == entry.EXIT_PLAN
    (alarm,) = worker.alerts.reasons  # type: ignore[attr-defined]
    assert "a plan for paper, and this worker runs shadow, so it runs nothing" in alarm


def test_a_stop_asked_while_the_loop_runs_ends_it_with_code_0(tmp_path: Path) -> None:
    import threading

    worker = _idle_worker(tmp_path, Mode.SHADOW)
    _store_plan(worker, Mode.SHADOW)
    ended: list[int] = []
    loops: list[entry.ReceiveLoop] = []

    class Seen(entry.ReceiveLoop):
        def __init__(self, **made) -> None:
            super().__init__(**made)
            loops.append(self)

    entry_loop = entry.ReceiveLoop
    entry.ReceiveLoop = Seen  # type: ignore[misc]
    try:
        thread = threading.Thread(target=lambda: ended.append(entry.run_alpha(worker)), daemon=True)
        thread.start()
        # Wait on the loop's own signal that it has run its first check.
        end = time.monotonic() + 15
        while not loops and time.monotonic() < end:
            time.sleep(0.01)
        assert loops and loops[0].wait_started(15), "the loop never started"
    finally:
        entry.ReceiveLoop = entry_loop  # type: ignore[misc]
    assert thread.is_alive()
    worker.stop.set()
    thread.join(15)
    assert not thread.is_alive() and ended == [0]


def test_a_worker_waits_for_the_release_file_as_well_as_the_plan(tmp_path: Path) -> None:
    import dataclasses
    import threading

    release = tmp_path / "local" / "release"
    worker = dataclasses.replace(
        _idle_worker(tmp_path, Mode.SHADOW), release_file=release, boot="boot-a"
    )
    _store_plan(worker, Mode.SHADOW)
    found: list[object] = []
    thread = threading.Thread(target=lambda: found.append(entry.wait_for_plan(worker)), daemon=True)
    thread.start()
    thread.join(0.5)
    # The plan is stored and the supervisor has not yet finished its hold.
    assert thread.is_alive() and found == []
    release.parent.mkdir(parents=True)
    release.write_text("sha256:other\n2026-11-30\nboot-a\n")
    thread.join(0.5)
    assert thread.is_alive(), "a file naming another release is not this plan's"
    release.write_text("sha256:r\n2026-11-30\nboot-a\n")
    thread.join(5)
    assert not thread.is_alive() and found and found[0].release == "sha256:r"


class StandInClients:
    """`AwsClients` with no network: real botocore clients, and a queue URL by name."""

    def __init__(self) -> None:
        from ithildinruntime.adapters import AwsClients

        self._real = AwsClients("us-east-1")
        self.asked: list[str] = []

    def s3(self):
        return self._real.s3()

    def sqs(self):
        return self._real.sqs()

    def queue_url(self, sqs, name: str) -> str:
        self.asked.append(name)
        return f"https://sqs.example/{name}"


class FixedTimer:
    def __init__(self, at) -> None:
        self.at = at

    def now(self):
        return self.at

    def monotonic(self) -> float:
        return 0.0

    def after(self, seconds: float) -> object:
        raise NotImplementedError


def _settings_of(tmp_path: Path, boot: Path):
    pin = tmp_path / "pin"
    pin.write_text("sha256:pinned\n")
    values = GOOD | {
        "pin": str(pin),
        "heartbeat": str(tmp_path / "beat"),
        "release_file": str(tmp_path / "local" / "release"),
        "mirror": str(tmp_path / "mirror"),
    }
    return read_settings(_write(tmp_path, values), boot)


@pytest.mark.parametrize("component", WORKERS)
def test_each_command_wires_its_environments_adapters(
    tmp_path: Path, boot: Path, component: str
) -> None:
    import threading
    from datetime import UTC, datetime

    from ithildinruntime.adapters import S3Bucket, WallClock

    settings = _settings_of(tmp_path, boot)
    clients = StandInClients()
    # 03:00 UTC on 2026-12-01 is still 2026-11-30 in New York.
    timer = FixedTimer(datetime(2026, 12, 1, 3, 0, tzinfo=UTC))
    stop = threading.Event()
    parts = entry.parts_for(settings, component, stop, clients=clients, timer=timer)
    worker = parts.worker
    assert clients.asked == [f"ithildin-shadow-{component}"]
    assert worker.queue._url == f"https://sqs.example/ithildin-shadow-{component}"
    assert (worker.mode, worker.deployment, worker.account) == (Mode.SHADOW, "shadow", "shadow1")
    assert worker.run == "2026-11-30" and worker.bucket == "ithildin-runtime-shadow"
    assert worker.store._bucket == "ithildin-runtime-shadow"
    assert worker.release_file == tmp_path / "local" / "release" and worker.stop is stop
    assert worker.boot == settings.boot
    assert [s.name for s in worker.table.strategies] == ["chan_bollinger"]
    worker.beat()
    assert (tmp_path / "beat").exists()
    assert parts.measured is True and parts.mirror == tmp_path / "mirror"
    if component in ("data", "execution"):
        assert isinstance(parts.clock, WallClock) and parts.clock._servers is parts.servers
        assert parts.servers is not None
    else:
        assert (parts.clock, parts.servers) == (None, None)
    if component == "data":
        assert isinstance(parts.lake.lake, S3Bucket) and isinstance(parts.lake.live, S3Bucket)
        assert (parts.lake.lake._bucket, parts.lake.lake._root) == ("lake-bucket", "lake/")
        assert parts.lake.live._root == "lake-live/"
    else:
        assert parts.lake is None


def test_the_supervisors_command_wires_its_environment(tmp_path: Path, boot: Path) -> None:
    import threading
    from datetime import UTC, datetime

    settings = _settings_of(tmp_path, boot)
    timer = FixedTimer(datetime(2026, 11, 30, 14, 0, tzinfo=UTC))
    stop = threading.Event()
    made = entry.supervisor_for(settings, stop, clients=StandInClients(), timer=timer)
    assert (made._mode, made._deployment, made._account) == (Mode.SHADOW, "shadow", "shadow1")
    assert made._release == "sha256:pinned" and made._boot == settings.boot
    assert made._release_file == tmp_path / "local" / "release"
    assert made._store._bucket == "ithildin-runtime-shadow" and made._timer is timer
    assert made._wait == stop.wait
    assert dict(made._schedules) == {
        "chan_bollinger": ENVIRONMENTS["shadow"].deployment.schedules["chan_bollinger"]
    }


def test_every_setting_reaches_its_field(tmp_path: Path, boot: Path) -> None:
    values = GOOD | {
        "mirror": "/m/mirror",
        "lake_bucket": "the-lake",
        "lake_prefix": "lake-prefix",
        "pin": "/m/pin",
        "release_file": "/m/release",
        "credentials": "/m/credentials",
        "heartbeat": "/m/beat",
    }
    settings = read_settings(_write(tmp_path, values), boot)
    assert (settings.mirror, settings.pin, settings.release_file) == (
        Path("/m/mirror"),
        Path("/m/pin"),
        Path("/m/release"),
    )
    assert (settings.lake_bucket, settings.lake_prefix) == ("the-lake", "lake-prefix")
    assert (settings.credentials, settings.heartbeat) == (Path("/m/credentials"), Path("/m/beat"))


@pytest.mark.parametrize(
    ("at", "day"),
    [
        ("2026-12-01T00:59:59+00:00", "2026-11-30"),
        ("2026-12-01T01:00:00+00:00", "2026-11-30"),
        ("2026-12-01T05:00:00+00:00", "2026-12-01"),
        ("2026-07-01T03:59:59+00:00", "2026-06-30"),
        ("2026-07-01T04:00:00+00:00", "2026-07-01"),
    ],
)
def test_the_runs_day_is_new_yorks_date(at: str, day: str) -> None:
    from datetime import date, datetime

    # 20:00 in New York is 01:00 UTC in winter and 00:00 UTC in summer, both still that day.
    assert entry.trading_day(datetime.fromisoformat(at)) == date.fromisoformat(day)


def test_a_plan_the_codec_cannot_read_halts_the_worker(tmp_path: Path) -> None:
    from ithildinruntime.core import Halt, plan_key, sha256_of

    worker = _idle_worker(tmp_path, Mode.SHADOW)
    worker.store.create(plan_key("ci", "2026-11-30"), b"{not a plan", sha256_of(b"{not a plan"))
    with pytest.raises(Halt, match="holds no plan the codec reads"):
        entry.wait_for_plan(worker)


def test_a_plan_read_the_store_does_not_answer_is_tried_again(tmp_path: Path) -> None:
    from ithildinruntime.core import StoreUnavailable, plan_key

    worker = _idle_worker(tmp_path, Mode.SHADOW)
    _store_plan(worker, Mode.SHADOW)
    key = plan_key("ci", "2026-11-30")
    store, end = worker.store, time.monotonic() + 1.5
    real = store.get

    def get(asked: str) -> bytes:
        # Refused past Storage's one-second retries, as S3 that stops answering for a while.
        if asked == key and time.monotonic() < end:
            raise StoreUnavailable(asked, "503: no answer")
        return real(asked)

    store.get = get  # type: ignore[method-assign]
    plan = entry.wait_for_plan(worker)
    assert plan is not None and plan.run == "2026-11-30"


def test_a_worker_started_after_its_runs_stop_ends_at_once(tmp_path: Path) -> None:
    import threading

    from ithildinruntime.core import run_stop_key, sha256_of

    worker = _idle_worker(tmp_path, Mode.SHADOW)
    _store_plan(worker, Mode.SHADOW)
    worker.store.create(run_stop_key("ci", "2026-11-30"), b"{}", sha256_of(b"{}"))
    ended: list[int] = []
    thread = threading.Thread(target=lambda: ended.append(entry.run_alpha(worker)), daemon=True)
    thread.start()
    thread.join(15)
    assert not thread.is_alive() and ended == [0]


@pytest.mark.parametrize("component", WORKERS)
def test_each_command_hands_its_core_the_parts_it_built(
    tmp_path: Path, boot: Path, component: str, monkeypatch
) -> None:
    import threading
    from datetime import UTC, datetime

    settings = _settings_of(tmp_path, boot)
    handed: dict[str, object] = {}

    def core(worker, **parts):
        handed.update(parts, worker=worker)
        return 17

    monkeypatch.setattr(entry, f"run_{component}", core)
    timer = FixedTimer(datetime(2026, 12, 1, 3, 0, tzinfo=UTC))
    code = entry.serve(settings, component, threading.Event(), StandInClients(), timer)
    assert code == 17 and handed["worker"].run == "2026-11-30"
    if component == "data":
        assert handed["measured"] is True and handed["mirror"] == tmp_path / "mirror"
        assert handed["servers"] is not None and handed["clock"]._servers is handed["servers"]
        assert handed["lake"].lake._root == "lake/"
    elif component == "execution":
        assert handed["servers"] is not None and handed["clock"]._servers is handed["servers"]
    else:
        assert set(handed) == {"worker"}


def test_the_supervisors_command_runs_todays_run_and_ends_with_its_code(
    tmp_path: Path, boot: Path, monkeypatch
) -> None:
    import threading
    from datetime import UTC, date, datetime

    from ithildinruntime.runtime.supervisor import Outcome, Supervisor

    asked: list[date] = []
    timers: list[object] = []

    def run(self, today):
        asked.append(today)
        timers.append(self._timer)
        return Outcome(4, "x")

    monkeypatch.setattr(Supervisor, "run", run)
    settings = _settings_of(tmp_path, boot)
    # 01:30 UTC on 2026-12-01 is 20:30 on 2026-11-30 in New York.
    timer = FixedTimer(datetime(2026, 12, 1, 1, 30, tzinfo=UTC))
    outcome = entry.supervise(settings, threading.Event(), StandInClients(), timer)
    assert asked == [date(2026, 11, 30)] and outcome.code == 4
    # The supervisor runs on the timer that named its day.
    assert timers == [timer]


def test_a_release_file_that_is_not_utf_8_releases_nothing(tmp_path: Path) -> None:
    import dataclasses

    release = tmp_path / "local" / "release"
    release.parent.mkdir(parents=True)
    release.write_bytes(b"\xff\n")
    assert not entry._released(release, "sha256:r", "2026-11-30", None)
    worker = dataclasses.replace(_idle_worker(tmp_path, Mode.SHADOW), release_file=release)
    assert entry._released(worker.release_file, "sha256:r", "2026-11-30", None) is False


@pytest.mark.parametrize(
    ("lines", "released"),
    [
        ("sha256:r\n2026-11-30\nboot-a\n", True),
        ("sha256:r\n2026-11-30\nboot-b\n", False),
        ("sha256:r\n2026-11-29\nboot-a\n", False),
        ("sha256:other\n2026-11-30\nboot-a\n", False),
        ("sha256:r\n", False),
        ("sha256:r\n2026-11-30\nboot-a\nmore\n", False),
    ],
    ids=["this run and boot", "another boot", "another run", "another release", "one line", "four"],
)
def test_a_worker_with_a_boot_waits_for_its_run_and_boot(
    tmp_path: Path, lines: str, released: bool
) -> None:
    import dataclasses
    import threading

    release = tmp_path / "local" / "release"
    release.parent.mkdir(parents=True)
    release.write_text(lines)
    worker = dataclasses.replace(
        _idle_worker(tmp_path, Mode.SHADOW), release_file=release, boot="boot-a"
    )
    _store_plan(worker, Mode.SHADOW)
    found: list[object] = []
    thread = threading.Thread(target=lambda: found.append(entry.wait_for_plan(worker)), daemon=True)
    thread.start()
    thread.join(1.0)
    assert (not thread.is_alive()) is released
    worker.stop.set()
    thread.join(5)


def test_a_worker_handed_a_release_file_and_no_boot_is_released_by_nothing(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.write_text("sha256:r\n2026-11-30\nboot-a\n")
    assert entry._released(release, "sha256:r", "2026-11-30", None) is False
    assert entry._released(None, "sha256:r", "2026-11-30", None) is True


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_each_command_ends_with_the_code_its_core_returns(command: str, monkeypatch) -> None:
    from ithildinruntime.runtime.supervisor import Outcome

    monkeypatch.setattr(entry, "_settings", lambda component="ithildin": "settings")
    monkeypatch.setattr(entry, "_stop_on_sigterm", lambda: "stop")
    monkeypatch.setattr(entry, "serve", lambda settings, component, stop: 40 + len(component))
    monkeypatch.setattr(entry, "supervise", lambda settings, stop: Outcome(4, "stalled"))
    with pytest.raises(SystemExit) as ended:
        getattr(entry, COMMANDS[command])()
    worker = command.removeprefix("ithildin-")
    assert ended.value.code == (4 if worker == "supervisor" else 40 + len(worker))
