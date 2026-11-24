"""Chan's return on a spread, as the replication's pins were computed.

It is profit over the gross dollars held, and it is not the platform's one
function for either figure, which #57 item 2 holds. Acceptance test 1 and the
alpha model's own pins both call it, so they compute Chan's figures one way.

- Each row's return is the profit on the shares held after the row before,
  over the gross dollars those shares came to at that row's close. A row with
  nothing held before it returns 0, and so does the first.
- The returns are simple, daily and with no rate subtracted.
- The APR compounds them and scales to 252 periods a year.
- The Sharpe ratio divides by the standard deviation with the n − 1 divisor,
  and scales by the square root of 252.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

PERIODS = 252


def chan_figures(
    rows: Sequence[tuple[date, Mapping[str, Decimal]]],
    closes: Mapping[str, Mapping[date, Decimal]],
) -> tuple[float, float]:
    """The APR and the Sharpe ratio of the shares held after each row's close.

    A row is a session and the shares of each instrument held after it, which
    may be fractional and may name no instrument at all. Only the ratio of the
    legs matters, because each return is over the gross dollars held.
    """
    returns = [0.0]
    for (before, held), (day, _) in zip(rows, rows[1:], strict=False):
        gross = sum((abs(q) * closes[i][before] for i, q in held.items()), Decimal(0))
        profit = sum(
            (q * (closes[i][day] - closes[i][before]) for i, q in held.items()), Decimal(0)
        )
        returns.append(float(profit) / float(gross) if gross else 0.0)
    n = len(returns)
    apr = math.exp(math.fsum(map(math.log1p, returns)) * PERIODS / n) - 1
    middle = math.fsum(returns) / n
    deviation = math.sqrt(math.fsum((r - middle) ** 2 for r in returns) / (n - 1))
    return apr, math.sqrt(PERIODS) * middle / deviation
