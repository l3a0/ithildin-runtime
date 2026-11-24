"""The two clocks a mode plugs in: the history clock and the wall clock.

docs/design.md's `## One runtime` names the clock as one of the three adapters
that change what a strategy sees. The data worker reads it to decide when a
decision's snapshot is taken, which is as fast as possible under the history
clock and at the decision time under the wall clock. Data holds no branch on
the mode, because both answer `wait_until(at, report)`.

1. **The history clock** returns from `wait_until` at once. Its `now` is the
   time this thread last waited until, which is the decision time a backtest
   handler is working on, so two runs of a backtest stamp the same read time.
   Handlers run side by side on a pool, so each thread keeps its own reading.
2. **The wall clock** waits on its timer, calling `report` at least every
   `interval` seconds while it waits, because the receive loop stops the
   heartbeat when a busy handler reports too rarely. A test fakes the timer
   and the sleep.

The machine's offset from reference time and the age of its last sync are
readings of the clock, under rule 2 of design.md's `### Time comes from one
clock, and order from sequence numbers`. No issue builds the time client that
reads them yet, so both clocks raise on those two reads rather than report a
value nothing measured.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from ithildinruntime.core import Timer

__all__ = ["HistoryClock", "WallClock"]

_NO_TIME_CLIENT = (
    "the clock's offset and sync age need a time client, which no issue builds yet, "
    "so this clock does not report them"
)


class HistoryClock:
    """The backtest's clock, which never waits and reads the decision being worked on."""

    def __init__(self) -> None:
        self._local = threading.local()

    def now(self) -> datetime:
        at = getattr(self._local, "at", None)
        if at is None:
            raise RuntimeError("the history clock reads a time only once this thread waited")
        return at

    def monotonic(self) -> float:
        return time.monotonic()

    def offset(self) -> timedelta:
        raise NotImplementedError(_NO_TIME_CLIENT)

    def sync_age(self) -> timedelta:
        raise NotImplementedError(_NO_TIME_CLIENT)

    def wait_until(self, at: datetime, report: Callable[[], None]) -> None:
        _utc(at)
        self._local.at = at


class WallClock:
    """The clock shadow, paper and live run on, read through a timer a test can fake."""

    def __init__(
        self,
        timer: Timer,
        *,
        sleep: Callable[[float], None] = time.sleep,
        interval: float = 5.0,
    ) -> None:
        if interval <= 0:
            raise ValueError("a wall clock reports on a positive interval")
        self._timer = timer
        self._sleep = sleep
        self._interval = interval

    def now(self) -> datetime:
        return self._timer.now()

    def monotonic(self) -> float:
        return self._timer.monotonic()

    def offset(self) -> timedelta:
        raise NotImplementedError(_NO_TIME_CLIENT)

    def sync_age(self) -> timedelta:
        raise NotImplementedError(_NO_TIME_CLIENT)

    def wait_until(self, at: datetime, report: Callable[[], None]) -> None:
        _utc(at)
        while True:
            left = (at - self._timer.now()).total_seconds()
            if left <= 0:
                return
            report()
            self._sleep(min(left, self._interval))


def _utc(at: datetime) -> None:
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise ValueError(f"a clock waits until a UTC time, not {at!r}")
