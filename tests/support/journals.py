"""Journals a test writes: a store in memory, one record of each kind, and seeded journals.

Every id, price and quantity is made up. A seeded journal walks `LIVES`, the
lives an order may lead, which are written here from #142's prose and share
nothing with the table in `journal/orders.py`.
"""

from __future__ import annotations

import hashlib
import random
from bisect import bisect_left, bisect_right, insort
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ithildinruntime.core import (
    BlobMissing,
    BlobRef,
    BrokerOrder,
    BrokerRecord,
    BrokerState,
    Fill,
    InstrumentId,
    KeyExists,
    Limit,
    LossReading,
    OrderIntent,
    OrderType,
    RecordKind,
    StaleTag,
    StopState,
    StoredBlob,
    TargetPortfolio,
    TimeInForce,
    Verdict,
    encode,
    order_id,
    sha256_of,
)
from ithildinruntime.journal import (
    AbandonedEntry,
    AckEntry,
    DecisionEntry,
    DisputedFillEntry,
    ExpiredEntry,
    FillEntry,
    InDoubtEntry,
    IntentEntry,
    Journal,
    LostEntry,
    OpeningEntry,
    OutsideRecordEntry,
    ReadEntry,
    Record,
    RejectedEntry,
    SendingEntry,
    UnmatchedFillEntry,
    UnmatchedOrderEntry,
    UnmatchedRecordEntry,
    VerdictEntry,
    View,
    body_of,
)
from ithildinruntime.risk import Gate
from tests.support import gates

ACCOUNT, WRITER = "paper-1", "0" * 32
AT = datetime(2026, 10, 1, 19, 40, tzinfo=UTC)
GLD, USO = InstrumentId("GLD"), InstrumentId("USO")
CHAN, OTHER = "chan_bollinger", "other-one.2"
# A day after `AT`, once every order planned at `AT` has had its close.
LATE = AT + timedelta(days=1)
REF = BlobRef("runs/d/2026-10-01/x/y.json", "a" * 64)
LOSS = LossReading(Decimal(1), Decimal(2), False, date(2026, 9, 30))


class Memory:
    """A `BlobStore` in memory that counts its calls, so thousands of entries cost no fsync."""

    def __init__(self, blobs: Mapping[str, bytes] | None = None) -> None:
        self.blobs = dict(blobs or {})
        self._keys = sorted(self.blobs)
        self.gets = self.lists = self.creates = 0

    def create(self, key: str, data: bytes, sha256: str) -> str:
        self.creates += 1
        if key in self.blobs:
            raise KeyExists(key, "the key already holds a blob")
        self.blobs[key] = data
        insort(self._keys, key)
        return _md5(data)

    def get(self, key: str) -> bytes:
        self.gets += 1
        try:
            return self.blobs[key]
        except KeyError:
            raise BlobMissing(key, "no blob at the key") from None

    def list(self, prefix: str, after: str | None) -> list[str]:
        self.lists += 1
        keys = self._keys
        start = bisect_left(keys, prefix) if after is None else bisect_right(keys, after)
        return [key for key in keys[start:] if key.startswith(prefix)]

    def head(self, key: str) -> StoredBlob:
        try:
            data = self.blobs[key]
        except KeyError:
            raise BlobMissing(key, "no blob at the key") from None
        return StoredBlob(BlobRef(key, sha256_of(data)), _md5(data), AT)

    def replace_if_match(self, key: str, data: bytes, sha256: str, etag: str) -> str:
        """Rewrite a blob only while it carries `etag`, whose tag is the MD5 of its bytes."""
        if key not in self.blobs:
            raise BlobMissing(key, "no blob at the key to rewrite")
        if _md5(self.blobs[key]) != etag:
            raise StaleTag(key, "the blob carries another tag")
        self.blobs[key] = data
        return _md5(data)


def _md5(data: bytes) -> str:
    # S3 gives a blob stored in one part the MD5 of its bytes as its tag.
    return f'"{hashlib.md5(data).hexdigest()}"'


def add(journal: Journal, record: Record) -> int:
    """Plant a record as the next entry, with none of the fold's checks."""
    record = placed(journal, record)
    return journal._append(journal._build(body_of(record, WRITER), AT)).seq


