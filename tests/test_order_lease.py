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
from dataclasses import replace
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
    Clears,
    FillsRead,
    FoundOrder,
    Halt,
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
    caps_key,
    decode,
    encode,
    kill_switch_key,
    reconcile,
    sha256_of,
    takeover_key,
)
from ithildinruntime.execution.lease import MARGIN, PERIOD, AbsentHalt, TakeoverRead, free_lease
from ithildinruntime.execution.order_path import Outage
from ithildinruntime.execution.outside import fill_entry, order_entry
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
from tests.support.broken_paths import HandedPath
from tests.support.fake_broker import accepted
from tests.support.journals import fill
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
        owed_from=None,
        owed_expired=None,
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
    assert f"held by {FROZEN} of the run {RUN} on the release r1 until " in reason
    takeover_at = (NOW + timedelta(seconds=10) + MARGIN).isoformat()
    assert f"the first take after {takeover_at} takes it over and alarms" in reason
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
    reason = halted(
        world,
        "rewritten since this handler.s last answered write of it, so its renewal was refused",
    )
    assert "another handler took it over after its expiry" in reason
    assert "a write of this handler's own landed with its answer lost" in reason
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
    (said,) = [a for a in world.alarms.sent if "release was not stored" in a]
    assert f"It expires no earlier than {(NOW + PERIOD).isoformat()}" in said
    # The next order of this process meets the lease as held, and the page above says why.
    with pytest.raises(Halt, match="held by"):
        world.path().send(world.orders[0].client_order_id)
    assert world.submits == 1


def test_a_release_the_store_refuses_after_a_send_that_halted_still_pages() -> None:
    world = World()
    world.store.put(kill_switch_key(ACCOUNT), b"{}")

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            raise StoreUnavailable(key, "SlowDown: the store's own text")

    world.store.before = before
    halted(world, "kill switch stands")
    (said,) = [a for a in world.alarms.sent if "release was not stored" in a]
    assert f"It expires no earlier than {(NOW + PERIOD).isoformat()}" in said


def test_a_release_another_handler_refused_pages_without_an_outage() -> None:
    world = World()
    replaced: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            replaced.append(number)
            if len(replaced) == 2:
                _plant(world, _held("e" * 32 + "-1", expires=timedelta(seconds=30)))

    world.store.before = before
    assert world.send() is OrderState.ACKNOWLEDGED
    (said,) = world.alarms.sent
    assert "so its release was refused" in said
    assert f"It expires no earlier than {(NOW + PERIOD).isoformat()}" in said
    assert "The owner's step" not in said


def test_a_takeover_list_whose_key_stands_and_cannot_be_read_is_an_outage() -> None:
    # A list already stands at the takeover's first key, so the create meets it and reads it to
    # compare. A store that does not answer that read pages once, with the owner's step.
    world = _two()
    _sent_by_another(world)
    _plant(world, _held())
    world.store.put(takeover_key(DEPLOYMENT, RUN, NOW), b"{}")

    def before(number: int, op: str, key: str) -> None:
        if op == "head" and "/takeovers/" in key:
            raise StoreUnavailable(key, "SlowDown: the store's own text")

    world.store.before = before
    with pytest.raises(Outage) as raised:
        world.send(2)
    assert "could not be read" in raised.value.reason
    (page,) = world.alarms.sent
    assert "the takeover of the account's lease failed" in page and "The owner's step" in page


def test_a_takeover_whose_journal_write_is_an_outage_that_paged_pages_no_more() -> None:
    # The storage layer raises an `Outage` once a write passes its deadline, after its own page,
    # so the failed takeover halts on that outage and sends no page of its own.
    world = _two()
    _sent_by_another(world)
    _plant(world, _held())

    def late(key: str) -> None:
        world.alarms.sent.append(f"the store did not answer {key} by its deadline")
        raise Outage(f"the store did not answer {key} by its deadline")

    _journal_creates(world, lambda op, key: op == "create" and "/takeovers/" in key, 1, late)
    with pytest.raises(Outage) as raised:
        world.send(2)
    assert "the takeover of the account's lease failed" in raised.value.reason
    assert "No new order goes out until a takeover stores the list" in raised.value.reason
    assert not any("the takeover of the account's lease failed" in a for a in world.alarms.sent)


