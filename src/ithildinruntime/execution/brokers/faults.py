"""How the simulated broker fails on purpose: a value its constructor is handed.

No fault holds randomness or a canned answer. One changes an order as it enters,
or what the account does with it, and the adapter's own code answers. It is read
whenever the account handles what it names, so a fault that has to hold across
two processes is handed to both.

A count is of the calls made on one `SimulatedAccount` in one process, from 1.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType

from ithildinruntime.core import InstrumentId

__all__ = [
    "READS",
    "AccountEvent",
    "EditCash",
    "EditOrder",
    "EditPosition",
    "Faults",
    "FillPart",
    "ForeignOrder",
    "Late",
    "OrderFault",
    "ReadFault",
]

# The reads a fault can name. A search for an order reads `orders`, so it counts as one.
READS = frozenset({"positions", "open_orders", "account", "fills", "orders", "activities"})
# A lookup answers one order and no list, so it can fail and cannot stop early.
_FAILS_ONLY = frozenset({"lookup"})


class OrderFault(StrEnum):
    """What happens to one order."""

    REJECT = "reject"
    LOSE_REQUEST = "lose_request"
    LOSE_ANSWER = "lose_answer"
    FILL_NONE = "fill_none"
    REJECT_LATER = "reject_later"
    DUPLICATE = "duplicate"
    BAD_TYPE = "bad_type"
    BAD_QTY = "bad_qty"


@dataclass(frozen=True)
class Late:
    """The order has no answer and arrives at the `calls`th call after its own."""

    calls: int

    def __post_init__(self) -> None:
        _count(self.calls, "an order arrives late by")


@dataclass(frozen=True)
class FillPart:
    """The close fills `qty` shares of the order, a size, and the rest expires."""

    qty: Decimal

    def __post_init__(self) -> None:
        if not self.qty.is_finite() or self.qty <= 0:
            raise ValueError(f"a part of {self.qty} shares is not above zero")


class ReadFault(StrEnum):
    """What happens to one read. A short one leaves out its last row and is not whole."""

    FAILS = "read_fails"
    SHORT = "read_short"


@dataclass(frozen=True)
class EditCash:
    """Someone moves the account's cash by hand."""

    by: Decimal


@dataclass(frozen=True)
class EditPosition:
    """Someone moves a position by hand."""

    instrument: InstrumentId
    by: Decimal


@dataclass(frozen=True)
class EditOrder:
    """Someone changes an open order's shares by hand."""

    broker_order_id: str
    qty: Decimal


@dataclass(frozen=True)
class ForeignOrder:
    """Someone places a market-on-close order, which carries no stamp of the platform's."""

    instrument: InstrumentId
    qty: Decimal


AccountEvent = EditCash | EditPosition | EditOrder | ForeignOrder


@dataclass(frozen=True)
class Faults:
    """Every failure one simulated broker is to make, and none by default.

    - `orders` maps a client order id to what happens to that order.
    - `reads` maps a read's name and its count among that read's calls to its failure.
    - `events` maps a count of every call to what someone does to the account just before it.
    """

    orders: Mapping[str, OrderFault | Late | FillPart] = field(default_factory=dict)
    reads: Mapping[tuple[str, int], ReadFault] = field(default_factory=dict)
    events: Mapping[int, AccountEvent] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "orders", MappingProxyType(dict(self.orders)))
        object.__setattr__(self, "reads", MappingProxyType(dict(self.reads)))
        object.__setattr__(self, "events", MappingProxyType(dict(self.events)))
        for (read, count), fault in self.reads.items():
            _count(count, f"a fault of {read} is at")
            if read not in READS and not (read in _FAILS_ONLY and fault is ReadFault.FAILS):
                raise ValueError(f"no read {read!r} of the simulated broker can {fault}")
        for count in self.events:
            _count(count, "an account event is at")


def _count(value: object, what: str) -> None:
    if type(value) is not int or value < 1:
        raise ValueError(f"{what} {value!r}, not a count from 1")
