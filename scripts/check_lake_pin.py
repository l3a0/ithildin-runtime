"""Fail when marketlake's `main`, or pyarrow here, differs from the pinned commit.

The runtime reads a day before marketlake seals it through marketlake's own
reader, at the commit uv.lock pins. marketlake's contract, the section `Read
an unsealed day` of its README, names what a consumer watches on `main`. This
compares the same three things there and at the pinned commit.

1. `SCHEMA_VERSION` in `src/lake/journal.py`. Once the capture host writes a
   version above the pin's, the pinned reader refuses every read, so a higher
   version is raised now.
2. The bytes of `src/lake/segment_join.py`, which joins segments into rows.
3. pyarrow's version in `uv.lock`.

The rows read before the seal equal the seal's only while the join and pyarrow
match the capture host's. A merge to marketlake's `main` can wait days for its
deploy, so a change to either alone waits until that deploy is known.

It also compares this repository's pyarrow with the one the pinned commit's own
`uv.lock` holds, which the contract says to pin. A raise that moved one of the
two without the other shows there, whatever `main` holds.

It exits 0 when nothing differs, 1 when something does, naming what and which
rule applies, and 2 when either side could not be read, so an unread side
never passes. It fetches both commits from marketlake's public repository with
git's credential helpers and prompts off, and it parses what it fetched and
runs none of it. It prints no text of marketlake's but a version it checked
the form of. It reads the tip of `main` alone, so it does not see a version
raised and reverted between two runs, or whether the pin is on `main`. Standard
library only, so `.github/workflows/lake-pin.yml` runs it with the runner's own
`python3`, which has to be 3.11 or later.

Usage: `uv run python scripts/check_lake_pin.py [--lock uv.lock] [--main refs/heads/main]`
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
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

JOURNAL = "src/lake/journal.py"
JOIN = "src/lake/segment_join.py"
LOCK = "uv.lock"
CONTRACT = "https://github.com/l3a0/marketlake#how-a-consumer-pins-it"
FETCH_SECONDS = 120
# Far past either file, whose sizes at the pin are under half a megabyte.
FILE_BYTES = 8_000_000

_VERSION = re.compile(r"[0-9]+(?:\.[0-9A-Za-z]+)*")
_VERSION_LENGTH = 32
_SCHEMA_VERSIONS = range(1, 10_000)
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
    "Rule: wait to raise the pin until marketlake's deploy of this is known. Nothing here says "
    "which commit the capture host runs."
)
NO_RAISE = (
    "Rule: raise nothing for this. A newer pin reads the older rows, where the lake's "
    "schema-version ledger holds their version."
)
MATCH_PIN = (
    "Rule: set pyarrow here to the version at the commit this repository pins now, or at the "
    "raised one where a raise is due. The two pins move in one pull request."
)


UNREAD = "The check could not run, so it says nothing of the pin"


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
    pyarrow: str


def package(text: str, name: str, where: str) -> dict:
    """The one entry a uv lock holds for a package. A lock can hold several, by marker."""
    try:
        found = [held for held in tomllib.loads(text)["package"] if held["name"] == name]
    except (ValueError, KeyError, TypeError) as error:
        raise Unreadable(f"{where} is no uv lock: {type(error).__name__}") from error
    if len(found) != 1:
        raise Unreadable(f"{where} holds {len(found)} entries for {name}")
    return found[0]


def pyarrow_version(text: str, where: str) -> str:
    version = package(text, "pyarrow", where).get("version")
    if (
        not isinstance(version, str)
        or len(version) > _VERSION_LENGTH
        or not _VERSION.fullmatch(version)
    ):
        raise Unreadable(f"{where} names no version of pyarrow")
    return version


def read_pin(text: str) -> Pin:
    source = package(text, "marketlake", LOCK).get("source", {}).get("git", "")
    remote, _, commit = source.partition("#")
    if not remote or not _COMMIT.fullmatch(commit):
        raise Unreadable(f"{LOCK} pins marketlake at no full commit")
    return Pin(remote.partition("?")[0], commit, pyarrow_version(text, LOCK))


def schema_version(source: bytes, where: str) -> int:
    """The one integer a module assigns to `SCHEMA_VERSION`, at its top level.

    A second assignment to the name, at any depth, is refused, because this
    reads the module and never runs it. A binding by an import, a `def` or
    `globals()` is not read.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
        stored = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Name)
            and node.id == "SCHEMA_VERSION"
            and not isinstance(node.ctx, ast.Load)
        ]
    except (SyntaxError, ValueError) as error:
        raise Unreadable(f"{where} does not parse: {type(error).__name__}") from error
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        else:
            continue
        if stored == targets and isinstance(stmt.value, ast.Constant):
            value = stmt.value.value
            if type(value) is int and value in _SCHEMA_VERSIONS:
                return value
    raise Unreadable(f"{where} assigns no one integer to SCHEMA_VERSION")


