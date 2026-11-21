"""The interfaces each adapter and module implements.

docs/architecture/05-building-blocks.md's `### The interfaces` draws them. A
`Protocol` names the methods a type must have, and any class with those
methods satisfies it without inheriting from it, which is what lets a run
swap the simulated broker for the paper one. `core` holds ports and no
adapter, so no module can build an S3 store or an SQS queue and choose its
own mode.

`OrderPath` and `Monitoring` are classes with behaviour that #10 and #11
build, and `Journal` lives in the `journal` package that #80 builds.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from .records import (
    Ack,
    Book,
    BrokerRead,
    Constraints,
    CostSchedule,
    Forecast,
    FoundOrder,
    GateInputs,
    OrderIntent,
    Snapshot,
    StoredBlob,
    Verdict,
)

__all__ = [
    "AlphaModel",
    "Alerts",
    "BlobStore",
    "Broker",
    "Clock",
    "CostModel",
    "DataSource",
    "Event",
    "Queue",
    "RiskGate",
    "RiskModel",
    "Timer",
]


class Event(Protocol):
    """What a queue or a timer hands a worker. #81 gives it its fields."""


class DataSource(Protocol):
    def snapshot(self, decision_at: datetime) -> Snapshot:
        """Only bars whose end and knowable time are at or before `decision_at`."""
        ...


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def offset(self) -> timedelta: ...

    def sync_age(self) -> timedelta: ...

    def wait_until(self, at: datetime) -> None: ...


class Timer(Protocol):
    """Wall time for deadlines and heartbeats, faked in tests."""

    def now(self) -> datetime: ...

    def after(self, seconds: float) -> Event: ...


class Queue(Protocol):
    def receive(self) -> Event: ...

    def done(self, event: Event) -> None: ...

    def extend(self, event: Event) -> None: ...

    def release(self, event: Event) -> None: ...


class Broker(Protocol):
    """Every read and write returns its received bytes apart from the converted value."""

    def submit(self, intent: OrderIntent) -> Ack: ...

    def find_order(self, intent: OrderIntent, since: datetime, mapped: object) -> FoundOrder: ...

    def orders(self, since: datetime) -> BrokerRead: ...

    def lookup(self, broker_order_id: str) -> Ack: ...

    def fills(self, since: datetime) -> BrokerRead: ...

    def positions(self) -> BrokerRead: ...

    def open_orders(self) -> BrokerRead: ...

    def activities(self, since: datetime) -> BrokerRead: ...

    def cancel(self, broker_order_id: str) -> Ack: ...

    def clock(self) -> datetime: ...


class AlphaModel(Protocol):
    def forecast(self, snapshot: Snapshot) -> Sequence[Forecast]: ...


class RiskModel(Protocol):
    def constraints(self, book: Book, snapshot: Snapshot) -> Constraints: ...


class CostModel(Protocol):
    def schedule(self, snapshot: Snapshot) -> CostSchedule: ...


class RiskGate(Protocol):
    def check(
        self,
        intent: OrderIntent,
        book: Book,
        snapshot: Snapshot,
        constraints: Constraints,
        inputs: GateInputs,
        now: datetime,
    ) -> Verdict: ...


class BlobStore(Protocol):
    """The storage port. Each write sends its SHA-256, and no worker deletes through it."""

    def create(self, key: str, data: bytes, sha256: str) -> str:
        """Store a blob under a key that does not exist yet, and return its tag."""
        ...

    def get(self, key: str) -> bytes: ...

    def head(self, key: str) -> StoredBlob: ...

    def list(self, prefix: str, after: str | None) -> Sequence[str]: ...

    def replace_if_match(self, key: str, data: bytes, sha256: str, etag: str) -> str:
        """Rewrite a blob only while it carries `etag`, and return the new tag."""
        ...


class Alerts(Protocol):
    def alarm(self, reason: str) -> None: ...

    def heartbeat(self) -> None: ...
