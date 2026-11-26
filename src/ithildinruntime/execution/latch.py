"""The loss stop's latch for one account: its firing, its completion, and its state.

docs/architecture/06-runtime.md's `#### What happens when the stop fires` gives
the rules. A firing is kept three ways, and each alone reads as fired.

1. In this process's memory, from before any write until the journal holds it.
2. In the journal, as a `stop_fired` entry or as a `verdict` that read the
   day's loss past its stop, which `journal.stop` folds.
3. As the blob at `accounts/<account>/stop.json`, which
   `core.Storage.hold_create` writes and holds through an outage, so the
   receive loop keeps the process alive until S3 stores it.

`complete` writes whichever part is missing, at each settle of the account. It
deletes nothing, since no worker deletes, and writes no clear. Only the
`ithildin stop clear` command (#203) clears a firing.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from ithildinruntime.core import (
    BlobMissing,
    BlobStore,
    Clock,
    CodecError,
    Halt,
    LossReading,
    StopState,
    Storage,
    StoreError,
    decode,
    encode,
    loss_stop_key,
)
from ithildinruntime.journal import (
    Journal,
    NumberTaken,
    Record,
    StopFiredEntry,
    StopStoredEntry,
    View,
)

from .patience import Patience, Tries, code_of

__all__ = ["Latch", "StopBlob"]

# How many writes one completion makes at most, each on a fresh view.
_PASSES = 8


@dataclass(frozen=True)
class StopBlob:
    """What `stop.json` holds: the firing, and the number of its entry where one was stored."""

    firing: str
    account: str
    deployment: str | None
    run: str | None
    decision_at: datetime | None
    reading: LossReading | None
    reason: str
    fired: int | None


class _Wrote(Enum):
    WROTE = "wrote"
    TAKEN = "taken"
    FAILED = "failed"


class Latch:
    """The loss stop of one account, which every path of the account shares.

    `lock` is the account's own. A firing is held and counted under it, and the
    submit holds it while it reads `holding` and `count` and marks a call, so
    no firing of this process falls between the last check and the broker's
    call, whether it is still held or was stored whole in between.
    """

    def __init__(
        self,
        account: str,
        journal: Journal,
        store: BlobStore,
        storage: Storage,
        patience: Patience,
        clock: Clock,
        lock: threading.Lock,
    ) -> None:
        self._account = account
        self._journal = journal
        self._store = store
        self._storage = storage
        self._patience = patience
        self._clock = clock
        self._lock = lock
        self._key = loss_stop_key(account)
        self._pending: list[StopFiredEntry] = []
        # How many firings this process has held, which only rises.
        self._count = 0

    # Firing

    def fire(
        self,
        reading: LossReading | None,
        where: tuple[str, str, datetime] | None,
        reason: str,
        report: Callable[[], None],
    ) -> str:
        """Hold a firing, write what can be written of it, and say what a halt adds.

        It never raises for a write that failed, since the firing is held
        either way and the halt it comes with matters more.
        """
        deployment, run, at = where if where is not None else (None, None, None)
        entry = StopFiredEntry(secrets.token_hex(16), deployment, run, at, reading, reason)
        with self._lock:
            self._pending.append(entry)
            self._count += 1
        try:
            failing = self._complete(report)
        except Exception as error:
            failing = [f"completing it raised {type(error).__name__}"]
        if not failing:
            return ""
        return f". The loss stop's firing is held, and not wholly stored: {'; '.join(failing)}"

    def complete(self, report: Callable[[], None]) -> None:
        """Write whatever part of a firing is missing, and halt naming each that failed.

        A read that fails with nothing to complete raises nothing, and the
        gate's read of the state finds it unreadable.
        """
        failing = self._complete(report)
        if failing:
            held = self.held()
            said = _on(held[-1].reading, held[-1].reason) if held else ""
            raise Halt(f"the loss stop fired{said}, and is not wholly stored: {'; '.join(failing)}")

    def holding(self) -> bool:
        """Whether this process holds a firing the journal has not taken. Call it under the lock."""
        return bool(self._pending)

    def count(self) -> int:
        """How many firings this process has held, which only rises. Call it under the lock.

        The submit compares the count its checks began with and the count as
        it marks the call, so a firing another thread held and stored whole in
        between is seen.
        """
        return self._count

    def held(self) -> tuple[StopFiredEntry, ...]:
        with self._lock:
            return tuple(self._pending)

    # The state

    def state(self, view: View, tries: Tries) -> tuple[StopState, str]:
        """The stop's state, from memory, the journal and the blob, with an error's code.

        A blob that cannot be read reads as unreadable whatever else is found,
        because an unreadable stop holds the close too.
        """
        try:
            tries(lambda: self._store.get(self._key))
            found = True
        except BlobMissing:
            found = False
        except Exception as error:
            return StopState.UNREADABLE, code_of(error)
        with self._lock:
            held = bool(self._pending)
        if held or view.stop.fired or found:
            return StopState.FIRED, ""
        return StopState.ABSENT, ""

    def said(self, view: View, state: StopState, error: str) -> str:
        """A stop that stands, in words: what fired it, its reading, and what clears it."""
        if state is StopState.UNREADABLE:
            return f"loss stop unreadable: {error}"
        latch, held = view.stop, self.held()
        if latch.standing is not None:
            entry = latch.standing.entry
            what = f"entry {latch.standing.seq} fired it{_on(entry.reading, entry.reason)}"
        elif latch.alone:
            seq, verdict = latch.alone[-1]
            what = f"the verdict at entry {seq} fired it{_on(verdict.verdict.loss, '')}"
        elif held:
            what = (
                "this process holds a firing the journal has not taken yet"
                f"{_on(held[-1].reading, held[-1].reason)}"
            )
        else:
            what = f"{self._key} stands, and the journal holds no firing of it yet"
        return (
            f"loss stop stands: {what}. `ithildin stop clear --account {self._account}` clears it, "
            "once #203 builds that command"
        )

    # Completing

    def _complete(self, report: Callable[[], None]) -> list[str]:
        failing: list[str] = []
        for _ in range(_PASSES):
            if not self._next(report, failing):
                return failing
        failing.append(f"other writers took its numbers {_PASSES} times in a row")
        return failing

    def _next(self, report: Callable[[], None], failing: list[str]) -> bool:
        """Make the one write that comes next, and say whether to look again.

        A read that finds nothing owed is tried once, because a settle comes
        before every order and must not wait out the store's deadline ahead of
        an order's cutoff. Each write, and each read a firing is owed, has the
        whole deadline.
        """
        tries = self._patience.tries(report)
        once = self._patience.once(report)
        try:
            view = (tries if self.held() else once)(self._journal.view)
        except (StoreError, Halt) as error:
            held = self.held()
            if held:
                failing.append(f"the journal could not be read: {_said(error)}")
                for entry in held:
                    self._create(entry, None, failing)
            return False
        latch = view.stop
        with self._lock:
            # The journal keeps a firing it holds, and one that stands covers this one, so
            # nothing more is journaled for it.
            self._pending = [
                entry
                for entry in self._pending
                if entry.firing not in latch.firings and latch.standing is None
            ]
            held = tuple(self._pending)
        if held:
            entry = held[0]
            wrote = self._write(view, entry, tries, failing, "its entry")
            if wrote is _Wrote.FAILED:
                self._create(entry, None, failing)
            return wrote is not _Wrote.FAILED
        if latch.standing is None and latch.alone:
            seq, verdict = latch.alone[-1]
            entry = StopFiredEntry(
                secrets.token_hex(16),
                None,
                None,
                None,
                verdict.verdict.loss,
                f"the verdict of {verdict.client_order_id} at entry {seq} read the day's loss "
                "past its stop",
            )
            wrote = self._write(view, entry, tries, failing, "the entry of a verdict's firing")
            if wrote is _Wrote.FAILED:
                self._create(entry, None, failing)
            return wrote is not _Wrote.FAILED
        standing = latch.standing
        if not latch.alone and (standing is None or standing.stored is not None):
            # Nothing needs a blob, so a copy `core.Storage` holds is let go rather than stored
            # after a clear has deleted the one that counts.
            self._storage.release(self._key)
        if standing is not None and standing.stored is not None:
            return False
        wanted = standing is not None
        try:
            found = self._tag(tries if wanted else once)
        except StoreError as error:
            if wanted:
                failing.append(f"{self._key} could not be read: {code_of(error)}")
            return False
        if standing is not None:
            if found is None:
                if not self._create(standing.entry, standing.seq, failing):
                    return False
                try:
                    found = self._tag(tries)
                except StoreError as error:
                    failing.append(f"the tag of {self._key} could not be read: {code_of(error)}")
                    return False
                if found is None:
                    return False
            if found in latch.tags:
                # A covered firing's blob: a clear between its entry and its delete.
                return False
            stored = StopStoredEntry(standing.seq, found)
            return self._write(view, stored, tries, failing, "its stop_stored") in (
                _Wrote.WROTE,
                _Wrote.TAKEN,
            )
        if found is None or found in latch.tags:
            return False
        # A blob no entry names, with no firing standing.
        entry = self._orphan(tries, latch.firings)
        return self._write(view, entry, tries, failing, "the entry of a blob's firing") in (
            _Wrote.WROTE,
            _Wrote.TAKEN,
        )

    def _write(
        self, view: View, record: Record, tries: Tries, failing: list[str], what: str
    ) -> _Wrote:
        at = self._clock.now()
        try:
            tries(lambda: self._journal.record(view, record, at))
        except NumberTaken:
            return _Wrote.TAKEN
        except (StoreError, Halt) as error:
            failing.append(f"{what} was not stored: {_said(error)}")
            return _Wrote.FAILED
        return _Wrote.WROTE

    def _create(self, entry: StopFiredEntry, fired: int | None, failing: list[str]) -> bool:
        """Create the blob for a firing, or find one there, and say whether one now stands.

        A blob `core.Storage` holds already is left to its retry, so one
        outage holds one blob.
        """
        if any(ref.key == self._key for ref in self._storage.unstored()):
            failing.append(f"{self._key} is held until S3 stores it")
            return False
        blob = StopBlob(
            entry.firing,
            self._account,
            entry.deployment,
            entry.run,
            entry.decision_at,
            entry.reading,
            entry.reason,
            fired,
        )
        try:
            self._storage.hold_create(self._key, encode(blob))
        except Exception as error:
            failing.append(f"{self._key} was not stored, and is held: {_said(error)}")
            return False
        return True

    def _tag(self, tries: Tries) -> str | None:
        """The blob's tag, or `None` where no blob stands."""
        try:
            return tries(lambda: self._store.head(self._key)).etag
        except BlobMissing:
            return None

    def _orphan(self, tries: Tries, taken: Mapping[str, int]) -> StopFiredEntry:
        """The firing a blob no entry names holds, or one that says it holds none this reads.

        A firing whose id an entry took already gets a new one, since its blob
        is then a copy no entry names.
        """
        try:
            blob = decode(tries(lambda: self._store.get(self._key)), StopBlob)
        except (StoreError, CodecError) as error:
            reason = f"{self._key} holds no firing this release reads: {_said(error)}"
            return StopFiredEntry(secrets.token_hex(16), None, None, None, None, reason)
        if blob.account != self._account:
            reason = f"{self._key} holds a firing of another account"
            return StopFiredEntry(secrets.token_hex(16), None, None, None, None, reason)
        firing = secrets.token_hex(16) if blob.firing in taken else blob.firing
        return StopFiredEntry(
            firing, blob.deployment, blob.run, blob.decision_at, blob.reading, blob.reason
        )


def _on(reading: LossReading | None, reason: str) -> str:
    if reading is None:
        return f", with no reading: {reason}"
    return (
        f", on a day's loss of {reading.loss} past its stop of {reading.bound}, measured from "
        f"the valuation of {reading.session.isoformat()}"
    )


def _said(error: BaseException) -> str:
    return error.reason if isinstance(error, Halt) else code_of(error)