def placed(journal: Journal, record: Record) -> Record:
    """A verdict that names no book, on the entry before its own."""
    if isinstance(record, VerdictEntry) and record.book is None:
        head = journal.head()
        return replace(record, book=head.seq if head else 0)
    return record


def planned(
    place: int, qty: object, instrument: InstrumentId = GLD, strategy: str = CHAN, at: datetime = AT
) -> OrderIntent:
    return OrderIntent(
        order_id(strategy, at, place),
        strategy,
        instrument,
        Decimal(str(qty)),
        OrderType.MARKET,
        TimeInForce.DAY,
    )


def opening(cash: object = 100_000, account: str = ACCOUNT) -> OpeningEntry:
    return OpeningEntry(account, Decimal(str(cash)))


def read(at: datetime = AT) -> ReadEntry:
    return ReadEntry("execution/send", at, REF, at)


def decision(
    *orders: OrderIntent,
    at: datetime = AT,
    read: int = 2,
    synced: int | None = None,
    off: tuple[str, ...] = ("idle",),
) -> DecisionEntry:
    return DecisionEntry(
        deployment="paper",
        run=at.date().isoformat(),
        decision_at=at,
        targets=(TargetPortfolio(at, CHAN, {GLD: Decimal(1)}),),
        orders=orders,
        off=off,
        inputs={"snapshot": REF, f"forecast/{CHAN}": REF},
        read=read,
        synced=read if synced is None else synced,
    )


def verdict(name: str, approved: bool = True, book: int | None = None) -> VerdictEntry:
    limit = None if approved else Limit.IN_DOUBT
    answer = Verdict(approved, limit, None, None, None, True, Decimal(1), Decimal("0.2"), LOSS, ())
    return VerdictEntry(
        client_order_id=name,
        verdict=answer,
        now=AT,
        stop=StopState.ABSENT,
        close_entry=None,
        book=book,  # type: ignore[arg-type]
        caps=b"{}",
        caps_sha256="b" * 64,
        snapshot=REF,
        constraints=REF,
        valuation=REF,
    )


def abandoned(name: str) -> AbandonedEntry:
    return AbandonedEntry(name, "a check failed")


def ack(name: str, broker: str = "b1", raw: bytes = b'{"id":"b"}', cut: bool = False) -> AckEntry:
    return AckEntry(name, broker, raw, cut, AT)


def rejected(name: str, raw: bytes = b"\xff", cut: bool = False) -> RejectedEntry:
    return RejectedEntry(name, "no", raw, cut, AT)


def in_doubt(name: str, raw: bytes | None = None, cut: bool = False) -> InDoubtEntry:
    return InDoubtEntry(name, "no answer", raw, cut, AT)


def lost(name: str, read: int = 2) -> LostEntry:
    return LostEntry(name, read, "a whole list omits it")


def expired(name: str, filled: object, raw: bytes = b"{}", cut: bool = False) -> ExpiredEntry:
    return ExpiredEntry(name, Decimal(str(filled)), raw, cut, AT)


def fill(
    fill_id: str,
    broker_order_id: str = "b1",
    qty: object = 2,
    price: object = "10.25",
    instrument: InstrumentId = GLD,
    **changes: object,
) -> Fill:
    fields: dict[str, object] = {
        "fill_id": fill_id,
        "correction_id": None,
        "broker_order_id": broker_order_id,
        "client_order_id": None,
        "instrument": instrument,
        "qty": Decimal(str(qty)),
        "price": Decimal(str(price)),
        "at_text": "2026-10-01T15:40:00-04:00",
        "at": AT,
        "received_at": AT,
        "excess_places": (),
        "basis": None,
    }
    return Fill(**fields | changes)  # type: ignore[arg-type]


def filled(made: Fill, read: int = 2) -> FillEntry:
    return FillEntry(made, read)


def disputed(made: Fill, read: int = 2) -> DisputedFillEntry:
    return DisputedFillEntry(made, read)


