"""The check that marketlake's `main` says the pin is due a raise, for #131.

Each case builds a small git repository in place of marketlake's, whose pinned
commit holds the two modules as the installed package does, and runs the whole
script against it. So the fetch, the reads and the exit code all run, with no
network. The workflow that runs the script gets its own checks, because a
credential or a pull request trigger added to it later would fail nothing else.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import lake
import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "check_lake_pin.py"
WORKFLOW = REPO / ".github" / "workflows" / "lake-pin.yml"
INSTALLED = Path(lake.__file__).parent

JOURNAL = "src/lake/journal.py"
JOIN = "src/lake/segment_join.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_lake_pin", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load()

# The pin's own files, as the installed package holds them, and the version its lock names.
PINNED = {
    JOURNAL: (INSTALLED / "journal.py").read_text(encoding="utf-8"),
    JOIN: (INSTALLED / "segment_join.py").read_text(encoding="utf-8"),
    "uv.lock": '[[package]]\nname = "pyarrow"\nversion = "25.0.1"\n',
}


# What a commit changes: each file's new text, or `None` where it is removed.
type Files = Mapping[str, str | bytes | None]

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def _git(where: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV}
    done = subprocess.run(
        ["git", *args], cwd=where, env=env, check=True, capture_output=True, text=True
    )
    return done.stdout.strip()


class Marketlake:
    """A stand-in for marketlake's repository, whose first commit is the pin."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir()
        _git(root, "init", "--quiet", "--initial-branch=main")
        self.pin = self.commit(PINNED)

    def commit(self, files: Files) -> str:
        for rel, text in files.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if text is None:
                path.unlink()
            else:
                path.write_bytes(text if isinstance(text, bytes) else text.encode())
        _git(self.root, "add", "--all")
        _git(self.root, "commit", "--quiet", "--allow-empty", "--message=x")
        return _git(self.root, "rev-parse", "HEAD")

    def lock(self, path: Path, pyarrow: str = "25.0.1") -> Path:
        """A lock of this repository's that pins this remote at its first commit."""
        remote = self.root.as_uri()
        path.write_text(
            f'[[package]]\nname = "marketlake"\n'
            f'source = {{ git = "{remote}?rev={self.pin}#{self.pin}" }}\n\n'
            f'[[package]]\nname = "pyarrow"\nversion = "{pyarrow}"\n',
            encoding="utf-8",
        )
        return path


@pytest.fixture
def marketlake(tmp_path: Path) -> Marketlake:
    return Marketlake(tmp_path / "marketlake")


@pytest.fixture
def lock(marketlake: Marketlake, tmp_path: Path) -> Path:
    return marketlake.lock(tmp_path / "uv.lock")


