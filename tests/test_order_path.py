"""The order path: what it journals, what it sends, and what it does when a step fails.

Each test's number is the acceptance item of #143 it shows. A test builds a
`World` that sends, and changes one thing.
"""

from __future__ import annotations

import threading
from asyncio import CancelledError
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    Ack,
    BlobRef,
    BrokerUnavailable,
    Halt,
    JournalAck,
    OrderIntent,
    OrderState,
    OrderType,
    Outcome,
    Retry,
    StopState,
    StoreUnavailable,
    TimeInForce,
    account_halts_prefix,
    caps_key,
    journal_entry_key,
    kill_switch_key,
    loss_stop_key,
    off_switch_key,
    result_key,
    run_halt_key,
    sha256_of,
    strategy_halts_prefix,
)
from ithildinruntime.journal import (
    RAW_LIMIT,
    AbandonedEntry,
    AckEntry,
    DisputedFillEntry,
    FillEntry,
    InDoubtEntry,
    IntentEntry,
    Journal,
    OpeningEntry,
    ReadEntry,
    RejectedEntry,
    SendingEntry,
    VerdictEntry,
)
from tests.support import gates
from tests.support.broken_paths import (
    BrokenPath,
    DoubledPath,
    FlippedPath,
    HandedPath,
    TwicePath,
    UnapprovedPath,
    UnsentPath,
)
from tests.support.fake_broker import FakeBroker, raising, says
from tests.support.journals import ack, fill, route
from tests.support.order_world import (
    ACCOUNT,
    AT,
    CASH,
    DEPLOYMENT,
    OTHER_WRITER,
    RUN,
    Kill,
    World,
    halted,
    last,
    order,
    planted_verdict,
)

ID = order(1, 2).client_order_id
SENT = ["intent", "verdict", "sending", "ack"]
PAIR = (order(1, 2), order(2, -3, gates.USO))


def down(number: int, op: str, key: str) -> None:
    raise StoreUnavailable(key, "AccessDenied: the store's own text")


# 1. An order that sends, and the fake broker's four refusals


def test_1_an_order_sends_once_with_every_entry_in_order() -> None:
    world = World()
    assert world.send() is OrderState.ACKNOWLEDGED
    assert world.kinds() == SENT
    assert world.broker.had == [world.orders[0]]
    assert world.alarms.sent == []
    assert world.rebuilt() == 1
    ack = last(world)
    assert ack == AckEntry(ID, "b-1", b'{"id": "b-1"}', False, AT)
    (entry,) = [r for r in world.records() if isinstance(r, VerdictEntry)]
    assert entry.snapshot == world.refs["snapshot"]
    assert entry.constraints == world.refs["constraints"]
    assert entry.valuation == world.refs["valuation"]
    assert entry.caps_sha256 == sha256_of(world.store.blobs[caps_key(ACCOUNT)])
    assert (entry.now, entry.stop, entry.close_entry) == (AT, StopState.ABSENT, None)
    assert entry.book == 4


def test_1_it_sends_through_the_folder_store(tmp_path) -> None:
    world = World()
    store = FolderStore(tmp_path / "store")
    for key, data in world.store.blobs.items():
        store.create(key, data, sha256_of(data))
    root = tmp_path / "store"

    def stored() -> dict[str, bytes]:
        found = (p for p in root.rglob("*.json") if not p.relative_to(root).parts[0][0] == ".")
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in found}

    world.store = store  # type: ignore[assignment]
    world.broker = FakeBroker(stored, ACCOUNT, AT)
    path = world.path(world.account())
    assert path.send(ID) is OrderState.ACKNOWLEDGED
    assert path.send(ID) is OrderState.ACKNOWLEDGED
    assert world.submits == 1
    view = Journal(store, ACCOUNT).view()
    assert [e.kind for e in view.orders[ID].entries] == SENT


@pytest.mark.parametrize(
    ("kind", "why"),
    [
        (UnapprovedPath, "has no approval in the journal"),
        (UnsentPath, "has no sending stored as its newest entry"),
        (TwicePath, "has been here before"),
        (DoubledPath, "is not as its decision plans it"),
        (FlippedPath, "is not as its decision plans it"),
    ],
)
def test_1_the_fake_broker_refuses_a_broken_path(kind: type[BrokenPath], why: str) -> None:
    world = World()
    path = kind(world.account()._shared, world.broker, world.report)
    try:
        path.send(ID)
    except AssertionError:
        pass
    (refused,) = world.broker.take_refusals()
    assert refused == f"{ID} {why}"


def test_1_a_broken_path_takes_no_broker_but_the_fake_one() -> None:
    world = World()

    class Other(FakeBroker):
        pass

    for broker in (object(), Other(lambda: {}, ACCOUNT, AT)):
        with pytest.raises(TypeError, match="a fake broker alone"):
            UnapprovedPath(world.account()._shared, broker, world.report)


def test_1_a_refusal_the_path_swallowed_still_fails_the_test() -> None:
    """The path reads a refusal as an order in doubt, so the fixture is what fails the test."""
    world = World()
    path = DoubledPath(world.account()._shared, world.broker, world.report)
    assert path.send(ID) is OrderState.IN_DOUBT
    assert world.broker.refused
    world.broker.take_refusals()


# 2. A kill and a refusing store at every call


def every_call(world: World) -> list[tuple[str, str]]:
    """Each store call a send makes, as its operation and its key."""
    world.send()
    return world.store.calls


EVERY = every_call(World())
CALLS = len(EVERY)
CREATES = [n for n, (op, _) in enumerate(EVERY, 1) if op == "create"]
# The number of the last call before the broker's. A view and an append follow it.
BEFORE_SUBMIT = CALLS - 2


