"""A position's initial risk, rebuilt from the journal alone (#216).

docs/design.md's rule 1 of `### A result is measured against the risk it took,
and size answers to what the owner can bear` gives the rule. Each journal here
is written entry by entry through `Journal.record`, so the fold checked every
entry. Each verdict carries a worst loss worked out by hand in the gate's own
way: for each leg, the share count furthest from zero that the leg could reach
with the order and every working order counted, times its mark, times the stress
move of 0.2. Two tests say where they choose a figure the gate would not give,
to tell one order from another. No figure is pinned from output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    Halt,
    InstrumentId,
    OrderIntent,
    OrderState,
    OrderType,
    TimeInForce,
    encode,
    sha256_of,
)
from ithildinruntime.journal import (
    IntentEntry,
    Journal,
    Record,
    SendingEntry,
    VerdictEntry,
    initial_risk,
)
from ithildinruntime.journal.initial_risk import WORKING, Counted, Position, positions
from ithildinruntime.journal.orders import ENDED, NOT_SENT
from tests.support.journals import (
    ACCOUNT,
    AT,
    CHAN,
    GLD,
    OTHER,
    USO,
    WRITER,
    Memory,
    abandoned,
    ack,
    decision,
    disputed,
    expired,
    fill,
    filled,
    in_doubt,
    lost,
    opening,
    planned,
    read,
    rejected,
    unmatched_fill,
    verdict,
)

D = Decimal
# Each decision a minute after the last, so every one falls before the session's close at 20:00.
MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class Leg:
    """One order of a decision: its instrument, its signed shares and its verdict's worst loss."""

    instrument: InstrumentId
    qty: int
    worst_loss: str
    adding: bool = True
    approved: bool = True
    closing: bool = False


class Written:
    """A journal in memory, written as a writer writes it, one decision at a time."""

    def __init__(self) -> None:
        self.journal = Journal(Memory(), ACCOUNT, WRITER)
        self.decisions = 0
        self.put(opening(1_000_000))

    def put(self, record: Record) -> int:
        view = self.journal.view()
        if isinstance(record, VerdictEntry):
            record = replace(record, book=view.through.seq)
        ack_, _ = self.journal.record(view, record, AT)
        return ack_.seq

    def decide(self, *legs: Leg, strategy: str = CHAN) -> list[OrderIntent]:
        """A read, a decision planning `legs` in order, and each one's intent and verdict."""
        self.decisions += 1
        at = AT + self.decisions * MINUTE
        reading = self.put(read(at))
        orders = [
            closing(planned(place, leg.qty, leg.instrument, strategy, at), leg.closing)
            for place, leg in enumerate(legs, start=1)
        ]
        decided = self.put(decision(*orders, at=at, read=reading))
        self.reading = reading
        for order, leg in zip(orders, legs, strict=True):
            self.put(IntentEntry(decided, order))
            self.put(passed(order.client_order_id, leg.worst_loss, leg.adding, leg.approved))
        return orders

    def send(self, *orders: OrderIntent) -> list[int]:
        """Each order's `sending` and its ack, and the number of each `sending`."""
        sent = []
        for order in orders:
            sent.append(self.put(SendingEntry(order.client_order_id)))
            self.put(ack(order.client_order_id, broker(order)))
        return sent

    def fill(self, order: OrderIntent, qty: int, price: str) -> int:
        made = fill(f"{broker(order)}-{qty}", broker(order), qty, price, order.instrument)
        return self.put(filled(made, self.reading))


def closing(order: OrderIntent, auction: bool) -> OrderIntent:
    """The order as a closing-auction order where `auction`, the one kind a list can give up."""
    if not auction:
        return order
    return replace(order, type=OrderType.MARKET_ON_CLOSE, time_in_force=TimeInForce.CLS)


def broker(order: OrderIntent) -> str:
    """The broker's id for an order, from its client order id."""
    return f"b-{sha256_of(order.client_order_id.encode())[:24]}"


