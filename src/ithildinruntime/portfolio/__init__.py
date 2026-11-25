"""Portfolio construction: a forecast, the limits and the prices turned into a target portfolio.

#9 builds it. docs/architecture/05-building-blocks.md's `#### How a target is
sized` says how, and its reasoning lives in docs/design.md. It imports only
`core`.

- `sizing` reads a strategy's `[portfolio]` section and sizes its target, as a
  pure function.
- `stage` is the portfolio stage, which stores one target and one record of how
  it was sized per strategy and decision.
"""

from .sizing import SECTION, gross, size
from .stage import PortfolioStage

__all__ = ["SECTION", "PortfolioStage", "gross", "size"]
