"""Sample values for every type in `core`, built from each field's type.

A test that writes each type and reads it back takes its values from here, so
a field added to a type is covered without a test naming it.
"""

from __future__ import annotations

import dataclasses
import types
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Union, get_args, get_origin, get_type_hints

from ithildinruntime import core

# The data types and interfaces that #22's part A names, less what a later part replaced.
DATA_TYPES = (
    "Instrument Bar Quote Snapshot Basket Forecast Constraints TargetPortfolio Book "
    "OrderIntent Fill Ack BrokerOrder FoundOrder JournalAck Verdict CostSchedule "
    "AccountRead Valuation GateInputs SendOutcome BlobRef StoredBlob BlobHeader "
    "DecisionResult HaltRecord RestartRecord Heartbeat Lease Plan JournalEntry Halt"
).split()
PROTOCOLS = (
    "DataSource Clock Timer Queue Broker AlphaModel RiskModel CostModel RiskGate BlobStore Alerts"
).split()

RECORDS = [getattr(core, name) for name in DATA_TYPES if name != "Halt"] + [
    core.PlanDecision,
    core.CostTerm,
    core.Correction,
    core.OrderEntry,
    core.OrderRecord,
    core.Signal,
    core.ForecastSet,
    core.HardCaps,
    core.PastLimit,
    core.LossReading,
    core.Sizing,
    core.ReceivedPage,
    core.PositionsRead,
    core.OrdersRead,
    core.FillsRead,
    core.CashRead,
    core.RecordsRead,
    core.Segment,
]


def _strip(hint: Any) -> Any:
    while True:
        if get_origin(hint) is Annotated:
            hint = get_args(hint)[0]
        elif hasattr(hint, "__supertype__"):
            hint = hint.__supertype__
        else:
            return hint


def sample(hint: Any, *, empty: bool = False, depth: int = 0) -> Any:
    """A value of `hint`. With `empty`, every optional field is None and every collection empty."""
    hint = _strip(hint)
    if get_origin(hint) in (Union, types.UnionType):
        if empty:
            return None
        (hint,) = [arm for arm in get_args(hint) if arm is not type(None)]
        return sample(hint, depth=depth)
    origin = get_origin(hint)
    if hint is object:
        return None if empty else {"kind": "fill", "ids": ["a", "b"], "n": 7}
    if origin is tuple:
        if empty:
            return ()
        (item,) = [a for a in get_args(hint) if a is not Ellipsis]
        return (sample(item, depth=depth + 1), sample(item, depth=depth + 1))
    if origin is Mapping:
        if empty:
            return {}
        key, item = get_args(hint)
        key = _strip(key)
        keys = (
            [key.FRESH]
            if isinstance(key, type) and issubclass(key, Enum)
            else ["\U0001f600GLD", "\ue000USO"]
        )
        return {k: sample(item, depth=depth + 1) for k in keys}
    if dataclasses.is_dataclass(hint):
        hints = get_type_hints(hint, include_extras=True)
        fields = {
            f.name: sample(hints[f.name], empty=empty, depth=depth + 1)
            for f in dataclasses.fields(hint)
        }
        if hint is core.BlobHeader:
            # A header names at least one strategy, each by a key name and a SHA-256.
            fields["config_hashes"] = {"chan": "0" * 64, "reversal.2": "f" * 64}
        if hint is core.Basket:
            # A basket names at least one leg.
            fields["legs"] = sample(hints["legs"], depth=depth + 1)
        if hint is core.Forecast:
            # A forecast bounds its direction, its horizon and its confidence.
            fields |= {"direction": -1, "horizon_sessions": 2, "confidence": Decimal("0.00004")}
        if hint is core.ForecastSet:
            # A forecast set holds at least one forecast, and signals of different names.
            (forecast, _), (signal, _) = (
                sample(hints[name], depth=depth + 1) for name in ("forecasts", "signals")
            )
            fields["forecasts"] = (forecast, forecast)
            fields["signals"] = (signal, dataclasses.replace(signal, name="z_score"))
        if hint is core.Valuation:
            # A valuation's value is its cash and its positions at their marks.
            fields["value"] = fields["cash"] = Decimal("-0.0004")
            fields["positions"] = {i: Decimal(0) for i in fields["marks"]}
        if "received" in fields:
            # A broker read holds at least one page.
            fields["received"] = sample(hints["received"], depth=depth + 1)
        if hint is core.PastLimit:
            # A past limit is one of three, at or over its bound.
            fields["limit"] = core.Limit.MAX_NET
        if hint is core.Sizing and not empty:
            # A target is cut by one or more of three limits, in the gate's order.
            fields["cut"] = (core.Limit.MAX_POSITION, core.Limit.MAX_NET)
        if hint is core.LossReading:
            # A loss past its bound is at or over it.
            fields["past"] = True
        if hint is core.Verdict:
            # A verdict agrees with itself: one refused on net, or with nothing set, approved.
            loss = core.LossReading(Decimal(1), Decimal(2), False, date(2026, 11, 2))
            fields |= {"loss": loss, "missing_term": None, "limit": None, "worst_loss": Decimal(1)}
            fields["stress_move"] = Decimal("0.123456789")
            if not empty:
                past = core.PastLimit(core.Limit.MAX_NET, Decimal(60), Decimal(50))
                fields |= {"approved": False, "limit": core.Limit.MAX_NET, "past": (past,)}
        if hint is core.Snapshot and not empty:
            # A snapshot files each bar under its own instrument and interval.
            bar = sample(core.Bar, depth=depth + 1)
            fields["bars"] = {bar.instrument: {bar.interval: (bar, bar)}}
            quote = sample(core.Quote, depth=depth + 1)
            fields["quotes"] = {quote.instrument: quote}
        if hint is core.Snapshot:
            # A snapshot that read no quotes is not provisional, and says when it read none.
            fields["provisional"] = not empty
            # One that read segments names no sealed partition.
            fields["partitions"] = ()
        return hint(**fields)
    if isinstance(hint, type) and issubclass(hint, Enum):
        return list(hint)[-1]
    if hint is bool:
        return True
    if hint is int:
        return 2**53 - 1 if depth % 2 else -7
    if hint is str:
        return "line one\r\nline two \u00e9 \U0001f600"
    if hint is Decimal:
        return Decimal("-0.00004") if depth % 2 else Decimal("123.45678")
    if hint is datetime:
        return datetime(2026, 11, 27, 17, 40, 0, 123456, tzinfo=UTC)
    if hint is date:
        return date(2026, 11, 27)
    if hint is bytes:
        return b"\xff\x00 not utf-8" if depth % 2 else b'{"qty": "1.10"}\r\n'
    raise TypeError(f"no sample for {hint}")
