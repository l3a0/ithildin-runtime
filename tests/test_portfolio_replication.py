"""Portfolio construction over the replication's decisions, for #9's acceptance 3, 4 and 6.

Each of the 1,461 forecasts `tests/support/replication.py` stores is sized at
its session's closes, and acceptance 4 then asks `risk.Gate` about each order.
Every figure is exploratory. No cap here is an account's.

The order days are walked by one convention. Each leg is asked in instrument
order, with an earlier approved leg open. A day with a refused leg moves
neither, as execution cancels an accepted leg whose partner was refused. Such a
day is classed by the change in the forecast's direction from the decision
before, so an exit counts once, on the day it is first asked.

#9 wrote these counts before the build, and the build reproduced them: the 1,146
and 315 days, the refused days of 70, 62, 34, 24, 636 and 1,038 with 28 exits,
the 9 flips, and 2,640 exits of which 344 are refused with no room. Every other
count here came from the code at 19034c0, over quantitative-trading's vintages
at `19aadbeb`, so it holds what the code did rather than what a plan expected.
Two checks are sampled: the stress exits at every seventh decision, and the
wrong prices at every fourth.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

import pytest

from ithildinruntime.alpha import build_model
from ithildinruntime.core import (
    Constraints,
    Forecast,
    InstrumentId,
    Limit,
    Sizing,
    Snapshot,
    TargetPortfolio,
    Valuation,
    Verdict,
    parse_strategy,
)
from ithildinruntime.portfolio import size
from ithildinruntime.risk import Gate
from tests.support import gates
from tests.support.chan_return import chan_figures
from tests.support.gates import LOOSE, d
from tests.support.replication import CHAN, GLD, LOOKBACK, ROWS_BEFORE, USO

type Shares = Mapping[InstrumentId, Decimal]
type Change = Callable[[str, Decimal], Decimal]

G = Decimal(10_000)
# The exploratory stress move, read from 2006 to 2026.
MOVE = d("0.347")
NINE = Decimal("1E-9")
CAPS = gates.caps(stress_move=MOVE, stress_through=date(2006, 6, 20), stress_max_age_days=365)
FLAT: Shares = {GLD: Decimal(0), USO: Decimal(0)}


@dataclass(frozen=True)
class Day:
    """One decision: its forecast, and the two legs' closes at it and at the session before."""

    day: date
    forecast: Forecast
    marks: Shares
    prior: Shares
    snapshot: Snapshot
    valuation: Valuation


def days_of(
    decided: list[tuple[date, Forecast]], closes: Mapping[InstrumentId, Mapping[date, Decimal]]
) -> list[Day]:
    sessions = sorted(closes[GLD])
    made = []
    for day, forecast in decided:
        before = sessions[sessions.index(day) - 1]
        marks = {i: closes[i][day] for i in (GLD, USO)}
        prior = {i: closes[i][before] for i in (GLD, USO)}
        at = gates.TABLE.session(day).close
        made.append(
            Day(
                day=day,
                # The gate's samples name their strategy so.
                forecast=replace(forecast, strategy=gates.CHAN),
                marks=marks,
                prior=prior,
                snapshot=gates.snapshot(marks, at=at, day=day, prior=prior),
                valuation=gates.valuation(LOOSE, session=before),
            )
        )
    return made


@pytest.fixture(scope="module")
def closes(every_session) -> dict[InstrumentId, dict[date, Decimal]]:
    return {i: {b.session: b.close for b in by["1d"]} for i, by in every_session.bars.items()}


@pytest.fixture(scope="module")
def days(replicated, closes) -> list[Day]:
    return days_of([(day, forecast) for day, forecast, _ in replicated], closes)


def sized(
    day: Day,
    limits: Constraints | None = None,
    *,
    g: Decimal = G,
    step: Decimal = NINE,
    forecast: Forecast | None = None,
    snapshot: Snapshot | None = None,
) -> tuple[TargetPortfolio, Sizing]:
    config = parse_strategy(
        gates.CHAN, f'instruments = ["GLD", "USO"]\n[portfolio]\ngross = {g}\n'.encode()
    )
    return size(
        config,
        (gates.CHAN,),
        forecast or day.forecast,
        snapshot or day.snapshot,
        gates.book(),
        limits or gates.constraints(),
        replace(aged(day), share_step=step),
        day.valuation,
    )


