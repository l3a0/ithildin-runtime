"""The exceptions that end a handler: `Halt` stops a decision, and `Stopped` leaves it for later.

`Halt` is a plain `Exception` subclass rather than a dataclass. A frozen
dataclass exception raised inside a `@contextmanager` block comes out as
`FrozenInstanceError`, because the context manager sets its traceback, and no
`except Halt` catches that. At a worker's edge a `Halt` becomes a
`DecisionResult` holding a `HaltRecord`.

`Outage` is a `Halt` on a store that did not answer, whose alarm has already
been tried. A caller that meets one stops its later reads and writes of the
store, since each would alarm on the same outage again. The order path still
tries three writes after one, as the class says.

`Stopped` derives from `BaseException`, as `KeyboardInterrupt` does, so an
`except Exception` inside a handler cannot turn a stop into a halt. A stop says
nothing about the decision, which a later check starts again where it finds no
result stored for it.
"""

__all__ = ["Halt", "Outage", "Stopped"]


class Halt(Exception):
    """Stop the decision. `reason` is what the journal records and the alarm sends."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Outage(Halt):
    """A halt on a store that did not answer, whose alarm has already gone.

    `Storage` raises one for S3, and the order path for the journal and the
    controls. A caller that meets one stops its other writes, since each would
    alarm on the same outage again. The order path still tries three writes after
    one. It tries the `abandoned` of an order whose `sending` stands once, with no
    page. It fires the loss stop with no reading on the claim's halt of a
    decision with no plan, since the call that claimed may have read the loss
    past its stop and died, and the latch holds a firing whose writes fail. It
    tries to release the account's lease, which is its own key. A
    release that fails then pages only where another handler refused it or the
    lease is gone, and one the store does not answer adds its words to the halt.
    A halt on an entry the journal did not take after a broker's answer is not
    one, because that answer is held only by the process that got it.
    """


class Stopped(BaseException):
    """End a handler that was waiting when its loop was asked to stop.

    When a stop was asked, the loop stores nothing for the decision and returns
    any message the handler holds to the queue. A clock's wait raises it, and a
    handler waits that way only before it has written anything.
    """
