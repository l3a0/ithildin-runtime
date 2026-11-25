"""Chan's band before the close, where each leg's newest price is its quote's midpoint.

#114's acceptance names these tests. Every price here is made up. `tests/test_alpha.py`
holds the model at the close, and the stage's halts that no quote reaches.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Context, Decimal, localcontext
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket
from ithildinruntime.alpha import BollingerSpread
from ithildinruntime.core import (
    Bar,
    Forecast,
    ForecastSet,
    Halt,
    InstrumentId,
    Signal,
    Snapshot,
    StrategyConfig,
    decode_output,
    output_key,
    result_key,
)
from ithildinruntime.data import DataSettings, DataStage, LakeSource, Mirror
from ithildinruntime.runtime.loop import Item
from tests.support import made_lake, segments
from tests.support.lookahead import answered_before_the_close
from tests.support.quotes import MAX_AGE, quote
from tests.support.spreads import DAYS, EARLY, GLD, TABLE, USO, Series, made, strategy, synthetic
from tests.test_alpha import DEPLOYMENT, RELEASE, RUN, halted, handler, store, table_of
from tests.test_alpha import run as run_alpha
from tests.test_data_source import Clock, Timer
from tests.test_data_stage import Reports

CHAN = strategy()
SMALL = strategy(hedge_sessions=3, band_sessions=3)
LOOKBACK = 60
READS = 39  # 20 + 20 − 1
TICK = timedelta(microseconds=1)
LEGS = [USO, GLD]


def d(text: object) -> Decimal:
    return Decimal(str(text))


def answer(config: StrategyConfig, held: Snapshot) -> ForecastSet | str:
    try:
        return BollingerSpread(config).forecast(held)
    except Halt as halt:
        return halt.reason


# 1. Through both stages
#
# Three prices fit each hedge ratio and three spreads make each band, as in
# tests/test_alpha.py. GLD rises by 1 a session, so each slope is half of USO's
# last price less its first. The last row's prices are the quotes' midpoints.
#
#   session     GLD     USO     hedge ratio                    spread
#   2012-03-30  10.005  30
#   2012-04-02  11.005  31.405
#   2012-04-03  12.005  32.405
#   2012-04-04  13.005  28.605  (28.605 − 31.405) / 2 = −1.4   28.605 + 1.4 × 13.005 = 46.812
#   2012-04-05  14.005  30.005  (30.005 − 32.405) / 2 = −1.2   30.005 + 1.2 × 14.005 = 46.811
#   2012-04-09  15.005  31.405  (31.405 − 28.605) / 2 = 1.4    31.405 − 1.4 × 15.005 = 10.398
#   2012-04-10  16.005  32.005  (32.005 − 30.005) / 2 = 1      32.005 − 16.005 = 16
#
# The newest band holds 46.811, 10.398 and 16. Their mean is 24.403, and they sit
# 8, −5 and −3 times 2.801 from it. Those squares sum to 98 of 2.801 squared, and
# half of that is 49, so the deviation is 7 × 2.801 = 19.607 and the z-score −3/7.
# It settles the short side alone. The band before holds 46.812, 46.811 and 10.398,
# whose z-score is −1.1547, the least that three spreads have, so it enters the long.
WORKED = [date(2012, 3, 30), *(date(2012, 4, n) for n in (2, 3, 4, 5, 9)), segments.DAY]
GOLD = ("10.005", "11.005", "12.005", "13.005", "14.005", "15.005")
OIL = ("30", "31.405", "32.405", "28.605", "30.005", "31.405")
FITTED = {
    "hedge_ratio": ("1.00000000", date(2012, 4, 5)),
    "spread": ("16.00000000", date(2012, 4, 5)),
    "spread_mean": ("24.40300000", date(2012, 4, 3)),
    "spread_std": ("19.60700000", date(2012, 4, 3)),
    "z_score": ("-0.42857143", date(2012, 4, 3)),
    "unit": ("1.00000000", date(2012, 4, 2)),
}


def test_1_both_stages_decide_at_close_20_on_the_midpoints(tmp_path: Path) -> None:
    start = DAYS.index(WORKED[0])
    assert DAYS[start : start + len(WORKED)] == WORKED
    series = [
        made_lake.Made(
            ticker,
            made_lake.ONE.format(ticker),
            segments.DAY,
            dict(zip(WORKED[:-1], map(d, closes), strict=True)),
        )
        for ticker, closes in (("GLD", GOLD), ("USO", OIL))
    ]
    lake, live = made_lake.build(tmp_path / "lake", series), tmp_path / "live"
    # The capture a minute before the decision, and one a minute before that at other prices.
    for back, moved in ((1, 0), (2, 1)):
        segments.capture(live, "GLD", back, bid=15.96 + moved, ask=16.05 + moved, last=16.02)
        segments.capture(live, "USO", back, bid=32.00 + moved, ask=32.01 + moved, last=31.9)
    table, storage = table_of(SMALL), store(tmp_path / "store")
    data = DataStage(
        LakeSource(
            Mirror(FolderBucket(lake), tmp_path / "mirror"),
            DataSettings((GLD, USO), 6, {}, 0, Decimal(100), segments.LIMITS),
            live=FolderBucket(live),
            clock=Clock(),
            timer=Timer(),
            measured=False,
            kinds=made_lake.KINDS,
            quote_kinds=segments.KINDS,
        ),
        storage,
        Clock(),
        deployment=DEPLOYMENT,
        run=RUN,
        release=RELEASE,
        config_hashes=table.hashes_for("data"),
    )
    at = segments.AT
    read = data(Item(at, "data"), Reports())
    assert read.halt is None
    storage.put_result(result_key(DEPLOYMENT, RUN, at, "data"), read)
    key = output_key(DEPLOYMENT, RUN, at, "data", "snapshot")
    (held,) = decode_output((tmp_path / "store" / key).read_bytes(), Snapshot).entries
    assert held.provisional and len(held.segments) == 4
    for leg, mid in ((GLD, d("16.005")), (USO, d("32.005"))):
        one = held.quotes[leg]
        assert (one.bid + one.ask) / 2 == mid
        assert len({one.bid, one.ask, one.last, mid}) == 4
        assert [bar.session for bar in held.bars[leg]["1d"]] == WORKED[:-1]

    result = run_alpha(handler(storage, table), at)
    assert result.halt is None
    (forecast,), signals = (
        decode_output((tmp_path / "store" / ref.key).read_bytes(), kind).entries
        for ref, kind in zip(result.outputs, (Forecast, Signal), strict=True)
    )
    assert {s.name: (f"{s.value:f}", s.first_session) for s in signals} == FITTED
    assert {s.last_session for s in signals} == {segments.DAY}
    assert forecast.direction == 1
    assert {i: str(q) for i, q in forecast.basket.legs.items()} == {
        USO: "1.00000000",
        GLD: "-1.00000000",
    }


# 2. One code path

SERIES = synthetic(260, seed=7)
FIRST = 80


def test_2_quotes_at_the_closes_give_what_the_decision_at_the_close_gives() -> None:
    sessions = range(FIRST, len(SERIES))
    assert len(sessions) >= 100
    days = [TABLE.session(SERIES.gld[n].session) for n in sessions]
    assert {(s.close + s.utc_offset).hour for s in days} == {13, 16}
    units = set()
    for n in sessions:
        late = answer(CHAN, SERIES.at(n, LOOKBACK))
        assert isinstance(late, ForecastSet)
        assert answer(CHAN, SERIES.early(n, LOOKBACK)) == late
        units.add(late.forecasts[0].direction)
    assert units == {-1, 0, 1}


def settled_at(prices: int) -> Series:
    """A series whose newest decision settles on its oldest z-score of `prices`, and no newer one.

    GLD rises by 1 a session, so under three-price fits each spread fixes USO's
    price. The first spread falls by 1 and the next by 2, so the oldest z-score is
    below −1 and enters the long. Each later fall is a little smaller than the one
    before, which puts every later z-score between −1 and 0.
    """
    gld = [d(100 + t) for t in range(prices)]
    uso, spread = [d(500), d(500)], d(500)
    falls = [d(0), d(1), *(d(2) - d(n) / 50 for n in range(prices - 4))]
    with localcontext(Context(prec=28)):
        for x, fall in zip(gld[2:], falls, strict=True):
            spread -= fall
            price = (spread - uso[-2] * x / 2) / (1 - x / 2)
            uso.append(price.quantize(d("1e-12")))
    start = DAYS.index(date(2010, 1, 4))
    return made(DAYS[start : start + prices], gld, uso)


def test_2_a_decision_before_the_close_reads_one_price_more_than_one_at_it() -> None:
    series = settled_at(LOOKBACK + 1)
    newest = len(series) - 1
    # Five prices make a z-score, so 60 prices hold none older than the sixth session's.
    assert answer(SMALL, series.at(newest, LOOKBACK)) == (
        f"strategy chan cannot tell which unit it holds: no z-score from "
        f"{series.gld[5].session} to {series.gld[newest].session} settles both sides"
    )
    found = answer(SMALL, series.early(newest, LOOKBACK))
    assert isinstance(found, ForecastSet) and found.forecasts[0].direction == 1
    (unit,) = [s for s in found.signals if s.name == "unit"]
    assert unit.first_session == series.gld[0].session
    # A settled unit does not depend on the length of the window.
    assert answer(SMALL, series.at(newest, LOOKBACK + 1)) == found


# 3. The halts

NOW = 100
DAY, BEFORE = SERIES.gld[NOW].session, SERIES.gld[NOW - 1].session
CLOSE, LAST_CLOSE = TABLE.session(DAY).close, TABLE.session(BEFORE).close
AT = CLOSE - EARLY
GOOD = SERIES.early(NOW, LOOKBACK)
NO_PRICE = f"strategy chan has no price for {DAY}: "
# Beyond it an age is past the maximum, with the one second the clock may be off.
OLDEST = AT - timedelta(seconds=MAX_AGE - 1)
TWO_DAYS = 2 * 86_400


def requoted(held: Snapshot, leg: InstrumentId, **changes: object) -> Snapshot:
    return replace(held, quotes={**held.quotes, leg: replace(held.quotes[leg], **changes)})


def without(held: Snapshot, *legs: InstrumentId) -> Snapshot:
    return replace(held, quotes={i: q for i, q in held.quotes.items() if i not in legs})


def legged(held: Snapshot, leg: InstrumentId, bars: Sequence[Bar]) -> Snapshot:
    return replace(held, bars={**held.bars, leg: {"1d": tuple(bars)}})


def raw(leg: InstrumentId, n: int) -> Snapshot:
    bars = list(GOOD.bars[leg]["1d"])
    bars[n] = replace(bars[n], basis="raw")
    return legged(GOOD, leg, bars)


WITH_BARS = replace(GOOD, bars=SERIES.at(NOW, LOOKBACK).bars)
AT_CLOSE = replace(
    WITH_BARS,
    decision_at=CLOSE,
    quotes={i: quote(i, q.bid, q.ask, CLOSE) for i, q in GOOD.quotes.items()},
)
BOTH_MARKS = f"has a quote and a bar for {DAY}, and nothing says which is the mark"


def cases_of(leg: InstrumentId) -> dict[str, tuple[Snapshot, str]]:
    one = GOOD.quotes[leg]
    unread = f"{NO_PRICE}{leg}'s quote cannot be read: "
    return {
        f"no-quote-of-{leg}": (
            without(GOOD, leg),
            f"{NO_PRICE}{leg} has no mark: its newest bar is for {BEFORE}, not later than {BEFORE}",
        ),
        f"a-quote-of-{leg}-past-its-age": (
            requoted(GOOD, leg, bid_at=OLDEST - TICK),
            f"{unread}its bid has the stamp {one.received['bid_time']!r}, at "
            f"{(OLDEST - TICK).isoformat()}, which is 89.000001 seconds old at {AT.isoformat()}, "
            f"and with the clock bound of 1 past the written maximum of 90, and it was captured "
            f"at {one.captured_at.isoformat()}",
        ),
        f"a-quote-of-{leg}-past-its-spread": (
            requoted(GOOD, leg, max_spread_share=d("0.0001")),
            f"{unread}its bid of {one.bid} and ask of {one.ask} are further apart than the "
            "written 0.0001 of their midpoint",
        ),
        f"a-quote-of-{leg}-not-trading-normally": (
            requoted(GOOD, leg, status="Halted"),
            f"{unread}its status is 'Halted', and the stated normal one is 'Normal'",
        ),
        f"a-quote-of-{leg}-stamped-at-the-last-close": (
            requoted(GOOD, leg, bid_at=LAST_CLOSE, max_age=TWO_DAYS),
            f"{NO_PRICE}{leg} has no mark: its quote's older stamp is {LAST_CLOSE.isoformat()}, "
            f"not later than the close of {BEFORE} at {LAST_CLOSE.isoformat()}",
        ),
        f"a-quote-of-{leg}-beside-both-bars-of-the-session": (
            without(AT_CLOSE, *(i for i in LEGS if i != leg)),
            f"{NO_PRICE}{leg} {BOTH_MARKS}",
        ),
        f"a-quote-of-{leg}-beside-the-others-bar-of-the-session": (
            legged(
                without(AT_CLOSE, *(i for i in LEGS if i != leg)),
                leg,
                AT_CLOSE.bars[leg]["1d"][:-1],
            ),
            "strategy chan reads both legs on the same sessions, and USO and GLD have bars of "
            f"different ones, such as {DAY}",
        ),
        f"a-bar-of-{leg}-as-traded-under-good-quotes": (
            raw(leg, -1),
            f"strategy chan reads adjusted prices, and {leg}'s bar of {BEFORE} has the basis raw",
        ),
        f"a-middle-bar-of-{leg}-as-traded-under-good-quotes": (
            raw(leg, 30),
            f"strategy chan reads adjusted prices, and {leg}'s bar of "
            f"{SERIES.gld[NOW - LOOKBACK + 30].session} has the basis raw",
        ),
        f"the-oldest-bar-of-{leg}-as-traded-under-good-quotes": (
            raw(leg, 0),
            f"strategy chan reads adjusted prices, and {leg}'s bar of "
            f"{SERIES.gld[NOW - LOOKBACK].session} has the basis raw",
        ),
    }


def two_back() -> Snapshot:
    held = GOOD
    for leg in LEGS:
        held = legged(held, leg, GOOD.bars[leg]["1d"][:-1])
    return held


# The first session from FIRST whose own z-score settles the unit.
OWN = next(
    n
    for n in range(FIRST, len(SERIES))
    if isinstance(answer(CHAN, SERIES.at(n, READS)), ForecastSet)
)
TOO_FEW = (
    f"strategy chan reads {READS} sessions of USO and GLD, and the snapshot holds 38 to "
    f"{SERIES.gld[OWN].session}"
)
HALTS = {
    **cases_of(USO),
    **cases_of(GLD),
    "quotes-beside-bars-that-end-after-the-decision": (
        WITH_BARS,
        f"{NO_PRICE}USO's bar for {DAY} ends at {CLOSE.isoformat()}, after the decision at "
        f"{AT.isoformat()}",
    ),
    "quotes-beside-bars-of-the-session-at-the-close": (AT_CLOSE, f"{NO_PRICE}USO {BOTH_MARKS}"),
    "a-newest-bar-two-sessions-back-under-good-quotes": (
        two_back(),
        f"strategy chan decides on the session {DAY}, and the newest bar of USO and GLD is "
        f"{SERIES.gld[NOW - 2].session}'s",
    ),
    "too-few-prices-with-the-decision-price-among-them": (SERIES.early(OWN, 37), TOO_FEW),
}


def test_3_the_snapshot_the_halts_below_change_forecasts() -> None:
    assert BEFORE == DAY - timedelta(days=1)
    assert isinstance(answer(CHAN, GOOD), ForecastSet)


@pytest.mark.parametrize("case", HALTS)
def test_3_a_price_the_model_cannot_use_halts_with_its_inputs_and_no_cause(tmp_path, case) -> None:
    held, reason = HALTS[case]
    assert halted(tmp_path, CHAN, held) == reason


@pytest.mark.parametrize("leg", LEGS)
def test_3_what_sits_at_each_bound_of_a_quote_forecasts(leg) -> None:
    whole = answer(CHAN, GOOD)
    assert isinstance(whole, ForecastSet)
    oldest = requoted(GOOD, leg, bid_at=OLDEST, ask_at=OLDEST)
    after = requoted(GOOD, leg, bid_at=LAST_CLOSE + TICK, max_age=TWO_DAYS)
    assert answer(CHAN, oldest) == answer(CHAN, after) == whole


def test_3_the_decision_price_counts_towards_the_window() -> None:
    assert answer(CHAN, SERIES.at(OWN, 38)) == TOO_FEW
    whole = answer(CHAN, SERIES.at(OWN, 39))
    assert isinstance(whole, ForecastSet)
    assert answer(CHAN, SERIES.early(OWN, 38)) == whole


def test_3_a_midpoint_equal_to_the_closes_before_it_halts(tmp_path) -> None:
    days = DAYS[DAYS.index(date(2011, 11, 17)) :][:7]
    series = made(days, (10, 11, 12, 13, 14, 14, 16), (38, 37, 13, 16, 19, 29, 26))
    assert isinstance(answer(SMALL, series.early(6, 6)), ForecastSet)
    assert halted(tmp_path, SMALL, series.early(6, 6, (d(14), d(26)))) == (
        f"strategy chan cannot fit a hedge ratio on {days[-1]}: GLD's last 3 prices are all 14"
    )


# 4. No look-ahead before the close

SESSIONS = range(FIRST, FIRST + 40)


class Ahead(BollingerSpread):
    """A model that prices a leg at its session's close where its run holds one."""

    def __init__(self, series: Series) -> None:
        super().__init__(CHAN)
        pairs = zip(series.gld, series.uso, strict=True)
        self._closes = {gld.session: (gld.close, uso.close) for gld, uso in pairs}

    def _fits(self, held: Snapshot):
        fits = super()._fits(held)
        if fits.days[-1] in self._closes:
            fits._independent[-1], fits._dependent[-1] = self._closes[fits.days[-1]]
        return fits


def test_4_a_decision_before_the_close_reads_nothing_of_its_session_but_its_quotes() -> None:
    found = answered_before_the_close(
        lambda series: BollingerSpread(CHAN), SERIES, lookback=LOOKBACK, sessions=SESSIONS
    )
    for whole, part in found.values():
        assert isinstance(whole, ForecastSet) and part == whole


def test_4_a_model_that_reads_its_sessions_close_fails() -> None:
    found = answered_before_the_close(Ahead, SERIES, lookback=LOOKBACK, sessions=SESSIONS)
    assert [n for n, (whole, part) in found.items() if whole != part] == list(SESSIONS)
