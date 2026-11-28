"""The loss stop's latch: the fold of its entries, its firing, its completion and its state.

docs/architecture/06-runtime.md's `#### What happens when the stop fires` gives
the rules, and #156 the cases. A clear is made by hand here in the three steps
#203's command will take: complete the firing, journal `stop_cleared`, and
delete the blob on its tag.
"""

from __future__ import annotations

import ast
import dataclasses
import re
import threading
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.core import (
    BlobMissing,
    BlobRef,
    Halt,
    LossReading,
    OrderState,
    StopState,
    StoreUnavailable,
    decode,
    encode,
    journal_entry_key,
    loss_stop_key,
)
from ithildinruntime.execution.latch import StopBlob
from ithildinruntime.journal import (
    IntentEntry,
    ReadEntry,
    StopClearedEntry,
    StopFiredEntry,
    StopStoredEntry,
    VerdictEntry,
)
from ithildinruntime.risk import Gate
from tests.support import gates
from tests.support.journals import AT, planned, verdict
from tests.support.journals import started as journal_started
from tests.support.order_world import ACCOUNT, CASH, DEPLOYMENT, RUN, World, halted, order

KEY = loss_stop_key(ACCOUNT)
PAST = LossReading(Decimal("100.0001"), Decimal("100.0000"), True, date(2026, 9, 30))
SRC = Path(__file__).parents[1] / "src" / "ithildinruntime"


def fired(firing: str = "a" * 32) -> StopFiredEntry:
    return StopFiredEntry(firing, "d-1", "r-1", AT, PAST, "the day's loss read past its stop")


def past_verdict(name: str) -> VerdictEntry:
    made = verdict(name, approved=False)
    return dataclasses.replace(made, verdict=dataclasses.replace(made.verdict, loss=PAST))


ORDERS = (planned(1, 1), planned(2, 1), planned(3, 1))
NAMES = [made.client_order_id for made in ORDERS]


class Folded:
    """A journal in memory, recorded through the fold's checks, with three orders intended."""

    def __init__(self) -> None:
        self.journal = journal_started(*ORDERS)
        for made in ORDERS:
            self.add(IntentEntry(3, made))

    def add(self, record: object) -> int:
        view = self.journal.view()
        if isinstance(record, VerdictEntry):
            record = dataclasses.replace(record, book=view.through.seq)
        ack, _ = self.journal.record(view, record, AT)  # type: ignore[arg-type]
        return ack.seq

    def refused(self, record: object, match: str) -> None:
        with pytest.raises(Halt, match=match):
            self.add(record)

    @property
    def stop(self):  # type: ignore[no-untyped-def]
        return self.journal.view().stop


# The fold


def test_a_firing_stands_until_a_clear_of_its_stored_blob() -> None:
    made = Folded()
    assert not made.stop.fired
    seq = made.add(fired())
    assert made.stop.fired and made.stop.standing.seq == seq
    made.add(StopStoredEntry(seq, "t-1"))
    assert made.stop.standing.stored == StopStoredEntry(seq, "t-1")
    clear = made.add(StopClearedEntry(seq))
    stop = made.stop
    assert not stop.fired and stop.covered == {"t-1"} and stop.tags == {"t-1": seq}
    assert stop.entries == {seq, seq + 1, clear}


def test_a_second_firing_while_one_stands_is_refused() -> None:
    made = Folded()
    made.add(fired())
    made.refused(fired("b" * 32), "stands already")


def test_a_firing_under_an_id_an_entry_took_is_refused() -> None:
    made = Folded()
    seq = made.add(fired())
    made.add(StopStoredEntry(seq, "t-1"))
    made.add(StopClearedEntry(seq))
    made.refused(fired(), f"the id entry {seq} took")


@pytest.mark.parametrize(
    ("named", "match"),
    [
        (lambda seq: seq + 5, "which is no firing that stands"),
        (lambda seq: seq - 1, "which is no firing that stands"),
    ],
    ids=["a later entry", "an entry that is no firing"],
)
def test_a_store_or_a_clear_that_names_anything_but_the_standing_firing_is_refused(
    named, match
) -> None:
    made = Folded()
    seq = made.add(fired())
    made.refused(StopStoredEntry(named(seq), "t-1"), match)
    made.add(StopStoredEntry(seq, "t-1"))
    made.refused(StopClearedEntry(named(seq)), match)


