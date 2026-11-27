"""Each strategy's positions, and the initial risk of each, from one account's journal alone.

docs/design.md's `### A result is measured against the risk it took, and size
answers to what the owner can bear` gives the rule, in its rule 1, and
docs/architecture/05-building-blocks.md's `##### A position's initial risk
comes from the journal alone` says how this module reads it. In short:

- The entries are folded with the journal's own fold, and a position's legs
  are read from the book each view gives, as the gate reads them. So an entry
  the fold refuses halts this read with the fold's reason, and so does what
  halts the book: a journal with no entry, a fill in dispute and anything kept
  as unmatched. A live order in a state outside `NOT_SENT` and `WORKING` halts
  it too.
- A strategy is flat when it holds no share and has no order working or in
  doubt. A position opens at the `sending` entry of an order of a flat
  strategy, and closes at the first entry after which the strategy is flat.
- Every order whose `sending` falls inside a position counts, with the worst
  loss its verdict journaled, whatever came after its sending. Initial risk is
  the largest of those figures, and missing where any one is not finite and
  positive.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple

from ithildinruntime.core import Halt

from .book import View, after
from .chain import Journal
from .entries import SendingEntry, VerdictEntry, record_of
from .orders import NOT_SENT, OPEN

__all__ = ["WORKING", "Counted", "Position", "positions"]

# An order in one of these is with the broker, or may be, so it keeps its strategy off flat.
# An order in `NOT_SENT` has no `sending`, so nothing of it can have left.
WORKING = OPEN - NOT_SENT


class Counted(NamedTuple):
    """One order a position counts: its client order id, its `sending` entry's number and its
    verdict's worst loss."""

    client_order_id: str
    sent: int
    worst_loss: Decimal


@dataclass(frozen=True)
class Position:
    """One strategy's position in one account, from flat to flat.

    - `opened` is the number of the `sending` entry that took the strategy off
      flat, and `closed` that of the entry after which it was flat again, or
      `None` while it is still open.
    - `counted` holds each order sent inside it, in the order they were sent.
    """

    account: str
    strategy: str
    opened: int
    closed: int | None
    counted: tuple[Counted, ...]

    @property
    def initial_risk(self) -> Decimal | None:
        """The largest worst loss counted, or `None` where any one is not finite and positive."""
        figures = [one.worst_loss for one in self.counted]
        if any(not figure.is_finite() or figure <= 0 for figure in figures):
            return None
        return max(figures)


def positions(journal: Journal) -> tuple[Position, ...]:
    """Every position the journal holds, in the order they opened, or a halt.

    It reads the journal's entries, checked as `Journal.read` checks them, and
    nothing else. It halts where the fold halts and where the book of the
    newest view halts, which an empty journal, a fill in dispute and anything
    kept as unmatched do, since a position read past them could close where
    the record does not say it did. It halts on a live order in a state
    outside `NOT_SENT` and `WORKING` too.
    """
    # The reading through no entry, which every reading of this journal folds from.
    view = journal.view(through=0)
    figures: dict[str, Decimal] = {}
    held: dict[str, Position] = {}
    found: list[Position] = []
    for entry in journal.read():
        view = after(view, entry).keep(entry.hash())
        record = record_of(entry)
        if isinstance(record, VerdictEntry) and record.verdict.approved:
            # An approval always names a strategy, so it always carries a finite worst loss.
            assert record.verdict.worst_loss is not None
            figures[record.client_order_id] = record.verdict.worst_loss
        elif isinstance(record, SendingEntry):
            name = record.client_order_id
            strategy = view.orders[name].intent.strategy
            one = Counted(name, entry.seq, figures[name])
            was = held.get(strategy)
            if was is None:
                held[strategy] = Position(view.account, strategy, entry.seq, None, (one,))
            else:
                held[strategy] = Position(
                    was.account, strategy, was.opened, None, (*was.counted, one)
                )
        for strategy, position in list(held.items()):
            if _flat(view, strategy, entry.seq):
                del held[strategy]
                found.append(
                    Position(
                        position.account, strategy, position.opened, entry.seq, position.counted
                    )
                )
    # No entry releases a dispute or an unmatched entry yet, so the newest view's book halts on
    # each one the read passed, and on a journal that holds no entry. The entries #203 and #150
    # build to release them must be read here before either ships.
    view.book  # noqa: B018
    found.extend(held.values())
    return tuple(sorted(found, key=lambda position: position.opened))


def _flat(view: View, strategy: str, seq: int) -> bool:
    """Whether a strategy holds no share and has no order working or in doubt, after entry `seq`."""
    shares = view.book.positions_by_strategy.get(strategy, {})
    if any(qty != 0 for qty in shares.values()):
        return False
    for order in view.live.values():
        if order.intent.strategy != strategy or order.state in NOT_SENT:
            continue
        if order.state not in WORKING:
            raise Halt(
                f"journal {view.account}: after entry {seq} the order "
                f"{order.intent.client_order_id} is {order.state.value}, which the reading of "
                "initial risk does not read"
            )
        return False
    return True
