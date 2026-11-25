"""The risk model: a strategy's `[risk]` section, tightened by the owner's hard caps.

The section holds each field of `core.Constraints` but the strategy's name.
A section looks like this, and its figures are made up.

    [risk]
    max_gross = 1000.00
    max_net = 900.00
    max_open_orders = 4
    loss_stop = 50.00
    moc_cutoff = 0

    [risk.max_order_qty]
    GLD = 10
    USO = 20

    [risk.max_position]
    GLD = 5
    USO = 10

A limit here is what the strategy was tested under, and a hard cap is what the
owner allows. `core.tighten` takes the tighter of each pair, so no limit
written here loosens a cap. `max_gross`, `max_net` and `loss_stop` are dollars,
`moc_cutoff` is minutes before the close, and the two tables are shares.

Every field is written, because an absent value never reads as a default (hard
limit 3). A section is refused, naming the strategy, the file and the field, on
each of these.

1. No `[risk]` section, or an empty one.
2. A field the section leaves out, or one this form does not have. `core`'s
   reader gives an empty table as absent.
3. A `max_order_qty` or a `max_position` that is no table, or one whose
   instruments are not exactly the ones the strategy trades.
4. A value `core.check_constraints` refuses, such as a negative one, a true or
   false, or an order size below the position it has to close.

An integer or a TOML float reads exactly, and a count is an integer alone. The
check runs at each decision, so a section it refuses halts the risk stage and
never the worker's start. `core`'s strategy reader refuses a number that is not
finite first, when the file is loaded.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from decimal import Decimal

from ithildinruntime.core import (
    Book,
    Constraints,
    Halt,
    HardCaps,
    Snapshot,
    StrategyConfig,
    StressRecords,
    Valuation,
    check_constraints,
    held,
    hold_to_record,
    mark,
    require_current,
    tighten,
)

__all__ = ["SECTION", "LimitRisk"]

SECTION = "risk"
_FIELDS = tuple(f.name for f in dataclasses.fields(Constraints) if f.name != "strategy")
_SHARES = ("max_order_qty", "max_position")
_COUNTS = ("max_open_orders", "moc_cutoff")


class LimitRisk:
    """One strategy's written limits, which implements `core.RiskModel`."""

    def __init__(self, strategy: StrategyConfig, records: StressRecords) -> None:
        self._strategy = strategy
        self._records = records

    def constraints(
        self, book: Book, snapshot: Snapshot, caps: HardCaps, valuation: Valuation
    ) -> Constraints:
        """The strategy's limits, tightened by the hard caps and the session's V.

        It halts on a V or a stress move the decision may not use, and on caps
        the stress records do not hold. It also halts on a book the snapshot
        cannot mark, because no limit in dollars can be read for a position or
        an open order with no price.
        """
        written = self._written()
        owners = hold_to_record(written, caps, self._records)
        tightened = tighten(written, owners, valuation)
        require_current(owners, valuation, snapshot.decision_at)
        for instrument in held(book):
            mark(snapshot, instrument, valuation.session)
        return tightened

    def _written(self) -> Constraints:
        """The section as `Constraints`, checked."""
        strategy = self._strategy
        section = strategy.section(SECTION)

        def refuse(what: str) -> Halt:
            return Halt(
                f"strategy {strategy.name}'s configuration {strategy.source} has a "
                f"[{SECTION}] section that {what}"
            )

        if missing := [name for name in _FIELDS if name not in section]:
            raise refuse(f"states no {', '.join(missing)}")
        if unknown := sorted(set(section) - set(_FIELDS)):
            raise refuse(f"holds {', '.join(unknown)}, which this form does not have")
        fields = {
            name: self._shares(name, section[name], refuse)
            if name in _SHARES
            else section[name]
            if name in _COUNTS
            else _exact(section[name])
            for name in _FIELDS
        }
        written = Constraints(strategy=strategy.name, **fields)  # type: ignore[arg-type]
        try:
            check_constraints(written)
        except Halt as halt:
            raise refuse(f"core refuses: {halt.reason}") from None
        return written

    def _shares(
        self, name: str, table: object, refuse: Callable[[str], Halt]
    ) -> dict[object, object]:
        if not isinstance(table, Mapping):
            raise refuse(f"has {name} = {table!r}, which is not a table of instruments")
        traded = self._strategy.instruments
        if missing := [i for i in traded if i not in table]:
            raise refuse(f"states no {name} for {', '.join(missing)}")
        if extra := sorted(set(table) - set(traded)):
            raise refuse(
                f"holds a {name} for {', '.join(extra)}, which the strategy does not trade"
            )
        return {i: _exact(table[i]) for i in traded}


def _exact(value: object) -> object:
    """An integer as the decimal it writes. `core`'s check refuses what is no decimal."""
    # A bool is an int to Python, and no figure.
    return Decimal(value) if type(value) is int else value
