"""The bounds on the machine's clock, and the one rule that halts on a reading of it.

docs/design.md's `### Time comes from one clock, and order from sequence
numbers` says why: the machine's offset from reference time and the age of its
last answer are readings of the clock, and either past its bound halts trading.
Every component that judges a reading calls `clock_faults`.
"""

from __future__ import annotations

from datetime import timedelta

from .records import ClockReading, TimeAnswer

__all__ = ["ANSWER_BOUND", "CLOCK_BOUND", "DRIFT_PPM", "SYNC_BOUND", "clock_faults"]

# How far the machine's clock may be from a server's time, its error included. It
# bounds one clock, so a time measured between two stamps, or between two
# machines, can be off by twice it.
CLOCK_BOUND = timedelta(seconds=1)
# How old a server's last answer may be. It is proposed, not ruled on.
SYNC_BOUND = timedelta(seconds=300)
# An answer less sure than this says too little to judge by, unless it proves a fault.
ANSWER_BOUND = CLOCK_BOUND / 4
# How far Linux lets a clock drift, in parts per million.
DRIFT_PPM = 500

_MICROSECOND = timedelta(microseconds=1)


def clock_faults(reading: ClockReading, *, measured: bool) -> tuple[str, ...]:
    """Each server's first fault, in the reading's order. A reading at a bound has none.

    Every server has to be fresh and within the bound, so there is no vote for a
    forged or broken answer to win. A history clock measures nothing, so a
    reading with no answers is one fault where the run measures its clock, and
    none where `measured` is false.
    """
    if reading.answers is None:
        return ("no time server was asked",) if measured else ()
    return tuple(fault for answer in reading.answers if (fault := _fault(answer)) is not None)


def _fault(answer: TimeAnswer) -> str | None:
    offset, error, age = answer.offset_us, answer.error_us, answer.age_us
    if offset is None or error is None or age is None:
        return (
            f"time server {answer.server} has given no answer yet: "
            f"{answer.failed or 'no ask has ended'}"
        )
    if age > SYNC_BOUND // _MICROSECOND:
        return (
            f"time server {answer.server}'s last answer is {_seconds(age)} seconds old, past the "
            f"bound of {_shown(SYNC_BOUND)}: {answer.failed or 'no ask has ended since'}"
        )
    if abs(offset) + error > CLOCK_BOUND // _MICROSECOND:
        side = "slow against" if offset < 0 else "fast against" if offset > 0 else "from"
        return (
            f"the clock is {_seconds(abs(offset))} seconds {side} time server "
            f"{answer.server}, give or take {_seconds(error)}, past the bound of "
            f"{_shown(CLOCK_BOUND)}"
        )
    return None


def _shown(bound: timedelta) -> str:
    return f"{_seconds(bound // _MICROSECOND)} seconds"


def _seconds(us: int) -> str:
    return f"{us // 10**6}.{us % 10**6:06d}"
