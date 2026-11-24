"""The trading calendar, read as data, and the functions that read it.

docs/architecture/08-crosscutting-concepts.md's `#### Which clock a schedule
follows, and how it becomes UTC` gives the rules. `core` imports no calendar
library. The sessions and their closes come from `sessions-xnys.csv` beside
this module, which `scripts/regenerate_sessions.py` writes from
`exchange_calendars` at a pinned version, and which is checked here by its
SHA-256 before anything reads it.

Each row carries the exchange's offset from UTC on that day, so a time on the
exchange's clock becomes UTC without the host's time zone database. New York
changes its clocks at 02:00 on a Sunday, never on a session day.

Six things halt rather than guess, because a missing input stops trading
(hard limit 3):

1. A table whose bytes do not match the SHA-256 below.
2. A date outside the range the table names.
3. A date inside the range that the table holds no session for.
4. A time before the open of the table's first session, when asked which
   session it belongs to.
5. The table's first session, when asked which session came before it.
6. A count of minutes before the close that reaches back past any date.
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
    "require_utc",
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

    def before(self, day: date) -> Session:
        """The session before `day`, which must itself be a session."""
        self.session(day)
        earlier = [d for d in self.sessions if d < day]
        if not earlier:
            raise Halt(f"the session table holds no session before {day.isoformat()}")
        return self.sessions[max(earlier)]

    def opened_by(self, at: datetime) -> Session:
        """The latest session whose open is at or before `at`, which is a UTC time.

        It is the session a decision belongs to. A time whose date is outside
        the table's range halts, and so does one before the table's first open.
        """
        if at.utcoffset() != timedelta(0):
            raise ValueError(f"{at!r} is not a UTC time")
        day = at.date()
        if not self.first <= day <= self.last:
            raise Halt(
                f"{at.isoformat()} is outside the session table's range, "
                f"{self.first.isoformat()} to {self.last.isoformat()}"
            )
        while day >= self.first:
            found = self.sessions.get(day)
            if found is not None and found.open <= at:
                return found
            day -= timedelta(days=1)
        raise Halt(f"the session table holds no session that opened by {at.isoformat()}")

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


def require_utc(what: str, at: object) -> datetime:
    """`at`, which halts naming `what` unless it is a UTC time."""
    if not isinstance(at, datetime) or at.utcoffset() != timedelta(0):
        raise Halt(f"{what} is {at!r}, not a UTC time")
    return at


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
        try:
            return session.close - timedelta(minutes=when.minutes)
        except OverflowError as error:
            raise Halt(f"{when.minutes} minutes before the close is no time at all") from error
    if isinstance(when, ExchangeTime):
        local = datetime.combine(day, when.local)
        return (local - session.utc_offset).replace(tzinfo=UTC)
    raise TypeError(f"{when!r} is not a schedule time")
