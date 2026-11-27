"""The stress move's calculation, the vintage's hash, the record and the hold, as #116 names.

Every figure is made up but Chan's, whose vintages the test lake fetches. Chan's are
exploratory, measured over 2006-04-26 to 2012-04-09.
"""

from __future__ import annotations

import csv
import dataclasses
import hashlib
import math
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from ithildinruntime.core import (
    STRESS_MOVES,
    Bar,
    FlaggedBar,
    Halt,
    HardCaps,
    InstrumentId,
    LeftOut,
    LegSeries,
    Series,
    StressRecord,
    hold_to_record,
    largest_move,
    leg_series,
    load_records,
    moves,
    read_records,
    stress_vintage,
)
from tests.support.gates import (
    GLD,
    LOOSE,
    OTHER,
    SPY,
    USO,
    VINTAGE,
    caps,
    constraints,
    d,
    stress_record,
)
from tests.support.spreads import DAYS, bar

T0 = DAYS.index(date(2012, 1, 3))
KEY = "bars/ticker={}/freq=1d/date={}.parquet"
BASES = {"a": ("adjusted", "a"), "b": ("adjusted", "b"), "r": ("raw", None)}


def leg(instrument: InstrumentId, *closes: object, series: str = "", at: int = T0) -> list[Bar]:
    """One bar a session from `at`, each of the series its letter names."""
    made = []
    for n, close in enumerate(closes):
        day = DAYS[at + n]
        sha = hashlib.sha256(f"{instrument}{day}{close}".encode()).hexdigest()
        basis, source = BASES[series[n] if series else "a"]
        made.append(
            replace(
                bar(instrument, day, Decimal(str(close))),
                vintage_id=f"{KEY.format(instrument, day)}#sha256={sha}#version=v1",
                basis=basis,
                source=source,
            )
        )
    return made


def legs(gold: Sequence[object], oil: Sequence[object], **series: str) -> dict:
    return {
        GLD: leg(GLD, *gold, series=series.get("gld", "")),
        USO: leg(USO, *oil, series=series.get("uso", "")),
    }


def found(bars: Mapping, n: int = 2) -> tuple:
    return tuple(largest_move(bars, n).move)


# 1. The calculation.

FLAT = (100, 100, 100)
MOVES = {
    "a-rise": (FLAT, (100, 90, 110), USO, "0.10000000"),
    "a-fall": (FLAT, (100, 110, 90), USO, "0.10000000"),
    "the-other-leg": ((100, 100, 80), FLAT, GLD, "0.20000000"),
    "a-fall-larger-than-a-rise": ((100, 100, 79), (100, 100, 120), GLD, "0.21000000"),
    "a-ninth-place-on-a-rise": (FLAT, (3, 3, 4), USO, "0.33333334"),
    "a-ninth-place-on-a-fall": (FLAT, (3, 3, 2), USO, "0.33333334"),
    "exact-at-the-eighth-on-a-rise": (FLAT, (10**8, 10**8, 10**8 + 1), USO, "0.00000001"),
    "exact-at-the-eighth-on-a-fall": (FLAT, (10**8, 10**8, 10**8 - 1), USO, "0.00000001"),
    "a-hair-past-the-eighth": (FLAT, (10**9, 10**9, 10**9 + 11), USO, "0.00000002"),
    # Past the digits Python's default context holds, so one rounded division alone rounds up.
    "a-hair-past-the-eighth-far-out": (
        FLAT,
        (10**30, 10**30, 10**30 + 10**22 + 1),
        USO,
        "0.00000002",
    ),
}


@pytest.mark.parametrize("name", MOVES)
def test_1_the_largest_two_session_move_rounds_up(name: str) -> None:
    gld, uso, expected, size = MOVES[name]
    assert found(legs(gld, uso)) == (d(size), expected, DAYS[T0], DAYS[T0 + 2])
    assert f"{largest_move(legs(gld, uso), 2).move.size:f}" == size


# The floors that the caps of a strategy with a fund on the S&P 500 or the Nasdaq-100 hold, from
# the index closes of October 1987 that design.md's paragraph on why those caps reach back to 1987
# quotes, by the rounding every measured move takes. The session table starts in 1993, so the
# closes stand on three of its sessions, and only their order matters.
FLOORS = {
    "the-s-and-p-500": (("298.08", "282.70", "224.84"), "0.20466927", "0.24570586"),
    "the-nasdaq-100": (("183.285", "155.65", "143.96"), "0.15077612", "0.21455657"),
}
INDEX = {"the-s-and-p-500": "S&P 500", "the-nasdaq-100": "Nasdaq-100"}


@pytest.mark.parametrize("name", FLOORS)
def test_1_the_1987_floors_are_the_index_falls_rounded_up(name: str) -> None:
    closes, one, two = FLOORS[name]
    bars = {GLD: leg(GLD, *closes)}
    assert f"{largest_move(bars, 1).move.size:f}" == one
    assert f"{largest_move(bars, 2).move.size:f}" == two


def _floor(closes: Sequence[str], multiple: int) -> str:
    """The size of the move of a fund holding `multiple` times the index's daily return, negative
    for one that moves against it, over the closes' two sessions, rounded up at eight places."""
    first, second, third = (Fraction(c) for c in closes)
    held = (1 + multiple * (second / first - 1)) * (1 + multiple * (third / second - 1))
    return f"{math.ceil(abs(1 - held) * 10**8) / Fraction(10**8):.8f}"


DOCS = Path(__file__).parents[1] / "docs"


def _section(text: str, opening: str) -> str:
    """The text from the line that opens with `opening` up to the next heading or blank line."""
    found = re.search(rf"^[ \t]*{re.escape(opening)}", text, re.MULTILINE)
    assert found, opening
    start = found.start()
    ends = [i for i in (text.find("\n#", start), text.find("\n\n", start)) if i != -1]
    return text[start : min(ends, default=len(text))]


