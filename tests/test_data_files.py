"""The data module's four files that only grow, pinned row by row.

Acceptance test 20 on #5, and acceptance 1 of #189 for the record of splits.
CI's checkout holds no history to compare with, so each row the security
master, the two kind contracts and the record of splits held when it was
added is written here by hand. Editing or removing a row fails the test, and
adding one means adding it here too.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from ithildinruntime.core import Halt, InstrumentId, load_sessions
from ithildinruntime.data.reference import (
    CAPTURE_SOURCE,
    MASTER,
    PRICE_KINDS,
    QUOTE_KINDS,
    QUOTE_SOURCE,
    SCHWAB_IMPORT,
    Kind,
    KindRow,
    MasterRow,
    PriceKinds,
    QuoteKind,
    SecurityMaster,
    load_master,
    load_price_kinds,
    load_quote_kinds,
)
from ithildinruntime.data.splits import SPLITS, load_splits

MASTER_ROWS = [
    "2026-11-22,GLD,GLD,2004-11-18,",
    "2026-11-22,USO,USO,2006-04-10,",
    "2026-11-22,SPY,SPY,1993-01-29,",
    "2026-11-22,QQQ,QQQ,1999-03-10,2004-11-30",
    "2026-11-22,QQQ,QQQQ,2004-12-01,2011-03-22",
    "2026-11-22,QQQ,QQQ,2011-03-23,",
]

_CHAN_READ_FROM = (
    "owner's ruling of 2026-11-22 on #5. quantitative-trading data/vintages.jsonl at 19aadbeb "
    "records vendor chan-mat and source_workbook inputData_ETF.mat and names no kind. Chan's "
    "closes equal Yahoo's on all 1500 sessions{split} and his volume is Yahoo's in hundreds of "
    "shares{volume}. Compared with gld_daily_yahoo_2026-11-22.csv at sha256 "
    "b6675ed455acf0174854377f8a51061fb18c0b1bf0a9a0f7632ac557b6fcb4f9 and "
    "uso_daily_yahoo_2026-11-22.csv at sha256 "
    "72bb286f57f637c9ef517b98631a68f7e8f2d7869af4935441b555b11f60d561. Yahoo publishes no "
    "statement of its close so official close is the usual meaning of a Yahoo close for a US ETF"
)
_VOLUME = {
    "gld": (
        " on 1498 of them: it differs on 2012-03-13 where Chan has 0 and Yahoo 20047000 shares"
        " and on 2012-03-26 where Chan has 132116 hundreds and Yahoo 13226700 shares"
    ),
    "uso": "",
}
_SPLIT = {
    "gld": "",
    "uso": " after Yahoo's factor of 8 for USO's one-for-eight reverse split of 2020",
}
_YAHOO_READ_FROM = (
    "owner's ruling of 2026-11-24 on #137 at "
    "https://github.com/l3a0/ithildin-runtime/issues/137#issuecomment-7152659296. "
    "l3a0/marketlake#1025 pins the file at sha256 {sha} and sets its later years beside two other "
    "vendors in an exploratory comparison whose differences it lists. Its closes equal Chan's on "
    "all 1500 sessions from 2006-04-26 to 2012-04-09{split} and the owner's ruling of 2026-11-22 "
    "on #5 gave the kind of Chan's as Yahoo's.{scale} That the volume is consolidated is measured "
    "from 2025 on and inferred before it and the price's venues rest on the ruling. Yahoo "
    "publishes no statement of its close so official close is the usual meaning of a Yahoo close "
    "for a US ETF"
)
_SHA = {
    "gld": "b6675ed455acf0174854377f8a51061fb18c0b1bf0a9a0f7632ac557b6fcb4f9",
    "uso": "72bb286f57f637c9ef517b98631a68f7e8f2d7869af4935441b555b11f60d561",
}
_SCALE = {
    "gld": "",
    "uso": (
        " Prices and volume before 2020-04-29 are on the scale after that split: prices are "
        "eight times those that traded and volume is divided by eight."
    ),
}
_COUNTED = {
    "gld": "consolidated in shares to the nearest hundred",
    "uso": "consolidated in shares to the nearest hundred before its source's split adjustment",
}
_CAPTURE_READ_FROM = (
    "owner's ruling of 2026-11-25 on #174 at "
    "https://github.com/l3a0/ithildin-runtime/issues/174#issuecomment-7171477518. The "
    "evidence below is of GLD USO SPY and QQQ alone. Schwab publishes no statement of its "
    "candle so the kind rests on agreement with a second vendor. In an exploratory "
    "comparison with Massive's unadjusted daily aggregates read on 2026-11-25 the open high "
    "low and close are equal to the cent on 34 of 34 captured bars of GLD and USO from "
    "2026-11-02 to 2026-11-24 and on 46 of 46 of SPY and QQQ from 2026-09-11 to 2026-10-13. "
    "No session from 2026-09-08 to 2026-09-10 was compared and no session of SPY or QQQ "
    "after 2026-10-13 was and no early close is in it. Of the 34 volumes of GLD and USO 16 "
    "differ by over 1 percent from 3.8 percent below to 9.5 percent above. The 20 fetched "
    "the same evening are all below and 13 of the 14 fetched later are above"
)
_SCHWAB_READ_FROM = (
    "owner's ruling of 2026-11-26 on #175 at "
    "https://github.com/l3a0/ithildin-runtime/issues/175#issuecomment-7180719855. The "
    "evidence below is of GLD and USO alone. The bars are Schwab's daily candles that "
    "marketlake imported on 2026-11-02 from the endpoint its capture calls "
    "(l3a0/marketlake#951) and on the two forward splits that issue measured Schwab adjusts "
    "them for splits to the day it serves them. A reverse split and how soon Schwab restates "
    "after an ex-date were not measured. "
    "Neither fund split from 2026-09-04 to 2026-11-02 so they are as traded and on the scale "
    "of the captured bars from 2026-11-02 on. Schwab publishes no statement of its candle so "
    "the kind rests on agreement with a second vendor. In an exploratory comparison with "
    "Massive's unadjusted daily aggregates read on 2026-11-25 the open high low and close are "
    "equal to the cent on 80 of 80 imported bars of GLD and USO. Their volume was not compared "
    "and is stated as the capture row states it because it comes from the same endpoint"
)
KIND_ROWS = (
    [
        f"2026-11-22,quantitative-trading:data/inputdata_etf/{name}.csv,2006-04-26,2012-04-09,"
        f"official close,consolidated,none,consolidated in hundreds of shares,"
        f"{_CHAN_READ_FROM.format(split=_SPLIT[name], volume=_VOLUME[name])}"
        for name in ("gld", "uso")
    ]
    + [
        f"2026-11-25,yahoo:{name}_daily_yahoo_2026-11-22.csv,2012-04-10,2026-09-03,"
        f"official close,consolidated,none,{_COUNTED[name]},"
        f"{_YAHOO_READ_FROM.format(sha=_SHA[name], split=_SPLIT[name], scale=_SCALE[name])}"
        for name in ("gld", "uso")
    ]
    + [
        "2026-11-25,marketlake:capture,2026-09-08,2027-03-31,official close,consolidated,none,"
        f"consolidated in shares as of the fetch,{_CAPTURE_READ_FROM}"
    ]
    + [
        "2026-11-26,schwab:pricehistory,2026-09-04,2026-10-30,official close,consolidated,none,"
        f"consolidated in shares as of the fetch,{_SCHWAB_READ_FROM}"
    ]
)

_QUOTE_READ_FROM = (
    "owner's ruling of 2026-11-26 on #133 at "
    "https://github.com/l3a0/ithildin-runtime/issues/133#issuecomment-7199939899. Schwab's "
    "Trader API Market Data Production specification at https://developer.schwab.com/products"
    "/trader-api--individual/details/specifications/Market%20Data%20Production read on "
    "2026-11-26 behind the owner's developer sign-in. Its QuoteEquity schema calls bidPrice "
    "and askPrice the current best bid and ask price and totalVolume the shares traded "
    "through the day including pre-market and post-market hours. It gives lastPrice no "
    "description. It calls securityStatus the status of the security with Normal as its "
    "example and gives no list of its values where other fields of the page give one. All 92 "
    "quotes read in an exploratory count on 2026-11-26 read Normal. They are 24 of GLD and "
    "USO from 2026-11-02 to 2026-11-25 and 68 of SPY and QQQ from 2026-09-15 to 2026-10-30. "
    "marketlake's committed test recording of Schwab's answer reads Normal too. The page "
    "gives each side a MIC code of its venue and each answer a quoteType of NBBO or NFL. "
    "marketlake's published quote columns carry neither so the venues are unstated"
)
# The row of 2026-11-26 gained the close_kind column, unstated, when #189 added it.
QUOTE_ROWS = [
    "2026-11-26,marketlake:quotes,2026-09-15,2027-02-25,current best bid and ask,unstated,unstated,"
    "shares traded through the day including pre-market and post-market hours,Normal,unstated,"
    f"{_QUOTE_READ_FROM}",
    (
        "2026-11-27,marketlake:quotes,2026-09-15,2027-02-25,current best bid and "
        "ask,unstated,unstated,shares traded through the day including pre-market and post-market"
        " hours,Normal,the previous session's close as Schwab states it on the day of the "
        "quote,every field but close_kind as the row of 2026-11-26 states it and from what it was"
        " read from. close_kind is from Schwab's Trader API Market Data Production specification "
        "at https://developer.schwab.com/products/trader-api--individual/details/specifications/M"
        "arket%20Data%20Production read on 2026-11-26 behind the owner's developer sign-in. Its "
        "QuoteEquity schema calls closePrice the previous day's closing price as PR 219's notes "
        "at https://github.com/l3a0/ithildin-runtime/pull/219 record. marketlake's journal maps "
        "closePrice to close_price. In an exploratory count from the laptop's copy of the lake it"
        " equalled the stored close of the session before on 42 of 44 ticker-days of SPY and QQQ "
        "from 2026-09-14 to 2026-10-13 and was lower by the dividend on the other 2. That is the "
        "figure exchanges publish so a day's net change reads correctly so on an ex-date it is "
        "the close less the dividend. Whether Schwab restates it on a split's own session is "
        "unmeasured"
    ),
]

SPLIT_ROWS = [
    (
        "2026-11-27,split,USO,2020-04-29,,1,8,2020-04-22T13:30:00Z,USO's 8-K accession "
        "0001171200-20-000271 accepted by EDGAR at 2020-04-22 07:00:37 New York time: its exhibit"
        " 99.1 at "
        "https://www.sec.gov/Archives/edgar/data/1327068/000117120020000271/i20263_ex99-1.htm "
        "fetched 2026-11-27T04:25:21Z at sha256 "
        "f120a201940f53769492893c6ab8baab35d99c99c7cd11065b9201c33167c5e2 says a one-for-eight "
        "reverse split takes effect after the close of 2020-04-28 and shares trade at post-split "
        "prices on 2020-04-29. Its index page fetched 2026-11-27T04:25:30Z is at sha256 "
        "80f0dacfcfb3aaf08496ed717ea3a4e0898ee6aacf7b898ce16840cef32e2a5b. The 8-K accession "
        "0001171200-20-000310 at "
        "https://www.sec.gov/Archives/edgar/data/1327068/000117120020000310/i20281_uso-8k.htm "
        "fetched 2026-11-27T04:25:30Z at sha256 "
        "ed92f5717309dc282cb4759375063c5b46e193bbf48221bacd18d15a722a858a reports the split done."
        " known_at is the open of 2020-04-22 since the filing was accepted before it. Both vendor"
        " lists agree: Alpha Vantage's SPLITS function read through its connector at about "
        "2026-11-27T04:27Z: GLD at sha256 "
        "e2385bb7b104c384ed8f892724ba70e3297d3b8a3c2f1e4b5571de73e0a7c6e3 and SPY at sha256 "
        "e1779bbaf798a1718970fbe3a88b5da27d10b2548bddf00ce089f13121c4afe1 list no split. USO at "
        "sha256 fb893bfd2323a979f2f54e9452e2331f96247c8311d9055cdd409b3adecd5b15 lists 2020-04-29"
        " at a factor of 0.1250 alone and QQQ at sha256 "
        "c64147e09228910757fb33cd3a495a079e5b8954d9f76e25272a684ccc3e642f lists 2000-03-20 at "
        "2.0000 alone. Massive's GET /stocks/v1/splits for GLD USO SPY and QQQ read through its "
        "connector at 2026-11-27T04:26Z at sha256 "
        "aea349d40de211b9fe5f18c09b6227df8639692fe839924f25d246cc99ab33cc lists USO's reverse "
        "split of 2020-04-29 from 8 to 1 alone. Each answer is saved as the connector returned it"
        " under ~/.config/ithildin-runtime/splits/2026-11-27/"
    ),
    (
        "2026-11-27,covered,SPY,2026-09-08,2026-11-19,,,,Alpha Vantage's SPLITS function read "
        "through its connector at about 2026-11-27T04:27Z: GLD at sha256 "
        "e2385bb7b104c384ed8f892724ba70e3297d3b8a3c2f1e4b5571de73e0a7c6e3 and SPY at sha256 "
        "e1779bbaf798a1718970fbe3a88b5da27d10b2548bddf00ce089f13121c4afe1 list no split. USO at "
        "sha256 fb893bfd2323a979f2f54e9452e2331f96247c8311d9055cdd409b3adecd5b15 lists 2020-04-29"
        " at a factor of 0.1250 alone and QQQ at sha256 "
        "c64147e09228910757fb33cd3a495a079e5b8954d9f76e25272a684ccc3e642f lists 2000-03-20 at "
        "2.0000 alone. Massive's GET /stocks/v1/splits for GLD USO SPY and QQQ read through its "
        "connector at 2026-11-27T04:26Z at sha256 "
        "aea349d40de211b9fe5f18c09b6227df8639692fe839924f25d246cc99ab33cc lists USO's reverse "
        "split of 2020-04-29 from 8 to 1 alone. Each answer is saved as the connector returned it"
        " under ~/.config/ithildin-runtime/splits/2026-11-27/"
    ),
    (
        "2026-11-27,covered,QQQ,2026-09-08,2026-11-19,,,,Alpha Vantage's SPLITS function read "
        "through its connector at about 2026-11-27T04:27Z: GLD at sha256 "
        "e2385bb7b104c384ed8f892724ba70e3297d3b8a3c2f1e4b5571de73e0a7c6e3 and SPY at sha256 "
        "e1779bbaf798a1718970fbe3a88b5da27d10b2548bddf00ce089f13121c4afe1 list no split. USO at "
        "sha256 fb893bfd2323a979f2f54e9452e2331f96247c8311d9055cdd409b3adecd5b15 lists 2020-04-29"
        " at a factor of 0.1250 alone and QQQ at sha256 "
        "c64147e09228910757fb33cd3a495a079e5b8954d9f76e25272a684ccc3e642f lists 2000-03-20 at "
        "2.0000 alone. Massive's GET /stocks/v1/splits for GLD USO SPY and QQQ read through its "
        "connector at 2026-11-27T04:26Z at sha256 "
        "aea349d40de211b9fe5f18c09b6227df8639692fe839924f25d246cc99ab33cc lists USO's reverse "
        "split of 2020-04-29 from 8 to 1 alone. Each answer is saved as the connector returned it"
        " under ~/.config/ithildin-runtime/splits/2026-11-27/"
    ),
    (
        "2026-11-27,covered,GLD,2026-10-30,2026-11-19,,,,Alpha Vantage's SPLITS function read "
        "through its connector at about 2026-11-27T04:27Z: GLD at sha256 "
        "e2385bb7b104c384ed8f892724ba70e3297d3b8a3c2f1e4b5571de73e0a7c6e3 and SPY at sha256 "
        "e1779bbaf798a1718970fbe3a88b5da27d10b2548bddf00ce089f13121c4afe1 list no split. USO at "
        "sha256 fb893bfd2323a979f2f54e9452e2331f96247c8311d9055cdd409b3adecd5b15 lists 2020-04-29"
        " at a factor of 0.1250 alone and QQQ at sha256 "
        "c64147e09228910757fb33cd3a495a079e5b8954d9f76e25272a684ccc3e642f lists 2000-03-20 at "
        "2.0000 alone. Massive's GET /stocks/v1/splits for GLD USO SPY and QQQ read through its "
        "connector at 2026-11-27T04:26Z at sha256 "
        "aea349d40de211b9fe5f18c09b6227df8639692fe839924f25d246cc99ab33cc lists USO's reverse "
        "split of 2020-04-29 from 8 to 1 alone. Each answer is saved as the connector returned it"
        " under ~/.config/ithildin-runtime/splits/2026-11-27/"
    ),
    (
        "2026-11-27,covered,USO,2026-10-30,2026-11-19,,,,Alpha Vantage's SPLITS function read "
        "through its connector at about 2026-11-27T04:27Z: GLD at sha256 "
        "e2385bb7b104c384ed8f892724ba70e3297d3b8a3c2f1e4b5571de73e0a7c6e3 and SPY at sha256 "
        "e1779bbaf798a1718970fbe3a88b5da27d10b2548bddf00ce089f13121c4afe1 list no split. USO at "
        "sha256 fb893bfd2323a979f2f54e9452e2331f96247c8311d9055cdd409b3adecd5b15 lists 2020-04-29"
        " at a factor of 0.1250 alone and QQQ at sha256 "
        "c64147e09228910757fb33cd3a495a079e5b8954d9f76e25272a684ccc3e642f lists 2000-03-20 at "
        "2.0000 alone. Massive's GET /stocks/v1/splits for GLD USO SPY and QQQ read through its "
        "connector at 2026-11-27T04:26Z at sha256 "
        "aea349d40de211b9fe5f18c09b6227df8639692fe839924f25d246cc99ab33cc lists USO's reverse "
        "split of 2020-04-29 from 8 to 1 alone. Each answer is saved as the connector returned it"
        " under ~/.config/ithildin-runtime/splits/2026-11-27/"
    ),
]


HEAD = "added,source,first_session,last_session,price_kind,venues,delay,volume_counted,read_from"


def _data_rows(path: Path) -> list[str]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [line for line in lines if not line.startswith("#")][1:]


@pytest.mark.parametrize(
    ("path", "pinned"),
    [
        (MASTER, MASTER_ROWS),
        (PRICE_KINDS, KIND_ROWS),
        (QUOTE_KINDS, QUOTE_ROWS),
        (SPLITS, SPLIT_ROWS),
    ],
)
def test_every_row_held_when_it_was_added_is_still_there_unchanged(
    path: Path, pinned: list[str]
) -> None:
    rows = _data_rows(path)
    assert rows[: len(pinned)] == pinned
    assert len(rows) == len(pinned), f"a row was added to {path.name}, so pin it here too"


def test_every_file_loads() -> None:
    assert len(load_master().rows) == len(MASTER_ROWS)
    assert len(load_price_kinds().rows) == len(KIND_ROWS)
    assert len(load_quote_kinds().rows) == len(QUOTE_ROWS)
    assert len(load_splits(master=load_master(), sessions=load_sessions()).rows) == len(SPLIT_ROWS)


CHAN = "quantitative-trading:data/inputdata_etf/{}.csv"
YAHOO = "yahoo:{}_daily_yahoo_2026-11-22.csv"
NOTHING = Kind("unstated", "unstated", "unstated", "unstated")


@pytest.mark.parametrize("name", ["gld", "uso"])
@pytest.mark.parametrize(
    ("source", "before", "first", "last", "after"),
    [
        (CHAN, date(2006, 4, 25), date(2006, 4, 26), date(2012, 4, 9), date(2012, 4, 10)),
        (YAHOO, date(2012, 4, 9), date(2012, 4, 10), date(2026, 9, 3), date(2026, 9, 4)),
    ],
)
def test_a_session_on_each_side_of_each_rows_edges_reads_the_kind_it_should(
    name: str, source: str, before: date, first: date, last: date, after: date
) -> None:
    """Item 4 of the acceptance of #137, whose rows the owner's two rulings state."""
    kinds, own = load_price_kinds(), source.format(name)
    # Typed again, so a typo shared by a row and its pin is still caught.
    shares = "consolidated in shares to the nearest hundred"
    counted = (
        "consolidated in hundreds of shares"
        if source == CHAN
        else shares + {"gld": "", "uso": " before its source's split adjustment"}[name]
    )
    stated = Kind("official close", "consolidated", "none", counted)
    assert [kinds.kind(own, day) for day in (before, first, last, after)] == [
        NOTHING,
        stated,
        stated,
        NOTHING,
    ]


