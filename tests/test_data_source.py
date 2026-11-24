"""The data module's point-in-time read, over a lake built from quantitative-trading's vintages.

Each test reads its own copy of the lake through the folder bucket and a mirror
in a second directory, as tests/support/lake.py says. Each names the mutant it
fails under, and each boundary is tested one step below, at and above it, with
its expected answer taken from the rule #5 writes. The numbers in each test's
first line are the acceptance tests' numbers on #5.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import json
import threading
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from lake.battery import build_entry
from lake.manifest import append_quarantine

from ithildinruntime.adapters import FolderBucket
from ithildinruntime.core import (
    BarStatus,
    BlobMissing,
    BucketObject,
    Halt,
    InstrumentId,
    StoreUnavailable,
    from_received,
    load_sessions,
)
from ithildinruntime.data import DataSettings, LakeSource, Mirror, source, spikes
from ithildinruntime.data.reference import (
    CAPTURE_SOURCE,
    Kind,
    KindRow,
    MasterRow,
    PriceKinds,
    SecurityMaster,
    load_master,
)
from tests.support import lake as lakes

GLD, USO = InstrumentId("GLD"), InstrumentId("USO")
TABLE = load_sessions()
DAYS = sorted(TABLE.sessions)
LAST = lakes.LAST_SESSION
FIRST = lakes.FIRST_SESSION
READ_AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def close(day: date) -> datetime:
    return TABLE.session(day).close


def before(day: date, n: int = 1) -> date:
    return DAYS[DAYS.index(day) - n]


class Clock:
    def __init__(self, at: datetime = READ_AT) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return 0.0

    def offset(self) -> timedelta:
        return timedelta(0)

    def sync_age(self) -> timedelta:
        return timedelta(0)

    def wait_until(self, at, report) -> None:
        pass


class Timer:
    def now(self) -> datetime:
        return READ_AT

    def monotonic(self) -> float:
        return 0.0

    def after(self, seconds: float) -> object:
        raise NotImplementedError


class Counting:
    """A bucket that counts each key it is asked for, and can refuse one or call back."""

    def __init__(self, bucket) -> None:
        self.bucket = bucket
        self.reads: list[str] = []
        self.refuse: dict[str, Exception] = {}
        self.on_read: dict[str, object] = {}

    def read(self, key: str) -> BucketObject:
        self.reads.append(key)
        if key in self.refuse:
            raise self.refuse[key]
        hook = self.on_read.pop(key, None)
        if hook is not None:
            hook()  # type: ignore[operator]
        return self.bucket.read(key)


def settings(**changes) -> DataSettings:
    made = DataSettings(
        instruments=(GLD, USO),
        lookback=40,
        first_sessions={GLD: FIRST, USO: FIRST},
        stale_lag=1,
        spike_multiple=Decimal(100),
    )
    return replace(made, **changes)


def make(bucket_root: Path, tmp_path: Path, bucket=None, **changes) -> LakeSource:
    kinds = changes.pop("kinds", None)
    master = changes.pop("master", None)
    return LakeSource(
        Mirror(bucket or FolderBucket(bucket_root), tmp_path / "mirror"),
        settings(**changes),
        clock=Clock(),
        timer=Timer(),
        kinds=kinds,
        master=master,
    )


def unlist(root: Path, key: str) -> None:
    """Take a partition out of the lake and its manifest, as a session never fetched."""
    (root / key).unlink()
    manifest = root / "manifest.jsonl"
    lines = manifest.read_text().splitlines(keepends=True)
    manifest.write_text("".join(line for line in lines if f'"{key}"' not in line))


# 1. The values.


def test_1_a_snapshot_at_the_close_holds_each_bar_as_received(
    lake: Path, tmp_path: Path, vintages: Path
) -> None:
    reading = make(lake, tmp_path).read(close(LAST))
    snap = reading.snapshot
    assert snap.status == {GLD: BarStatus.FRESH, USO: BarStatus.FRESH}
    gld = snap.bars[GLD]["1d"]
    assert len(gld) == 40 and len(snap.bars[USO]["1d"]) == 40
    assert [b.session for b in gld] == DAYS[DAYS.index(LAST) - 39 : DAYS.index(LAST) + 1]
    newest = gld[-1]
    assert newest.close == Decimal("159.37")
    assert (newest.origin, newest.basis, newest.volume) == ("imported", "adjusted", None)
    assert newest.end == close(LAST) == datetime(2012, 4, 9, 20, 0, tzinfo=UTC)
    assert newest.knowable_at == newest.end
    assert newest.source == "quantitative-trading:data/inputdata_etf/gld.csv"
    assert newest.source_as_of == date(2012, 4, 10)
    assert newest.fetched is None and newest.fetched_at is None
    assert newest.first_seen == READ_AT
    assert newest.excess_places == ()
    key = lakes.partition_key("GLD", LAST)
    sha = hashlib.sha256((lake / key).read_bytes()).hexdigest()
    assert newest.vintage_id == f"{key}#sha256={sha}#version={sha}"
    assert set(snap.ledgers) == {
        "manifest.jsonl",
        "quarantine.jsonl",
        "actions/corporate_actions.jsonl",
        "reference/schema_versions.parquet",
    }
    # Every close is the vintage's own value, read as marketlake stores it.
    for name, instrument in (("gld.csv", GLD), ("uso.csv", USO)):
        held = {b.session: b.close for b in snap.bars[instrument]["1d"]}
        with open(vintages / name, newline="") as handle:
            rows = {date.fromisoformat(r[0]): r[1] for r in list(csv.reader(handle))[3:]}
        assert len(held) == 40
        assert all(held[d] == from_received(float(rows[d])) for d in held)


def test_1_a_contradiction_flag_halts_beside_the_unchanged_price(
    lake: Path, tmp_path: Path
) -> None:
    """No real trade prints a high below its close, so the decision halts."""
    lakes.with_row(lake, "GLD", LAST, high=150.0, low=149.0)
    src = make(lake, tmp_path)
    reading = src.read(close(LAST))
    snap = reading.snapshot
    assert snap.bars[GLD]["1d"][-1].high == Decimal("150")
    assert [(f.session, f.field, f.rule) for f in snap.flags[GLD]] == [
        (LAST, "high", spikes.CONTRADICT)
    ]
    assert GLD in snap.flags and USO not in snap.flags
    # 159.72 is the largest of GLD's open, close and low on 2012-04-09 in Chan's vintage.
    assert reading.halts == (
        f"GLD's bar for {LAST.isoformat()} has prices that contradict each other: "
        "its high of 150.0 is below the largest of its open, close and low, 159.72",
    )
    with pytest.raises(Halt, match="contradict each other"):
        src.snapshot(close(LAST))


def test_1_a_contradiction_halts_each_decision_that_reads_it_and_holds_nothing_after(
    lake: Path, tmp_path: Path
) -> None:
    """A halt is the decision's own, so the source carries nothing to the next one."""
    bad = before(LAST, 45)
    lakes.with_row(lake, "GLD", bad, high=1.0, low=1.0)
    src = make(lake, tmp_path)
    first = src.read(close(before(LAST, 44)))
    again = src.read(close(before(LAST, 43)))
    clear = src.read(close(LAST))
    assert any("contradict each other" in r for r in first.halts)
    assert any("contradict each other" in r for r in again.halts)
    assert clear.halts == ()
    assert bad not in [b.session for b in clear.snapshot.bars[GLD]["1d"]]


