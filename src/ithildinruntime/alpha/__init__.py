"""The alpha model: what one strategy expects, from the decision's snapshot.

#6 builds it. docs/architecture/05-building-blocks.md places it, and its
reasoning lives in docs/design.md. It imports only `core`.

- `bollinger` holds Chan's Bollinger band on the price spread between two
  instruments.
- `models` maps the name a strategy's `[alpha]` section writes to its model.
- `stage` is the alpha stage, which stores one strategy's forecast and its
  signals per decision.
"""

from .bollinger import SECTION, BollingerSpread
from .models import MODELS, build_model
from .stage import AlphaStage

__all__ = ["MODELS", "SECTION", "AlphaStage", "BollingerSpread", "build_model"]