@pytest.mark.parametrize("name", ["gld", "uso"])
def test_the_yahoo_rows_name_the_digest_the_chan_rows_compared_with(name: str) -> None:
    """The tracked rows of 2026-11-22 and of Yahoo name one digest for each file."""
    # By the date each was added, so a later row that corrects one is no second match.
    rows = _data_rows(PRICE_KINDS)
    (yahoo,) = [r for r in rows if r.startswith(f"2026-11-25,yahoo:{name}_")]
    (digest,) = re.findall(r"pins the file at sha256 ([0-9a-f]{64}) ", yahoo)
    (chan,) = [r for r in rows if r.startswith("2026-11-22,quant") and f"/{name}.csv," in r]
    assert f"{name}_daily_yahoo_2026-11-22.csv at sha256 {digest}" in chan


def test_the_captured_bars_row_reads_the_kind_it_should_on_each_side_of_its_edges() -> None:
    kinds = load_price_kinds()
    # Typed again, so a typo shared by the row and its pin is still caught.
    stated = Kind(
        "official close", "consolidated", "none", "consolidated in shares as of the fetch"
    )
    assert [
        kinds.kind("marketlake:capture", day)
        for day in (date(2026, 9, 7), date(2026, 9, 8), date(2027, 3, 31), date(2027, 4, 1))
    ] == [NOTHING, stated, stated, NOTHING]


