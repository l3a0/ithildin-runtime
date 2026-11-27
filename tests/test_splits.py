"""The record of splits: its file, its checks of a stored snapshot, and the gate's refusal.

#189's acceptance names each case, and a test's number is the acceptance
item's. Every price is made up. The checks run on hand-built snapshots here,
and tests/test_splits_read.py runs them through `LakeSource` on a made-up lake.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.core import (
    Bar,
    BarStatus,
    ClockReading,
    Halt,
    InstrumentId,
    Limit,
    PairCover,
    Snapshot,
    SplitRow,
    SplitsRead,
    load_sessions,
)
from ithildinruntime.data import load_master
from ithildinruntime.data.splits import (
    HEADER,
    QUOTE,
    SEALED,
    SPLITS,
    SplitRecord,
    check_splits,
    load_splits,
    pairs,
)
from tests.support import gates
from tests.support import splits as made
from tests.support.quotes import NONE_READ, quote, read_at
from tests.support.spreads import bar

TABLE = load_sessions()
DAYS = sorted(TABLE.sessions)
GLD, USO = InstrumentId("GLD"), InstrumentId("USO")
MASTER = load_master()
KIND = "the close of the session before"
# A window of 21 sessions of captured bars, and a decision at close−20 of the session after.
WINDOW = [d for d in DAYS if date(2026, 11, 2) <= d][:21]
DAY = DAYS[DAYS.index(WINDOW[-1]) + 1]
AT = TABLE.session(DAY).close - timedelta(minutes=20)
HEAD = ",".join(HEADER)


# The file


def write(tmp_path: Path, *rows: str, head: str = HEAD) -> Path:
    path = tmp_path / "splits.csv"
    path.write_text("\n".join(["# a comment", head, *rows]) + "\n", encoding="utf-8")
    return path


def load(path: Path) -> SplitRecord:
    return load_splits(path, master=MASTER, sessions=TABLE)


SPLIT = "2026-11-27,split,USO,2020-04-29,,1,8,2020-04-22T13:30:00Z,a filing"
# Its last session is the fifth before the day it was added, the latest a covered row may reach.
COVERED = "2026-11-27,covered,GLD,2026-10-30,2026-11-19,,,,two lists"


def test_1_the_tracked_file_loads_and_holds_the_seed_rows() -> None:
    record = load_splits(master=MASTER, sessions=TABLE)
    (uso,) = record.known(USO, datetime(2026, 11, 27, tzinfo=UTC))
    assert (uso.first_session, uso.new_shares, uso.old_shares) == (date(2020, 4, 29), 1, 8)
    assert uso.known_at == datetime(2020, 4, 22, 13, 30, tzinfo=UTC)
    # The open of the first session the press release reached, before which nothing knew it.
    assert record.known(USO, datetime(2020, 4, 22, 13, 29, 59, 999999, tzinfo=UTC)) == ()
    spans = {
        r.instrument: (r.first_session, r.last_session) for r in record.rows if r.kind == "covered"
    }
    assert spans == {
        InstrumentId("SPY"): (date(2026, 9, 8), date(2026, 11, 19)),
        InstrumentId("QQQ"): (date(2026, 9, 8), date(2026, 11, 19)),
        GLD: (date(2026, 10, 30), date(2026, 11, 19)),
        USO: (date(2026, 10, 30), date(2026, 11, 19)),
    }


def test_1_a_good_file_reads_each_row_with_its_line(tmp_path: Path) -> None:
    record = load(write(tmp_path, SPLIT, COVERED))
    assert [(r.kind, r.line) for r in record.rows] == [("split", 3), ("covered", 4)]


def test_1_a_known_at_at_the_open_of_the_first_session_passes(tmp_path: Path) -> None:
    opens = TABLE.session(date(2020, 4, 29)).open.isoformat().replace("+00:00", "Z")
    row = f"2026-11-27,split,USO,2020-04-29,,1,8,{opens},a filing"
    assert load(write(tmp_path, row)).rows[0].known_at == TABLE.session(date(2020, 4, 29)).open


@pytest.mark.parametrize(
    ("row", "said"),
    [
        ("2026-11-27,splat,USO,2020-04-29,,1,8,2020-04-22T13:30:00Z,x", "of kind 'splat'"),
        ("2026-11-27,split,XYZ,2020-04-29,,1,8,2020-04-22T13:30:00Z,x", "security master does not"),
        ("2026-11-27,split,USO,2020-04-29,,0,8,2020-04-22T13:30:00Z,x", "no positive whole number"),
        ("2026-11-27,split,USO,2020-04-29,,1.5,8,2020-04-22T13:30:00Z,x", "no positive whole"),
        ("2026-11-27,split,USO,2020-04-29,,8,8,2020-04-22T13:30:00Z,x", "new shares equal to old"),
        ("2026-11-27,split,USO,2020-04-29,,1,8,2020-04-22T13:30:00,x", "no UTC instant"),
        ("2026-11-27,split,USO,2020-04-29,,1,8,2020-04-22T09:30:00-04:00,x", "no UTC instant"),
        ("2026-11-27,split,USO,2020-04-29,,1,8,2020-04-29T13:30:00.000001Z,x", "after the open"),
        ("2026-11-27,split,USO,2020-04-25,,1,8,2020-04-22T13:30:00Z,x", "no session on"),
        ("2026-11-27,split,USO,2020-04-29,2020-05-01,1,8,2020-04-22T13:30:00Z,x", "fills"),
        ("2026-11-27,split,USO,2020-04-29,,1,8,2020-04-22T13:30:00Z,", "names nothing"),
        ("2026-11-27,covered,GLD,2026-11-19,2026-10-30,,,,x", "first session is after its last"),
        ("2026-11-27,covered,GLD,2026-10-30,2026-11-21,,,,x", "no session on"),
        ("2026-11-27,covered,GLD,2026-10-30,2026-11-20,,,,x", "is not 5 sessions before"),
        ("2026-11-27,covered,GLD,2026-10-30,2026-11-19,1,,,x", "fills new_shares"),
        ("2026-11-27,withdrawn,USO,2020-04-29,,,,,x", "no split row on that key"),
        ("2026-11-27,split,USO,2020-04-29,,1,8,x", "holds 8 fields, not 9"),
        ("27/11/2026,split,USO,2020-04-29,,1,8,2020-04-22T13:30:00Z,x", "which is no date"),
    ],
)
def test_1_each_check_refuses_a_file_that_breaks_it_naming_the_row(
    tmp_path: Path, row: str, said: str
) -> None:
    with pytest.raises(Halt, match=r"splits\.csv line 4 ") as raised:
        load(write(tmp_path, COVERED, row))
    assert said in raised.value.reason
    assert "every decision halts" in raised.value.reason


def test_1_two_rows_of_one_key_added_on_one_date_are_refused(tmp_path: Path) -> None:
    again = SPLIT.replace(",1,8,", ",1,4,")
    with pytest.raises(Halt, match="line 4 is a second row of USO from 2020-04-29"):
        load(write(tmp_path, SPLIT, again))
    withdrawn = "2026-11-27,withdrawn,USO,2020-04-29,,,,,wrong"
    with pytest.raises(Halt, match="line 4 is a second row"):
        load(write(tmp_path, SPLIT, withdrawn))
    # A covered row has no key, so two of one day are no conflict.
    assert len(load(write(tmp_path, COVERED, COVERED)).rows) == 2


def test_1_a_withdrawn_row_needs_a_split_row_added_before_it(tmp_path: Path) -> None:
    later = "2026-11-28,withdrawn,USO,2020-04-29,,,,,wrong"
    record = load(write(tmp_path, SPLIT, later))
    assert record.known(USO, AT) == ()


def test_8_a_newer_split_row_on_the_key_replaces_the_ratio(tmp_path: Path) -> None:
    newer = "2026-11-28,split,USO,2020-04-29,,1,4,2020-04-22T13:30:00Z,a correction"
    (row,) = load(write(tmp_path, SPLIT, newer)).known(USO, AT)
    assert (row.new_shares, row.old_shares, row.line) == (1, 4, 4)


# 2. A record that is not there halts, and never reads as no split


def test_2_a_missing_file_halts(tmp_path: Path) -> None:
    with pytest.raises(Halt, match="cannot be read"):
        load(tmp_path / "gone.csv")


def test_2_an_empty_file_halts(tmp_path: Path) -> None:
    path = tmp_path / "splits.csv"
    path.write_text("")
    with pytest.raises(Halt, match="is empty"):
        load(path)
    path.write_text("# only a comment\n")
    with pytest.raises(Halt, match="is empty"):
        load(path)
    with pytest.raises(Halt, match="holds no row"):
        load(write(tmp_path))


def test_2_a_wrong_header_halts(tmp_path: Path) -> None:
    with pytest.raises(Halt, match="has the header"):
        load(write(tmp_path, COVERED, head=HEAD.replace("known_at,", "")))


def test_2_an_unreadable_file_halts(tmp_path: Path) -> None:
    path = tmp_path / "splits.csv"
    path.write_bytes(b"\xff\xfe" + HEAD.encode())
    with pytest.raises(Halt, match="is not UTF-8"):
        load(path)


def test_2_the_tracked_file_is_beside_the_other_data_files() -> None:
    assert SPLITS.name == "splits.csv" and SPLITS.parent.name == "data"


# The checks, on hand-built snapshots


def raw(instrument: InstrumentId, day: date, close: object) -> Bar:
    return bar(instrument, day, Decimal(str(close)), basis="raw")


def cover(
    instrument: InstrumentId,
    earlier: date,
    later: date,
    *said: str,
    covered: SplitRow | None = None,
    source: str | None = SEALED,
    kind: str = KIND,
) -> PairCover:
    return PairCover(
        instrument=instrument,
        earlier=earlier,
        later=later,
        covered=covered,
        source=source if said else None,
        partition=f"quotes/ticker={instrument}/date={later}.parquet" if source == SEALED else None,
        partition_sha256="a" * 64 if source == SEALED else None,
        close_prices=tuple(said),
        close_kind=kind,
        fault=None,
    )


def snap(
    closes: dict[date, object],
    *,
    instrument: InstrumentId = USO,
    covers: tuple[PairCover, ...] | None = None,
    rows: tuple[SplitRow, ...] = (),
    covered_from: date | None = date(2026, 10, 30),
    quoted: object = None,
    quote_covered: SplitRow | None = None,
    at: datetime = AT,
    bars: tuple[Bar, ...] | None = None,
) -> Snapshot:
    """One instrument's raw bars, each pair covered by its sealed day at the close before.

    `quoted` is the decision's own quote's `close_price`, where it read one.
    """
    held = bars or tuple(raw(instrument, d, c) for d, c in closes.items())
    if covers is None:
        days = list(closes)
        covers = tuple(
            cover(instrument, p, s, str(closes[p])) for p, s in zip(days, days[1:], strict=False)
        )
    quotes = {}
    if quoted is not None:
        newest = held[-1].session
        # Priced at the newest close, so a quote's midpoint reads no step.
        mid = held[-1].close
        quotes = {instrument: quote(instrument, mid - Decimal("0.01"), mid + Decimal("0.01"), at)}
        covers += (
            cover(instrument, newest, DAY, str(quoted), source=QUOTE, covered=quote_covered),
        )
    return Snapshot(
        decision_at=at,
        bars={instrument: {"1d": held}},
        quotes=quotes,
        status={instrument: BarStatus.FRESH},
        flags={},
        segments=(),
        ledgers={},
        **(read_at(at) if quotes else {"provisional": False, **NONE_READ}),
        clock=ClockReading(at, None),
        splits=SplitsRead(
            sha256=made.SHA,
            rows={instrument: rows},
            covered_from={instrument: covered_from} if covered_from else {},
            pairs=covers,
        ),
    )


def flat(n: int = len(WINDOW), close: object = "100.00") -> dict[date, object]:
    return dict.fromkeys(WINDOW[-n:], close)


def test_3_a_window_whose_every_pair_a_quote_states_passes() -> None:
    assert check_splits(snap(flat(), quoted="100.0"), TABLE) == []


def test_3_the_pairs_run_from_the_first_raw_bar_and_on_to_the_quote() -> None:
    imported = bar(USO, date(2026, 10, 29), Decimal(100))
    held = (imported, *(raw(USO, d, 100) for d in WINDOW))
    found = pairs({USO: {"1d": held}}, [USO], DAY, TABLE)
    assert [(p.earlier, p.later, p.quoted) for p in found] == [
        *((p, s, False) for p, s in zip(WINDOW, WINDOW[1:], strict=False)),
        (WINDOW[-1], DAY, True),
    ]


def test_3_the_pair_across_schwabs_import_and_the_captured_bars_is_checked() -> None:
    seam = replace(
        bar(USO, date(2026, 10, 30), Decimal(100)),
        origin="imported",
        source="schwab:pricehistory",
        source_as_of=date(2026, 11, 2),
    )
    captured = replace(raw(USO, date(2026, 11, 2), 100), origin="captured", source=None)
    found = pairs({USO: {"1d": (seam, captured)}}, [], None, TABLE)
    assert [(p.earlier, p.later) for p in found] == [(date(2026, 10, 30), date(2026, 11, 2))]
    # Another imported bar before the captured ones is a change of series, which halts apart.
    other = replace(seam, source="a test")
    assert [
        (p.earlier, p.later) for p in pairs({USO: {"1d": (other, captured)}}, [], None, TABLE)
    ] == []


def test_3_a_window_of_adjusted_bars_alone_checks_no_pair() -> None:
    held = tuple(bar(USO, d, Decimal(100)) for d in WINDOW)
    assert pairs({USO: {"1d": held}}, [USO], DAY, TABLE) == []


# 7. The bounds, one cent from each edge


@pytest.mark.parametrize(
    ("stated", "halts"), [("94.99", True), ("95.0", False), ("105.0", False), ("105.01", True)]
)
def test_7_the_residual_halts_outside_its_bounds(stated: str, halts: bool) -> None:
    found = check_splits(snap(flat(), quoted=stated), TABLE)
    assert bool(found) is halts
    if halts:
        (reason,) = found
        assert "outside 0.95 to 1.05" in reason and stated not in reason and "100" not in reason


@pytest.mark.parametrize(
    ("close", "halts"), [("70.01", False), ("70.00", True), ("142.89", False), ("142.90", True)]
)
def test_7_the_step_halts_at_its_bounds(close: str, halts: bool) -> None:
    closes = {**flat(3), WINDOW[-1]: close}
    days = list(closes)
    # The quote restates nothing, as Schwab may not on a split's own session.
    covers = tuple(cover(USO, p, s, "100.0") for p, s in zip(days, days[1:], strict=False))
    found = check_splits(snap(closes, covers=covers), TABLE)
    assert bool(found) is halts
    if halts:
        (reason,) = found
        assert "only a quote covers" in reason and close not in reason


def test_7_a_pair_a_covered_row_holds_reads_no_step() -> None:
    closes = {**flat(3), WINDOW[-1]: "50.00"}
    days = list(closes)
    row = made.covered("USO", WINDOW[0], WINDOW[-1])
    # Each pair also holds a sealed day's close, so the covered row alone keeps the step out.
    covers = tuple(
        cover(USO, p, s, "100.0", covered=row) for p, s in zip(days, days[1:], strict=False)
    )
    assert check_splits(snap(closes, covers=covers), TABLE) == []


def test_5_the_decisions_own_quote_is_checked_under_a_covered_row_too() -> None:
    row = made.covered("USO", WINDOW[0], DAY)
    days = list(flat())
    covers = tuple(cover(USO, p, s, covered=row) for p, s in zip(days, days[1:], strict=False))
    found = snap(flat(), covers=covers, quoted="50.0", quote_covered=row)
    (reason,) = check_splits(found, TABLE)
    assert "USO's own quote of" in reason and "0.5000 times" in reason


def test_5_two_values_of_the_close_on_a_sealed_day_halt() -> None:
    days = list(flat(3))
    covers = (cover(USO, days[0], days[1], "100.0"), cover(USO, days[1], days[2], "100.0", "99.0"))
    (reason,) = check_splits(snap(flat(3), covers=covers), TABLE)
    assert "states 2 values of the close" in reason and "99" not in reason


# 6 and 9. A split with no row


@pytest.mark.parametrize("ratio", [Decimal(2), Decimal("0.5"), Decimal("0.125")])
def test_6_a_split_with_no_row_halts_on_the_residual_where_the_quote_restates_it(
    ratio: Decimal,
) -> None:
    closes = {**flat(5)}
    days = list(closes)
    after = (Decimal(100) / ratio).quantize(Decimal("0.01"))
    closes[days[-1]] = after
    covers = (
        *(cover(USO, p, s, "100.0") for p, s in zip(days[:-2], days[1:-1], strict=False)),
        # Restated: the quote of the split's session states the session before on the new scale.
        cover(USO, days[-2], days[-1], str(after)),
    )
    found = check_splits(snap(closes, covers=covers), TABLE)
    (reason,) = [r for r in found if "outside 0.95 to 1.05" in r]
    assert f"{days[-1]} states a close of {days[-2]} at" in reason
    assert "A split row" in reason


@pytest.mark.parametrize("ratio", [Decimal(2), Decimal("0.5"), Decimal("0.125")])
def test_6_a_split_with_no_row_halts_on_the_step_where_the_quote_does_not_restate(
    ratio: Decimal,
) -> None:
    closes = {**flat(5)}
    days = list(closes)
    closes[days[-1]] = (Decimal(100) / ratio).quantize(Decimal("0.01"))
    covers = tuple(cover(USO, p, s, "100.0") for p, s in zip(days, days[1:], strict=False))
    (reason,) = check_splits(snap(closes, covers=covers), TABLE)
    assert "only a quote covers" in reason


def test_6_a_four_for_three_the_quote_does_not_restate_reads_as_a_move() -> None:
    """The gap the owner accepted on shadow and paper, on 2026-11-26."""
    closes = {**flat(5)}
    days = list(closes)
    closes[days[-1]] = Decimal("75.00")
    covers = tuple(cover(USO, p, s, "100.0") for p, s in zip(days, days[1:], strict=False))
    assert check_splits(snap(closes, covers=covers), TABLE) == []


def test_9_twenty_uncovered_sessions_give_one_reason_naming_what_releases_it() -> None:
    days = list(flat())
    covers = tuple(cover(USO, p, s) for p, s in zip(days, days[1:], strict=False))
    (reason,) = check_splits(snap(flat(), covers=covers), TABLE)
    assert f"the 20 pairs of sessions from {days[0]} to {days[-1]}" in reason
    assert f"A covered row reaching {days[-1]} releases it" in reason


def test_9_an_unstated_kind_of_close_covers_nothing() -> None:
    days = list(flat(3))
    covers = tuple(
        cover(USO, p, s, "100.0", kind="unstated") for p, s in zip(days, days[1:], strict=False)
    )
    (reason,) = check_splits(snap(flat(3), covers=covers), TABLE)
    assert "under a kind data/quote_kinds.csv states" in reason


def test_9_once_its_row_is_known_the_split_halts_on_the_row_instead() -> None:
    closes = {**flat(5)}
    days = list(closes)
    closes[days[-1]] = Decimal("50.00")
    covers = tuple(cover(USO, p, s, "100.0") for p, s in zip(days, days[1:], strict=False))
    row = made.split("USO", days[-1], 2, 1, line=7)
    (reason,) = check_splits(snap(closes, covers=covers, rows=(row,)), TABLE)
    assert f"a recorded split of 2 new shares for 1 old from {days[-1]}" in reason
    assert "data/splits.csv line 7" in reason and "#190 and #191" in reason


def test_9_a_pair_with_no_cover_in_the_snapshot_halts() -> None:
    (reason,) = check_splits(snap(flat(2), covers=()), TABLE)
    assert "names no cover for 1 pairs" in reason


def test_9_a_stale_newest_bar_leaves_the_quote_with_no_close_to_state() -> None:
    closes = dict.fromkeys(WINDOW[-3:-1], "100.00")
    found = check_splits(snap(closes, quoted="100.0"), TABLE)
    assert any("not the session before the quote's own" in r for r in found)


# 8. A recorded split


def window_snap(rows: tuple[SplitRow, ...], at: datetime = AT, **changes: object) -> Snapshot:
    return snap(flat(), rows=rows, quoted="100.0", at=at, **changes)


def test_8_a_split_row_halts_from_its_first_session_naming_the_row() -> None:
    row = made.split("USO", DAY, 1, 8, line=19)
    (reason,) = check_splits(window_snap((row,)), TABLE)
    assert f"from {DAY}, at data/splits.csv line 19" in reason


def test_8_a_decision_one_session_before_its_first_session_meets_no_such_halt() -> None:
    later = DAYS[DAYS.index(DAY) + 1]
    assert check_splits(window_snap((made.split("USO", later, 1, 8),)), TABLE) == []


def test_8_a_split_before_the_record_checked_any_captured_bar_halts_nothing() -> None:
    """USO's split of 2020, which every series the platform reads is adjusted for or after."""
    row = made.split("USO", date(2020, 4, 29), 1, 8)
    assert check_splits(window_snap((row,)), TABLE) == []
    # With no covered row, nothing says the platform read no bar before it.
    assert check_splits(window_snap((row,), covered_from=None), TABLE) != []


