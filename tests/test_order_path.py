"""The order path: what it journals, what it sends, and what it does when a step fails.

Each test's number is the acceptance item of #143 it shows. A test builds a
`World` that sends, and changes one thing.
"""

from __future__ import annotations

import json
import threading
from asyncio import CancelledError
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, tzinfo
from decimal import Decimal

import pytest

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    Ack,
    BlobRef,
    BrokerState,
    BrokerUnavailable,
    Halt,
    HardCaps,
    JournalAck,
    OrderIntent,
    OrderState,
    OrderType,
    Outage,
    Outcome,
    Retry,
    Snapshot,
    StopState,
    StoreError,
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
    View,
)
from tests import conftest
from tests.support import fake_broker, gates
from tests.support.broken_paths import (
    BrokenPath,
    DoubledPath,
    DownAfterCallPath,
    FlippedPath,
    HandedPath,
    TwicePath,
    UnapprovedPath,
    UnsentPath,
)
from tests.support.fake_broker import FakeBroker, Refused, check, raising, says
from tests.support.journals import ack, fill, route
from tests.support.order_world import (
    ACCOUNT,
    AT,
    CASH,
    DEPLOYMENT,
    NOW,
    OTHER_WRITER,
    RUN,
    WRITER,
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
WENT = f"which was sent at {NOW.isoformat()}, as the broker's b-1"


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
    assert ack == AckEntry(ID, "b-1", b'{"id": "b-1"}', False, NOW)
    (entry,) = [r for r in world.entries() if isinstance(r, VerdictEntry)]
    assert entry.snapshot == world.refs["snapshot"]
    assert entry.constraints == world.refs["constraints"]
    assert entry.valuation == world.refs["valuation"]
    assert entry.caps_sha256 == sha256_of(world.store.blobs[caps_key(ACCOUNT)])
    assert (entry.now, entry.stop, entry.close_entry) == (NOW, StopState.ABSENT, None)
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
    # `check` is the whole of what the fixture in conftest.py does after each test.
    with pytest.raises(Refused, match="is not as its decision plans it"):
        check()
    world.broker.take_refusals()
    check()


def test_1_the_fixture_checks_every_test() -> None:
    """The fixture runs around this test too, and calls `check` once it ends."""
    ran: list[str] = []
    plain = fake_broker.check
    fixture = conftest.no_order_reached_a_fake_broker_wrongly.__wrapped__
    steps = fixture()
    next(steps)
    fake_broker.check = lambda: ran.append("check")  # type: ignore[assignment]
    try:
        assert list(steps) == [] and ran == ["check"]
    finally:
        fake_broker.check = plain
    marker = conftest.no_order_reached_a_fake_broker_wrongly
    assert marker._fixture_function_marker.autouse


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
# The numbers of the two reads of the hard caps, the second among the submit's checks.
CAPS_READS = [n for n, (_, key) in enumerate(EVERY, 1) if key == caps_key(ACCOUNT)]


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
        # The settle's view, and its look for a blob of the loss stop no entry names.
        ("list", ACCOUNT),
        *(("get", f"{n:012d}.json") for n in (1, 2, 3)),
        ("head", "stop.json"),
        ("list", ACCOUNT),
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
        ("get", "caps.json"),
        *control,
        ("get", "stop.json"),
        *strategy,
        ("list", ACCOUNT),
        ("create", "000000000007.json"),
    ]
    assert CALLS == len(names) and CREATES == [8, 23, 24, CALLS]


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
    assert all("The owner's step: the broker's site" in a for a in world.alarms.sent)
    # An alarm of `core.Storage`'s own names no account.
    named = [a for a in world.alarms.sent if not a.startswith("S3 did not answer")]
    assert all(a.startswith(f"account {ACCOUNT}: ") for a in named)
    assert all("StoreUnavailable AccessDenied" in a for a in named)
    if sent:
        assert f"the ack of {ID}, {WENT}" in world.alarms.sent[0]
    if at >= CREATES[2] and not sent:
        # One page, and the `abandoned` is kept for the next settle. Where the path met the
        # outage among its checks, it paged and the entry is not tried (#211). Where the
        # `sending` or a read inside the journal's own append failed, the entry's page is the one.
        assert len(world.alarms.sent) == 1, world.alarms.sent
        held = f"The abandoned of {ID} is kept and tried again before the next send" in reason
        assert held or any("which was not sent" in a for a in named)
        assert held or "It is kept and tried again" in reason
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
    assert account.unstored() == (f"the ack of {ID}, {WENT}",)
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
    came = NOW + timedelta(minutes=2)
    raw = b'{"id": "b-1"}'
    world.broker.answer = lambda intent, n: Ack(Outcome.ACCEPTED, "b-1", None, raw, came)
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
    assert found.received_at == NOW + timedelta(minutes=2)
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
        f"account {ACCOUNT}: the order {ID} was kept as in doubt, perhaps sent at "
        f"{NOW.isoformat()}, and the journal shows it Acknowledged, as the broker's b-1"
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


def test_2_report_is_called_between_every_two_store_calls_of_a_send() -> None:
    """A view's own calls, each a read of one entry, are one call to it."""
    world = World()
    told: list[str] = []
    world.store.before = lambda n, op, key: told.append(key)
    path = world.account().path(world.broker, lambda: told.append("report"))
    path.send(ID)
    pairs = zip(told, told[1:], strict=False)
    silent = [(a, b) for a, b in pairs if "report" not in (a, b)]
    view = f"journal/{ACCOUNT}/"
    assert all(a.startswith(view) and b.startswith(view) for a, b in silent), silent
    assert (
        "report" in told[told.index(caps_key(ACCOUNT)) + 1 : told.index(view + "000000000004.json")]
    )


def test_2_report_is_called_after_each_append_and_each_broker_call() -> None:
    world = World()
    told: list[str] = []
    world.store.before = lambda n, op, key: told.append("call")
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


def test_4_a_sending_the_journal_refuses_ends_the_order_abandoned() -> None:
    """The journal halts on it, such as on entries that no longer replay."""
    world = World()
    account = world.account()
    journal = account._shared.journal
    plain = journal.record

    def record(view: View, entry: object, at: datetime) -> object:
        if isinstance(entry, SendingEntry):
            raise Halt("entry 4 does not replay")
        return plain(view, entry, at)

    journal.record = record  # type: ignore[method-assign]
    halted(world, "^entry 4 does not replay$", lambda: world.send(account=account))
    assert world.state() is OrderState.ABANDONED


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
    halt = pytest.raises(Halt, match="could not be sent to the broker")
    with halt:
        world.send()
    assert isinstance(last(world), AbandonedEntry) and world.alarms.sent == []
    assert world.state() is OrderState.ABANDONED and world.submits == 1
    lost = World()
    lost.broker.answer = world.broker.answer
    lost.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt, match="the abandoned of .*, which was not sent"):
        lost.send()


@pytest.mark.parametrize("outcome", [Outcome.UNKNOWN, Outcome.NOT_FOUND, Outcome.SEVERAL])
def test_6_every_other_outcome_is_in_doubt(outcome: Outcome) -> None:
    world = World()
    world.broker.answer = says(outcome, "b-1", b"what")
    assert world.send() is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry) and (found.raw, found.cut) == (b"what", False)
    (alarm,) = world.alarms.sent
    assert alarm.startswith(f"account {ACCOUNT}: the order {ID} is in doubt") and "b-1" in alarm


@pytest.mark.parametrize(
    "error",
    [
        *(ValueError("x"), SystemExit("x"), CancelledError("x"), KeyboardInterrupt("x")),
        BrokerUnavailable("submit", "Timeout"),
    ],
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
        {"outcome": []},
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
    if outcome is Outcome.NOT_SENT:
        return
    whole = World()
    whole.broker.answer = says(outcome, "b-1", b"x" * RAW_LIMIT)
    ended = {Outcome.ACCEPTED: OrderState.ACKNOWLEDGED, Outcome.REJECTED: OrderState.REJECTED}
    assert whole.send() is ended[outcome]


@pytest.mark.parametrize("broker", ["b 1", "a" * 129, "acct.PA3FAKE"])
def test_6_a_broker_id_the_journal_refuses_is_in_doubt_with_its_bytes(broker: str) -> None:
    world = World()
    world.broker.answer = says(Outcome.ACCEPTED, broker, b"the answer")
    assert world.send() is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry) and found.raw == b"the answer"
    assert found.reason == "the broker's id is one the journal refuses"
    (alarm,) = world.alarms.sent
    assert ID in alarm and broker not in alarm
    # A rejection's entry holds no id, so it is taken whatever the id.
    rejected = World()
    rejected.broker.answer = says(Outcome.REJECTED, broker)
    assert rejected.send() is OrderState.REJECTED and rejected.alarms.sent == []


