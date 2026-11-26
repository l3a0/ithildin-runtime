"""A decision inside its session: its quotes in the snapshot, its halts, and a redelivery.

The fixture day is 2012-04-10, the session after the built lake's last. Its
segments are made up and are written by marketlake's own writer.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest
from lake import journal
from lake.battery import build_entry
from lake.manifest import append_quarantine

from ithildinruntime.adapters import FolderBucket, FolderStore, HistoryClock
from ithildinruntime.core import (
    BlobHeader,
    BlobMissing,
    Bucket,
    ClockReading,
    DecisionResult,
    Halt,
    Mode,
    OutputFile,
    Retry,
    Segment,
    Snapshot,
    Storage,
    StoreUnavailable,
    attempt_key,
    decode,
    decode_output,
    encode,
    encode_output,
    mark,
    output_key,
    parse_strategy,
    quote_faults,
)
from ithildinruntime.data import DataStage, LakeSource, Mirror, QuoteLimits, differences, spikes
from ithildinruntime.data import source as source_module
from ithildinruntime.data.reference import UNSTATED, QuoteKind, QuoteKinds, load_quote_kinds
from ithildinruntime.data.source import _session_read, judge
from ithildinruntime.runtime.data_worker import DATA_ROW, data_buckets, data_handler
from ithildinruntime.runtime.loop import Item, Table
from tests.support import lake as lakes
from tests.support.clocks import slow
from tests.support.segments import (
    AT,
    DAY,
    DAYS,
    GLD,
    KIND,
    KINDS,
    LIMITS,
    MINUTE,
    SESSION,
    TABLE,
    USO,
    Live,
    capture,
    flipped,
    gap,
    key_of,
    landed,
)
from tests.test_data_source import Clock, Counting, Timer, settings
from tests.test_data_stage import Reports
from tests.test_storage_contract import Alarms

TICK = timedelta(microseconds=1)
LAST = DAYS[DAYS.index(DAY) - 1]
DEPLOYMENT, RUN = "test", "2012-04-10"
CHAN = parse_strategy("chan", b'instruments = ["GLD", "USO"]\n')
HASHES = Table(stages=(DATA_ROW,), strategies=(CHAN,)).hashes_for("data")


@pytest.fixture
def lake(lake: Path) -> Path:
    """The test's lake, which lists the sealed quotes of the session before the fixture day.

    No read of the fixture day takes another day's partition, so every test here
    holds beside it.
    """
    from tests.support.sealed import close_row, partition, row

    before = TABLE.session(LAST)
    for ticker in ("GLD", "USO"):
        minute = before.close - 20 * MINUTE
        partition(lake, ticker, row(ticker, minute), close_row(ticker, before.close), day=LAST)
    return lake


@pytest.fixture
def live(lake: Path) -> Path:
    """The folder beside the lake, with two cycles of both instruments in it."""
    root = lake.with_name(lake.name + "-live")
    for ticker in ("GLD", "USO"):
        for back in (1, 2):
            capture(root, ticker, back)
    return root


class Ticking(Clock):
    """A worker's clock that is a minute later each time it is read."""

    def now(self) -> datetime:
        self.at += MINUTE
        return self.at


def source(lake: Path, tmp_path: Path, **changes) -> LakeSource:
    given = {k: changes.pop(k) for k in ("bucket", "live", "clock") if k in changes}
    kinds = changes.pop("quote_kinds", KINDS)
    measured = changes.pop("measured", False)
    return LakeSource(
        Mirror(given.get("bucket") or FolderBucket(lake), tmp_path / "mirror"),
        settings(**{"quotes": LIMITS} | changes),
        live=given.get("live") or FolderBucket(lake.with_name(lake.name + "-live")),
        clock=given.get("clock") or Clock(),
        timer=Timer(),
        measured=measured,
        quote_kinds=kinds,
    )


def stage(lake: Path, tmp_path: Path, **changes) -> tuple[DataStage, Alarms]:
    alarms = Alarms()
    clock = changes.setdefault("clock", Ticking())
    made = DataStage(
        source(lake, tmp_path, **changes),
        Storage(
            FolderStore(tmp_path / "store"),
            timer=Timer(),
            pause=lambda s: None,
            retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
            alerts=alarms,
            owner_step="none",
        ),
        clock,
        deployment=DEPLOYMENT,
        run=RUN,
        release="r1",
        config_hashes=HASHES,
    )
    return made, alarms


def key(n: int = 1) -> str:
    if n == 1:
        return output_key(DEPLOYMENT, RUN, AT, "data", "snapshot")
    return attempt_key(DEPLOYMENT, RUN, AT, "data", "snapshot", n)