def test_8_an_adjusted_series_from_before_the_split_halts() -> None:
    row = made.split("USO", date(2020, 4, 29), 1, 8)
    old = tuple(replace(bar(USO, d, Decimal(100)), source_as_of=date(2012, 4, 10)) for d in WINDOW)
    (reason,) = check_splits(window_snap((row,), bars=old), TABLE)
    assert "not adjusted for it" in reason
    adjusted = tuple(replace(b, source_as_of=date(2026, 11, 22)) for b in old)
    assert check_splits(window_snap((row,), bars=adjusted), TABLE) == []


def test_8_a_withdrawn_row_ends_it(tmp_path: Path) -> None:
    first = DAY.isoformat()
    known = TABLE.before(DAY).open.isoformat().replace("+00:00", "Z")
    path = write(
        tmp_path,
        f"2026-11-27,split,USO,{first},,1,8,{known},a filing",
        f"2026-11-28,withdrawn,USO,{first},,,,,the filing named another fund",
    )
    assert load(path).known(USO, AT) == ()


# 10. The gate, from a split row's known_at


def gated(known_at: datetime, qty: object) -> object:
    """The gate's verdict at its decision, on a raw window, with a split row known at `known_at`.

    The snapshot holds the rows the read knows by the decision, as `LakeSource` chooses them.
    """
    later = DAYS[DAYS.index(gates.TODAY) + 3]
    record = made.record(made.split("GLD", later, 2, 1, known_at=known_at))
    world = gates.World(intent=gates.order(qty), book=gates.book(positions={"chan": {GLD: 5}}))
    bars = {
        i: {"1d": tuple(replace(b, basis="raw") for b in by["1d"])}
        for i, by in world.snapshot.bars.items()
    }
    known = replace(made.NO_SPLITS, rows={GLD: record.known(GLD, gates.DECISION)})
    return world.at(snapshot=replace(world.snapshot, bars=bars, splits=known)).check()


