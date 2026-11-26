"""The measurement of how often recorded quotes would be refused, #133's item 3.

Every price is made up. Each made-up session holds one kind of refusal, so a
count in what the command prints names the session that made it.
"""

from __future__ import annotations

import io
from collections.abc import Iterable
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket
from ithildinruntime.core import BeforeClose, InstrumentId, quote_faults, to_utc
from ithildinruntime.data.reference import QUOTE_SOURCE, QuoteKind, QuoteKindRow, QuoteKinds
from ithildinruntime.runtime import quote_refusals as qr
from tests.support import lake as lakes
from tests.support.gates import DECISION
from tests.support.quotes import MAX_AGE, quote
from tests.support.sealed import BIDS, close_row, partition, row
from tests.support.segments import DAY, DAYS, GLD, TABLE, USO
from tests.test_data_source import Clock

MINUTE = timedelta(minutes=1)
SECOND = timedelta(seconds=1)
FIRST = DAYS.index(DAY)
PLAIN, FLAGS, STAMPS, UNLISTED, UNSHARED = DAYS[FIRST : FIRST + 5]
LAST = UNSHARED
BOTH = ("GLD", "USO")
EMPTY = QuoteKinds(())


def at(day: date) -> datetime:
    return to_utc(day, BeforeClose(20), TABLE)


def rows(t: str, day: date, backs: Iterable[int], **newest: object) -> list[dict[str, object]]:
    """One row a minute, `backs` minutes before the decision, each a cent wide.

    The newest row takes `newest`'s changes. Each row's sides are stamped at its
    minute, and its capture lands a second later.
    """
    backs = sorted(backs)
    made = []
    for back in backs:
        changes = newest if back == backs[0] else {}
        made.append(row(t, at(day) - back * MINUTE, **({"ask": BIDS[t] + 0.01} | changes)))
    return made


def millis(when: datetime) -> int:
    return int(when.timestamp() * 1000)


@pytest.fixture
def lake(built_lake: Path, tmp_path: Path) -> Path:
    small = lakes.copy_lake(built_lake, tmp_path / "small", kept=())
    for day in (PLAIN, FLAGS, STAMPS, UNLISTED, UNSHARED):
        made = {t: rows(t, day, range(1, 31)) for t in BOTH}
        if day == FLAGS:
            made["GLD"] = rows("GLD", day, range(1, 31), bid_size=0)
            made["USO"] = rows("USO", day, range(1, 31), ask=38.99, security_status="Halted")
        if day == STAMPS:
            stamp = at(day) - MINUTE
            made["GLD"] = rows("GLD", day, range(1, 31), bid_time=millis(stamp + 3 * SECOND))
            made["USO"] = rows(
                "USO", day, range(1, 31), ask_time=millis(stamp + 1500 * SECOND / 1000)
            )
        if day == UNLISTED:
            del made["USO"]
        if day == UNSHARED:
            made = {"GLD": rows("GLD", day, (1, 8)), "USO": rows("USO", day, (2, 8))}
        session = TABLE.session(day)
        for t, held in made.items():
            partition(small, t, *held, close_row(t, session.close), day=day)
    return small


def run(lake: Path, tmp_path: Path, *args: str, kinds: QuoteKinds = EMPTY) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = qr.main(
        [
            "--lake",
            str(lake),
            "--instruments",
            *BOTH,
            "--from",
            str(PLAIN),
            "--through",
            str(LAST),
            *args,
        ],
        kinds=kinds,
        clock=Clock(),
        out=out,
        err=err,
    )
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def measured(lake: Path, tmp_path: Path) -> qr.Measurement:
    return qr.measure(
        FolderBucket(lake),
        tmp_path / "mirror",
        [GLD, USO],
        PLAIN,
        LAST,
        normal_status="Normal",
        clock=Clock(),
        kinds=EMPTY,
    )


def days(m: qr.Measurement, age: int, spread: Decimal, moment: int) -> dict[date, set[str]]:
    """The reasons each refused session gave."""
    out = {}
    for d in m.days:
        if found := d.refused(age, spread, moment):
            out[d.session.day] = {f"{r}, {i}" if i else r for r, i in found}
    return out