def broker_record(
    record_id: str = "r::1",
    amount: object = "-1.25",
    kind: RecordKind = RecordKind.FEE,
    instrument: InstrumentId | None = None,
    **changes: object,
) -> BrokerRecord:
    """A broker's record, every value made up."""
    fields: dict[str, object] = {
        "record_id": record_id,
        "kind": kind,
        "amount": Decimal(str(amount)),
        "instrument": instrument,
        "at": AT,
        "received_at": AT,
        "excess_places": (),
    }
    return BrokerRecord(**fields | changes)  # type: ignore[arg-type]


def foreign(broker_order_id: str = "x1", qty: object = 7, **changes: object) -> BrokerOrder:
    """An order the broker lists that the platform never sent, every value made up."""
    fields: dict[str, object] = {
        "broker_order_id": broker_order_id,
        "client_order_id": None,
        "instrument": GLD,
        "qty": Decimal(str(qty)),
        "filled": Decimal(0),
        "type": OrderType.MARKET_ON_CLOSE,
        "time_in_force": TimeInForce.CLS,
        "state": BrokerState.WORKING,
        "submitted_at": AT,
    }
    return BrokerOrder(**fields | changes)  # type: ignore[arg-type]


def outside(made: BrokerRecord, read: int = 2) -> OutsideRecordEntry:
    return OutsideRecordEntry(made, read)


def unmatched_record(made: BrokerRecord, read: int = 2) -> UnmatchedRecordEntry:
    return UnmatchedRecordEntry(made, read, f"the record {made.record_id} is off the list")


def unmatched_fill(made: Fill, read: int = 2) -> UnmatchedFillEntry:
    return UnmatchedFillEntry(made, read, f"the fill {made.fill_id} is no order's")


def unmatched_order(made: BrokerOrder, read: int = 2) -> UnmatchedOrderEntry:
    return UnmatchedOrderEntry(made, read, f"the order {made.broker_order_id} is no ack's")


def started(*orders: OrderIntent, cash: object = 100_000, writer: str | None = WRITER) -> Journal:
    """A journal in memory that holds an opening, a read and one decision."""
    journal = Journal(Memory(), ACCOUNT, writer)
    add(journal, opening(cash))
    add(journal, read())
    add(journal, decision(*orders))
    return journal


def route(state: str, order: OrderIntent, decided: int = 3, broker: str = "b1") -> list[Record]:
    """The entries, after its decision, that bring one order to a state.

    Two states have two routes. A late one passed through a broker's answer.
    """
    name, half = order.client_order_id, order.qty / 2
    intent: list[Record] = [IntentEntry(decided, order)]
    approved = [*intent, verdict(name)]
    sending = [*approved, SendingEntry(name)]
    acked = [*sending, ack(name, broker)]
    first, rest = (
        filled(fill(f"{broker}-f{n}", broker, half, instrument=order.instrument)) for n in (1, 2)
    )
    return {
        "none": [],
        "Intended": intent,
        "Refused": [*intent, verdict(name, approved=False)],
        "Approved": approved,
        "Abandoned": [*intent, abandoned(name)],
        "Abandoned late": [*sending, abandoned(name)],
        "Sending": sending,
        "InDoubt": [*sending, in_doubt(name)],
        # The list is asked for a day later, and is entry `decided` + 5.
        "Lost": [*sending, in_doubt(name), read(LATE), lost(name, decided + 5)],
        "Acknowledged": acked,
        "PartlyFilled": [*acked, first],
        "Filled": [*acked, first, rest],
        "Rejected": [*sending, rejected(name)],
        "Rejected late": [*acked, rejected(name)],
        "Expired": [*acked, first, expired(name, half)],
        "Expired unfilled": [*acked, expired(name, 0)],
    }[state]


def shown(view: View) -> dict[str, object]:
    """Everything a view shows, as plain values two views can be compared by."""
    return {
        "account": view.account,
        "through": view.through,
        "opened_at": view.opened_at,
        "sent_at": view.sent_at,
        "orders": dict(view.orders),
        "live": list(view.live.items()),
        "decisions": dict(view.decisions),
        "decided": dict(view.decided),
        "reads": dict(view.reads),
        "fills": list(view.fills.items()),
        "disputes": view.disputes,
        "records": list(view.records.items()),
        "unmatched": view.unmatched,
        "book": view.book,
    }


