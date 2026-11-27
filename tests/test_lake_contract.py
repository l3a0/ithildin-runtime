"""What the runtime relies on from marketlake, at the commit pyproject.toml pins.

Acceptance tests 22 and 23 on #5, and the contract tests of #131. A raise of
the pin that changes a name here fails here first.

marketlake's README publishes `lake.unsealed` in its `Read an unsealed day`, and
there it names `lake.loader`'s `LoadError`, `is_reader_file`, `latest_entries` and
`lake.journal.SCHEMA_VERSION`. Every other name, `load_bars` among them, is relied
on by its docstring at the pinned commit. A test of a published name holds what
the README states, with a planted case or more of each check the reader makes of
a row, so a change it allows passes. One goes further and holds that a damaged
quarantine ledger is refused. marketlake's own suite covers the reader's checks
beyond these cases.

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
   `gap_batch`, `QUOTES_SURFACE` and the two row kinds, and `lake.gap.SEGMENT_STAMP_FORMAT`,
   in tests only, which build a segment. `lake.schema_versions` and
   `lake.battery.build_entry`, in tests only, build the two ledgers the reader reads.
9. `lake.loader.load_quotes`, `SnapAbsent` with its `unreadable`, `NoSpotClose`
   with its `tagged_gaps`, `PartitionQuarantined` and
   `LakePaths.quotes_partition_path`, which the read of a sealed day relies on
   by their docstrings, beside `captured_by`. l3a0/marketlake#1050 asks for
   `tagged_gaps` to be published, and for the rule that no row tagged
   `spot_close` is written before the equity close, which the read relies on
   from marketlake's code, read at `8565362` with its writers unchanged since
   the pin. A raise of the pin re-reads it, as
   docs/architecture/07-deployment.md's `#### What says the marketlake pin is
   due a raise` says, and `scripts/check_lake_pin.py` prints that step
   wherever it says to raise the pin or to wait for a deploy. A writer change
   deployed before a raise goes unseen until then.
10. pyarrow's version. The check of the pin, `scripts/check_lake_pin.py`, is what
   compares it with the one marketlake's `uv.lock` holds at the pinned commit.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pytest
from lake import journal
from lake.battery import build_entry
from lake.calendar import MARKET_TZ, ExchangeCalendar
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

DAY = "2026-11-24"
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
    assert sorted(_read(tmp_path, segments, landed).column("bid").to_pylist()) == [60.0, 61.0]
    earlier = _read(tmp_path, segments, landed - timedelta(microseconds=1))
    assert earlier.column("bid").to_pylist() == [60.0]
    assert earlier.column("fetch_end_ts").to_pylist() == [(MINUTES[0] + SECOND).isoformat()]


def test_unsealed_the_cut_compares_instants_and_never_text(tmp_path: Path) -> None:
    """This row lands a second after `at`, and its text sorts before `at`'s in UTC."""
    landed = MINUTES[2] + SECOND
    stamp = landed.astimezone(MARKET_TZ).isoformat()
    assert stamp < MINUTES[2].isoformat()
    row = lakes.quote_row("GLD", MINUTES[2], fetch_end_ts=stamp)
    rel, digest = lakes.quotes_segment(tmp_path, "GLD", MINUTES[2], row)
    assert _read(tmp_path, {rel: digest}, MINUTES[2]).num_rows == 0
    assert _read(tmp_path, {rel: digest}, landed).num_rows == 1


def test_unsealed_every_row_of_a_minute_is_returned(tmp_path: Path) -> None:
    rows = [
        lakes.quote_row("GLD", MINUTES[0], fetch_end_ts=(MINUTES[0] + wait * SECOND).isoformat())
        for wait in (2, 1)
    ]
    rel, digest = lakes.quotes_segment(tmp_path, "GLD", MINUTES[0], *rows)
    stamps = _read(tmp_path, {rel: digest}).column("fetch_end_ts").to_pylist()
    assert sorted(stamps) == sorted(row["fetch_end_ts"] for row in rows)


