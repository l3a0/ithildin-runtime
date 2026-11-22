"""The interfaces each adapter and module implements.

docs/architecture/05-building-blocks.md's `### The interfaces` draws them. A
`Protocol` names the methods a type must have, and any class with those
methods satisfies it without inheriting from it, which is what lets a run
swap the simulated broker for the paper one. `core` holds ports and no
adapter, so no module can build an S3 store or an SQS queue and choose its
own mode.

`OrderPath` and `Monitoring` are classes with behaviour that #10 and #11
build, and `Journal` lives in the `journal` package.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from .queue import Event
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
    "Queue",
    "RiskGate",
    "RiskModel",
    "Timer",
]


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
    """Time for deadlines and heartbeats, faked in tests.

    `now` is wall time, which keys a heartbeat. `monotonic` is the reading every
    deadline and limit measures elapsed time on, in seconds, because a wall
    clock the time service steps back would stretch a deadline. #11 types the
    event `after` hands back when it builds the deadlines.
    """

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def after(self, seconds: float) -> object: ...


class Queue(Protocol):
    """A component's queue, which the receive loop alone reads.

    A call the queue does not carry out raises `QueueUnavailable`.
    """

    def receive(self, limit: int) -> Sequence[Event]:
        """Wait for up to `limit` events, and return what arrived, which may be none."""
        ...

    def done(self, event: Event) -> None:
        """Delete the message, so no reader sees it again."""
        ...

    def extend(self, event: Event) -> None:
        """Hide the message from every other reader for the queue's timeout from now."""
        ...

    def release(self, event: Event) -> None:
        """Return the message to the queue at once, for any reader."""
        ...


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
    """The storage port. Each write sends its SHA-256, and no worker deletes through it.

    A call the store does not carry out raises one of the refusals in
    `core.storage`, the same from every adapter: `KeyExists`, `ChecksumMismatch`,
    `StaleTag`, `BlobMissing`, or `StoreUnavailable` for any other refusal and
    for no answer. Workers write through `core.Storage`, which applies the rule
    for each kind of stage on top of this port.
    """

    def create(self, key: str, data: bytes, sha256: str) -> str:
        """Store a blob under a key that does not exist yet, and return its tag."""
        ...

    def get(self, key: str) -> bytes: ...

    def head(self, key: str) -> StoredBlob: ...

    def list(self, prefix: str, after: str | None) -> Sequence[str]: ...

    def replace_if_match(self, key: str, data: bytes, sha256: str, etag: str) -> str:
        """Rewrite a blob only while it carries `etag`, and return the new tag."""
        ...

    def announce(self, key: str) -> None:
        """Announce a stored blob again, as a restarted writer does for its newest result.

        The folder delivers the event to each reader its routes name. S3 skips
        it, because S3 announced the blob when it stored it.
        """
        ...


class Alerts(Protocol):
    def alarm(self, reason: str) -> None: ...

    def heartbeat(self) -> None: ...
