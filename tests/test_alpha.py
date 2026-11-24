"""Chan's band, the alpha stage and its entry point, over series written or drawn here.

#6's acceptance names these tests, and tests/test_alpha_replication.py holds
its first two, over the vintages the replication reads. The hand calculations
are written out beside the figures they give.
"""

from __future__ import annotations

import math
import random
import re
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Context, Decimal, localcontext
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.alpha import MODELS, AlphaStage, BollingerSpread, build_model
from ithildinruntime.alpha.bollinger import deviation, ends, held_unit, mean, slope
from ithildinruntime.core import (
    FORECAST_OUTPUT,
    SIGNALS_OUTPUT,
    STRATEGIES,
    AlphaModel,
    BarStatus,
    Basket,
    BlobHeader,
    BlobRef,
    DecisionResult,
    Forecast,
    ForecastSet,
    Halt,
    HaltRecord,
    InstrumentId,
    Mode,
    OutputFile,
    Plan,
    PlanDecision,
    Retry,
    Signal,
    Snapshot,
    StageKind,
    Storage,
    StrategyConfig,
    decode_output,
    encode,
    encode_output,
    load_strategies,
    output_key,
    parse_strategy,
    result_key,
    sha256_of,
)
from ithildinruntime.runtime.alpha_worker import alpha_handlers, alpha_rows
from ithildinruntime.runtime.data_worker import DATA_ROW
from ithildinruntime.runtime.loop import Item, ReceiveLoop, Stage, Table, routes
from tests.support.lookahead import decided, disagreements
from tests.support.queues import Folder, SystemTimer, running
from tests.support.queues import storage as folder_storage
from tests.support.spreads import DAYS, GLD, TABLE, USO, Series, made, strategy, synthetic
from tests.test_data_source import Timer
from tests.test_storage_contract import Alarms

ROOT = Path(__file__).parents[1]
DEPLOYMENT, RUN, RELEASE = "ci", "2011-11-29", "sha256:release"
CHAN = strategy()
LOOKBACK = 60
READS = 39  # 20 + 20 − 1


def d(text: object) -> Decimal:
    return Decimal(str(text))


# The hand-worked series
#
# Three closes fit each hedge ratio and three spreads make each band, so a
# z-score reads 3 + 3 − 1 = 5 sessions. GLD's close rises by 1 a session, so
# over three closes x the mean is the middle one, the squares sum to 2, and the
# slope is half of USO's last close less its first.
#
#   session     GLD  USO   hedge ratio              spread
#   2011-11-17   10   38
#   2011-11-18   11   37
#   2011-11-21   12   13   (13 − 38) / 2 = −12.5    13 + 12.5 × 12 = 163
#   2011-11-22   13   16   (16 − 37) / 2 = −10.5    16 + 10.5 × 13 = 152.5
#   2011-11-23   14   19   (19 − 13) / 2 = 3        19 − 3 × 14 = −23
#   2011-11-25   15   29   (29 − 16) / 2 = 6.5      29 − 6.5 × 15 = −68.5
#   2011-11-28   16   26   (26 − 19) / 2 = 3.5      26 − 3.5 × 16 = −30
#
# On 11-28 the band holds −23, −68.5 and −30. Their mean is −40.5, they sit
# 17.5, −28 and 10.5 from it, those squares sum to 1200.5, and half of that is
# 600.25, whose root is 24.5. So the z-score is 10.5 / 24.5 = 3/7. On 11-25 the
# band holds 152.5, −23 and −68.5, whose mean is above −68.5, so it is below 0.
HAND_DAYS = [date(2011, 11, n) for n in (17, 18, 21, 22, 23, 25, 28)]
GOLD = (10, 11, 12, 13, 14, 15, 16)
OIL = (38, 37, 13, 16, 19, 29, 26)
HAND = made(HAND_DAYS, GOLD, OIL)
NEWEST = len(HAND_DAYS) - 1
WHOLE = len(HAND_DAYS)
SMALL = strategy(hedge_sessions=3, band_sessions=3)
HAND_FITTED = {
    "hedge_ratio": ("3.50000000", date(2011, 11, 23)),
    "spread": ("-30.00000000", date(2011, 11, 23)),
    "spread_mean": ("-40.50000000", date(2011, 11, 21)),
    "spread_std": ("24.50000000", date(2011, 11, 21)),
    "z_score": ("0.42857143", date(2011, 11, 21)),
}
# The same GLD with other USO closes. The newest band holds 101, 24 and 10, so
# its mean is 45, its deviation 49 and its z-score −35/49 = −5/7. The one before
# holds 95.5, 101 and 24, whose z-score is −49.5 / √1845.25, below −1.
LONG = made(HAND_DAYS, GOLD, (26, 35, 29, 24, 17, 24, 18))
# Here the newest band holds −69, −27 and 204: mean 36, deviation 147, z-score
# 168/147 = 8/7, above 1 on its own.
SHORT = made(HAND_DAYS, GOLD, (16, 17, 21, 12, 36, 18, 12))


def forecast_of(config: StrategyConfig, held: Snapshot) -> ForecastSet:
    return BollingerSpread(config).forecast(held)


def written(signals: tuple[Signal, ...]) -> dict[str, tuple[str, date, date]]:
    return {s.name: (f"{s.value:f}", s.first_session, s.last_session) for s in signals}


def test_sessions_of_the_hand_worked_series_are_consecutive() -> None:
    start = DAYS.index(HAND_DAYS[0])
    assert DAYS[start : start + WHOLE] == HAND_DAYS


def test_the_hand_worked_series_gives_each_fitted_value_and_a_flat_basket() -> None:
    made_set = forecast_of(SMALL, HAND.at(NEWEST, WHOLE))
    (forecast,) = made_set.forecasts
    # Up past 0 ends a long, and 11-25's z-score below 0 ends a short: flat, on two z-scores.
    assert (forecast.strategy, forecast.direction, forecast.horizon_sessions) == ("chan", 0, 1)
    assert str(forecast.confidence) == "1.00000000"
    # A flat forecast names both legs.
    assert {i: str(q) for i, q in forecast.basket.legs.items()} == {
        USO: "1.00000000",
        GLD: "-3.50000000",
    }
    expected = {
        name: (value, first, date(2011, 11, 28)) for name, (value, first) in HAND_FITTED.items()
    }
    # 11-25's z-score reads the five sessions from 11-18.
    expected["unit"] = ("0.00000000", date(2011, 11, 18), date(2011, 11, 28))
    assert written(made_set.signals) == expected
    assert [s.name for s in made_set.signals] == [*HAND_FITTED, "unit"]


def test_a_short_settled_by_the_newest_z_score_reads_one_band() -> None:
    made_set = forecast_of(SMALL, SHORT.at(NEWEST, 5))
    assert made_set.forecasts[0].direction == -1
    found = written(made_set.signals)
    assert found["z_score"][0] == "1.14285714"
    assert found["spread_std"] == ("147.00000000", date(2011, 11, 21), date(2011, 11, 28))
    assert found["unit"] == ("-1.00000000", date(2011, 11, 21), date(2011, 11, 28))
    # USO's closes fell as GLD's rose, so the hedge is bought: −(−12).
    assert str(made_set.forecasts[0].basket.legs[GLD]) == "12.00000000"