def test_2_the_calls_of_one_send_are_the_ones_the_steps_name() -> None:
    world = World()
    world.send()
    names = [(op, key.rpartition("/")[2] or key.split("/")[-2]) for op, key in world.store.calls]
    control = [
        ("get", "switch.json"),
        ("list", "halts"),
        ("get", "halt.json"),
        ("get", "result.json"),
    ]
    strategy = [("get", "off.json"), ("list", "halts")]
    files = [
        ("get", name)
        for file in ("snapshot", "constraints", "valuation")
        for name in ("result.json", f"{file}.json")
    ]
    assert names == [
        ("list", ACCOUNT),
        *(("get", f"{n:012d}.json") for n in (1, 2, 3)),
        ("get", "caps.json"),
        ("create", "000000000004.json"),
        *control,
        *strategy,
        *files,
        ("list", ACCOUNT),
        ("get", "stop.json"),
        ("create", "000000000005.json"),
        ("create", "000000000006.json"),
        ("list", ACCOUNT),
        ("get", "000000000006.json"),
        *control,
        ("get", "stop.json"),
        *strategy,
        ("list", ACCOUNT),
        ("create", "000000000007.json"),
    ]
    assert CALLS == len(names) and CREATES == [6, 21, 22, CALLS]


@pytest.mark.parametrize("after", [False, True], ids=["before", "after"])
@pytest.mark.parametrize("at", range(1, CALLS + 1))
def test_2_a_kill_at_a_store_call(at: int, after: bool) -> None:
    world = World()

    def kill(number: int, op: str, key: str) -> None:
        if number == at:
            raise Kill

    if after:
        world.store.after = kill
    else:
        world.store.before = kill
    with pytest.raises(Kill):
        world.send()
    landed = sum(1 for n in CREATES if n < at or (after and n == at))
    assert world.kinds() == SENT[:landed]
    assert world.submits == (at > BEFORE_SUBMIT)
    assert world.alarms.sent == []
    world.store.before = world.store.after = lambda n, op, key: None
    # A new process finds what is stored, and asks the broker nothing twice.
    account = world.account(OTHER_WRITER)
    match landed:
        case 0:
            assert world.send(account=account) is OrderState.ACKNOWLEDGED
        case 1 | 2:
            with pytest.raises(Halt, match="was left (Intended|Approved)"):
                world.send(account=account)
        case 3:
            assert world.send(account=account) is OrderState.SENDING
            assert world.reader().in_doubt()[0].intent == world.orders[0]
        case _:
            assert world.send(account=account) is OrderState.ACKNOWLEDGED
    assert world.submits == (1 if landed in (0, 4) or at > BEFORE_SUBMIT else 0)


@pytest.mark.parametrize("at", range(1, CALLS + 1))
def test_2_a_store_that_refuses_one_call_is_asked_again(at: int) -> None:
    world = World()

    def refuse_once(number: int, op: str, key: str) -> None:
        if number == at:
            down(number, op, key)

    world.store.before = refuse_once
    assert world.send() is OrderState.ACKNOWLEDGED
    assert (world.kinds(), world.submits) == (SENT, 1)
    # After the submit the first refusal alarms at once.
    assert len(world.alarms.sent) == (at > BEFORE_SUBMIT)


@pytest.mark.parametrize("at", CREATES)
def test_2_an_append_whose_answer_is_lost_is_resent_unchanged(at: int) -> None:
    world = World()

    def lose(number: int, op: str, key: str) -> None:
        if number == at:
            down(number, op, key)

    world.store.after = lose
    assert world.send() is OrderState.ACKNOWLEDGED
    assert (world.kinds(), world.submits) == (SENT, 1)


@pytest.mark.parametrize(
    ("at", "lost"),
    [*((n, False) for n in range(1, CALLS + 1)), *((n, True) for n in CREATES)],
)
def test_2_a_store_down_from_a_call_on(at: int, lost: bool) -> None:
    """Nothing goes out, the halt says so, and a store that is back settles what was owed."""
    world = World()
    account = world.account()

    def before(number: int, op: str, key: str) -> None:
        if number > at or (number == at and not lost):
            down(number, op, key)

    def after(number: int, op: str, key: str) -> None:
        if number == at and lost:
            down(number, op, key)

    world.store.before, world.store.after = before, after
    with pytest.raises(Halt) as raised:
        world.send(account=account)
    reason = raised.value.reason
    # `core.Storage` says what S3 answered for a decision's own files, and nothing else does.
    assert "the store's own text" not in reason or reason.startswith("S3 did not answer a read")
    landed = [k for n, k in zip(CREATES, SENT, strict=True) if n < at or (lost and n == at)]
    assert world.kinds() == landed
    sent = at > BEFORE_SUBMIT
    assert world.submits == sent
    assert world.alarms.sent, "no halt blob reaches monitoring while the store is down"
    if sent:
        assert f"the ack of {ID}, which was sent, as the broker's b-1" in world.alarms.sent[0]
    owed = account.unstored()
    assert bool(owed) == (at >= CREATES[2])
    world.store.before = world.store.after = lambda n, op, key: None
    if at < CREATES[0] or (at == CREATES[0] and not lost):
        assert world.send(account=account) is OrderState.ACKNOWLEDGED
    elif at < CREATES[2]:
        with pytest.raises(Halt, match="was left (Intended|Approved)"):
            world.send(account=account)
        assert account.unstored() == ()
    else:
        # What this process alone knew is stored before anything else is sent.
        want = OrderState.ACKNOWLEDGED if sent else OrderState.ABANDONED
        assert world.send(account=account) is want
        assert account.unstored() == ()
        assert world.reader().in_doubt() == ()
    assert world.submits <= 1
    assert len({o.client_order_id for o in world.broker.had}) == len(world.broker.had)