# The lives an order may lead. One that stops short of an end is one a journal
# holds while the order is still going, or after a crash.
LIVES: tuple[tuple[str, ...], ...] = (
    ("intent",),
    ("intent", "abandoned"),
    ("intent", "refused"),
    ("intent", "approved"),
    ("intent", "approved", "abandoned"),
    ("intent", "approved", "sending"),
    ("intent", "approved", "sending", "abandoned"),
    ("intent", "approved", "sending", "rejected"),
    ("intent", "approved", "sending", "in_doubt"),
    ("intent", "approved", "sending", "in_doubt", "ack", "whole"),
    ("intent", "approved", "sending", "in_doubt", "in_doubt", "ack", "whole"),
    ("intent", "approved", "sending", "ack"),
    ("intent", "approved", "sending", "ack", "rejected"),
    ("intent", "approved", "sending", "ack", "expired"),
    ("intent", "approved", "sending", "ack", "part"),
    ("intent", "approved", "sending", "ack", "part", "expired"),
    ("intent", "approved", "sending", "ack", "part", "part", "expired"),
    ("intent", "approved", "sending", "ack", "part", "repeat", "whole", "repeat"),
    ("intent", "approved", "sending", "ack", "part", "part", "whole"),
    *(("intent", "approved", "sending", "ack", "whole"),) * 12,
)
NAMES = tuple(InstrumentId(name) for name in ("GLD", "USO", "SPY", "QQQ", "EWA", "EWC", "IGE"))
MARKS = {name: Decimal(10 + 7 * n) for n, name in enumerate(NAMES)}
STRATEGIES = (CHAN, OTHER)
# What every check of a seeded journal is handed, loose enough to approve.
LIMITS = {name: dict.fromkeys(NAMES, gates.LOOSE) for name in ("max_order_qty", "max_position")}
CAPS = gates.caps(**LIMITS)
VALUATION = gates.valuation(50_000, replayed_through=1)
SNAPSHOT = gates.snapshot(MARKS)
SNAPSHOT_BYTES = encode(SNAPSHOT)
_ENDS = {"abandoned", "refused", "rejected", "whole", "repeat", "expired"}
_STEP = Decimal("1E-9")


@dataclass(frozen=True)
class Checked:
    """One verdict entry, the blobs its check was handed, by SHA-256, and the m it was held to."""

    entry: VerdictEntry
    intent: OrderIntent
    blobs: Mapping[str, bytes]
    stress_move: Decimal