@pytest.mark.parametrize("outcome", [Outcome.REJECTED, Outcome.NOT_SENT])
def test_6_an_end_whose_answer_was_lost_is_stored_once_and_let_go(outcome: Outcome) -> None:
    world = World()
    account = world.account()
    world.broker.answer = says(outcome, None, b"")
    world.store.after = lambda n, op, key: down(n, op, key) if n == CREATES[3] else None
    if outcome is Outcome.REJECTED:
        assert world.send(account=account) is OrderState.REJECTED
    else:
        with pytest.raises(Halt, match="could not be sent to the broker$"):
            world.send(account=account)
    assert account.unstored() == () and len(world.kinds()) == 4
    assert "the journal did not take" in world.alarms.sent[0]


def test_6_a_rejection_the_journal_did_not_take_names_its_id_and_when_it_went() -> None:
    for broker, says_ in (
        ("b-9", f"sent at {NOW.isoformat()}, as the broker's b-9, and rejected"),
        (None, f"sent at {NOW.isoformat()}, and rejected"),
    ):
        world = World()
        world.broker.answer = says(Outcome.REJECTED, broker)
        world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
        with pytest.raises(Halt):
            world.send()
        assert f"which was {says_}" in world.alarms.sent[0]


class Odd(Halt):
    """A kind of halt that holds no reason, as an adapter could write one."""

    def __init__(self) -> None:
        pass


class Spoken(Halt):
    def __init__(self, reason: object) -> None:
        Exception.__init__(self)
        self.reason = reason  # type: ignore[assignment]


def test_6_an_adapters_halt_is_said_even_where_its_entry_is_not_stored() -> None:
    world = World()
    world.broker.answer = raising(Halt("the simulated clock went back"))
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt, match="in doubt: the simulated clock went back. the journal did not"):
        world.send()


class Words(str):
    """Text of an adapter's own kind, whose code runs wherever it is used."""

    def __format__(self, spec: str) -> str:
        raise RuntimeError("PKFAKEKEY")


class Fatal(Halt):
    """A halt whose reason ends what reads it."""

    def __init__(self) -> None:
        Exception.__init__(self)

    @property
    def reason(self) -> str:  # type: ignore[override]
        raise SystemExit("PKFAKEKEY")


@pytest.mark.parametrize(
    "halt", [Odd(), Spoken(None), Spoken(ValueError("PKFAKEKEY")), Spoken(Words("a")), Fatal()]
)
def test_6_a_halt_with_no_reason_of_an_adapters_is_in_doubt_and_names_its_type(halt: Halt) -> None:
    world = World()
    world.broker.answer = raising(halt)
    with pytest.raises(Halt, match=f"is in doubt: {type(halt).__name__}$"):
        world.send()
    assert world.state() is OrderState.IN_DOUBT and len(world.alarms.sent) == 1

    def raises() -> object:
        raise halt

    world.broker.reads["positions"] = raises
    with pytest.raises(Halt, match=f"^{type(halt).__name__}$"):
        world.path().positions()


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
    sent = f"is in doubt, perhaps sent at {NOW.isoformat()}: the submit raised TimeoutError"
    assert sent in world.alarms.sent[0]


@pytest.mark.parametrize(
    "error",
    [RuntimeError("the channel's own text"), CancelledError(), SystemExit(), KeyboardInterrupt()],
)
def test_6_an_alarm_that_cannot_be_sent_halts_once_the_entry_is_stored(
    error: BaseException,
) -> None:
    world = World()
    world.broker.answer = raising(TimeoutError())
    world.alarms.error = error
    account = world.account()
    with pytest.raises(Halt, match="An alarm about it could not be sent") as raised:
        world.send(account=account)
    assert "own text" not in raised.value.reason
    assert world.state() is OrderState.IN_DOUBT
    # The alarm is tried again at each settle until it goes out, and halts nothing.
    assert account.unstored() == ()
    for tries in (2, 3):
        account.settle(world.report)
        assert len(world.alarms.sent) == tries
    world.alarms.error = None
    account.settle(world.report)
    account.settle(world.report)
    assert len(world.alarms.sent) == 4 and len(set(world.alarms.sent)) == 1
    assert world.kinds() == [*SENT[:3], "in_doubt"]


def test_6_an_alarm_lost_with_the_store_goes_out_when_the_channel_is_back() -> None:
    """One outage takes the broker, the store and the alert channel together."""
    world = World()
    account = world.account()
    world.broker.answer = raising(TimeoutError())
    world.alarms.error = ConnectionError("down")
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt, match="did not take the in_doubt"):
        world.send(account=account)
    world.alarms.error, world.alarms.sent = None, []
    with pytest.raises(Halt, match="still does not take the in_doubt"):
        account.settle(world.report)
    assert f"the order {ID} is in doubt" in world.alarms.sent[0]
    world.store.before = lambda n, op, key: None
    account.settle(world.report)
    assert account.unstored() == () and world.state() is OrderState.IN_DOUBT


def test_6_an_alarm_lost_while_an_answer_is_settled_is_tried_again() -> None:
    world = World(orders=PAIR)
    account = world.account()
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt):
        world.send(account=account)
    world.store.before = lambda n, op, key: None
    for record in route("Acknowledged", PAIR[1], broker="b-1"):
        world.record(_numbered(world, record))
    world.alarms.error = ConnectionError("down")
    account.settle(world.report)
    assert world.state() is OrderState.IN_DOUBT and account.unstored() == ()
    world.alarms.error, world.alarms.sent = None, []
    account.settle(world.report)
    account.settle(world.report)
    (alarm,) = world.alarms.sent
    assert f"the order {ID} is in doubt" in alarm


@pytest.mark.parametrize("ends", ["sent", "fails"])
def test_6_an_alarm_another_thread_is_trying_again_is_not_lost(ends: str) -> None:
    """`settle` holds the alarm on one thread until the send on another has ended."""
    world = World(orders=PAIR)
    account = world.account()
    trying, done = threading.Event(), threading.Event()
    sent: list[str] = []
    tries: list[str] = []

    def alarm(reason: str) -> None:
        tries.append(reason)
        if threading.current_thread() is not threading.main_thread():
            trying.set()
            assert done.wait(5)
            if ends == "sent":
                sent.append(reason)
                return
        raise ConnectionError("down")

    world.alarms.alarm = alarm  # type: ignore[method-assign]
    threads: list[threading.Thread] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", journal_entry_key(ACCOUNT, 7)) and not threads:
            world.store.before = lambda n, o, k: None
            threads.append(threading.Thread(target=_settled, args=(account, world)))
            threads[0].start()
            assert trying.wait(5)

    world.store.before = before
    world.broker.answer = raising(TimeoutError())
    with pytest.raises(Halt, match="An alarm about it could not be sent"):
        world.send(account=account)
    done.set()
    threads[0].join(5)
    world.alarms.alarm = sent.append  # type: ignore[method-assign]
    for _ in range(2):
        account.settle(world.report)
    assert [r for r in sent if f"the order {ID} is in doubt" in r] == sent and len(sent) == 1


def _settled(account: object, world: World) -> None:
    try:
        account.settle(world.report)  # type: ignore[attr-defined]
    except Halt:
        pass


def test_6_every_unsent_alarm_is_tried_once_at_each_settle_in_its_order() -> None:
    world = World(orders=PAIR)
    account = world.account()
    world.broker.answer = raising(TimeoutError())
    world.alarms.error = ConnectionError("down")
    for place in (1, 2):
        with pytest.raises(Halt):
            world.send(place, account)
    world.alarms.sent = []
    account.settle(world.report)
    first, second = world.alarms.sent
    assert PAIR[0].client_order_id in first and PAIR[1].client_order_id in second
    world.alarms.error = None
    reports = world.reports
    account.settle(world.report)
    account.settle(world.report)
    assert world.alarms.sent == [first, second, first, second]
    # A report follows each alarm, and nothing else of a settle with nothing kept.
    assert world.reports == reports + 2


