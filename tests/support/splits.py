"""Made-up records of splits for tests, and the read of one that holds no split.

None of these rows is the tracked file's. A test that decides on captured bars
past the tracked file's covered rows hands `LakeSource` one of these instead.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, date, datetime

from ithildinruntime.core import InstrumentId, SplitRow, SplitsRead, load_sessions
from ithildinruntime.data.splits import SplitRecord

DAYS = sorted(load_sessions().sessions)
ADDED = date(2026, 11, 27)
SHA = hashlib.sha256(b"a made-up record of splits").hexdigest()

# What a snapshot holds where its read checked no pair and knew no split.
NO_SPLITS = SplitsRead(sha256=SHA, rows={}, covered_from={}, pairs=())


def covered(
    instrument: str,
    first: date = DAYS[0],
    last: date = DAYS[-1],
    *,
    added: date = ADDED,
    line: int = 1,
) -> SplitRow:
    """A covered row of one instrument, over the whole calendar unless the span is named."""
    return SplitRow(
        added=added,
        kind="covered",
        instrument=InstrumentId(instrument),
        first_session=first,
        last_session=last,
        new_shares=None,
        old_shares=None,
        known_at=None,
        read_from="made up",
        line=line,
    )


def split(
    instrument: str,
    first: date,
    new: int,
    old: int,
    *,
    known_at: datetime | None = None,
    added: date = ADDED,
    line: int = 1,
) -> SplitRow:
    """A split row, knowable at the open of the session before its first unless named."""
    sessions = load_sessions()
    return SplitRow(
        added=added,
        kind="split",
        instrument=InstrumentId(instrument),
        first_session=first,
        last_session=None,
        new_shares=new,
        old_shares=old,
        known_at=known_at or sessions.before(first).open.astimezone(UTC),
        read_from="made up",
        line=line,
    )


def withdrawn(instrument: str, first: date, *, added: date, line: int = 1) -> SplitRow:
    return SplitRow(
        added=added,
        kind="withdrawn",
        instrument=InstrumentId(instrument),
        first_session=first,
        last_session=None,
        new_shares=None,
        old_shares=None,
        known_at=None,
        read_from="made up",
        line=line,
    )


def record(*rows: SplitRow) -> SplitRecord:
    return SplitRecord(tuple(rows), SHA)


def all_covered(instruments: Iterable[str]) -> SplitRecord:
    """A record whose covered rows hold every session of each instrument, with no split."""
    return record(*(covered(i, line=n) for n, i in enumerate(instruments, 1)))
