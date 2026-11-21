"""The `Halt` exception, which stops a decision rather than guessing.

`Halt` is a plain `Exception` subclass rather than a dataclass. A frozen
dataclass exception raised inside a `@contextmanager` block comes out as
`FrozenInstanceError`, because the context manager sets its traceback, and no
`except Halt` catches that. At a worker's edge a `Halt` becomes a
`DecisionResult` holding a `HaltRecord`.
"""

__all__ = ["Halt"]


class Halt(Exception):
    """Stop the decision. `reason` is what the journal records and the alarm sends."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
