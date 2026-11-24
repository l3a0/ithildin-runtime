"""The read every stage makes of data's result and the snapshot it lists.

Alpha, cost, risk and portfolio construction each read data's snapshot, and no
worker module imports another, so the steps live here, once. docs/design.md's
`### Rule 7: a halt is passed on` says what a stage does with a halt it reads.
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from .codec import encode
from .halt import Halt
from .keys import output_key, result_key
from .names import SNAPSHOT_OUTPUT
from .records import BlobRef, HaltRecord, Snapshot
from .storage import Storage, sha256_of

__all__ = ["SnapshotInput", "read_snapshot"]


class SnapshotInput(NamedTuple):
    """Data's result, the snapshot file it lists and the snapshot that file holds.

    A stage's header names the first two as its inputs.
    """

    result: BlobRef
    file: BlobRef
    snapshot: Snapshot


def read_snapshot(
    storage: Storage,
    *,
    deployment: str,
    run: str,
    decision_at: datetime,
    stage: str,
    data_stage: str,
) -> SnapshotInput | HaltRecord:
    """The snapshot `data_stage` stored for a decision, or the halt to pass on.

    A result that holds a halt gives the record the stage `stage` passes on,
    whose `cause` is that result. It raises `Halt` where data's result is
    missing, lists the snapshot that counts other than once, or lists one that
    holds other than one snapshot, and where storage refuses either read.
    """
    key = result_key(deployment, run, decision_at, data_stage)
    data = storage.read_result(key, decision_at)
    if data is None:
        raise Halt(f"{key} is not stored, and stage {stage} reads it")
    # The codec reads only canonical bytes, so this is the stored blob's SHA-256.
    found = BlobRef(key, sha256_of(encode(data)))
    if data.halt is not None:
        return storage.halt_record(Halt(f"{key} holds a halt: {data.halt.reason}"), cause=found)
    # Data lists the capture that counts under its own key, and any later
    # capture that differed under an attempt key.
    counts = output_key(deployment, run, decision_at, data_stage, SNAPSHOT_OUTPUT)
    listed = [ref for ref in data.outputs if ref.key == counts]
    if len(listed) != 1:
        raise Halt(f"{key} lists {counts} {len(listed)} times, not once")
    file = storage.read_output(key, data, listed[0], decision_at, Snapshot)
    if len(file.entries) != 1:
        raise Halt(f"{counts} holds {len(file.entries)} snapshots, not one")
    return SnapshotInput(found, listed[0], file.entries[0])