def test_an_outage_at_the_renewal_pages_once_and_its_halt_names_the_lease_left_held() -> None:
    # The take creates the lease, the renewal is its first rewrite and the release its second.
    # The store stops answering from the renewal on, so the release meets the same outage, sends
    # no second page, and the halt says how long the lease stays held.
    world = World()
    down: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY) or down:
            down.append(number)
            raise StoreUnavailable(key, "SlowDown: the store's own text")

    world.store.before = before
    with pytest.raises(Outage) as raised:
        world.send()
    assert world.submits == 0
    (said,) = world.alarms.sent
    assert "The owner's step" in said and "release" not in said
    reason = raised.value.reason
    assert f"It expires no earlier than {(NOW + PERIOD).isoformat()}" in reason
    assert "The account's lease's release was not stored" in reason
    assert reason.endswith(
        "Whether it landed is unknown. If it did not, the next take after that takes it over and "
        "alarms"
    )


def test_a_release_another_handler_refused_after_an_outage_still_pages() -> None:
    # The second read of the hard caps meets a store that does not answer, which pages once.
    # Another handler then holds the lease, so the release is refused, which is no outage and
    # pages on its own.
    world = World()
    caps: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("get", caps_key(ACCOUNT)):
            caps.append(number)
            if len(caps) > 1:
                raise StoreUnavailable(key, "SlowDown: the store's own text")
        elif (op, key) == ("replace", KEY) and len(caps) > 1:
            _plant(world, _held("e" * 32 + "-1", expires=timedelta(seconds=30)))

    world.store.before = before
    with pytest.raises(Outage) as raised:
        world.send()
    assert world.submits == 0
    outage, refused = world.alarms.sent
    assert "The owner's step" in outage
    assert "so its release was refused" in refused and "The owner's step" not in refused
    assert "release" not in raised.value.reason.split(". An alarm")[0].split("The owner's step")[0]


@pytest.mark.parametrize("sent", [False, True], ids=["a first use", "a journal that sent"])
def test_a_release_that_finds_the_lease_gone_says_what_the_next_take_does(sent: bool) -> None:
    orders = (order(1, 2), order(2, 1, gates.USO))
    world = World(orders=orders, position=Decimal(3)) if sent else World(orders=orders)
    world.store.put(kill_switch_key(ACCOUNT), b"{}")
    # A journal that sent planted a lease, which the take rewrites, so the release is the second
    # rewrite there and the first on a first use.
    replaces: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            replaces.append(number)
            if len(replaces) == 1 + sent:
                world.store.drop(KEY)

    world.store.before = before
    halted(world, "kill switch stands")
    (said,) = [a for a in world.alarms.sent if "is gone from" in a]
    assert f"the account's lease is gone from {KEY}, so its release was refused" in said
    assert "halts on it as absent where the journal holds a sending" in said
    assert "otherwise starts the lease anew" in said and "expires" not in said
    assert "The owner's step" not in said, "a gone lease is not a store's outage"
    world.store.before = lambda *_: None
    world.store.drop(kill_switch_key(ACCOUNT))
    if sent:
        halted(world, "lease is absent", lambda: world.send(2))
    else:
        assert world.send(2) is OrderState.ACKNOWLEDGED


@pytest.mark.parametrize("sent", [False, True], ids=["a first use", "a journal that sent"])
def test_a_renewal_that_finds_the_lease_gone_sends_nothing(sent: bool) -> None:
    orders = (order(1, 2), order(2, 1, gates.USO))
    world = World(orders=orders, position=Decimal(3)) if sent else World(orders=orders)
    # A journal that sent planted a lease, which the take rewrites, so the renewal is the second
    # rewrite there and the first on a first use.
    replaces: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            replaces.append(number)
            if len(replaces) == 1 + sent:
                world.store.drop(KEY)

    world.store.before = before
    reason = halted(world, f"the account's lease is gone from {KEY}, so its renewal was refused")
    assert "The owner's step" not in reason, "a gone lease is not a store's outage"
    assert world.state() is OrderState.ABANDONED
    # The renewal pages nothing itself, so the release's refusal is the one page naming the lease.
    named = [a for a in world.alarms.sent if f"gone from {KEY}" in a]
    assert len(named) == 1 and "so its release was refused" in named[0]
    world.store.before = lambda *_: None
    # The next take finds the lease lost: on a first use its only `sending` is the refused
    # order's own, and on a journal that sent an earlier one stands as well.
    halted(world, "lease is absent", lambda: world.send(2))


