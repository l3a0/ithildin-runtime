"""Write the committed journal of every kind again, and say which entries moved.

`tests/fixtures/orders/journal/fixture-1/` holds one journal entry of every
kind, and `test_2_this_release_writes_the_committed_bytes_of_every_kind` in
tests/test_journal_entries.py checks it byte for byte against what this
release writes from that module's `_committed()` list. Each entry carries the
hash of the one before it. So when two branches each append an entry, the
merge puts the second branch's entry under the next number, and every entry
from there on has to be written again.

This script writes `_committed()` into an empty folder through that module's
`write_committed`, the function the bytes test writes through, and prints one
line per entry. The line says whether the entry matches the committed file
and the file at a git ref, `origin/main` unless `--against` names another.
A merge where both sides appended an entry takes five steps.

1. Fetch and merge, so the ref is current and this branch holds it.
2. Resolve the conflict in `_committed()` with the ref's entries first and
   this branch's after them. Leave the conflict in the fixture folder, which
   `--write` replaces.
3. Run the script, and read each line.
4. Run it with `--write`.
5. Mark the folder resolved, and move `replay.json` by hand.

    git fetch origin
    git merge origin/main
    uv run python scripts/regenerate_journal_fixture.py
    uv run python scripts/regenerate_journal_fixture.py --write
    git add tests/fixtures/orders

Each column reads `same`, `differs`, `new` where only this run writes the
entry, `gone` where only that side holds it, or `-` where neither does.

An entry the ref holds must read `same` in its column, because a merge only
appends. When one reads `differs` or `gone`, the script names those entries,
writes nothing and ends with 1, with or without `--write`. That has one of
three causes.

1. The ref holds entries this branch has not merged, so merge it first.
2. `_committed()` lost the ref's order in the merge.
3. This branch changed how an entry the ref holds is written, which the rules
   in docs/architecture/08-crosscutting-concepts.md's `##### What holds a
   kind still` govern.

This script writes none of the three. For the third, call `write_committed`
from tests/test_journal_entries.py on an empty folder, read the diff of its
`journal/fixture-1/` against the committed one, and copy it in by hand.

A traceback, such as from a `_committed()` that still holds conflict markers,
also ends with 1. The script ends with 2, writing nothing,
when the ref cannot be listed or holds no file in the folder, so an unread ref
never passes.

With `--write`, and only once every entry the ref holds reads `same`, it names
any committed file this run did not write, then replaces the committed folder
with what it wrote, which removes those files. It prints the last entry's
number beside `replay.json`'s `afterwards.through` and never edits
`replay.json`, because that file was worked out by hand from the entries, and
the replay test checks the journal against it. Move it by hand to what the
new entries do.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# A script run by path has its own folder on the path, not the repository's,
# and `tests` is a package at the repository's root.
sys.path.insert(0, str(ROOT))

from tests.test_journal_entries import FIXED, write_committed  # noqa: E402

ORDERS = Path("tests") / "fixtures" / "orders"
FOLDER = ORDERS / "journal" / FIXED
REPLAY = ORDERS / "replay.json"

# Reads the committed folder at a ref, by file name, or gives `None` when the
# ref cannot be read or holds no file there.
type ReadRef = Callable[[Path, str], Mapping[str, bytes] | None]


@dataclass(frozen=True)
class Entry:
    """One file name, and how this run's bytes compare with each side's."""

    name: str
    here: str
    there: str


def _files(folder: Path) -> dict[str, bytes]:
    if not folder.is_dir():
        return {}
    return {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}


def _state(name: str, ours: Mapping[str, bytes], theirs: Mapping[str, bytes]) -> str:
    if name not in ours and name not in theirs:
        return "-"
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
    """The committed folder's files at `ref`, read with `git ls-tree` and `git show`.

    It gives `None` where git cannot start or read the ref, and where the ref
    holds no file in the folder, such as a ref older than the fixture.
    """
    try:
        listing = subprocess.run(
            ["git", "ls-tree", "-z", "--name-only", ref, "--", f"{FOLDER.as_posix()}/"],
            cwd=root,
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    paths = [path for path in listing.stdout.decode().split("\0") if path]
    if listing.returncode != 0 or not paths:
        return None
    files = {}
    for path in paths:
        # A file the listing names is there to show, so a failure here raises.
        shown = subprocess.run(
            ["git", "show", f"{ref}:{path}"], cwd=root, capture_output=True, check=True
        )
        files[path.rpartition("/")[2]] = shown.stdout
    return files


def _through(root: Path) -> str:
    """`replay.json`'s `afterwards.through`, or why it could not be read."""
    try:
        return str(json.loads((root / REPLAY).read_text())["afterwards"]["through"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        return f"unreadable ({type(error).__name__}), so resolve replay.json by hand"


def main(argv: list[str] | None = None, root: Path = ROOT, read_ref: ReadRef = git_ref) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--against", default="origin/main", help="the ref to compare with")
    parser.add_argument("--write", action="store_true", help="replace the committed folder")
    args = parser.parse_args(argv)

    at_ref = read_ref(root, args.against)
    if not at_ref:
        print(
            f"{args.against} could not be read, or holds no file in {FOLDER.as_posix()}, "
            "so nothing is compared or written."
        )
        return 2
    committed_folder = root / FOLDER
    with tempfile.TemporaryDirectory() as scratch:
        write_committed(Path(scratch))
        written_folder = Path(scratch) / "journal" / FIXED
        written = _files(written_folder)
        entries = compare(written, _files(committed_folder), at_ref)
        print(f"{'entry':<18} {'committed':<10} {args.against}")
        for entry in entries:
            print(f"{entry.name:<18} {entry.here:<10} {entry.there}")
        last = max(int(Path(name).stem) for name in written)
        through = _through(root)
        print(f"The last entry is {last}, and replay.json's afterwards.through is {through}.")
        if through != str(last):
            print("Move replay.json's afterwards by hand to what the new entries do.")
        moved = [entry.name for entry in entries if entry.there in ("differs", "gone")]
        if moved:
            print(
                f"{args.against} holds entries this run would change or remove: "
                f"{', '.join(moved)}. Either this branch has not merged the ref, "
                "or _committed() lost the ref's order in the merge, "
                "or this branch changed how those entries are written, which the rules in "
                "docs/architecture/08-crosscutting-concepts.md govern. Nothing is written."
            )
            return 1
        stale = [entry.name for entry in entries if entry.here == "gone"]
        if args.write:
            if stale:
                print(f"Removing {', '.join(stale)}, which this run does not write.")
            if committed_folder.exists():
                shutil.rmtree(committed_folder)
            shutil.copytree(written_folder, committed_folder)
            print(f"Wrote {len(written)} entries to {FOLDER.as_posix()}.")
        elif any(entry.here != "same" for entry in entries):
            print("The committed folder differs. Run again with --write to replace it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