def test_10_a_decision_at_known_at_refuses_an_order_that_adds_and_passes_one_that_reduces() -> None:
    adds = gated(gates.DECISION, 1)
    assert not adds.approved and adds.limit is Limit.SPLIT
    assert adds.value is None and adds.bound is None
    assert gated(gates.DECISION, -1).approved


def test_10_a_decision_a_microsecond_before_known_at_passes_both() -> None:
    later = gates.DECISION + timedelta(microseconds=1)
    assert gated(later, 1).approved and gated(later, -1).approved


def test_10_an_adjusted_window_meets_no_refusal() -> None:
    """A backtest on a series already adjusted for the split trades through it."""
    later = DAYS[DAYS.index(gates.TODAY) + 3]
    row = made.split("GLD", later, 2, 1, known_at=gates.DECISION)
    world = gates.World(intent=gates.order(1), book=gates.book(positions={"chan": {GLD: 5}}))
    bars = {
        i: {"1d": tuple(replace(b, source_as_of=later) for b in by["1d"])}
        for i, by in world.snapshot.bars.items()
    }
    known = replace(made.NO_SPLITS, rows={GLD: (row,)})
    assert world.at(snapshot=replace(world.snapshot, bars=bars, splits=known)).check().approved


def test_8_a_split_on_the_instruments_first_covered_session_halts_and_one_before_it_does_not() -> (
    None
):
    """The first covered row starts where the platform first held a captured bar."""
    on = made.split("USO", date(2026, 10, 30), 1, 8)
    (reason,) = check_splits(window_snap((on,), covered_from=date(2026, 10, 30)), TABLE)
    assert "not adjusted for it" in reason
    before = made.split("USO", date(2026, 10, 29), 1, 8)
    assert check_splits(window_snap((before,), covered_from=date(2026, 10, 30)), TABLE) == []


