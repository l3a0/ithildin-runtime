"""The data module's two tracked files: the security master and the price-kind contract.

Both ship in the release image, so the image's digest records what each said
when a bar was read. Both are only ever added to, which tests/test_data_files.py
holds by pinning every row by hand, since CI's checkout holds no history to
compare with.

1. **The security master** maps each instrument's runtime id to marketlake's
   ticker, one row per span. A rename or a delisting is a new row, so a
   backtest's universe keeps the members that later failed. A decision that
   reads an instrument on a session no row covers halts.
2. **The price-kind contract** states, for each source, the kind of each
   price, the venues it covers, any delay and how volume is counted, with the
   sessions it covers. A source is an imported row's `source`, or
   `CAPTURE_SOURCE` for a row marketlake captured. The fields are copied into
   each bar when it is read, so a later row never changes what a recorded bar
   says. A source with no row, or a session outside its row's span, reads
   `unstated` throughout.

In both files each row carries the date it was `added`. A row that corrects
an earlier one is a new row with a later `added` date over the sessions it
corrects, and on those sessions it supersedes every row added before it, so
the earlier row stays in the file as it was. Two rows added on one date that
cover one session are refused.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ithildinruntime.core import Halt, InstrumentId

__all__ = [
    "CAPTURE_SOURCE",
    "MASTER",
    "PRICE_KINDS",
    "UNSTATED",
    "Kind",
    "KindRow",
    "MasterRow",
    "PriceKinds",
    "SecurityMaster",
    "load_master",
    "load_price_kinds",
]

MASTER = Path(__file__).with_name("security_master.csv")
PRICE_KINDS = Path(__file__).with_name("price_kinds.csv")
UNSTATED = "unstated"
# The source a captured row reads as in the price-kind contract. marketlake
# writes no `source` on a row it captured itself.
CAPTURE_SOURCE = "marketlake:capture"


@dataclass(frozen=True)
class MasterRow:
    added: date
    id: InstrumentId
    ticker: str
    first_session: date
    last_session: date | None

    def covers(self, day: date) -> bool:
        return self.first_session <= day and (self.last_session is None or day <= self.last_session)


@dataclass(frozen=True)
class SecurityMaster:
    rows: tuple[MasterRow, ...]

    def ticker(self, instrument: InstrumentId, day: date) -> str:
        """marketlake's ticker for `instrument` on `day`, halting when no row covers it."""
        found = _latest([r for r in self.rows if r.id == instrument and r.covers(day)])
        if not found:
            raise Halt(
                f"{instrument} has no security master row valid on {day.isoformat()}, "
                "so no ticker names it"
            )
        if len(found) > 1:
            raise Halt(f"{instrument} has {len(found)} security master rows on {day.isoformat()}")
        return found[0].ticker


@dataclass(frozen=True)
class Kind:
    """What a source states about one session's prices and volume."""

    price_kind: str
    venues: str
    delay: str
    volume_counted: str

    @property
    def stated(self) -> bool:
        return UNSTATED not in (self.price_kind, self.venues, self.delay, self.volume_counted)


NOT_STATED = Kind(UNSTATED, UNSTATED, UNSTATED, UNSTATED)


@dataclass(frozen=True)
class KindRow:
    added: date
    source: str
    first_session: date
    last_session: date
    kind: Kind
    read_from: str


@dataclass(frozen=True)
class PriceKinds:
    rows: tuple[KindRow, ...]

    def kind(self, source: str, day: date) -> Kind:
        """What `source` states for `day`, or `unstated` throughout where it states nothing."""
        found = _latest(
            [
                r
                for r in self.rows
                if r.source == source and r.first_session <= day <= r.last_session
            ]
        )
        if len(found) > 1:
            raise Halt(f"{source} has {len(found)} price-kind rows on {day.isoformat()}")
        return found[0].kind if found else NOT_STATED


def _latest[R: (MasterRow, KindRow)](rows: list[R]) -> list[R]:
    """The rows added last, which supersede every row added before them."""
    if not rows:
        return rows
    newest = max(r.added for r in rows)
    return [r for r in rows if r.added == newest]


def _rows(path: Path, header: Sequence[str]) -> list[dict[str, str]]:
    text = path.read_text(encoding="utf-8")
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    if reader.fieldnames != list(header):
        raise Halt(f"{path.name} has the header {reader.fieldnames}, not {list(header)}")
    return list(reader)


def _day(text: str, where: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise Halt(f"{where} holds {text!r}, which is no date") from None


def load_master(path: Path = MASTER) -> SecurityMaster:
    rows = []
    header = ("added", "id", "ticker", "first_session", "last_session")
    for n, raw in enumerate(_rows(path, header), 1):
        where = f"{path.name} row {n}"
        last = raw["last_session"]
        row = MasterRow(
            added=_day(raw["added"], where),
            id=InstrumentId(raw["id"]),
            ticker=raw["ticker"],
            first_session=_day(raw["first_session"], where),
            last_session=_day(last, where) if last else None,
        )
        if not row.id or not row.ticker:
            raise Halt(f"{where} names no id or no ticker")
        if row.last_session is not None and row.last_session < row.first_session:
            raise Halt(f"{where} ends before it starts")
        for other in rows:
            if other.id == row.id and other.added == row.added and _overlap(other, row):
                raise Halt(f"{where} overlaps another row of {row.id} added the same day")
        rows.append(row)
    return SecurityMaster(tuple(rows))


def _overlap(a: MasterRow, b: MasterRow) -> bool:
    a_end = a.last_session or date.max
    b_end = b.last_session or date.max
    return a.first_session <= b_end and b.first_session <= a_end


_KIND_HEADER = (
    "added",
    "source",
    "first_session",
    "last_session",
    "price_kind",
    "venues",
    "delay",
    "volume_counted",
    "read_from",
)


def load_price_kinds(path: Path = PRICE_KINDS) -> PriceKinds:
    rows: list[KindRow] = []
    for n, raw in enumerate(_rows(path, _KIND_HEADER), 1):
        where = f"{path.name} row {n}"
        row = KindRow(
            added=_day(raw["added"], where),
            source=raw["source"],
            first_session=_day(raw["first_session"], where),
            last_session=_day(raw["last_session"], where),
            kind=Kind(raw["price_kind"], raw["venues"], raw["delay"], raw["volume_counted"]),
            read_from=raw["read_from"],
        )
        kind = row.kind
        if not row.source or not all(
            (kind.price_kind, kind.venues, kind.delay, kind.volume_counted)
        ):
            raise Halt(f"{where} leaves a field empty, where `unstated` says the source is silent")
        if row.last_session < row.first_session:
            raise Halt(f"{where} ends before it starts")
        for other in rows:
            if (
                other.source == row.source
                and other.added == row.added
                and other.first_session <= row.last_session
                and row.first_session <= other.last_session
            ):
                raise Halt(
                    f"{where} covers a session another row of {row.source} "
                    "added the same day covers"
                )
        rows.append(row)
    return PriceKinds(tuple(rows))
