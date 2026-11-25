"""The risk worker's entry point: its row in the stage table and its handler.

One risk stage serves every strategy of the deployment, and reads data's and
execution's recover results. It builds no adapter of its own, because the risk
stage reads nothing but stored blobs, so the same code runs in every mode. It
reads nothing under `accounts/`, so its role needs no read of the owner's caps
blob. It imports no `data`, whose reader would load marketlake and pandas into
this process.
"""

from __future__ import annotations

from ithildinruntime.core import DATA_STAGE, RECOVER_STAGE, RISK_STAGE, StageKind, Storage
from ithildinruntime.risk import RiskStage

from .loop import Stage, Table

__all__ = ["RISK_ROW", "risk_handler"]

RISK_ROW = Stage(RISK_STAGE, StageKind.DETERMINISTIC, inputs=(DATA_STAGE, RECOVER_STAGE))


def risk_handler(
    *, storage: Storage, deployment: str, run: str, release: str, table: Table
) -> RiskStage:
    """The risk worker's handler for the receive loop.

    It reads the deployment's strategies and their configuration hashes, and
    its own kind and inputs, from `table`.
    """
    stage = table.stage(RISK_STAGE)
    data_stage, recover_stage = stage.inputs
    return RiskStage(
        table.strategies,
        storage,
        stage=stage.name,
        kind=stage.kind,
        data_stage=data_stage,
        recover_stage=recover_stage,
        deployment=deployment,
        run=run,
        release=release,
        config_hashes=table.hashes_for(stage.name),
    )
