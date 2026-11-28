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
from datetime import datetime
from typing import Protocol

from .alarms import Alarm
from .queue import Event
from .records import (
    Ack,
    Book,
    BucketObject,
    CashRead,
    ClockReading,
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

    def reading(self) -> ClockReading:
        """The machine's clock against each time server, from memory.

        On a wall clock it never raises. A history clock handed recorded
        readings raises `Halt` on a decision they lack.
        """
        ...

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
    an empty answer is returned only where the broker said none. A `Halt` an
    adapter raises reaches a person, so its reason holds no word of the broker's.
    """

    @property
    def alias(self) -> str: ...

    def submit(self, intent: OrderIntent) -> Ack: ...

    def find_order(self, intent: OrderIntent, since: datetime, mapped: object) -> FoundOrder:
        """Every order the broker holds under the intent, open or ended, sent at or after `since`.

        A filled, cancelled or expired order is listed too, because `whole_list`
        true with no row is what gives an order in doubt up as lost. `whole_list`
        says that every page of the list came. `mapped` holds each broker's id the
        journal has mapped, for an adapter that pairs on an order's details.
        """
        ...

    def orders(self, since: datetime) -> OrdersRead: ...

    def lookup(self, broker_order_id: str) -> Ack: ...

    def fills(self, since: datetime) -> FillsRead:
        """Every fill whose `at` is at or after `since`, which recover's bound relies on."""
        ...

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
        """One strategy's limits for a decision, each the tighter of its own and the hard caps'.

        The caps are first held to the stress records.
        """
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
        """The day's loss, with no order needed.

        It has `check`'s halts on its inputs but one, the hold of the caps to the
        stress records, because the day's loss reads no stress move.
        """
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
    """A bucket the platform may only read, such as marketlake's, rooted at one prefix.

    It holds no call that writes, because the data worker's grant on
    marketlake's bucket allows none. A key is relative to the prefix. A key
    that holds no object, or not the version asked for, raises
    `core.BlobMissing`. A download whose bytes fail the checksum stored with
    them raises `core.ChecksumMismatch`. Any other refusal or no answer raises
    `core.StoreUnavailable`. Each is the same from every adapter. A key or a
    prefix that could climb out of the bucket's root is a `ValueError`, and a
    call the adapter's client refuses to send is a `TypeError`, since each is a
    mistake here and no answer of the bucket's.
    """

    def list(self, prefix: str) -> Sequence[str]:
        """Every key that starts with `prefix`, sorted by its UTF-8 bytes.

        A prefix that holds nothing is an empty list, and the one refusal a
        listing raises is `core.StoreUnavailable`, with the bucket's answer in it.
        """
        ...

    def read(self, key: str, version: str | None = None) -> BucketObject:
        """The object's bytes, with the bucket's version and stored SHA-256.

        It reads the newest version, or the one named.
        """
        ...


class Alerts(Protocol):
    """Where an alarm goes: the host's journal, or the environment's push topic.

    `alarm` raises when the alarm went nowhere, so the caller always knows,
    and an adapter that collapses repeats of one cause into one page counts a
    held repeat as sent. `core.alarms` says what an `Alarm` carries.
    """

    def alarm(self, alarm: Alarm) -> None: ...

    def heartbeat(self) -> None: ...
