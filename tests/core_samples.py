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

# The 33 data types and 11 interfaces that #22's part A names.
DATA_TYPES = (
    "Instrument Bar Quote Snapshot Basket Forecast Constraints TargetPortfolio Book "
    "OrderIntent Fill Ack BrokerOrder FoundOrder BrokerRead JournalAck Verdict CostSchedule "
    "BrokerAccount Valuation GateInputs SendOutcome BlobRef StoredBlob BlobHeader "
    "DecisionResult HaltRecord RestartRecord Heartbeat Lease Plan JournalEntry Halt"
).split()
PROTOCOLS = (
    "DataSource Clock Timer Queue Broker AlphaModel RiskModel CostModel RiskGate BlobStore Alerts"
).split()

RECORDS = [getattr(core, name) for name in DATA_TYPES if name != "Halt"] + [core.PlanDecision]


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
        return hint(
            **{
                f.name: sample(hints[f.name], empty=empty, depth=depth + 1)
                for f in dataclasses.fields(hint)
            }
        )
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