def test_a_renewal_unanswered_that_finds_the_lease_gone_on_reading_back_sends_nothing() -> None:
    """The renewal's answer is lost, and the read that settles it finds no lease at all."""
    world = World()
    replaces: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            replaces.append(number)
            if len(replaces) == 1:
                world.store.drop(KEY)
                raise StoreUnavailable(key, "no answer")

    world.store.before = before
    reason = halted(world, f"the account's lease is gone from {KEY}, so its renewal was refused")
    assert "The owner's step" not in reason
    assert world.state() is OrderState.ABANDONED
    # The renewal is sent once, as the read-back settles it, and the release once.
    assert len(replaces) == 2


def test_a_release_unanswered_that_finds_the_lease_gone_on_reading_back_pages_it_gone() -> None:
    world = World()
    replaces: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            replaces.append(number)
            if len(replaces) == 2:
                world.store.drop(KEY)
                raise StoreUnavailable(key, "no answer")

    world.store.before = before
    assert world.send() is OrderState.ACKNOWLEDGED
    (said,) = world.alarms.sent
    assert f"the account's lease is gone from {KEY}, so its release was refused" in said
    assert "The owner's step" not in said and len(replaces) == 2


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
    assert f"absent at {KEY}" in reason and f"the halt stands at {key}" in reason
    stored = decode(world.store.blobs[key], AbsentHalt)
    assert (stored.format, stored.account, stored.through) == (1, ACCOUNT, view.through.seq)
    assert stored.sent_at == view.sent_at and stored.halt.reason == reason
    assert any(reason in said for said in world.alarms.sent)


def test_an_absent_lease_whose_halt_key_holds_other_bytes_leaves_them_alone() -> None:
    world = _lost()
    view = world.reader().view()
    assert view.sent_at is not None
    key = account_halt_key(ACCOUNT, "execution", "lease_absent", view.sent_at, view.through.seq)
    world.store.put(key, b"another finder's halt")
    halted(world, "lease is absent")
    assert world.store.blobs[key] == b"another finder's halt"


def test_an_absent_lease_whose_halt_the_store_refuses_pages_once_as_an_outage() -> None:
    world = _lost()
    world.store.refuse(("create", account_halts_prefix(ACCOUNT)))
    with pytest.raises(Outage) as raised:
        world.send()
    assert world.submits == 0
    reason = raised.value.reason
    assert "lease is absent" in reason
    assert "Its halt was not stored at" in reason and "StoreUnavailable AccessDenied" in reason
    # A store that did not take the halt is an outage, which pages once with the owner's step.
    (said,) = world.alarms.sent
    assert "Its halt was not stored" in said and "The owner's step" in said
    # No halt stands yet, so the page says what the next order does rather than naming a release.
    assert "until a person releases it" not in reason
    assert reason.endswith("tries to store the halt again. The owner's step: the broker's site")
    assert not _halts(world)


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
    assert (stored.format, stored.account, stored.count, stored.orders.orders) == (
        1,
        ACCOUNT,
        12,
        (),
    )
    assert stored.handler.startswith("1" * 32 + "-") and stored.release is None
    assert sha256_of(world.store.blobs[entry.orders.key]) == entry.orders.sha256
    sent = world.reader().view().orders[late].sent_at
    assert sent is not None and entry.since == stored.since == sent - timedelta(seconds=60)
    # The take, the rewrite that clears `owed`, the renewal and the release.
    assert _lease(world) == free_lease(15, False)
    (said,) = [a for a in world.alarms.sent if "taken over from" in a]
    ran_out = (NOW - timedelta(minutes=1)).isoformat()
    assert f"taken over from {FROZEN} of the run {RUN}, whose lease ran out at {ran_out}" in said
    assert f"the broker's order list is stored at {entry.orders.key}, and finds {late} late" in said
    # Scoped to its stored list, so a second takeover pages apart, and late orders need a person.
    (raised,) = [a for a in world.alarms.alarms if "taken over from" in a.reason]
    assert raised.cause == f"execution.lease-taken:{entry.orders.key}:{ACCOUNT}"
    assert raised.clears is Clears.BY_MODE
    # An ordinary takeover names no earlier taker that held the lease with the takeover owed.
    assert "with the takeover still owed" not in said