def test_a_long_is_held_inside_the_band_from_the_z_score_below_it() -> None:
    made_set = forecast_of(SMALL, LONG.at(NEWEST, 6))
    assert made_set.forecasts[0].direction == 1
    assert str(made_set.forecasts[0].basket.legs[GLD]) == "-0.50000000"
    found = written(made_set.signals)
    assert found["z_score"][0] == "-0.71428571"
    assert (found["spread_mean"][0], found["spread_std"][0]) == ("45.00000000", "49.00000000")
    assert found["unit"] == ("1.00000000", date(2011, 11, 18), date(2011, 11, 28))


@pytest.mark.parametrize(
    ("series", "stored", "unit"), [(SHORT, "1.14285714", -1), (LONG, "-0.71428571", 1)]
)
def test_the_model_decides_on_unrounded_values(series, stored, unit) -> None:
    # 8/7 is 1.142857142857… and 5/7 is 0.714285714285…, so each stored z-score sits on
    # the entry it was rounded to, and the one decided on sits past it.
    config = strategy(hedge_sessions=3, band_sessions=3, entry=stored.removeprefix("-"))
    made_set = forecast_of(config, series.at(NEWEST, 5))
    assert made_set.forecasts[0].direction == unit
    assert written(made_set.signals)["z_score"][0] == stored


# USO's closes of HAND taken from 60, which turns each spread s into 60 − s and so
# each z-score over.
MIRRORED = made(HAND_DAYS, GOLD, tuple(60 - close for close in OIL))


@pytest.mark.parametrize(
    ("series", "stored"), [(HAND, "0.42857143"), (MIRRORED, "-0.42857143")], ids=["short", "long"]
)
def test_the_model_ends_a_position_on_unrounded_values(series, stored) -> None:
    # 3/7 is 0.428571428571…, so the stored z-score sits on the exit, which ends nothing,
    # and the one decided on sits inside it.
    config = strategy(hedge_sessions=3, band_sessions=3, exit="0.42857143")
    made_set = forecast_of(config, series.at(NEWEST, 5))
    assert made_set.forecasts[0].direction == 0
    assert written(made_set.signals)["z_score"][0] == stored


def zero_hedge(last: object) -> ForecastSet:
    """LONG with USO closing at `last` on 11-28, and 17 on 11-23."""
    closes = made(HAND_DAYS, GOLD, (26, 35, 29, 24, 17, 24, last))
    return forecast_of(SMALL, closes.at(NEWEST, WHOLE))


def test_a_hedge_ratio_of_zero_is_written_with_no_sign() -> None:
    made_set = zero_hedge(17)
    assert written(made_set.signals)["hedge_ratio"][0] == "0.00000000"
    stored = encode(made_set.forecasts[0])
    assert b'"GLD":"0.00000000"' in stored and b"-0" not in stored


@pytest.mark.parametrize("last", ["17.000000001", "16.999999999"])
def test_a_ratio_that_rounds_to_zero_is_stored_as_zero_with_no_sign(last) -> None:
    made_set = zero_hedge(last)
    for value in (made_set.forecasts[0].basket.legs[GLD], made_set.signals[0].value):
        assert value.is_zero() and not value.is_signed()


def test_a_hedging_leg_that_ends_a_window_where_it_began_is_fitted() -> None:
    # GLD closes 14, 15, 14 over the newest window, which moves.
    gold = (10, 11, 12, 13, 14, 15, 14)
    made_set = forecast_of(SMALL, made(HAND_DAYS, gold, OIL).at(NEWEST, WHOLE))
    # x sits −1/3, 2/3 and −1/3 from its mean and y −17/3, 13/3 and 4/3, so the
    # products sum to 13/3 and the squares to 2/3.
    assert written(made_set.signals)["hedge_ratio"][0] == "6.50000000"


def test_the_two_session_counts_are_read_apart() -> None:
    # Four closes fit the ratio and three spreads make the band, so a z-score reads six.
    config = strategy(hedge_sessions=4, band_sessions=3, entry="0.01", exit="0")
    made_set = forecast_of(config, HAND.at(NEWEST, 6))
    assert made_set.forecasts[0].direction in (-1, 1)
    first = {name: start for name, (_, start, _) in written(made_set.signals).items()}
    assert first["hedge_ratio"] == first["spread"] == date(2011, 11, 22)
    assert first["spread_mean"] == first["z_score"] == date(2011, 11, 18)
    with pytest.raises(Halt, match="reads 6 sessions of USO and GLD, and the snapshot holds 5 to"):
        forecast_of(config, HAND.at(NEWEST, 5))


# Each fitted value against a known answer. The three constants were written
# before the first run, as #6's acceptance 5 asks, and #65 item 4 gives why.
SEEDS = 10_000
STANDARD_ERRORS = 4
UNUSABLE_SEEDS_ALLOWED = 0
RATIO, INTERCEPT, MIDDLE, SIGMA = 0.5, 5.0, 3.0, 2.0
# E[s] = c₄σ at n = 20, with the n − 1 divisor.
C4 = math.sqrt(2 / 19) * math.gamma(10) / math.gamma(9.5)


@pytest.fixture(scope="module")
def estimates() -> dict[str, list[float]]:
    """Each estimator's answer on 20 draws, seed by seed, and three wrong ones beside them."""
    found: dict[str, list[float]] = {
        k: [] for k in ("slope", "mean", "deviation", "flipped", "no constant", "n divisor")
    }
    for seed in range(SEEDS):
        draw = random.Random(seed)
        x, level = [], 100.0
        for _ in range(20):
            level += draw.gauss(0, 1)
            x.append(level)
        noise = [draw.gauss(0, SIGMA) for _ in range(20)]
        xs = [d(repr(v)) for v in x]
        ys = [d(repr(INTERCEPT + RATIO * v + e)) for v, e in zip(x, noise, strict=True)]
        values = [d(repr(MIDDLE + e)) for e in noise]
        fitted, wide = slope(ys, xs), deviation(values)
        found["slope"].append(float(fitted))
        found["mean"].append(float(mean(values)))
        found["deviation"].append(float(wide))
        found["flipped"].append(float(-fitted))
        found["no constant"].append(
            float(sum(a * b for a, b in zip(xs, ys, strict=True)) / sum(a * a for a in xs))
        )
        found["n divisor"].append(float(wide) * math.sqrt(19 / 20))
    return found


def apart(values: list[float], truth: float) -> float:
    """How many standard errors the average of `values` sits from the truth."""
    usable = [v for v in values if math.isfinite(v)]
    assert len(values) - len(usable) <= UNUSABLE_SEEDS_ALLOWED and len(values) == SEEDS
    middle = math.fsum(usable) / len(usable)
    spread = math.sqrt(math.fsum((v - middle) ** 2 for v in usable) / (len(usable) - 1))
    return abs(middle - truth) / (spread / math.sqrt(len(usable)))


