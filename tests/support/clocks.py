"""Readings of the clock a test hands in, and clocks it sets by hand."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ithildinruntime.adapters import AskFailed, Exchange, TimeServer, TimeServers
from ithildinruntime.core import ClockReading, TimeAnswer

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
START = datetime(2012, 4, 9, 20, 0, tzinfo=UTC)
NEAR, FAR = TWO = (TimeServer("near", 16), TimeServer("far", 64))
SENT = bytes(range(1, 9))


def micro(us: int) -> timedelta:
    return timedelta(microseconds=us)


def answer(server: str = "near", **changes: object) -> TimeAnswer:
    fields: dict[str, object] = {
        "server": server,
        "offset_us": -7,
        "error_us": 11,
        "age_us": 13,
        "received": "24" * 48,
        "failed": None,
    }
    return TimeAnswer(**fields | changes)  # type: ignore[arg-type]


def good(at: datetime = START) -> ClockReading:
    return ClockReading(at, (answer("near"), answer("far", offset_us=5)))


def slow(at: datetime = START) -> ClockReading:
    return ClockReading(at, (answer("near", offset_us=-2_000_000), answer("far")))


class Hands:
    """A wall and a raw clock, in microseconds, that move apart only when a case says.

    Each read moves both on by `step_us` once it is taken.
    """

    def __init__(self, wall: datetime = START, raw_us: int = 5_000_000, step_us: int = 0) -> None:
        self.wall = wall
        self.raw_us = raw_us
        self.step_us = step_us
        self.calls: list[str] = []

    def pass_us(self, us: int) -> None:
        self.wall += micro(us)
        self.raw_us += us

    def now(self) -> datetime:
        at = self.wall
        self.calls.append("now")
        self.pass_us(self.step_us)
        return at

    def raw(self) -> int:
        at = self.raw_us
        self.calls.append("raw")
        self.pass_us(self.step_us)
        return at


def sampler(hands: Hands, ask, **more) -> TimeServers:  # type: ignore[no-untyped-def]
    return TimeServers(TWO, now=hands.now, raw=hands.raw, ask=ask, **more)


def never_asked(_: object = None) -> TimeServers:
    """A sampler no case starts, whose ask would fail."""

    def refuse(server: TimeServer, **_: object) -> Exchange:
        raise AskFailed("no network in a test")

    return sampler(Hands(), refuse)
