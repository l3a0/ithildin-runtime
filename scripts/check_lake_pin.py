"""Fail when marketlake's `main` says the pinned commit is due a raise.

The runtime reads a day before marketlake seals it through marketlake's own
reader, at the commit uv.lock pins. marketlake's contract, the section `Read
an unsealed day` of its README, names what a consumer watches on `main`, and
this compares three things there with the pin.

1. `SCHEMA_VERSION` in `src/lake/journal.py`. Once the capture host writes a
   version above the pin's, the pinned reader refuses every read, so a higher
   version is raised now.
2. The bytes of `src/lake/segment_join.py`, which joins segments into rows.
3. pyarrow's version in `uv.lock`, against the one this repository's `uv.lock`
   holds rather than the pinned commit's, so a raise that moved one of the two
   without the other also shows.

The rows read before the seal equal the seal's only while the join and pyarrow
match the capture host's. A merge to marketlake's `main` can wait days for its
deploy, so a change to either alone waits until that deploy is known.

It exits 0 when nothing moved, 1 when something did, naming what and which
rule applies, and 2 when either side could not be read, so an unread side
never passes. It fetches both commits from marketlake's public repository with
git's credential helpers and prompts off, and it parses what it fetched and
runs none of it. It prints no text of marketlake's but a version it checked
the form of. Standard library only, so `.github/workflows/lake-pin.yml` runs it
with the runner's own `python3`.

Usage: `python3 scripts/check_lake_pin.py [--lock uv.lock] [--main refs/heads/main]`
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

JOURNAL = "src/lake/journal.py"
JOIN = "src/lake/segment_join.py"
LOCK = "uv.lock"
CONTRACT = "https://github.com/l3a0/marketlake#how-a-consumer-pins-it"
FETCH_SECONDS = 120

_VERSION = re.compile(r"[0-9]+(?:\.[0-9A-Za-z]+)*")
_COMMIT = re.compile(r"[0-9a-f]{40}")

RAISE_NOW = (
    "Rule: raise the pin now, before the first session captured under the higher version. "
    "The pinned reader refuses every row written under it."
)
RAISE_TOGETHER = (
    "Rule: the join or pyarrow moves in that same raise. Until marketlake's deploy, take the "
    "rows read before a seal as possibly unequal to the seal's."
)
WAIT = (
    "Rule: wait to raise the pin until marketlake's deploy of this is known. Until then the "
    "pin matches the capture host, and afterwards it does not."
)
NO_RAISE = "Rule: raise nothing for this. A newer pin reads the older rows."


class Unreadable(Exception):
    """A side of the comparison could not be read, so nothing can be said of it."""


@dataclass(frozen=True)
class Pin:
    """What this repository's lock pins: marketlake's remote and commit, and pyarrow."""

    remote: str
    commit: str
    pyarrow: str


@dataclass(frozen=True)
class Side:
    """What one marketlake commit holds. `join` is `None` where the file is gone."""

    version: int
    join: bytes | None


def package(text: str, name: str, where: str) -> dict:
    """The one entry a uv lock holds for a package. A lock can hold several, by marker."""
    try:
        found = [held for held in tomllib.loads(text)["package"] if held["name"] == name]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as error:
        raise Unreadable(f"{where} is no uv lock: {type(error).__name__}") from error
    if len(found) != 1:
        raise Unreadable(f"{where} holds {len(found)} entries for {name}")
    return found[0]


def pyarrow_version(text: str, where: str) -> str:
    version = package(text, "pyarrow", where).get("version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise Unreadable(f"{where} names no version of pyarrow")
    return version


def read_pin(text: str) -> Pin:
    source = package(text, "marketlake", LOCK).get("source", {}).get("git", "")
    remote, _, commit = source.partition("#")
    if not remote or not _COMMIT.fullmatch(commit):
        raise Unreadable(f"{LOCK} pins marketlake at no full commit")
    return Pin(remote.partition("?")[0], commit, pyarrow_version(text, LOCK))


def schema_version(source: bytes, where: str) -> int:
    """The one integer a module assigns to `SCHEMA_VERSION` at its top level."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError) as error:
        raise Unreadable(f"{where} does not parse: {type(error).__name__}") from error
    found = []
    for stmt in tree.body:
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        if any(isinstance(t, ast.Name) and t.id == "SCHEMA_VERSION" for t in targets):
            found.append(stmt.value)
    if len(found) != 1 or not isinstance(found[0], ast.Constant) or type(found[0].value) is not int:
        raise Unreadable(f"{where} assigns no one integer to SCHEMA_VERSION")
    return found[0].value