def test_2_a_halt_for_a_store_that_is_down_names_what_the_journal_holds() -> None:
    world = World()
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 5)))
    reason = halted(world, f"the verdict of {ID} was not stored: StoreUnavailable")
    assert f"and the journal showed {ID} Intended" in reason
    assert "the store's own text" not in reason + str(world.alarms.sent)


@pytest.mark.parametrize("state", ["none", "Intended", "Approved"])
def test_2_abandon_records_what_was_never_sent(state: str) -> None:
    world = World()
    for record in route(state, world.orders[0]):
        world.record(_numbered(world, record))
    assert world.path().abandon(ID, "a partner was refused") is OrderState.ABANDONED
    assert world.state() is OrderState.ABANDONED
    assert last(world) == AbandonedEntry(ID, "a partner was refused")
    assert (
        world.kinds()[-2:]
        == {
            "none": ["intent", "abandoned"],
            "Intended": ["intent", "abandoned"],
            "Approved": ["verdict", "abandoned"],
        }[state]
    )


@pytest.mark.parametrize(
    "state",
    [
        *("Refused", "Abandoned", "Sending", "Abandoned late", "InDoubt", "Acknowledged"),
        *("PartlyFilled", "Filled", "Rejected", "Rejected late", "Expired", "Expired unfilled"),
    ],
)
def test_2_abandon_halts_on_every_other_state_and_stores_nothing(state: str) -> None:
    world = World()
    for record in route(state, world.orders[0]):
        world.record(_numbered(world, record))
    stored = dict(world.store.blobs)
    halted(world, "so it is not abandoned", lambda: world.path().abandon(ID, "no"))
    assert world.store.blobs == stored


def _numbered(world: World, record: object) -> object:
    """A verdict on the book right before it, as the fold asks."""
    if isinstance(record, VerdictEntry):
        return replace(record, book=world.reader().view().through.seq)
    return record


def test_2_abandon_halts_on_an_order_in_no_plan() -> None:
    world = World()
    halted(world, "in no decision", lambda: world.path().abandon(order(2, 1).client_order_id, "x"))
    assert world.kinds() == []


def test_2_abandon_starts_over_when_its_number_is_taken() -> None:
    world = World()
    beside(world, lambda n, op, key: op == "create" and key.endswith("04.json"))
    assert world.path().abandon(ID, "no") is OrderState.ABANDONED
    assert world.kinds() == ["intent", "abandoned"]


def test_2_abandon_with_the_store_down_says_what_the_journal_shows() -> None:
    world = World()
    world.store.refuse(("create", "journal/"))
    halted(world, "showed no entry of ith-chan", lambda: world.path().abandon(ID, "no"))
    assert world.alarms.sent


def test_2_an_answer_the_journal_did_not_take_is_settled_at_the_next_decision() -> None:
    """The ack is refused at decision N, and at N+1 recover settles it and halts on nothing."""
    world = World()
    account = world.account()
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt, match="the journal did not take the ack"):
        world.send(account=account)
    assert account.unstored() == (f"the ack of {ID}, which was sent, as the broker's b-1",)
    with pytest.raises(Halt, match="the journal still does not take the ack"):
        account.settle(world.report)
    assert world.reader().in_doubt()
    world.store.before = lambda n, op, key: None
    account.settle(world.report)
    assert account.unstored() == () and world.reader().in_doubt() == ()
    assert world.kinds() == SENT
    account.settle(world.report)
    assert world.kinds() == SENT


def test_2_a_second_path_stores_the_answer_before_it_sends() -> None:
    world = World(orders=PAIR)
    account = world.account()
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt):
        world.send(1, account)
    with pytest.raises(Halt, match="the journal still does not take"):
        world.send(2, account)
    with pytest.raises(Halt, match="the journal still does not take"):
        world.path(account).hold([PAIR[1].client_order_id])
    assert world.submits == 1
    world.store.before = lambda n, op, key: None
    assert world.send(2, account) is OrderState.ACKNOWLEDGED
    assert world.kinds() == [*SENT, *SENT]
    assert world.rebuilt() == 2


def test_2_a_kept_answer_the_journal_already_shows_is_done() -> None:
    world = World()
    account = world.account()
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt):
        world.send(account=account)
    world.store.before = lambda n, op, key: None
    world.record(AckEntry(ID, "b-1", b"", False, AT))
    account.settle(world.report)
    assert account.unstored() == ()
    assert world.kinds() == SENT


def test_2_a_kept_answer_the_fold_refuses_is_stored_as_in_doubt_with_its_bytes() -> None:
    world = World(orders=PAIR)
    account = world.account()
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt):
        world.send(account=account)
    world.store.before = lambda n, op, key: None
    # Another order has taken the broker's id in the meantime.
    for record in route("Acknowledged", PAIR[1], broker="b-1"):
        world.record(_numbered(world, record))
    account.settle(world.report)
    assert account.unstored() == ()
    found = last(world)
    assert isinstance(found, InDoubtEntry) and found.raw == b'{"id": "b-1"}'
    assert f"the order {ID} is in doubt" in world.alarms.sent[-1]


def test_2_a_kept_doubt_whose_order_moved_on_is_done_with_an_alarm() -> None:
    world = World()
    account = world.account()
    world.broker.answer = raising(ValueError("x"))
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt):
        world.send(account=account)
    world.store.before = lambda n, op, key: None
    world.record(AckEntry(ID, "b-1", b"", False, AT))
    account.settle(world.report)
    assert account.unstored() == ()
    assert world.alarms.sent[-1] == (
        f"the order {ID} was kept as in doubt, and the journal shows it Acknowledged"
    )


