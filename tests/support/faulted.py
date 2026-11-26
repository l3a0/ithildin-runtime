"""Test 2 of #4: a short stretch of test 1's decisions, run with faults injected on chosen days.

`Replay` runs the same handlers `tests/support/trading.py` runs, in the same order
over one folder store, and adds what test 2 injects.

1. **A data day** stores data's result as a halt whose reasons are data's own
   `judge` of a snapshot with an instrument missing or stale, as data's stage
   would have stored it.
2. **Broker faults** reach the backtest's simulated broker through the worker's
   `faults`, so an answer is lost or the order list fails as the simulator says.
3. **A kill** ends the execution worker the moment the journal has taken a given
   entry, as a process that dies can. A new worker starts at once with a writer of
   its own and nothing the dead one held in memory, and before the next
   decision's data is stored, so execution is up before data. The stage that died
   runs again for its decision, as the receive loop gives back an item whose
   handler never finished.

Every figure is exploratory, as test 1's are.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

from ithildinruntime.adapters import FolderStore
from ithildinruntime.core import (
    DATA_STAGE,
    PORTFOLIO_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SEND_STAGE,
    BarStatus,
    DecisionResult,
    HaltRecord,
    Mode,
    OrderState,
    Retry,
    SendOutcome,
    Snapshot,
    output_key,
    result_key,
)
from ithildinruntime.data.source import judge
from ithildinruntime.execution.brokers.faults import Faults
from ithildinruntime.journal import Journal
from ithildinruntime.runtime.alpha_worker import alpha_handlers
from ithildinruntime.runtime.cost_worker import cost_handlers
from ithildinruntime.runtime.execution_worker import ExecutionWorker, open_backtest
from ithildinruntime.runtime.portfolio_worker import portfolio_handler
from ithildinruntime.runtime.risk_worker import risk_handler
from tests.support.replication import FIRST_DECISION, GLD, LOOKBACK, USO, cut
from tests.support.trading import (
    ACCOUNT,
    CASH,
    DEPLOYMENT,
    RECORDS,
    RELEASE,
    RUN,
    SESSIONS,
    TABLE,
    _caps,
    _data,
    _run,
    caps_on,
    stage_order,
    storage_of,
)
from tests.support.watched_broker import watch_submits
from tests.test_storage_contract import Alarms

__all__ = ["Day", "Died", "Dying", "Replay", "counted"]


class Died(BaseException):
    """The execution worker's process ending, which nothing in it catches."""


