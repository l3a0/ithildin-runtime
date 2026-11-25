"""The marketlake lake every data test reads, built from quantitative-trading's vintages.

docs/architecture/10-quality.md asks a run with no credential to read the
committed vintages. The lake is built once per test session in a temporary
directory, and each test reads a copy of it through the folder bucket, with
the mirror in a second directory, so every test runs the download, the
checksum checks and the check that each reader file is present, as a live
run does. Building it takes six steps.

1. **Fetch the two vintages test 1 pins**, GLD's and USO's from Chan's
   `inputData_ETF.mat`, from quantitative-trading at a fixed commit, with a
   timeout. A fetch that fails, or a digest that differs, fails the test and
   never skips it, on CI and on a laptop with no network alike. The files are
   never committed here, because committing a vendor's data to this public
   repository is a question for the board.
2. **Fix the clock outside marketlake's import window.** `import_vintages`
   refuses a clock inside a session, its margin before the open or Sunday's
   scrub window.
3. **Register GLD and USO** in marketlake's security master and open a capture
   span for each, with a first session after 2012-04-09, which
   `import_vintages` requires.
4. **Record the schema version**, which also creates `manifest.jsonl`.
5. **Write both ledgers and record them in the manifest**, so the tests that
   remove one have something to remove.
6. **Import the two files** with `import_vintages`.

`quotes_segment` adds one capture segment to a lake's journal with
marketlake's own writer, for a day that is not sealed yet.

marketlake's own test helpers sit under its `tests/` folder and are not
installed, so these use its public names and, where a test needs a row
marketlake would only capture, pyarrow directly.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import urllib.request
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from lake import journal
from lake.bar_import import import_vintages
from lake.calendar import MARKET_TZ, ExchangeCalendar
from lake.capture_spans import CaptureSpans, spans_path
from lake.gap import SEGMENT_STAMP_FORMAT
from lake.manifest import append_manifest, manifest_path, sha256_file
from lake.schema_versions import record_schema_version
from lake.security_master import KIND_EQUITY, SecurityMaster, master_path

COMMIT = "19aadbeb9730bf34f724af5688669b6bc208d136"
URL = f"https://raw.githubusercontent.com/l3a0/quantitative-trading/{COMMIT}/data/inputdata_etf/"
# The digests marketlake's `VINTAGES` accepts, each with basis `adjusted`.
VINTAGES = {
    "gld.csv": "81e6295a8028362f82f9e96a89c686ef89991cb23afe42d4f884756019dc9d4c",
    "uso.csv": "94b6521980f64ce80feb5cdffb22168ed44f71eeaa93caca672f316eeda60ec2",
}
FETCH_SECONDS = 30
# A Saturday noon in New York, outside every window `import_vintages` refuses.
CLOCK = datetime(2026, 11, 21, 12, 0, tzinfo=MARKET_TZ)
QUARANTINE = "quarantine.jsonl"
ACTIONS = "actions/corporate_actions.jsonl"
FIRST_SESSION = date(2006, 4, 26)
LAST_SESSION = date(2012, 4, 9)


class _Clock:
    """The clock `import_vintages` and `record_schema_version` read, fixed at `CLOCK`."""

    def now(self) -> datetime:
        return CLOCK

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        pass

    def wait(self, futures, until):  # noqa: ANN001, ANN201
        return set()


def fetch_vintages(directory: Path) -> Path:
    """Download both vintages into `directory`, failing on a fetch or digest that differs."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, digest in VINTAGES.items():
        with urllib.request.urlopen(URL + name, timeout=FETCH_SECONDS) as reply:  # noqa: S310
            data = reply.read()
        found = hashlib.sha256(data).hexdigest()
        if found != digest:
            raise AssertionError(
                f"{name} from quantitative-trading has SHA-256 {found}, not {digest}"
            )
        (directory / name).write_bytes(data)
    return directory


def build_lake(root: Path, vintages: Path) -> Path:
    """A lake at `root` holding both series, both ledgers and a manifest that lists them."""
    root.mkdir(parents=True)
    manifest_path(root).touch()
    clock = _Clock()
    onboard = datetime(2026, 8, 26, 17, 30, tzinfo=MARKET_TZ)
    master, spans = SecurityMaster(), CaptureSpans()
    for ticker in ("GLD", "USO"):
        instrument = master.register(
            kind=KIND_EQUITY,
            capture_start=onboard.astimezone(UTC),
            valid_from=onboard.date(),
            ticker=ticker,
        )
        spans.open_span(instrument, onboard.astimezone(UTC), options=False)
    master_path(root).parent.mkdir(parents=True, exist_ok=True)
    master.write(master_path(root))
    spans.write(spans_path(root))
    record_schema_version(clock=clock, lake_root=root)
    for ledger in (QUARANTINE, ACTIONS):
        path = root / ledger
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        record(root, ledger)
    import_vintages(
        root,
        [vintages / "gld.csv", vintages / "uso.csv"],
        clock=clock,
        calendar=ExchangeCalendar(start=date(2006, 1, 1)),
    )
    return root


def record(root: Path, key: str, rows: int = 0) -> None:
    """Record the file at `key` in the manifest at its current SHA-256."""
    append_manifest(
        root,
        partition=key,
        source="fixture",
        sha256=sha256_file(root / key),
        rows=rows,
        fetched_at=None,
        guard=False,
    )


