"""A second gate, which lists every case of which open orders fill.

It shares no helper with `core` or `risk`. It reads a check's inputs as plain
figures, as fractions rather than decimals, enumerates each subset of the open
orders and the order itself, and gives the verdict those cases imply. It
assumes inputs that halt nothing, which its caller builds.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from itertools import chain, combinations

from ithildinruntime.core import GateInputs, OrderIntent, Snapshot

Cases = list[dict[str, Fraction]]


def _f(value: object) -> Fraction:
    return Fraction(str(value))


def _subsets(orders: list) -> list:
    return list(chain.from_iterable(combinations(orders, n) for n in range(len(orders) + 1)))


def _half_even(value: Fraction, places: int) -> Fraction:
    return Fraction(round(value * 10**places), 10**places)


@dataclass(frozen=True)
class Answer:
    approved: bool
    limit: str | None
    value: Fraction | None
    bound: Fraction | None
    missing_term: str | None
    adding: bool
    worst_loss: Fraction | None
    loss: Fraction
    loss_bound: Fraction
    loss_past: bool
    past: tuple[tuple[str, Fraction, Fraction], ...]


def answer(
    intent: OrderIntent,
    positions: Mapping[str, Mapping[str, object]],
    opens: list[OrderIntent],
    cash: object,
    in_doubt: tuple,
    snapshot: Snapshot,
    constraints: object,
    inputs: GateInputs,
    now: datetime,
    close: datetime,
) -> Answer:
    caps, v = inputs.caps, _f(inputs.valuation.value)
    prices = {
        i: _f(max(by["1d"], key=lambda b: b.session).close) for i, by in snapshot.bars.items()
    }

    def start(strategy: str | None) -> dict[str, Fraction]:
        found = dict.fromkeys(prices, Fraction(0))
        for name, held in positions.items():
            for i, q in held.items():
                if strategy is None or name == strategy:
                    found[i] += _f(q)
        return found

    def cases(strategy: str | None, orders: list[OrderIntent], must: list[OrderIntent]) -> Cases:
        made = []
        for filled in _subsets(orders):
            here = start(strategy)
            for o in (*filled, *must):
                here[o.instrument] += _f(o.qty)
            made.append(here)
        return made

    mine = [o for o in opens if o.strategy == intent.strategy]
    q, i = _f(intent.qty), intent.instrument

    # Adding or reducing, case by case over which of the other orders fill.
    reduces = True
    for strategy, orders in ((intent.strategy, mine), (None, opens)):
        for case in cases(strategy, orders, []):
            was, now_is = case[i], case[i] + q
            shrinks = abs(now_is) < abs(was) and was * now_is >= 0
            reduces = reduces and shrinks
    adding = not reduces

    def position(case: dict[str, Fraction]) -> Fraction:
        return abs(case[i])

    def gross(case: dict[str, Fraction]) -> Fraction:
        return sum((abs(case[k]) * prices[k] for k in prices), Fraction(0))

    def net(case: dict[str, Fraction]) -> Fraction:
        return abs(sum((case[k] * prices[k] for k in prices), Fraction(0)))

    bounds = {
        "max_position": min(_f(constraints.max_position[i]), _f(caps.max_position[i])),
        "max_gross": min(_f(constraints.max_gross), _f(caps.max_gross)),
        "max_net": min(
            _f(constraints.max_net),
            Fraction(math.floor(_f(caps.share) * v / _f(caps.stress_move) * 10**4), 10**4),
        ),
    }
    every = cases(None, [*opens, intent], [])
    before = cases(None, opens, [])
    further, refusing = {}, {}
    for name, figure in (("max_position", position), ("max_gross", gross), ("max_net", net)):
        bound = bounds[name]
        further[name] = max(figure(case) for case in every)
        if adding:
            if further[name] > bound:
                refusing[name] = further[name]
        else:
            # Each case of the other orders, with the order filled and without it.
            grown = []
            for case in before:
                after = dict(case)
                after[i] += q
                if figure(after) > bound and figure(after) > figure(case):
                    grown.append(figure(after))
            if grown:
                refusing[name] = max(grown)

    worst = None
    if intent.strategy:
        worst = sum(
            (
                max(abs(c[k]) for c in cases(intent.strategy, [*mine, intent], []))
                * prices[k]
                * _f(caps.stress_move)
                for k in prices
            ),
            Fraction(0),
        )
        worst = Fraction(math.ceil(worst * 10**4), 10**4)

    held = start(None)
    loss = v - _f(cash) - sum((held[k] * prices[k] for k in prices), Fraction(0))
    stop = min(_f(constraints.loss_stop), _f(caps.loss_stop))
    cutoff = max(constraints.moc_cutoff, caps.moc_cutoff)
    left = Fraction((close - now).days * 86400 + (close - now).seconds) + Fraction(
        (close - now).microseconds, 10**6
    )
    most = min(_f(constraints.max_order_qty[i]), _f(caps.max_order_qty[i]))
    count = min(constraints.max_open_orders, caps.max_open_orders)
    minutes = left / 60

    ladder = [
        ("stop", str(inputs.stop) != "absent", None, None),
        ("close_entry", inputs.close_entry is not None, None, None),
        ("in_doubt", i in in_doubt, None, None),
        ("position", intent.strategy == "", None, None),
        ("max_order_qty", abs(q) > most, abs(q), most),
        *((n, n in refusing, refusing.get(n), bounds[n]) for n in ("max_position", "max_gross")),
        ("max_open_orders", len(opens) + 1 > count, Fraction(len(opens) + 1), Fraction(count)),
        ("max_net", "max_net" in refusing, refusing.get("max_net"), bounds["max_net"]),
        ("loss_stop", loss > stop, loss, stop),
        (
            "moc_cutoff",
            str(intent.type) == "market_on_close" and minutes < cutoff,
            minutes,
            Fraction(cutoff),
        ),
    ]
    named = next((row for row in ladder if row[1]), None)
    return Answer(
        approved=named is None,
        limit=named[0] if named else None,
        value=_half_even(named[2], 9) if named and named[2] is not None else None,
        bound=_half_even(named[3], 9) if named and named[3] is not None else None,
        missing_term="position" if named and named[0] == "position" else None,
        adding=adding,
        worst_loss=worst,
        loss=_half_even(loss, 4),
        loss_bound=_half_even(stop, 4),
        loss_past=loss > stop,
        past=tuple(
            (n, _half_even(further[n], 9), _half_even(bounds[n], 9))
            for n in ("max_position", "max_gross", "max_net")
            if further[n] > bounds[n]
        ),
    )