def _run(lock: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = script.main(["--lock", str(lock)])
    return code, capsys.readouterr().out


def _versioned(version: int) -> str:
    text = PINNED[JOURNAL]
    assert text.count("\nSCHEMA_VERSION = 2\n") == 1
    return text.replace("\nSCHEMA_VERSION = 2\n", f"\nSCHEMA_VERSION = {version}\n")


def _locked(version: str) -> str:
    return PINNED["uv.lock"].replace("25.0.1", version)


def test_the_pins_own_commit_passes(
    lock: Path, marketlake: Marketlake, capsys: pytest.CaptureFixture[str]
) -> None:
    code, said = _run(lock, capsys)
    assert code == 0
    assert said == (
        f"Nothing moved: marketlake's main at {marketlake.pin[:12]} "
        f"against the pin at {marketlake.pin[:12]}.\n"
    )


def test_a_change_to_anything_else_passes(
    lock: Path, marketlake: Marketlake, capsys: pytest.CaptureFixture[str]
) -> None:
    marketlake.commit(
        {
            "README.md": "x",
            "src/lake/bar_import.py": "x",
            JOURNAL: PINNED[JOURNAL] + "\nX = 1\n",
            "uv.lock": PINNED["uv.lock"] + '\n[[package]]\nname = "duckdb"\nversion = "9.9"\n',
        }
    )
    assert _run(lock, capsys)[0] == 0


VERSION_SAID = (
    "- `SCHEMA_VERSION` in src/lake/journal.py is 3 on marketlake's main and 2 at the pin."
)
JOIN_SAID = "- src/lake/segment_join.py on marketlake's main differs from the pin's."
PYARROW_SAID = (
    "- pyarrow is 26.0.1 in marketlake's uv.lock on main and 25.0.1 in this repository's uv.lock."
)


@pytest.mark.parametrize(
    ("files", "said"),
    [
        ({JOURNAL: _versioned(3)}, [VERSION_SAID, f"- {script.RAISE_NOW}"]),
        ({JOIN: PINNED[JOIN] + "\n"}, [JOIN_SAID, f"- {script.WAIT}"]),
        ({"uv.lock": _locked("26.0.1")}, [PYARROW_SAID, f"- {script.WAIT}"]),
        (
            {JOIN: PINNED[JOIN] + "\n", "uv.lock": _locked("26.0.1")},
            [JOIN_SAID, PYARROW_SAID, f"- {script.WAIT}"],
        ),
        (
            {JOURNAL: _versioned(3), JOIN: PINNED[JOIN] + "\n"},
            [VERSION_SAID, f"- {script.RAISE_NOW}", JOIN_SAID, f"- {script.RAISE_TOGETHER}"],
        ),
        (
            {JOURNAL: _versioned(3), "uv.lock": _locked("26.0.1")},
            [VERSION_SAID, f"- {script.RAISE_NOW}", PYARROW_SAID, f"- {script.RAISE_TOGETHER}"],
        ),
        (
            {JOIN: None},
            [
                "- src/lake/segment_join.py is not on marketlake's main, and the pin holds it.",
                f"- {script.WAIT}",
            ],
        ),
        (
            {JOURNAL: _versioned(1)},
            [VERSION_SAID.replace("is 3", "is 1"), f"- {script.NO_RAISE}"],
        ),
        (
            {JOURNAL: _versioned(1), JOIN: None},
            [
                VERSION_SAID.replace("is 3", "is 1"),
                f"- {script.NO_RAISE}",
                "- src/lake/segment_join.py is not on marketlake's main, and the pin holds it.",
                f"- {script.WAIT}",
            ],
        ),
    ],
    ids=[
        "a higher version",
        "the join",
        "pyarrow",
        "the join and pyarrow",
        "a higher version and the join",
        "a higher version and pyarrow",
        "the join is gone",
        "a lower version",
        "a lower version and the join",
    ],
)
def test_each_difference_fails_the_run_and_is_named_with_its_rule(
    lock: Path,
    marketlake: Marketlake,
    capsys: pytest.CaptureFixture[str],
    files: Files,
    said: list[str],
) -> None:
    commit = marketlake.commit(files)
    code, out = _run(lock, capsys)
    assert code == 1
    assert out.splitlines() == [
        "- The marketlake pin may be due a raise: marketlake's main at "
        f"{commit[:12]} against the pin at {marketlake.pin[:12]}.",
        *said,
        f"- Read {script.CONTRACT}.",
    ]


def test_the_rules_are_the_contracts_own_two() -> None:
    assert "raise the pin now" in script.RAISE_NOW
    assert "wait to raise the pin until marketlake's deploy" in script.WAIT


def test_pyarrow_moved_here_alone_fails_the_run(
    marketlake: Marketlake, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(marketlake.lock(tmp_path / "uv.lock", pyarrow="26.0.1"), capsys)
    assert code == 1
    assert "pyarrow is 25.0.1 in marketlake's uv.lock on main and 26.0.1 in this" in out


def test_a_branch_other_than_main_is_not_read(
    lock: Path, marketlake: Marketlake, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(marketlake.root, "switch", "--quiet", "--create", "other")
    marketlake.commit({JOURNAL: _versioned(3)})
    assert _run(lock, capsys)[0] == 0


TWO_PYARROWS = PINNED["uv.lock"] + '\n[[package]]\nname = "pyarrow"\nversion = "26.0.1"\n'


@pytest.mark.parametrize(
    ("files", "said"),
    [
        ({JOURNAL: None}, "src/lake/journal.py is not at"),
        ({JOURNAL: "def broken(:\n"}, "does not parse"),
        ({JOURNAL: "OTHER = 2\n"}, "assigns no one integer to SCHEMA_VERSION"),
        ({JOURNAL: "SCHEMA_VERSION = 2\nSCHEMA_VERSION = 3\n"}, "assigns no one integer"),
        ({JOURNAL: "SCHEMA_VERSION = 1 + 1\n"}, "assigns no one integer"),
        ({JOURNAL: "SCHEMA_VERSION = True\n"}, "assigns no one integer"),
        ({JOURNAL: 'SCHEMA_VERSION = "2"\n'}, "assigns no one integer"),
        ({JOURNAL: "SCHEMA_VERSION: int\n"}, "assigns no one integer"),
        ({"uv.lock": None}, "uv.lock is not at"),
        ({"uv.lock": b"\xff"}, "is not text"),
        ({"uv.lock": "not = toml = at all"}, "is no uv lock"),
        ({"uv.lock": "version = 1\n"}, "is no uv lock"),
        ({"uv.lock": '[[package]]\nname = "duckdb"\n'}, "holds 0 entries for pyarrow"),
        ({"uv.lock": TWO_PYARROWS}, "holds 2 entries for pyarrow"),
        ({"uv.lock": _locked("25.0.1\\n::error::planted")}, "names no version of pyarrow"),
    ],
)
def test_a_side_that_cannot_be_read_is_no_pass(
    lock: Path,
    marketlake: Marketlake,
    capsys: pytest.CaptureFixture[str],
    files: Files,
    said: str,
) -> None:
    marketlake.commit(files)
    code, out = _run(lock, capsys)
    assert code == 2
    assert said in out
    assert "planted" not in out
    assert "says nothing of the pin" in out


def test_a_pin_that_holds_no_join_is_no_pass(
    marketlake: Marketlake, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    marketlake.pin = marketlake.commit({JOIN: None})
    code, out = _run(marketlake.lock(tmp_path / "uv.lock"), capsys)
    assert code == 2
    assert "src/lake/segment_join.py is not at the pin" in out


def test_an_annotated_version_reads(
    lock: Path, marketlake: Marketlake, capsys: pytest.CaptureFixture[str]
) -> None:
    marketlake.commit({JOURNAL: "SCHEMA_VERSION: int = 3\n"})
    code, out = _run(lock, capsys)
    assert code == 1
    assert VERSION_SAID in out


def test_a_remote_that_does_not_answer_is_no_pass(
    lock: Path, marketlake: Marketlake, capsys: pytest.CaptureFixture[str]
) -> None:
    marketlake.root.rename(marketlake.root.with_name("gone"))
    code, out = _run(lock, capsys)
    assert code == 2
    assert "git fetch failed" in out


@pytest.mark.parametrize("source", ["", "https://example.com/x", "https://example.com/x?rev=a#abc"])
def test_a_lock_that_pins_no_full_commit_is_no_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    path = tmp_path / "uv.lock"
    path.write_text(f'[[package]]\nname = "marketlake"\nsource = {{ git = "{source}" }}\n')
    code, out = _run(path, capsys)
    assert code == 2
    assert "pins marketlake at no full commit" in out


def test_the_fetch_takes_no_credential_from_the_machine(
    lock: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither the machine's git config nor a token in the environment reaches git."""
    home = tmp_path / "home"
    home.mkdir()
    config = home / ".gitconfig"
    config.write_text("[credential]\n\thelper = planted\n")
    for name, value in {
        "GIT_CONFIG_GLOBAL": str(config),
        "HOME": str(home),
        "GIT_ASKPASS": "planted",
        "GITHUB_TOKEN": "planted",
    }.items():
        monkeypatch.setenv(name, value)
    bare = tmp_path / "bare"
    bare.mkdir()
    remote = script.Remote(bare, "unused")
    assert "credential" not in remote._git("config", "--list").decode()
    assert "planted" not in remote.env.values()
    assert remote.env["GIT_TERMINAL_PROMPT"] == "0"


def test_the_tracked_lock_holds_what_pyproject_pins() -> None:
    """What the check reads is what the dependency rule writes."""
    pin = script.read_pin((REPO / "uv.lock").read_text(encoding="utf-8"))
    wanted = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = wanted["project"]["dependencies"]
    assert f"marketlake @ git+{pin.remote}@{pin.commit}" in dependencies
    assert f"pyarrow=={pin.pyarrow}" in dependencies
    assert pin.remote == "https://github.com/l3a0/marketlake"


# The workflow.


def _workflow() -> dict:
    # YAML 1.1 reads the bare key `on` as true.
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_workflow_runs_on_a_push_to_main_a_schedule_and_by_hand_alone() -> None:
    on = _workflow()[True]
    assert set(on) == {"push", "schedule", "workflow_dispatch"}
    assert on["push"] == {"branches": ["main"]}


def test_every_scheduled_slot_is_on_a_weekday_before_the_earliest_open() -> None:
    """13:30 UTC is 09:30 in New York in summer, and 14:30 is in winter."""
    (slot,) = _workflow()[True]["schedule"]
    minutes, hours, day, month, weekdays = slot["cron"].split()
    assert (day, month, weekdays) == ("*", "*", "1-5")
    first, last = (int(hour) for hour in hours.split("-"))
    assert first < last < 13
    assert all(0 < int(minute) < 60 for minute in minutes.split(","))


def test_the_workflow_reads_and_holds_no_credential() -> None:
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read"}
    (job,) = workflow["jobs"].values()
    assert "permissions" not in job and "environment" not in job and "env" not in job
    checkout, check = job["steps"]
    assert re.fullmatch(r"actions/checkout@[0-9a-f]{40}", checkout["uses"])
    assert checkout["with"] == {"persist-credentials": False}
    assert set(check) == {"name", "shell", "run"}
    # `${{` is how a secret, the token or an event's own text reaches a step.
    assert "${{" not in WORKFLOW.read_text(encoding="utf-8")


def test_the_step_fails_when_the_check_does() -> None:
    """`shell: bash` sets pipefail, so `tee` cannot turn a failed check into a pass."""
    (job,) = _workflow()["jobs"].values()
    step = job["steps"][1]
    assert step["shell"] == "bash"
    assert step["run"] == 'python3 scripts/check_lake_pin.py | tee -a "$GITHUB_STEP_SUMMARY"'


def test_dependabot_leaves_pyarrows_version_alone() -> None:
    """Its weekly group would move pyarrow with no word of marketlake in the title."""
    updates = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    (uv,) = [entry for entry in updates["updates"] if entry["package-ecosystem"] == "uv"]
    assert uv["ignore"] == [
        {
            "dependency-name": "pyarrow",
            "update-types": [
                "version-update:semver-major",
                "version-update:semver-minor",
                "version-update:semver-patch",
            ],
        }
    ]