def test_the_schwab_import_row_reads_the_kind_it_should_on_each_side_of_its_edges() -> None:
    """The row the owner's ruling of 2026-11-26 on #175 states, for the import's 40 sessions."""
    kinds = load_price_kinds()
    # Typed again, so a typo shared by the row and its pin is still caught.
    stated = Kind(
        "official close", "consolidated", "none", "consolidated in shares as of the fetch"
    )
    assert SCHWAB_IMPORT == "schwab:pricehistory"
    assert [
        kinds.kind(SCHWAB_IMPORT, day)
        for day in (date(2026, 9, 3), date(2026, 9, 4), date(2026, 10, 30), date(2026, 11, 2))
    ] == [NOTHING, stated, stated, NOTHING]


def test_the_quote_row_reads_the_kind_it_should_on_each_side_of_its_edges() -> None:
    """The rows the owner's ruling of 2026-11-26 on #133 and #189 state, until #220 renews them.

    The row of 2026-11-27 states what the row of 2026-11-26 does and the close's
    kind too, over the same sessions, so it supersedes it on all of them.
    """
    kinds = load_quote_kinds()
    # Typed again, so a typo shared by the row and its pin is still caught.
    stated = QuoteKind(
        "current best bid and ask",
        "unstated",
        "unstated",
        "shares traded through the day including pre-market and post-market hours",
        "Normal",
        "the previous session's close as Schwab states it on the day of the quote",
    )
    silent = QuoteKind(*["unstated"] * 6)
    assert [
        kinds.kind(day)
        for day in (date(2026, 9, 14), date(2026, 9, 15), date(2027, 2, 25), date(2027, 2, 26))
    ] == [silent, stated, stated, silent]


