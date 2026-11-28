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

__all__ = ["Inputs", "read_inputs", "read_measured"]


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
    read = _Reader(storage, deployment, run, at, report)
    snapshot = read.snapshot()
    constraints = read.entries(RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints)
    if not constraints:
        raise Halt(f"{read.refs[CONSTRAINTS_OUTPUT].key} holds no constraints")
    valuation = read.one(RECOVER_STAGE, VALUATION_OUTPUT, Valuation)
    return Inputs(snapshot, constraints, valuation, read.refs)


def read_measured(
    storage: Storage, deployment: str, run: str, at: datetime, report: Callable[[], None]
) -> tuple[Snapshot, Valuation]:
    """Data's snapshot and recover's V alone, which is what the day's loss is measured from.

    They are read as `read_inputs` reads them, and the risk stage's file is not
    read at all, so a halted risk stage leaves the day's loss readable.
    """
    read = _Reader(storage, deployment, run, at, report)
    return read.snapshot(), read.one(RECOVER_STAGE, VALUATION_OUTPUT, Valuation)


class _Reader:
    """One decision's files, each read through the result that lists it."""

    def __init__(
        self,
        storage: Storage,
        deployment: str,
        run: str,
        at: datetime,
        report: Callable[[], None],
    ) -> None:
        self._storage = storage
        self._where = (deployment, run, at)
        self._report = report
        self.refs: dict[str, BlobRef] = {}

    def entries[T](self, stage: str, name: str, kind: type[T]) -> tuple[T, ...]:
        deployment, run, at = self._where
        key = result_key(deployment, run, at, stage)
        result = self._storage.read_result(key, at)
        self._report()
        if result is None:
            raise Halt(f"{key} is not stored, and an order is checked against it")
        counts = output_key(deployment, run, at, stage, name)
        listed = [ref for ref in result.outputs if ref.key == counts]
        if result.halt is None and len(listed) != 1:
            raise Halt(f"{key} lists {counts} {len(listed)} times, not once")
        # A result that holds a halt lists nothing to read, which storage halts on by name.
        ref = listed[0] if listed else BlobRef(counts, "")
        found = self._storage.read_output(key, result, ref, at, kind).entries
        self.refs[name] = ref
        self._report()
        return found

    def one[T](self, stage: str, name: str, kind: type[T]) -> T:
        found = self.entries(stage, name, kind)
        if len(found) != 1:
            raise Halt(f"{self.refs[name].key} holds {len(found)} entries, not one")
        return found[0]

    def snapshot(self) -> Snapshot:
        at = self._where[2]
        snapshot = self.one(DATA_STAGE, SNAPSHOT_OUTPUT, Snapshot)
        if snapshot.decision_at != at:
            # Storage checks the decision the file's header names, and the gate goes by
            # the snapshot's own.
            raise Halt(
                f"{self.refs[SNAPSHOT_OUTPUT].key} holds a snapshot of "
                f"{snapshot.decision_at.isoformat()}, not of {at.isoformat()}"
            )
        return snapshot
