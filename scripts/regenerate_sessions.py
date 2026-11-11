"""Regenerate the committed table of New York Stock Exchange sessions.

`core` imports no calendar library, because `exchange_calendars` pulls pandas
and numpy into every worker. So this script, run by hand, reads the sessions
from `exchange_calendars` at a pinned version and writes them to a committed
file that `ithildinruntime.core.sessions` reads and checks by its SHA-256.
docs/architecture/08-crosscutting-concepts.md's `#### Which clock a schedule
follows, and how it becomes UTC` gives the reasons.

The table runs from 2006-01-01, before the first session of the span the
first strategy's backtest reads, through the end of the next calendar year.
Move `LAST` forward a year, and `FIRST` back for an older backtest, then run:

    uv run --no-project --python 3.12 --with exchange_calendars==4.13.2 \\
        python scripts/regenerate_sessions.py

It rewrites the table and the SHA-256 that `core/sessions.py` checks it by.
"""

from __future__ import annotations

import hashlib
import re
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars

VERSION = "4.13.2"
FIRST = date(2006, 1, 1)
LAST = date(2027, 12, 31)

CORE = Path(__file__).resolve().parent.parent / "src" / "ithildinruntime" / "core"
TABLE = CORE / "sessions-xnys.csv"
MODULE = CORE / "sessions.py"
NEW_YORK = ZoneInfo("America/New_York")


def table_text() -> str:
    calendar = exchange_calendars.get_calendar(
        "XNYS", start=FIRST.isoformat(), end=LAST.isoformat()
    )
    lines = [
        f"# The New York Stock Exchange's sessions, from exchange_calendars {VERSION}.",
        "# scripts/regenerate_sessions.py writes this file. Do not edit it by hand.",
        f"range,{FIRST.isoformat()},{LAST.isoformat()}",
        "session,open_utc,close_utc,utc_offset_minutes",
    ]
    for label in calendar.sessions:
        day = label.date()
        opened = calendar.session_open(label).to_pydatetime()
        closed = calendar.session_close(label).to_pydatetime()
        if opened.date() != day or closed.date() != day:
            raise SystemExit(f"{day}: the open or close falls on another UTC date")
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=NEW_YORK)
        offset = noon.utcoffset()
        assert offset is not None
        minutes = int(offset.total_seconds()) // 60
        lines.append(f"{day.isoformat()},{opened:%H:%M},{closed:%H:%M},{minutes}")
    return "\n".join(lines) + "\n"


def main() -> int:
    if exchange_calendars.__version__ != VERSION:
        print(f"needs exchange_calendars {VERSION}, found {exchange_calendars.__version__}")
        return 1
    data = table_text().encode("ascii")
    TABLE.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    source = MODULE.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'^TABLE_SHA256 = "[0-9a-f]{64}"$',
        f'TABLE_SHA256 = "{digest}"',
        source,
        flags=re.MULTILINE,
    )
    if count != 1:
        print("core/sessions.py has no single TABLE_SHA256 line to rewrite")
        return 1
    MODULE.write_text(updated, encoding="utf-8")
    print(f"wrote {TABLE.name}, {len(data)} bytes, SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
