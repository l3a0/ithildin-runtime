"""How long execution waits on a store, and what a reason says of one that refused."""

from __future__ import annotations

from datetime import timedelta

import pytest

from ithildinruntime.core import BlobMissing, ChecksumMismatch, Retry, StoreUnavailable
from ithildinruntime.execution.patience import Patience, code_of
from tests.support.order_world import NOW, Clock

RETRY = Retry(timedelta(seconds=20), 1.0, 4.0)


class Refusing:
    """A call that refuses a number of times, as a store down for a while does."""

    def __init__(self, times: int, kind: type = StoreUnavailable) -> None:
        self.left = times
        self.kind = kind
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if self.left:
            self.left -= 1
            raise self.kind("key", "503 SlowDown: try later")
        return "found"


def made(retry: Retry = RETRY, until: float | None = None):  # type: ignore[no-untyped-def]
    clock, told = Clock(NOW), []
    at = None if until is None else NOW + timedelta(seconds=until)
    tries = Patience(clock, retry, clock.wait).tries(lambda: told.append(clock.elapsed), at)
    return clock, told, tries


@pytest.mark.parametrize("kind", [StoreUnavailable, ChecksumMismatch])
def test_a_refused_call_is_made_again_with_a_report_for_each_try(kind: type) -> None:
    clock, told, tries = made()
    call = Refusing(3, kind)
    assert tries(call) == "found"
    assert clock.waits == [1.0, 2.0, 4.0] and tries.waited and not tries.over
    assert told == [0.0, 1.0, 3.0, 7.0]


def test_the_tries_end_at_the_deadline_with_one_made_at_it() -> None:
    clock, told, tries = made()
    call = Refusing(99)
    with pytest.raises(StoreUnavailable):
        tries(call)
    assert clock.elapsed == 20.0 and clock.waits == [1.0, 2.0, 4.0, 4.0, 4.0, 4.0, 1.0]
    assert call.calls == 8 and tries.over and len(told) == 8


@pytest.mark.parametrize(("until", "waits"), [(3.5, [1.0, 2.0, 0.5]), (0, []), (-5, [])])
def test_the_tries_end_by_the_moment_given_and_one_is_always_made(
    until: float, waits: list[float]
) -> None:
    clock, _, tries = made(until=until)
    with pytest.raises(StoreUnavailable):
        tries(Refusing(99))
    assert clock.waits == waits
    _, _, tries = made(until=until)
    assert tries(Refusing(0)) == "found" and not tries.waited


def test_calls_under_one_deadline_share_it() -> None:
    clock, _, tries = made()
    tries(Refusing(5))
    assert clock.elapsed == 15.0
    with pytest.raises(StoreUnavailable):
        tries(Refusing(99))
    assert clock.elapsed == 20.0


def test_any_other_answer_is_raised_at_once() -> None:
    clock, told, tries = made()
    for kind in (BlobMissing, PermissionError):
        with pytest.raises(kind):
            tries(Refusing(1, kind))
    assert clock.waits == [] and not tries.waited and len(told) == 2


@pytest.mark.parametrize(
    ("error", "said"),
    [
        (
            StoreUnavailable("k", "403 AccessDenied: User arn:aws"),
            "StoreUnavailable 403 AccessDenied",
        ),
        (StoreUnavailable("k", "no answer: timed out"), "StoreUnavailable no answer"),
        (StoreUnavailable("k", "PermissionError: [Errno 13]"), "StoreUnavailable PermissionError"),
        (StoreUnavailable("k", "a sentence of the store's own"), "StoreUnavailable"),
        (StoreUnavailable("k", ""), "StoreUnavailable"),
        (StoreUnavailable("k", "x" * 41), "StoreUnavailable"),
        (PermissionError("the store's own text"), "PermissionError"),
    ],
)
def test_a_reason_names_a_stores_code_and_none_of_its_text(error: Exception, said: str) -> None:
    assert code_of(error) == said