def test_a_takeover_owed_names_its_holder_and_the_taker_that_died_holding_it() -> None:
    world = _two()
    late = _sent_by_another(world)
    dead = "d" * 32 + "-1"
    owed = replace(
        _held(dead), owed=True, owed_from=FROZEN, owed_expired=NOW - timedelta(minutes=2)
    )
    _plant(world, owed)
    assert world.send(2) is OrderState.ACKNOWLEDGED
    (entry,) = [r for r in world.entries() if isinstance(r, TakeoverEntry)]
    assert (entry.holder, entry.late) == (FROZEN, (late,))
    (said,) = [a for a in world.alarms.sent if "taken over from" in a]
    assert f"taken over from {FROZEN}, whose lease ran out at" in said
    assert f"after {dead}, which held it last with the takeover still owed" in said


def test_an_order_the_broker_lists_is_not_named_late() -> None:
    world = _two()
    _, entry = _taken_over(world, _row(world.orders[0]))
    assert entry.late == ()
    # A takeover that finds no late order leaves nothing for a person to clear.
    (raised,) = [a for a in world.alarms.alarms if "taken over from" in a.reason]
    assert raised.clears is Clears.NOBODY


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
    if fails == "a list not whole":
        # The partial list is stored before it is judged, and the reason names it.
        (kept,) = [k for k in world.store.blobs if "/execution/takeovers/" in k]
        assert f"did not come whole, as {kept}" in reason
        assert decode(world.store.blobs[kept], TakeoverRead).orders.whole is False
    if fails == "the journal":
        # The list stood stored, and the reason names it, since no entry does.
        assert f"the list at {takeover_key(DEPLOYMENT, RUN, NOW)}" in reason
    assert any(reason.split(". An alarm")[0] in said for said in world.alarms.sent)
    # One page for the failed takeover, and a store that did not take a write gives its owner's
    # step, as every outage does.
    (page,) = [a for a in world.alarms.sent if "the takeover of the account's lease failed" in a]
    assert ("The owner's step" in page) == (fails in ("the store", "the journal"))
    assert len(world.alarms.sent) == 1, world.alarms.sent
    assert world.every_kind() == before
    lease = _lease(world)
    assert lease.handler is None and lease.owed
    # The next take does the takeover still owed, from a free lease.
    world.store.before = lambda *_: None
    world.broker.reads.pop("orders", None)
    assert world.send(2) is OrderState.ACKNOWLEDGED
    (entry,) = [r for r in world.entries() if isinstance(r, TakeoverEntry)]
    # The release kept whom the takeover is owed from, so the entry and the alarm name them.
    assert (entry.holder, entry.expired, entry.late) == (
        FROZEN,
        NOW - timedelta(minutes=1),
        (late,),
    )
    assert any(f"taken over from {FROZEN}" in said for said in world.alarms.sent)
    # The release left the lease free, so no taker held it last with the takeover owed.
    assert not any("with the takeover still owed" in said for said in world.alarms.sent)
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


@pytest.mark.parametrize("sent", [False, True], ids=["no sending", "a sending"])
@pytest.mark.parametrize("answered", [False, True], ids=["unanswered", "answered"])
def test_a_takeover_whose_closing_rewrite_finds_the_lease_gone_names_the_absence(
    answered: bool, sent: bool
) -> None:
    world = _two()
    if sent:
        _sent_by_another(world)
    _plant(world, _held())
    replaces: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            replaces.append(number)
            if len(replaces) == 2:
                world.store.drop(KEY)
                if not answered:
                    raise StoreUnavailable(key, "no answer")

    world.store.before = before
    reason = halted(world, "is gone from", lambda: world.send(2))
    assert len(replaces) == 2, "no release rewrites a lease that is gone"
    assert "rewrite after its takeover was refused" in reason
    assert "until a takeover stores the list" not in reason
    assert "The owner's step" not in reason
    assert "takeover" in world.every_kind()
    # One page names the gone lease, and no release follows it.
    gone = [a for a in world.alarms.sent if "is gone from" in a]
    assert len(gone) == 1 and "rewrite after its takeover was refused" in gone[0]
    world.store.before = lambda *_: None
    if sent:
        halted(world, "lease is absent", lambda: world.send(2))
    else:
        # With no `sending`, the next take starts the lease anew.
        assert world.send(2) is OrderState.ACKNOWLEDGED


