"""The calendar function, the committed session table and the plan builder.

docs/architecture/08-crosscutting-concepts.md's `#### Which clock a schedule
follows, and how it becomes UTC` gives the rules, and #22's acceptance 7 the
early close below.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, time

import pytest

from ithildinruntime import core
from ithildinruntime.core import BeforeClose, ExchangeTime, Halt, Mode, to_utc

CLOSE_20 = BeforeClose(20)


def test_close_minus_20_moves_with_the_early_close() -> None:
    assert to_utc(date(2026, 11, 27), CLOSE_20) == datetime(2026, 11, 27, 17, 40, tzinfo=UTC)
    assert to_utc(date(2026, 11, 30), CLOSE_20) == datetime(2026, 11, 30, 20, 40, tzinfo=UTC)


def test_a_time_on_the_exchanges_clock_follows_its_offset_from_utc() -> None:
    assert to_utc(date(2026, 3, 6), ExchangeTime(time(15, 40))) == datetime(
        2026, 3, 6, 20, 40, tzinfo=UTC
    )
    assert to_utc(date(2026, 3, 9), ExchangeTime(time(15, 40))) == datetime(
        2026, 3, 9, 19, 40, tzinfo=UTC
    )


def test_a_date_with_no_session_halts() -> None:
    with pytest.raises(Halt) as raised:
        to_utc(date(2026, 11, 26), CLOSE_20)
    assert raised.value.reason == "the session table holds no session on 2026-11-26"


@pytest.mark.parametrize("day", [date(2005, 12, 30), date(2028, 1, 3)])
def test_a_date_outside_the_tables_range_halts(day: date) -> None:
    with pytest.raises(Halt) as raised:
        to_utc(day, CLOSE_20)
    assert "outside the session table's range, 2006-01-01 to 2027-12-31" in raised.value.reason


def test_the_committed_table_matches_its_sha256() -> None:
    data = core.sessions.TABLE.read_bytes()
    assert hashlib.sha256(data).hexdigest() == core.sessions.TABLE_SHA256
    table = core.load_sessions()
    assert (table.first, table.last) == (date(2006, 1, 1), date(2027, 12, 31))
    assert min(table.sessions) == date(2006, 1, 3)


def test_a_table_whose_bytes_changed_halts() -> None:
    data = core.sessions.TABLE.read_bytes().replace(
        b"2026-11-27,14:30,18:00", b"2026-11-27,14:30,21:00"
    )
    with pytest.raises(Halt) as raised:
        core.parse_sessions(data, core.sessions.TABLE_SHA256)
    assert "SHA-256" in raised.value.reason


def _table(text: str) -> core.SessionTable:
    data = text.encode("ascii")
    return core.parse_sessions(data, hashlib.sha256(data).hexdigest())


SMALL = (
    "# a table for tests\n"
    "range,2026-11-25,2026-12-01\n"
    "session,open_utc,close_utc,utc_offset_minutes\n"
    "2026-11-25,14:30,21:00,-300\n"
    "2026-11-27,14:30,18:00,-300\n"
    "2026-11-30,14:30,21:00,-300\n"
)


@pytest.mark.parametrize(
    "text",
    [
        SMALL.replace("range,", "span,"),
        SMALL.replace("session,open_utc", "day,open_utc"),
        SMALL + "2026-11-30,14:30,21:00,-300\n",
        SMALL + "2026-12-02,14:30,21:00,-300\n",
        SMALL.replace("18:00", "14:00"),
        SMALL.replace("2026-11-25,14:30", "2026-11-25,1430"),
    ],
)
def test_a_table_that_cannot_be_read_halts(text: str) -> None:
    with pytest.raises(Halt):
        _table(text)


def test_a_one_day_plan_holds_that_days_decisions() -> None:
    built = core.build_plan(
        deployment="paper",
        mode=Mode.PAPER,
        release="r1",
        schedules={
            "bollinger": [CLOSE_20],
            "momentum": [ExchangeTime(time(10, 0)), CLOSE_20],
        },
        first=date(2026, 11, 30),
        last=date(2026, 11, 30),
        table=_table(SMALL),
    )
    plan = built.plan
    assert (plan.deployment, plan.run, plan.mode, plan.release) == (
        "paper",
        "2026-11-30",
        Mode.PAPER,
        "r1",
    )
    assert [(d.at, d.strategies) for d in plan.decisions] == [
        (datetime(2026, 11, 30, 15, 0, tzinfo=UTC), ("momentum",)),
        (datetime(2026, 11, 30, 20, 40, tzinfo=UTC), ("bollinger", "momentum")),
    ]
    assert built.sessions_left == 0


def test_a_clock_time_after_an_early_close_is_dropped() -> None:
    built = core.build_plan(
        deployment="paper",
        mode=Mode.PAPER,
        release="r1",
        schedules={"late": [ExchangeTime(time(15, 40)), ExchangeTime(time(13, 0))]},
        first=date(2026, 11, 27),
        last=date(2026, 11, 27),
        table=_table(SMALL),
    )
    assert [d.at for d in built.plan.decisions] == [datetime(2026, 11, 27, 18, 0, tzinfo=UTC)]
    assert built.sessions_left == 1


def test_a_backtest_plan_spans_every_session() -> None:
    built = core.build_plan(
        deployment="backtest",
        mode=Mode.BACKTEST,
        release="r1",
        schedules={"bollinger": [CLOSE_20]},
        first=date(2026, 11, 25),
        last=date(2026, 11, 30),
        table=_table(SMALL),
    )
    assert built.plan.run == "2026-11-25_2026-11-30"
    assert [d.at for d in built.plan.decisions] == [
        datetime(2026, 11, 25, 20, 40, tzinfo=UTC),
        datetime(2026, 11, 27, 17, 40, tzinfo=UTC),
        datetime(2026, 11, 30, 20, 40, tzinfo=UTC),
    ]


@pytest.mark.parametrize(
    ("first", "last"),
    [(date(2026, 11, 26), date(2026, 11, 30)), (date(2026, 11, 25), date(2026, 12, 2))],
)
def test_a_span_that_starts_or_ends_off_the_table_halts(first: date, last: date) -> None:
    with pytest.raises(Halt):
        core.build_plan(
            deployment="backtest",
            mode=Mode.BACKTEST,
            release="r1",
            schedules={"bollinger": [CLOSE_20]},
            first=first,
            last=last,
            table=_table(SMALL),
        )


def test_the_committed_table_reports_the_sessions_it_has_left() -> None:
    built = core.build_plan(
        deployment="paper",
        mode=Mode.PAPER,
        release="r1",
        schedules={"bollinger": [CLOSE_20]},
        first=date(2027, 12, 30),
        last=date(2027, 12, 30),
    )
    assert built.sessions_left == 1
    assert core.decode(core.encode(built.plan), core.Plan) == built.plan


@pytest.mark.parametrize("minutes", [True, 20.5, -30, "20"])
def test_minutes_before_the_close_are_a_whole_number_not_below_zero(minutes: object) -> None:
    with pytest.raises(ValueError):
        to_utc(date(2026, 11, 30), BeforeClose(minutes))  # type: ignore[arg-type]
    assert to_utc(date(2026, 11, 30), BeforeClose(0)) == datetime(2026, 11, 30, 21, 0, tzinfo=UTC)


def test_a_table_whose_close_equals_its_open_halts() -> None:
    with pytest.raises(Halt):
        _table(SMALL.replace("2026-11-27,14:30,18:00", "2026-11-27,14:30,14:30"))


def _plan(times: list, day: date = date(2026, 11, 30)) -> core.BuiltPlan:
    return core.build_plan(
        deployment="paper",
        mode=Mode.PAPER,
        release="r1",
        schedules={"s": times},
        first=day,
        last=day,
        table=_table(SMALL),
    )


@pytest.mark.parametrize(
    "when",
    [
        ExchangeTime(time(16, 30)),
        ExchangeTime(time(9, 0)),
        ExchangeTime(time(3, 0)),
        BeforeClose(600),
    ],
    ids=["after a regular close", "before the open", "at night", "close minus 600"],
)
def test_a_decision_outside_a_regular_session_halts(when: object) -> None:
    with pytest.raises(Halt) as raised:
        _plan([when])
    assert "outside that session's 14:30 to 21:00 UTC" in raised.value.reason


@pytest.mark.parametrize(
    ("mode", "first", "last"),
    [
        (Mode.PAPER, date(2026, 11, 30), date(2026, 11, 30)),
        (Mode.PAPER, date(2026, 11, 27), date(2026, 11, 27)),
        (Mode.BACKTEST, date(2026, 11, 25), date(2026, 11, 30)),
    ],
)
def test_minutes_before_an_early_close_that_fall_before_the_open_halt_the_whole_plan(
    mode: Mode, first: date, last: date
) -> None:
    def build() -> core.BuiltPlan:
        return core.build_plan(
            deployment="paper",
            mode=mode,
            release="r1",
            schedules={"bollinger": [CLOSE_20], "early": [BeforeClose(240)]},
            first=first,
            last=last,
            table=_table(SMALL),
        )

    if first == date(2026, 11, 30):
        assert len(build().plan.decisions) == 2
        return
    with pytest.raises(Halt) as raised:
        build()
    assert "early decides at BeforeClose(minutes=240) on 2026-11-27" in raised.value.reason


def test_the_bounds_of_a_session_are_inside_it() -> None:
    built = _plan([ExchangeTime(time(9, 30)), ExchangeTime(time(16, 0)), BeforeClose(0)])
    assert [d.at for d in built.plan.decisions] == [
        datetime(2026, 11, 30, 14, 30, tzinfo=UTC),
        datetime(2026, 11, 30, 21, 0, tzinfo=UTC),
    ]


def test_a_clock_time_after_the_regular_close_halts_even_on_an_early_close() -> None:
    with pytest.raises(Halt):
        _plan([ExchangeTime(time(16, 30))], day=date(2026, 11, 27))


def test_a_plan_records_its_format() -> None:
    assert _plan([CLOSE_20]).plan.format == core.PLAN_FORMAT == 1


def test_the_regular_close_on_an_early_close_is_dropped() -> None:
    assert _plan([ExchangeTime(time(16, 0))], day=date(2026, 11, 27)).plan.decisions == ()


def test_a_table_with_no_sessions_halts_the_plan() -> None:
    empty = _table("range,2026-11-25,2026-12-01\nsession,open_utc,close_utc,utc_offset_minutes\n")
    with pytest.raises(Halt):
        empty.regular_close()
