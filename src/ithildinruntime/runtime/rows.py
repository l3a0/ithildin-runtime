"""The stage-table rows of a component that runs one stage per strategy."""

from __future__ import annotations

from collections.abc import Iterable

from ithildinruntime.core import DATA_STAGE, StageKind, StrategyConfig

from .loop import Stage

__all__ = ["strategy_rows"]


def strategy_rows(component: str, strategies: Iterable[StrategyConfig]) -> tuple[Stage, ...]:
    """One deterministic row per strategy, named for the component, each reading data's stage."""
    return tuple(
        Stage(
            f"{component}/{strategy.name}",
            StageKind.DETERMINISTIC,
            inputs=(DATA_STAGE,),
            strategy=strategy.name,
        )
        for strategy in strategies
    )
