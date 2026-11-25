"""How one strategy's target is sized: a forecast's basket, scaled to a fixed gross and cut.

The strategy's `[portfolio]` section holds one key, `gross`, in dollars, above
zero and at no more than the places money declares. It is the gross dollars a
position of the strategy is sized to, the sum of each leg's size in dollars, and
the owner sets it. A section looks like this, and its figure is made up.

    [portfolio]
    gross = 1000

With one basket per strategy, equal risk weight is one scale factor. Let a leg's
weight be its shares in one basket times its mark, as a size.

1. **What is wanted** is the number of baskets whose gross is `gross`.
2. **Three limits bound that number**, and the smallest of the four is taken, so
   every leg shrinks together. `max_position` bounds each leg's shares.
   `max_gross` bounds the legs' dollars, and `max_net` the larger of the long
   legs' dollars and the short legs' dollars, because the gate counts each leg
   with the other unfilled.
3. **A dollar limit is read less room for one stress move**, as the limit over
   one plus the stress move. A leg the market moved that far is then still
   inside a limit that has not moved, so an exit is not refused on that limit.
4. **Each leg is rounded toward zero** to the account's share step, so no leg
   passes `gross` or a limit. A leg can round to zero beside one that does not,
   and only a target of zero in every leg halts.

Each bound is kept as one exact fraction, two bounds are compared across their
divisors, and each share count is one division, so a target that sits exactly
on a limit or a step is kept.

Nothing here reads what is held, the cash, the profit or the account's value,
but through the limits. The target is absolute, and the gate counts what is
held and sent when it checks each order. The section is read at every decision
and every direction, so a strategy with no `gross` halts while it is flat too.
"""

from __future__ import annotations

from collections.abc import Collection
from decimal import ROUND_DOWN, Decimal
from typing import NamedTuple

from ithildinruntime.core import (
    Book,
    Constraints,
    Forecast,
    Halt,
    HardCaps,
    InstrumentId,
    Limit,
    Sizing,
    Snapshot,
    StrategyConfig,
    TargetPortfolio,
    Valuation,
    exact_product,
    exact_total,
    exceeds_places,
    finite,
    held,
    mark,
    places,
    quotient,
    require_current,
    rounded,
    tighten,
)

__all__ = ["SECTION", "gross", "size"]

SECTION = "portfolio"
_KEY = "gross"
# `gross` is compared with the dollar limits, so it carries the places they do.
_GROSS = places(Constraints, "max_gross")
_WANTED = places(Sizing, "wanted")


class _Baskets(NamedTuple):
    """A number of baskets, as one exact fraction whose divisor is above zero."""

    dividend: Decimal
    divisor: Decimal

    def below(self, other: _Baskets) -> bool:
        """Whether this is fewer baskets than `other`."""
        return exact_product("a size", self.dividend, other.divisor) < exact_product(
            "a size", other.dividend, self.divisor
        )


def gross(strategy: StrategyConfig) -> Decimal:
    """The gross dollars a position of the strategy is sized to, from its section."""
    section = strategy.section(SECTION)

    def refuse(what: str) -> Halt:
        return Halt(
            f"strategy {strategy.name}'s configuration {strategy.source} has a "
            f"[{SECTION}] section that {what}"
        )

    if _KEY not in section:
        raise refuse(f"states no {_KEY}")
    if unknown := sorted(set(section) - {_KEY}):
        raise refuse(f"holds {', '.join(unknown)}, which this form does not have")
    written = section[_KEY]
    # A bool is an int to Python, and no figure.
    value = Decimal(written) if type(written) is int else written
    if not isinstance(value, Decimal):
        raise refuse(f"has {_KEY} = {written!r}, which is not a number")
    if not value.is_finite() or value <= 0:
        raise refuse(f"has {_KEY} = {written}, which is not a finite number above zero")
    if exceeds_places(value, _GROSS):
        raise refuse(f"has {_KEY} = {written}, which carries more than {_GROSS} places")
    return value


