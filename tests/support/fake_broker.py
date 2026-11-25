"""A broker that answers as a test says, and fails the test when an order reaches it wrongly.

It refuses four things, on every test that uses it.

1. An order the stored journal holds no approval of.
2. One whose `sending` is not the newest entry stored about it.
3. One whose client order id it has had before.
4. One that differs in any field from the order its `decision` entry plans.

It reads the stored bytes itself, with `json` and no code of `journal`,
`core.codec` or `OrderPath`, because those are what it checks. The order path
turns whatever a broker raises into an order in doubt, so a refusal is kept in
`refused`, and tests/conftest.py fails the test at its end unless the test
took the refusals with `take_refusals`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from ithildinruntime.core import (
    Ack,
    BrokerOrder,
    BrokerState,
    CashRead,
    FillsRead,
    FoundOrder,
    OrderIntent,
    OrdersRead,
    Outcome,
    PositionsRead,
    ReceivedPage,
    RecordsRead,
    read_order_id,
)

__all__ = ["FakeBroker", "Refused", "check", "copy", "made", "raising", "says"]

# Every fake broker the running test built, which conftest.py checks and clears.
made: list[FakeBroker] = []


def check() -> None:
    """Raise where a fake broker the running test built refused an order."""
    refused = [why for broker in made for why in broker.refused]
    if refused:
        raise Refused("; ".join(refused))


class Refused(AssertionError):
    """An order that should never have reached a broker did."""


@dataclass(eq=False)
class FakeBroker:
    """A broker for the account `alias`. `stored` gives the blobs that are stored right now."""

    stored: Callable[[], Mapping[str, bytes]]
    alias: str
    at: datetime
    # What a submit answers, which may raise. The order's number from 1 is the second argument.
    answer: Callable[[OrderIntent, int], object] | None = None
    # What each read answers, by its name.
    reads: dict[str, Callable[[], object]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    had: list[OrderIntent] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        made.append(self)

    def take_refusals(self) -> list[str]:
        taken, self.refused = self.refused, []
        return taken

    # The one call that moves the account

    def submit(self, intent: OrderIntent) -> object:
        self.calls.append("submit")
        for why in self._wrong(intent):
            self.refused.append(why)
            raise Refused(why)
        self.had.append(intent)
        if self.answer is not None:
            return self.answer(intent, len(self.had))
        return accepted(intent, len(self.had), self.at)

    def _wrong(self, intent: OrderIntent) -> list[str]:
        name = intent.client_order_id
        bodies = [json.loads(data)["body"] for data in self._entries()]
        about = [b for b in bodies if b.get("client_order_id") == name or _planned_in(b, name)]
        wrong = []
        if not any(b["kind"] == "verdict" and b["verdict"]["approved"] for b in about):
            wrong.append(f"{name} has no approval in the journal")
        moving = [b["kind"] for b in about if b["kind"] != "decision"]
        if not moving or moving[-1] != "sending":
            wrong.append(f"{name} has no sending stored as its newest entry")
        if any(order.client_order_id == name for order in self.had):
            wrong.append(f"{name} has been here before")
        plans = [o for b in bodies if b["kind"] == "decision" for o in b["orders"]]
        if not any(_same(intent, plan) for plan in plans):
            wrong.append(f"{name} is not as its decision plans it")
        return wrong

    def _entries(self) -> list[bytes]:
        prefix = f"journal/{self.alias}/"
        stored = self.stored()
        return [stored[key] for key in sorted(stored) if key.startswith(prefix)]

    # The reads

    def _answer(self, name: str, default: object) -> object:
        self.calls.append(name)
        return self.reads[name]() if name in self.reads else default

    @property
    def _pages(self) -> tuple[ReceivedPage, ...]:
        return (ReceivedPage(b"[]", self.at),)

    def positions(self) -> object:
        return self._answer("positions", PositionsRead({}, self._pages, True))

    def open_orders(self) -> object:
        return self._answer("open_orders", OrdersRead((), self._pages, True))

    def account(self) -> object:
        return self._answer("account", CashRead(Decimal(5), self._pages, True))

    def fills(self, since: datetime) -> object:
        return self._answer("fills", FillsRead((), self._pages, True))

    def orders(self, since: datetime) -> object:
        return self._answer("orders", OrdersRead((), self._pages, True))

    def activities(self, since: datetime) -> object:
        return self._answer("activities", RecordsRead(self._pages, True))

    def lookup(self, broker_order_id: str) -> object:
        return self._answer("lookup", Ack(Outcome.NOT_FOUND, None, None, b"", self.at))

    def find_order(self, intent: OrderIntent, since: datetime, mapped: object) -> object:
        return self._answer("find_order", FoundOrder(Outcome.NOT_FOUND, (), True, b""))

    def cancel(self, broker_order_id: str) -> object:
        return self._answer("cancel", Ack(Outcome.NOT_FOUND, None, None, b"", self.at))

    def clock(self) -> datetime:
        self.calls.append("clock")
        return self.at


def accepted(intent: OrderIntent, number: int, at: datetime) -> Ack:
    raw = json.dumps({"id": f"b-{number}"}).encode()
    return Ack(Outcome.ACCEPTED, f"b-{number}", None, raw, at)


def _planned_in(body: dict, name: str) -> bool:
    return body["kind"] == "intent" and body["order"]["client_order_id"] == name


def _same(intent: OrderIntent, plan: dict) -> bool:
    return (
        plan["client_order_id"],
        plan["strategy"],
        plan["instrument"],
        Decimal(plan["qty"]),
        plan["type"],
        plan["time_in_force"],
    ) == (
        intent.client_order_id,
        intent.strategy,
        intent.instrument,
        intent.qty,
        intent.type.value,
        intent.time_in_force.value,
    )


def raising(error: BaseException) -> Callable[[OrderIntent, int], object]:
    def answer(intent: OrderIntent, number: int) -> object:
        raise error

    return answer


def copy(intent: OrderIntent, **changes: object) -> BrokerOrder:
    fields: dict[str, object] = {
        "broker_order_id": "b-1",
        "client_order_id": intent.client_order_id,
        "instrument": intent.instrument,
        "qty": intent.qty,
        "filled": Decimal(0),
        "type": intent.type,
        "time_in_force": intent.time_in_force,
        "state": BrokerState.WORKING,
        "submitted_at": intent_at(intent),
    }
    return BrokerOrder(**fields | changes)  # type: ignore[arg-type]


def says(outcome: Outcome, broker: str | None = None, raw: bytes = b"{}", **changes: object):
    def answer(intent: OrderIntent, number: int) -> Ack:
        found = copy(intent, **changes) if changes else None
        return Ack(outcome, broker, found, raw, intent_at(intent) + timedelta(seconds=1))

    return answer


def intent_at(intent: OrderIntent) -> datetime:
    """The decision an order's stamp names."""
    return read_order_id(intent.client_order_id)[1]
