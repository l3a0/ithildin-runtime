"""What recover journals from the broker that the platform did not cause, #151.

Each record reaches recover through the broker's activities read, and the
simulated account books none, as Alpaca's paper account books none. So a broker
that wraps the simulator answers that read, and an `EditCash` fault moves the
simulated cash the way the record says the broker did. Every figure is made up.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.core import (
    ACCOUNT_OUTPUT,
    AccountRead,
    BrokerRecord,
    DecisionResult,
    Fill,
    FillsRead,
    Halt,
    InstrumentId,
    RecordKind,
    RecordsRead,
    broker_read_key,
    decode,
    decode_output,
    encode,
)
from ithildinruntime.execution.brokers.faults import EditCash, Faults, ForeignOrder
from ithildinruntime.execution.outside import (
    RANGES,
    Range,
    fill_entry,
    load_ranges,
    order_entry,
    record_entry,
)
from ithildinruntime.journal import EXPLAINING, Repeat
from tests.support.gates import d as D
from tests.support.journals import (
    GLD,
    broker_record,
    fill,
    foreign,
    placed,
    planned,
    route,
    started,
    unmatched_fill,
)
from tests.support.recover_world import FILES, RecoverWorld
from tests.support.sim_world import DEPLOYMENT, close_of
from tests.test_recover import (
    CAPTURED,
    DAY,
    DAYS,
    SECRET,
    broker_fill,
    doctored,
    faulted,
    halts,
    page,
    ready,
)

HELD = "which holds the account until a person releases it, and nothing releases one yet"
# The decision `ready` makes.
AT = close_of(DAYS[3])


def made(
    record_id: str = "20261105::a1",
    amount: object = "-1.25",
    kind: RecordKind = RecordKind.FEE,
    **changes: object,
) -> BrokerRecord:
    """A record dated the evening before the decision `ready` makes."""
    return broker_record(
        record_id, amount, kind, at=AT - timedelta(hours=20), received_at=AT, **changes
    )


def listing(records: Iterable[BrokerRecord]):
    """The activities read, as a broker answers it: each record dated at or after `since`."""
    held = tuple(records)
    return lambda since: RecordsRead(tuple(r for r in held if r.at >= since), page(), True)


def booked(*records: BrokerRecord, moved: object | None = None) -> tuple[RecoverWorld, object]:
    """`ready`, where the broker lists the records and its cash moved by their sum, or `moved`."""
    total = sum((r.amount for r in records), Decimal(0)) if moved is None else D(moved)
    world = faulted(Faults(events={1: EditCash(total)})) if total else ready()
    return world, doctored(world, activities=listing(records))


def attempt(world: RecoverWorld, brokers) -> DecisionResult:
    return world.attempt(world.stage(brokers=brokers))


# Item 1: a record on the closed list, inside its range, moves the book's cash and halts nothing


@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        (RecordKind.FEE, "-0.03"),
        (RecordKind.FEE, "-50.00"),
        (RecordKind.FEE, "-1.2345"),
        (RecordKind.INTEREST, "-0.01"),
        (RecordKind.DIVIDEND, "12.5"),
        (RecordKind.DIVIDEND, "-500"),
    ],
)
def test_1_a_listed_record_inside_its_range_explains_the_cash(
    kind: RecordKind, amount: str
) -> None:
    record = made(amount=amount, kind=kind, instrument=GLD)
    world, brokers = booked(record)
    result = attempt(world, brokers)
    assert result.halt is None, result.halt.reason
    assert world.kinds(world.before)[:2] == ["read", "outside_record"]
    view = world.view()
    assert view.records[record.record_id] == record
    # The book's cash comes from the journal, so it now agrees with the broker's.
    assert view.book.cash == world.broker().account().cash


def test_1_with_no_record_the_same_move_of_cash_halts_as_unexplained() -> None:
    world, brokers = booked(moved="-1.25")
    reason = halts(
        world, "cash is", ["account.json"], ["read", "fill"], CAPTURED, attempt(world, brokers)
    )
    assert reason.startswith("the book and the broker differ")


def test_1_a_record_that_explains_part_of_a_move_halts_on_the_rest() -> None:
    world, _ = booked(moved="-2.00")
    brokers = doctored(world, activities=listing([made(amount="-1.25")]))
    result = attempt(world, brokers)
    assert result.halt is not None and "cash is" in result.halt.reason
    assert world.kinds(world.before) == ["read", "outside_record", "fill"]


UNMATCHED = {
    "a deposit": (made(amount="1000", kind=RecordKind.DEPOSIT), "is no fee, debit interest"),
    "a withdrawal": (made(amount="-1000", kind=RecordKind.WITHDRAWAL), "is no fee, debit"),
    "a type off the list": (made(amount="-1", kind=RecordKind.OTHER), "is no fee, debit"),
    "a fee above its range": (made(amount="-50.01"), "is outside its range of -50.00 to -0.01"),
    "a fee that is a credit": (made(amount="0.01"), "is outside its range of -50.00 to -0.01"),
    "credit interest": (
        made(amount="0.40", kind=RecordKind.INTEREST),
        "is outside its range of -50.00 to -0.01",
    ),
    "a dividend past its range": (
        made(amount="500.01", kind=RecordKind.DIVIDEND),
        "is outside its range of -500.00 to 500.00",
    ),
    "a record of nothing": (made(amount="0", kind=RecordKind.DIVIDEND), "moves no cash"),
    "more places than cash holds": (made(amount="-1.00001"), "carries more places than cash"),
    "an amount the adapter flagged": (
        made(amount="-1.25", excess_places=("amount",)),
        "carries more places than cash holds",
    ),
    "an unnamed instrument": (
        made(instrument=InstrumentId(SECRET)),
        "names an instrument whose name is none",
    ),
}


@pytest.mark.parametrize("case", UNMATCHED)
def test_1_any_other_record_is_kept_as_unmatched_moves_nothing_and_holds(case: str) -> None:
    record, said = UNMATCHED[case]
    world, brokers = booked(record)
    result = attempt(world, brokers)
    reason = halts(
        world, said, ["account.json"], ["read", "unmatched_record", "fill"], CAPTURED, result
    )
    assert f"keeps an unmatched record, {HELD}" in reason
    assert SECRET not in reason
    view = world.view()
    assert record.record_id not in view.records
    (kept,) = view.unmatched
    assert (kept.kind, kept.name, kept.held) == ("record", record.record_id, record)
    # It holds every later decision, and is not journaled again.
    world.prepare(world.at + DAY)
    again = attempt(world, brokers)
    assert again.halt is not None and said in again.halt.reason
    assert world.kinds(world.before) == ["read"]


def test_1_an_identical_repeat_is_skipped_at_every_later_read() -> None:
    record = made()
    world, brokers = booked(record, replace(record, received_at=AT + DAY), moved=record.amount)
    assert attempt(world, brokers).halt is None
    assert world.kinds(world.before).count("outside_record") == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"amount": D("-1.30")},
        {"at": AT - timedelta(hours=19)},
        {"instrument": GLD},
        {"kind": RecordKind.INTEREST},
    ],
    ids=["amount", "date", "instrument", "kind"],
)
def test_1_a_repeat_with_other_content_is_kept_beside_the_first_and_holds(changes) -> None:
    first = made()
    world, brokers = booked(first)
    first_result = world.recover(world.stage(brokers=brokers))
    assert first_result.halt is None, first_result.halt.reason
    world.plan(())
    world.prepare(world.at + DAY)
    other = replace(first, **changes)
    result = attempt(world, doctored(world, activities=listing([other])))
    said = "repeats an id the journal holds with other content"
    halts(world, said, ["account.json"], ["read", "unmatched_record"], CAPTURED, result)
    assert world.view().records[first.record_id] == first


def test_1_a_record_whose_id_the_journal_refuses_halts_and_is_not_journaled() -> None:
    world, brokers = booked(made(SECRET))
    reason = halts(
        world,
        "a record whose id the journal refuses",
        ["account.json"],
        ["read"],
        CAPTURED,
        attempt(world, brokers),
    )
    assert SECRET not in reason


# Item 2: the records are read with the fills' bound, so one posted late is still fetched


def test_2_the_records_are_asked_from_the_fills_bound() -> None:
    asked: list[datetime] = []
    world = ready()

    def activities(since: datetime) -> RecordsRead:
        asked.append(since)
        return RecordsRead((), page(), True)

    assert attempt(world, doctored(world, activities=activities)).halt is None
    assert asked == [world.entry(ACCOUNT_OUTPUT).since]


def test_2_a_record_posted_a_day_late_is_fetched_at_the_next_read_and_explains_it() -> None:
    """Posted after one decision's read and dated before it, inside the day the bound reaches."""
    world = ready()
    assert world.recover().halt is None
    world.plan(())
    late = broker_record(
        "20261105::late", "-0.50", at=world.at - timedelta(hours=2), received_at=world.at + DAY
    )
    world.faults = Faults(events={1: EditCash(late.amount)})
    world.restart()
    world.prepare(world.at + DAY)
    result = attempt(world, doctored(world, activities=listing([late])))
    assert result.halt is None, result.halt.reason
    assert "outside_record" in world.kinds(world.before)


