"""A decision's quotes from a sealed day, the choice of it, and what is stored."""

from __future__ import annotations

import logging
import os
import random
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pytest
from lake.battery import build_entry
from lake.manifest import append_quarantine

from ithildinruntime.adapters import FolderBucket, HistoryClock
from ithildinruntime.core import (
    Halt,
    Snapshot,
    Stopped,
    decode,
    decode_output,
    encode,
)
from ithildinruntime.data import Mirror, QuoteLimits, load_master, sealed
from ithildinruntime.data import source as source_module
from ithildinruntime.data.cycles import COLUMNS
from ithildinruntime.data.reference import MasterRow, QuoteKinds, SecurityMaster
from ithildinruntime.data.segments import read_quotes
from ithildinruntime.runtime.loop import Item
from tests.support import lake as lakes
from tests.support.gates import World
from tests.support.sealed import (
    SCHEMA,
    close_row,
    day_rows,
    part_key,
    partition,
    row,
    seal,
    segment,
)
from tests.support.segments import (
    AT,
    DAY,
    DAYS,
    GLD,
    KINDS,
    LIMITS,
    MINUTE,
    SECOND,
    SESSION,
    SPY,
    TABLE,
    USO,
    Live,
)
from tests.test_data_quotes import source, stage, stored, two
from tests.test_data_source import Clock, Counting
from tests.test_data_stage import Reports

TICK = timedelta(microseconds=1)
BOTH = ("GLD", "USO")
CUT = AT - timedelta(seconds=15)
LAST = DAYS[DAYS.index(DAY) - 1]
WIDE = {i: QuoteLimits(3600, Decimal("0.02")) for i in (GLD, USO)}


def cap(at: datetime = AT, back: int = 1) -> datetime:
    return at - back * MINUTE + SECOND


def minute(back: int) -> str:
    """How a reason names the minute `back` minutes before the decision: its UTC instant."""
    return (AT - back * MINUTE).isoformat()


@pytest.fixture
def live(lake: Path) -> Path:
    return lake.with_name(lake.name + "-live")


@pytest.fixture
def small(built_lake: Path, tmp_path: Path) -> Path:
    """The lake with no bars, for a read that reads none."""
    return lakes.copy_lake(built_lake, tmp_path / "small", kept=())


@pytest.fixture
def day(lake: Path) -> Path:
    """The whole day before the decision, sealed, with its close."""
    for t in BOTH:
        partition(lake, t, *day_rows(t, AT - 30 * MINUTE, AT - MINUTE), close_row(t, SESSION.close))
    return lake


@pytest.fixture
def day_small(small: Path) -> Path:
    for t in BOTH:
        partition(
            small, t, *day_rows(t, AT - 30 * MINUTE, AT - MINUTE), close_row(t, SESSION.close)
        )
    return small


