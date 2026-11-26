"""A marketlake lake of made-up bars in several series per fund.

Every price is drawn from a seeded generator and none is a vendor's. The lake
is built by marketlake's own import, so each partition carries the provenance
a real import writes, and a captured bar is written as marketlake writes one.
"""

from __future__ import annotations

import csv
import hashlib
import io
import random
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

import pyarrow as pa
import pyarrow.parquet as pq
from lake.bar_import import Vintage, import_vintages
from lake.calendar import MARKET_TZ, ExchangeCalendar
from lake.capture_spans import CaptureSpans, spans_path
from lake.loader import BASIS_KEY, SOURCE_AS_OF_KEY, SOURCE_KEY
from lake.manifest import manifest_path
from lake.schema_versions import record_schema_version
from lake.security_master import KIND_EQUITY, SecurityMaster, master_path

from ithildinruntime.core import load_sessions
from ithildinruntime.data.reference import CAPTURE_SOURCE, Kind, KindRow, PriceKinds
from tests.support import lake as lakes

TABLE = load_sessions()
DAYS = sorted(TABLE.sessions)
ONE, TWO = "made-up:one-{}.csv", "made-up:two-{}.csv"
STATED = Kind("official close", "consolidated", "none", "consolidated in shares")
# When marketlake's sweep fetches a session's daily bar, after its close. A late fetch lands later.
SWEEP = time(18, 30)
# A Friday, so that the first session a capture span covers is 2026-11-02.
ONBOARD = datetime(2026, 10, 30, 17, 30, tzinfo=MARKET_TZ)


class Made(NamedTuple):
    """One imported series of one fund: its source and date, and its closes by session."""

    ticker: str
    source: str
    as_of: date
    closes: Mapping[date, Decimal]


def days(first: date, last: date) -> list[date]:
    return [d for d in DAYS if first <= d <= last]


def walk(ds: Iterable[date], seed: int, start: float, vol: float = 0.01) -> dict[date, Decimal]:
    """Closes by session, at whole cents."""
    draw, price, out = random.Random(seed), start, {}
    for d in ds:
        out[d] = Decimal(f"{price:.2f}")
        price = max(1.0, price * (1 + draw.gauss(0, vol)))
    return out


def build(
    root: Path,
    series: Sequence[Made],
    captured: Mapping[str, Mapping[date, Decimal]] | None = None,
) -> Path:
    """A lake at `root` of these series, and these captured bars."""
    root.mkdir(parents=True)
    manifest_path(root).touch()
    clock = lakes._Clock()
    master, spans = SecurityMaster(), CaptureSpans()
    for ticker in sorted({s.ticker for s in series}):
        instrument = master.register(
            kind=KIND_EQUITY,
            capture_start=ONBOARD.astimezone(UTC),
            valid_from=ONBOARD.date(),
            ticker=ticker,
        )
        spans.open_span(instrument, ONBOARD.astimezone(UTC), options=False)
    master_path(root).parent.mkdir(parents=True, exist_ok=True)
    master.write(master_path(root))
    spans.write(spans_path(root))
    record_schema_version(clock=clock, lake_root=root)
    for ledger in (lakes.QUARANTINE, lakes.ACTIONS):
        path = root / ledger
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        lakes.record(root, ledger)
    sources = root.parent / f"{root.name}-csv"
    sources.mkdir()
    vintages, files = {}, []
    for n, made in enumerate(series):
        data = _csv(made.ticker, made.closes)
        sha = hashlib.sha256(data).hexdigest()
        path = sources / f"{n}.csv"
        path.write_bytes(data)
        files.append(path)
        vintages[sha] = Vintage(sha, made.ticker, made.source, made.as_of, "adjusted")
    calendar = ExchangeCalendar(start=date(2006, 1, 1))
    for path in files:
        # One call refuses two files of one ticker.
        import_vintages(root, [path], clock=clock, calendar=calendar, vintages=vintages)
    for ticker, closes in (captured or {}).items():
        template = next((root / f"bars/ticker={ticker}/freq=1d").glob("*.parquet"))
        for day, close in closes.items():
            _capture(root, ticker, day, close, template)
    return root


def _csv(ticker: str, closes: Mapping[date, Decimal]) -> bytes:
    """The closes in the form marketlake imports, with no volume."""
    buffer = io.StringIO()
    out = csv.writer(buffer, lineterminator="\n")
    out.writerow(["Price", "Open", "High", "Low", "Close"])
    out.writerow(["Ticker"] + [ticker] * 4)
    out.writerow(["Date", "", "", "", ""])
    for d, c in closes.items():
        out.writerow([d.isoformat(), c, c * Decimal("1.01"), c * Decimal("0.99"), c])
    return buffer.getvalue().encode()


def _capture(root: Path, ticker: str, day: date, close: Decimal, template: Path) -> None:
    """Write one session's partition with no provenance keys, as marketlake captures one."""
    table = pq.read_table(template)
    row = table.to_pylist()[0]
    stamp = datetime.combine(day, SWEEP, MARKET_TZ).astimezone(UTC).isoformat()
    price = float(close)
    row.update(
        bar_ts=datetime(day.year, day.month, day.day, tzinfo=MARKET_TZ).astimezone(UTC).isoformat(),
        open=price,
        high=price * 1.01,
        low=price * 0.99,
        close=price,
        volume=1000,
        fetch_ts=stamp,
        fetch_end_ts=stamp,
    )
    made = pa.Table.from_pylist([row], schema=table.schema.remove_metadata())
    sink = pa.BufferOutputStream()
    pq.write_table(made, sink)
    key = lakes.partition_key(ticker, day)
    lakes.rewrite(root, key, sink.getvalue().to_pybytes())
    lakes.record(root, key, rows=1)


def of_series(
    root: Path,
    ticker: str,
    day: date,
    *,
    basis: str | None = None,
    source: str | None = None,
    as_of: str | None = None,
    undated: bool = False,
) -> None:
    """Rewrite one imported partition with part of its series changed or undated, and record it."""
    key = lakes.partition_key(ticker, day)
    table = pq.read_table(root / key)
    written = dict(table.schema.metadata)
    for name, value in ((BASIS_KEY, basis), (SOURCE_KEY, source), (SOURCE_AS_OF_KEY, as_of)):
        if value is not None:
            assert written[name] != value.encode()
            written[name] = value.encode()
    if undated:
        del written[SOURCE_AS_OF_KEY]
    sink = pa.BufferOutputStream()
    pq.write_table(table.replace_schema_metadata(written), sink)
    lakes.rewrite(root, key, sink.getvalue().to_pybytes())
    lakes.record(root, key, rows=1)


def kinds(*sources: str) -> PriceKinds:
    """Rows that state every kind of each source and of capture, whatever the session."""
    return PriceKinds(
        tuple(
            KindRow(date(2026, 11, 25), s, DAYS[0], DAYS[-1], STATED, "made up")
            for s in (*sources, CAPTURE_SOURCE)
        )
    )


KINDS = kinds(*(s.format(t) for s in (ONE, TWO) for t in ("GLD", "USO")))
