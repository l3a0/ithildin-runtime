"""Seeded journals, replayed by `journal` and by a second implementation, and read by the gate."""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from fractions import Fraction

import pytest

from ithildinruntime.core import (
    Book,
    Constraints,
    Halt,
    HardCaps,
    Snapshot,
    StopState,
    Valuation,
    decode,
    encode,
)
from ithildinruntime.journal import Journal, View
from ithildinruntime.risk import Gate
from tests.support import gates
from tests.support.book_oracle import flattened, oracle, row
from tests.support.gate_oracle import answer
from tests.support.journals import (
    ACCOUNT,
    CAPS,
    LIMITS,
    MARKS,
    NAMES,
    SNAPSHOT,
    VALUATION,
    Checked,
    Seeded,
    shown,
)

SEEDS = 200

ENDS = ("Refused", "Abandoned", "Filled", "Rejected", "Expired")
MOVES = (
    "intent from none",
    "approved from Intended",
    "refused from Intended",
    "abandoned from Intended",
    "abandoned from Approved",
    "abandoned from Sending",
    "sending from Approved",
    "ack from Sending",
    "ack from InDoubt",
    "rejected from Sending",
    "rejected from Acknowledged",
    "in_doubt from Sending",
    "fill to PartlyFilled from Acknowledged",
    "fill to PartlyFilled from PartlyFilled",
    "fill to Filled from Acknowledged",
    "fill to Filled from PartlyFilled",
    "expired from Acknowledged",
    "expired from PartlyFilled",
    "a repeated fill of PartlyFilled",
    "a repeated fill of Filled",
)


def _effect(old: Fraction, new: Fraction) -> str:
    if not old:
        return "opening"
    if not new:
        return "closing"
    if (old > 0) != (new > 0):
        return "crossing"
    return "adding" if abs(new) > abs(old) else "reducing"


def _move(was: dict, now: dict, body: dict) -> str | None:
    """The move one entry made, as the oracle's answers before and after it say."""
    kind = body["kind"]
    for name, order in now["orders"].items():
        before = was.get("orders", {}).get(name)
        if before != order:
            start, end = before["state"] if before else "none", order["state"]
            if kind == "verdict":
                kind = "approved" if body["verdict"]["approved"] else "refused"
            return f"fill to {end} from {start}" if kind == "fill" else f"{kind} from {start}"
        if kind == "fill" and before["broker"] == body["fill"]["broker_order_id"]:
            return f"a repeated fill of {order['state']}"
    return None


def _world(view: View) -> gates.World:
    return gates.World(
        intent=gates.order(1, NAMES[0]),
        book=view.book,
        snapshot=SNAPSHOT,
        constraints=gates.constraints(**LIMITS),
        inputs=gates.inputs(caps=CAPS, valuation=VALUATION),
    )


def _the_gate_reads(view: View) -> None:
    """The gate checks an order and reads the loss on the book, with no halt."""
    world = _world(view)
    world.check()
    worth = Fraction(view.book.cash) + sum(
        Fraction(q) * Fraction(MARKS[i])
        for held in view.book.positions_by_strategy.values()
        for i, q in held.items()
    )
    assert abs(Fraction(world.loss().loss) - (50_000 - worth)) <= Fraction(1, 20_000)


