"""Each kind of alarm the supervisor and its hold check raise, each scoped to its deployment.

`core.alarms` says what a `Kind` carries, and `tests/test_alarms.py` checks
each row it names. The supervisor knows its mode, so every alarm it and its
hold check raise goes through `raised`, which turns a kind's "by the mode" into
the operator or the owner on paper and the owner alone on live. No call site
says who clears on its own. An attempt and its outcome,
such as a start record that could not be read and the start it then left
unrecorded, are two kinds, so a pager never holds the outcome as a repeat.
"""

from __future__ import annotations

from ithildinruntime.core import EXPLAIN, STATUS, Alarm, Clears, Kind

__all__ = [
    "HALT_STANDS",
    "HOLD_UNFINISHED",
    "PLAN_UNSTORED",
    "RELEASE_UNWRITTEN",
    "RUN_HALTED",
    "RUN_HALT_IN_PLACE",
    "RUN_HALT_STORED",
    "RUN_HALT_UNREAD",
    "RUN_HALT_UNSTORED",
    "STALL",
    "STARTS_UNREAD",
    "START_UNRECORDED",
    "START_UNWRITTEN",
    "STOP_ASKED",
    "STOP_ASKED_BEFORE_HALT",
    "UNCLEAN_HOLD",
    "raised",
]

_SUPERVISOR = "Alarms of the supervisor and the commands"
_ORDER = "Alarms on the order path"
_START = "A start that could not be recorded"

# The start records' read failing holds every strategy, which the hold's own alarm names.
STARTS_UNREAD = Kind("supervisor.starts-unread", _SUPERVISOR, _START, clears=Clears.BY_MODE)
START_UNWRITTEN = Kind("supervisor.start-unwritten", _SUPERVISOR, _START)
START_UNRECORDED = Kind("supervisor.start-unrecorded", _SUPERVISOR, _START)
HOLD_UNFINISHED = Kind(
    "supervisor.hold-unfinished",
    _SUPERVISOR,
    "A hold check that could not finish",
    clears=Clears.BY_MODE,
)
UNCLEAN_HOLD = Kind(
    "supervisor.unclean-hold",
    _ORDER,
    "The hold after a stop with no clean-shutdown record",
    EXPLAIN,
    Clears.BY_MODE,
)
PLAN_UNSTORED = Kind("supervisor.plan-unstored", _SUPERVISOR, "A plan that could not be stored")
RELEASE_UNWRITTEN = Kind(
    "supervisor.release-unwritten", _SUPERVISOR, "A release file that could not be written"
)
HALT_STANDS = Kind(
    "supervisor.halt-stands", _SUPERVISOR, "A halt that still stands", EXPLAIN, Clears.BY_MODE
)
STALL = Kind("supervisor.stall", _SUPERVISOR, "A stall", clears=Clears.BY_MODE)
STOP_ASKED = Kind(
    "supervisor.stop-asked",
    _SUPERVISOR,
    "A stop asked before the run's stop",
    clears=Clears.BY_MODE,
)
RUN_HALTED = Kind(
    "supervisor.run-halted",
    _ORDER,
    "An order first journaled `InDoubt`, again once it is in doubt past its limit",
    EXPLAIN,
    Clears.BY_MODE,
)
RUN_HALT_UNSTORED = Kind(
    "supervisor.run-halt-unstored",
    _SUPERVISOR,
    "A run's halt that could not be stored",
    STATUS,
    Clears.BY_MODE,
)
# The outcome of a run's halt that could not be stored: halts in its place, and an exit.
RUN_HALT_IN_PLACE = Kind(
    "supervisor.run-halt-in-place",
    _SUPERVISOR,
    "A run's halt that could not be stored",
    STATUS,
    Clears.BY_MODE,
)
STOP_ASKED_BEFORE_HALT = Kind(
    "supervisor.stop-asked-before-halt",
    _SUPERVISOR,
    "A run's halt that could not be stored",
    STATUS,
    Clears.BY_MODE,
)
RUN_HALT_STORED = Kind(
    "supervisor.run-halt-stored",
    _SUPERVISOR,
    "A run's halt that could not be stored",
    EXPLAIN,
    Clears.BY_MODE,
)
RUN_HALT_UNREAD = Kind(
    "supervisor.run-halt-unread",
    _SUPERVISOR,
    "A run's halt that could not be read",
    clears=Clears.BY_MODE,
)


def raised(kind: Kind, reason: str, *, deployment: str, live: bool) -> Alarm:
    """`kind`'s alarm, scoped to `deployment`, with who clears read from the mode where it can."""
    clears = Clears.on(live) if kind.clears is Clears.BY_MODE else None
    return kind(reason, scope=deployment, clears=clears)