def size(
    strategy: StrategyConfig,
    deployed: Collection[str],
    forecast: Forecast,
    snapshot: Snapshot,
    book: Book,
    constraints: Constraints,
    caps: HardCaps,
    valuation: Valuation,
) -> tuple[TargetPortfolio, Sizing]:
    """One strategy's target for the decision, and the record of how it was sized.

    `deployed` names every strategy of the deployment. It halts on the first
    input it cannot size from, and each check but the last holds for a flat
    forecast too.
    """
    name = strategy.name
    wanted_gross = gross(strategy)
    _check_book(book, deployed)
    legs = _checked_legs(strategy, forecast, book)
    require_current(caps, valuation, snapshot.decision_at)
    limits = tighten(constraints, caps, valuation)
    if unlimited := sorted(set(legs) - set(limits.max_position)):
        raise Halt(f"no limit names {unlimited[0]}, a leg of {name}'s basket")
    marks = {i: mark(snapshot, i, valuation.session) for i in legs}
    # The step is a power of ten, so its exponent is the places a share count carries.
    step = -caps.share_step.adjusted()

    if forecast.direction == 0:
        return (
            TargetPortfolio(snapshot.decision_at, name, dict.fromkeys(legs, _zero(step))),
            Sizing(name, dict.fromkeys(legs, _zero(_WANTED)), ()),
        )

    shares = {i: leg.copy_abs() for i, leg in legs.items()}
    long = {i: (leg > 0) == (forecast.direction > 0) for i, leg in legs.items()}
    dollars = {i: exact_product(f"a basket's dollars in {i}", shares[i], marks[i]) for i in shares}
    whole = exact_total("a basket's gross", dollars.values())
    side = max(
        exact_total("a basket's one side", (dollars[i] for i in legs if long[i] is which))
        for which in (True, False)
    )
    room = exact_total("one plus the stress move", (Decimal(1), caps.stress_move))
    asked = _Baskets(wanted_gross, whole)
    per_leg = ((limits.max_position[i], q) for i, q in shares.items() if q)
    bounds = [
        *((Limit.MAX_POSITION, _Baskets(*bound)) for bound in per_leg),
        (Limit.MAX_GROSS, _Baskets(limits.max_gross, exact_product("gross room", room, whole))),
        (Limit.MAX_NET, _Baskets(limits.max_net, exact_product("net room", room, side))),
    ]
    cut = tuple(dict.fromkeys(limit for limit, bound in bounds if bound.below(asked)))
    taken = asked
    for _, bound in bounds:
        if bound.below(taken):
            taken = bound

    def count(baskets: _Baskets, n: int) -> dict[InstrumentId, Decimal]:
        made = {}
        for i, q in shares.items():
            what = f"{name}'s target in {i}"
            exact = exact_product(what, q, baskets.dividend)
            qty = rounded(what, quotient(what, exact, baskets.divisor, n, ROUND_DOWN), n)
            made[i] = qty if long[i] or not qty else qty.copy_negate()
        return made

    target = count(taken, step)
    if not any(target.values()):
        raise Halt(
            f"{name} forecasts {forecast.direction} and its target is zero shares in every leg, "
            f"at a {_KEY} of {wanted_gross}"
            + (f" cut by {' and '.join(cut)}" if cut else " that no limit cut")
        )
    return (
        TargetPortfolio(snapshot.decision_at, name, target),
        Sizing(name, count(asked, _WANTED), cut),
    )


def _check_book(book: Book, deployed: Collection[str]) -> None:
    """Halt on shares or an open order of a strategy the deployment does not run.

    A target is sized as if the account held nothing else, and one whose legs
    net against another's on the account can be let in and then not out.
    """
    for other in sorted({*book.positions_by_strategy, *book.open_orders_by_strategy}):
        if other not in deployed and (odd := held(book, other)):
            raise Halt(
                f"the book holds {odd[0]}, or an open order in it, under {other!r}, which the "
                "deployment does not run"
            )


def _checked_legs(
    strategy: StrategyConfig, forecast: Forecast, book: Book
) -> dict[InstrumentId, Decimal]:
    """The forecast's legs in instrument order, checked against its strategy and the book."""
    name = strategy.name
    if forecast.strategy != name:
        raise Halt(f"{name}'s forecast is of strategy {forecast.strategy!r}")
    legs = forecast.basket.legs
    if odd := sorted(set(legs) ^ set(strategy.instruments)):
        raise Halt(f"{name}'s basket and the instruments it trades do not both name {odd[0]}")
    if unnamed := sorted(set(held(book, name)) - set(legs)):
        raise Halt(
            f"{name} holds {unnamed[0]}, or an open order in it, and its forecast does not name it"
        )
    checked = {i: finite(f"{name}'s leg in {i}", legs[i]) for i in sorted(legs)}
    if not any(checked.values()):
        raise Halt(f"every leg of {name}'s basket is zero")
    return checked


def _zero(n: int) -> Decimal:
    return Decimal(0).scaleb(-n)