def test_a_clear_of_a_firing_whose_blob_was_never_stored_is_refused() -> None:
    made = Folded()
    seq = made.add(fired())
    made.refused(StopClearedEntry(seq), "whose blob was never stored")


def test_a_clear_of_a_covered_firing_is_refused() -> None:
    made = Folded()
    seq = made.add(fired())
    made.add(StopStoredEntry(seq, "t-1"))
    made.add(StopClearedEntry(seq))
    made.refused(StopClearedEntry(seq), "which is no firing that stands")
    made.refused(StopStoredEntry(seq, "t-2"), "which is no firing that stands")


def test_a_second_store_and_a_covered_tag_are_refused() -> None:
    made = Folded()
    seq = made.add(fired())
    made.add(StopStoredEntry(seq, "t-1"))
    made.refused(StopStoredEntry(seq, "t-2"), "is stored already")
    made.add(StopClearedEntry(seq))
    again = made.add(fired("b" * 32))
    made.refused(StopStoredEntry(again, "t-1"), "a covered firing's blob had")


def test_a_past_verdict_stands_alone_until_a_firing_comes_and_then_belongs_to_it() -> None:
    made = Folded()
    one = made.add(past_verdict(NAMES[0]))
    assert made.stop.fired and [seq for seq, _ in made.stop.alone] == [one]
    seq = made.add(fired())
    assert made.stop.alone == () and made.stop.standing.verdicts == (one,)
    two = made.add(past_verdict(NAMES[1]))
    assert made.stop.standing.verdicts == (one, two)
    made.add(StopStoredEntry(seq, "t-1"))
    made.add(StopClearedEntry(seq))
    assert not made.stop.fired


def test_a_verdict_inside_its_stop_is_not_kept_for_the_stop() -> None:
    made = Folded()
    made.add(verdict(NAMES[0], approved=False))
    assert not made.stop.fired and made.stop.entries == frozenset()


def test_no_module_but_the_journal_builds_a_clear() -> None:
    """Only #203's command clears a firing, so no worker builds the entry."""
    found = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and "StopClearedEntry" in (getattr(node.func, "id", None), getattr(node.func, "attr", None))
    }
    assert found == set()


# Firing, through the order path


def losing(loss: Decimal = 100 + Decimal("0.0001"), **changes: object) -> World:
    return World(
        valuation=gates.valuation(CASH + loss, replayed_through=1),
        constraints=(gates.constraints(loss_stop=gates.d(100)),),
        **changes,  # type: ignore[arg-type]
    )


def stop_kinds(world: World) -> list[str]:
    return [k for k in world.every_kind() if k.startswith("stop_")]


def standing(world: World):  # type: ignore[no-untyped-def]
    return world.reader().view().stop


def blob(world: World) -> StopBlob:
    return decode(world.store.blobs[KEY], StopBlob)


def test_a_loss_past_its_stop_journals_the_firing_and_stores_its_blob() -> None:
    world = losing()
    halted(
        world,
        "past its stop of 100",
        lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION, world.strategies()),
    )
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]
    stop = standing(world)
    entry = stop.standing.entry
    assert entry.reading.past and entry.reading.loss == Decimal("100.0001")
    assert (entry.deployment, entry.run, entry.decision_at) == (DEPLOYMENT, RUN, gates.DECISION)
    made = blob(world)
    assert (made.firing, made.fired, made.account) == (entry.firing, stop.standing.seq, ACCOUNT)
    assert stop.standing.stored.etag == world.store.tags[KEY]


def recovered(orders: int = 2) -> World:
    """A decision inside its stop, after a firing of an earlier one: the price recovered."""
    world = World(orders=tuple(order(n, 1) for n in range(1, orders + 1)))
    latch = world.account()._shared.latch
    assert (
        latch.fire(PAST, (DEPLOYMENT, RUN, gates.DECISION), "an earlier loss", world.report) == ""
    )
    return world


