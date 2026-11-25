"""The alpha stage over the vintages the replication reads, for #6's acceptance 1 and 2.

`tests/support/replication.py` holds the run and the fixtures. Every figure
over these vintages is exploratory, so this checks the engine against a known
answer and shows no edge.
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest

from ithildinruntime.alpha import build_model
from ithildinruntime.core import Halt
from tests.support import lake as lakes
from tests.support.chan_return import chan_figures
from tests.support.replication import (
    CHAN,
    FIRST_DECISION,
    GLD,
    LOOKBACK,
    ROWS_BEFORE,
    SESSIONS,
    USO,
    cut,
)

# The SHA-256 of `chan.bollinger`'s own units, run at 19aadbeb, one line a decision.
UNITS_SHA256 = "5c7e77fd9f7632a0ca1f6cd0e10246c19321735f4747eecac799d459b646bca3"


def held_spells(units: Sequence[int]) -> tuple[list[int], int]:
    """Each position's sessions, counted flat to flat, and the reversals inside them."""
    spells, current, reversals, before = [], 0, 0, 0
    for unit in units:
        if unit:
            current += 1
            reversals += before == -unit
        elif current:
            spells.append(current)
            current = 0
        before = unit
    return (spells + [current] if current else spells), reversals


def test_the_units_are_the_replications_on_every_decision(every_session, replicated) -> None:
    assert len(every_session.bars[GLD]["1d"]) == len(every_session.bars[USO]["1d"]) == 1500
    assert len(replicated) == 1461 and (replicated[0][0], replicated[-1][0]) == (
        FIRST_DECISION,
        lakes.LAST_SESSION,
    )
    units = [forecast.direction for _, forecast, _ in replicated]
    assert (units.count(-1), units.count(0), units.count(1)) == (547, 315, 599)
    lines = "".join(
        f"{day.isoformat()},{unit}\n" for (day, _, _), unit in zip(replicated, units, strict=True)
    )
    assert hashlib.sha256(lines.encode()).hexdigest() == UNITS_SHA256


def test_the_positions_the_units_make(replicated) -> None:
    units = [forecast.direction for _, forecast, _ in replicated]
    spells, reversals = held_spells(units)
    assert (len(spells), statistics.median(spells), max(spells)) == (77, 14, 40)
    assert units[-1] != 0 and reversals == 9
    # The 162 changes of unit the replication pins: 77 entries, 76 exits and 9 reversals.
    changes = sum(a != b for a, b in zip([0, *units], units, strict=False))
    assert changes == 162


def test_the_longest_walk_stays_inside_the_lookback(replicated) -> None:
    days = SESSIONS.between(lakes.FIRST_SESSION, lakes.LAST_SESSION)
    place = {session.day: n for n, session in enumerate(days)}
    reads = [
        place[unit.last_session] - place[unit.first_session] + 1 for _, _, (*_, unit) in replicated
    ]
    # 17 z-scores, the first of which reads 39 sessions and each other one more.
    assert max(reads) == 55
    longest = [day for (day, _, _), n in zip(replicated, reads, strict=True) if n == 55]
    assert longest == [date(2010, 3, 3), date(2012, 3, 27)]


def test_a_decision_on_the_session_before_the_first_would_enter_a_day_early(every_session) -> None:
    days = [bar.session for bar in every_session.bars[GLD]["1d"]]
    n = days.index(date(2006, 6, 20))
    # The last of the replication's 20 dropped rows is its 20th session, and 2006-06-20 is
    # the 19th it keeps, where it has no z-score.
    assert days[19] == date(2006, 5, 23) and n - 19 == ROWS_BEFORE
    made = build_model(CHAN).forecast(cut(every_session, n, LOOKBACK))
    assert made.forecasts[0].direction == 1
    (score,) = [s.value for s in made.signals if s.name == "z_score"]
    assert score == Decimal("-1.07539456")


def test_a_lookback_of_40_halts_where_it_cannot_settle_and_is_never_wrong(
    every_session, replicated
) -> None:
    model = build_model(CHAN)
    days = [bar.session for bar in every_session.bars[GLD]["1d"]]
    halted = wrong = 0
    for day, forecast, _ in replicated:
        try:
            made = model.forecast(cut(every_session, days.index(day), 40))
        except Halt as halt:
            assert "cannot tell which unit it holds" in halt.reason
            halted += 1
        else:
            wrong += made.forecasts != (forecast,)
    assert (halted, wrong) == (513, 0)


def test_chans_return_on_the_stored_forecasts_is_the_replications(
    every_session, replicated
) -> None:
    closes = {
        i: {bar.session: bar.close for bar in by["1d"]} for i, by in every_session.bars.items()
    }
    days = sorted(closes[GLD])
    first = days.index(FIRST_DECISION)
    rows = [(day, {}) for day in days[first - ROWS_BEFORE : first]]
    rows += [
        (day, {i: forecast.direction * leg for i, leg in forecast.basket.legs.items()})
        for day, forecast, _ in replicated
    ]
    assert len(rows) == 1480 and rows[0][0] == date(2006, 5, 24)
    apr, sharpe = chan_figures(rows, closes)
    assert apr == pytest.approx(0.17824928, abs=5e-9, rel=0)
    assert sharpe == pytest.approx(0.96467289, abs=5e-9, rel=0)
