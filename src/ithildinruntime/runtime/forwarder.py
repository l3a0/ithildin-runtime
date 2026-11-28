"""The forwarder on the host: each alarm line a runtime unit writes, paged to the push topic.

Every process but monitoring writes its alarm to the host's journal, as
`adapters/alerts.py` says, and holds no address of the push topic. The
forwarder runs once per host, beside the `OnFailure` handler, as an entry point
of the same image the workers run, so the line a release writes and the
forwarder that reads it ship together. #202 builds its unit.

1. **It reads the host's journal from where it left off.** It keeps the cursor
   of the last entry it handled in a file in memory-backed storage, which
   outlives a restart of its unit, so lines written while it was down are read
   when it starts. A reboot empties that storage, and it then reads from the
   start of the boot.
2. **It trusts the unit the journal records, not the line.** It forwards only
   entries whose `_SYSTEMD_UNIT`, which journald sets from the writer's cgroup
   and no process can choose, is one of the runtime's units, so a line another
   process writes cannot page. Its own unit is not among them, so its own
   output never feeds back into it. The page names the component by its unit.
3. **It pages each alarm through `adapters.ntfy.Pager`**, which pages a cause
   once and again at its interval. One forwarder serves every container of a
   host, so its collapse spans all of them.
4. **A line that starts `ALARM` and is not an alarm** pages as
   `FORWARDER_UNREAD`, naming the unit and none of the line, since the line
   is not of this release's form and could hold anything.
5. **A push the server refuses** writes `refused.json` beside the cursor,
   naming the cause, the time and the answer, which #258's monitoring reads
   before each dead-man ping. The line counts as handled, and the cause's next
   alarm tries again at its interval.
6. **Its count of pages a day** is kept beside the cursor too, so a restart of
   its unit does not start the day's count again.
7. **It beats while it runs.** It touches its heartbeat file every
   `BEAT_EVERY` seconds, as each worker does, so the host's watchdog, which
   #202 points at it, sees a forwarder that stopped. A forwarder that runs and
   hears nothing is not told apart from a quiet host, and #260's drills fire a
   line through it.

The marker of a refused push holds the latest refusal and is never removed:
monitoring compares its time with that of its own last ping. Every file the
forwarder writes replaces the old one whole, and a planted link in the state
directory is refused rather than followed.

The forwarder needs three things of #202, which `07-deployment.md`'s
`## Where it runs` states.

1. Each runtime unit is named `ithildin-<component>[@<instance>].service`,
   which `RUNTIME_UNIT` reads.
2. Each container's standard error reaches the journal under its own unit, as
   a container run attached in its unit's process does, and not under the
   Docker daemon's, as Docker's own journald log driver would write it.
3. A journal it reads from a mounted directory is mounted read-only.

A journal that ends, or an entry it cannot read, ends the process with an
error. Its unit restarts it from the last cursor, and its own `OnFailure`
handler, which #202 builds, pages where the restarts run out.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from ithildinruntime.adapters.ntfy import DayCount, Pager, Refusal, Sent
from ithildinruntime.core import Alarm, AlarmUnread

from .alarms import FORWARDER_UNREAD

__all__ = [
    "BEAT_EVERY",
    "RUNTIME_UNIT",
    "Entry",
    "FileDayCount",
    "Forwarder",
    "ForwarderState",
    "Journalctl",
    "JournalEnded",
    "entry_of",
    "runtime_component",
]

# Seconds between the forwarder's touches of its heartbeat file.
BEAT_EVERY = 10.0

# The runtime's units, one per component and an instance after an `@`. The forwarder's own,
# `ithildin-forwarder.service`, is left out.
RUNTIME_UNIT = re.compile(
    r"ithildin-(supervisor|data|alpha|cost|risk|portfolio|execution|monitoring)"
    r"(?:@[A-Za-z0-9_.:-]+)?\.service"
)


class JournalEnded(Exception):
    """The journal reader stopped, or gave an entry that is not one."""


@dataclass(frozen=True)
class Entry:
    """One entry of the host's journal, as much of it as the forwarder reads."""

    cursor: str
    unit: str | None
    message: str | None
    at: datetime