def passed(name: str, worst_loss: str, adding: bool, approved: bool) -> VerdictEntry:
    """The gate's answer on `name`, whose worst loss is `worst_loss`."""
    made = verdict(name, approved)
    return replace(made, verdict=replace(made.verdict, adding=adding, worst_loss=D(worst_loss)))


def figures(position: Position) -> list[Decimal]:
    return [one.worst_loss for one in position.counted]


# The base walk, long GLD and short USO. Marks are GLD 200 and USO 70 at the open, GLD 190
# and USO 72 at the add, GLD 195 and USO 71 at the add never filled, GLD 205 and USO 68 at the
# close.
LONG = (
    # The open: buy 10 GLD, sell 30 USO.
    (Leg(GLD, 10, "400"), Leg(USO, -30, "820")),  # 10*200*0.2; 400 + 30*70*0.2
    # The add: buy 5 GLD, sell 15 USO.
    (Leg(GLD, 5, "1002"), Leg(USO, -15, "1218")),  # 15*190*0.2 + 30*72*0.2; 570 + 45*72*0.2
    # The add sent and never filled: buy 5 GLD, sell 15 USO.
    (Leg(GLD, 5, "1419"), Leg(USO, -15, "1632")),  # 20*195*0.2 + 45*71*0.2; 780 + 60*71*0.2
    # The close, which reduces: sell 15 GLD, buy 45 USO.
    (Leg(GLD, -15, "1227", False), Leg(USO, 45, "1227", False)),  # 15*205*0.2 + 45*68*0.2
)
# The other direction, short GLD and long USO, at the same marks.
SHORT = (
    (Leg(GLD, -8, "320"), Leg(USO, 25, "670")),  # 8*200*0.2; 320 + 25*70*0.2
    (Leg(GLD, -4, "816"), Leg(USO, 10, "960")),  # 12*190*0.2 + 25*72*0.2; 456 + 35*72*0.2
    (Leg(GLD, -4, "1121"), Leg(USO, 10, "1263")),  # 16*195*0.2 + 35*71*0.2; 624 + 45*71*0.2
    (Leg(GLD, 12, "968", False), Leg(USO, -35, "968", False)),  # 12*205*0.2 + 35*68*0.2
)
PRICES = (("200", "70"), ("190", "72"), ("195", "71"), ("205", "68"))


def walked(walk: tuple[tuple[Leg, Leg], ...]) -> tuple[Written, dict[str, int]]:
    """The base walk: open, add, an add sent and never filled, and a close one leg at a time."""
    written, seen = Written(), {}
    (gld, uso) = written.decide(*walk[0])
    seen["opened"], _ = written.send(gld, uso)
    written.fill(gld, gld.qty, PRICES[0][0])
    written.fill(uso, uso.qty, PRICES[0][1])
    adds = written.decide(*walk[1])
    written.send(*adds)
    for order, price in zip(adds, PRICES[1], strict=True):
        written.fill(order, order.qty, price)
    never = written.decide(*walk[2])
    written.send(*never)
    for order in never:
        written.put(expired(order.client_order_id, 0))
    gld, uso = written.decide(*walk[3])
    written.send(gld, uso)
    seen["one leg flat"] = written.fill(gld, gld.qty, PRICES[3][0])
    half = uso.qty // 2
    written.fill(uso, half, PRICES[3][1])
    seen["closed"] = written.fill(uso, uso.qty - half, PRICES[3][1])
    return written, seen