def unnamed(kinds: PriceKinds, master: SecurityMaster) -> set[tuple[str, date]]:
    """Each instrument on each session that a capture row states and does not name.

    A row has no instrument column, so every ticker marketlake captures reads the
    row's kind, whether or not its evidence looked at that ticker.
    """
    found = set()
    for day in load_sessions().sessions:
        covering = [
            r
            for r in kinds.rows
            if r.source == CAPTURE_SOURCE and r.first_session <= day <= r.last_session
        ]
        if not covering:
            continue
        newest = max(covering, key=lambda r: r.added)
        (names,) = re.findall(r"of ([A-Z]+(?: [A-Z]+)* and [A-Z]+) alone", newest.read_from)
        named = set(re.findall(r"[A-Z]+", names))
        held = {x for r in master.rows if r.covers(day) for x in (r.id, r.ticker)}
        found |= {(i, day) for i in held - named}
    return found


def captured_row() -> KindRow:
    """The row added on 2026-11-25, which a later renewal leaves as it is."""
    (row,) = [
        r
        for r in load_price_kinds().rows
        if r.source == CAPTURE_SOURCE and r.added == date(2026, 11, 25)
    ]
    return row


def test_every_instrument_a_capture_row_states_the_kind_of_is_one_its_evidence_names() -> None:
    master = load_master()
    assert unnamed(load_price_kinds(), master) == set()
    kinds = PriceKinds((captured_row(),))
    # A fifth ticker would inherit the row.
    fifth = MasterRow(date(2026, 12, 1), InstrumentId("IWM"), "IWM", date(2027, 3, 31), None)
    early = replace(fifth, id=InstrumentId("IAU"), ticker="IAU", first_session=date(2026, 9, 8))
    assert unnamed(kinds, replace(master, rows=(*master.rows, fifth))) == {
        ("IWM", date(2027, 3, 31))
    }
    assert ("IAU", date(2026, 9, 8)) in unnamed(kinds, replace(master, rows=(*master.rows, early)))
    # A ticker an id moves to inherits the row too.
    moved = MasterRow(date(2026, 12, 1), InstrumentId("GLD"), "IAU", date(2026, 12, 1), None)
    assert ("IAU", date(2026, 12, 1)) in unnamed(kinds, replace(master, rows=(*master.rows, moved)))


