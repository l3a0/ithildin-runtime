"""The mark, the range a position could reach, what reduces, V and the exact arithmetic."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

import pytest

from ithildinruntime import core
from ithildinruntime.core import (
    Bar,
    BarStatus,
    Effect,
    Exposure,
    Halt,
    InstrumentId,
    Snapshot,
    Valuation,
    classify,
    exact_product,
    exact_total,
    exposure,
    finite,
    held,
    mark,
    position,
    quotient,
    rounded,
    value_book,
    worth,
)
from tests.support.gates import (
    CHAN,
    DECISION,
    GLD,
    OTHER,
    PRIOR,
    QTY,
    SPY,
    TABLE,
    TODAY,
    USO,
    book,
    d,
    order,
    snapshot,
    utc,
)
from tests.support.quotes import quote

TICK = timedelta(microseconds=1)
OPEN = TABLE.session(TODAY).open
# A bar that ended and was known long before any decision here.
LONG_AGO = {"end": utc(2026, 11, 1), "knowable_at": utc(2026, 11, 1)}
AT = DECISION
MARKS = snapshot({GLD: "100.5", USO: 10}, prior={GLD: 99, USO: 11})


def _with(
    series: tuple[Bar, ...], instrument: InstrumentId = GLD, interval: str = "1d"
) -> Snapshot:
    """The snapshot with one instrument's daily bars replaced, and the other's left."""
    return replace(MARKS, bars={**MARKS.bars, instrument: {interval: series}})


def _bars(**changes: object) -> Snapshot:
    """The snapshot with fields of GLD's newest bar replaced."""
    old, new = MARKS.bars[GLD]["1d"]
    return _with((old, replace(new, **changes)))


def _weekend() -> dict:
    """GLD's newest bar moved to a Saturday, which a Monday's decision would else read."""
    old, new = MARKS.bars[GLD]["1d"]
    return {**MARKS.bars, GLD: {"1d": (old, replace(new, session=date(2026, 11, 7)))}}


def _status(instrument: InstrumentId, status: BarStatus) -> Snapshot:
    return replace(MARKS, status={**MARKS.status, instrument: status})


def test_the_mark_is_the_newest_daily_bars_close() -> None:
    assert mark(MARKS, GLD, PRIOR, AT) == d("100.5")
    assert mark(_with(tuple(reversed(MARKS.bars[GLD]["1d"]))), GLD, PRIOR, AT) == d("100.5")


