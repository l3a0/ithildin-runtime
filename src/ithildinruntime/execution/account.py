"""One broker account, as the execution worker holds it for its whole life.

The worker builds one `Account` and keeps it across decisions, because a cold
view of a long journal is slow and because the account keeps each answer the
journal has not taken. `path` gives the `OrderPath` for one decision's broker.

Nothing here raises while it is built. A worker whose start raises is restarted
by the host and read by no person, so an account that cannot be used halts each
call with the reason instead.
"""

from __future__ import annotations

from collections.abc import Callable

from ithildinruntime.core import (
    Alerts,
    BlobStore,
    Broker,
    Clock,
    Halt,
    Retry,
    RiskGate,
    Storage,
)
from ithildinruntime.journal import Journal

from .controls import Controls
from .order_path import Named, OrderPath, Shared, Unstored, quietly
from .patience import Patience

__all__ = ["Account"]


class Account:
    """The journal of one account, what its paths share, and the answers it still owes the journal.

    `account` is the platform's alias, never the broker's number. `writer` is
    drawn afresh for this `Account`, as `Journal` says. `gate` is the one risk
    gate, and `pause` waits between two tries of a store.
    """

    def __init__(
        self,
        store: BlobStore,
        storage: Storage,
        account: str,
        writer: str,
        gate: RiskGate,
        clock: Clock,
        alerts: Alerts,
        retry: Retry,
        pause: Callable[[float], None],
    ) -> None:
        self._shared: Shared | Halt
        patience = Patience(clock, retry, pause)
        try:
            journal = Journal(store, account, writer)
        except ValueError:
            self._shared = Halt(f"the account {account!r} is no alias a journal is kept under")
        else:
            named = Named(alerts, account)
            self._shared = Shared(
                account=account,
                store=store,
                storage=storage,
                journal=journal,
                gate=gate,
                clock=clock,
                alerts=named,
                patience=patience,
                controls=Controls(store, account),
                unstored=Unstored(journal, named, patience, storage.owner_step),
            )

    def path(self, broker: Broker, report: Callable[[], None]) -> OrderPath:
        """The order path for one decision's broker, calling `report` as it works."""
        return OrderPath(self._shared, broker, report)

    def settle(self, report: Callable[[], None]) -> None:
        """Store each entry the journal has not taken, or halt naming each still owed.

        An alarm about one that could not be sent is tried again here too, and
        never halts.

        Each stage's handler calls it before its first view of the journal. An
        order in doubt halts recover, so an answer held back until the next
        send would never be written.
        """
        if isinstance(self._shared, Halt):
            raise Halt(self._shared.reason)
        self._shared.unstored.settle(quietly(report))

    def unstored(self) -> tuple[str, ...]:
        """Each entry this process holds that the journal has not taken."""
        return () if isinstance(self._shared, Halt) else self._shared.unstored.names()