def test_a_later_capture_row_names_the_instruments_it_states_on_its_own() -> None:
    row, master = captured_row(), load_master()
    span = date(2027, 1, 4), date(2027, 3, 26)
    later = replace(
        row,
        added=date(2027, 3, 1),
        first_session=span[0],
        last_session=span[1],
        read_from="made up. Its evidence is of GLD and SPY alone. No bar of USO was compared.",
    )
    found = unnamed(PriceKinds((row, later)), master)
    assert {i for i, _ in found} == {"USO", "QQQ"}
    assert {day for _, day in found} == {
        d for d in load_sessions().sessions if span[0] <= d <= span[1]
    }
    # A new id that takes a named ticker is not named itself.
    alias = MasterRow(date(2026, 12, 1), InstrumentId("ALIAS"), "GLD", date(2026, 12, 1), None)
    aliased = replace(master, rows=(*master.rows, alias))
    assert ("ALIAS", date(2026, 12, 1)) in unnamed(PriceKinds((row,)), aliased)


MASTER_HEAD = "added,id,ticker,first_session,last_session"


def test_an_id_with_two_overlapping_rows_added_one_day_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "master.csv"
    path.write_text(f"{MASTER_HEAD}\n2026-11-22,X,A,2020-01-02,\n2026-11-22,X,B,2021-01-04,\n")
    with pytest.raises(Halt, match="overlaps another row of X added the same day"):
        load_master(path)


