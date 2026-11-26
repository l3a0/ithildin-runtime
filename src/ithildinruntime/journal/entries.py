"""What a journal entry holds: one frozen dataclass per kind, and the way to a body and back.

docs/architecture/08-crosscutting-concepts.md's `#### What a journal entry's
body holds, and how a retry is recognised` lists the kinds and who writes
each. In short:

- A body is the record's canonical JSON object with two more members, `kind`
  and `writer`, so each decimal, time and byte string has the codec's one form
  and `JournalEntry.body` stays plain JSON values.
- A kind's fields never change, and neither do those of a `core` type inside
  one, an enum's members or a check its constructor makes, because any of them
  can make every old entry undecodable. A changed shape is a new kind under a
  new name, and every kind ever written stays readable. tests/fixtures/orders
  holds one committed entry per kind.
- A body that names no kind this release knows, or whose fields are not that
  kind's, halts. A skipped entry would be a book built on part of the record.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType

from ithildinruntime.core import (
    BlobRef,
    BrokerOrder,
    BrokerRecord,
    CodecError,
    Fill,
    Halt,
    JournalEntry,
    Money,
    OrderIntent,
    Quantity,
    RecordKind,
    StopState,
    TargetPortfolio,
    Verdict,
    decode,
    encode,
    is_broker_id,
    is_sha256,
    shows_instrument,
)

__all__ = [
    "EXPLAINING",
    "KINDS",
    "RAW_LIMIT",
    "AbandonedEntry",
    "AckEntry",
    "DecisionEntry",
    "DisputedFillEntry",
    "ExpiredEntry",
    "FillEntry",
    "InDoubtEntry",
    "IntentEntry",
    "OpeningEntry",
    "OutsideRecordEntry",
    "ReadEntry",
    "Record",
    "RejectedEntry",
    "SendingEntry",
    "UnmatchedFillEntry",
    "UnmatchedOrderEntry",
    "UnmatchedRecordEntry",
    "VerdictEntry",
    "body_of",
    "is_writer",
    "kind_of",
    "past_bounds",
    "record_of",
]

# The most bytes of a broker's answer an entry keeps. A longer one keeps its first part, with
# `cut` true.
RAW_LIMIT = 65_536
_WRITER = re.compile(r"[0-9a-f]{32}")
# How much of a value from outside a halt shows, since its reason is stored and sent.
_SHOWN = 80
# The kinds of a broker's own record that move the book's cash with no halt: the closed list.
# A kind may join it in a later release and never leave it, since every old entry still reads.
EXPLAINING = frozenset({RecordKind.FEE, RecordKind.INTEREST, RecordKind.DIVIDEND})


@dataclass(frozen=True)
class OpeningEntry:
    """The account's opening cash, flat. An account's entry 1 and no other."""

    account: str
    cash: Money


@dataclass(frozen=True)
class ReadEntry:
    """The stored file that holds a broker read, named before any value in it is used.

    `at` is when the read was made.
    """

    stage: str
    decision_at: datetime
    ref: BlobRef
    at: datetime


@dataclass(frozen=True)
class DecisionEntry:
    """The whole target of every strategy and the orders planned from it, in order.

    - `off` names each strategy that was off.
    - `inputs` maps `snapshot`, `constraints`, `valuation`, `targets` and each
      strategy's `forecast/<strategy>` and `schedule/<strategy>` to its file.
    - `read` is the number of the `read` entry it was planned on, and `synced`
      that of the entry it was synced through.
    """

    deployment: str
    run: str
    decision_at: datetime
    targets: tuple[TargetPortfolio, ...]
    orders: tuple[OrderIntent, ...]
    off: tuple[str, ...]
    inputs: Mapping[str, BlobRef]
    read: int
    synced: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "inputs", MappingProxyType(dict(self.inputs)))


@dataclass(frozen=True)
class IntentEntry:
    """One planned order, under its `decision` entry's number."""

    decision: int
    order: OrderIntent


@dataclass(frozen=True)
class VerdictEntry:
    """The gate's answer, and all it was handed but the order and the stress records.

    The verdict's `stress_move` is the one the check used, which a record can hold
    above the caps'.

    - `now` is the clock reading, `stop` the loss stop's state and
      `close_entry` the number of the entry of a close after a broker's trade,
      or `None`.
    - `book` is the number of the entry before this one, whose view holds the
      book and the order the gate was handed.
    - `caps` is the canonical bytes of the parsed `HardCaps`, and
      `caps_sha256` the SHA-256 of the blob it was parsed from. The owner
      rewrites that blob, so its values are kept here. They are bytes the fold
      never decodes, so a cap added later leaves old entries readable.
    """

    client_order_id: str
    verdict: Verdict
    now: datetime
    stop: StopState
    close_entry: int | None
    book: int
    caps: bytes
    caps_sha256: str
    snapshot: BlobRef
    constraints: BlobRef
    valuation: BlobRef


