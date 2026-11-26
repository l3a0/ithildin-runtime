"""Test 1 of #4: the replicated strategy traded through the whole platform, for #146 item 13.

`tests/support/trading.py` runs the 1,461 decisions once per session. Every
position here is read from the `book.json` recover stored at the decision after,
which recover has reconciled against the simulated broker's account, and never
from a target or an outcome. The figures are then computed a second way, from
the account's cash and shares alone.

Every figure is exploratory. A fill at the close its signal read is a price no
live order reaches. It is the replication's own assumption, named in each fill's
`basis`, so none of these numbers is quoted as evidence of an edge.

The regression pins at the end are of the code at the commit that added them,
over quantitative-trading's vintages at `19aadbeb`.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    FillBasis,
    InstrumentId,
    Snapshot,
    decode_output,
    encode,
    sim_account_key,
)
from ithildinruntime.journal import Journal, record_of
from tests.support.chan_return import PERIODS, chan_figures
from tests.support.replication import FIRST_DECISION, GLD, ROWS_BEFORE, USO
from tests.support.trading import ACCOUNT, AFTER, CASH, STEP, STRATEGY, G, Traded, trade

pytestmark = pytest.mark.replication

APR, SHARPE = 0.17824928, 0.96467289
ZERO = Decimal(0)


@pytest.fixture(scope="module")
def traded(every_session, tmp_path_factory) -> Traded:
    return trade(every_session, tmp_path_factory.mktemp("test-1"))


@pytest.fixture(scope="module")
def closes(every_session) -> dict[InstrumentId, dict[date, Decimal]]:
    return {i: {bar.session: bar.close for bar in by["1d"]} for i, by in every_session.bars.items()}


@pytest.fixture(scope="module")
def held(traded) -> list[tuple[date, dict[InstrumentId, Decimal]]]:
    """Each decision's session and the shares held after its close, from the next book."""
    days = [d.day for d in traded.decided]
    made = []
    for day, later in zip(days, [*days[1:], AFTER], strict=True):
        book = traded.books[later]
        made.append((day, _shares(book)))
    return made


def test_every_decision_ran_and_the_last_ones_orders_filled(traded) -> None:
    assert len(traded.decided) == 1461
    assert traded.decided[0].day == FIRST_DECISION
    assert traded.decided[-1].day == date(2012, 4, 9)
    view = Journal(traded.store, ACCOUNT).view()
    assert not view.live
    assert all(fill.basis is FillBasis.SIGNAL_CLOSE for fill in view.fills.values())


def test_each_position_after_its_fill_is_the_decisions_target(traded, held) -> None:
    for decided, (_, shares) in zip(traded.decided, held, strict=True):
        wanted = decided.target.shares
        for leg in (GLD, USO):
            assert abs(shares.get(leg, ZERO) - wanted[leg]) < STEP, (decided.day, leg)


def test_no_limit_cuts_a_target_and_each_held_day_is_g_to_half_a_step(traded, closes) -> None:
    """Nothing binds, so a figure that divides by gross, as both ways here do, sees the whole run.

    Each leg is rounded to the nearest step, so a held day's gross is G to within
    half a step of each leg at its close, on either side. The stress records are
    made up and read through dates of their own, which matters only if a limit
    cuts a target, and none of the tracked file's does.
    """
    held = 0
    for decided in traded.decided:
        assert decided.sizing.cut == (), decided.day
        marks = {i: closes[i][decided.day] for i in decided.target.shares}
        gross = sum((abs(q) * marks[i] for i, q in decided.target.shares.items()), ZERO)
        if gross:
            held += 1
            assert abs(gross - G) <= STEP / 2 * sum(marks.values()), decided.day
    assert held == 1146


def test_the_units_are_the_replications(held, replicated) -> None:
    units = [_unit(shares) for _, shares in held]
    assert units == [forecast.direction for _, forecast, _ in replicated]
    assert sum(a != b for a, b in zip([0, *units], units, strict=False)) == 162


def test_chans_figures_from_the_positions_held(held, closes) -> None:
    sessions = sorted(closes[GLD])
    first = sessions.index(FIRST_DECISION)
    rows = [(day, {}) for day in sessions[first - ROWS_BEFORE : first]] + held
    apr, sharpe = chan_figures(rows, closes)
    assert apr == pytest.approx(APR, abs=5e-9, rel=0)
    assert sharpe == pytest.approx(SHARPE, abs=5e-9, rel=0)