def test_c4_is_the_factor_the_issue_states() -> None:
    assert round(C4, 6) == 0.986934


@pytest.mark.parametrize(
    ("estimator", "truth"), [("slope", RATIO), ("mean", MIDDLE), ("deviation", C4 * SIGMA)]
)
def test_each_fitted_value_recovers_a_known_answer(estimates, estimator, truth) -> None:
    assert apart(estimates[estimator], truth) <= STANDARD_ERRORS


@pytest.mark.parametrize(
    ("estimator", "truth"),
    [("flipped", RATIO), ("no constant", RATIO), ("n divisor", C4 * SIGMA)],
)
def test_a_wrong_estimator_misses_the_known_answer(estimates, estimator, truth) -> None:
    assert apart(estimates[estimator], truth) > STANDARD_ERRORS


# The walk


def rule(scores: list[Decimal], entry: Decimal, exit_at: Decimal) -> list[int]:
    """The replication's rule run forward over the whole series, oldest z-score first."""
    long = short = 0
    units = []
    for z in scores:
        if z < -entry:
            long = 1
        elif z > -exit_at:
            long = 0
        if z > entry:
            short = 1
        elif z < exit_at:
            short = 0
        units.append(long - short)
    return units


@pytest.mark.parametrize(
    ("scores", "entry", "exit_at", "found"),
    [
        # Newest first. An entry on each side settles both at once.
        (["-1.5"], 1, 0, (1, 1)),
        (["1.5"], 1, 0, (-1, 1)),
        # Inside the band, a long is held until a z-score above 0.
        (["-0.5", "-1.5"], 1, 0, (1, 2)),
        (["0.5", "-1.5"], 1, 0, (0, 2)),
        # A short is held until one below 0.
        (["0.5", "1.5"], 1, 0, (-1, 2)),
        (["-0.5", "1.5"], 1, 0, (0, 2)),
        # A reversal, each way, whatever was held before.
        (["-1.5", "1.5"], 1, 0, (1, 1)),
        (["1.5", "-1.5"], 1, 0, (-1, 1)),
        # A z-score on a threshold neither enters nor exits.
        (["-1", "-1.5"], 1, 0, (1, 2)),
        (["-1", "0.5"], 1, 0, (0, 2)),
        (["1", "1.5"], 1, 0, (-1, 2)),
        (["1", "-0.5"], 1, 0, (0, 2)),
        (["0", "0", "-1.5"], 1, 0, (1, 3)),
        (["0", "0", "1.5"], 1, 0, (-1, 3)),
        (["0", "0.5", "-0.5"], 1, 0, (0, 3)),
        # An exit of 0.5 ends a long above −0.5 and a short below 0.5.
        (["-0.6", "-1.5"], 1, "0.5", (1, 2)),
        (["-0.5", "-1.5"], 1, "0.5", (1, 2)),
        (["-0.4", "-1.5"], 1, "0.5", (0, 1)),
        (["0.6", "1.5"], 1, "0.5", (-1, 2)),
        (["0.5", "1.5"], 1, "0.5", (-1, 2)),
        (["0.4", "1.5"], 1, "0.5", (0, 1)),
        # An exit of −0.5 holds a long to 0.5 and a short to −0.5.
        (["0.4", "-1.5"], 1, "-0.5", (1, 2)),
        (["0.5", "-1.5"], 1, "-0.5", (1, 2)),
        (["0.6", "-1.5"], 1, "-0.5", (0, 2)),
        (["-0.4", "1.5"], 1, "-0.5", (-1, 2)),
        (["-0.5", "1.5"], 1, "-0.5", (-1, 2)),
        (["-0.6", "1.5"], 1, "-0.5", (0, 2)),
        # An entry of 2 leaves 1.5 inside the band.
        (["-1.5", "-2.1"], 2, 0, (1, 2)),
        (["-1.5", "0.5"], 2, 0, (0, 2)),
        (["-2", "0.5"], 2, 0, (0, 2)),
        (["1.5", "-0.5"], 2, 0, (0, 2)),
        (["2", "-0.5"], 2, 0, (0, 2)),
        (["2.1", "-0.5"], 2, 0, (-1, 1)),
    ],
)
def test_the_unit_is_what_the_newest_settling_z_scores_say(scores, entry, exit_at, found) -> None:
    values = [d(z) for z in scores]
    assert held_unit(values, d(entry), d(exit_at)) == found
    assert found[0] == rule(values[::-1], d(entry), d(exit_at))[-1]


@pytest.mark.parametrize(
    "scores",
    [[], ["0"], ["-0.5"], ["0.5"], ["0.5", "0.9", "0.1"], ["-0.5", "-1"], ["1", "0.2"], ["0", "0"]],
)
def test_a_window_that_ends_before_both_sides_settle_gives_no_unit(scores) -> None:
    assert held_unit([d(z) for z in scores], d(1), d(0)) is None


def test_the_walk_reads_no_z_score_past_the_one_that_settles() -> None:
    def scores() -> Iterator[Decimal]:
        yield from (d("-0.5"), d("0"), d("-1.5"))
        raise AssertionError("the walk read on past the z-score that settled it")

    assert held_unit(scores(), d(1), d(0)) == (1, 3)


@pytest.mark.parametrize(("entry", "exit_at"), [(1, 0), (1, "0.5"), (1, "-0.5"), ("1.5", "0.25")])
def test_a_settled_window_equals_the_rule_run_over_the_whole_series(entry, exit_at) -> None:
    entry, exit_at = d(entry), d(exit_at)
    edges = [entry, -entry, exit_at, -exit_at, d(0)]
    settled = unsettled = 0
    for seed in range(300):
        draw = random.Random(seed)
        level, scores = 0.0, []
        for _ in range(80):
            level = 0.8 * level + draw.gauss(0, 0.8)
            scores.append(draw.choice(edges) if draw.random() < 0.1 else d(f"{level:.6f}"))
        units = rule(scores, entry, exit_at)
        for end in range(len(scores)):
            for width in (3, 8, 22):
                window = scores[max(0, end - width + 1) : end + 1]
                found = held_unit(reversed(window), entry, exit_at)
                if found is None:
                    unsettled += 1
                else:
                    settled += 1
                    assert found[0] == units[end], (seed, end, width)
    # A walk that never settled, or one that always did, would prove nothing here.
    assert settled > 10_000 and unsettled > 100


@pytest.mark.parametrize(
    ("side", "score", "exit_at", "ended"),
    [
        (1, "0.1", 0, True),
        (1, "0", 0, False),
        (1, "-0.1", 0, False),
        (-1, "-0.1", 0, True),
        (-1, "0", 0, False),
        (-1, "0.1", 0, False),
        (1, "-0.4", "0.5", True),
        (1, "-0.5", "0.5", False),
        (-1, "0.4", "0.5", True),
        (-1, "0.5", "0.5", False),
    ],
)
def test_the_exit_stands_apart_from_the_entry(side, score, exit_at, ended) -> None:
    assert ends(side, d(score), d(exit_at)) is ended


# Look-ahead