@pytest.mark.parametrize(
    ("walk", "risk"), [(LONG, D("1632")), (SHORT, D("1263"))], ids=["long-short", "short-long"]
)
def test_the_base_walk_in_each_direction(walk: tuple[tuple[Leg, Leg], ...], risk: Decimal) -> None:
    written, seen = walked(walk)
    (position,) = positions(written.journal)
    assert (position.account, position.strategy) == (ACCOUNT, CHAN)
    # It opens at the first sending and closes at the second leg's last fill, not the first's.
    assert (position.opened, position.closed) == (seen["opened"], seen["closed"])
    assert seen["one leg flat"] < seen["closed"]
    # All eight orders count, the two never filled among them.
    assert figures(position) == [D(leg.worst_loss) for pair in walk for leg in pair]
    # The add never filled holds the largest figure, so neither the last figure nor the
    # largest of the filled orders is the answer.
    assert position.initial_risk == risk
    assert risk not in (figures(position)[-1], max(figures(position)[:4]))


def test_flat_again_opens_a_second_position_which_stays_open() -> None:
    written, seen = walked(LONG)
    gld, uso = written.decide(Leg(GLD, 3, "120"), Leg(USO, -9, "246"))  # 3*200*0.2; 120+9*70*.2
    opened, _ = written.send(gld, uso)
    written.fill(gld, 3, "200")
    first, second = positions(written.journal)
    assert first.closed == seen["closed"]
    assert (second.opened, second.closed) == (opened, None)
    assert [one.client_order_id for one in second.counted] == [
        gld.client_order_id,
        uso.client_order_id,
    ]
    assert second.initial_risk == D("246")


def test_an_order_in_doubt_keeps_the_position_open() -> None:
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    written.fill(gld, 10, "200")
    (more,) = written.decide(Leg(GLD, 5, "600"))
    written.put(SendingEntry(more.client_order_id))
    written.put(in_doubt(more.client_order_id))
    (sell,) = written.decide(Leg(GLD, -10, "600", False))
    written.send(sell)
    written.fill(sell, -10, "200")
    (position,) = positions(written.journal)
    assert position.closed is None
    assert position.initial_risk == D("600")


def test_each_order_with_a_sending_counts_and_none_without() -> None:
    """A rejected order counts, as #216 settles, and so does one abandoned after its sending.

    A refused order and one abandoned before its sending were never sent, so they do not,
    though their figures are the largest.
    """
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    written.fill(gld, 10, "200")
    # 15*200*0.2, 20*200*0.2 and 25*200*0.2, each counting the adds planned before it.
    (early, late, lost) = written.decide(
        Leg(GLD, 5, "600"), Leg(GLD, 5, "800"), Leg(GLD, 5, "1000")
    )
    written.put(SendingEntry(early.client_order_id))
    written.put(rejected(early.client_order_id))
    written.send(late)
    written.put(rejected(late.client_order_id))
    written.put(SendingEntry(lost.client_order_id))
    written.put(abandoned(lost.client_order_id))
    # Figures chosen above every other, so either one counted would show.
    (refused, held) = written.decide(Leg(GLD, 5, "5000", approved=False), Leg(GLD, 5, "6000"))
    written.put(abandoned(held.client_order_id))
    (position,) = positions(written.journal)
    names = [one.client_order_id for one in position.counted]
    assert names == [o.client_order_id for o in (gld, early, late, lost)]
    assert refused.client_order_id not in names
    assert position.initial_risk == D("1000")


def test_a_figure_at_zero_leaves_the_initial_risk_missing() -> None:
    """Only an approval that reduces can carry one, since `core.Verdict` refuses the rest."""
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    written.fill(gld, 10, "200")
    (sell,) = written.decide(Leg(GLD, -4, "0", False))
    written.send(sell)
    (position,) = positions(written.journal)
    assert figures(position) == [D(400), D(0)]
    assert position.initial_risk is None