def test_the_steps_are_the_ones_written_on_the_issue() -> None:
    assert qr.AGES == (10, 15, 30, 45, 60, 75, 90, 105, 120, 150, 180, 240, 300, 600, 900)
    assert [str(s) for s in qr.SPREADS] == [
        "0.00002",
        "0.00005",
        "0.0001",
        "0.0002",
        "0.0005",
        "0.001",
        "0.002",
        "0.005",
        "0.01",
    ]
    assert (qr.DECISION, qr.SEND, qr.CUTOFF) == (BeforeClose(20), 30 * SECOND, BeforeClose(12))


def test_a_quote_a_minute_old_passes_at_75_seconds_and_not_60(measured: qr.Measurement) -> None:
    widest = qr.SPREADS[-1]
    assert days(measured, 60, widest, 0)[PLAIN] == {"too old, GLD", "too old, USO"}
    assert PLAIN not in days(measured, 75, widest, 0)


def test_each_moment_adds_its_own_seconds(measured: qr.Measurement) -> None:
    widest = qr.SPREADS[-1]
    # Stamped 60 seconds before the decision, with the clock bound of one second.
    assert PLAIN in days(measured, 90, widest, 1) and PLAIN not in days(measured, 105, widest, 1)
    # The cutoff is 480 seconds after the decision.
    assert PLAIN in days(measured, 300, widest, 2) and PLAIN not in days(measured, 600, widest, 2)
    (day,) = [d for d in measured.days if d.session.day == PLAIN]
    assert day.moments == (at(PLAIN), at(PLAIN) + 30 * SECOND, at(PLAIN) + 8 * MINUTE)


def test_each_spread_refuses_the_legs_wider_than_it(measured: qr.Measurement) -> None:
    # A cent on 160 and on 39, a share of about 0.0000625 and 0.000256.
    assert days(measured, 900, Decimal("0.00005"), 0)[PLAIN] == {"too wide, GLD", "too wide, USO"}
    assert days(measured, 900, Decimal("0.0002"), 0)[PLAIN] == {"too wide, USO"}
    assert PLAIN not in days(measured, 900, Decimal("0.0005"), 0)


def test_each_reason_names_its_instrument(measured: qr.Measurement) -> None:
    got = days(measured, 900, qr.SPREADS[-1], 0)
    assert got[FLAGS] == {"crossed, USO", "status not normal, USO"}
    assert got[STAMPS] == {"a stamp after its capture, GLD"}
    assert got[UNLISTED] == {"partitions not listed"}
    assert set(got) == {FLAGS, STAMPS, UNLISTED}


def test_each_age_is_its_own_read(measured: qr.Measurement) -> None:
    """A small age stops the walk before the capture both legs share."""
    widest = qr.SPREADS[-1]
    for age in (10, 45, 60, 105):
        assert days(measured, age, widest, 0)[UNSHARED] == {"no capture every instrument shares"}
    for age in (120, 300):
        assert days(measured, age, widest, 0)[UNSHARED] == {"too old, GLD", "too old, USO"}
    assert UNSHARED not in days(measured, 600, widest, 0)


def test_what_it_prints(lake: Path, tmp_path: Path) -> None:
    code, out, err = run(lake, tmp_path, "--normal-status", "Normal")
    assert (code, err) == (0, "")
    assert "exploratory" in out.splitlines()[0]
    tables = [line for line in out.splitlines() if line.startswith("**Table:**")]
    assert len(tables) == 6 and all(line.endswith("exploratory") for line in tables)
    assert "at the decision, 3 of 5 sessions are refused" in out
    for line in (
        "| a stamp after its capture | GLD 1 |",
        "| crossed | USO 1 |",
        "| partitions not listed | 1 |",
        "| status not normal | USO 1 |",
    ):
        assert line in out
    assert "| 60 | 5 of 5: no capture every instrument shares, 1; too old, GLD 3, USO 3 |" in out
    assert "| 75 | 4 of 5: no capture every instrument shares, 1 | 5 of 5: " in out
    assert "| 600 | 3 of 5 | 3 of 5 | 4 of 5: too old, GLD 1, USO 1 |" in out
    assert "| 0.0001 | 5 of 5: too wide, USO 3 |" in out
    assert "| 0.0005 | 3 of 5 |" in out
    assert "| A bid or ask stamp later than its capture | 2 of 8 |" in out
    assert (
        "| The same, by more than the clock bound of 1 second, which is refused | 1 of 8 |" in out
    )
    assert "| A bid or ask size of zero | 1 of 8 |" in out
    assert "| The same, passing every check at the widest steps | 1 of 1 |" in out
    assert "| `security_status` of 'Halted' | 1 of 8 |" in out
    assert "| `security_status` of 'Normal' | 7 of 8 |" in out
    assert "| `realtime` of True | 8 of 8 |" in out
    assert f"| partitions not listed | {UNLISTED} | {', '.join(map(str, qr.AGES))} |" in out
    assert (
        f"| no capture every instrument shares | {UNSHARED} | 10, 15, 30, 45, 60, 75, 90, 105 |"
        in out
    )
    assert "stand-in on 5 of 5 sessions, stating the normal status 'Normal'" in out
    # GLD's quote is a minute old at the decision on three sessions, and eight on one.
    assert "| GLD | 4 | 60.000 | 60.000 | 480.000 | 59.000 | 59.000 | 479.000 |" in out
    for price in ("160", "39.0", "38.99"):
        assert price not in out


