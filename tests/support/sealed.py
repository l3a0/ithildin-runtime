"""A sealed quotes partition, in the lake a test reads. Every price is made up."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from lake import journal
from lake.paths import LakePaths
from lake.segment_join import merge_by_name, pinned_order
from lake.unsealed import segment_prefix

from tests.support import lake as lakes
from tests.support.segments import BIDS, DAY

SCHEMA = journal.schema_for(journal.QUOTES_SURFACE)


def part_key(ticker: str, day: date = DAY) -> str:
    return LakePaths(Path("/l")).quotes_partition_path(ticker, day).relative_to("/l").as_posix()


def row(ticker: str, minute: datetime, landed: datetime | None = None, /, **changes: object):
    bid = BIDS.get(ticker, 10.0)
    given = {"bid": bid, "ask": bid + 0.5, "last": 1.0}
    if landed is not None:
        given["fetch_end_ts"] = landed.isoformat()
    return lakes.quote_row(ticker, minute, **given | changes)


def close_row(ticker: str, close: datetime) -> dict[str, object]:
    return row(ticker, close, close_tag="spot_close", bid=7777.0, ask=7778.0)


def write(lake: Path, key: str, table: pa.Table, listed: bool = True) -> str:
    sink = pa.BufferOutputStream()
    pq.write_table(table, sink)
    lakes.rewrite(lake, key, sink.getvalue().to_pybytes())
    if listed:
        lakes.record(lake, key, rows=table.num_rows)
    return key


def partition(
    lake: Path,
    ticker: str,
    *rows: Mapping[str, object],
    day: date = DAY,
    schema: pa.Schema = SCHEMA,
    key: str | None = None,
) -> str:
    """Seal these rows as one ticker-day's partition."""
    table = pa.Table.from_pylist([dict(r) for r in rows], schema=schema)
    return write(lake, key or part_key(ticker, day), table)


def seal(
    lake: Path, live: Path, ticker: str, day: date = DAY, close: datetime | None = None
) -> str:
    """Seal one ticker-day's segments as marketlake's seal joins them, with an equity close."""
    tables = []
    for path in sorted((live / segment_prefix(ticker, day)).glob("*.arrows")):
        with pa.ipc.open_stream(path) as reader:
            tables.append(reader.read_all())
    if close is not None:
        tables.append(pa.Table.from_pylist([close_row(ticker, close)], schema=SCHEMA))
    return write(lake, part_key(ticker, day), pinned_order(merge_by_name(tables), SCHEMA))


def segment(
    live: Path,
    ticker: str,
    minute: datetime,
    landed: datetime | None = None,
    schema: pa.Schema | None = None,
    **changes: object,
) -> str:
    made = row(ticker, minute, landed, **changes)
    return lakes.quotes_segment(live, ticker, minute, made, schema=schema)[0]


def day_rows(ticker: str, first: datetime, last: datetime) -> list[dict[str, object]]:
    out, minute = [], first
    while minute <= last:
        out.append(row(ticker, minute))
        minute += timedelta(minutes=1)
    return out
