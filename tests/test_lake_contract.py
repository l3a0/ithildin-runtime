"""What the runtime relies on from marketlake, at the commit pyproject.toml pins.

Acceptance tests 22 and 23 on #5. marketlake's README publishes none of these
names, so each is relied on by its docstring at the pinned commit, and a test
here exercises each over the built lake. A raise of the pin that changes one
fails here first.

1. `lake.loader.load_bars`, its `LoadError` family and the columns it returns.
2. `lake.paths.is_reader_file`.
3. `lake.manifest.latest_entries`, for each file's SHA-256.
4. `lake.bar_import.import_vintages`, in tests only, which built the lake.
5. `lake.calendar`, which must agree with `core.sessions` on every session.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, timedelta
from pathlib import Path

import pytest
from lake.calendar import ExchangeCalendar
from lake.loader import BarsAbsent, LoadError, load_bars
from lake.manifest import latest_entries
from lake.paths import is_reader_file

from ithildinruntime.core import load_sessions
from tests.support import lake as lakes

COLUMNS = {
    "bar_ts",
    "fetch_ts",
    "fetch_end_ts",
    "ticker",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "origin",
    "basis",
    "source",
    "source_as_of",
}


def test_22_the_two_calendars_agree_on_every_session() -> None:
    table = load_sessions()
    calendar = ExchangeCalendar(start=table.first)
    last = min(table.last, calendar._cal.last_session.date())
    # exchange_calendars ends its calendar about a year after today, so the
    # overlap reaches past today and covers every session a run can read.
    assert last >= date.today()
    day = calendar._cal.first_session.date()
    assert day <= date(2006, 4, 26)
    checked = 0
    while day <= last:
        held = day in table.sessions
        assert held == calendar.is_session(day), day
        if held:
            session = table.sessions[day]
            assert calendar.session_open(day).astimezone(UTC) == session.open, day
            assert calendar.session_close(day).astimezone(UTC) == session.close, day
            checked += 1
        day += timedelta(days=1)
    assert checked > 5000


def test_23_load_bars_reads_one_session_and_names_where_each_row_came_from(
    built_lake: Path,
) -> None:
    table = load_bars(
        "GLD",
        "1d",
        start=lakes.LAST_SESSION,
        end=lakes.LAST_SESSION,
        adjust="none",
        lake_root=built_lake,
    )
    assert COLUMNS <= set(table.column_names)
    (row,) = table.to_pylist()
    assert (row["origin"], row["basis"], row["close"]) == ("imported", "adjusted", 159.37)
    assert row["source_as_of"] == "2012-04-10"


def test_23_load_bars_answers_a_session_with_no_partition_with_bars_absent(
    built_lake: Path,
) -> None:
    with pytest.raises(BarsAbsent) as absent:
        load_bars(
            "GLD",
            "1d",
            start=date(2012, 4, 6),
            end=date(2012, 4, 6),
            adjust="none",
            lake_root=built_lake,
        )
    assert isinstance(absent.value, LoadError)


def test_23_load_bars_returns_around_a_session_missing_inside_a_range(
    lake: Path,
) -> None:
    """Why the source reads one session per call: a range read does not say what it skipped."""
    from tests.test_data_source import before, unlist

    unlist(lake, lakes.partition_key("GLD", before(lakes.LAST_SESSION, 2)))
    table = load_bars(
        "GLD",
        "1d",
        start=before(lakes.LAST_SESSION, 4),
        end=lakes.LAST_SESSION,
        adjust="none",
        lake_root=lake,
    )
    assert table.num_rows == 4


@pytest.mark.parametrize(
    ("key", "reader"),
    [
        ("quarantine.jsonl", True),
        ("actions/corporate_actions.jsonl", True),
        ("reference/schema_versions.parquet", True),
        ("reference/security_master.parquet", True),
        ("manifest.jsonl", False),
        ("bars/ticker=GLD/freq=1d/date=2012-04-09.parquet", False),
    ],
)
def test_23_is_reader_file_names_the_ledgers_and_every_reference_file(
    key: str, reader: bool
) -> None:
    assert is_reader_file(key) is reader


def test_23_latest_entries_gives_each_files_sha256(built_lake: Path) -> None:
    entries = latest_entries(built_lake)
    for key in ("quarantine.jsonl", lakes.partition_key("USO", lakes.LAST_SESSION)):
        data = (built_lake / key).read_bytes()
        assert entries[key]["sha256"] == hashlib.sha256(data).hexdigest()
    assert sum(1 for k in entries if k.startswith("bars/")) == 3000


def test_23_latest_entries_drops_a_torn_last_line_and_still_answers(lake: Path) -> None:
    """Why the mirror refuses a manifest that does not end in a newline."""
    manifest = lake / "manifest.jsonl"
    before = latest_entries(lake)
    lakes.record(lake, lakes.partition_key("GLD", lakes.LAST_SESSION), rows=1)
    manifest.write_bytes(manifest.read_bytes()[:-20])
    assert latest_entries(lake) == before
