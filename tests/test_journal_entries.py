"""What a journal entry holds: each kind there and back, every field it refuses and every bound."""

from __future__ import annotations

import copy
import dataclasses
import json
import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin, get_type_hints

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    BlobRef,
    BrokerState,
    FillBasis,
    Halt,
    InstrumentId,
    JournalEntry,
    Limit,
    LossReading,
    OrderType,
    PastLimit,
    RecordKind,
    StopState,
    TargetPortfolio,
    TimeInForce,
    Verdict,
    encode,
    sha256_of,
)
from ithildinruntime.journal import (
    KINDS,
    RAW_LIMIT,
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
    body_of,
    is_writer,
    kind_of,
    record_of,
)
from tests.support.journals import (
    ACCOUNT,
    AT,
    CHAN,
    GLD,
    OTHER,
    REF,
    USO,
    WRITER,
    Memory,
    abandoned,
    ack,
    add,
    broker_record,
    decision,
    disputed,
    expired,
    fill,
    filled,
    foreign,
    in_doubt,
    lost,
    opening,
    outside,
    planned,
    read,
    rejected,
    route,
    started,
    unmatched_fill,
    unmatched_order,
    unmatched_record,
    verdict,
)

ONE = planned(1, 4)
NAME = ONE.client_order_id
FULL = fill(
    "f::1",
    "b-1",
    "0.123456789",
    "10.250000",
    client_order_id=NAME,
    excess_places=("price",),
    basis=FillBasis.QUOTE,
)
# Two of each kind where a field may hold nothing: one with it and one without.
SAMPLES: tuple[Record, ...] = (
    opening("100000.0100"),
    read(),
    decision(ONE, planned(2, -3, USO, OTHER)),
    decision(),
    route("Intended", ONE)[0],
    verdict(NAME, book=3),
    dataclasses.replace(verdict(NAME, approved=False, book=4), close_entry=2, caps=b"\xff\x00"),
    abandoned(NAME),
    SendingEntry(NAME),
    ack(NAME, "a:1_-Z", b"\xff", cut=True),
    rejected(NAME),
    in_doubt(NAME),
    in_doubt(NAME, b"{}", cut=True),
    filled(FULL),
    filled(fill("f")),
    disputed(FULL),
    expired(NAME, "1.5"),
    outside(broker_record(instrument=GLD, excess_places=("amount",))),
    outside(broker_record("r::2", "3.5", RecordKind.DIVIDEND)),
    unmatched_record(broker_record("r::3", "100", RecordKind.DEPOSIT)),
    unmatched_fill(FULL),
    unmatched_order(foreign()),
    unmatched_order(foreign("x2", -3, client_order_id=NAME, type=None, time_in_force=None)),
    lost(NAME),
)


def test_every_kind_has_a_sample() -> None:
    assert {kind_of(record) for record in SAMPLES} == set(KINDS)
    assert len(KINDS) == 18


# Acceptance 1: each kind there and back.


@pytest.mark.parametrize("record", SAMPLES, ids=kind_of)
def test_1_each_kind_there_and_back(record: Record) -> None:
    name = kind_of(record)
    journal = Journal(Memory(), ACCOUNT)
    add(journal, record)
    (entry,) = Journal(journal._store, ACCOUNT).read()
    assert (entry.body["kind"], entry.body["writer"]) == (name, WRITER)
    again = record_of(entry)
    assert again == record and type(again) is KINDS[name]
    assert encode(again) == encode(record)
    fields = {f.name for f in dataclasses.fields(record)}
    assert set(entry.body) == fields | {"kind", "writer"}


