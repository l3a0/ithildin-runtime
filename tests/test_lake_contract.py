"""What the runtime relies on from marketlake, at the commit pyproject.toml pins.

Acceptance tests 22 and 23 on #5, and the contract tests of #131. A raise of
the pin that changes a name here fails here first.

marketlake's README publishes `lake.unsealed` in its `Read an unsealed day`,
and of the rest only `is_reader_file` and `latest_entries`, as names a consumer
imports. So every other name is relied on by its docstring at the pinned
commit, and a test here exercises each over the built lake.

1. `lake.loader.load_bars`, its `LoadError` family and the columns it returns.
2. `lake.paths.is_reader_file`, and `lake.paths.parse_bars_rel`, which inverts
   `LakePaths.bars_partition_path`.
3. `lake.manifest.latest_entries`, for each file's SHA-256.
4. `lake.bar_import.import_vintages`, in tests only, which built the lake.
5. `lake.calendar`, which must agree with `core.sessions` on every session.
6. `lake.unsealed`, the reader of a day before its seal: `load_unsealed_quotes`,
   `segment_prefix`, `captured_by`, the five exceptions with their attributes,
   and the 21 columns the README publishes, at their types.
7. `lake.journal`'s `SCHEMA_VERSION`, the newest the pinned reader reads.
8. `lake.journal`'s `schema_for`, `segment_path`, `SegmentWriter`,
   `QUOTES_SURFACE` and the two row kinds, in tests only, which build a segment.
9. pyarrow's version, the one marketlake's `uv.lock` holds at the pinned commit.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from lake import journal
from lake.battery import build_entry
from lake.calendar import ExchangeCalendar
from lake.loader import BarsAbsent, LoadError, load_bars
from lake.manifest import append_quarantine, latest_entries
from lake.paths import LakePaths, is_reader_file, parse_bars_rel
from lake.schema_versions import (
    RecordedVersion,
    SchemaVersionLedger,
    ledger_path,
    running_fingerprints,
)
from lake.unsealed import (
    SchemaVersionUnknown,
    SegmentAbsent,
    SegmentMismatch,
    UnsealedArgument,
    UnsealedRefused,
    captured_by,
    load_unsealed_quotes,
    segment_prefix,
)

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


def test_23_parse_bars_rel_reads_back_every_partition_the_lake_lists(built_lake: Path) -> None:
    bars = [key for key in latest_entries(built_lake) if key.startswith("bars/")]
    assert bars
    paths = LakePaths(built_lake)
    for key in bars:
        read = parse_bars_rel(key)
        assert read is not None, key
        assert read.freq == "1d"
        assert paths.bars_partition_path(read.ticker, read.freq, read.day) == built_lake / key
    assert parse_bars_rel("bars/ticker=GLD/freq=1d/date=2006-04-26") is None


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


# The reader of a day before its seal, over segments marketlake's own writer wrote.

DAY = date(2026, 11, 24)
MINUTES = [datetime(2026, 11, 24, 20, minute, tzinfo=UTC) for minute in (37, 38, 39)]
LATE = datetime(2026, 11, 24, 20, 40, tzinfo=UTC)
SECOND = timedelta(seconds=1)
# The README's table of published columns, at the Arrow types it pins.
PUBLISHED = {
    "snap_ts": pa.string(),
    "fetch_ts": pa.string(),
    "fetch_end_ts": pa.string(),
    "vendor_quote_ts": pa.string(),
    "bid_time": pa.int64(),
    "ask_time": pa.int64(),
    "trade_time": pa.int64(),
    "ticker": pa.string(),
    "bid": pa.float64(),
    "ask": pa.float64(),
    "last": pa.float64(),
    "bid_size": pa.int64(),
    "ask_size": pa.int64(),
    "last_size": pa.int64(),
    "total_volume": pa.int64(),
    "security_status": pa.string(),
    "realtime": pa.bool_(),
    "close_tag": pa.string(),
    "session_phase": pa.string(),
    "row_kind": pa.string(),
    "schema_version": pa.int64(),
}


def _day(root: Path) -> dict[str, str]:
    """GLD's segments of three minutes, the middle one a gap, by path and SHA-256."""
    first, gap, last = MINUTES
    return dict(
        [
            lakes.quotes_segment(root, "GLD", first, lakes.quote_row("GLD", first, bid=60.0)),
            lakes.quotes_segment(root, "GLD", gap, lakes.gap_row("GLD", gap)),
            lakes.quotes_segment(root, "GLD", last, lakes.quote_row("GLD", last, bid=61.0)),
        ]
    )