def stored(tmp_path: Path, n: int = 1) -> Snapshot:
    data = (tmp_path / "store" / key(n)).read_bytes()
    (entry,) = decode_output(data, Snapshot).entries
    return entry


# 7. The fixture day.


def test_7_close_minus_20_reads_its_quotes_and_its_snapshot_names_each_segment(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    made, alarms = stage(lake, tmp_path)
    result = made(Item(AT, "data"), Reports())
    assert result.halt is None and alarms.reasons == []
    snap = stored(tmp_path)
    data = (tmp_path / "store" / key()).read_bytes()
    assert decode_output(data, Snapshot).header.format == 4
    assert snap.provisional is True
    held = [
        FolderBucket(live).read(key_of(ticker, back))
        for ticker in ("GLD", "USO")
        for back in (2, 1)
    ]
    assert snap.segments == tuple(Segment(h.key, h.version, h.sha256) for h in held)
    for instrument in (GLD, USO):
        quote = snap.quotes[instrument]
        assert quote.captured_at == landed(1)
        assert quote_faults(quote, AT, AT) == ()
        assert snap.bars[instrument]["1d"][-1].session == LAST
        # It is what the mark reads, and the bar V was valued at is not.
        assert mark(snap, instrument, LAST, AT) == (quote.bid + quote.ask) / 2
        (older,) = snap.rows[instrument]
        assert older["fetch_end_ts"] == landed(2).isoformat()
    assert snap.faults == ()
    assert decode(encode(snap), Snapshot) == snap
    # The read of the bars came first, and the clock is read again after the bucket's last answer.
    assert snap.quotes_read_at > snap.bars[GLD]["1d"][-1].first_seen
    # No segment is a ledger, so the bars' cache key and the ledgers are as they were.
    assert not any("journal" in name for name in snap.ledgers)
    assert snap.ledgers == source(lake, tmp_path).read(SESSION.close).snapshot.ledgers


def _entry_point(lake: Path, tmp_path: Path) -> tuple[DecisionResult, Snapshot]:
    """One decision at `AT` through the data worker's own entry point and its default source."""
    text = (
        'instruments = ["GLD", "USO"]\n[data]\nlookback = 40\nstale_lag = 0\nspike_multiple = 100\n'
        "[data.quotes]\nmax_age_seconds = 90\nmax_spread_share = 0.02\n"
        "[data.first_sessions]\nGLD = 2006-04-26\nUSO = 2006-04-26\n"
    )
    buckets = data_buckets(Mode.BACKTEST, folder=lake)
    alarms = Alarms()
    clock = HistoryClock(readings=None)
    handler = data_handler(
        bucket=buckets.lake,
        live=buckets.live,
        mirror=tmp_path / "mirror",
        storage=Storage(
            FolderStore(tmp_path / "store"),
            timer=Timer(),
            pause=lambda s: None,
            retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
            alerts=alarms,
            owner_step="none",
        ),
        clock=clock,
        timer=Timer(),
        measured=False,
        deployment=DEPLOYMENT,
        run=RUN,
        release="r1",
        table=Table(stages=(DATA_ROW,), strategies=(parse_strategy("chan", text.encode()),)),
    )
    return handler(Item(AT, "data"), Reports()), stored(tmp_path)


def test_7_the_entry_points_buckets_read_the_folder_beside_the_lake(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    """A live quote halts on a session no row of the tracked `quote_kinds.csv` covers."""
    result, snap = _entry_point(lake, tmp_path)
    assert set(snap.quotes) == {GLD, USO}
    assert snap.quotes_read_at == AT
    # The tracked rows start in 2026, and this session is in 2012.
    assert load_quote_kinds().kind(DAY) == QuoteKind(*[UNSTATED] * 5)
    assert result.halt is not None
    assert (
        result.halt.reason
        == "; ".join(
            f"{i}'s quote cannot be read: its status is 'Normal', and the stated normal one is "
            f"'unstated'; {i}'s quote is of unstated price kind"
            for i in (GLD, USO)
        )
        + "; data/quote_kinds.csv states no price kind or no normal status of marketlake:quotes "
        f"on {DAY}"
    )


def test_7_the_entry_points_default_source_reads_the_quote_kind_contract(
    lake: Path, live: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source reads `load_quote_kinds()` by default, so a row covering the session reads.

    The tracked row covers 2026 sessions and this one is in 2012, so a loader that
    returns a row covering it stands in for the tracked file, which
    tests/test_data_files.py reads at each edge of its row.
    """
    monkeypatch.setattr(source_module, "load_quote_kinds", lambda: KINDS)
    result, snap = _entry_point(lake, tmp_path)
    assert set(snap.quotes) == {GLD, USO}
    for q in snap.quotes.values():
        stated = QuoteKind(q.price_kind, q.last_kind, q.venues, q.volume_counted, q.normal_status)
        assert stated == KIND
    assert result.halt is None


# 5 to 8. Which decisions read quotes.


@pytest.mark.parametrize(
    ("at", "reads"),
    [
        (SESSION.open - TICK, False),
        (SESSION.open, True),
        (SESSION.open + TICK, True),
        (SESSION.close - TICK, True),
        (SESSION.close, False),
        (SESSION.close + TICK, False),
    ],
)
def test_5_a_decision_reads_quotes_exactly_inside_its_session(
    lake: Path, live: Path, tmp_path: Path, at: datetime, reads: bool
) -> None:
    bucket = Live(live)
    reading = source(lake, tmp_path, live=bucket, stale_lag=1).read(at)
    snap = reading.snapshot
    assert snap.provisional is reads
    assert (snap.quotes_read_at is not None) is reads
    assert (bucket.lists != []) is reads
    if not reads:
        assert snap.quotes == snap.rows == {} and snap.segments == snap.faults == ()
        assert (reading.segments_listed, reading.segments_opened) == (0, 0)
        assert (reading.quote_seconds, reading.cycle_age) == (None, None)
        assert not any("quote" in reason for reason in reading.halts)


def test_5_a_decision_at_the_open_always_halts(lake: Path, live: Path, tmp_path: Path) -> None:
    reading = source(lake, tmp_path).read(SESSION.open)
    assert reading.snapshot.quotes == {}
    assert reading.halts[0].startswith("no capture by ")
    assert reading.halts[1:] == tuple(f"{i} has no quote in the snapshot" for i in (GLD, USO))


def test_5_an_empty_listing_is_a_provisional_snapshot_all_the_same(
    lake: Path, tmp_path: Path
) -> None:
    """The folder beside the lake is not there."""
    reading = source(lake, tmp_path).read(AT)
    assert reading.snapshot.provisional is True
    assert reading.snapshot.segments == ()
    assert reading.snapshot.quotes_read_at is not None
    assert len(reading.halts) == 3


@pytest.mark.parametrize("bare", [(GLD,), (USO,), (GLD, USO)])
def test_6_an_instrument_with_no_maximum_halts_before_either_bucket_is_called(
    lake: Path, live: Path, tmp_path: Path, bare: Sequence[str]
) -> None:
    bars, quotes = Counting(FolderBucket(lake)), Live(live)
    made = source(
        lake,
        tmp_path,
        bucket=bars,
        live=quotes,
        quotes={i: limit for i, limit in LIMITS.items() if i not in bare},
    )
    with pytest.raises(Halt) as halted:
        made.read(AT)
    assert halted.value.reason == (
        f"{', '.join(bare)} has no max_age_seconds and max_spread_share written under "
        "[data.quotes], and a quote read under no maximum is stale"
    )
    assert bars.reads == [] and quotes.lists == []
    # A decision at the close reads no quote, so it needs no maximum.
    assert made.read(TABLE.session(LAST).close).halts == ()


def test_7_a_halt_in_the_bars_read_raises_and_no_quote_is_read(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    bucket = Live(live)
    (lake / "manifest.jsonl").unlink()
    with pytest.raises(Halt, match="manifest.jsonl is not in the bucket"):
        source(lake, tmp_path, live=bucket).read(AT)
    assert bucket.lists == []


def test_7_the_bars_are_read_before_the_quotes(lake: Path, live: Path, tmp_path: Path) -> None:
    order: list[str] = []

    class Ordered(Live):
        def list(self, prefix: str) -> list[str]:
            order.append("quotes")
            return super().list(prefix)

    bars = Counting(FolderBucket(lake))
    real = bars.read

    def read(key: str, version: str | None = None):
        order.append("bars")
        return real(key, version)

    bars.read = read  # type: ignore[method-assign]
    source(lake, tmp_path, bucket=bars, live=Ordered(live)).read(AT)
    assert "bars" not in order[order.index("quotes") :]


def test_8_a_fault_halts_beside_a_stored_snapshot_that_keeps_what_was_read(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    bucket = Live(live)
    bad = key_of("USO", 1)
    bucket.plant[bad] = [flipped, flipped]
    made, _ = stage(lake, tmp_path, live=bucket)
    result = made(Item(AT, "data"), Reports())
    snap = stored(tmp_path)
    (fault,) = snap.faults
    assert fault.startswith(f"{bad} failed twice")
    assert result.halt is not None and result.halt.cause == result.outputs[0]
    assert result.halt.reason == "; ".join(
        [fault, "GLD has no quote in the snapshot", "USO has no quote in the snapshot"]
    )
    assert snap.quotes == {} and snap.provisional is True
    assert len(snap.rows[GLD]) == 2 and USO not in snap.rows
    assert {s.key for s in snap.segments} == {key_of(t, b) for t in ("GLD", "USO") for b in (1, 2)}
    assert len(snap.bars[GLD]["1d"]) == 40
    with pytest.raises(Halt, match="failed twice"):
        bucket.plant[bad] = [flipped, flipped]
        source(lake, tmp_path, live=bucket).snapshot(AT)


def _no_listing(bucket: Live, live: Path) -> str:
    for back in (1, 2):
        (live / key_of("USO", back)).unlink()
    return "USO has 0 segments under "


def _one_leg_stopped(bucket: Live, live: Path) -> str:
    for back in (1, 2):
        (live / key_of("USO", back)).unlink()
        capture(live, "USO", back + 200)
    return "no capture by "


def _all_gaps(bucket: Live, live: Path) -> str:
    for back in (1, 2):
        (live / key_of("USO", back)).unlink()
        gap(live, "USO", back)
    return "and no row read"


def _refused_listing(bucket: Live, live: Path) -> str:
    prefix = key_of("USO", 1).rpartition("/")[0] + "/"
    bucket.plant[prefix] = [StoreUnavailable(prefix, "403 AccessDenied: no")] * 2
    return "could not be listed: StoreUnavailable"


def _no_answer(bucket: Live, live: Path) -> str:
    bucket.plant[key_of("USO", 1)] = [StoreUnavailable(key_of("USO", 1), "no answer")] * 2
    return "failed twice"


def _expired(bucket: Live, live: Path) -> str:
    bucket.plant[key_of("USO", 1)] = [RuntimeError("the refreshed credentials are still expired")]
    return "could not be downloaded: RuntimeError"


def _gone(bucket: Live, live: Path) -> str:
    bucket.plant[key_of("USO", 1)] = [BlobMissing(key_of("USO", 1), "404 NoSuchKey")]
    return "is listed and the bucket holds none"


def _bare(bucket: Live, live: Path) -> str:
    bucket.plant[key_of("USO", 1)] = [lambda got: replace(got, sha256=None)]
    return "has no stored SHA-256 in the bucket"


def _torn(bucket: Live, live: Path) -> str:
    path = live / key_of("USO", 1)
    path.write_bytes(path.read_bytes()[:-8])
    return "was refused by marketlake's reader"


def _newer_version(bucket: Live, live: Path) -> str:
    capture(live, "USO", 1, schema_version=journal.SCHEMA_VERSION + 1)
    return "so the marketlake pin is raised"


def _retyped(bucket: Live, live: Path) -> str:
    schema = journal.schema_for(journal.QUOTES_SURFACE)
    place = schema.get_field_index("bid_size")
    for back in (1, 2):
        capture(live, "USO", back, schema=schema.set(place, pa.field("bid_size", pa.int32())))
    return "has bid_size as int32"


def _twice(bucket: Live, live: Path) -> str:
    capture(live, "USO", 3, fetch_end_ts=landed(1).isoformat())
    return "USO has 2 rows captured at "


def _withheld(bucket: Live, live: Path) -> str:
    lake = live.with_name(live.name.removesuffix("-live"))
    entry = build_entry(
        partition=f"quotes/ticker=USO/date={DAY}.parquet",
        verdict="quarantined",
        check="planted",
        observed_at=AT,
    )
    append_quarantine(lake, entry)
    lakes.record(lake, "quarantine.jsonl")
    return "the quarantine ledger withholds"


ANSWERS = (
    _no_listing,
    _withheld,
    _one_leg_stopped,
    _all_gaps,
    _refused_listing,
    _no_answer,
    _expired,
    _gone,
    _bare,
    _torn,
    _newer_version,
    _retyped,
    _twice,
)


@pytest.mark.parametrize("answer", ANSWERS, ids=lambda a: a.__name__.strip("_"))
def test_8_each_answer_stores_the_snapshot_and_then_halts(
    lake: Path, live: Path, tmp_path: Path, answer
) -> None:
    """The drill of each alarm on the read: the stored record, then the decision's halt."""
    bucket = Live(live)
    said = answer(bucket, live)
    made, _ = stage(lake, tmp_path, live=bucket)
    result = made(Item(AT, "data"), Reports())
    snap = stored(tmp_path)
    (fault,) = snap.faults
    assert said in fault
    assert result.halt is not None and result.halt.cause == result.outputs[0]
    assert result.halt.reason == "; ".join(
        [fault, "GLD has no quote in the snapshot", "USO has no quote in the snapshot"]
    )
    assert snap.provisional is True and snap.quotes == {}
    assert len(snap.bars[GLD]["1d"]) == 40
    assert len(snap.rows.get(GLD, ())) == (2 if bucket.reads else 0)


def test_8_a_quote_the_judge_refuses_halts_beside_every_row(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    capture(live, "USO", 1, realtime=False)
    made, _ = stage(lake, tmp_path)
    result = made(Item(AT, "data"), Reports())
    snap = stored(tmp_path)
    assert snap.faults == () and set(snap.quotes) == {GLD, USO}
    assert {i: len(rows) for i, rows in snap.rows.items()} == {GLD: 1, USO: 1}
    assert result.halt is not None
    assert result.halt.reason == (
        "USO's quote cannot be read: its realtime flag is received as 'False', and only true "
        "says its delay is none"
    )


def test_8_no_fault_of_the_bucket_leaves_the_read_as_an_error(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    class Down(Live):
        def list(self, prefix: str) -> list[str]:
            raise StoreUnavailable(prefix, "no answer: timed out")

    reading = source(lake, tmp_path, live=Down(live)).read(AT)
    assert len(reading.snapshot.faults) == 2 and len(reading.halts) == 4


def test_the_bars_halts_come_before_the_quotes(lake: Path, live: Path, tmp_path: Path) -> None:
    lakes.with_row(lake, "GLD", LAST, high=150.0, low=149.0)
    capture(live, "USO", 1, realtime=False)
    halts = source(lake, tmp_path).read(AT).halts
    assert len(halts) == 2
    assert "contradict each other" in halts[0] and "realtime flag" in halts[1]


def test_12_the_loops_report_runs_after_each_call_the_stages_read_makes(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    bucket = Live(live)
    made, _ = stage(lake, tmp_path, live=bucket)
    reports = Reports()
    made(Item(AT, "data"), reports)
    # Two listings, four downloads and two calls of marketlake's reader, beside the stage's own two.
    assert len(bucket.lists) + len(bucket.reads) + 2 + 2 == reports.count == 10


def test_33_a_reading_and_the_log_carry_the_load_and_the_lag(
    lake: Path, live: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    class Ticks(Timer):
        def __init__(self) -> None:
            self.at = 0.0

        def monotonic(self) -> float:
            self.at += 1.5
            return self.at

    for back in range(3, 9):
        for ticker in ("GLD", "USO"):
            capture(live, ticker, back)
    made = source(lake, tmp_path)
    made._timer = Ticks()
    with caplog.at_level(logging.INFO):
        reading = made.read(AT)
    assert (reading.segments_listed, reading.segments_opened) == (16, 10)
    assert reading.quote_seconds == 1.5
    assert reading.cycle_age == timedelta(seconds=59)
    (line,) = [r.getMessage() for r in caplog.records if "segments" in r.getMessage()]
    assert (reading.sealed, reading.quote_calls) == (False, 0)
    cut = (AT - timedelta(seconds=15)).isoformat()
    assert line == (
        f"data read segments for the decision at {AT.isoformat()}, opening 10 of 16 listed, "
        f"with the cut at {cut}, in 1.500 seconds, and the newest capture its instruments share "
        "was 59.000 seconds old"
    )


# 30. The spike flags on a quote.


def test_8_a_spiked_midpoint_is_flagged_beside_its_unchanged_price_and_halts_nothing(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    capture(live, "GLD", 1, bid=1600.0, ask=1600.5)
    made, _ = stage(lake, tmp_path, spike_multiple=Decimal(10))
    result = made(Item(AT, "data"), Reports())
    assert result.halt is None
    snap = stored(tmp_path)
    (flag,) = snap.flags[GLD]
    before = snap.bars[GLD]["1d"][-1].close
    assert (flag.session, flag.field, flag.price, flag.against, flag.rule) == (
        DAY,
        "midpoint",
        Decimal("1600.25"),
        before,
        spikes.MOVE,
    )
    assert snap.quotes[GLD].bid == Decimal("1600.0")
    # A redelivery judges the stored flag by its rule, and halts nothing either.
    again = made(Item(AT, "data"), Reports())
    assert again.outputs[1].key == key(2) and again.halt is None


def test_30_a_quotes_flag_stands_beside_its_bars_flags(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    lakes.with_row(lake, "GLD", LAST, close=150.0)
    capture(live, "GLD", 1, bid=1600.0, ask=1600.5)
    reading = source(lake, tmp_path, spike_multiple=Decimal(10)).read(AT)
    assert [(f.session, f.field, f.rule) for f in reading.snapshot.flags[GLD]] == [
        (LAST, "close", spikes.BELOW_LOW),
        (DAY, "midpoint", spikes.MOVE),
    ]


def test_5_a_half_day_reads_quotes_until_its_own_close() -> None:
    half = TABLE.session(date(2026, 11, 27))
    assert half.close - half.open < SESSION.close - SESSION.open
    assert _session_read(TABLE, half.close - MINUTE) == half
    assert _session_read(TABLE, half.close) is None


def test_8_a_quote_whose_prices_contradict_each_other_is_flagged_and_halts(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    capture(live, "GLD", 1, bid=160.5, ask=160.0)
    made, _ = stage(lake, tmp_path)
    result = made(Item(AT, "data"), Reports())
    (flag,) = stored(tmp_path).flags[GLD]
    assert (flag.field, flag.rule) == ("ask", spikes.CONTRADICT)
    assert result.halt is not None
    said = "GLD's quote cannot be read: its ask of 160.0 is below its bid of 160.5"
    assert result.halt.reason == said
    again = made(Item(AT, "data"), Reports())
    assert again.halt is not None and again.halt.reason == f"{key()}, which counts: {said}"


# 31. A redelivery.


def test_31_a_redelivery_judges_the_first_captures_quotes_and_not_its_own(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    made, alarms = stage(lake, tmp_path)
    assert made(Item(AT, "data"), Reports()).halt is None
    capture(live, "USO", 1, realtime=False)
    again = made(Item(AT, "data"), Reports())
    assert again.halt is None and again.outputs[1].key == key(2)
    (reason,) = alarms.reasons
    assert "These differ: quotes, segments" in reason


def test_31_a_redelivery_halts_on_a_first_capture_its_judge_refuses(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    capture(live, "USO", 1, realtime=False)
    made, _ = stage(lake, tmp_path)
    first = made(Item(AT, "data"), Reports())
    capture(live, "USO", 1)
    again = made(Item(AT, "data"), Reports())
    assert again.halt is not None
    assert again.halt.reason == f"{key()}, which counts: {first.halt.reason}"


def test_31_a_redelivery_halts_on_a_first_capture_with_no_quote(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    """A crash after the snapshot stored cannot drop the halt of an absent quote."""
    for ticker in ("GLD", "USO"):
        for back in (1, 2):
            (live / key_of(ticker, back)).unlink()
    made, _ = stage(lake, tmp_path)
    first = made(Item(AT, "data"), Reports())
    for ticker in ("GLD", "USO"):
        capture(live, ticker, 1)
    again = made(Item(AT, "data"), Reports())
    reasons = stored(tmp_path).faults + tuple(
        f"{i} has no quote in the snapshot" for i in (GLD, USO)
    )
    assert first.halt.reason == "; ".join(reasons)
    assert again.halt.reason == "; ".join(f"{key()}, which counts: {r}" for r in reasons)


def test_31_the_judge_names_an_absent_quote_only_where_the_decision_reads_one(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    snap = source(lake, tmp_path).read(AT).snapshot
    assert judge(snap, (GLD, USO), measured=False) == ()
    bare = replace(snap, quotes={GLD: snap.quotes[GLD]})
    assert judge(bare, (GLD, USO), measured=False) == ("USO has no quote in the snapshot",)
    assert judge(replace(bare, faults=("a reason",)), (GLD, USO), measured=False) == (
        "a reason",
        "USO has no quote in the snapshot",
    )
    # A capture that says it is not provisional is judged by its decision all the same.
    assert judge(replace(bare, provisional=False, segments=()), (GLD, USO), measured=False) == (
        "USO has no quote in the snapshot",
    )
    at_close = source(lake, tmp_path).read(TABLE.session(LAST).close).snapshot
    assert judge(at_close, (GLD, USO), measured=False) == ()


def test_31_a_stored_contradiction_the_quote_does_not_raise_halts_as_recorded(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    snap = source(lake, tmp_path).read(AT).snapshot
    crossed = replace(
        snap.quotes[GLD],
        bid=Decimal("160.5"),
        ask=Decimal("160.0"),
        max_spread_share=Decimal("0.5"),
    )
    flags = tuple(spikes.quote_contradictions(crossed, DAY))
    one = replace(snap, quotes={**snap.quotes, GLD: crossed}, flags={GLD: flags})
    assert len(judge(one, (GLD, USO), measured=False)) == 1
    (reason,) = judge(replace(snap, flags={GLD: flags}), (GLD, USO), measured=False)
    assert "halts as recorded: its ask of 160.0 against 160.5" in reason
    other_day = tuple(replace(f, session=LAST) for f in flags)
    assert len(judge(replace(one, flags={GLD: other_day}), (GLD, USO), measured=False)) == 2


def test_31_a_later_listing_is_named_as_what_adds_the_segments(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    made, alarms = stage(lake, tmp_path)
    made(Item(AT, "data"), Reports())
    # A segment that started before and lands after the decision joins the listing.
    for ticker in ("GLD", "USO"):
        capture(live, ticker, 3)
        capture(live, ticker, 0)
    again = made(Item(AT, "data"), Reports())
    assert again.halt is None
    (reason,) = alarms.reasons
    assert "These differ: segments, rows. A later listing adds them, and every segment both " in (
        reason
    )


def two(first: Snapshot, later: Snapshot, release: str = "r1") -> str:
    def encoded(snap: Snapshot, by: str) -> bytes:
        header = BlobHeader(3, DEPLOYMENT, RUN, "data", "capturing", AT, {}, by, HASHES)
        return encode_output(Snapshot, OutputFile(header, (snap,), False))

    return differences(encoded(first, "r1"), encoded(later, release), measured=False)


def test_31_what_a_later_listing_does_and_does_not_explain(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    snap = source(lake, tmp_path).read(AT).snapshot
    added = Segment("journal/new.arrows", "v", "0" * 64)
    explained = ". A later listing adds them, and every segment both captures name kept its "
    more = replace(snap, segments=(*snap.segments, added))
    assert explained in two(snap, more)
    assert two(snap, more).startswith("These differ: segments.")
    rows = {**snap.rows, GLD: snap.rows[GLD] * 2}
    assert explained in two(snap, replace(more, rows=rows))
    # No segment was added, or something else moved too, or one both name changed.
    assert two(snap, replace(snap, rows=rows)) == "These differ: rows"
    assert two(snap, replace(more, faults=("a reason",))) == "These differ: segments, faults"
    quotes = {i: replace(q, bid=q.bid - 1) for i, q in snap.quotes.items()}
    assert two(snap, replace(more, quotes=quotes)) == "These differ: quotes, segments"
    flags = {GLD: tuple(spikes.quote_contradictions(replace(quotes[GLD], bid=Decimal(0)), DAY))}
    assert two(snap, replace(more, flags=flags)) == "These differ: flags, segments"
    assert two(snap, more, release="r2") == "These differ: the release, segments"
    first, *rest = snap.segments
    for change in ({"version": "v9"}, {"sha256": "1" * 64}):
        moved = replace(snap, segments=(replace(first, **change), *rest, added))
        assert two(snap, moved) == "These differ: segments"
    # A longer listing moves the newest five on, and reads as a segment that left does.
    moved = replace(snap, segments=(*snap.segments[1:], added))
    assert two(snap, moved) == (
        "These differ: segments. Every segment both captures name kept its version and SHA-256"
    )
    assert two(snap, replace(snap, segments=snap.segments[1:])) == "These differ: segments"
    later = replace(snap, quotes_read_at=snap.quotes_read_at + MINUTE)
    assert two(snap, later) == (
        "Only the read time or the clock's reading differs, as on a redelivery after a crash"
    )
    # The clock's fault is named beside the segments, so the listing explains nothing.
    past = replace(more, clock=slow(AT))
    assert two(snap, past) == "These differ: segments, whether the clock was past a bound"


def test_31_a_first_capture_of_the_format_before_halts_as_no_snapshot_the_codec_reads(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    made, alarms = stage(lake, tmp_path)
    old = (
        b'{"body":{"entries":[{"bars":{},"decision_at":"2012-04-10T19:40:00.000000Z","faults":[],'
        b'"flags":{},"ledgers":{},"provisional":false,"quotes":{},"quotes_read_at":null,'
        b'"rows":{},"segments":[],"status":{}}],'
        b'"trade_nothing":false},"header":{"config_hashes":{"chan":"'
        + HASHES["chan"].encode()
        + b'"},"decision_at":"2012-04-10T19:40:00.000000Z","deployment":"test","format":2,'
        b'"inputs":{},"kind":"capturing","release":"r1","run":"2012-04-10","stage":"data"}}'
    )
    path = tmp_path / "store" / key()
    path.parent.mkdir(parents=True)
    path.write_bytes(old)
    result = made(Item(AT, "data"), Reports())
    assert result.halt is not None
    assert result.halt.reason.startswith(f"{key()} counts and is no snapshot the codec reads: ")
    assert "holds fields ['bars', 'decision_at', 'faults'," in result.halt.reason
    assert "not ['bars', 'clock', 'decision_at', 'faults'," in result.halt.reason


# The clock's reading.


class Noting:
    """A live bucket that notes each call it answers."""

    def __init__(self, bucket: Bucket, order: list[str]) -> None:
        self._bucket, self._order = bucket, order

    def list(self, prefix: str):
        self._order.append("list")
        return self._bucket.list(prefix)

    def read(self, key: str, version: str | None = None):
        self._order.append("read")
        return self._bucket.read(key, version)


def test_the_clock_is_read_after_the_last_answer_of_the_quotes(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    order: list[str] = []

    class Noted(Ticking):
        def now(self) -> datetime:
            order.append("now")
            return super().now()

        def reading(self) -> ClockReading:
            order.append("reading")
            return slow(AT)

    reading = source(
        lake,
        tmp_path,
        live=Noting(FolderBucket(live), order),
        clock=Noted(),
        quote_kinds=QuoteKinds(()),
    ).read(AT)
    # The quotes' read time is taken after its last answer, and the reading after that.
    assert order[-3:] == ["read", "now", "reading"] and order.count("reading") == 1
    assert reading.snapshot.clock == slow(AT)
    assert reading.halts[0].startswith("the clock is 2.000000 seconds slow against")
    assert len(reading.halts) == 4 and "quote cannot be read" in reading.halts[1]


# 21. What is recorded and not judged.


def test_21_unstated_venues_and_volume_counting_halt_nothing(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    (row,) = KINDS.rows
    quiet = replace(row.kind, venues="unstated", volume_counted="unstated", last_kind="unstated")
    kinds = QuoteKinds((replace(row, kind=quiet),))
    reading = source(lake, tmp_path, quote_kinds=kinds).read(AT)
    assert reading.halts == ()
    assert reading.snapshot.quotes[GLD].venues == "unstated"
    for field in ("price_kind", "normal_status"):
        loud = QuoteKinds((replace(row, kind=replace(row.kind, **{field: "unstated"})),))
        halts = source(lake, tmp_path, quote_kinds=loud).read(AT).halts
        assert len(halts) == 3 and "data/quote_kinds.csv states no price kind" in halts[2]


def test_28_a_lapsed_last_session_halts_every_decision_inside_a_session(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    (row,) = KINDS.rows
    lapsed = QuoteKinds((replace(row, first_session=LAST, last_session=LAST),))
    halts = source(lake, tmp_path, quote_kinds=lapsed).read(AT).halts
    assert len(halts) == 3 and all("unstated price kind" in h for h in halts[:2])
    assert halts[2].endswith(f"of marketlake:quotes on {DAY}")


def test_29_each_instruments_quote_carries_its_own_maxima(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    limits = {GLD: QuoteLimits(90, Decimal("0.02")), USO: QuoteLimits(45, Decimal("0.5"))}
    reading = source(lake, tmp_path, quotes=limits).read(AT)
    assert reading.snapshot.quotes[GLD].max_age == 90
    assert reading.snapshot.quotes[USO].max_spread_share == Decimal("0.5")
    (halt,) = reading.halts
    assert halt.startswith("USO's quote cannot be read: its ask has the stamp")


def test_a_backtests_clock_reads_the_decision_time_over_a_folder_of_the_whole_day(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    """A row captured after the decision is cut by its stamp, and never by the worker's clock."""
    for ticker in ("GLD", "USO"):
        for back in range(-19, 1):
            capture(live, ticker, back)
    clock = HistoryClock(readings=None)
    clock.wait_until(AT, None)  # type: ignore[arg-type]
    reading = source(lake, tmp_path, clock=clock).read(AT)
    assert reading.halts == ()
    assert reading.snapshot.quotes[GLD].captured_at == landed(1)
    assert reading.snapshot.quotes_read_at == AT


def test_the_lake_root_is_removed_with_the_segments_in_it(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    source(lake, tmp_path).read(AT)
    assert list((tmp_path / "mirror" / "roots").iterdir()) == []
