"""A lookback over Schwab's imported bars and captured ones after them, as #175's acceptance names.

Every price is made up and drawn from a seeded generator. The lake is one
marketlake's own import writes. Its bars are each fund's Yahoo series to
2026-09-03, Schwab's import from 2026-09-04 to 2026-10-30, `adjusted` as of
2026-11-02, and captured bars from 2026-11-02 on. Each decision runs data's and
alpha's stages under the tracked strategy and the tracked price-kind rows,
through `tests/test_captured_bars.py`'s `decide`, which makes up the quotes and
their kind. The numbers in each test's name are the acceptance items' numbers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.core import Bar, InstrumentId, Snapshot, moves, series_of
from ithildinruntime.data.reference import SCHWAB_IMPORT, KindRow, PriceKinds
from ithildinruntime.data.source import judge
from ithildinruntime.data.spikes import MOVE
from tests.support import made_lake as made
from tests.support.spreads import GLD, USO, synthetic
from tests.test_captured_bars import KINDS, Decided, decide

SEAM = date(2026, 11, 2)
YAHOO = "yahoo:{}_daily_yahoo_2026-11-22.csv"
EARLIER = made.days(date(2026, 6, 1), date(2026, 9, 3))
IMPORTED = made.days(date(2026, 9, 4), date(2026, 10, 30))
CAPTURED = made.days(SEAM, date(2027, 1, 29))
# The sessions whose decision at close−20 reads the import and capture alone.
DECIDED = made.days(date(2026, 12, 1), date(2027, 1, 28))
CAPTURED_SERIES = "raw from no source as of no date"


def _walks() -> dict[str, dict[date, Decimal]]:
    days = [*EARLIER, *IMPORTED, *CAPTURED]
    drawn = synthetic(len(days), seed=17)
    return {
        "GLD": {d: bar.close for d, bar in zip(days, drawn.gld, strict=True)},
        "USO": {d: bar.close for d, bar in zip(days, drawn.uso, strict=True)},
    }


WALKS = _walks()


def built(
    root: Path,
    source: str = SCHWAB_IMPORT,
    as_of: date = SEAM,
    scale: Mapping[str, Decimal] | None = None,
) -> Path:
    """The lake, its import from `source` as of `as_of`, and each leg's import times `scale`."""
    by = scale or {}
    return made.build(
        root,
        [
            made.Made(t, name.format(t.lower()), dated, closes)
            for t, w in WALKS.items()
            for name, dated, closes in (
                (YAHOO, date(2026, 11, 22), {d: w[d] for d in EARLIER}),
                (
                    source.replace("{}", t),
                    as_of,
                    {d: w[d] * by.get(t, Decimal(1)) for d in IMPORTED},
                ),
            )
        ],
        {t: {d: w[d] for d in CAPTURED} for t, w in WALKS.items()},
    )


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return built(tmp_path_factory.mktemp("one-scale") / "lake")


@pytest.fixture(scope="module")
def first(lake: Path, tmp_path_factory: pytest.TempPathFactory) -> Decided:
    """The first decision whose window holds the import and capture alone, then its redelivery."""
    return decide(lake, tmp_path_factory.mktemp("first"), DECIDED[0], walks=WALKS, redeliver=True)


def reason(leg: str, one: date, two: date, before: str, after: str) -> str:
    return f"{leg}'s bars for {one} and {two} are of two series, {before} and {after}"


def series_halts(found: Decided) -> list[str]:
    halt = found.data.halt
    return [] if halt is None else [r for r in halt.reason.split("; ") if "two series" in r]


# 1. A window over the pair


def test_1_a_decision_at_close_20_reads_a_window_over_the_import_and_capture(
    first: Decided,
) -> None:
    assert first.data.halt is None and first.alpha.halt is None
    assert first.forecast is not None and first.forecast.direction in (-1, 0, 1)
    assert first.snapshot.flags == {}
    for leg in (GLD, USO):
        bars = first.snapshot.bars[leg]["1d"]
        assert [b.session for b in bars] == [*IMPORTED, *made.days(SEAM, date(2026, 11, 30))]
        assert len(bars) == 60
        assert {(b.origin, *series_of(b)) for b in bars} == {
            ("imported", "adjusted", SCHWAB_IMPORT, SEAM),
            ("captured", "raw", None, None),
        }
        # Every bar reads a kind the tracked rows state.
        assert {(b.price_kind, b.venues, b.delay, b.volume_counted) for b in bars} == {
            ("official close", "consolidated", "none", "consolidated in shares as of the fetch")
        }


def test_1_its_redelivery_judges_the_stored_snapshot_and_passes_it(first: Decided) -> None:
    assert first.redelivered is not None and first.redelivered.halt is None
    assert judge(first.snapshot, (GLD, USO), measured=False) == ()


# 2. Every other change halts as before

# Each import that differs from the pair in one way, with the series it is of.
OTHER_IMPORTS = {
    "another-source": ("made-up:two-{}.csv", SEAM),
    "a-day-earlier": (SCHWAB_IMPORT, date(2026, 10, 30)),
    "a-day-later": (SCHWAB_IMPORT, date(2026, 11, 3)),
}


