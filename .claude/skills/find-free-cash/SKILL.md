---
name: find-free-cash
description: Work out how much cash in a brokerage account is free to spend without margin, after what short puts, spreads and open orders hold back, and how many shares of something it buys. Use when asked how much cash is in Schwab, Fidelity or another linked account, how many shares of SGOV or anything else the cash buys, what buying power is without margin, or to check a cash figure against a thinkorswim screenshot.
---

# Find the free cash in a brokerage account

The cash balance a broker reports is not what can be spent. In one margin account, SnapTrade showed $140,000.00 of cash, and its "buying power" read the same. A first answer bought 1,390 shares of SGOV with it. The free cash was $69,000.00, enough for 685 shares, because three things the balance does not show had claims on it.

1. **An uncovered short put holds back its strike times 100.** One short $700 put held $70,000.
2. **A credit spread holds back its width times 100 per contract.** Ten call spreads $1 wide held $1,000.
3. **A margin account's buying power adds the loan value of marginable holdings.** thinkorswim's $150,000.00 Option Buying Power included $80,000.00 of borrowing against 1,000 shares of SGOV.

A short call covered by a long call with the same or a lower strike and the same or a later expiry holds nothing. So does one covered by 100 shares.

The examples in this skill use invented balances and trades. The repository is public, so no figure here comes from a real account.

## Gather the account

Use the SnapTrade tools, which are read-only. Load them in one `ToolSearch` call.

1. **Ask which account is in question,** unless the request names one or asks for all of them. `snaptrade_list_accounts` names the accounts, so match the answer by the last digits in an account's name.
2. `snaptrade_get_balance` gives the cash and the broker's buying power.
3. `snaptrade_get_positions` gives the stock holdings and a current price for any of them.
4. `snaptrade_get_option_positions` gives every option as an OCC ticker with signed contracts, short below zero.
5. `snaptrade_get_orders` with status `open` gives working orders. Put the cost of open buy orders in `open_order_reserve`. A working order to sell a put or a spread to open holds cash only once it fills, so name it in the report rather than subtracting it.

### Judge how fresh each account is

SnapTrade's sync time does not say how fresh the data is, so judge each account by its prices. On 2026-11-05 one Schwab account's `last_successful_sync` stayed at 04:18 UTC while its positions, cash and quotes all changed. The same sync time on the Fidelity accounts came with data frozen overnight.

1. **Compare the quotes.** When one symbol carries different prices across accounts, compare each with the last daily close from the Massive tools. `/v2/aggs/ticker/<SYMBOL>/range/1/day/<from>/<to>` works and has no bar for the current session, while the snapshot endpoint answers 403. A price equal to the last close, on a trading day, means that account's data is frozen.
2. **Never call an intraday price wrong because it misses the daily bars.** On 2026-11-05 a report called Schwab's $664.87 QQQ quote wrong because it sat below the last seven daily lows. It was the live price. The frozen figure was Fidelity's $691.95, the previous close, so the report also placed three short puts on the wrong side of their strikes. Check a doubtful quote against the account's own option prices and its fills today.
3. **Say which accounts look frozen.** A frozen account may be missing today's trades, so its free cash needs checking on the broker's site. An option still listed after its expiry is another sign. Frozen data can show a put that expired in the money on Friday as still open on Monday, before the assignment that spends its hold.

Pull everything again on every run, because the owner trades between runs. On 2026-11-05 one account went from $1,000 free to $15,000 within the day.

### Read the latest fills

`snaptrade_get_orders` with status `executed` returns the account's whole history, close to a million characters for an active account, so it lands in a file. Read it with a short script that sorts on `time_executed` and prints the latest fills. Those fills date any stock sale. A sale dated today settles the next business day, so a report whose cash rests on those proceeds says so.

### Read a thinkorswim screenshot

A thinkorswim screenshot of the Account Info panel, when the owner gives one, is live and is the better source. Take four fields from it.

1. Margin Equity.
2. Maintenance Requirement.
3. Option Buying Power.
4. Long Marginable Value.

Also read Money Market Balance. A nonzero balance there is cash-like but sits outside these figures, so name it separately.

## Run the calculation

Write the inputs to a JSON file in the scratchpad, in the shape the script's docstring shows. Then run `free_cash.py` from this skill's base directory with `-I`, which keeps Python from loading code from the current directory.

```bash
python3 -I <skill base directory>/free_cash.py <scratchpad>/find-free-cash/input.json
```

The script pairs every short option with the long option or 100-share lot that covers it most cheaply, the way a broker would, and prints what each pairing holds back. It then works out free cash up to three ways and keeps the lowest.

1. **From the broker's cash:** cash minus what short puts, spreads and open orders hold back.
2. **From the broker's buying power, when given:** SnapTrade's `buying_power`, as `broker_buying_power`.
3. **From thinkorswim, when given:** Option Buying Power minus the loan value of marginable holdings. The loan value is Long Marginable Value minus the stocks' share of the maintenance requirement.

### Check thinkorswim's figure

The thinkorswim check has to come out to $0.00. Margin equity minus maintenance minus what short puts and orders hold back must equal Option Buying Power. A nonzero gap means the script's pairings differ from thinkorswim's, so find the difference before giving a figure. In one account the gap was $0.00 while the cash itself differed by a few hundred dollars between SnapTrade and thinkorswim. The script keeps the lower figure for that reason.

### Explain a gap in the broker's buying power

In a cash-secured account, buying power can count a fill or a sweep that has not reached the cash balance yet. The script prints the gap between buying power and cash minus holds. Explain each gap from the fills of the last two business days before reporting, and name it. A Fidelity account gave three examples.

1. **A sweep that had not reached cash.** Buying power ran $750.00 above cash minus holds. A sweep of $750.00 into FDRXX, Fidelity's money market fund, was dated that day, and the cash showed it on the next pull.
2. **A buyback the cash had not paid for.** Buying power ran $75.30 below. A buyback of 25 calls at $0.03 the day before accounts for $75.00, and the other $0.30 was fees. The lower figure, from buying power, was the one to report.
3. **Premium that had not settled.** Buying power ran $1,386.70 above. That matched the premium, after fees, from 20 covered calls sold at $0.70 on the previous business day. The script keeps the lower figure from cash until the premium settles, and the report names the higher one and when it arrives.

Schwab's `buying_power` in SnapTrade equals its cash and ignores what short puts hold back. It only ever comes out higher, so it checks nothing there.

### Count shares

For a share count, put the quoted price in `buy.price` and a limit price in `buy.limit`. A limit near the 52-week high keeps the count from overshooting the cash if the price rises. Schwab sells no fractional shares of ETFs, so counts are whole shares.

## Report it

Lead with the free cash and the share count. Then list what holds cash back, one line per short put, spread and open order, and say which source the figure came from. Name anything that will change it soon, such as a short put expiring this week, which frees its hold if it expires worthless and spends it if it is assigned. Measure a short option's distance from its strike with the freshest price of the underlying across all accounts, never with the frozen account's own quote.

Stop and say so when the script warns of an uncovered short call. A naked call has no cash limit on its loss, so no free-cash figure is reliable.

Never place, change or cancel an order. The owner places trades, and the ticket should show the order uses no margin before it is sent.