def test_1_each_kind_goes_in_through_record_and_comes_back() -> None:
    one, two, three = planned(1, 4), planned(2, -3, USO, OTHER), planned(3, 1)
    journal = Journal(Memory(), ACCOUNT, WRITER)
    names = [order.client_order_id for order in (one, two, three)]
    part = fill("f1", "b1", 1)
    given: list[Record] = [opening(), read(), decision(one, two, three)]
    given += [
        *route("Acknowledged", one),
        filled(part),
        disputed(dataclasses.replace(part, qty=Decimal(2))),
    ]
    given += [expired(names[0], 1), *route("InDoubt", two)]
    # A list asked for an hour after the close, which the `lost` names by its number.
    given += [read(AT + timedelta(hours=1)), lost(names[1], len(given) + 1)]
    given += [*route("Rejected", three)]
    given += [
        outside(broker_record()),
        unmatched_record(broker_record("r::2", 9, RecordKind.WITHDRAWAL)),
        unmatched_fill(fill("f9", "x1")),
        unmatched_order(foreign()),
    ]
    given += [read(AT + timedelta(days=1)), decision(at=AT + timedelta(days=1))]
    view = journal.view()
    kept = []
    for record in given:
        if getattr(record, "book", 0) is None:
            record = dataclasses.replace(record, book=view.through.seq)
        kept.append(record)
        ack_, view = journal.record(view, record, AT)
        assert (ack_.seq, ack_.entry_hash) == (view.through.seq, view.through.entry_hash)
    assert [record_of(entry) for entry in Journal(journal._store, ACCOUNT).read()] == kept
    # The other end state, `abandoned`, is the one kind not yet here.
    assert {kind_of(record) for record in kept} == set(KINDS) - {"abandoned"}


def _strip(hint: Any) -> Any:
    while True:
        if get_origin(hint) is Annotated:
            hint = get_args(hint)[0]
        elif hasattr(hint, "__supertype__"):
            hint = hint.__supertype__
        else:
            return hint


def _places(value: object, hint: Any) -> Iterator[tuple[list, Any]]:
    """The path to each field inside a body, with its type."""
    hint = _strip(hint)
    arms = (
        [a for a in get_args(hint) if a is not type(None)] if type(None) in get_args(hint) else []
    )
    if arms:
        hint = _strip(arms[0])
    if dataclasses.is_dataclass(hint) and value is not None:
        for name, inner in get_type_hints(hint, include_extras=True).items():
            yield [name], inner
            for path, found in _places(value[name], inner):
                yield [name, *path], found
    elif get_origin(hint) is tuple and value:
        for path, found in _places(value[0], get_args(hint)[0]):
            yield [0, *path], found
    elif isinstance(value, dict) and get_args(hint) and hint is not bytes:
        key = next(iter(value), None)
        if key is not None:
            yield [key], get_args(hint)[1]
            for path, found in _places(value[key], get_args(hint)[1]):
                yield [key, *path], found


def _set(body: dict, path: list, value: object) -> dict:
    made = copy.deepcopy(body)
    at = made
    for part in path[:-1]:
        at = at[part]
    if value is _ABSENT:
        del at[path[-1]]
    else:
        at[path[-1]] = value
    return made


_ABSENT = object()
_OTHER_TYPE = {str: 7, int: "7", bool: "true", dict: [1], list: {}, type(None): [1]}


def _faults() -> Iterator[tuple[str, dict]]:
    for n, record in enumerate(SAMPLES):
        body, kind = body_of(record, WRITER), type(record)
        name = f"{kind_of(record)}-{n}"
        yield f"{name} extra", {**body, "more": 1}
        for path, hint in _places(body, kind):
            at = body
            for part in path:
                at = at[part]
            where = f"{name} {'.'.join(map(str, path))}"
            # A mapping's own key may be absent.
            if len(path) < 2 or path[-2] not in ("inputs", "shares"):
                yield f"{where} absent", _set(body, path, _ABSENT)
            if isinstance(at, dict) and "data" not in at and path[-1] not in ("inputs", "shares"):
                yield f"{where} extra", _set(body, [*path, "more"], "x")
            yield f"{where} another type", _set(body, path, _OTHER_TYPE[type(at)])
            if type(None) not in get_args(hint):
                yield f"{where} null", _set(body, path, None)
            if _strip(hint) is Decimal or Decimal in map(_strip, get_args(hint)):
                for bad in ("NaN", "Infinity", "-Infinity", "1E+5", "+1", "", 1, 1.5):
                    if not isinstance(bad, float):
                        yield f"{where} {bad!r}", _set(body, path, bad)


FAULTS = dict(_faults())


def test_1_the_faults_reach_every_field() -> None:
    assert len(FAULTS) > 400
    for needed in (
        "fill-13 fill.price 'NaN'",
        "verdict-5 verdict.loss.loss null",
        "read-1 at null",
    ):
        assert needed in FAULTS
    assert {name.partition("-")[0] for name in FAULTS} == set(KINDS)


