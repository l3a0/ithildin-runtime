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
    assert done.stderr.strip() == "ithildin: ITHILDIN_SETTINGS names no settings file"


GOOD = {
    "environment": "shadow",
    "mirror": "/tmp/mirror",
    "lake_bucket": "lake-bucket",
    "lake_prefix": "lake",
    "pin": "PIN",
    "release_file": "/tmp/release",
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
    assert (settings.credentials, settings.heartbeat) == (None, None)


@pytest.mark.parametrize(
    ("values", "raw", "said"),
    [
        ({k: v for k, v in GOOD.items() if k != "mirror"}, None, "has no mirror"),
        (GOOD | {"mirror": ""}, None, "gives mirror no text"),
        (GOOD | {"mirror": "  "}, None, "gives mirror no text"),
        (GOOD, "WRONG_TYPE", "gives environment no text"),
        (GOOD | {"heartbeat": ""}, None, "gives heartbeat no text"),
        (GOOD | {"extra": "x"}, None, "holds keys no reader knows: ['extra']"),
        (GOOD | {"environment": "live"}, None, "knows no environment 'live', only shadow"),
        (GOOD, "environment = [", "is not TOML"),
    ],
    ids=["missing", "empty", "blank", "wrong type", "empty optional", "extra", "unknown", "toml"],
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


def test_a_stop_asked_while_the_loop_runs_ends_it_with_code_0(tmp_path: Path) -> None:
    import threading

    worker = _idle_worker(tmp_path, Mode.SHADOW)
    _store_plan(worker, Mode.SHADOW)
    ended: list[int] = []
    thread = threading.Thread(target=lambda: ended.append(entry.run_alpha(worker)), daemon=True)
    thread.start()
    # The loop has started once it has run its first check, which stores nothing here.
    thread.join(1.0)
    assert thread.is_alive()
    worker.stop.set()
    thread.join(15)
    assert not thread.is_alive() and ended == [0]