@pytest.mark.parametrize("name", OTHER_IMPORTS)
def test_2_an_import_of_another_source_or_date_halts_and_so_does_its_redelivery(
    tmp_path: Path, name: str
) -> None:
    source, as_of = OTHER_IMPORTS[name]
    lake = built(tmp_path / "lake", source, as_of)
    stated = KindRow(as_of, "", IMPORTED[0], IMPORTED[-1], made.STATED, "a test")
    kinds = PriceKinds(
        (*KINDS.rows, *(replace(stated, source=source.replace("{}", t)) for t in WALKS))
    )
    found = decide(lake, tmp_path, DECIDED[0], kinds=kinds, walks=WALKS, redeliver=True)
    expected = [
        reason(
            t,
            IMPORTED[-1],
            SEAM,
            f"adjusted from {source.replace('{}', t)} as of {as_of}",
            CAPTURED_SERIES,
        )
        for t in ("GLD", "USO")
    ]
    assert series_halts(found) == expected
    assert "unstated" not in found.data.halt.reason
    assert found.alpha.outputs == () and found.alpha.halt is not None
    assert found.redelivered is not None and found.redelivered.halt is not None
    assert all(r in found.redelivered.halt.reason for r in expected)


def _imported(bar: Bar) -> bool:
    return bar.origin == "imported"


# Each change to every bar on one side of the pair in USO's window, and the series after it.
OTHER_BARS = {
    "an-adjusted-later-bar": (False, {"basis": "adjusted"}),
    "an-imported-raw-later-bar": (False, {"origin": "imported"}),
    "an-imported-raw-later-bar-from-schwab": (
        False,
        {"origin": "imported", "source": SCHWAB_IMPORT, "source_as_of": SEAM},
    ),
    "a-captured-later-bar-with-a-date": (False, {"source_as_of": SEAM}),
    "a-raw-earlier-bar": (True, {"basis": "raw"}),
    "a-captured-earlier-bar": (True, {"origin": "captured"}),
}


@pytest.mark.parametrize("name", OTHER_BARS)
def test_2_a_pair_that_differs_in_one_field_or_origin_halts_on_the_stored_snapshot(
    first: Decided, name: str
) -> None:
    """Changed on a whole side of the pair, so the pair is the window's one change."""
    earlier, change = OTHER_BARS[name]
    held = first.snapshot.bars[USO]["1d"]
    bars = tuple(replace(b, **change) if _imported(b) == earlier else b for b in held)
    stored = replace(first.snapshot, bars={**first.snapshot.bars, USO: {"1d": bars}})
    one, two = bars[len(IMPORTED) - 1], bars[len(IMPORTED)]
    assert (one.session, two.session) == (IMPORTED[-1], SEAM)
    assert judge(stored, (GLD, USO), measured=False) == (
        reason("USO", one.session, two.session, str(series_of(one)), str(series_of(two))),
    )


def test_2_a_split_at_the_seam_is_read_as_a_move_and_flagged(tmp_path: Path) -> None:
    """The price of the ruling: an import on another scale than capture passes as one series.

    Here USO's import is on twice the scale of its captured bars, as one fetched on the
    day a split went ex, before Schwab restated, would be. The move rule's flag is the one
    sign of it, and it halts nothing.
    """
    lake = built(tmp_path / "lake", scale={"USO": Decimal(2)})
    found = decide(lake, tmp_path, DECIDED[0], walks=WALKS)
    assert found.data.halt is None and found.alpha.halt is None
    (flag,) = found.snapshot.flags[InstrumentId("USO")]
    assert (flag.session, flag.rule) == (SEAM, MOVE)
    assert list(found.snapshot.flags) == [USO]


# 3. The tracked strategy's decisions


def test_3_the_tracked_strategy_decides_at_close_20_from_2026_12_01_to_2027_01_28(
    lake: Path, tmp_path: Path
) -> None:
    """The session before halts on the change at 2026-09-04 alone."""
    before = decide(lake, tmp_path / "before", date(2026, 11, 30), walks=WALKS)
    assert series_halts(before) == [
        reason(
            t,
            EARLIER[-1],
            IMPORTED[0],
            f"adjusted from {YAHOO.format(t.lower())} as of 2026-11-22",
            f"adjusted from {SCHWAB_IMPORT} as of {SEAM}",
        )
        for t in ("GLD", "USO")
    ]
    assert len(DECIDED) == 40
    for day in DECIDED:
        found = decide(lake, tmp_path / str(day), day, walks=WALKS)
        assert (day, found.data.halt, found.alpha.halt) == (day, None, None)
        assert found.forecast is not None


def test_3_core_moves_still_leaves_out_every_move_across_the_pair(first: Decided) -> None:
    """The stress move measures each series alone, whatever data passes."""
    for leg in (GLD, USO):
        bars = first.snapshot.bars[leg]["1d"]
        found = moves(bars, 2)
        assert [(m.moved_from, m.moved_to) for m in found.left_out] == [
            (date(2026, 10, 29), date(2026, 11, 2)),
            (IMPORTED[-1], date(2026, 11, 3)),
        ]
        assert len(found.kept) == len(bars) - 2 - 2


def test_3_the_snapshot_decodes_with_every_field_the_pair_reads(first: Decided) -> None:
    """The judge reads origin, basis, source and its date from the stored bars."""
    assert isinstance(first.snapshot, Snapshot)
    pair = first.snapshot.bars[USO]["1d"][len(IMPORTED) - 1 : len(IMPORTED) + 1]
    assert [(b.origin, b.basis, b.source, b.source_as_of) for b in pair] == [
        ("imported", "adjusted", SCHWAB_IMPORT, SEAM),
        ("captured", "raw", None, None),
    ]
