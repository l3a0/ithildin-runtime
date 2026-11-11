"""The trading calendar, read as data, and the one calendar function.

docs/architecture/08-crosscutting-concepts.md's `#### Which clock a schedule
follows, and how it becomes UTC` gives the rules. `core` imports no calendar
library. The sessions and their closes come from `sessions-xnys.csv` beside
this module, which `scripts/regenerate_sessions.py` writes from
`exchange_calendars` at a pinned version, and which is checked here by its
SHA-256 before anything reads it.

Each row carries the exchange's offset from UTC on that day, so a time on the
exchange's clock becomes UTC without the host's time zone database. New York
changes its clocks at 02:00 on a Sunday, never on a session day.

Three things halt rather than guess, because a missing input stops trading
(hard limit 3):

1. A table whose bytes do not match the SHA-256 below.
2. A date outside the range the table names.
3. A date inside the range that the table holds no session for.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from pathlib import Path
from types import MappingProxyType

from .halt import Halt

__all__ = [
    "TABLE_SHA256",
    "BeforeClose",
    "ExchangeTime",
    "ScheduleTime",
    "Session",
    "SessionTable",
    "load_sessions",
    "parse_sessions",
    "to_utc",
]

TABLE = Path(__file__).with_name("sessions-xnys.csv")
TABLE_SHA256 = "bea308f0969a5511fb700a32736fa853d76eee91f8ff0f3f5ddf2a0ef4d0e9e7"
_HEADER = "session,open_utc,close_utc,utc_offset_minutes"
_RANGE = re.compile(r"range,([0-9]{4}-[0-9]{2}-[0-9]{2}),([0-9]{4}-[0-9]{2}-[0-9]{2})")
_ROW = re.compile(
    r"([0-9]{4}-[0-9]{2}-[0-9]{2}),([0-9]{2}:[0-9]{2}),([0-9]{2}:[0-9]{2}),(-?[0-9]+)"
)


@dataclass(frozen=True)
class ExchangeTime:
    """A time on the exchange's clock, such as 15:40 in New York."""

    local: time


@dataclass(frozen=True)
class BeforeClose:
    """A time stated as minutes before the session's close, such as close−20."""

    minutes: int


type ScheduleTime = ExchangeTime | BeforeClose


@dataclass(frozen=True)
class Session:
    day: date
    open: datetime
    close: datetime
    utc_offset: timedelta


@dataclass(frozen=True)
class SessionTable:
    """The sessions between `first` and `last`, the range the table covers."""

    first: date
    last: date
    sessions: Mapping[date, Session]

    def session(self, day: date) -> Session:
        if not self.first <= day <= self.last:
            raise Halt(
                f"{day.isoformat()} is outside the session table's range, "
                f"{self.first.isoformat()} to {self.last.isoformat()}"
            )
        found = self.sessions.get(day)
        if found is None:
            raise Halt(f"the session table holds no session on {day.isoformat()}")
        return found

    def between(self, first: date, last: date) -> tuple[Session, ...]:
        """Every session from `first` to `last`, both of which must be sessions."""
        self.session(first)
        self.session(last)
        return tuple(self.sessions[d] for d in sorted(self.sessions) if first <= d <= last)

    def regular_close(self) -> time:
        """The latest close on the exchange's clock the table holds, 16:00 in New York."""
        if not self.sessions:
            raise Halt("the session table holds no session")
        return max((s.close + s.utc_offset).time() for s in self.sessions.values())

    def sessions_after(self, day: date) -> int:
        """How many sessions the table holds after `day`."""
        return sum(1 for d in self.sessions if d > day)


def parse_sessions(data: bytes, sha256: str) -> SessionTable:
    """Read a session table, halting unless its bytes match `sha256`."""
    found = hashlib.sha256(data).hexdigest()
    if found != sha256:
        raise Halt(f"the session table's SHA-256 is {found}, not {sha256}")
    lines = [line for line in data.decode("ascii").splitlines() if not line.startswith("#")]
    try:
        bounds = _RANGE.fullmatch(lines[0])
        if bounds is None or lines[1] != _HEADER:
            raise ValueError("the table's first two lines are not its range and header")
        first, last = bounds.groups()
        table = SessionTable(date.fromisoformat(first), date.fromisoformat(last), {})
        sessions: dict[date, Session] = {}
        for line in lines[2:]:
            row = _ROW.fullmatch(line)
            if row is None:
                raise ValueError(f"the row {line!r} is not a session")
            day_text, opened, closed, minutes = row.groups()
            day = date.fromisoformat(day_text)
            if day in sessions or not table.first <= day <= table.last:
                raise ValueError(f"{day} is repeated or outside the range")
            session = Session(
                day=day,
                open=datetime.combine(day, time.fromisoformat(opened), UTC),
                close=datetime.combine(day, time.fromisoformat(closed), UTC),
                utc_offset=timedelta(minutes=int(minutes)),
            )
            if session.close <= session.open:
                raise ValueError(f"{day} closes before it opens")
            sessions[day] = session
    except (ValueError, IndexError) as error:
        raise Halt(f"the session table cannot be read: {error}") from error
    return SessionTable(table.first, table.last, MappingProxyType(sessions))


@cache
def load_sessions() -> SessionTable:
    """The committed session table, checked by its SHA-256."""
    return parse_sessions(TABLE.read_bytes(), TABLE_SHA256)


def to_utc(day: date, when: ScheduleTime, table: SessionTable | None = None) -> datetime:
    """Turn a schedule's time on one session into a UTC instant.

    A time on the exchange's clock is read at that day's offset from UTC. A
    time stated as minutes before the close is counted back from that
    session's own close, so it moves with an early close.
    """
    session = (table or load_sessions()).session(day)
    if isinstance(when, BeforeClose):
        minutes = when.minutes
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes < 0:
            raise ValueError(f"{when!r} counts minutes before the close in a whole number")
        return session.close - timedelta(minutes=when.minutes)
    if isinstance(when, ExchangeTime):
        local = datetime.combine(day, when.local)
        return (local - session.utc_offset).replace(tzinfo=UTC)
    raise TypeError(f"{when!r} is not a schedule time")