SYNTHETIC = synthetic(260, seed=7)


def honest(series: Series) -> AlphaModel:
    return BollingerSpread(CHAN)


class Planted(BollingerSpread):
    """A model that fits its hedge ratio on the whole run's series, past its decision."""

    def __init__(self, series: Series) -> None:
        super().__init__(CHAN)
        self._ahead = slope([b.close for b in series.uso], [b.close for b in series.gld])

    def _fits(self, held: Snapshot):
        fits = super()._fits(held)
        fits.hedge = lambda at: self._ahead
        return fits


def run_of(factory, series: Series, **changes) -> dict[date, ForecastSet | str]:
    # Seven z-scores leave some decisions unsettled, and the first has too few sessions.
    return decided(factory, series, lookback=READS + 6, first=READS - 2, **changes)


@pytest.fixture(scope="module")
def whole() -> dict[date, ForecastSet | str]:
    return run_of(honest, SYNTHETIC)


@pytest.mark.parametrize("removed", [10, 100])
def test_removing_the_latest_sessions_changes_no_decision_before_them(whole, removed) -> None:
    part = run_of(honest, SYNTHETIC.first(len(SYNTHETIC) - removed))
    assert disagreements(whole, part) == []
    shared = [whole[day] for day in part]
    assert len(shared) == len(SYNTHETIC) - removed - (READS - 2)
    units = {one.forecasts[0].direction for one in shared if not isinstance(one, str)}
    assert units == {-1, 0, 1}
    reasons = [one for one in shared if isinstance(one, str)]
    assert "snapshot holds 38" in reasons[0]
    assert len(reasons) > 1 and all("cannot tell which unit" in r for r in reasons[1:])


def test_the_decisions_in_reverse_on_one_model_agree(whole) -> None:
    back = run_of(honest, SYNTHETIC, newest_first=True)
    assert back == whole and list(back) == list(whole)[::-1]


@pytest.mark.parametrize("removed", [10, 100])
def test_a_model_that_fits_on_a_later_session_fails_the_harness(removed) -> None:
    ahead = run_of(Planted, SYNTHETIC)
    part = run_of(Planted, SYNTHETIC.first(len(SYNTHETIC) - removed))
    found = {what for _, what in disagreements(ahead, part)}
    assert {"hedge_ratio", "forecast"} <= found
    # Far enough ahead, it holds another unit and halts where the other run decides.
    assert removed == 10 or {"unit", "halt"} <= found


# The section


def test_the_model_reads_every_setting_of_its_section() -> None:
    held = SYNTHETIC.at(len(SYNTHETIC) - 1, 120)
    base = forecast_of(CHAN, held)
    assert forecast_of(strategy(hedge_sessions=19), held) != base
    assert forecast_of(strategy(band_sessions=19), held) != base
    swapped = forecast_of(strategy(dependent='"GLD"', independent='"USO"'), held)
    assert swapped.forecasts[0].basket.legs[GLD] == 1
    assert swapped.forecasts[0].basket.legs[USO] != 1
    # The hand-worked series is flat at an entry of 1 and short at 0.4, as 3/7 is above it.
    small = {"hedge_sessions": 3, "band_sessions": 3}
    for entry, unit in (("1", 0), ("0.4", -1)):
        config = strategy(entry=entry, **small)
        assert forecast_of(config, HAND.at(NEWEST, 6)).forecasts[0].direction == unit
    # An exit of −0.8 holds both sides past ±0.8, so neither z-score settles either.
    with pytest.raises(Halt, match="cannot tell which unit it holds"):
        forecast_of(strategy(exit="-0.8", **small), HAND.at(NEWEST, 6))


@pytest.mark.parametrize(
    "changes",
    [
        {"hedge_sessions": 3},
        {"band_sessions": 2, "entry": "0.7"},
        {"entry": "0.00000001"},
        {"exit": "0.99999999"},
        {"exit": "-0.99999999"},
        {"entry": "2.5", "exit": "-1.5"},
        # A z-score over 4 spreads stays within 3 / √4 = 1.5, and one over 20 within 4.2485.
        {"band_sessions": 4, "entry": "1.49999999"},
        {"band_sessions": 4, "entry": "1.4999999999999999999999999999999999999"},
        {"band_sessions": 16, "entry": "3.74"},
        {"entry": "4.24"},
    ],
)
def test_a_setting_at_its_bound_is_accepted(changes) -> None:
    assert isinstance(build_model(strategy(**changes)), BollingerSpread)


@pytest.mark.parametrize("spreads", [2, 3, 4, 5, 20])
def test_no_z_score_passes_the_bound_the_section_is_held_to(spreads) -> None:
    # The newest spread far from the others, which sit together, is the furthest it can be.
    with localcontext(Context(prec=28)):
        values = [d(0)] * (spreads - 1) + [d(1)]
        score = (values[-1] - mean(values)) / deviation(values)
        assert abs(score**2 * spreads - (spreads - 1) ** 2) < d("1e-24")
        draw = random.Random(spreads)
        for _ in range(200):
            values = [d(repr(draw.gauss(0, 1))) for _ in range(spreads)]
            score = (values[-1] - mean(values)) / deviation(values)
            assert score**2 * spreads < (spreads - 1) ** 2 + d("1e-24")


