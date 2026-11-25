"""The spike flag's rules on a bar and on a quote, and what the multiple is stated beside.

Acceptance test 14 on #5. Each flag sits beside a price that stays as it
arrived. The measurement over the Chan vintages is exploratory: it was found
by looking at the data, and it says only how large a move those two series
reach, so a multiple below it flags a bar of that series in test 1.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import get_type_hints

import pytest

from ithildinruntime.core import STRATEGIES, Bar, InstrumentId, load_strategies
from ithildinruntime.data import data_settings, largest_multiple, spike_flags
from ithildinruntime.data.spikes import (
    BELOW_LOW,
    CONTRADICT,
    MOVE,
    below_low,
    contradictions,
    quote_contradictions,
    quote_flags,
)
from tests.core_samples import sample
from tests.support.quotes import quote as a_quote

START = date(2012, 1, 3)


def bar(n: int, close: str, **prices: str) -> Bar:
    hints = get_type_hints(Bar, include_extras=True)
    fields = {f.name: sample(hints[f.name]) for f in dataclasses.fields(Bar)}
    price = Decimal(close)
    fields.update(
        instrument=InstrumentId("GLD"),
        interval="1d",
        session=START + timedelta(days=n),
        open=Decimal(prices.get("open", close)),
        high=Decimal(prices.get("high", str(price))),
        low=Decimal(prices.get("low", str(price))),
        close=price,
        volume=None,
    )
    return Bar(**fields)


def series(*closes: str) -> list[Bar]:
    return [bar(n, c) for n, c in enumerate(closes)]


# Rule 1: a bar whose prices contradict each other.


def test_a_high_below_its_close_is_flagged_beside_the_unchanged_price() -> None:
    odd = bar(0, "100.0000", high="99.9999", low="99.0000", open="99.5000")
    (flag,) = spike_flags([odd], Decimal(100))
    assert (flag.field, flag.price, flag.against, flag.rule) == (
        "high",
        Decimal("99.9999"),
        Decimal("100.0000"),
        CONTRADICT,
    )
    assert odd.high == Decimal("99.9999")
    assert "the bar carries no volume" in flag.note


def test_a_high_below_its_open_alone_is_flagged() -> None:
    odd = bar(0, "100.0000", high="100.5000", low="99.0000", open="101.0000")
    (flag,) = spike_flags([odd], Decimal(100))
    assert (flag.field, flag.against) == ("high", Decimal("101.0000"))


@pytest.mark.parametrize(
    ("high", "low", "open_", "close", "field", "against"),
    [
        ("100.0000", "99.0000", "100.5000", "100.0000", "high", "100.5000"),
        ("100.0000", "99.0000", "100.0000", "100.5000", "high", "100.5000"),
        ("100.0000", "100.5000", "100.0000", "100.0000", "high", "100.5000"),
    ],
)
def test_each_comparison_of_the_first_rule_is_made(
    high: str, low: str, open_: str, close: str, field: str, against: str
) -> None:
    odd = bar(0, close, high=high, low=low, open=open_)
    flags = spike_flags([odd], Decimal(100))
    assert (field, Decimal(against)) in {(f.field, f.against) for f in flags}


@pytest.mark.parametrize(
    ("open_", "close", "rules"),
    [("100.2000", "101.0000", []), ("101.0000", "100.2000", [BELOW_LOW])],
)
def test_a_low_above_its_open_or_close_alone_is_no_contradiction(
    open_: str, close: str, rules: list[str]
) -> None:
    """The contradiction rule names neither, and the below-the-low rule names the close alone."""
    odd = bar(0, close, high="101.0000", low="100.5000", open=open_)
    assert contradictions(odd) == []
    assert [f.rule for f in spike_flags([odd], Decimal(100))] == rules


def test_a_low_above_its_high_is_flagged_once_as_the_high_below_it() -> None:
    """Its close sits below that low too, so the third rule flags it beside the contradiction."""
    odd = bar(0, "100.0000", high="100.0000", low="100.5000", open="100.0000")
    flags = spike_flags([odd], Decimal(100))
    assert [f.field for f in flags if f.rule == BELOW_LOW] == ["close"]
    (flag,) = [f for f in flags if f.rule == CONTRADICT]
    assert (flag.field, flag.price, flag.against) == (
        "high",
        Decimal("100.0000"),
        Decimal("100.5000"),
    )


def test_a_bar_whose_prices_agree_is_not_flagged() -> None:
    fine = bar(0, "100.0000", high="100.0000", low="100.0000", open="100.0000")
    assert spike_flags([fine], Decimal(100)) == ()


@pytest.mark.parametrize("field", ["open", "high", "low", "close"])
@pytest.mark.parametrize(("price", "flagged"), [("-0.0001", True), ("0", True), ("0.0001", False)])
def test_a_price_at_or_below_zero_is_flagged(field: str, price: str, flagged: bool) -> None:
    """Each field has its own zero check, since no other rule halts on a zero close."""
    prices = {"open": "1.0000", "high": "1.0000", "low": "0.0001", "close": "1.0000"} | {
        field: price
    }
    close = prices.pop("close")
    odd = bar(0, close, **prices)
    flags = spike_flags([odd], Decimal(100))
    assert any(f.field == field and f.against == 0 for f in flags) is flagged


RISING = ("100", "101", "102.01", "103.0301")


@pytest.mark.parametrize("after", [(), ("104.060401",)])
@pytest.mark.parametrize("price", ["0", "-0.0001"])
def test_the_move_rule_judges_no_return_on_a_close_that_is_not_positive(
    price: str, after: tuple[str, ...]
) -> None:
    """So the move rule does not flag such a close, and its own zero check does.

    The returns before it are all 0.01, so any return judged into or out of it
    would differ from them, and a zero spread flags any return that differs.
    """
    flags = spike_flags(series(*RISING, price, *after), Decimal(1))
    assert [f for f in flags if f.rule == MOVE] == []
    assert {f.field for f in flags if f.rule == CONTRADICT and f.against == 0} >= {"close"}


def test_the_move_rule_judges_a_return_on_the_first_close_above_zero() -> None:
    """The control for the test above: one step above zero, the move is judged and flagged."""
    flags = spike_flags(series(*RISING, "0.0001"), Decimal(100))
    assert [(f.rule, f.field) for f in flags] == [(MOVE, "close")]


# Rule 2: a close-to-close move past the written multiple.

# Returns of +0.01, -0.01 and 0, then +0.05. Judged against the other three,
# whose sample standard deviation is exactly 0.01, the last move is 5 of them.
FIVE = series("100", "101", "99.99", "99.99", "104.9895")


@pytest.mark.parametrize(
    ("multiple", "flagged"),
    [(Decimal("4.9999"), True), (Decimal("5"), False), (Decimal("5.0001"), False)],
)
def test_a_move_past_the_multiple_is_flagged_and_one_at_it_is_not(
    multiple: Decimal, flagged: bool
) -> None:
    flags = [f for f in spike_flags(FIVE, multiple) if f.rule == MOVE]
    assert bool(flags) is flagged
    if flagged:
        (flag,) = flags
        assert (flag.session, flag.price, flag.against) == (
            FIVE[-1].session,
            Decimal("104.9895"),
            Decimal("99.99"),
        )
        assert "5.00 standard deviations" in flag.note


def test_a_move_is_judged_against_the_other_returns_only() -> None:
    assert largest_multiple(FIVE, 40) == Decimal(5)


def test_a_lookback_with_too_few_returns_judges_none() -> None:
    flags = spike_flags(series("100", "150", "100"), Decimal("0.1"))
    assert [f for f in flags if f.rule == MOVE] == []


def test_a_move_after_returns_that_are_all_equal_is_flagged() -> None:
    flat = series("100", "100", "100", "100", "101")
    (flag,) = [f for f in spike_flags(flat, Decimal(1000)) if f.rule == MOVE]
    assert flag.session == flat[-1].session
    assert spike_flags(series("100", "100", "100", "100"), Decimal(1)) == ()


# Rule 3: a close below its bar's low.


def test_every_bar_of_the_lookback_is_held_to_its_own_low_with_or_without_a_volume() -> None:
    odd = [bar(n, "95", high="101", low="100", open="100") for n in (0, 1, 2)]
    odd[1] = dataclasses.replace(odd[1], volume=1000)
    flags = [f for f in spike_flags(odd, Decimal(100)) if f.rule == BELOW_LOW]
    assert [f.session for f in flags] == [START + timedelta(days=n) for n in (0, 1, 2)]
    assert ["no volume threshold is written" in f.note for f in flags] == [False, True, False]


def test_a_zero_close_below_its_low_raises_both_rules() -> None:
    odd = bar(0, "0", high="101", low="100", open="100")
    assert [f.rule for f in spike_flags([odd], Decimal(100))] == [BELOW_LOW, CONTRADICT]


def test_each_rule_name_stays_as_recorded() -> None:
    """A stored flag holds its rule's name, so a new name would orphan the ones already stored."""
    assert (CONTRADICT, MOVE, BELOW_LOW) == (
        "prices contradict each other",
        "close-to-close move past the written multiple",
        "close below the bar's low",
    )


