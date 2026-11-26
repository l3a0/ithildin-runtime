"""The send stage's check that every quote its plan's gate will mark lasts the plan, for #206.

Every figure is made up. Each decision is at close−20, where the snapshot holds a
quote of GLD and of USO stamped 59 seconds before it, under a maximum age of 90
seconds and the clock bound of 1. So each quote can be read until 30 seconds
after the decision, and a case moves the clock or a stamp to put that moment
either side of the plan's margin. Each case reads what the journal, the
simulated broker and the alarms hold afterwards.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from ithildinruntime.core import Halt, Snapshot, StoreUnavailable, readable_until
from ithildinruntime.execution import order_path
from ithildinruntime.execution.account import Account
from ithildinruntime.execution.send import MARGIN, SendStage
from tests.support.send_world import CHAN, SendWorld
from tests.support.sim_world import FULL, GLD, USO, after, close_of

DAYS = [FULL, after(FULL), after(FULL, 2)]
CLOSES = {GLD: {day: 100 + n for n, day in enumerate(DAYS)}, USO: {day: 20 for day in DAYS}}
PAIR = {CHAN: {GLD: 3, USO: -5}}
TICK = timedelta(microseconds=1)


def early(day) -> datetime:
    return close_of(day) - timedelta(minutes=20)


def world(targets=None) -> SendWorld:
    """A world whose first decision, at the close of the first session, has sent `targets`."""
    made = SendWorld(CLOSES)
    made.ready(close_of(FULL), targets if targets is not None else {CHAN: {GLD: 0, USO: 0}})
    result = made.send_stage()
    assert result.halt is None, result.halt.reason
    return made


def older(made: SendWorld, instrument: str, by: timedelta) -> None:
    """Every later snapshot's quote of `instrument` stamped `by` earlier on both sides."""
    snapshot = made.snapshot

    def aged(at: datetime, **changes: object) -> Snapshot:
        found = snapshot(at, **changes)
        if instrument not in found.quotes:
            return found
        quote = found.quotes[instrument]
        moved = replace(quote, bid_at=quote.bid_at - by, ask_at=quote.ask_at - by)
        return replace(found, quotes={**found.quotes, instrument: moved})

    made.snapshot = aged  # type: ignore[method-assign]


def decide(made: SendWorld, at: datetime, targets, late: timedelta):
    """The send stage at `at`, with its clock `late` past the decision."""
    made.ready(at, targets)
    made.clock.at = at + late
    return made.send_attempt()


def abandoned(made: SendWorld) -> list[str]:
    """The reason of each `abandoned` entry the journal holds, oldest first."""
    return [r["body"]["reason"] for r in made.rows() if r["body"]["kind"] == "abandoned"]


def test_the_quotes_last_thirty_seconds_past_the_decision() -> None:
    made = world()
    at = early(DAYS[1])
    assert {i: readable_until(q) for i, q in made.snapshot(at).quotes.items()} == {
        GLD: at + timedelta(seconds=30),
        USO: at + timedelta(seconds=30),
    }
    # The margin is the order path's own bound on one pass of a submit's checks.
    assert MARGIN == timedelta(seconds=5) == timedelta(seconds=order_path._PASS)