def aged(day: Day):
    """The caps, with the vintage's end at the decision's session before."""
    return replace(CAPS, stress_through=day.valuation.session)


def gross_of(shares: Shares, marks: Shares) -> Decimal:
    return sum((abs(q) * marks[i] for i, q in shares.items()), Decimal(0))


# Acceptance 3: test 1's positions


def held_rows(days: list[Day], closes, step: Decimal) -> list[tuple[date, Shares]]:
    sessions = sorted(closes[GLD])
    first = sessions.index(days[0].day)
    rows: list[tuple[date, Shares]] = [(day, {}) for day in sessions[first - ROWS_BEFORE : first]]
    return rows + [(day.day, sized(day, step=step)[0].shares) for day in days]


def test_3_test_1s_positions_give_the_replications_return(days, closes) -> None:
    rows = held_rows(days, closes, NINE)
    assert len(rows) == 1480
    apr, sharpe = chan_figures(rows, closes)
    assert apr == pytest.approx(0.17824928, abs=5e-9, rel=0)
    assert sharpe == pytest.approx(0.96467289, abs=5e-9, rel=0)


def test_3_each_held_day_is_g_within_a_step_of_each_leg(days) -> None:
    """`chan_figures` divides by gross, so it cannot see a size."""
    held = 0
    for day in days:
        target, record = sized(day)
        assert record.cut == () and set(target.shares) == {GLD, USO}
        if day.forecast.direction == 0:
            assert [str(q) for q in target.shares.values()] == ["0E-9", "0E-9"]
            assert dict(record.wanted) == dict(target.shares)
            continue
        held += 1
        short = G - gross_of(target.shares, day.marks)
        assert 0 <= short < NINE * sum(day.marks.values())
        for i, q in target.shares.items():
            leg = day.forecast.basket.legs[i] * day.forecast.direction
            assert q * leg > 0
        assert dict(record.wanted) == dict(target.shares)
    assert (held, len(days) - held) == (1146, 315)


def test_3_whole_shares_miss_the_replications_return(days, closes) -> None:
    """Why test 1's account alone moves in billionths of a share."""
    apr, sharpe = chan_figures(held_rows(days, closes, Decimal(1)), closes)
    assert abs(apr - 0.17824928) == pytest.approx(1.56e-3, abs=5e-6)
    assert abs(sharpe - 0.96467289) == pytest.approx(4.99e-3, abs=5e-6)


# Acceptance 4: the gate agrees


def ask(
    book: Shares, orders: Shares, day: Day, limits: Constraints, snapshot: Snapshot | None = None
) -> list[Verdict]:
    """The gate's verdict on each order, in the order given, with the approved ones open."""
    asked, verdicts = [], []
    for n, (instrument, qty) in enumerate(orders.items()):
        intent = gates.order(qty, instrument, name=f"new-{n}")
        verdict = Gate().check(
            intent,
            gates.book({gates.CHAN: book}, asked),
            snapshot or day.snapshot,
            limits,
            gates.inputs(caps=aged(day), valuation=day.valuation),
            day.snapshot.decision_at,
        )
        verdicts.append(verdict)
        if verdict.approved:
            asked.append(intent)
    return verdicts


def difference(target: Shares, book: Shares) -> Shares:
    return {i: target[i] - book[i] for i in sorted(target) if target[i] != book[i]}


def kind_of(before: int, now: int) -> str:
    if before == now:
        return "rebalance"
    if 0 not in (before, now):
        return "flip"
    return "exit" if now == 0 else "entry"


@dataclass(frozen=True)
class Walk:
    """What the gate refused over the days, and what the refusals left held."""

    order_days: int
    refused: Mapping[str, int]
    against: int

    @property
    def total(self) -> int:
        return sum(self.refused.values())


def walk(
    days: list[Day],
    asked: Callable[[Day], Constraints],
    sizing: Callable[[Day], Constraints] | None = None,
) -> Walk:
    book, before = FLAT, 0
    refused: Counter[str] = Counter()
    order_days = against = 0
    for day in days:
        target = sized(day, (sizing or asked)(day))[0].shares
        orders = difference(target, book)
        order_days += bool(orders)
        if not all(v.approved for v in ask(book, orders, day, asked(day))):
            refused[kind_of(before, day.forecast.direction)] += 1
        else:
            book = target
        before = day.forecast.direction
        # USO's one share in the basket carries the direction.
        sign = (book[USO] > 0) - (book[USO] < 0)
        against += sign != day.forecast.direction
    return Walk(order_days, refused, against)


