"""Decimal arithmetic that is exact or stops.

Python's default decimal context rounds at 28 digits without a word, and a sum
rounded that way depends on the order of its terms. A limit compared against
such a sum could pass on one order of a book's positions and refuse on another.
So the sums and products a limit reads run in one context here, wide enough for
four factors of the codec's digits, and it traps any result it would have to
round. Only a figure that is recorded, or the one division, is rounded, and
each says which way.

A value that is not a finite decimal, and a trapped error, raise `Halt` naming
the value (hard limit 3).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import (
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DecimalException,
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
)

from .codec import MAX_DIGITS, CodecError, from_received
from .halt import Halt

__all__ = ["exact_product", "exact_total", "finite", "quotient", "rounded", "worth"]

# A product here takes at most four factors. A worst loss is a size, a price and the
# stress move. Two limits on a target are compared across their divisors, such as a
# share limit, one plus the stress move, a leg and its price.
_DIGITS = 4 * MAX_DIGITS
_TRAPS = [Inexact, InvalidOperation, DivisionByZero, Overflow]


def _exact() -> Context:
    return Context(prec=_DIGITS, traps=list(_TRAPS))


def _rounding(rounding: str) -> Context:
    return Context(
        prec=_DIGITS,
        rounding=rounding,
        traps=[InvalidOperation, DivisionByZero, Overflow],
    )


def finite(what: str, value: object) -> Decimal:
    """`value`, which halts naming `what` unless it is a finite decimal."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise Halt(f"{what} is {value!r}, not a finite decimal")
    return value


def exact_total(what: str, values: Iterable[Decimal]) -> Decimal:
    """The sum of `values`, or a halt naming `what` where it is not exact."""
    context = _exact()
    result = Decimal(0)
    try:
        for value in values:
            result = context.add(result, finite(what, value))
    except DecimalException as error:
        raise Halt(f"{what} has no exact sum: {type(error).__name__}") from error
    return result


def exact_product(what: str, *values: Decimal) -> Decimal:
    """The product of `values`, or a halt naming `what` where it is not exact."""
    context = _exact()
    result = Decimal(1)
    try:
        for value in values:
            result = context.multiply(result, finite(what, value))
    except DecimalException as error:
        raise Halt(f"{what} has no exact product: {type(error).__name__}") from error
    return result


def worth[K](
    what: str, cash: Decimal, shares: Mapping[K, Decimal], prices: Mapping[K, Decimal]
) -> Decimal:
    """Cash plus each holding at its price, the one sum V and the day's loss are read from."""
    held = (exact_product(f"{what}, of {i}", qty, prices[i]) for i, qty in shares.items())
    return exact_total(what, (cash, *held))


def quotient(
    what: str, dividend: Decimal, divisor: Decimal, n: int, rounding: str = ROUND_HALF_EVEN
) -> Decimal:
    """`dividend` over `divisor`, rounded at `n` places."""
    context = _rounding(rounding)
    finite(what, dividend)
    finite(what, divisor)
    try:
        return context.quantize(context.divide(dividend, divisor), Decimal(1).scaleb(-n))
    except DecimalException as error:
        raise Halt(f"{what} cannot be divided: {type(error).__name__}") from error


def rounded(what: str, value: Decimal, n: int, rounding: str = ROUND_HALF_EVEN) -> Decimal:
    """`value` at `n` places for a record, or a halt where the codec could not write it."""
    try:
        result = _rounding(rounding).quantize(finite(what, value), Decimal(1).scaleb(-n))
        return from_received(result)
    except (DecimalException, CodecError) as error:
        raise Halt(f"{what} cannot be recorded at {n} places: {error}") from error