class Doors:
    """Counts each call of marketlake's and of the mirror's in one list, with its report."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[str] = []
        self.loads: list[tuple[str, str | None]] = []
        real, cut = sealed.load_quotes, sealed.captured_by

        def load(ticker, on, snap=None, **kw):
            self.events.append("call")
            self.loads.append((ticker, snap))
            return real(ticker, on, snap, **kw)

        def captured(table, at):
            return cut(table, at)

        monkeypatch.setattr(sealed, "load_quotes", load)
        monkeypatch.setattr(sealed, "captured_by", captured)
        link = Mirror.partitions

        def partitions(mirror, root, keys):
            keys = list(keys)
            if all(k.startswith("quotes/") for k in keys):
                assert len(keys) == 1
                self.events.append("call")
            link(mirror, root, keys)

        monkeypatch.setattr(Mirror, "partitions", partitions)

    def report(self) -> None:
        self.events.append("report")

    def ok(self) -> None:
        assert self.events[::2] == ["call"] * (len(self.events) // 2)
        assert self.events[1::2] == ["report"] * (len(self.events) // 2)

    @property
    def minutes(self) -> dict[str, list[str]]:
        return {t: [m for k, m in self.loads if k == t and m] for t in BOTH}


@pytest.fixture
def doors(monkeypatch: pytest.MonkeyPatch) -> Doors:
    return Doors(monkeypatch)


def read(
    lake: Path,
    tmp_path: Path,
    at=AT,
    session=SESSION,
    limits=WIDE,
    live=None,
    kinds=KINDS,
    report=lambda: None,
    master=None,
):
    """`read_sealed`, or the read of segments where `live` is given, with no bars."""
    mirror = Mirror(FolderBucket(lake), tmp_path / "direct")
    root = mirror.open()
    given = dict(master=master or load_master(), kinds=kinds, clock=Clock(), report=report)
    try:
        if live is not None:
            return read_quotes(FolderBucket(live), root.path, session, at, limits, **given)
        return sealed.read_sealed(mirror, root, session, at, limits, **given)
    finally:
        root.remove()


# 1. One day, two reads.


def same(a, b) -> None:
    """Item 16: equal in every field, `received` on each column both hold."""
    for i in (GLD, USO):
        one, other = a.quotes[i], b.quotes[i]
        assert replace(one, received={}) == replace(other, received={})
        both = one.received.keys() & other.received.keys()
        assert {c: one.received[c] for c in both} == {c: other.received[c] for c in both}
        extra = (one.received.keys() | other.received.keys()) - both
        assert all(q.received.get(c) is None for q in (one, other) for c in extra)


@pytest.mark.parametrize("late", [False, True])
def test_1_one_day_two_reads(lake: Path, live: Path, tmp_path: Path, late: bool) -> None:
    more = SCHEMA.append(pa.field("new_column", pa.string()))
    for t in BOTH:
        for back in range(1, 12):
            landed = AT - 20 * SECOND if late and back == 4 else None
            if late and back == 1:
                landed = AT - 50 * SECOND
            segment(live, t, AT - back * MINUTE, landed)
        segment(live, t, AT + 3 * MINUTE, schema=more, new_column="x")
    first = source(lake, tmp_path, quotes=WIDE).read(AT).snapshot
    for t in BOTH:
        seal(lake, live, t, close=SESSION.close)
    bucket = Live(live)
    reading = source(lake, tmp_path, live=bucket, quotes=WIDE).read(AT)
    later = reading.snapshot
    assert first.faults == later.faults == () == reading.halts
    (at,) = {q.captured_at for q in later.quotes.values()}
    assert at == (AT - 20 * SECOND if late else cap())
    same(first, later)
    assert "new_column" in later.quotes[GLD].received
    assert bucket.lists == bucket.reads == []
    assert (first.provisional, later.provisional) == (True, False)
    assert len(first.segments) == 10 + 2 * 0 and later.segments == ()
    held = [FolderBucket(lake).read(part_key(t)) for t in BOTH]
    assert first.partitions == ()
    assert later.partitions == tuple(
        sorted(f"{h.key}#sha256={h.sha256}#version={h.version}" for h in held)
    )
    assert first.quotes_read_at is not None and later.quotes_read_at is not None
    assert reading.sealed is True and reading.cycle_age == AT - at
    assert two(first, later).startswith("These differ: ledgers, ")
    assert "partitions" in two(first, later) and "provisional" in two(first, later)


@pytest.mark.parametrize(
    "bad",
    [{}, {"security_status": "Halted"}, {"bid": 70.0, "ask": 60.0}, {"bid": 1.0, "ask": 99.0}],
)
def test_1_a_capture_in_the_last_fifteen_seconds(
    lake: Path, live: Path, tmp_path: Path, bad
) -> None:
    wide = {i: QuoteLimits(150, Decimal("0.02")) for i in (GLD, USO)}
    for t in BOTH:
        segment(live, t, AT - MINUTE, AT - 10 * SECOND, **bad)
        segment(live, t, AT - 2 * MINUTE)
    first = source(lake, tmp_path, quotes=wide).read(AT)
    for t in BOTH:
        seal(lake, live, t, close=SESSION.close)
    later = source(lake, tmp_path, quotes=wide).read(AT)
    assert first.snapshot.provisional and not later.snapshot.provisional
    for one in (first, later):
        assert one.snapshot.quotes[GLD].captured_at == cap(back=2) and one.halts == ()
        assert one.snapshot.rows == {}
    assert first.snapshot.quotes == later.snapshot.quotes


@pytest.mark.parametrize(
    "bad", [{"security_status": "Halted"}, {"bid": 70.0, "ask": 60.0}, {"bid": 1.0, "ask": 99.0}]
)
def test_1_a_bad_capture_exactly_at_the_cut_halts_both(
    lake: Path, live: Path, tmp_path: Path, bad
) -> None:
    wide = {i: QuoteLimits(150, Decimal("0.02")) for i in (GLD, USO)}
    for t in BOTH:
        segment(live, t, AT - MINUTE, CUT, **bad)
        segment(live, t, AT - 2 * MINUTE)
    first = source(lake, tmp_path, quotes=wide).read(AT)
    for t in BOTH:
        seal(lake, live, t, close=SESSION.close)
    later = source(lake, tmp_path, quotes=wide).read(AT)
    for one in (first, later):
        assert one.snapshot.quotes[GLD].captured_at == CUT and one.halts != ()


# 2. The choice.


@pytest.mark.parametrize("listed", [(), ("GLD",), ("USO",), BOTH])
@pytest.mark.parametrize("order", [(GLD, USO), (USO, GLD)])
def test_2_the_choice(
    lake: Path, live: Path, tmp_path: Path, doors: Doors, listed: Sequence[str], order
) -> None:
    for t in BOTH:
        segment(live, t, AT - MINUTE)
        if t in listed:
            seal(lake, live, t, close=SESSION.close)
    bucket = Live(live)
    reading = source(lake, tmp_path, live=bucket, instruments=order).read(AT, doors.report)
    snap = reading.snapshot
    assert snap.provisional is (listed == ())
    assert reading.sealed is (listed != ())
    assert (bucket.lists == [] and bucket.reads == []) is (listed != ())
    assert snap.quotes_read_at is not None
    assert sorted(p.split("#")[0] for p in snap.partitions) == [part_key(t) for t in listed]
    if len(listed) == 1:
        (fault,) = snap.faults
        assert all(part_key(t) in fault for t in BOTH)
        assert doors.minutes == {"GLD": [], "USO": []} and snap.rows == {}
    else:
        assert reading.halts == ()
    if listed == ():
        assert doors.loads == []


def test_2_another_days_and_another_instruments_partition_choose_nothing(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    for t in BOTH:
        segment(live, t, AT - MINUTE)
        partition(lake, t, row(t, AT), day=LAST)
    partition(lake, "SPY", row("SPY", AT))
    partition(lake, "GLDM", row("GLDM", AT))
    reading = source(lake, tmp_path).read(AT)
    assert reading.sealed is False and reading.halts == ()
    assert reading.snapshot.partitions == ()


def test_2_the_key_is_the_tickers_and_not_the_ids(small: Path, tmp_path: Path) -> None:
    partition(small, "GLD", row("GLD", AT - MINUTE), close_row("GLD", SESSION.close))
    master = SecurityMaster((MasterRow(DAY, SPY, "GLD", DAY, None),))
    got = read(small, tmp_path, limits={SPY: LIMITS[GLD]}, master=master)
    assert got.faults == () and got.quotes[SPY].received["ticker"] == "GLD"
    mirror = Mirror(FolderBucket(small), tmp_path / "m")
    root = mirror.open()
    assert sealed.is_listed(root, DAY, [SPY], master)
    assert not sealed.is_listed(root, DAY, [SPY], load_master())


@pytest.mark.parametrize("listed", [(), ("USO",)])
def test_2_an_instrument_with_no_ticker_is_a_fault_under_either_read(
    small: Path, live: Path, tmp_path: Path, listed
) -> None:
    segment(live, "USO", AT - MINUTE)
    for t in listed:
        seal(small, live, t, close=SESSION.close)
    master = SecurityMaster(tuple(r for r in load_master().rows if r.id == USO))
    mirror = Mirror(FolderBucket(small), tmp_path / "m")
    assert sealed.is_listed(mirror.open(), DAY, [GLD, USO], master) is bool(listed)
    got = read(small, tmp_path, master=master, live=None if listed else live)
    assert got.sealed is bool(listed)
    (fault,) = got.faults
    assert "GLD has no security master row" in fault
    assert len(got.partitions) == len(listed)


@pytest.mark.parametrize(
    ("at", "reads"),
    [
        (SESSION.open - TICK, False),
        (SESSION.open, True),
        (SESSION.close - TICK, True),
        (SESSION.close, False),
    ],
)
def test_2_a_decision_inside_its_session(day: Path, tmp_path: Path, at, reads, doors) -> None:
    reading = source(day, tmp_path, stale_lag=1).read(at)
    assert reading.sealed is (True if reads else None)
    assert (reading.snapshot.quotes_read_at is not None) is reads
    assert bool(doors.loads) is reads
    assert len(reading.snapshot.partitions) == (2 if reads else 0)


def test_2_a_hand_made_partition_with_no_close(small: Path, tmp_path: Path, doors: Doors) -> None:
    partition(small, "GLD", *day_rows("GLD", AT - 5 * MINUTE, AT), close_row("GLD", SESSION.close))
    partition(small, "USO", *day_rows("USO", AT - 5 * MINUTE, AT))
    got = read(small, tmp_path, report=doors.report)
    (fault,) = got.faults
    assert fault == f"{part_key('USO')} holds no row of the session's equity close"
    assert got.quotes == {} == got.rows and len(got.partitions) == 2
    assert doors.loads == [("GLD", None), ("USO", None)] and got.calls == 2
    doors.ok()


# 3. The cut.


@pytest.mark.parametrize(("step", "kept"), [(-TICK, True), (TICK * 0, True), (TICK, False)])
def test_3_the_cut(small: Path, tmp_path: Path, step: timedelta, kept: bool) -> None:
    landed = AT - timedelta(seconds=15) + step
    for t in BOTH:
        partition(
            small,
            t,
            row(t, AT - 2 * MINUTE),
            row(t, AT - MINUTE, landed),
            close_row(t, SESSION.close),
        )
    got = read(small, tmp_path)
    assert got.quotes[GLD].captured_at == (landed if kept else cap(back=2))
    assert got.rows == ({i: got.rows[i] for i in (GLD, USO)} if kept else {})
    assert got.cycle_age == AT - got.quotes[GLD].captured_at


def test_3_rows_after_the_cut_change_nothing_else(lake: Path, tmp_path: Path, doors: Doors) -> None:
    early = {t: day_rows(t, SESSION.open, AT - 2 * MINUTE) for t in BOTH}
    snaps, readings, calls = [], [], []
    for extra in (False, True):
        for t in BOTH:
            late = []
            if extra:
                late = [row(t, AT - MINUTE, CUT + TICK, bid=1.0, ask=9999.0, ticker=t)]
                late += [row(t, AT - MINUTE, CUT + 2 * TICK, ticker="XYZ")]
                late += [
                    row(t, AT + n * MINUTE, bid=2.0, ask=2.5, realtime=False) for n in range(20)
                ]
            partition(lake, t, *early[t], *late, close_row(t, SESSION.close))
        del doors.loads[:]
        reading = source(lake, tmp_path, quotes=WIDE, clock=Clock()).read(AT)
        readings.append(reading)
        calls.append(list(doors.loads))
        snaps.append(reading.snapshot)
    ledgers = [{k: v for k, v in s.ledgers.items() if k != "manifest.jsonl"} for s in snaps]
    assert ledgers[0] == ledgers[1]
    a, b = (replace(s, partitions=(), ledgers={}) for s in snaps)
    assert encode(a) == encode(b)
    assert readings[0].cycle_age == readings[1].cycle_age and calls[0] == calls[1]


def test_3_the_judge_the_stop_and_the_ages_read_the_decision(
    small: Path, tmp_path: Path, doors: Doors
) -> None:
    """Every row of the first round is 90 seconds old at the decision plus one step."""
    for step, rounds in ((TICK * 0, 2), (TICK, 1)):
        for t in BOTH:
            other = TICK if t == "USO" else TICK * 0
            rows = [row(t, AT - 2 * MINUTE, AT - 90 * SECOND - step - other)]
            if t == "USO":
                rows = [row(t, AT - 2 * MINUTE, AT - 200 * SECOND)]
            partition(small, t, *rows, close_row(t, SESSION.close))
        del doors.loads[:]
        limits = {GLD: QuoteLimits(90, Decimal("0.02")), USO: QuoteLimits(90, Decimal("0.02"))}
        got = read(small, tmp_path, limits=limits)
        assert got.quotes == {}
        assert len(doors.minutes["GLD"]) >= 5 * rounds
        assert (len(doors.minutes["GLD"]) == 5) is (rounds == 1)
        assert len(doors.minutes["USO"]) == 5


# 4. The walk.


def build(draw: random.Random, at: datetime) -> dict[str, list]:
    """Each ticker's rows as (minute, landed or None for a gap), of one seeded day.

    No capture lands after a later minute's, the shape the walk can miss, which
    `test_4_a_late_cycle_worked_by_hand` works by hand instead.
    """
    floor = at.replace(second=0, microsecond=0)
    out: dict[str, list] = {}
    for t in BOTH:
        stop = draw.choice([0, 0, 0, 3, 15]) - draw.choice([0, 0, 12])
        missing, gaps = draw.choice([0, 0.2, 0.6]), draw.choice([0, 0.2])
        out[t] = []
        for back in range(30, stop - 1, -1):
            if draw.random() < missing:
                continue
            minute = floor - back * MINUTE
            if draw.random() < gaps:
                out[t].append((minute, None))
            else:
                # A capture lands up to 59 seconds after its minute, and never after the next.
                within = timedelta(microseconds=draw.randrange(500_000, 59_000_000))
                out[t].append((minute, minute + within))
    return out


def truth(rows, cut: datetime) -> datetime | None:
    shared = set.intersection(*({c for _, c in v if c and c <= cut} for v in rows.values()))
    return max(shared, default=None)


def planted(lake: Path, live: Path, rows) -> None:
    for t in BOTH:
        made = []
        for minute, landed in rows[t]:
            if landed is None:
                lakes.quotes_segment(live, t, minute, lakes.gap_row(t, minute))
                made.append(lakes.gap_row(t, minute))
            else:
                segment(live, t, minute, landed)
                made.append(row(t, minute, landed))
        partition(lake, t, *made, close_row(t, SESSION.close))


@pytest.mark.parametrize("off", [0, 7, 14, 16, 31])
@pytest.mark.parametrize("seed", range(int(os.environ.get("COLD_SEEDS", "40"))))
def test_4_the_walk_finds_the_brute_forces_cycle(
    small: Path, live: Path, tmp_path: Path, seed: int, off: int
) -> None:
    draw = random.Random(seed * 100 + off)
    at = AT + off * SECOND
    rows = build(draw, at)
    planted(small, live, rows)
    limits = {i: QuoteLimits(draw.choice([60, 150, 400]), Decimal("0.02")) for i in (GLD, USO)}
    cut = at - timedelta(seconds=15)
    oldest = timedelta(seconds=min(x.max_age for x in limits.values()))
    found = {}
    for name, got, by in (
        ("sealed", read(small, tmp_path, at, limits=limits), cut),
        ("segments", read(small, tmp_path, at, limits=limits, live=live), cut),
    ):
        want = truth(rows, by)
        if got.quotes:
            (found[name],) = {q.captured_at for q in got.quotes.values()}
            assert found[name] == want
        else:
            assert want is None or at - want > oldest
    if len(found) == 2:
        assert found["sealed"] == found["segments"]


def test_4_a_late_cycle_worked_by_hand(small: Path, live: Path, tmp_path: Path) -> None:
    """15:36's cycle landed at 15:39:40, after 15:38's and 15:39's, and 15:34's a second later."""
    rows = {
        t: [
            (AT - 6 * MINUTE, AT - 19 * SECOND),
            (AT - 4 * MINUTE, AT - 20 * SECOND),
            (AT - 2 * MINUTE, cap(back=2)),
            (AT - MINUTE, cap(back=1)),
        ]
        for t in BOTH
    }
    planted(small, live, rows)
    got = read(small, tmp_path)
    assert got.quotes[GLD].captured_at == datetime(2012, 4, 10, 19, 39, 40, tzinfo=UTC)
    # Segments skip the empty minutes, so its newest five hold 15:34's too.
    other = read(small, tmp_path, live=live)
    assert other.quotes[GLD].captured_at == datetime(2012, 4, 10, 19, 39, 41, tzinfo=UTC)


@pytest.mark.parametrize(
    ("on", "at", "first"),
    [
        (date(2011, 11, 25), None, "12:39"),  # a half day, close-20
        (date(2012, 2, 15), None, "15:39"),  # a winter day
        (DAY, SESSION.open + 14 * SECOND, None),
        (DAY, SESSION.open + 15 * SECOND, "09:30"),
        (DAY, SESSION.open + 3 * MINUTE, "09:32"),
        (DAY, AT + 14 * SECOND, "15:39"),
        (DAY, AT + 15 * SECOND, "15:40"),
    ],
)
def test_4_the_minutes_read(small: Path, tmp_path: Path, doors: Doors, on, at, first) -> None:
    session = TABLE.session(on)
    at = at or session.close - 20 * MINUTE
    for t in BOTH:
        rows = day_rows(t, session.open, at.replace(second=0) - MINUTE)
        # A row only one leg has at each instant, so the walk goes on to its end.
        if t == "USO":
            rows = [
                row(
                    t,
                    datetime.fromisoformat(r["snap_ts"]),
                    datetime.fromisoformat(r["fetch_end_ts"]) + TICK,
                )
                for r in rows
            ]
        partition(small, t, *rows, close_row(t, session.close), day=on)
    day_long = {i: QuoteLimits(10**6, Decimal("0.02")) for i in (GLD, USO)}
    got = read(small, tmp_path, at, session, day_long, report=doors.report)
    doors.ok()
    (fault,) = got.faults
    assert fault.startswith(f"no capture by {(at - timedelta(seconds=15)).isoformat()} ")
    assert part_key("GLD", on) in fault and part_key("USO", on) in fault
    for t in BOTH:
        assert (doors.minutes[t][:1] or [None]) == [first]
        assert doors.minutes[t] == sorted(doors.minutes[t], reverse=True)
        if first:
            assert doors.minutes[t][-1] == "09:30"
    assert got.calls == len(doors.loads) == 2 + sum(map(len, doors.minutes.values()))


@pytest.mark.parametrize(("common", "reads"), [(1, 5), (5, 5), (6, 10), (10, 10), (11, 15)])
def test_4_five_at_a_time_to_the_newest_common(
    small: Path, tmp_path: Path, doors: Doors, common: int, reads: int
) -> None:
    for t in BOTH:
        rows = []
        for back in range(1, 13):
            off = SECOND if t == "USO" and back != common else 0 * SECOND
            rows.append(row(t, AT - back * MINUTE, cap(back=back) + off))
        partition(small, t, *rows, close_row(t, SESSION.close))
    got = read(small, tmp_path, at=AT - TICK)
    assert got.quotes[GLD].captured_at == cap(back=common)
    assert {t: len(m) for t, m in doors.minutes.items()} == {"GLD": reads, "USO": reads}


def test_4_one_leg_that_stopped(small: Path, tmp_path: Path, doors: Doors) -> None:
    partition(
        small, "GLD", *day_rows("GLD", SESSION.open, AT - MINUTE), close_row("GLD", SESSION.close)
    )
    partition(
        small,
        "USO",
        *day_rows("USO", SESSION.open, AT - 200 * MINUTE),
        close_row("USO", SESSION.close),
    )
    got = read(small, tmp_path, limits=LIMITS)
    # USO's rows are all past its maximum once a round gives one, and GLD stops at its second.
    assert len(doors.minutes["GLD"]) == 10
    assert len(doors.minutes["USO"]) == 200
    assert got.quotes == {} and len(got.faults) == 1


# 5. Each answer.


def withheld(lake: Path, t: str) -> str:
    entry = build_entry(
        partition=part_key(t), verdict="quarantined", check="planted", observed_at=AT
    )
    append_quarantine(lake, entry)
    lakes.record(lake, "quarantine.jsonl")
    return "is quarantined in marketlake"


def good(t: str, *rows, close: bool = True, schema=SCHEMA) -> dict:
    made = [row(t, AT - 2 * MINUTE), row(t, AT - MINUTE), *rows]
    return {"rows": made + ([close_row(t, SESSION.close)] if close else []), "schema": schema}


def answers(lake: Path, t: str, what: str) -> tuple[str, int, int]:
    """Plant one answer for `t`: what its fault says, its rows kept, its minutes read."""
    made = good(t)
    said, kept, minutes = "", 0, 0
    if what == "unreadable":
        bad = row(t, AT - 3 * MINUTE, snap_ts="never")
        made = good(t, bad)
        said = f"{part_key(t)} has no row at {minute(3)}, and 1 snap_ts values in it cannot be read"
        kept, minutes = 2, 3
    elif what == "no close":
        made, said = good(t, close=False), "holds no row of the session's equity close"
    elif what == "no instant":
        made = good(t, row(t, AT - 2 * MINUTE, fetch_end_ts="never"))
        said, kept, minutes = f"could not be read at {minute(2)}: UnsealedRefused", 1, 2
    elif what == "ticker":
        made = good(t)
        made["rows"][0] = row(t, AT - 2 * MINUTE, ticker="XYZ")
        said, kept, minutes = f"holds a row of ticker 'XYZ' at {minute(2)}", 2, 2
    elif what == "type":
        place = SCHEMA.get_field_index("bid_size")
        made = good(t, schema=SCHEMA.set(place, pa.field("bid_size", pa.int32())))
        said, kept, minutes = "has bid_size as int32", 1, 1
    elif what == "twice":
        made = good(t, row(t, AT - MINUTE))
        said, kept, minutes = "2 rows captured at", 3, 5
    partition(lake, t, *made["rows"], schema=made["schema"])
    if what == "withheld":
        said = withheld(lake, t)
    elif what == "absent":
        (lake / part_key(t)).unlink()
        said = "the data is not ready"
    elif what == "differs":
        data = bytearray((lake / part_key(t)).read_bytes())
        data[len(data) // 2] ^= 1
        lakes.rewrite(lake, part_key(t), bytes(data))
        said = "records"
    return said, kept, minutes


@pytest.mark.parametrize("bad", BOTH)
@pytest.mark.parametrize(
    "what",
    [
        "unreadable",
        "withheld",
        "no close",
        "no instant",
        "absent",
        "differs",
        "ticker",
        "type",
        "twice",
    ],
)
def test_5_each_answer(
    lake: Path, live: Path, tmp_path: Path, doors: Doors, bad: str, what: str
) -> None:
    (other,) = set(BOTH) - {bad}
    partition(lake, other, *good(other)["rows"])
    said, kept, minutes = answers(lake, bad, what)
    made, _ = stage(lake, tmp_path, live=Live(live))
    result = made(Item(AT, "data"), Reports())
    snap = stored(tmp_path)
    (fault,) = snap.faults
    assert said in fault and str(tmp_path) not in result.halt.reason
    if what in ("unreadable", "withheld", "no close", "no instant", "absent", "differs", "ticker"):
        assert part_key(bad) in fault
    assert result.halt.cause == result.outputs[0]
    assert result.halt.reason == "; ".join(
        [fault, *(f"{i} has no quote in the snapshot" for i in (GLD, USO))]
    )
    assert snap.provisional is False and snap.quotes == {} and snap.quotes_read_at is not None
    held = len(snap.partitions)
    assert held == (1 if what in ("absent", "differs") else 2)
    got = {str(i): len(r) for i, r in snap.rows.items()}
    walked = minutes > 0
    assert got == ({other: 2} | ({bad: kept} if kept else {}) if walked else {})
    assert {t: len(m) for t, m in doors.minutes.items()} == {
        other: 5 if walked else 0,
        bad: minutes,
    }
    assert sum(m is None for _, m in doors.loads) == (2 if held == 2 else 0)


def test_5_errors_hide_their_words_and_a_stop_and_memory_leave(
    day_small: Path, tmp_path: Path, monkeypatch, caplog
) -> None:
    for where in ("load_quotes", "captured_by"):
        for error in (OSError(f"{tmp_path}/secret"), pa.ArrowInvalid("x")):
            with monkeypatch.context() as patch:
                patch.setattr(sealed, where, _raises(error))
                caplog.clear()
                got = read(day_small, tmp_path)
            assert len(got.faults) == 2
            for fault, t in zip(got.faults, BOTH, strict=True):
                assert part_key(t) in fault and fault.endswith(type(error).__name__)
                if where == "captured_by":
                    assert minute(1) in fault
                assert str(tmp_path) not in fault
            assert str(error) in caplog.text
        for error in (MemoryError(), Stopped(), pa.ArrowMemoryError("x")):
            with monkeypatch.context() as patch:
                patch.setattr(sealed, where, _raises(error))
                with pytest.raises(type(error)):
                    read(day_small, tmp_path)
    for error in (RuntimeError(f"{tmp_path}/x"), OSError("full")):
        with monkeypatch.context() as patch:
            patch.setattr(Mirror, "partitions", _raises(error))
            got = read(day_small, tmp_path)
        assert [f.endswith(type(error).__name__) for f in got.faults] == [True, True]
        assert got.partitions == () and got.calls == 0
    for error in (MemoryError(), Stopped()):
        with monkeypatch.context() as patch:
            patch.setattr(Mirror, "partitions", _raises(error))
            with pytest.raises(type(error)):
                read(day_small, tmp_path)


def _raises(error: BaseException):
    def call(*args, **kwargs):
        raise error

    return call


def test_5_two_kinds_rows_are_a_fault(day_small: Path, tmp_path: Path) -> None:
    got = read(day_small, tmp_path, kinds=QuoteKinds(KINDS.rows * 2))
    (fault,) = got.faults
    assert "quote-kind rows" in fault and len(got.rows[GLD]) == 5


# 6. Through the stage.


@pytest.mark.parametrize("seal_first", [False, True])
def test_6_a_redelivery_from_the_other_read(
    lake: Path, live: Path, tmp_path: Path, seal_first: bool
) -> None:
    for t in BOTH:
        segment(live, t, AT - MINUTE)
        segment(live, t, AT - 2 * MINUTE)
    made, alarms = stage(lake, tmp_path)
    if not seal_first:
        made(Item(AT, "data"), Reports())
    manifest = (lake / "manifest.jsonl").read_bytes()
    for t in BOTH:
        seal(lake, live, t, close=SESSION.close)
    if seal_first:
        made(Item(AT, "data"), Reports())
        (lake / "manifest.jsonl").write_bytes(manifest)
    again = made(Item(AT, "data"), Reports())
    assert again.halt is None and len(again.outputs) == 2
    (reason,) = alarms.reasons
    assert "partitions" in reason and "provisional" in reason
    assert stored(tmp_path).provisional is not seal_first
    assert stored(tmp_path, 2).provisional is seal_first


def test_6_a_partition_restated_and_the_bytes_of_a_backtest(day: Path, tmp_path: Path) -> None:
    for n in (1, 2):
        made, _ = stage(day, tmp_path / str(n), clock=HistoryClock())
        result = made(Item(AT, "data"), Reports())
        assert result.halt is None
        path = tmp_path / str(n) / "store" / result.outputs[0].key
        if n == 1:
            first = path.read_bytes()
        assert path.read_bytes() == first
    assert decode_output(first, Snapshot).header.format == 3
    made, alarms = stage(day, tmp_path / "r")
    made(Item(AT, "data"), Reports())
    for t in BOTH:
        partition(day, t, *day_rows(t, AT - 31 * MINUTE, AT - MINUTE), close_row(t, SESSION.close))
    assert made(Item(AT, "data"), Reports()).halt is None
    (reason,) = alarms.reasons
    assert "These differ: ledgers, partitions" in reason


def test_6_each_sample_reads_back(day: Path, live: Path, tmp_path: Path) -> None:
    snap = source(day, tmp_path, quotes=WIDE).read(AT).snapshot
    assert snap.partitions and snap.rows and decode(encode(snap), Snapshot) == snap
    for changes in (
        {"provisional": True},
        {"quotes_read_at": None},
    ):
        with pytest.raises(ValueError, match="snapshot"):
            replace(snap, **changes)


# 7. The gate.


@pytest.mark.parametrize("now", [SESSION.close + TICK, SESSION.close + timedelta(days=1)])
def test_7_the_gate_refuses_after_the_close(day: Path, tmp_path: Path, now) -> None:
    made, _ = stage(day, tmp_path)
    assert made(Item(AT, "data"), Reports()).halt is None
    world = World().at(snapshot=stored(tmp_path), now=now)
    for call in (world.check, world.loss):
        with pytest.raises(Halt, match="not from the decision at"):
            call()


# 8. The order.


def test_8_the_link_comes_after_the_bars(day: Path, tmp_path: Path, monkeypatch) -> None:
    order: list[str] = []
    bars = source_module.load_bars

    def load(*a, **k):
        order.append("bars")
        return bars(*a, **k)

    monkeypatch.setattr(source_module, "load_bars", load)
    counting = Counting(FolderBucket(day))
    real = counting.read

    def bucket_read(key, version=None):
        if key.startswith("quotes/"):
            order.append("quotes")
        return real(key, version)

    counting.read = bucket_read  # type: ignore[method-assign]
    made = source(day, tmp_path, bucket=counting)
    snap = made.read(AT).snapshot
    assert order.count("bars") == 80 and "bars" not in order[order.index("quotes") :]
    assert not any(k.startswith("quotes/") for k in snap.ledgers)
    made.read(AT + MINUTE)
    assert order.count("bars") == 80


# 9. A backtest of an unlisted day.


@pytest.mark.parametrize(("landed", "read"), [(AT - TICK, False), (CUT, True)])
def test_9_a_backtest_of_an_unlisted_day_reads_segments_under_the_same_cut(
    lake: Path, live: Path, tmp_path: Path, landed: datetime, read: bool
) -> None:
    for t in BOTH:
        segment(live, t, AT - 2 * MINUTE)
        segment(live, t, AT - MINUTE, landed)
    clock = HistoryClock()
    clock.wait_until(AT, None)  # type: ignore[arg-type]
    snap = source(lake, tmp_path, clock=clock).read(AT).snapshot
    assert snap.provisional and snap.partitions == ()
    assert snap.quotes[GLD].captured_at == (landed if read else cap(back=2))
    assert bool(snap.rows) is read


def test_19_the_reading_and_the_log(day: Path, tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.INFO):
        reading = source(day, tmp_path).read(AT)
    assert (reading.sealed, reading.quote_calls, reading.segments_listed) == (True, 12, 0)
    (line,) = [r.getMessage() for r in caplog.records if "load_quotes" in r.getMessage()]
    assert "12 calls" in line and all(p in line for p in reading.snapshot.partitions)
    assert "59.000 seconds old" in line
    assert f"with the cut at {(AT - 15 * SECOND).isoformat()}" in line
    assert line.endswith("A decision before the seal may have read another row")


# What no acceptance line holds.


def test_x_the_clock_is_read_after_the_last_answer(
    day_small: Path, tmp_path: Path, doors: Doors
) -> None:
    class Asking(Clock):
        def now(self):
            doors.events.append("clock")
            return super().now()

    mirror = Mirror(FolderBucket(day_small), tmp_path / "direct")
    root = mirror.open()
    sealed.read_sealed(
        mirror,
        root,
        SESSION,
        AT,
        WIDE,
        master=load_master(),
        kinds=KINDS,
        clock=Asking(),
        report=doors.report,
    )
    assert doors.events.index("clock") == len(doors.events) - 1


def test_x_a_row_of_another_ticker_past_the_cut_is_no_fault(small: Path, tmp_path: Path) -> None:
    for t in BOTH:
        late = row(t, AT - MINUTE, CUT + TICK, ticker="XYZ")
        partition(small, t, row(t, AT - MINUTE), late, close_row(t, SESSION.close))
    assert read(small, tmp_path).faults == ()


def test_x_each_rule_of_the_record(day: Path, live: Path, tmp_path: Path) -> None:
    for t in BOTH:
        segment(live, t, AT - MINUTE)
    for t in BOTH:
        (day / part_key(t)).unlink()
    snap = source(day, tmp_path).read(AT).snapshot
    bare = replace(snap, partitions=(), quotes={}, rows={}, faults=(), quotes_read_at=None)
    with pytest.raises(ValueError, match="quotes_read_at"):
        replace(bare, provisional=True)
    from ithildinruntime.core import Segment

    with pytest.raises(ValueError, match="is provisional"):
        replace(snap, segments=(Segment("a", "v", "0" * 64),))


def test_6_a_first_capture_of_the_format_before(day: Path, tmp_path: Path) -> None:
    made, _ = stage(day, tmp_path)
    result = made(Item(AT, "data"), Reports())
    path = tmp_path / "store" / result.outputs[0].key
    data = path.read_bytes().replace(b'"format":3', b'"format":2')
    i = data.index(b'"partitions":[')
    data = data[:i] + data[data.index(b"],", i) + 2 :]
    path.write_bytes(data)
    again = made(Item(AT, "data"), Reports())
    assert "is no snapshot the codec reads" in again.halt.reason
    assert "'partitions'" in again.halt.reason


# 1. One sealed quote, every field as a literal, so a change to the shared conversion fails here.


def test_1_one_sealed_quote_in_every_field(small: Path, tmp_path: Path) -> None:
    from ithildinruntime.core import Quote

    for t in BOTH:
        partition(small, t, row(t, AT - MINUTE), close_row(t, SESSION.close))
    got = read(small, tmp_path)
    at, landed = (
        datetime(2012, 4, 10, 19, 39, tzinfo=UTC),
        datetime(2012, 4, 10, 19, 39, 1, tzinfo=UTC),
    )
    held = {k: v for k, v in got.quotes[GLD].received.items() if v is not None}
    assert held == {
        "snap_ts": "2012-04-10T19:39:00+00:00",
        "fetch_ts": "2012-04-10T19:39:00+00:00",
        "fetch_end_ts": "2012-04-10T19:39:01+00:00",
        "vendor_quote_ts": "2012-04-10T19:39:00+00:00",
        "ticker": "GLD",
        "bid": "160.0",
        "ask": "160.5",
        "last": "1.0",
        "bid_size": "1",
        "ask_size": "2",
        "last_size": "3",
        "bid_time": "1334086740000",
        "ask_time": "1334086740000",
        "trade_time": "1334086740000",
        "total_volume": "4",
        "security_status": "Normal",
        "realtime": "True",
        "row_kind": "data",
        "suspect": "False",
        "schema_version": "2",
    }
    assert got.quotes[GLD] == Quote(
        instrument=GLD,
        bid=Decimal("160.0"),
        ask=Decimal("160.5"),
        last=Decimal("1.0"),
        volume=4,
        bid_at=at,
        ask_at=at,
        last_at=at,
        fetched_at=at,
        captured_at=landed,
        realtime=True,
        status="Normal",
        price_kind="consolidated quote",
        last_kind="consolidated sale",
        venues="consolidated",
        volume_counted="shares",
        normal_status="Normal",
        max_age=3600,
        max_spread_share=Decimal("0.02"),
        received=got.quotes[GLD].received,
        excess_places=(),
    )
    assert len(got.quotes[GLD].received) == 75


def test_1_past_a_maximum_both_reads_halt(lake: Path, live: Path, tmp_path: Path) -> None:
    tight = {i: QuoteLimits(60, Decimal("0.02")) for i in (GLD, USO)}
    for t in BOTH:
        segment(live, t, AT - 2 * MINUTE)
    first = source(lake, tmp_path, quotes=tight).read(AT)
    for t in BOTH:
        seal(lake, live, t, close=SESSION.close)
    later = source(lake, tmp_path, quotes=tight).read(AT)
    assert first.snapshot.provisional and not later.snapshot.provisional
    for one in (first, later):
        assert one.snapshot.quotes[GLD].captured_at == cap(back=2)
        assert any("past the written maximum of 60" in h for h in one.halts)
    assert first.halts == later.halts


# 4. A row stamped off its minute is read before the seal and not found after it.


def test_4_a_row_stamped_off_its_minute_worked_by_hand(
    small: Path, live: Path, tmp_path: Path
) -> None:
    """15:39's cycle wrote 15:39:30 as its minute. `load_quotes` at 15:39 answers no row."""
    off = AT - MINUTE + 30 * SECOND
    rows = {
        t: [row(t, AT - 2 * MINUTE), row(t, AT - MINUTE, off + SECOND, snap_ts=off.isoformat())]
        for t in BOTH
    }
    for t in BOTH:
        segment(live, t, AT - 2 * MINUTE)
        lakes.quotes_segment(live, t, AT - MINUTE, rows[t][1])
        partition(small, t, *rows[t], close_row(t, SESSION.close))
    before = read(small, tmp_path, live=live)
    after = read(small, tmp_path)
    assert before.quotes[GLD].captured_at == off + SECOND
    assert after.quotes[GLD].captured_at == cap(back=2)
    assert before.faults == after.faults == ()


