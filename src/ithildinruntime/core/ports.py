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
    BucketObject,
    CashRead,
    Constraints,
    CostSchedule,
    FillsRead,
    ForecastSet,
    FoundOrder,
    GateInputs,
    HardCaps,
    LossReading,
    OrderIntent,
    OrdersRead,
    PositionsRead,
    RecordsRead,
    Snapshot,
    StoredBlob,
    Valuation,
    Verdict,
)

__all__ = [
    "AlphaModel",
    "Alerts",
    "BlobStore",
    "Broker",
    "Bucket",
    "Clock",
    "CostModel",
    "DataSource",
    "Progress",
    "Queue",
    "RiskGate",
    "RiskModel",
    "Timer",
]


class DataSource(Protocol):
    def snapshot(self, decision_at: datetime) -> Snapshot:
        """Only bars whose end and knowable time are at or before `decision_at`."""
        ...


class Progress(Protocol):
    """What the receive loop hands a handler: how it reports, and how a wait in it ends on a stop.

    It only reads the stop. A handler that could ask one would end the loop's
    receives while the loop went on beating as if it worked.
    """

    def __call__(self) -> None:
        """Tell the loop the handler is not hung. This never raises for a stop."""
        ...

    def wait_for_stop(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds, and return whether the loop was asked to stop.

        Only a clock's wait calls it, and on a true answer it raises `Stopped`.
        A handler never returns a result or raises `Halt` because of a stop,
        since the loop stores either as the decision's result.
        It reports nothing while it waits, so a longer wait calls it in steps
        shorter than the heartbeat limit and reports between them.
        """
        ...


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...

    def offset(self) -> timedelta: ...

    def sync_age(self) -> timedelta: ...

    def wait_until(self, at: datetime, report: Progress) -> None:
        """Return once the clock reads `at`, calling `report` while it waits.

        A history clock returns at once. A wall clock waits, calling `report`
        on its timer's interval, so the receive loop reads a handler that is
        waiting for its decision time as one that is not hung. A stop asked
        while it waits raises `Stopped` at once, so a handler calls this only
        before it has written anything. A wait that finds the clock already at
        `at` returns whether or not a stop was asked.
        """
        ...


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
    """A broker's account, which only `OrderPath` calls.

    `alias` names the account it trades, by the platform's alias and never by
    the broker's number. Every read and write returns its received bytes apart
    from the converted value. A read that fails raises `BrokerUnavailable`, and
    an empty answer is returned only where the broker said none.
    """

    @property
    def alias(self) -> str: ...

    def submit(self, intent: OrderIntent) -> Ack: ...

    def find_order(self, intent: OrderIntent, since: datetime, mapped: object) -> FoundOrder: ...

    def orders(self, since: datetime) -> OrdersRead: ...

    def lookup(self, broker_order_id: str) -> Ack: ...

    def fills(self, since: datetime) -> FillsRead: ...

    def positions(self) -> PositionsRead: ...

    def open_orders(self) -> OrdersRead: ...

    def account(self) -> CashRead:
        """The account's cash."""
        ...

    def activities(self, since: datetime) -> RecordsRead: ...

    def cancel(self, broker_order_id: str) -> Ack: ...

    def clock(self) -> datetime: ...


class AlphaModel(Protocol):
    def forecast(self, snapshot: Snapshot) -> ForecastSet: ...


class RiskModel(Protocol):
    def constraints(
        self, book: Book, snapshot: Snapshot, caps: HardCaps, valuation: Valuation
    ) -> Constraints:
        """One strategy's limits for a decision, each the tighter of its own and the hard caps'."""
        ...


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
    ) -> Verdict:
        """Approve one order, or refuse it. An absent, stale or invalid input raises `Halt`."""
        ...

    def loss(
        self,
        book: Book,
        snapshot: Snapshot,
        constraints: Constraints,
        inputs: GateInputs,
        now: datetime,
    ) -> LossReading:
        """The day's loss, with `check`'s halts on its inputs and no order needed."""
        ...


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

    def list(self, prefix: str, after: str | None) -> Sequence[str]:
        """Every key that starts with `prefix` and sorts after `after`, which is left out."""
        ...

    def replace_if_match(self, key: str, data: bytes, sha256: str, etag: str) -> str:
        """Rewrite a blob only while it carries `etag`, and return the new tag."""
        ...

    def announce(self, key: str) -> None:
        """Announce a stored blob again, as a restarted writer does for its newest result.

        The folder delivers the event to each reader its routes name. S3 skips
        it, because S3 announced the blob when it stored it.
        """
        ...


class Bucket(Protocol):
    """A bucket the platform may only read, such as marketlake's.

    It holds no call that writes, because the data worker's grant on
    marketlake's bucket allows none. A key that holds no object raises
    `core.BlobMissing`, and any other refusal or no answer raises
    `core.StoreUnavailable`, the same from every adapter.
    """

    def read(self, key: str) -> BucketObject:
        """The object's bytes, with the bucket's version and stored SHA-256."""
        ...


class Alerts(Protocol):
    def alarm(self, reason: str) -> None: ...

    def heartbeat(self) -> None: ...