# Each answer that says two things, by what its copy says the order has filled, as a share of it.
CONTRADICTIONS: dict[str, tuple[Outcome, str | None, bytes, dict]] = {
    "not sent with an id": (Outcome.NOT_SENT, "b-1", b"", {}),
    "not sent with bytes": (Outcome.NOT_SENT, None, b"{}", {}),
    "not sent with a working copy": (
        Outcome.NOT_SENT,
        "b-1",
        b"",
        {"state": BrokerState.WORKING},
    ),
    "not sent with a filled copy": (
        Outcome.NOT_SENT,
        "b-1",
        b"",
        {"state": BrokerState.FILLED, "filled": 1},
    ),
    "rejected with a working copy": (
        Outcome.REJECTED,
        "b-1",
        b"{}",
        {"state": BrokerState.WORKING},
    ),
    "rejected with a filled copy": (
        Outcome.REJECTED,
        "b-1",
        b"{}",
        {"state": BrokerState.FILLED, "filled": 1},
    ),
    "rejected with a part filled": (
        Outcome.REJECTED,
        "b-1",
        b"{}",
        {"state": BrokerState.REJECTED, "filled": Decimal("0.5")},
    ),
    "accepted with a rejected copy": (
        Outcome.ACCEPTED,
        "b-1",
        b"{}",
        {"state": BrokerState.REJECTED},
    ),
}


@pytest.mark.parametrize("qty", [2, -2])
@pytest.mark.parametrize("name", CONTRADICTIONS)
def test_6_an_answer_that_says_two_things_is_in_doubt(name: str, qty: int) -> None:
    """An order journaled as never sent leaves the book, and the next would double it."""
    outcome, broker, raw, fields = CONTRADICTIONS[name]
    if "filled" in fields:
        fields = fields | {"filled": fields["filled"] * Decimal(qty)}
    world = World(orders=(order(1, qty), order(2, qty)))
    world.broker.answer = says(outcome, broker, raw, **fields)
    account = world.account()
    assert world.send(1, account) is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry) and found.raw == raw
    assert len(world.alarms.sent) == 1
    assert world.send(2, account) is OrderState.REFUSED and world.submits == 1


def test_6_an_answer_that_agrees_with_its_copy_is_taken() -> None:
    world = World()
    world.broker.answer = says(Outcome.REJECTED, "b-1", state=BrokerState.REJECTED)
    assert world.send() is OrderState.REJECTED
    for state in (BrokerState.WORKING, BrokerState.FILLED, BrokerState.CANCELLED):
        world = World()
        world.broker.answer = says(Outcome.ACCEPTED, "b-1", state=state)
        assert world.send() is OrderState.ACKNOWLEDGED


def hostile(**raises: BaseException) -> Ack:
    """An `Ack` of an adapter's own kind, some of whose fields raise as they are read."""

    def field(error: BaseException) -> property:
        def read(self: object) -> object:
            raise error

        return property(read)

    kind = type("Hostile", (Ack,), {name: field(error) for name, error in raises.items()})
    made = object.__new__(kind)
    fields = {"outcome": Outcome.ACCEPTED, "broker_order_id": "b-1", "order": None}
    for name, value in (fields | {"raw": b"{}", "received_at": AT}).items():
        if name not in raises:
            object.__setattr__(made, name, value)
    return made


def bent(**fields: object) -> Ack:
    """An `accepted` whose fields are whatever an adapter put there after it was made."""
    made = Ack(Outcome.ACCEPTED, "b-1", None, b"{}", AT)
    for name, value in fields.items():
        object.__setattr__(made, name, value)
    return made


class Lazy:
    """A proxy whose class cannot be asked for."""

    @property
    def __class__(self) -> type:  # type: ignore[override]
        raise RuntimeError("PKFAKEKEY")