# The sessions a copy holds partitions for unless a test asks for more: the
# first months of both series and their last months, which every test reads.
KEPT = ((date(2006, 4, 26), date(2006, 8, 31)), (date(2011, 12, 1), date(2012, 4, 9)))


def copy_lake(source: Path, target: Path, kept: tuple[tuple[date, date], ...] = KEPT) -> Path:
    """A copy of a lake, with the partitions of the sessions in `kept` as hard links.

    Every other file is copied, because a test appends to the manifest and the
    ledgers, which a link would append to in the lake it came from too. The
    manifest still lists every partition, so one outside `kept` reads as listed
    and not in the bucket. `rewrite` breaks a partition's link before it
    changes it.
    """
    for path in source.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(source)
        out = target / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        if rel.parts[0] != "bars":
            shutil.copy2(path, out)
            continue
        day = date.fromisoformat(path.stem.removeprefix("date="))
        if any(first <= day <= last for first, last in kept):
            os.link(path, out)
    return target


def rewrite(root: Path, key: str, data: bytes) -> None:
    """Change one file in a copied lake, leaving the lake it was linked from as it was."""
    path = root / key
    path.unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def partition_key(ticker: str, day: date) -> str:
    return f"bars/ticker={ticker}/freq=1d/date={day.isoformat()}.parquet"


def captured(root: Path, ticker: str, day: date, *, fetch_ts: str | None, **prices: float) -> str:
    """Rewrite one session's partition as marketlake writes a captured one, and record it.

    A captured partition carries no provenance keys, which `load_bars` reads as
    `origin` captured and `basis` raw. Its row keeps the imported row's other
    values, with `fetch_ts` and any price given here replaced.
    """
    key = partition_key(ticker, day)
    table = pq.read_table(root / key)
    row = table.to_pylist()[0]
    row.update(fetch_ts=fetch_ts, fetch_end_ts=fetch_ts, **prices)
    schema = table.schema.remove_metadata()
    out = pa.Table.from_pylist([row], schema=schema)
    sink = pa.BufferOutputStream()
    pq.write_table(out, sink)
    rewrite(root, key, sink.getvalue().to_pybytes())
    record(root, key, rows=1)
    return key


def with_row(root: Path, ticker: str, day: date, **changes: object) -> str:
    """Rewrite one imported partition with its row's values changed, and record it."""
    key = partition_key(ticker, day)
    table = pq.read_table(root / key)
    row = table.to_pylist()[0]
    row.update(changes)
    out = pa.Table.from_pylist([row], schema=table.schema)
    sink = pa.BufferOutputStream()
    pq.write_table(out, sink)
    rewrite(root, key, sink.getvalue().to_pybytes())
    record(root, key, rows=1)
    return key


def doubled(root: Path, ticker: str, day: date) -> str:
    """Rewrite one partition holding its row twice, and record it."""
    key = partition_key(ticker, day)
    table = pq.read_table(root / key)
    sink = pa.BufferOutputStream()
    pq.write_table(pa.concat_tables([table, table]), sink)
    rewrite(root, key, sink.getvalue().to_pybytes())
    record(root, key, rows=2)
    return key


def quote_row(ticker: str, minute: datetime, /, **changes: object) -> dict[str, object]:
    """One captured quote, landed a second after its minute, with any column changed.

    A column not named here is null. The prices are made up.
    """
    landed = minute + timedelta(seconds=1)
    millis = int(minute.timestamp() * 1000)
    return {
        "snap_ts": minute.isoformat(),
        "fetch_ts": minute.isoformat(),
        "fetch_end_ts": landed.isoformat(),
        "vendor_quote_ts": minute.isoformat(),
        "bid_time": millis,
        "ask_time": millis,
        "trade_time": millis,
        "ticker": ticker,
        "bid": 60.0,
        "ask": 60.5,
        "last": 60.25,
        "bid_size": 1,
        "ask_size": 2,
        "last_size": 3,
        "total_volume": 4,
        "security_status": "Normal",
        "realtime": True,
        "close_tag": None,
        "session_phase": None,
        "row_kind": journal.ROW_KIND_DATA,
        "suspect": False,
        "schema_version": journal.SCHEMA_VERSION,
        **changes,
    }


def gap_row(ticker: str, minute: datetime) -> dict[str, object]:
    """The marker capture writes for a minute it could not read."""
    return {
        "snap_ts": minute.isoformat(),
        "ticker": ticker,
        "row_kind": journal.ROW_KIND_GAP,
        "error_class": "timeout",
        "suspect": False,
        "schema_version": journal.SCHEMA_VERSION,
    }


def quotes_segment(
    root: Path,
    ticker: str,
    minute: datetime,
    *rows: Mapping[str, object],
    schema: pa.Schema | None = None,
) -> tuple[str, str]:
    """Write one closed quotes segment, and give its lake-relative path and SHA-256.

    It goes through `lake.journal`'s `schema_for`, `segment_path` and
    `SegmentWriter`, so the file is one marketlake's own writer wrote. `schema`
    retypes a column.
    """
    schema = schema or journal.schema_for(journal.QUOTES_SURFACE)
    path = journal.segment_path(
        root,
        journal.QUOTES_SURFACE,
        ticker,
        minute.astimezone(MARKET_TZ).date(),
        minute.strftime(SEGMENT_STAMP_FORMAT),
        1,
    )
    with journal.SegmentWriter(path, schema) as writer:
        writer.write_cycle(pa.Table.from_pylist([dict(row) for row in rows], schema=schema))
    return path.relative_to(root).as_posix(), sha256_file(path)