@pytest.mark.parametrize("name", sorted(FAULTS))
def test_1_a_body_whose_fields_are_not_its_kinds_halts_naming_the_entry(name: str) -> None:
    body = FAULTS[name]
    entry = JournalEntry(7, 1, AT, body, "0" * 64)
    with pytest.raises(Halt, match=f"^entry 7 is not of its kind {body['kind']}: "):
        record_of(entry)


@pytest.mark.parametrize("record", SAMPLES, ids=kind_of)
def test_1_a_record_whose_field_is_of_another_type_is_stored_by_no_writer(record: Record) -> None:
    for field in dataclasses.fields(record):
        journal = Journal(store := Memory(), ACCOUNT, WRITER)
        bad = dataclasses.replace(record)
        object.__setattr__(bad, field.name, object())
        with pytest.raises(Halt, match="entry 1 cannot be written"):
            journal.record(journal.view(), bad, AT)
        assert store.blobs == {} and store.creates == 0
        assert journal.view().through.seq == 0


def test_1_a_decimal_that_is_not_finite_and_a_time_that_is_not_utc_are_stored_by_no_writer() -> (
    None
):
    for bad, at in (
        (opening("NaN"), AT),
        (opening("Infinity"), AT),
        (opening("1" * 39), AT),
        (opening(), AT.replace(tzinfo=None)),
        (opening(), AT.astimezone(timezone(timedelta(hours=1)))),
    ):
        journal = Journal(store := Memory(), ACCOUNT, WRITER)
        with pytest.raises(Halt, match="entry 1 cannot be written"):
            journal.record(journal.view(), bad, at)
        assert store.blobs == {}


@pytest.mark.parametrize(
    "record",
    [
        dataclasses.replace(opening(), account="paper-\ud800"),
        dataclasses.replace(rejected(NAME), reason="no \udfff"),
        filled(fill("f", at_text="\udcff")),
        dataclasses.replace(decision(), inputs={"forecast/\ud800": REF}),
        dataclasses.replace(
            decision(), targets=(TargetPortfolio(AT, CHAN, {"\ud800": Decimal(1)}),)
        ),
    ],
)
def test_1_text_that_is_no_unicode_is_stored_by_no_writer(record: Record) -> None:
    journal = Journal(store := Memory(), ACCOUNT, WRITER)
    with pytest.raises(Halt, match="entry 1 cannot be written: .* holds text that is no Unicode"):
        journal.record(journal.view(), record, AT)
    assert store.blobs == {}


def test_1_a_planted_entry_whose_text_is_no_unicode_halts() -> None:
    for old, new in ((b'"paper-1"', b'"\\ud800"'), (b'"account"', b'"\\ud800"')):
        journal = Journal(store := Memory(), ACCOUNT)
        add(journal, opening())
        (key,) = store.blobs
        assert old in store.blobs[key]
        store.blobs[key] = store.blobs[key].replace(old, new)
        with pytest.raises(Halt, match="entry 1 is not a canonical journal entry"):
            Journal(store, ACCOUNT).view()


def test_1_a_value_that_is_no_record_is_refused() -> None:
    journal = Journal(store := Memory(), ACCOUNT, WRITER)
    for bad in (None, {"kind": "opening"}, ONE):
        with pytest.raises(TypeError, match="no journal entry's record"):
            journal.record(journal.view(), bad, AT)
    assert store.blobs == {}


@pytest.mark.parametrize("kind", ["fixture", "Opening", "", None, 3, ["opening"], {"a": 1}, True])
def test_1_an_unknown_kind_halts_with_the_entrys_number(kind: object) -> None:
    journal = started()
    journal._append(journal._build({**body_of(read(), WRITER), "kind": kind}, AT))
    for reader in (journal, Journal(journal._store, ACCOUNT)):
        for _ in range(2):
            with pytest.raises(Halt, match="entry 4 is of the kind .* does not read"):
                reader.view()


def test_1_a_missing_kind_halts_with_the_entrys_number() -> None:
    journal = started()
    body = body_of(read(), WRITER)
    del body["kind"]
    journal._append(journal._build(body, AT))
    with pytest.raises(Halt, match="entry 4 is of the kind None"):
        journal.view()


BAD_WRITERS = [None, "", "a", "A" * 32, "g" * 32, "0" * 31, "0" * 33, "0" * 32 + "\n", 7]


