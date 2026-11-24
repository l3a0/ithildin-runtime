"""The look-ahead harness for an alpha model, which #57 item 1 asks for.

It runs a model over a whole series and again with the latest sessions removed.
Each model comes from a factory handed the series of that run, so a model that
reads past its decision has somewhere to read it from. A decision that answers
otherwise, once sessions after it are gone, read one of them.

Cutting the snapshot at the decision is the data module's rule, and its own
tests hold it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ithildinruntime.core import AlphaModel, ForecastSet, Halt

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
        try:
            answers[day] = model.forecast(series.at(n, lookback))
        except Halt as halt:
            answers[day] = halt.reason
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
