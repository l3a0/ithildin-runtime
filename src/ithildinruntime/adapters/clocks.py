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
   heartbeat when a busy handler reports too rarely. Each step waits on
   `report.wait_for_stop`, so a stop ends the wait at once with `Stopped`
   rather than at the decision time, which can be hours away. A test fakes
   the timer and the `report`.

The machine's offset from reference time and the age of its last sync are
readings of the clock, under rule 2 of design.md's `### Time comes from one
clock, and order from sequence numbers`. Both come from one call, `reading`,
because two calls could straddle an update. The wall clock hands through what
its `TimeServers` last measured. Under the history clock the time is the
decision's by definition, so nothing is measured, unless it is handed the
readings a recorded session took.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from ithildinruntime.core import ClockReading, Halt, Progress, Stopped, Timer

from .time_servers import TimeServers

__all__ = ["HistoryClock", "SystemTimer", "WallClock"]


class HistoryClock:
    """The backtest's clock, which never waits and reads the decision being worked on.

    `readings` is `None`, where nothing is measured, or each decision time's
    recorded `ClockReading`. It has no default, so a replay cannot forget it.
    """

    def __init__(self, *, readings: Mapping[datetime, ClockReading] | None) -> None:
        self._readings = None if readings is None else dict(readings)
        self._local = threading.local()

    def now(self) -> datetime:
        at = getattr(self._local, "at", None)
        if at is None:
            raise RuntimeError("the history clock reads a time only once this thread waited")
        return at

    def monotonic(self) -> float:
        return time.monotonic()

    def reading(self) -> ClockReading:
        at = self.now()
        if self._readings is None:
            return ClockReading(at, None)
        if at not in self._readings:
            raise Halt(f"no reading of the clock is recorded for the decision at {at.isoformat()}")
        return self._readings[at]

    def wait_until(self, at: datetime, report: Progress) -> None:
        _utc(at)
        self._local.at = at


class WallClock:
    """The clock shadow, paper and live run on, read through a timer a test can fake.

    `servers` is `None` for a command that reads the time and trades nothing,
    whose reading then holds no answers. It has no default.
    """

    def __init__(self, timer: Timer, *, servers: TimeServers | None, interval: float = 5.0) -> None:
        if interval <= 0:
            raise ValueError("a wall clock reports on a positive interval")
        self._timer = timer
        self._servers = servers
        self._interval = interval

    def now(self) -> datetime:
        return self._timer.now()

    def monotonic(self) -> float:
        return self._timer.monotonic()

    def reading(self) -> ClockReading:
        if self._servers is None:
            return ClockReading(self.now(), None)
        return self._servers.reading()

    def wait_until(self, at: datetime, report: Progress) -> None:
        _utc(at)
        while True:
            left = (at - self._timer.now()).total_seconds()
            if left <= 0:
                return
            report()
            if report.wait_for_stop(min(left, self._interval)):
                raise Stopped(f"asked to stop while waiting until {at.isoformat()}")


class SystemTimer:
    """The machine's own time: UTC wall time, and Python's monotonic reading for every limit."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def after(self, seconds: float) -> object:
        raise NotImplementedError("no caller asks the timer for an event yet")


def _utc(at: datetime) -> None:
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise ValueError(f"a clock waits until a UTC time, not {at!r}")