SECTION_OF = "strategy chan's configuration chan.toml has an [alpha] section that "
NOT_A_COUNT = "which is not a whole number of"
BAD_SECTIONS = {
    "no-model": ({"model": None}, "states no model, and the models are bollinger_spread"),
    "an-unknown-model": (
        {"model": '"reversal"'},
        "names the model 'reversal', and the models are bollinger_spread",
    ),
    "a-model-that-is-no-text": ({"model": "7"}, "names the model 7, and the models are"),
    "a-model-that-is-a-list": ({"model": '["bollinger_spread"]'}, "names the model ("),
    **{
        f"no-{key}": ({key: None}, f"states no {key}")
        for key in ("dependent", "independent", "hedge_sessions", "band_sessions", "entry", "exit")
    },
    "two-keys-missing": ({"entry": None, "dependent": None}, "states no dependent, entry"),
    "a-key-this-model-lacks": (
        {"lookback": "60"},
        "holds lookback, which this model does not have",
    ),
    "a-leg-that-is-no-text": (
        {"dependent": "7"},
        "has dependent = 7, which is not an instrument id",
    ),
    "a-hedging-leg-that-is-no-text": (
        {"independent": "true"},
        "has independent = True, which is not an instrument id",
    ),
    "a-leg-the-strategy-does-not-trade": (
        {"independent": '"SPY"'},
        "names the legs USO and SPY, which are not exactly the strategy's instruments, GLD, USO",
    ),
    "one-leg-twice": (
        {"independent": '"USO"'},
        "names the legs USO and USO, which are not exactly the strategy's instruments, GLD, USO",
    ),
    "a-third-instrument": (
        {"instruments": '["GLD", "USO", "SPY"]'},
        "names the legs USO and GLD, which are not exactly the strategy's instruments, GLD, USO, "
        "SPY",
    ),
    "a-fit-on-two-closes": (
        {"hedge_sessions": "2"},
        f"has hedge_sessions = 2, {NOT_A_COUNT} 3 or more",
    ),
    "a-band-of-one-spread": (
        {"band_sessions": "1"},
        f"has band_sessions = 1, {NOT_A_COUNT} 2 or more",
    ),
    **{
        f"{key}-that-is-{n}": ({key: value}, f"has {key} = {shown}, {NOT_A_COUNT}")
        for key in ("hedge_sessions", "band_sessions")
        for n, value, shown in (
            ("a-decimal", "20.0", "20.0"),
            ("a-bool", "true", "True"),
            ("text", '"20"', "20"),
        )
    },
    **{
        f"{key}-that-is-{n}": ({key: value}, f"has {key} = {shown}, which is not a number")
        for key in ("entry", "exit")
        for n, value, shown in (("a-bool", "false", "False"), ("text", '"1"', "'1'"))
    },
    **{
        f"thresholds-{entry}-and-{exit_at}": (
            {"entry": entry, "exit": exit_at},
            f"has entry = {entry} and exit = {exit_at}, and an exit sits between −entry and entry",
        )
        for entry, exit_at in (("1", "1"), ("1", "-1"), ("1", "1.5"), ("1", "-1.5"), ("0", "0"))
    },
    **{
        f"an-entry-of-{entry}-no-z-score-over-{spreads}-spreads-reaches": (
            {"entry": entry, "band_sessions": str(spreads)},
            f"has entry = {entry}, which no z-score over {spreads} spreads can pass",
        )
        for entry, spreads in (
            ("10", 20),
            ("4.25", 20),
            ("1", 2),
            ("1.16", 3),
            ("1.5", 4),
            ("3.75", 16),
        )
    },
    "an-entry-below-zero": (
        {"entry": "-1"},
        "has entry = -1 and exit = 0, and an exit sits between −entry and entry",
    ),
}
NO_SECTION = "strategy chan's configuration chan.toml has no [alpha] section, or an empty one"
BAD_FILES = {
    **{case: strategy(**changes) for case, (changes, _) in BAD_SECTIONS.items()},
    "no-section": parse_strategy("chan", b'instruments = ["GLD", "USO"]\n'),
    "an-empty-section": parse_strategy("chan", b'instruments = ["GLD", "USO"]\n[alpha]\n'),
}
BAD_REASONS = {
    **{case: SECTION_OF + reason for case, (_, reason) in BAD_SECTIONS.items()},
    "no-section": NO_SECTION,
    "an-empty-section": NO_SECTION,
}


# The stage


def tracked() -> list[str]:
    folder = STRATEGIES.relative_to(ROOT).as_posix()
    listed = subprocess.run(
        ["git", "ls-files", "--", folder], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    return sorted(Path(path).stem for path in listed)


@pytest.mark.parametrize("name", tracked())
def test_every_tracked_strategys_alpha_section_builds_its_model(name) -> None:
    (config,) = load_strategies(STRATEGIES, [name])
    assert type(build_model(config)) in MODELS.values()


def test_chans_tracked_file_states_the_replications_settings() -> None:
    (chan,) = load_strategies(STRATEGIES, ["chan_bollinger"])
    assert dict(chan.section("alpha")) == {
        "model": "bollinger_spread",
        "dependent": "USO",
        "independent": "GLD",
        "hedge_sessions": 20,
        "band_sessions": 20,
        "entry": 1,
        "exit": 0,
    }


def table_of(*configs: StrategyConfig) -> Table:
    return Table(stages=(DATA_ROW, *alpha_rows(configs)), strategies=configs)


def store(root: Path) -> Storage:
    return Storage(
        FolderStore(root),
        timer=Timer(),
        pause=lambda s: None,
        retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
        alerts=Alarms(),
        owner_step="none",
    )


def data_key(at: datetime) -> str:
    return result_key(DEPLOYMENT, RUN, at, "data")


def snapshot_key(at: datetime) -> str:
    return output_key(DEPLOYMENT, RUN, at, "data", "snapshot")


def data_header(at: datetime, table: Table) -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        stage="data",
        kind="capturing",
        decision_at=at,
        inputs={},
        release=RELEASE,
        config_hashes=table.hashes_for("data"),
    )


def write_data(
    storage: Storage, held: Snapshot, table: Table, halt: str | None = None
) -> tuple[BlobRef, BlobRef]:
    """Store data's snapshot and its result, and return the result and the snapshot."""
    at = held.decision_at
    header = data_header(at, table)
    file = encode_output(Snapshot, OutputFile(header, (held,), False))
    ref = storage.put(StageKind.CAPTURING, snapshot_key(at), file).ref
    record = HaltRecord(halt, ref, (), (), ()) if halt is not None else None
    result = DecisionResult(header, (ref,), record)
    storage.put_result(data_key(at), result)
    return BlobRef(data_key(at), sha256_of(encode(result))), ref


def handler(storage: Storage, table: Table, name: str = "chan") -> AlphaStage:
    made_stages = alpha_handlers(
        storage=storage, deployment=DEPLOYMENT, run=RUN, release=RELEASE, table=table
    )
    return made_stages[f"alpha/{name}"]


def run(stage: AlphaStage, at: datetime, name: str = "chan") -> DecisionResult:
    return stage(Item(at, f"alpha/{name}"), lambda: None)


def blobs(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in (root / "runs").rglob("*") if p.is_file()}


def stored_result(storage: Storage, result: DecisionResult, at: datetime) -> DecisionResult:
    """The result as the loop stores it and a reader reads it back."""
    key = result_key(DEPLOYMENT, RUN, at, "alpha/chan")
    storage.put_result(key, result)
    read = storage.read_result(key, at)
    assert read is not None
    return read


def halted(tmp_path: Path, config: StrategyConfig, held: Snapshot) -> str:
    """The reason of the halt the stage stores, after checking what it names and leaves behind."""
    table = table_of(config)
    storage = store(tmp_path)
    data, file = write_data(storage, held, table)
    before = blobs(tmp_path)
    result = run(handler(storage, table), held.decision_at)
    assert blobs(tmp_path) == before
    read = stored_result(storage, result, held.decision_at)
    assert read.outputs == () and read.halt is not None
    assert read.halt.cause is None
    assert dict(read.header.inputs) == {data.key: data.sha256, file.key: file.sha256}
    assert dict(read.header.config_hashes) == {"chan": config.config_hash}
    return read.halt.reason


def with_closes(gld: tuple, uso: tuple) -> Snapshot:
    return made(HAND_DAYS, gld, uso).at(NEWEST, WHOLE)


HAND_SNAPSHOT = HAND.at(NEWEST, WHOLE)
CLOSE = TABLE.session(date(2011, 11, 28)).close


def leg(held: Snapshot, instrument, bars) -> Snapshot:
    return replace(held, bars={**held.bars, instrument: {"1d": tuple(bars)}})


def basis(instrument, n: int) -> Snapshot:
    bars = list(HAND_SNAPSHOT.bars[instrument]["1d"])
    bars[n] = replace(bars[n], basis="raw")
    return leg(HAND_SNAPSHOT, instrument, bars)