def test_8_a_raw_window_wholly_after_a_split_the_platform_captured_across_still_halts() -> None:
    """A book or a share cap written before the split is on the old scale until #190 and #191."""
    row = made.split("USO", date(2026, 10, 1), 1, 8)
    (reason,) = check_splits(window_snap((row,), covered_from=date(2026, 9, 8)), TABLE)
    assert "not adjusted for it" in reason


def test_8_one_bar_not_adjusted_in_a_window_halts_however_many_are() -> None:
    row = made.split("USO", date(2026, 10, 1), 1, 8)
    adjusted = [replace(bar(USO, d, Decimal(100)), source_as_of=date(2026, 11, 22)) for d in WINDOW]
    mixed = (*adjusted[:-1], replace(adjusted[-1], basis="raw", source_as_of=None))
    found = check_splits(window_snap((row,), bars=mixed, covered_from=None), TABLE)
    assert any("not adjusted for it" in r for r in found)
    assert not any(
        "not adjusted for it" in r
        for r in check_splits(window_snap((row,), bars=tuple(adjusted), covered_from=None), TABLE)
    )


def test_8_a_raw_bar_is_adjusted_for_no_split_whatever_date_it_carries() -> None:
    row = made.split("USO", date(2026, 10, 1), 1, 8)
    dated = tuple(replace(raw(USO, d, 100), source_as_of=date(2026, 11, 22)) for d in WINDOW)
    found = check_splits(window_snap((row,), bars=dated, covered_from=date(2026, 9, 8)), TABLE)
    assert any("not adjusted for it" in r for r in found)