def test_2_an_answer_the_journal_can_never_take_stays_named_and_stops_every_send() -> None:
    world = World(orders=PAIR)
    account = world.account()
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt):
        world.send(account=account)
    world.store.before = lambda n, op, key: None
    world.record(RejectedEntry(ID, "no", b"", False, AT))
    for _ in range(2):
        with pytest.raises(Halt, match="the journal still does not take the ack"):
            world.send(2, account)
    assert account.unstored() and world.submits == 1


class Report(Exception):
    pass


@pytest.mark.parametrize("error", [Report("x"), SystemExit("x"), Halt("x")])
def test_2_a_report_that_raises_changes_nothing(error: BaseException) -> None:
    world = World()
    world.report_error = error
    assert world.send() is OrderState.ACKNOWLEDGED
    assert (world.kinds(), world.submits) == (SENT, 1)
    quiet = World()
    quiet.send()
    assert world.reports == quiet.reports > 10
    assert world.reader().view().orders[ID].entries == quiet.reader().view().orders[ID].entries


def test_2_report_is_called_after_each_append_and_each_broker_call() -> None:
    world = World()
    told: list[str] = []
    world.store.after = lambda n, op, key: told.append(op) if op == "create" else None
    path = world.account().path(world.broker, lambda: told.append("report"))

    def answer(intent: OrderIntent, number: int) -> Ack:
        told.append("submit")
        return Ack(Outcome.ACCEPTED, "b-1", None, b"", AT)

    world.broker.answer = answer
    path.send(ID)
    for what in ("create", "create", "create", "submit", "create"):
        at = told.index(what)
        assert told[at + 1] == "report"
        told = told[at + 1 :]


def test_2_hold_stops_at_the_first_order_the_gate_refuses() -> None:
    small = {"max_order_qty": {gates.GLD: gates.LOOSE, gates.USO: gates.d(1)}}
    small["max_position"] = small["max_order_qty"]
    world = World(orders=(*PAIR, order(3, 1)), constraints=(gates.constraints(**small),))
    names = [o.client_order_id for o in world.orders]
    assert world.path().hold(names) == (OrderState.APPROVED, OrderState.REFUSED)
    assert world.submits == 0
    assert world.kinds() == ["intent", "verdict", "intent", "verdict"]
    assert [world.state(n) for n in (1, 2, 3)] == [OrderState.APPROVED, OrderState.REFUSED, None]
    # The order it left approved is never sent later, and `abandon` ends it.
    halted(world, "was left Approved")
    assert world.path().abandon(names[0], "a partner was refused") is OrderState.ABANDONED


def test_2_hold_leaves_an_order_that_halts_at_its_first_step_to_abandon() -> None:
    world = World(orders=(order(1, "1.5"),))
    halted(world, "no multiple", lambda: world.path().hold([ID]))
    assert world.kinds() == []
    assert world.path().abandon(ID, "no") is OrderState.ABANDONED


def test_2_an_order_with_an_entry_is_not_sent_again() -> None:
    world = World()
    account = world.account()
    assert world.send(account=account) is OrderState.ACKNOWLEDGED
    assert world.send(account=account) is OrderState.ACKNOWLEDGED
    assert world.send(account=world.account(OTHER_WRITER)) is OrderState.ACKNOWLEDGED
    assert world.submits == 1


@pytest.mark.parametrize("state", ["Refused", "Abandoned", "Sending", "InDoubt", "Rejected"])
def test_2_an_order_that_went_further_returns_its_state(state: str) -> None:
    world = World()
    for record in route(state, world.orders[0]):
        world.record(_numbered(world, record))
    assert world.send() is OrderState(state)
    assert world.submits == 0


# 4. Each refusal of the private submit


def handed(world: World) -> tuple[HandedPath, JournalAck]:
    account = world.account()
    path = HandedPath(account._shared, world.broker, world.report)
    return path, path.sending(ID)


def test_4_an_acknowledgement_is_good_for_one_submit() -> None:
    world = World()
    path, ack = handed(world)
    path.handed(ack)
    assert world.submits == 1
    # Its order is still `Sending`, so nothing in the journal tells the second use apart.
    assert world.state() is OrderState.SENDING
    with pytest.raises(Halt, match="an acknowledgement this account is not holding"):
        path.handed(ack)
    assert world.submits == 1 and world.kinds() == SENT[:3]


def test_4_a_forged_or_swapped_acknowledgement_sends_nothing() -> None:
    world = World(orders=PAIR)
    path, ack = handed(world)
    forged = [
        JournalAck(ack.seq, ack.entry_hash),
        JournalAck(ack.seq, "0" * 64),
        JournalAck(ack.seq - 1, ack.entry_hash),
        JournalAck(99, ack.entry_hash),
    ]
    for made in forged:
        with pytest.raises(Halt, match="is not holding"):
            path.handed(made)
    assert world.submits == 0
    # A forged one takes nothing from the one the account holds.
    path.handed(ack)
    assert world.submits == 1


def test_4_an_acknowledgement_of_another_process_sends_nothing() -> None:
    world = World()
    path, ack = handed(world)
    other = HandedPath(world.account(OTHER_WRITER)._shared, world.broker, world.report)
    with pytest.raises(Halt, match="is not holding"):
        other.handed(ack)
    assert world.submits == 0