HALTS = {
    "a-leg-missing": (
        replace(HAND_SNAPSHOT, bars={USO: HAND_SNAPSHOT.bars[USO]}, status={USO: BarStatus.FRESH}),
        "GLD missing for the session",
    ),
    "a-leg-data-calls-missing": (
        replace(HAND_SNAPSHOT, status={GLD: BarStatus.FRESH, USO: BarStatus.MISSING}),
        "USO missing for the session",
    ),
    "a-leg-stale": (
        replace(HAND_SNAPSHOT, status={GLD: BarStatus.STALE, USO: BarStatus.FRESH}),
        "GLD stale for the session",
    ),
    "no-daily-bar": (
        replace(
            HAND_SNAPSHOT,
            bars={
                GLD: HAND_SNAPSHOT.bars[GLD],
                USO: {
                    "1m": tuple(replace(b, interval="1m") for b in HAND_SNAPSHOT.bars[USO]["1d"])
                },
            },
        ),
        "USO has no 1d bar for the session",
    ),
    "another-instrument-stale": (
        replace(
            HAND_SNAPSHOT, status={**HAND_SNAPSHOT.status, InstrumentId("SPY"): BarStatus.STALE}
        ),
        "SPY stale for the session",
    ),
    "a-bar-as-traded": (
        basis(GLD, 0),
        "strategy chan reads adjusted prices, and GLD's bar of 2011-11-17 has the basis raw",
    ),
    "the-newest-bar-as-traded": (
        basis(USO, NEWEST),
        "strategy chan reads adjusted prices, and USO's bar of 2011-11-28 has the basis raw",
    ),
    "one-leg-a-session-short": (
        leg(HAND_SNAPSHOT, USO, HAND_SNAPSHOT.bars[USO]["1d"][:-1]),
        "strategy chan reads both legs on the same sessions, and USO and GLD have bars of "
        "different ones, such as 2011-11-28",
    ),
    "one-leg-with-a-gap": (
        leg(HAND_SNAPSHOT, GLD, [b for n, b in enumerate(HAND_SNAPSHOT.bars[GLD]["1d"]) if n != 3]),
        "strategy chan reads both legs on the same sessions, and USO and GLD have bars of "
        "different ones, such as 2011-11-22",
    ),
    "one-leg-out-of-order": (
        leg(HAND_SNAPSHOT, GLD, HAND_SNAPSHOT.bars[GLD]["1d"][::-1]),
        "strategy chan reads both legs on the same sessions, and USO and GLD have bars of "
        "different ones",
    ),
    "a-newest-bar-of-the-day-before": (
        replace(
            HAND_SNAPSHOT, decision_at=TABLE.session(date(2011, 11, 29)).close - timedelta(hours=1)
        ),
        "strategy chan decides on the session 2011-11-29, and the newest bar of USO and GLD is "
        "2011-11-28's",
    ),
    "a-newest-bar-after-the-decision": (
        replace(HAND_SNAPSHOT, decision_at=TABLE.session(date(2011, 11, 25)).close),
        "strategy chan decides on the session 2011-11-25, and the newest bar of USO and GLD is "
        "2011-11-28's",
    ),
    "a-decision-past-the-calendar": (
        replace(HAND_SNAPSHOT, decision_at=datetime(2050, 1, 3, 21, 0, tzinfo=UTC)),
        "strategy chan cannot place its decision on a session: 2050-01-03T21:00:00+00:00 is "
        "outside the session table's range, 2006-01-01 to 2027-12-31",
    ),
    "too-few-sessions": (
        HAND.at(NEWEST, 4),
        "strategy chan reads 5 sessions of USO and GLD, and the snapshot holds 4 to 2011-11-28",
    ),
    "a-hedging-leg-that-does-not-move": (
        with_closes((10, 11, 12, 13, 14, 14, 14), OIL),
        "strategy chan cannot fit a hedge ratio on 2011-11-28: GLD's last 3 closes are all 14",
    ),
    "a-band-that-does-not-move": (
        # USO is 3 + GLD / 2 throughout, so each spread is 3.
        with_closes((9, 10, 12, 13, 15, 16, 18), ("7.5", "8", "9", "9.5", "10.5", "11", "12")),
        "strategy chan has no z-score on 2011-11-28: the last 3 spreads' standard deviation is 0",
    ),
    "an-unsettled-window": (
        HAND.at(NEWEST, 5),
        "strategy chan cannot tell which unit it holds: no z-score from 2011-11-28 to 2011-11-28 "
        "settles both sides",
    ),
    "a-figure-past-the-codec": (
        with_closes(GOLD, tuple(f"{close}e25" for close in OIL)),
        "strategy chan cannot store its forecast for 2011-11-28T21:00:00+00:00: ",
    ),
}


def test_the_snapshot_the_halts_below_change_is_one_the_stage_accepts(tmp_path) -> None:
    table = table_of(SMALL)
    storage = store(tmp_path)
    write_data(storage, HAND_SNAPSHOT, table)
    assert run(handler(storage, table), CLOSE).halt is None


@pytest.mark.parametrize("case", HALTS)
def test_an_input_the_model_cannot_use_halts_with_its_inputs_and_no_cause(tmp_path, case) -> None:
    held, reason = HALTS[case]
    found = halted(tmp_path, SMALL, held)
    assert found.startswith(reason) if reason.endswith(": ") else found == reason


@pytest.mark.parametrize("case", BAD_FILES)
def test_a_section_the_model_refuses_halts_with_its_inputs_and_no_cause(tmp_path, case) -> None:
    found = halted(tmp_path, BAD_FILES[case], HAND_SNAPSHOT)
    assert found.startswith(BAD_REASONS[case])


@pytest.mark.parametrize(
    "at",
    [
        # From the session's own open, through its close, to just before the next one opens.
        TABLE.session(date(2011, 11, 28)).open,
        CLOSE - timedelta(minutes=20),
        CLOSE + timedelta(hours=2),
        TABLE.session(date(2011, 11, 29)).open - timedelta(microseconds=1),
    ],
)
def test_a_newest_bar_of_the_decisions_own_session_is_read(at) -> None:
    held = replace(HAND_SNAPSHOT, decision_at=at)
    assert forecast_of(SMALL, held) == forecast_of(SMALL, HAND_SNAPSHOT)


def test_a_window_past_the_walks_reach_is_not_fitted() -> None:
    # GLD's first three closes are equal, and the walk settles on z-scores that read none of them.
    held = made(
        [date(2011, 11, 15), date(2011, 11, 16), *HAND_DAYS],
        (10, 10, *GOLD),
        (7, 8, *OIL),
    )
    assert forecast_of(SMALL, held.at(WHOLE + 1, WHOLE + 2)) == forecast_of(SMALL, HAND_SNAPSHOT)


