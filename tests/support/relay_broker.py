"""A broker between the order path and the simulator, for a case whose clock moves first.

The path reads the clock last before its one call, so an order that arrives after
its close needs the clock moved between the two. The relay forwards the one order
it is handed, once and unchanged, and reaches a `SimulatedBroker` alone.
"""

from __future__ import annotations

from collections.abc import Callable

from ithildinruntime.core import Ack, OrderIntent
from ithildinruntime.execution.brokers.simulated import SimulatedBroker

__all__ = ["RelayBroker"]


class RelayBroker:
    def __init__(self, to: SimulatedBroker, first: Callable[[], None]) -> None:
        if type(to) is not SimulatedBroker:
            raise TypeError("a relay reaches a simulated broker alone")
        self._to = to
        self._first = first
        self._went = False

    @property
    def alias(self) -> str:
        return self._to.alias

    def submit(self, intent: OrderIntent) -> Ack:
        if self._went:
            raise AssertionError("a relay forwards one order, once")
        self._went = True
        self._first()
        return self._to.submit(intent)
