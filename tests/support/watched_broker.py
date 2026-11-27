"""The simulated broker's submit, watched by a test, and called through once per call.

A test that must see what the platform asks of the broker, or must break the
journal right after the broker took an order, wraps the submit here. No other
file may name the calls tests/test_order_sweep.py looks for, so the wrapper
lives here or nowhere.
"""

from __future__ import annotations

from collections.abc import Callable

from ithildinruntime.core import Ack, OrderIntent
from ithildinruntime.execution.brokers.simulated import SimulatedBroker

__all__ = ["watch_submits"]


def watch_submits(
    monkeypatch,
    before: Callable[[OrderIntent], None] = lambda intent: None,
    after: Callable[[OrderIntent], None] = lambda intent: None,
) -> None:
    """Call `before` ahead of each submit the simulated broker gets, and `after` once it answers."""
    real = SimulatedBroker.submit

    def submit(self: SimulatedBroker, intent: OrderIntent) -> Ack:
        before(intent)
        answer = real(self, intent)
        after(intent)
        return answer

    monkeypatch.setattr(SimulatedBroker, "submit", submit)