@pytest.mark.parametrize(
    ("gap", "halts"), [("0.9999999e-12", True), ("1e-12", True), ("1.0000001e-12", False)]
)
def test_the_floor_under_a_bands_deviation_is_a_trillionth(gap, halts) -> None:
    # With a hedge ratio of 0 the last three spreads are USO's closes, 3 − e, 3 and 3 + e,
    # whose deviation with the n − 1 divisor is e.
    closes = (1, 2, 3, 4, d(3) - d(gap), 3, d(3) + d(gap))
    with localcontext(Context(prec=28)):
        fits = BollingerSpread(SMALL)._fits(with_closes(GOLD, closes))
        fits.hedge = lambda at: d(0)
        if halts:
            with pytest.raises(Halt, match="has no z-score on 2011-11-28"):
                fits.band(NEWEST)
        else:
            assert fits.band(NEWEST).deviation == d(gap)


def test_a_spread_constant_in_exact_arithmetic_halts_though_rounding_moves_it() -> None:
    # USO is 3 + GLD / 7, which 28 digits cannot hold, so the spreads differ in their last digits.
    gold = [b.close for b in SYNTHETIC.gld[:LOOKBACK]]
    days = [b.session for b in SYNTHETIC.gld[:LOOKBACK]]
    held = made(days, gold, [d(3) + g / 7 for g in gold]).at(LOOKBACK - 1, LOOKBACK)
    with pytest.raises(Halt) as caught:
        forecast_of(CHAN, held)
    found = re.search(r"standard deviation is ([0-9.]+E-2[0-9])$", caught.value.reason)
    assert found is not None
    # The figure carries the 28 digits the model works at.
    assert len(d(found[1]).as_tuple().digits) == 28


def test_a_halt_in_datas_result_is_passed_on_naming_it(tmp_path) -> None:
    table = table_of(SMALL)
    storage = store(tmp_path)
    data, _ = write_data(storage, HAND_SNAPSHOT, table, halt="GLD missing for the session")
    before = blobs(tmp_path)
    result = stored_result(storage, run(handler(storage, table), CLOSE), CLOSE)
    assert result.outputs == () and result.halt is not None
    assert result.halt.cause == data
    assert result.halt.reason == f"{data.key} holds a halt: GLD missing for the session"
    assert dict(result.header.inputs) == {data.key: data.sha256}
    assert blobs(tmp_path) - before == {result_key(DEPLOYMENT, RUN, CLOSE, "alpha/chan")}


def test_a_halt_in_datas_result_is_passed_on_before_the_section_is_read(tmp_path) -> None:
    table = table_of(BAD_FILES["no-section"])
    storage = store(tmp_path)
    data, _ = write_data(storage, HAND_SNAPSHOT, table, halt="data not ready")
    result = run(handler(storage, table), CLOSE)
    assert result.halt is not None and result.halt.cause == data


def test_a_decision_with_no_data_result_halts(tmp_path) -> None:
    with pytest.raises(Halt, match="data/result.json is not stored, and stage alpha/chan reads it"):
        run(handler(store(tmp_path), table_of(SMALL)), CLOSE)


def test_a_forecast_of_another_strategy_halts(tmp_path, monkeypatch) -> None:
    table = table_of(SMALL)
    storage = store(tmp_path)
    data, file = write_data(storage, HAND_SNAPSHOT, table)
    ours = forecast_of(SMALL, HAND_SNAPSHOT)

    class Foreign:
        def forecast(self, held: Snapshot) -> ForecastSet:
            return replace(
                ours, forecasts=(*ours.forecasts, replace(ours.forecasts[0], strategy="other"))
            )

    before = blobs(tmp_path)
    monkeypatch.setattr("ithildinruntime.alpha.stage.build_model", lambda config: Foreign())
    result = run(handler(storage, table), CLOSE)
    assert result.halt is not None and result.halt.cause is None and result.outputs == ()
    assert result.halt.reason == (
        "stage alpha/chan made a forecast of strategy other, and forecasts strategy chan"
    )
    assert dict(result.header.inputs) == {data.key: data.sha256, file.key: file.sha256}
    assert blobs(tmp_path) == before


def test_the_model_is_built_again_at_each_decision(tmp_path, monkeypatch) -> None:
    # A model that remembers would answer a run of decisions in one process otherwise
    # than a worker that restarts between them.
    ours = forecast_of(SMALL, HAND_SNAPSHOT)

    class Remembers:
        def __init__(self, config: StrategyConfig) -> None:
            self.seen = 0

        def forecast(self, held: Snapshot) -> ForecastSet:
            self.seen += 1
            return replace(ours, signals=(replace(ours.signals[0], value=d(self.seen)),))

    monkeypatch.setattr("ithildinruntime.alpha.stage.build_model", Remembers)
    table = table_of(SMALL)
    storage = store(tmp_path)
    stage = handler(storage, table)
    for n in (NEWEST - 1, NEWEST):
        held = HAND.at(n, WHOLE)
        write_data(storage, held, table)
        ref = run(stage, held.decision_at).outputs[1]
        (signal,) = decode_output((tmp_path / ref.key).read_bytes(), Signal).entries
        assert signal.value == 1


def test_the_stage_stores_the_forecast_and_then_the_signals(tmp_path) -> None:
    table = table_of(SMALL)
    storage = store(tmp_path)
    write_data(storage, HAND_SNAPSHOT, table)
    keys = [
        output_key(DEPLOYMENT, RUN, CLOSE, "alpha/chan", n)
        for n in (FORECAST_OUTPUT, SIGNALS_OUTPUT)
    ]
    seen: list[list[bool]] = []
    result = handler(storage, table)(
        Item(CLOSE, "alpha/chan"), lambda: seen.append([(tmp_path / k).exists() for k in keys])
    )
    # It reports once it has read, once the forecast is stored and once the signals are.
    assert seen == [[False, False], [True, False], [True, True]]
    assert [ref.key for ref in result.outputs] == keys
    made_set = forecast_of(SMALL, HAND_SNAPSHOT)
    forecast = decode_output((tmp_path / keys[0]).read_bytes(), Forecast)
    signals = decode_output((tmp_path / keys[1]).read_bytes(), Signal)
    assert (forecast.entries, forecast.trade_nothing) == (made_set.forecasts, False)
    assert (signals.entries, signals.trade_nothing) == (made_set.signals, False)
    assert result.header == forecast.header == signals.header


def test_a_crash_between_the_two_files_stores_the_same_bytes_again(tmp_path) -> None:
    table = table_of(SMALL)
    storage = store(tmp_path)
    write_data(storage, HAND_SNAPSHOT, table)
    beats: list[None] = []

    def crash() -> None:
        beats.append(None)
        if len(beats) == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        handler(storage, table)(Item(CLOSE, "alpha/chan"), crash)
    forecast = output_key(DEPLOYMENT, RUN, CLOSE, "alpha/chan", FORECAST_OUTPUT)
    signals = output_key(DEPLOYMENT, RUN, CLOSE, "alpha/chan", SIGNALS_OUTPUT)
    assert forecast in blobs(tmp_path) and signals not in blobs(tmp_path)
    held = (tmp_path / forecast).read_bytes()
    # Storage halts a deterministic stage whose second run writes other bytes.
    result = run(handler(store(tmp_path), table), CLOSE)
    assert result.halt is None and (tmp_path / forecast).read_bytes() == held
    assert run(handler(store(tmp_path), table), CLOSE) == result