def test_the_order_after_a_firing_is_refused_with_the_firing_named_once_the_price_recovers() -> (
    None
):
    world = recovered()
    seq = standing(world).standing.seq
    reason = halted(world, "loss stop stands")
    assert (
        f"entry {seq} fired it, on a day's loss of 100.0001 past its stop of 100.0000, measured "
        "from the valuation of 2026-09-30. `ithildin stop clear --account paper-1` clears it"
    ) in reason
    assert world.state() is OrderState.REFUSED
    halted(
        world,
        "loss stop stands",
        lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION, world.strategies()),
    )


def test_a_second_reading_past_its_stop_before_a_clear_records_nothing_more() -> None:
    world = losing()
    for _ in range(2):
        halted(
            world,
            "past its stop",
            lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION, world.strategies()),
        )
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]


def test_a_rerun_after_the_close_fires_on_the_decisions_own_loss() -> None:
    world = losing()
    world.clock.at = gates.CLOSE + timedelta(minutes=1)
    halted(
        world,
        "past its stop",
        lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION, world.strategies()),
    )
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]
    entry = standing(world).standing.entry
    assert entry.reason == "the day's loss at the decision's own time read past its stop"


def test_an_unread_loss_fires_only_where_asked() -> None:
    world = World()
    world.store.drop(next(k for k in world.store.blobs if "valuation" in k))
    missing = "valuation.json, listed by .* is missing"
    halted(
        world,
        missing,
        lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION, world.strategies()),
    )
    assert stop_kinds(world) == [] and world.alarms.sent == []
    reason = halted(
        world,
        "^the loss stop fired with no reading",
        lambda: world.account().loss(
            (DEPLOYMENT, RUN, gates.DECISION),
            world.strategies(),
            world.report,
            after="a halt",
            unread=True,
        ),
    )
    assert re.search(missing, reason) and world.alarms.sent == [f"account {ACCOUNT}: {reason}"]
    entry = standing(world).standing.entry
    assert entry.reading is None and "could not be read" in entry.reason
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]


def test_a_firing_held_between_the_last_check_and_the_call_stops_the_call() -> None:
    world = World()
    account = world.account()
    strategy_halts = f"accounts/{ACCOUNT}/strategies/{gates.CHAN}/halts/"

    lists: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        # The strategy's halts are read last among the controls, once before the gate and
        # once in the submit's checks.
        if op == "list" and key == strategy_halts:
            lists.append(number)
            if len(lists) == 2:
                account._shared.latch._pending.append(fired())

    world.store.before = before
    halted(world, "is not sent: the loss stop fired", lambda: world.send(account=account))
    assert world.state() is OrderState.ABANDONED


# Each write failing, and a restart after it


def entry_prefix() -> str:
    return journal_entry_key(ACCOUNT, 1).rsplit("/", 1)[0] + "/"


def fire_in(world: World, account=None) -> str:  # type: ignore[no-untyped-def]
    """Fire the stop through a new process's latch, and return what the halt would add."""
    latch = (account or world.account())._shared.latch
    return latch.fire(PAST, (DEPLOYMENT, RUN, gates.DECISION), "an earlier loss", world.report)


def answered(world: World) -> None:
    world.store.before = lambda n, op, k: None


def test_a_firing_the_journal_refuses_stands_by_its_blob_and_is_journaled_later() -> None:
    world = World()
    world.store.refuse(("create", entry_prefix()))
    assert "its entry was not stored" in fire_in(world) and KEY in world.store.blobs
    assert stop_kinds(world) == []
    answered(world)
    # A new process finds the blob alone, refuses, and its settle journals the firing.
    reason = halted(world, "loss stop stands")
    assert "fired it, on a day's loss of 100.0001" in reason
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]
    assert standing(world).standing.entry.firing == blob(world).firing


def test_a_firing_whose_blob_is_refused_stands_by_its_entry_and_gets_its_blob_later() -> None:
    world = World()
    world.store.refuse(("create", KEY))
    assert f"{KEY} was not stored, and is held" in fire_in(world)
    assert KEY not in world.store.blobs and stop_kinds(world) == ["stop_fired"]
    answered(world)
    halted(world, "loss stop stands")
    assert stop_kinds(world) == ["stop_fired", "stop_stored"] and KEY in world.store.blobs