def test_9_a_split_with_no_row_still_halts_beside_an_old_recorded_one() -> None:
    """USO's row of 2020 skips the checks on its own session alone."""
    closes = {**flat(5)}
    days = list(closes)
    closes[days[-1]] = Decimal("50.00")
    covers = tuple(cover(USO, p, s, "100.0") for p, s in zip(days, days[1:], strict=False))
    old = made.split("USO", date(2020, 4, 29), 1, 8)
    (reason,) = check_splits(snap(closes, covers=covers, rows=(old,)), TABLE)
    assert "only a quote covers" in reason


def test_10_a_split_on_the_decisions_own_session_refuses_an_order_that_adds_too() -> None:
    """Data's stage halts that decision first, and the gate refuses it as well."""
    record = made.record(made.split("GLD", gates.TODAY, 2, 1, known_at=gates.DECISION))
    world = gates.World(intent=gates.order(1), book=gates.book(positions={"chan": {GLD: 5}}))
    bars = {
        i: {"1d": tuple(replace(b, basis="raw") for b in by["1d"])}
        for i, by in world.snapshot.bars.items()
    }
    known = replace(made.NO_SPLITS, rows={GLD: record.known(GLD, gates.DECISION)})
    verdict = world.at(snapshot=replace(world.snapshot, bars=bars, splits=known)).check()
    assert verdict.limit is Limit.SPLIT