def held_to(net: object = None, gross: object = None, widened: Decimal = Decimal(1)):
    """Limits at these parts of G, loose elsewhere, and widened by a factor."""
    changes = {
        name: d(part) * G * widened
        for name, part in (("max_net", net), ("max_gross", gross))
        if part is not None
    }
    made = gates.constraints(**changes)
    return lambda day: made


def test_4_no_limit_refuses_no_order(days) -> None:
    walked = walk(days, held_to())
    assert (walked.order_days, walked.total, walked.against) == (1222, 0, 0)


@pytest.mark.parametrize(
    ("net", "gross", "refused", "against"),
    [
        ("0.6", None, {"rebalance": 63, "flip": 7}, 66),
        ("1.0", None, {"rebalance": 55, "flip": 7}, 61),
        (None, "1.0", {"rebalance": 32, "flip": 2}, 10),
        ("0.6", "0.7", {"rebalance": 23, "flip": 1}, 16),
    ],
    ids=["net-0.6", "net-1.0", "gross-1.0", "net-0.6-gross-0.7"],
)
def test_4_with_room_no_entry_and_no_exit_is_refused(days, net, gross, refused, against) -> None:
    walked = walk(days, held_to(net, gross))
    assert dict(walked.refused) == refused
    assert walked.against == against


@pytest.mark.parametrize(
    ("net", "gross", "total", "exits"),
    [("0.6", None, 636, 28), (None, "1.0", 1038, 0)],
    ids=["net-0.6", "gross-1.0"],
)
def test_4_with_no_room_the_gate_refuses_entries_and_exits(days, net, gross, total, exits) -> None:
    """Sized under limits widened by one plus the stress move, which takes the room away."""
    walked = walk(days, held_to(net, gross), held_to(net, gross, 1 + MOVE))
    assert (walked.total, walked.refused.get("exit", 0)) == (total, exits)


def test_4_an_order_size_of_one_unit_refuses_every_flip(days) -> None:
    """A flip's order is both directions' shares."""

    def limits(day: Day) -> Constraints:
        most = {i: (d("1.01") * G / mark).quantize(NINE) for i, mark in day.marks.items()}
        return gates.constraints(max_order_qty=most, max_position=most)

    walked = walk(days, limits)
    assert walked.refused["flip"] == 9


def stressed_exits(days: list[Day], limits: Constraints, sizing: Constraints) -> Counter[bool]:
    """Whether each whole exit is approved, a stress move after a resize, on each leg and side."""
    seen: Counter[bool] = Counter()
    for day, after in list(zip(days, days[1:], strict=False))[::7]:
        if not day.forecast.direction:
            continue
        held = sized(day, sizing)[0].shares
        for leg in (GLD, USO):
            for factor in (1 + MOVE, 1 - MOVE):
                marks = {**day.marks, leg: day.marks[leg] * factor}
                moved = gates.snapshot(
                    marks, at=after.snapshot.decision_at, day=after.day, prior=day.marks
                )
                for legs in ((GLD, USO), (USO, GLD)):
                    orders = {i: -held[i] for i in legs if held[i]}
                    verdicts = ask(held, orders, after, limits, moved)
                    seen[all(v.approved for v in verdicts)] += 1
    return seen


def test_4_a_stress_move_on_either_leg_lets_the_pair_leave(days) -> None:
    for part, refused in (("0.6", 300), ("1.0", 44)):
        limits = gates.constraints(max_net=d(part) * G)
        roomy = stressed_exits(days, limits, limits)
        assert roomy == {True: 1320}
        bare = replace(limits, max_net=limits.max_net * (1 + MOVE))
        # The check bites: with no room some of the same exits are refused.
        assert stressed_exits(days, limits, bare) == {True: 1320 - refused, False: refused}


# Acceptance 6: a price wrong by 100 times either way, on each leg