@pytest.mark.parametrize(
    ("figure", "adding"),
    [
        ("0", True),
        ("-0.01", True),
        (None, True),
        ("NaN", True),
        ("Infinity", True),
        (None, False),
        ("NaN", False),
        ("Infinity", False),
    ],
)
def test_an_approval_with_a_figure_core_refuses_halts_the_read(
    figure: str | None, adding: bool
) -> None:
    """A hand-written entry of a kind no writer can build halts, and never reads as smaller.

    `core.Verdict` refuses an approval with no finite worst loss, and an adding one at or
    below zero. The decoder refuses a NaN or an Infinity before `core.Verdict` sees it, and
    either refusal halts the fold.
    """
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    written.fill(gld, 10, "200")
    # 15*200*0.2 for the add, 10*200*0.2 for the sale, then replaced below.
    (more,) = written.decide(Leg(GLD, 5 if adding else -5, "600" if adding else "400", adding))
    journal = written.journal
    # The add's verdict is the newest entry. A second journal holds the same entries, with that
    # verdict's worst loss written by hand as `figure`, which `core.Verdict` refuses to build.
    store = Memory()
    copy = Journal(store, ACCOUNT, WRITER)
    for entry in journal.read():
        body = json.loads(encode(entry))["body"]
        if body["kind"] == "verdict" and body["client_order_id"] == more.client_order_id:
            body["verdict"]["worst_loss"] = figure
        copy._append(copy._build(body, entry.built_at))
    with pytest.raises(Halt, match=r"journal paper-1: entry \d+ is not of its kind verdict"):
        positions(copy)


def test_strategies_keep_positions_of_their_own() -> None:
    """Each strategy's position is its own, and the read lists them in the order they opened."""
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    (uso,) = written.decide(Leg(USO, -30, "420"), strategy=OTHER)
    written.send(gld)
    written.send(uso)
    written.fill(gld, 10, "200")
    written.fill(uso, -30, "70")
    (buy,) = written.decide(Leg(USO, 30, "420", False), strategy=OTHER)
    written.send(buy)
    closed = written.fill(buy, 30, "70")
    mine, other = positions(written.journal)
    assert (mine.strategy, mine.closed, mine.initial_risk) == (CHAN, None, D(400))
    assert (other.strategy, other.closed, other.initial_risk) == (OTHER, closed, D(420))


def test_the_read_holds_each_counted_order_with_its_sending() -> None:
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    (sent,) = written.send(gld)
    (position,) = positions(written.journal)
    assert position.counted == (Counted(gld.client_order_id, sent, D(400)),)
    assert (position.opened, position.closed) == (sent, None)


def test_an_order_approved_and_not_sent_keeps_no_position_open() -> None:
    """It has no `sending`, so nothing of it can have left, and the close still closes."""
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    written.fill(gld, 10, "200")
    sell, later = written.decide(Leg(GLD, -10, "400", False), Leg(GLD, 5, "600"))
    written.send(sell)
    closed = written.fill(sell, -10, "200")
    written.put(abandoned(later.client_order_id))
    (position,) = positions(written.journal)
    assert position.closed == closed
    assert later.client_order_id not in [one.client_order_id for one in position.counted]


def test_a_figure_one_cent_above_zero_counts() -> None:
    held = (Counted("a", 4, D("0.01")), Counted("b", 9, D("0.001")))
    assert Position(ACCOUNT, CHAN, 4, None, held).initial_risk == D("0.01")


@pytest.mark.parametrize("figure", ["Infinity", "-Infinity", "NaN", "-1"])
def test_a_figure_that_is_not_finite_and_positive_reads_as_missing(figure: str) -> None:
    """No verdict a journal can hold carries the first three, and the reading holds them all."""
    held = (Counted("a", 4, D(400)), Counted("b", 9, D(figure)))
    assert Position(ACCOUNT, CHAN, 4, None, held).initial_risk is None


def test_another_strategys_working_order_keeps_no_position_of_this_one_open() -> None:
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    written.fill(gld, 10, "200")
    (uso,) = written.decide(Leg(USO, -30, "420"), strategy=OTHER)
    written.send(uso)
    (sell,) = written.decide(Leg(GLD, -10, "400", False))
    written.send(sell)
    closed = written.fill(sell, -10, "200")
    mine, other = positions(written.journal)
    assert (mine.strategy, mine.closed) == (CHAN, closed)
    assert (other.strategy, other.closed) == (OTHER, None)