@pytest.mark.parametrize(
    ("mid", "halts"),
    [
        (50, True),
        (800, True),
        (75, False),
        (100, False),
        (Decimal("70.00"), True),
        (Decimal("70.01"), False),
        (Decimal("142.89"), False),
        (Decimal("142.90"), True),
    ],
)
def test_6_a_split_on_the_decisions_own_session_the_quote_does_not_restate_halts_on_the_step(
    mid: Decimal, halts: bool
) -> None:
    """The quote's midpoint against the newest stored close, where only the quote covers the pair.

    A two-for-one or a one-for-eight halts. A four-for-three reads as a move, the gap the
    owner accepted on shadow and paper.
    """
    found = snap(flat(), quoted="100.0")
    priced = {USO: quote(USO, mid - Decimal("0.01"), mid + Decimal("0.01"), AT)}
    found = replace(found, quotes=priced)
    reasons = check_splits(found, TABLE)
    assert any("only a quote covers" in r for r in reasons) is halts
    if halts:
        (reason,) = reasons
        assert f"own quote of {DAY} has a midpoint" in reason and f" {mid}." not in reason


def test_6_a_covered_row_over_the_decisions_own_pair_reads_no_step_on_its_quote() -> None:
    row = made.covered("USO", WINDOW[0], DAY)
    days = list(flat())
    covers = tuple(cover(USO, p, s, covered=row) for p, s in zip(days, days[1:], strict=False))
    found = snap(flat(), covers=covers, quoted="100.0", quote_covered=row)
    found = replace(found, quotes={USO: quote(USO, Decimal("49.99"), Decimal("50.01"), AT)})
    assert check_splits(found, TABLE) == []


