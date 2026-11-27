"""The account's lease on the order path: one holder sends at a time, and a late order halts (#148).

docs/architecture/06-runtime.md's `#### The account's lease, around each submit`
gives the steps. design.md's `### Every fact has one writer` names four guards
on the window an unfenced send leaves, and each has its own test here with the
other three intact.

1. One sender: only the lease's holder calls the broker, and a new holder
   re-reads the journal before it acts.
2. A `sending` entry is stored before every send.
3. Doubt settles by lookup, and a new holder never resends an older one's order.
4. A late order under an older lease halts before it is `Acknowledged`.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    AccountRead,
    Ack,
    BlobMissing,
    BlobRef,
    BrokerOrder,
    BrokerState,
    CashRead,
    FillsRead,
    FoundOrder,
    Halt,
    HaltRecord,
    Lease,
    OrderIntent,
    OrderLookup,
    OrdersRead,
    OrderState,
    Outcome,
    PositionsRead,
    ReceivedPage,
    RecordsRead,
    StoreUnavailable,
    account_halt_key,
    account_halts_prefix,
    account_lease_key,
    decode,
    encode,
    kill_switch_key,
    reconcile,
    sha256_of,
    takeover_key,
)
from ithildinruntime.execution.lease import MARGIN, PERIOD, TakeoverRead, free_lease
from ithildinruntime.journal import (
    AckEntry,
    InDoubtEntry,
    IntentEntry,
    Journal,
    LostEntry,
    ReadEntry,
    SendingEntry,
    TakeoverEntry,
    lost_from,
)
from tests.support import gates
from tests.support.fake_broker import accepted
from tests.support.order_world import (
    ACCOUNT,
    AT,
    DEPLOYMENT,
    NOW,
    OTHER_WRITER,
    RUN,
    World,
    halted,
    order,
    planted_verdict,
)

KEY = account_lease_key(ACCOUNT)
# A handler of another instance, whose lease ran out a minute before the send begins.
FROZEN = "f" * 32 + "-0123456789abcdef"
PAGES = (ReceivedPage(b"[]", NOW),)


def _lease(world: World) -> Lease:
    return decode(world.store.blobs[KEY], Lease)


def _plant(world: World, lease: Lease) -> None:
    world.store.put(KEY, encode(lease))


def _held(handler: str = FROZEN, expires: timedelta = -timedelta(minutes=1)) -> Lease:
    return Lease(
        format=1,
        vm=None,
        boot=None,
        container=None,
        restart_count=None,
        handler=handler,
        release="r1",
        run=RUN,
        taken=NOW - PERIOD,
        expires=NOW + expires,
        status=None,
        count=11,
        owed=False,
    )


def _two() -> World:
    return World(orders=(order(1, 2), order(2, 1, gates.USO)))


def _sent_by_another(world: World, place: int = 1) -> str:
    """Journal an order as another instance does up to its `sending`, which then goes quiet."""
    made = world.orders[place - 1]
    name = made.client_order_id
    other = Journal(world.store, ACCOUNT, OTHER_WRITER)
    world.record(IntentEntry(world.reader().view().decided[AT], made), other)
    world.record(planted_verdict(name, world.reader().view().through.seq), other)
    world.record(SendingEntry(name), other)
    return name


def _row(intent: OrderIntent, broker: str = "b-late") -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=broker,
        client_order_id=intent.client_order_id,
        instrument=intent.instrument,
        qty=intent.qty,
        filled=Decimal(0),
        type=intent.type,
        time_in_force=intent.time_in_force,
        state=BrokerState.WORKING,
        submitted_at=NOW,
    )


def _taken_over(world: World, *rows: BrokerOrder) -> tuple[str, TakeoverEntry]:
    """Order 1 sent by a frozen holder whose lease expired, then order 2 sent by this one."""
    late = _sent_by_another(world)
    _plant(world, _held())
    world.broker.reads["orders"] = lambda: OrdersRead(rows, PAGES, True)
    assert world.send(2) is OrderState.ACKNOWLEDGED
    (entry,) = [r for r in world.entries() if isinstance(r, TakeoverEntry)]
    return late, entry


def _halts(world: World) -> list[str]:
    return [k for k in world.store.blobs if k.startswith(account_halts_prefix(ACCOUNT))]


def _journal_creates(
    world: World,
    after: Callable[[str, str], bool],
    nth: int,
    then: Callable[[str], None],
    *,
    landed: bool = False,
) -> None:
    """Call `then` at the `nth` journal create after the store call `after` picks.

    It runs before that create, or once it has `landed`. A refusal there refuses
    every journal create from then on, since a store that is down is tried again.
    """
    seen: list[str] = []

    def hook(number: int, op: str, key: str) -> None:
        if not seen and after(op, key):
            seen.append("")
        elif seen and op == "create" and key.startswith(f"journal/{ACCOUNT}/0"):
            if len(seen) <= nth:
                seen.append(key)
            if len(seen) == nth + 1:
                then(key)

    if landed:
        world.store.after = hook
    else:
        world.store.before = hook


def _refuse(key: str) -> None:
    raise StoreUnavailable(key, "AccessDenied: the store's own text")


# One send


def test_a_send_takes_the_lease_renews_it_right_before_the_call_and_releases_it() -> None:
    world = World()
    seen: list[Lease] = []

    def submit(intent: OrderIntent, number: int) -> Ack:
        seen.append(_lease(world))
        return accepted(intent, number, NOW)

    world.broker.answer = submit
    assert world.send() is OrderState.ACKNOWLEDGED
    (at_call,) = seen
    # The take created it, and the renewal rewrote it right before the call.
    assert (at_call.count, at_call.expires, at_call.owed) == (2, NOW + PERIOD, False)
    assert at_call.handler is not None and at_call.handler.startswith("1" * 32 + "-")
    assert _lease(world) == free_lease(3, False)
    ops = [op for op, key in world.store.calls if key == KEY]
    assert ops == ["head", "create", "replace", "replace"]
    first = world.store.calls.index(("head", KEY))
    journal = [n for n, (_, key) in enumerate(world.store.calls) if key.startswith("journal/")]
    assert journal[0] < first, "the journal is read before the lease"


def test_each_order_of_a_plan_takes_the_lease_once_under_a_handler_of_its_own() -> None:
    world = _two()
    handlers = []

    def submit(intent: OrderIntent, number: int) -> Ack:
        handlers.append(_lease(world).handler)
        return accepted(intent, number, NOW)

    world.broker.answer = submit
    path = world.path()
    assert path.send(world.orders[0].client_order_id) is OrderState.ACKNOWLEDGED
    assert path.send(world.orders[1].client_order_id) is OrderState.ACKNOWLEDGED
    assert _lease(world) == free_lease(6, False)
    assert len(set(handlers)) == 2


def test_a_lease_another_handler_holds_ends_the_order_with_nothing_journaled() -> None:
    world = World()
    _plant(world, _held(expires=timedelta(seconds=10)))
    before = world.every_kind()
    reason = halted(world, "held by")
    assert FROZEN in reason
    assert world.every_kind() == before
    assert _lease(world) == _held(expires=timedelta(seconds=10))


def test_a_lease_still_inside_its_margin_is_not_taken_over() -> None:
    world = World()
    _plant(world, _held(expires=-MARGIN))
    halted(world, "held by")
    _plant(world, _held(expires=-MARGIN - timedelta(microseconds=1)))
    assert world.send() is OrderState.ACKNOWLEDGED


def test_a_lease_taken_over_before_the_broker_call_ends_the_order_unsent() -> None:
    world = World()
    other = "e" * 32 + "-1"

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            world.store.before = lambda *_: None
            # Another handler takes it over between this one's take and its renewal.
            _plant(world, _held(other, expires=timedelta(seconds=30)))

    world.store.before = before
    halted(world, "rewritten by another handler, so its renewal was refused")
    assert world.state() is OrderState.ABANDONED
    assert _lease(world).handler == other


def test_the_lease_is_released_when_the_broker_raises() -> None:
    world = World()

    def down(intent: OrderIntent, number: int) -> Ack:
        raise ConnectionError

    world.broker.answer = down
    assert world.send() is OrderState.IN_DOUBT
    assert _lease(world) == free_lease(3, False)


def test_the_lease_is_released_when_a_control_stands() -> None:
    world = World()
    world.store.put(kill_switch_key(ACCOUNT), b"{}")
    halted(world, "kill switch stands")
    assert _lease(world) == free_lease(2, False)


def test_a_release_the_store_does_not_answer_leaves_the_lease_to_expire() -> None:
    world = World()
    replaced: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            replaced.append(number)
            if len(replaced) >= 2:
                raise StoreUnavailable(key, "SlowDown: the store's own text")

    world.store.before = before
    assert world.send() is OrderState.ACKNOWLEDGED
    assert len(replaced) > 2, "a release the store does not answer is asked again"
    lease = _lease(world)
    assert lease.handler is not None and lease.expires == NOW + PERIOD


def test_a_release_whose_first_try_landed_unanswered_leaves_the_lease_free() -> None:
    world = World()
    replaced: list[int] = []

    def after(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            replaced.append(number)
            if len(replaced) == 2:
                raise StoreUnavailable(key, "no answer")

    world.store.after = after
    assert world.send() is OrderState.ACKNOWLEDGED
    assert _lease(world) == free_lease(3, False)


# An absent lease


def _lost() -> World:
    """A journal with an earlier session's `sending`, and no lease where one stood."""
    world = World(position=Decimal(3))
    world.store.drop(KEY)
    return world


