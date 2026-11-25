"""The prices the simulated broker may fill at, read from what data stored.

`execution.brokers` imports `core` alone, so marketlake's reader never loads
here. The snapshot a decision's order was checked against is the one it is
filled from, or a later decision's.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Protocol

from ithildinruntime.core import (
    DATA_STAGE,
    InstrumentId,
    Quote,
    Snapshot,
    StageKind,
    StageRun,
    Storage,
    session_close,
)

__all__ = ["Prices", "StoredPrices"]


class Prices(Protocol):
    """The prices of one decision."""

    @property
    def decision_at(self) -> datetime: ...

    def close(self, instrument: InstrumentId, session: date) -> Decimal | None:
        """The official close of a session, or `None` where the decision holds no bar of it."""
        ...

    def quote(self, instrument: InstrumentId) -> Quote | None:
        """The quote the decision read, not judged, or `None` where it read none."""
        ...


class StoredPrices:
    """`Prices` from data's result and snapshot for one decision, read once, when first asked.

    A result that is missing, stale or holds a halt raises `Halt`, and so does a
    snapshot that fails the checks every stage makes of one. Each later ask
    then reads again.
    """

    def __init__(self, storage: Storage, deployment: str, run: str, decision_at: datetime) -> None:
        # `StageDecision` is the one reader of a stage's result and file. What its
        # headers would name is never written, because nothing here is stored.
        reader = StageRun(
            storage,
            format=0,
            stage="simulated-broker",
            kind=StageKind.DETERMINISTIC,
            deployment=deployment,
            run=run,
            release="",
            config_hashes={},
        )
        self._decision_at = decision_at
        self._reader = reader
        self._read: Snapshot | None = None

    @property
    def decision_at(self) -> datetime:
        return self._decision_at

    def close(self, instrument: InstrumentId, session: date) -> Decimal | None:
        return session_close(self._snapshot(), instrument, session)

    def quote(self, instrument: InstrumentId) -> Quote | None:
        return self._snapshot().quotes.get(instrument)

    def _snapshot(self) -> Snapshot:
        if self._read is None:
            decision = self._reader.decision(self._decision_at, lambda: None)
            self._read = decision.snapshot(DATA_STAGE)
        return self._read