@pytest.mark.parametrize(
    ("close", "flagged"), [("99.9999", True), ("100.0000", False), ("100.0001", False)]
)
def test_a_close_one_step_below_its_low_is_flagged_and_one_at_it_is_not(
    close: str, flagged: bool
) -> None:
    odd = bar(0, close, high="101.0000", low="100.0000", open="100.5000")
    flags = spike_flags([odd], Decimal(100))
    expected = [(BELOW_LOW, "close", Decimal(close), Decimal("100.0000"))] if flagged else []
    assert [(f.rule, f.field, f.price, f.against) for f in flags] == expected
    assert all("the bar carries no volume" in f.note for f in flags)


def test_a_close_below_its_low_by_too_little_to_move_is_flagged_by_the_third_rule_alone() -> None:
    """The gap the third rule closes: a close 5% too low, well inside the multiple."""
    closes = ["100", "101", "99.99", "101", "100"]
    bars = series(*closes) + [bar(5, "95.0000", high="101.0000", low="100.0000", open="100.5000")]
    flags = spike_flags(bars, Decimal(10))
    assert [(f.rule, f.field) for f in flags] == [(BELOW_LOW, "close")]


def test_the_multiple_is_a_positive_decimal() -> None:
    for bad in (Decimal(0), Decimal(-1), 5):
        with pytest.raises(ValueError):
            spike_flags(FIVE, bad)  # type: ignore[arg-type]