@dataclass(frozen=True)
class AbandonedEntry:
    """Nothing was sent, and why."""

    client_order_id: str
    reason: str


@dataclass(frozen=True)
class SendingEntry:
    """The order may leave now."""

    client_order_id: str


@dataclass(frozen=True)
class AckEntry:
    """The broker has the order, with the bytes it sent."""

    client_order_id: str
    broker_order_id: str
    raw: bytes
    cut: bool
    received_at: datetime


@dataclass(frozen=True)
class RejectedEntry:
    """The broker refused the order."""

    client_order_id: str
    reason: str
    raw: bytes
    cut: bool
    received_at: datetime


@dataclass(frozen=True)
class InDoubtEntry:
    """No answer that settles the order, or one this release does not read.

    `raw` is `None` where no plain bytes came.
    """

    client_order_id: str
    reason: str
    raw: bytes | None
    cut: bool
    received_at: datetime


@dataclass(frozen=True)
class FillEntry:
    """One fill, and the number of the `read` entry whose file holds it."""

    fill: Fill
    read: int


@dataclass(frozen=True)
class DisputedFillEntry:
    """A repeat of a fill whose content differs, kept beside the first."""

    fill: Fill
    read: int


@dataclass(frozen=True)
class ExpiredEntry:
    """The broker says the order ended with a part unfilled. `filled` is signed as fills are."""

    client_order_id: str
    filled: Quantity
    raw: bytes
    cut: bool
    received_at: datetime


@dataclass(frozen=True)
class OutsideRecordEntry:
    """A record the broker booked on its own, of a kind the closed list holds, which moves the cash.

    `read` is the number of the `read` entry whose file holds it.
    """

    record: BrokerRecord
    read: int


@dataclass(frozen=True)
class UnmatchedRecordEntry:
    """A record of the broker's that no rule lets reach the book, kept as it came.

    It moves nothing and holds the account. `reason` is the platform's own words
    for why, and holds none of the broker's.
    """

    record: BrokerRecord
    read: int
    reason: str


@dataclass(frozen=True)
class UnmatchedFillEntry:
    """A fill the journal cannot give to any order it sent, kept as it came.

    It moves nothing and holds the account. `reason` is the platform's own words.
    """

    fill: Fill
    read: int
    reason: str


@dataclass(frozen=True)
class UnmatchedOrderEntry:
    """An order the broker lists open under an id no `ack` mapped, kept as it came.

    It moves nothing and holds the account. `reason` is the platform's own words.
    """

    order: BrokerOrder
    read: int
    reason: str


Record = (
    OpeningEntry
    | ReadEntry
    | DecisionEntry
    | IntentEntry
    | VerdictEntry
    | AbandonedEntry
    | SendingEntry
    | AckEntry
    | RejectedEntry
    | InDoubtEntry
    | FillEntry
    | DisputedFillEntry
    | ExpiredEntry
    | OutsideRecordEntry
    | UnmatchedRecordEntry
    | UnmatchedFillEntry
    | UnmatchedOrderEntry
)

KINDS: Mapping[str, type] = MappingProxyType(
    {
        "opening": OpeningEntry,
        "read": ReadEntry,
        "decision": DecisionEntry,
        "intent": IntentEntry,
        "verdict": VerdictEntry,
        "abandoned": AbandonedEntry,
        "sending": SendingEntry,
        "ack": AckEntry,
        "rejected": RejectedEntry,
        "in_doubt": InDoubtEntry,
        "fill": FillEntry,
        "disputed_fill": DisputedFillEntry,
        "expired": ExpiredEntry,
        "outside_record": OutsideRecordEntry,
        "unmatched_record": UnmatchedRecordEntry,
        "unmatched_fill": UnmatchedFillEntry,
        "unmatched_order": UnmatchedOrderEntry,
    }
)
_NAMES: Mapping[type, str] = MappingProxyType({kind: name for name, kind in KINDS.items()})


def kind_of(record: object) -> str:
    """The name a record's kind is written under."""
    name = _NAMES.get(type(record))
    if name is None:
        raise TypeError(f"a {type(record).__name__} is no journal entry's record")
    return name


def is_writer(value: object) -> bool:
    """Whether `value` names a writer: 32 lower case hex characters, which say nothing else."""
    return isinstance(value, str) and _WRITER.fullmatch(value) is not None


def body_of(record: Record, writer: str) -> dict[str, object]:
    """A record as an entry's body: its canonical JSON object, its kind's name and its writer."""
    name = kind_of(record)
    return {**json.loads(encode(record)), "kind": name, "writer": writer}