# 5. The mirror's own refusals, which a bar's halt words the same way.


class Refusing:
    """The lake's bucket, which answers a quotes partition its own way."""

    def __init__(self, lake: Path, how: str) -> None:
        self.bucket, self.how = FolderBucket(lake), how

    def list(self, prefix: str):
        return self.bucket.list(prefix)

    def read(self, key: str, version: str | None = None):
        from ithildinruntime.core import StoreError

        got = self.bucket.read(key, version)
        if not key.startswith("quotes/ticker=USO"):
            return got
        if self.how == "refuses":
            raise StoreError(key, "the bucket refused")
        return replace(got, **{self.how: None})


@pytest.mark.parametrize(
    ("how", "said"),
    [
        ("refuses", "could not be downloaded: StoreError: "),
        ("version", "has no version in the bucket, so nothing read from it can name one"),
        ("sha256", "has no SHA-256 stored in the bucket"),
    ],
)
def test_5_a_bucket_that_refuses_or_a_file_with_no_version_or_hash(
    day: Path, tmp_path: Path, how: str, said: str
) -> None:
    made = source(day, tmp_path, bucket=Refusing(day, how))
    snap = made.read(AT).snapshot
    (fault,) = snap.faults
    assert fault.startswith(part_key("USO")) and said in fault
    assert [p.split("#")[0] for p in snap.partitions] == [part_key("GLD")]
    assert snap.quotes == {} and snap.rows == {} and snap.quotes_read_at is not None