def _checked_again(book: Book, checked: Checked) -> None:
    """Both gates give a verdict again from its entry and the book its `book` names."""
    entry = checked.entry
    assert book.replayed_through == entry.book
    snapshot, constraints, valuation = (
        decode(checked.blobs[ref.sha256], kind)
        for ref, kind in (
            (entry.snapshot, Snapshot),
            (entry.constraints, Constraints),
            (entry.valuation, Valuation),
        )
    )
    inputs = gates.inputs(
        caps=decode(entry.caps, HardCaps), valuation=valuation, stop=StopState(entry.stop)
    )
    records = gates.fitting(constraints, inputs.caps)
    again = Gate(records).check(checked.intent, book, snapshot, constraints, inputs, entry.now)
    assert again == entry.verdict
    given = answer(
        checked.intent,
        book.positions_by_strategy,
        [o for orders in book.open_orders_by_strategy.values() for o in orders],
        book.cash,
        book.in_doubt,
        snapshot,
        constraints,
        inputs,
        entry.now,
        gates.CLOSE,
        records,
    )
    said = entry.verdict
    assert given.stress_move == Fraction(said.stress_move)
    assert (given.approved, given.limit, given.adding) == (
        said.approved,
        said.limit and said.limit.value,
        said.adding,
    )
    assert (given.worst_loss, given.loss) == (Fraction(said.worst_loss), Fraction(said.loss.loss))
    assert (given.value, given.bound) == tuple(
        None if figure is None else Fraction(figure) for figure in (said.value, said.bound)
    )


def test_5_and_6_both_implementations_agree_and_the_gate_reads_each_book() -> None:
    seen: Counter[str] = Counter()
    for seed in range(SEEDS):
        made = Seeded(seed)
        # It reads the stored bytes alone, where `made.journal` folds its own entries.
        reader = Journal(made.store, ACCOUNT)
        rows: list[dict] = []
        was: dict = {}
        books: dict[int, Book] = {}
        for _, view in made.walk():
            key = made.store.list(f"journal/{ACCOUNT}/", None)[-1]
            rows.append(row(made.store.blobs[key]))
            now = oracle(rows)
            read = reader.view()
            assert flattened(read) == now, (seed, len(rows))
            assert shown(read) == shown(view), (seed, len(rows))
            _the_gate_reads(read)
            books[len(rows)] = read.book
            seen.update(_marks(was, now, rows[-1]["body"]))
            was = now
        assert len(rows) == 60
        assert decode(encode(read.book), Book) == read.book
        assert shown(made.journal.view()) == shown(Journal(made.store, ACCOUNT).view())
        seen.update(order["state"] for order in was["orders"].values())
        for checked in made.checks:
            # `view(through=...)` folds from the first entry each time, so one seed in four
            # shows it gives the book the reader had there, and the rest start from that book.
            number = checked.entry.book
            book = reader.view(through=number).book if seed % 4 == 0 else books[number]
            assert book == books[number]
            _checked_again(book, checked)
            seen["a verdict given again"] += 1
    for what in (
        *ENDS,
        *MOVES,
        *("opening", "adding", "reducing", "closing", "crossing"),
        "two strategies in one instrument",
        "an order in doubt",
        "a short position",
        "a verdict given again",
    ):
        assert seen[what] > 10, (what, seen)


def _marks(was: dict, now: dict, body: dict) -> list[str]:
    """What one entry did, to count what the journals cover."""
    found = [move] if (move := _move(was, now, body)) else []
    held, before = now["positions"], was.get("positions", {})
    for key in {*held, *before}:
        old, new = before.get(key, Fraction(0)), held.get(key, Fraction(0))
        if old != new:
            found.append(_effect(old, new))
    instruments = [instrument for _, instrument in held]
    if len(set(instruments)) < len(instruments):
        found.append("two strategies in one instrument")
    if now["in_doubt"]:
        found.append("an order in doubt")
    if any(qty < 0 for qty in held.values()):
        found.append("a short position")
    return found


def test_6_a_book_the_gate_halts_on_is_told_apart_from_one_it_reads() -> None:
    """The check above would miss nothing: a book with an order counted twice halts the gate."""
    made = Seeded(3)
    *_, (_, view) = made.walk()
    world = _world(view)
    twice = {gates.CHAN: (gates.order(1), gates.order(1))}
    with pytest.raises(Halt, match="an open order whose id is"):
        world.at(
            book=Book(
                view.book.positions_by_strategy,
                twice,
                Decimal(1),
                view.book.replayed_through,
                (),
                (),
            )
        ).check()