def test_the_same_figures_from_the_account_alone(traded, closes) -> None:
    """Each day's profit is the change in cash and shares valued at the close, over gross before.

    Read this way, a fill at another price than the signal's close would move
    the cash and so the figures, which the first way cannot see.
    """
    sessions = sorted(closes[GLD])
    first = sessions.index(FIRST_DECISION)
    days = [d.day for d in traded.decided]
    after = [*days[1:], AFTER]
    # The account as each decision's next book holds it: cash and shares after that close.
    marks = [(day, traded.books[later]) for day, later in zip(days, after, strict=True)]
    returns = [0.0] * (ROWS_BEFORE + 1)
    for (before, one), (day, two) in zip(marks, marks[1:], strict=False):
        gross = sum((abs(q) * closes[i][before] for i, q in _shares(one).items()), ZERO)
        value = _value(two, closes, day) - _value(one, closes, before)
        returns.append(float(value) / float(gross) if gross else 0.0)
    assert len(returns) == len(sessions[first - ROWS_BEFORE :])
    apr, sharpe = _figures(returns)
    assert apr == pytest.approx(APR, abs=5e-9, rel=0)
    assert sharpe == pytest.approx(SHARPE, abs=5e-9, rel=0)


def test_the_accounts_gain_is_the_sum_of_each_days_shares_times_its_move(
    traded, held, closes
) -> None:
    last = traded.books[AFTER]
    end = _value(last, closes, date(2012, 4, 9))
    moved = ZERO
    for (before, shares), (day, _) in zip(held, held[1:], strict=False):
        moved += sum((q * (closes[i][day] - closes[i][before]) for i, q in shares.items()), ZERO)
    assert end - CASH == moved


def test_the_simulated_account_holds_what_the_last_book_holds(traded) -> None:
    from ithildinruntime.execution.brokers.log import SimulatedAccount

    log = SimulatedAccount.load(traded.store, ACCOUNT, "f" * 32)
    log.catch_up()
    now = log.account_now
    book = traded.books[AFTER]
    assert {i: q for i, q in now.positions.items() if q} == {
        i: q for i, q in _shares(book).items() if q
    }
    assert now.cash == book.cash


def test_each_fill_walks_to_the_snapshot_it_was_priced_from_and_its_decisions(traded) -> None:
    """The trace: a fill, the simulator's event that priced it, and the decision that planned it."""
    store = traded.store
    view = Journal(store, ACCOUNT).view()
    priced = {}
    for key in store.list(sim_account_key(ACCOUNT, 1).rpartition("/")[0] + "/", None):
        for closed in json.loads(store.get(key))["events"]["closed"]:
            if closed["fill"] is not None:
                priced[closed["fill"]["fill_id"]] = closed["priced_from"]
    snapshots: dict[str, Snapshot] = {}

    def bar_close(ref_key: str, sha256: str, leg: InstrumentId, day: date) -> Decimal:
        data = store.get(ref_key)
        assert hashlib.sha256(data).hexdigest() == sha256, ref_key
        if ref_key not in snapshots:
            (snapshots[ref_key],) = decode_output(data, Snapshot).entries
        (bar,) = [b for b in snapshots[ref_key].bars[leg]["1d"] if b.session == day]
        return bar.close

    assert len(priced) == len(view.fills)
    for fill in view.fills.values():
        order = view.orders[fill.client_order_id]
        planned = view.decisions[order.decision]
        day = planned.decision_at.date()
        source = priced[fill.fill_id]
        assert bar_close(source["key"], source["sha256"], fill.instrument, day) == fill.price
        snapshot = planned.inputs["snapshot"]
        assert bar_close(snapshot.key, snapshot.sha256, fill.instrument, day) == fill.price