def test_4_an_order_moved_since_its_sending_is_not_sent() -> None:
    world = World()
    path, ack = handed(world)
    world.record(InDoubtEntry(ID, "another handler", None, False, AT))
    with pytest.raises(Halt, match="is not sending"):
        path.handed(ack)
    assert world.submits == 0


def test_4_an_approval_the_journal_has_moved_past_is_not_sent_on() -> None:
    """The fold takes a `sending` at any distance from its verdict, so the submit checks it."""
    world = World()
    account = world.account()
    path = HandedPath(account._shared, world.broker, world.report)
    approved = path._approve(ID)
    other = ReadEntry("execution/send", AT, BlobRef("r", "a" * 64), AT)
    world.record(other)
    ack = path._sending(ID, approved._replace(view=account._shared.journal.view()))
    with pytest.raises(Halt, match="has no verdict right before"):
        path.handed(ack)
    assert world.submits == 0


def test_4_a_sending_stored_as_other_bytes_is_not_sent() -> None:
    world = World()
    key = journal_entry_key(ACCOUNT, 6)

    def swap(number: int, op: str, at: str) -> None:
        if (op, at) == ("get", key):
            world.store.blobs[key] += b" "

    world.store.before = swap
    halted(world, "entry 6 is not stored as it was acknowledged")


def test_4_a_book_that_halts_between_the_sending_and_the_submit_ends_abandoned() -> None:
    world = World(position=Decimal(5))
    bad = replace(fill("old-f1", "old-1", 5, 2, gates.GLD), at=AT)
    at = next_create(world, 3)
    world.store.after = lambda n, op, key: (
        world.record(DisputedFillEntry(bad, 2)) if n == at else None
    )
    halted(world, "the fill old-f1 is in dispute")
    assert world.kinds()[-4:] == ["verdict", "sending", "disputed_fill", "abandoned"]


def next_create(world: World, count: int) -> int:
    """The number of the send's nth create, in a world whose journal starts longer."""
    other = World(orders=world.orders, position=world.position)
    other.send()
    return [n for n, (op, _) in enumerate(other.store.calls, 1) if op == "create"][count - 1]


# 6. What the broker answers


def test_6_an_accepted_order_with_the_brokers_own_copy() -> None:
    world = World()
    world.broker.answer = says(Outcome.ACCEPTED, "b-1", filled=Decimal(1))
    assert world.send() is OrderState.ACKNOWLEDGED
    assert last(world) == AckEntry(ID, "b-1", b"{}", False, AT + timedelta(seconds=1))


def test_6_a_rejected_order() -> None:
    world = World()
    world.broker.answer = says(Outcome.REJECTED, None, b"no shorts today")
    assert world.send() is OrderState.REJECTED
    found = last(world)
    assert isinstance(found, RejectedEntry) and found.raw == b"no shorts today"
    assert "shorts" not in found.reason and world.alarms.sent == []


def test_6_an_order_the_adapter_proves_was_not_sent() -> None:
    world = World()
    world.broker.answer = says(Outcome.NOT_SENT, None, b"")
    assert world.send() is OrderState.ABANDONED
    assert isinstance(last(world), AbandonedEntry) and world.alarms.sent == []


@pytest.mark.parametrize("outcome", [Outcome.UNKNOWN, Outcome.NOT_FOUND, Outcome.SEVERAL])
def test_6_every_other_outcome_is_in_doubt(outcome: Outcome) -> None:
    world = World()
    world.broker.answer = says(outcome, "b-1", b"what")
    assert world.send() is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry) and (found.raw, found.cut) == (b"what", False)
    (alarm,) = world.alarms.sent
    assert ID in alarm and "b-1" in alarm and ACCOUNT not in alarm


@pytest.mark.parametrize(
    "error",
    [ValueError("x"), SystemExit("x"), CancelledError("x"), BrokerUnavailable("submit", "Timeout")],
)
def test_6_whatever_a_submit_raises_is_in_doubt(error: BaseException) -> None:
    world = World()
    world.broker.answer = raising(error)
    assert world.send() is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry) and found.raw is None
    assert found.reason == f"the submit raised {type(error).__name__}"
    assert len(world.alarms.sent) == 1
    assert world.send() is OrderState.IN_DOUBT
    assert world.submits == 1


def test_6_a_halt_from_the_adapter_is_in_doubt_and_then_halts() -> None:
    world = World()
    world.broker.answer = raising(Halt("the simulated clock went back"))
    with pytest.raises(Halt, match="is in doubt: the simulated clock went back"):
        world.send()
    assert world.state() is OrderState.IN_DOUBT and len(world.alarms.sent) == 1


