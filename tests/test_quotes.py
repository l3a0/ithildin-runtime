"""The one judge of a quote, its midpoint and the mark that reads it.

Each fault is tried one step below, at and above its boundary, and each reason
is asserted whole. Every price, age and spread is made up.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from ithildinruntime.core import (
    CLOCK_BOUND,
    Halt,
    Quote,
    Snapshot,
    decode,
    encode,
    mark,
    maximum_faults,
    midpoint,
    quote_faults,
    require_quote,
    value_book,
)
from ithildinruntime.core.codec import INT_LIMIT
from tests.support.gates import (
    CHAN,
    CLOSE,
    DECISION,
    GLD,
    PRIOR,
    TABLE,
    TODAY,
    USO,
    book,
    d,
    snapshot,
    utc,
)
from tests.support.quotes import MAX_AGE, MAX_SPREAD, quote, read_at

TICK = timedelta(microseconds=1)
OPEN = TABLE.session(TODAY).open
AGE = timedelta(seconds=MAX_AGE)
STAMP = DECISION - timedelta(seconds=59)
CAPTURE = STAMP + timedelta(seconds=1)
MILLIS = str(int(STAMP.timestamp() * 1000))
GOOD = quote(GLD, "100", "100.5", DECISION)


def faults(now: datetime = DECISION, **changes: object) -> tuple[str, ...]:
    return quote_faults(replace(GOOD, **changes), DECISION, now)


def test_a_quote_with_no_fault_gives_no_reason() -> None:
    assert faults() == ()
    assert quote_faults(decode(encode(GOOD), Quote), DECISION, DECISION) == ()


def test_the_clock_bound_is_one_second() -> None:
    assert CLOCK_BOUND == timedelta(seconds=1)


# The maxima.


@pytest.mark.parametrize("age", [10, 11, INT_LIMIT - 1])
def test_a_maximum_age_in_bounds_is_no_fault(age: int) -> None:
    assert maximum_faults(age, MAX_SPREAD) == ()


@pytest.mark.parametrize(
    "age", [None, 0, -10, 9, 9.5, 10.0, True, "90", Decimal(90), INT_LIMIT, 10**40]
)
def test_a_maximum_age_out_of_bounds_is_one_fault_and_never_an_error(age: object) -> None:
    reason = (
        f"GLD is read under a maximum age of {age!r}, which is not a whole number of seconds "
        f"from 10 to below {2**53}"
    )
    assert faults(max_age=age) == (reason,)


@pytest.mark.parametrize("share", ["0.0000001", "0.5", "0.9999999"])
def test_a_maximum_spread_in_bounds_is_no_fault(share: str) -> None:
    assert maximum_faults(90, Decimal(share)) == ()


@pytest.mark.parametrize(
    "share",
    [None, 0, 1, 0.5, True, "0.5", *map(Decimal, ("0", "1", "-0.1", "1.1", "NaN", "Infinity"))],
)
def test_a_maximum_spread_out_of_bounds_is_one_fault_and_never_an_error(share: object) -> None:
    reason = (
        f"GLD is read under a maximum spread of {share!r}, which is not a decimal above 0 and "
        "below 1"
    )
    assert faults(max_spread_share=share) == (reason,)


# The time it is read at.


@pytest.mark.parametrize("after", [timedelta(0), TICK])
def test_a_quote_read_at_or_after_the_decision_is_no_fault(after: timedelta) -> None:
    assert faults(DECISION + after) == ()


@pytest.mark.parametrize(
    ("now", "shown"),
    [
        (DECISION - TICK, "2026-11-03T20:39:59.999999+00:00"),
        (DECISION.replace(tzinfo=None), "2026-11-03T20:40:00"),
        (None, "None"),
        ("2026-11-03", "'2026-11-03'"),
    ],
)
def test_a_quote_read_before_the_decision_or_at_no_utc_time_is_a_fault(
    now: object, shown: str
) -> None:
    assert faults(now) == (  # type: ignore[arg-type]
        f"now is {shown}, not a UTC time from the decision at 2026-11-03T20:40:00+00:00 on",
    )


# The prices.


@pytest.mark.parametrize("side", ["bid", "ask"])
@pytest.mark.parametrize(
    ("price", "received", "shown"),
    [
        (None, None, "None"),
        (None, "nan", "None"),
        (Decimal("NaN"), "x", "NaN"),
        (Decimal("Infinity"), "x", "Infinity"),
        (Decimal(0), "0.0", "0"),
        (Decimal("-0.0001"), "-0.0001", "-0.0001"),
        (100.0, "100.0", "100.0"),
    ],
)
def test_a_side_with_no_price_above_zero_is_a_fault_naming_what_was_received(
    side: str, price: object, received: str | None, shown: str
) -> None:
    changed = {side: price, "received": {**GOOD.received, side: received}}
    assert faults(**changed) == (
        f"its {side} is {shown}, received as {received!r}, and not a price above zero",
    )


def test_both_sides_are_named_where_neither_has_a_price() -> None:
    assert len(faults(bid=None, ask=None)) == 2


@pytest.mark.parametrize(
    ("bid", "fine"), [("100.4999", True), ("100.5", True), ("100.5001", False)]
)
def test_an_ask_below_the_bid_is_a_fault_and_one_at_it_is_not(bid: str, fine: bool) -> None:
    said = (f"its ask of 100.5 is below its bid of {bid}",)
    assert faults(bid=Decimal(bid)) == (() if fine else said)


@pytest.mark.parametrize(
    ("ask", "fine"), [("102.0202", True), ("102.0203", False), ("102.02020201", True)]
)
def test_a_spread_past_the_written_share_of_the_midpoint_is_a_fault(ask: str, fine: bool) -> None:
    """At 2%, 100 by 102.020202... is the boundary, and no price sits on it at four places."""
    said = f"its bid of 100 and ask of {ask} are further apart than the written 0.02 of "
    assert faults(ask=Decimal(ask)) == (() if fine else (said + "their midpoint",))


@pytest.mark.parametrize(("ask", "fine"), [("119.9999", True), ("120", True), ("120.0001", False)])
def test_a_spread_at_the_written_share_is_no_fault(ask: str, fine: bool) -> None:
    """Of 80 by 120 the spread is 40, and 0.4 of the midpoint of 100 is 40 exactly."""
    found = faults(bid=Decimal(80), ask=Decimal(ask), max_spread_share=Decimal("0.4"))
    assert (found == ()) is fine


def test_a_spread_that_has_no_exact_comparison_is_a_fault() -> None:
    long = Decimal("1." + "3" * 200)
    (reason,) = faults(bid=Decimal(1), ask=long, max_spread_share=Decimal("0.9"))
    assert reason.endswith("are further apart than the written 0.9 of their midpoint")


# What the vendor said.


@pytest.mark.parametrize("flag", [False, None, "True", 1])
def test_a_quote_not_flagged_realtime_is_a_fault(flag: object) -> None:
    received = {**GOOD.received, "realtime": None if flag is None else str(flag)}
    assert faults(realtime=flag, received=received) == (
        f"its realtime flag is received as {received['realtime']!r}, and only true says its "
        "delay is none",
    )


@pytest.mark.parametrize(
    ("status", "normal"),
    [
        (None, "Normal"),
        ("", "Normal"),
        ("Halted", "Normal"),
        ("normal", "Normal"),
        ("Normal", "unstated"),
        ("Normal", ""),
        ("Normal", None),
        ("unstated", "unstated"),
        (None, None),
    ],
)
def test_a_status_that_is_not_the_stated_normal_one_is_a_fault(
    status: object, normal: object
) -> None:
    assert faults(status=status, normal_status=normal) == (
        f"its status is {status!r}, and the stated normal one is {normal!r}",
    )


@pytest.mark.parametrize("kind", ["unstated", "", " ", None])
def test_an_unstated_price_kind_is_a_fault_naming_the_instrument(kind: object) -> None:
    assert faults(price_kind=kind) == ("GLD's quote is of unstated price kind",)


def test_what_is_recorded_and_not_judged_is_no_fault() -> None:
    """Venues, volume counting and the last sale are kept, and nothing here reads them."""
    changed = {
        "venues": "unstated",
        "volume_counted": "unstated",
        "last_kind": "unstated",
        "last": None,
        "last_at": None,
        "volume": None,
        "fetched_at": None,
    }
    assert faults(**changed) == ()
    for name, value in changed.items():
        assert faults(**{name: value}) == ()


# The stamps.


@pytest.mark.parametrize("side", ["bid", "ask"])
@pytest.mark.parametrize("at", [None, STAMP.replace(tzinfo=None), "2026-11-03"])
def test_a_side_with_no_stamp_is_a_fault(side: str, at: object) -> None:
    received = {**GOOD.received, f"{side}_time": "99"}
    assert faults(**{f"{side}_at": at, "received": received}) == (
        f"its {side} has the stamp '99', which is no instant",
    )


@pytest.mark.parametrize("side", ["bid", "ask"])
@pytest.mark.parametrize(("step", "fine"), [(-TICK, True), (timedelta(0), True), (TICK, False)])
def test_a_stamp_later_than_its_capture_by_more_than_the_bound_is_a_fault(
    side: str, step: timedelta, fine: bool
) -> None:
    at = CAPTURE + CLOCK_BOUND + step
    said = (
        f"its {side} has the stamp '{MILLIS}', at {at.isoformat()}, which is later than its "
        f"capture at {CAPTURE.isoformat()}"
    )
    assert faults(**{f"{side}_at": at}) == (() if fine else (said,))


@pytest.mark.parametrize("older", ["bid", "ask"])
@pytest.mark.parametrize(("step", "fine"), [(-TICK, True), (timedelta(0), True), (TICK, False)])
def test_the_age_is_the_older_sides_with_the_clock_bound_added(
    older: str, step: timedelta, fine: bool
) -> None:
    """One side that froze cannot hide behind one that moved."""
    at = DECISION - AGE + CLOCK_BOUND - step
    changed = {f"{older}_at": at}
    said = (
        f"its {older} has the stamp '{MILLIS}', at {at.isoformat()}, which is 89.000001 seconds "
        "old at 2026-11-03T20:40:00+00:00, and with the clock bound of 1 past the written "
        f"maximum of 90, and it was captured at {CAPTURE.isoformat()}"
    )
    assert faults(**changed) == (() if fine else (said,))


@pytest.mark.parametrize(("step", "fine"), [(-TICK, True), (timedelta(0), True), (TICK, False)])
def test_the_age_is_measured_at_now_and_not_at_the_decision(step: timedelta, fine: bool) -> None:
    now = STAMP + AGE - CLOCK_BOUND + step
    assert now > DECISION
    assert faults(DECISION) == ()
    assert (faults(now) == ()) is fine


def test_an_age_far_past_any_date_is_compared_and_not_an_error() -> None:
    assert faults(max_age=INT_LIMIT - 1) == ()
    assert faults(bid_at=utc(1, 1, 1), max_age=INT_LIMIT - 1) == ()
    assert len(faults(bid_at=utc(1, 1, 1))) == 1


@pytest.mark.parametrize(
    ("captured", "fine"),
    [
        (OPEN - TICK, False),
        (OPEN, True),
        (DECISION, True),
        (DECISION + TICK, False),
        (None, False),
        (CAPTURE.replace(tzinfo=None), False),
    ],
)
def test_a_capture_outside_the_decisions_session_so_far_is_a_fault(
    captured: object, fine: bool
) -> None:
    shown = captured.isoformat() if isinstance(captured, datetime) else repr(captured)
    said = (
        f"it was captured at {shown}, not from the open at {OPEN.isoformat()} to the decision at "
        f"{DECISION.isoformat()}"
    )
    found = quote_faults(
        replace(GOOD, captured_at=captured, max_age=10**6),  # type: ignore[arg-type]
        DECISION,
        DECISION,
    )
    late = [f for f in found if "later than its capture" not in f]
    assert late == ([] if fine else [said])


def test_a_quote_of_yesterday_is_a_fault_at_todays_decision() -> None:
    old = CLOSE - timedelta(days=1, minutes=20)
    assert any("not from the open" in f for f in faults(captured_at=old, bid_at=old, ask_at=old))


def test_a_decision_outside_the_session_table_is_a_fault_and_never_an_error() -> None:
    at = utc(1990, 1, 2, 18)
    found = quote_faults(GOOD, at, at)
    assert any("from the open at no session" in f for f in found)
    found = quote_faults(GOOD, None, None)  # type: ignore[arg-type]
    assert any("to the decision at None" in f for f in found)


def test_every_reason_is_given_at_once_in_the_tables_order() -> None:
    found = faults(
        DECISION - TICK,
        max_age=None,
        max_spread_share=None,
        bid=None,
        realtime=False,
        status="Halted",
        price_kind="unstated",
        bid_at=None,
        captured_at=DECISION + TICK,
    )
    starts = [
        "GLD is read under a maximum age",
        "GLD is read under a maximum spread",
        "now is",
        "its bid is None",
        "its realtime flag",
        "its status",
        "GLD's quote is of unstated price kind",
        "its bid has the stamp",
        "it was captured",
    ]
    assert len(found) == len(starts)
    assert all(f.startswith(s) for f, s in zip(found, starts, strict=True))


def test_a_quote_whose_row_is_no_mapping_is_judged_all_the_same() -> None:
    (reason,) = faults(bid=None, received=None)
    assert "received as nothing" in reason


# The midpoint, and what reads one.


def test_the_midpoint_is_exact_at_five_places() -> None:
    assert midpoint(replace(GOOD, bid=d("100.0001"), ask=d("100.0002"))) == d("100.00015")
    assert midpoint(GOOD) == d("100.25")


@pytest.mark.parametrize("side", ["bid", "ask"])
@pytest.mark.parametrize("price", [None, Decimal("NaN"), 100.0])
def test_a_midpoint_with_no_price_halts(side: str, price: object) -> None:
    with pytest.raises(Halt, match=f"GLD's {side} is .*, not a finite decimal"):
        midpoint(replace(GOOD, **{side: price}))


def quoted(
    quotes: Mapping[str, Quote], at: datetime = DECISION, today: bool = False, **changes: object
) -> Snapshot:
    """A snapshot at close−20 with these quotes, whose newest bars are of the session before."""
    made = snapshot({GLD: 98, USO: 10}, at=at, prior={GLD: 97, USO: 11})
    if not today:
        made = replace(made, bars={i: {"1d": by["1d"][:1]} for i, by in made.bars.items()})
    return replace(made, quotes=dict(quotes), **read_at(at) | changes)


BOTH = {GLD: GOOD, USO: quote(USO, "10", "10.0001", DECISION)}
BEFORE = TABLE.before(PRIOR).day


def test_require_quote_gives_the_quote_or_every_reason_it_cannot_be_read() -> None:
    assert require_quote(quoted(BOTH), GLD, DECISION) is GOOD
    with pytest.raises(Halt) as absent:
        require_quote(quoted({USO: BOTH[USO]}), GLD, DECISION)
    assert absent.value.reason == "GLD has no quote in the snapshot"
    bad = replace(GOOD, realtime=False, price_kind="unstated")
    with pytest.raises(Halt) as refused:
        require_quote(quoted({GLD: bad}), GLD, DECISION)
    assert refused.value.reason == "GLD's quote cannot be read: " + "; ".join(
        quote_faults(bad, DECISION, DECISION)
    )


def test_a_read_before_the_decision_halts_through_the_mark_and_require_quote() -> None:
    made = quoted(BOTH)
    for read in (
        lambda: require_quote(made, GLD, DECISION - TICK),
        lambda: mark(made, GLD, PRIOR, DECISION - TICK),
    ):
        with pytest.raises(Halt, match="now is 2026-11-03T20:39:59.999999"):
            read()


def test_a_quote_captured_after_the_decision_halts_at_a_later_now() -> None:
    late = DECISION + timedelta(seconds=5)
    after = replace(GOOD, captured_at=late)
    for now in (late, late + timedelta(seconds=1)):
        with pytest.raises(Halt, match="it was captured at 2026-11-03T20:40:05"):
            require_quote(quoted({GLD: after}), GLD, now)


def test_5_the_mark_is_the_midpoint_where_the_snapshot_holds_a_quote() -> None:
    made = quoted(BOTH)
    assert made.bars[GLD]["1d"][-1].session == PRIOR
    assert mark(made, GLD, PRIOR, DECISION) == d("100.25")
    assert mark(made, USO, PRIOR, DECISION) == d("10.00005")


def test_5_the_mark_is_the_close_where_the_snapshot_holds_no_quote_of_it() -> None:
    made = quoted({USO: BOTH[USO]}, today=True)
    assert mark(made, GLD, PRIOR, DECISION) == 98
    # With no bar of the session either, a close V was valued at is no mark.
    with pytest.raises(Halt, match="GLD has no mark: its newest bar is for 2026-11-02"):
        mark(quoted({USO: BOTH[USO]}), GLD, PRIOR, DECISION)


def test_5_a_quote_beside_a_bar_of_the_decisions_session_halts() -> None:
    with pytest.raises(Halt, match="GLD has a quote and a bar for 2026-11-03"):
        mark(quoted(BOTH, today=True), GLD, PRIOR, DECISION)


def test_5_a_quote_of_the_session_it_is_compared_with_is_no_mark() -> None:
    assert mark(quoted(BOTH), GLD, BEFORE, DECISION) == d("100.25")
    for after in (TODAY, date(2026, 11, 4)):
        with pytest.raises(Halt, match="GLD has no mark: its quote is for 2026-11-03, not later"):
            mark(quoted(BOTH), GLD, after, DECISION)


CLOSED = TABLE.session(PRIOR).close


def test_5_the_stamp_is_held_to_the_close_of_the_session_it_is_compared_with() -> None:
    between = replace(GOOD, bid_at=CLOSED - timedelta(hours=1), max_age=100_000)
    assert mark(quoted({GLD: between}), GLD, BEFORE, DECISION) == d("100.25")


@pytest.mark.parametrize("older", ["bid", "ask"])
@pytest.mark.parametrize(("step", "marks"), [(-TICK, False), (timedelta(0), False), (TICK, True)])
def test_5_a_quote_stamped_at_or_before_the_close_v_was_valued_at_is_no_mark(
    older: str, step: timedelta, marks: bool
) -> None:
    """Captured today and within a maximum age longer than the night, it would read no loss."""
    old = replace(GOOD, **{f"{older}_at": CLOSED + step}, max_age=86_400)
    made = quoted({**BOTH, GLD: old})
    assert quote_faults(old, DECISION, DECISION) == ()
    if marks:
        assert mark(made, GLD, PRIOR, DECISION) == d("100.25")
        return
    with pytest.raises(Halt) as halted:
        mark(made, GLD, PRIOR, DECISION)
    assert halted.value.reason == (
        f"GLD has no mark: its quote's older stamp is {(CLOSED + step).isoformat()}, not later "
        f"than the close of 2026-11-02 at {CLOSED.isoformat()}"
    )


def test_5_the_mark_judges_the_quote_at_the_now_it_is_handed() -> None:
    late = STAMP + AGE - CLOCK_BOUND
    assert mark(quoted(BOTH), GLD, PRIOR, late) == d("100.25")
    with pytest.raises(Halt, match="GLD's quote cannot be read: its ask has the stamp"):
        mark(quoted(BOTH), GLD, PRIOR, late + TICK)


def test_5_the_bars_are_checked_under_a_quote_and_the_one_close_is_not() -> None:
    """A bar's kind and its price are read only where its close is the mark."""
    made = quoted(BOTH)
    (bar,) = made.bars[GLD]["1d"]
    for change in ({"price_kind": "unstated"}, {"close": d(0)}, {"close": d("NaN")}):
        bars = {**made.bars, GLD: {"1d": (replace(bar, **change),)}}
        assert mark(replace(made, bars=bars), GLD, PRIOR, DECISION) == d("100.25")
    late = {**made.bars, GLD: {"1d": (replace(bar, knowable_at=DECISION + TICK),)}}
    with pytest.raises(Halt, match="GLD's bar for 2026-11-02 was knowable at"):
        mark(replace(made, bars=late), GLD, PRIOR, DECISION)
    with pytest.raises(Halt, match="USO has a bar and no status"):
        mark(replace(made, status={GLD: made.status[GLD]}, bars=made.bars), GLD, PRIOR, DECISION)


def test_26_v_reads_bars_alone_beside_a_quote() -> None:
    assert value_book(book({CHAN: {GLD: 1}}, cash=0), quoted(BOTH), PRIOR).value == 97


# The record.


def test_17_a_snapshot_of_two_captures_is_refused() -> None:
    other = replace(BOTH[USO], captured_at=CAPTURE + TICK)
    with pytest.raises(ValueError, match="quotes are of one capture, not of 2026-11-03T20:39:02"):
        quoted({GLD: GOOD, USO: other})


@pytest.mark.parametrize(("provisional", "at"), [(True, None), (False, DECISION)])
def test_16_a_snapshot_says_when_its_quotes_were_read_exactly_where_it_read_them(
    provisional: bool, at: datetime | None
) -> None:
    with pytest.raises(ValueError, match="has quotes_read_at"):
        replace(quoted(BOTH), provisional=provisional, quotes_read_at=at)


def test_16_a_provisional_snapshot_reads_back_with_every_field() -> None:
    made = quoted(BOTH, faults=("a reason",), rows={GLD: ({"bid": "1.5", "extra": None},)})
    assert decode(encode(made), Snapshot) == made