@dataclass(frozen=True)
class Wrong:
    """One held day decided at a wrong price, in the snapshot alpha and sizing both read."""

    day: Day
    leg: InstrumentId
    factor: str
    snapshot: Snapshot
    forecast: Forecast
    # The true price's target, whose sizes are the true `max_position`.
    true: Shares
    capped: TargetPortfolio
    record: Sizing
    loosened: Shares

    @property
    def most(self) -> Shares:
        return {i: abs(q) for i, q in self.true.items()}

    @property
    def past(self) -> list[InstrumentId]:
        return [i for i in (GLD, USO) if abs(self.loosened[i]) > self.most[i]]

    @property
    def outcome(self) -> str:
        if self.forecast.direction == 0:
            return "flat"
        sides = {self.true[i] * self.capped.shares[i] > 0 for i in (GLD, USO)}
        side = "the same sides" if sides == {True} else "another side"
        return f"{'cut' if self.record.cut else 'uncut'}, {side}"


def wrongly_priced(days: list[Day], closes) -> list[Wrong]:
    """Every fourth decision's held day, at each leg's close 100 times too high and too low."""
    sessions = sorted(closes[GLD])
    model = build_model(CHAN)
    made = []
    for day in days[::4]:
        if not day.forecast.direction:
            continue
        true = sized(day)[0].shares
        most = {i: abs(q) for i, q in true.items()}
        n = sessions.index(day.day)
        window = sessions[max(0, n - LOOKBACK + 1) : n + 1]
        for leg in (GLD, USO):
            for factor in ("100", "0.01"):
                read = {i: {s: closes[i][s] for s in window} for i in (GLD, USO)}
                read[leg][day.day] *= d(factor)
                bars = {
                    i: {"1d": tuple(gates.stated(i, s, price) for s, price in by.items())}
                    for i, by in read.items()
                }
                snapshot = replace(day.snapshot, bars=bars)
                forecast = replace(model.forecast(snapshot).forecasts[0], strategy=gates.CHAN)
                given = {"forecast": forecast, "snapshot": snapshot}
                capped, record = sized(day, gates.constraints(max_position=most), **given)
                loosened = sized(day, **given)[0].shares
                made.append(
                    Wrong(
                        day,
                        leg,
                        factor,
                        snapshot,
                        forecast,
                        true,
                        capped,
                        record,
                        loosened,
                    )
                )
    return made


@pytest.fixture(scope="module")
def wrongs(days, closes) -> list[Wrong]:
    return wrongly_priced(days, closes)


def test_6_each_case_of_a_wrong_price_is_listed(wrongs) -> None:
    """The cases `max_position` does not cut end flat, or smaller, on the same or another side."""
    assert Counter((w.leg, w.factor, w.outcome, ",".join(w.past)) for w in wrongs) == {
        (GLD, "0.01", "cut, another side", "GLD,USO"): 2,
        (GLD, "0.01", "cut, another side", "USO"): 159,
        (GLD, "0.01", "cut, the same sides", "GLD,USO"): 6,
        (GLD, "0.01", "cut, the same sides", "USO"): 63,
        (GLD, "0.01", "flat", ""): 59,
        (GLD, "100", "cut, another side", "USO"): 140,
        (GLD, "100", "cut, the same sides", "USO"): 84,
        (GLD, "100", "flat", ""): 59,
        (GLD, "100", "uncut, another side", ""): 4,
        (GLD, "100", "uncut, the same sides", ""): 2,
        (USO, "0.01", "cut, another side", "GLD"): 98,
        (USO, "0.01", "cut, another side", "GLD,USO"): 63,
        (USO, "0.01", "cut, the same sides", "GLD"): 86,
        (USO, "0.01", "cut, the same sides", "GLD,USO"): 24,
        (USO, "0.01", "flat", ""): 18,
        (USO, "100", "cut, another side", "GLD"): 149,
        (USO, "100", "cut, the same sides", "GLD"): 97,
        (USO, "100", "uncut, another side", ""): 30,
        (USO, "100", "uncut, the same sides", ""): 13,
    }


def test_6_no_target_passes_the_true_prices_position(wrongs) -> None:
    for w in wrongs:
        assert all(abs(q) <= w.most[i] for i, q in w.capped.shares.items())
        # With the limit loosened the same price gives a target past it, and `cut` says so.
        assert w.record.cut == ((Limit.MAX_POSITION,) if w.past else ())
        if not w.past:
            assert w.loosened == w.capped.shares