def test_1_a_contradicting_bar_halts_through_the_last_decision_whose_lookback_holds_it(
    lake: Path, tmp_path: Path
) -> None:
    """One step below, at and past the lookback's edge."""
    span = settings().lookback
    bad = before(LAST, span + 5)
    lakes.with_row(lake, "GLD", bad, high=1.0, low=1.0)
    src = make(lake, tmp_path)
    last_holding = DAYS[DAYS.index(bad) + span - 1]
    first_clear = DAYS[DAYS.index(bad) + span]
    for day, halts in ((before(last_holding), True), (last_holding, True), (first_clear, False)):
        reading = src.read(close(day))
        assert any("contradict each other" in r for r in reading.halts) is halts, day


@pytest.mark.parametrize(
    ("price", "rules"),
    [
        (15.937, [spikes.BELOW_LOW, spikes.MOVE]),
        (137.41, [spikes.BELOW_LOW, spikes.MOVE]),
        (137.42, [spikes.BELOW_LOW]),
        (158.9, [spikes.BELOW_LOW]),
        (158.91, []),
    ],
)
def test_1_a_lone_wrong_close_below_its_low_alerts_and_halts_nothing(
    lake: Path, tmp_path: Path, price: float, rules: list[str]
) -> None:
    """A close below its low with the rest of the bar as it was is flagged, however far it moved.

    A close at a tenth also trips the move rule. At a multiple of 10 the closes
    from 137.42 to 158.90 move too little for it, which is the band design.md's
    2026-11-24 entry 2 quotes, and the below-the-low rule alone flags them.
    """
    lakes.with_row(lake, "GLD", LAST, close=price)
    reading = make(lake, tmp_path, spike_multiple=Decimal(10)).read(close(LAST))
    # 158.91 is GLD's low on 2012-04-09 in Chan's vintage.
    flags = reading.snapshot.flags.get(GLD, ())
    assert [(f.rule, f.field, f.price) for f in flags] == [
        (rule, "close", Decimal(str(price))) for rule in rules
    ]
    bar = reading.snapshot.bars[GLD]["1d"][-1]
    assert (bar.low, bar.close) == (Decimal("158.91"), Decimal(str(price)))
    assert [f.against for f in flags if f.rule == spikes.BELOW_LOW] == [bar.low] * bool(rules)
    assert reading.halts == ()


def test_1_a_zero_close_halts_as_a_zero_price_alone(lake: Path, tmp_path: Path) -> None:
    """The move rule judges no return on a close that is not positive, and no other rule halts."""
    lakes.with_row(lake, "GLD", LAST, close=0.0)
    reading = make(lake, tmp_path).read(close(LAST))
    contradicting = [r for r in reading.halts if "contradict each other" in r]
    assert len(contradicting) == 1
    assert "its close of 0.0 is at or below zero" in contradicting[0]


def test_1_each_contradiction_on_a_bar_has_its_own_halt(lake: Path, tmp_path: Path) -> None:
    lakes.with_row(lake, "GLD", LAST, high=0.0, low=0.0)
    reading = make(lake, tmp_path).read(close(LAST))
    contradictions = [f for f in reading.snapshot.flags[GLD] if f.rule == spikes.CONTRADICT]
    # A high and a low at zero, and a high below the largest of its open, close and low.
    assert len(contradictions) == 3
    assert any(
        "its high of 0.0 is below the largest of its open, close and low" in r
        for r in reading.halts
    )
    assert any("its high of 0.0 is at or below zero" in r for r in reading.halts)
    assert len([r for r in reading.halts if "contradict each other" in r]) == len(contradictions)
    assert any("its low of 0.0 is at or below zero" in r for r in reading.halts)


def test_1_a_zero_open_halts_as_a_zero_price_alone(lake: Path, tmp_path: Path) -> None:
    """A low above a zero open is no contradiction of its own."""
    lakes.with_row(lake, "GLD", LAST, open=0.0)
    reading = make(lake, tmp_path).read(close(LAST))
    contradicting = [r for r in reading.halts if "contradict each other" in r]
    assert len(contradicting) == 1
    assert "its open of 0.0 is at or below zero" in contradicting[0]


def test_1_judge_halts_on_what_the_read_halts_on(lake: Path, tmp_path: Path) -> None:
    """A stale, a contradicting and an unstated bar each give one reason both ways."""
    unlist(lake, lakes.partition_key("USO", LAST))
    unlist(lake, lakes.partition_key("USO", before(LAST)))
    lakes.with_row(lake, "GLD", LAST, high=150.0, low=149.0)
    src = make(lake, tmp_path, kinds=PriceKinds(()))
    reading = src.read(close(LAST))
    judged = source.judge(reading.snapshot, src.instruments)
    assert len(reading.halts) == len(judged) == 4
    assert {r.split(":")[0] for r in reading.halts if "contradict" in r or "unstated" in r} == {
        r.split(":")[0] for r in judged if "contradict" in r or "unstated" in r
    }


def _stored(bars, flags=None, status=None):  # type: ignore[no-untyped-def]
    from ithildinruntime.core import Snapshot

    return Snapshot(
        close(LAST),
        {GLD: {"1d": tuple(bars)}} if bars is not None else {},
        {},
        status if status is not None else {GLD: BarStatus.FRESH},
        False,
        flags or {},
        (),
        {},
    )


def test_1_judge_finds_a_contradiction_in_the_bars_where_no_flag_was_stored() -> None:
    from tests.test_data_spikes import bar

    stored = _stored([bar(0, "100", high="99", low="98", open="99")])
    (reason,) = [r for r in source.judge(stored, (GLD,)) if "contradict" in r]
    assert "its high of 99 is below the largest of its open, close and low, 100" in reason


def test_1_judge_halts_on_a_stored_contradiction_flag_the_bars_do_not_raise() -> None:
    """A stored flag the bars do not raise halts as recorded."""
    from ithildinruntime.core import SpikeFlag
    from tests.test_data_spikes import bar

    clean = bar(0, "100")
    flag = SpikeFlag(clean.session, "low", Decimal(100), Decimal(99), spikes.CONTRADICT, "")
    reasons = source.judge(_stored([clean], flags={GLD: (flag,)}), (GLD,))
    assert reasons == (
        f"GLD's stored flag for {clean.session.isoformat()} halts as recorded: its low of 100 "
        f"against 99, by the rule {spikes.CONTRADICT!r}",
    )


@pytest.mark.parametrize(
    ("rule", "field"), [("prices contradict", "high"), ("a new rule", "close")]
)
def test_1_judge_halts_on_a_stored_flag_of_a_rule_or_field_this_release_does_not_know(
    rule: str, field: str
) -> None:
    from ithildinruntime.core import SpikeFlag
    from tests.test_data_spikes import bar

    clean = bar(0, "100")
    flag = SpikeFlag(clean.session, field, Decimal(1), Decimal(2), rule, "")
    (reason,) = source.judge(_stored([clean], flags={GLD: (flag,)}), (GLD,))
    assert f"its {field} of 1 against 2, by the rule {rule!r}" in reason


