"""Opening an account: the journal's `opening` entry and the broker's own first record, together.

An empty journal must never read as an account that holds nothing, so an
account's first entry says its cash and that it is flat, and nothing but this
function writes it. A backtest's starting code calls it with the simulated
broker's own opening, which `runtime/execution_worker.py` hands it, so this
module imports no broker. On shadow and paper a person opens the account with a
command (#11).

The cash goes to both halves in one call, and the call is safe to repeat.

1. A journal whose `opening` holds this cash is opened, and the broker's half is
   called again, which finishes an opening a kill cut between its two writes.
2. A journal whose `opening` holds other cash, or whose first entry is no
   opening, is refused, and nothing is written.
3. An empty journal has the broker's half written first, which refuses other
   cash on its own, and then the `opening` entry.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from ithildinruntime.core import BlobStore, Halt
from ithildinruntime.journal import Journal, NumberTaken, OpeningEntry

__all__ = ["open_account"]


def open_account(
    store: BlobStore,
    open_simulator: Callable[[BlobStore, str, Decimal], None],
    account: str,
    cash: Decimal,
    at: datetime,
    writer: str,
) -> None:
    """Write account `account`'s opening with `cash` in the journal and at the broker, at `at`.

    `writer` is drawn for this call, as a `Journal` that records needs.
    """
    journal = Journal(store, account, writer)
    if journal.head() is not None:
        _same(journal, account, cash)
        open_simulator(store, account, cash)
        return
    open_simulator(store, account, cash)
    try:
        journal.record(journal.view(), OpeningEntry(account, cash), at)
    except NumberTaken:
        # Another call opened it between the view and the append.
        _same(journal, account, cash)


def _same(journal: Journal, account: str, cash: Decimal) -> None:
    """Halt unless the journal's entry 1 opens the account flat with `cash`."""
    first = journal.view(through=1)
    book = first.book
    if first.opened_at is None or book.cash != cash or book.positions_by_strategy:
        raise Halt(
            f"the journal of {account} opens with another entry than {cash} of cash, so it is not "
            "opened again"
        )
