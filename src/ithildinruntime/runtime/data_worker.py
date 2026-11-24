"""The data worker's entry point: its row in the stage table, its bucket and its handler.

Each worker's entry point adds its own stage's row to the table, as
`runtime.loop.table` says. Data's stage is capturing and reads no other
stage's result.

The entry point builds the read-only bucket and hands it to `data`, which
imports no adapter, so every mode runs one `LakeSource` and differs only in
the bucket and the clock.

1. A backtest reads a folder that holds marketlake's lake, with no
   credential.
2. Shadow, paper and live read marketlake's bucket in S3 through the data
   worker's read-only role. Each environment's bucket name and the mirror's
   directory are configuration, as docs/design.md's `## Configuration` says.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ithildinruntime.adapters import FolderBucket, S3Bucket
from ithildinruntime.core import Bucket, Clock, Mode, StageKind, Storage, Timer
from ithildinruntime.data import STAGE, DataSettings, DataStage, LakeSource, Mirror

from .loop import Stage, Table

__all__ = ["DATA_ROW", "data_bucket", "data_handler"]

DATA_ROW = Stage(STAGE, StageKind.CAPTURING)


def data_bucket(
    mode: Mode,
    *,
    folder: str | Path | None = None,
    client: Any = None,
    bucket: str | None = None,
) -> Bucket:
    """The bucket data reads in `mode`: a folder in a backtest, marketlake's S3 bucket otherwise."""
    mode = Mode(mode)
    if mode is Mode.BACKTEST:
        if folder is None or client is not None or bucket is not None:
            raise ValueError("a backtest reads marketlake's lake from a folder, and only that")
        return FolderBucket(folder)
    if folder is not None or client is None or not bucket:
        raise ValueError(f"{mode} reads marketlake's bucket in S3, through a client and its name")
    return S3Bucket(client, bucket)


def data_handler(
    *,
    bucket: Bucket,
    mirror: str | Path,
    settings: DataSettings,
    storage: Storage,
    clock: Clock,
    timer: Timer,
    deployment: str,
    run: str,
    release: str,
    table: Table,
) -> DataStage:
    """Data's handler for the receive loop, reading `bucket` through a mirror in `mirror`.

    Data's stage is shared by every strategy in `table`, so each blob it writes
    names every strategy's configuration hash, read from `table` as the receive
    loop checks it.
    """
    source = LakeSource(Mirror(bucket, mirror), settings, clock=clock, timer=timer)
    return DataStage(
        source,
        storage,
        clock,
        deployment=deployment,
        run=run,
        release=release,
        config_hashes=table.hashes_for(STAGE),
    )