def test_an_absent_lease_with_a_sending_in_the_journal_halts_new_orders_and_pages() -> None:
    world = _lost()
    before = world.every_kind()
    reason = halted(world, "lease is absent")
    assert world.every_kind() == before
    with pytest.raises(BlobMissing):
        world.store.get(KEY)
    view = world.reader().view()
    assert view.sent_at is not None
    key = account_halt_key(ACCOUNT, "execution", "lease_absent", view.sent_at, view.through.seq)
    assert _halts(world) == [key]
    assert decode(world.store.blobs[key], HaltRecord).reason == reason
    assert any(reason in said for said in world.alarms.sent)


def test_two_handlers_that_find_the_lease_absent_raise_one_halt() -> None:
    world = _lost()
    halted(world, "lease is absent")
    halted(world, "lease is absent", lambda: world.send(account=world.account(OTHER_WRITER)))
    assert len(_halts(world)) == 1


def test_a_lease_lost_again_later_raises_a_halt_of_its_own() -> None:
    world = _lost()
    halted(world, "lease is absent")
    # Another sending lands after the first halt, so the journal's head has moved.
    _sent_by_another(world)
    halted(world, "lease is absent")
    assert len(_halts(world)) == 2


def test_a_journal_with_no_sending_is_a_first_use_and_creates_the_lease() -> None:
    world = World()
    assert world.send() is OrderState.ACKNOWLEDGED
    assert not _halts(world)


