"""The types and interfaces in `core`, and the two behaviours they carry.

#22's contract says every type but `Halt` is a frozen dataclass whose mapping
fields are typed `Mapping`, that no type holds a float, that every decimal
declares its places, and that `Halt` is a plain exception.
"""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any, Protocol, get_args, get_origin, get_type_hints

import pytest

from ithildinruntime import core
from ithildinruntime.core import Bar, BarStatus, Halt, InstrumentId, Snapshot
from tests.core_samples import DATA_TYPES, PROTOCOLS, RECORDS, sample

AT = datetime(2026, 11, 2, 20, 40, tzinfo=UTC)
USO = InstrumentId("USO")
GLD = InstrumentId("GLD")


def _walk(hint: Any) -> list[Any]:
    found = [hint]
    for arg in get_args(hint):
        found.extend(_walk(arg))
    return found


def test_core_exports_every_type_and_interface_part_a_names() -> None:
    for name in DATA_TYPES + PROTOCOLS:
        assert name in core.__all__, name
        assert hasattr(core, name), name
    for name in PROTOCOLS:
        assert Protocol in getattr(core, name).__mro__, name


def test_core_leaves_out_what_other_issues_build() -> None:
    for name in ("Journal", "OrderPath", "Monitoring", "reconcile"):
        assert not hasattr(core, name), name


@pytest.mark.parametrize("kind", RECORDS, ids=lambda k: k.__name__)
def test_every_type_is_a_frozen_dataclass(kind: type) -> None:
    assert dataclasses.is_dataclass(kind)
    value = sample(kind)
    field = dataclasses.fields(kind)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(value, field, None)


@pytest.mark.parametrize("kind", RECORDS, ids=lambda k: k.__name__)
def test_no_field_holds_a_float_and_every_decimal_declares_its_places(kind: type) -> None:
    for name, hint in get_type_hints(kind, include_extras=True).items():
        parts = _walk(hint)
        assert float not in parts, f"{kind.__name__}.{name} holds a float"
        assert dict not in parts and list not in parts, f"{kind.__name__}.{name} is mutable"
        for part in parts:
            origin = get_origin(part)
            assert origin not in (dict, list), f"{kind.__name__}.{name} is mutable"
        decimals = [p for p in parts if p is Decimal]
        annotated = [
            p
            for p in parts
            if get_origin(p) is Annotated
            and get_args(p)[0] is Decimal
            and any(isinstance(m, core.Places) for m in p.__metadata__)
        ]
        assert len(decimals) == len(annotated), f"{kind.__name__}.{name} declares no places"


def test_a_records_mappings_and_lists_cannot_be_changed() -> None:
    snapshot = sample(Snapshot)
    with pytest.raises(TypeError):
        snapshot.status[USO] = BarStatus.FRESH  # type: ignore[index]
    with pytest.raises(AttributeError):
        snapshot.segments.append("x")  # type: ignore[attr-defined]
    held = {GLD: Decimal("1.0")}
    portfolio = core.TargetPortfolio(decision_at=AT, strategy="bollinger", shares=held)
    held[GLD] = Decimal("9")
    assert portfolio.shares[GLD] == Decimal("1.0")


def test_instrument_id_is_a_new_type_over_str() -> None:
    assert isinstance(InstrumentId, typing.NewType)
    assert InstrumentId.__supertype__ is str


def test_places_reads_a_fields_declared_places() -> None:
    assert core.places(core.Bar, "close") == 4
    assert core.places(core.TargetPortfolio, "shares") == 9
    assert core.places(core.Verdict, "value") == 9
    with pytest.raises(core.CodecError):
        core.places(core.Bar, "volume")


def _bar(instrument: InstrumentId) -> Bar:
    hints = get_type_hints(Bar, include_extras=True)
    fields = {f.name: sample(hints[f.name]) for f in dataclasses.fields(Bar)}
    fields["instrument"] = instrument
    return Bar(**fields)


def _snapshot(bars: dict, status: dict) -> Snapshot:
    return Snapshot(
        decision_at=AT,
        bars=bars,
        quotes={},
        status=status,
        provisional=False,
        flags={},
        segments=(),
        ledgers={},
    )


def test_require_fresh_returns_when_every_bar_is_fresh() -> None:
    snapshot = _snapshot(
        {USO: (_bar(USO),), GLD: (_bar(GLD),)}, {USO: BarStatus.FRESH, GLD: BarStatus.FRESH}
    )
    assert snapshot.require_fresh() is None


@pytest.mark.parametrize(
    ("bars", "status", "reason"),
    [
        ({}, {USO: BarStatus.MISSING}, "USO missing for the session"),
        ({USO: (_bar(USO),)}, {USO: BarStatus.STALE}, "USO stale for the session"),
        ({}, {}, "the snapshot for 2026-11-02T20:40:00+00:00 is empty"),
        ({}, {USO: BarStatus.FRESH}, "USO is fresh and has no bar"),
        ({USO: ()}, {USO: BarStatus.FRESH}, "USO is fresh and has no bar"),
        ({USO: (_bar(USO),)}, {}, "USO has a bar and no status"),
        (
            {USO: (_bar(USO),), GLD: (_bar(GLD),)},
            {USO: BarStatus.FRESH, GLD: BarStatus.MISSING},
            "GLD missing for the session",
        ),
    ],
)
def test_require_fresh_halts_on_a_gap(bars: dict, status: dict, reason: str) -> None:
    with pytest.raises(Halt) as raised:
        _snapshot(bars, status).require_fresh()
    assert raised.value.reason == reason


def test_halt_is_a_plain_exception_caught_inside_a_context_manager() -> None:
    assert not dataclasses.is_dataclass(Halt)
    assert Halt.__mro__[1] is Exception

    @contextmanager
    def held():
        yield

    with pytest.raises(Halt) as raised:
        with held():
            raise Halt("USO missing for the session")
    assert raised.value.reason == "USO missing for the session"
    assert str(raised.value) == "USO missing for the session"


def test_a_cost_schedule_estimates_a_trade_and_halts_on_a_missing_instrument() -> None:
    schedule = core.CostSchedule(per_share={USO: Decimal("0.0035")}, fixed={USO: Decimal("1.00")})
    assert schedule.estimate(USO, Decimal("-200")) == Decimal("1.7000")
    with pytest.raises(Halt):
        schedule.estimate(GLD, Decimal("1"))


def test_a_journal_entrys_hash_is_the_sha256_of_its_bytes() -> None:
    import hashlib

    entry = core.JournalEntry(
        seq=1,
        format=1,
        built_at=AT,
        body={"kind": "intent", "client_order_id": "a1"},
        prev_hash="0" * 64,
    )
    data = core.encode(entry)
    assert entry.hash() == hashlib.sha256(data).hexdigest()
    assert core.decode(data, core.JournalEntry).hash() == entry.hash()
    changed = dataclasses.replace(entry, seq=2)
    assert changed.hash() != entry.hash()


def test_the_types_module_holds_no_mutable_default() -> None:
    for kind in RECORDS:
        for f in dataclasses.fields(kind):
            assert f.default is dataclasses.MISSING, f"{kind.__name__}.{f.name} has a default"


def test_a_session_date_and_a_mapping_type_are_what_the_diagram_names() -> None:
    hints = get_type_hints(Bar, include_extras=True)
    assert hints["session"] is date
    snapshot_hints = get_type_hints(Snapshot)
    assert get_origin(snapshot_hints["bars"]) is Mapping
    assert types.MappingProxyType is type(sample(Snapshot).bars)