def test_a_second_run_that_writes_other_bytes_halts_as_not_deterministic(tmp_path) -> None:
    table = table_of(SMALL)
    storage = store(tmp_path)
    write_data(storage, HAND_SNAPSHOT, table)
    key = output_key(DEPLOYMENT, RUN, CLOSE, "alpha/chan", FORECAST_OUTPUT)
    storage.put(StageKind.DETERMINISTIC, key, b"{}")
    with pytest.raises(Halt, match="so the step is not deterministic"):
        run(handler(storage, table), CLOSE)


@pytest.mark.parametrize("config", [SMALL, strategy(entry="0.333333333", exit="-0.1")])
def test_the_callers_decimal_context_changes_no_byte(tmp_path, config) -> None:
    table = table_of(config)
    held = SYNTHETIC.at(len(SYNTHETIC) - 1, LOOKBACK)
    at = held.decision_at
    files = []
    for precision in (28, 6, 50):
        root = tmp_path / str(precision)
        storage = store(root)
        write_data(storage, held, table)
        with localcontext(Context(prec=precision, rounding="ROUND_UP")):
            result = run(handler(storage, table), at)
        assert result.halt is None
        files.append([(root / ref.key).read_bytes() for ref in result.outputs])
    assert files[0] == files[1] == files[2]


# The entry point


def test_the_entry_point_gives_each_strategy_a_deterministic_row_that_reads_data() -> None:
    other = strategy(name="other")
    assert alpha_rows([SMALL, other]) == (
        Stage("alpha/chan", StageKind.DETERMINISTIC, inputs=("data",), strategy="chan"),
        Stage("alpha/other", StageKind.DETERMINISTIC, inputs=("data",), strategy="other"),
    )
    table = table_of(SMALL, other)
    assert [s.name for s in table.stages_of("alpha")] == ["alpha/chan", "alpha/other"]
    (heard,) = [r for r in routes(table, DEPLOYMENT) if r.reader == "alpha"]
    assert heard.key == f"runs/{DEPLOYMENT}/*/data/result.json"


def test_the_handler_takes_its_kind_and_its_input_from_the_table(tmp_path) -> None:
    feed = Stage("feed", StageKind.CAPTURING)
    row = Stage("alpha/chan", StageKind.CAPTURING, inputs=("feed",), strategy="chan")
    table = Table(stages=(feed, row), strategies=(SMALL,))
    storage = store(tmp_path)
    with pytest.raises(Halt, match="/feed/result.json is not stored"):
        run(handler(storage, table), CLOSE)
    header = replace(data_header(CLOSE, table_of(SMALL)), stage="feed")
    key = output_key(DEPLOYMENT, RUN, CLOSE, "feed", "snapshot")
    file = encode_output(Snapshot, OutputFile(header, (HAND_SNAPSHOT,), False))
    ref = storage.put(StageKind.CAPTURING, key, file).ref
    storage.put_result(
        result_key(DEPLOYMENT, RUN, CLOSE, "feed"), DecisionResult(header, (ref,), None)
    )
    result = run(handler(storage, table), CLOSE)
    assert result.halt is None and result.header.kind == "capturing"
    # A capturing stage with no attempt key halts in its own words on other bytes.
    other = store(tmp_path / "second")
    other.put(StageKind.CAPTURING, key, file)
    other.put_result(
        result_key(DEPLOYMENT, RUN, CLOSE, "feed"), DecisionResult(header, (ref,), None)
    )
    taken = output_key(DEPLOYMENT, RUN, CLOSE, "alpha/chan", FORECAST_OUTPUT)
    other.put(StageKind.CAPTURING, taken, b"{}")
    with pytest.raises(Halt, match="has no other key to go to"):
        run(handler(other, table), CLOSE)


def test_the_alpha_worker_loads_neither_marketlake_nor_pandas() -> None:
    code = (
        "import sys, ithildinruntime.runtime.alpha_worker; "
        "print(sorted({'lake', 'pandas', 'pyarrow', 'numpy'} & set(sys.modules)))"
    )
    ran = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert ran.stdout.strip() == "[]"


def test_the_loop_stores_one_strategys_forecast_and_another_files_halt(tmp_path) -> None:
    bad = strategy(name="other", entry="0")
    table = table_of(SMALL, bad)
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    alarms = Alarms()
    storage = folder_storage(folder.store(), alarms)
    data, file = write_data(storage, HAND_SNAPSHOT, table)
    plan = Plan(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        mode=Mode.PAPER,
        release=RELEASE,
        decisions=(PlanDecision(CLOSE, ("chan", "other")),),
    )
    loop = ReceiveLoop(
        worker="alpha-1",
        component="alpha",
        table=table,
        plan=plan,
        queue=folder.queue("alpha"),
        storage=storage,
        bucket=folder.bucket,
        handlers=alpha_handlers(
            storage=storage, deployment=DEPLOYMENT, run=RUN, release=RELEASE, table=table
        ),
        alerts=alarms,
        timer=SystemTimer(),
        beat=lambda: None,
        tick=0.05,
        stop_timeout=5.0,
    )
    keys = {s: result_key(DEPLOYMENT, RUN, CLOSE, f"alpha/{s}") for s in ("chan", "other")}
    with running({"alpha": loop}) as started:
        assert started.wait_for(lambda: all((folder.root / k).exists() for k in keys.values()), 20)

    chan = storage.read_result(keys["chan"], CLOSE)
    assert chan is not None and chan.halt is None
    inputs = {data.key: data.sha256, file.key: file.sha256}
    assert chan.header == BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        stage="alpha/chan",
        kind="deterministic",
        decision_at=CLOSE,
        inputs=inputs,
        release=RELEASE,
        config_hashes={"chan": SMALL.config_hash},
    )
    assert sha256_of((folder.root / data.key).read_bytes()) == data.sha256
    assert [ref.key.rsplit("/", 1)[1] for ref in chan.outputs] == ["forecast.json", "signals.json"]
    read = storage.read_output(keys["chan"], chan, chan.outputs[0], CLOSE, Forecast)
    assert read.header == chan.header
    assert read.entries == (
        Forecast(
            strategy="chan",
            basket=Basket({USO: d("1.00000000"), GLD: d("-3.50000000")}),
            direction=0,
            horizon_sessions=1,
            confidence=d("1.00000000"),
        ),
    )
    signals = storage.read_output(keys["chan"], chan, chan.outputs[1], CLOSE, Signal)
    assert signals.entries == forecast_of(SMALL, HAND_SNAPSHOT).signals

    other = storage.read_result(keys["other"], CLOSE)
    assert other is not None and other.halt is not None and other.outputs == ()
    assert other.halt.reason.startswith("strategy other's configuration other.toml has an [alpha]")
    assert other.halt.cause is None and dict(other.header.inputs) == inputs
    assert dict(other.header.config_hashes) == {"other": bad.config_hash}
