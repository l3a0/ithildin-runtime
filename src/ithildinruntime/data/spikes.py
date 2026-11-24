"""The spike flag on a daily bar: a record beside a price the data module judges wrong.

This doc's rules are a proposal of #5, which the charter's data engineer owns.
The flag never replaces the price, which stays as it arrived. A flag from the
first rule halts the decision, because no real trade produces one, and
`source.LakeSource.read` adds that halt, and `source.judge` adds it to a
redelivered decision's first capture. A flag from the second rule halts
nothing: monitoring alerts the owner, which #11 builds, and the decision trades
on the price as it arrived. A backtest meets both as a live decision does.

1. **A bar whose prices contradict each other is flagged.** That is a high
   below its open, close or low, or any price at or below zero. A low above
   the high is flagged as the high below it. A low above the open or the
   close alone is not flagged by this rule.
2. **A close-to-close move past a written multiple of the lookback's standard
   deviation of daily returns is flagged.** Each return is judged against the
   sample standard deviation of the lookback's other returns, so a spike does
   not widen the yardstick it is measured with. A lookback with fewer than
   three returns judges none. Where the other returns are all equal, any move
   apart from them is flagged, because no multiple of a zero spread bounds it.
   The charter discounts a spike that traded on
   little volume, and no volume threshold is written yet, so nothing is
   discounted. Each flag on a bar that carries no volume says so.

The arithmetic is decimal at 28 digits, so two runs flag the same bars.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Context, Decimal

from ithildinruntime.core import Bar, SpikeFlag

__all__ = ["CONTRADICT", "MOVE", "contradictions", "largest_multiple", "spike_flags"]

# Both rule names are recorded in every stored flag, so the record keeps the
# name it was written under.
CONTRADICT = "prices contradict each other"
MOVE = "close-to-close move past the written multiple"
_CONTEXT = Context(prec=28)
_NO_VOLUME = "the bar carries no volume, so the flag weighs none"
_VOLUME_NOT_WEIGHED = "no volume threshold is written, so the flag weighs no volume"


def _note(bar: Bar, text: str) -> str:
    weighed = _NO_VOLUME if bar.volume is None else _VOLUME_NOT_WEIGHED
    return f"{text}. {weighed}" if text else weighed


def contradictions(bar: Bar) -> list[SpikeFlag]:
    """The first rule's flags on one bar, which no real trade produces."""
    found: list[SpikeFlag] = []
    prices = {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close}
    for name, price in prices.items():
        if price <= 0:
            flag = SpikeFlag(bar.session, name, price, Decimal(0), CONTRADICT, _note(bar, ""))
            found.append(flag)
    others = max(bar.open, bar.close, bar.low)
    if bar.high < others:
        found.append(SpikeFlag(bar.session, "high", bar.high, others, CONTRADICT, _note(bar, "")))
    return found


def _returns(bars: Sequence[Bar]) -> list[Decimal | None]:
    """Each bar's return on the bar before it, `None` where a close is not positive."""
    out: list[Decimal | None] = []
    for before, after in zip(bars, bars[1:], strict=False):
        if before.close <= 0 or after.close <= 0:
            out.append(None)
        else:
            out.append(_CONTEXT.subtract(_CONTEXT.divide(after.close, before.close), 1))
    return out


def _std(values: Sequence[Decimal]) -> Decimal | None:
    n = len(values)
    if n < 2:
        return None
    mean = _CONTEXT.divide(sum(values, Decimal(0)), n)
    square = sum((_CONTEXT.power(v - mean, 2) for v in values), Decimal(0))
    return _CONTEXT.sqrt(_CONTEXT.divide(square, n - 1))


def _multiples(bars: Sequence[Bar]) -> list[tuple[int, Decimal]]:
    """Each judged return's size in the standard deviations of the lookback's other returns.

    The first item is the index of the bar whose close moved.
    """
    returns = _returns(bars)
    out: list[tuple[int, Decimal]] = []
    for i, r in enumerate(returns):
        if r is None:
            continue
        others = [x for j, x in enumerate(returns) if j != i and x is not None]
        if len(others) < 2:
            continue
        std = _std(others)
        if std is None:
            continue
        if std == 0:
            if r != others[0]:
                out.append((i + 1, Decimal("Infinity")))
            continue
        out.append((i + 1, _CONTEXT.divide(abs(r), std)))
    return out


def spike_flags(bars: Sequence[Bar], multiple: Decimal) -> tuple[SpikeFlag, ...]:
    """Every flag on one instrument's lookback, oldest bar first."""
    if not isinstance(multiple, Decimal) or multiple <= 0:
        raise ValueError("the spike multiple is a positive decimal")
    found: list[SpikeFlag] = []
    for bar in bars:
        found.extend(contradictions(bar))
    for i, size in _multiples(bars):
        if size > multiple:
            bar = bars[i]
            text = f"a move of {size:.2f} standard deviations, past the written {multiple}"
            before = bars[i - 1].close
            found.append(SpikeFlag(bar.session, "close", bar.close, before, MOVE, _note(bar, text)))
    return tuple(sorted(found, key=lambda f: (f.session, f.rule, f.field)))


def largest_multiple(bars: Sequence[Bar], lookback: int) -> Decimal:
    """The largest move any lookback of a series reaches, as `spike_flags` measures it.

    This is the measurement the spike multiple is stated beside. Each session's
    lookback holds up to `lookback` bars ending at it, as a decision on that
    session reads, and every return in it is judged as `spike_flags` judges it.
    """
    best = Decimal(0)
    for end in range(1, len(bars) + 1):
        for _, size in _multiples(bars[max(0, end - lookback) : end]):
            best = max(best, size)
    return best