def test_6_an_error_that_ends_the_process_leaves_the_sending_alone() -> None:
    world = World()
    world.broker.answer = raising(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        world.send()
    assert world.kinds() == SENT[:3]
    assert world.reader().in_doubt()
    assert world.send() is OrderState.SENDING and world.submits == 1


@pytest.mark.parametrize("answer", [None, "accepted", object()])
def test_6_an_answer_that_is_no_ack_is_in_doubt(answer: object) -> None:
    world = World()
    world.broker.answer = lambda intent, number: answer
    assert world.send() is OrderState.IN_DOUBT
    assert len(world.alarms.sent) == 1


def forged(**changes: object) -> Callable[[OrderIntent, int], Ack]:
    """An `Ack` whose own checks were got around."""

    def answer(intent: OrderIntent, number: int) -> Ack:
        ack = Ack(Outcome.ACCEPTED, "b-1", None, b"{}", AT)
        for name, value in changes.items():
            object.__setattr__(ack, name, value)
        return ack

    return answer


@pytest.mark.parametrize(
    "changes",
    [
        {"broker_order_id": None},
        {"received_at": datetime(2026, 11, 3, 20, 40)},
        {"received_at": None},
        {"raw": None},
        {"raw": "text"},
        {"outcome": "shrug"},
        {"outcome": None},
        {"order": "b-1"},
    ],
)
def test_6_an_ack_that_is_none_of_the_things_it_says_is_in_doubt(changes: dict) -> None:
    world = World()
    world.broker.answer = forged(**changes)
    assert world.send() is OrderState.IN_DOUBT
    assert len(world.alarms.sent) == 1 and world.submits == 1
    found = last(world)
    assert isinstance(found, InDoubtEntry)
    assert found.raw == (None if "raw" in changes else b"{}")


def test_6_an_accepted_with_no_broker_id_is_no_ack_at_all() -> None:
    for broker in (None, ""):
        with pytest.raises(ValueError, match="no broker's order id"):
            Ack(Outcome.ACCEPTED, broker, None, b"", AT)
    world = World()
    world.broker.answer = lambda intent, n: Ack(Outcome.ACCEPTED, None, None, b"", AT)
    assert world.send() is OrderState.IN_DOUBT


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_order_id", "ith-other"),
        ("client_order_id", None),
        ("instrument", gates.USO),
        ("qty", Decimal(3)),
        ("qty", Decimal(-2)),
        ("type", OrderType.MARKET),
        ("type", None),
        ("time_in_force", TimeInForce.DAY),
        ("time_in_force", None),
        ("broker_order_id", "b-2"),
    ],
)
@pytest.mark.parametrize("outcome", [Outcome.ACCEPTED, Outcome.REJECTED, Outcome.NOT_SENT])
def test_6_a_copy_that_differs_in_any_field_is_in_doubt(
    outcome: Outcome, field: str, value: object
) -> None:
    world = World()
    world.broker.answer = says(outcome, "b-1", **{field: value})
    assert world.send() is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry)
    assert (found.raw, found.reason) == (b"{}", f"the broker's copy differs in {field}")
    assert len(world.alarms.sent) == 1


@pytest.mark.parametrize("outcome", [Outcome.ACCEPTED, Outcome.REJECTED, Outcome.NOT_SENT])
def test_6_an_answer_too_long_to_keep_whole_is_in_doubt(outcome: Outcome) -> None:
    world = World()
    world.broker.answer = says(outcome, "b-1", b"x" * (RAW_LIMIT + 1))
    assert world.send() is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry) and found.cut and found.raw == b"x" * RAW_LIMIT
    whole = World()
    whole.broker.answer = says(outcome, "b-1", b"x" * RAW_LIMIT)
    assert whole.send() is not OrderState.IN_DOUBT


@pytest.mark.parametrize("broker", ["b 1", "a" * 129, "acct.PA3FAKE"])
def test_6_a_broker_id_the_journal_refuses_is_in_doubt_with_its_bytes(broker: str) -> None:
    world = World()
    world.broker.answer = says(Outcome.ACCEPTED, broker, b"the answer")
    assert world.send() is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry) and found.raw == b"the answer"
    (alarm,) = world.alarms.sent
    assert ID in alarm and broker not in alarm


def test_6_a_broker_id_another_order_holds_is_in_doubt_with_its_bytes() -> None:
    world = World(orders=PAIR)
    world.broker.answer = says(Outcome.ACCEPTED, "b-1", b"the answer")
    account = world.account()
    assert world.send(1, account) is OrderState.ACKNOWLEDGED
    assert world.send(2, account) is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry) and found.raw == b"the answer"
    assert len(world.alarms.sent) == 1 and world.submits == 2


def test_6_an_order_in_doubt_alarms_even_where_its_entry_is_not_stored() -> None:
    world = World()
    world.broker.answer = raising(TimeoutError())
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt, match="did not take the in_doubt"):
        world.send()
    assert "is in doubt, perhaps sent: the submit raised TimeoutError" in world.alarms.sent[0]


def test_6_an_alarm_that_cannot_be_sent_halts_once_the_entry_is_stored() -> None:
    world = World()
    world.broker.answer = raising(TimeoutError())
    world.alarms.error = RuntimeError("the channel's own text")
    with pytest.raises(Halt, match="The alarm could not be sent: RuntimeError") as raised:
        world.send()
    assert "own text" not in raised.value.reason
    assert world.state() is OrderState.IN_DOUBT


def test_6_a_clock_that_fails_after_the_submit_loses_no_answer() -> None:
    world = World()
    plain = world.clock.now

    def broken() -> datetime:
        raise RuntimeError("no time")

    def answer(intent: OrderIntent, number: int) -> Ack:
        world.clock.now = broken  # type: ignore[method-assign]
        return Ack(Outcome.ACCEPTED, "b-1", None, b"", AT)

    world.broker.answer = answer
    assert world.send() is OrderState.ACKNOWLEDGED
    world.clock.now = plain  # type: ignore[method-assign]
    assert world.kinds() == SENT


# 7. Races


def entry(n: int = 1) -> ReadEntry:
    return ReadEntry("execution/send", AT, BlobRef(f"r{n}", "a" * 64), AT)


def beside(world: World, when: Callable[[int, str, str], bool], times: int = 1) -> list[int]:
    """Another writer's entry, stored right before the calls `when` picks."""
    left = [times]
    stored: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if left[0] and when(number, op, key):
            left[0] -= 1
            stored.append(world.record(entry(number)))

    world.store.before = before
    return stored


