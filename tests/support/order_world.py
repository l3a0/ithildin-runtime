"""One decision an order path works on: its stored files, its journal and its broker.

Every figure is made up and loose enough to pass. A test changes one thing, and
the rest sends, so a halt or a refusal has the one cause the test gives it.

`Wired` is a store in memory that a test can stop. `stops` raises at a chosen
call, before it lands or after, and `refuse` answers as a store that is down.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest

from ithildinruntime.core import (
    CONSTRAINTS_OUTPUT,
    DATA_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SNAPSHOT_OUTPUT,
    VALUATION_OUTPUT,
    BlobHeader,
    BlobRef,
    Constraints,
    DecisionResult,
    GateInputs,
    Halt,
    HaltRecord,
    HardCaps,
    OrderIntent,
    OrderState,
    OrderType,
    Retry,
    Snapshot,
    StageKind,
    StopState,
    Storage,
    StoredBlob,
    StoreUnavailable,
    StressRecord,
    TargetPortfolio,
    TimeInForce,
    Valuation,
    caps_key,
    decode,
    decode_output,
    encode,
    encode_output,
    order_id,
    output_key,
    result_key,
    sha256_of,
)
from ithildinruntime.core import OutputFile as File
from ithildinruntime.execution.account import Account
from ithildinruntime.execution.order_path import OrderPath
from ithildinruntime.journal import (
    DecisionEntry,
    FillEntry,
    IntentEntry,
    Journal,
    OpeningEntry,
    ReadEntry,
    SendingEntry,
    VerdictEntry,
    record_of,
)
from ithildinruntime.risk import Gate
from tests.support import gates
from tests.support.fake_broker import FakeBroker
from tests.support.gate_oracle import answer
from tests.support.journals import Memory, ack, fill
from tests.support.journals import verdict as planted

# The run is a backtest's two dates and the deployment no part of the alias, so a key built from
# the decision's date or from the account reads nothing.
ACCOUNT, DEPLOYMENT, RUN = "paper-1", "d-7", "2026-11-02_2026-11-30"
PLANNER, WRITER, OTHER_WRITER = "0" * 32, "1" * 32, "2" * 32
AT = gates.DECISION
# What the clock reads when a send begins, which is never the decision time itself.
NOW = AT + timedelta(seconds=7)
HASHES = {gates.CHAN: "c" * 64}
CASH = Decimal(1_000_000)


class Kill(BaseException):
    """The process ends here, so nothing above it runs but what Python unwinds."""


class Wired(Memory):
    """A store whose every call a test can hook, by its number, its operation and its key."""

    def __init__(self, blobs: Mapping[str, bytes] | None = None) -> None:
        super().__init__(blobs)
        self.calls: list[tuple[str, str]] = []
        self.before: Callable[[int, str, str], None] = lambda n, op, key: None
        self.after: Callable[[int, str, str], None] = lambda n, op, key: None
        self.quiet = False

    def _hooked[R](self, op: str, key: str, call: Callable[[], R]) -> R:
        if self.quiet:
            return call()
        self.calls.append((op, key))
        number = len(self.calls)
        self.before(number, op, key)
        try:
            return call()
        finally:
            self.after(number, op, key)

    def create(self, key: str, data: bytes, sha256: str) -> str:
        return self._hooked("create", key, lambda: super(Wired, self).create(key, data, sha256))

    def get(self, key: str) -> bytes:
        return self._hooked("get", key, lambda: super(Wired, self).get(key))

    def list(self, prefix: str, after: str | None) -> list[str]:
        return self._hooked("list", prefix, lambda: super(Wired, self).list(prefix, after))

    def head(self, key: str) -> StoredBlob:
        data = self.blobs[key]
        return StoredBlob(BlobRef(key, sha256_of(data)), "", AT)

    def put(self, key: str, data: bytes) -> None:
        """Write a blob by hand, around every rule the store keeps and every hook."""
        if key in self.blobs:
            self.blobs[key] = data
        else:
            Memory.create(self, key, data, "")

    def drop(self, key: str) -> None:
        del self.blobs[key]
        self._keys.remove(key)

    def refuse(self, *ops_and_keys: tuple[str, str]) -> None:
        """Answer as a store that is down, for each operation and the start of its key."""

        def before(number: int, op: str, key: str) -> None:
            if any(o in ("*", op) and key.startswith(k) for o, k in ops_and_keys):
                raise StoreUnavailable(key, "AccessDenied: the store's own text")

        self.before = before


class Clock:
    """A clock a test sets. Waiting moves its elapsed reading and, where asked, its time."""

    def __init__(self, at: datetime) -> None:
        self.at = at
        self.elapsed = 0.0
        self.waits: list[float] = []
        self.moves = False

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return self.elapsed

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.elapsed += seconds
        if self.moves:
            self.at += timedelta(seconds=seconds)


class Alarms:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.error: BaseException | None = None

    def alarm(self, reason: str) -> None:
        self.sent.append(reason)
        if self.error is not None:
            raise self.error

    def heartbeat(self) -> None:
        pass


def order(
    place: int,
    qty: object,
    instrument: str = gates.GLD,
    strategy: str = gates.CHAN,
    at: datetime = AT,
    kind: OrderType = OrderType.MARKET_ON_CLOSE,
    tif: TimeInForce = TimeInForce.CLS,
) -> OrderIntent:
    name = order_id(strategy, at, place)
    return OrderIntent(name, strategy, instrument, gates.d(qty), kind, tif)  # type: ignore[arg-type]


def caps_blob(caps: HardCaps, account: str = ACCOUNT) -> bytes:
    """A caps blob as the owner writes one, by hand."""
    limits: dict[str, object] = {}
    for name in HardCaps.__dataclass_fields__:
        value = getattr(caps, name)
        if isinstance(value, Mapping):
            limits[name] = {i: f"{q:f}" for i, q in value.items()}
        elif isinstance(value, Decimal):
            limits[name] = f"{value:f}"
        elif isinstance(value, int | str):
            limits[name] = value
        else:
            limits[name] = value.isoformat()
    return json.dumps({"format": 1, "account": account, "limits": limits}, indent=1).encode()


def planted_verdict(name: str, book: int) -> VerdictEntry:
    """An approval planted by hand, which rebuilds nothing and is never sent on."""
    return planted(name, book=book)


@dataclass
class World:
    """One decision with its orders planned, its files stored and its path built on demand."""

    orders: tuple[OrderIntent, ...] = field(default_factory=lambda: (order(1, 2),))
    caps: HardCaps = field(default_factory=gates.caps)
    constraints: tuple[Constraints, ...] = field(default_factory=lambda: (gates.constraints(),))
    snapshot: Snapshot = field(default_factory=lambda: gates.snapshot({gates.GLD: 1, gates.USO: 1}))
    valuation: Valuation = field(default_factory=lambda: gates.valuation(CASH, replayed_through=1))
    store: Wired = field(default_factory=Wired)
    retry: Retry = field(default_factory=lambda: Retry(timedelta(seconds=10), 1.0, 4.0))
    # Builds the gate from the stress records the world's caps are held to.
    gate: Callable[[tuple[StressRecord, ...]], object] = Gate
    records: tuple[StressRecord, ...] | None = None
    # What the `decision` entry's inputs name instead, where `None` leaves a name out.
    inputs: Mapping[str, BlobRef | None] = field(default_factory=dict)
    plan: bool = True
    # GLD shares the strategy holds from an earlier decision, bought or sold at 1.
    position: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        self.clock = Clock(NOW)
        self.alarms = Alarms()
        self.reports = 0
        self.report_error: BaseException | None = None
        self.store.quiet = True
        if self.records is None:
            # A strategy's two constraints, which halt, are held to one record.
            fitted = [r for c in self.constraints for r in gates.fitting(c, self.caps)]
            self.records = tuple({r.strategy: r for r in fitted}.values())
        self.refs = {
            "snapshot": self.file(DATA_STAGE, SNAPSHOT_OUTPUT, Snapshot, (self.snapshot,)),
            "constraints": self.file(RISK_STAGE, CONSTRAINTS_OUTPUT, Constraints, self.constraints),
            "valuation": self.file(RECOVER_STAGE, VALUATION_OUTPUT, Valuation, (self.valuation,)),
        }
        self.store.put(caps_key(ACCOUNT), caps_blob(self.caps))
        self.planner = Journal(self.store, ACCOUNT, PLANNER)
        if self.plan:
            self.record(OpeningEntry(ACCOUNT, CASH))
            self.record(ReadEntry("execution/send", AT, BlobRef("r", "a" * 64), AT))
            if self.position:
                self.hold(self.position)
            self.decide(self.orders)
        self.store.quiet = False
        self.broker = FakeBroker(lambda: self.store.blobs, ACCOUNT, NOW)

    # What is stored

    def header(self, stage: str, at: datetime = AT, hashes: Mapping | None = None) -> BlobHeader:
        return BlobHeader(
            format=1,
            deployment=DEPLOYMENT,
            run=RUN,
            stage=stage,
            kind=str(StageKind.DETERMINISTIC),
            decision_at=at,
            inputs={},
            release="r1",
            config_hashes=hashes or HASHES,
        )

    def file(
        self,
        stage: str,
        name: str,
        kind: type,
        entries: Iterable[object],
        *,
        at: datetime = AT,
        hashes: Mapping | None = None,
        halt: HaltRecord | None = None,
        listed: int = 1,
        trade_nothing: bool = False,
        decision_at: datetime = AT,
    ) -> BlobRef:
        """Store one stage's file and the result that lists it, and name the file."""
        key = output_key(DEPLOYMENT, RUN, decision_at, stage, name)
        data = encode_output(
            kind, File(self.header(stage, at, hashes), tuple(entries), trade_nothing)
        )
        ref = BlobRef(key, sha256_of(data))
        self.store.put(key, data)
        outputs = () if halt else (ref,) * listed
        self.result(stage, outputs, halt, decision_at=decision_at)
        return ref

    def result(
        self,
        stage: str,
        outputs: tuple[BlobRef, ...],
        halt: HaltRecord | None = None,
        at: datetime = AT,
        decision_at: datetime = AT,
    ) -> None:
        result = DecisionResult(self.header(stage, at), outputs, halt)
        self.store.put(result_key(DEPLOYMENT, RUN, decision_at, stage), encode(result))

    def rewrite_caps(self, **changes: object) -> None:
        self.store.put(caps_key(ACCOUNT), caps_blob(replace(self.caps, **changes)))

    # The journal

    def record(self, record: object, journal: Journal | None = None) -> int:
        """Record one entry as a writer of its own, around every hook."""
        journal, quiet = journal or self.planner, self.store.quiet
        self.store.quiet = True
        try:
            ack, _ = journal.record(journal.view(), record, AT)
        finally:
            self.store.quiet = quiet
        return ack.seq

    def decide(
        self, orders: Iterable[OrderIntent], at: datetime = AT, read: int = 2, run: str = RUN
    ) -> int:
        return self.record(
            DecisionEntry(
                deployment=DEPLOYMENT,
                run=run,
                decision_at=at,
                targets=(TargetPortfolio(at, gates.CHAN, {gates.GLD: Decimal(1)}),),
                orders=tuple(orders),
                off=(),
                inputs={
                    name: ref
                    for name, ref in (dict(self.refs) | dict(self.inputs)).items()
                    if ref is not None
                },
                read=read,
                synced=read,
            )
        )

    def hold(self, qty: Decimal) -> None:
        """Plant an order of the session before, filled whole at 1."""
        at = AT - timedelta(days=1)
        made = order(1, qty, at=at)
        name = made.client_order_id
        seq = self.decide((made,), at)
        self.record(IntentEntry(seq, made))
        self.record(planted_verdict(name, seq + 1))
        self.record(SendingEntry(name))
        self.record(ack(name, "old-1"))
        bought = fill("old-f1", "old-1", qty, 1, gates.GLD, at=at, received_at=at)
        self.record(FillEntry(bought, 2))

    def reader(self) -> Journal:
        return Journal(Memory(self.store.blobs), ACCOUNT)

    def kinds(self) -> list[str]:
        """The kinds of entry that moved an order or a fill, oldest first."""
        return [k for k in self.every_kind() if k not in ("opening", "read", "decision")]

    def every_kind(self) -> list[str]:
        return [entry.body["kind"] for entry in self.reader().read()]

    def entries(self) -> list:
        return [record_of(entry) for entry in self.reader().read()]

    def state(self, place: int = 1) -> OrderState | None:
        found = self.reader().view().orders.get(self.orders[place - 1].client_order_id)
        return found.state if found else None

    # The path

    def account(self, writer: str = WRITER, **changes: object) -> Account:
        given: dict[str, object] = {
            "store": self.store,
            "storage": self.storage(),
            "account": ACCOUNT,
            "writer": writer,
            "gate": self.gate(self.records or ()),
            "clock": self.clock,
            "alerts": self.alarms,
            "retry": self.retry,
            "pause": self.clock.wait,
        }
        return Account(**given | changes)  # type: ignore[arg-type]

    def storage(self) -> Storage:
        return Storage(
            self.store,  # type: ignore[arg-type]
            timer=self.clock,  # type: ignore[arg-type]
            pause=self.clock.wait,
            retry=self.retry,
            alerts=self.alarms,
            owner_step="the broker's site",
        )

    def report(self) -> None:
        self.reports += 1
        if self.report_error is not None:
            raise self.report_error

    def path(self, account: Account | None = None, broker: object | None = None) -> OrderPath:
        made = account or self.account()
        return made.path(broker or self.broker, self.report)  # type: ignore[arg-type]

    def send(self, place: int = 1, account: Account | None = None) -> OrderState:
        return self.path(account).send(self.orders[place - 1].client_order_id)

    @property
    def submits(self) -> int:
        return self.broker.calls.count("submit")

    # The gate's answer, again

    def rebuilt(self) -> int:
        """Rebuild the gate's answer for every `sending`, from the `verdict` entry alone.

        It reads the caps from the entry, so a blob rewritten or gone since
        changes nothing, and the stress move from the verdict, since no entry
        holds the stress records. Returns how many it rebuilt.
        """
        reader, count = self.reader(), 0
        entries = [(entry.seq, record_of(entry)) for entry in reader.read()]
        for seq, sending in entries:
            if not isinstance(sending, SendingEntry):
                continue
            name = sending.client_order_id
            if name not in {o.client_order_id for o in self.orders}:
                continue
            order_ = reader.view().orders[name].intent
            (verdict,) = [
                r
                for s, r in entries
                if isinstance(r, VerdictEntry) and r.client_order_id == name and s < seq
            ]
            book = reader.view(through=verdict.book).book
            snapshot = self._blob(verdict.snapshot, Snapshot)[0]
            (limits,) = [
                c
                for c in self._blob(verdict.constraints, Constraints)
                if c.strategy == order_.strategy
            ]
            valuation = self._blob(verdict.valuation, Valuation)[0]
            caps = decode(verdict.caps, HardCaps)
            inputs = GateInputs(
                caps=replace(caps, stress_move=verdict.verdict.stress_move),
                valuation=valuation,
                stop=StopState(verdict.stop),
                close_entry=None,
            )
            again = answer(
                order_,
                book.positions_by_strategy,
                [o for held in book.open_orders_by_strategy.values() for o in held],
                book.cash,
                book.in_doubt,
                snapshot,
                limits,
                inputs,
                verdict.now,
                gates.CLOSE,
                (),
            )
            said = verdict.verdict
            assert again.approved and said.approved, name
            assert again.adding == said.adding, name
            assert again.worst_loss == Fraction(said.worst_loss), name
            assert again.loss == Fraction(said.loss.loss), name
            assert again.past == tuple(
                (p.limit.value, Fraction(p.value), Fraction(p.bound)) for p in said.past
            ), name
            count += 1
        return count

    def _blob[T](self, ref: BlobRef, kind: type[T]) -> tuple[T, ...]:
        data = self.store.blobs[ref.key]
        assert sha256_of(data) == ref.sha256
        return decode_output(data, kind).entries


def halted(world: World, match: str = "", call: Callable[[], object] | None = None) -> str:
    """The reason a call halts with, where nothing reached the broker."""
    with pytest.raises(Halt, match=match) as raised:
        (call or world.send)()
    assert world.submits == 0
    return raised.value.reason


def last(world: World) -> object:
    return world.entries()[-1]


__all__ = [
    "ACCOUNT",
    "AT",
    "CASH",
    "DEPLOYMENT",
    "NOW",
    "OTHER_WRITER",
    "PLANNER",
    "RUN",
    "WRITER",
    "Alarms",
    "Clock",
    "Kill",
    "Wired",
    "World",
    "caps_blob",
    "halted",
    "last",
    "order",
]
