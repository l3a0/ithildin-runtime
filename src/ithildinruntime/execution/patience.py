"""How long execution waits on a store that does not answer.

`core.Storage` keeps the same rule for the blobs it reads and writes. A journal
entry and a control are not among them, because an entry is resent unchanged by
its own writer, and a control that cannot be read stops an order rather than a
decision. So execution waits here, calling `report` after each call and between
tries, and never past the last moment an order may go out.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from ithildinruntime.core import (
    ChecksumMismatch,
    Clock,
    Retry,
    StoreError,
    StoreUnavailable,
    error_name,
)

__all__ = ["Patience", "Tries", "code_of"]

_CODE = re.compile(r"[A-Za-z0-9_]{1,40}( [A-Za-z0-9_]{1,40})?")


def code_of(error: BaseException) -> str:
    """An error as a reason names it: its type, and a store's code where it gave one.

    A store's code is what stands before the first colon of its answer, such as
    `403 AccessDenied`, which tells a denied grant from an outage. The rest of
    the answer is the store's own text and is left out.
    """
    name = error_name(error)
    code = error.answer.partition(":")[0] if isinstance(error, StoreError) else ""
    return f"{name} {code}" if _CODE.fullmatch(code) else name


class Patience:
    """One account's tries, under one `Retry`."""

    def __init__(self, clock: Clock, retry: Retry, pause: Callable[[float], None]) -> None:
        self._clock = clock
        self._retry = retry
        self._pause = pause

    def tries(self, report: Callable[[], None], until: datetime | None = None) -> Tries:
        """Tries that end at the retry's deadline, or at `until` where that comes first."""
        limit = self._retry.deadline.total_seconds()
        if until is not None:
            limit = min(limit, (until - self._clock.now()).total_seconds())
        return Tries(self._clock, self._retry, self._pause, report, limit)


class Tries:
    """One deadline, and every call made under it.

    `waited` is whether any call has waited since it was last set false.
    """

    def __init__(
        self,
        clock: Clock,
        retry: Retry,
        pause: Callable[[float], None],
        report: Callable[[], None],
        limit: float,
    ) -> None:
        self._clock = clock
        self._retry = retry
        self._pause = pause
        self._report = report
        self._ends = clock.monotonic() + limit
        self.waited = False

    @property
    def over(self) -> bool:
        return self._clock.monotonic() >= self._ends

    def __call__[R](self, call: Callable[[], R]) -> R:
        """`call`'s answer, tried again while the store does not answer.

        A call is always tried once, and the store's last refusal is raised as
        it came.
        """
        pause = self._retry.pause
        while True:
            try:
                return call()
            except (StoreUnavailable, ChecksumMismatch):
                left = self._ends - self._clock.monotonic()
                if left <= 0:
                    raise
            finally:
                self._report()
            self.waited = True
            self._pause(min(pause, left))
            pause = min(pause * 2, self._retry.longest_pause)
