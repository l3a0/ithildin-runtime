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
# `core.CLOCK_BOUND`, past which the clock check already halts.
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
    """The broker's order list a takeover read, stored before its entry is journaled."""

    account: str
    count: int
    since: datetime
    asked_at: datetime
    orders: OrdersRead


def free_lease(count: int, owed: bool) -> Lease:
    """A lease with no holder, at `count`."""
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
                lease = self._holding(free_lease(0, False), holder, now, owed=False)
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
                raise LeaseHeld(
                    f"the account's lease is held by {lease.handler} until "
                    f"{lease.expires.isoformat()}, so this handler does not act"
                )
            owes = lease.owed or lease.handler is not None
            mine = self._holding(lease, holder, now, owed=owes)
            try:
                new = self._write(tag, mine, tries)
            except StaleTag:
                # Another handler wrote it after this one read it, and won.
                raise LeaseHeld(
                    "another handler took the account's lease between this handler's read and "
                    "its rewrite, so this handler does not act"
                ) from None
            except BlobMissing:
                raise absent() from None
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
        lease = replace(held.lease, owed=False, count=held.lease.count + 1)
        return self._rewrite(held, lease, "rewrite after its takeover", tries)

    def release(self, held: Held, tries: Tries) -> str | None:
        """Clear the holder on this handler's own tag, and say why not where it could not.

        A lease another handler took over, or a store that stays down, is left
        as it is: it expires, and the next take is a takeover.
        """
        lease = free_lease(held.lease.count + 1, held.lease.owed)
        try:
            self._rewrite(held, lease, "release", tries)
        except LeaseLost as lost:
            return lost.reason
        return None

    def _rewrite(self, held: Held, lease: Lease, what: str, tries: Tries) -> Held:
        try:
            tag = self._write(held.tag, lease, tries)
        except (StaleTag, BlobMissing):
            raise LeaseLost(
                f"the account's lease was rewritten by another handler, so its {what} was refused"
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

    def _holding(self, lease: Lease, holder: Holder, now: datetime, *, owed: bool) -> Lease:
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
            owed=owed,
        )
