"""Snapshots of two legs' daily bars, for the alpha model's tests.

Every price here is written in a test or drawn from a seeded generator. None is
a vendor's.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from ithildinruntime.core import (
    Bar,
    BarStatus,
    InstrumentId,
    Snapshot,
    StrategyConfig,
    load_sessions,
    parse_strategy,
)

GLD, USO = InstrumentId("GLD"), InstrumentId("USO")
TABLE = load_sessions()
DAYS = sorted(TABLE.sessions)
READ_AT = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)

# Chan's own settings, as `chan_bollinger.toml` states them.
CHAN_SECTION = {
    "model": '"bollinger_spread"',
    "dependent": '"USO"',
    "independent": '"GLD"',
    "hedge_sessions": "20",
    "band_sessions": "20",
    "entry": "1",
    "exit": "0",
}


def strategy(
    *, name: str = "chan", instruments: str = '["GLD", "USO"]', extra: str = "", **changes: object
) -> StrategyConfig:
    """A strategy whose `[alpha]` section is Chan's with these keys changed, or dropped for None."""
    section = {**CHAN_SECTION, **changes}
    written = "\n".join(f"{k} = {v}" for k, v in section.items() if v is not None)
    text = f"instruments = {instruments}\n[alpha]\n{written}\n{extra}"
    return parse_strategy(name, text.encode())


def bar(instrument: InstrumentId, day: date, close: Decimal, basis: str = "adjusted") -> Bar:
    ended = TABLE.session(day).close
    # The open, the high and the low differ from the close, so a model that
    # read one of them in its place would answer otherwise.
    return Bar(
        instrument=instrument,
        session=day,
        open=close * 3,
        high=close * 4,
        low=close / 2,
        close=close,
        volume=None,
        basis=basis,
        origin="vintage",
        vintage_id="v1",
        end=ended,
        interval="1d",
        first_seen=READ_AT,
        fetched=None,
        fetched_at=None,
        fetched_end=None,
        fetched_end_at=None,
        knowable_at=ended,
        source="a test",
        source_as_of=None,
        price_kind="unstated",
        venues="unstated",
        delay="unstated",
        volume_counted="unstated",
        excess_places=(),
    )


def snapshot(
    gld: Sequence[Bar], uso: Sequence[Bar], at: datetime | None = None, **changes: object
) -> Snapshot:
    """Both legs' daily bars at `at`, which is the newest bar's close unless a test says."""
    fields: dict[str, object] = {
        "decision_at": at or TABLE.session(gld[-1].session).close,
        "bars": {GLD: {"1d": tuple(gld)}, USO: {"1d": tuple(uso)}},
        "quotes": {},
        "status": {GLD: BarStatus.FRESH, USO: BarStatus.FRESH},
        "provisional": False,
        "flags": {},
        "segments": (),
        "ledgers": {},
    }
    return Snapshot(**fields | changes)  # type: ignore[arg-type]


@dataclass(frozen=True)
class Series:
    """A run's whole series of both legs, on consecutive sessions."""

    gld: tuple[Bar, ...]
    uso: tuple[Bar, ...]

    def __len__(self) -> int:
        return len(self.gld)

    def first(self, n: int) -> Series:
        return Series(self.gld[:n], self.uso[:n])

    def at(self, n: int, lookback: int) -> Snapshot:
        """What a decision at session `n`'s close reads, `lookback` sessions back."""
        window = slice(max(0, n - lookback + 1), n + 1)
        return snapshot(self.gld[window], self.uso[window])


def made(days: Sequence[date], gld: Sequence[object], uso: Sequence[object]) -> Series:
    """The series of these closes, oldest first."""

    def leg(instrument: InstrumentId, closes: Sequence[object]) -> tuple[Bar, ...]:
        pairs = zip(days, closes, strict=True)
        return tuple(bar(instrument, day, Decimal(str(close))) for day, close in pairs)

    return Series(leg(GLD, gld), leg(USO, uso))


def synthetic(sessions: int, seed: int, first: date = date(2010, 1, 4)) -> Series:
    """A seeded random walk for GLD, and USO a multiple of it plus a spread that returns."""
    draw = random.Random(seed)
    start = DAYS.index(first)
    gold, apart, gld, uso = 100.0, 0.0, [], []
    for _ in range(sessions):
        gold += draw.gauss(0, 1)
        apart = 0.85 * apart + draw.gauss(0, 1)
        gld.append(f"{gold:.2f}")
        uso.append(f"{20 + 0.3 * gold + apart:.2f}")
    return made(DAYS[start : start + sessions], gld, uso)