FOUR = {
    "price_kind": "price kind",
    "venues": "venues",
    "delay": "delay",
    "volume_counted": "volume counting",
}
MARK_HALTS: dict[str, tuple[Snapshot, str]] = {
    "not-held": (
        replace(MARKS, status={USO: BarStatus.FRESH}, bars={USO: MARKS.bars[USO]}),
        "GLD missing for the session",
    ),
    "empty": (replace(MARKS, status={}, bars={}), "the snapshot for .* is empty"),
    "missing": (_status(GLD, BarStatus.MISSING), "GLD missing for the session"),
    "stale": (_status(GLD, BarStatus.STALE), "GLD stale for the session"),
    # One stale instrument halts every module that reads the snapshot, the gate included.
    "another-stale": (_status(USO, BarStatus.STALE), "USO stale for the session"),
    "no-bars": (replace(MARKS, bars={USO: MARKS.bars[USO]}), "GLD is fresh and has no bar"),
    "no-daily-bar": (
        _with((replace(MARKS.bars[GLD]["1d"][1], interval="1m"),), interval="1m"),
        "GLD has no 1d bar",
    ),
    "an-empty-series": (_with(()), "GLD is fresh and has no bar"),
    "no-later-bar": (
        _bars(session=PRIOR),
        "GLD has no mark: its newest bar is for 2026-11-02, not later than 2026-11-02",
    ),
    "a-bar-after-the-decision": (
        _bars(session=date(2026, 11, 4)),
        "GLD has a bar for 2026-11-04, after the decision's session 2026-11-03",
    ),
    "a-bar-of-no-session": (
        replace(MARKS, decision_at=utc(2026, 11, 9, 20, 40), bars=_weekend()),
        "the session table holds no session on 2026-11-07",
    ),
    "a-bar-of-no-date": (_bars(session=None), "GLD has a bar for None, not a date"),
    "a-bar-of-a-time": (
        _bars(session=utc(2026, 11, 3, 1)),
        "GLD has a bar for .*, not a date",
    ),
    **{
        f"a-decision-of-{at!r}": (
            replace(MARKS, decision_at=at),
            "the snapshot's decision_at is .*, not a UTC time",
        )
        for at in (None, "2026-11-03", 5)
    },
    "a-decision-a-microsecond-before-the-open": (
        replace(_bars(**LONG_AGO), decision_at=OPEN - TICK),
        "GLD has a bar for 2026-11-03, after the decision's session 2026-11-02",
    ),
    "a-bar-that-ends-after-the-decision": (
        _bars(end=MARKS.decision_at + TICK),
        "GLD's bar for 2026-11-03 ends at 2026-11-03T20:40:00.000001",
    ),
    "a-bar-knowable-after-the-decision": (
        _bars(knowable_at=MARKS.decision_at + TICK),
        "GLD's bar for 2026-11-03 was knowable at 2026-11-03T20:40:00.000001",
    ),
    "an-older-bar-knowable-after-the-decision": (
        _with(
            (
                replace(MARKS.bars[GLD]["1d"][0], knowable_at=MARKS.decision_at + TICK),
                MARKS.bars[GLD]["1d"][1],
            )
        ),
        "GLD's bar for 2026-11-02 was knowable at",
    ),
    "an-older-bar-that-ends-after-the-decision": (
        _with(
            (
                replace(MARKS.bars[GLD]["1d"][0], end=MARKS.decision_at + TICK),
                MARKS.bars[GLD]["1d"][1],
            )
        ),
        "GLD's bar for 2026-11-02 ends at",
    ),
    "a-bar-that-ends-at-no-time": (_bars(end=None), "when GLD's bar ends is None, not a UTC"),
    "a-decision-with-no-zone": (
        replace(MARKS, decision_at=MARKS.decision_at.replace(tzinfo=None)),
        "the snapshot's decision_at is .*, not a UTC time",
    ),
    "zero": (_bars(close=d(0)), "GLD's close for 2026-11-03 is 0, not above zero"),
    "negative": (_bars(close=d(-1)), "GLD's close for 2026-11-03 is -1, not above zero"),
    "nan": (_bars(close=d("NaN")), "GLD's close for 2026-11-03 is .*not a finite decimal"),
    "infinite": (_bars(close=d("Infinity")), "GLD's close for 2026-11-03 is .*not a finite"),
    **{
        f"unstated-{name}-{said!r}": (
            _bars(**{name: said}),
            f"GLD's bar for 2026-11-03 has unstated {shown}",
        )
        for name, shown in FOUR.items()
        for said in ("unstated", "", " ", None)
    },
    "a-quote": (
        replace(MARKS, quotes={GLD: quote(GLD, 1, 1, AT)}),
        "GLD has a quote and a bar for 2026-11-03, and nothing says which is the mark",
    ),
}


def test_5_a_bar_of_the_session_a_decision_opens_is_read_at_its_open() -> None:
    at_open = replace(_bars(**LONG_AGO), decision_at=OPEN)
    old, new = at_open.bars[GLD]["1d"]
    at_open = replace(
        at_open, bars={GLD: {"1d": (replace(old, **LONG_AGO), new)}}, status={GLD: BarStatus.FRESH}
    )
    assert mark(at_open, GLD, PRIOR, AT) == d("100.5")
    assert value_book(book({CHAN: {GLD: 1}}, cash=0), at_open, TODAY).value == d("100.5")
    early = replace(at_open, decision_at=OPEN - TICK)
    for read in (
        lambda: mark(early, GLD, PRIOR, AT),
        lambda: value_book(book({CHAN: {GLD: 1}}), early, PRIOR),
    ):
        with pytest.raises(Halt, match="after the decision's session 2026-11-02"):
            read()


@pytest.mark.parametrize("name", MARK_HALTS)
def test_5_the_mark_halts_naming_the_instrument(name: str) -> None:
    broken, reason = MARK_HALTS[name]
    with pytest.raises(Halt, match=reason):
        mark(broken, GLD, PRIOR, AT)


def test_5_two_bars_of_one_session_halt() -> None:
    twice = _with((*MARKS.bars[GLD]["1d"], MARKS.bars[GLD]["1d"][1]))
    with pytest.raises(Halt, match="GLD has 2 daily bars for 2026-11-03, not one"):
        mark(twice, GLD, PRIOR, AT)