def test_a_first_use_that_races_another_first_use_does_not_read_as_a_lost_lease() -> None:
    """The journal is read first, so a lease created after that read is a first use's."""
    world = _two()
    other = world.account(OTHER_WRITER)
    heads: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("head", KEY) and not heads:
            heads.append(number)
            # The other instance sends a whole order after this one read the journal.
            world.store.before = lambda *_: None
            assert world.send(2, other) is OrderState.ACKNOWLEDGED

    world.store.before = before
    assert world.send(1) is OrderState.ACKNOWLEDGED
    assert heads and not _halts(world)


# A takeover


def test_a_take_of_an_expired_lease_stores_the_list_and_journals_the_takeover_first() -> None:
    world = _two()
    late, entry = _taken_over(world)
    kinds = world.every_kind()
    assert kinds.index("takeover") < len(kinds) - kinds[::-1].index("intent") - 1
    assert (entry.holder, entry.expired) == (FROZEN, NOW - timedelta(minutes=1))
    assert (entry.late, entry.count) == ((late,), 12)
    assert entry.orders.key == takeover_key(DEPLOYMENT, RUN, NOW)
    stored = decode(world.store.blobs[entry.orders.key], TakeoverRead)
    assert (stored.account, stored.count, stored.orders.orders) == (ACCOUNT, 12, ())
    assert sha256_of(world.store.blobs[entry.orders.key]) == entry.orders.sha256
    sent = world.reader().view().orders[late].sent_at
    assert sent is not None and entry.since == stored.since == sent - timedelta(seconds=60)
    # The take, the rewrite that clears `owed`, the renewal and the release.
    assert _lease(world) == free_lease(15, False)
    assert any(f"taken over from {FROZEN}" in said for said in world.alarms.sent)


def test_an_order_the_broker_lists_is_not_named_late() -> None:
    world = _two()
    _, entry = _taken_over(world, _row(world.orders[0]))
    assert entry.late == ()


def test_a_free_lease_with_no_takeover_owed_journals_none() -> None:
    world = _two()
    _sent_by_another(world)
    _plant(world, free_lease(4, False))
    assert world.send(2) is OrderState.ACKNOWLEDGED
    assert "takeover" not in world.every_kind()


