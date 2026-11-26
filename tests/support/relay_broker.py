"""Brokers between the order path and the simulator, which reach a `SimulatedBroker` alone.

1. `RelayBroker`, for a case whose clock moves first. The path reads the clock
   last before its one call, so an order that arrives after its close needs the
   clock moved between the two. The relay forwards the one order it is handed,
   once and unchanged.
2. `CheckedBroker`, for a day of orders behind the fake broker's four refusals.
   Each submit is checked against the stored journal as `FakeBroker` checks it,
   and then reaches the simulator unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import NoReturn

from ithildinruntime.core import Ack, OrderIntent
from ithildinruntime.execution.brokers.simulated import SimulatedBroker
from tests.support.fake_broker import FakeBroker, Refused

__all__ = ["CheckedBroker", "RelayBroker"]


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


@dataclass(eq=False)
class CheckedBroker(FakeBroker):
    """The simulated broker behind the fake broker's four refusals.

    A submit the checks pass reaches the simulator unchanged. A refusal fails
    the test through tests/conftest.py's fixture. The account's five reads and
    a lookup are the simulator's, which are every call the order path makes of
    a broker today. The order list, the clock, a cancel and a search for an
    order are not forwarded, since no case here makes one. Each is kept as a
    refusal before it raises, so the fixture fails the test whatever catches
    the error.
    """

    to: SimulatedBroker | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        to = self.to
        if type(to) is not SimulatedBroker:
            raise TypeError("a checked broker reaches a simulated broker alone")
        self.answer = lambda intent, number: to.submit(intent)

    @property
    def _sim(self) -> SimulatedBroker:
        assert self.to is not None
        return self.to

    def positions(self):
        return self._sim.positions()

    def open_orders(self):
        return self._sim.open_orders()

    def account(self):
        return self._sim.account()

    def fills(self, since: datetime):
        return self._sim.fills(since)

    def activities(self, since: datetime):
        return self._sim.activities(since)

    def lookup(self, broker_order_id: str):
        return self._sim.lookup(broker_order_id)

    def orders(self, since: datetime):
        self._unforwarded("reads the order list")

    def clock(self) -> datetime:
        self._unforwarded("reads the clock")

    def find_order(self, intent: OrderIntent, since: datetime, mapped: object):
        self._unforwarded("searches for an order")

    def cancel(self, broker_order_id: str):
        self._unforwarded("sends a cancel")

    def _unforwarded(self, what: str) -> NoReturn:
        why = f"no case behind a checked broker {what}"
        self.refused.append(why)
        raise Refused(why)