@pytest.mark.parametrize("at", range(1, CALLS + 1))
def test_7_another_writers_entry_between_each_pair_of_calls(at: int) -> None:
    world = World()
    beside(world, lambda n, op, key: n == at)
    try:
        state = world.send()
    except Halt as halt:
        # Its number was taken by the time the verdict or the sending was recorded.
        assert CREATES[0] < at <= CREATES[2] and "took the number" in halt.reason
        assert world.submits == 0 and world.state() is OrderState.ABANDONED
    else:
        assert state is OrderState.ACKNOWLEDGED and world.submits == 1
        assert at <= CREATES[0] + 13 or at > CREATES[2]
    assert world.reader().in_doubt() == ()
    assert world.rebuilt() == ("sending" in world.kinds())


# A view holds the journal's reading lock across its calls, so a thread that records inside
# one waits for it, and cannot come between them. The read of one entry holds nothing.
IN_A_VIEW = {
    n for n, (op, key) in enumerate(EVERY, 1) if key.startswith("journal/") and op != "create"
} - {CREATES[2] + 2}


@pytest.mark.parametrize("peek", [False, True], ids=["view", "head"])
@pytest.mark.parametrize("at", sorted({*range(1, CALLS + 1)} - IN_A_VIEW))
def test_7_another_threads_entry_on_the_same_journal(at: int, peek: bool) -> None:
    world = World()
    account = world.account()
    journal = account._shared.journal

    def record() -> None:
        if peek:
            journal.head()
        world.record(entry(), journal)

    def before(number: int, op: str, key: str) -> None:
        if number == at and not world.store.quiet:
            thread = threading.Thread(target=record)
            thread.start()
            thread.join(5)
            # A thread that waits on the journal's own lock would never end here.
            assert not thread.is_alive()

    world.store.before = before
    try:
        state = world.send(account=account)
    except Halt as halt:
        assert "took the number" in halt.reason
        assert world.submits == 0 and world.state() is OrderState.ABANDONED
    else:
        assert state is OrderState.ACKNOWLEDGED and world.submits == 1
    assert world.reader().in_doubt() == ()


def test_7_an_intent_that_loses_its_number_starts_over_five_times() -> None:
    for times, ends in ((5, True), (6, False)):
        world = World()
        stored = beside(
            world,
            lambda n, op, key: op == "create",
            times=times,
        )
        if ends:
            # The verdict and the sending are left alone once the five are used.
            assert world.send() is OrderState.ACKNOWLEDGED
            assert len(stored) == 5
        else:
            halted(world, "lost its intent's number 6 times")
            assert world.kinds() == ["read"] * 6 or world.state() is None


def test_7_an_answer_that_loses_its_number_is_appended_until_it_lands() -> None:
    world = World()
    beside(world, lambda n, op, key: op == "create" and int(key[-17:-5]) >= 7, times=20)
    assert world.send() is OrderState.ACKNOWLEDGED
    assert world.submits == 1 and world.state() is OrderState.ACKNOWLEDGED


def test_7_an_answer_that_loses_every_number_is_kept() -> None:
    world = World()
    account = world.account()
    beside(world, lambda n, op, key: op == "create" and int(key[-17:-5]) >= 7, times=1000)
    with pytest.raises(Halt, match="other writers took 100 numbers in a row"):
        world.send(account=account)
    world.store.before = lambda n, op, key: None
    account.settle(world.report)
    assert world.state() is OrderState.ACKNOWLEDGED and world.submits == 1


CONTROLS = {
    "kill switch": (kill_switch_key(ACCOUNT), True),
    "account halt": (account_halts_prefix(ACCOUNT) + "execution-cancel-1-1.json", True),
    "run halt": (run_halt_key(DEPLOYMENT, RUN), True),
    "this decision's send result": (
        result_key(DEPLOYMENT, RUN, AT, "execution/send"),
        True,
    ),
    "loss stop": (loss_stop_key(ACCOUNT), True),
    "strategy off switch": (off_switch_key(ACCOUNT, gates.CHAN), False),
    "strategy halt": (strategy_halts_prefix(ACCOUNT, gates.CHAN) + "a-b-1-1.json", False),
}


@pytest.mark.parametrize("name", CONTROLS)
@pytest.mark.parametrize("at", range(1, CALLS + 1))
def test_7_a_control_set_before_each_call_stops_the_order_until_its_last_read(
    name: str, at: int
) -> None:
    key, halts = CONTROLS[name]
    reads = [
        n
        for n, (op, k) in enumerate(EVERY, 1)
        if key.startswith(k) and k.startswith(("accounts", "runs")) and (op == "get") == (k == key)
    ]
    assert len(reads) == 2, reads
    world = World()
    world.store.before = lambda n, op, k: world.store.put(key, b"{}") if n == at else None
    if at > reads[-1]:
        assert world.send() is OrderState.ACKNOWLEDGED
        return
    if halts:
        halted(world, f"{name} stands")
    else:
        assert world.send() is OrderState.ABANDONED
        assert world.submits == 0
    late = at > reads[0]
    assert ("sending" in world.kinds()) == late
    ended = OrderState.REFUSED if name == "loss stop" and not late else OrderState.ABANDONED
    assert world.state() is ended


def test_7_a_strategy_that_is_off_never_hides_a_halt() -> None:
    world = World()
    world.store.put(off_switch_key(ACCOUNT, gates.CHAN), b"{}")
    world.store.put(run_halt_key(DEPLOYMENT, RUN), b"{}")
    halted(world, "run halt stands")