@pytest.mark.parametrize("writer", [*BAD_WRITERS, "absent"])
def test_1_a_missing_or_malformed_writer_halts_with_the_entrys_number(writer: object) -> None:
    journal = started()
    body = {**body_of(read(), WRITER), "writer": writer}
    if writer == "absent":
        del body["writer"]
    journal._append(journal._build(body, AT))
    with pytest.raises(Halt, match="entry 4, of the kind read, has the writer"):
        journal.view()


@pytest.mark.parametrize("writer", BAD_WRITERS)
def test_1_a_journal_with_no_writer_records_nothing(writer: object) -> None:
    journal = Journal(store := Memory(), ACCOUNT, writer)  # type: ignore[arg-type]
    assert not is_writer(writer)
    view = journal.view()
    with pytest.raises(Halt, match="its writer is .*, no writer"):
        journal.record(view, opening(), AT)
    assert store.creates == 0


# Each bound of item 5, one step inside and one step outside.

BIG = b"x" * RAW_LIMIT


@pytest.mark.parametrize(
    ("record", "past"),
    [
        (ack(NAME, raw=BIG), None),
        (ack(NAME, raw=BIG, cut=True), None),
        (ack(NAME, raw=b""), None),
        (ack(NAME, raw=BIG + b"x", cut=True), "raw has 65537 bytes, more than 65536"),
        (rejected(NAME, BIG, cut=True), None),
        (rejected(NAME, BIG + b"x", cut=True), "raw has 65537 bytes"),
        (in_doubt(NAME, BIG, cut=True), None),
        (in_doubt(NAME, BIG + b"x", cut=True), "raw has 65537 bytes"),
        (expired(NAME, 0, BIG, cut=True), None),
        (expired(NAME, 0, BIG + b"x", cut=True), "raw has 65537 bytes"),
        (ack(NAME, "a"), None),
        (ack(NAME, "a" * 128), None),
        (ack(NAME, "a" * 129), "the broker's order id is not a broker's id"),
        (ack(NAME, ""), "the broker's order id is not a broker's id"),
        (ack(NAME, "20261103::a_B-9"), None),
        (ack(NAME, "a.b"), "the broker's order id is not a broker's id"),
        (ack(NAME, "a b"), "the broker's order id is not a broker's id"),
        (ack(NAME, "a\n"), "the broker's order id is not a broker's id"),
        (ack(NAME, "é"), "the broker's order id is not a broker's id"),
        *(
            case
            for make in (filled, disputed)
            for case in (
                (make(fill("f" * 128)), None),
                (make(fill("f" * 129)), "the fill's id is not a broker's id"),
                (make(fill("")), "the fill's id is not a broker's id"),
                (make(fill("f/1")), "the fill's id is not a broker's id"),
                (make(fill("f", "b" * 128)), None),
                (make(fill("f", "b" * 129)), "the broker's order id is not a broker's id"),
                (make(fill("f", "")), "the broker's order id is not a broker's id"),
                (make(fill("f", price="0.0001")), None),
                (make(fill("f", qty="0.000000001")), None),
                (make(fill("f", qty="-0.000000001")), None),
            )
        ),
        (filled(fill("f", price="0")), "has a price of 0, not above zero"),
        (filled(fill("f", price="-0.0001")), "has a price of -0.0001, not above zero"),
        (filled(fill("f", qty="0")), "is for zero shares"),
        (filled(fill("f", qty="-0.000")), "is for zero shares"),
        # A dispute moves nothing, so it keeps what came.
        (disputed(fill("f", price="0")), None),
        (disputed(fill("f", qty="0")), None),
        (filled(fill("f", correction_id="c")), "the fill f carries a correction's id"),
        (filled(fill("f", correction_id="")), "the fill f carries a correction's id"),
        # No rule reads a correction yet, so a disputed one is kept as it came.
        (disputed(fill("f", correction_id="c")), None),
        (outside(broker_record("r" * 128)), None),
        (outside(broker_record("r" * 129)), "the record's id is not a broker's id"),
        (outside(broker_record("")), "the record's id is not a broker's id"),
        (outside(broker_record(amount="0.0001")), None),
        (outside(broker_record(amount="-0.0001")), None),
        (outside(broker_record(amount="0")), "the record r::1 moves no cash"),
        (outside(broker_record(amount="-0.00")), "the record r::1 moves no cash"),
        *(
            (
                outside(broker_record(kind=kind)),
                f"the record r::1 is a {kind}, which no rule lets move",
            )
            for kind in (RecordKind.DEPOSIT, RecordKind.WITHDRAWAL, RecordKind.OTHER)
        ),
        (
            outside(broker_record(instrument=InstrumentId("a b"))),
            "names an instrument whose name is",
        ),
        # What is kept as unmatched moves nothing, so it keeps what came but for its id.
        (unmatched_record(broker_record(amount="0", kind=RecordKind.OTHER)), None),
        (unmatched_record(broker_record("r/1")), "the record's id is not a broker's id"),
        (unmatched_fill(fill("f", qty="0", price="0", correction_id="c")), None),
        (unmatched_fill(fill("f/1")), "the fill's id is not a broker's id"),
        (unmatched_fill(fill("f", "")), "the broker's order id is not a broker's id"),
        (unmatched_order(foreign(qty=0)), None),
        (unmatched_order(foreign("x" * 129)), "the broker's order id is not a broker's id"),
        *(
            (dataclasses.replace(record, **{field: bad}), f"{shown}.* name no blob")
            for record, field in (
                (read(), "ref"),
                *((verdict(NAME, book=3), f) for f in ("snapshot", "constraints", "valuation")),
            )
            for bad, shown in (
                (BlobRef("", "a" * 64), "the key '' and"),
                (BlobRef("k", ""), "the key 'k' and SHA-256 ''"),
                (BlobRef("k", "a" * 63), "the key 'k' and"),
                (BlobRef("k", "a" * 65), "the key 'k' and"),
                (BlobRef("k", "A" * 64), "the key 'k' and"),
            )
        ),
        (
            dataclasses.replace(decision(), inputs={"snapshot": BlobRef("k", "x")}),
            "the key 'k' and SHA-256 'x' name no blob",
        ),
        (dataclasses.replace(verdict(NAME, book=3), caps=b""), "the caps are empty"),
        (dataclasses.replace(verdict(NAME, book=3), caps_sha256="b" * 63), "their SHA-256 is none"),
        (decision(planned(1, "0.000000001")), None),
        (decision(planned(1, "-0.000000001")), None),
        (decision(ONE, planned(2, 0)), "is for zero shares"),
        (decision(planned(1, "-0.0")), "is for zero shares"),
        (dataclasses.replace(SAMPLES[4], order=planned(1, 0)), "is for zero shares"),
    ],
)
def test_1_each_bound_is_held_one_step_inside_and_outside(record: Record, past: str | None) -> None:
    entry = JournalEntry(7, 1, AT, body_of(record, WRITER), "0" * 64)
    if past is None:
        assert record_of(entry) == record
        return
    with pytest.raises(Halt, match=f"^entry 7 is past a bound of its kind .*{past}"):
        record_of(entry)