def test_a_firing_both_writes_refuse_is_held_and_keeps_the_process_until_stored() -> None:
    world = World(orders=(order(1, 1), order(2, 1)))
    world.store.refuse(("create", entry_prefix()), ("create", KEY))
    account = world.account()
    storage = account._shared.storage
    assert "is held" in fire_in(world, account)
    assert [ref.key for ref in storage.unstored()] == [KEY] and account._shared.latch.held()
    # The same process refuses while it holds the firing, and a halt names the held blob.
    answered(world)
    world.store.refuse(("create", KEY))
    reason = halted(world, "is not wholly stored", lambda: world.send(account=account))
    assert f"{KEY} is held until S3 stores it" in reason
    assert reason.count("Captures not yet stored") <= 1 and len(storage.unstored()) == 1
    assert stop_kinds(world) == ["stop_fired"]
    answered(world)
    storage.retry_held()
    assert storage.unstored() == () and KEY in world.store.blobs
    halted(world, "loss stop stands", lambda: world.send(2))
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]


def test_a_firing_held_in_memory_alone_refuses_in_its_own_process() -> None:
    world = World()
    account = world.account()
    world.store.refuse(("create", entry_prefix()), ("create", KEY))
    fire_in(world, account)
    assert account._shared.latch.held()
    reason = halted(world, "is not wholly stored", lambda: world.send(account=account))
    assert reason.startswith(
        "the loss stop fired, on a day's loss of 100.0001 past its stop of 100.0000"
    )


def test_a_store_that_refuses_the_stored_entry_leaves_the_firing_unstored_until_later() -> None:
    world = World()
    stored = journal_entry_key(ACCOUNT, len(world.every_kind()) + 2)
    world.store.refuse(("create", stored))
    assert "its stop_stored was not stored" in fire_in(world)
    assert stop_kinds(world) == ["stop_fired"] and KEY in world.store.blobs
    answered(world)
    halted(world, "loss stop stands")
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]


# Clearing by hand, in #203's three steps


def clear(world: World) -> None:
    """Complete the firing, journal its clear, and delete its blob on its tag."""
    world.account()._shared.latch.complete(world.report)
    stop = standing(world)
    world.record(StopClearedEntry(stop.standing.seq))
    world.store.delete_if_match(KEY, stop.standing.stored.etag)


def test_a_clear_that_counts_lets_the_next_order_go() -> None:
    world = recovered()
    halted(world, "loss stop stands")
    clear(world)
    assert not standing(world).fired and KEY not in world.store.blobs
    assert world.send(2) is OrderState.ACKNOWLEDGED


@pytest.mark.parametrize("refused", ["blob", "entry"], ids=["journal-only", "blob-only"])
def test_a_clear_on_a_firing_with_one_record(refused: str) -> None:
    world = World()
    world.store.refuse(("create", KEY if refused == "blob" else entry_prefix()))
    fire_in(world)
    answered(world)
    assert stop_kinds(world) == ([] if refused == "entry" else ["stop_fired"])
    clear(world)
    assert not standing(world).fired and KEY not in world.store.blobs
    assert world.send() is OrderState.ACKNOWLEDGED


def test_a_clear_that_died_before_its_delete_holds_until_the_blob_goes() -> None:
    world = recovered()
    stop = standing(world)
    world.record(StopClearedEntry(stop.standing.seq))
    assert not standing(world).fired
    reason = halted(world, "loss stop stands")
    assert "the journal holds no firing of it yet" in reason
    # A settle leaves a covered firing's blob alone, for the command's first step to delete.
    assert stop_kinds(world) == ["stop_fired", "stop_stored", "stop_cleared"]
    world.store.delete_if_match(KEY, stop.standing.stored.etag)
    assert world.send(2) is OrderState.ACKNOWLEDGED