def test_5_a_mark_is_later_than_the_session_it_is_compared_with() -> None:
    assert mark(MARKS, GLD, date(2026, 11, 2), AT) == d("100.5")
    with pytest.raises(Halt, match="not later than 2026-11-03"):
        mark(MARKS, GLD, TODAY, AT)


def test_another_instruments_quote_does_not_hold_this_one() -> None:
    beside = replace(MARKS, quotes={USO: quote(USO, 1, 1, AT)})
    assert mark(beside, GLD, PRIOR, AT) == d("100.5")


# The issue's own example: long 100 with a working sale of 30, and a sale of 90 to check.


def test_an_exposure_counts_open_sales_alone_and_open_buys_alone() -> None:
    holding = book({CHAN: {GLD: 100}}, opens=[order(-30, name="old-1")])
    assert exposure(holding, GLD) == Exposure(d(70), d(100))
    assert exposure(holding, GLD, order(-90)) == Exposure(d(-20), d(100))
    assert exposure(holding, GLD, order(-90)).size == 100
    assert exposure(holding, GLD, order(-190)).size == 120
    assert exposure(holding, GLD, order(5, USO)) == Exposure(d(70), d(100))
    assert exposure(holding, USO, order(5, USO)) == Exposure(d(0), d(5))


def test_an_exposure_counts_every_strategy_or_one() -> None:
    mixed = book(
        {CHAN: {GLD: 10}, OTHER: {GLD: -4}},
        opens=[order(3, name="a"), order(-5, strategy=OTHER, name="b")],
    )
    assert position(mixed, GLD) == 6
    assert position(mixed, GLD, OTHER) == -4
    assert exposure(mixed, GLD, order(1)) == Exposure(d(1), d(10))
    assert exposure(mixed, GLD, order(1), CHAN) == Exposure(d(10), d(14))
    assert exposure(mixed, GLD, order(1), OTHER) == Exposure(d(-9), d(-4))


def test_an_order_already_in_the_book_is_never_counted_twice() -> None:
    sale = order(-60, name="exit-1")
    working = book({CHAN: {GLD: 100}}, opens=[sale])
    assert exposure(working, GLD, sale) == Exposure(d(40), d(100))
    assert classify(working, sale) is Effect.REDUCES
    assert classify(working, order(-60, name="exit-2")) is Effect.ADDS


@pytest.mark.parametrize("flip", [1, -1], ids=["long", "short"])
def test_a_partly_filled_order_counts_at_its_remainder(flip: int) -> None:
    """Long 100 and a sale of 100, of which 40 filled: 60 held, and 60 still to sell."""
    whole = order(flip * -100, name="exit-1")
    part = book({CHAN: {GLD: flip * 60}}, opens=[order(flip * -60, name="exit-1")])
    reached = exposure(part, GLD, whole)
    assert sorted((reached.low, reached.high)) == sorted((d(0), d(flip * 60)))
    assert exposure(part, GLD, whole, CHAN) == reached
    assert classify(part, whole) is Effect.REDUCES
    # The part still open crosses zero, so it adds, whatever the first order was.
    past = book({CHAN: {GLD: flip * 59}}, opens=[order(flip * -60, name="exit-1")])
    assert classify(past, whole) is Effect.ADDS


@pytest.mark.parametrize(
    "changes",
    [
        {"instrument": USO},
        {"strategy": OTHER},
        {"qty": d(5)},
        {"type": core.OrderType.MARKET_ON_CLOSE},
        {"time_in_force": core.TimeInForce.CLS},
        {"qty": d(0)},
    ],
    ids=["instrument", "strategy", "side", "type", "time-in-force", "nothing-left"],
)
def test_another_order_under_the_same_id_halts(changes: dict) -> None:
    """Counting it as the same order would drop its exposure."""
    working = order(-5, name="one")
    other = replace(working, **changes)
    for holding, asked in (
        (book({CHAN: {GLD: 9}}, opens=[working]), other),
        (book({CHAN: {GLD: 9}}, opens=[other]), working),
    ):
        for read in (lambda b, i: exposure(b, GLD, i), classify):
            with pytest.raises(Halt, match="the book holds another order under the id one"):
                read(holding, asked)