def test_1_a_record_past_a_bound_is_stored_by_no_writer_and_halts_a_replay_the_same_way() -> None:
    journal = started(ONE)
    for record in route("Sending", ONE):
        add(journal, record)
    past = ack(NAME, "a" * 129)
    view, count = journal.view(), len(journal._store.blobs)
    with pytest.raises(Halt) as at_record:
        journal.record(view, past, AT)
    assert len(journal._store.blobs) == count
    add(journal, past)
    with pytest.raises(Halt) as at_replay:
        journal.view()
    assert at_record.value.reason == at_replay.value.reason


def test_1_a_long_id_is_not_shown_whole_in_a_halt() -> None:
    entry = JournalEntry(7, 1, AT, body_of(ack(NAME, "x" * 10_000), WRITER), "0" * 64)
    with pytest.raises(Halt) as halted:
        record_of(entry)
    assert len(halted.value.reason) < 200


# Acceptance 2: the committed journal of every kind.

ORDERS = Path(__file__).parent / "fixtures" / "orders"
FIXED = "fixture-1"


def _committed() -> list[tuple[Record, datetime]]:
    """What the committed journal was written from, every value made up.

    Entry n is built n seconds after the decision. Each of the first eleven
    orders ends in a state of its own, and the last two are refused, so that
    each optional part of a verdict is filled once. The verdicts fill fields and
    are not answers the gate would give.
    """
    at = datetime(2026, 10, 1, 19, 40, tzinfo=UTC)
    spy = InstrumentId("SPY")
    on_close = {"type": OrderType.MARKET_ON_CLOSE, "time_in_force": TimeInForce.CLS}
    orders = (
        planned(1, 10, at=at),
        planned(2, -4, USO, at=at),
        planned(3, "2.5", GLD, OTHER, at=at),
        planned(4, 7, spy, at=at),
        planned(5, 1, spy, OTHER, at=at),
        planned(6, -3, GLD, OTHER, at=at),
        planned(7, 5, USO, OTHER, at=at),
        planned(8, 6, spy, at=at),
        planned(9, "8.000000001", USO, at=at),
        planned(10, -2, spy, OTHER, at=at),
        dataclasses.replace(planned(11, 9, GLD, at=at), **on_close),
        dataclasses.replace(planned(12, 3, GLD, at=at), **on_close),
        planned(13, -1, spy, OTHER, at=at),
    )
    names = [order.client_order_id for order in orders]
    targets = (
        TargetPortfolio(at, CHAN, {GLD: Decimal(19), USO: Decimal("4.000000001")}),
        TargetPortfolio(at, OTHER, {GLD: Decimal("-0.5"), spy: Decimal(0)}),
    )

    def t(seconds: int) -> datetime:
        return at + timedelta(seconds=seconds)

    next_day = at + timedelta(days=1)

    def blob(name: str) -> BlobRef:
        return BlobRef(f"runs/fixture/2026-10-01/{name}.json", sha256_of(name.encode()))

    plan = DecisionEntry(
        deployment="fixture",
        run="2026-10-01",
        decision_at=at,
        targets=targets,
        orders=orders,
        off=("idle",),
        inputs={
            name: blob(name.replace("/", "-"))
            for name in (
                *("snapshot", "constraints", "valuation", "targets"),
                *(f"{kind}/{name}" for kind in ("forecast", "schedule") for name in (CHAN, OTHER)),
            )
        },
        read=2,
        synced=2,
    )
    approved = Verdict(
        approved=True,
        limit=None,
        value=None,
        bound=None,
        missing_term=None,
        adding=True,
        worst_loss=Decimal("41.0000"),
        stress_move=Decimal("0.20500000"),
        loss=LossReading(Decimal("12.5000"), Decimal("50.0000"), False, date(2026, 9, 30)),
        past=(),
    )
    too_large = dataclasses.replace(
        approved,
        approved=False,
        limit=Limit.MAX_ORDER_QTY,
        value=Decimal("2.500000000"),
        bound=Decimal("2.000000000"),
    )
    stopped = dataclasses.replace(
        approved,
        approved=False,
        limit=Limit.STOP,
        adding=False,
        loss=LossReading(Decimal("50.0001"), Decimal("50.0000"), True, date(2026, 9, 30)),
        past=(
            PastLimit(Limit.MAX_POSITION, Decimal("12.000000000"), Decimal("10.000000000")),
            PastLimit(Limit.MAX_NET, Decimal("900.6"), Decimal("900.5")),
        ),
    )
    no_position = dataclasses.replace(
        approved, approved=False, limit=Limit.POSITION, missing_term="position", worst_loss=None
    )

    def says(n: int, place: int, verdict: Verdict = approved, **changes: object) -> VerdictEntry:
        made = VerdictEntry(
            client_order_id=names[place - 1],
            verdict=verdict,
            now=t(n),
            stop=StopState.ABSENT,
            close_entry=None,
            book=n - 1,
            caps=b'{"max_gross":"1000"}',
            caps_sha256=sha256_of(b"caps"),
            snapshot=blob("snapshot"),
            constraints=blob("constraints"),
            valuation=blob("valuation"),
        )
        return dataclasses.replace(made, **changes)  # type: ignore[arg-type]

    def sent(n: int, place: int) -> list[Record]:
        """The intent, verdict and sending of an order, the intent at entry `n`."""
        return [
            IntentEntry(3, orders[place - 1]),
            says(n + 1, place),
            SendingEntry(names[place - 1]),
        ]

    def filled_at(n: int, fill_id: str, broker: str, qty: object, price: str, instrument, **more):
        """A fill made the second before entry `n` and received as it was built."""
        told = {
            "at": t(n - 1),
            "received_at": t(n),
            "at_text": f"2026-10-01T15:40:{n - 1:02d}-04:00",
        }
        return fill(fill_id, broker, qty, price, instrument, **{**told, **more})

    b1 = filled_at(8, "sim-f-1", "sim-o-1", 6, "210.5000", GLD, basis=FillBasis.SIGNAL_CLOSE)
    b2 = filled_at(9, "sim-f-2", "sim-o-1", 4, "210.5000", GLD, basis=FillBasis.OFFICIAL_CLOSE)
    b3 = filled_at(15, "f::2", "o-2", "-1.5", "71.25", USO, client_order_id=names[1])
    b4 = filled_at(
        35,
        "f9",
        "o-9",
        "8",
        "70.123456",
        USO,
        basis=FillBasis.QUOTE,
        excess_places=("price",),
        at_text="20261001 15:40:34",
    )
    again = dataclasses.replace(
        b4, price=Decimal("70.124"), excess_places=(), correction_id="c-1", received_at=t(49)
    )
    records: list[Record] = [
        OpeningEntry(FIXED, Decimal("100000.00")),
        ReadEntry("execution/send", at, blob("broker-read-1"), t(1)),
        plan,
        *sent(4, 1),
        AckEntry(names[0], "sim-o-1", b'{"id":"sim-o-1"}', False, t(7)),
        FillEntry(b1, 2),
        FillEntry(b2, 2),
        *sent(10, 2),
        InDoubtEntry(names[1], "the broker did not answer", None, False, t(13)),
        AckEntry(names[1], "o-2", b"\xff\xfe", True, t(14)),
        FillEntry(b3, 2),
        ExpiredEntry(names[1], Decimal("-1.5"), b"{}", False, t(16)),
        IntentEntry(3, orders[2]),
        says(18, 3, too_large),
        IntentEntry(3, orders[3]),
        AbandonedEntry(names[3], "the kill switch is on"),
        *sent(21, 5),
        RejectedEntry(names[4], "no shares to borrow", b"", False, t(24)),
        *sent(25, 6),
        IntentEntry(3, orders[6]),
        says(29, 7),
        IntentEntry(3, orders[7]),
        *sent(31, 9),
        AckEntry(names[8], "o-9", b"{}", False, t(34)),
        FillEntry(b4, 2),
        *sent(36, 10),
        InDoubtEntry(names[9], "an answer this release does not read", b"?", False, t(39)),
        *sent(40, 11),
        AckEntry(names[10], "o-11", b"{}", False, t(43)),
        IntentEntry(3, orders[11]),
        says(45, 12, stopped, stop=StopState.FIRED),
        IntentEntry(3, orders[12]),
        says(47, 13, no_position, stop=StopState.UNREADABLE, close_entry=1),
        ReadEntry("execution/send", at, blob("broker-read-2"), t(47)),
        DisputedFillEntry(again, 48),
        OutsideRecordEntry(
            broker_record("20261001::f1e2", "-0.0278", at=t(49), received_at=t(50)), 48
        ),
        UnmatchedRecordEntry(
            broker_record(
                "20261001::d3c4", "5000", RecordKind.DEPOSIT, at=t(50), received_at=t(51)
            ),
            48,
            "the record 20261001::d3c4, 5000 as a deposit, is no fee, debit interest or dividend",
        ),
        UnmatchedFillEntry(
            filled_at(52, "f::x", "x-1", 3, "20.5", spy),
            48,
            "the fill f::x is of the broker's order x-1, which no order was acknowledged under",
        ),
        UnmatchedOrderEntry(
            foreign("x-1", 3, instrument=spy, submitted_at=t(51)),
            48,
            "the broker lists its order x-1 open, 3 of SPY under no client order id, which no ack "
            "mapped",
        ),
        # A recover the next day takes the order a crash left sending, and a whole list omits it.
        InDoubtEntry(names[5], "its send stage ended with no answer journaled", None, False, t(54)),
        ReadEntry("execution/recover", next_day, blob("lookup-1"), next_day),
        LostEntry(names[5], 55, "the broker's whole order list omits it"),
    ]
    return [(record, t(n)) for n, record in enumerate(records, start=1)]


