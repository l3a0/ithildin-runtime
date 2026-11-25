"""One account over many decisions: its journal, its simulated broker and what data stored.

Every order goes in through `OrderPath`, gate and journal included. Every figure is
made up, and the gate's limits are loose, so an order has the one outcome a test
gives it. `recover` does the part of the recover stage a test needs, which journals
each new fill and each order that ended.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from ithildinruntime.core import (
    CONSTRAINTS_OUTPUT,
    DATA_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SNAPSHOT_OUTPUT,
    VALUATION_OUTPUT,
    Bar,
    BarStatus,
    BlobHeader,
    BlobRef,
    BlobStore,
    BrokerState,
    Constraints,
    DecisionResult,
    HardCaps,
    InstrumentId,
    OrderIntent,
    OrderState,
    Retry,
    Snapshot,
    StageKind,
    Storage,
    TargetPortfolio,
    Valuation,
    caps_key,
    encode,
    encode_output,
    load_sessions,
    output_key,
    result_key,
    sha256_of,
    sim_account_key,
)
from ithildinruntime.core import OutputFile as File
from ithildinruntime.execution.account import Account
from ithildinruntime.execution.brokers.faults import Faults
from ithildinruntime.execution.brokers.log import SimulatedAccount
from ithildinruntime.execution.brokers.prices import StoredPrices
from ithildinruntime.execution.brokers.simulated import SimulatedBroker
from ithildinruntime.execution.order_path import OrderPath
from ithildinruntime.journal import (
    DecisionEntry,
    ExpiredEntry,
    FillEntry,
    Journal,
    OpeningEntry,
    ReadEntry,
    RejectedEntry,
)
from ithildinruntime.risk import Gate
from tests.support import gates
from tests.support.order_world import Alarms, Clock, Wired, caps_blob, order
from tests.support.quotes import NONE_READ, quote, read_at

ACCOUNT, DEPLOYMENT = "sim-1", "d-7"
CASH = Decimal(1_000_000)
WRITERS = tuple(f"{n:032x}" for n in range(1, 10))
PLANNER, SIM_WRITER = "a" * 32, "b" * 32
TABLE = load_sessions()
GLD, USO = gates.GLD, gates.USO
FULL, EARLY = date(2026, 11, 3), date(2012, 11, 23)
MICRO = timedelta(microseconds=1)
BEFORE_OPENING = timedelta(days=3650)


def close_of(day: date) -> datetime:
    return TABLE.session(day).close


def after(day: date, count: int = 1) -> date:
    """The session `count` after `day`."""
    later = sorted(d for d in TABLE.sessions if d > day)
    return later[count - 1]


@dataclass
class SimWorld:
    """`closes` gives each instrument's close by session. A session it leaves out has no bar."""

    closes: Mapping[InstrumentId, Mapping[date, object]]
    faults: Faults = field(default_factory=Faults)
    store: Wired = field(default_factory=Wired)
    opened: bool = True
    # The store the simulator's log alone uses, where a test gives it another.
    sim_store: BlobStore | None = None
    sim_writer: str = SIM_WRITER

    def __post_init__(self) -> None:
        self.clock = Clock(datetime.min)
        self.alarms = Alarms()
        self.constraints = gates.constraints()
        # A gate's record is good for a year, and no run here crosses into another.
        self.year = min(d.year for by in self.closes.values() for d in by)
        self.store.quiet = True
        self.planner = Journal(self.store, ACCOUNT, PLANNER)
        self.at = datetime.min
        self.run = ""
        self.writers = iter(WRITERS)
        if self.opened:
            self.clock.at = min(close_of(d) for by in self.closes.values() for d in by)
            self._record(OpeningEntry(ACCOUNT, CASH))
            SimulatedBroker.open(self.sim, ACCOUNT, CASH)
        self.restart()
        self.store.quiet = False

    @property
    def sim(self) -> BlobStore:
        return self.store if self.sim_store is None else self.sim_store

    def restart(self, sim_writer: str | None = None) -> None:
        """End the process and start another over the same store."""
        self.log = SimulatedAccount.load(self.sim, ACCOUNT, sim_writer or self.sim_writer)
        self.account = Account(
            store=self.store,  # type: ignore[arg-type]
            storage=self.storage(),
            account=ACCOUNT,
            writer=next(self.writers, "c" * 32),
            gate=Gate(gates.fitting(self.constraints, _caps(self.year))),
            clock=self.clock,  # type: ignore[arg-type]
            alerts=self.alarms,
            retry=Retry(timedelta(seconds=10), 1.0, 4.0),
            pause=self.clock.wait,
        )

    def storage(self) -> Storage:
        return Storage(
            self.store,  # type: ignore[arg-type]
            timer=self.clock,  # type: ignore[arg-type]
            pause=self.clock.wait,
            retry=Retry(timedelta(seconds=10), 1.0, 4.0),
            alerts=self.alarms,
            owner_step="the broker's site",
        )

    # What data, risk and recover stored

    def bars(self, instrument: InstrumentId, at: datetime) -> tuple[Bar, ...]:
        """The newest three bars that had closed by `at`."""
        by = self.closes[instrument]
        days = sorted(d for d in by if close_of(d) <= at)
        return tuple(gates.stated(instrument, d, Decimal(str(by[d]))) for d in days[-3:])

    def snapshot(self, at: datetime, **changes: object) -> Snapshot:
        today = TABLE.opened_by(at).day
        # Before its close a decision reads a quote of each instrument, at the prices of no bar.
        early = all(today not in by or close_of(today) > at for by in self.closes.values())
        read = read_at(at) if early else NONE_READ
        fields: dict[str, object] = {
            "decision_at": at,
            "bars": {i: {"1d": self.bars(i, at)} for i in self.closes},
            "quotes": {i: quote(i, 3, "3.01", at) for i in self.closes} if early else {},
            "status": {i: BarStatus.FRESH for i in self.closes},
            "provisional": False,
            "flags": {},
            "segments": (),
            "ledgers": {},
            **read,
        }
        return Snapshot(**fields | changes)  # type: ignore[arg-type]

    def _file(self, at: datetime, stage: str, name: str, kind: type, entry: object) -> BlobRef:
        header = BlobHeader(
            format=1,
            deployment=DEPLOYMENT,
            run=self.run,
            stage=stage,
            kind=str(StageKind.DETERMINISTIC),
            decision_at=at,
            inputs={},
            release="r1",
            config_hashes={gates.CHAN: "c" * 64},
        )
        key = output_key(DEPLOYMENT, self.run, at, stage, name)
        data = encode_output(kind, File(header, (entry,), False))
        ref = BlobRef(key, sha256_of(data))
        self.store.put(key, data)
        result = DecisionResult(header, (ref,), None)
        self.store.put(result_key(DEPLOYMENT, self.run, at, stage), encode(result))
        return ref

    # A decision

    def decide(
        self,
        at: datetime,
        orders: Iterable[OrderIntent] = (),
        snapshot: Snapshot | None = None,
        recover: bool = True,
    ) -> tuple[OrderIntent, ...]:
        """Store a decision's files, recover at its time, and journal its plan."""
        quiet, self.store.quiet = self.store.quiet, True
        session = TABLE.opened_by(at).day
        self.store.put(caps_key(ACCOUNT), caps_blob(_caps(self.year), ACCOUNT))
        self.at, self.run, self.clock.at = at, session.isoformat(), max(at, self.clock.at)
        prior = TABLE.before(session).day
        held = gates.valuation(CASH, session=prior, replayed_through=1)
        refs = {
            "snapshot": self._file(
                at, DATA_STAGE, SNAPSHOT_OUTPUT, Snapshot, snapshot or self.snapshot(at)
            ),
            "constraints": self._file(
                at, RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints, self.constraints
            ),
            "valuation": self._file(at, RECOVER_STAGE, VALUATION_OUTPUT, Valuation, held),
        }
        self.store.quiet = quiet
        if recover:
            self.recover()
        planned = tuple(orders)
        read = self._record(ReadEntry("execution/send", at, BlobRef("r", "a" * 64), at))
        self._record(
            DecisionEntry(
                deployment=DEPLOYMENT,
                run=self.run,
                decision_at=at,
                targets=(TargetPortfolio(at, gates.CHAN, {GLD: Decimal(1)}),),
                orders=planned,
                off=(),
                inputs=refs,
                read=read,
                synced=read,
            )
        )
        return planned

    def day(
        self,
        day: date,
        orders: Iterable[tuple[object, InstrumentId]],
        early: int = 0,
        recover: bool = True,
    ) -> list[OrderState]:
        """A decision `early` minutes before a session's close, with its orders sent."""
        at = close_of(day) - timedelta(minutes=early)
        made = self.decide(at, self.orders(at, orders), recover=recover)
        return [self.send(o) for o in made]

    def orders(
        self, at: datetime, wanted: Iterable[tuple[object, InstrumentId]]
    ) -> tuple[OrderIntent, ...]:
        return tuple(order(n, qty, i, at=at) for n, (qty, i) in enumerate(wanted, start=1))

    def broker(self) -> SimulatedBroker:
        """The broker of the newest decision."""
        prices = StoredPrices(self.storage(), DEPLOYMENT, self.run, self.at)
        return SimulatedBroker(self.log, self.clock, prices, self.faults)  # type: ignore[arg-type]

    def path(self, broker: object | None = None) -> OrderPath:
        return self.account.path(broker or self.broker(), lambda: None)  # type: ignore[arg-type]

    def send(self, intent: OrderIntent, broker: object | None = None) -> OrderState:
        return self.path(broker).send(intent.client_order_id)

    # The journal

    def _record(self, record: object) -> int:
        quiet, self.store.quiet = self.store.quiet, True
        try:
            ack, _ = self.planner.record(self.planner.view(), record, self.clock.at)
        finally:
            self.store.quiet = quiet
        return ack.seq

    def view(self):
        return Journal(Plain(self.store.blobs), ACCOUNT).view()

    def recover(self) -> None:
        """Journal each fill the journal has not applied, and each order that ended unfilled."""
        path = self.path()
        found = path.fills(self.clock.at - BEFORE_OPENING)
        listed = {o.broker_order_id for o in path.open_orders().orders}
        view = self.planner.view()
        # A fill of an order in doubt has no acknowledged order to apply to until #147.
        acked = {o.broker_order_id for o in view.orders.values()}
        new = [f for f in found.fills if f.fill_id not in view.fills and f.broker_order_id in acked]
        stage = "execution/recover"
        read = self._record(ReadEntry(stage, self.at, BlobRef("r", "a" * 64), self.at))
        for fill in new:
            self._record(FillEntry(fill, read))
        for name, held in self.planner.view().live.items():
            if held.broker_order_id is None or held.broker_order_id in listed:
                continue
            ack = path.lookup(held.broker_order_id)
            assert ack.order is not None
            given = {"raw": ack.raw, "cut": False, "received_at": ack.received_at}
            if ack.order.state is BrokerState.EXPIRED:
                self._record(ExpiredEntry(name, ack.order.filled, **given))
            elif ack.order.state is BrokerState.REJECTED:
                self._record(RejectedEntry(name, "the broker rejected it", **given))

    def agree(self) -> None:
        """The journal's book and the broker's account are one, as recover compares them."""
        path, book = self.path(), self.view().book
        held: dict[str, Decimal] = {}
        for positions in book.positions_by_strategy.values():
            for instrument, qty in positions.items():
                held[instrument] = held.get(instrument, Decimal(0)) + qty
        assert dict(path.positions().positions) == {i: q for i, q in held.items() if q}
        assert path.account().cash == book.cash
        journaled = [o for orders in book.open_orders_by_strategy.values() for o in orders]
        opened = {o.client_order_id: o.qty for o in journaled}
        listed = path.open_orders().orders
        assert {o.client_order_id: o.qty - o.filled for o in listed} == opened

    # The simulator's log

    def blobs(self) -> dict[str, bytes]:
        prefix = sim_account_key(ACCOUNT, 1).rpartition("/")[0] + "/"
        held = self.sim.blobs  # type: ignore[attr-defined]
        return {key: held[key] for key in sorted(held) if key.startswith(prefix)}


def _caps(year: int) -> HardCaps:
    return gates.caps(stress_through=date(year, 1, 1))


class Plain(Wired):
    """The same blobs with no hook, so reading them back calls none of the test's."""

    def __init__(self, blobs: Mapping[str, bytes]) -> None:
        super().__init__(blobs)
        self.quiet = True


__all__ = [
    "ACCOUNT",
    "CASH",
    "DEPLOYMENT",
    "EARLY",
    "FULL",
    "GLD",
    "MICRO",
    "Plain",
    "SIM_WRITER",
    "TABLE",
    "USO",
    "SimWorld",
    "after",
    "close_of",
]
