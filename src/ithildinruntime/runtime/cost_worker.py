"""The cost worker's entry point: its rows in the stage table and its handlers.

Each strategy has a cost stage of its own, which reads data's result, so the
one worker runs a stage per strategy the table declares. It builds no adapter
of its own, because the cost stage reads nothing but stored blobs, so the same
code runs in every mode. It imports no `data`, whose reader would load
marketlake and pandas into this process.
"""

from __future__ import annotations

from collections.abc import Iterable

from ithildinruntime.core import COST_COMPONENT, Storage, StrategyConfig
from ithildinruntime.cost import CostStage, LinearCostModel

from .loop import Stage, Table
from .rows import strategy_rows

__all__ = ["cost_handlers", "cost_rows"]


def cost_rows(strategies: Iterable[StrategyConfig]) -> tuple[Stage, ...]:
    """One deterministic row per strategy, each reading data's stage."""
    return strategy_rows(COST_COMPONENT, strategies)


def cost_handlers(
    *, storage: Storage, deployment: str, run: str, release: str, table: Table
) -> dict[str, CostStage]:
    """The cost worker's handlers for the receive loop, one per strategy's cost stage.

    Each reads its strategy's terms, instruments and configuration hash, and
    its own kind and input, from `table`.
    """
    made = {}
    for stage in table.stages_of(COST_COMPONENT):
        (data_stage,) = stage.inputs
        made[stage.name] = CostStage(
            LinearCostModel(table.strategy(stage.strategy)),
            storage,
            stage=stage.name,
            kind=stage.kind,
            data_stage=data_stage,
            deployment=deployment,
            run=run,
            release=release,
            config_hashes=table.hashes_for(stage.name),
        )
    return made