# What an entry's body may hold of each enumeration, which #142's item 3 says
# never changes once a journal outside a test is opened: one renamed would make
# every old entry that holds it undecodable. A new member is added here.
FROZEN = {
    BrokerState: "working pending_cancel filled cancelled expired rejected unknown",
    FillBasis: "signal_close official_close quote",
    Limit: (
        "max_order_qty max_position max_gross max_open_orders max_net loss_stop moc_cutoff stop "
        "close_entry in_doubt position"
    ),
    OrderType: "market market_on_close",
    RecordKind: "fee interest dividend deposit withdrawal other",
    StopState: "absent fired unreadable",
    TimeInForce: "day cls",
}


@pytest.mark.parametrize("kind", FROZEN, ids=lambda kind: kind.__name__)
def test_3_no_member_of_an_enumeration_an_entry_holds_is_renamed(kind: type) -> None:
    assert {member.value for member in kind} == set(FROZEN[kind].split()), (
        "none may be renamed or removed, and a new one is added above"
    )


def test_3_every_enumeration_an_entry_can_hold_is_frozen_above() -> None:
    found: set[type] = set()

    def look(hint: Any) -> None:
        hint = _strip(hint)
        for arg in get_args(hint):
            look(arg)
        if isinstance(hint, type) and issubclass(hint, Enum):
            found.add(hint)
        elif dataclasses.is_dataclass(hint):
            for inner in get_type_hints(hint, include_extras=True).values():
                look(inner)

    for kind in KINDS.values():
        look(kind)
    assert found == set(FROZEN)


