"""What a gate test hands `risk.Gate`, built from plain figures.

Every figure is made up. A test names what it changes and the rest is loose
enough to pass, so a refusal or a halt has the one cause the test gives it.

The decision is at close−20 on 2026-11-03 and the snapshot already holds a bar
for that session, which ends at the decision. Before #100 no real close−20
snapshot holds one, so this stands in for the price #100 brings.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from ithildinruntime.core import (
    Bar,
    BarStatus,
    Book,
    Constraints,
    GateInputs,
    HardCaps,
    InstrumentId,
    LossReading,
    OrderIntent,
    OrderType,
    Snapshot,
    StopState,
    TimeInForce,
    Valuation,
    Verdict,
    load_sessions,
    mark,
)
from ithildinruntime.risk import Gate
from tests.support.spreads import bar

GLD, USO, SPY = InstrumentId("GLD"), InstrumentId("USO"), InstrumentId("SPY")
CHAN, OTHER = "chan", "other"
TABLE = load_sessions()
PRIOR, TODAY = date(2026, 11, 2), date(2026, 11, 3)
CLOSE = TABLE.session(TODAY).close
DECISION = CLOSE - timedelta(minutes=20)
VINTAGE = "a" * 64
QTY = Decimal("1E-9")
CENT = Decimal("0.0001")
LOOSE = Decimal(10**9)


def d(value: object) -> Decimal:
    return Decimal(str(value))


def order(
    qty: object,
    instrument: InstrumentId = GLD,
    strategy: str = CHAN,
    name: str = "new-1",
    kind: OrderType = OrderType.MARKET,
    tif: TimeInForce = TimeInForce.DAY,
) -> OrderIntent:
    return OrderIntent(name, strategy, instrument, d(qty), kind, tif)


def moc(qty: object, instrument: InstrumentId = GLD) -> OrderIntent:
    return order(qty, instrument, kind=OrderType.MARKET_ON_CLOSE, tif=TimeInForce.CLS)


def caps(**changes: object) -> HardCaps:
    fields: dict[str, object] = {
        "max_order_qty": {GLD: LOOSE, USO: LOOSE},
        "max_position": {GLD: LOOSE, USO: LOOSE},
        "max_gross": LOOSE,
        "max_open_orders": 100,
        "loss_stop": LOOSE,
        "moc_cutoff": 0,
        "share": d("0.5"),
        "stress_move": d("0.2"),
        "stress_low": d("0.1"),
        "stress_high": d("0.6"),
        "stress_vintage": VINTAGE,
        "stress_through": date(2026, 10, 30),
        "stress_max_age_days": 365,
        "share_step": Decimal(1),
    }
    return HardCaps(**fields | changes)  # type: ignore[arg-type]


def constraints(**changes: object) -> Constraints:
    fields: dict[str, object] = {
        "strategy": CHAN,
        "max_order_qty": {GLD: LOOSE, USO: LOOSE},
        "max_position": {GLD: LOOSE, USO: LOOSE},
        "max_gross": LOOSE,
        "max_open_orders": 100,
        "max_net": LOOSE,
        "loss_stop": LOOSE,
        "moc_cutoff": 0,
    }
    return Constraints(**fields | changes)  # type: ignore[arg-type]


def book(
    positions: Mapping[str, Mapping[InstrumentId, object]] | None = None,
    opens: Sequence[OrderIntent] = (),
    cash: object = 10**9,
    **changes: object,
) -> Book:
    filed: dict[str, tuple[OrderIntent, ...]] = {}
    for made in opens:
        filed[made.strategy] = (*filed.get(made.strategy, ()), made)
    fields: dict[str, object] = {
        "positions_by_strategy": {
            name: {i: d(q) for i, q in held.items()} for name, held in (positions or {}).items()
        },
        "open_orders_by_strategy": filed,
        "cash": d(cash),
        "replayed_through": 7,
        "in_doubt": (),
        "corrections": (),
    }
    return Book(**fields | changes)  # type: ignore[arg-type]


def stated(instrument: InstrumentId, day: date, close: Decimal) -> Bar:
    """A bar of stated kind. Its other prices differ, so a mark read from one answers otherwise."""
    made = bar(instrument, day, close)
    return replace(
        made,
        price_kind="official close",
        venues="consolidated",
        delay="none",
        volume_counted="consolidated in shares",
    )


def _by(made: Bar, at: datetime) -> Bar:
    """The bar, ended and known by `at`."""
    return replace(made, end=min(made.end, at), knowable_at=min(made.knowable_at, at))


def snapshot(
    marks: Mapping[InstrumentId, object],
    at: datetime = DECISION,
    day: date = TODAY,
    prior: Mapping[InstrumentId, object] | None = None,
    **changes: object,
) -> Snapshot:
    """Each instrument's bar for `day` and, at half its price or as `prior` says, the one before."""
    before = TABLE.before(day).day
    bars = {}
    for instrument, price in marks.items():
        earlier = d((prior or {}).get(instrument, d(price) / 2))
        bars[instrument] = {
            "1d": (
                stated(instrument, before, earlier),
                _by(stated(instrument, day, d(price)), at),
            )
        }
    fields: dict[str, object] = {
        "decision_at": at,
        "bars": bars,
        "quotes": {},
        "status": {i: BarStatus.FRESH for i in marks},
        "provisional": False,
        "flags": {},
        "segments": (),
        "ledgers": {},
    }
    return Snapshot(**fields | changes)  # type: ignore[arg-type]