@pytest.mark.parametrize("fails", ["the read", "a list not whole", "the store", "the journal"])
def test_a_failed_takeover_halts_new_orders_and_the_next_take_owes_it(fails: str) -> None:
    world = _two()
    late = _sent_by_another(world)
    _plant(world, _held())
    if fails == "the read":
        world.broker.reads["orders"] = lambda: (_ for _ in ()).throw(ConnectionError())
    elif fails == "a list not whole":
        world.broker.reads["orders"] = lambda: OrdersRead((), PAGES, False)
    elif fails == "the store":
        world.store.refuse(("create", f"runs/{DEPLOYMENT}/{RUN}/execution/takeovers/"))
    else:
        _journal_creates(world, lambda op, key: op == "create" and "/takeovers/" in key, 1, _refuse)
    before = world.every_kind()
    reason = halted(world, "the takeover of the account's lease failed", lambda: world.send(2))
    assert "No new order goes out until a takeover stores the list" in reason
    assert world.every_kind() == before
    lease = _lease(world)
    assert lease.handler is None and lease.owed
    # The next take does the takeover still owed, from a free lease.
    world.store.before = lambda *_: None
    world.broker.reads.pop("orders", None)
    assert world.send(2) is OrderState.ACKNOWLEDGED
    (entry,) = [r for r in world.entries() if isinstance(r, TakeoverEntry)]
    assert (entry.holder, entry.expired, entry.late) == (None, None, (late,))
    if fails == "the journal":
        # Its list was stored under the same time, so the second is stored beside it.
        assert entry.orders.key == takeover_key(DEPLOYMENT, RUN, NOW, 2)
    # A late answer of the older holder still never reaches `Acknowledged`.
    with pytest.raises(Halt, match="a late order under an older lease"):
        world.record(AckEntry(late, "b-late", b"{}", False, NOW))
    assert world.state(1) is OrderState.SENDING


def test_a_rerun_after_a_kill_before_the_takes_rewrite_takes_the_lease_as_the_first_would() -> None:
    world = _two()
    _sent_by_another(world)
    _plant(world, _held())

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            raise StoreUnavailable(key, "no answer, and nothing landed")

    world.store.before = before
    halted(world, "could not be read or taken", lambda: world.send(2))
    assert _lease(world) == _held()
    world.store.before = lambda *_: None
    assert world.send(2) is OrderState.ACKNOWLEDGED
    assert world.every_kind().count("takeover") == 1


def test_a_take_whose_create_lands_after_its_tries_leaves_the_lease_to_expire() -> None:
    world = World()
    held: list[tuple[bytes, str]] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", KEY):
            raise StoreUnavailable(key, "no answer")

    real = world.store.create

    def creating(key: str, data: bytes, sha256: str) -> str:
        if key == KEY and not held:
            held.append((data, sha256))
        return real(key, data, sha256)

    world.store.create = creating  # type: ignore[method-assign]
    world.store.before = before
    halted(world, "could not be read or taken")
    # The first try lands after the take gave up, naming a handler nobody runs.
    world.store.before = lambda *_: None
    world.store.put(KEY, held[0][0])
    halted(world, "held by")
    world.clock.at += PERIOD + MARGIN + timedelta(seconds=1)
    assert world.send() is OrderState.ACKNOWLEDGED
    (entry,) = [r for r in world.entries() if isinstance(r, TakeoverEntry)]
    assert entry.late == ()


# Guard 4: a late order under an older lease halts before `Acknowledged`


def test_4_the_older_holders_own_answer_is_never_journaled_acknowledged() -> None:
    world = _two()
    late, _ = _taken_over(world)
    with pytest.raises(Halt, match=f"acknowledges {late}, a late order under an older lease"):
        world.record(AckEntry(late, "b-late", b"{}", False, NOW))
    assert world.state(1) is OrderState.SENDING


def test_4_a_lookup_that_finds_a_late_order_halts_before_acknowledged() -> None:
    world = _two()
    late, _ = _taken_over(world)
    world.record(InDoubtEntry(late, "its send stage ended", None, False, NOW))
    row = _row(world.orders[0])
    world.broker.reads["find_order"] = lambda: FoundOrder(Outcome.ACCEPTED, (row,), True, b"[]")
    world.broker.reads["lookup"] = lambda: Ack(Outcome.ACCEPTED, "b-late", row, b"{}", NOW)
    kept: list[OrderLookup] = []

    def keep(found: OrderLookup) -> BlobRef:
        kept.append(found)
        data = encode(found)
        key = f"runs/{DEPLOYMENT}/{RUN}/lookup-{len(kept)}.json"
        world.store.put(key, data)
        return BlobRef(key, sha256_of(data))

    with pytest.raises(Halt, match="a late order under an older lease"):
        world.path().settle(late, keep, "execution/recover", NOW)
    assert world.state(1) is OrderState.IN_DOUBT
    assert len(kept) == 2


