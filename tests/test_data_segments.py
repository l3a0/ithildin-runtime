"""The read of one decision's quotes from marketlake's unsealed capture segments.

Each test builds its segments with marketlake's own writer and reads them
through the pinned reader. Every read asserts that the loop's `report` ran after
each call to the bucket and to that reader.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest
from lake import journal
from lake.battery import build_entry
from lake.manifest import append_quarantine
from lake.schema_versions import (
    RecordedVersion,
    SchemaVersionLedger,
    ledger_path,
    running_fingerprints,
)

from ithildinruntime.core import (
    BlobMissing,
    ChecksumMismatch,
    Quote,
    Segment,
    Stopped,
    StoreUnavailable,
    quote_faults,
)
from ithildinruntime.core.codec import INT_LIMIT
from ithildinruntime.data import QuoteLimits, load_master, segments
from ithildinruntime.data.reference import MasterRow, QuoteKinds, SecurityMaster
from ithildinruntime.data.segments import BATCH, COLUMNS, QuoteRead, read_quotes
from tests.support import lake as lakes
from tests.support.segments import (
    AT,
    BIDS,
    DAY,
    GLD,
    KIND,
    KINDS,
    LIMITS,
    MINUTE,
    SECOND,
    SESSION,
    SPY,
    USO,
    Live,
    capture,
    digest,
    flipped,
    gap,
    key_of,
    landed,
)
from tests.test_lake_contract import PUBLISHED

READ_AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
PREFIX = {i: f"journal/date={DAY}/surface=quotes/ticker={i}/" for i in (GLD, USO, SPY)}


class Clock:
    """A clock that counts its reads, and asks that the bucket has nothing left to answer."""

    def __init__(self, live: Live) -> None:
        self._live = live
        self.reads: list[int] = []

    def now(self) -> datetime:
        self.reads.append(len(self._live.events))
        return READ_AT


class Reader:
    """One decision's read over a folder of live segments, with every call counted."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.live_root = tmp_path / "lake-live"
        self.root = tmp_path / "root"
        self.live_root.mkdir()
        self.root.mkdir()
        (self.root / "quarantine.jsonl").touch()
        self.live = Live(self.live_root)
        self.door: list[list[str]] = []
        real = segments.load_unsealed_quotes

        def door(*args, **kwargs):
            self.door.append(sorted(kwargs["segments"]))
            self.live.events.append("call")
            return real(*args, **kwargs)

        monkeypatch.setattr(segments, "load_unsealed_quotes", door)

    def capture(self, ticker: str, backs: Sequence[int], **changes: object) -> list[str]:
        return [capture(self.live_root, ticker, back, **changes) for back in backs]

    def both(self, backs: Sequence[int]) -> None:
        for ticker in ("GLD", "USO"):
            self.capture(ticker, backs)

    def read(
        self,
        limits: dict = LIMITS,
        master: SecurityMaster | None = None,
        kinds: QuoteKinds = KINDS,
        at: datetime = AT,
    ) -> QuoteRead:
        clock = Clock(self.live)
        made = read_quotes(
            self.live,
            self.root,
            SESSION,
            at,
            limits,
            master=master or load_master(),
            kinds=kinds,
            clock=clock,
            report=lambda: self.live.events.append("report"),
        )
        # Each call is followed by a report, and the clock is read once, after the last one.
        assert self.live.events[::2] == ["call"] * (len(self.live.events) // 2)
        assert self.live.events[1::2] == ["report"] * (len(self.live.events) // 2)
        assert clock.reads == [len(self.live.events)]
        assert made.read_at == READ_AT
        if not any("could not be listed" in fault for fault in made.faults):
            assert made.listed == sum(len(self.live.bucket.list(p)) for p in self.live.lists[-2:])
        # Every segment opened is named by its version and stored SHA-256, in key order.
        assert [s.key for s in made.segments] == sorted(s.key for s in made.segments)
        if not made.faults or made.faults[0].startswith("no capture by"):
            assert {s.key for s in made.segments} == set(self.live.keys)
        for segment in made.segments:
            held = self.live.bucket.read(segment.key)
            assert segment == Segment(segment.key, held.version, held.sha256)
        assert not (made.quotes and made.faults)
        return made


@pytest.fixture
def reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Reader]:
    yield Reader(tmp_path, monkeypatch)


def captured(read: QuoteRead) -> datetime:
    (at,) = {q.captured_at for q in read.quotes.values()}
    assert list(read.quotes) == [GLD, USO]
    assert read.faults == ()
    return at


def counts(reader: Reader) -> dict[str, int]:
    return {t: sum(f"ticker={t}/" in k for k in reader.live.keys) for t in ("GLD", "USO")}


# 1. The read.


def test_1_one_cycle_gives_one_quote_of_each_from_its_own_row(reader: Reader) -> None:
    reader.both([1])
    read = reader.read()
    assert captured(read) == landed(1)
    for instrument, quote in read.quotes.items():
        bid = Decimal(repr(BIDS[instrument]))
        assert (quote.bid, quote.ask, quote.last) == (bid, bid + Decimal("0.5"), Decimal("1.0"))
    assert reader.live.lists == [PREFIX[GLD], PREFIX[USO]]
    assert counts(reader) == {"GLD": 1, "USO": 1}
    assert len(reader.door) == 2
    assert read.cycle_age == AT - landed(1) == timedelta(seconds=59)
    assert read.listed == 2
    assert read.rows == {}


@pytest.mark.parametrize("n", [1, 2, 3, 5, 6, 11])
def test_1_the_newest_of_several_cycles_is_read_from_the_newest_five(
    reader: Reader, n: int
) -> None:
    reader.both(range(1, n + 1))
    read = reader.read()
    assert captured(read) == landed(1)
    assert counts(reader) == {"GLD": min(n, BATCH), "USO": min(n, BATCH)}
    assert len(read.segments) == 2 * min(n, BATCH)
    assert read.listed == 2 * n
    # Every other row the door returned is kept beside the quotes.
    kept = {i: len(rows) for i, rows in read.rows.items()}
    assert kept == ({GLD: min(n, 5) - 1, USO: min(n, 5) - 1} if n > 1 else {})


@pytest.mark.parametrize("empty", [(), ("GLD",), ("USO",), ("GLD", "USO")])
def test_1_an_instrument_that_lists_nothing_opens_nothing(
    reader: Reader, empty: tuple[str, ...]
) -> None:
    for ticker in ("GLD", "USO"):
        if ticker not in empty:
            reader.capture(ticker, [1, 2])
    read = reader.read()
    if not empty:
        assert captured(read) == landed(1)
        return
    assert read.quotes == {} and reader.live.reads == [] and reader.door == []
    said = {t: f"{t} has {0 if t in empty else 2} segments under" for t in ("GLD", "USO")}
    assert read.faults == (
        f"no capture by {(AT - 15 * SECOND).isoformat()} holds a row of every instrument: "
        f"{said['GLD']} {PREFIX[GLD]}, 0 of them opened, and no row read; "
        f"{said['USO']} {PREFIX[USO]}, 0 of them opened, and no row read",
    )
    assert read.cycle_age is None
    assert reader.live.lists == [PREFIX[GLD], PREFIX[USO]]


def test_1_a_leg_that_lists_three_shares_a_cycle_with_the_others_sixth(reader: Reader) -> None:
    wide = {i: QuoteLimits(3600, Decimal("0.02")) for i in (GLD, USO)}
    reader.capture("GLD", [1, 2, 3, 4, 5, 6, 7, 8])
    reader.capture("USO", [6, 7, 8])
    read = reader.read(wide)
    assert captured(read) == landed(6)
    assert counts(reader) == {"GLD": 8, "USO": 3}


@pytest.mark.parametrize(("common", "downloads"), [(1, 5), (5, 5), (6, 10), (10, 10), (11, 12)])
def test_1_the_walk_goes_back_five_at_a_time_to_the_newest_common_cycle(
    reader: Reader, common: int, downloads: int
) -> None:
    """USO's other rows land a second after GLD's, so one cycle alone is both legs'."""
    wide = {i: QuoteLimits(3600, Decimal("0.02")) for i in (GLD, USO)}
    reader.capture("GLD", range(1, 13))
    for back in range(1, 13):
        late = landed(back) + (timedelta(0) if back == common else SECOND)
        reader.capture("USO", [back], fetch_end_ts=late.isoformat())
    read = reader.read(wide)
    assert captured(read) == landed(common)
    assert counts(reader) == {"GLD": downloads, "USO": downloads}
    assert {i: len(rows) for i, rows in read.rows.items()} == {
        GLD: downloads - 1,
        USO: downloads - 1,
    }


def test_1_an_older_cycle_both_legs_have_is_taken_over_a_newer_one_only_one_has(
    reader: Reader,
) -> None:
    reader.capture("GLD", [1, 2, 3])
    reader.capture("USO", [2, 3])
    read = reader.read()
    assert captured(read) == landed(2)
    # The newer row GLD alone has is kept, and the age is the capture's that both have.
    assert [r["fetch_end_ts"] for r in read.rows[GLD]] == [
        landed(3).isoformat(),
        landed(1).isoformat(),
    ]
    assert read.cycle_age == AT - landed(2)


def no_cycle(reader: Reader, read: QuoteRead) -> str:
    assert read.quotes == {}
    (reason,) = read.faults
    cut = (AT - 15 * SECOND).isoformat()
    assert reason.startswith(f"no capture by {cut} holds a row of every instrument: ")
    return reason


def test_1_a_full_day_with_no_common_cycle_ends_at_the_maximum_age(reader: Reader) -> None:
    reader.capture("GLD", range(1, 371))
    for back in range(1, 371):
        reader.capture("USO", [back], fetch_end_ts=(landed(back) + SECOND).isoformat())
    read = reader.read()
    reason = no_cycle(reader, read)
    assert counts(reader) == {"GLD": 10, "USO": 10}
    assert reason.endswith(
        f"GLD has 370 segments under {PREFIX[GLD]}, 10 of them opened, and its newest row "
        f"captured at {landed(1).isoformat()}; USO has 370 segments under {PREFIX[USO]}, 10 of "
        f"them opened, and its newest row captured at {(landed(1) + SECOND).isoformat()}"
    )
    assert {i: len(rows) for i, rows in read.rows.items()} == {GLD: 10, USO: 10}


@pytest.mark.parametrize(
    ("newest", "downloads"),
    [
        (timedelta(seconds=90) - timedelta(microseconds=1), 10),
        (timedelta(seconds=90), 10),
        (timedelta(seconds=90) + timedelta(microseconds=1), 5),
    ],
)
def test_1_a_batch_ends_an_instrument_only_where_every_row_is_past_its_maximum_age(
    reader: Reader, newest: timedelta, downloads: int
) -> None:
    """USO's newest is at the boundary, and nothing of GLD's lands with it."""
    reader.capture("GLD", range(1, 13))
    for back in range(2, 13):
        late = AT - newest if back == 2 else landed(back) + SECOND
        reader.capture("USO", [back], fetch_end_ts=late.isoformat())
    tight = {**LIMITS, GLD: QuoteLimits(10, Decimal("0.02"))}
    read = reader.read(tight)
    no_cycle(reader, read)
    assert counts(reader) == {"GLD": 5, "USO": downloads}


def test_1_one_leg_that_stopped_is_left_and_the_other_goes_on(reader: Reader) -> None:
    reader.capture("GLD", range(1, 30))
    reader.capture("USO", range(200, 230))
    read = reader.read()
    reason = no_cycle(reader, read)
    # GLD's newest row is fresh, and the age that says how far capture lags is not its own.
    assert read.cycle_age is None
    assert counts(reader) == {"GLD": 10, "USO": 5}
    assert f"its newest row captured at {landed(200).isoformat()}" in reason


def test_1_a_late_read_where_one_leg_stopped_finds_the_cycle_they_share(reader: Reader) -> None:
    """Stopping both legs at USO's first batch, all past its age, would lose the cycle."""
    reader.capture("GLD", range(-12, 13))
    reader.capture("USO", range(3, 13))
    read = reader.read()
    assert captured(read) == landed(3)
    assert counts(reader) == {"GLD": 20, "USO": 5}
    # The judge, and not the walk, refuses a quote that old.
    assert all(quote_faults(q, AT, AT) for q in read.quotes.values())


def test_1_a_leg_whose_capture_wrote_only_gaps_is_opened_to_its_listings_end(
    reader: Reader,
) -> None:
    reader.capture("GLD", range(1, 13))
    for back in range(1, 13):
        gap(reader.live_root, "USO", back)
    read = reader.read()
    reason = no_cycle(reader, read)
    assert counts(reader) == {"GLD": 10, "USO": 12}
    assert "12 of them opened, and no row read" in reason
    assert set(read.rows) == {GLD}
    assert read.cycle_age is None


def test_1_gap_only_newest_segments_are_passed_over(reader: Reader) -> None:
    wide = {i: QuoteLimits(600, Decimal("0.02")) for i in (GLD, USO)}
    for ticker in ("GLD", "USO"):
        for back in range(1, 7):
            gap(reader.live_root, ticker, back)
        reader.capture(ticker, [7, 8])
    read = reader.read(wide)
    assert captured(read) == landed(7)
    assert counts(reader) == {"GLD": 8, "USO": 8}


@pytest.mark.parametrize(("back", "seen"), [(5, True), (6, False)])
def test_1_a_cycle_run_by_hand_and_late_is_seen_only_inside_the_segments_opened(
    reader: Reader, back: int, seen: bool
) -> None:
    """It started `back` minutes before the decision and landed after the newer cycles did."""
    late = AT - 20 * SECOND
    for ticker in ("GLD", "USO"):
        reader.capture(ticker, [n for n in range(1, 9) if n != back])
        reader.capture(ticker, [back], fetch_end_ts=late.isoformat(), bid=77.0)
    read = reader.read()
    assert captured(read) == (late if seen else landed(1))
    assert counts(reader) == {"GLD": 5, "USO": 5}


@pytest.mark.parametrize("ticker", ["GLD", "USO"])
@pytest.mark.parametrize("apart", [True, False], ids=["two segments", "one segment"])
def test_1_two_rows_of_one_instrument_at_the_cycle_are_a_fault(
    reader: Reader, apart: bool, ticker: str
) -> None:
    reader.both([1, 2])
    minute = AT - MINUTE
    if apart:
        reader.capture(ticker, [3], fetch_end_ts=landed(1).isoformat())
    else:
        (reader.live_root / key_of(ticker, 1)).unlink()
        row = lakes.quote_row(ticker, minute)
        lakes.quotes_segment(reader.live_root, ticker, minute, row, row)
    read = reader.read()
    assert read.quotes == {}
    assert read.faults == (
        f"{ticker} has 2 rows captured at {landed(1).isoformat()}, under "
        f"{PREFIX[GLD if ticker == 'GLD' else USO]}",
    )
    assert {str(i): len(rows) for i, rows in read.rows.items()} == {"GLD": 2, "USO": 2} | {
        ticker: 3
    }
    # The capture was found, whatever refused its rows.
    assert read.cycle_age == AT - landed(1)


@pytest.mark.parametrize("after", [timedelta(microseconds=1), 30 * SECOND])
def test_1_a_row_landed_after_the_decision_is_never_read(reader: Reader, after: timedelta) -> None:
    reader.both([1])
    for ticker in ("GLD", "USO"):
        reader.capture(ticker, [0], fetch_end_ts=(AT + after).isoformat(), bid=1.0, ask=9999.0)
    read = reader.read()
    assert captured(read) == landed(1)
    assert read.rows == {}
    assert read.cycle_age == AT - landed(1)


@pytest.mark.parametrize("step", [timedelta(0), -timedelta(microseconds=1)])
def test_1_a_row_landed_at_the_cut_is_read(reader: Reader, step: timedelta) -> None:
    """The cut is 15 seconds before the decision."""
    cut = AT - 15 * SECOND + step
    for ticker in ("GLD", "USO"):
        reader.capture(ticker, [0], fetch_end_ts=cut.isoformat())
    assert captured(reader.read()) == cut


@pytest.mark.parametrize("after", [timedelta(microseconds=1), 14 * SECOND])
def test_1_a_row_landed_after_the_cut_and_before_the_decision_is_not_read(
    reader: Reader, after: timedelta
) -> None:
    reader.both([1])
    for ticker in ("GLD", "USO"):
        reader.capture(ticker, [0], fetch_end_ts=(AT - 15 * SECOND + after).isoformat())
    read = reader.read()
    assert captured(read) == landed(1) and read.rows == {}


def test_1_rows_spelled_in_another_zone_are_one_instant(reader: Reader) -> None:
    reader.capture("GLD", [1])
    local = landed(1).astimezone(lakes.MARKET_TZ).isoformat()
    reader.capture("USO", [1], fetch_end_ts=local)
    read = reader.read()
    assert captured(read) == landed(1)
    assert read.quotes[USO].captured_at.utcoffset() == timedelta(0)
    assert read.quotes[USO].received["fetch_end_ts"] == local


def test_1_spy_never_reads_spygs_segments(reader: Reader) -> None:
    reader.capture("SPY", [2])
    reader.capture("SPYG", [1])
    read = reader.read({SPY: QuoteLimits(600, Decimal("0.02"))})
    assert read.quotes[SPY].captured_at == landed(2)
    assert reader.live.lists == [PREFIX[SPY]]
    assert len(reader.live.reads) == 1


def test_1_an_instrument_is_read_under_its_ticker_and_filed_under_its_id(reader: Reader) -> None:
    reader.capture("GLD", [1])
    row = MasterRow(DAY, SPY, "GLD", DAY, None)
    read = reader.read({SPY: LIMITS[GLD]}, master=SecurityMaster((row,)))
    assert read.faults == () and list(read.quotes) == [SPY]
    assert read.quotes[SPY].instrument == SPY
    assert read.quotes[SPY].received["ticker"] == "GLD"
    assert reader.live.lists == [PREFIX[GLD]]


def test_1_a_decision_read_ten_minutes_late_reads_what_it_would_have(reader: Reader) -> None:
    reader.both(range(-9, 12))
    read = reader.read()
    assert captured(read) == landed(1)
    # Two batches of five hold only later rows, and the third holds the newest by the decision.
    assert counts(reader) == {"GLD": 15, "USO": 15}


def test_1_a_segment_named_a_lexical_neighbour_is_opened_in_key_order(reader: Reader) -> None:
    """Keys under one prefix sort by cycle start, so the last in key order is the newest."""
    reader.both([1, 2, 3, 4, 5, 6])
    reader.read()
    keys = reader.live.bucket.list(PREFIX[GLD])
    assert reader.live.keys[:5] == keys[-5:]
    assert reader.door[0] == keys[-5:]


def _truth(rows: dict[str, set[datetime]]) -> datetime | None:
    """The newest instant by the decision every leg has, over the whole day."""
    shared = set.intersection(*({c for c in v if c <= AT} for v in rows.values()))
    return max(shared, default=None)


@pytest.mark.parametrize("seed", range(40))
def test_1_the_walk_finds_the_brute_forces_cycle_or_one_no_judge_would_take(
    reader: Reader, seed: int
) -> None:
    draw = random.Random(seed)
    late = draw.choice([0, 0, 7, 12])
    rows: dict[str, set[datetime]] = {}
    limits = {}
    for ticker, instrument in (("GLD", GLD), ("USO", USO)):
        stop = draw.choice([0, 0, 3, 15]) - late
        missing, gaps = draw.choice([0, 0.2, 0.6]), draw.choice([0, 0.2])
        rows[ticker] = set()
        for back in range(25, stop - 1, -1):
            if draw.random() < missing:
                continue
            if draw.random() < gaps:
                gap(reader.live_root, ticker, back)
            else:
                reader.capture(ticker, [back])
                rows[ticker].add(landed(back))
        limits[instrument] = QuoteLimits(draw.choice([60, 150, 400]), Decimal("0.02"))
    read = reader.read(limits)
    truth = _truth(rows)
    if read.quotes:
        assert captured(read) == truth
    else:
        oldest = min(limit.max_age for limit in limits.values())
        assert truth is None or (AT - truth).total_seconds() > oldest


# 2. Each answer, and what is kept of the rest.


def kept(read: QuoteRead) -> dict[str, int]:
    return {str(i): len(rows) for i, rows in read.rows.items()}


def test_2_a_byte_changed_in_one_download_passes_at_a_whole_second_one(reader: Reader) -> None:
    reader.both([1, 2])
    key = key_of("USO", 1)
    reader.live.plant[key] = [flipped]
    read = reader.read()
    assert captured(read) == landed(1)
    version = reader.live.bucket.read(key).version
    assert [r for r in reader.live.reads if r[0] == key] == [(key, None), (key, version)]
    # The door is asked again for that instrument's batch, and for no other.
    assert len(reader.door) == 3


def test_2_a_byte_changed_in_both_downloads_is_a_fault_naming_both_digests(
    reader: Reader,
) -> None:
    reader.both([1, 2])
    key = key_of("USO", 2)
    reader.live.plant[key] = [flipped, flipped]
    read = reader.read()
    held = reader.live.bucket.read(key)
    assert read.quotes == {}
    assert read.faults == (
        f"{key} failed twice. First it hashed to {digest(flipped(held).data)}, where the bucket "
        f"stored {held.sha256}, and then it hashed to {digest(flipped(held).data)}, where the "
        f"bucket stored {held.sha256}",
    )
    # GLD's rows are kept, and no row of USO's batch was returned.
    assert kept(read) == {"GLD": 2}
    assert counts(reader) == {"GLD": 2, "USO": 3}


def test_2_a_second_download_is_checked_against_the_first_answers_digest(
    reader: Reader, caplog: pytest.LogCaptureFixture
) -> None:
    """Another object under the same key, beside its own digest, is not the one first named."""
    reader.both([1])
    key = key_of("USO", 1)
    held = reader.live.bucket.read(key)
    other = (reader.live_root / key_of("GLD", 1)).read_bytes()
    reader.live.plant[key] = [
        flipped,
        lambda got: replace(got, data=other, sha256=digest(other), version="v9"),
    ]
    with caplog.at_level(logging.WARNING):
        (reason,) = reader.read().faults
    assert reason == (
        f"{key} failed twice. First it hashed to {digest(flipped(held).data)}, where the bucket "
        f"stored {held.sha256}, and then it hashed to {digest(other)}, where the bucket stored "
        f"{held.sha256}"
    )
    # The first failure is on the log even where a second download passes.
    (line,) = [r.getMessage() for r in caplog.records]
    assert line.startswith(f"data downloads {key} once more, because it hashed to ")


def test_2_a_second_damaged_segment_gets_a_second_download_of_its_own(reader: Reader) -> None:
    reader.both([1, 2, 3])
    for back in (1, 3):
        reader.live.plant[key_of("GLD", back)] = [flipped]
    assert captured(reader.read()) == landed(1)
    assert counts(reader) == {"GLD": 5, "USO": 3}


ANSWERS = {
    "no answer": lambda key: StoreUnavailable(key, "no answer: timed out"),
    "a checksum": lambda key: ChecksumMismatch(key, "did not match"),
}


@pytest.mark.parametrize("answer", ANSWERS)
def test_2_a_download_with_no_answer_is_tried_once_more(reader: Reader, answer: str) -> None:
    reader.both([1])
    key = key_of("GLD", 1)
    reader.live.plant[key] = [ANSWERS[answer](key)]
    assert captured(reader.read()) == landed(1)
    assert [r for r in reader.live.reads if r[0] == key] == [(key, None), (key, None)]


@pytest.mark.parametrize("answer", ANSWERS)
def test_2_a_second_failed_download_is_a_fault_naming_the_segment(
    reader: Reader, answer: str
) -> None:
    reader.both([1])
    key = key_of("GLD", 1)
    error = ANSWERS[answer](key)
    reader.live.plant[key] = [error, error]
    read = reader.read()
    said = f"could not be downloaded: {type(error).__name__}: {error}"
    assert read.faults == (f"{key} failed twice. First it {said}, and then it {said}",)
    assert kept(read) == {"USO": 1}
    assert [s.key for s in read.segments] == [key_of("USO", 1)]


def test_2_a_segment_that_fails_two_ways_has_failed_twice(reader: Reader) -> None:
    reader.both([1])
    key = key_of("GLD", 1)
    reader.live.plant[key] = [StoreUnavailable(key, "no answer"), flipped]
    read = reader.read()
    (reason,) = read.faults
    assert reason.startswith(f"{key} failed twice. First it could not be downloaded: Store")
    assert ", and then it hashed to " in reason
    reader.live.reads.clear()
    reader.live.events.clear()
    reader.live.lists.clear()
    reader.live.plant[key] = [flipped, StoreUnavailable(key, "no answer")]
    (reason,) = reader.read().faults
    assert reason.startswith(f"{key} failed twice. First it hashed to ")
    assert ", and then it could not be downloaded: StoreUnavailable" in reason


def test_2_a_segment_that_left_the_root_is_downloaded_once_more(
    reader: Reader, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader.both([1])
    key = key_of("GLD", 1)
    real = segments.load_unsealed_quotes

    def gone(times: int):
        left = [times]

        def door(*args, **kwargs):
            if key in kwargs["segments"] and left[0]:
                left[0] -= 1
                (reader.root / key).unlink()
            return real(*args, **kwargs)

        return door

    monkeypatch.setattr(segments, "load_unsealed_quotes", gone(1))
    assert captured(reader.read()) == landed(1)
    for seen in (reader.live.reads, reader.live.events, reader.live.lists):
        seen.clear()
    monkeypatch.setattr(segments, "load_unsealed_quotes", gone(2))
    (reason,) = reader.read().faults
    said = (
        f"was refused by marketlake's reader: SegmentAbsent: {key} is not under the lake root. "
        "Download it again."
    )
    assert reason == f"{key} failed twice. First it {said}, and then it {said}"


def test_2_a_listed_key_the_bucket_does_not_hold_is_a_fault_with_no_second_try(
    reader: Reader,
) -> None:
    reader.both([1])
    key = key_of("GLD", 1)
    reader.live.plant[key] = [BlobMissing(key, "404 NoSuchKey: gone")]
    read = reader.read()
    assert read.faults == (f"{key} is listed and the bucket holds none: 404 NoSuchKey: gone",)
    assert counts(reader) == {"GLD": 1, "USO": 1}
    assert kept(read) == {"USO": 1}


@pytest.mark.parametrize(
    ("change", "what"), [({"version": None}, "version"), ({"sha256": None}, "stored SHA-256")]
)
def test_2_a_segment_with_no_version_or_no_stored_sha256_is_a_fault(
    reader: Reader, change: dict, what: str
) -> None:
    reader.both([1])
    key = key_of("USO", 1)
    reader.live.plant[key] = [lambda got: replace(got, **change)]
    read = reader.read()
    assert read.faults == (f"{key} has no {what} in the bucket",)
    assert kept(read) == {"GLD": 1}
    assert [s.key for s in read.segments] == [key_of("GLD", 1)]


def test_2_a_first_failure_is_kept_beside_whatever_ends_the_second_try(
    reader: Reader, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = key_of("USO", 1)
    quiet = StoreUnavailable(key, "no answer")
    torn = (
        (BlobMissing(key, "404 NoSuchVersion"), "is listed and the bucket holds none: 404 NoSuch"),
        (lambda got: replace(got, sha256=None), "has no stored SHA-256 in the bucket"),
        (None, "was refused by marketlake's reader: UnsealedRefused"),
        ("newer", "holds rows at schema version "),
        ("full", "could not be written: OSError"),
    )
    real = Path.write_bytes

    def full(self: Path, data: bytes) -> int:
        if self == reader.root / key:
            raise OSError(28, "No space left on device", str(self))
        return real(self, data)

    for answer, said in torn:
        for ticker in ("GLD", "USO"):
            reader.capture(ticker, [1])
        for seen in (reader.live.reads, reader.live.events, reader.live.lists):
            seen.clear()
        planted = answer is not None and not isinstance(answer, str)
        reader.live.plant[key] = [quiet, answer] if planted else [quiet]
        if answer == "newer":
            capture(reader.live_root, "USO", 1, schema_version=journal.SCHEMA_VERSION + 1)
        if answer == "full":
            monkeypatch.setattr(Path, "write_bytes", full)
        if answer is None:
            path = reader.live_root / key
            path.write_bytes(path.read_bytes()[:-8])
        (reason,) = reader.read().faults
        assert reason.startswith(
            f"{key} failed twice. First it could not be downloaded: StoreUnavailable: {key}: no "
            f"answer, and then it {said}"
        )


def test_2_a_listing_with_no_answer_is_tried_once_more(
    reader: Reader, caplog: pytest.LogCaptureFixture
) -> None:
    reader.both([1])
    reader.live.plant[PREFIX[USO]] = [StoreUnavailable(PREFIX[USO], "no answer: timed out")]
    with caplog.at_level(logging.WARNING):
        assert captured(reader.read()) == landed(1)
    assert reader.live.lists == [PREFIX[GLD], PREFIX[USO], PREFIX[USO]]
    (line,) = [r.getMessage() for r in caplog.records]
    assert line.startswith(f"data lists {PREFIX[USO]} once more, because: StoreUnavailable")


def test_2_a_listing_refused_twice_is_a_fault_with_the_answer_and_opens_nothing(
    reader: Reader,
) -> None:
    reader.both([1])
    refusal = StoreUnavailable(PREFIX[GLD], "403 AccessDenied: not authorized")
    reader.live.plant[PREFIX[GLD]] = [refusal, refusal]
    read = reader.read()
    assert read.faults == (
        f"{PREFIX[GLD]} could not be listed: StoreUnavailable: {PREFIX[GLD]}: 403 AccessDenied: "
        "not authorized",
    )
    # Each other prefix is still listed, so a missing grant and an empty prefix read apart.
    assert reader.live.lists == [PREFIX[GLD], PREFIX[GLD], PREFIX[USO]]
    assert reader.live.reads == []


def test_2_a_version_past_the_pin_is_a_fault_naming_it_and_the_pin(reader: Reader) -> None:
    newer = journal.SCHEMA_VERSION + 1
    reader.both([1, 2])
    key = capture(reader.live_root, "GLD", 1, schema_version=newer)
    read = reader.read()
    assert read.faults == (
        f"{key} holds rows at schema version {newer}, which the pinned marketlake does not read, "
        "so the marketlake pin is raised",
    )
    assert kept(read) == {"USO": 2}


def test_2_a_row_of_an_older_version_reads_with_its_ledger_and_is_a_fault_without(
    reader: Reader,
) -> None:
    older = journal.SCHEMA_VERSION - 1
    for ticker in ("GLD", "USO"):
        reader.capture(ticker, [1], schema_version=older)
    read = reader.read()
    assert read.quotes == {} and len(read.faults) == 2
    assert "was refused by marketlake's reader: UnsealedRefused" in read.faults[0]
    recorded = RecordedVersion(older, datetime(2026, 8, 1, tzinfo=UTC), running_fingerprints())
    SchemaVersionLedger([recorded]).write(ledger_path(reader.root))
    for seen in (reader.live.reads, reader.live.events, reader.live.lists):
        seen.clear()
    assert captured(reader.read()) == landed(1)


def test_2_a_torn_segment_is_a_fault_naming_it(reader: Reader) -> None:
    reader.both([1, 2])
    key = key_of("USO", 2)
    path = reader.live_root / key
    path.write_bytes(path.read_bytes()[:-8])
    read = reader.read()
    (reason,) = read.faults
    assert reason.startswith(f"{key} was refused by marketlake's reader: UnsealedRefused: ")
    assert kept(read) == {"GLD": 2}


def test_2_a_day_marketlake_withholds_is_a_fault_naming_the_ticker_and_day(
    reader: Reader,
) -> None:
    reader.both([1])
    partition = f"quotes/ticker=GLD/date={DAY}.parquet"
    entry = build_entry(
        partition=partition, verdict="quarantined", check="planted", observed_at=READ_AT
    )
    append_quarantine(reader.root, entry)
    read = reader.read()
    (reason,) = read.faults
    assert reason.startswith(f"GLD on {DAY} was refused by marketlake's reader: UnsealedRefused: ")
    assert "withholds" in reason
    assert kept(read) == {"USO": 1}


def test_2_a_stray_key_under_the_prefix_is_a_fault_naming_the_ticker_and_day(
    reader: Reader,
) -> None:
    reader.both([1])
    (reader.live_root / PREFIX[GLD] / "zzz.tmp").write_bytes(b"?")
    (reason,) = reader.read().faults
    assert reason.startswith(f"GLD on {DAY} was refused by marketlake's reader: UnsealedArgument:")


def test_2_a_listed_key_no_bucket_call_takes_is_a_fault_and_never_an_error(
    reader: Reader, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader.both([1])
    real = reader.live.bucket.list
    odd = PREFIX[GLD] + "seg-9\\9.arrows"
    monkeypatch.setattr(
        reader.live.bucket, "list", lambda p: [*real(p), *([odd] if p == PREFIX[GLD] else [])]
    )
    (reason,) = reader.read().faults
    assert reason == f"{odd} could not be downloaded: ValueError"


@pytest.mark.parametrize(
    "error", [RuntimeError, ValueError, AttributeError, TypeError, FileNotFoundError]
)
def test_2_an_error_that_is_no_answer_of_the_buckets_keeps_the_rows_and_hides_its_words(
    reader: Reader, error: type[Exception], caplog: pytest.LogCaptureFixture
) -> None:
    """Such as refreshed credentials that are still expired, partway through a read."""
    reader.capture("GLD", [1])
    (key,) = reader.capture("USO", [1])
    reader.live.plant[key] = [error("/a/path/of/the/machine")]
    read = reader.read()
    assert read.faults == (f"{key} could not be downloaded: {error.__name__}",)
    assert {i: len(rows) for i, rows in read.rows.items()} == {GLD: 1}
    assert "/a/path/of/the/machine" in caplog.text and key in caplog.messages[-1]
    reader.live.plant[key] = [StoreUnavailable(key, "no answer"), error("/a/path/of/the/machine")]
    (reason,) = reader.read().faults
    assert "failed twice. First it could not be downloaded: StoreUnavailable: " in reason
    assert reason.endswith(f"and then it could not be downloaded: {error.__name__}")
    caplog.clear()
    reader.live.plant[PREFIX[USO]] = [error("/a/path/of/the/machine")]
    assert reader.read().faults == (f"{PREFIX[USO]} could not be listed: {error.__name__}",)
    assert "/a/path/of/the/machine" in caplog.text
    assert PREFIX[USO] in caplog.messages[-1]


def test_2_a_stop_is_no_fault_and_leaves_the_read(reader: Reader) -> None:
    reader.capture("GLD", [1])
    (key,) = reader.capture("USO", [1])
    for where in (PREFIX[USO], key):
        reader.live.plant[where] = [Stopped("a stop")]
        with pytest.raises(Stopped, match="a stop"):
            reader.read()


def test_2_an_error_opening_a_segment_is_a_fault_with_no_path_of_the_machine(
    reader: Reader, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader.both([1])
    real = segments.load_unsealed_quotes

    def door(ticker, *args, **kwargs):
        if ticker == "USO":
            reader.live.events.append("call")
            raise PermissionError(13, "Permission denied", str(reader.root / "a"))
        return real(ticker, *args, **kwargs)

    monkeypatch.setattr(segments, "load_unsealed_quotes", door)
    read = reader.read()
    assert read.faults == (f"USO on {DAY} could not be read: PermissionError",)
    assert kept(read) == {"GLD": 1}


def test_2_a_lake_root_that_cannot_be_written_is_a_fault(
    reader: Reader, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader.both([1])
    key = key_of("GLD", 1)
    real = Path.write_bytes

    def full(self: Path, data: bytes) -> int:
        if self == reader.root / key:
            raise OSError(28, "No space left on device", str(self))
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", full)
    read = reader.read()
    assert read.faults == (f"{key} could not be written: OSError",)
    assert kept(read) == {"USO": 1}


CHECKED = ("schema_version", "row_kind", "fetch_end_ts", "snap_ts", "ticker")
UNCHECKED = [name for name in COLUMNS if name not in CHECKED]
OTHER = {
    pa.string(): (pa.large_string(), None),
    pa.int64(): (pa.int32(), 1),
    pa.float64(): (pa.float32(), 1.0),
    pa.bool_(): (pa.string(), "True"),
}


def test_2_the_published_columns_are_the_contracts_twenty_one() -> None:
    assert dict(COLUMNS) == dict(PUBLISHED)
    assert len(COLUMNS) == 21 and len(UNCHECKED) == 16


@pytest.mark.parametrize("name", UNCHECKED)
def test_2_a_column_of_another_type_is_a_fault_naming_it_and_its_row_is_kept(
    reader: Reader, name: str
) -> None:
    schema = journal.schema_for(journal.QUOTES_SURFACE)
    place = schema.get_field_index(name)
    other, value = OTHER[schema.field(place).type]
    retyped = schema.set(place, pa.field(name, other))
    reader.capture("GLD", [1, 2])
    key = capture(reader.live_root, "USO", 1, schema=retyped, **{name: value})
    read = reader.read()
    assert read.quotes == {}
    assert read.faults == (
        f"USO on {DAY} has {name} as {other}, and marketlake publishes {COLUMNS[name]}",
    )
    assert kept(read) == {"GLD": 2, "USO": 1}
    assert read.rows[USO][0][name] == (None if value is None else str(value))
    assert key in {s.key for s in read.segments}


FAR = pa.array([0, None, 10**15], pa.int64()).cast(pa.timestamp("s"))
FAR_LISTS = pa.ListArray.from_arrays([0, 1, 2, 3], FAR)


@pytest.mark.parametrize("held", [FAR, FAR_LISTS], ids=lambda h: str(h.type))
@pytest.mark.parametrize("name", ["vendor_quote_ts", "not_published"])
def test_2_a_value_python_cannot_hold_is_noted_and_every_row_and_other_value_is_kept(
    reader: Reader, held: pa.Array, name: str
) -> None:
    schema = journal.schema_for(journal.QUOTES_SURFACE)
    place = schema.get_field_index(name)
    field = pa.field(name, held.type)
    retyped = schema.append(field) if place < 0 else schema.set(place, field)
    reader.capture("GLD", [1])
    minute = AT - MINUTE
    stamps = [landed(n).isoformat() for n in (3, 2, 1)]
    rows = [lakes.quote_row("USO", minute, bid=60.0 + n) for n in range(3)]
    plain = [{**row, "fetch_end_ts": at} for row, at in zip(rows, stamps, strict=True)]
    table = pa.Table.from_pylist([r | {name: None} for r in plain], schema=retyped)
    place = retyped.get_field_index(name)
    table = table.set_column(place, field, pa.chunked_array([held]))
    path = reader.live_root / key_of("USO", 1)
    with journal.SegmentWriter(path, retyped) as writer:
        writer.write_cycle(table)
    read = reader.read()
    if name == "not_published":
        assert captured(read) == landed(1)
        assert read.quotes[USO].received[name] == f"a {held.type} that cannot be read"
        first, second = read.rows[USO]
    else:
        assert read.faults == (
            f"USO on {DAY} has {name} as {held.type}, and marketlake publishes string",
        )
        first, second, third = read.rows[USO]
        assert third[name] == f"a {held.type} that cannot be read"
    assert [first["bid"], second["bid"]] == ["60.0", "61.0"]
    assert "1970" in first[name]
    assert second[name] == (None if held is FAR else "[None]")


@pytest.mark.parametrize("name", UNCHECKED)
def test_2_a_column_absent_from_the_segments_opened_reads_as_null(
    reader: Reader, name: str
) -> None:
    schema = journal.schema_for(journal.QUOTES_SURFACE)
    without = schema.remove(schema.get_field_index(name))
    reader.capture("USO", [1])
    capture(reader.live_root, "GLD", 1, schema=without)
    read = reader.read()
    assert captured(read) == landed(1)
    quote = read.quotes[GLD]
    assert name not in quote.received or quote.received[name] is None
    empty = {
        "bid": quote.bid,
        "ask": quote.ask,
        "last": quote.last,
        "bid_time": quote.bid_at,
        "ask_time": quote.ask_at,
        "trade_time": quote.last_at,
        "fetch_ts": quote.fetched_at,
        "total_volume": quote.volume,
        "security_status": quote.status,
        "realtime": quote.realtime,
    }
    assert {n for n, v in empty.items() if v is None} == {name} & set(empty)


@pytest.mark.parametrize("kinds", ["two rows", "stated"])
def test_2_two_kind_rows_for_the_day_are_a_fault_that_keeps_the_rows(
    reader: Reader, kinds: str
) -> None:
    reader.both([1])
    rows = KINDS.rows * (2 if kinds == "two rows" else 1)
    read = reader.read(kinds=QuoteKinds(rows))
    if kinds == "stated":
        assert captured(read) == landed(1)
        return
    assert read.faults == (f"marketlake:quotes has 2 quote-kind rows on {DAY}",)
    assert kept(read) == {"GLD": 1, "USO": 1}


def test_2_an_instrument_no_security_master_row_covers_is_a_fault_naming_it(
    reader: Reader,
) -> None:
    reader.both([1])
    row = MasterRow(DAY, GLD, "GLD", DAY, None)
    read = reader.read(master=SecurityMaster((row,)))
    assert read.faults == (f"USO has no security master row valid on {DAY}, so no ticker names it",)
    assert reader.live.reads == []


def test_2_a_ticker_no_prefix_can_hold_is_a_fault_naming_the_instrument(reader: Reader) -> None:
    master = SecurityMaster((MasterRow(DAY, GLD, "G/D", DAY, None),))
    read = reader.read({GLD: LIMITS[GLD]}, master=master)
    assert read.faults == ("GLD has no segments to list: 'G/D' is not a ticker.",)


def test_2_a_prefix_no_bucket_call_takes_is_a_fault_and_never_an_error(reader: Reader) -> None:
    master = SecurityMaster((MasterRow(DAY, GLD, "G\\D", DAY, None),))
    (reason,) = reader.read({GLD: LIMITS[GLD]}, master=master).faults
    assert reason.endswith("could not be listed: ValueError")


def test_2_a_stamp_the_reader_passes_and_no_clock_holds_is_a_fault(reader: Reader) -> None:
    reader.capture("GLD", [1])
    stamp = "0001-01-01T00:00:00+14:00"
    reader.capture("USO", [1], fetch_end_ts=stamp)
    read = reader.read()
    assert read.faults == (f"USO on {DAY} has a fetch_end_ts {stamp!r} that is no time",)
    assert kept(read) == {"GLD": 1, "USO": 1}


def test_2_an_error_that_is_no_answer_is_not_hidden(
    reader: Reader, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a process out of memory, and a mistake in the code, leave the read as themselves."""
    reader.both([1])

    def door(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(segments, "load_unsealed_quotes", door)
    with pytest.raises(MemoryError):
        reader.read()


# What a quote holds of its row.


def one(reader: Reader, **changes: object) -> Quote:
    reader.capture("GLD", [1], **changes)
    reader.capture("USO", [1])
    read = reader.read()
    assert captured(read) == landed(1)
    return read.quotes[GLD]


def test_a_quote_holds_its_row_as_received_and_each_instant_in_utc(reader: Reader) -> None:
    minute = AT - MINUTE
    quote = one(reader, bid=160.12345, last=0.00001, trade_time=1, extra='{"a": 1}')
    assert quote.bid == Decimal("160.12345")
    assert quote.excess_places == ("bid", "last")
    assert (quote.volume, quote.realtime, quote.status) == (4, True, "Normal")
    assert quote.bid_at == quote.ask_at == quote.fetched_at == minute
    assert quote.last_at == datetime(1970, 1, 1, 0, 0, 0, 1000, tzinfo=UTC)
    assert quote.captured_at == landed(1)
    assert (quote.price_kind, quote.last_kind, quote.venues) == (
        KIND.price_kind,
        KIND.last_kind,
        KIND.venues,
    )
    assert (quote.volume_counted, quote.normal_status) == (KIND.volume_counted, "Normal")
    assert (quote.max_age, quote.max_spread_share) == (90, Decimal("0.02"))
    assert quote.received["bid"] == "160.12345" and quote.received["last"] == "1e-05"
    assert quote.received["bid_time"] == str(int(minute.timestamp() * 1000))
    assert quote.received["realtime"] == "True" and quote.received["close_tag"] is None
    # Every column the row returned is kept, and not the published ones alone.
    assert quote.received["extra"] == '{"a": 1}'
    assert set(COLUMNS) < set(quote.received)
    assert quote_faults(quote, AT, AT) == ()


@pytest.mark.parametrize("name", ["bid", "ask", "last"])
@pytest.mark.parametrize(
    ("value", "text"),
    [
        (None, None),
        (float("nan"), "nan"),
        (float("inf"), "inf"),
        (1e300, "1e+300"),
        (1e-300, "1e-300"),
    ],
)
def test_a_price_that_is_no_price_is_none_beside_what_was_received(
    reader: Reader, name: str, value: float | None, text: str | None
) -> None:
    quote = one(reader, **{name: value})
    assert getattr(quote, name) is None
    assert quote.received[name] == text
    assert (quote_faults(quote, AT, AT) == ()) is (name == "last")


@pytest.mark.parametrize(
    ("value", "want"),
    [
        (None, None),
        (0, 0),
        (INT_LIMIT - 1, INT_LIMIT - 1),
        (INT_LIMIT, None),
        (-INT_LIMIT, None),
    ],
)
def test_a_volume_past_the_codecs_integer_is_none(
    reader: Reader, value: int | None, want: int | None
) -> None:
    quote = one(reader, total_volume=value)
    assert quote.volume == want
    assert quote.received["total_volume"] == (None if value is None else str(value))


@pytest.mark.parametrize("value", [None, 2**63 - 1, -(2**63), 253402300800000])
def test_a_millisecond_stamp_that_is_no_instant_is_none(reader: Reader, value: int | None) -> None:
    quote = one(reader, bid_time=value, ask_time=value, trade_time=value)
    assert (quote.bid_at, quote.ask_at, quote.last_at) == (None, None, None)
    said = None if value is None else str(value)
    assert quote_faults(quote, AT, AT) == (
        f"its bid has the stamp {said!r}, which is no instant",
        f"its ask has the stamp {said!r}, which is no instant",
    )


@pytest.mark.parametrize(
    "value", [None, "nonsense", "2012-04-10T15:39:00", "0001-01-01T00:00:00+14:00"]
)
def test_a_fetch_stamp_that_is_no_instant_is_none_and_no_fault(
    reader: Reader, value: str | None
) -> None:
    quote = one(reader, fetch_ts=value)
    assert quote.fetched_at is None
    assert quote_faults(quote, AT, AT) == ()


def test_the_limits_refuse_what_the_judge_refuses() -> None:
    assert str(QuoteLimits(90, Decimal("0.02"))) == "90 seconds and 0.02"
    for age, share in ((9, "0.02"), (90, "1")):
        with pytest.raises(ValueError, match="which is not a"):
            QuoteLimits(age, Decimal(share))
