"""The spike flag's three rules, and the measurement the spike multiple is stated beside.

Acceptance test 14 on #5. Each flag sits beside a price that stays as it
arrived. The measurement over the Chan vintages is exploratory: it was found
by looking at the data, and it says only how large a move those two series
reach, so a multiple below it flags a bar of that series in test 1.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import get_type_hints

import pytest

from ithildinruntime.core import Bar, InstrumentId
from ithildinruntime.data import largest_multiple, spike_flags
from ithildinruntime.data.spikes import BELOW_LOW, CONTRADICT, MOVE, below_low, contradictions
from tests.core_samples import sample

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
    """Each field has its own zero check, since no other rule flags a zero close."""
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
    """So such a close is flagged by its own zero check alone.

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


@pytest.mark.parametrize(("close", "flagged"), [("99.9999", True), ("100.0000", False)])
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
    """Exploratory, first pinned at ed9d1f7. GLD reaches 6.59 and USO 5.72.

    So a multiple at or below 6.59 flags a GLD bar in test 1. No bar of either
    has a low above its open or close, which design.md's 2026-11-24 entry
    quotes, and none contradicts itself.
    """
    from tests.support import lake as lakes
    from tests.test_data_source import FIRST, LAST, close, make

    whole = lakes.copy_lake(built_lake, tmp_path / "whole", kept=((FIRST, LAST),))
    found = {}
    src = make(whole, tmp_path, lookback=1500, spike_multiple=Decimal(1000))
    snap = src.read(close(LAST)).snapshot
    for instrument in (InstrumentId("GLD"), InstrumentId("USO")):
        bars = snap.bars[instrument]["1d"]
        assert len(bars) == 1500
        assert [b for b in bars if b.low > min(b.open, b.close)] == []
        assert [b for b in bars if contradictions(b) or below_low(b)] == []
        found[instrument] = round(largest_multiple(bars, 40), 6)
    assert found == {"GLD": Decimal("6.592043"), "USO": Decimal("5.716228")}