def test_1_the_docs_quote_the_closes_and_floors_this_file_pins() -> None:
    # The owner types each floor into the hard caps by hand from what-a-person-runs.md's table of
    # floors, so that table is held row by row to the floors worked out here from the closes, and
    # to the funds each row names.
    design = (DOCS / "design.md").read_text(encoding="utf-8")
    why = _section(
        design, "**Why the caps of funds on the S&P 500 and the Nasdaq-100 reach back to 1987.**"
    )
    runs = (DOCS / "what-a-person-runs.md").read_text(encoding="utf-8")
    table = _section(runs, "| Index | Multiple | Funds such as | Floor |")
    steps = _section(runs, "1. Find each leg's floor in the table.")
    floors = {
        (INDEX[name], multiple): _floor(closes, multiple)
        for name, (closes, _, _) in FLOORS.items()
        for multiple in (1, 2, 3, -1, -2, -3)
    }
    spy, qqq = floors["S&P 500", 1], floors["Nasdaq-100", 1]
    assert (spy, qqq) == (FLOORS["the-s-and-p-500"][2], FLOORS["the-nasdaq-100"][2])
    rows = re.findall(
        r"^\| (S&P 500|Nasdaq-100) \| (−?\d) \| ([^|]+) \| ([\d.]+) \|$", table, re.MULTILINE
    )
    rows = [(index, int(multiple.replace("−", "-")), *rest) for index, multiple, *rest in rows]
    assert {(index, multiple): floor for index, multiple, _, floor in rows} == floors
    assert len(rows) == len(floors)
    # The owner finds a leg's row by its ticker, so each row names the funds the ruling named.
    assert {(index, multiple): funds.strip() for index, multiple, funds, _ in rows} == {
        ("S&P 500", 1): "SPY, IVV, VOO",
        ("S&P 500", 2): "SSO",
        ("S&P 500", 3): "UPRO, SPXL",
        ("Nasdaq-100", 1): "QQQ, QQQM",
        ("Nasdaq-100", 2): "QLD",
        ("Nasdaq-100", 3): "TQQQ",
        ("S&P 500", -1): "SH",
        ("S&P 500", -2): "SDS",
        ("S&P 500", -3): "SPXU, SPXS",
        ("Nasdaq-100", -1): "PSQ",
        ("Nasdaq-100", -2): "QID",
        ("Nasdaq-100", -3): "SQQQ",
    }
    assert f"So a strategy with SPY and QQQ legs takes SPY's, {spy}." in steps
    # No code knows a leg's index or multiple, so the steps' stop is the only guard for a leg the
    # floors do not cover, and design.md's rule behind it is held with it.
    beyond = "holds less than one or more than three times its index in either direction"
    assert (
        f"Stop where a leg {beyond}, or where two legs hold one index at different multiples"
        in steps
    )
    assert f"where a leg {beyond}" in why
    assert "where two legs hold one index at different multiples" in why
    # The stop ends only on the owner's ruling, never on a strategy's own proposal.
    assert "until the owner rules on a strategy that proposes such a pair" in why
    assert "such caps are not written until the owner rules on that strategy" in steps
    assert "such as IVV or VOO for the S&P 500 and QQQM for the Nasdaq-100" in why
    assert "takes the move L gives over the same two sessions, 1 − (1 + L × r₁)(1 + L × r₂)" in why
    # Every figure with a decimal point the paragraph holds, and how many times, so a typo in one
    # of two copies fails. The percentages are the falls and the records' moves it compares them
    # with. Its dates and whole percentages are not held here.
    spx, ndx = FLOORS["the-s-and-p-500"], FLOORS["the-nasdaq-100"]
    assert Counter(re.findall(r"\d+\.\d+", why)) == Counter(
        {**dict.fromkeys((*spx[0], *ndx[0]), 1), "224.84": 2, spy: 2, qqq: 2}
        | {"20.47": 1, "24.57": 1, "15.08": 1, "21.46": 1, "14.52": 1, "17.09": 2}
        | {floors["S&P 500", 2]: 1, floors["Nasdaq-100", 3]: 1, "3.37": 1, "3.50": 1}
        | {floors["S&P 500", -1]: 1, "22.5": 1}
    )
    for fund, floor in (("SPY", spy), ("QQQ", qqq)):
        leg = f"{fund} leg write a `stress_move` of at least {floor} and a `stress_low` of {floor}"
        assert leg in why, fund
    assert "and a strategy with both legs writes the larger, SPY's" in why
    assert f"SSO, twice the S&P 500, takes {floors['S&P 500', 2]}" in why
    assert f"TQQQ, three times the Nasdaq-100, takes {floors['Nasdaq-100', 3]}" in why
    assert f"SH, against the S&P 500 at one times, takes {floors['S&P 500', -1]}" in why
    # A number on any page that starts as a floor does to its sixth place is that floor whole, so
    # a floor mistyped past that place, or cut short, fails. A number of eight places or more from
    # 0.2 to 0.3 is a floor, so one mistyped in an early place fails too.
    starts = "|".join(re.escape(f[:8]) for f in floors.values())
    for page in sorted(DOCS.rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        for named in re.findall(rf"\b0\.2\d{{7,}}\b|\b(?:{starts})\d*", text):
            assert named in floors.values(), f"{page.name} names {named}"


def test_1_a_tie_goes_to_the_earliest_date_then_to_the_first_leg_by_name() -> None:
    bars = legs((100, 100, 100, 100, 120), (100, 100, 100, 120, 110))
    assert found(bars) == (d("0.2"), USO, DAYS[T0 + 1], DAYS[T0 + 3])
    tied = legs((100, 100, 120), (100, 100, 80))
    assert found(tied) == (d("0.2"), GLD, DAYS[T0], DAYS[T0 + 2])
    assert found(dict(reversed(tied.items()))) == found(tied)
    # Two moves that differ only past the eighth place tie, and the earlier one is named.
    close = legs((1, 1, 1, 1, 1), (10**10, 10**10, 10**10 + 1, 10**10 + 2, 10**10))
    assert found(close) == (d("0.00000001"), USO, DAYS[T0], DAYS[T0 + 2])


def test_1_one_session_can_move_further_than_two() -> None:
    bars = legs((100, 100, 100, 100), (100, 130, 120, 120))
    assert found(bars, 1) == (d("0.3"), USO, DAYS[T0], DAYS[T0 + 1])
    assert found(bars, 2) == (d("0.2"), USO, DAYS[T0], DAYS[T0 + 2])


SEVEN = (100, 101, 102, 103, 104, 105, 106)
UP = (100, 101, 102, 816, 824, 832, 840)


def out(leg: InstrumentId, *pairs: tuple[int, int]) -> list[LeftOut]:
    return [LeftOut(leg, b - a, DAYS[T0 + a], DAYS[T0 + b]) for a, b in pairs]


def test_1_a_seam_on_one_leg_is_left_out_at_both_lengths() -> None:
    bars = legs(SEVEN, UP, uso="aaabbbb")
    assert list(largest_move(bars, 2).left_out) == out(USO, (1, 3), (2, 4))
    assert list(largest_move(bars, 1).left_out) == out(USO, (2, 3))
    assert largest_move(bars, 2).move.size == d("0.02000000")


def test_1_a_seam_on_both_legs_is_left_out_on_both() -> None:
    bars = legs(UP, UP, gld="aaabbbb", uso="aaabbbb")
    assert list(largest_move(bars, 2).left_out) == [
        *out(GLD, (1, 3)),
        *out(USO, (1, 3)),
        *out(GLD, (2, 4)),
        *out(USO, (2, 4)),
    ]
    assert list(largest_move(bars, 1).left_out) == [*out(GLD, (2, 3)), *out(USO, (2, 3))]


def test_1_a_seam_at_the_first_and_at_the_last_session() -> None:
    first = legs(SEVEN, UP, uso="abbbbbb")
    assert list(largest_move(first, 2).left_out) == out(USO, (0, 2))
    assert list(largest_move(first, 1).left_out) == out(USO, (0, 1))
    last = legs(SEVEN, UP, uso="aaaaaab")
    assert list(largest_move(last, 2).left_out) == out(USO, (4, 6))
    assert list(largest_move(last, 1).left_out) == out(USO, (5, 6))


def test_1_a_series_that_changes_and_changes_back_is_left_out_whole() -> None:
    """Both ends of the move from the second session agree, and the session between does not."""
    bars = legs(SEVEN, (100, 100, 800, 100, 100, 100, 101), uso="aabaaaa")
    assert list(largest_move(bars, 2).left_out) == out(USO, (0, 2), (1, 3), (2, 4))
    assert list(largest_move(bars, 1).left_out) == out(USO, (1, 2), (2, 3))
    ends = legs(SEVEN, (100, 100, 800, 190, 190, 190, 190), uso="aabaaaa")
    assert largest_move(ends, 2).move.size == d("0.02000000")


def test_1_the_basis_alone_changes_a_series() -> None:
    bars = legs(SEVEN, UP, uso="aaarrrr")
    assert list(largest_move(bars, 1).left_out) == out(USO, (2, 3))


def _missing(bars: dict, n: int) -> dict:
    return {**bars, USO: [b for b in bars[USO] if b.session != DAYS[T0 + n]]}


def _closed(bars: dict, close: str) -> dict:
    changed = [replace(b, close=d(close)) if n == 3 else b for n, b in enumerate(bars[USO])]
    return {**bars, USO: changed}


WHOLE = legs(SEVEN, SEVEN)
HALTS = {
    "no-leg": ({}, "no leg has a bar"),
    "a-leg-with-no-bar": ({**WHOLE, USO: []}, "USO has no bar"),
    "a-session-missing": (_missing(WHOLE, 3), "USO has no bar for 2012-01-06"),
    "a-session-repeated": (
        {**WHOLE, USO: [*WHOLE[USO], WHOLE[USO][2]]},
        "USO has two bars for 2012-01-05",
    ),
    "an-earlier-last": (_missing(WHOLE, 6), "USO's last bar is for 2012-01-10, and GLD's for"),
    "a-zero-close": (_closed(WHOLE, "0"), "USO's close for 2012-01-06 is 0, not above zero"),
    "a-negative-close": (_closed(WHOLE, "-1"), "USO's close for 2012-01-06 is -1, not above zero"),
    "a-close-not-finite": (_closed(WHOLE, "NaN"), "USO's close for 2012-01-06 is .*NaN"),
    "two-sessions": (legs((1, 2), (1, 2)), "GLD has no 2-session move left"),
    "no-move-of-one-series": (
        legs((1, 2, 3), (1, 2, 3), uso="abb"),
        "USO has no 2-session move left through 2012-01-05",
    ),
    "a-move-of-zero": (legs(FLAT, FLAT), "largest 2-session move of GLD, USO is zero"),
}


@pytest.mark.parametrize("name", HALTS)
def test_1_each_halt_of_the_measure(name: str) -> None:
    bars, why = HALTS[name]
    with pytest.raises(Halt, match=why):
        largest_move(bars, 2)


def test_1_legs_may_begin_on_different_sessions_and_neither_is_cut() -> None:
    """GLD's move over its first three sessions sets m, and USO begins after it."""
    bars = {GLD: leg(GLD, 100, 100, 70, 70, 70, 70), USO: leg(USO, 10, 10, 11, at=T0 + 3)}
    assert found(bars) == (d("0.3"), GLD, DAYS[T0], DAYS[T0 + 2])
    assert found({**bars, GLD: bars[GLD][3:], USO: bars[USO]}) == (
        d("0.1"),
        USO,
        DAYS[T0 + 3],
        DAYS[T0 + 5],
    )


def test_1_moves_gives_one_legs_moves_and_reads_a_still_leg_as_zero() -> None:
    (still,) = moves(leg(GLD, 100, 101, 100), 2).kept
    assert still == (d("0"), GLD, DAYS[T0], DAYS[T0 + 2])
    across = moves(leg(GLD, 100, 101, 102, series="abb"), 2)
    assert across == ((), tuple(out(GLD, (0, 2))))
    assert moves([], 2) == moves(leg(GLD, 1, 2), 2) == ((), ())
    assert [m.size for m in moves(leg(GLD, 100, 50, 100), 1).kept] == [d("0.5"), d(1)]


@pytest.mark.parametrize("name", ["a-session-missing", "a-session-repeated", "a-zero-close"])
def test_1_moves_halts_on_its_own(name: str) -> None:
    bars, why = HALTS[name]
    with pytest.raises(Halt, match=why):
        moves(bars[USO], 2)


def test_1_a_bar_on_a_day_that_is_no_session_halts() -> None:
    saturday = replace(WHOLE[USO][3], session=date(2012, 1, 7))
    with pytest.raises(Halt, match="USO has a bar for 2012-01-07, which is no session"):
        largest_move({**WHOLE, USO: [*WHOLE[USO], saturday]}, 2)


def chan(vintages: Path, name: str, instrument: InstrumentId) -> list[Bar]:
    with open(vintages / name, newline="") as handle:
        rows = list(csv.reader(handle))[3:]
    return [bar(instrument, date.fromisoformat(r[0]), Decimal(r[1])) for r in rows]


def test_1_the_chan_vintages_give_the_figures_the_issue_names(vintages: Path) -> None:
    gld, uso = chan(vintages, "gld.csv", GLD), chan(vintages, "uso.csv", USO)
    both = {GLD: gld, USO: uso}
    size, named, moved_from, moved_to = found(both)
    assert (f"{size:.4f}", named) == ("0.1798", USO)
    assert (moved_from, moved_to) == (date(2008, 12, 30), date(2009, 1, 2))
    alone = largest_move({GLD: gld}, 2).move
    assert (f"{alone.size:.4f}", alone.moved_from, alone.moved_to) == (
        "0.1019",
        date(2008, 9, 15),
        date(2008, 9, 17),
    )
    # A larger one than GLD's two-session move, and a smaller one than m.
    assert largest_move(both, 1).move.leg == GLD
    one = largest_move({GLD: gld}, 1)
    assert (f"{one.move.size:.4f}", one.move.moved_to) == ("0.1129", date(2008, 9, 17))
    later = {i: [b for b in held if b.session >= date(2009, 4, 27)] for i, held in both.items()}
    assert f"{largest_move(later, 2).move.size:.4f}" == "0.1080"


# 2. The vintage.

ONE = legs((100, 101, 102), (30, 31, 32))


def swapped(**changes: object) -> dict:
    return {**ONE, USO: [replace(ONE[USO][0], **changes), *ONE[USO][1:]]}


def test_2_the_vintage_is_the_hash_of_one_line_a_bar() -> None:
    lines = ""
    for instrument in (GLD, USO):
        for made in ONE[instrument]:
            sha = made.vintage_id.split("#sha256=")[1].split("#")[0]
            key = KEY.format(instrument, made.session)
            lines += f"{instrument}\t{made.session}\t{key}\t{sha}\n"
    assert stress_vintage(ONE) == hashlib.sha256(lines.encode()).hexdigest()


FIRST_USO = ONE[USO][0]
KEY_USO, REST = FIRST_USO.vintage_id.split("#", 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"instrument": SPY},
        {"session": DAYS[T0 - 1]},
        {"vintage_id": f"{KEY_USO}x#{REST}"},
        {"vintage_id": f"{KEY_USO}#sha256={'0' * 64}#version=v1"},
    ],
    ids=["instrument", "session", "key", "partition-sha256"],
)
def test_2_each_field_changed_alone_moves_the_vintage(changes: dict) -> None:
    assert stress_vintage(swapped(**changes)) != stress_vintage(ONE)