def test_a_reading_inside_a_clear_is_a_new_firing_that_outlives_the_delete() -> None:
    world = recovered()
    stop = standing(world)
    world.record(StopClearedEntry(stop.standing.seq))
    account = world.account()
    assert fire_in(world, account) == "" and not account._shared.latch.held()
    # The old blob is there, so the new firing is journaled and left unstored.
    assert stop_kinds(world) == ["stop_fired", "stop_stored", "stop_cleared", "stop_fired"]
    world.store.delete_if_match(KEY, stop.standing.stored.etag)
    halted(world, "loss stop stands")
    assert stop_kinds(world)[-1] == "stop_stored" and KEY in world.store.blobs


def test_a_blob_deleted_by_hand_clears_nothing() -> None:
    world = recovered()
    world.store.drop(KEY)
    halted(world, "loss stop stands")
    assert KEY not in world.store.blobs


def test_a_second_losing_session_inside_a_firing_records_nothing_more() -> None:
    world = recovered()
    account = world.account()
    later = (DEPLOYMENT, RUN, gates.DECISION + timedelta(days=1))
    assert account._shared.latch.fire(PAST, later, "the next session's loss", world.report) == ""
    assert not account._shared.latch.held()
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]


# The state


@pytest.mark.parametrize("memory", [False, True])
@pytest.mark.parametrize("journal", [False, True])
@pytest.mark.parametrize("stored", ["absent", "present", "unreadable"])
def test_the_state_reads_each_of_its_three_records(
    memory: bool, journal: bool, stored: str
) -> None:
    world = World()
    account = world.account()
    latch = account._shared.latch
    if journal:
        world.record(fired())
    if memory:
        latch._pending.append(fired("c" * 32))
    if stored == "present":
        world.store.put(KEY, b"{}")
    elif stored == "unreadable":
        world.store.refuse(("get", KEY))
    view = account.view(world.report)
    state, error = latch.state(view, account._shared.patience.tries(world.report))
    if stored == "unreadable":
        assert state is StopState.UNREADABLE and error == "StoreUnavailable AccessDenied"
    elif memory or journal or stored == "present":
        assert (state, error) == (StopState.FIRED, "")
    else:
        assert (state, error) == (StopState.ABSENT, "")


def test_a_read_that_fails_with_nothing_to_complete_raises_nothing() -> None:
    world = World()
    world.store.refuse(("head", KEY), ("get", KEY))
    world.account().settle(world.report)


def test_hold_create_holds_through_an_outage_and_counts_a_blob_already_there() -> None:
    world = World()
    storage = world.storage()
    world.store.refuse(("create", KEY))
    with pytest.raises(Halt, match="S3 did not store"):
        storage.hold_create(KEY, b"one")
    assert [ref.key for ref in storage.unstored()] == [KEY]
    world.store.before = lambda n, op, k: None
    world.store.put(KEY, b"another")
    assert storage.retry_held() != () and storage.unstored() == ()
    assert world.store.blobs[KEY] == b"another"
    assert storage.hold_create(KEY, b"third").key == KEY


def test_head_of_a_missing_blob_is_blob_missing() -> None:
    world = World()
    with pytest.raises(BlobMissing):
        world.store.head(KEY)
    with pytest.raises(StoreUnavailable):
        world.store.refuse(("head", KEY))
        world.store.head(KEY)


# What round 1 of the review asked for


def test_a_firing_stored_whole_by_another_thread_before_the_call_stops_it() -> None:
    """A firing held and stored whole between the last check and the call leaves no memory."""
    world = World()
    account = world.account()
    strategy_halts = f"accounts/{ACCOUNT}/strategies/{gates.CHAN}/halts/"
    lists: list[int] = []
    added: list[str] = []

    def before(number: int, op: str, key: str) -> None:
        if op == "list" and key == strategy_halts:
            lists.append(number)
            if len(lists) == 2:
                added.append(fire_in(world, account))

    world.store.before = before
    halted(world, "is not sent: the loss stop fired", lambda: world.send(account=account))
    assert added == [""] and not account._shared.latch.held()
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]
    assert world.state() is OrderState.ABANDONED