def test_2_a_record_dated_before_the_bound_is_never_fetched_and_its_cash_halts() -> None:
    world = ready()
    assert world.recover().halt is None
    world.plan(())
    world.prepare(world.at + DAY)
    old = broker_record("20261101::old", "-0.50", at=world.at - 5 * DAY, received_at=world.at)
    world.faults = Faults(events={1: EditCash(old.amount)})
    world.restart()
    result = attempt(world, doctored(world, activities=listing([old])))
    assert result.halt is not None and "cash is" in result.halt.reason
    assert world.entry(ACCOUNT_OUTPUT).records.records == ()
    assert "outside_record" not in world.kinds(world.before)


# Item 3: an open order no ack mapped is kept as unmatched and holds


def test_3_an_order_the_platform_did_not_send_is_kept_as_unmatched_and_holds() -> None:
    world = faulted(Faults(events={1: ForeignOrder(GLD, D(7))}))
    said = "the broker lists its order sim-o-4 open, 7 of GLD under no client order id"
    reason = halts(world, said, ["account.json"], ["read", "fill", "unmatched_order"], CAPTURED)
    assert f"keeps an unmatched order, {HELD}" in reason
    world.prepare(world.at + DAY)
    again = world.attempt()
    assert again.halt is not None and said in again.halt.reason
    # It filled at the close, so it is no longer open and is kept once, and so is its fill.
    assert world.kinds(world.before) == ["read", "unmatched_fill"]
    assert "the fill sim-f-3 is of the broker's order sim-o-4" in again.halt.reason