@pytest.mark.parametrize("at", range(1, CALLS + 1))
def test_7_a_fill_of_another_order_between_each_pair_of_calls(at: int) -> None:
    # An order of the session before is left acknowledged, and its fill lands now.
    world = World(plan=False)
    open_order(world)
    late = FillEntry(replace(fill("f-late", "b-early", 1, 1, gates.GLD), at=AT), 2)
    world.store.before = lambda n, op, key: world.record(late) if n == at else None
    try:
        assert world.send() is OrderState.ACKNOWLEDGED
    except Halt as halt:
        assert "took the number" in halt.reason
        assert world.state() is OrderState.ABANDONED and world.submits == 0
    assert world.rebuilt() == ("sending" in world.kinds()[4:])


def open_order(world: World) -> None:
    """A journal whose earlier decision left one order acknowledged and not filled."""
    at = AT - timedelta(days=1)
    early = order(1, 4, at=at)
    name = early.client_order_id
    world.record(OpeningEntry(ACCOUNT, CASH))
    world.record(entry())
    seq = world.decide((early,), at)
    world.record(IntentEntry(seq, early))
    world.record(planted_verdict(name, seq + 1))
    world.record(SendingEntry(name))
    world.record(ack(name, "b-early"))
    world.decide(world.orders)


@pytest.mark.parametrize("at", range(1, CALLS + 1))
def test_7_two_processes_sending_one_id_make_one_broker_call(at: int) -> None:
    """The second runs whole before call `at` of the first, under one reading of the clock."""
    world = World()
    first, second = world.account(), world.account(OTHER_WRITER)
    ended: list[object] = []

    def before(number: int, op: str, key: str) -> None:
        if number == at:
            world.store.before = lambda n, o, k: None
            try:
                ended.append(world.send(account=second))
            except Halt as halt:
                ended.append(halt.reason)

    world.store.before = before
    try:
        ended.append(world.send(account=first))
    except Halt as halt:
        ended.append(halt.reason)
    assert len(ended) == 2 and world.submits == 1, ended
    assert world.state() is OrderState.ACKNOWLEDGED
    assert world.kinds() == SENT


def test_7_two_threads_sending_one_id_make_one_broker_call() -> None:
    for _ in range(20):
        world = World()
        account = world.account()
        ended: list[object] = []

        def run(account=account, world=world, ended=ended) -> None:
            try:
                ended.append(world.send(account=account))
            except Halt as halt:
                ended.append(halt.reason)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert world.submits == 1 and len(ended) == 4, ended
        assert world.state() is OrderState.ACKNOWLEDGED


def test_7_a_sending_another_writer_stored_is_left_as_in_doubt() -> None:
    """Another writer may have the order at the broker, so it is not abandoned here."""
    world = World()
    other = Journal(world.store, ACCOUNT, OTHER_WRITER)

    def before(number: int, op: str, key: str) -> None:
        if op == "create" and key.endswith("06.json") and not world.store.quiet:
            world.store.before = lambda n, o, k: None
            world.record(SendingEntry(ID), other)

    world.store.before = before
    reason = halted(world, "took the number")
    assert f"The journal shows {ID} Sending" in reason
    assert world.alarms.sent and world.state() is OrderState.SENDING


# 8. The cutoff


MICRO = timedelta(microseconds=1)
RETRY = Retry(timedelta(seconds=600), 1.0, 4.0)


@pytest.mark.parametrize("later", ["strategy", "caps"])
@pytest.mark.parametrize("by", [-MICRO, timedelta(0), MICRO])
@pytest.mark.parametrize("read", ["first", "submit"])
def test_8_the_cutoff(later: str, by: timedelta, read: str) -> None:
    minutes = {"strategy": (10, 5), "caps": (5, 10)}[later]
    world = World(
        constraints=(gates.constraints(moc_cutoff=minutes[0]),),
        caps=gates.caps(moc_cutoff=minutes[1]),
    )
    cutoff = gates.CLOSE - timedelta(minutes=10)
    if read == "first":
        world.clock.at = cutoff + by
    else:
        # The clock moves once the `sending` is stored.
        def after(number: int, op: str, key: str) -> None:
            if number == CREATES[2]:
                world.clock.at = cutoff + by

        world.store.after = after
    if by > timedelta(0):
        halted(world, "the market-on-close cutoff")
        assert world.state() is OrderState.ABANDONED
        assert ("sending" in world.kinds()) == (read == "submit")
        assert "verdict" not in world.kinds() or read == "submit"
    else:
        assert world.send() is OrderState.ACKNOWLEDGED


def test_8_a_market_order_for_the_day_has_no_cutoff() -> None:
    made = order(1, 2, kind=OrderType.MARKET, tif=TimeInForce.DAY)
    world = World(orders=(made,), caps=gates.caps(moc_cutoff=390))
    world.clock.at = gates.CLOSE
    assert world.send() is OrderState.ACKNOWLEDGED


def test_8_the_tries_end_by_the_cutoff_and_one_at_it_is_made() -> None:
    """An intent is tried for the time left to the cutoff, and its abandoned for the deadline."""
    for left, waited in ((60, 60.0), (0, 0.0)):
        minutes = 20 - left // 60
        world = World(caps=gates.caps(moc_cutoff=minutes), retry=RETRY)
        world.clock.moves = True
        world.store.refuse(("create", journal_entry_key(ACCOUNT, 4)))
        told: list[float] = []
        plain = world.clock.wait

        def wait(seconds: float, told=told, plain=plain, world=world) -> None:
            told.append(seconds)
            # The tries of the intent end at the first view after them.
            plain(seconds)

        world.clock.wait = wait  # type: ignore[method-assign]
        halted(world, "the intent of")
        tries = world.store.calls[5:]
        first = [op for op, _ in tries].index("list")
        assert sum(told[: first - 1]) == waited
        assert first == len(told[: first - 1]) + 1