def differences(pin: Pin, pinned: Side, main: Side) -> list[str]:
    """Each thing that differs, and after it the rule that applies to it."""
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
    if main.pyarrow != pinned.pyarrow:
        moved.append(
            f"pyarrow in marketlake's {LOCK} is {main.pyarrow} on main and {pinned.pyarrow} "
            "at the pin."
        )
    if moved:
        said += [*moved, RAISE_TOGETHER if higher else WAIT]
    if pin.pyarrow != pinned.pyarrow:
        said.append(
            f"pyarrow is {pin.pyarrow} in this repository's {LOCK} and {pinned.pyarrow} in "
            "marketlake's at the pin."
        )
        said.append(MATCH_PIN)
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
            # `file` is for the tests. ssh would read the machine's own keys.
            "GIT_ALLOW_PROTOCOL": "https:file",
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
        self._git("fetch", "--quiet", "--depth=1", "--no-tags", "--", self.remote, ref)
        return self._git("rev-parse", "FETCH_HEAD^{commit}").decode().strip()

    def file(self, commit: str, path: str) -> bytes | None:
        """The file's bytes at a fetched commit, or `None` where the commit holds none."""
        blob = f"{commit}:{path}"
        if not self._git("ls-tree", "--name-only", commit, "--", path).strip():
            return None
        if int(self._git("cat-file", "-s", blob)) > FILE_BYTES:
            raise Unreadable(f"{path} at {commit[:12]} is past {FILE_BYTES} bytes")
        return self._git("cat-file", "blob", blob)

    def side(self, commit: str) -> Side:
        journal, locked = self.file(commit, JOURNAL), self.file(commit, LOCK)
        for path, held in ((JOURNAL, journal), (LOCK, locked)):
            if held is None:
                raise Unreadable(f"{path} is not at {commit[:12]}")
        return Side(
            schema_version(journal, f"{JOURNAL} at {commit[:12]}"),
            self.file(commit, JOIN),
            pyarrow_version(locked.decode(), f"marketlake's {LOCK} at {commit[:12]}"),
        )


def check(lock: Path, main_ref: str) -> list[str]:
    pin = read_pin(lock.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as directory:
        remote = Remote(Path(directory), pin.remote)
        pinned = remote.side(remote.fetch(pin.commit))
        if pinned.join is None:
            raise Unreadable(f"{JOIN} is not at the pin")
        commit = remote.fetch(main_ref)
        said = differences(pin, pinned, remote.side(commit))
    against = f"marketlake's main at {commit[:12]} against the pin at {pin.commit[:12]}"
    if said:
        return [f"Something differs: {against}.", *said, f"Read {CONTRACT}."]
    print(f"Nothing differs: {against}.")
    return []


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    parser.add_argument("--lock", type=Path, default=Path(LOCK))
    parser.add_argument("--main", default="refs/heads/main")
    args = parser.parse_args(argv)
    try:
        said = check(args.lock, args.main)
    except Unreadable as error:
        print(f"{UNREAD}: {error}")
        return 2
    except Exception as error:  # Any other failure is a side unread, so never a pass or a raise.
        print(f"{UNREAD}: {type(error).__name__}")
        return 2
    for line in said:
        print(f"- {line}")
    return 1 if said else 0


if __name__ == "__main__":
    sys.exit(main())
