"""How long execution waits on a store that does not answer.

`core.Storage` keeps the same rule for the blobs it reads and writes. A journal
entry and a control are not among them, because an entry is resent unchanged by
its own writer, and a control that cannot be read stops an order rather than a
decision. So execution waits here, calling `report` between tries, and never
past the last moment an order may go out.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from ithildinruntime.core import ChecksumMismatch, Clock, Retry, StoreUnavailable

__all__ = ["Patience"]


class Patience:
    """One account's tries, under one `Retry`."""

    def __init__(self, clock: Clock, retry: Retry, pause: Callable[[float], None]) -> None:
        self._clock = clock
        self._retry = retry
        self._pause = pause

    def tried[R](
        self, call: Callable[[], R], report: Callable[[], None], until: datetime | None = None
    ) -> R:
        """`call`'s answer, tried again while the store does not answer.

        A call is always tried once. The tries end at the retry's deadline, or
        at `until` where that comes first, and the store's last refusal is then
        raised as it came.
        """
        limit = self._retry.deadline.total_seconds()
        if until is not None:
            limit = min(limit, (until - self._clock.now()).total_seconds())
        start = self._clock.monotonic()
        pause = self._retry.pause
        while True:
            try:
                return call()
            except (StoreUnavailable, ChecksumMismatch):
                left = limit - (self._clock.monotonic() - start)
                if left <= 0:
                    raise
            report()
            self._pause(min(pause, left))
            pause = min(pause * 2, self._retry.longest_pause)
