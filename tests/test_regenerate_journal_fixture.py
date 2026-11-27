"""The script that writes the committed journal of every kind again.

scripts/regenerate_journal_fixture.py writes `_committed()` from
tests/test_journal_entries.py and compares each entry with the committed
folder and with the folder at a git ref. Each case copies the committed
fixture under `tmp_path`, changes what the case names, and runs the script's `main`
there, so no case moves the real fixture or reads the real checkout's git
history. Most hand the ref's files in. The cases that read a ref through git
commit the copy into a repository of their own under `tmp_path`, so a
regenerated fixture not yet committed in the real checkout fails none of them.
The case that runs the script as a command copies nothing, and points git at
an empty folder under `tmp_path`, so it reads nothing of the checkout either.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "regenerate_journal_fixture.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("regenerate_journal_fixture", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load()
FOLDER = REPO / script.FOLDER


def _files(folder: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in sorted(folder.iterdir())}


MAIN = _files(FOLDER)
LAST = max(MAIN)
LAST_NUMBER = int(Path(LAST).stem)
THROUGH = json.loads((REPO / script.REPLAY).read_text())["afterwards"]["through"]
DRY_AND_WRITE = pytest.mark.parametrize("argv", [[], ["--write"]], ids=["dry", "write"])


def _copy(tmp_path: Path) -> Path:
    """A root holding a copy of the committed orders fixture, and its folder of entries."""
    shutil.copytree(REPO / script.ORDERS, tmp_path / script.ORDERS)
    return tmp_path / script.FOLDER


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _committed_copy(tmp_path: Path) -> Path:
    """A repository under `tmp_path` whose one commit holds a copy of the orders fixture."""
    _copy(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "fixture")
    return tmp_path


class _Ref:
    """A ref reader that hands back `files`, and records which ref it was asked for."""

    def __init__(self, files: Mapping[str, bytes] | None) -> None:
        self.files = files
        self.asked: list[str] = []

    def __call__(self, root: Path, ref: str) -> Mapping[str, bytes] | None:
        self.asked.append(ref)
        return self.files


def _rows(out: str) -> list[tuple[str, str, str]]:
    """Each entry line of the output, in the order printed, as its name and two columns."""
    rows = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0].endswith(".json"):
            rows.append((parts[0], parts[1], parts[2]))
    return rows


def _lines(out: str) -> dict[str, tuple[str, str]]:
    """Each entry line of the output, by file name, as its two columns."""
    return {name: (here, there) for name, here, there in _rows(out)}


def _flip(data: bytes) -> bytes:
    """`data` with one byte changed, inside the entry's text."""
    at = len(data) // 2
    return data[:at] + bytes([data[at] ^ 1]) + data[at + 1 :]


def test_on_a_tree_like_this_one_every_entry_is_the_same_here_and_at_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _committed_copy(tmp_path)

    assert script.main(["--against", "HEAD"], root=root) == 0

    out = capsys.readouterr().out
    assert _rows(out) == [(name, "same", "same") for name in sorted(MAIN)]
    assert (
        f"The last entry is {LAST_NUMBER}, and replay.json's afterwards.through is {THROUGH}."
        in out
    )
    assert "Move replay.json" not in out
    assert "--write" not in out


def test_a_changed_byte_in_a_committed_entry_reads_differs_and_nothing_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _copy(tmp_path)
    changed = folder / "000000000005.json"
    changed.write_bytes(_flip(changed.read_bytes()))
    before = _files(folder)
    read = _Ref(MAIN)

    assert script.main([], root=tmp_path, read_ref=read) == 0

    assert read.asked == ["origin/main"]
    out = capsys.readouterr().out
    rows = _lines(out)
    assert rows.pop("000000000005.json") == ("differs", "same")
    assert set(rows.values()) == {("same", "same")}
    assert "Run again with --write" in out
    assert _files(folder) == before


def test_a_branch_whose_new_entry_is_not_yet_committed_is_told_to_run_with_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _copy(tmp_path)
    (folder / LAST).unlink()
    before = _files(folder)

    assert script.main([], root=tmp_path, read_ref=_Ref(MAIN)) == 0

    out = capsys.readouterr().out
    assert _lines(out)[LAST] == ("new", "same")
    assert "Run again with --write" in out
    assert _files(folder) == before


@DRY_AND_WRITE
def test_an_entry_the_ref_holds_that_this_run_would_change_ends_with_1_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    folder = _copy(tmp_path)
    (folder / "000000000099.json").write_bytes(b"stale")
    before = _files(folder)
    theirs = dict(MAIN)
    theirs["000000000040.json"] = _flip(theirs["000000000040.json"])

    assert script.main(argv, root=tmp_path, read_ref=_Ref(theirs)) == 1

    out = capsys.readouterr().out
    assert _lines(out)["000000000040.json"] == ("same", "differs")
    assert "would change or remove: 000000000040.json. Either this branch has not merged" in out
    assert "or _committed() lost the ref's order" in out
    assert "or this branch changed how those entries are written" in out
    assert _files(folder) == before


