"""The account's lease: one blob that says which handler may change the account.

docs/design.md's `### Rule 9: who may send, the account's lease` and `### Rule 9:
who takes over a lease` give the rules. In short:

1. **One object per account, kept between holders.** It lives at
   `core.account_lease_key`. Only a first use creates it, create-only, and every
   later write is a rewrite with `If-Match` on the tag the writer last read or
   wrote. Nothing deletes it. Every rewrite raises `count`, so its bytes always
   change and S3's tag never repeats.
2. **A take** reads the lease and rewrites it naming the taker. A lease that is
   free, or whose holder's expiry is more than `MARGIN` past, can be taken. A
   412 means another handler won, a 409 means nothing landed, so the take writes
   again, and a 404 means the lease is absent. Every write's bytes are its own,
   so a write whose reply is lost is settled by reading the lease back.
3. **A takeover** is a take of an expired lease, or of a free one with `owed`
   set. The take writes `owed`, and the order path then stores the broker's
   order list and journals a `takeover` entry before it acts, and clears `owed`
   with one more rewrite. A release leaves `owed` as it found it, so a takeover
   that failed is still owed to the next take.
4. **The renewal** is the check right before each broker call. It rewrites the
   lease on the holder's own last tag and moves its expiry `PERIOD` ahead, so a
   handler that lost the lease cannot read its way back.
5. **A release** rewrites the lease on the holder's own last tag and clears the
   holder. It reads no clock, so it runs on a thread that has no time.
6. **An absent lease** never reads as free. Where the journal holds a `sending`
   entry the lease once existed, so an absent one halts new orders, raises one
   account halt and pages. The journal is read before the lease, since the other
   order halts a healthy account in 20 of 252 interleavings.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import NamedTuple

from ithildinruntime.core import (
    BlobMissing,
    BlobStore,
    Clock,
    CodecError,
    Halt,
    HaltRecord,
    KeyExists,
    Lease,
    OrdersRead,
    StaleTag,
    StoreError,
    StoreUnavailable,
    account_lease_key,
    decode,
    encode,
    sha256_of,
)

from .patience import Tries, code_of

__all__ = [
    "FORMAT",
    "MARGIN",
    "PERIOD",
    "Held",
    "Holder",
    "LeaseHeld",
    "LeaseLost",
    "AbsentHalt",
    "Taken",
    "TakeoverRead",
    "AccountLease",
    "free_lease",
]

# The lease's format, which `core.Lease` carries as every blob stored alone does.
FORMAT = 1
# How long a take or a renewal holds the lease.
PERIOD = timedelta(seconds=30)
# How far past its expiry a lease must be before another handler takes it over: five times
# `core.CLOCK_BOUND`. Nothing holds the execution worker's clock to that bound until #165 builds
# the clock's check before each submit, so until then the late-order halt guards a larger skew.
MARGIN = timedelta(seconds=5)
# How many times a take reads again after a 409 or another handler's first create.
_READS = 5


class LeaseHeld(Halt):
    """The lease is held by another handler that has not expired, so this one does not act."""


class LeaseLost(Halt):
    """A rewrite the store refused, or did not answer, so this handler no longer holds the lease.

    `unanswered` says the store gave no answer, so whether the rewrite landed is unknown.
    """

    def __init__(self, reason: str, *, unanswered: bool = False) -> None:
        super().__init__(reason)
        self.unanswered = unanswered


@dataclass(frozen=True)
class Holder:
    """Who takes the lease: the handler, and what of its host and run it knows.

    The VM, boot, container and restart count are `None` until the worker's
    entry point hands them in (#200).
    """

    handler: str
    release: str | None = None
    run: str | None = None
    vm: str | None = None
    boot: str | None = None
    container: str | None = None
    restart_count: int | None = None


class Held(NamedTuple):
    """The lease as this handler last wrote it, and the tag S3 gave that write."""

    tag: str
    lease: Lease


class Taken(NamedTuple):
    """A take that landed, and what it took over where it owes a takeover.

    `took` is the lease as it was read, and `tag` its tag, which the takeover
    entry names. Both are `None` for a take that owes no takeover.
    """

    held: Held
    took: Lease | None
    tag: str | None


@dataclass(frozen=True)
class TakeoverRead:
    """The broker's order list a takeover read, stored before its entry is journaled.

    `format` is `FORMAT`, as every blob stored at a key of its own carries one.
    `handler` and `release` name the take that read it, and `count` the lease's
    count that take wrote.
    """

    format: int
    account: str
    handler: str
    release: str | None
    count: int
    since: datetime
    asked_at: datetime
    orders: OrdersRead


@dataclass(frozen=True)
class AbsentHalt:
    """The halt an absent lease raises, stored once at the key its journal's head fixes.

    `format` is `FORMAT`. `through` and `sent_at` are the number of the journal's
    newest entry and the time of its newest `sending` when the halt was raised.
    """

    format: int
    account: str
    through: int
    sent_at: datetime
    halt: HaltRecord


def free_lease(
    count: int, owed: bool, owed_from: str | None = None, owed_expired: datetime | None = None
) -> Lease:
    """A lease with no holder, at `count`, and the takeover it still owes where it owes one."""
    return Lease(
        format=FORMAT,
        vm=None,
        boot=None,
        container=None,
        restart_count=None,
        handler=None,
        release=None,
        run=None,
        taken=None,
        expires=None,
        status=None,
        count=count,
        owed=owed,
        owed_from=owed_from if owed else None,
        owed_expired=owed_expired if owed else None,
    )


class AccountLease:
    """The lease of one account, read and written through the store's conditional writes."""

    def __init__(self, store: BlobStore, account: str, clock: Clock) -> None:
        self._store = store
        self._account = account
        self._clock = clock
        self.key = account_lease_key(account)

    def read(self, tries: Tries) -> tuple[str, Lease] | None:
        """The lease and its tag, or `None` where no blob stands at its key.

        The tag and the bytes come from two calls, so the bytes are read again
        until they hash to the SHA-256 the tag's call gave.
        """
        for _ in range(_READS):
            try:
                head = tries(lambda: self._store.head(self.key))
                data = tries(lambda: self._store.get(self.key))
            except BlobMissing:
                return None
            if sha256_of(data) != head.ref.sha256:
                continue
            try:
                lease = decode(data, Lease)
            except CodecError:
                raise Halt(f"{self.key} holds no lease this release reads") from None
            if lease.format != FORMAT:
                raise Halt(
                    f"{self.key} is of the format {lease.format}, which this release does not read"
                )
            return head.etag, lease
        raise Halt(f"{self.key} was rewritten while it was read, {_READS} times in a row")

    def take(self, holder: Holder, tries: Tries, sent: bool, absent: Callable[[], Halt]) -> Taken:
        """Take the lease for `holder`, or raise why it cannot be taken.

        `sent` says whether the journal, read before this call, holds a `sending`
        entry, and `absent` raises the absent lease's halt where it does. A lease
        another handler holds raises `LeaseHeld`. A store that never answers
        raises its last refusal, for the caller to halt on.
        """
        for _ in range(_READS):
            found = self.read(tries)
            now = self._clock.now()
            if found is None:
                if sent:
                    raise absent()
                lease = self._holding(free_lease(0, False), holder, now, None)
                try:
                    tag = self._write(None, lease, tries)
                except KeyExists:
                    # Another handler's first use landed first, so read what it wrote.
                    continue
                return Taken(Held(tag, lease), None, None)
            tag, lease = found
            expired = lease.expires is not None and lease.expires + MARGIN < now
            if lease.handler is not None and not expired:
                if lease.expires is None:
                    raise Halt(f"{self.key} names a holder and no expiry")
                run = f" of the run {lease.run}" if lease.run else ""
                release = f" on the release {lease.release}" if lease.release else ""
                raise LeaseHeld(
                    f"the account's lease is held by {lease.handler}{run}{release} until "
                    f"{lease.expires.isoformat()}, so this handler does not act. If that holder "
                    f"is gone, the first take after {(lease.expires + MARGIN).isoformat()} takes "
                    "it over and alarms"
                )
            owes = lease.owed or lease.handler is not None
            # The holder a takeover is owed from: the one whose lease ran out, or the one an
            # earlier takeover that failed was owed from, or none for a person's clear.
            # A takeover already owed keeps whom it is owed from, though a later taker that
            # failed it too then died holding the lease.
            owed: tuple[str | None, datetime | None] | None
            if lease.owed:
                owed = (lease.owed_from, lease.owed_expired)
            elif lease.handler is not None:
                owed = (lease.handler, lease.expires)
            else:
                owed = None
            mine = self._holding(lease, holder, now, owed)
            try:
                new = self._write(tag, mine, tries)
            except StaleTag:
                # Another handler wrote it after this one read it, and won.
                raise LeaseHeld(
                    "another handler took the account's lease between this handler's read and "
                    "its rewrite, so this handler does not act"
                ) from None
            except BlobMissing:
                if sent:
                    raise absent() from None
                # Gone between the read and the rewrite, on a journal with no `sending`: read
                # again, which finds a first use.
                continue
            took = lease if owes else None
            return Taken(Held(new, mine), took, tag if owes else None)
        raise Halt(f"{self.key} could not be taken: {_READS} reads in a row met another write")

    def renew(self, held: Held, tries: Tries) -> Held:
        """Prove the lease still carries this handler's tag, and move its expiry a period on.

        Any refusal raises `LeaseLost`.
        """
        now = self._clock.now()
        lease = replace(held.lease, expires=now + PERIOD, count=held.lease.count + 1)
        return self._rewrite(held, lease, "renewal", tries)

    def settled(self, held: Held, tries: Tries) -> Held:
        """Clear `owed` once the takeover's entry is journaled, on this handler's own tag."""
        lease = replace(
            held.lease, owed=False, owed_from=None, owed_expired=None, count=held.lease.count + 1
        )
        return self._rewrite(held, lease, "rewrite after its takeover", tries)

    def release(self, held: Held, tries: Tries) -> LeaseLost | None:
        """Clear the holder on this handler's own tag, and say why not where it could not.

        A lease another handler took over, or a store that stays down, is left
        as it is: it expires, and the next take is a takeover.
        """
        was = held.lease
        lease = free_lease(was.count + 1, was.owed, was.owed_from, was.owed_expired)
        try:
            self._rewrite(held, lease, "release", tries)
        except LeaseLost as lost:
            return lost
        return None

    def _rewrite(self, held: Held, lease: Lease, what: str, tries: Tries) -> Held:
        try:
            tag = self._write(held.tag, lease, tries)
        except StaleTag:
            raise LeaseLost(
                f"the account's lease was rewritten since this handler's last answered write of "
                f"it, so its {what} was refused. Either another handler took it over after its "
                "expiry, and that takeover alarms, or a write of this handler's own landed with "
                "its answer lost"
            ) from None
        except BlobMissing:
            raise LeaseLost(
                f"the account's lease is gone from {self.key}, so its {what} was refused, and "
                "the next take halts on it as absent"
            ) from None
        except StoreError as error:
            raise LeaseLost(
                f"the account's lease's {what} was not stored: {code_of(error)}", unanswered=True
            ) from None
        return Held(tag, lease)

    def _write(self, tag: str | None, lease: Lease, tries: Tries) -> str:
        """Write `lease` where the blob carries `tag`, or create it where `tag` is `None`.

        Every write's bytes are its own, since each take names a handler of its
        own and `count` rises on every write. So a write whose answer was lost is
        settled by reading the lease back: these bytes standing mean it landed,
        and the old tag standing means it did not, and it is sent again while
        `tries` lasts. Anything else standing means another handler wrote it,
        which raises `StaleTag`, or `KeyExists` for a create.
        """
        data = encode(lease)
        sha = sha256_of(data)

        def once() -> str:
            try:
                if tag is None:
                    return self._store.create(self.key, data, sha)
                return self._store.replace_if_match(self.key, data, sha, tag)
            except (KeyExists, StaleTag, StoreUnavailable) as error:
                try:
                    stands = self._store.head(self.key)
                except BlobMissing:
                    if tag is None and isinstance(error, StoreUnavailable):
                        raise error from None
                    raise BlobMissing(self.key, "no blob at the key to rewrite") from None
                except StoreError:
                    raise error from None
                if stands.ref.sha256 == sha:
                    return stands.etag
                if isinstance(error, StoreUnavailable) and stands.etag == tag:
                    raise
                if tag is None:
                    raise KeyExists(self.key, "another handler created the lease") from None
                raise StaleTag(self.key, "another handler rewrote the lease") from None

        return tries(once)

    def _holding(
        self,
        lease: Lease,
        holder: Holder,
        now: datetime,
        owed: tuple[str | None, datetime | None] | None,
    ) -> Lease:
        return Lease(
            format=FORMAT,
            vm=holder.vm,
            boot=holder.boot,
            container=holder.container,
            restart_count=holder.restart_count,
            handler=holder.handler,
            release=holder.release,
            run=holder.run,
            taken=now,
            expires=now + PERIOD,
            status=None,
            count=lease.count + 1,
            owed=owed is not None,
            owed_from=None if owed is None else owed[0],
            owed_expired=None if owed is None else owed[1],
        )