def record_of(entry: JournalEntry) -> Record:
    """The record an entry's body holds, or a halt naming the entry and its kind."""
    body = dict(entry.body)
    name, writer = body.pop("kind", None), body.pop("writer", None)
    kind = KINDS.get(name) if isinstance(name, str) else None
    if kind is None:
        raise Halt(f"entry {entry.seq} is of the kind {name!r}, which this release does not read")
    if not is_writer(writer):
        raise Halt(f"entry {entry.seq}, of the kind {name}, has the writer {writer!r}, no writer")
    try:
        record = decode(encode(body), kind)
    except CodecError as error:
        raise Halt(f"entry {entry.seq} is not of its kind {name}: {error}") from None
    for past in _past_bounds(record):
        raise Halt(f"entry {entry.seq} is past a bound of its kind {name}: {past}")
    return record


def past_bounds(record: Record) -> tuple[str, ...]:
    """Each bound of its kind's fields that a record is past, which `record_of` halts on.

    A writer asks it before it builds an entry, so what the journal refuses is
    said in one place.
    """
    return tuple(_past_bounds(record))


def _past_bounds(record: Record) -> Iterator[str]:
    """Each bound of a kind's fields that this record is past."""
    raw = getattr(record, "raw", None)
    if raw is not None and len(raw) > RAW_LIMIT:
        yield f"raw has {len(raw)} bytes, more than {RAW_LIMIT}"
    match record:
        case AckEntry():
            yield from _broker_ids(("the broker's order id", record.broker_order_id))
        case FillEntry(fill):
            yield from _fill_ids(fill)
            if fill.price <= 0:
                yield f"the fill {fill.fill_id} has a price of {fill.price}, not above zero"
            if fill.qty == 0:
                yield f"the fill {fill.fill_id} is for zero shares"
            if fill.correction_id is not None:
                # Nothing of the broker's text is said. A person reads it in the stored read.
                yield f"the fill {fill.fill_id} carries a correction's id"
        case DisputedFillEntry(fill):
            # A dispute moves nothing, so it keeps what came, such as a fill
            # sent again for zero shares.
            yield from _fill_ids(fill)
        case OutsideRecordEntry(kept):
            yield from _broker_ids(("the record's id", kept.record_id))
            if kept.kind not in EXPLAINING:
                yield f"the record {kept.record_id} is a {kept.kind}, which no rule lets move cash"
            if kept.amount == 0:
                yield f"the record {kept.record_id} moves no cash"
            if kept.instrument is not None and not shows_instrument(kept.instrument):
                yield f"the record {kept.record_id} names an instrument whose name is none"
        case UnmatchedRecordEntry(kept):
            yield from _broker_ids(("the record's id", kept.record_id))
        case UnmatchedFillEntry(fill):
            # It moves nothing, so it keeps what came, as a dispute does.
            yield from _fill_ids(fill)
        case UnmatchedOrderEntry(order):
            yield from _broker_ids(("the broker's order id", order.broker_order_id))
        case ReadEntry():
            yield from _blobs(record.ref)
        case DecisionEntry():
            yield from _shares(*record.orders)
            yield from _blobs(*record.inputs.values())
        case IntentEntry():
            yield from _shares(record.order)
        case VerdictEntry():
            yield from _blobs(record.snapshot, record.constraints, record.valuation)
            if not record.caps or not is_sha256(record.caps_sha256):
                yield "the caps are empty, or their SHA-256 is none"
        case _:
            pass


def _fill_ids(fill: Fill) -> Iterator[str]:
    yield from _broker_ids(
        ("the fill's id", fill.fill_id), ("the broker's order id", fill.broker_order_id)
    )


def _broker_ids(*named: tuple[str, str]) -> Iterator[str]:
    # The id is not shown, since it could be any text of a broker's.
    for field, value in named:
        if not is_broker_id(value):
            yield f"{field} is not a broker's id"


def shown(value: str) -> str:
    """The start of a key, as a halt shows it."""
    return repr(value[:_SHOWN])


def named(instrument: object) -> str:
    """An instrument as a halt shows it, where it is a name and no other text."""
    if shows_instrument(instrument):
        return repr(instrument)
    return "an instrument whose name is none"


def _blobs(*refs: BlobRef) -> Iterator[str]:
    for ref in refs:
        if not ref.key or not is_sha256(ref.sha256):
            yield f"the key {shown(ref.key)} and SHA-256 {shown(ref.sha256)} name no blob"


def _shares(*orders: OrderIntent) -> Iterator[str]:
    for order in orders:
        if order.qty == 0:
            yield f"the order {order.client_order_id} is for zero shares"