@pytest.mark.parametrize("flip", [1, -1], ids=["buy", "sale"])
def test_more_left_open_than_the_order_asked_for_halts(flip: int) -> None:
    asked = order(flip * 5, name="one")
    for left, same in ((5, True), (QTY, True), (5 + QTY, False), (0, False), (-QTY, False)):
        working = book(opens=[order(flip * left, name="one")])
        if same:
            assert exposure(working, GLD, asked) == exposure(working, GLD)
        else:
            with pytest.raises(Halt, match="the book holds another order under the id one"):
                exposure(working, GLD, asked)


def test_held_names_what_any_strategy_holds_or_has_an_order_in() -> None:
    mixed = book({CHAN: {GLD: 1, USO: 0}, OTHER: {GLD: -1}}, opens=[order(1, SPY, OTHER, name="a")])
    assert held(mixed) == (GLD, SPY)
    assert held(book()) == ()


@pytest.mark.parametrize("flip", [1, -1], ids=["long", "short"])
def test_3_an_order_reduces_up_to_flat_and_no_further(flip: int) -> None:
    holding = book({CHAN: {GLD: flip * 10}})
    for qty, effect in (
        (-10 + QTY, Effect.REDUCES),
        (d(-10), Effect.REDUCES),
        (-10 - QTY, Effect.ADDS),
        (QTY, Effect.ADDS),
    ):
        assert classify(holding, order(flip * qty)) is effect, qty
    assert classify(book(), order(flip * QTY)) is Effect.ADDS


@pytest.mark.parametrize("flip", [1, -1], ids=["long", "short"])
def test_3_an_order_in_doubt_on_either_side_is_read_both_ways(flip: int) -> None:
    sale = book({CHAN: {GLD: flip * 10}}, opens=[order(flip * -4, name="d")], in_doubt=(GLD,))
    assert classify(sale, order(flip * -6)) is Effect.REDUCES
    assert classify(sale, order(flip * (-6 - QTY))) is Effect.ADDS
    buy = book({CHAN: {GLD: flip * 10}}, opens=[order(flip * 4, name="d")], in_doubt=(GLD,))
    assert classify(buy, order(flip * -10)) is Effect.REDUCES
    assert classify(buy, order(flip * (-10 - QTY))) is Effect.ADDS


@pytest.mark.parametrize("flip", [1, -1], ids=["long", "short"])
def test_3_an_order_reduces_the_strategys_position_and_the_accounts(flip: int) -> None:
    # The account is short what the strategy is long, so a sale adds to the account.
    other = book({CHAN: {GLD: flip * 10}, OTHER: {GLD: flip * -15}})
    assert classify(other, order(flip * -5)) is Effect.ADDS
    # The strategy is short what the account is long, so a sale adds to the strategy.
    mine = book({CHAN: {GLD: flip * -5}, OTHER: {GLD: flip * 15}})
    assert classify(mine, order(flip * -5)) is Effect.ADDS
    # A sale past the strategy's own position adds, though the account has more.
    both = book({CHAN: {GLD: flip * 5}, OTHER: {GLD: flip * 15}})
    assert classify(both, order(flip * -5)) is Effect.REDUCES
    assert classify(both, order(flip * (-5 - QTY))) is Effect.ADDS
    # And one past the account's adds, though the strategy has more.
    less = book({CHAN: {GLD: flip * 15}, OTHER: {GLD: flip * -10}})
    assert classify(less, order(flip * -5)) is Effect.REDUCES
    assert classify(less, order(flip * (-5 - QTY))) is Effect.ADDS


def test_5_a_book_or_an_order_that_is_not_finite_halts_naming_it() -> None:
    with pytest.raises(Halt, match="chan's position in GLD is .*NaN"):
        exposure(book({CHAN: {GLD: "NaN"}}), GLD)
    with pytest.raises(Halt, match="the order old-1 is .*NaN"):
        exposure(book(opens=[order("NaN", name="old-1")]), GLD)
    for qty in ("Infinity", "NaN", "sNaN"):
        with pytest.raises(Halt, match=f"the order new-1 is .*{qty}"):
            classify(book(), order(qty))
    with pytest.raises(Halt, match="the book files o, an order of 'chan', under 'other'"):
        exposure(replace(book(), open_orders_by_strategy={OTHER: (order(1, name="o"),)}), GLD)


# V, the book at one session's closes.


def test_v_is_cash_plus_each_position_at_its_sessions_close() -> None:
    holding = book({CHAN: {GLD: 10, USO: -3}, OTHER: {GLD: 2, USO: 3}}, cash=100)
    v = value_book(holding, MARKS, PRIOR)
    assert v == Valuation(PRIOR, d(1288), d(100), {GLD: d(12)}, {GLD: d(99)}, 7)
    assert value_book(book(cash="7.25"), MARKS, PRIOR).value == d("7.25")