def test_unsealed_a_value_held_in_extra_is_never_read_as_null(tmp_path: Path) -> None:
    """A field the writer could not hold in its column sits in `extra`, its column null."""
    older = journal.SCHEMA_VERSION - 1
    shapes = running_fingerprints()
    del shapes[journal.QUOTES_SURFACE]["bid_size"]
    recorded = RecordedVersion(older, datetime(2026, 8, 1, tzinfo=UTC), shapes)
    SchemaVersionLedger([recorded]).write(ledger_path(tmp_path))
    schema = journal.schema_for(journal.QUOTES_SURFACE)
    held = {"extra": '{"quote": {"bidSize": 7}}'}
    lifted, digest = lakes.quotes_segment(
        tmp_path,
        "GLD",
        MINUTES[0],
        lakes.quote_row("GLD", MINUTES[0], schema_version=older, **held),
        schema=schema.remove(schema.get_field_index("bid_size")),
    )
    assert _read(tmp_path, {lifted: digest}).column("bid_size").to_pylist() == [7]
    row = lakes.quote_row("GLD", MINUTES[1], bid_size=None, **held)
    routed, digest = lakes.quotes_segment(tmp_path, "GLD", MINUTES[1], row)
    with pytest.raises(UnsealedRefused) as raised:
        _read(tmp_path, {routed: digest})
    assert raised.value.segment == routed


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
    assert all(rel.startswith(prefix) for rel in _day(tmp_path))
    other, _ = lakes.quotes_segment(tmp_path, "GLDM", MINUTES[0], lakes.gap_row("GLDM", MINUTES[0]))
    assert not other.startswith(prefix)


def test_unsealed_paths_under_one_prefix_sort_by_cycle_start(tmp_path: Path) -> None:
    """#100's walk from the newest segment back rests on it."""
    start = datetime(2026, 11, 24, 14, 58, 59, 999999, tzinfo=UTC)
    steps = [timedelta(microseconds=1), SECOND, 60 * SECOND, 3600 * SECOND]
    cycles = [start, *(start + step for step in steps)]
    rels = [
        lakes.quotes_segment(tmp_path, "GLD", cycle, lakes.quote_row("GLD", cycle))[0]
        for cycle in cycles
    ]
    assert rels == sorted(rels)


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
    assert sorted(captured_by(table, landed).column("bid").to_pylist()) == [60.0, 61.0]
    assert captured_by(table, landed - timedelta(microseconds=1)).column("bid").to_pylist() == [
        60.0
    ]
    with pytest.raises(UnsealedArgument):
        captured_by(table, landed.replace(tzinfo=None))
    for column in ("fetch_end_ts", "row_kind"):
        with pytest.raises(UnsealedRefused):
            captured_by(table.slice(0, 1).drop_columns([column]), landed)


