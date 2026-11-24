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

Every figure is written, a zero included, so a cost nobody wrote never reads
as free (hard limit 3). The model halts, naming the strategy and the
instrument, on each of these.

1. A strategy with no cost section, or an empty one, which names every
   instrument the strategy trades.
2. An instrument the strategy trades with no terms, or with no `both` term of
   one kind. A buy with only a sale's fee written would otherwise cost zero.
3. A section entry for an instrument the strategy does not trade, which is
   usually a name mistyped or left behind.
4. A term that leaves out a field or holds one this form does not have.
5. A figure that is negative, is no number, or is written with more places
   than its field declares, trailing zeros included.
6. A `kind` or a `side` this form does not have.
7. A `source` that is empty, or that starts with the word placeholder in any
   case and is not `placeholder:` and a reason.

The check runs at each decision, inside the stage, so a section it refuses
halts the cost stage with its reason stored. `core`'s strategy reader refuses
some forms first, such as a blank `[[cost.USO]]` or a figure that is not
finite, and those fail the load of every strategy named with it. It reads an
empty table as absent.
`tests/test_cost.py` reads every tracked strategy file both ways, so a bad one
fails CI before any run.
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
        try:
            section = strategy.section(SECTION)
        except Halt as halt:
            absent = ", ".join(strategy.instruments)
            raise Halt(f"{halt.reason}, so it states no cost for {absent}") from None
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
            raise self._refuse(
                instrument,
                f'which holds no list of terms. Each term starts with [[{SECTION}."{instrument}"]]',
            )
        terms = tuple(
            _term(entry, lambda what, n=n: self._refuse(instrument, f"whose term {n} {what}"))
            for n, entry in enumerate(listed, start=1)
        )
        stated = {t.kind for t in terms if t.side == CostSide.BOTH}
        if absent := [k for k in CostKind if k not in stated]:
            raise self._refuse(
                instrument,
                f"which states no {' and no '.join(absent)} term for both sides. "
                "Write one whose figures are zero where there is no such cost",
            )
        return terms


def _term(entry: object, refuse: Callable[[str], Halt]) -> CostTerm:
    if not isinstance(entry, Mapping):
        raise refuse("is not a table")
    if missing := sorted(_FIELDS - set(entry)):
        raise refuse(f"states no {', '.join(missing)}")
    if unknown := sorted(set(entry) - _FIELDS):
        raise refuse(f"holds {', '.join(unknown)}, which no term has")
    source = entry["source"]
    if not isinstance(source, str) or not _named(source.strip()):
        raise refuse(
            f"has the source {source!r}. Write a dated published text, or "
            f"`{_PLACEHOLDER}` in lower case and a reason"
        )
    return CostTerm(
        kind=_choice(CostKind, "kind", entry, refuse),
        side=_choice(CostSide, "side", entry, refuse),
        fixed=_figure("fixed", entry, refuse),
        per_share=_figure("per_share", entry, refuse),
        per_dollar=_figure("per_dollar", entry, refuse),
        source=source.strip(),
    )


def _named(source: str) -> bool:
    """Whether a source names a published text, or is `placeholder:` and a reason."""
    if not source.casefold().startswith("placeholder"):
        return bool(source)
    return source.startswith(_PLACEHOLDER) and bool(source.removeprefix(_PLACEHOLDER).strip())


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
