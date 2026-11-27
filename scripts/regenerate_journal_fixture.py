"""Write the committed journal of every kind again, and say which entries moved.

`tests/fixtures/orders/journal/fixture-1/` holds one journal entry of every
kind, and `test_2_this_release_writes_the_committed_bytes_of_every_kind` in
tests/test_journal_entries.py checks it byte for byte against what this
release writes from that module's `_committed()` list. Each entry carries the
hash of the one before it. So when two branches each append an entry, the
merge puts the second branch's entry under the next number, and every entry
from there on has to be written again.

This script replays `_committed()` into an empty folder, exactly as that test
does, and prints one line per entry. The line says whether the entry matches
the committed file and the file at a git ref, `origin/main` unless `--against`
names another. Fetch first, so the ref is current, then run:

    git fetch origin
    uv run python scripts/regenerate_journal_fixture.py
    uv run python scripts/regenerate_journal_fixture.py --write

Each column reads `same`, `differs`, `new` where only this run writes the
entry, or `gone` where only that side holds it. An entry the ref holds must
read `same` in its column, because a merge only appends. When one reads
`differs` or `gone`, `_committed()` lost the ref's order in the merge, so the
script names those entries, writes nothing and ends with 1. It ends with 2,
writing nothing, when the ref cannot be read, so an unread ref never passes.

With `--write`, and only once every entry the ref holds reads `same`, it
replaces the committed folder with what it wrote, which removes any file this
run did not write. It prints the last entry's number beside `replay.json`'s
`afterwards.through` and never edits `replay.json`, because that file was
worked out by hand from the entries, and the replay test checks the journal
against it. Move it by hand to what the new entries do.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# A script run by path has its own folder on the path, not the repository's,
# and `tests` is a package at the repository's root.
sys.path.insert(0, str(ROOT))

from ithildinruntime.adapters import FolderStore  # noqa: E402
from ithildinruntime.journal import Journal, Record  # noqa: E402

ORDERS = Path("tests") / "fixtures" / "orders"
FIXED = "fixture-1"
FOLDER = ORDERS / "journal" / FIXED
REPLAY = ORDERS / "replay.json"
WRITER = "0123456789abcdef" * 2

# Reads the committed folder at a ref, by file name, or gives `None` when the
# ref cannot be read.
type ReadRef = Callable[[Path, str], Mapping[str, bytes] | None]


@dataclass(frozen=True)
class Entry:
    """One file name, and how this run's bytes compare with each side's."""

    name: str
    here: str
    there: str


def write(records: Iterable[tuple[Record, datetime]], into: Path) -> Path:
    """Record each item into an empty folder, as the bytes test does, and return the journal."""
    journal = Journal(FolderStore(into), FIXED, WRITER)
    view = journal.view()
    for record, at in records:
        _, view = journal.record(view, record, at)
    return into / "journal" / FIXED


def _files(folder: Path) -> dict[str, bytes]:
    if not folder.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}


def _state(name: str, ours: Mapping[str, bytes], theirs: Mapping[str, bytes]) -> str:
    if name not in theirs:
        return "new"
    if name not in ours:
        return "gone"
    return "same" if ours[name] == theirs[name] else "differs"


def compare(
    written: Mapping[str, bytes],
    committed: Mapping[str, bytes],
    at_ref: Mapping[str, bytes],
) -> list[Entry]:
    """One entry per file name any of the three holds, in name order."""
    names = sorted(written.keys() | committed.keys() | at_ref.keys())
    return [
        Entry(name, _state(name, written, committed), _state(name, written, at_ref))
        for name in names
    ]


def git_ref(root: Path, ref: str) -> dict[str, bytes] | None:
    """The committed folder's files at `ref`, read with `git ls-tree` and `git show`."""
    listing = subprocess.run(
        ["git", "ls-tree", "-z", "--name-only", ref, "--", f"{FOLDER.as_posix()}/"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if listing.returncode != 0:
        return None
    files = {}
    for path in listing.stdout.decode().split("\0"):
        if not path:
            continue
        # A file the listing names is there to show, so a failure here raises.
        shown = subprocess.run(
            ["git", "show", f"{ref}:{path}"], cwd=root, capture_output=True, check=True
        )
        files[path.rpartition("/")[2]] = shown.stdout
    return files


def main(argv: list[str] | None = None, root: Path = ROOT, read_ref: ReadRef = git_ref) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--against", default="origin/main", help="the ref to compare with")
    parser.add_argument("--write", action="store_true", help="replace the committed folder")
    args = parser.parse_args(argv)
    from tests.test_journal_entries import _committed

    at_ref = read_ref(root, args.against)
    if at_ref is None:
        print(f"{args.against} could not be read, so nothing is compared or written.")
        return 2
    committed_folder = root / FOLDER
    with tempfile.TemporaryDirectory() as scratch:
        written_folder = write(_committed(), Path(scratch))
        written = _files(written_folder)
        entries = compare(written, _files(committed_folder), at_ref)
        print(f"{'entry':<18} {'committed':<10} {args.against}")
        for entry in entries:
            print(f"{entry.name:<18} {entry.here:<10} {entry.there}")
        last = max(int(Path(name).stem) for name in written)
        through = json.loads((root / REPLAY).read_text())["afterwards"]["through"]
        print(f"The last entry is {last}, and replay.json's afterwards.through is {through}.")
        moved = [entry.name for entry in entries if entry.there in ("differs", "gone")]
        if moved:
            print(
                f"{args.against} holds entries this run would change or remove: "
                f"{', '.join(moved)}. _committed() has lost its order, so nothing is written."
            )
            return 1
        if args.write:
            if committed_folder.exists():
                shutil.rmtree(committed_folder)
            shutil.copytree(written_folder, committed_folder)
            print(f"Wrote {len(written)} entries to {FOLDER.as_posix()}.")
        elif any(entry.here != "same" for entry in entries):
            print("The committed folder differs. Run again with --write to replace it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