def test_10_a_split_before_the_first_covered_session_refuses_nothing_on_its_own_session() -> None:
    """The gate reads the instrument's first covered session, as data's halt does."""
    record = made.record(made.split("GLD", gates.TODAY, 2, 1, known_at=gates.DECISION))
    world = gates.World(intent=gates.order(1), book=gates.book(positions={"chan": {GLD: 5}}))
    bars = {
        i: {"1d": tuple(replace(b, basis="raw", source_as_of=None) for b in by["1d"][-1:])}
        for i, by in world.snapshot.bars.items()
    }
    later = DAYS[DAYS.index(gates.TODAY) + 1]
    known = replace(
        made.NO_SPLITS,
        rows={GLD: record.known(GLD, gates.DECISION)},
        covered_from={GLD: later},
    )
    assert world.at(snapshot=replace(world.snapshot, bars=bars, splits=known)).check().approved


@pytest.mark.parametrize("side", ["bid", "ask"])
@pytest.mark.parametrize(
    "price", [None, Decimal("NaN"), Decimal("Infinity"), Decimal(0), Decimal(-1)]
)
def test_6_a_quote_with_an_unusable_side_meets_no_step_and_raises_nothing(
    side: str, price: object
) -> None:
    """Its own check halts on such a side, as `core.quote_faults` does."""
    found = snap(flat(), quoted="100.0")
    found = replace(found, quotes={USO: replace(found.quotes[USO], **{side: price})})
    assert not any("midpoint" in r for r in check_splits(found, TABLE))