def test_2_this_release_writes_the_committed_bytes_of_every_kind(tmp_path: Path) -> None:
    journal = Journal(FolderStore(tmp_path), FIXED, "0123456789abcdef" * 2)
    view = journal.view()
    for record, at in _committed():
        _, view = journal.record(view, record, at)
    committed = sorted((ORDERS / "journal" / FIXED).iterdir())
    written = sorted((tmp_path / "journal" / FIXED).iterdir())
    assert [p.name for p in written] == [p.name for p in committed]
    for one, other in zip(written, committed, strict=True):
        assert one.read_bytes() == other.read_bytes(), other.name
    kinds = {record_of(entry).__class__ for entry in journal.read()}
    assert kinds == set(KINDS.values())


def test_2_the_committed_journal_replays_to_what_was_written_out_by_hand(tmp_path: Path) -> None:
    """`replay.json` was worked out by hand from the entries, and no test writes it."""
    expected = json.loads((ORDERS / "replay.json").read_text())
    afterwards = expected.pop("afterwards")
    shutil.copytree(ORDERS / "journal", tmp_path / "journal")
    journal = Journal(FolderStore(tmp_path), FIXED)
    whole = journal.view()
    # After the dispute come a fee that moves the cash and three entries that move nothing.
    assert {
        "through": whole.through.seq,
        "cash": format(whole._cash, "f"),
        "records": list(whole.records),
        "unmatched": [[one.seq, one.kind, one.name] for one in whole.unmatched],
    } == afterwards
    with pytest.raises(Halt) as halted:
        journal.replay()
    assert halted.value.reason == expected["dispute"]
    view = journal.view(through=expected["through"])
    book = view.book
    assert {
        "through": book.replayed_through,
        "dispute": halted.value.reason,
        "cash": format(book.cash, "f"),
        "positions": {
            strategy: {i: format(qty, "f") for i, qty in sorted(held.items())}
            for strategy, held in sorted(book.positions_by_strategy.items())
        },
        "open": {
            order.client_order_id: format(order.qty, "f")
            for _, orders in sorted(book.open_orders_by_strategy.items())
            for order in orders
        },
        "in_doubt": list(book.in_doubt),
        "orders": {
            name: {
                "state": order.state.value,
                "filled": format(order.filled, "f"),
                "broker": order.broker_order_id,
                "entries": " ".join(f"{e.seq}:{e.kind}" for e in order.entries),
            }
            for name, order in sorted(view.orders.items())
        },
        "fills": list(view.fills),
        "newest_sent_at": view.sent_at.isoformat(),
    } == expected


def test_2_the_committed_folder_holds_nothing_else() -> None:
    assert sorted(p.name for p in ORDERS.iterdir()) == ["journal", "replay.json"]
    assert [p.name for p in (ORDERS / "journal").iterdir()] == [FIXED]


def test_2_the_old_fixture_still_reads_and_its_replay_halts_on_its_kind(tmp_path: Path) -> None:
    shutil.copytree(
        Path(__file__).parent / "fixtures" / "journal" / "format-1", tmp_path, dirs_exist_ok=True
    )
    journal = Journal(FolderStore(tmp_path), "fixture")
    assert len(list(journal.read())) == 3
    with pytest.raises(Halt, match="entry 1 is of the kind 'fixture'"):
        journal.view()