class Compared:
    """A broker's copy that raises as it is compared with the order sent."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    @property
    def client_order_id(self) -> str:
        raise self._error


class _Nameless(type):
    @property
    def __name__(cls) -> str:  # type: ignore[override]
        raise RuntimeError("PKFAKEKEY")


class Ours(str):
    def __eq__(self, other: object) -> bool:
        raise SystemExit("PKFAKEKEY")

    __hash__ = str.__hash__


class Bytes(bytes):
    def __eq__(self, other: object) -> bool:
        raise SystemExit("PKFAKEKEY")

    __hash__ = bytes.__hash__


class Time(datetime):
    def astimezone(self, tz: object = None) -> Time:  # type: ignore[override]
        return self

    def isoformat(self, *given: object) -> str:  # type: ignore[override]
        raise SystemExit("PKFAKEKEY")

    strftime = __format__ = isoformat  # type: ignore[assignment]


NAMELESS = _Nameless("N", (Exception,), {})
NAMELESS_HALT = _Nameless("H", (Halt,), {})
ANSWERS: dict[str, Callable[[OrderIntent, int], object]] = {
    **{
        f"{name} raises {type(error).__name__}": (
            lambda intent, n, name=name, error=error: hostile(**{name: error})
        )
        for name in ("outcome", "broker_order_id", "order", "raw", "received_at")
        for error in (RuntimeError("PKFAKEKEY"), SystemExit("PKFAKEKEY"), KeyboardInterrupt())
    },
    **{
        f"its {name} is an adapter's own": (
            lambda intent, n, name=name, value=value: bent(**{name: value})
        )
        for name, value in (
            ("broker_order_id", Ours("b-1")),
            ("raw", Bytes(b"{}")),
            ("received_at", Time(2026, 11, 3, 20, 48, tzinfo=UTC)),
            ("outcome", Ours("accepted")),
            ("order", Lazy()),
        )
    },
    **{
        f"its order raises {type(error).__name__} as it is compared": (
            lambda intent, n, error=error: bent(order=Compared(error))
        )
        for error in (RuntimeError("PKFAKEKEY"), SystemExit("PKFAKEKEY"), KeyboardInterrupt())
    },
    "a lazy proxy": lambda intent, n: Lazy(),
    "an error with no name": raising(NAMELESS("PKFAKEKEY")),
    "a halt with no name": raising(NAMELESS_HALT("PKFAKEKEY")),
}


NAMED = {name for name in ANSWERS if name.startswith("its ") and "broker_order_id" not in name}


@pytest.mark.parametrize("name", ANSWERS)
def test_6_nothing_an_answer_does_as_it_is_read_makes_it_one_that_was_not_sent(name: str) -> None:
    world = World(orders=(order(1, 2), order(2, 2)))
    account = world.account()
    world.broker.answer = ANSWERS[name]
    try:
        state = world.send(1, account)
    except Halt as halt:
        assert "is in doubt" in halt.reason
        state = world.state()
    assert state is OrderState.IN_DOUBT
    (alarm,) = world.alarms.sent
    assert f"the order {ID} is in doubt" in alarm
    assert "sent at" in alarm and "not sent" not in alarm
    # The broker's id stands wherever it is plain text of a plain `Ack`.
    assert ("as the broker's b-1" in alarm) == (name in NAMED)
    said = [
        alarm,
        *(str({k: v for k, v in e.body.items() if k != "raw"}) for e in world.reader().read()),
    ]
    if "halt" not in name:
        assert not [text for text in said if "PKFAKEKEY" in text]
    # The order counts at its worst case, so the next is refused.
    assert world.send(2, account) is OrderState.REFUSED and world.submits == 1


def test_6_an_answer_of_an_adapters_own_kind_is_in_doubt_with_its_bytes() -> None:
    world = World()
    world.broker.answer = lambda intent, n: hostile()
    assert world.send() is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry)
    assert (found.reason, found.raw) == ("the submit gave a Hostile", b"{}")


def test_6_an_answer_whose_time_cannot_be_read_keeps_its_bytes() -> None:
    world = World()
    world.broker.answer = lambda intent, n: hostile(received_at=SystemExit("PKFAKEKEY"))
    assert world.send() is OrderState.IN_DOUBT
    found = last(world)
    assert isinstance(found, InDoubtEntry) and (found.raw, found.received_at) == (b"{}", NOW)


def test_6_a_time_whose_zone_is_an_adapters_own_is_kept_in_utc() -> None:
    class Once(tzinfo):
        late = False

        def utcoffset(self, dt: datetime | None) -> timedelta:
            if Once.late:
                raise RuntimeError("PKFAKEKEY")
            return timedelta(0)

        def dst(self, dt: datetime | None) -> timedelta:
            return timedelta(0)

    for outcome in (Outcome.ACCEPTED, Outcome.REJECTED, Outcome.UNKNOWN):
        world = World()
        Once.late = False

        def before(number: int, op: str, key: str) -> None:
            # The first call after the answer is read.
            Once.late = number > BEFORE_SUBMIT

        world.store.before = before
        answer = replace(bent(), outcome=outcome)
        object.__setattr__(answer, "received_at", AT.replace(tzinfo=Once()))
        world.broker.answer = lambda intent, n, answer=answer: answer
        world.send()
        assert last(world).received_at == AT and last(world).received_at.tzinfo is UTC


@pytest.mark.parametrize("raises", [False, True], ids=["answered", "raised"])
def test_6_an_error_of_the_paths_own_after_the_call_is_in_doubt(monkeypatch, raises: bool) -> None:
    from ithildinruntime.execution import order_path

    world = World()
    if raises:
        world.broker.answer = raising(TimeoutError())
    plain, made = order_path._Answer, []

    def once(*given: object) -> object:
        if not made:
            made.append(given)
            raise MemoryError
        return plain(*given)  # type: ignore[arg-type]

    monkeypatch.setattr(order_path, "_Answer", once)
    assert world.send() is OrderState.IN_DOUBT and world.submits == 1
    assert last(world).reason == "the submit raised MemoryError"


@pytest.mark.parametrize("error", [RuntimeError("no time"), SystemExit(), KeyboardInterrupt()])
def test_6_a_clock_that_fails_after_the_submit_loses_no_answer(error: BaseException) -> None:
    world = World()
    plain = world.clock.now

    def broken() -> datetime:
        raise error

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
            assert world.kinds() == [] and world.state() is None


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


@pytest.mark.parametrize("fault", ["missing", "malformed", "lowered"])
def test_7_a_strategy_that_is_off_never_hides_what_the_caps_say(fault: str) -> None:
    world = World()

    def before(number: int, op: str, k: str) -> None:
        if number != CREATES[2] + 1:
            return
        world.store.put(off_switch_key(ACCOUNT, gates.CHAN), b"{}")
        if fault == "missing":
            world.store.drop(caps_key(ACCOUNT))
        elif fault == "malformed":
            world.store.put(caps_key(ACCOUNT), b"{}")
        else:
            world.rewrite_caps(max_order_qty=ONE, max_position=ONE)

    world.store.before = before
    halted(world, "the hard caps")
    assert world.state() is OrderState.ABANDONED


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


READS = ["first", "after verdict", "after sending", "last"]


def moved(world: World, read: str, at: datetime) -> None:
    """Set the clock so that one of an order's three reads of its last moment sees `at`.

    The submit's is read after its controls, so the clock moves once the last
    store call before the broker's has answered.
    """
    if read == "first":
        world.clock.at = at
        return

    def after(number: int, op: str, key: str) -> None:
        at_call = {"after verdict": CREATES[1], "after sending": CREATES[2], "last": BEFORE_SUBMIT}
        if number == at_call[read]:
            world.clock.at = at

    world.store.after = after


def marked_at(quoted: bool) -> Snapshot:
    """What the gate marks at: a quote that is good for an hour, or a bar."""
    if not quoted:
        return gates.snapshot({gates.GLD: 1, gates.USO: 1})
    return gates.quoted({gates.GLD: (1, 1), gates.USO: (1, 1)}, at=AT, max_age=3600)


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("later", ["strategy", "caps"])
@pytest.mark.parametrize("by", [-MICRO, timedelta(0), MICRO])
@pytest.mark.parametrize("read", READS)
def test_8_the_cutoff(later: str, by: timedelta, read: str, quoted: bool) -> None:
    minutes = {"strategy": (10, 5), "caps": (5, 10)}[later]
    world = World(
        snapshot=marked_at(quoted),
        constraints=(gates.constraints(moc_cutoff=minutes[0]),),
        caps=gates.caps(moc_cutoff=minutes[1]),
    )
    moved(world, read, gates.CLOSE - timedelta(minutes=10) + by)
    if by > timedelta(0):
        reason = halted(world, "its market-on-close cutoff")
        assert ID in reason
        assert world.state() is OrderState.ABANDONED
        assert ("sending" in world.kinds()) == (read in ("after sending", "last"))
    else:
        assert world.send() is OrderState.ACKNOWLEDGED


@pytest.mark.parametrize("quoted", [False, True])
@pytest.mark.parametrize("by", [-MICRO, timedelta(0), MICRO])
@pytest.mark.parametrize("read", READS)
def test_8_an_order_for_the_day_goes_out_until_its_sessions_close(
    by: timedelta, read: str, quoted: bool
) -> None:
    made = order(1, 2, kind=OrderType.MARKET, tif=TimeInForce.DAY)
    world = World(orders=(made,), snapshot=marked_at(quoted), caps=gates.caps(moc_cutoff=390))
    moved(world, read, gates.CLOSE + by)
    if by > timedelta(0):
        halted(world, "its session's close")
        assert world.state() is OrderState.ABANDONED
        assert ("sending" in world.kinds()) == (read in ("after sending", "last"))
    else:
        assert world.send() is OrderState.ACKNOWLEDGED


def test_8_an_order_for_the_day_is_not_tried_past_its_sessions_close() -> None:
    made = order(1, 2, kind=OrderType.MARKET, tif=TimeInForce.DAY)
    world = World(orders=(made,), retry=RETRY)
    world.clock.at, world.clock.moves = gates.CLOSE - timedelta(seconds=30), True
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 6)))
    halted(world, "the sending of")
    # The `sending` for the 30 seconds left. Its outage paged, so its `abandoned` is tried once
    # and kept for the next settle, and nothing waits for it (#211).
    assert sum(world.clock.waits) == 30.0


@pytest.mark.parametrize(
    "step",
    [
        *("first control", "second view", "gate's stop"),
        *("verdict", "sending", "view", "control", "folder"),
    ],
)
def test_8_each_try_ahead_of_the_call_ends_by_the_cutoff(step: str) -> None:
    """Each is tried for the 53 seconds left to the cutoff, and never for the deadline."""
    world = World(caps=gates.caps(moc_cutoff=19), retry=RETRY)
    world.clock.moves = True
    calls = [None, *EVERY]
    number, key = {
        "first control": (0, kill_switch_key(ACCOUNT)),
        # Past the settle's view and the path's first, both before the caps are read.
        "second view": (
            calls.index(("list", "journal/paper-1/"), CAPS_READS[0]),
            "journal/paper-1/",
        ),
        "gate's stop": (0, loss_stop_key(ACCOUNT)),
        "verdict": (0, journal_entry_key(ACCOUNT, 5)),
        "sending": (0, journal_entry_key(ACCOUNT, 6)),
        "view": (CREATES[2] + 1, "journal/"),
        "control": (CREATES[2] + 3, kill_switch_key(ACCOUNT)),
        "folder": (CREATES[2] + 4, account_halts_prefix(ACCOUNT)),
    }[step]
    waited: list[float] = []
    gave_up: list[int] = []
    world.clock.wait = waited.append  # type: ignore[method-assign]

    def before(n: int, op: str, k: str) -> None:
        # The first refused call is tried until it gives up. What follows it goes through.
        if k.startswith(key) and n >= number and not gave_up:
            world.clock.elapsed = sum(waited)
            if sum(waited) >= 53:
                gave_up.append(n)
            down(n, op, k)

    world.store.before = before
    halted(world, "unreadable|not stored|could not be read")
    # The call after a lost `sending` is its `abandoned`, which has the whole deadline.
    assert gave_up and sum(waited) == 53.0


def test_8_the_clock_is_the_gates_now_and_the_answers_time() -> None:
    world = World()
    world.clock.at = AT + timedelta(minutes=3)
    late = AT + timedelta(minutes=4)

    def answer(intent: OrderIntent, number: int) -> object:
        world.clock.at = late
        raise TimeoutError

    world.broker.answer = answer
    assert world.send() is OrderState.IN_DOUBT
    (verdict,) = [r for r in world.entries() if isinstance(r, VerdictEntry)]
    assert verdict.now == AT + timedelta(minutes=3)
    found = last(world)
    assert isinstance(found, InDoubtEntry) and found.received_at == late


def test_8_the_tries_end_by_the_cutoff_and_one_at_it_is_made() -> None:
    """An intent is tried for the time left to the cutoff, and its abandoned for the deadline."""
    for left, waited in ((60, 60.0), (0, 0.0)):
        minutes = 20 - left // 60
        world = World(caps=gates.caps(moc_cutoff=minutes), retry=RETRY)
        world.clock.at = AT
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
        # Past the settle's view and its look for the blob, the path's view and the caps.
        tries = world.store.calls[CAPS_READS[0] :]
        first = [op for op, _ in tries].index("list")
        assert sum(told[: first - 1]) == waited
        assert first == len(told[: first - 1]) + 1


# 7. A control set while another is being read again


SEND_RESULT = (DEPLOYMENT, RUN, AT, "execution/send")
ONE = dict.fromkeys((gates.GLD, gates.USO), gates.d(1))
WAITS = [n for n in range(CREATES[2] + 1, BEFORE_SUBMIT + 1)]


@pytest.mark.parametrize("name", CONTROLS)
@pytest.mark.parametrize("at", WAITS)
def test_7_a_control_set_while_a_later_read_waits_stops_the_order(name: str, at: int) -> None:
    """The submit's reads count only when none of them waited, so each is made again."""
    key, halts = CONTROLS[name]
    world = World()

    def before(number: int, op: str, k: str) -> None:
        if number == at:
            world.store.put(key, b"{}")
            down(number, op, k)

    world.store.before = before
    if halts:
        halted(world, f"{name} stands")
    else:
        assert world.send() is OrderState.ABANDONED
    assert world.submits == 0 and world.state() is OrderState.ABANDONED


