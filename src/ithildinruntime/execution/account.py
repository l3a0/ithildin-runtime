"""One broker account, as the execution worker holds it for its whole life.

The worker builds one `Account` and keeps it across decisions, because a cold
view of a long journal is slow and because the account keeps each answer the
journal has not taken. `path` gives the `OrderPath` for one decision's broker.

A stage reads the journal through `view` and adds what the broker said of an
order through `record`, which takes the five kinds a stage may write and no
entry of the path's own.

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
    StoreError,
)
from ithildinruntime.journal import (
    DisputedFillEntry,
    ExpiredEntry,
    FillEntry,
    Journal,
    ReadEntry,
    RejectedEntry,
    View,
    kind_of,
)

from .controls import Controls
from .order_path import Named, OrderPath, Shared, Unstored, quietly
from .patience import Patience, code_of

__all__ = ["Account", "StageRecord"]

# What a stage may record: a read of the broker, and what it says became of an order.
StageRecord = ReadEntry | FillEntry | DisputedFillEntry | ExpiredEntry | RejectedEntry


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
        self.alias = account
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

    def view(self, report: Callable[[], None]) -> View:
        """One consistent reading of the journal, which halts and alarms on a store that is down."""
        shared = self._held()
        try:
            return shared.patience.tries(quietly(report))(shared.journal.view)
        except StoreError as error:
            raise shared.outage(f"the journal could not be read: {code_of(error)}") from None

    def record(self, view: View, record: StageRecord, report: Callable[[], None]) -> View:
        """Add one record on `view`, and return the view after it.

        It is built once and resent unchanged. Another writer's entry at that
        number raises `NumberTaken`, for the caller to view again and decide
        again, and an entry the fold refuses halts with nothing stored.
        """
        if not isinstance(record, StageRecord):
            raise TypeError(f"a stage does not record a {kind_of(record)}")
        shared = self._held()
        at, tries = shared.clock.now(), shared.patience.tries(quietly(report))
        try:
            _, after = tries(lambda: shared.journal.record(view, record, at))
        except StoreError as error:
            what = f"a {kind_of(record)} was not stored in the journal"
            raise shared.outage(f"{what}: {code_of(error)}") from None
        return after

    def alarm(self, reason: str, report: Callable[[], None]) -> str:
        """Send one alarm that names the account, and say what a halt adds where it could not.

        One that could not be sent is tried again at each later settle, the
        worker's last one included, which names it where it is still owed.
        """
        if self._held().unstored.alarm(reason, quietly(report)):
            return ""
        return ". The alarm could not be sent, and is tried again at the next stage's start"

    def unsent(self) -> tuple[str, ...]:
        """Each alarm this process owes, which the channel would not take."""
        return () if isinstance(self._shared, Halt) else self._shared.unstored.unsent()

    def settle(self, report: Callable[[], None]) -> None:
        """Store each entry the journal has not taken, or halt naming each still owed.

        An alarm about one that could not be sent is tried again here too, and
        never halts.

        Each stage's handler calls it before its first view of the journal. An
        order in doubt halts recover, so an answer held back until the next
        send would never be written.
        """
        self._held().unstored.settle(quietly(report))

    def unstored(self) -> tuple[str, ...]:
        """Each entry this process holds that the journal has not taken."""
        return () if isinstance(self._shared, Halt) else self._shared.unstored.names()

    def _held(self) -> Shared:
        if isinstance(self._shared, Halt):
            raise Halt(self._shared.reason)
        return self._shared