def test_a_firing_journaled_between_the_verdict_and_the_submit_stops_the_order() -> None:
    """Another process's firing that only the journal holds, with no blob."""
    world = World()
    sending = journal_entry_key(ACCOUNT, len(world.every_kind()) + 3)

    def after(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", sending):
            world.record(fired())

    world.store.after = after
    reason = halted(world, "loss stop stands: entry")
    assert KEY not in world.store.blobs and "fired it" in reason
    assert world.state() is OrderState.ABANDONED


def test_the_ask_fires_the_stop_on_a_reading_past_it() -> None:
    world = losing()
    (asked,) = world.path().ask([world.orders[0].client_order_id])
    assert asked.verdict is None and "past its stop" in asked.reason
    assert stop_kinds(world) == ["stop_fired", "stop_stored"] and len(world.alarms.sent) == 1
    assert (
        standing(world).standing.entry.reason
        == "an order's check read the day's loss past its stop"
    )


def test_a_reading_past_its_stop_at_the_clocks_time_alone_fires_it_too() -> None:
    class Later(Gate):
        def loss_against(self, book, snapshot, stop, valuation, state, now):  # type: ignore[no-untyped-def]
            read = super().loss_against(book, snapshot, stop, valuation, state, now)
            if now == gates.DECISION:
                return read
            return dataclasses.replace(read, loss=read.bound + 1, past=True)

    world = World(gate=Later)
    halted(
        world,
        "past its stop",
        lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION, world.strategies()),
    )
    entry = standing(world).standing.entry
    assert entry.reason == "the day's loss at the clock's time read past its stop"
    assert stop_kinds(world) == ["stop_fired", "stop_stored"] and len(world.alarms.sent) == 1


def test_a_past_verdict_standing_alone_gets_its_firing_at_the_next_settle() -> None:
    world = World()
    world.record(IntentEntry(3, world.orders[0]))
    world.record(dataclasses.replace(past_verdict(world.orders[0].client_order_id), book=4))
    assert standing(world).fired and standing(world).standing is None
    world.account()._shared.latch.complete(world.report)
    entry = standing(world).standing.entry
    assert "at entry 5 read the day's loss past its stop" in entry.reason
    assert standing(world).standing.verdicts == (5,) and KEY in world.store.blobs


@pytest.mark.parametrize("taken", [False, True], ids=["another account's", "an id taken"])
def test_an_orphan_blob_that_names_no_firing_this_account_can_take(taken: bool) -> None:
    world = World()
    if taken:
        seq = world.record(fired())
        world.record(StopStoredEntry(seq, "t-old"))
        world.record(StopClearedEntry(seq))
        made = StopBlob("a" * 32, ACCOUNT, None, None, None, PAST, "a copy", None)
    else:
        made = StopBlob("a" * 32, "paper-2", None, None, None, PAST, "another's", None)
    world.store.put(KEY, encode(made))
    world.account()._shared.latch.complete(world.report)
    entry = standing(world).standing.entry
    if taken:
        assert entry.firing != "a" * 32 and entry.reading == PAST
    else:
        assert entry.reading is None and "another account" in entry.reason
    assert standing(world).standing.stored.etag == world.store.tags[KEY]


def test_a_settle_with_nothing_owed_reads_once_and_one_owed_waits_out_the_deadline() -> None:
    world = World()
    world.store.refuse(("list", "journal/"))
    calls = len(world.store.calls)
    world.account()._shared.latch.complete(world.report)
    assert len(world.store.calls) - calls == 1
    account = world.account()
    account._shared.latch._pending.append(fired())
    calls = len(world.store.calls)
    with pytest.raises(Halt, match="the journal could not be read"):
        account._shared.latch.complete(world.report)
    assert len([c for c in world.store.calls[calls:] if c[0] == "list"]) > 1


def test_a_firing_whose_journal_cannot_be_read_still_stores_its_blob() -> None:
    world = World()
    world.store.refuse(("list", "journal/"))
    added = fire_in(world)
    assert "the journal could not be read" in added and KEY in world.store.blobs


def test_a_firing_whose_blob_cannot_be_read_says_so() -> None:
    world = World()
    world.record(fired())
    world.store.refuse(("head", KEY))
    with pytest.raises(Halt, match=f"{KEY} could not be read"):
        world.account()._shared.latch.complete(world.report)