def test_6_the_gate_refuses_each_order_past_the_true_position(wrongs) -> None:
    """From flat, and from a position under the limit and at it."""
    asked: Counter[tuple[InstrumentId, str]] = Counter()
    for w in wrongs:
        limits = gates.constraints(max_position=w.most)
        half = {i: (q / 2).quantize(NINE) for i, q in w.true.items()}
        for i in w.past:
            for book in (FLAT, half, w.true):
                order = {i: w.loosened[i] - book[i]}
                (verdict,) = ask(book, order, w.day, limits, w.snapshot)
                assert verdict.limit is Limit.MAX_POSITION, (w.day.day, w.leg, w.factor, i)
            asked[i, "long" if w.loosened[i] > 0 else "short"] += 1
    assert asked == {
        (GLD, "long"): 18,
        (GLD, "short"): 507,
        (USO, "long"): 109,
        (USO, "short"): 432,
    }


def test_6_a_flip_is_refused_at_the_true_order_size(wrongs) -> None:
    """A wrong price that turns a leg asks for both directions' shares at once."""
    flips = 0
    for w in wrongs:
        turned = [i for i in (GLD, USO) if w.true[i] * w.capped.shares[i] < 0]
        limits = gates.constraints(max_position=w.most, max_order_qty=w.most)
        for i in turned:
            order = {i: w.capped.shares[i] - w.true[i]}
            (verdict,) = ask(w.true, order, w.day, limits, w.snapshot)
            assert verdict.limit is Limit.MAX_ORDER_QTY
        flips += bool(turned)
    assert flips == 645


@pytest.mark.parametrize(
    ("legs", "direction"),
    [({USO: "1", GLD: "0.4"}, 1), ({USO: "1", GLD: "-0.4"}, -1)],
    ids=["one-side", "opposite-sides"],
)
@pytest.mark.parametrize(
    ("leg", "price", "past"),
    [(GLD, "1", (GLD, USO)), (USO, "0.5", (GLD, USO)), (GLD, "10000", ()), (USO, "5000", ())],
    ids=["gld-low", "uso-low", "gld-high", "uso-high"],
)
def test_6_a_wrong_price_where_gld_is_long(days, legs, direction, leg, price, past) -> None:
    """By hand, since the days above reach GLD long least.

    At true prices of 100 and 50 and a G of 900 the target is 4 GLD against 10 USO. A price a
    hundred times low buys more of both legs, and one a hundred times high less of both.
    """
    day = replace(days[0], forecast=replace(days[0].forecast, direction=direction))
    basket = type(day.forecast.basket)({i: d(q) for i, q in legs.items()})
    forecast = replace(day.forecast, basket=basket)
    true_marks = {GLD: d(100), USO: d(50)}

    def snapshot(marks: Shares) -> Snapshot:
        return gates.snapshot(marks, at=day.snapshot.decision_at, day=day.day, prior=true_marks)

    g = d(900)
    true = sized(day, g=g, forecast=forecast, snapshot=snapshot(true_marks))[0].shares
    assert true == {GLD: d(4), USO: d(10) if legs[GLD] == "0.4" else d(-10)}
    most = {i: abs(q) for i, q in true.items()}
    read = snapshot({**true_marks, leg: d(price)})
    given = {"g": g, "forecast": forecast, "snapshot": read}
    limits = gates.constraints(max_position=most)
    capped, record = sized(day, limits, **given)
    loosened = sized(day, **given)[0].shares
    assert tuple(i for i in (GLD, USO) if abs(loosened[i]) > most[i]) == past
    assert record.cut == ((Limit.MAX_POSITION,) if past else ())
    assert all(abs(q) <= most[i] for i, q in capped.shares.items())
    for i in past:
        for book in (FLAT, {j: q / 2 for j, q in true.items()}, true):
            (verdict,) = ask(book, {i: loosened[i] - book[i]}, day, limits, read)
            assert verdict.limit is Limit.MAX_POSITION
    if past:
        return
    # Left standing: the price a hundred times high ends smaller with no limit cut, and the
    # gate approves each sale, both legs being on one side. Data's move flag reports the price.
    assert all(abs(capped.shares[i]) < most[i] / 4 for i in most)
    if direction == 1:
        sales = ask(true, difference(capped.shares, true), day, gates.constraints(), read)
        assert [v.approved and not v.adding for v in sales] == [True, True]