def test_3_an_unmatched_fill_kept_once_and_its_repeat_with_other_content_beside_it() -> None:
    world = ready()
    odd = broker_fill(world, broker_order_id="b-unknown")
    reads = {"fills": lambda since: _fills(odd)}
    world.attempt(world.stage(brokers=doctored(world, **reads)))
    world.prepare(world.at + DAY)
    other = replace(odd, price=odd.price + 1)
    result = world.attempt(world.stage(brokers=doctored(world, fills=lambda s: _fills(other))))
    assert result.halt is not None
    assert world.kinds(world.before) == ["read", "unmatched_fill"]
    kept = [one.held for one in world.view().unmatched]
    assert kept == [odd, other]
    assert "repeats a fill kept as unmatched, with other content" in result.halt.reason


def _fills(*fills: Fill) -> FillsRead:
    return FillsRead(fills, page(), True)


# What is kept as unmatched is never kept while an order may be with the broker unmapped


@pytest.mark.parametrize("state", ["Sending", "InDoubt"])
def test_3_nothing_is_kept_as_unmatched_while_an_order_is_in_doubt(state: str) -> None:
    one = planned(1, 4)
    journal = started(one)
    view = journal.view()
    for record in route(state, one):
        _, view = journal.record(view, placed(journal, record), AT)
    name = one.client_order_id
    with pytest.raises(Halt, match=f"not kept as unmatched while the order {name} may be"):
        fill_entry(view, fill("f1", "x1"), 2)
    with pytest.raises(Halt, match=f"not kept as unmatched while the order {name} may be"):
        order_entry(view, foreign(), 2)
    # A repeat of a fill kept as unmatched, with other content, waits the same way.
    kept = fill("f2", "x2")
    _, view = journal.record(view, unmatched_fill(kept), AT)
    with pytest.raises(Halt, match=f"not kept as unmatched while the order {name} may be"):
        fill_entry(view, replace(kept, price=kept.price + 1), 2)
    # A record is never the platform's order, so an order in doubt changes nothing for one.
    assert record_entry(view, broker_record(), 2, load_ranges()) is not None


def test_3_an_order_an_ack_mapped_is_left_to_the_comparison() -> None:
    one = planned(1, 4)
    journal = started(one)
    view = journal.view()
    for record in route("Acknowledged", one, broker="x1"):
        _, view = journal.record(view, placed(journal, record), AT)
    assert order_entry(view, foreign("x1"), 2) is None
    assert view.unmatched_repeats("order", "x1", foreign("x1")) is Repeat.NEW


# The ranges file


