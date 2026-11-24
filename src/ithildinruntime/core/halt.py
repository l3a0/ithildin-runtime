"""The exceptions that end a handler: `Halt` stops a decision, and `Stopped` leaves it for later.

`Halt` is a plain `Exception` subclass rather than a dataclass. A frozen
dataclass exception raised inside a `@contextmanager` block comes out as
`FrozenInstanceError`, because the context manager sets its traceback, and no
`except Halt` catches that. At a worker's edge a `Halt` becomes a
`DecisionResult` holding a `HaltRecord`.

`Stopped` derives from `BaseException`, as `KeyboardInterrupt` does, so an
`except Exception` inside a handler cannot turn a stop into a halt. A stop says
nothing about the decision, which runs again after the restart.
"""

__all__ = ["Halt", "Stopped"]


class Halt(Exception):
    """Stop the decision. `reason` is what the journal records and the alarm sends."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Stopped(BaseException):
    """End a handler that was waiting when its loop was asked to stop.

    The loop stores nothing for the decision and returns its message to the
    queue. A handler raises it only where it waits and has written nothing.
    """