def test_5_v_halts_without_its_sessions_bar_though_an_older_and_a_newer_are_there() -> None:
    holding = book({CHAN: {GLD: 1}})
    for session in (PRIOR, TODAY):
        assert value_book(holding, MARKS, session).session == session
    later = date(2026, 11, 5)
    gap = snapshot({GLD: 5}, day=later, at=TABLE.session(later).close)
    old, new = gap.bars[GLD]["1d"]
    gap = replace(gap, bars={GLD: {"1d": (replace(old, session=PRIOR), new)}})
    with pytest.raises(Halt, match="GLD has 0 daily bars for 2026-11-03, not one"):
        value_book(holding, gap, TODAY)


def test_5_v_halts_on_what_the_mark_halts_on() -> None:
    holding = book({CHAN: {GLD: 1}})
    with pytest.raises(Halt, match="GLD stale for the session"):
        value_book(holding, _status(GLD, BarStatus.STALE), PRIOR)
    with pytest.raises(Halt, match="the book's cash is"):
        value_book(book(cash="NaN"), MARKS, PRIOR)
    with pytest.raises(Halt, match="GLD missing for the session"):
        value_book(holding, snapshot({USO: 1}), PRIOR)
    with pytest.raises(Halt, match="after the decision's session"):
        value_book(holding, _bars(session=date(2026, 11, 4)), PRIOR)


def test_v_is_rounded_once_to_four_places_from_the_exact_sum() -> None:
    marks = snapshot({GLD: 1}, prior={GLD: "0.00005"})
    # 3 at 0.00005 is 0.00015, which a half rounded to even makes 0.0002.
    assert value_book(book({CHAN: {GLD: 3}}, cash=0), marks, PRIOR).value == d("0.0002")
    assert value_book(book({CHAN: {GLD: 1}}, cash=0), marks, PRIOR).value == d(0)


# The exact arithmetic.


def test_a_sum_and_a_product_are_exact_or_halt() -> None:
    assert exact_total("x", [d("1E+20"), d("1E-20"), d("-1E+20")]) == d("1E-20")
    wide = Decimal("9" * 38)
    assert exact_product("x", wide, wide, wide, wide) == int(wide) ** 4
    with pytest.raises(Halt, match="the sum has no exact sum: Inexact"):
        exact_total("the sum", [d("1E+200"), d(1)])
    with pytest.raises(Halt, match="the size has no exact product: Inexact"):
        exact_product("the size", wide, wide, wide, wide, wide)


@pytest.mark.parametrize("value", [d("NaN"), d("sNaN"), d("Infinity"), 1.0, 1, None, "1"])
def test_a_value_that_is_no_finite_decimal_halts(value: object) -> None:
    with pytest.raises(Halt, match="the size is .*, not a finite decimal"):
        finite("the size", value)
    with pytest.raises(Halt, match="the size is"):
        exact_total("the size", [d(1), value])
    with pytest.raises(Halt, match="the size is"):
        exact_product("the size", d(1), value)
    with pytest.raises(Halt, match="the size is"):
        quotient("the size", d(1), value, 4)
    with pytest.raises(Halt, match="the size is"):
        rounded("the size", value, 4)


def test_worth_is_cash_and_each_holding_at_its_price() -> None:
    assert worth("x", d(5), {GLD: d(2), USO: d(-3)}, {GLD: d(10), USO: d(4), SPY: d(9)}) == 13
    with pytest.raises(Halt, match="x, of USO is .*not a finite decimal"):
        worth("x", d(5), {USO: d(1)}, {USO: d("NaN")})


def test_a_quotient_and_a_record_round_the_way_they_are_asked() -> None:
    assert quotient("x", d(2), d(3), 4) == d("0.6667")
    assert quotient("x", d(2), d(3), 4, ROUND_DOWN) == d("0.6666")
    assert rounded("x", d("0.00005"), 4) == d(0)
    assert rounded("x", d("0.00001"), 4, ROUND_CEILING) == d("0.0001")
    with pytest.raises(Halt, match="x cannot be divided"):
        quotient("x", d(1), d(0), 4)
    with pytest.raises(Halt, match="x cannot be recorded at 4 places"):
        rounded("x", d("1E+31"), 4)