# The measurement.


def test_the_largest_multiple_the_chan_vintages_reach_is_pinned(
    built_lake: Path, tmp_path: Path
) -> None:
    """Exploratory, measured over the lookback Chan's tracked file states.

    First pinned at fb96609 over 40 sessions, at 6.592043 for GLD and 5.716228
    for USO, and pinned again over 60 in #110, which moved the lookback.
    GLD's largest move is between 6.80 and 6.81 standard deviations and USO's
    between 5.30 and 5.31. So a multiple of 6.80 or below flags a GLD bar in
    test 1, one of 5.30 or below flags both series, and 6.81 and up flags
    neither. No bar of either
    has a low above its open or close, which design.md's 2026-11-24 entry
    quotes, and none contradicts itself.
    """
    from tests.support import lake as lakes
    from tests.test_data_source import FIRST, LAST, close, make

    whole = lakes.copy_lake(built_lake, tmp_path / "whole", kept=((FIRST, LAST),))
    (chan,) = load_strategies(STRATEGIES, ["chan_bollinger"])
    lookback = data_settings([chan]).lookback
    found = {}
    src = make(whole, tmp_path, lookback=1500, spike_multiple=Decimal(1000))
    snap = src.read(close(LAST)).snapshot
    for instrument in (InstrumentId("GLD"), InstrumentId("USO")):
        bars = snap.bars[instrument]["1d"]
        assert len(bars) == 1500
        assert [b for b in bars if b.low > min(b.open, b.close)] == []
        assert [b for b in bars if contradictions(b) or below_low(b)] == []
        found[instrument] = round(largest_multiple(bars, lookback), 6)
    assert found == {"GLD": Decimal("6.807921"), "USO": Decimal("5.302537")}


# A quote.

TODAY = date(2012, 4, 10)
AT = datetime(2012, 4, 10, 19, 40, tzinfo=UTC)
NO_VOLUME = "a quote carries no volume the rule weighs, so the flag weighs none"
# Two returns of 1% and -1%, whose sample standard deviation is 1.414...%.
WALK = series("100", "101", "99.99")


def quoted(bid: str | None, ask: str | None):
    made = a_quote("GLD", 1, 1, AT)
    sides = {"bid": bid, "ask": ask}
    return dataclasses.replace(
        made, **{n: None if v is None else Decimal(v) for n, v in sides.items()}
    )


@pytest.mark.parametrize(
    ("bid", "ask", "found"),
    [
        ("100", "100", []),
        ("100", "100.0001", []),
        ("100.0001", "100", [("ask", "100", "100.0001")]),
        ("0", "1", [("bid", "0", "0")]),
        ("-1", "1", [("bid", "-1", "0")]),
        ("0.0001", "0.0001", []),
        ("0", "0", [("bid", "0", "0"), ("ask", "0", "0")]),
        ("1", "0", [("ask", "0", "0"), ("ask", "0", "1")]),
        (None, "1", []),
        (None, "0", [("ask", "0", "0")]),
        ("1", None, []),
        (None, None, []),
    ],
)
def test_a_quote_whose_prices_contradict_each_other_is_flagged(
    bid: str | None, ask: str | None, found: list[tuple[str, str, str]]
) -> None:
    flags = quote_contradictions(quoted(bid, ask), TODAY)
    assert [(f.field, f.price, f.against) for f in flags] == [
        (name, Decimal(price), Decimal(against)) for name, price, against in found
    ]
    assert all((f.session, f.rule, f.note) == (TODAY, CONTRADICT, NO_VOLUME) for f in flags)
    assert quote_flags(quoted(bid, ask), TODAY, [], Decimal(10)) == tuple(flags)


