"""The live bucket a test reads quotes from, and what it counts.

Each segment is one marketlake's own writer wrote, through `lakes.quotes_segment`,
in a folder laid out as the live prefix is. The fixture day is the session after
the built lake's last. Every price, maximum and kind is made up.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
from lake import journal
from lake.gap import SEGMENT_STAMP_FORMAT

from ithildinruntime.adapters import FolderBucket
from ithildinruntime.core import BucketObject, InstrumentId, load_sessions
from ithildinruntime.data import QuoteLimits
from ithildinruntime.data.reference import QUOTE_SOURCE, QuoteKind, QuoteKindRow, QuoteKinds
from tests.support import lake as lakes

GLD, USO, SPY = InstrumentId("GLD"), InstrumentId("USO"), InstrumentId("SPY")
TABLE = load_sessions()
DAYS = sorted(TABLE.sessions)
DAY = DAYS[DAYS.index(lakes.LAST_SESSION) + 1]
SESSION = TABLE.session(DAY)
AT = SESSION.close - timedelta(minutes=20)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
KIND = QuoteKind("consolidated quote", "consolidated sale", "consolidated", "shares", "Normal")
KINDS = QuoteKinds((QuoteKindRow(DAY, QUOTE_SOURCE, DAY, DAY, KIND, "a test"),))
LIMITS = {i: QuoteLimits(90, Decimal("0.02")) for i in (GLD, USO)}
# Each ticker's own bid, so a quote read of the other's row answers otherwise.
BIDS = {"GLD": 160.0, "USO": 39.0, "SPY": 140.0}


def landed(back: int) -> datetime:
    """When the cycle `back` minutes before the decision landed, a second after it started."""
    return AT - back * MINUTE + SECOND


def key_of(ticker: str, back: int) -> str:
    """The key of the segment marketlake writes for the cycle `back` minutes before the decision."""
    stamp = (AT - back * MINUTE).strftime(SEGMENT_STAMP_FORMAT)
    path = journal.segment_path(Path("/"), journal.QUOTES_SURFACE, ticker, DAY, stamp, 1)
    return path.relative_to("/").as_posix()


def capture(
    live: Path, ticker: str, back: int, *, schema: pa.Schema | None = None, **changes: object
) -> str:
    """One segment of one quote, of the cycle `back` minutes before the decision, by its key.

    It takes the place of one a test wrote before.
    """
    (live / key_of(ticker, back)).unlink(missing_ok=True)
    minute = AT - back * MINUTE
    bid = BIDS.get(ticker, 10.0)
    row = lakes.quote_row(ticker, minute, **{"bid": bid, "ask": bid + 0.5, "last": 1.0} | changes)
    return lakes.quotes_segment(live, ticker, minute, row, schema=schema)[0]


def gap(live: Path, ticker: str, back: int) -> str:
    minute = AT - back * MINUTE
    return lakes.quotes_segment(live, ticker, minute, lakes.gap_row(ticker, minute))[0]


class Live:
    """A bucket over a folder that counts each call, and can answer one its own way."""

    def __init__(self, root: Path) -> None:
        self.bucket = FolderBucket(root)
        self.lists: list[str] = []
        self.reads: list[tuple[str, str | None]] = []
        # What each call was and, after it, each report.
        self.events: list[str] = []
        # Answers a test plants for a key or a prefix, each used once, in order. An
        # exception is raised, and a function is handed the bucket's own answer.
        self.plant: dict[str, list[Exception | Callable[[BucketObject], BucketObject]]] = {}

    def list(self, prefix: str) -> list[str]:
        self.lists.append(prefix)
        self.events.append("call")
        if planted := self.plant.get(prefix):
            raise planted.pop(0)  # type: ignore[misc]
        return self.bucket.list(prefix)

    def read(self, key: str, version: str | None = None) -> BucketObject:
        self.reads.append((key, version))
        self.events.append("call")
        got = self.bucket.read(key, version)
        if planted := self.plant.get(key):
            answer = planted.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer(got)
        return got

    @property
    def keys(self) -> list[str]:
        return [key for key, _ in self.reads]


def flipped(got: BucketObject) -> BucketObject:
    """One byte changed in the middle, beside the digest and version stored with the object."""
    data = bytearray(got.data)
    data[len(data) // 2] ^= 1
    return replace(got, data=bytes(data))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