def _read(root: Path, segments: dict[str, str], at: datetime = LATE) -> pa.Table:
    return load_unsealed_quotes("GLD", DAY, at, segments=segments, lake_root=root)


def test_unsealed_the_door_returns_the_data_rows_landed_by_the_instant(tmp_path: Path) -> None:
    segments = _day(tmp_path)
    landed = MINUTES[2] + SECOND
    assert _read(tmp_path, segments, landed).column("bid").to_pylist() == [60.0, 61.0]
    earlier = _read(tmp_path, segments, landed - timedelta(microseconds=1))
    assert earlier.column("bid").to_pylist() == [60.0]
    assert earlier.column("fetch_end_ts").to_pylist() == [(MINUTES[0] + SECOND).isoformat()]


def test_unsealed_the_door_returns_an_empty_table_with_its_columns(tmp_path: Path) -> None:
    table = _read(tmp_path, _day(tmp_path), MINUTES[0])
    assert table.num_rows == 0
    assert set(PUBLISHED) <= set(table.column_names)


def test_unsealed_the_door_reads_only_the_segments_named(tmp_path: Path) -> None:
    rel, digest = list(_day(tmp_path).items())[0]
    assert _read(tmp_path, {rel: digest}).column("bid").to_pylist() == [60.0]


def test_unsealed_every_segment_the_writer_names_sits_under_its_own_tickers_prefix(
    tmp_path: Path,
) -> None:
    prefix = segment_prefix("GLD", DAY)
    assert prefix == "journal/date=2026-11-24/surface=quotes/ticker=GLD/"
    assert prefix == segment_prefix("GLD", "2026-11-24")
    rels = list(_day(tmp_path))
    assert all(rel.startswith(prefix) for rel in rels)
    # Paths under one prefix sort by the cycle's start, which the walk back rests on.
    assert rels == sorted(rels)
    other, _ = lakes.quotes_segment(tmp_path, "GLDM", MINUTES[0], lakes.gap_row("GLDM", MINUTES[0]))
    assert not other.startswith(prefix)


def test_unsealed_captured_by_makes_the_doors_own_cut(tmp_path: Path) -> None:
    landed = MINUTES[2] + SECOND
    table = pa.Table.from_pylist(
        [
            lakes.quote_row("GLD", MINUTES[2], bid=61.0),
            lakes.gap_row("GLD", MINUTES[1]),
            lakes.quote_row("GLD", MINUTES[0], bid=60.0),
        ],
        schema=journal.schema_for(journal.QUOTES_SURFACE),
    )
    assert captured_by(table, landed).column("bid").to_pylist() == [61.0, 60.0]
    assert captured_by(table, landed - timedelta(microseconds=1)).column("bid").to_pylist() == [
        60.0
    ]
    with pytest.raises(UnsealedArgument):
        captured_by(table, landed.replace(tzinfo=None))
    with pytest.raises(UnsealedRefused):
        captured_by(table.drop_columns(["fetch_end_ts"]), landed)


@pytest.mark.parametrize("name", sorted(PUBLISHED))
def test_unsealed_each_published_column_holds_its_type(tmp_path: Path, name: str) -> None:
    """The door checks the type of six columns. The rest #100 reads by these names and types."""
    pinned = journal.schema_for(journal.QUOTES_SURFACE).field(name)
    assert pinned.type == PUBLISHED[name]
    assert _read(tmp_path, _day(tmp_path)).schema.field(name).type == PUBLISHED[name]


def test_unsealed_the_refusals_are_one_family_under_load_error() -> None:
    assert issubclass(UnsealedRefused, LoadError)
    assert issubclass(UnsealedArgument, ValueError)
    for refusal in (SegmentAbsent, SegmentMismatch, SchemaVersionUnknown, UnsealedArgument):
        assert issubclass(refusal, UnsealedRefused)


def test_unsealed_no_segment_named_is_absent_and_names_none(tmp_path: Path) -> None:
    with pytest.raises(SegmentAbsent, match="GLD 2026-11-24") as raised:
        _read(tmp_path, {})
    assert raised.value.segment is None


def test_unsealed_a_named_segment_not_under_the_root_is_absent(tmp_path: Path) -> None:
    segments = _day(tmp_path)
    rel = list(segments)[1]
    (tmp_path / rel).unlink()
    with pytest.raises(SegmentAbsent) as raised:
        _read(tmp_path, segments)
    assert raised.value.segment == rel