def entry_of(record: object) -> Entry:
    """An entry from one object of `journalctl --output=json`, or `JournalEnded`."""
    if not isinstance(record, dict) or not isinstance(record.get("__CURSOR"), str):
        raise JournalEnded("the journal gave an entry with no cursor")
    unit = record.get("_SYSTEMD_UNIT")
    message = record.get("MESSAGE")
    if isinstance(message, list):
        # journalctl gives a message that is not UTF-8 as its bytes.
        try:
            message = bytes(message).decode(errors="replace")
        except (TypeError, ValueError):
            message = None
    stamp = record.get("__REALTIME_TIMESTAMP")
    try:
        at = datetime.fromtimestamp(int(stamp) / 1_000_000, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        raise JournalEnded("the journal gave an entry with no time") from None
    return Entry(
        record["__CURSOR"],
        unit if isinstance(unit, str) else None,
        message if isinstance(message, str) else None,
        at,
    )


def runtime_component(unit: str | None) -> str | None:
    """The component a runtime unit runs, or `None` for any other unit."""
    found = RUNTIME_UNIT.fullmatch(unit) if unit is not None else None
    return found.group(1) if found else None


class Journalctl:
    """The host's journal, read and followed through `journalctl` after a cursor."""

    def __init__(self, command: Sequence[str] = ("journalctl",), directory: Path | None = None):
        self._command = tuple(command)
        self._directory = directory

    def arguments(self, after: str | None) -> list[str]:
        args = [
            *self._command,
            "--output=json",
            "--follow",
            "--no-tail",
            "--no-pager",
            "--boot",
            "--unit=ithildin-*",
        ]
        if self._directory is not None:
            args.append(f"--directory={self._directory}")
        if after is not None:
            args.append(f"--after-cursor={after}")
        return args

    def entries(self, after: str | None) -> Iterator[Entry]:
        with subprocess.Popen(self.arguments(after), stdout=subprocess.PIPE) as reader:
            assert reader.stdout is not None
            for raw in reader.stdout:
                try:
                    record = json.loads(raw)
                except ValueError:
                    raise JournalEnded("journalctl gave a line that is not JSON") from None
                yield entry_of(record)
            code = reader.wait()
        raise JournalEnded(f"journalctl ended with exit code {code}")


def _write(path: Path, text: str) -> None:
    """Replace `path` with `text` whole, so a crash leaves the old file or the new one.

    The part file is made afresh, owner-only, and never through a link planted in its place.
    """
    part = path.with_name(f".{path.name}.part")
    part.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with open(part, "w", opener=lambda p, _: os.open(p, flags, 0o600)) as file:
        file.write(text)
    os.replace(part, path)


class ForwarderState:
    """The forwarder's files in memory-backed storage: its cursor, its count and its marker."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._cursor = directory / "cursor"
        self.refused_file = directory / "refused.json"
        self.count_file = directory / "pages.json"

    def cursor(self) -> str | None:
        try:
            text = self._cursor.read_text().strip()
        except FileNotFoundError:
            return None
        return text or None

    def save_cursor(self, cursor: str) -> None:
        _write(self._cursor, cursor)

    def mark_refused(self, refusal: Refusal) -> None:
        body = {"cause": refusal.cause, "at": refusal.at.isoformat(), "answer": refusal.answer}
        _write(self.refused_file, json.dumps(body, sort_keys=True))


class FileDayCount(DayCount):
    """A `DayCount` kept in a file, so a restart of the forwarder keeps the day's count."""

    def __init__(self, path: Path) -> None:
        self._path = path
        try:
            held = json.loads(path.read_text())
            super().__init__(date.fromisoformat(held["day"]), int(held["pages"]))
        except (OSError, ValueError, KeyError, TypeError):
            # A count that is absent or cannot be read starts at zero, which undercounts rather
            # than stops the forwarder.
            super().__init__()

    def add(self, day: date) -> int:
        pages = super().add(day)
        _write(self._path, json.dumps({"day": day.isoformat(), "pages": pages}))
        return pages


class Forwarder:
    """Reads the runtime's alarm lines from the journal and offers each to the pager."""

    def __init__(
        self,
        *,
        journal: Callable[[str | None], Iterable[Entry]],
        pager: Pager,
        state: ForwarderState,
        beat: Callable[[], None] = lambda: None,
        beat_every: float = BEAT_EVERY,
    ) -> None:
        self._journal = journal
        self._pager = pager
        self._state = state
        self._beat = beat
        self._beat_every = beat_every

    def run(self) -> None:
        """Handle each entry after the saved cursor, saving the cursor after each one.

        It beats at once and then every `beat_every` seconds until the journal ends.
        """
        stop = threading.Event()

        def beat() -> None:
            try:
                self._beat()
            except OSError:
                # A heartbeat file that cannot be touched reads as stopped, which pages.
                pass

        def beating() -> None:
            while not stop.wait(self._beat_every):
                beat()

        beat()

        beater = threading.Thread(target=beating, name="forwarder-beat", daemon=True)
        beater.start()
        try:
            for entry in self._journal(self._state.cursor()):
                self.handle(entry)
                self._state.save_cursor(entry.cursor)
        finally:
            stop.set()

    def handle(self, entry: Entry) -> Sent | None:
        """Page one entry where it is a runtime unit's alarm, and say what became of it."""
        component = runtime_component(entry.unit)
        if component is None or entry.message is None or not entry.message.startswith("ALARM "):
            return None
        try:
            _, alarm = Alarm.from_line(entry.message)
        except AlarmUnread:
            alarm = FORWARDER_UNREAD(
                f"a line of {entry.unit} starts ALARM and is not an alarm of this release's "
                "form, so the page shows none of it. Read that unit's journal at "
                f"{entry.at:%Y-%m-%dT%H:%M:%S}Z",
                scope=str(entry.unit),
            )
        return self._pager.offer(component, alarm, entry.at)
