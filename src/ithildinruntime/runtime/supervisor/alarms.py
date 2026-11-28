"""Each kind of alarm the supervisor and its hold check raise, each scoped to its deployment.

`core.alarms` says what a `Kind` carries, and `tests/test_alarms.py` checks
each row it names.
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
    "START_UNRECORDED",
    "STOP_ASKED",
    "STOP_ASKED_BEFORE_HALT",
    "UNCLEAN_HOLD",
]

_SUPERVISOR = "Alarms of the supervisor and the commands"
_ORDER = "Alarms on the order path"

START_UNRECORDED = Kind(
    "supervisor.start-unrecorded", _SUPERVISOR, "A start that could not be recorded"
)
HOLD_UNFINISHED = Kind(
    "supervisor.hold-unfinished", _SUPERVISOR, "A hold check that could not finish"
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
RELEASE_UNWRITTEN = Kind(
    "supervisor.release-unwritten", _SUPERVISOR, "A release file that could not be written"
)
HALT_STANDS = Kind(
    "supervisor.halt-stands", _SUPERVISOR, "A halt that still stands", EXPLAIN, Clears.BY_MODE
)
STALL = Kind("supervisor.stall", _SUPERVISOR, "A stall")
STOP_ASKED = Kind("supervisor.stop-asked", _SUPERVISOR, "A stop asked before the run's stop")
RUN_HALTED = Kind(
    "supervisor.run-halted",
    _ORDER,
    "An order first journaled `InDoubt`, again once it is in doubt past its limit",
    EXPLAIN,
    Clears.BY_MODE,
)
STOP_ASKED = Kind("supervisor.stop-asked", _SUPERVISOR, "A stop asked before the run's stop")
RUN_HALTED = Kind(
    "supervisor.run-halted",
    _ORDER,
    "An order first journaled `InDoubt`, again once it is in doubt past its limit",
    EXPLAIN,
    Clears.BY_MODE,
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
    "supervisor.run-halt-unread", _SUPERVISOR, "A run's halt that could not be read"
)