def test_unsealed_changed_bytes_are_a_mismatch_with_both_digests(tmp_path: Path) -> None:
    segments = _day(tmp_path)
    rel = list(segments)[2]
    path = tmp_path / rel
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(SegmentMismatch) as raised:
        _read(tmp_path, segments)
    assert raised.value.segment == rel
    assert raised.value.expected == segments[rel]
    assert raised.value.actual == hashlib.sha256(path.read_bytes()).hexdigest()


def test_unsealed_a_version_past_the_pins_names_itself(tmp_path: Path) -> None:
    newer = journal.SCHEMA_VERSION + 1
    rel, digest = lakes.quotes_segment(
        tmp_path,
        "GLD",
        MINUTES[0],
        lakes.quote_row("GLD", MINUTES[0], schema_version=newer),
        lakes.quote_row("GLD", MINUTES[0], schema_version=None),
    )
    with pytest.raises(SchemaVersionUnknown) as raised:
        _read(tmp_path, {rel: digest})
    assert raised.value.segment == rel
    assert raised.value.versions == (None, newer)


def test_unsealed_an_older_row_reads_with_its_ledger_and_is_refused_without(
    tmp_path: Path,
) -> None:
    """A newer pin reads the older rows, which is what lets a raise go first."""
    older = journal.SCHEMA_VERSION - 1
    row = lakes.quote_row("GLD", MINUTES[0], schema_version=older)
    rel, digest = lakes.quotes_segment(tmp_path, "GLD", MINUTES[0], row)
    with pytest.raises(UnsealedRefused) as raised:
        _read(tmp_path, {rel: digest})
    assert raised.value.segment == rel
    recorded = RecordedVersion(older, datetime(2026, 8, 1, tzinfo=UTC), running_fingerprints())
    SchemaVersionLedger([recorded]).write(ledger_path(tmp_path))
    assert ledger_path("").as_posix() == "reference/schema_versions.parquet"
    assert _read(tmp_path, {rel: digest}).column("bid").to_pylist() == [60.0]


@pytest.mark.parametrize(
    "bad", ["ticker", "day", "instant", "path", "digest"], ids=lambda bad: f"bad {bad}"
)
def test_unsealed_a_bad_argument_is_refused_as_a_value_error(tmp_path: Path, bad: str) -> None:
    segments = _day(tmp_path)
    rel = list(segments)[0]
    ticker, day, at = "GLD", "2026-11-24", LATE
    if bad == "ticker":
        ticker = "GLD/"
    elif bad == "day":
        day = "2026-11-31"
    elif bad == "instant":
        at = LATE.replace(tzinfo=None)
    elif bad == "path":
        segments = {rel.replace("GLD", "USO"): segments[rel]}
    else:
        segments = {rel: segments[rel].upper()}
    with pytest.raises(UnsealedArgument) as raised:
        load_unsealed_quotes(ticker, day, at, segments=segments, lake_root=tmp_path)
    assert isinstance(raised.value, ValueError)
    assert raised.value.segment is None


def test_unsealed_a_torn_segment_is_refused_by_name(tmp_path: Path) -> None:
    rel = list(_day(tmp_path))[0]
    path = tmp_path / rel
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(UnsealedRefused) as raised:
        _read(tmp_path, {rel: hashlib.sha256(path.read_bytes()).hexdigest()})
    assert type(raised.value) is UnsealedRefused
    assert raised.value.segment == rel


def test_unsealed_a_withheld_day_is_refused_from_the_roots_own_quarantine(tmp_path: Path) -> None:
    segments = _day(tmp_path)
    append_quarantine(
        tmp_path,
        build_entry(
            partition="quotes/ticker=GLD/date=2026-11-24.parquet",
            verdict="quarantined",
            check="planted",
            observed_at=LATE,
        ),
    )
    with pytest.raises(UnsealedRefused, match="planted") as raised:
        _read(tmp_path, segments)
    assert raised.value.segment is None


def test_unsealed_an_unreadable_segment_escapes_as_its_own_oserror(tmp_path: Path) -> None:
    segments = _day(tmp_path)
    path = tmp_path / list(segments)[0]
    path.unlink()
    path.mkdir()
    with pytest.raises(IsADirectoryError):
        _read(tmp_path, segments)


def test_unsealed_the_pinned_reader_reads_up_to_version_2() -> None:
    """A raise that moves this is the one a higher version on marketlake's main asks for."""
    assert journal.SCHEMA_VERSION == 2


def test_unsealed_pyarrow_is_the_version_marketlakes_lock_holds_at_the_pin() -> None:
    """The rows equal the seal's only while the two match, so it moves with the pin."""
    assert pa.__version__ == "25.0.1"
