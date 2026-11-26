"""Decisions on the bars marketlake captures, as #174's acceptance names.

Every price is made up and drawn from a seeded generator. The lake is one
marketlake's own import writes, with captured bars written as marketlake writes
one, and each decision runs data's and alpha's stages under the tracked
strategy and the tracked price-kind rows. The strategy's file writes no maximum
age or spread of a quote yet, and no quote-kind row is tracked, so those are
made up too. The numbers in each test's name are the acceptance items' numbers.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import pytest

from ithildinruntime.adapters import FolderBucket
from ithildinruntime.alpha import BollingerSpread
from ithildinruntime.core import (
    STRATEGIES,
    DecisionResult,
    Forecast,
    InstrumentId,
    Snapshot,
    SpikeFlag,
    decode_output,
    load_strategies,
    output_key,
    result_key,
)
from ithildinruntime.data import DataStage, LakeSource, Mirror, data_settings
from ithildinruntime.data.reference import (
    CAPTURE_SOURCE,
    QUOTE_SOURCE,
    KindRow,
    PriceKinds,
    QuoteKindRow,
    QuoteKinds,
    load_price_kinds,
)
from ithildinruntime.data.spikes import MOVE
from ithildinruntime.runtime.loop import Item
from tests.support import lake as lakes
from tests.support import made_lake as made
from tests.support import segments
from tests.support.spreads import GLD, USO, bar, synthetic
from tests.test_alpha import DEPLOYMENT, RELEASE, RUN, handler, run, store, table_of
from tests.test_data_source import Clock, Timer
from tests.test_data_stage import Reports

(TRACKED,) = load_strategies(STRATEGIES, ["chan_bollinger"])
SETTINGS = replace(data_settings([TRACKED]), quotes=segments.LIMITS)
KINDS = load_price_kinds()
IMPORTED = made.ONE
BEFORE_CAPTURE = made.days(date(2026, 10, 26), date(2026, 10, 30))
CAPTURED = made.days(date(2026, 11, 2), date(2027, 4, 1))
# The first session whose decision at close−20 reads captured bars alone.
DAY = date(2027, 1, 29)
WINDOW = made.days(date(2026, 11, 2), date(2027, 1, 28))
LAST_STATED = date(2027, 3, 31)
AFTER_LAST = [date(2027, 4, 1), date(2027, 4, 2)]
EARLY, MINUTE = timedelta(minutes=20), timedelta(minutes=1)


def _walks() -> dict[str, dict[date, Decimal]]:
    """A close of each leg on each session from the first imported one to the last decided."""
    days = made.days(BEFORE_CAPTURE[0], AFTER_LAST[-1])
    drawn = synthetic(len(days), seed=11)
    return {
        "GLD": {d: bar.close for d, bar in zip(days, drawn.gld, strict=True)},
        "USO": {d: bar.close for d, bar in zip(days, drawn.uso, strict=True)},
    }


WALKS = _walks()


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return made.build(
        tmp_path_factory.mktemp("captured") / "lake",
        [
            made.Made(t, IMPORTED.format(t), date(2026, 10, 31), {d: w[d] for d in BEFORE_CAPTURE})
            for t, w in WALKS.items()
        ],
        {t: {d: w[d] for d in CAPTURED} for t, w in WALKS.items()},
    )


class Decided(NamedTuple):
    data: DecisionResult
    snapshot: Snapshot
    alpha: DecisionResult
    forecast: Forecast | None


def decide(
    lake: Path,
    tmp_path: Path,
    day: date,
    *,
    kinds: PriceKinds = KINDS,
    mids: Mapping[str, Decimal] | None = None,
) -> Decided:
    """Run data's stage and then alpha's for the decision at close−20 of `day`."""
    session = made.TABLE.session(day)
    at = session.close - EARLY
    live = tmp_path / "live"
    for ticker, walk in WALKS.items():
        mid = (mids or {}).get(ticker, walk[day])
        bid, ask = float(mid - Decimal("0.01")), float(mid + Decimal("0.01"))
        row = lakes.quote_row(ticker, at - MINUTE, bid=bid, ask=ask, last=bid)
        lakes.quotes_segment(live, ticker, at - MINUTE, row)
    table, storage = table_of(TRACKED), store(tmp_path / "store")
    stage = DataStage(
        LakeSource(
            Mirror(FolderBucket(lake), tmp_path / "mirror"),
            SETTINGS,
            live=FolderBucket(live),
            clock=Clock(),
            timer=Timer(),
            measured=False,
            kinds=kinds,
            quote_kinds=QuoteKinds(
                (QuoteKindRow(day, QUOTE_SOURCE, day, day, segments.KIND, "a test"),)
            ),
        ),
        storage,
        Clock(),
        deployment=DEPLOYMENT,
        run=RUN,
        release=RELEASE,
        config_hashes=table.hashes_for("data"),
    )
    data = stage(Item(at, "data"), Reports())
    storage.put_result(result_key(DEPLOYMENT, RUN, at, "data"), data)
    key = output_key(DEPLOYMENT, RUN, at, "data", "snapshot")
    (snapshot,) = decode_output((tmp_path / "store" / key).read_bytes(), Snapshot).entries
    alpha = run(handler(storage, table, TRACKED.name), at, TRACKED.name)
    forecast = None
    for ref in alpha.outputs:
        if ref.key.endswith("forecast.json"):
            (forecast,) = decode_output(
                (tmp_path / "store" / ref.key).read_bytes(), Forecast
            ).entries
    return Decided(data, snapshot, alpha, forecast)


@pytest.fixture(scope="module")
def unsplit(lake: Path, tmp_path_factory: pytest.TempPathFactory) -> Forecast:
    """What the same decision forecasts on the lake with no split planted."""
    found = decide(lake, tmp_path_factory.mktemp("unsplit"), DAY).forecast
    assert found is not None
    return found


def sessions(snapshot: Snapshot, leg: InstrumentId) -> list[date]:
    return [bar.session for bar in snapshot.bars[leg]["1d"]]


# 1. A window of captured bars alone


def test_1_a_decision_at_close_20_forecasts_on_sixty_captured_bars(
    lake: Path, tmp_path: Path
) -> None:
    found = decide(lake, tmp_path, DAY)
    assert found.data.halt is None and found.alpha.halt is None
    assert found.snapshot.provisional and found.snapshot.flags == {}
    for leg in (GLD, USO):
        assert sessions(found.snapshot, leg) == WINDOW and len(WINDOW) == 60
        bars = found.snapshot.bars[leg]["1d"]
        assert {(b.origin, b.basis, b.source) for b in bars} == {("captured", "raw", None)}
        assert {(b.price_kind, b.venues, b.delay, b.volume_counted) for b in bars} == {
            ("official close", "consolidated", "none", "consolidated in shares as of the fetch")
        }
        # Each arrived after its session's close, as marketlake's fetch does.
        assert all(b.knowable_at > b.end for b in bars)
    assert found.forecast is not None and found.forecast.direction in (-1, 0, 1)


# 2. The row's last session


def test_2_the_tracked_row_is_read_up_to_its_last_session(lake: Path, tmp_path: Path) -> None:
    found = decide(lake, tmp_path, AFTER_LAST[0])
    assert sessions(found.snapshot, USO)[-1] == LAST_STATED
    assert found.data.halt is None and found.alpha.halt is None
    assert found.forecast is not None


def test_2_a_newest_bar_after_the_rows_last_session_halts(lake: Path, tmp_path: Path) -> None:
    found = decide(lake, tmp_path, AFTER_LAST[1])
    day = AFTER_LAST[0]
    assert sessions(found.snapshot, GLD)[-1] == day
    reason = found.data.halt.reason
    assert all(f"{leg}'s bar for {day} has unstated" in reason for leg in (GLD, USO))
    assert str(LAST_STATED) not in reason
    assert found.alpha.outputs == () and reason in found.alpha.halt.reason


def test_2_a_row_that_ends_a_session_early_halts_on_the_newest_bar_alone(
    lake: Path, tmp_path: Path
) -> None:
    short = PriceKinds(
        tuple(
            replace(r, last_session=WINDOW[-2]) if r.source == CAPTURE_SOURCE else r
            for r in KINDS.rows
        )
    )
    reason = decide(lake, tmp_path, DAY, kinds=short).data.halt.reason
    assert [str(d) in reason for d in WINDOW] == [False] * 59 + [True]


# 3. A split, under hard limit 3's second written exception

# How many sessions before the decision's the split takes effect, and 0 is between the
# newest bar and the quote.
EFFECTIVE = {"the-second-bar": 59, "a-middle-bar": 30, "the-newest-bar": 1, "the-quote": 0}
# The price is divided by the ratio from that session on.
RATIOS = {"two-for-one": Decimal(2), "one-for-eight": Decimal("0.125")}


def read_as_a_move(
    found: Decided, leg: InstrumentId, planted: Mapping[date, Decimal], unsplit: Forecast
) -> None:
    """The stored bars and the forecast read the planted closes, with no split undone."""
    assert [b.close for b in found.snapshot.bars[leg]["1d"]] == [planted[d] for d in WINDOW]
    # Fresh bars of the same sessions and closes, so nothing of a captured bar is left.
    adjusted = {
        i: {"1d": tuple(bar(i, b.session, b.close) for b in by["1d"])}
        for i, by in found.snapshot.bars.items()
    }
    held = replace(found.snapshot, bars=adjusted, flags={})
    expected = BollingerSpread(TRACKED).forecast(held)
    assert found.forecast == expected.forecasts[0]
    assert found.forecast != unsplit


@pytest.mark.parametrize("ratio", RATIOS.values(), ids=RATIOS)
@pytest.mark.parametrize("back", EFFECTIVE.values(), ids=EFFECTIVE)
@pytest.mark.parametrize("ticker", ["GLD", "USO"])
def test_3_a_split_in_the_window_is_flagged_and_the_decision_forecasts(
    lake: Path, tmp_path: Path, ticker: str, back: int, ratio: Decimal, unsplit: Forecast
) -> None:
    mine = lakes.copy_lake(lake, tmp_path / "split", ((WINDOW[0], WINDOW[-1]),))
    days = [*WINDOW, DAY]
    effective = days[-1 - back]
    template = mine / lakes.partition_key(ticker, WINDOW[0])
    split = {d: (c / ratio).quantize(Decimal("0.01")) for d, c in WALKS[ticker].items()}
    for day in days[-1 - back : -1]:
        made._capture(mine, ticker, day, split[day], template)
    found = decide(mine, tmp_path, DAY, mids={ticker: split[DAY]})
    assert found.data.halt is None and found.alpha.halt is None
    leg = InstrumentId(ticker)
    (flag,) = found.snapshot.flags[leg]
    assert isinstance(flag, SpikeFlag)
    assert (flag.session, flag.rule) == (effective, MOVE)
    assert list(found.snapshot.flags) == [leg]
    read_as_a_move(
        found,
        leg,
        {d: split[d] if d >= effective else c for d, c in WALKS[ticker].items()},
        unsplit,
    )


def test_3_a_split_too_small_to_flag_is_read_with_no_flag(
    lake: Path, tmp_path: Path, unsplit: Forecast
) -> None:
    """The flag is no check on every split, as docs/design.md's rule on splits says."""
    mine = lakes.copy_lake(lake, tmp_path / "split", ((WINDOW[0], WINDOW[-1]),))
    template = mine / lakes.partition_key("USO", WINDOW[0])
    for day in WINDOW[30:]:
        price = (WALKS["USO"][day] / Decimal("1.1")).quantize(Decimal("0.01"))
        made._capture(mine, "USO", day, price, template)
    mids = {"USO": (WALKS["USO"][DAY] / Decimal("1.1")).quantize(Decimal("0.01"))}
    found = decide(mine, tmp_path, DAY, mids=mids)
    assert found.data.halt is None and found.alpha.halt is None
    assert found.snapshot.flags == {}
    small = {d: (c / Decimal("1.1")).quantize(Decimal("0.01")) for d, c in WALKS["USO"].items()}
    planted = {d: small[d] if d in WINDOW[30:] else c for d, c in WALKS["USO"].items()}
    read_as_a_move(found, USO, planted, unsplit)


# 4. A change of series


def test_4_a_captured_bar_after_an_adjusted_one_halts_on_the_change_alone(
    lake: Path, tmp_path: Path
) -> None:
    """The window's oldest bar is imported, and every bar of it has a stated kind."""
    stated = KindRow(DAY, "", BEFORE_CAPTURE[0], BEFORE_CAPTURE[-1], made.STATED, "a test")
    kinds = PriceKinds(
        (*KINDS.rows, *(replace(stated, source=IMPORTED.format(t)) for t in ("GLD", "USO")))
    )
    found = decide(lake, tmp_path, WINDOW[-1], kinds=kinds)
    assert found.data.halt.reason.endswith(
        "; ".join(
            f"{t}'s bars for 2026-10-30 and 2026-11-02 are of two series, adjusted from "
            f"{IMPORTED.format(t)} as of 2026-10-31 and raw from no source as of no date"
            for t in ("GLD", "USO")
        )
    )
    assert "unstated" not in found.data.halt.reason
    assert found.alpha.outputs == () and found.alpha.halt is not None