def test_an_empty_journal_halts_the_read() -> None:
    """It reads the same as a wrong account or a lost store, so it never reads as no position."""
    with pytest.raises(Halt, match="it holds no entry, so there is no book"):
        positions(Journal(Memory(), ACCOUNT, WRITER))


def test_a_fill_in_dispute_halts_the_read() -> None:
    """The record holds two contents for one fill, so it does not say where the position stands."""
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    made = fill(f"{broker(gld)}-10", broker(gld), 10, "200", GLD)
    written.put(filled(made, written.reading))
    written.put(disputed(replace(made, qty=D(4)), written.reading))
    with pytest.raises(Halt, match="is in dispute"):
        positions(written.journal)


def test_a_fill_kept_as_unmatched_halts_the_read() -> None:
    """The broker reports a fill no order holds, so the strategy may not be where the book says."""
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    written.fill(gld, 10, "200")
    written.put(unmatched_fill(fill("x-1", "x-1", -10, "200"), written.reading))
    with pytest.raises(Halt, match="keeps an unmatched fill"):
        positions(written.journal)


def test_every_state_is_read_on_purpose() -> None:
    """A state a release adds fails here until the reading of initial risk is told what it is.

    The three sets are written out, so a state added to any of them, or to none, fails.
    """
    s = OrderState
    assert NOT_SENT == {s.INTENDED, s.APPROVED}
    assert WORKING == {s.SENDING, s.IN_DOUBT, s.ACKNOWLEDGED, s.PARTLY_FILLED}
    assert ENDED == {s.REFUSED, s.ABANDONED, s.FILLED, s.REJECTED, s.EXPIRED, s.LOST}
    assert NOT_SENT | WORKING | ENDED == set(OrderState)


def test_a_live_order_in_a_state_the_read_does_not_list_halts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No state reaches this today, so the list is cut short to show the halt."""
    monkeypatch.setattr(initial_risk, "WORKING", initial_risk.WORKING - {OrderState.ACKNOWLEDGED})
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    with pytest.raises(Halt, match="is Acknowledged, which the reading of initial risk does not"):
        positions(written.journal)


@pytest.mark.parametrize("kind", ["dispute", "unmatched"])
def test_a_journal_held_after_every_position_closed_halts_the_read(kind: str) -> None:
    """No position is open to read the book, so the read's last look at it halts."""
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    made = fill(f"{broker(gld)}-10", broker(gld), 10, "200", GLD)
    written.put(filled(made, written.reading))
    (sell,) = written.decide(Leg(GLD, -10, "400", False))
    written.send(sell)
    written.fill(sell, -10, "200")
    if kind == "dispute":
        written.put(disputed(replace(made, qty=D(4)), written.reading))
    else:
        written.put(unmatched_fill(fill("x-1", "x-1", -10, "200"), written.reading))
    said = "is in dispute" if kind == "dispute" else "keeps an unmatched fill"
    with pytest.raises(Halt, match=said):
        positions(written.journal)


def test_an_order_given_up_as_lost_counts_and_then_keeps_no_position_open() -> None:
    """It was sent, so it counts, and once it ends the strategy can be flat again."""
    written = Written()
    (gld,) = written.decide(Leg(GLD, 10, "400"))
    written.send(gld)
    written.fill(gld, 10, "200")
    (more,) = written.decide(Leg(GLD, 5, "600", closing=True))
    written.put(SendingEntry(more.client_order_id))
    written.put(in_doubt(more.client_order_id))
    (sell,) = written.decide(Leg(GLD, -10, "400", False))
    written.send(sell)
    written.fill(sell, -10, "200")
    asked = written.put(read(AT + timedelta(days=1)))
    given_up = written.put(lost(more.client_order_id, asked))
    (position,) = positions(written.journal)
    assert position.closed == given_up
    assert more.client_order_id in [one.client_order_id for one in position.counted]
    assert position.initial_risk == D("600")