def test_a_later_master_row_supersedes_an_earlier_one_on_the_sessions_both_cover(
    tmp_path: Path,
) -> None:
    path = tmp_path / "master.csv"
    path.write_text(
        f"{MASTER_HEAD}\n2026-11-22,X,A,2020-01-02,\n2026-12-01,X,B,2021-01-04,2021-12-31\n"
    )
    master = load_master(path)
    assert master.ticker(InstrumentId("X"), date(2020, 6, 1)) == "A"
    assert master.ticker(InstrumentId("X"), date(2021, 6, 1)) == "B"
    assert master.ticker(InstrumentId("X"), date(2022, 6, 1)) == "A"


def test_two_kind_rows_of_one_source_on_one_session_are_refused(tmp_path: Path) -> None:
    head = (
        "added,source,first_session,last_session,price_kind,venues,delay,volume_counted,read_from"
    )
    row = "2026-11-22,s,2020-01-02,2020-12-31,close,all,none,all,x"
    path = tmp_path / "kinds.csv"
    path.write_text(f"{head}\n{row}\n{row}\n")
    with pytest.raises(Halt, match="covers a session another row of s added the same day covers"):
        load_price_kinds(path)


def test_a_later_kind_row_states_what_an_earlier_one_left_unstated(tmp_path: Path) -> None:
    """A later dated row states what an earlier one left unstated, where both cover a session."""
    silent = "2026-11-22,s,2020-01-02,2020-12-31,unstated,unstated,unstated,unstated,x"
    named = "2026-12-01,s,2020-07-01,2020-12-31,official close,consolidated,none,all,owner"
    path = tmp_path / "kinds.csv"
    path.write_text(f"{HEAD}\n{silent}\n{named}\n")
    kinds = load_price_kinds(path)
    assert kinds.kind("s", date(2020, 6, 30)) == Kind(*("unstated",) * 4)
    assert kinds.kind("s", date(2020, 7, 1)) == Kind(
        "official close", "consolidated", "none", "all"
    )


