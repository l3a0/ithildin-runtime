"""What one decision's gate is handed from storage: the snapshot, the limits and V.

Each file is read at the key its stage writes, through the result that lists it
and `core.Storage`'s checks of the decision, the listing and the hashes. A
caller that plans from a `decision` entry compares each file's key and SHA-256
with the ones that entry names, because the entry's own names alone would be
checked against themselves.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from typing import NamedTuple

from ithildinruntime.core import (
    CONSTRAINTS_OUTPUT,
    DATA_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SNAPSHOT_OUTPUT,
    VALUATION_OUTPUT,
    BlobRef,
    Constraints,
    Halt,
    Snapshot,
    Storage,
    Valuation,
    output_key,
    result_key,
)

__all__ = ["Inputs", "read_inputs"]


class Inputs(NamedTuple):
    """`refs` names each file by the name a `decision` entry's `inputs` gives it."""

    snapshot: Snapshot
    constraints: tuple[Constraints, ...]
    valuation: Valuation
    refs: Mapping[str, BlobRef]

    def limits(self, strategy: str) -> Constraints:
        """The one `Constraints` of a strategy, which halts on none and on two."""
        found = [c for c in self.constraints if c.strategy == strategy]
        if len(found) != 1:
            raise Halt(
                f"{self.refs['constraints'].key} holds {len(found)} constraints of {strategy!r}, "
                "not one"
            )
        return found[0]


def read_inputs(
    storage: Storage, deployment: str, run: str, at: datetime, report: Callable[[], None]
) -> Inputs:
    """The three files of one decision, or a halt naming the first that cannot be used."""
    refs: dict[str, BlobRef] = {}

    def entries[T](stage: str, name: str, kind: type[T]) -> tuple[T, ...]:
        key = result_key(deployment, run, at, stage)
        result = storage.read_result(key, at)
        if result is None:
            raise Halt(f"{key} is not stored, and an order is checked against it")
        counts = output_key(deployment, run, at, stage, name)
        listed = [ref for ref in result.outputs if ref.key == counts]
        if result.halt is None and len(listed) != 1:
            raise Halt(f"{key} lists {counts} {len(listed)} times, not once")
        # A result that holds a halt lists nothing to read, which storage halts on by name.
        ref = listed[0] if listed else BlobRef(counts, "")
        found = storage.read_output(key, result, ref, at, kind).entries
        refs[name] = ref
        report()
        return found

    def one[T](stage: str, name: str, kind: type[T]) -> T:
        found = entries(stage, name, kind)
        if len(found) != 1:
            raise Halt(f"{refs[name].key} holds {len(found)} entries, not one")
        return found[0]

    snapshot = one(DATA_STAGE, SNAPSHOT_OUTPUT, Snapshot)
    if snapshot.decision_at != at:
        # Storage checks the decision the file's header names, and the gate goes by
        # the snapshot's own.
        raise Halt(
            f"{refs[SNAPSHOT_OUTPUT].key} holds a snapshot of {snapshot.decision_at.isoformat()}, "
            f"not of {at.isoformat()}"
        )
    constraints = entries(RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints)
    if not constraints:
        raise Halt(f"{refs[CONSTRAINTS_OUTPUT].key} holds no constraints")
    return Inputs(snapshot, constraints, one(RECOVER_STAGE, VALUATION_OUTPUT, Valuation), refs)
