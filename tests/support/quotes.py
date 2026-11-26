"""A quote a test can read, and the fields of a snapshot that read none.

Every price, age and spread here is made up.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from ithildinruntime.core import InstrumentId, Quote

# What a snapshot holds beside its quotes where its decision read none.
NONE_READ: dict[str, object] = {"partitions": (), "rows": {}, "faults": (), "quotes_read_at": None}
MAX_AGE = 90
MAX_SPREAD = Decimal("0.02")


def read_at(at: datetime) -> dict[str, object]:
    """What a snapshot holds beside its quotes where its decision read them at `at`."""
    return {"provisional": True, "partitions": (), "rows": {}, "faults": (), "quotes_read_at": at}


def quote(
    instrument: str,
    bid: object,
    ask: object,
    decision_at: datetime,
    *,
    age: timedelta = timedelta(seconds=59),
    **changes: object,
) -> Quote:
    """A quote with no fault at `decision_at`, whose last sale is no price of either side.

    Its two sides were stamped `age` before the decision and captured a second later.
    """
    stamped = decision_at - age
    millis = str(int(stamped.timestamp() * 1000))
    fields: dict[str, object] = {
        "instrument": InstrumentId(instrument),
        "bid": Decimal(str(bid)),
        "ask": Decimal(str(ask)),
        "last": Decimal(str(bid)) * 3,
        "volume": 4,
        "bid_at": stamped,
        "ask_at": stamped,
        "last_at": stamped - timedelta(hours=1),
        "fetched_at": stamped,
        "captured_at": stamped + timedelta(seconds=1),
        "realtime": True,
        "status": "Normal",
        "price_kind": "consolidated quote",
        "last_kind": "consolidated sale",
        "venues": "consolidated",
        "volume_counted": "consolidated in shares",
        "normal_status": "Normal",
        "max_age": MAX_AGE,
        "max_spread_share": MAX_SPREAD,
        "received": {
            "bid": str(bid),
            "ask": str(ask),
            "bid_time": millis,
            "ask_time": millis,
            "realtime": "True",
        },
        "excess_places": (),
    }
    return Quote(**fields | changes)  # type: ignore[arg-type]