def test_1_judge_passes_an_older_bar_below_its_low_as_the_read_did(
    lake: Path, tmp_path: Path
) -> None:
    day = before(LAST, 3)
    lakes.with_row(lake, "GLD", day, close=150.0)
    src = make(lake, tmp_path, spike_multiple=Decimal(10))
    reading = src.read(close(LAST))
    assert [(f.session, f.rule) for f in reading.snapshot.flags[GLD]] == [(day, spikes.BELOW_LOW)]
    assert reading.halts == source.judge(reading.snapshot, src.instruments) == ()


def test_1_judge_states_one_reason_for_a_bar_that_raises_both_rules() -> None:
    from tests.test_data_spikes import bar

    odd = bar(0, "100", high="99", low="101", open="99")
    flags = spikes.spike_flags([odd], Decimal(100))
    assert {f.rule for f in flags} == {spikes.CONTRADICT, spikes.BELOW_LOW}
    assert len(source.judge(_stored([odd], flags={GLD: flags}), (GLD,))) == 1


def test_1_judge_halts_on_no_stored_below_the_low_flag_its_bars_raise() -> None:
    """It alerts and halts nothing. One the bars do not raise halts as a changed record."""
    from tests.test_data_spikes import bar

    odd = bar(0, "95", high="101", low="100", open="100")
    (flag,) = spikes.below_low(odd)
    assert source.judge(_stored([odd], flags={GLD: (flag,)}), (GLD,)) == ()
    assert source.judge(_stored([odd]), (GLD,)) == ()
    (reason,) = source.judge(_stored([bar(0, "100")], flags={GLD: (flag,)}), (GLD,))
    assert f"its close of 95 against 100, by the rule {spikes.BELOW_LOW!r}" in reason


def test_1_judge_halts_on_no_stored_flag_of_the_move_rule() -> None:
    """A move flag alerts and halts nothing, stored or not."""
    from ithildinruntime.core import SpikeFlag
    from tests.test_data_spikes import bar

    clean = bar(0, "100")
    flag = SpikeFlag(clean.session, "close", Decimal(100), Decimal(10), spikes.MOVE, "")
    assert source.judge(_stored([clean], flags={GLD: (flag,)}), (GLD,)) == ()


def test_1_judge_halts_on_a_stored_flag_for_an_instrument_with_no_bars() -> None:
    from ithildinruntime.core import SpikeFlag
    from tests.test_data_spikes import bar

    clean = bar(0, "100")
    flag = SpikeFlag(clean.session, "high", Decimal(1), Decimal(2), spikes.CONTRADICT, "")
    stored = _stored([clean], flags={USO: (flag,)})
    (reason,) = source.judge(stored, (GLD,))
    assert reason.startswith("USO's stored flag for ")


def test_1_judge_halts_on_stored_and_recomputed_contradictions_once_each() -> None:
    """A bar that contradicts on its high, a flag recorded on its low, and the high's own flag."""
    from ithildinruntime.core import SpikeFlag
    from tests.test_data_spikes import bar

    odd = bar(0, "100", high="99", low="98", open="99")
    (own,) = spikes.contradictions(odd)
    low = SpikeFlag(odd.session, "low", Decimal(98), Decimal(97), spikes.CONTRADICT, "")
    reasons = source.judge(_stored([odd], flags={GLD: (own, low)}), (GLD,))
    assert len(reasons) == 2
    assert "its high of 99 is below the largest" in reasons[0]
    assert "halts as recorded: its low of 98 against 97" in reasons[1]


@pytest.mark.parametrize("change", ["session", "instrument", "field", "rule", "price", "against"])
def test_1_judge_states_a_stored_flag_that_differs_from_the_recomputed_one(change: str) -> None:
    """Only a flag equal in every field but its note to one the bars raise counts as found."""
    from tests.test_data_spikes import bar

    odd = bar(0, "100", high="99", low="98", open="99")
    (own,) = spikes.contradictions(odd)
    moved = {
        "session": dataclasses.replace(own, session=own.session + timedelta(days=1)),
        "instrument": own,
        "field": dataclasses.replace(own, field="low"),
        "rule": dataclasses.replace(own, rule="a new rule"),
        "price": dataclasses.replace(own, price=Decimal(1)),
        "against": dataclasses.replace(own, against=Decimal(2)),
    }[change]
    flags = {USO: (moved,)} if change == "instrument" else {GLD: (moved,)}
    reasons = source.judge(_stored([odd], flags=flags), (GLD,))
    assert len(reasons) == 2
    assert "is below the largest" in reasons[0] and "halts as recorded" in reasons[1]


def test_1_contradictions_halt_in_the_order_spike_flags_gives_them() -> None:
    from tests.test_data_spikes import bar

    bars = [bar(0, "0", open="0", high="-1", low="-2"), bar(1, "5", high="0", low="0")]
    flags = [f for f in spikes.spike_flags(bars, Decimal(100)) if f.rule == spikes.CONTRADICT]
    expected = source._contradicted(GLD, flags)
    assert source._bar_halts(GLD, bars) == expected
    assert len(expected) == len(flags)


def test_1_judge_halts_on_an_instrument_marked_fresh_with_no_bar() -> None:
    assert source.judge(_stored(None), (GLD,)) == (
        "GLD is fresh and has no bar in the stored snapshot",
    )
    assert source.judge(_stored([]), (GLD,)) == (
        "GLD is fresh and has no bar in the stored snapshot",
    )
    from tests.test_data_spikes import bar

    assert source.judge(_stored([bar(0, "100")]), (GLD,)) == ()


def test_1_a_move_flag_halts_nothing_and_the_price_reaches_the_decision(
    lake: Path, tmp_path: Path
) -> None:
    """A close a tenth of the one before is flagged by the move rule alone."""
    lakes.with_row(lake, "GLD", LAST, open=15.937, high=15.937, low=15.937, close=15.937)
    src = make(lake, tmp_path, spike_multiple=Decimal(10))
    reading = src.read(close(LAST))
    assert reading.halts == ()
    assert [(f.session, f.field, f.rule) for f in reading.snapshot.flags[GLD]] == [
        (LAST, "close", spikes.MOVE)
    ]
    snap = src.snapshot(close(LAST))
    assert snap.bars[GLD]["1d"][-1].close == Decimal("15.937")
    assert snap.flags[GLD][0].rule == spikes.MOVE


def test_1_the_ports_snapshot_halts_where_a_stored_snapshot_would(
    lake: Path, tmp_path: Path
) -> None:
    """`core.DataSource.snapshot` has no halts beside it, so a stale input raises."""
    unlist(lake, lakes.partition_key("USO", LAST))
    unlist(lake, lakes.partition_key("USO", before(LAST)))
    src = make(lake, tmp_path)
    assert src.read(close(LAST)).halts
    with pytest.raises(Halt, match="USO stale for the decision"):
        src.snapshot(close(LAST))


# 2. The start of a series.


