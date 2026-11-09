"""Pin the find-free-cash skill's script to figures worked out by hand.

The script's output is the cash the owner spends without margin, so a figure
that comes out too high means borrowing. Each expected value below follows
from the rules the skill states, not from the script's own printout:

- An uncovered short put holds its strike times 100.
- A credit spread holds its width times 100.
- A short is covered only by a long of the same underlying and right with the
  same or a later expiry, or a short call by 100 shares.
- The lowest of the figures is the one reported.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".claude/skills/find-free-cash/free_cash.py"


def run(tmp_path, data):
    path = tmp_path / "input.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    proc = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, proc.stdout + proc.stderr


def account(**fields):
    data = {"cash": 100000.00, "stocks": {}, "options": {}, "open_order_reserve": 0}
    data.update(fields)
    return data


def test_skill_example_reconciles_and_counts_at_the_limit(tmp_path):
    # The skill's opening example: $140,000 cash, one short $700 put and ten
    # $1-wide call spreads, against thinkorswim's Account Info.
    code, out = run(
        tmp_path,
        account(
            cash=140000.00,
            stocks={"SGOV": 1000},
            options={
                "QQQ   261120P00700000": -1,
                "QQQ   261120C00745000": -10,
                "QQQ   261120C00746000": 10,
            },
            tos={
                "margin_equity": 240000.00,
                "maintenance_requirement": 20000.00,
                "option_buying_power": 150000.00,
                "long_marginable_value": 100000.00,
            },
            buy={"price": 100.70, "limit": 100.74},
        ),
    )
    assert code == 0, out
    assert "Held for short puts:   $70,000.00" in out
    assert "Held for spreads:      $1,000.00" in out
    assert "Broker cash $140,000.00 - held $71,000.00 = $69,000.00" in out
    assert "Loan value of holdings = $81,000.00" in out
    assert "option BP = $0.00" in out
    assert "MISMATCH" not in out
    assert "Option BP $150,000.00 - loan value $81,000.00 = $69,000.00" in out
    assert "FREE CASH (lower figure, from broker cash): $69,000.00" in out
    # 685 shares at the $100.74 limit would cost $69,006.90.
    assert "At $100.74 (limit): 684 shares, $68,906.16, $93.84 left" in out
    assert "(price)" not in out


def test_lower_buying_power_is_reported(tmp_path):
    code, out = run(tmp_path, account(cash=10000.00, broker_buying_power=9000.00))
    assert code == 0, out
    assert "FREE CASH (lower figure, from broker buying power): $9,000.00" in out


def test_lower_thinkorswim_figure_is_reported(tmp_path):
    # thinkorswim implies $99,000 of cash against the broker's $100,000.
    code, out = run(
        tmp_path,
        account(
            open_order_reserve=5000.00,
            tos={
                "margin_equity": 99000.00,
                "maintenance_requirement": 0,
                "option_buying_power": 94000.00,
                "long_marginable_value": 0,
            },
        ),
    )
    assert code == 0, out
    assert "Broker cash $100,000.00 - held $5,000.00 = $95,000.00" in out
    assert "Option BP $94,000.00 - loan value $0.00 = $94,000.00" in out
    assert "FREE CASH (lower figure, from thinkorswim): $94,000.00" in out


def test_reconciliation_flags_a_put_thinkorswim_does_not_hold(tmp_path):
    code, out = run(
        tmp_path,
        account(
            options={"QQQ   261120P00600000": -1},
            tos={
                "margin_equity": 100000.00,
                "maintenance_requirement": 0,
                "option_buying_power": 100000.00,
                "long_marginable_value": 0,
            },
        ),
    )
    assert "option BP = $-60,000.00" in out
    assert "MISMATCH" in out


def test_cheapest_pairing_beats_pairing_in_order(tmp_path):
    # Pairing the October short with its cheapest cover, the November 699,
    # leaves the November short uncovered and holds $70,100. The cheapest
    # assignment pairs October 700/698 and November 700/699 and holds $300.
    code, out = run(
        tmp_path,
        account(
            options={
                "QQQ   261016P00700000": -1,
                "QQQ   261120P00700000": -1,
                "QQQ   261016P00698000": 1,
                "QQQ   261120P00699000": 1,
            }
        ),
    )
    assert code == 0, out
    assert "Held for short puts:   $0.00" in out
    assert "Held for spreads:      $300.00" in out
    assert "FREE CASH (lower figure, from broker cash): $99,700.00" in out


def test_call_spread_holds_its_width(tmp_path):
    code, out = run(
        tmp_path, account(options={"QQQ   261120C00700000": -2, "QQQ   261120C00705000": 2})
    )
    assert code == 0, out
    assert "Held for spreads:      $1,000.00" in out


@pytest.mark.parametrize(
    "cover",
    [
        "SPY   261120C00600000",  # another underlying
        "QQQ   261016C00600000",  # expires before the short
        "QQQ   261120P00600000",  # a put cannot cover a call
    ],
)
def test_a_cover_the_broker_refuses_leaves_the_call_naked(tmp_path, cover):
    code, out = run(tmp_path, account(options={"QQQ   261120C00700000": -1, cover: 1}))
    assert code == 1
    assert "WARNING: uncovered short calls" in out
    assert "FREE CASH" not in out


def test_shares_cover_a_call_but_never_a_put(tmp_path):
    code, out = run(
        tmp_path,
        account(
            stocks={"QQQ": 200},
            options={"QQQ   261120C00700000": -1, "QQQ   261120P00600000": -1},
        ),
    )
    assert code == 0, out
    assert "covered by QQQ stock: $0.00" in out
    assert "Held for short puts:   $60,000.00" in out


def test_open_orders_are_subtracted(tmp_path):
    code, out = run(tmp_path, account(cash=10000.00, open_order_reserve=2500.00))
    assert code == 0, out
    assert "FREE CASH (lower figure, from broker cash): $7,500.00" in out


def test_share_count_rounds_down(tmp_path):
    code, out = run(tmp_path, account(cash=1000.00, buy={"price": 100.01}))
    assert "At $100.01 (price): 9 shares" in out


def test_no_share_count_when_nothing_is_free(tmp_path):
    code, out = run(
        tmp_path,
        account(cash=1000.00, options={"QQQ   261120P00600000": -1}, buy={"price": 100.00}),
    )
    assert "FREE CASH (lower figure, from broker cash): $-59,000.00" in out
    assert "shares" not in out


def test_thinkorswim_holding_more_for_spreads_stops(tmp_path):
    # thinkorswim pairs the four calls as 700/720 and holds $2,000, while the
    # cheapest pairing holds $1,500. The put check still reads $0.00, so only
    # the impossible negative loan value shows the difference.
    code, out = run(
        tmp_path,
        account(
            options={
                "QQQ   261120C00700000": -1,
                "QQQ   261120C00710000": -1,
                "QQQ   261120C00705000": 1,
                "QQQ   261120C00720000": 1,
            },
            tos={
                "margin_equity": 100000.00,
                "maintenance_requirement": 2000.00,
                "option_buying_power": 98000.00,
                "long_marginable_value": 0,
            },
        ),
    )
    assert code != 0
    assert "STOP" in out
    assert "FREE CASH" not in out


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        pytest.param({"cash": 140000.00}, "missing field", id="positions left out"),
        pytest.param(
            {**account(), "option": {}},
            "unknown field ['option']",
            id="misspelled field",
        ),
        pytest.param(
            '{"cash": 200000, "stocks": {}, "open_order_reserve": 0, "options":'
            ' {"QQQ   261120P00600000": -1, "QQQ   261120P00600000": -1}}',
            "repeated field",
            id="repeated ticker",
        ),
        pytest.param(
            account(options={"QQQ   261120P00700000": -0.5}),
            "nonzero whole number",
            id="half a contract",
        ),
        pytest.param(account(stocks={"QQQ": -100}), "shares of QQQ", id="negative shares"),
        pytest.param(
            account(open_order_reserve=-5000), "open_order_reserve", id="negative order reserve"
        ),
        pytest.param(account(cash=None), "cash must be a number", id="no cash figure"),
        pytest.param(account(buy={"price": 0}), "buy price", id="zero price"),
        pytest.param(account(tos={"margin_equity": 1}), "tos is missing", id="partial thinkorswim"),
    ],
)
def test_input_that_would_drop_a_hold_is_refused(tmp_path, data, reason):
    code, out = run(tmp_path, data)
    assert code != 0
    assert reason in out
    assert "FREE CASH" not in out