def test_the_tracked_ranges_name_exactly_the_closed_list() -> None:
    ranges = load_ranges()
    assert set(ranges) == set(EXPLAINING)
    assert ranges[RecordKind.FEE] == Range(D("-50.00"), D("-0.01"))
    assert ranges[RecordKind.INTEREST] == Range(D("-50.00"), D("-0.01"))
    assert ranges[RecordKind.DIVIDEND] == Range(D("-500.00"), D("500.00"))
    assert RANGES.is_file()


GOOD = RANGES.read_text()


@pytest.mark.parametrize(
    ("text", "said"),
    [
        ("", "is not of format 1"),
        ("format = 2\n" + GOOD.partition("format = 1\n")[2], "is not of format 1"),
        (GOOD.replace("[interest]", "[credit]"), "names credit, dividend, fee"),
        (GOOD + '\n[deposit]\nleast = "0"\nmost = "1"\n', "names deposit, dividend, fee"),
        (GOOD.replace('least = "-50.00"', "least = -50.00", 1), "no amount of dollars and cents"),
        (GOOD.replace('least = "-50.00"', 'least = "-50.001"', 1), "no amount of dollars"),
        (GOOD.replace('least = "-50.00"', 'least = "NaN"', 1), "no amount of dollars"),
        (GOOD.replace('least = "-50.00"', 'least = "1.00"', 1), "least of 1.00, above its most"),
        (GOOD.replace('most = "-0.01"', 'most = "-0.01"\nmore = "1"', 1), "other than a least"),
        ("format = 1\n[fee", "cannot be read: TOMLDecodeError"),
    ],
)
def test_a_ranges_file_that_is_wrong_halts(tmp_path: Path, text: str, said: str) -> None:
    path = tmp_path / "records.toml"
    path.write_text(text)
    with pytest.raises(Halt, match=said):
        load_ranges(path)


def test_a_missing_ranges_file_halts(tmp_path: Path) -> None:
    with pytest.raises(Halt, match="records.toml cannot be read: FileNotFoundError"):
        load_ranges(tmp_path / "records.toml")


def test_a_bad_ranges_file_halts_every_decision_ahead_of_any_record(monkeypatch) -> None:
    import ithildinruntime.execution.sync as sync

    def broken() -> object:
        raise Halt("records.toml is not of format 1")

    world = ready()
    monkeypatch.setattr(sync, "load_ranges", broken)
    result = world.attempt()
    assert result.halt is not None and "records.toml is not of format 1" in result.halt.reason
    assert world.kinds(world.before) == ["read"]


def test_a_record_in_a_stored_capture_reads_back_as_it_was() -> None:
    record = made(instrument=GLD)
    world, brokers = booked(record)
    attempt(world, brokers)
    stored = world.entry(ACCOUNT_OUTPUT)
    assert stored.records.records == (record,)
    assert decode(encode(stored), type(stored)) == stored


def test_a_second_attempt_whose_read_holds_a_new_record_halts_as_a_move() -> None:
    from ithildinruntime.execution.recover import _moved

    record = made()
    world, brokers = booked(record)
    attempt(world, brokers)
    first = world.entry(ACCOUNT_OUTPUT)
    without = replace(first, records=replace(first.records, records=()))
    assert _moved(without, first) == ["records"]
    # One only the first holds, dated before the later read's bound, is no move.
    later = replace(without, since=record.at + timedelta(microseconds=1))
    assert _moved(first, later) == []
    assert _moved(first, replace(without, since=record.at)) == ["records"]


def test_a_second_attempt_that_reads_a_record_again_later_is_no_move() -> None:
    from ithildinruntime.execution.recover import _moved, differences

    world, brokers = booked(made())
    attempt(world, brokers)
    first = world.entry(ACCOUNT_OUTPUT)
    stamped = tuple(replace(r, received_at=r.received_at + DAY) for r in first.records.records)
    later = replace(first, records=replace(first.records, records=stamped))
    assert _moved(first, later) == []
    assert differences(first, later).startswith("Only the times the pages came differ")


def test_3_an_order_the_platform_did_not_send_that_stays_open_is_kept_once() -> None:
    world = ready()
    stray = foreign("x-stray", 7, submitted_at=AT - DAY)
    listed = world.broker().open_orders()
    reads = {"open_orders": lambda: replace(listed, orders=(*listed.orders, stray))}
    said = "the broker lists its order x-stray open, 7 of GLD"
    first = attempt(world, doctored(world, **reads))
    assert first.halt is not None and said in first.halt.reason
    assert world.kinds(world.before)[-1] == "unmatched_order"
    world.prepare(world.at + DAY)
    listed = world.broker().open_orders()
    again = attempt(world, doctored(world, **reads))
    assert again.halt is not None and said in again.halt.reason
    assert world.kinds(world.before) == ["read"]