@pytest.mark.parametrize("at", WAITS)
def test_7_a_book_disputed_while_a_read_waits_stops_the_order(at: int) -> None:
    world = World(position=Decimal(5))
    shift = next_create(world, 3) - CREATES[2]
    bad = DisputedFillEntry(replace(fill("old-f1", "old-1", 5, 2, gates.GLD), at=AT), 2)

    def before(number: int, op: str, k: str) -> None:
        if number == at + shift:
            world.record(bad)
            down(number, op, k)

    world.store.before = before
    halted(world, "is in dispute")


def test_7_one_slow_pass_is_made_again_and_the_order_goes() -> None:
    world = World()
    slow: list[int] = []

    def after(number: int, op: str, k: str) -> None:
        if not slow and number > CREATES[2] and k == result_key(*SEND_RESULT):
            slow.append(number)
            world.clock.elapsed += 6

    world.store.after = after
    assert world.send() is OrderState.ACKNOWLEDGED and slow


@pytest.mark.parametrize("took", [4.9, 5.0, 5.1])
def test_7_a_pass_of_checks_that_took_too_long_is_made_again(took: float) -> None:
    """A read that answers slowly leaves the ones before it as stale as one tried again."""
    world = World()
    slow: list[int] = []

    def after(number: int, op: str, k: str) -> None:
        if not slow and number > CREATES[2] and k == result_key(*SEND_RESULT):
            slow.append(number)
            world.clock.elapsed += took
            world.store.put(kill_switch_key(ACCOUNT), b"{}")

    world.store.after = after
    if took > 5:
        halted(world, "kill switch stands")
    else:
        assert world.send() is OrderState.ACKNOWLEDGED


def test_7_checks_that_are_always_slow_end_by_their_deadline() -> None:
    world = World()

    def after(number: int, op: str, k: str) -> None:
        if number > CREATES[2] and k == result_key(*SEND_RESULT):
            world.clock.elapsed += 6

    world.store.after = after
    halted(world, "in every pass a read was tried again, or the pass took longer than 5 seconds")
    assert world.state() is OrderState.ABANDONED


@pytest.mark.parametrize("at", range(1, CALLS + 1))
def test_7_a_cap_lowered_before_the_call_stops_the_order(at: int) -> None:
    """The caps are read again among the checks, and an order their limits changed for halts."""
    reads = CAPS_READS
    assert len(reads) == 2 and CREATES[2] < reads[1] < BEFORE_SUBMIT
    world = World()
    world.store.before = lambda n, op, k: (
        world.rewrite_caps(max_order_qty=ONE, max_position=ONE) if n == at else None
    )
    if at <= reads[0]:
        assert world.send() is OrderState.REFUSED and world.submits == 0
    elif at <= reads[1]:
        halted(world, "the hard caps' limits changed since the send of")
        assert world.state() is OrderState.ABANDONED
    else:
        assert world.send() is OrderState.ACKNOWLEDGED


@pytest.mark.parametrize("appended", [5, 6])
def test_7_a_cap_lowered_while_an_append_waits_stops_the_order(appended: int) -> None:
    world = World()
    key = journal_entry_key(ACCOUNT, appended)
    waited: list[int] = []

    def before(number: int, op: str, k: str) -> None:
        if (op, k) == ("create", key) and len(waited) < 4:
            waited.append(number)
            world.rewrite_caps(max_order_qty=ONE, max_position=ONE)
            down(number, op, k)

    world.store.before = before
    halted(world, "the hard caps' limits changed")


@pytest.mark.parametrize("at", [CREATES[0], CREATES[1], CREATES[2] + 1])
def test_7_a_caps_blob_whose_limits_are_the_same_stops_nothing(at: int) -> None:
    """The owner may add a section another reader reads, during a send."""
    world = World()
    blob = json.loads(world.store.blobs[caps_key(ACCOUNT)])
    again = json.dumps(blob | {"holds": ["a cause"]}).encode() + b"\n"
    world.store.before = lambda n, op, k: (
        world.store.put(caps_key(ACCOUNT), again) if n == at else None
    )
    assert world.send() is OrderState.ACKNOWLEDGED
    assert world.store.blobs[caps_key(ACCOUNT)] == again


OTHER_CAPS: dict[str, object] = {
    "max_order_qty": {gates.GLD: Decimal(2 * 10**9), gates.USO: Decimal(10**9)},
    "max_position": {gates.GLD: Decimal(999), gates.USO: Decimal(999)},
    "max_gross": Decimal(2 * 10**9),
    "max_open_orders": 101,
    "loss_stop": Decimal(2 * 10**9),
    "moc_cutoff": 1,
    "share": Decimal("0.4"),
    "stress_move": Decimal("0.3"),
    "stress_low": Decimal("0.05"),
    "stress_high": Decimal("0.7"),
    "stress_vintage": "f" * 64,
    "stress_through": date(2026, 10, 29),
    "stress_max_age_days": 364,
    "share_step": Decimal("0.1"),
}


@pytest.mark.parametrize("name", HardCaps.__dataclass_fields__)
def test_7_a_change_to_any_one_of_the_caps_stops_the_order(name: str) -> None:
    """Looser or tighter, and whichever of the order's instruments it is of."""
    world = World()
    changed = {name: OTHER_CAPS[name]}
    assert getattr(world.caps, name) != changed[name]
    world.store.before = lambda n, op, k: (
        world.rewrite_caps(**changed) if n == CREATES[2] + 1 else None
    )
    halted(world, "the hard caps' limits changed since the send of")


@pytest.mark.parametrize("fault", ["missing", "malformed", "unreadable"])
def test_7_caps_that_cannot_be_read_again_stop_the_order(fault: str) -> None:
    world = World()

    def before(number: int, op: str, k: str) -> None:
        if number == CAPS_READS[1] and fault == "missing":
            world.store.drop(caps_key(ACCOUNT))
        if number == CAPS_READS[1] and fault == "malformed":
            world.store.put(caps_key(ACCOUNT), b"{}")
        if number >= CAPS_READS[1] and fault == "unreadable" and k == caps_key(ACCOUNT):
            down(number, op, k)

    world.store.before = before
    said = {
        "missing": "the hard caps could not be read again: BlobMissing",
        "malformed": "the hard caps read again are not whole: the caps blob's format",
        "unreadable": "the hard caps could not be read again: StoreUnavailable",
    }
    account = world.account()
    reason = halted(world, said[fault], lambda: world.send(account=account))
    if fault != "unreadable":
        assert world.state() is OrderState.ABANDONED
        return
    # The store did not answer the caps, so the outage pages once, and the journal, which
    # answers, takes the `abandoned` at its one try (#211).
    assert len(world.alarms.sent) == 1, world.alarms.sent
    assert world.state() is OrderState.ABANDONED
    assert account.unstored() == ()
    assert f"The journal shows {ID} Abandoned" in reason and "is kept" not in reason


def test_7_a_refused_abandoned_after_an_outage_is_named_in_the_halt(monkeypatch) -> None:
    """The one try's halt, such as a fold that refuses the entry, is said, and the entry kept."""
    from ithildinruntime.execution.order_path import Unstored

    world = World()
    account = world.account()

    def before(number: int, op: str, k: str) -> None:
        if number >= CAPS_READS[1] and k == caps_key(ACCOUNT):
            down(number, op, k)

    def refused(self, kept, report, once=False):
        raise Halt("the fold refuses it")

    world.store.before = before
    monkeypatch.setattr(Unstored, "_store", refused)
    with pytest.raises(Halt, match="could not be read again") as raised:
        world.send(account=account)
    reason = raised.value.reason
    # The journal answered, so the send stage reads it rather than calling the order unsent.
    assert not isinstance(raised.value, Outage) and world.submits == 0
    assert f"The journal did not take the abandoned of {ID}: the fold refuses it" in reason
    assert len(account.unstored()) == 1 and len(world.alarms.sent) == 1