@pytest.mark.parametrize(
    ("late", "halts"), [(timedelta(seconds=20), False), (timedelta(seconds=20) + TICK, True)]
)
def test_a_pair_halts_before_its_first_order_where_a_quote_would_not_last_ten_seconds(
    late: timedelta, halts: bool
) -> None:
    """At 20 seconds the pair's 10 run to the quotes' last moment exactly, which still reads."""
    made = world()
    at = early(DAYS[1])
    result = decide(made, at, PAIR, late)
    planned = [o.client_order_id for o in made.view().decisions[made.view().decided[at]].orders]
    assert len(planned) == 2
    if not halts:
        assert result.halt is None, result.halt.reason
        assert [made.states()[name] for name in planned] == ["Acknowledged", "Acknowledged"]
        assert made.submits() == planned
        return
    assert result.halt is not None
    reason = result.halt.reason
    assert reason.startswith("nothing is sent: a quote would not last its plan: ")
    assert "GLD's quote cannot be read" in reason and "USO's quote cannot be read" in reason
    assert "the margin of 10 seconds for the plan's 2 orders" in reason
    assert made.submits() == []
    assert [made.states()[name] for name in planned] == ["Abandoned", "Abandoned"]
    # Nothing was asked, so the gate saw no order of the plan and none has a verdict.
    assert not set(planned) & set(made.keeper.asked)
    assert "verdict" not in made.kinds(made.before)
    (alarm,) = [a for a in made.alarms.sent if "would not last" in a]
    assert "the margin of 10 seconds" in alarm
    for said in (
        "On shadow and paper this halts the decision alone, and the next decision trades on its "
        "own quotes",
        "On live the account is held until the owner clears it",
    ):
        assert said in reason and said in alarm
    reasons = abandoned(made)
    assert len(reasons) == 2
    assert all(r.startswith("a quote would not last its plan: ") for r in reasons)


def test_a_held_leg_the_plan_does_not_order_halts_it_too() -> None:
    """USO is held and its target unchanged, and its quote runs out first.

    The gate marks USO for the GLD order, since the account holds it, so a
    check of the ordered leg alone would send GLD while USO's quote runs out.
    """
    made = world(PAIR)
    older(made, USO, timedelta(seconds=5))
    at = early(DAYS[1])
    result = decide(made, at, {CHAN: {GLD: 5, USO: -5}}, timedelta(seconds=20) + TICK)
    snapshot = made.snapshot(at)
    assert readable_until(snapshot.quotes[GLD]) == at + timedelta(seconds=30)
    assert readable_until(snapshot.quotes[USO]) == at + timedelta(seconds=25)
    planned = [o.client_order_id for o in made.view().decisions[made.view().decided[at]].orders]
    assert [o.instrument for o in made.view().decisions[made.view().decided[at]].orders] == [GLD]
    assert result.halt is not None
    reason = result.halt.reason
    assert "USO's quote cannot be read" in reason and "GLD's quote" not in reason
    assert "the margin of 5 seconds for the plan's 1 order: USO's quote" in reason
    assert [made.states()[name] for name in planned] == ["Abandoned"]
    assert not set(planned) & set(made.keeper.asked)
    assert planned[0] not in made.submits()


@pytest.mark.parametrize(
    ("wanted", "orders"), [({CHAN: {GLD: 5, USO: -5}}, 1), ({CHAN: {GLD: 6, USO: -7}}, 2)]
)
def test_the_margin_is_five_seconds_for_each_order(wanted, orders: int) -> None:
    """Seven seconds left sends one order, and halts a pair."""
    made = world(PAIR)
    at = early(DAYS[1])
    result = decide(made, at, wanted, timedelta(seconds=23))
    planned = made.view().decisions[made.view().decided[at]].orders
    assert len(planned) == orders
    states = [made.states()[o.client_order_id] for o in planned]
    if orders == 2:
        assert result.halt is not None and "would not last" in result.halt.reason
        assert states == ["Abandoned", "Abandoned"]
    else:
        assert result.halt is None, result.halt.reason
        assert states == ["Acknowledged"]


def test_a_plan_with_no_order_sends_and_alarms_nothing() -> None:
    made = world(PAIR)
    result = decide(made, early(DAYS[1]), PAIR, timedelta(seconds=29))
    assert result.halt is None, result.halt.reason
    assert made.outcome(result).orders == ()
    assert not [a for a in made.alarms.sent if "would not last" in a]


def changed(made: SendWorld, instrument: str, **fields: object) -> None:
    """Every later snapshot's quote of `instrument` with `fields` changed."""
    snapshot = made.snapshot

    def with_fields(at: datetime, **changes: object) -> Snapshot:
        found = snapshot(at, **changes)
        if instrument not in found.quotes:
            return found
        quote = replace(found.quotes[instrument], **fields)
        return replace(found, quotes={**found.quotes, instrument: quote})

    made.snapshot = with_fields  # type: ignore[method-assign]