def test_2_a_snapshot_on_the_fifth_session_holds_five_and_reads_fresh(
    lake: Path, tmp_path: Path
) -> None:
    fifth = DAYS[DAYS.index(FIRST) + 4]
    snap = make(lake, tmp_path).read(close(fifth)).snapshot
    assert [b.session for b in snap.bars[GLD]["1d"]] == DAYS[
        DAYS.index(FIRST) : DAYS.index(fifth) + 1
    ]
    assert snap.status[GLD] is BarStatus.FRESH


def test_2_a_lookback_without_a_first_session_reads_missing_before_the_series(
    lake: Path, tmp_path: Path
) -> None:
    fifth = DAYS[DAYS.index(FIRST) + 4]
    # USO began trading on 2006-04-10, so only GLD's master row covers the
    # sessions before the vintages start.
    reading = make(lake, tmp_path, instruments=(GLD,), first_sessions={}).read(close(fifth))
    assert reading.snapshot.status[GLD] is BarStatus.MISSING


# A first session written later than the lake's series starts, test 10 of #110.

QQQ = InstrumentId("QQQ")
# The 40th session of both series, the first whose lookback they fill, and the 11th.
FILLED = date(2006, 6, 21)
EARLY = date(2006, 5, 10)
LATE = date(2012, 2, 27)


def window(at: date, lookback: int = 40) -> list[date]:
    end = DAYS.index(at) + 1
    return DAYS[end - lookback : end]


def list_partition(root: Path, key: str) -> None:
    """Put a partition in the lake and its manifest that no decision here reads."""
    (root / key).parent.mkdir(parents=True, exist_ok=True)
    (root / key).write_bytes(b"not read")
    lakes.record(root, key)


def late(src: LakeSource, at: date) -> str:
    with pytest.raises(Halt) as caught:
        src.read(close(at))
    return caught.value.reason


@pytest.mark.parametrize(
    ("at", "first", "gone"),
    [
        # One session after the lake's start.
        (FILLED, date(2006, 4, 27), []),
        # The same where the other leg's first session cuts the lookback too, and is right.
        (EARLY, date(2006, 4, 27), []),
        # After a hole of one session inside the lookback.
        (LAST, LATE, [before(LATE)]),
        # After a hole that runs to the lookback's old end.
        (LAST, LATE, [d for d in window(LAST) if d < LATE]),
        # One session past the lookback's old end, with no hole.
        (LAST, window(LAST)[1], []),
        # After the decision itself, which leaves no session to read.
        (LAST, date(2012, 4, 10), []),
    ],
    ids=[
        "one-late",
        "both-cut",
        "a-hole-inside",
        "a-hole-at-the-old-end",
        "the-old-end",
        "past-the-decision",
    ],
)
@pytest.mark.parametrize(("leg", "other"), [("GLD", "USO"), ("USO", "GLD")])
def test_a_first_session_that_cuts_a_lookback_the_lake_could_fill_halts(
    lake: Path, tmp_path: Path, at: date, first: date, gone: list[date], leg: str, other: str
) -> None:
    for day in gone:
        unlist(lake, lakes.partition_key(leg, day))
    src = make(
        lake,
        tmp_path,
        first_sessions={InstrumentId(leg): first, InstrumentId(other): FIRST},
    )
    manifest = hashlib.sha256((lake / "manifest.jsonl").read_bytes()).hexdigest()
    assert late(src, at) == (
        f"{leg}'s first session is written as {first.isoformat()}, which cuts the decision's "
        f"lookback, and the lake lists {lakes.partition_key(leg, FIRST)} before it. "
        f"Read from manifest.jsonl at SHA-256 {manifest}, bucket version {manifest}"
    )
    assert list((tmp_path / "mirror" / "roots").iterdir()) == []


@pytest.mark.parametrize(
    ("at", "first"),
    [
        (FILLED, FIRST),
        # At the lookback's old end and before it, where it cuts nothing.
        (LAST, window(LAST)[0]),
        (LAST, before(window(LAST)[0])),
        # The master names no USO ticker for part of the lookback.
        (EARLY, FIRST),
    ],
)
def test_a_first_session_that_cuts_nothing_the_lake_lists_passes(
    lake: Path, tmp_path: Path, at: date, first: date
) -> None:
    reading = make(lake, tmp_path, first_sessions={GLD: first, USO: first}).read(close(at))
    assert reading.halts == ()
    assert set(reading.snapshot.status.values()) == {BarStatus.FRESH}


def test_a_first_session_too_early_reads_missing(lake: Path, tmp_path: Path) -> None:
    early = make(lake, tmp_path, first_sessions={GLD: before(FIRST), USO: FIRST})
    assert early.read(close(EARLY)).snapshot.status == {
        GLD: BarStatus.MISSING,
        USO: BarStatus.FRESH,
    }


def test_a_partition_before_the_calendars_first_session_is_an_earlier_one(
    lake: Path, tmp_path: Path
) -> None:
    """GLD traded from 2004-11-18, and the session table starts in 2006."""
    earlier = date(2005, 12, 30)
    assert earlier < TABLE.first
    list_partition(lake, lakes.partition_key("GLD", earlier))
    assert f"the lake lists {lakes.partition_key('GLD', earlier)} before it" in late(
        make(lake, tmp_path), EARLY
    )
    # The master names no USO ticker that day, so the partition is none of USO's.
    uso = make(lake, tmp_path, instruments=(USO,), first_sessions={USO: FIRST})
    assert uso.read(close(EARLY)).halts == ()


@pytest.mark.parametrize(
    ("partition", "first", "at", "halts"),
    [
        ("bars/ticker=QQQQ/freq=1d/date=2006-04-25.parquet", FIRST, EARLY, True),
        # The master gives QQQ another ticker that day.
        ("bars/ticker=QQQ/freq=1d/date=2006-04-25.parquet", FIRST, EARLY, False),
        # Not a daily bar.
        ("bars/ticker=QQQQ/freq=1m/date=2006-04-25.parquet", FIRST, EARLY, False),
        # The partition's own day decides the ticker, which changed before the first session.
        (
            "bars/ticker=QQQQ/freq=1d/date=2011-03-21.parquet",
            date(2011, 3, 24),
            date(2011, 4, 8),
            True,
        ),
        (
            "bars/ticker=QQQ/freq=1d/date=2011-03-21.parquet",
            date(2011, 3, 24),
            date(2011, 4, 8),
            False,
        ),
    ],
)
def test_only_an_earlier_daily_partition_of_the_instruments_own_ticker_halts(
    lake: Path, tmp_path: Path, partition: str, first: date, at: date, halts: bool
) -> None:
    list_partition(lake, partition)
    src = make(lake, tmp_path, instruments=(QQQ,), first_sessions={QQQ: first})
    with pytest.raises(Halt) as caught:
        src.snapshot(close(at))
    if halts:
        assert f"the lake lists {partition} before it" in caught.value.reason
    else:
        assert "first session is written as" not in caught.value.reason


# 3. End time.