def move(bars: list[Bar], price: str, multiple: str = "2"):
    flags = quote_flags(quoted(price, price), TODAY, bars, Decimal(multiple))
    assert all(f.rule == MOVE for f in flags)
    return flags


def test_a_midpoint_past_the_written_multiple_is_flagged_beside_the_newest_close() -> None:
    (flag,) = move(WALK, "110")
    assert (flag.session, flag.field, flag.price, flag.against) == (
        TODAY,
        "midpoint",
        Decimal(110),
        Decimal("99.99"),
    )
    assert flag.note == (
        "a move of the midpoint by 7.08 standard deviations, past the written 2. " + NO_VOLUME
    )


@pytest.mark.parametrize(
    ("closes", "price", "flags"),
    [
        # The returns are 10% and -10%, a deviation of 14.14%, and 1.4 of it is 19.8%.
        (("100", "110", "99"), "118.8", 1),
        (("100", "110", "99"), "118.5", 0),
        # A midpoint at the newest close has not moved.
        (("100", "120", "120"), "120", 0),
    ],
)
def test_a_midpoints_move_is_its_return_on_the_newest_close(
    closes: tuple[str, ...], price: str, flags: int
) -> None:
    assert len(move(series(*closes), price, "1.4")) == flags


@pytest.mark.parametrize(
    ("price", "flagged"), [("109.9999", False), ("110", False), ("110.0001", True)]
)
@pytest.mark.parametrize("down", [False, True])
def test_a_midpoint_at_the_written_multiple_is_not_flagged_and_one_step_past_is(
    price: str, flagged: bool, down: bool
) -> None:
    """Returns of 0, 0.1 and 0.2 have a deviation of 0.1, so a tenth is one deviation exactly."""
    bars = series("100", "100", "110", "132")
    last = Decimal(132)
    moved = last * Decimal(price) / 100
    if down:
        moved = 2 * last - moved
    assert (len(move(bars, str(moved), "1")) == 1) is flagged


@pytest.mark.parametrize("n", [0, 1, 2])
def test_fewer_than_two_returns_judge_no_midpoint(n: int) -> None:
    assert move(series(*["100", "101"][:n]), "1000") == ()


@pytest.mark.parametrize(
    ("closes", "price", "flagged"),
    [
        (("100", "100", "100"), "100", False),
        (("100", "100", "100"), "100.0001", True),
        (("100", "100", "100"), "99.9999", True),
        (("100", "110", "121"), "133.1", False),
        (("100", "110", "121"), "133.1001", True),
    ],
)
def test_a_lookback_whose_returns_are_all_equal_flags_any_other_move(
    closes: tuple[str, ...], price: str, flagged: bool
) -> None:
    flags = move(series(*closes), price, "1000")
    assert bool(flags) is flagged
    for flag in flags:
        assert flag.note == (
            "a move of the midpoint where every return of the lookback is the same. " + NO_VOLUME
        )


@pytest.mark.parametrize(("bid", "ask"), [("0", "0"), ("-5", "1"), (None, "1000"), ("1000", None)])
def test_a_midpoint_that_is_no_price_raises_no_move_flag(bid: str | None, ask: str | None) -> None:
    flags = quote_flags(quoted(bid, ask), TODAY, WALK, Decimal(2))
    assert all(f.rule == CONTRADICT for f in flags)


def test_a_newest_close_at_or_below_zero_raises_no_move_flag() -> None:
    bars = series("100", "101", "102", "0")
    assert move(bars, "1000") == ()


def test_a_midpoint_past_the_codecs_digits_raises_no_flag_and_no_error() -> None:
    """Thirty-eight digits and a half make thirty-nine, which no record holds."""
    long = "1." + "0" * 36 + "1"
    flags = quote_flags(quoted(long, "1"), TODAY, WALK, Decimal(2))
    assert [f.rule for f in flags] == [CONTRADICT]
    assert quote_flags(quoted("1", long), TODAY, WALK, Decimal(2)) == ()


def test_a_midpoint_flag_keeps_a_fifth_place() -> None:
    (flag,) = quote_flags(quoted("110", "110.0001"), TODAY, WALK, Decimal(2))
    assert flag.price == Decimal("110.00005")