@pytest.mark.parametrize(
    "changes",
    [
        {"row_kind": None},
        {"fetch_end_ts": None},
        {"fetch_end_ts": "nonsense"},
        {"fetch_end_ts": MINUTES[0].replace(tzinfo=None).isoformat()},
    ],
    ids=["no kind", "no stamp", "a stamp that is no time", "a stamp with no offset"],
)
def test_unsealed_a_row_that_cannot_be_cut_is_refused_and_never_dropped(
    tmp_path: Path, changes: dict[str, str | None]
) -> None:
    good = lakes.quote_row("GLD", MINUTES[0])
    bad = lakes.quote_row("GLD", MINUTES[0], **changes)
    schema = journal.schema_for(journal.QUOTES_SURFACE)
    with pytest.raises(UnsealedRefused):
        captured_by(pa.Table.from_pylist([good, bad], schema=schema), LATE)
    rel, digest = lakes.quotes_segment(tmp_path, "GLD", MINUTES[0], good, bad)
    with pytest.raises(UnsealedRefused) as raised:
        _read(tmp_path, {rel: digest})
    assert "GLD" in str(raised.value) and DAY in str(raised.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"ticker": "USO"},
        {"ticker": None},
        {"snap_ts": (MINUTES[0] - timedelta(days=1)).isoformat()},
        {"snap_ts": None},
        {"snap_ts": MINUTES[0].replace(tzinfo=None).isoformat()},
        {"schema_version": 0},
        {"schema_version": journal.SCHEMA_VERSION + 1},
    ],
    ids=lambda changes: "{} {}".format(*next(iter(changes.items()))),
)
def test_unsealed_a_row_of_another_ticker_day_or_version_is_refused(
    tmp_path: Path, changes: dict[str, str | int | None]
) -> None:
    """The planted row is second in the second of three segments, and lands after `at`."""
    good = [lakes.quote_row("GLD", minute) for minute in MINUTES]
    first, one = lakes.quotes_segment(tmp_path, "GLD", MINUTES[0], good[0])
    rel, two = lakes.quotes_segment(tmp_path, "GLD", MINUTES[1], good[1], {**good[1], **changes})
    last, three = lakes.quotes_segment(tmp_path, "GLD", MINUTES[2], good[2])
    with pytest.raises(UnsealedRefused) as raised:
        _read(tmp_path, {first: one, rel: two, last: three}, MINUTES[0] + SECOND)
    assert raised.value.segment == rel


@pytest.mark.parametrize(
    "name", ["schema_version", "row_kind", "fetch_end_ts", "snap_ts", "ticker", "extra"]
)
def test_unsealed_the_six_columns_the_door_checks_are_refused_at_another_type(
    tmp_path: Path, name: str
) -> None:
    schema = journal.schema_for(journal.QUOTES_SURFACE)
    place = schema.get_field_index(name)
    other = pa.int32() if schema.field(place).type == pa.int64() else pa.large_string()
    rel, digest = lakes.quotes_segment(
        tmp_path,
        "GLD",
        MINUTES[0],
        lakes.quote_row("GLD", MINUTES[0]),
        schema=schema.set(place, pa.field(name, other)),
    )
    with pytest.raises(UnsealedRefused) as raised:
        _read(tmp_path, {rel: digest})
    assert raised.value.segment == rel


def test_unsealed_a_bad_segment_after_the_instant_is_refused_too(tmp_path: Path) -> None:
    segments = _day(tmp_path)
    rel = list(segments)[2]
    (tmp_path / rel).write_bytes(b"not a segment")
    with pytest.raises(SegmentMismatch) as raised:
        _read(tmp_path, segments, MINUTES[0] + SECOND)
    assert raised.value.segment == rel


@pytest.mark.parametrize("name", sorted(PUBLISHED))
def test_unsealed_each_published_column_holds_its_type(tmp_path: Path, name: str) -> None:
    """#100 reads each by this name and type."""
    pinned = journal.schema_for(journal.QUOTES_SURFACE).field(name)
    assert pinned.type == PUBLISHED[name]
    assert _read(tmp_path, _day(tmp_path)).schema.field(name).type == PUBLISHED[name]


def test_unsealed_the_refusals_are_one_family_under_load_error() -> None:
    assert issubclass(UnsealedRefused, LoadError)
    assert issubclass(UnsealedArgument, ValueError)
    for refusal in (SegmentAbsent, SegmentMismatch, SchemaVersionUnknown, UnsealedArgument):
        assert issubclass(refusal, UnsealedRefused)


def test_unsealed_no_segment_named_is_absent_and_names_none(tmp_path: Path) -> None:
    with pytest.raises(SegmentAbsent) as raised:
        _read(tmp_path, {})
    assert raised.value.segment is None
    assert "GLD" in str(raised.value) and DAY in str(raised.value)


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
    ticker, day, at = "GLD", DAY, LATE
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


