"""Each model's name, as a strategy's `[alpha]` section writes it, and its class.

A new model adds a file and one line here, and changes no shared code.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from ithildinruntime.core import AlphaModel, Halt, StrategyConfig

from .bollinger import SECTION, BollingerSpread

__all__ = ["MODELS", "build_model"]

MODELS: Mapping[str, Callable[[StrategyConfig], AlphaModel]] = MappingProxyType(
    {"bollinger_spread": BollingerSpread}
)


def build_model(strategy: StrategyConfig) -> AlphaModel:
    """The model a strategy's section names, halting on a section that names none."""
    name = strategy.section(SECTION).get("model")
    made = MODELS.get(name) if isinstance(name, str) else None
    if made is None:
        stated = "states no model" if name is None else f"names the model {name!r}"
        raise Halt(
            f"strategy {strategy.name}'s configuration {strategy.source} has an [{SECTION}] "
            f"section that {stated}, and the models are {', '.join(sorted(MODELS))}"
        )
    return made(strategy)
