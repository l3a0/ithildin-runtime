"""The account's lease on its own: a take, a renewal, a release and an absent lease (#148).

`execution/lease.py` gives the rules. Each test runs on the folder store, which
answers `replace_if_match` as S3 does, and on the store in memory the order
path's tests use, so the two agree on what a rewrite answers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    BlobMissing,
    Conflict,
    Halt,
    Lease,
    Retry,
    StaleTag,
    StoreUnavailable,
    account_lease_key,
    decode,
    encode,
    sha256_of,
)
from ithildinruntime.execution.lease import (
    FORMAT,
    MARGIN,
    PERIOD,
    AccountLease,
    Holder,
    LeaseHeld,
    LeaseLost,
    free_lease,
)
from ithildinruntime.execution.patience import Patience
from tests.support.journals import Memory

ACCOUNT = "paper-1"
KEY = account_lease_key(ACCOUNT)
AT = datetime(2026, 11, 2, 20, 40, tzinfo=UTC)
A, B = Holder("a" * 32 + "-1", release="r1", run="run-1"), Holder("b" * 32 + "-1")


class Clock:
    def __init__(self) -> None:
        self.at = AT
        self.elapsed = 0.0

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return self.elapsed

    def wait(self, seconds: float) -> None:
        self.elapsed += seconds


class Absent(Halt):
    pass


def _absent() -> Halt:
    return Absent("absent")


@pytest.fixture(params=["folder", "memory"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> object:
    return FolderStore(tmp_path) if request.param == "folder" else Memory()


def _lease(store: object, clock: Clock) -> AccountLease:
    return AccountLease(store, ACCOUNT, clock)  # type: ignore[arg-type]


def _tries(clock: Clock):  # type: ignore[no-untyped-def]
    return Patience(clock, Retry(timedelta(seconds=10), 1.0, 4.0), clock.wait).tries(lambda: None)


def _stored(store: object) -> Lease:
    return decode(store.get(KEY), Lease)  # type: ignore[attr-defined]


# A take


def test_a_first_use_creates_the_lease_holding_the_taker(store: object) -> None:
    clock = Clock()
    taken = _lease(store, clock).take(A, _tries(clock), False, _absent)
    assert taken.took is None and taken.tag is None
    lease = _stored(store)
    assert lease == taken.held.lease
    assert (lease.format, lease.handler, lease.release, lease.run) == (
        FORMAT,
        A.handler,
        "r1",
        "run-1",
    )
    assert (lease.taken, lease.expires, lease.count, lease.owed) == (AT, AT + PERIOD, 1, False)


def test_an_absent_lease_where_the_journal_holds_a_sending_is_never_created(store: object) -> None:
    clock = Clock()
    with pytest.raises(Absent):
        _lease(store, clock).take(A, _tries(clock), True, _absent)
    with pytest.raises(BlobMissing):
        store.get(KEY)  # type: ignore[attr-defined]


def test_a_lease_another_handler_holds_is_not_taken_until_it_expires_past_the_margin(
    store: object,
) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    lease.take(A, _tries(clock), False, _absent)
    for after in (timedelta(0), PERIOD, PERIOD + MARGIN):
        clock.at = AT + after
        with pytest.raises(
            LeaseHeld, match=f"held by {A.handler} of the run run-1 on the release r1 until"
        ):
            lease.take(B, _tries(clock), True, _absent)
    clock.at = AT + PERIOD + MARGIN + timedelta(microseconds=1)
    taken = lease.take(B, _tries(clock), True, _absent)
    assert taken.took is not None and taken.took.handler == A.handler
    assert taken.held.lease.owed and taken.held.lease.handler == B.handler


def test_a_release_clears_the_holder_and_keeps_what_is_owed(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    held = lease.take(A, _tries(clock), False, _absent).held
    assert lease.release(held, _tries(clock)) is None
    assert _stored(store) == free_lease(2, False)
    held = lease.take(A, _tries(clock), True, _absent).held
    clock.at += PERIOD + MARGIN + timedelta(seconds=1)
    # B takes over and fails before its takeover settles, so its release leaves it owed.
    owing = lease.take(B, _tries(clock), True, _absent)
    assert owing.took is not None and owing.held.lease.owed
    lease.release(owing.held, _tries(clock))
    # The takeover is still owed from A, whose lease ran out, and the release keeps that.
    owed = free_lease(5, True, A.handler, AT + PERIOD)
    assert _stored(store) == owed
    assert lease.take(A, _tries(clock), True, _absent).took == owed


def test_a_free_lease_with_a_takeover_owed_is_taken_as_a_takeover(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    store.create(KEY, encode(free_lease(7, True)), sha256_of(encode(free_lease(7, True))))  # type: ignore[attr-defined]
    taken = lease.take(A, _tries(clock), True, _absent)
    assert taken.took == free_lease(7, True) and taken.tag is not None
    assert taken.held.lease.owed and taken.held.lease.count == 8
    settled = lease.settled(taken.held, _tries(clock))
    assert not settled.lease.owed and _stored(store) == settled.lease
    lease.release(settled, _tries(clock))
    assert _stored(store) == free_lease(10, False)


def test_a_settled_takeover_owes_nothing_and_names_nobody(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    lease.take(A, _tries(clock), False, _absent)
    clock.at += PERIOD + MARGIN + timedelta(seconds=1)
    taken = lease.take(B, _tries(clock), True, _absent)
    assert (taken.held.lease.owed_from, taken.held.lease.owed_expired) == (A.handler, AT + PERIOD)
    lease.settled(taken.held, _tries(clock))
    stored = _stored(store)
    assert (stored.owed, stored.owed_from, stored.owed_expired) == (False, None, None)


def test_a_takeover_owed_keeps_its_holder_through_a_taker_that_died_holding_it(
    store: object,
) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    lease.take(A, _tries(clock), False, _absent)
    clock.at += PERIOD + MARGIN + timedelta(seconds=1)
    # B takes it over, fails its takeover and dies holding the lease, with nothing released.
    lease.take(B, _tries(clock), True, _absent)
    clock.at += PERIOD + MARGIN + timedelta(seconds=1)
    third = lease.take(Holder("c" * 32 + "-1"), _tries(clock), True, _absent)
    assert (third.held.lease.owed_from, third.held.lease.owed_expired) == (A.handler, AT + PERIOD)


def test_every_write_holds_new_bytes_and_a_new_tag(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    seen = []
    held = lease.take(A, _tries(clock), False, _absent).held
    seen.append((held.tag, store.get(KEY)))  # type: ignore[attr-defined]
    for _ in range(3):
        # The clock does not move, so only the count tells the renewals apart.
        held = lease.renew(held, _tries(clock))
        seen.append((held.tag, store.get(KEY)))  # type: ignore[attr-defined]
    lease.release(held, _tries(clock))
    held = lease.take(A, _tries(clock), False, _absent).held
    seen.append((held.tag, store.get(KEY)))  # type: ignore[attr-defined]
    lease.release(held, _tries(clock))
    seen.append(("", store.get(KEY)))  # type: ignore[attr-defined]
    assert len({data for _, data in seen}) == len(seen)
    assert len({tag for tag, _ in seen[:-1]}) == len(seen) - 1


# A renewal


def test_a_renewal_moves_the_expiry_a_period_on_from_the_clock(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    held = lease.take(A, _tries(clock), False, _absent).held
    clock.at += timedelta(seconds=20)
    renewed = lease.renew(held, _tries(clock))
    assert renewed.lease.expires == clock.at + PERIOD and _stored(store) == renewed.lease


def test_a_holder_whose_lease_was_taken_over_cannot_renew_or_release_it(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    frozen = lease.take(A, _tries(clock), False, _absent).held
    clock.at += PERIOD + MARGIN + timedelta(seconds=1)
    new = lease.take(B, _tries(clock), True, _absent).held
    with pytest.raises(LeaseLost, match="rewritten since this handler.s last answered write"):
        lease.renew(frozen, _tries(clock))
    lost = lease.release(frozen, _tries(clock))
    assert lost is not None and "refused" in lost.reason
    assert _stored(store) == new.lease


def test_a_renewal_the_store_never_answers_is_lost_and_says_so(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    held = lease.take(A, _tries(clock), False, _absent).held
    real = store.replace_if_match  # type: ignore[attr-defined]

    def down(*args: object) -> str:
        raise StoreUnavailable(KEY, "SlowDown: the store's own text")

    store.replace_if_match = down  # type: ignore[attr-defined]
    with pytest.raises(
        LeaseLost, match="renewal was not stored: StoreUnavailable SlowDown"
    ) as lost:
        lease.renew(held, _tries(clock))
    assert lost.value.unanswered
    store.replace_if_match = real  # type: ignore[attr-defined]
    assert _stored(store) == held.lease


@pytest.mark.parametrize("landed", [False, True], ids=["refused", "landed unanswered"])
def test_a_write_whose_answer_was_lost_is_settled_by_reading_the_lease_back(
    store: object, landed: bool
) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    held = lease.take(A, _tries(clock), False, _absent).held
    real, calls = store.replace_if_match, []  # type: ignore[attr-defined]

    def once_lost(*args: object) -> str:
        calls.append(args)
        if len(calls) == 1:
            if landed:
                real(*args)
            raise StoreUnavailable(KEY, "no answer")
        return real(*args)

    store.replace_if_match = once_lost  # type: ignore[attr-defined]
    renewed = lease.renew(held, _tries(clock))
    assert _stored(store) == renewed.lease
    assert len(calls) == (1 if landed else 2)
    store.replace_if_match = real  # type: ignore[attr-defined]
    assert lease.release(renewed, _tries(clock)) is None


# What a take's rewrite answers


def test_a_409_reads_again_and_a_412_means_another_handler_won(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    lease.release(lease.take(A, _tries(clock), False, _absent).held, _tries(clock))
    real, answers = store.replace_if_match, [Conflict(KEY, "409 ConditionalRequestConflict")]  # type: ignore[attr-defined]

    def racing(*args: object) -> str:
        if answers:
            raise answers.pop()
        return real(*args)

    store.replace_if_match = racing  # type: ignore[attr-defined]
    assert lease.take(B, _tries(clock), True, _absent).held.lease.handler == B.handler
    answers.append(StaleTag(KEY, "412 PreconditionFailed"))
    clock.at += PERIOD + MARGIN + timedelta(seconds=1)
    with pytest.raises(LeaseHeld, match="between this handler's read and its rewrite"):
        lease.take(A, _tries(clock), True, _absent)


def test_a_take_whose_rewrite_meets_no_blob_reads_as_absent(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    lease.release(lease.take(A, _tries(clock), False, _absent).held, _tries(clock))

    def gone(*args: object) -> str:
        raise BlobMissing(KEY, "NoSuchKey")

    store.replace_if_match = gone  # type: ignore[attr-defined]
    with pytest.raises(Absent):
        lease.take(B, _tries(clock), True, _absent)


def test_a_take_whose_rewrite_landed_unanswered_holds_the_lease(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    lease.release(lease.take(A, _tries(clock), False, _absent).held, _tries(clock))
    real = store.replace_if_match  # type: ignore[attr-defined]

    def landed_unanswered(*args: object) -> str:
        store.replace_if_match = real  # type: ignore[attr-defined]
        real(*args)
        raise StoreUnavailable(KEY, "no answer")

    store.replace_if_match = landed_unanswered  # type: ignore[attr-defined]
    taken = lease.take(B, _tries(clock), True, _absent)
    assert taken.held.lease.handler == B.handler and _stored(store) == taken.held.lease


def test_a_take_the_store_never_answers_raises_the_stores_refusal(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    lease.release(lease.take(A, _tries(clock), False, _absent).held, _tries(clock))

    def down(*args: object) -> str:
        raise StoreUnavailable(KEY, "no answer")

    store.replace_if_match = down  # type: ignore[attr-defined]
    with pytest.raises(StoreUnavailable):
        lease.take(B, _tries(clock), True, _absent)


def test_a_second_first_use_finds_the_first_one_s_lease(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    real = store.create  # type: ignore[attr-defined]

    def first_lands(key: str, data: bytes, sha256: str) -> str:
        store.create = real  # type: ignore[attr-defined]
        _lease(store, clock).take(A, _tries(clock), False, _absent)
        return real(key, data, sha256)

    store.create = first_lands  # type: ignore[attr-defined]
    with pytest.raises(LeaseHeld, match=f"held by {A.handler}"):
        lease.take(B, _tries(clock), False, _absent)


def test_a_read_whose_bytes_differ_from_the_heads_checksum_reads_again(store: object) -> None:
    clock = Clock()
    lease = _lease(store, clock)
    held = lease.take(A, _tries(clock), False, _absent).held
    real, gets = store.get, []  # type: ignore[attr-defined]

    def torn(key: str) -> bytes:
        gets.append(key)
        # The first read meets bytes another write left between the head and the get.
        return b"torn" if len(gets) == 1 else real(key)

    store.get = torn  # type: ignore[attr-defined]
    assert lease.read(_tries(clock)) == (held.tag, held.lease)
    assert len(gets) == 2
    store.get = lambda key: b"torn"  # type: ignore[attr-defined]
    with pytest.raises(Halt, match="rewritten while it was read, 5 times in a row"):
        lease.read(_tries(clock))


def test_a_lease_of_another_format_or_none_this_release_reads_halts(store: object) -> None:
    clock = Clock()
    bad = encode(free_lease(1, False)).replace(b'"format":1', b'"format":2')
    store.create(KEY, bad, sha256_of(bad))  # type: ignore[attr-defined]
    with pytest.raises(Halt, match="of the format 2"):
        _lease(store, clock).take(A, _tries(clock), True, _absent)