def test_unsealed_a_torn_segment_is_refused_by_name(tmp_path: Path) -> None:
    rel = list(_day(tmp_path))[0]
    path = tmp_path / rel
    path.write_bytes(path.read_bytes()[:-8])
    with pytest.raises(UnsealedRefused) as raised:
        _read(tmp_path, {rel: hashlib.sha256(path.read_bytes()).hexdigest()})
    named = (SegmentAbsent, SegmentMismatch, SchemaVersionUnknown, UnsealedArgument)
    assert not isinstance(raised.value, named)
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
    with pytest.raises(UnsealedRefused) as raised:
        _read(tmp_path, segments)
    assert "GLD" in str(raised.value) and DAY in str(raised.value)
    ledger = tmp_path / "quarantine.jsonl"
    ledger.write_text(f"not a verdict\n{ledger.read_text()}".replace("GLD", "USO"))
    with pytest.raises(UnsealedRefused):
        _read(tmp_path, segments)


def test_unsealed_an_unreadable_segment_escapes_as_its_own_oserror(tmp_path: Path) -> None:
    segments = _day(tmp_path)
    path = tmp_path / list(segments)[0]
    path.unlink()
    path.mkdir()
    with pytest.raises(OSError) as raised:
        _read(tmp_path, segments)
    assert not isinstance(raised.value, UnsealedRefused)


def test_unsealed_the_pinned_reader_reads_up_to_version_2() -> None:
    """A raise that moves this is the one a higher version on marketlake's main asks for."""
    assert journal.SCHEMA_VERSION == 2


def test_unsealed_the_installed_pyarrow_is_the_pinned_one() -> None:
    """marketlake's uv.lock holds it at 2d41793, the pinned commit, and the two move together."""
    assert pa.__version__ == "25.0.1"


# The sealed day, which #132's read relies on through `load_quotes`, `SnapAbsent`
# and `LakePaths.quotes_partition_path`, by their docstrings at the pinned commit.


def _sealed(lake: Path, *rows: dict[str, object]) -> str:
    """GLD's quotes partition for the support's session, listed in the manifest."""
    from tests.support.sealed import partition

    return partition(lake, "GLD", *rows)


def test_sealed_the_manifest_lists_a_quotes_partition_under_the_paths_key(lake: Path) -> None:
    from tests.support.segments import AT, MINUTE
    from tests.support.segments import DAY as SESSION_DAY

    key = _sealed(lake, lakes.quote_row("GLD", AT - MINUTE))
    root = Path("/lake")
    path = LakePaths(root).quotes_partition_path("GLD", SESSION_DAY)
    assert (
        key == path.relative_to(root).as_posix() == f"quotes/ticker=GLD/date={SESSION_DAY}.parquet"
    )
    assert key in latest_entries(lake)
    assert not is_reader_file(key)


def test_sealed_load_quotes_answers_a_minute_and_the_equity_close(lake: Path) -> None:
    from lake.loader import load_quotes

    from tests.support.sealed import close_row
    from tests.support.segments import AT, MINUTE, SESSION
    from tests.support.segments import DAY as SESSION_DAY

    _sealed(
        lake,
        lakes.quote_row("GLD", AT - 2 * MINUTE, bid=60.0),
        lakes.quote_row("GLD", AT - MINUTE, bid=61.0),
        close_row("GLD", SESSION.close),
    )
    minute = load_quotes("GLD", SESSION_DAY, "15:39", lake_root=lake)
    assert minute.column("bid").to_pylist() == [61.0]
    close = load_quotes("GLD", SESSION_DAY, lake_root=lake)
    assert close.column("close_tag").to_pylist() == ["spot_close"]
    for name, kind in PUBLISHED.items():
        assert minute.schema.field(name).type == kind, name


