"""The cost model: a strategy's cost section, checked and read as a `CostSchedule`.

The section holds one list of terms per instrument. A term looks like this.

    [[cost.USO]]
    kind = "charge"
    side = "sell"
    fixed = 0
    per_share = 0
    per_dollar = 0.0000206
    source = "SEC fee rate advisory 2026-2, in force from 2026-04-04"

`kind` is `charge`, for a commission or a fee the broker books, or `gap`, for
the gap from the quote's midpoint. `side` is `both` or `sell`. `fixed` is
charged per trade, `per_share` per share and `per_dollar` per dollar traded.
`source` is a dated published text, or `placeholder:` and a reason.

Every figure is written, a zero included, so nothing absent reads as free
(hard limit 3). The model halts, naming the strategy and the instrument, on
each of these.

1. A strategy with no cost section, or an empty one.
2. An instrument the strategy trades with no terms, or with no term of one
   kind.
3. A section entry for an instrument the strategy does not trade.
4. A term that leaves out a field or holds one this form does not have.
5. A figure that is negative, is not a number, or carries more places than
   its field declares, which would otherwise be rounded or stored unrounded.
6. A `kind` or a `side` this form does not have.
7. A `source` that is empty, or a placeholder with no reason.

The check runs at each decision, inside the stage, so a bad section halts the
cost stage with its reason stored. `tests/test_cost.py` reads every tracked
strategy file first, so a bad one fails CI before any run.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from decimal import Decimal

from ithildinruntime.core import (
    CostKind,
    CostSchedule,
    CostSide,
    CostTerm,
    Halt,
    InstrumentId,
    Snapshot,
    StrategyConfig,
    exceeds_places,
    places,
)

__all__ = ["SECTION", "LinearCostModel"]

SECTION = "cost"
_PLACEHOLDER = "placeholder:"
_FIELDS = frozenset(f.name for f in dataclasses.fields(CostTerm))


class LinearCostModel:
    """One strategy's flat and linear costs, which implements `core.CostModel`."""

    def __init__(self, strategy: StrategyConfig) -> None:
        self._strategy = strategy

    def schedule(self, snapshot: Snapshot) -> CostSchedule:
        """The strategy's terms. It reads no price: `estimate` takes the caller's."""
        strategy = self._strategy
        section = strategy.section(SECTION)
        if extra := sorted(set(section) - set(strategy.instruments)):
            raise self._refuse(", ".join(extra), "which the strategy does not trade")
        return CostSchedule(terms={i: self._terms(i, section.get(i)) for i in strategy.instruments})

    def _refuse(self, instrument: str, what: str) -> Halt:
        return Halt(
            f"strategy {self._strategy.name}'s configuration {self._strategy.source} has a "
            f"[{SECTION}] entry for {instrument}, {what}"
        )

    def _terms(self, instrument: InstrumentId, listed: object) -> tuple[CostTerm, ...]:
        if listed is None:
            raise Halt(
                f"strategy {self._strategy.name}'s configuration {self._strategy.source} has no "
                f"[{SECTION}] terms for {instrument}"
            )
        if not isinstance(listed, tuple) or not listed:
            raise self._refuse(instrument, "which holds no list of terms")
        terms = tuple(
            _term(entry, lambda what, n=n: self._refuse(instrument, f"whose term {n} {what}"))
            for n, entry in enumerate(listed, start=1)
        )
        if absent := [k for k in CostKind if k not in {t.kind for t in terms}]:
            raise self._refuse(instrument, f"which states no {' and no '.join(absent)} term")
        return terms


def _term(entry: object, refuse: Callable[[str], Halt]) -> CostTerm:
    if not isinstance(entry, Mapping):
        raise refuse("is not a table")
    if missing := sorted(_FIELDS - set(entry)):
        raise refuse(f"states no {', '.join(missing)}")
    if unknown := sorted(set(entry) - _FIELDS):
        raise refuse(f"holds {', '.join(unknown)}, which no term has")
    source = entry["source"]
    if not isinstance(source, str) or not source.strip().removeprefix(_PLACEHOLDER).strip():
        raise refuse(f"has the source {source!r}, which names no text and no reason")
    return CostTerm(
        kind=_choice(CostKind, "kind", entry, refuse),
        side=_choice(CostSide, "side", entry, refuse),
        fixed=_figure("fixed", entry, refuse),
        per_share=_figure("per_share", entry, refuse),
        per_dollar=_figure("per_dollar", entry, refuse),
        source=source,
    )


def _choice[E: (CostKind, CostSide)](
    choices: type[E], field: str, entry: Mapping[str, object], refuse: Callable[[str], Halt]
) -> E:
    value = entry[field]
    if not isinstance(value, str) or value not in list(choices):
        raise refuse(f"has the {field} {value!r}, which is not {' or '.join(choices)}")
    return choices(value)


def _figure(field: str, entry: Mapping[str, object], refuse: Callable[[str], Halt]) -> Decimal:
    value = entry[field]
    # A bool is an int to Python, and no figure.
    if isinstance(value, bool) or not isinstance(value, int | Decimal):
        raise refuse(f"has {field} = {value!r}, which is not a number")
    figure = Decimal(value)
    if figure < 0:
        raise refuse(f"has {field} = {figure}, which is negative")
    if exceeds_places(figure, n := places(CostTerm, field)):
        raise refuse(f"has {field} = {figure}, with more than the {n} places it is stored at")
    return figure
