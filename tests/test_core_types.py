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
    inner = {USO: Decimal("1")}
    book = core.Book(
        positions_by_strategy={"bollinger": inner},
        open_orders_by_strategy={},
        cash=Decimal("0"),
        platform_realised_pnl=Decimal("0"),
        replayed_through=0,
    )
    inner[USO] = Decimal("5")
    assert book.positions_by_strategy["bollinger"][USO] == Decimal("1")
    with pytest.raises(TypeError):
        book.positions_by_strategy["bollinger"][USO] = Decimal("5")  # type: ignore[index]


def test_instrument_id_is_a_new_type_over_str() -> None:
    assert isinstance(InstrumentId, typing.NewType)
    assert InstrumentId.__supertype__ is str


@pytest.mark.parametrize(
    ("kind", "field", "n"),
    [
        (core.Bar, "close", 4),
        (core.TargetPortfolio, "shares", 9),
        (core.Book, "cash", 4),
        (core.CostSchedule, "per_share", 8),
        (core.Forecast, "confidence", 8),
        (core.Verdict, "value", 9),
    ],
)
def test_each_kind_of_decimal_declares_places_that_never_shrink(kind, field, n) -> None:
    """A price 4, a quantity 9, money 4, a rate 8 and a statistic 8, as records.py says."""
    assert core.places(kind, field) == n


def test_places_refuses_a_field_that_is_no_decimal() -> None:
    with pytest.raises(core.CodecError):
        core.places(core.Bar, "volume")


def _bar(instrument: InstrumentId, interval: str = "1d") -> Bar:
    hints = get_type_hints(Bar, include_extras=True)
    fields = {f.name: sample(hints[f.name]) for f in dataclasses.fields(Bar)}
    fields["instrument"] = instrument
    fields["interval"] = interval
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


def _daily(instrument: InstrumentId) -> dict:
    return {"1d": (_bar(instrument),)}


DAILY = ["1d"]


def test_require_fresh_returns_when_every_bar_is_fresh() -> None:
    snapshot = _snapshot(
        {USO: _daily(USO), GLD: _daily(GLD)}, {USO: BarStatus.FRESH, GLD: BarStatus.FRESH}
    )
    assert snapshot.require_fresh({USO: DAILY, GLD: DAILY}) is None
    assert snapshot.require_fresh({USO: DAILY}) is None


@pytest.mark.parametrize(
    ("bars", "status", "needed", "reason"),
    [
        ({}, {USO: BarStatus.MISSING}, {USO: DAILY}, "USO missing for the session"),
        ({USO: _daily(USO)}, {USO: BarStatus.STALE}, {USO: DAILY}, "USO stale for the session"),
        ({}, {}, {USO: DAILY}, "the snapshot for 2026-11-02T20:40:00+00:00 is empty"),
        ({}, {USO: BarStatus.FRESH}, {USO: DAILY}, "USO is fresh and has no bar"),
        ({USO: {}}, {USO: BarStatus.FRESH}, {USO: DAILY}, "USO is fresh and has no bar"),
        ({USO: {"1d": ()}}, {USO: BarStatus.FRESH}, {USO: DAILY}, "USO is fresh and has no bar"),
        ({USO: _daily(USO)}, {}, {USO: DAILY}, "USO has a bar and no status"),
        (
            {USO: _daily(USO), GLD: _daily(GLD)},
            {USO: BarStatus.FRESH, GLD: BarStatus.MISSING},
            {USO: DAILY},
            "GLD missing for the session",
        ),
        (
            {USO: _daily(USO)},
            {USO: BarStatus.FRESH},
            {USO: DAILY, GLD: DAILY},
            "GLD missing for the session",
        ),
        (
            {USO: _daily(USO)},
            {USO: BarStatus.FRESH},
            {},
            "the caller names no bar it needs from the snapshot",
        ),
        (
            {USO: _daily(USO)},
            {USO: BarStatus.FRESH},
            {USO: []},
            "the caller names no bar it needs from the snapshot",
        ),
        (
            {USO: {"1m": (_bar(USO, "1m"),)}},
            {USO: BarStatus.FRESH},
            {USO: DAILY},
            "USO has no 1d bar for the session",
        ),
        (
            {USO: {"1m": (_bar(USO, "1m"),), "1d": ()}},
            {USO: BarStatus.FRESH},
            {USO: ["1m", "1d"]},
            "USO has no 1d bar for the session",
        ),
    ],
)
def test_require_fresh_halts_on_a_gap(bars: dict, status: dict, needed: dict, reason: str) -> None:
    with pytest.raises(Halt) as raised:
        _snapshot(bars, status).require_fresh(needed)
    assert raised.value.reason == reason


@pytest.mark.parametrize("needed", ["USO", [USO], None, {USO: "1d"}])
def test_require_fresh_takes_a_mapping_of_what_is_needed(needed: object) -> None:
    snapshot = _snapshot({USO: _daily(USO)}, {USO: BarStatus.FRESH})
    with pytest.raises(TypeError):
        snapshot.require_fresh(needed)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("instrument", "interval"), [(GLD, "1d"), (USO, "1m")], ids=["instrument", "interval"]
)
def test_a_bar_filed_under_another_key_is_refused(instrument: InstrumentId, interval: str) -> None:
    with pytest.raises(ValueError):
        _snapshot({USO: {"1d": (_bar(instrument, interval),)}}, {USO: BarStatus.FRESH})


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
    one_sided = core.CostSchedule(per_share={GLD: Decimal("0.01")}, fixed={USO: Decimal("1")})
    for instrument in (GLD, USO):
        with pytest.raises(Halt):
            one_sided.estimate(instrument, Decimal("1"))


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
    instrument_bars = get_args(snapshot_hints["bars"])[1]
    assert get_origin(instrument_bars) is Mapping


@pytest.mark.parametrize(
    "kind",
    [core.Plan, core.Lease, core.RestartRecord, core.Heartbeat, core.JournalEntry],
    ids=lambda k: k.__name__,
)
def test_every_type_stored_at_a_key_of_its_own_names_its_format(kind: type) -> None:
    """A later release reads an older blob by its format, so each type stored alone carries one."""
    assert get_type_hints(kind)["format"] is int


def test_a_gate_reads_a_close_from_its_journal_entry() -> None:
    hints = get_type_hints(core.GateInputs)
    assert hints["close_entry"] == core.JournalEntry | None
