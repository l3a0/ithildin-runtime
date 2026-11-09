#!/usr/bin/env python3
"""Cash in a brokerage account that is free to spend without borrowing.

A broker's cash balance counts money already promised elsewhere. An uncovered
short put holds back its strike times 100, a credit spread holds back its width
times 100, and an open buy order holds back its cost. A margin account's
buying power also adds the loan value of marginable holdings, which is
borrowing. This script takes the positions, pairs every short option with the
long option or stock lot that covers it most cheaply, and subtracts what is
left over.

Usage: python3 -I free_cash.py input.json

Input, all money in dollars:

    {
      "cash": 140000.00,                       # broker's cash balance
      "stocks": {"SGOV": 1000},                # shares held, any symbol
      "options": {"QQQ   261030P00700000": -1},# OCC ticker -> contracts, short < 0
      "open_order_reserve": 0,                 # cost of open buy orders, optional
      "broker_buying_power": 140000.00,        # SnapTrade buying_power, optional
      "tos": {                                 # thinkorswim Account Info, optional
        "margin_equity": 240000.00,
        "maintenance_requirement": 20000.00,
        "option_buying_power": 150000.00,
        "long_marginable_value": 100000.00
      },
      "buy": {"price": 100.70, "limit": 100.74}# optional share count
    }
"""

import json
import math
import re
import sys
from dataclasses import dataclass

OCC = re.compile(r"^(\S+)\s*(\d{6})([CP])(\d{8})$")
NAKED_CALL = 10**12  # cents; a naked call has no cash bound, so it must lose
NEVER = 10**15       # cents; a pairing the broker would not allow


@dataclass(frozen=True)
class Contract:
    ticker: str
    root: str
    expiry: str  # YYMMDD sorts as a date
    right: str   # "C" or "P", or "S" for a 100-share stock lot
    strike: float


def parse(ticker):
    m = OCC.match(ticker.strip())
    if not m:
        sys.exit(f"not an OCC option ticker: {ticker!r}")
    root, expiry, right, strike = m.groups()
    return Contract(ticker.strip(), root, expiry, right, int(strike) / 1000)


def cost_cents(short, cover):
    """Cash a broker holds for `short` when `cover` (or None) is paired with it."""
    if cover is None:
        if short.right == "P":
            return round(short.strike * 100 * 100)
        return NAKED_CALL
    if cover.root != short.root:
        return NEVER
    if cover.right == "S":
        return 0 if short.right == "C" else NEVER
    if cover.right != short.right or cover.expiry < short.expiry:
        return NEVER
    if short.right == "C":
        width = cover.strike - short.strike
    else:
        width = short.strike - cover.strike
    return round(max(0.0, width) * 100 * 100)


def hungarian(cost):
    """Minimum-cost assignment of every row to a distinct column, rows <= columns."""
    n, m = len(cost), len(cost[0])
    u, v = [0] * (n + 1), [0] * (m + 1)
    p, way = [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [math.inf] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], math.inf, 0
            for j in range(1, m + 1):
                if not used[j]:
                    cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if cur < minv[j]:
                        minv[j], way[j] = cur, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    return {p[j] - 1: j - 1 for j in range(1, m + 1) if p[j]}