def test_7_a_kept_abandoned_the_journal_turns_in_doubt_is_a_plain_halt(monkeypatch) -> None:
    """The journal answered with another state, so the send stage reads it, never "not sent"."""
    from ithildinruntime.execution.order_path import Unstored

    world = World()
    account = world.account()

    def before(number: int, op: str, k: str) -> None:
        if number >= CAPS_READS[1] and k == caps_key(ACCOUNT):
            down(number, op, k)

    world.store.before = before

    def turned(self, kept, report, once=False):
        return OrderState.IN_DOUBT

    monkeypatch.setattr(Unstored, "_store", turned)
    with pytest.raises(Halt) as raised:
        world.send(account=account)
    assert not isinstance(raised.value, Outage)
    assert f"The journal shows {ID} InDoubt, which this call did not do" in raised.value.reason


@pytest.mark.parametrize("at", range(CREATES[2] + 1, BEFORE_SUBMIT + 1))
def test_7_a_pass_counts_the_time_of_each_of_its_reads(at: int) -> None:
    """The book is disputed while one read is slow, so the pass is made again."""
    world = World(position=Decimal(5))
    moved = next_create(world, 3) - CREATES[2]
    bad = DisputedFillEntry(replace(fill("old-f1", "old-1", 5, 2, gates.GLD), at=AT), 2)
    slow: list[int] = []

    def after(number: int, op: str, k: str) -> None:
        if number == at + moved and not slow:
            slow.append(number)
            world.clock.elapsed += 6
            world.record(bad)

    world.store.after = after
    halted(world, "is in dispute")


def test_7_checks_that_never_come_in_one_pass_end_by_their_deadline() -> None:
    world = World()
    calls: list[int] = []

    def before(number: int, op: str, k: str) -> None:
        # The run's halt is refused once in every pass of the submit.
        if number > CREATES[2] and (op, k) == ("get", run_halt_key(DEPLOYMENT, RUN)):
            calls.append(number)
            if len(calls) % 2:
                down(number, op, k)

    world.store.before = before
    halted(world, "were not all read in one pass by the deadline")
    assert world.state() is OrderState.ABANDONED
    assert len(calls) == 20, "ten passes, each with one wait of a second"


# 7. Other ends of a race


def test_7_an_older_decisions_order_is_not_sent_after_a_newer_one_is_journaled() -> None:
    newer = AT + timedelta(minutes=5)
    world = World()
    world.decide((order(1, -2, at=newer),), newer)
    halted(world, "and the journal holds a later one")
    assert world.kinds() == []
    assert world.path().abandon(ID, "a later decision") is OrderState.ABANDONED
    raced = World()
    raced.store.before = lambda n, op, key: raced.decide((), newer) if n == CREATES[2] + 1 else None
    halted(raced, "and the journal holds a later one")
    assert raced.state() is OrderState.ABANDONED


def test_7_an_id_of_another_strategy_at_the_same_place_is_in_no_plan() -> None:
    world = World()
    other = order(1, 2, strategy=gates.OTHER).client_order_id
    halted(world, "is in no decision", lambda: world.path().send(other))


def test_7_abandon_leaves_an_order_this_writer_has_since_made_sending() -> None:
    world = World()
    account = world.account()
    world.path(account).hold([ID])
    views: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("list", f"journal/{ACCOUNT}/") and not world.store.quiet:
            views.append(number)
            if len(views) == 2:
                world.record(SendingEntry(ID), Journal(world.store, ACCOUNT, WRITER))

    world.store.before = before
    halted(world, "is Sending, so it is not", lambda: world.path(account).abandon(ID, "no"))
    assert world.state() is OrderState.SENDING


def test_7_a_kept_ack_stays_where_the_journal_shows_another_broker_id() -> None:
    world = World()
    account = world.account()
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt):
        world.send(account=account)
    world.store.before = lambda n, op, key: None
    world.record(AckEntry(ID, "b-other", b"", False, AT))
    with pytest.raises(Halt, match="still does not take the ack"):
        account.settle(world.report)
    assert account.unstored()


def test_7_an_in_doubt_whose_answer_was_lost_is_not_stored_twice() -> None:
    for answer, kept in ((raising(TimeoutError()), None), (says(Outcome.UNKNOWN), b"{}")):
        world = World()
        world.broker.answer = answer
        world.store.after = lambda n, op, key, w=world: (
            down(n, op, key) if n == CREATES[3] else None
        )
        assert world.send() is OrderState.IN_DOUBT
        assert world.kinds() == [*SENT[:3], "in_doubt"]
        assert last(world).raw == kept


def test_7_a_stand_in_whose_answer_was_lost_is_done_with_its_alarm() -> None:
    """The fold refuses the ack, its `in_doubt` lands, and the store does not answer for it."""
    world = World(orders=PAIR)
    account = world.account()
    world.broker.answer = says(Outcome.ACCEPTED, "b-1", b"the answer")
    assert world.send(1, account) is OrderState.ACKNOWLEDGED
    creates: list[int] = []

    def after(number: int, op: str, key: str) -> None:
        if op == "create":
            creates.append(number)
            if len(creates) == 4:
                down(number, op, key)

    world.store.after = after
    assert world.send(2, account) is OrderState.IN_DOUBT
    assert world.kinds()[-2:] == ["sending", "in_doubt"] and account.unstored() == ()
    assert f"the order {PAIR[1].client_order_id} is in doubt" in world.alarms.sent[-1]
    account.settle(world.report)


def test_7_an_answers_bytes_are_stored_beside_another_writers_doubt() -> None:
    world = World()
    other = Journal(world.store, ACCOUNT, OTHER_WRITER)

    def answer(intent: OrderIntent, number: int) -> Ack:
        world.record(InDoubtEntry(ID, "taken over", None, False, AT), other)
        return says(Outcome.UNKNOWN, "b-1", b"what it said")(intent, number)

    world.broker.answer = answer
    assert world.send() is OrderState.IN_DOUBT
    assert world.kinds()[-2:] == ["in_doubt", "in_doubt"]
    assert last(world).raw == b"what it said"


def test_7_an_order_not_sent_that_another_writer_put_in_doubt_says_so_and_holds_nobody() -> None:
    world = World(orders=PAIR)
    account = world.account()
    world.store.put(off_switch_key(ACCOUNT, gates.CHAN), b"")
    world.store.drop(off_switch_key(ACCOUNT, gates.CHAN))
    key = kill_switch_key(ACCOUNT)

    def before(number: int, op: str, k: str) -> None:
        if number == CREATES[2] + 1:
            world.store.put(key, b"")
        if number > CREATES[2] + 1 and op == "create":
            down(number, op, k)

    world.store.before = before
    halted(world, "kill switch stands", lambda: world.send(1, account))
    world.store.before = lambda n, op, k: None
    world.record(
        InDoubtEntry(ID, "taken over", None, False, AT), Journal(world.store, ACCOUNT, OTHER_WRITER)
    )
    account.settle(world.report)
    assert account.unstored() == ()
    assert last(world).reason.endswith("an order that was not sent")
    assert f"the order {ID} is in doubt, not sent" in world.alarms.sent[-1]


@pytest.mark.parametrize("then", ["numbers", "clock", "channel", "store"])
def test_7_a_sending_whose_answer_was_lost_is_never_left_sending(then: str) -> None:
    """It may be stored, and an order nobody sent could never be settled by a lookup."""
    world = World(retry=Retry(timedelta(seconds=0.5), 1.0, 4.0))
    account, plain, got = world.account(), world.clock.now, world.store.get
    key = journal_entry_key(ACCOUNT, 6)
    state = {"lost": False, "gave up": False, "taken": 0}

    def broken() -> datetime:
        raise RuntimeError("no time")

    def after(n: int, op: str, k: str) -> None:
        if (op, k) == ("create", key) and not state["lost"]:
            state["lost"] = True
            down(n, op, k)

    def get(k: str) -> bytes:
        # The one try left finds the key taken, and cannot read what holds it.
        if k == key and state["lost"] and not state["gave up"]:
            state["gave up"] = True
            if then == "clock":
                world.clock.now = broken  # type: ignore[method-assign]
            down(0, "get", k)
        return got(k)

    def before(n: int, op: str, k: str) -> None:
        if not state["gave up"]:
            return
        if then == "numbers" and op == "create" and state["taken"] < 7:
            state["taken"] += 1
            world.record(entry(n))
        if then in ("store", "channel") and op == "create":
            down(n, op, k)

    if then == "channel":
        world.alarms.error = ConnectionError("down")
    world.store.after, world.store.before, world.store.get = after, before, get  # type: ignore[method-assign]
    with pytest.raises(Halt, match="the sending of .* was not stored"):
        world.send(account=account)
    world.clock.now, world.alarms.error = plain, None  # type: ignore[method-assign]
    world.store.before = world.store.after = lambda n, op, k: None
    account.settle(world.report)
    assert world.state() is OrderState.ABANDONED
    assert account.unstored() == () and world.submits == 0
    assert [e.built_at for e in world.reader().read()][-1] == NOW
    if then in ("store", "channel"):
        # The outage pages once, naming the `sending`, and nothing pages the kept `abandoned`.
        (page,) = world.alarms.sent
        assert "the sending of" in page and "was not stored" in page