@pytest.mark.parametrize(("minutes", "holds"), [(-1, False), (0, True), (1, True)])
def test_3_a_bar_is_in_a_decision_only_once_its_session_closed(
    lake: Path, tmp_path: Path, minutes: int, holds: bool
) -> None:
    at = close(LAST) + timedelta(minutes=minutes)
    snap = make(lake, tmp_path).read(at).snapshot
    for instrument in (GLD, USO):
        assert (snap.bars[instrument]["1d"][-1].session == LAST) is holds
        assert snap.status[instrument] is BarStatus.FRESH


def test_3_close_minus_20_reads_through_the_session_before(lake: Path, tmp_path: Path) -> None:
    snap = make(lake, tmp_path).read(close(LAST) - timedelta(minutes=20)).snapshot
    assert snap.bars[GLD]["1d"][-1].session == before(LAST)
    assert len(snap.bars[GLD]["1d"]) == 40


# 4. Knowable time.


@pytest.mark.parametrize(("after_fetch", "holds"), [(-1, False), (0, True), (1, True)])
def test_4_a_captured_bar_is_known_only_from_its_fetch_stamp(
    lake: Path, tmp_path: Path, after_fetch: int, holds: bool
) -> None:
    fetched = close(LAST) + timedelta(hours=2)
    lakes.captured(lake, "USO", LAST, fetch_ts=fetched.astimezone(lakes.MARKET_TZ).isoformat())
    src = make(lake, tmp_path)
    at_close = src.read(close(LAST)).snapshot
    assert at_close.bars[USO]["1d"][-1].session == before(LAST)
    at = fetched + timedelta(microseconds=after_fetch)
    snap = src.read(at).snapshot
    newest = snap.bars[USO]["1d"][-1]
    assert (newest.session == LAST) is holds
    if holds:
        assert (newest.origin, newest.basis, newest.source) == ("captured", "raw", None)
        assert newest.knowable_at == fetched
        assert newest.fetched == fetched.astimezone(lakes.MARKET_TZ).isoformat()


def test_4_a_fetch_stamp_with_no_zone_halts(lake: Path, tmp_path: Path) -> None:
    lakes.captured(lake, "USO", LAST, fetch_ts="2012-04-09T18:00:00")
    with pytest.raises(Halt, match="with no zone, so it is not usable"):
        make(lake, tmp_path).read(close(LAST) + timedelta(days=1))


def test_4_a_captured_bar_with_no_fetch_stamp_halts(lake: Path, tmp_path: Path) -> None:
    lakes.captured(lake, "USO", LAST, fetch_ts=None)
    with pytest.raises(Halt, match="captured with no fetch stamp"):
        make(lake, tmp_path).read(close(LAST))


def test_4_an_imported_bar_with_fetch_stamps_is_known_from_its_end(
    lake: Path, tmp_path: Path
) -> None:
    """The Schwab import's rows carry fetch stamps, and an imported bar ignores them."""
    later = (close(LAST) + timedelta(days=3)).isoformat()
    lakes.with_row(lake, "GLD", LAST, fetch_ts=later, fetch_end_ts=later)
    newest = make(lake, tmp_path).read(close(LAST)).snapshot.bars[GLD]["1d"][-1]
    assert newest.session == LAST and newest.knowable_at == close(LAST)
    assert newest.fetched == later and newest.fetched_end == later
    assert newest.fetched_end_at == close(LAST) + timedelta(days=3)


def test_4_a_captured_bar_reads_its_kind_from_the_capture_row(lake: Path, tmp_path: Path) -> None:
    fetched = (close(LAST) + timedelta(hours=2)).isoformat()
    lakes.captured(lake, "USO", LAST, fetch_ts=fetched)
    stated = Kind("last trade", "consolidated", "none", "consolidated")
    kinds = PriceKinds((KindRow(date(2026, 11, 22), CAPTURE_SOURCE, LAST, LAST, stated, "a test"),))
    reading = make(lake, tmp_path, kinds=kinds).read(close(LAST) + timedelta(days=1))
    assert reading.snapshot.bars[USO]["1d"][-1].price_kind == "last trade"


@pytest.mark.parametrize("name", ["open", "high", "low", "close"])
def test_4_a_bar_with_no_price_halts(lake: Path, tmp_path: Path, name: str) -> None:
    lakes.with_row(lake, "GLD", LAST, **{name: None})
    with pytest.raises(Halt, match=f"GLD on {LAST} has no {name}"):
        make(lake, tmp_path).read(close(LAST))


def test_4_a_price_that_is_no_number_halts(lake: Path, tmp_path: Path) -> None:
    lakes.with_row(lake, "GLD", LAST, close=float("nan"))
    with pytest.raises(Halt, match="has a close nan that is no price"):
        make(lake, tmp_path).read(close(LAST))


def test_a_decision_time_that_is_not_utc_is_refused(lake: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="a decision time is UTC"):
        make(lake, tmp_path).read(close(LAST).astimezone(lakes.MARKET_TZ))


def test_a_decision_before_the_session_table_halts(lake: Path, tmp_path: Path) -> None:
    first = TABLE.first
    with pytest.raises(Halt, match="outside the session table's range"):
        make(lake, tmp_path).read(
            datetime(first.year, first.month, first.day, tzinfo=UTC) - timedelta(days=1)
        )


# 5. A missing session.


def test_5_a_session_missing_inside_the_lookback_reads_missing_and_is_never_filled(
    lake: Path, tmp_path: Path
) -> None:
    gone = before(LAST, 3)
    unlist(lake, lakes.partition_key("GLD", gone))
    reading = make(lake, tmp_path).read(close(LAST))
    snap = reading.snapshot
    assert snap.status[GLD] is BarStatus.MISSING
    assert gone not in {b.session for b in snap.bars[GLD]["1d"]}
    assert len(snap.bars[GLD]["1d"]) == 39
    assert any(f"no bar for {gone.isoformat()}" in r for r in reading.halts)
    with pytest.raises(Halt, match="GLD missing"):
        snap.require_fresh({GLD: ["1d"], USO: ["1d"]})


# 6. A stale series.


@pytest.mark.parametrize(("dropped", "state"), [(1, "fresh"), (2, "fresh"), (3, "stale")])
def test_6_a_series_lagging_past_the_written_lag_reads_stale(
    lake: Path, tmp_path: Path, dropped: int, state: str
) -> None:
    for n in range(dropped):
        unlist(lake, lakes.partition_key("USO", before(LAST, n)))
    reading = make(lake, tmp_path, stale_lag=2).read(close(LAST))
    assert reading.snapshot.status[USO] == BarStatus(state)
    assert any("USO stale" in r for r in reading.halts) is (state == "stale")


def test_6_an_absent_lag_halts(lake: Path, tmp_path: Path) -> None:
    with pytest.raises(Halt, match="no stale lag is written"):
        make(lake, tmp_path, stale_lag=None).read(close(LAST))


# 7. Two rows for one session.


def test_7_two_rows_for_one_session_halt(lake: Path, tmp_path: Path) -> None:
    key = lakes.doubled(lake, "GLD", before(LAST, 2))
    with pytest.raises(
        Halt, match=f"GLD holds 2 daily rows for the session of {before(LAST, 2)}"
    ) as e:
        make(lake, tmp_path).read(close(LAST))
    # No snapshot is stored, so the halt names the file and its version.
    sha = hashlib.sha256((lake / key).read_bytes()).hexdigest()
    assert str(e.value).endswith(f"Read from {key} at SHA-256 {sha}, bucket version {sha}")


