"""The portfolio worker's entry point: its row in the stage table and its handler.

One portfolio stage serves every strategy of the deployment. It reads data's,
execution's recover, risk's and each strategy's alpha and cost results. It
builds no adapter of its own, because the portfolio stage reads nothing but
stored blobs, so the same code runs in every mode. It imports no `data`, whose
reader would load marketlake and pandas into this process, and no other model.
"""

from __future__ import annotations

from collections.abc import Iterable

from ithildinruntime.core import (
    ALPHA_COMPONENT,
    COST_COMPONENT,
    DATA_STAGE,
    PORTFOLIO_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    StageKind,
    Storage,
    StrategyConfig,
)
from ithildinruntime.portfolio import PortfolioStage

from .loop import Stage, Table

__all__ = ["portfolio_handler", "portfolio_row"]


def portfolio_row(strategies: Iterable[StrategyConfig]) -> Stage:
    """The one row, which reads each strategy's alpha and cost stages by name."""
    models = [
        f"{component}/{name}"
        for name in sorted(strategy.name for strategy in strategies)
        for component in (ALPHA_COMPONENT, COST_COMPONENT)
    ]
    return Stage(
        PORTFOLIO_STAGE,
        StageKind.DETERMINISTIC,
        inputs=(DATA_STAGE, RECOVER_STAGE, RISK_STAGE, *models),
    )


def portfolio_handler(
    *, storage: Storage, deployment: str, run: str, release: str, table: Table
) -> PortfolioStage:
    """The portfolio worker's handler for the receive loop.

    It reads the deployment's strategies, its own kind, its inputs and every
    stage's configuration hashes from `table`.
    """
    stage = table.stage(PORTFOLIO_STAGE)
    return PortfolioStage(
        table.strategies,
        storage,
        stage=stage.name,
        kind=stage.kind,
        inputs={name: table.hashes_for(name) for name in stage.inputs},
        deployment=deployment,
        run=run,
        release=release,
        config_hashes=table.hashes_for(stage.name),
    )
