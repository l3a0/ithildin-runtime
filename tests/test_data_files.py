"""The data module's two files that only grow, pinned row by row.

Acceptance test 20 on #5. CI's checkout holds no history to compare with, so
each row the security master and the price-kind contract held when it was
added is written here by hand. Editing or removing a row fails the test, and
adding one means adding it here too.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ithildinruntime.core import Halt, InstrumentId
from ithildinruntime.data.reference import (
    MASTER,
    PRICE_KINDS,
    Kind,
    load_master,
    load_price_kinds,
)

MASTER_ROWS = [
    "2026-11-22,GLD,GLD,2004-11-18,",
    "2026-11-22,USO,USO,2006-04-10,",
    "2026-11-22,SPY,SPY,1993-01-29,",
    "2026-11-22,QQQ,QQQ,1999-03-10,2004-11-30",
    "2026-11-22,QQQ,QQQQ,2004-12-01,2011-03-22",
    "2026-11-22,QQQ,QQQ,2011-03-23,",
]

_YAHOO = (
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
KIND_ROWS = [
    f"2026-11-22,quantitative-trading:data/inputdata_etf/{name}.csv,2006-04-26,2012-04-09,"
    f"official close,consolidated,none,consolidated in hundreds of shares,"
    f"{_YAHOO.format(split=_SPLIT[name], volume=_VOLUME[name])}"
    for name in ("gld", "uso")
]


HEAD = "added,source,first_session,last_session,price_kind,venues,delay,volume_counted,read_from"


def _data_rows(path: Path) -> list[str]:
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    return [line for line in lines if not line.startswith("#")][1:]


@pytest.mark.parametrize(("path", "pinned"), [(MASTER, MASTER_ROWS), (PRICE_KINDS, KIND_ROWS)])
def test_every_row_held_when_it_was_added_is_still_there_unchanged(
    path: Path, pinned: list[str]
) -> None:
    rows = _data_rows(path)
    assert rows[: len(pinned)] == pinned
    assert len(rows) == len(pinned), f"a row was added to {path.name}, so pin it here too"


def test_both_files_load() -> None:
    assert len(load_master().rows) == len(MASTER_ROWS)
    assert len(load_price_kinds().rows) == len(KIND_ROWS)


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
