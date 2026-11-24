"""The transaction cost model: what any trade would cost, from a strategy's written terms.

#8 builds it. docs/architecture/05-building-blocks.md places it, under
`#### The cost model writes a schedule rather than answering calls`, and its
reasoning lives in docs/design.md. It imports only `core`.

- `model` turns a strategy's cost section into a `CostSchedule`.
- `stage` is the cost stage, which stores one schedule per strategy and
  decision.

It says what a trade would cost and minimizes nothing. No simulated fill
calibrates a term.
"""

from .model import SECTION, LinearCostModel
from .stage import COMPONENT, SCHEDULE, CostStage

__all__ = ["COMPONENT", "SCHEDULE", "SECTION", "CostStage", "LinearCostModel"]