def test_a_blob_held_through_an_interrupt_stays_held() -> None:
    world = World()
    storage = world.storage()

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", KEY):
            raise KeyboardInterrupt

    world.store.before = before
    with pytest.raises(KeyboardInterrupt):
        storage.hold_create(KEY, b"one")
    assert [ref.key for ref in storage.unstored()] == [KEY]


def test_a_held_copy_is_let_go_once_the_journal_has_a_blob_for_the_firing() -> None:
    """Else `retry_held` would store it after a clear deleted the blob that counts."""
    world = World()
    account = world.account()
    storage = account._shared.storage
    world.store.refuse(("create", KEY))
    fire_in(world, account)
    assert [ref.key for ref in storage.unstored()] == [KEY]
    answered(world)
    # Another process stores the firing's blob and its stop_stored, and a person clears it.
    stop = standing(world)
    world.store.put(KEY, b"another process's blob")
    world.record(StopStoredEntry(stop.standing.seq, world.store.tags[KEY]))
    # The firing stands with its blob stored, so the held copy is let go before any clear.
    account._shared.latch.complete(world.report)
    assert storage.unstored() == ()
    world.record(StopClearedEntry(stop.standing.seq))
    world.store.delete_if_match(KEY, world.store.tags[KEY])
    account._shared.latch.complete(world.report)
    assert storage.unstored() == ()
    storage.retry_held()
    assert KEY not in world.store.blobs


def test_a_completion_that_raises_is_named_and_stops_nothing_else() -> None:
    world = World()
    account = world.account()
    latch = account._shared.latch

    def broken(report):  # type: ignore[no-untyped-def]
        raise RuntimeError("x")

    latch._complete = broken  # type: ignore[method-assign]
    assert "completing it raised RuntimeError" in fire_in(world, account)
    latch.complete = broken  # type: ignore[method-assign]
    with pytest.raises(Halt, match="completing the loss stop's firing raised RuntimeError"):
        account._shared.settle(world.report)


def test_a_worker_that_ends_holding_a_firing_names_it() -> None:
    world = World()
    account = world.account()
    world.store.refuse(("create", entry_prefix()), ("create", KEY))
    fire_in(world, account)
    (named,) = account.unstored()
    assert named.startswith("the loss stop's firing ")


def test_a_stop_stored_whose_number_another_writer_took_is_tried_again() -> None:
    world = World()
    stored = journal_entry_key(ACCOUNT, len(world.every_kind()) + 2)
    taken: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", stored) and not taken:
            taken.append(number)
            world.record(ReadEntry("execution/send", AT, BlobRef("r", "b" * 64), AT))

    world.store.before = before
    assert fire_in(world) == "" and taken
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]


# What round 2 of the review asked for


class _Hooked:
    """A lock that runs `call` as it is taken for the `at`-th time, as another thread would."""

    def __init__(self, at: int, call) -> None:  # type: ignore[no-untyped-def]
        self._lock = threading.Lock()
        self._at, self._call, self.taken = at, call, 0

    def __enter__(self) -> None:
        self.taken += 1
        if self.taken == self._at:
            self._call()
        self._lock.acquire()

    def __exit__(self, *_: object) -> None:
        self._lock.release()


def test_a_release_never_drops_the_blob_of_a_firing_another_thread_has_just_held() -> None:
    world = World()
    account = world.account()
    latch, storage = account._shared.latch, account._shared.storage
    world.store.refuse(("create", KEY))
    with pytest.raises(Halt):
        storage.hold_create(KEY, b"a firing's blob, held through an outage")
    # The settle's third time under the lock is the release's, after its look at what it holds
    # and its filter, and another thread's firing is held just before it.
    latch._lock = _Hooked(3, lambda: latch._pending.append(fired("d" * 32)))  # type: ignore[assignment]
    latch._complete(world.report)
    assert [ref.key for ref in storage.unstored()] == [KEY]