@pytest.mark.parametrize("unreadable", [False, True])
def test_sealed_a_minute_with_no_row_is_snap_absent(lake: Path, unreadable: bool) -> None:
    from lake.loader import SnapAbsent, load_quotes

    from tests.support.sealed import close_row
    from tests.support.segments import AT, MINUTE, SESSION
    from tests.support.segments import DAY as SESSION_DAY

    rows = [lakes.quote_row("GLD", AT - 2 * MINUTE), close_row("GLD", SESSION.close)]
    if unreadable:
        rows.append(lakes.quote_row("GLD", AT - 3 * MINUTE, snap_ts="never"))
    _sealed(lake, *rows)
    with pytest.raises(SnapAbsent) as raised:
        load_quotes("GLD", SESSION_DAY, "15:39", lake_root=lake)
    assert bool(raised.value.unreadable) is unreadable


@pytest.mark.parametrize(("close", "gaps"), [("none", 0), ("untagged", 0), ("tagged", 1)])
def test_sealed_a_partition_with_no_equity_close_raises_no_spot_close(
    lake: Path, close: str, gaps: int
) -> None:
    """`tagged_gaps` counts the gap rows tagged `spot_close`, as `NoCloseOfRecord` documents."""
    from lake.loader import NoSpotClose, load_quotes

    from tests.support.sealed import gap_close_row
    from tests.support.segments import AT, MINUTE, SESSION
    from tests.support.segments import DAY as SESSION_DAY

    rows = [lakes.quote_row("GLD", AT - MINUTE)]
    if close != "none":
        rows.append(gap_close_row("GLD", SESSION.close, tagged=close == "tagged"))
    _sealed(lake, *rows)
    with pytest.raises(NoSpotClose) as raised:
        load_quotes("GLD", SESSION_DAY, lake_root=lake)
    assert type(raised.value.tagged_gaps) is int and raised.value.tagged_gaps == gaps
    # A minute before the close still reads, with the gap row beside it.
    assert load_quotes("GLD", SESSION_DAY, "15:39", lake_root=lake).num_rows == 1


def test_sealed_a_quarantined_partition_is_refused_unless_asked_for(lake: Path) -> None:
    from lake.loader import PartitionQuarantined, load_quotes

    from tests.support.segments import AT, MINUTE
    from tests.support.segments import DAY as SESSION_DAY

    key = _sealed(lake, lakes.quote_row("GLD", AT - MINUTE))
    entry = build_entry(partition=key, verdict="quarantined", check="planted", observed_at=AT)
    append_quarantine(lake, entry)
    with pytest.raises(PartitionQuarantined):
        load_quotes("GLD", SESSION_DAY, "15:39", lake_root=lake)
    assert load_quotes("GLD", SESSION_DAY, "15:39", lake_root=lake, include_quarantined=True)


@pytest.mark.parametrize("step", [-1, 0, 1])
def test_sealed_captured_by_on_its_answer_is_the_doors_cut(
    lake: Path, tmp_path: Path, step: int
) -> None:
    """One minute's row, read from its segment and from the sealed partition, cut alike."""
    from lake.loader import load_quotes

    from tests.support.segments import AT, MINUTE
    from tests.support.segments import DAY as SESSION_DAY

    minute, landed = AT - MINUTE, AT - MINUTE + 20 * SECOND
    made = lakes.quote_row("GLD", minute, bid=61.0, fetch_end_ts=landed.isoformat())
    key, sha = lakes.quotes_segment(tmp_path, "GLD", minute, made)
    _sealed(lake, made)
    cut = landed + step * timedelta(microseconds=1)
    door = load_unsealed_quotes("GLD", SESSION_DAY, cut, segments={key: sha}, lake_root=tmp_path)
    sealed = captured_by(load_quotes("GLD", SESSION_DAY, "15:39", lake_root=lake), cut)
    assert sealed.column("bid").to_pylist() == door.column("bid").to_pylist()
    assert door.num_rows == (step >= 0)
