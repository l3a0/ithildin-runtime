"""The script that writes the committed journal of every kind again.

scripts/regenerate_journal_fixture.py replays `_committed()` from
tests/test_journal_entries.py and compares each entry with the committed
folder and with the folder at a git ref. Most cases copy the committed
fixture under `tmp_path`, change one thing, and run the script's `main`
there with the ref's files handed in, so no case moves the real fixture. The
first case reads the real tree and `HEAD` through git.
"""

from __future__ import annotations

import importlib.util
import shutil
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


def _copy(tmp_path: Path) -> Path:
    """A root holding a copy of the committed orders fixture, and its folder of entries."""
    shutil.copytree(REPO / script.ORDERS, tmp_path / script.ORDERS)
    return tmp_path / script.FOLDER


class _Ref:
    """A ref reader that hands back `files`, and records which ref it was asked for."""

    def __init__(self, files: Mapping[str, bytes] | None) -> None:
        self.files = files
        self.asked: list[str] = []

    def __call__(self, root: Path, ref: str) -> Mapping[str, bytes] | None:
        self.asked.append(ref)
        return self.files


def _lines(out: str) -> dict[str, tuple[str, str]]:
    """Each entry line of the output, by file name, as its two columns."""
    rows = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[0].endswith(".json"):
            rows[parts[0]] = (parts[1], parts[2])
    return rows


def _flip(data: bytes) -> bytes:
    """`data` with one byte changed, inside the entry's text."""
    at = len(data) // 2
    return data[:at] + bytes([data[at] ^ 1]) + data[at + 1 :]


def test_on_this_tree_every_entry_is_the_same_here_and_at_head(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert script.main(["--against", "HEAD"]) == 0
    out = capsys.readouterr().out
    assert _lines(out) == {name: ("same", "same") for name in MAIN}
    assert "--write" not in out
    assert f"The last entry is {int(Path(LAST).stem)}, " in out


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


def test_an_entry_the_ref_holds_that_this_run_would_change_ends_with_1_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _copy(tmp_path)
    (folder / "000000000099.json").write_bytes(b"stale")
    before = _files(folder)
    theirs = dict(MAIN)
    theirs["000000000040.json"] = _flip(theirs["000000000040.json"])

    assert script.main(["--write"], root=tmp_path, read_ref=_Ref(theirs)) == 1

    out = capsys.readouterr().out
    assert _lines(out)["000000000040.json"] == ("same", "differs")
    assert "would change or remove: 000000000040.json." in out
    assert _files(folder) == before


def test_an_entry_only_the_ref_holds_ends_with_1_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _copy(tmp_path)
    (folder / "000000000005.json").write_bytes(b"changed")
    before = _files(folder)
    theirs = dict(MAIN)
    theirs["000000000099.json"] = b"main's entry"

    assert script.main(["--write"], root=tmp_path, read_ref=_Ref(theirs)) == 1

    out = capsys.readouterr().out
    assert _lines(out)["000000000099.json"] == ("new", "gone")
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

    rows = _lines(capsys.readouterr().out)
    assert rows[LAST] == ("same", "new")
    assert rows["000000000099.json"] == ("gone", "new")
    assert read.asked == ["main"]
    assert _files(folder) == MAIN
    assert replay.read_bytes() == replay_before


def test_a_ref_that_cannot_be_read_ends_with_2_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    folder = _copy(tmp_path)
    (folder / "000000000099.json").write_bytes(b"stale")
    before = _files(folder)

    assert script.main(["--write"], root=tmp_path, read_ref=_Ref(None)) == 2

    assert "could not be read" in capsys.readouterr().out
    assert _files(folder) == before


def test_git_ref_reads_the_folder_at_head_and_gives_none_for_a_ref_git_cannot_read() -> None:
    assert script.git_ref(REPO, "HEAD") == MAIN
    assert script.git_ref(REPO, "no-such-ref-of-this-repository") is None