def test_a_release_while_retry_held_places_the_blob_lets_that_place_finish() -> None:
    world = World()
    storage = world.storage()
    world.store.refuse(("create", KEY))
    with pytest.raises(Halt):
        storage.hold_create(KEY, b"one")
    answered(world)

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", KEY):
            storage.release(KEY)

    world.store.before = before
    assert [made.ref.key for made in storage.retry_held()] == [KEY] and storage.unstored() == ()


def test_a_firing_counted_before_the_checks_and_journaled_during_their_view_stops_the_call() -> (
    None
):
    """Its entry lands after the view's listing, and it leaves memory before the view ends."""
    world = World()
    account = world.account()
    latch = account._shared.latch
    sending = journal_entry_key(ACCOUNT, len(world.every_kind()) + 3)
    entry = fired("e" * 32)
    armed: list[bool] = []

    def after(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", sending):
            with account._shared.lock:
                latch._pending.append(entry)
                latch._count += 1
            armed.append(True)
        elif armed and op == "list" and key.startswith("journal/"):
            armed.clear()
            world.record(entry)
            latch._pending.clear()

    world.store.after = after
    halted(world, "loss stop stands", lambda: world.send(account=account))
    assert world.state() is OrderState.ABANDONED


@pytest.mark.parametrize("unread", [False, True])
def test_the_page_goes_out_before_the_firings_writes(unread: bool) -> None:
    world = World() if unread else losing()
    if unread:
        world.store.drop(next(k for k in world.store.blobs if "valuation" in k))
    paged: list[int] = []
    first = journal_entry_key(ACCOUNT, len(world.every_kind()) + 1)

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", first):
            paged.append(len(world.alarms.sent))

    world.store.before = before
    halted(
        world,
        "no reading" if unread else "past its stop",
        lambda: (
            world.account().loss(
                (DEPLOYMENT, RUN, gates.DECISION),
                world.strategies(),
                world.report,
                after="a halt",
                unread=True,
            )
            if unread
            else world.path().loss(DEPLOYMENT, RUN, gates.DECISION, world.strategies())
        ),
    )
    assert paged == [1]


def test_an_orders_check_past_its_stop_fires_with_its_own_reason() -> None:
    world = losing()
    halted(world, "past its stop")
    entry = standing(world).standing.entry
    assert entry.reason == "an order's check read the day's loss past its stop"


def test_an_orphan_whose_number_another_writer_took_is_tried_again() -> None:
    world = World()
    world.store.put(KEY, b"{}")
    fired_at = journal_entry_key(ACCOUNT, len(world.every_kind()) + 1)
    taken: list[int] = []

    def before(number: int, op: str, key: str) -> None:
        if (op, key) == ("create", fired_at) and not taken:
            taken.append(number)
            world.record(ReadEntry("execution/send", AT, BlobRef("r", "b" * 64), AT))

    world.store.before = before
    world.account()._shared.latch.complete(world.report)
    assert taken and stop_kinds(world) == ["stop_fired", "stop_stored"]


def test_a_halt_on_a_reading_past_its_stop_says_which_of_its_writes_failed() -> None:
    world = losing()
    world.store.refuse(("create", entry_prefix()), ("create", KEY))
    reason = halted(
        world,
        "past its stop",
        lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION, world.strategies()),
    )
    assert "not wholly stored: its entry was not stored" in reason
    assert f"{KEY} was not stored" in reason


# What round 3 of the review asked for


def test_a_release_holds_the_lock_so_a_firing_on_another_thread_keeps_its_blob() -> None:
    world = World()
    account = world.account()
    latch, storage = account._shared.latch, account._shared.storage
    world.store.refuse(("create", KEY))
    real, threads = storage.release, []

    def release(key: str) -> None:
        # Another thread fires just as the release begins. It waits for the lock to be let go.
        thread = threading.Thread(target=fire_in, args=(world, account))
        thread.start()
        thread.join(0.2)
        threads.append(thread)
        real(key)

    storage.release = release  # type: ignore[method-assign]
    latch._complete(world.report)
    storage.release = real  # type: ignore[method-assign]
    for thread in threads:
        thread.join(5)
    assert threads and [ref.key for ref in storage.unstored()] == [KEY]