def test_2_the_bucket_version_and_the_order_of_the_bars_do_not_move_it() -> None:
    other = FIRST_USO.vintage_id.replace("#version=v1", "#version=zz#version=9")
    assert stress_vintage(swapped(vintage_id=other)) == stress_vintage(ONE)
    backward = {i: list(reversed(ONE[i])) for i in (USO, GLD)}
    assert stress_vintage(backward) == stress_vintage(ONE)


def test_2_another_hash_seed_gives_the_same_vintage() -> None:
    code = (
        "from tests.test_stress import ONE\n"
        "from ithildinruntime.core import stress_vintage\n"
        "print(stress_vintage(ONE))\n"
    )
    for seed in ("1", "2"):
        ran = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).parents[1],
            env={"PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        )
        assert ran.stdout.strip() == stress_vintage(ONE)


@pytest.mark.parametrize(
    "made", ["v1", "", f"k#sha256={'A' * 64}#version=1", "k#sha256=ab#version="]
)
def test_2_a_bar_whose_vintage_names_no_partition_halts(made: str) -> None:
    with pytest.raises(Halt, match="USO's bar for 2012-01-03 has a vintage_id"):
        stress_vintage(swapped(vintage_id=made))


def test_2_a_legs_series_are_its_runs() -> None:
    bars = legs(SEVEN, SEVEN, uso="aabaarr")
    runs = [
        (r.instrument, r.series.basis, r.series.source, r.first, r.last) for r in leg_series(bars)
    ]
    day = lambda n: DAYS[T0 + n]  # noqa: E731
    assert runs == [
        (GLD, "adjusted", "a", day(0), day(6)),
        (USO, "adjusted", "a", day(0), day(1)),
        (USO, "adjusted", "b", day(2), day(2)),
        (USO, "adjusted", "a", day(3), day(4)),
        (USO, "raw", None, day(5), day(6)),
    ]


# 4. The record.

SAMPLE = """\
[[record]]
strategy = "chan"
instruments = ["GLD", "USO"]
vintage = "{vintage}"
through = 2026-10-30
stress_move = "0.17980000"
leg = "USO"
moved_from = 2008-12-30
moved_to = 2009-01-02
one_session = "0.11290000"
one_session_leg = "GLD"
one_session_on = 2008-09-17
series = [
    {{ instrument = "GLD", first = 2006-04-26, last = 2012-04-09, basis = "adjusted", \
source = "o", source_as_of = 2012-04-10 }},
    {{ instrument = "GLD", first = 2012-04-10, last = 2026-10-30, basis = "adjusted", \
source = "s", source_as_of = 2026-11-22 }},
    {{ instrument = "USO", first = 2006-04-26, last = 2026-10-30, basis = "adjusted", \
source = "s", source_as_of = 2026-11-22 }},
]
left_out = [{{ instrument = "USO", sessions = 1, moved_from = 2012-04-09, \
moved_to = 2012-04-10 }}]
flagged = [{{ instrument = "USO", session = 2008-12-31, field = "close", \
rule = "r", what = "a real move" }}]
manifest = "{manifest}"
measured_on = 2026-11-24
""".format(vintage=VINTAGE, manifest="b" * 64)


def read(tmp_path: Path, text: str) -> tuple[StressRecord, ...]:
    path = tmp_path / "moves.toml"
    path.write_text(text, encoding="utf-8")
    return load_records(path)


def test_4_a_record_reads_field_by_field(tmp_path: Path) -> None:
    (made,) = read(tmp_path, SAMPLE)
    assert (made.strategy, made.instruments, made.vintage) == ("chan", (GLD, USO), VINTAGE)
    assert made.through == date(2026, 10, 30)
    assert (made.stress_move, made.one_session) == (d("0.1798"), d("0.1129"))
    assert made.series[1].series == ("adjusted", "s", date(2026, 11, 22))
    assert made.left_out == (LeftOut(USO, 1, date(2012, 4, 9), date(2012, 4, 10)),)
    assert made.flagged[0].what == "a real move" and made.withdrawn is None
    (gone,) = read(tmp_path, SAMPLE + 'withdrawn = "mistyped"\n')
    assert gone == replace(made, withdrawn="mistyped")


def test_4_a_one_session_move_equal_to_m_reads(tmp_path: Path) -> None:
    """m is the larger of the two lengths, so a one-session move may equal it and not pass it."""
    (made,) = read(tmp_path, put('"0.11290000"', '"0.17980000"'))
    assert made.one_session == made.stress_move == d("0.1798")


def test_4_a_withdrawn_record_whose_one_session_move_is_above_m_reads(tmp_path: Path) -> None:
    """A record found wrong for that reason gains `withdrawn`, and the file must still read."""
    wrong = put('"0.11290000"', '"0.17980001"') + 'withdrawn = "m read two sessions alone"\n'
    (made,) = read(tmp_path, wrong)
    assert made.one_session > made.stress_move and made.withdrawn == "m read two sessions alone"
    assert not made.counts_for("chan", (GLD, USO))


KEYS = [line.split(" = ")[0] for line in SAMPLE.splitlines() if " = " in line and line[0] != " "]


def without(key: str) -> str:
    lines, skipping = [], False
    for line in SAMPLE.splitlines():
        if line.startswith(f"{key} = "):
            skipping = line.endswith("[")
            continue
        if skipping:
            skipping = line != "]"
            continue
        lines.append(line)
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("key", KEYS)
def test_4_each_key_left_out_halts_naming_it(tmp_path: Path, key: str) -> None:
    with pytest.raises(Halt) as caught:
        read(tmp_path, without(key))
    assert caught.value.reason == f"moves.toml record 1 holds no {key}"


def test_4_every_field_of_the_record_is_a_key() -> None:
    names = {f.name for f in dataclasses.fields(StressRecord)}
    # A record measured before data/splits.csv existed holds neither of its two fields.
    assert set(KEYS) == names - {"withdrawn", "splits", "split_rows"}


def put(old: str, new: str) -> str:
    assert old in SAMPLE
    return SAMPLE.replace(old, new)


LONG = "x" * 10_000
MORE = (
    '    { instrument = "SPY", first = 2006-04-26, last = 2026-10-30, basis = "adjusted", '
    'source = "s", source_as_of = 2026-11-22 },\n'
)
BAD = {
    "an-unknown-key": (SAMPLE + "x = 1\n", "record 1 holds 'x', which"),
    "a-top-level-key": ("x = 1\n" + SAMPLE, "moves.toml holds 'x', which"),
    "record-of-no-list": ("[record]\nx = 1\n", "holds record as a dict, not a list"),
    "a-record-of-no-table": ("record = [1]\n", "record 1 is a int, not a table"),
    "a-name": (put('strategy = "chan"', 'strategy = "a/b"'), "record 1's strategy is"),
    "no-legs": (put('["GLD", "USO"]', "[]"), "record 1's instruments is"),
    "legs-out-of-order": (put('["GLD", "USO"]', '["USO", "GLD"]'), "record 1's instruments is"),
    "a-leg-twice": (put('["GLD", "USO"]', '["GLD", "GLD"]'), "record 1's instruments is"),
    "a-vintage": (put(VINTAGE, "A" * 64), "record 1's vintage is"),
    "a-through-of-text": (put("through = 2026-10-30", 'through = "2026-10-30"'), "1's through is"),
    "a-through-with-a-time": (
        put("through = 2026-10-30", "through = 2026-10-30T00:00:00Z"),
        "1's through is",
    ),
    "a-float-m": (put('"0.17980000"', "0.17980000"), "1's stress_move is"),
    "seven-places": (put('"0.17980000"', '"0.1798000"'), "1's stress_move is"),
    "nine-places": (put('"0.17980000"', '"0.179800000"'), "1's stress_move is"),
    "a-zero-m": (put('"0.17980000"', '"0.00000000"'), "1's stress_move is"),
    "a-negative-m": (put('"0.17980000"', '"-0.17980000"'), "1's stress_move is"),
    "an-infinite-m": (put('"0.17980000"', '"Infinity"'), "1's stress_move is"),
    "a-one-session-of-no-form": (put('"0.11290000"', '"x"'), "1's one_session is"),
    "a-one-session-above-m": (
        put('"0.11290000"', '"0.17980001"'),
        "1's one_session is 0.17980001, above its stress_move of 0.17980000",
    ),
    "a-leg-of-no-text": (put('leg = "USO"\nmoved', "leg = 1\nmoved"), "1's leg is 1"),
    "a-flag-of-no-what": (put("a real move", ""), "1's flagged 1's what is ''"),
    "a-flag-of-a-blank-what": (put("a real move", "  "), "1's flagged 1's what is"),
    "a-flag-with-no-rule": (put(', rule = "r"', ""), "flagged 1 holds no rule"),
    "a-left-out-unknown": (
        put("moved_to = 2012-04-10", "moved_to = 2012-04-10, x = 1"),
        "holds 'x'",
    ),
    "left-out-of-no-list": (
        put(
            'left_out = [{ instrument = "USO", sessions = 1, moved_from = 2012-04-09, '
            "moved_to = 2012-04-10 }]",
            "left_out = 1",
        ),
        "1's left_out is 1, not a list",
    ),
    "left-out-over-three": (put("sessions = 1", "sessions = 3"), "left_out 1's sessions is 3"),
    "left-out-over-true": (put("sessions = 1", "sessions = true"), "left_out 1's sessions is True"),
    "a-flag-with-no-field": (put('field = "close", ', ""), "flagged 1 holds no field"),
    "an-old-key": (
        put("through =", "start = 2006-04-26\nthrough ="),
        "record 1 holds 'start', which",
    ),
    "two-of-one-vintage-on-other-legs": (
        SAMPLE
        + re.sub('    { instrument = "GLD".*\\n', "", SAMPLE.replace('"GLD", "USO"', '"USO"')),
        "record 2 has the strategy and vintage of record 1",
    ),
    "a-series-of-no-basis": (
        put('2012-04-09, basis = "adjusted", ', "2012-04-09, "),
        "series 1 holds no basis",
    ),
    "a-manifest": (put("b" * 64, "b"), "1's manifest is"),
    "a-leg-of-none-of-its-instruments": (
        put('leg = "USO"\nmoved', 'leg = "SPY"\nmoved'),
        "record 1's leg is 'SPY', which is none of its instruments",
    ),
    "a-leg-of-another-case": (
        put('leg = "USO"\nmoved', 'leg = "uso"\nmoved'),
        "record 1's leg is 'uso', which is none of its instruments",
    ),
    "a-through-after-its-series": (
        put("through = 2026-10-30", "through = 2026-11-02"),
        "record 1's series do not end at its through for each",
    ),
    "a-series-that-ends-before-its-through": (
        put(
            '"USO", first = 2006-04-26, last = 2026-10-30',
            '"USO", first = 2006-04-26, last = 2026-10-29',
        ),
        "record 1's series do not end at its through for each",
    ),
    "a-leg-of-no-series": (
        put('{ instrument = "USO", first', '{ instrument = "GLD", first'),
        "record 1's series do not end at its through for each",
    ),
    "a-series-of-no-leg": (
        put("series = [\n", "series = [\n" + MORE),
        "record 1's series do not end at its through for each",
    ),
    "a-series-that-ends-after-its-through": (
        put(
            '"USO", first = 2006-04-26, last = 2026-10-30',
            '"USO", first = 2006-04-26, last = 2026-11-02',
        ),
        "record 1's series do not end at its through for each",
    ),
    "a-long-leg": (
        put('leg = "USO"\nmoved', f'leg = "{LONG}"\nmoved'),
        "record 1's leg is 'xxxx",
    ),
    "a-long-key-of-a-record": (SAMPLE + f"{LONG} = 1\n", "record 1 holds 'xxxx"),
    "a-long-text-for-a-list": (
        put("flagged = [", f'flagged = "{LONG}" # ['),
        "record 1's flagged is 'xxxx",
    ),
    "a-key-twice-of-many-characters": (
        f"a = {{ {LONG} = 1, {LONG} = 2 }}\n" + SAMPLE,
        "moves.toml cannot be read: TOMLDecodeError",
    ),
    "a-leading-zero": (put('"0.17980000"', '"00.17980000"'), "1's stress_move is"),
    "a-number-past-any-decimal": (
        put('"0.17980000"', "1e9999999999999999999"),
        "moves.toml cannot be read: InvalidOperation",
    ),
    "a-number-of-too-many-digits": (
        put('"0.17980000"', "9" * 5000),
        "moves.toml cannot be read: ValueError",
    ),
    "a-hex-number-too-long-to-show": (
        put("through = 2026-10-30", "through = 0x" + "f" * 5000),
        "record 1's through is a huge int, not a date",
    ),
    "a-list-of-one-too-long-to-show": (
        put("through = 2026-10-30", "through = [0x" + "f" * 5000 + "]"),
        "moves.toml cannot be read: ValueError",
    ),
    "a-text-of-many-characters": (
        put("through = 2026-10-30", f'through = "{"x" * 10_000}"'),
        "record 1's through is 'xxxx",
    ),
    "a-key-of-many-characters": (f"{'x' * 10_000} = 1\n" + SAMPLE, "moves.toml holds 'xxxx"),
    "brackets-past-any-depth": (
        "x = " + "[" * 100_000,
        "moves.toml cannot be read: RecursionError",
    ),
    "an-empty-withdrawn": (SAMPLE + 'withdrawn = ""\n', "1's withdrawn is ''"),
    "two-of-one-vintage": (
        SAMPLE + SAMPLE.replace("0.17980000", "0.20000000"),
        "record 2 has the strategy and vintage of record 1",
    ),
    "not-toml": ("[[record]\n", "moves.toml cannot be read: TOMLDecodeError"),
}


@pytest.mark.parametrize("name", BAD)
def test_4_each_refusal_names_the_file_the_record_and_the_key(tmp_path: Path, name: str) -> None:
    text, why = BAD[name]
    with pytest.raises(Halt, match=why) as caught:
        read(tmp_path, text)
    assert caught.value.reason.startswith("moves.toml")
    assert str(tmp_path) not in caught.value.reason
    # The reason is stored and sent.
    assert len(caught.value.reason) < 300


def test_4_a_file_that_is_absent_unreadable_or_not_utf8_halts(tmp_path: Path) -> None:
    with pytest.raises(Halt, match="^moves.toml is missing$"):
        load_records(tmp_path / "moves.toml")
    (tmp_path / "loop.toml").symlink_to(tmp_path / "loop.toml")
    with pytest.raises(Halt, match="^loop.toml cannot be read: OSError$"):
        load_records(tmp_path / "loop.toml")
    with pytest.raises(Halt, match="^moves.toml cannot be read: IsADirectoryError$"):
        (tmp_path / "moves.toml").mkdir()
        load_records(tmp_path / "moves.toml")
    (tmp_path / "bad.toml").write_bytes(b"\xff")
    with pytest.raises(Halt, match="^bad.toml cannot be read: UnicodeDecodeError"):
        load_records(tmp_path / "bad.toml")


def test_4_an_entry_point_is_handed_the_records_or_the_halt(tmp_path: Path) -> None:
    (tmp_path / "moves.toml").write_text(SAMPLE)
    assert read_records(tmp_path / "moves.toml") == load_records(tmp_path / "moves.toml")
    halt = read_records(tmp_path / "absent.toml")
    assert isinstance(halt, Halt) and halt.reason == "absent.toml is missing"


def test_4_a_second_record_is_named_by_its_place(tmp_path: Path) -> None:
    other = SAMPLE.replace(VINTAGE, "c" * 64).replace("measured_on = 2026-11-24", "measured_on = 1")
    with pytest.raises(Halt, match="record 2's measured_on is 1, not a date"):
        read(tmp_path, SAMPLE + other)


_QT = "quantitative-trading:data/inputdata_etf/"
_YAHOO, _MOVE = (
    "yahoo:{}_daily_yahoo_2026-11-22.csv",
    "close-to-close move past the written multiple",
)
# The records the file held when each was added, by hand. Removing or changing one fails.
PINNED: list[StressRecord] = [
    # #137's first record, printed by the run of 2026-11-26 at `main`'s e4fd6a8, which
    # https://github.com/l3a0/ithildin-runtime/issues/137#issuecomment-7181369603 holds.
    StressRecord(
        strategy="chan_bollinger",
        instruments=(GLD, USO),
        vintage="c6f37344aca5871ea96601e8cca20937df017a3a893d22fe251013a3796b2153",
        through=date(2026, 9, 3),
        stress_move=d("0.34703196"),
        leg=USO,
        moved_from=date(2020, 4, 1),
        moved_to=date(2020, 4, 3),
        one_session=d("0.25315002"),
        one_session_leg=USO,
        one_session_on=date(2020, 3, 9),
        series=(
            LegSeries(
                GLD,
                date(2006, 4, 26),
                date(2012, 4, 9),
                Series("adjusted", _QT + "gld.csv", date(2012, 4, 10)),
            ),
            LegSeries(
                GLD,
                date(2012, 4, 10),
                date(2026, 9, 3),
                Series("adjusted", _YAHOO.format("gld"), date(2026, 11, 22)),
            ),
            LegSeries(
                USO,
                date(2006, 4, 26),
                date(2012, 4, 9),
                Series("adjusted", _QT + "uso.csv", date(2012, 4, 10)),
            ),
            LegSeries(
                USO,
                date(2012, 4, 10),
                date(2026, 9, 3),
                Series("adjusted", _YAHOO.format("uso"), date(2026, 11, 22)),
            ),
        ),
        left_out=tuple(
            LeftOut(leg, n, date(2012, 4, a), date(2012, 4, b))
            for leg in (GLD, USO)
            for n, a, b in ((1, 9, 10), (2, 5, 10), (2, 9, 11))
        ),
        flagged=(
            FlaggedBar(
                USO,
                date(2020, 3, 9),
                "close",
                _MOVE,
                "a move a second vendor agrees with: the close fell 25.32% from 2020-03-06, and "
                "Alpha Vantage's daily closes give the same return to six places, read on "
                "2026-11-26. Exploratory: it compares returns and not the level of either close, "
                "so a factor common to both closes changes no move, and an error both vendors "
                "share would pass unseen",
            ),
            FlaggedBar(
                USO,
                date(2020, 4, 21),
                "close",
                _MOVE,
                "a move a second vendor agrees with: the close fell 25.07% from 2020-04-20, and "
                "Alpha Vantage's daily closes give the same return to six places, read on "
                "2026-11-25. Exploratory: it compares returns and not the level of either close, "
                "so a factor common to both closes changes no move, and an error both vendors "
                "share would pass unseen",
            ),
            FlaggedBar(
                GLD,
                date(2026, 1, 30),
                "close",
                _MOVE,
                "a move a second vendor agrees with: the close fell 10.27% from 2026-01-29, and "
                "Massive's daily aggregates give the same return to six places, read on "
                "2026-11-26. Exploratory: it compares returns and not the level of either close, "
                "so a factor common to both closes changes no move, and an error both vendors "
                "share would pass unseen",
            ),
        ),
        manifest="d6b1bc2dab67329a4963fab86b4ce94b14c79c4b763a18b37c50b251b4e2d77b",
        measured_on=date(2026, 11, 26),
    ),
]


def test_4_the_tracked_file_reads_and_holds_every_record_pinned_here() -> None:
    assert list(load_records(STRESS_MOVES)) == PINNED


# 5. The hold.

M = d("0.20000000")
STEP = d("0.00000001")
C = constraints()
OWN = caps(stress_move=M)
R = stress_record(stress_move=M)
NEWER, OLDER = "c" * 64, "d" * 64


def raised(size: object, **changes: object) -> StressRecord:
    return stress_record(**{"vintage": NEWER, "stress_move": d(size)} | changes)


def test_5_the_recorded_move_holds_and_gives_the_caps_back() -> None:
    assert hold_to_record(C, OWN, (R,)) == OWN
    above = caps(stress_move=M + STEP)
    assert hold_to_record(C, above, (R,)) == above


REFUSED = {
    "one-step-below": (
        OWN,
        (stress_record(stress_move=M + STEP),),
        "stress_move is 0.20000000, below",
    ),
    "another-vintage": (
        OWN,
        (stress_record(vintage=NEWER),),
        "stress_vintage is a+, of no record of chan",
    ),
    "another-strategys-vintage": (
        OWN,
        (stress_record(strategy=OTHER),),
        "stress_vintage is a+, of no record of chan",
    ),
    "other-legs": (
        OWN,
        (stress_record(instruments=(GLD, SPY)),),
        "stress_vintage is a+, whose record and the constraints do not both name SPY",
    ),
    "fewer-legs": (OWN, (stress_record(instruments=(GLD,)),), "do not both name USO"),
    "more-legs": (OWN, (stress_record(instruments=(GLD, SPY, USO)),), "do not both name SPY"),
    "another-last-session": (
        OWN,
        (stress_record(through=date(2026, 10, 29)),),
        "stress_through is 2026-10-30, and its record's is 2026-10-29",
    ),
    "a-later-last-session": (
        OWN,
        (stress_record(through=date(2026, 11, 2)),),
        "stress_through is 2026-10-30, and its record's is 2026-11-02",
    ),
    "no-record": (OWN, (), "there is no stress record"),
    "no-records-at-all": (OWN, None, "there is no stress record"),
    "a-readers-halt": (OWN, Halt("moves.toml is missing"), "^moves.toml is missing$"),
    "a-withdrawn-record": (
        OWN,
        (stress_record(withdrawn="read the wrong bars"),),
        "stress_vintage is a+, whose record is withdrawn: read the wrong bars",
    ),
    "a-larger-one-past-stress-high": (
        OWN,
        (R, raised("0.60000001")),
        f"stress_high is 0.6, below the stress_move of 0.60000001 .* {NEWER}",
    ),
}


@pytest.mark.parametrize("name", REFUSED)
def test_5_each_halt_of_the_hold(name: str) -> None:
    owners, records, why = REFUSED[name]
    with pytest.raises(Halt, match=why):
        hold_to_record(C, owners, records)


@pytest.mark.parametrize("vintage", [OLDER, NEWER])
@pytest.mark.parametrize("backward", [False, True])
def test_5_a_larger_record_of_the_same_legs_raises_the_move(vintage: str, backward: bool) -> None:
    through = date(2026, 10, 1) if vintage == OLDER else date(2026, 11, 2)
    records = (R, raised("0.35", vintage=vintage, through=through), raised("0.3", vintage="e" * 64))
    held = hold_to_record(C, OWN, records[::-1] if backward else records)
    assert held == replace(OWN, stress_move=d("0.35"))
    # Twice is once.
    assert hold_to_record(C, held, records) == held


def test_5_one_equal_to_stress_high_is_taken_and_one_step_above_halts() -> None:
    assert hold_to_record(C, OWN, (R, raised("0.6"))).stress_move == d("0.6")
    with pytest.raises(Halt, match="stress_high"):
        hold_to_record(C, OWN, (R, raised(d("0.6") + STEP)))


@pytest.mark.parametrize(
    "other",
    [
        raised("0.5", strategy=OTHER),
        raised("0.5", instruments=(GLD, SPY, USO), leg=SPY),
        raised("0.9", instruments=(SPY,), leg=SPY),
        raised("0.5", withdrawn="x"),
        # Past stress_high, and listed all the same.
        raised("0.9", strategy=OTHER),
        raised("0.9", withdrawn="x"),
    ],
    ids=[
        "another-strategys",
        "another-leg",
        "another-leg-past-high",
        "withdrawn",
        "another-past-high",
        "withdrawn-past-high",
    ],
)
def test_5_a_larger_record_that_does_not_count_is_not_taken(other: StressRecord) -> None:
    assert hold_to_record(C, OWN, (R, other)) == hold_to_record(C, OWN, (other, R)) == OWN


@pytest.mark.parametrize("instruments", [(GLD, SPY, USO), (USO,)], ids=["one-more", "one-fewer"])
def test_5_a_larger_record_whose_leg_is_held_counts_whatever_its_other_legs(instruments) -> None:
    other = raised("0.35", instruments=instruments, leg=USO)
    for records in ((R, other), (other, R)):
        assert hold_to_record(C, OWN, records).stress_move == d("0.35")
    past = raised("0.7", instruments=instruments, leg=USO)
    with pytest.raises(Halt, match="stress_high"):
        hold_to_record(C, OWN, (R, past))


def test_5_a_move_above_every_record_is_kept() -> None:
    assert hold_to_record(C, caps(stress_move=d("0.4")), (R, raised("0.3"))).stress_move == d("0.4")


@pytest.mark.parametrize("field", [f.name for f in dataclasses.fields(HardCaps)])
@pytest.mark.parametrize("value", [None, Decimal("NaN"), Decimal(-1)], ids=["absent", "nan", "out"])
def test_5_each_field_of_the_caps_halts_as_tighten_does(field: str, value: object) -> None:
    broken = replace(OWN, **{field: value})
    with pytest.raises(Halt, match=f"the hard caps.* {field}") as caught:
        # No record, so a check made after the records would name them.
        hold_to_record(C, broken, ())
    assert "record" not in caught.value.reason


def test_5_the_pair_is_checked_first() -> None:
    with pytest.raises(Halt, match="the constraints are missing"):
        hold_to_record(None, None, ())  # type: ignore[arg-type]
    with pytest.raises(Halt, match="the hard caps are missing"):
        hold_to_record(C, None, ())  # type: ignore[arg-type]
    with pytest.raises(Halt, match="the constraints' max_net"):
        hold_to_record(constraints(max_net=d(-1)), OWN, ())
    three = {GLD: LOOSE, USO: LOOSE, SPY: LOOSE}
    with pytest.raises(Halt, match="the constraints limit SPY, which the hard caps do not"):
        hold_to_record(constraints(max_position=three, max_order_qty=three), OWN, ())


# 13 of #189. The record of splits a stress record names.

SPLIT_LINES = (
    'splits = "' + "b" * 64 + '"\n'
    "split_rows = [\n"
    '    { instrument = "USO", first_session = 2020-04-29, new_shares = 1, old_shares = 8 },\n'
    "]\n"
)


def test_13_a_record_reads_the_record_of_splits_it_names(tmp_path: Path) -> None:
    from ithildinruntime.core import KnownSplit

    (named,) = read(tmp_path, SAMPLE + SPLIT_LINES)
    assert named.splits == "b" * 64
    assert named.split_rows == (KnownSplit(InstrumentId("USO"), date(2020, 4, 29), 1, 8),)
    (old,) = read(tmp_path, SAMPLE)
    assert (old.splits, old.split_rows) == (None, ())


@pytest.mark.parametrize(
    ("text", "said"),
    [
        (SAMPLE + SPLIT_LINES.split("\n", 1)[1], "names split rows and not the SHA-256"),
        (SAMPLE + SPLIT_LINES.replace("new_shares = 1", "new_shares = 0"), "new_shares is 0"),
        (SAMPLE + SPLIT_LINES.replace('"' + "b" * 64, '"' + "B" * 64), "splits is"),
    ],
)
def test_13_a_bad_record_of_splits_halts(tmp_path: Path, text: str, said: str) -> None:
    with pytest.raises(Halt, match=said):
        read(tmp_path, text)
