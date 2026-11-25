"""The rules a name and a SHA-256 meet to become part of a key, and the names modules share.

`keys.py` builds every key from names such as a deployment, a stage or a
strategy, and from SHA-256 digests. `records.py` checks a header's strategy
names and hashes by the same rules, and cannot import `keys.py`, which
imports it. So the rules live here, once.

Data's stage and its snapshot are named here because the stages that read
them import no `data`. A worker that imported it for two names would load
marketlake's reader and pandas with it. The alpha stage's two output files are
named here too, because portfolio construction reads the forecast and no
worker module imports another. So are cost's schedule, which it reads beside
it, and the parts that begin an alpha and a cost stage's name. So are the risk
stage and its constraints, which portfolio construction and execution read,
execution's recover stage and the three of its files risk reads, and the
portfolio stage and its two files, which execution reads. So are the daily
interval a bar is filed under and the word a price-kind field holds where its
source states nothing, which data writes and the gate reads.
"""

from __future__ import annotations

import re

__all__ = [
    "ALPHA_COMPONENT",
    "BOOK_OUTPUT",
    "CAPS_OUTPUT",
    "CONSTRAINTS_OUTPUT",
    "COST_COMPONENT",
    "DAILY",
    "DATA_STAGE",
    "FORECAST_OUTPUT",
    "PORTFOLIO_STAGE",
    "RECOVER_STAGE",
    "RISK_STAGE",
    "SCHEDULE_OUTPUT",
    "SIGNALS_OUTPUT",
    "SIZING_OUTPUT",
    "SNAPSHOT_OUTPUT",
    "TARGET_OUTPUT",
    "UNSTATED",
    "VALUATION_OUTPUT",
    "is_name",
    "is_sha256",
]

DATA_STAGE = "data"
SNAPSHOT_OUTPUT = "snapshot"
ALPHA_COMPONENT = "alpha"
FORECAST_OUTPUT = "forecast"
SIGNALS_OUTPUT = "signals"
COST_COMPONENT = "cost"
SCHEDULE_OUTPUT = "schedule"
RISK_STAGE = "risk"
CONSTRAINTS_OUTPUT = "constraints"
RECOVER_STAGE = "execution/recover"
BOOK_OUTPUT = "book"
VALUATION_OUTPUT = "valuation"
CAPS_OUTPUT = "caps"
PORTFOLIO_STAGE = "portfolio"
TARGET_OUTPUT = "target"
SIZING_OUTPUT = "sizing"

DAILY = "1d"
UNSTATED = "unstated"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def is_name(value: object) -> bool:
    """Whether `value` can be part of a key.

    A name starts with a letter or a digit, goes on in letters, digits, `.`,
    `_` and `-`, and never holds `..`, so it cannot climb out of a folder.
    """
    return isinstance(value, str) and _NAME.fullmatch(value) is not None and ".." not in value


def is_sha256(value: object) -> bool:
    """Whether `value` is a SHA-256 written as 64 lower case hex digits."""
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None
