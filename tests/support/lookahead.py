"""The look-ahead harness for an alpha model, which #57 item 1 asks for.

It runs a model over a whole series and again with the latest sessions removed.
Each model comes from a factory handed the series of that run, so a model that
reads past its decision has somewhere to read it from. A decision that answers
otherwise, once sessions after it are gone, read one of them.

A decision before its session's close can look ahead to that close, which the
removal above leaves in both runs. `answered_before_the_close` is for that one: it
hands the factory a series that ends the session before the decision, beside
the same snapshot and quotes, whose midpoints are not the closes.

Cutting the snapshot at the decision is the data module's rule, and its own
tests hold it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from decimal import Decimal

from ithildinruntime.core import AlphaModel, ForecastSet, Halt, Snapshot

from .spreads import Series

type Factory = Callable[[Series], AlphaModel]


def decided(
    factory: Factory, series: Series, *, lookback: int, first: int, newest_first: bool = False
) -> dict[date, ForecastSet | str]:
    """What one model instance decides at each session's close, a halt as its reason."""
    model = factory(series)
    order = range(first, len(series))
    answers: dict[date, ForecastSet | str] = {}
    for n in reversed(order) if newest_first else order:
        day = series.gld[n].session
        answers[day] = _answer(model, series.at(n, lookback))
    return answers


def disagreements(
    whole: dict[date, ForecastSet | str], part: dict[date, ForecastSet | str]
) -> list[tuple[date, str]]:
    """Each session both runs decided and what differs there, a signal by its name."""
    found = []
    for day in sorted(set(whole) & set(part)):
        one, two = whole[day], part[day]
        if isinstance(one, str) or isinstance(two, str):
            if one != two:
                found.append((day, "halt"))
            continue
        if one.forecasts != two.forecasts:
            found.append((day, "forecast"))
        if [s.name for s in one.signals] != [s.name for s in two.signals]:
            found.append((day, "signals"))
            continue
        pairs = zip(one.signals, two.signals, strict=True)
        found += [(day, a.name) for a, b in pairs if a != b]
    return found


def answered_before_the_close(
    factory: Factory, series: Series, *, lookback: int, sessions: Iterable[int]
) -> dict[int, tuple[ForecastSet | str, ForecastSet | str]]:
    """Each session's close−20 answer, and the one given once the run holds nothing of it."""
    found = {}
    for n in sessions:
        away = (series.gld[n].close + Decimal("0.5"), series.uso[n].close - Decimal("0.3"))
        held = series.early(n, lookback, away)
        whole, part = factory(series), factory(series.first(n))
        found[n] = (_answer(whole, held), _answer(part, held))
    return found


def _answer(model: AlphaModel, held: Snapshot) -> ForecastSet | str:
    try:
        return model.forecast(held)
    except Halt as halt:
        return halt.reason