# The records refuse to contradict themselves.


def _verdict(**changes: object) -> core.Verdict:
    fields: dict[str, object] = {
        "approved": True,
        "limit": None,
        "value": None,
        "bound": None,
        "missing_term": None,
        "adding": True,
        "worst_loss": d(1),
        "stress_move": d("0.2"),
        "loss": core.LossReading(d(1), d(2), False, PRIOR),
        "past": (),
    }
    return core.Verdict(**fields | changes)  # type: ignore[arg-type]


NET = core.PastLimit(core.Limit.MAX_NET, d(60), d(50))
GROSS = core.PastLimit(core.Limit.MAX_GROSS, d(60), d(50))
PAST_STOP = core.LossReading(d(3), d(2), True, PRIOR)
REFUSED: dict[str, object] = {"approved": False, "value": d(60), "bound": d(50)}
ON_NET = REFUSED | {"limit": core.Limit.MAX_NET, "past": (NET,)}
CONTRADICTIONS: dict[str, tuple[dict[str, object], str]] = {
    "an-approval-naming-a-limit": (ON_NET | {"approved": True}, "an approval names max_net"),
    "a-refusal-naming-none": ({"approved": False}, "a refusal names no limit"),
    "a-limit-that-is-a-string": (ON_NET | {"limit": "max_net"}, "limit 'max_net' is not a limit"),
    "a-limit-with-no-value": (ON_NET | {"value": None}, "carries a value of None"),
    "a-limit-with-no-bound": (ON_NET | {"bound": None}, "and a bound of None"),
    **{
        f"a-{state}-with-a-figure": (
            REFUSED
            | {"limit": state}
            | ({"missing_term": "position"} if state == "position" else {}),
            f"a verdict on {state} carries a value of 60",
        )
        for state in (
            core.Limit.STOP,
            core.Limit.CLOSE_ENTRY,
            core.Limit.IN_DOUBT,
            core.Limit.POSITION,
        )
    },
    "an-approval-with-a-figure": ({"value": d(1)}, "a verdict on None carries a value of 1"),
    "an-approval-with-a-bound": ({"bound": d(1)}, "and a bound of 1"),
    "an-approval-missing-a-term": ({"missing_term": "position"}, "a verdict on None lacks"),
    "a-position-refusal-missing-none": (
        {"approved": False, "limit": core.Limit.POSITION},
        "a verdict on position lacks None",
    ),
    "past-left-out-of-a-refusal": (ON_NET | {"past": (GROSS,)}, "leaves it out of past"),
    "past-out-of-order": (ON_NET | {"past": (NET, GROSS)}, "out of order or twice"),
    "past-twice": (ON_NET | {"past": (NET, NET)}, "out of order or twice"),
    "an-approval-that-adds-past-one": ({"past": (NET,)}, "an approval that adds is past max_net"),
    "a-loss-refusal-inside-the-stop": (
        REFUSED | {"limit": core.Limit.LOSS_STOP},
        "a refusal on loss_stop reads a loss inside the stop",
    ),
    "an-approval-with-no-worst-loss": ({"worst_loss": None}, "carries a worst loss of None"),
    "an-exit-approved-with-no-worst-loss": (
        {"worst_loss": None, "adding": False},
        "carries a worst loss of None",
    ),
    "an-approval-whose-worst-loss-is-not-finite": (
        {"worst_loss": d("NaN")},
        "carries a worst loss of NaN",
    ),
    "a-stress-move-of-zero": ({"stress_move": d(0)}, "carries a stress move of 0"),
    "a-stress-move-below-zero": ({"stress_move": d("-0.2")}, "carries a stress move of -0.2"),
    "a-stress-move-that-is-not-finite": (
        {"stress_move": d("NaN")},
        "carries a stress move of NaN",
    ),
    "an-approval-that-adds-nothing-to-lose": ({"worst_loss": d(0)}, "a worst loss of 0"),
}


@pytest.mark.parametrize("name", CONTRADICTIONS)
def test_5_a_verdict_that_contradicts_itself_is_refused(name: str) -> None:
    changes, reason = CONTRADICTIONS[name]
    with pytest.raises(ValueError, match=reason):
        _verdict(**changes)


