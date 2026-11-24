"""The cost worker's entry point: its rows in the stage table and its handlers.

Each strategy has a cost stage of its own, which reads data's result, so the
one worker runs a stage per strategy the table declares. It builds no adapter
of its own, because the cost stage reads nothing but stored blobs, so the same
code runs in every mode.
"""

from __future__ import annotations

from collections.abc import Iterable

from ithildinruntime.core import StageKind, Storage, StrategyConfig
from ithildinruntime.cost import COMPONENT, CostStage, LinearCostModel
from ithildinruntime.data import SNAPSHOT, STAGE

from .loop import Stage, Table

__all__ = ["cost_handlers", "cost_rows"]


def cost_rows(strategies: Iterable[StrategyConfig]) -> tuple[Stage, ...]:
    """One deterministic row per strategy, each reading data's stage."""
    return tuple(
        Stage(
            f"{COMPONENT}/{strategy.name}",
            StageKind.DETERMINISTIC,
            inputs=(STAGE,),
            strategy=strategy.name,
        )
        for strategy in strategies
    )


def cost_handlers(
    *, storage: Storage, deployment: str, run: str, release: str, table: Table
) -> dict[str, CostStage]:
    """The cost worker's handlers for the receive loop, one per strategy's cost stage.

    Each reads its strategy's terms, instruments and configuration hash from
    `table`, as the receive loop checks them.
    """
    return {
        stage.name: CostStage(
            LinearCostModel(table.strategy(stage.strategy)),
            storage,
            stage=stage.name,
            data_stage=STAGE,
            snapshot=SNAPSHOT,
            deployment=deployment,
            run=run,
            release=release,
            config_hashes=table.hashes_for(stage.name),
        )
        for stage in table.stages_of(COMPONENT)
    }