def _drop(t: pa.Table, n: str) -> pa.Table:
    return t.drop_columns([n])


def _null(t: pa.Table, n: str) -> pa.Table:
    i = t.schema.get_field_index(n)
    return t.set_column(i, t.schema.field(n), pa.nulls(t.num_rows, t.schema.field(n).type))


def _text(t: pa.Table, n: str) -> pa.Table:
    i = t.schema.get_field_index(n)
    return t.set_column(i, n, pa.array(["x"] * t.num_rows, pa.string()))


def _float(t: pa.Table, n: str) -> pa.Table:
    i = t.schema.get_field_index(n)
    return t.set_column(i, n, pa.array([float("nan")] * t.num_rows, pa.float64()))


def _no_python(t: pa.Table, n: str) -> pa.Table:
    """A time past Python's last year."""
    i = t.schema.get_field_index(n)
    big = pa.array([2**62] * t.num_rows, pa.int64()).cast(pa.timestamp("s"))
    return t.set_column(i, n, big)


def _twice(t: pa.Table, n: str) -> pa.Table:
    return t.append_column(n, t.column(n))


SHAPES = {
    "dropped": _drop,
    "nulled": _null,
    "retyped": _text,
    "a float": _float,
    "past Python": _no_python,
    "doubled": _twice,
}