def test_a_quote_already_past_its_age_at_now_is_left_to_the_gate() -> None:
    """At 31 seconds both quotes fail at `now`, which is the gate's fault and not this check's."""
    made = world()
    result = decide(made, early(DAYS[1]), PAIR, timedelta(seconds=31))
    assert result.halt is not None
    reason = result.halt.reason
    assert "would not last" not in reason
    assert reason.startswith("nothing is sent: GLD's quote cannot be read")
    assert made.submits() == []
    assert not [a for a in made.alarms.sent if "would not last" in a]
    planned = [
        o.client_order_id for o in made.view().decisions[made.view().decided[made.at]].orders
    ]
    assert [made.states()[name] for name in planned] == ["Abandoned", "Abandoned"]


def test_an_ordered_quote_of_another_status_is_left_to_the_gate() -> None:
    """USO is ordered and not held, and its status fails at `now`, so the gate halts the plan."""
    made = world({CHAN: {GLD: 3, USO: 0}})
    before = list(made.submits())
    changed(made, USO, status="Halted")
    at = early(DAYS[1])
    result = decide(made, at, {CHAN: {GLD: 3, USO: -5}}, timedelta(0))
    assert result.halt is not None
    reason = result.halt.reason
    assert reason.startswith("nothing is sent: USO's quote cannot be read: its status is 'Halted'")
    assert "would not last" not in reason
    assert made.submits() == before


@pytest.mark.parametrize("fault", ["status", "age"])
def test_a_quote_that_fails_at_now_leaves_the_whole_plan_to_the_gate(fault: str) -> None:
    """GLD is held and would run out inside the margin, and USO, ordered and not held, fails now.

    The gate's halt on USO is the quote's own fault, so it is not hidden behind GLD's time.
    """
    made = world({CHAN: {GLD: 3, USO: 0}})
    before = list(made.submits())
    if fault == "status":
        changed(made, USO, status="Halted")
    else:
        older(made, USO, timedelta(seconds=22))
    at = early(DAYS[1])
    result = decide(made, at, {CHAN: {GLD: 5, USO: -5}}, timedelta(seconds=21))
    assert result.halt is not None
    reason = result.halt.reason
    assert reason.startswith("nothing is sent: ")
    assert "would not last" not in reason and "USO's quote cannot be read" in reason
    assert made.submits() == before


def test_an_instrument_neither_held_nor_ordered_is_not_checked() -> None:
    """USO passes now and runs out inside the margin, and the plan neither holds nor orders it."""
    made = world({CHAN: {GLD: 3, USO: 0}})
    older(made, USO, timedelta(seconds=27))
    at = early(DAYS[1])
    result = decide(made, at, {CHAN: {GLD: 5, USO: 0}}, timedelta(0))
    assert at <= readable_until(made.snapshot(at).quotes[USO]) < at + MARGIN
    assert result.halt is None, result.halt.reason
    planned = made.view().decisions[made.view().decided[at]].orders
    assert [(o.instrument, made.states()[o.client_order_id]) for o in planned] == [
        (GLD, "Acknowledged")
    ]


def test_an_abandon_the_store_refuses_is_named_in_the_halt_and_the_alarm() -> None:
    made = world()
    at = early(DAYS[1])
    made.ready(at, PAIR)
    made.clock.at = at + timedelta(seconds=20) + TICK

    def before(number: int, op: str, key: str) -> None:
        if op == "create" and key.startswith("journal/") and made.kinds()[-1:] == ["abandoned"]:
            raise StoreUnavailable(key, "the store's own text")

    made.store.before = before
    result = made.send_attempt()
    first, second = [
        o.client_order_id for o in made.view().decisions[made.view().decided[at]].orders
    ]
    assert made.states().get(first) == "Abandoned" and second not in made.states()
    assert result.halt is not None
    assert f"the abandoned of {second} was not stored" in result.halt.reason
    (alarm,) = [a for a in made.alarms.sent if a.startswith("account") and "would not last" in a]
    assert f"the abandoned of {second} was not stored" in alarm
    # An order has no stored end, so the halt promises nothing of the next decision.
    assert "next decision trades" not in result.halt.reason
    assert made.submits() == []