def test_an_empty_kind_field_is_refused_where_unstated_says_the_source_is_silent(
    tmp_path: Path,
) -> None:
    head = (
        "added,source,first_session,last_session,price_kind,venues,delay,volume_counted,read_from"
    )
    path = tmp_path / "kinds.csv"
    path.write_text(f"{head}\n2026-11-22,s,2020-01-02,2020-12-31,close,all,none,,x\n")
    with pytest.raises(Halt, match="leaves a field empty"):
        load_price_kinds(path)


# The quote-kind contract.

QUOTE_HEAD = (
    "added,source,first_session,last_session,price_kind,last_kind,venues,volume_counted,"
    "normal_status,close_kind,read_from"
)
STATED = "two sides,a sale,all,shares,Normal,a close,x"
SILENT = QuoteKind(*["unstated"] * 6)
SAID = QuoteKind("two sides", "a sale", "all", "shares", "Normal", "a close")


def quote_kinds(tmp_path: Path, *rows: str, head: str = QUOTE_HEAD):
    path = tmp_path / "quotes.csv"
    path.write_text("\n".join([head, *rows]) + "\n")
    return load_quote_kinds(path)


def quote_row(
    added: str = "2026-11-25",
    source: str = QUOTE_SOURCE,
    first: str = "2026-01-02",
    last: str = "2026-12-31",
    stated: str = STATED,
) -> str:
    return f"{added},{source},{first},{last},{stated}"