def valuation(value: object = 10**9, **changes: object) -> Valuation:
    """V as cash alone, which is how a test sets its value without a position."""
    fields: dict[str, object] = {
        "session": PRIOR,
        "value": d(value),
        "cash": d(value),
        "positions": {},
        "marks": {},
        "replayed_through": 7,
    }
    return Valuation(**fields | changes)  # type: ignore[arg-type]


def inputs(**changes: object) -> GateInputs:
    fields: dict[str, object] = {
        "caps": caps(),
        "valuation": valuation(),
        "stop": StopState.ABSENT,
        "close_entry": None,
    }
    return GateInputs(**fields | changes)  # type: ignore[arg-type]


@dataclass(frozen=True)
class World:
    """One check's whole input. `at` replaces any part, and `loss_of` sets the day's loss."""

    intent: OrderIntent = field(default_factory=lambda: order(1))
    book: Book = field(default_factory=book)
    snapshot: Snapshot = field(default_factory=lambda: snapshot({GLD: 1, USO: 1}))
    constraints: Constraints = field(default_factory=constraints)
    inputs: GateInputs = field(default_factory=inputs)
    now: datetime = DECISION

    def at(self, **changes: object) -> World:
        return replace(self, **changes)  # type: ignore[arg-type]

    def capped(self, **changes: object) -> World:
        return self.at(inputs=replace(self.inputs, caps=replace(self.inputs.caps, **changes)))

    def held_to(self, **changes: object) -> World:
        return self.at(constraints=replace(self.constraints, **changes))

    def given(self, **changes: object) -> World:
        return self.at(inputs=replace(self.inputs, **changes))

    def losing(self, loss: object) -> World:
        """V set so the day's loss is `loss`, to V's four places, whatever the book holds."""
        worth = self.book.cash
        for positions in self.book.positions_by_strategy.values():
            for instrument, qty in positions.items():
                worth += qty * mark(self.snapshot, instrument, PRIOR)
        return self.given(valuation=valuation((worth + d(loss)).quantize(CENT)))

    def check(self) -> Verdict:
        return Gate().check(
            self.intent, self.book, self.snapshot, self.constraints, self.inputs, self.now
        )

    def loss(self) -> LossReading:
        return Gate().loss(self.book, self.snapshot, self.constraints, self.inputs, self.now)


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)  # type: ignore[misc,arg-type]