def test_4_a_reconcile_names_a_late_order_the_broker_lists_open() -> None:
    world = _two()
    late, _ = _taken_over(world)
    view = world.reader().view()
    read = AccountRead(
        since=NOW,
        positions=PositionsRead({}, PAGES, True),
        open_orders=OrdersRead((_row(world.orders[0]),), PAGES, True),
        cash=CashRead(view.book.cash, PAGES, True),
        fills=FillsRead((), PAGES, True),
        records=RecordsRead((), PAGES, True),
    )
    said = [str(d) for d in reconcile(view.book, view.live.values(), read, view.late)]
    assert any("a late order under an older lease" in s for s in said), said
    plain = [str(d) for d in reconcile(view.book, view.live.values(), read)]
    assert not any("late order" in s for s in plain)
    assert late in view.late


def test_a_late_order_leaves_the_set_once_it_ends() -> None:
    world = _two()
    late, _ = _taken_over(world)
    world.record(InDoubtEntry(late, "its send stage ended", None, False, NOW))
    assert late in world.reader().view().late
    asked = lost_from(late) + timedelta(seconds=1)
    read = world.record(ReadEntry("execution/recover", AT, BlobRef("l", "a" * 64), asked))
    world.record(LostEntry(late, read, "the broker's whole order list omits it"))
    assert late not in world.reader().view().late


# Guards 1 to 3, each with the other three intact


def test_1_one_sender_a_second_instance_sends_nothing_while_the_first_holds_the_lease() -> None:
    world = _two()
    other = world.account(OTHER_WRITER)

    def submit(intent: OrderIntent, number: int) -> Ack:
        # While this order is at the broker, the other instance tries the next one.
        with pytest.raises(Halt, match="held by"):
            world.send(2, other)
        return accepted(intent, number, NOW)

    world.broker.answer = submit
    assert world.send(1) is OrderState.ACKNOWLEDGED
    assert world.submits == 1 and world.state(2) is None


def test_2_no_order_goes_out_without_its_sending_stored_even_under_the_lease() -> None:
    world = World()
    # The third journal create after the lease's own is the `sending`.
    _journal_creates(world, lambda op, key: (op, key) == ("create", KEY), 3, _refuse)
    halted(world, "the sending of")
    assert _lease(world).handler is None


def test_3_a_new_holder_never_sends_an_older_holders_order_again() -> None:
    world = _two()
    late = _sent_by_another(world)
    _plant(world, _held())
    assert world.send(1) is OrderState.SENDING
    assert world.submits == 0
    (entry,) = [r for r in world.entries() if isinstance(r, TakeoverEntry)]
    assert entry.late == (late,)


def test_the_pause_scenario_leaves_exactly_one_order_at_the_broker() -> None:
    """A holder passes its checks and freezes. Another takes over, and the first wakes and sends.

    The broker holds one order for the decision, and the older holder's answer
    halts as a late order rather than reaching `Acknowledged`.
    """
    world = _two()
    other = world.account(OTHER_WRITER)

    def submit(intent: OrderIntent, number: int) -> Ack:
        # Frozen past its expiry, between its last check and its request: the other instance
        # takes over and finds the order sending, then sends the plan's next one.
        world.clock.at = NOW + PERIOD + MARGIN + timedelta(seconds=1)
        world.broker.answer = None
        assert world.send(1, other) is OrderState.SENDING
        return accepted(intent, number, NOW)

    world.broker.answer = submit
    with pytest.raises(Halt, match="a late order under an older lease"):
        world.send(1)
    assert world.submits == 1
    assert [o.client_order_id for o in world.broker.had] == [world.orders[0].client_order_id]
    assert world.state(1) is not OrderState.ACKNOWLEDGED


# The loss stop across two instances


def test_a_loss_stop_another_instance_fires_between_the_two_checks_stops_the_send() -> None:
    world = World()
    other = world.account(OTHER_WRITER)

    def fire(key: str) -> None:
        # The gate has approved the order, and its `sending` has landed: the other instance's
        # loss stop fires there with no lease, as #156's `loss` fires it.
        world.store.after = lambda *_: None
        other.path(world.broker, world.report)._shared.latch.fire(
            None, None, "a test fired it", lambda: None
        )

    _journal_creates(world, lambda op, key: (op, key) == ("create", KEY), 3, fire, landed=True)
    halted(world, "loss stop")
    assert world.state() is OrderState.ABANDONED