def test_the_operating_range_the_tracked_limits_rest_on(traded, held, closes) -> None:
    """The figures `chan_bollinger.toml` quotes for its `[risk]` limits, each inside its limit.

    Each is exploratory, read at the tracked G over test 1's decisions. The net is
    the widest the gate can count for a decision's two legs, each leg either
    filled or not, at the decision's closes, and the gross takes each leg at the
    larger of before and after. A loss is a day's move on what the close before
    held. Each `max_position` is the largest position rounded up to ten shares, as
    the tracked file says, and no day of test 1 reaches the loss stop. The gate in
    the run refuses a gross, a net or a loss past its limit before these asserts
    read it. Pinned at c288e22.
    """
    limits = STRATEGY.section("risk")
    most_held = dict.fromkeys((GLD, USO), ZERO)
    most_order = dict.fromkeys((GLD, USO), ZERO)
    most_orders = 0
    most_net = most_gross = worst_loss = ZERO
    before: dict[InstrumentId, Decimal] = {}
    for n, (day, after) in enumerate(held):
        moved = [i for i in (GLD, USO) if after.get(i, ZERO) != before.get(i, ZERO)]
        most_orders = max(most_orders, len(moved))
        for i in (GLD, USO):
            most_held[i] = max(most_held[i], abs(after.get(i, ZERO)))
            most_order[i] = max(most_order[i], abs(after.get(i, ZERO) - before.get(i, ZERO)))
        # The gate counts each leg's gross at the larger of before and after its order.
        widest = (max(abs(before.get(i, ZERO)), abs(after.get(i, ZERO))) for i in (GLD, USO))
        gross = sum((q * closes[i][day] for i, q in zip((GLD, USO), widest, strict=True)), ZERO)
        most_gross = max(most_gross, gross)
        for gld, uso in itertools.product((before, after), repeat=2):
            corner = {GLD: gld.get(GLD, ZERO), USO: uso.get(USO, ZERO)}
            net = abs(sum((q * closes[j][day] for j, q in corner.items()), ZERO))
            most_net = max(most_net, net)
        if n:
            last = held[n - 1][0]
            moves = (q * (closes[i][day] - closes[i][last]) for i, q in before.items())
            worst_loss = max(worst_loss, -sum(moves, ZERO))
        before = after
    found = {
        "largest GLD": round(float(most_held[GLD]), 1),
        "largest USO": round(float(most_held[USO]), 1),
        "largest GLD order": round(float(most_order[GLD]), 1),
        "largest USO order": round(float(most_order[USO]), 1),
        "most orders a decision": most_orders,
        "largest net over G": round(float(most_net / G), 3),
        "largest gross over G": round(float(most_gross / G), 3),
        "worst day over G": round(float(worst_loss / G), 3),
    }
    assert found == RANGE, found
    for i in (GLD, USO):
        assert limits["max_position"][i] == math.ceil(most_held[i] / 10) * 10
        assert most_order[i] <= limits["max_order_qty"][i]
    assert most_orders <= limits["max_open_orders"]
    assert most_net <= limits["max_net"] and most_gross <= limits["max_gross"]
    assert worst_loss < limits["loss_stop"]


RANGE = {
    "largest GLD": 117.7,
    "largest USO": 327.9,
    "largest GLD order": 159.7,
    "largest USO order": 484.0,
    "most orders a decision": 2,
    "largest net over G": 1.447,
    "largest gross over G": 1.453,
    "worst day over G": 0.066,
}


# Regression pins of the code that added them, so a change anywhere on the path shows here.


def test_the_runs_record_is_pinned(traded) -> None:
    """The orders, the fills, the journal's head and the last book, at #129's G and rounding.

    CI's run 45858921846 of 9f9e0d9 reproduced them.

    The head is pinned by its record, since each entry's bytes name the
    process's writer, which is drawn afresh at each start.
    """
    journal = Journal(traded.store, ACCOUNT)
    view = journal.view()
    orders = sum(len(d.outcome.orders) for d in traded.decided)
    head = view.through.seq
    (entry,) = journal.read(after=head - 1)
    found = (
        orders,
        len(view.fills),
        head,
        _digest(encode(record_of(entry))),
        _digest(encode(traded.books[AFTER])),
    )
    assert found == PINNED, found


PINNED = (
    2444,
    2444,
    16605,
    "7385137f125c2204da5515cb51d0206a95fe066cc62348de8ca4f704ed03d8f0",
    "f898ca28d0c2c1da9a58d2336d8751ee238f2a41008bbea0cb0daa132d357dd4",
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unit(shares: Mapping[InstrumentId, Decimal]) -> int:
    # The spread holds USO against GLD, so the unit is the sign of the USO leg.
    qty = shares.get(USO, ZERO)
    return (qty > 0) - (qty < 0)


def _shares(book) -> dict[InstrumentId, Decimal]:
    return dict(book.positions_by_strategy.get("chan_bollinger", {}))


def _value(book, closes, day: date) -> Decimal:
    return book.cash + sum((q * closes[i][day] for i, q in _shares(book).items()), ZERO)


def _figures(returns: Sequence[float]) -> tuple[float, float]:
    """The APR and Sharpe ratio, by the formulas `chan_figures` documents."""
    n = len(returns)
    apr = math.exp(math.fsum(map(math.log1p, returns)) * PERIODS / n) - 1
    middle = math.fsum(returns) / n
    deviation = math.sqrt(math.fsum((r - middle) ** 2 for r in returns) / (n - 1))
    return apr, math.sqrt(PERIODS) * middle / deviation
