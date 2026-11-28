"""Each kind of alarm the receive loop raises.

`core.alarms` says what a `Kind` carries, and `tests/test_alarms.py` checks
each row it names. The loop scopes each cause by its component and leaves out
the instance, so one fault on every instance of a component pages once, and a
queue that two components each lose pages twice. A cause scopes by the item too
where two items' faults must page apart. A kind that goes with a halt or a held
capture says who clears by the mode.
"""

from __future__ import annotations

from ithildinruntime.core import EXPLAIN, Clears, Kind

__all__ = [
    "COULD_NOT_FINISH",
    "HALT_DROPPED",
    "HALT_UNSTORED",
    "HOLDING",
    "LOOP_STOPPED",
    "QUEUE_SILENT",
    "UNHANDLED",
]

_OPERATIONS = "Alarms on operations"

UNHANDLED = Kind(
    "loop.unhandled",
    _OPERATIONS,
    "A handler failed with an error it did not handle",
    EXPLAIN,
    Clears.BY_MODE,
)
COULD_NOT_FINISH = Kind("loop.could-not-finish", _OPERATIONS, "A worker could not finish an item")
LOOP_STOPPED = Kind("loop.stopped", _OPERATIONS, "A worker stopped receiving")
QUEUE_SILENT = Kind("loop.queue-silent", _OPERATIONS, "A worker stopped receiving")
HOLDING = Kind(
    "loop.holding",
    _OPERATIONS,
    "A worker stopped receiving and holds a halt or a capture S3 has not stored",
    clears=Clears.BY_MODE,
)
HALT_UNSTORED = Kind(
    "loop.halt-unstored",
    _OPERATIONS,
    "A worker could not store a halt",
    EXPLAIN,
    Clears.BY_MODE,
)
HALT_DROPPED = Kind(
    "loop.halt-dropped",
    _OPERATIONS,
    "A halt a worker held was answered by S3 and not stored",
    EXPLAIN,
    Clears.BY_MODE,
)
