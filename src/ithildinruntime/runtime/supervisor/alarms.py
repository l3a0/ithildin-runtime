"""Each kind of alarm the supervisor and its hold check raise, each scoped to its deployment.

`core.alarms` says what a `Kind` carries, and `tests/test_alarms.py` checks
each row it names. The supervisor knows its mode, so a caller passes who clears
by it where an alarm goes with a halt or a hold. An attempt and its outcome,
such as a start record that could not be read and the start it then left
unrecorded, are two kinds, so a pager never holds the outcome as a repeat.
"""

from __future__ import annotations

from ithildinruntime.core import EXPLAIN, STATUS, Clears, Kind

__all__ = [
    "HALT_STANDS",
    "HOLD_UNFINISHED",
    "PLAN_UNSTORED",
    "RELEASE_UNWRITTEN",
    "RUN_HALTED",
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
]

_SUPERVISOR = "Alarms of the supervisor and the commands"
_ORDER = "Alarms on the order path"
_START = "A start that could not be recorded"

STARTS_UNREAD = Kind("supervisor.starts-unread", _SUPERVISOR, _START)
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