@DRY_AND_WRITE
def test_an_entry_only_the_ref_holds_ends_with_1_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    folder = _copy(tmp_path)
    (folder / "000000000005.json").write_bytes(b"changed")
    before = _files(folder)
    theirs = dict(MAIN)
    theirs["000000000099.json"] = b"main's entry"

    assert script.main(argv, root=tmp_path, read_ref=_Ref(theirs)) == 1

    out = capsys.readouterr().out
    assert _lines(out)["000000000099.json"] == ("-", "gone")
    assert "would change or remove: 000000000099.json." in out
    assert _files(folder) == before


def test_write_replaces_the_folder_and_removes_a_stale_file_once_the_ref_only_gains(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _copy(tmp_path)
    (folder / "000000000099.json").write_bytes(b"stale")
    changed = folder / "000000000005.json"
    changed.write_bytes(_flip(changed.read_bytes()))
    replay = tmp_path / script.REPLAY
    replay_before = replay.read_bytes()
    # The ref lacks the last entry, as main does before a branch's new entry merges.
    theirs = {name: data for name, data in MAIN.items() if name != LAST}
    read = _Ref(theirs)

    assert script.main(["--write", "--against", "main"], root=tmp_path, read_ref=read) == 0

    out = capsys.readouterr().out
    rows = _lines(out)
    assert rows[LAST] == ("same", "new")
    assert rows["000000000099.json"] == ("gone", "-")
    assert "Removing 000000000099.json, which this run does not write." in out
    assert f"The last entry is {LAST_NUMBER}, " in out
    assert read.asked == ["main"]
    assert _files(folder) == MAIN
    assert replay.read_bytes() == replay_before


def test_a_replay_json_behind_the_entries_is_named_and_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _copy(tmp_path)
    replay = tmp_path / script.REPLAY
    expected = json.loads(replay.read_text())
    expected["afterwards"]["through"] = THROUGH - 1
    replay.write_text(json.dumps(expected))
    replay_before = replay.read_bytes()

    assert script.main(["--write"], root=tmp_path, read_ref=_Ref(MAIN)) == 0

    out = capsys.readouterr().out
    assert f"afterwards.through is {THROUGH - 1}." in out
    assert "Move replay.json's afterwards by hand" in out
    assert replay.read_bytes() == replay_before


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("<<<<<<< ours\n{}\n=======\n{}\n>>>>>>> theirs\n", "JSONDecodeError"),
        (None, "FileNotFoundError"),
    ],
    ids=["conflicted", "missing"],
)
def test_a_replay_json_that_cannot_be_read_is_named_and_the_entries_are_still_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], text: str | None, error: str
) -> None:
    _copy(tmp_path)
    replay = tmp_path / script.REPLAY
    if text is None:
        replay.unlink()
    else:
        replay.write_text(text)

    assert script.main([], root=tmp_path, read_ref=_Ref(MAIN)) == 0

    out = capsys.readouterr().out
    assert set(_lines(out).values()) == {("same", "same")}
    assert f"afterwards.through is unreadable ({error})" in out


def test_write_builds_a_committed_folder_that_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _copy(tmp_path)
    shutil.rmtree(folder)

    assert script.main(["--write"], root=tmp_path, read_ref=_Ref(MAIN)) == 0

    assert set(_lines(capsys.readouterr().out).values()) == {("new", "same")}
    assert _files(folder) == MAIN


@pytest.mark.parametrize("files", [None, {}], ids=["unread", "empty"])
def test_a_ref_that_cannot_be_read_or_holds_no_entry_ends_with_2_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], files: Mapping[str, bytes] | None
) -> None:
    folder = _copy(tmp_path)
    (folder / "000000000099.json").write_bytes(b"stale")
    before = _files(folder)

    assert script.main(["--write"], root=tmp_path, read_ref=_Ref(files)) == 2

    assert "could not be listed, or holds no file in" in capsys.readouterr().out
    assert _files(folder) == before


def test_git_ref_reads_the_folder_at_a_commit_and_gives_none_where_it_holds_nothing(
    tmp_path: Path,
) -> None:
    root = _committed_copy(tmp_path)
    # A changed byte in the working tree shows the bytes come from the commit.
    changed = root / script.FOLDER / "000000000005.json"
    changed.write_bytes(_flip(changed.read_bytes()))
    assert script.git_ref(root, "HEAD") == MAIN
    shutil.rmtree(root / script.FOLDER)
    _git(root, "commit", "-qam", "no fixture")

    assert script.git_ref(root, "HEAD~1") == MAIN
    assert script.git_ref(root, "HEAD") is None
    assert script.git_ref(root, "no-such-ref-of-this-repository") is None
    assert script.git_ref(tmp_path / "no-such-folder", "HEAD") is None


def test_run_as_a_command_it_ends_with_the_code_main_returns(tmp_path: Path) -> None:
    run = subprocess.run(
        [sys.executable, str(SCRIPT), "--against", "no-such-ref-of-this-repository"],
        env={**os.environ, "GIT_DIR": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 2, run.stderr
    assert "could not be listed, or holds no file in" in run.stdout