def differences(pin: Pin, pinned: Side, main: Side, main_pyarrow: str) -> list[str]:
    """Each thing that moved, and after them the rule that applies to it."""
    said: list[str] = []
    higher = main.version > pinned.version
    if main.version != pinned.version:
        said.append(
            f"`SCHEMA_VERSION` in {JOURNAL} is {main.version} on marketlake's main and "
            f"{pinned.version} at the pin."
        )
        said.append(RAISE_NOW if higher else NO_RAISE)
    moved = []
    if main.join is None:
        moved.append(f"{JOIN} is not on marketlake's main, and the pin holds it.")
    elif main.join != pinned.join:
        moved.append(f"{JOIN} on marketlake's main differs from the pin's.")
    if main_pyarrow != pin.pyarrow:
        moved.append(
            f"pyarrow is {main_pyarrow} in marketlake's {LOCK} on main and {pin.pyarrow} in "
            f"this repository's {LOCK}."
        )
    if moved:
        said += [*moved, RAISE_TOGETHER if higher else WAIT]
    return said


class Remote:
    """A bare repository holding the commits fetched from a remote, with no credential."""

    def __init__(self, directory: Path, remote: str) -> None:
        self.directory = directory
        self.remote = remote
        # No config but the directory's own, so no credential helper, header or
        # rewritten address of the machine's is sent.
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(directory),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self._git("init", "--quiet", "--bare", ".")

    def _git(self, *args: str) -> bytes:
        try:
            done = subprocess.run(
                ["git", *args],
                cwd=self.directory,
                env=self.env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=FETCH_SECONDS,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise Unreadable(f"git {args[0]} failed: {type(error).__name__}") from error
        return done.stdout

    def fetch(self, ref: str) -> str:
        """The commit `ref` names at the remote, fetched."""
        self._git("fetch", "--quiet", "--depth=1", "--no-tags", self.remote, ref)
        return self._git("rev-parse", "FETCH_HEAD^{commit}").decode().strip()

    def file(self, commit: str, path: str) -> bytes | None:
        """The file's bytes at a fetched commit, or `None` where the commit holds none."""
        listed = self._git("ls-tree", "--name-only", commit, "--", path)
        return self._git("cat-file", "blob", f"{commit}:{path}") if listed.strip() else None

    def side(self, commit: str) -> Side:
        journal = self.file(commit, JOURNAL)
        if journal is None:
            raise Unreadable(f"{JOURNAL} is not at {commit[:12]}")
        return Side(schema_version(journal, f"{JOURNAL} at {commit[:12]}"), self.file(commit, JOIN))


def check(lock: Path, main_ref: str) -> list[str]:
    pin = read_pin(lock.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory:
        remote = Remote(Path(directory), pin.remote)
        pinned = remote.side(remote.fetch(pin.commit))
        if pinned.join is None:
            raise Unreadable(f"{JOIN} is not at the pin")
        commit = remote.fetch(main_ref)
        locked = remote.file(commit, LOCK)
        if locked is None:
            raise Unreadable(f"{LOCK} is not at {commit[:12]}")
        where = f"marketlake's {LOCK} at {commit[:12]}"
        try:
            main_pyarrow = pyarrow_version(locked.decode(), where)
        except UnicodeDecodeError as error:
            raise Unreadable(f"{where} is not text") from error
        said = differences(pin, pinned, remote.side(commit), main_pyarrow)
    against = f"marketlake's main at {commit[:12]} against the pin at {pin.commit[:12]}"
    if said:
        return [f"The marketlake pin may be due a raise: {against}.", *said, f"Read {CONTRACT}."]
    print(f"Nothing moved: {against}.")
    return []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument("--lock", type=Path, default=Path(LOCK))
    parser.add_argument("--main", default="refs/heads/main")
    args = parser.parse_args(argv)
    try:
        said = check(args.lock, args.main)
    except (Unreadable, OSError) as error:
        print(f"The check could not run, so it says nothing of the pin: {error}")
        return 2
    for line in said:
        print(f"- {line}")
    return 1 if said else 0


if __name__ == "__main__":
    sys.exit(main())
