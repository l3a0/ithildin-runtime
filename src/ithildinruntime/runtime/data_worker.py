"""The data worker's entry point: its row in the stage table, its buckets and its handler.

Each worker's entry point adds its own stage's row to the table, as
`runtime.loop.table` says. Data's stage is capturing and reads no other
stage's result.

The entry point builds the two read-only buckets and hands them to `data`, which
imports no adapter, so every mode runs one `LakeSource` and differs only in
the buckets and the clock. One holds marketlake's lake and the other, beside it,
the capture segments of days not yet sealed. Both come from one configured
name, with `-live` after it for the second.

1. A backtest reads a folder that holds marketlake's lake and the folder beside
   it, with no credential. A folder that is not there lists no segment.
2. Shadow, paper and live read two prefixes of marketlake's bucket in S3 through
   the data worker's read-only role. Each environment's bucket name, its prefix
   and the mirror's directory are configuration, as docs/design.md's `##
   Configuration` says.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NamedTuple

from ithildinruntime.adapters import FolderBucket, S3Bucket
from ithildinruntime.core import Bucket, Clock, Halt, Mode, StageKind, Storage, Timer
from ithildinruntime.data import STAGE, DataStage, LakeSource, Mirror, data_settings

from .loop import Stage, Table

__all__ = ["DATA_ROW", "DataBuckets", "data_buckets", "data_handler"]

DATA_ROW = Stage(STAGE, StageKind.CAPTURING)


_LIVE = "-live"


class DataBuckets(NamedTuple):
    """marketlake's lake, and the capture segments of the days it has not sealed."""

    lake: Bucket
    live: Bucket


def data_buckets(
    mode: Mode,
    *,
    folder: str | Path | None = None,
    client: Any = None,
    bucket: str | None = None,
    prefix: str | None = None,
) -> DataBuckets:
    """The buckets data reads in `mode`: folders in a backtest, marketlake's S3 bucket otherwise."""
    mode = Mode(mode)
    if mode is Mode.BACKTEST:
        if folder is None or client is not None or bucket is not None or prefix is not None:
            raise ValueError("a backtest reads marketlake's lake from a folder, and only that")
        lake = Path(folder)
        if lake.name in ("", ".."):
            raise ValueError(f"{folder} names no folder, so nothing names the one beside it")
        return DataBuckets(FolderBucket(lake), FolderBucket(lake.with_name(lake.name + _LIVE)))
    if folder is not None or client is None or not bucket or not prefix:
        raise ValueError(
            f"{mode} reads marketlake's bucket in S3, through a client, its name and its prefix"
        )
    return DataBuckets(S3Bucket(client, bucket, prefix), S3Bucket(client, bucket, prefix + _LIVE))


def data_handler(
    *,
    bucket: Bucket,
    live: Bucket,
    mirror: str | Path,
    storage: Storage,
    clock: Clock,
    timer: Timer,
    measured: bool,
    deployment: str,
    run: str,
    release: str,
    table: Table,
) -> DataStage:
    """Data's handler for the receive loop, reading `bucket` through a mirror in `mirror`.

    `live` holds the capture segments of the days marketlake has not sealed.

    Data's stage is shared by every strategy in `table`, so each blob it writes
    names every strategy's configuration hash, read from `table` as the receive
    loop checks it. The settings are built from the same strategies, so a change
    to one moves the hash of the file that writes it. Files that build no
    settings halt each decision with the reason stored, and never the worker's
    start. One `LakeSource` serves every decision, because it keeps each
    session's answer for the run. `measured` is true where the clock is the
    machine's, so a history clock wired into a trading run halts.
    """
    source: LakeSource | Halt
    try:
        settings = data_settings(table.strategies)
    except Halt as halt:
        source = halt
    else:
        source = LakeSource(
            Mirror(bucket, mirror),
            settings,
            live=live,
            clock=clock,
            timer=timer,
            measured=measured,
        )
    return DataStage(
        source,
        storage,
        clock,
        deployment=deployment,
        run=run,
        release=release,
        config_hashes=table.hashes_for(STAGE),
    )