def test_a_range_of_one_value_loads(tmp_path: Path) -> None:
    path = tmp_path / "records.toml"
    path.write_text(GOOD.replace('least = "-50.00"', 'least = "-0.01"', 1))
    assert load_ranges(path)[RecordKind.FEE] == Range(D("-0.01"), D("-0.01"))


def test_only_the_sort_builds_an_entry_that_moves_cash_from_a_record() -> None:
    """The ranges are checked where an `outside_record` is built, so nothing else in src builds one.

    The fold reads no range, so an old entry replays the same after a range moves.
    A call by a bare name or through a module counts, and so does an import that
    renames the kind, which would hide a call from the first two.
    """
    name = "OutsideRecordEntry"
    src = Path(__file__).parent.parent / "src" / "ithildinruntime"
    builders, renamed = set(), set()
    for path in src.rglob("*.py"):
        where = str(path.relative_to(src))
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                called = node.func
                if getattr(called, "id", None) == name or getattr(called, "attr", None) == name:
                    builders.add(where)
            if isinstance(node, ast.alias) and node.name == name and node.asname:
                renamed.add(where)
    assert builders == {"execution/outside.py"}
    assert renamed == set()


def test_recovers_files_are_of_format_2_whose_capture_holds_the_records_read() -> None:
    world, brokers = booked(made())
    assert attempt(world, brokers).halt is None
    for name in FILES:
        assert world.file(name).header.format == 2, name


# The send stage reads the broker through the same sort


@pytest.mark.parametrize("listed", [True, False], ids=["with its record", "with none"])
def test_a_fee_booked_between_recover_and_send_is_explained_only_by_its_record(
    listed: bool,
) -> None:
    from tests.support.sim_world import FULL
    from tests.test_send import PAIR, world

    made = world()
    made.ready(close_of(FULL), PAIR)
    fee = broker_record("20261105::send", "-0.75", at=made.at, received_at=made.at)
    # The simulator counts every call of the account, so this edit lands at the send's first read.
    made.faults = Faults(events={made.log.calls + 1: EditCash(fee.amount)})
    records = listing([fee] if listed else [])
    stage = made.sender(brokers=doctored(made, activities=records))
    result = made.send_attempt(stage)
    if listed:
        assert result.halt is None, result.halt.reason
        assert "outside_record" in made.kinds(made.before)
        # The stored broker read the `read` entry names holds the record, at format 2.
        key = broker_read_key(DEPLOYMENT, made.run, made.at, 1)
        stored = decode_output(made.store.blobs[key], AccountRead)
        assert stored.header.format == 2
        (read,) = stored.entries
        assert read.records.records == (fee,)
        assert result.header.format == 2
    else:
        assert result.halt is not None and "the book and the broker differ" in result.halt.reason
        assert made.submits() == []


def test_an_order_the_platform_did_not_send_between_recover_and_send_sends_nothing() -> None:
    from tests.support.sim_world import FULL
    from tests.test_send import PAIR, world

    made = world()
    made.ready(close_of(FULL), PAIR)
    made.faults = Faults(events={made.log.calls + 1: ForeignOrder(GLD, D(7))})
    result = made.send_attempt()
    assert result.halt is not None and f"keeps an unmatched order, {HELD}" in result.halt.reason
    assert "unmatched_order" in made.kinds(made.before)
    assert "decision" not in made.kinds(made.before)
    assert made.submits() == []


def test_a_fill_of_no_order_between_recover_and_send_sends_nothing() -> None:
    from tests.support.sim_world import FULL
    from tests.test_send import PAIR, world

    made = world()
    made.ready(close_of(FULL), PAIR)
    odd = fill("f-foreign-1", "b-foreign-1", at=made.at, received_at=made.at)

    def fills(since: datetime) -> FillsRead:
        return FillsRead((*made.broker().fills(since).fills, odd), page(), True)

    result = made.send_attempt(made.sender(brokers=doctored(made, fills=fills)))
    assert result.halt is not None and f"keeps an unmatched fill, {HELD}" in result.halt.reason
    assert "unmatched_fill" in made.kinds(made.before)
    assert made.submits() == []