def test_7_an_outage_after_the_brokers_call_leaves_the_order_in_doubt() -> None:
    """Only an outage before the call means nothing left, so one after it is never called unsent."""
    world = World()
    account = world.account()
    path = DownAfterCallPath(account._shared, world.broker, world.report)
    # It is returned in doubt, for the send stage to end its plan on.
    assert path.send(world.orders[0].client_order_id) is OrderState.IN_DOUBT
    assert world.submits == 1
    assert world.state() is OrderState.IN_DOUBT
    assert "abandoned" not in world.kinds()


@pytest.mark.parametrize("heals", [False, True])
@pytest.mark.parametrize("error", [RuntimeError("no time"), SystemExit("no time")])
def test_7_a_clock_that_fails_after_the_sending(error: BaseException, heals: bool) -> None:
    """An ordinary error still journals what was not sent, and any other is left to replay."""
    world = World()
    account = world.account()
    plain = world.clock.now

    def broken() -> datetime:
        if heals:
            world.clock.now = plain  # type: ignore[method-assign]
        raise error

    later = NOW + timedelta(seconds=3)

    def after(number: int, op: str, key: str) -> None:
        if number == BEFORE_SUBMIT:
            world.clock.at = later
            world.clock.now = broken  # type: ignore[method-assign]

    world.store.after = after
    with pytest.raises(type(error), match="no time"):
        world.send(account=account)
    world.clock.now = plain  # type: ignore[method-assign]
    left = OrderState.ABANDONED if isinstance(error, Exception) else OrderState.SENDING
    assert world.state() is left and world.submits == 0
    assert account.unstored() == ()
    # An `abandoned` written with no clock takes the time its `sending` was built.
    took = later if heals and left is OrderState.ABANDONED else NOW
    assert [e.built_at for e in world.reader().read()][-1] == took


@pytest.mark.parametrize("create", range(4))
@pytest.mark.parametrize("calls", [1, 2, 5, 9, 14, 40])
def test_7_a_create_that_lands_late_doubles_nothing_and_unsays_nothing(
    create: int, calls: int
) -> None:
    """The store does not answer for a create, and the create lands `calls` calls later."""
    world = World()
    account = world.account()
    held: list[tuple[str, bytes, int]] = []
    plain = world.store.create

    def creating(key: str, data: bytes, sha256: str) -> str:
        seen = len([c for c in world.store.calls if c[0] == "create"])
        if seen == create and not held and not world.store.quiet:
            world.store.calls.append(("create", key))
            held.append((key, data, len(world.store.calls)))
            down(0, "create", key)
        if held and key == held[0][0] and key not in world.store.blobs:
            # The same key is refused until the first try of it has landed.
            world.store.calls.append(("create", key))
            down(0, "create", key)
        return plain(key, data, sha256)

    def before(number: int, op: str, key: str) -> None:
        if held and number >= held[0][2] + calls and held[0][0] not in world.store.blobs:
            world.store.put(held[0][0], held[0][1])

    world.store.create = creating  # type: ignore[method-assign]
    world.store.before = before
    for _ in range(3):
        try:
            world.send(account=account)
        except Halt:
            pass
    if held and held[0][0] not in world.store.blobs:
        world.store.put(held[0][0], held[0][1])
    try:
        world.send(account=account)
    except Halt:
        pass
    assert world.submits <= 1
    if world.submits:
        assert world.state() is OrderState.ACKNOWLEDGED
    else:
        assert world.state() in (OrderState.ABANDONED, OrderState.INTENDED, OrderState.APPROVED)
    assert world.reader().in_doubt() == () and account.unstored() == ()


class Unheard(StoreError):
    """A store's error that holds no answer, as an alert channel of a store's own could raise."""

    def __init__(self) -> None:
        Exception.__init__(self)


class Hidden(Exception):
    @property
    def __class__(self) -> type:  # type: ignore[override]
        raise RuntimeError("PKFAKEKEY")


@pytest.mark.parametrize(
    "made", [lambda: NAMELESS("PKFAKEKEY"), lambda: Unheard(), lambda: Hidden()]
)
def test_6_an_alarm_error_that_cannot_be_read_stops_no_answers_entry(
    made: Callable[[], Exception],
) -> None:
    world = World()
    world.broker.answer = raising(TimeoutError())
    world.alarms.error = made()
    with pytest.raises(Halt, match="An alarm about it could not be sent"):
        world.send()
    assert world.state() is OrderState.IN_DOUBT


def test_6_a_stand_in_for_an_abandoned_says_its_alarm_was_not_sent() -> None:
    """Another writer says the order is in doubt before this call can say it was not sent."""
    world = World()

    def before(number: int, op: str, key: str) -> None:
        if number == CREATES[2] + 1:
            world.store.before = lambda n, o, k: None
            world.record(InDoubtEntry(ID, "a takeover", None, False, AT))
            world.alarms.error = ConnectionError("down")

    world.store.before = before
    with pytest.raises(Halt, match="is not sending.*is InDoubt. An alarm about it could not"):
        world.send()
    assert world.submits == 0


def test_6_a_halt_says_an_entry_and_its_alarm_were_both_lost() -> None:
    world = World()
    world.broker.answer = raising(TimeoutError())
    world.alarms.error = ConnectionError("down")
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt, match="the next send. An alarm about it could not be sent$"):
        world.send()


def test_6_an_entry_stored_at_a_later_try_says_its_alarm_was_lost() -> None:
    world = World()
    world.broker.answer = raising(TimeoutError())
    world.alarms.error = ConnectionError("down")
    refused: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", journal_entry_key(ACCOUNT, 7)) and not refused:
            refused.append(number)
            down(number, op, key)

    world.store.before = before
    with pytest.raises(Halt, match="is InDoubt. An alarm about it could not be sent$"):
        world.send()


def test_6_an_adapters_halt_says_its_alarm_was_lost() -> None:
    world = World()
    world.broker.answer = raising(Halt("the clock went back"))
    world.alarms.error = ConnectionError("down")
    with pytest.raises(Halt, match="the clock went back. An alarm about it could not be sent$"):
        world.send()


def test_6_an_accepted_orders_lost_entry_is_no_doubt() -> None:
    world = World()
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt, match="^the journal did not take the ack of"):
        world.send()


def test_6_an_order_stopped_at_the_call_says_why_where_its_abandoned_is_not_stored() -> None:
    world = World()

    def before(number: int, op: str, key: str) -> None:
        if number == CREATES[2] + 1:
            world.store.put(kill_switch_key(ACCOUNT), b"{}")
        if (op, key) == ("create", journal_entry_key(ACCOUNT, 7)):
            down(number, op, key)

    world.store.before = before
    halted(world, "^kill switch stands. the journal did not take the abandoned of")


def test_2_a_kept_doubt_of_an_order_with_no_id_names_none() -> None:
    world = World()
    account = world.account()
    world.broker.answer = raising(ValueError("x"))
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt):
        world.send(account=account)
    world.store.before = lambda n, op, key: None
    world.record(RejectedEntry(ID, "no", b"", False, AT))
    account.settle(world.report)
    assert world.alarms.sent[-1].endswith("the journal shows it Rejected")


def test_2_settling_one_kept_entry_leaves_another() -> None:
    world = World(orders=PAIR)
    account = world.account()
    second = PAIR[1].client_order_id
    for record in route("Sending", PAIR[1]):
        world.record(_numbered(world, record))
    world.store.before = lambda n, op, key: down(n, op, key) if op == "create" else None
    for name, broker in ((ID, "b-1"), (second, "b-2")):
        if name == ID:
            world.store.quiet = True
            for record in route("Sending", PAIR[0]):
                world.record(_numbered(world, record))
            world.store.quiet = False
        with pytest.raises(Halt):
            account._shared.unstored.put(
                name, AckEntry(name, broker, b"", False, AT), AT, "sent", world.report
            )
    assert len(account.unstored()) == 2
    creates: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if op == "create":
            creates.append(number)
            if len(creates) > 1:
                down(number, op, key)

    world.store.before = before
    with pytest.raises(Halt, match="still does not take the ack"):
        account.settle(world.report)
    (left,) = account.unstored()
    assert second in left
    world.store.before = lambda n, op, key: None
    account.settle(world.report)
    assert account.unstored() == ()


