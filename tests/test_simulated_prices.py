"""The prices the simulated broker may fill at. Each number is #144's acceptance item."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    DATA_STAGE,
    SNAPSHOT_OUTPUT,
    BarStatus,
    BrokerState,
    DecisionResult,
    Halt,
    HaltRecord,
    OrderState,
    decode,
    encode,
    output_key,
    result_key,
)
from ithildinruntime.execution.brokers.prices import StoredPrices
from tests.support import gates
from tests.support.sim_world import (
    DEPLOYMENT,
    FULL,
    GLD,
    MICRO,
    TABLE,
    USO,
    SimWorld,
    after,
    close_of,
)

D = Decimal
PRIOR = TABLE.before(FULL).day
NEXT, LATER = after(FULL), after(FULL, 2)


def world(*missing) -> SimWorld:
    """Two sessions before `FULL` and three from it on, less the sessions with no bar."""
    days = [TABLE.before(PRIOR).day, PRIOR, FULL, NEXT, LATER]
    given = {d: D(10 + n) for n, d in enumerate(days) if d not in missing}
    return SimWorld({GLD: given, USO: {d: p * 2 for d, p in given.items()}})


def prices(made: SimWorld) -> StoredPrices:
    return StoredPrices(made.storage(), DEPLOYMENT, made.run, made.at)


def at(made: SimWorld, day=FULL, early: int = 0, **changes: object) -> StoredPrices:
    when = close_of(day) - timedelta(minutes=early)
    made.decide(when, snapshot=made.snapshot(when, **changes), recover=False)
    return prices(made)


def key(made: SimWorld) -> str:
    return result_key(DEPLOYMENT, made.run, made.at, DATA_STAGE)


def stored(made: SimWorld) -> DecisionResult:
    return decode(made.store.blobs[key(made)], DecisionResult)


# What they give


def test_3_a_close_is_the_bar_of_that_session_and_none_where_there_is_none() -> None:
    found = at(made := world())
    assert found.decision_at == close_of(FULL) == made.at
    assert found.close(GLD, FULL) == 12 and found.close(USO, FULL) == 24
    assert found.close(GLD, PRIOR) == 11
    assert found.close(GLD, NEXT) is None
    early = at(world(), early=20)
    assert early.close(GLD, FULL) is None
    assert early.close(GLD, PRIOR) == 11


def test_3_a_quote_is_the_decisions_as_it_was_read_and_none_where_it_read_none() -> None:
    assert at(world()).quote(GLD) is None
    made = world()
    early = at(made, early=20)
    assert early.quote(GLD) == made.snapshot(made.at).quotes[GLD]


def test_3_the_snapshot_is_read_once_and_only_when_asked() -> None:
    found = at(made := world())
    made.store.calls.clear()
    made.store.quiet = False
    assert made.store.calls == []
    found.close(GLD, FULL)
    found.close(USO, PRIOR)
    found.quote(GLD)
    assert [op for op, _ in made.store.calls] == ["get", "get"]


# What halts


def test_3_a_missing_result_halts_and_is_read_again() -> None:
    found = at(made := world())
    kept = made.store.blobs[key(made)]
    made.store.drop(key(made))
    for _ in range(2):
        with pytest.raises(Halt, match="is not stored"):
            found.close(GLD, FULL)
    made.store.put(key(made), kept)
    assert found.close(GLD, FULL) == 12


def test_3_a_stale_result_halts() -> None:
    found = at(made := world())
    old = stored(made)
    header = replace(old.header, decision_at=made.at - timedelta(days=1))
    made.store.put(key(made), encode(replace(old, header=header)))
    with pytest.raises(Halt, match="so it is stale"):
        found.close(GLD, FULL)


def test_3_a_result_that_holds_a_halt_halts() -> None:
    found = at(made := world())
    halt = HaltRecord("GLD stale for the session", None, (), (), ())
    made.store.put(key(made), encode(replace(stored(made), outputs=(), halt=halt)))
    with pytest.raises(Halt, match="holds a halt: GLD stale"):
        found.quote(GLD)


def test_3_a_snapshot_of_another_decision_or_of_other_bytes_halts() -> None:
    made = world()
    found = at(made, decision_at=close_of(FULL) - MICRO)
    with pytest.raises(Halt, match="holds a snapshot of"):
        found.close(GLD, FULL)
    found = at(made := world())
    name = output_key(DEPLOYMENT, made.run, made.at, DATA_STAGE, SNAPSHOT_OUTPUT)
    made.store.put(name, made.store.blobs[name] + b" ")
    with pytest.raises(Halt, match="has SHA-256"):
        found.close(GLD, FULL)


def test_3_an_instrument_that_is_stale_or_not_in_the_snapshot_halts() -> None:
    found = at(world(), status={GLD: BarStatus.STALE, USO: BarStatus.FRESH})
    with pytest.raises(Halt, match="GLD stale"):
        found.close(GLD, FULL)
    with pytest.raises(Halt, match="SPY missing"):
        at(world()).close(gates.SPY, FULL)


@pytest.mark.parametrize("field", ["end", "knowable_at"])
def test_3_a_bar_past_the_decision_halts(field: str) -> None:
    made = world()
    bars = made.bars(GLD, close_of(FULL))
    late = replace(bars[-1], **{field: close_of(FULL) + MICRO})
    held = {GLD: {"1d": (*bars[:-1], late)}, USO: {"1d": made.bars(USO, close_of(FULL))}}
    with pytest.raises(Halt, match="after the decision"):
        at(made, bars=held).close(GLD, FULL)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda b: (*b, b[-1]), "has 2 daily bars"),
        (lambda b: (*b[:-1], replace(b[-1], price_kind="unstated")), "unstated price kind"),
        (lambda b: (*b[:-1], replace(b[-1], close=D(0))), "not above zero"),
        (lambda b: (*b[:-1], replace(b[-1], close=D(-1))), "not above zero"),
    ],
)
def test_3_a_bar_that_cannot_be_a_price_halts(change, reason: str) -> None:
    made = world()
    held = {
        GLD: {"1d": change(made.bars(GLD, close_of(FULL)))},
        USO: {"1d": made.bars(USO, close_of(FULL))},
    }
    with pytest.raises(Halt, match=reason):
        at(made, bars=held).close(GLD, FULL)


# What the broker does with them


def test_3_an_order_whose_session_has_no_bar_keeps_working_until_one_comes() -> None:
    made = world(FULL)
    made.day(FULL, [(4, GLD)], early=20)
    for day in (NEXT, LATER):
        made.decide(close_of(day))
        (held,) = made.broker().open_orders().orders
        assert (held.state, held.filled) == (BrokerState.WORKING, 0)
        assert made.broker().fills(close_of(PRIOR)).fills == ()
        made.agree()
    made.closes[GLD][FULL] = D("12.25")  # type: ignore[index]
    made.closes[USO][FULL] = D("24.5")  # type: ignore[index]
    when = close_of(after(FULL, 3))
    made.decide(when)
    (fill,) = made.broker().fills(close_of(PRIOR)).fills
    assert (fill.price, fill.at, fill.received_at) == (D("12.25"), close_of(FULL), when)
    made.agree()


def test_3_a_missing_result_halts_a_call_with_an_order_past_its_close() -> None:
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    made.decide(close_of(NEXT), recover=False)
    made.store.drop(key(made))
    number = made.log.number
    with pytest.raises(Halt, match="is not stored"):
        made.path().positions()
    assert made.log.number == number


def test_3_a_call_with_no_order_past_its_close_reads_no_price() -> None:
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    made.store.drop(key(made))
    assert len(made.broker().open_orders().orders) == 1
    made.clock.at = close_of(FULL)
    assert len(made.broker().open_orders().orders) == 1


def test_3_an_order_for_an_instrument_the_snapshot_lost_halts_until_it_is_back() -> None:
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    when = close_of(NEXT)
    lost = {"bars": {USO: {"1d": made.bars(USO, when)}}, "status": {USO: BarStatus.FRESH}}
    made.decide(when, snapshot=made.snapshot(when, **lost), recover=False)
    with pytest.raises(Halt, match="GLD missing"):
        made.path().fills(close_of(PRIOR))
    made.decide(close_of(LATER))
    assert made.broker().positions().positions == {GLD: 4}


def test_3_a_fill_reads_its_own_sessions_bar_and_no_newer_one() -> None:
    made = world()
    made.day(FULL, [(4, GLD)], early=20)
    made.decide(close_of(LATER))
    (fill,) = made.broker().fills(close_of(PRIOR)).fills
    assert fill.price == 12
    assert made.view().orders[fill.client_order_id].state is OrderState.FILLED