# 8. Every other answer.


def test_8_a_partition_whose_provenance_cannot_be_read_halts(lake: Path, tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    key = lakes.partition_key("GLD", LAST)
    table = pq.read_table(lake / key)
    meta = {**table.schema.metadata, b"marketlake.origin": b"bogus"}
    sink = pa.BufferOutputStream()
    pq.write_table(table.replace_schema_metadata(meta), sink)
    lakes.rewrite(lake, key, sink.getvalue().to_pybytes())
    lakes.record(lake, key, rows=1)
    with pytest.raises(Halt, match="ProvenanceMalformed"):
        make(lake, tmp_path).read(close(LAST))


def test_8_a_quarantine_ledger_that_is_not_utf8_halts(lake: Path, tmp_path: Path) -> None:
    lakes.rewrite(lake, "quarantine.jsonl", b"\xff\xfe\n")
    lakes.record(lake, "quarantine.jsonl")
    with pytest.raises(Halt, match="LedgerNotUtf8"):
        make(lake, tmp_path).read(close(LAST))


def _errors() -> list[Exception]:
    from lake import actions, config, loader, manifest

    made: list[Exception] = []
    for cls in (
        *(c for c in loader.LoadError.__subclasses__() if c is not loader.BarsAbsent),
        config.ConfigError,
        actions.LedgerLineError,
        actions.LedgerNotUtf8,
        manifest.ManifestError,
    ):
        error = cls.__new__(cls)
        Exception.__init__(error, f"a planted {cls.__name__}")
        made.append(error)
    return made


@pytest.mark.parametrize("error", _errors(), ids=lambda e: type(e).__name__)
def test_8_each_answer_the_table_halts_on_halts(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def refuse(*args, **kwargs):
        raise error

    monkeypatch.setattr(source, "load_bars", refuse)
    with pytest.raises(Halt, match=type(error).__name__):
        make(lake, tmp_path).read(close(LAST))


# 9. The bucket's copy is checked.


def test_9_a_byte_changed_in_the_buckets_copy_halts_naming_both_hashes(
    lake: Path, tmp_path: Path
) -> None:
    key = lakes.partition_key("GLD", LAST)
    data = bytearray((lake / key).read_bytes())
    listed = hashlib.sha256(data).hexdigest()
    data[100] ^= 1
    lakes.rewrite(lake, key, bytes(data))
    found = hashlib.sha256(data).hexdigest()
    with pytest.raises(Halt) as halt:
        make(lake, tmp_path).read(close(LAST))
    assert found in halt.value.reason and listed in halt.value.reason
    assert "upload stopped before its manifest.jsonl" in halt.value.reason


def test_9_a_byte_changed_in_the_mirrors_copy_is_downloaded_again(
    lake: Path, tmp_path: Path
) -> None:
    bucket = Counting(FolderBucket(lake))
    src = make(lake, tmp_path, bucket=bucket)
    first = src.read(close(LAST)).snapshot
    key = lakes.partition_key("GLD", LAST)
    sha = hashlib.sha256((lake / key).read_bytes()).hexdigest()
    cached = tmp_path / "mirror" / "objects" / sha
    data = bytearray(cached.read_bytes())
    data[100] ^= 1
    cached.unlink()
    cached.write_bytes(bytes(data))
    bucket.reads.clear()
    again = src.read(close(LAST)).snapshot
    assert key in bucket.reads
    assert again.bars == {i: {"1d": tuple(first.bars[i]["1d"])} for i in first.bars}
    assert hashlib.sha256(cached.read_bytes()).hexdigest() == sha


def test_9_a_file_the_mirror_holds_is_not_downloaded_again(lake: Path, tmp_path: Path) -> None:
    bucket = Counting(FolderBucket(lake))
    src = make(lake, tmp_path, bucket=bucket)
    src.read(close(LAST))
    bucket.reads.clear()
    src.read(close(LAST))
    assert bucket.reads == ["manifest.jsonl"]


# 10. The manifest is read whole.


def test_10_a_manifest_not_ending_in_a_newline_halts(lake: Path, tmp_path: Path) -> None:
    manifest = lake / "manifest.jsonl"
    manifest.write_bytes(manifest.read_bytes().rstrip(b"\n"))
    with pytest.raises(Halt, match="does not end in a newline"):
        make(lake, tmp_path).read(close(LAST))


def test_10_a_manifest_cut_short_mid_line_halts(lake: Path, tmp_path: Path) -> None:
    manifest = lake / "manifest.jsonl"
    manifest.write_bytes(manifest.read_bytes()[:-20])
    with pytest.raises(Halt, match="does not end in a newline"):
        make(lake, tmp_path).read(close(LAST))


def test_10_a_whole_entry_hidden_behind_a_damaged_line_mid_file_halts(
    lake: Path, tmp_path: Path
) -> None:
    manifest = lake / "manifest.jsonl"
    lines = manifest.read_bytes().splitlines(keepends=True)
    middle = len(lines) // 2
    manifest.write_bytes(b"".join([*lines[:middle], b'{"torn": \n', *lines[middle:]]))
    with pytest.raises(Halt, match="manifest.jsonl cannot be read: TornLedger"):
        make(lake, tmp_path).read(close(LAST))


def test_10_a_manifest_sha256_that_is_not_hex_halts_and_touches_nothing_outside(
    lake: Path, tmp_path: Path
) -> None:
    """The mirror names stored files by the manifest's SHA-256, so a path in it must not climb."""
    victim = tmp_path / "victim"
    victim.write_text("kept")
    key = lakes.partition_key("GLD", LAST)
    climb = "../" * 4 + "victim"
    entry = json.loads((lake / "manifest.jsonl").read_text().splitlines()[-1])
    entry.update(partition=key, sha256=climb + "x" * (64 - len(climb)))
    with (lake / "manifest.jsonl").open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
    with pytest.raises(Halt, match="records no SHA-256 for " + key):
        make(lake, tmp_path).read(close(LAST))
    assert victim.read_text() == "kept"


def test_10_a_manifest_entry_marketlake_cannot_read_halts_and_leaves_no_root(
    lake: Path, tmp_path: Path
) -> None:
    with (lake / "manifest.jsonl").open("a") as handle:
        handle.write(json.dumps({"sha256": "0" * 64}) + "\n")
    with pytest.raises(Halt, match="manifest.jsonl cannot be read"):
        make(lake, tmp_path).read(close(LAST))
    assert list((tmp_path / "mirror" / "roots").iterdir()) == []


def test_10_a_failed_manifest_download_halts_with_an_older_copy_held(
    lake: Path, tmp_path: Path
) -> None:
    bucket = Counting(FolderBucket(lake))
    src = make(lake, tmp_path, bucket=bucket)
    src.read(close(LAST))
    bucket.refuse["manifest.jsonl"] = StoreUnavailable("manifest.jsonl", "503 SlowDown")
    with pytest.raises(Halt, match="manifest.jsonl could not be downloaded"):
        src.read(close(LAST))


def test_10_a_manifest_missing_from_the_bucket_halts(lake: Path, tmp_path: Path) -> None:
    (lake / "manifest.jsonl").unlink()
    with pytest.raises(Halt, match="manifest.jsonl is not in the bucket"):
        make(lake, tmp_path).read(close(LAST))


class _Damaged:
    """A bucket whose answer's bytes do not match the SHA-256 it stored with them."""

    def __init__(self, bucket, key: str) -> None:
        self.bucket, self.key = bucket, key

    def read(self, key: str) -> BucketObject:
        got = self.bucket.read(key)
        if key == self.key:
            return replace(got, data=got.data[:-1])
        return got


def test_10_a_download_whose_bytes_differ_from_the_stored_sha256_halts(
    lake: Path, tmp_path: Path
) -> None:
    bucket = _Damaged(FolderBucket(lake), "manifest.jsonl")
    with pytest.raises(Halt, match="so the download was damaged"):
        make(lake, tmp_path, bucket=bucket).read(close(LAST))


@pytest.mark.parametrize("field", ["version", "sha256"])
def test_10_a_file_with_no_version_or_no_stored_sha256_halts(
    lake: Path, tmp_path: Path, field: str
) -> None:
    class Bare:
        def read(self, key: str) -> BucketObject:
            got = FolderBucket(lake).read(key)
            return replace(got, **{field: None}) if key.startswith("bars/") else got

    with pytest.raises(Halt, match=f"has no {'version' if field == 'version' else 'SHA-256'}"):
        make(lake, tmp_path, bucket=Bare()).read(close(LAST))


# 11. A missing reader file.


@pytest.mark.parametrize("ledger", ["quarantine.jsonl", "actions/corporate_actions.jsonl"])
def test_11_a_bucket_missing_a_ledger_halts_before_any_read(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: str
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(source, "load_bars", lambda *a, **k: calls.append(a))
    (lake / ledger).unlink()
    with pytest.raises(Halt, match=f"{ledger} is a reader file the bucket does not hold"):
        make(lake, tmp_path).read(close(LAST))
    assert calls == []


@pytest.mark.parametrize("ledger", ["quarantine.jsonl", "actions/corporate_actions.jsonl"])
def test_11_a_manifest_listing_no_ledger_halts_before_any_read(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ledger: str
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(source, "load_bars", lambda *a, **k: calls.append(a))
    unlist(lake, ledger)
    with pytest.raises(Halt, match=f"lists no {ledger}"):
        make(lake, tmp_path).read(close(LAST))
    assert calls == []


def _quarantine(root: Path, day: date) -> str:
    key = lakes.partition_key("GLD", day)
    entry = build_entry(
        partition=key,
        verdict="quarantined",
        check="planted",
        observed_at=datetime(2026, 11, 21, 17, 0, tzinfo=UTC),
    )
    path = root / "quarantine.jsonl"
    data = path.read_bytes()
    path.unlink()
    path.write_bytes(data)
    append_quarantine(root, entry)
    lakes.record(root, "quarantine.jsonl", rows=1)
    return key


def test_11_a_quarantined_partition_in_the_lookback_halts(lake: Path, tmp_path: Path) -> None:
    key = _quarantine(lake, before(LAST, 5))
    with pytest.raises(Halt, match=f"{key} is quarantined in marketlake"):
        make(lake, tmp_path).read(close(LAST))


# 12. Data not ready.


def test_12_a_listed_partition_absent_from_the_bucket_reads_as_data_not_ready(
    lake: Path, tmp_path: Path
) -> None:
    key = lakes.partition_key("USO", before(LAST, 7))
    (lake / key).unlink()
    with pytest.raises(Halt, match=f"{key} is listed in the manifest .* the data is not ready"):
        make(lake, tmp_path).read(close(LAST))


# 13. Two decisions at once.


def test_13_two_decisions_record_the_ledger_each_one_read(lake: Path, tmp_path: Path) -> None:
    bucket = Counting(FolderBucket(lake))
    src = make(lake, tmp_path, bucket=bucket)
    old = hashlib.sha256((lake / "quarantine.jsonl").read_bytes()).hexdigest()
    later: dict[str, object] = {}

    def change_and_read() -> None:
        # The bucket's quarantine ledger changes while the first decision reads,
        # and a second decision reads the new one in full.
        _quarantine(lake, before(LAST, 5))

        def second() -> None:
            try:
                src.read(close(LAST))
            except Halt as halt:
                later["halt"] = halt

        thread = threading.Thread(target=second)
        thread.start()
        thread.join()

    bucket.on_read[lakes.partition_key("USO", LAST)] = change_and_read
    first = src.read(close(LAST)).snapshot
    new = hashlib.sha256((lake / "quarantine.jsonl").read_bytes()).hexdigest()
    assert new != old
    assert first.ledgers["quarantine.jsonl"] == old
    assert "is quarantined in marketlake" in later["halt"].reason  # type: ignore[union-attr]
    assert (tmp_path / "mirror" / "objects" / old).exists()
    assert (tmp_path / "mirror" / "objects" / new).exists()


def test_13_an_interrupted_download_leaves_the_old_file_or_none(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ithildinruntime.data import mirror

    src = make(lake, tmp_path)
    src.read(close(LAST))
    objects = tmp_path / "mirror" / "objects"
    held = {p.name: p.read_bytes() for p in objects.iterdir()}
    key = lakes.partition_key("GLD", before(LAST, 50))
    real = mirror.os.replace

    def dies(src_path, dst_path):
        raise OSError("the machine lost power")

    monkeypatch.setattr(mirror.os, "replace", dies)
    with pytest.raises(OSError):
        src._mirror._download(key, None)
    monkeypatch.setattr(mirror.os, "replace", real)
    after = {p.name: p.read_bytes() for p in objects.iterdir()}
    assert after == held


# 15. Places.


def test_15_prices_with_more_places_than_declared_are_kept_and_named(
    lake: Path, tmp_path: Path
) -> None:
    lakes.with_row(lake, "GLD", LAST, open=100.1234, high=100.12345, low=99.123456, close=100.0)
    newest = make(lake, tmp_path).read(close(LAST)).snapshot.bars[GLD]["1d"][-1]
    assert format(newest.open, "f") == "100.1234"
    assert format(newest.high, "f") == "100.12345"
    assert format(newest.low, "f") == "99.123456"
    assert newest.excess_places == ("high", "low")


# 16. Unstated kind.


def _kinds(first: date, last: date) -> PriceKinds:
    stated = Kind("official close", "consolidated", "none", "consolidated")
    return PriceKinds(
        tuple(
            KindRow(
                date(2026, 11, 22),
                f"quantitative-trading:data/inputdata_etf/{t}.csv",
                first,
                last,
                stated,
                "a test",
            )
            for t in ("gld", "uso")
        )
    )


def test_16_a_bar_of_unstated_kind_halts_after_the_snapshot(lake: Path, tmp_path: Path) -> None:
    # A contract with no row for the source, so every field reads unstated.
    reading = make(lake, tmp_path, kinds=PriceKinds(())).read(close(LAST))
    assert reading.snapshot.bars[GLD]["1d"][-1].price_kind == "unstated"
    assert any("GLD's bar" in r and "unstated price kind" in r for r in reading.halts)


def test_16_the_tracked_contract_states_yahoos_kind_for_both_vintages(
    lake: Path, tmp_path: Path
) -> None:
    reading = make(lake, tmp_path).read(close(LAST))
    assert reading.halts == ()
    for instrument in (GLD, USO):
        for bar in reading.snapshot.bars[instrument]["1d"]:
            assert (bar.price_kind, bar.venues, bar.delay, bar.volume_counted) == (
                "official close",
                "consolidated",
                "none",
                "consolidated in hundreds of shares",
            )


@pytest.mark.parametrize("silent", ["price_kind", "venues", "delay", "volume_counted"])
def test_16_any_one_unstated_field_halts_and_is_named(
    lake: Path, tmp_path: Path, silent: str
) -> None:
    fields = {"price_kind": "official close", "venues": "consolidated", "delay": "none"}
    fields["volume_counted"] = "consolidated"
    fields[silent] = "unstated"
    rows = tuple(replace(r, kind=Kind(**fields)) for r in _kinds(FIRST, LAST).rows)
    reading = make(lake, tmp_path, kinds=PriceKinds(rows)).read(close(LAST))
    named = {"price_kind": "price kind", "volume_counted": "volume counting"}.get(silent, silent)
    assert any(f"has unstated {named}, from" in r for r in reading.halts)


@pytest.mark.parametrize(("shift", "halts"), [(-1, False), (0, False), (1, True)])
def test_16_a_session_outside_its_sources_row_reads_unstated(
    lake: Path, tmp_path: Path, shift: int, halts: bool
) -> None:
    oldest = DAYS[DAYS.index(LAST) - 39]
    first = DAYS[DAYS.index(oldest) + shift]
    reading = make(lake, tmp_path, kinds=_kinds(first, LAST)).read(close(LAST))
    assert any("unstated" in r for r in reading.halts) is halts
    if not halts:
        assert reading.snapshot.bars[GLD]["1d"][0].price_kind == "official close"


# 17. No row in the security master.


@pytest.mark.parametrize(("ends", "halts"), [(-1, True), (0, False), (1, False)])
def test_17_an_instrument_with_no_master_row_on_a_session_halts(
    lake: Path, tmp_path: Path, ends: int, halts: bool
) -> None:
    rows = tuple(r for r in load_master().rows if r.id != USO)
    last = DAYS[DAYS.index(LAST) + ends]
    master = SecurityMaster(
        (*rows, MasterRow(date(2026, 11, 22), USO, "USO", date(2006, 4, 10), last))
    )
    src = make(lake, tmp_path, master=master)
    if halts:
        with pytest.raises(Halt, match=f"USO has no security master row valid on {LAST}"):
            src.read(close(LAST))
    else:
        assert src.read(close(LAST)).snapshot.status[USO] is BarStatus.FRESH


def test_17_the_master_maps_an_id_to_the_ticker_on_each_session() -> None:
    master = load_master()
    assert master.ticker(InstrumentId("QQQ"), date(2008, 1, 2)) == "QQQQ"
    assert master.ticker(InstrumentId("QQQ"), date(2011, 3, 23)) == "QQQ"
    with pytest.raises(Halt):
        master.ticker(InstrumentId("GLD"), date(2004, 11, 17))


# The answers kept for the run.


def test_a_sessions_answer_is_read_once_while_its_files_are_unchanged(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[date] = []
    real = source.load_bars

    def counted(ticker, freq, start, end, **kwargs):
        calls.append(start)
        return real(ticker, freq, start, end, **kwargs)

    monkeypatch.setattr(source, "load_bars", counted)
    src = make(lake, tmp_path)
    src.read(close(LAST))
    assert len(calls) == 80
    src.read(close(LAST))
    assert len(calls) == 80
    lakes.with_row(lake, "GLD", LAST, close=200.0)
    snap = src.read(close(LAST)).snapshot
    assert calls[80:] == [LAST]
    assert snap.bars[GLD]["1d"][-1].close == Decimal("200.0")


def test_the_log_names_the_manifests_bytes_and_the_download_seconds(
    lake: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="ithildinruntime.data.source")
    reading = make(lake, tmp_path).read(close(LAST))
    size = (lake / "manifest.jsonl").stat().st_size
    assert reading.manifest_bytes == size
    assert f"manifest of {size} bytes" in caplog.text


def test_each_decision_reads_its_own_lake_root_and_leaves_none(lake: Path, tmp_path: Path) -> None:
    make(lake, tmp_path).read(close(LAST))
    assert list((tmp_path / "mirror" / "roots").iterdir()) == []


def test_a_decision_outside_the_session_table_halts(lake: Path, tmp_path: Path) -> None:
    with pytest.raises(Halt, match="outside the session table's range"):
        make(lake, tmp_path).read(datetime(2028, 1, 3, 21, tzinfo=UTC))


def test_a_version_record_lost_from_the_mirror_is_downloaded_again(
    lake: Path, tmp_path: Path
) -> None:
    bucket = Counting(FolderBucket(lake))
    src = make(lake, tmp_path, bucket=bucket)
    src.read(close(LAST))
    for record in (tmp_path / "mirror" / "versions").iterdir():
        record.unlink()
    bucket.reads.clear()
    snap = src.read(close(LAST)).snapshot
    key = lakes.partition_key("GLD", LAST)
    assert key in bucket.reads
    sha = hashlib.sha256((lake / key).read_bytes()).hexdigest()
    assert snap.bars[GLD]["1d"][-1].vintage_id == f"{key}#sha256={sha}#version={sha}"
    records = [json.loads(p.read_text()) for p in (tmp_path / "mirror" / "versions").iterdir()]
    assert {"key": key, "version": sha} in records


def test_a_version_record_for_another_key_is_not_trusted(lake: Path, tmp_path: Path) -> None:
    bucket = Counting(FolderBucket(lake))
    src = make(lake, tmp_path, bucket=bucket)
    src.read(close(LAST))
    for record in (tmp_path / "mirror" / "versions").iterdir():
        held = json.loads(record.read_text())
        record.write_text(json.dumps({**held, "key": "bars/another"}))
    bucket.reads.clear()
    src.read(close(LAST))
    assert lakes.partition_key("GLD", LAST) in bucket.reads


def test_a_changed_reader_file_reads_each_session_again(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer is kept by the hashes of every reader file, as well as its partition."""
    calls: list[date] = []
    real = source.load_bars

    def counted(ticker, interval, *, start, **kwargs):
        calls.append(start)
        return real(ticker, interval, start=start, **kwargs)

    monkeypatch.setattr(source, "load_bars", counted)
    src = make(lake, tmp_path)
    src.read(close(LAST))
    first = len(calls)
    _quarantine(lake, before(LAST, 200))
    src.read(close(LAST))
    assert len(calls) == 2 * first


def test_a_folder_bucket_reads_a_missing_key_as_blob_missing(lake: Path) -> None:
    with pytest.raises(BlobMissing):
        FolderBucket(lake).read("bars/ticker=GLD/freq=1d/date=1999-01-04.parquet")