def test_5_a_verdict_that_agrees_with_itself_builds() -> None:
    for changes in (
        {},
        # A close after a broker's trade goes out past a fired stop, once #117 reads its entry.
        {"adding": False, "loss": PAST_STOP},
        ON_NET,
        {"adding": False, "past": (GROSS, NET), "worst_loss": d(0)},
        REFUSED | {"limit": core.Limit.LOSS_STOP, "loss": PAST_STOP},
        REFUSED | {"limit": core.Limit.MAX_NET, "loss": PAST_STOP, "past": (GROSS, NET)},
        {"approved": False, "limit": core.Limit.STOP, "loss": PAST_STOP, "past": (NET,)},
        {
            "approved": False,
            "limit": core.Limit.POSITION,
            "missing_term": "position",
            "worst_loss": None,
        },
    ):
        made = _verdict(**changes)
        assert core.decode(core.encode(made), core.Verdict) == made


def test_5_decoding_a_verdict_that_contradicts_itself_is_a_codec_error() -> None:
    data = core.encode(_verdict(**ON_NET)).replace(b'"approved":false', b'"approved":true')
    with pytest.raises(core.CodecError, match="an approval names max_net"):
        core.decode(data, core.Verdict)
    data = core.encode(_verdict(**ON_NET)).replace(b'"limit":"max_net"', b'"limit":"max_cost"', 1)
    with pytest.raises(core.CodecError, match="'max_cost', no Limit"):
        core.decode(data, core.Verdict)


def test_5_a_past_limit_is_one_of_the_three_and_over_its_bound() -> None:
    with pytest.raises(ValueError, match="is not max_position, max_gross or max_net"):
        core.PastLimit(core.Limit.MAX_ORDER_QTY, d(60), d(50))
    # Rounded for the record, a figure past its bound can equal it and never be below it.
    assert core.PastLimit(core.Limit.MAX_NET, d(50), d(50))
    for value, bound in ((50 - QTY, d(50)), (d("NaN"), d(50)), (d(60), d("NaN"))):
        with pytest.raises(ValueError, match="is not past"):
            core.PastLimit(core.Limit.MAX_NET, value, bound)


def test_5_a_loss_reading_says_past_only_where_it_is() -> None:
    for loss in (d(2), d("1.9999")):
        assert not core.LossReading(loss, d(2), False, PRIOR).past
    for loss in (d(2), d("2.0001")):
        assert core.LossReading(loss, d(2), True, PRIOR).past
    for loss, bound, past in ((d("2.0001"), d(2), False), (d("1.9999"), d(2), True)):
        with pytest.raises(ValueError, match="does not read past as"):
            core.LossReading(loss, bound, past, PRIOR)
    for loss, bound in ((d("NaN"), d(2)), (d(2), d("Infinity"))):
        with pytest.raises(ValueError, match="is not finite"):
            core.LossReading(loss, bound, False, PRIOR)


def test_5_a_valuation_that_contradicts_itself_is_refused() -> None:
    fields = (PRIOR, d("12.0000"), d(2), {GLD: d(1)}, {GLD: d(10), USO: d(1)}, 7)
    made = Valuation(*fields)
    for changes, reason in (
        ({"value": d("12.0001")}, "a valuation of 12.0001 is not its cash and positions"),
        ({"cash": d("2.0001")}, "a valuation of 12.0000 is not its cash and positions"),
        ({"positions": {GLD: d(1), SPY: d(1)}}, "a valuation holds SPY with no mark"),
        ({"cash": d("NaN")}, "is not finite|not a finite decimal"),
        ({"marks": {GLD: d("Infinity")}}, "not a finite decimal"),
        ({"positions": {GLD: d("1E+200")}, "cash": d(1)}, "no exact sum"),
    ):
        with pytest.raises(ValueError, match=reason):
            replace(made, **changes)
    # A valuation stored at four places, rounded from more, stays readable.
    stored = (
        b'{"cash":"2","marks":{"GLD":"10.1234"},"positions":{"GLD":"1.234567"},'
        b'"replayed_through":7,"session":"2026-11-02","value":"14.4980"}'
    )
    assert core.decode(stored, Valuation).value == d("14.4980")
    data = core.encode(made).replace(b'"value":"12.0000"', b'"value":"13.0000"')
    with pytest.raises(core.CodecError, match="a valuation of 13.0000 is not its cash"):
        core.decode(data, Valuation)
    data = core.encode(made).replace(b'"marks":{"GLD"', b'"marks":{"SPY"')
    with pytest.raises(core.CodecError, match="a valuation holds GLD with no mark"):
        core.decode(data, Valuation)