class Dying(FolderStore):
    """A folder store whose `kill_after`th journal entry from now ends the process once it lands."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.kill_after: int | None = None
        self.appends = 0

    def create(self, key: str, data: bytes, sha256: str) -> str:
        tag = super().create(key, data, sha256)
        if key.startswith(f"journal/{ACCOUNT}/"):
            self.appends += 1
            if self.kill_after is not None and self.appends == self.kill_after:
                self.kill_after = None
                raise Died(key)
        return tag


@dataclass(frozen=True)
class Day:
    """One decision as test 2 reads it back: what each stage ended in, and what was sent."""

    at: datetime
    halts: Mapping[str, str]
    # Each planned order's id, instrument, type, shares and the state the day left it in.
    sent: tuple[tuple[str, str, str, str, OrderState | None], ...]
    died: str | None = None


@dataclass
class Replay:
    """Test 1's decisions from the first, with faults, one `decide` per decision."""

    every_session: Snapshot
    root: Path
    faults: Faults = field(default_factory=Faults)

    def __post_init__(self) -> None:
        self.store = Dying(self.root / "store")
        self.storage = storage_of(self.store)
        self.records = self.root / "stress_moves.toml"
        self.records.write_text(RECORDS)
        self.days = [bar.session for bar in self.every_session.bars[GLD]["1d"]]
        self.first = self.days.index(FIRST_DECISION)
        open_backtest(
            Mode.BACKTEST,
            self.store,
            ACCOUNT,
            CASH,
            SESSIONS.session(self.days[self.first - 1]).close,
        )
        self.alarms = Alarms()
        self.written: list = []
        self.starts = 0
        made = dict(
            storage=self.storage, deployment=DEPLOYMENT, run=RUN, release=RELEASE, table=TABLE
        )
        models: dict[str, Callable] = {**cost_handlers(**made), **alpha_handlers(**made)}
        self.order = stage_order(models)
        self.handlers: dict[str, Callable] = {
            **models,
            RISK_STAGE: risk_handler(**made, stress_moves=self.records),
            PORTFOLIO_STAGE: portfolio_handler(**made, stress_moves=self.records),
        }
        # Each order the simulated account was asked to take, with the stage that asked.
        self.submits: list[tuple[str, str]] = []
        self.running: str | None = None
        self.start()

    def start(self) -> None:
        """A new execution process over the same store, as the host restarts one."""
        from ithildinruntime.adapters import HistoryClock

        self.starts += 1
        self.worker = ExecutionWorker(
            mode=Mode.BACKTEST,
            store=self.store,
            storage=self.storage,
            account=ACCOUNT,
            clock=HistoryClock(readings=None),
            alerts=self.alarms,
            retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
            pause=lambda s: None,
            deployment=DEPLOYMENT,
            run=RUN,
            release=RELEASE,
            table=TABLE,
            stress_moves=self.records,
            faults=self.faults,
        )
        self.handlers[RECOVER_STAGE] = self.worker.recover
        self.handlers[SEND_STAGE] = self.worker.send

    def asked(self, client_order_id: str) -> None:
        """Note one submit, which `faulted.counted` reports from the simulated broker."""
        self.submits.append((self.running or "no stage", client_order_id))

    def decide(
        self,
        n: int,
        data: str | None = None,
        kill_after: int | None = None,
        twice: tuple[str, ...] = (),
    ) -> Day:
        """Run decision `n` from the first, every stage in order, and read back what it left.

        `data` names an instrument data finds missing, or one it finds stale with
        `stale:` before it. `kill_after` ends the worker once the journal has taken
        that many more entries in this decision. Each stage `twice` names is given
        its item a second time, as a queue that redelivers one does.
        """
        held = cut(self.every_session, self.first + n, LOOKBACK)
        at = held.decision_at
        _caps(self.store, caps_on(self.days[self.first + n]), self.written)
        self._data(held, data)
        self.store.appends, self.store.kill_after = 0, kill_after
        died = None
        for name in self.order:
            if name == DATA_STAGE:
                continue
            try:
                self._run(name, at)
            except Died:
                died = name
                self.start()
                # The item comes back, and the new process runs it again.
                self._run(name, at)
            if name in twice:
                self._run(name, at)
        self.store.kill_after = None
        return self._day(at, died)

    def _run(self, name: str, at: datetime) -> DecisionResult:
        self.running = name
        try:
            return _run(self.storage, self.handlers[name], name, at)
        finally:
            self.running = None

    def _data(self, held: Snapshot, fault: str | None) -> None:
        """Store data's snapshot and result, a halt where data's own judgement finds one."""
        if fault is not None:
            stale = fault.startswith("stale:")
            instrument = fault.removeprefix("stale:")
            status = BarStatus.STALE if stale else BarStatus.MISSING
            bars = dict(held.bars)
            if stale:
                bars[instrument] = {"1d": held.bars[instrument]["1d"][:-1]}
            else:
                bars.pop(instrument)
            held = replace(held, bars=bars, status={**held.status, instrument: status})
        reasons = judge(held, (GLD, USO), measured=False)
        halt = HaltRecord("; ".join(reasons), None, (), (), ()) if reasons else None
        _data(self.storage, held, halt)

    def _day(self, at: datetime, died: str | None) -> Day:
        halts = {}
        for name in self.order:
            result = self.storage.read_result(result_key(DEPLOYMENT, RUN, at, name), at)
            assert result is not None, name
            if result.halt is not None:
                halts[name] = result.halt.reason
        view = Journal(self.store, ACCOUNT).view()
        seq = view.decided.get(at)
        orders = () if seq is None else view.decisions[seq].orders
        sent = tuple(
            (
                o.client_order_id,
                o.instrument,
                o.type.value,
                f"{o.qty}",
                view.orders[o.client_order_id].state if o.client_order_id in view.orders else None,
            )
            for o in orders
        )
        return Day(at, halts, sent, died)

    def outcome(self, at: datetime) -> SendOutcome | None:
        """The send stage's outcome of a decision, where it stored one."""

        key = result_key(DEPLOYMENT, RUN, at, SEND_STAGE)
        result = self.storage.read_result(key, at)
        if result is None or result.halt is not None:
            return None
        listed = output_key(DEPLOYMENT, RUN, at, SEND_STAGE, "outcome")
        (ref,) = [r for r in result.outputs if r.key == listed]
        (entry,) = self.storage.read_output(key, result, ref, at, SendOutcome).entries
        return entry

    def taken(self) -> set[str]:
        """Each client order id the simulated account took, which it takes once at most."""
        from ithildinruntime.execution.brokers.log import SimulatedAccount

        log = SimulatedAccount.load(self.store, ACCOUNT, "f" * 32)
        log.catch_up()
        return set(log.names)

    def view(self):
        return Journal(self.store, ACCOUNT).view()


def counted(monkeypatch, replay: Replay) -> None:
    """Have the simulated broker tell `replay` of each submit, before the account sees it.

    The account refuses an order of another decision's prices, so what it took
    cannot show a submit made while a later decision settles. This counts what
    the platform asked.
    """
    watch_submits(monkeypatch, before=lambda intent: replay.asked(intent.client_order_id))
