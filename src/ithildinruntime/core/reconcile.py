"""The one comparison of the platform's book with a broker's account.

docs/architecture/05-building-blocks.md's `##### One function lists the
differences from the broker` names its four callers, and each decides only what
to do with what it lists. It computes nothing it was not handed and reads no
clock.

What it compares, exactly and with no tolerance:

1. **Positions**, by instrument: the sum of every strategy's against the
   broker's. One the book does not hold is zero there, and one the broker does
   not list is missing, which agrees with a sum of zero alone.
2. **Open orders**, by the broker's order id, each in its instrument, signed
   shares, filled total and client order id. One on either side alone is a
   difference, and so is one the broker lists twice. A book's open orders carry
   no broker's id, so the caller hands the journal's records of them beside it.
   An open order with no record, or whose record holds no broker's id, such as
   one that is `Approved`, is on the book's side alone.
3. **Cash**.

A difference reaches a person, so its words hold none of a broker's text. An
instrument is shown where it is a name, a broker's order id where it is one the
journal takes, and a client order id only where it is a stamp of the platform's.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .halt import Halt
from .keys import read_order_id
from .names import is_name
from .records import AccountRead, Book, BrokerOrder, OrderRecord
from .valuing import position

__all__ = ["Compared", "Difference", "broker_id_shown", "reconcile"]

_ZERO = Decimal(0)
_BROKER_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}")
# The longest instrument a difference shows.
_SHOWN = 40


class Compared(StrEnum):
    """What a difference is in."""

    POSITION = "position"
    OPEN_ORDER = "open order"
    CASH = "cash"


@dataclass(frozen=True)
class Difference:
    """One thing the book and the broker do not agree on, with each side's.

    - `name` is the instrument, the broker's order id, or the client order id
      of an order the book holds under no broker's id. It is empty for cash.
    - `book` and `broker` are the shares, the order or the cash of each side,
      and `None` where that side holds none.
    - `fields` names each field of an order both sides hold that differs, and
      is `("listed twice",)` for one the broker lists more than once.
    """

    of: Compared
    name: str
    book: Decimal | OrderRecord | None
    broker: Decimal | BrokerOrder | None
    fields: tuple[str, ...] = ()

    def __str__(self) -> str:
        if self.of is Compared.CASH:
            return f"cash is {self.book} in the book and {self.broker} at the broker"
        if self.of is Compared.POSITION:
            held = "none" if self.broker is None else f"{self.broker}"
            return f"{_instrument(self.name)} is {self.book} in the book and {held} at the broker"
        ours = None if not isinstance(self.book, OrderRecord) else self.book
        theirs = None if not isinstance(self.broker, BrokerOrder) else self.broker
        if self.fields == (_TWICE,):
            return f"the broker lists its order {broker_id_shown(self.name)} more than once"
        if ours is not None and theirs is not None:
            parts = [f"{f} {_field(f, ours, theirs)}" for f in self.fields]
            return f"the order {_order(self.name, ours)} differs in {', '.join(parts)}"
        if ours is not None:
            return f"the order {_order(self.name, ours)} is open in the book alone, {ours.state}"
        if theirs is None:
            return f"the order {self.name} is open in the book and no record of it was handed in"
        return (
            f"the broker's order {broker_id_shown(self.name)}, {theirs.qty} of "
            f"{_instrument(theirs.instrument)} under {_client(theirs.client_order_id)}, is open "
            "at the broker alone"
        )


def reconcile(
    book: Book, orders: Iterable[OrderRecord], account: AccountRead
) -> tuple[Difference, ...]:
    """Every difference between a book, with the journal's records of its open orders, and a read.

    They come sorted: positions by instrument, open orders by name, then cash.
    The caller has checked that every part of the read is whole.
    """
    return (*_positions(book, account), *_open_orders(book, orders, account), *_cash(book, account))


def _positions(book: Book, account: AccountRead) -> list[Difference]:
    theirs = account.positions.positions
    names = {i for held in book.positions_by_strategy.values() for i in held} | set(theirs)
    found = []
    for name in sorted(names):
        ours, listed = position(book, name), theirs.get(name)
        if ours != (_ZERO if listed is None else listed):
            found.append(Difference(Compared.POSITION, name, ours, listed))
    return found


def _open_orders(
    book: Book, orders: Iterable[OrderRecord], account: AccountRead
) -> list[Difference]:
    records = {order.intent.client_order_id: order for order in orders}
    found = [
        Difference(Compared.OPEN_ORDER, o.client_order_id, None, None)
        for held in book.open_orders_by_strategy.values()
        for o in held
        if o.client_order_id not in records
    ]
    ours: dict[str, OrderRecord] = {}
    for name, order in records.items():
        if order.broker_order_id is None:
            found.append(Difference(Compared.OPEN_ORDER, name, order, None))
        else:
            ours[order.broker_order_id] = order
    listed: dict[str, list[BrokerOrder]] = {}
    for theirs in account.open_orders.orders:
        listed.setdefault(theirs.broker_order_id, []).append(theirs)
    for name in {*ours, *listed}:
        order, copies = ours.get(name), listed.get(name, [])
        first = copies[0] if copies else None
        fields: tuple[str, ...] = ()
        if len(copies) > 1:
            fields = (_TWICE,)
        elif order is not None and first is not None:
            fields = _differing(order, first)
            if not fields:
                continue
        found.append(Difference(Compared.OPEN_ORDER, name, order, first, fields))
    return sorted(found, key=lambda d: d.name)


def _cash(book: Book, account: AccountRead) -> list[Difference]:
    theirs = account.cash.cash
    return [] if book.cash == theirs else [Difference(Compared.CASH, "", book.cash, theirs)]


_TWICE = "listed twice"


def _differing(order: OrderRecord, copy: BrokerOrder) -> tuple[str, ...]:
    intent = order.intent
    pairs = (
        ("instrument", intent.instrument, copy.instrument),
        ("shares", intent.qty, copy.qty),
        ("filled", order.filled, copy.filled),
        ("client order id", intent.client_order_id, copy.client_order_id),
    )
    return tuple(name for name, one, other in pairs if one != other)


def _field(name: str, order: OrderRecord, copy: BrokerOrder) -> str:
    said = {
        "instrument": (order.intent.instrument, _instrument(copy.instrument)),
        "shares": (order.intent.qty, copy.qty),
        "filled": (order.filled, copy.filled),
        "client order id": (order.intent.client_order_id, _client(copy.client_order_id)),
    }.get(name)
    return "" if said is None else f"({said[0]} in the book and {said[1]} at the broker)"


def _order(name: str, order: OrderRecord) -> str:
    client = order.intent.client_order_id
    if order.broker_order_id is None:
        return client
    return f"{client}, the broker's {broker_id_shown(name)},"


def broker_id_shown(value: object) -> str:
    """A broker's order or fill id as a halt shows it, where it is one the journal takes."""
    if isinstance(value, str) and _BROKER_ID.fullmatch(value):
        return value
    return "whose id the journal refuses"


def _instrument(value: object) -> str:
    if is_name(value) and len(str(value)) <= _SHOWN:
        return str(value)
    return "an instrument whose name is none"


def _client(value: str | None) -> str:
    if value is None:
        return "no client order id"
    try:
        read_order_id(value)
    except (Halt, TypeError):
        return "a client order id that is no stamp of the platform's"
    return value