class Seeded:
    """One seeded journal, written a record at a time as a writer does.

    Each verdict is the gate's own answer on the view before it. Quantities
    come from the held position, because blind draws almost never close one.
    """

    def __init__(self, seed: int, entries: int = 60) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.store = Memory()
        self.journal = Journal(self.store, ACCOUNT, WRITER)
        self.checks: list[Checked] = []
        self._left = entries
        self._view = self.journal.view()
        self._number = 0

    def walk(self) -> Iterator[tuple[Record, View]]:
        """Each record as it is stored, with the view `record` gave after it."""
        yield self._put(opening(Decimal(self.rng.randint(0, 10**9)).scaleb(-4)))
        while self._left > 0:
            self._number += 1
            at = gates.DECISION + timedelta(seconds=self._number)
            yield self._put(read(at))
            reading = self._view.through.seq
            if self._left <= 0:
                return
            orders = [self._order(place, at) for place in range(1, self.rng.randint(2, 4))]
            yield self._put(decision(*orders, at=at, read=reading))
            decided = self._view.through.seq
            lives = [self._live(order, decided, reading) for order in orders]
            while lives and self._left > 0:
                life = self.rng.choice(lives)
                record = next(life, None)
                if record is None:
                    lives.remove(life)
                else:
                    yield self._put(record)

    def _put(self, record: Record) -> tuple[Record, View]:
        self._left -= 1
        _, self._view = self.journal.record(self._view, record, AT + timedelta(days=self._number))
        return record, self._view

    def _order(self, place: int, at: datetime) -> OrderIntent:
        strategy, instrument = self.rng.choice(STRATEGIES), self.rng.choice(NAMES)
        held = self._view.book.positions_by_strategy.get(strategy, {})
        if held and self.rng.random() < 0.7:
            instrument = self.rng.choice(sorted(held))
            qty = held[instrument] * self.rng.choice((-1, -1, Decimal("-0.5"), -2, 1))
        else:
            qty = Decimal(self.rng.randint(1, 5 * 10**10)).scaleb(-9) * self.rng.choice((1, -1))
        return planned(place, qty.quantize(_STEP) or Decimal(1), instrument, strategy, at)

    def _live(self, order: OrderIntent, decided: int, reading: int) -> Iterator[Record]:
        name, broker = order.client_order_id, f"b-{order.client_order_id[-19:]}"
        # The gate's second implementation lists every case of which open orders fill,
        # so few orders are left open for good.
        lives = [life for life in LIVES if len(self._view.live) < 3 or life[-1] in _ENDS]
        life, left, made = self.rng.choice(lives), order.qty, None
        for step in life:
            match step:
                case "intent":
                    yield IntentEntry(decided, order)
                case "approved" | "refused":
                    checked = self._check(order, refuse=step == "refused")
                    yield checked.entry
                    if not checked.entry.verdict.approved:
                        return
                case "abandoned":
                    yield abandoned(name)
                case "sending":
                    yield SendingEntry(name)
                case "ack":
                    yield ack(name, broker)
                case "rejected":
                    yield rejected(name)
                case "in_doubt":
                    yield in_doubt(name, self.rng.choice((None, b"?")))
                case "part" | "whole":
                    qty = left
                    if step == "part":
                        share = Decimal(self.rng.randint(1, 9)) / 10
                        qty = (left * share).quantize(_STEP)
                        if qty in (0, left):
                            return
                    price = Decimal(self.rng.randint(1000, 900_000)).scaleb(-4)
                    left -= qty
                    made = fill(f"f-{broker}-{left}".replace(".", "_"), broker, qty, price)
                    made = replace(made, instrument=order.instrument)
                    yield filled(made, reading)
                case "repeat":
                    assert made is not None
                    yield filled(replace(made, received_at=AT + timedelta(days=9)), reading)
                case "expired":
                    yield expired(name, order.qty - left)

    def _check(self, order: OrderIntent, *, refuse: bool) -> Checked:
        """The gate's answer on the newest view, which a small order limit refuses where asked.

        Some checks hold a larger stress record than the caps name, which shows in the
        verdict's m and its worst loss, since every other limit but the small one is loose.
        """
        none = dict.fromkeys(NAMES, Decimal(0))
        small = {"max_order_qty": none, "max_position": none} if refuse else {}
        constraints = gates.constraints(strategy=order.strategy, **LIMITS | small)
        inputs = gates.inputs(caps=CAPS, valuation=VALUATION)
        blobs: dict[str, bytes] = {}

        def stored(data: bytes) -> BlobRef:
            blobs[sha256_of(data)] = data
            return BlobRef("runs/d/x.json", sha256_of(data))

        view = self._view
        named = gates.fitting(constraints, inputs.caps)
        # A draw of its own, so the rest of each journal is what it was without it.
        raised = random.Random(f"{self.seed} {order.client_order_id}").random() < 0.5
        more = replace(named[0], vintage="c" * 64, stress_move=Decimal("0.25"))
        gate = Gate((*named, more) if raised else named)
        answer = gate.check(order, view.book, SNAPSHOT, constraints, inputs, gates.DECISION)
        assert refuse <= (not answer.approved)
        entry = VerdictEntry(
            client_order_id=order.client_order_id,
            verdict=answer,
            now=gates.DECISION,
            stop=StopState.ABSENT,
            close_entry=None,
            book=view.through.seq,
            caps=encode(CAPS),
            caps_sha256=sha256_of(b"the blob the owner wrote"),
            snapshot=stored(SNAPSHOT_BYTES),
            constraints=stored(encode(constraints)),
            valuation=stored(encode(VALUATION)),
        )
        made = Checked(entry, order, blobs, more.stress_move if raised else CAPS.stress_move)
        self.checks.append(made)
        return made
