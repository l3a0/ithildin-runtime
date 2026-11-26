"""The loss stop's latch: the fold of its entries, its firing, its completion and its state.

docs/architecture/06-runtime.md's `#### What happens when the stop fires` gives
the rules, and #156 the cases. A clear is made by hand here in the three steps
#203's command will take: complete the firing, journal `stop_cleared`, and
delete the blob on its tag.
"""

from __future__ import annotations

import ast
import dataclasses
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.core import (
    BlobMissing,
    Halt,
    LossReading,
    OrderState,
    StopState,
    StoreUnavailable,
    decode,
    journal_entry_key,
    loss_stop_key,
)
from ithildinruntime.execution.latch import StopBlob
from ithildinruntime.journal import (
    IntentEntry,
    StopClearedEntry,
    StopFiredEntry,
    StopStoredEntry,
    VerdictEntry,
)
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
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "StopClearedEntry"
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
        world, "past its stop of 100", lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION)
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
    halted(world, "loss stop stands", lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION))


def test_a_second_reading_past_its_stop_before_a_clear_records_nothing_more() -> None:
    world = losing()
    for _ in range(2):
        halted(world, "past its stop", lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION))
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]


def test_a_rerun_after_the_close_fires_on_the_decisions_own_loss() -> None:
    world = losing()
    world.clock.at = gates.CLOSE + timedelta(minutes=1)
    reason = halted(world, "", lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION))
    assert "past its stop" in reason
    assert stop_kinds(world) == ["stop_fired", "stop_stored"]
    entry = standing(world).standing.entry
    assert entry.reason == "the day's loss at the decision's own time read past its stop"


def test_an_unread_loss_fires_only_where_asked() -> None:
    world = World()
    world.store.drop(next(k for k in world.store.blobs if "valuation" in k))
    halted(world, "", lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION))
    assert stop_kinds(world) == []
    halted(world, "", lambda: world.path().loss(DEPLOYMENT, RUN, gates.DECISION, unread=True))
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
    world.account().settle(world.report)
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
    fire_in(world)
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
    fire_in(world)
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