def test_a_tracked_row_takes_the_place_of_the_stand_in(lake: Path, tmp_path: Path) -> None:
    kind = QuoteKind("consolidated quote", "consolidated sale", "consolidated", "shares", "Halted")
    kinds = QuoteKinds((QuoteKindRow(PLAIN, QUOTE_SOURCE, FLAGS, FLAGS, kind, "a test"),))
    m = qr.measure(
        FolderBucket(lake),
        tmp_path / "m",
        [GLD, USO],
        PLAIN,
        LAST,
        normal_status="Normal",
        clock=Clock(),
        kinds=kinds,
    )
    assert [d.stand_in for d in m.days] == [True, False, True, True, True]
    assert days(m, 900, qr.SPREADS[-1], 0)[FLAGS] == {"crossed, USO", "status not normal, GLD"}


def test_no_row_and_no_stand_in_refuses(lake: Path, tmp_path: Path) -> None:
    code, out, err = run(lake, tmp_path)
    assert (code, out) == (1, "")
    assert err == (
        f"quote_refusals: no row of quote_kinds.csv covers {PLAIN}, so give --normal-status "
        "for a stand-in\n"
    )


@pytest.mark.parametrize(
    "args",
    [["--instruments"], ["--from", "2026-13-01"], ["--instruments", "GLD", "GLD"]],
)
def test_a_bad_argument_prints_one_line(lake: Path, tmp_path: Path, args: list[str]) -> None:
    code, out, err = run(lake, tmp_path, "--normal-status", "Normal", *args)
    assert (code, out) == (1, "") and err.count("\n") == 1 and str(lake) not in err


NOW = DECISION
# One change to a good quote for each reason the judge gives, and its class.
REASONS = [
    ({"max_age": 5}, NOW, "a maximum it cannot be read under"),
    ({}, NOW - SECOND, "now before the decision"),
    ({"bid": Decimal(0)}, NOW, "a side missing or not above zero"),
    ({"bid": Decimal("101")}, NOW, "crossed"),
    ({"ask": Decimal("150")}, NOW, "too wide"),
    ({"realtime": False}, NOW, "not real-time"),
    ({"status": "Halted"}, NOW, "status not normal"),
    ({"price_kind": "unstated"}, NOW, "price kind unstated"),
    ({"bid_at": None}, NOW, "a stamp that is no instant"),
    ({"bid_at": DECISION}, NOW, "a stamp after its capture"),
    ({"captured_at": DECISION + SECOND}, NOW + 2 * SECOND, "captured outside the session"),
    ({}, NOW + MAX_AGE * SECOND, "too old"),
]


@pytest.mark.parametrize(("changes", "now", "reason"), REASONS)
def test_every_reason_of_the_judge_is_classed(changes: dict, now: datetime, reason: str) -> None:
    good = quote("GLD", "100", "100.5", DECISION)
    faults = quote_faults(replace(good, **changes), DECISION, now)
    assert faults and {qr.quote_reason(f) for f in faults} == {reason}


def test_every_class_is_tested_and_an_unknown_reason_is_said() -> None:
    assert {r for _, _, r in REASONS} == {name for name, _ in qr._QUOTE_REASONS}
    assert qr.quote_reason("something new") == qr.UNCLASSED
    assert qr.read_reason("something new") == "another fault of the read"
    assert InstrumentId("GLD") == GLD