@pytest.mark.parametrize("bad", BOTH)
@pytest.mark.parametrize("shape", sorted(SHAPES))
@pytest.mark.parametrize("name", [*COLUMNS, "cusip"])
def test_5_no_shape_of_answer_leaves_the_read(
    day_small: Path, tmp_path: Path, monkeypatch, bad: str, shape: str, name: str
) -> None:
    """Each column of a minute's answer changed: the read returns, and keeps what it holds."""
    real = sealed.load_quotes

    def changed(ticker, on, snap=None, **kw):
        out = real(ticker, on, snap, **kw)
        if snap is None or ticker != bad or name not in out.schema.names or not out.num_rows:
            return out
        return SHAPES[shape](out, name)

    monkeypatch.setattr(sealed, "load_quotes", changed)
    got = read(day_small, tmp_path)
    assert len(got.partitions) == 2
    assert got.calls >= 2 + 5
    assert all(str(tmp_path) not in f for f in got.faults)
    if got.faults:
        assert got.quotes == {}
    else:
        assert set(got.quotes) == {GLD, USO}


# 6. Two samples that read back, one of each read, and two decisions side by side.


def test_6_a_sample_of_each_read_reads_back_in_every_field(
    lake: Path, live: Path, tmp_path: Path
) -> None:
    for t in BOTH:
        segment(live, t, AT - MINUTE, CUT + TICK)
        segment(live, t, AT - 2 * MINUTE)
        segment(live, t, AT - 3 * MINUTE)
    provisional = source(lake, tmp_path, quotes=WIDE).read(AT).snapshot
    for t in BOTH:
        seal(lake, live, t, close=SESSION.close)
    sealed_one = source(lake, tmp_path, quotes=WIDE).read(AT).snapshot
    assert provisional.provisional and provisional.segments and provisional.rows
    assert sealed_one.partitions and sealed_one.rows and not sealed_one.provisional
    for snap in (provisional, sealed_one):
        assert snap.quotes and snap.quotes_read_at is not None
        assert decode(encode(snap), Snapshot) == snap


def test_6_two_decisions_side_by_side_over_one_mirror(day: Path, tmp_path: Path) -> None:
    import threading

    made = source(day, tmp_path, quotes=WIDE)
    alone = {at: encode(made.read(at).snapshot) for at in (AT, AT - 5 * MINUTE)}
    got: dict[datetime, bytes] = {}

    def one(at: datetime) -> None:
        got[at] = encode(made.read(at).snapshot)

    threads = [threading.Thread(target=one, args=(at,)) for at in alone]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert got == alone
