"""The order manager: from each strategy's target and the book to the orders that close the gap.

`plan_orders` is a pure function, so the send stage, a test and a reader of the
journal's `decision` entry all get the same plan from the same inputs. It
refuses, by raising `Halt`, every input it cannot plan from rather than reading
it as a target of zero (hard limit 3).

1. Each strategy of the deployment has exactly one target, of this decision, and
   the target names every instrument its strategy's book holds or has an open
   order in.
2. One order per strategy and instrument is planned, for the target less the
   position less the strategy's own open orders there, at their unfilled
   remainder. It is rounded toward zero to the account's share step, and a
   quantity of zero plans no order. What the rounding leaves is a `Remainder`.
3. An open order in an instrument whose planned quantity is not zero halts,
   because cancelling and replacing an order is #149's. In slice 1 every order
   ends at its session's close, so none rests across a decision.
4. The orders are sorted by strategy and then instrument and numbered from 01.
   A plan of more than 99 orders halts, since an order id holds two digits.

`order_type` reads the type a strategy's orders take from its `[execution]`
section, whose one key is `order`.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import NamedTuple

from ithildinruntime.core import (
    Book,
    Halt,
    InstrumentId,
    OrderIntent,
    OrderType,
    Remainder,
    StrategyConfig,
    TargetPortfolio,
    TimeInForce,
    order_id,
)

__all__ = ["MOST_ORDERS", "Plan", "order_type", "plan_orders"]

# The most orders one decision may plan, since an order id numbers its place in two digits.
MOST_ORDERS = 99
# The order types a strategy's `[execution] order` may name, and what each sends.
_TYPES = {"market_on_close": (OrderType.MARKET_ON_CLOSE, TimeInForce.CLS)}
_ZERO = Decimal(0)


class Plan(NamedTuple):
    """The orders one decision plans, in the plan's order, and what no order trades."""

    orders: tuple[OrderIntent, ...]
    remainders: tuple[Remainder, ...]


def order_type(strategy: StrategyConfig) -> tuple[OrderType, TimeInForce]:
    """The type and time in force of a strategy's orders, as its `[execution]` section names them.

    A missing or empty section, a key other than `order`, and a type other than
    `market_on_close` halt naming the strategy. The day's market order is
    #155's.
    """
    section = strategy.section("execution")
    where = f"strategy {strategy.name}'s configuration {strategy.source}"
    if odd := sorted(key for key in section if key != "order"):
        raise Halt(f"{where} holds {', '.join(odd)} in [execution], whose one key is order")
    named = section.get("order")
    if named is None:
        raise Halt(f"{where} names no order in [execution]")
    found = _TYPES.get(named) if isinstance(named, str) else None
    if found is None:
        raise Halt(
            f"{where} names the order {named!r} in [execution], and only market_on_close is "
            "sent until #155 adds the day's market order"
        )
    return found


def plan_orders(
    targets: Sequence[TargetPortfolio],
    book: Book,
    *,
    types: Mapping[str, tuple[OrderType, TimeInForce]],
    share_step: Decimal,
    decision_at: datetime,
    off: Collection[str] = (),
) -> Plan:
    """The orders that move each strategy's book to its target, and what no order can move.

    `types` maps each strategy of the deployment to its order type, and is the
    list of strategies a target file must cover. A strategy in `off` plans no
    order, though its target is checked like every other.
    """
    if share_step <= _ZERO:
        raise Halt(f"the account's share step is {share_step}, and a step is above zero")
    by_strategy = _targets(targets, types, decision_at)
    planned: list[tuple[str, InstrumentId, Decimal]] = []
    remainders: list[Remainder] = []
    for strategy in sorted(by_strategy):
        target = by_strategy[strategy]
        held = book.positions_by_strategy.get(strategy, {})
        open_orders = book.open_orders_by_strategy.get(strategy, ())
        opened: dict[InstrumentId, Decimal] = {}
        for order in open_orders:
            opened[order.instrument] = opened.get(order.instrument, _ZERO) + order.qty
        if missing := sorted({*(i for i, q in held.items() if q != _ZERO), *opened} - {*target}):
            raise Halt(
                f"the target of {strategy} leaves out {', '.join(missing)}, which its book holds "
                "or has an open order in, and a leg left out never reads as zero"
            )
        for instrument in sorted(target):
            gap = target[instrument] - held.get(instrument, _ZERO) - opened.get(instrument, _ZERO)
            qty = _toward_zero(gap, share_step)
            if qty != gap:
                remainders.append(Remainder(strategy, instrument, gap - qty))
            if qty == _ZERO:
                continue
            if instrument in opened:
                raise Halt(
                    f"{strategy} has an open order in {instrument} and needs {qty} more, and "
                    "cancelling and replacing an order is #149's"
                )
            if strategy not in off:
                planned.append((strategy, instrument, qty))
    if len(planned) > MOST_ORDERS:
        raise Halt(f"the plan holds {len(planned)} orders, past the {MOST_ORDERS} one decision may")
    orders = tuple(
        OrderIntent(
            client_order_id=order_id(strategy, decision_at, place),
            strategy=strategy,
            instrument=instrument,
            qty=qty,
            type=types[strategy][0],
            time_in_force=types[strategy][1],
        )
        for place, (strategy, instrument, qty) in enumerate(planned, start=1)
    )
    return Plan(orders, tuple(remainders))


def _targets(
    targets: Sequence[TargetPortfolio],
    types: Mapping[str, tuple[OrderType, TimeInForce]],
    decision_at: datetime,
) -> dict[str, Mapping[InstrumentId, Decimal]]:
    """Each strategy's one target, which halts where the file is not one target per strategy."""
    found: dict[str, Mapping[InstrumentId, Decimal]] = {}
    for target in targets:
        if target.decision_at != decision_at:
            raise Halt(
                f"the target of {target.strategy} is of the decision at "
                f"{target.decision_at.isoformat()}, not of {decision_at.isoformat()}"
            )
        if target.strategy not in types:
            raise Halt(f"the targets hold one of {target.strategy}, a strategy of no deployment")
        if target.strategy in found:
            raise Halt(f"the targets hold two of {target.strategy}")
        found[target.strategy] = target.shares
    if missing := sorted(set(types) - set(found)):
        raise Halt(f"the targets hold none of {', '.join(missing)}")
    return found


def _toward_zero(qty: Decimal, step: Decimal) -> Decimal:
    """`qty` rounded toward zero to a whole number of `step`, worked out exactly."""
    steps = int(Fraction(qty) / Fraction(step))
    return step * steps if steps else _ZERO