def test_the_quote_source_is_marketlakes() -> None:
    assert QUOTE_SOURCE == "marketlake:quotes"


def test_a_quote_kind_is_stated_from_its_first_session_to_its_last(tmp_path: Path) -> None:
    made = quote_kinds(tmp_path, quote_row())
    for day, kind in (
        (date(2026, 1, 1), SILENT),
        (date(2026, 1, 2), SAID),
        (date(2026, 12, 31), SAID),
        (date(2027, 1, 1), SILENT),
    ):
        assert made.kind(day) == kind


def test_another_sources_row_states_nothing_of_a_quote(tmp_path: Path) -> None:
    assert (
        quote_kinds(tmp_path, quote_row(source="marketlake:capture")).kind(date(2026, 6, 1))
        == SILENT
    )


def test_two_quote_rows_added_on_one_date_that_cover_a_session_are_refused(tmp_path: Path) -> None:
    with pytest.raises(Halt, match="row 2 covers a session another row of marketlake:quotes"):
        quote_kinds(tmp_path, quote_row(), quote_row(first="2026-12-31", last="2027-06-01"))
    apart = quote_kinds(tmp_path, quote_row(), quote_row(first="2027-01-01", last="2027-06-01"))
    assert apart.kind(date(2027, 1, 1)) == SAID


def test_a_later_quote_row_supersedes_an_earlier_one_where_both_cover(tmp_path: Path) -> None:
    made = quote_kinds(
        tmp_path,
        quote_row(),
        quote_row(
            added="2026-11-26", first="2026-06-01", stated="other,a sale,all,shares,OK,a close,x"
        ),
    )
    assert made.kind(date(2026, 5, 31)) == SAID
    assert made.kind(date(2026, 6, 1)) == QuoteKind(
        "other", "a sale", "all", "shares", "OK", "a close"
    )


@pytest.mark.parametrize(
    ("row", "said"),
    [
        (quote_row(last=""), "row 1 holds '', which is no date"),
        (quote_row(first=""), "row 1 holds '', which is no date"),
        (quote_row(first="2027-01-01"), "row 1 ends before it starts"),
        (quote_row(source=""), "row 1 leaves a field empty"),
        *(
            (quote_row(stated=",".join(cut)), "row 1 leaves a field empty")
            for cut in (
                ["" if n == gone else f for n, f in enumerate(STATED.split(","))]
                for gone in range(6)
            )
        ),
    ],
)
def test_a_quote_row_that_is_not_whole_is_refused(tmp_path: Path, row: str, said: str) -> None:
    with pytest.raises(Halt, match=said):
        quote_kinds(tmp_path, row)


def test_an_unstated_field_of_each_kind_reads_as_itself(tmp_path: Path) -> None:
    for n in range(6):
        stated = ["unstated" if n == m else f for m, f in enumerate(STATED.split(","))]
        made = quote_kinds(tmp_path, quote_row(stated=",".join(stated))).kind(date(2026, 6, 1))
        assert made == QuoteKind(*stated[:6])


@pytest.mark.parametrize("head", [HEAD, QUOTE_HEAD.replace("last_kind,", ""), QUOTE_HEAD + ",y"])
def test_a_quote_file_with_another_header_is_refused(tmp_path: Path, head: str) -> None:
    with pytest.raises(Halt, match="has the header"):
        quote_kinds(tmp_path, head=head)


def test_each_instruments_earliest_covered_row_starts_where_its_captured_bars_do() -> None:
    """A split before it reads as one the platform held no captured bar across.

    data/splits.py takes a raw bar after a split as on the scale the platform first
    held only where the split falls before this session, so a later start would let a
    split's halt end while a book written before it is on the old scale. SPY and QQQ are
    captured from 2026-09-08. GLD and USO are captured from 2026-11-02, and their rows
    start on 2026-10-30, the last session of Schwab's import before it, so the pair across
    that change is covered too.
    """
    record = load_splits(master=load_master(), sessions=load_sessions())
    assert {i: record.covered_from(InstrumentId(i)) for i in ("SPY", "QQQ", "GLD", "USO")} == {
        "SPY": date(2026, 9, 8),
        "QQQ": date(2026, 9, 8),
        "GLD": date(2026, 10, 30),
        "USO": date(2026, 10, 30),
    }