def pair(options, stocks):
    shorts, covers = [], []
    for ticker, units in options.items():
        c = parse(ticker)
        (shorts if units < 0 else covers).extend([c] * abs(int(units)))
    for symbol, shares in stocks.items():
        lot = Contract(f"{symbol} stock", symbol, "999999", "S", 0.0)
        covers.extend([lot] * (int(shares) // 100))
    if not shorts:
        return []
    # One column per cover, then one "uncovered" column per short.
    matrix = [
        [cost_cents(s, c) for c in covers] + [cost_cents(s, None)] * len(shorts)
        for s in shorts
    ]
    assigned = hungarian(matrix)
    result = []
    for row, col in sorted(assigned.items()):
        cover = covers[col] if col < len(covers) else None
        result.append((shorts[row], cover, matrix[row][col]))
    return result


def main(path):
    with open(path) as f:
        data = json.load(f)
    pairs = pair(data.get("options", {}), data.get("stocks", {}))

    put_reserve = spread_req = 0
    naked_calls = []
    lines = {}
    for short, cover, cents in pairs:
        if cents >= NAKED_CALL:
            naked_calls.append(short.ticker)
            continue
        if cover is None:
            put_reserve += cents
            kind = "uncovered short put, strike x 100"
        elif cents:
            spread_req += cents
            kind = f"spread against {cover.ticker}, width x 100"
        else:
            kind = f"covered by {cover.ticker}"
        key = (short.ticker, kind)
        n, total = lines.get(key, (0, 0))
        lines[key] = (n + 1, total + cents)

    print("Short options and what holds them:")
    for (ticker, kind), (n, cents) in sorted(lines.items()):
        print(f"  {n:>3} x {ticker}  {kind}: ${cents / 100:,.2f}")
    if naked_calls:
        print("WARNING: uncovered short calls, no cash figure is reliable:")
        for t in sorted(set(naked_calls)):
            print(f"  {naked_calls.count(t)} x {t}")

    order_reserve = round(data.get("open_order_reserve", 0) * 100)
    held = put_reserve + spread_req + order_reserve
    print(f"\nHeld for short puts:   ${put_reserve / 100:,.2f}")
    print(f"Held for spreads:      ${spread_req / 100:,.2f}")
    print(f"Held for open orders:  ${order_reserve / 100:,.2f}")

    candidates = {}
    cash = round(data["cash"] * 100)
    candidates["broker cash"] = cash - held
    print(f"\nBroker cash ${cash / 100:,.2f} - held ${held / 100:,.2f}"
          f" = ${(cash - held) / 100:,.2f}")

    # A cash account's buying power can count a fill or a sweep that has not
    # reached the cash balance yet. Where buying power equals cash, as at
    # Schwab, it comes out higher and the lower figure below ignores it.
    if "broker_buying_power" in data:
        bbp = round(data["broker_buying_power"] * 100)
        candidates["broker buying power"] = bbp
        gap = bbp - (cash - held)
        print(f"Broker buying power ${bbp / 100:,.2f}, ${gap / 100:+,.2f} against"
              " cash - held")
        if gap and bbp < cash:
            print("  GAP: look for a fill or sweep in the last two business days"
                  " that explains it, and name it in the report.")

    tos = data.get("tos")
    if tos:
        me = round(tos["margin_equity"] * 100)
        maint = round(tos["maintenance_requirement"] * 100)
        obp = round(tos["option_buying_power"] * 100)
        lmv = round(tos["long_marginable_value"] * 100)
        stock_maint = maint - spread_req
        loan_value = lmv - stock_maint
        residual = me - maint - put_reserve - order_reserve - obp
        print("\nthinkorswim reconciliation:")
        share = f" ({stock_maint / lmv:.1%} of long marginable value)" if lmv else ""
        print(f"  Stock maintenance = maintenance - spreads = ${stock_maint / 100:,.2f}{share}")
        print(f"  Loan value of holdings = ${loan_value / 100:,.2f} (this is margin)")
        print(f"  Margin equity - maintenance - held - option BP = ${residual / 100:,.2f}"
              "  (should be $0.00)")
        if residual:
            print("  MISMATCH: the pairing above does not match how thinkorswim"
                  " holds cash. Find the difference before trusting either figure.")
        # Option buying power is already net of open orders.
        candidates["thinkorswim"] = obp - loan_value
        print(f"  Option BP ${obp / 100:,.2f} - loan value ${loan_value / 100:,.2f}"
              f" = ${candidates['thinkorswim'] / 100:,.2f}")
        implied = me - lmv
        if implied != cash:
            print(f"  Cash implied by thinkorswim ${implied / 100:,.2f} differs from"
                  f" broker cash by ${(cash - implied) / 100:,.2f}")

    source = min(candidates, key=candidates.get)
    free = candidates[source]
    print(f"\nFREE CASH (lower figure, from {source}): ${free / 100:,.2f}")

    buy = data.get("buy")
    if buy:
        for label in ("price", "limit"):
            if label in buy:
                px = round(buy[label] * 100)
                shares = free // px
                left = free - shares * px
                print(f"  At ${px / 100:,.2f} ({label}): {shares:,} shares,"
                      f" ${shares * px / 100:,.2f}, ${left / 100:,.2f} left")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
