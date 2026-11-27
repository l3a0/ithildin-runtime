"""Order paths broken on purpose, to show that a test sees each break.

Each one reaches a `FakeBroker` alone and refuses any other broker when it is
built. No other file may name the calls tests/test_order_sweep.py looks for, so
a broken path lives here or nowhere.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from ithildinruntime.core import OrderRecord, OrderState
from ithildinruntime.execution.order_path import OrderPath
from tests.support.fake_broker import FakeBroker

__all__ = [
    "BrokenPath",
    "DoubledPath",
    "DownAfterCallPath",
    "FlippedPath",
    "TwicePath",
    "UnapprovedPath",
    "UnsentPath",
]


class BrokenPath(OrderPath):
    def __init__(self, shared, broker, report) -> None:
        if type(broker) is not FakeBroker:
            raise TypeError("a broken path reaches a fake broker alone")
        super().__init__(shared, broker, report)


class UnapprovedPath(BrokenPath):
    """It sends a planned order with nothing journaled."""

    def send(self, client_order_id: str) -> OrderState:
        planned = self._planned(self._view(), client_order_id)
        self._broker.submit(planned.order)
        return OrderState.ACKNOWLEDGED


class UnsentPath(BrokenPath):
    """It sends an approved order with no `sending` stored."""

    def send(self, client_order_id: str) -> OrderState:
        self._approve(client_order_id)
        planned = self._planned(self._view(), client_order_id)
        self._broker.submit(planned.order)
        return OrderState.ACKNOWLEDGED


class TwicePath(BrokenPath):
    """It calls the broker a second time before it stores the first answer."""

    def _read(self, answer):
        self._broker.submit(answer.order)
        return super()._read(answer)


class DownAfterCallPath(BrokenPath):
    """It meets a store that stops answering once the broker has been called."""

    def _submit(self, ack):
        super()._submit(ack)
        raise self._outage("a store stopped answering after the call")


class _ResizedPath(BrokenPath):
    by = Decimal(1)

    def _just_sending(self, view, cleared, tries) -> OrderRecord:
        order = super()._just_sending(view, cleared, tries)
        return replace(order, intent=replace(order.intent, qty=order.intent.qty * self.by))


class DoubledPath(_ResizedPath):
    by = Decimal(2)


class FlippedPath(_ResizedPath):
    by = Decimal(-1)


class HandedPath(BrokenPath):
    """It hands its private submit whatever acknowledgement a test gives it."""

    def handed(self, ack) -> None:
        self._submit(ack)

    def sending(self, client_order_id: str):
        """Go as far as a stored `sending`, and return its acknowledgement."""
        return self._sending(client_order_id, self._approve(client_order_id))
