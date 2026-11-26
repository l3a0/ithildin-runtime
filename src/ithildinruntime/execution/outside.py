"""What the journal takes from a read of the broker that the platform did not cause.

A broker moves cash on its own, with a fee, debit interest or a dividend, and an
order or fill can reach the account that the platform never sent. Each function
here is handed a view and one thing the broker listed, and says which entry it
becomes, or `None` where the view holds it already.

1. **A record the broker booked.** A fee, debit interest or a dividend inside the
   range `records.toml` writes for its kind is an `outside_record`, which moves
   the book's cash. Any other kind, an amount outside its range, an amount with
   more places than cash declares, and a repeat of a held id with other content
   is an `unmatched_record`, kept as it came. An identical repeat is skipped.
2. **A fill.** One the journal can apply to an order it sent is a `fill`, and a
   repeat that differs is a `disputed_fill`, as before. Any other is an
   `unmatched_fill`, kept as it came.
3. **An open order.** One under a broker's id no `ack` mapped is an
   `unmatched_order`, kept as it came.

An unmatched entry moves nothing and holds the account, because the book halts
while one stands, until a person releases it. Nothing yet releases one. An order
that may be with the broker unmapped, one `Sending` or `InDoubt`, could own the
fill or the order, so while one stands nothing is kept as unmatched and the read
halts instead.

What a person reads names a record, fill or order by an id the journal takes,
an instrument where it is a name and a client order id where it is a stamp of
the platform's, and holds no other text of the broker's.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from functools import cache
from pathlib import Path
from types import MappingProxyType

from ithildinruntime.core import (
    BrokerOrder,
    BrokerRecord,
    Fill,
    Halt,
    RecordKind,
    broker_id_shown,
    is_broker_id,
    read_order_id,
    shows_instrument,
)
from ithildinruntime.journal import (
    DOUBTFUL,
    EXPLAINING,
    DisputedFillEntry,
    FillEntry,
    OutsideRecordEntry,
    Repeat,
    UnmatchedFillEntry,
    UnmatchedOrderEntry,
    UnmatchedRecordEntry,
    View,
)

__all__ = ["RANGES", "Range", "fill_entry", "load_ranges", "order_entry", "record_entry"]

# The tracked ranges, which ship inside the package.
RANGES = Path(__file__).parent / "records.toml"
_FORMAT = 1
# The places a cash amount declares, so a record with more is kept rather than summed.
_MONEY_PLACES = 4


@dataclass(frozen=True)
class Range:
    """The least and the most a kind of record may move the cash by, a debit below zero."""

    least: Decimal
    most: Decimal

    def holds(self, amount: Decimal) -> bool:
        return amount != 0 and self.least <= amount <= self.most


@cache
def load_ranges(path: Path = RANGES) -> Mapping[RecordKind, Range]:
    """Each kind on the closed list and its range, or a halt naming what is wrong with the file.

    The file names exactly the kinds the journal lets move cash, so a kind
    added to one and not the other halts rather than reading as no range.
    """
    where = path.name
    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise Halt(f"{where} cannot be read: {type(error).__name__}") from None
    if parsed.get("format") != _FORMAT:
        raise Halt(f"{where} is not of format {_FORMAT}")
    named = {key for key in parsed if key != "format"}
    wanted = {kind.value for kind in EXPLAINING}
    if named != wanted:
        raise Halt(
            f"{where} names {', '.join(sorted(named)) or 'no kind'}, and the kinds that move cash "
            f"are {', '.join(sorted(wanted))}"
        )
    ranges: dict[RecordKind, Range] = {}
    for kind in sorted(EXPLAINING):
        table = parsed[kind.value]
        if not isinstance(table, dict) or set(table) != {"least", "most"}:
            raise Halt(f"{where}'s {kind} holds other than a least and a most")
        least, most = (_amount(where, kind, table[end]) for end in ("least", "most"))
        if least > most:
            raise Halt(f"{where}'s {kind} has a least of {least}, above its most of {most}")
        ranges[kind] = Range(least, most)
    return MappingProxyType(ranges)


def _amount(where: str, kind: RecordKind, value: object) -> Decimal:
    if isinstance(value, str):
        try:
            amount = Decimal(value)
        except InvalidOperation:
            amount = None
        if amount is not None and amount.is_finite() and -amount.as_tuple().exponent <= 2:
            return amount
    raise Halt(f"{where}'s {kind} holds a bound that is no amount of dollars and cents in quotes")


def record_entry(
    view: View, record: BrokerRecord, read: int, ranges: Mapping[RecordKind, Range]
) -> OutsideRecordEntry | UnmatchedRecordEntry | None:
    """The entry a record the broker booked comes to, or `None` where the view holds it."""
    name = record.record_id
    if not is_broker_id(name):
        # No entry can be keyed by it, so it is kept by the stored read alone.
        raise Halt("the broker lists a record whose id the journal refuses")
    repeat = view.record_repeats(record)
    if repeat is Repeat.HELD:
        return None
    what = f"the record {name}, {record.amount} as a {record.kind.value}"
    if repeat is Repeat.DIFFERS:
        return UnmatchedRecordEntry(
            record, read, f"{what}, repeats an id the journal holds with other content"
        )
    if record.kind not in EXPLAINING:
        return UnmatchedRecordEntry(record, read, f"{what}, is no fee, debit interest or dividend")
    bounds = ranges[record.kind]
    if not bounds.holds(record.amount):
        return UnmatchedRecordEntry(
            record, read, f"{what}, is outside its range of {bounds.least} to {bounds.most}"
        )
    if record.excess_places or -record.amount.as_tuple().exponent > _MONEY_PLACES:  # type: ignore[operator]
        return UnmatchedRecordEntry(record, read, f"{what}, carries more places than cash holds")
    if record.instrument is not None and not shows_instrument(record.instrument):
        return UnmatchedRecordEntry(record, read, f"{what}, names an instrument whose name is none")
    return OutsideRecordEntry(record, read)


def fill_entry(
    view: View, fill: Fill, read: int
) -> FillEntry | DisputedFillEntry | UnmatchedFillEntry | None:
    """The entry a fill the broker lists comes to, or `None` where the view holds it."""
    if not is_broker_id(fill.broker_order_id):
        # No entry can be keyed by it, so it is kept by the stored read alone.
        raise Halt(
            f"the fill {broker_id_shown(fill.fill_id)} is of the broker's order "
            f"{broker_id_shown(fill.broker_order_id)}, which no order of the platform's was "
            "acknowledged under"
        )
    if not is_broker_id(fill.fill_id):
        # Its bounds refuse it as a fill, naming the id, and it is stored nowhere.
        return FillEntry(fill, read)
    repeat = view.repeats(fill)
    if repeat is Repeat.HELD:
        return None
    if repeat is Repeat.DIFFERS:
        return DisputedFillEntry(fill, read)
    kept = view.unmatched_repeats("fill", fill.fill_id, fill)
    if kept is Repeat.HELD:
        return None
    if kept is Repeat.DIFFERS:
        reason = f"the fill {fill.fill_id} repeats a fill kept as unmatched, with other content"
        return UnmatchedFillEntry(fill, read, _unless_doubtful(view, reason))
    why = view.unowned(fill)
    if why is None:
        return FillEntry(fill, read)
    return UnmatchedFillEntry(fill, read, _unless_doubtful(view, f"the {why}"))


def order_entry(view: View, order: BrokerOrder, read: int) -> UnmatchedOrderEntry | None:
    """The entry an open order under an id no `ack` mapped comes to, or `None`.

    An order the journal mapped, and one whose id the journal refuses, is left
    to the comparison of open orders, which names it.
    """
    name = order.broker_order_id
    if not is_broker_id(name) or view.acknowledged(name) is not None:
        return None
    if view.unmatched_repeats("order", name, order) is Repeat.HELD:
        return None
    instrument = order.instrument if shows_instrument(order.instrument) else "an instrument"
    reason = (
        f"the broker lists its order {name} open, {order.qty} of {instrument} under "
        f"{_client(order.client_order_id)}, which no ack mapped"
    )
    return UnmatchedOrderEntry(order, read, _unless_doubtful(view, reason))


def _unless_doubtful(view: View, reason: str) -> str:
    """The reason, or a halt where an order that may be with the broker could own the thing."""
    doubtful = [name for name, order in view.live.items() if order.state in DOUBTFUL]
    if doubtful:
        raise Halt(
            f"{reason}, and it is not kept as unmatched while the order {doubtful[0]} may be "
            "with the broker under no id the journal holds"
        )
    return reason


def _client(value: str | None) -> str:
    if value is None:
        return "no client order id"
    try:
        read_order_id(value)
    except (Halt, TypeError):
        return "a client order id that is no stamp of the platform's"
    return value