@pytest.mark.parametrize("answered", [False, True], ids=["unanswered", "refused"])
def test_a_takeover_whose_closing_rewrite_fails_has_alarmed_and_halts(answered: bool) -> None:
    world = _two()
    _sent_by_another(world)
    _plant(world, _held())
    replaced: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            replaced.append(number)
            if len(replaced) >= 2:
                if answered:
                    # Another handler's take, which carries the takeover still owed from the
                    # frozen holder and when that holder's lease ran out.
                    other = _held("e" * 32 + "-1", expires=timedelta(seconds=30))
                    ran_out = NOW - timedelta(minutes=1)
                    _plant(world, replace(other, owed=True, owed_from=FROZEN, owed_expired=ran_out))
                    world.store.before = lambda *_: None
                    return
                raise StoreUnavailable(key, "SlowDown: the store's own text")

    world.store.before = before
    if answered:
        reason = halted(world, "now carries another handler's rewrite", lambda: world.send(2))
        # The entry is journaled, so the halt says it stands, not that the takeover failed.
        assert "takeover entry stands journaled" in reason
        assert "A handler took the lease over after this handler's ran out" in reason
        assert "journals a takeover of its own before it acts, if it has not already" in reason
        assert "until a takeover stores the list" not in reason
        assert "a write of this handler's own" not in reason
        # Another handler holds the lease, so no release is tried and the halt is the one page.
        pages = [a for a in world.alarms.sent if "now carries another handler's rewrite" in a]
        assert len(pages) == 1
        assert not [a for a in world.alarms.sent if "takeover of the account's lease failed" in a]
        assert not [a for a in world.alarms.sent if "release was refused" in a]
    else:
        # The entry is journaled, so the halt is the store's outage, not a takeover still owed.
        with pytest.raises(Outage) as raised:
            world.send(2)
        assert "rewrite after its takeover was not stored" in raised.value.reason
        assert "The owner's step: the broker's site" in raised.value.reason
        assert "until a takeover stores the list" not in raised.value.reason
        assert world.submits == 0
    assert "takeover" in world.every_kind()
    assert any(f"taken over from {FROZEN}" in said for said in world.alarms.sent)


def test_a_takeovers_list_whose_create_landed_unanswered_is_its_own() -> None:
    world = _two()
    lost: list[str] = []

    def after(number: int, op: str, key: str) -> None:
        if op == "create" and "/execution/takeovers/" in key and not lost:
            lost.append(key)
            raise StoreUnavailable(key, "no answer")

    world.store.after = after
    late, entry = _taken_over(world)
    assert entry.orders.key == takeover_key(DEPLOYMENT, RUN, NOW) == lost[0]
    stored = [k for k in world.store.blobs if "/execution/takeovers/" in k]
    assert stored == [entry.orders.key] and entry.late == (late,)


def test_a_takeover_that_loses_its_number_is_journaled_on_a_fresh_view() -> None:
    world = _two()
    other = Journal(world.store, ACCOUNT, "3" * 32)
    taken: list[int] = []

    def before(key: str) -> None:
        if not taken:
            taken.append(
                world.record(ReadEntry("execution/send", AT, BlobRef("r", "b" * 64), AT), other)
            )

    _journal_creates(world, lambda op, key: op == "create" and "/takeovers/" in key, 1, before)
    late, entry = _taken_over(world)
    seqs = [e.seq for e in world.reader().read() if e.body["kind"] == "takeover"]
    assert seqs == [taken[0] + 1] and entry.late == (late,)


def test_a_takeover_that_loses_its_number_six_times_halts_and_pages() -> None:
    world = _two()
    late = _sent_by_another(world)
    _plant(world, _held())
    other = Journal(world.store, ACCOUNT, "3" * 32)
    listed: list[str] = []

    def before(number: int, op: str, key: str) -> None:
        if op == "create" and "/takeovers/" in key:
            listed.append(key)
        elif listed and op == "create" and key.startswith(f"journal/{ACCOUNT}/0"):
            world.record(ReadEntry("execution/send", AT, BlobRef("r", "b" * 64), AT), other)

    world.store.before = before
    halted(world, "lost its number 6 times", lambda: world.send(2))
    assert any("lost its number 6 times" in said for said in world.alarms.sent)
    assert "takeover" not in world.every_kind() and _lease(world).owed
    assert late in world.reader().view().orders


