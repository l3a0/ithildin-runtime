"""Each kind of alarm the receive loop raises.

`core.alarms` says what a `Kind` carries, and `tests/test_alarms.py` checks
each row it names. A cause leaves out the worker's instance, so one fault on
every instance of a host pages once, and scopes by the item where two items'
faults must page apart.
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
)
LOOP_STOPPED = Kind("loop.stopped", _OPERATIONS, "A worker stopped receiving")
QUEUE_SILENT = Kind("loop.queue-silent", _OPERATIONS, "A worker stopped receiving")
HOLDING = Kind(
    "loop.holding",
    _OPERATIONS,
    "A worker stopped receiving and holds a halt or a capture S3 has not stored",
)
QUEUE_SILENT = Kind("loop.queue-silent", _OPERATIONS, "A worker stopped receiving")
HOLDING = Kind(
    "loop.holding",
    _OPERATIONS,
    "A worker stopped receiving and holds a halt or a capture S3 has not stored",
)
HOLDING = Kind(
    "loop.holding",
    _OPERATIONS,
    "A worker stopped receiving and holds a halt or a capture S3 has not stored",
)
HALT_UNSTORED = Kind("loop.halt-unstored", _OPERATIONS, "A worker could not store a halt")
HALT_DROPPED = Kind(
    "loop.halt-dropped", _OPERATIONS, "A halt a worker held was answered by S3 and not stored"
)
HALT_DROPPED = Kind(
    "loop.halt-dropped", _OPERATIONS, "A halt a worker held was answered by S3 and not stored"
)