def test_6_a_halt_on_the_decisions_own_pair_names_no_covered_row_as_its_release() -> None:
    """No covered row can reach the decision's own session, which ends 5 sessions before it."""
    found = snap(flat(), quoted="50.0")
    found = replace(found, quotes={USO: quote(USO, Decimal("49.99"), Decimal("50.01"), AT)})
    no_price = snap(flat(), quoted="abc")
    days = list(flat())
    covers = tuple(cover(USO, p, s, "100.0") for p, s in zip(days, days[1:], strict=False))
    stated_none = snap(flat(), covers=covers, quoted="100.0")
    stated_none = replace(
        stated_none,
        splits=replace(
            stated_none.splits,
            pairs=(*covers, replace(stated_none.splits.pairs[-1], close_prices=(), source=None)),
        ),
    )
    reasons = [
        *check_splits(found, TABLE),
        *check_splits(no_price, TABLE),
        *check_splits(stated_none, TABLE),
    ]
    # The residual and the midpoint step, a close that is no price, and no close stated.
    assert len(reasons) == 4
    for reason in reasons:
        assert "stays halted for this session" in reason
        assert "covered row reaching" not in reason


def test_9_a_run_of_unreached_pairs_ending_at_the_own_pair_keeps_its_covered_row_release() -> None:
    """The stored pairs name the covered row that releases them, and the own pair its session."""
    days = list(flat(3))
    covers = (cover(USO, days[0], days[1], "100.0"), cover(USO, days[1], days[2]))
    found = snap(flat(3), covers=covers, quoted="100.0")
    own = replace(found.splits.pairs[-1], close_prices=(), source=None)
    found = replace(found, splits=replace(found.splits, pairs=(*covers, own)))
    stored, today = check_splits(found, TABLE)
    assert f"the sessions {days[1]} and {days[2]}" in stored
    assert f"A covered row reaching {days[2]} releases it" in stored
    assert f"the sessions {days[2]} and {DAY}" in today and "stays halted for this session" in today


def test_9_a_faulted_pair_ends_a_run_of_unreached_pairs() -> None:
    """Each run's reason counts the pairs it names, and a fault in between names its own."""
    days = list(flat(4))
    covers = (
        cover(USO, days[0], days[1]),
        replace(cover(USO, days[1], days[2]), fault="a made-up fault"),
        cover(USO, days[2], days[3]),
    )
    found = check_splits(snap(flat(4), covers=covers), TABLE)
    assert found == [
        found[0],
        "a made-up fault",
        found[2],
    ]
    assert f"the sessions {days[0]} and {days[1]}" in found[0]
    assert f"the sessions {days[2]} and {days[3]}" in found[2]