def test_6_an_error_whose_name_cannot_be_read_after_the_call_is_in_doubt(monkeypatch) -> None:
    from ithildinruntime.execution import order_path

    world = World()
    world.broker.answer = raising(TimeoutError())
    plain, read = order_path.error_name, []

    def once(error: object) -> str:
        if not read:
            read.append(error)
            raise MemoryError
        return plain(error)

    monkeypatch.setattr(order_path, "error_name", once)
    assert world.send() is OrderState.IN_DOUBT and world.submits == 1


ENDS = [SystemExit("PKFAKEKEY"), CancelledError("PKFAKEKEY"), KeyboardInterrupt("PKFAKEKEY")]


@pytest.mark.parametrize("error", ENDS)
def test_7_an_alarm_that_raises_anything_loses_no_order_that_was_not_sent(
    error: BaseException,
) -> None:
    world = World()
    account = world.account()
    world.alarms.error = error

    def before(number: int, op: str, key: str) -> None:
        if number > CREATES[2]:
            down(number, op, key)

    world.store.before = before
    said = halted(world, "The alarm could not be sent", lambda: world.send(account=account))
    assert "PKFAKEKEY" not in said and account.unstored()
    world.store.before = lambda n, op, key: None
    account.settle(world.report)
    assert world.state() is OrderState.ABANDONED


@pytest.mark.parametrize("error", ENDS)
def test_6_an_alarm_raised_as_an_answer_is_settled_stops_nothing(error: BaseException) -> None:
    world = World(orders=PAIR)
    account = world.account()
    world.broker.answer = raising(TimeoutError())
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    with pytest.raises(Halt):
        world.send(1, account)
    world.store.before = lambda n, op, key: None
    world.record(AckEntry(ID, "b-0", b"", False, AT))
    world.broker.answer, world.alarms.error = says(Outcome.ACCEPTED, "b-1"), error
    assert world.send(2, account) is OrderState.ACKNOWLEDGED
    world.alarms.error, world.alarms.sent = None, []
    account.settle(world.report)
    (alarm,) = world.alarms.sent
    assert "was kept as in doubt" in alarm


class Falsy(str):
    """An id that reads as none, and says what it likes where it is put into words."""

    def __bool__(self) -> bool:
        return False

    def __format__(self, spec: str) -> str:
        return "PKFAKEKEY"


@pytest.mark.parametrize(
    ("field", "value", "said"),
    [
        ("raw", Bytes(b"{}"), "its bytes are a Bytes"),
        ("raw", bytearray(b"{}"), "its bytes are a bytearray"),
        ("raw", None, "its bytes are a NoneType"),
        ("received_at", Time(2026, 11, 3, 20, 48, tzinfo=UTC), "its time is not a UTC datetime"),
        ("received_at", None, "its time is not a UTC datetime"),
        ("received_at", AT.replace(tzinfo=None), "its time is not a UTC datetime"),
        ("broker_order_id", Ours("b-1"), "its broker's id is a Ours"),
        ("broker_order_id", Ours(""), "its broker's id is a Ours"),
        ("broker_order_id", Falsy("b-1"), "its broker's id is a Falsy"),
        ("broker_order_id", 0, "its broker's id is a int"),
    ],
)
@pytest.mark.parametrize("outcome", [Outcome.ACCEPTED, Outcome.REJECTED, Outcome.NOT_SENT])
def test_6_an_answer_whose_field_is_of_another_type_is_in_doubt_and_says_which(
    outcome: Outcome, field: str, value: object, said: str
) -> None:
    world = World()
    world.broker.answer = lambda intent, n: bent(outcome=outcome, **{field: value})
    assert world.send() is OrderState.IN_DOUBT
    assert last(world).reason == said
    (alarm,) = world.alarms.sent
    # The alarm is where the id stands, since an entry of a doubt holds none.
    assert ("as the broker's b-1" in alarm) == (field != "broker_order_id")
    assert "PKFAKEKEY" not in alarm


@pytest.mark.parametrize("broker", ["b-1", "b-9", None])
@pytest.mark.parametrize("outage", [False, True])
def test_2_a_rejection_of_an_order_another_writer_saw_taken_stays_kept(
    outage: bool, broker: str | None
) -> None:
    """The fold takes one, and the order would leave the book while the broker may hold it."""
    world = World(orders=(order(1, 2), order(2, 2)))
    account = world.account()

    def answer(intent: OrderIntent, number: int) -> Ack:
        world.record(AckEntry(ID, "b-9", b"", False, AT))
        if outage:
            world.store.refuse(("create", journal_entry_key(ACCOUNT, 8)))
        return Ack(Outcome.REJECTED, broker, None, b"{}", AT)

    world.broker.answer = answer
    with pytest.raises(Halt, match="shows it Acknowledged, as the broker's b-9"):
        world.send(1, account)
    world.store.before = lambda n, op, key: None
    for _ in range(2):
        with pytest.raises(Halt, match="still does not take the rejected"):
            world.send(2, account)
    assert world.state() is OrderState.ACKNOWLEDGED and world.submits == 1
    assert len(world.alarms.sent) == 1 and account.unstored()


def test_2_a_stand_in_that_landed_is_done_whatever_another_writer_then_says() -> None:
    """The broker rejects an order in doubt, its stand-in lands, and then the store goes down."""
    world = World(orders=(order(1, 2), order(2, 2)))
    account = world.account()

    def answer(intent: OrderIntent, number: int) -> Ack:
        world.record(InDoubtEntry(ID, "a takeover", None, False, AT))
        return Ack(Outcome.REJECTED, None, None, b"the answer", AT)

    def after(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", journal_entry_key(ACCOUNT, 8)):
            world.store.after = lambda n, o, k: None
            world.store.before = down
            down(number, op, key)

    world.broker.answer, world.store.after = answer, after
    with pytest.raises(Halt, match="did not take the rejected"):
        world.send(1, account)
    assert account.unstored()
    world.store.before = lambda n, op, key: None
    world.record(AckEntry(ID, "b-9", b"", False, AT))
    account.settle(world.report)
    assert account.unstored() == () and world.state() is OrderState.ACKNOWLEDGED
    alarm = world.alarms.sent[-1]
    assert f"the order {ID} is in doubt" in alarm
    assert alarm.endswith("and the journal shows it Acknowledged, as the broker's b-9")
    assert world.kinds()[-2:] == ["in_doubt", "ack"]


@pytest.mark.parametrize("error", ENDS)
@pytest.mark.parametrize("answered", [False, True])
def test_2_a_store_call_that_ends_in_more_than_an_exception_is_left_to_replay(
    answered: bool, error: BaseException
) -> None:
    world = World()
    account = world.account()

    def before(number: int, op: str, key: str) -> None:
        if number == (CALLS if answered else BEFORE_SUBMIT):
            world.store.before = lambda n, o, k: None
            raise error

    world.store.before = before
    ends = AssertionError if isinstance(error, KeyboardInterrupt) else type(error)
    with pytest.raises(ends):
        world.send(account=account)
    assert world.state() is OrderState.SENDING and world.submits == answered
    assert bool(account.unstored()) == answered and world.alarms.sent == []
    account.settle(world.report)
    assert world.state() is (OrderState.ACKNOWLEDGED if answered else OrderState.SENDING)


@pytest.mark.parametrize(
    ("outcome", "broker", "said"),
    [
        (Outcome.UNKNOWN, None, "perhaps sent at {at}"),
        (Outcome.UNKNOWN, "b-1", "sent at {at}, as the broker's b-1"),
        (Outcome.REJECTED, None, "sent at {at}, and rejected"),
    ],
)
def test_6_an_alarm_says_when_the_order_went_out_and_not_when_its_answer_came(
    outcome: Outcome, broker: str | None, said: str
) -> None:
    world = World()

    def answer(intent: OrderIntent, number: int) -> Ack:
        world.clock.at += timedelta(minutes=1)
        return Ack(outcome, broker, None, b"{}", AT)

    world.broker.answer = answer
    world.store.refuse(("create", journal_entry_key(ACCOUNT, 7)))
    went = world.clock.now().isoformat()
    with pytest.raises(Halt):
        world.send()
    assert all(said.format(at=went) in alarm for alarm in world.alarms.sent)
