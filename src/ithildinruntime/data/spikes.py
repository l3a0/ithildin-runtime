"""The spike flag on a daily bar or a quote: a record beside a price the data module judges wrong.

The owner's rulings on #5 settle what each rule's flag does. The rest of these
rules is a proposal of #5, which the charter's data engineer owns.
The flag never replaces the price, which stays as it arrived. A flag from the
first rule halts the decision, because no real trade produces one, and
`source.LakeSource.read` adds that halt, and `source.judge` adds it to a
redelivered decision's first capture. A flag from the second or third rule
halts nothing: monitoring alerts the owner, which #11 builds, and the decision
trades on the price as it arrived. A backtest meets all three as a live
decision does.

1. **A bar whose prices contradict each other is flagged.** That is a high
   below its open, close or low, or any price at or below zero. A low above
   the high is flagged as the high below it. A low above the
   open or the close alone is not flagged by this rule.
2. **A close-to-close move past a written multiple of the lookback's standard
   deviation of daily returns is flagged.** Each return is judged against the
   sample standard deviation of the lookback's other returns, so a spike does
   not widen the yardstick it is measured with. A lookback with fewer than
   three returns judges none. Where the other returns are all equal, any move
   apart from them is flagged, because no multiple of a zero spread bounds it.
   The charter discounts a spike that traded on
   little volume, and no volume threshold is written yet, so nothing is
   discounted. Each flag on a bar that carries no volume says so.
3. **A close below its bar's low is flagged.** A close a few percent too low
   moves too little for the second rule to flag, so this rule is what alerts
   on it, whatever the size of the move.

Two of the rules read a quote, with no new name for either.

1. **A quote whose prices contradict each other is flagged.** That is a bid or
   an ask at or below zero, or an ask below the bid. `core.quote_faults` halts
   the decision on the same prices.
2. **A midpoint that moved past the written multiple is flagged.** Its return
   on the newest bar's close is judged against the sample standard deviation of
   the lookback's returns, and the flag alerts and halts nothing. Fewer than two
   returns judge none.

The arithmetic is decimal at 28 digits, so two runs flag the same prices.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Context, Decimal

from ithildinruntime.core import (
    Bar,
    CodecError,
    Halt,
    Quote,
    SpikeFlag,
    from_received,
    midpoint,
)

__all__ = [
    "BELOW_LOW",
    "CONTRADICT",
    "MOVE",
    "below_low",
    "contradictions",
    "largest_multiple",
    "quote_contradictions",
    "quote_flags",
    "spike_flags",
]

# Each rule name is recorded in every stored flag, so the record keeps the
# name it was written under.
CONTRADICT = "prices contradict each other"
MOVE = "close-to-close move past the written multiple"
BELOW_LOW = "close below the bar's low"
_CONTEXT = Context(prec=28)
_NO_VOLUME = "the bar carries no volume, so the flag weighs none"
_NO_QUOTE_VOLUME = "a quote carries no volume the rule weighs, so the flag weighs none"
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


def below_low(bar: Bar) -> list[SpikeFlag]:
    """The third rule's flag on one bar, which alerts and halts nothing."""
    if bar.close >= bar.low:
        return []
    return [SpikeFlag(bar.session, "close", bar.close, bar.low, BELOW_LOW, _note(bar, ""))]


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
        found.extend(below_low(bar))
    for i, size in _multiples(bars):
        if size > multiple:
            bar = bars[i]
            text = f"a move of {size:.2f} standard deviations, past the written {multiple}"
            before = bars[i - 1].close
            found.append(SpikeFlag(bar.session, "close", bar.close, before, MOVE, _note(bar, text)))
    return tuple(sorted(found, key=lambda f: (f.session, f.rule, f.field)))


def quote_contradictions(quote: Quote, session: date) -> list[SpikeFlag]:
    """The first rule's flags on one quote, which no real market shows."""
    found = []
    sides = {"bid": quote.bid, "ask": quote.ask}
    for name, price in sides.items():
        if price is not None and price <= 0:
            found.append(SpikeFlag(session, name, price, Decimal(0), CONTRADICT, _NO_QUOTE_VOLUME))
    if quote.bid is not None and quote.ask is not None and quote.ask < quote.bid:
        found.append(SpikeFlag(session, "ask", quote.ask, quote.bid, CONTRADICT, _NO_QUOTE_VOLUME))
    return found


def quote_flags(
    quote: Quote, session: date, bars: Sequence[Bar], multiple: Decimal
) -> tuple[SpikeFlag, ...]:
    """Every flag on one quote, against the bars of its instrument's lookback."""
    return (*quote_contradictions(quote, session), *_midpoint_move(quote, session, bars, multiple))


def _midpoint_move(
    quote: Quote, session: date, bars: Sequence[Bar], multiple: Decimal
) -> list[SpikeFlag]:
    try:
        price = from_received(midpoint(quote))
    except (Halt, CodecError):
        # No midpoint, or one no record can hold.
        return []
    if not bars or price <= 0 or bars[-1].close <= 0:
        return []
    before = bars[-1].close
    returns = [r for r in _returns(bars) if r is not None]
    std = _std(returns)
    if std is None:
        return []
    move = _CONTEXT.subtract(_CONTEXT.divide(price, before), 1)
    if std == 0:
        if move == returns[0]:
            return []
        text = "a move of the midpoint where every return of the lookback is the same"
    else:
        size = _CONTEXT.divide(abs(move), std)
        if size <= multiple:
            return []
        text = (
            f"a move of the midpoint by {size:.2f} standard deviations, past the written {multiple}"
        )
    return [SpikeFlag(session, "midpoint", price, before, MOVE, f"{text}. {_NO_QUOTE_VOLUME}")]


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