@pytest.mark.parametrize(("refused", "unfinished"), [("the second", True), ("each", False)])
def test_the_next_decision_after_a_refused_abandon_reads_what_the_journal_holds(
    refused: str, unfinished: bool
) -> None:
    """A plan with one order ended and one with no entry is unfinished, and one with none is not.

    The next recover settles an unfinished plan, journaling the order with no entry
    `abandoned`, as #147 builds, and sends nothing of it.
    """
    made = world()
    at = early(DAYS[1])
    made.ready(at, PAIR)
    made.clock.at = at + timedelta(seconds=20) + TICK

    def before(number: int, op: str, key: str) -> None:
        if op != "create" or not key.startswith("journal/"):
            return
        kinds = made.kinds()
        if (refused == "each" and "decision" in kinds[-1:]) or kinds[-1:] == ["abandoned"]:
            raise StoreUnavailable(key, "the store's own text")

    made.store.before = before
    result = made.send_attempt()
    assert result.halt is not None and "next decision trades" not in result.halt.reason
    if refused == "each":
        # The first refusal is an outage, which stops the rest: the second is not tried.
        first, second = [
            o.client_order_id for o in made.view().decisions[made.view().decided[at]].orders
        ]
        assert first in result.halt.reason and second not in result.halt.reason
        assert first not in made.states() and second not in made.states()
    made.store.before = lambda number, op, key: None
    sent = list(made.submits())
    later = early(DAYS[2])
    made.ready(later, PAIR, recover=False)
    recovered = made.recover()
    assert recovered.halt is None, recovered.halt.reason
    if unfinished:
        orders = made.view().decisions[made.view().decided[at]].orders
        assert made.states()[orders[1].client_order_id] == "Abandoned"
        assert made.submits() == sent
    # The next decision trades: its own pair goes out.
    made.ready(later, PAIR)
    nxt = made.send_attempt()
    assert nxt.halt is None, nxt.halt.reason
    assert len(made.submits()) == len(sent) + 2


@pytest.mark.parametrize("cause", ["fold", "store"])
def test_a_quote_halt_whose_journal_cannot_be_read_abandons_nothing_and_promises_nothing(
    monkeypatch, cause: str
) -> None:
    """A journal the quote check cannot read takes no abandon, and its cause is paged.

    The test makes the read fail once the stage starts ending the plan's orders: as a
    fold that refuses an entry, which pages once, or as a store that stops answering,
    whose outage pages before the page that nothing is sent. The halt says nothing of
    the next decision, because a journal the stage cannot read is one the next
    decision cannot read either.
    """
    made = world()
    at = early(DAYS[1])
    made.ready(at, PAIR)
    made.clock.at = at + timedelta(seconds=20) + TICK
    real = SendStage._none_sent
    said = "entry 9 is refused" if cause == "fold" else "the journal could not be read"

    def none_sent(self, *args, **kwargs):
        if cause == "fold":
            monkeypatch.setattr(Account, "view", lambda *a: (_ for _ in ()).throw(Halt(said)))
        else:
            made.store.refuse(("*", "journal/"))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(SendStage, "_none_sent", none_sent)
    alarms = len(made.alarms.sent)
    result = made.send_attempt()
    monkeypatch.undo()
    assert result.halt is not None and said in result.halt.reason
    assert "would not last" in result.halt.reason
    assert "next decision trades" not in result.halt.reason
    pages = [a for a in made.alarms.sent[alarms:] if a.startswith("account")]
    assert len(pages) == (1 if cause == "fold" else 2)
    assert said in pages[0] and "nothing is sent" in pages[-1] and said in pages[-1]
    assert "abandoned" not in made.kinds()
    assert made.submits() == []