def test_the_fold_refuses_a_takeover_naming_an_order_that_has_ended_or_is_none() -> None:
    world = World()
    assert world.send() is OrderState.ACKNOWLEDGED
    name = world.orders[0].client_order_id
    ref = BlobRef("t.json", "c" * 64)
    for named in (name, "no-such-order"):
        with pytest.raises(Halt, match=f"names {named} as late"):
            world.record(TakeoverEntry('"t"', 9, None, None, NOW, ref, (named,)))


def test_a_renewal_the_store_never_answers_is_an_outage_that_pages() -> None:
    world = World()

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("replace", KEY):
            raise StoreUnavailable(key, "SlowDown: the store's own text")

    world.store.before = before
    with pytest.raises(Outage) as raised:
        world.send()
    assert "renewal was not stored: StoreUnavailable SlowDown" in raised.value.reason
    assert "The owner's step: the broker's site" in raised.value.reason
    assert any("renewal was not stored" in said for said in world.alarms.sent)
    assert world.submits == 0 and world.state() is OrderState.ABANDONED


def test_no_order_goes_out_on_a_path_that_holds_no_lease() -> None:
    world = World()
    holding = HandedPath(world.account()._shared, world.broker, world.report)
    ack = holding.sending(world.orders[0].client_order_id)
    # Another path of the same account, which took no lease, is handed the acknowledgement.
    other = HandedPath(holding._held, world.broker, world.report)
    with pytest.raises(Halt, match="no order goes out without the account's lease"):
        other.handed(ack)
    assert world.submits == 0


def test_a_lease_gone_between_the_read_and_the_rewrite_of_a_first_use_is_created() -> None:
    world = World()
    _plant(world, free_lease(4, False))
    real = world.store.replace_if_match
    gone: list[str] = []

    def vanish(key: str, data: bytes, sha256: str, etag: str) -> str:
        if key == KEY and not gone:
            gone.append(key)
            world.store.drop(KEY)
        return real(key, data, sha256, etag)

    world.store.replace_if_match = vanish  # type: ignore[method-assign]
    assert world.send() is OrderState.ACKNOWLEDGED
    assert gone and not _halts(world)


# Guard 4: a late order under an older lease halts before `Acknowledged`


def test_4_the_older_holders_own_answer_is_never_journaled_acknowledged() -> None:
    world = _two()
    late, _ = _taken_over(world)
    with pytest.raises(
        Halt,
        match=f"acknowledges {late}, a late order under an older lease.*a person looks it up",
    ):
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

    with pytest.raises(
        Halt, match="a late order under an older lease.*so a person looks it up there"
    ):
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
    assert any(
        "a late order under an older lease" in s and "so a person looks it up there" in s
        for s in said
    ), said
    plain = [str(d) for d in reconcile(view.book, view.live.values(), read)]
    assert not any("late order" in s for s in plain)
    assert late in view.late


@pytest.mark.parametrize("met", ["an open order", "a fill"])
def test_4_the_brokers_read_that_lists_a_late_order_halts_naming_it(met: str) -> None:
    """The sync meets the broker's rows before its reconcile, and names a late one there."""
    world = _two()
    late, _ = _taken_over(world)
    view = world.reader().view()
    with pytest.raises(
        Halt,
        match=f"under {late}, a late order under an older lease.*so a person looks it up there",
    ):
        if met == "an open order":
            order_entry(view, _row(world.orders[0]), 2)
        else:
            made = fill("f-late", "b-late", 2, 1, gates.GLD, client_order_id=late)
            fill_entry(view, made, 2)


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
    with pytest.raises(Halt, match="a late order under an older lease") as raised:
        world.send(1)
    took = [e.seq for e in world.reader().read() if e.body["kind"] == "takeover"]
    assert f"the takeover at entry {took[0]} found it sent with no answer" in raised.value.reason
    assert "so a person looks it up there" in raised.value.reason
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
