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
from tests.support.sealed import BIDS, close_row, part_key, partition, row
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
        if day == PLAIN:
            made["USO"] = rows("USO", day, range(1, 31), ask_size=None)
        if day == FLAGS:
            # A zero bid size, and a bid stamped exactly at its capture, which is not later.
            stamp = at(day) - MINUTE
            made["GLD"] = rows(
                "GLD", day, range(1, 31), bid_size=0, bid_time=millis(stamp + SECOND)
            )
            made["USO"] = rows("USO", day, range(1, 31), ask=38.99, security_status="Halted")
        if day == STAMPS:
            stamp = at(day) - MINUTE
            made["GLD"] = rows(
                "GLD", day, range(1, 31), bid_time=millis(stamp + 3 * SECOND), ask_size=0
            )
            # Exactly the clock bound after its capture, which the check passes.
            made["USO"] = rows(
                "USO", day, range(1, 31), ask_time=millis(stamp + 2 * SECOND), bid_size=None
            )
        if day == UNLISTED:
            del made["USO"]
        if day == UNSHARED:
            # GLD's quote at eight minutes has a zero bid size and passes at the decision, and
            # at the cutoff it is past the widest age.
            gld = rows("GLD", day, (1,)) + rows("GLD", day, (8,), bid_size=0)
            made = {"GLD": gld, "USO": rows("USO", day, (2, 8))}
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
    assert (
        "| 60 | 5 of 5, 4 for age: no capture every instrument shares, 1; too old, GLD 3, USO 3 |"
    ) in out
    assert "| 75 | 4 of 5, 1 for age: no capture every instrument shares, 1 | 5 of 5, 4 for" in out
    assert "| 600 | 3 of 5 | 3 of 5 | 4 of 5, 1 for age: too old, GLD 1, USO 1 |" in out
    assert "| 0.0001 | 5 of 5, 3 for the spread: too wide, USO 3 |" in out
    assert "| 0.0005 | 3 of 5 |" in out
    assert "| Reason | Count |" in out
    assert (
        "**Table:** Sessions refused at each age, with the spread at its widest step of 0.01, then "
        "those refused for age, with the reasons of age by instrument and session, or by session "
        "for a fault of the read, exploratory"
    ) in out
    assert (
        "A reason a quote gives counts once for each instrument and session it refuses, and "
        "each kind of fault of the read once for each session."
    ) in out
    assert "the sessions refused for age include any that another reason refuses as well" in out
    assert qr.PLAN in out
    # 1 − 0.05 ** (1 / 5) is 0.4507, the share five sessions with none refused still allow.
    assert "5 sessions is a short record. A step that refuses none of them still allows a " in out
    assert (
        "true share as high as 45.1%, the one-sided 95% bound 1 − 0.05^(1/n) for sessions drawn "
        "independently. Sessions in a row are not independent, so the true bound is likely wider."
    ) in out
    assert "the send 30 seconds after the decision is a stand-in" in out
    assert "Refused at the cutoff at close−12, a proposal |" in out
    assert "| A bid or ask stamp later than its capture | 2 of 8 |" in out
    assert (
        "| The same, by more than the clock bound of 1 second, which the check refuses | 1 of 8 |"
    ) in out
    assert "| A bid or ask size of zero | 3 of 8 |" in out
    assert "| A bid or ask size that is missing | 2 of 8 |" in out
    assert "| Either, passing every check at the widest steps at the decision | 4 of 5 |" in out
    assert "| `security_status` of 'Halted' | 1 of 8 |" in out
    assert "| `security_status` of 'Normal' | 7 of 8 |" in out
    assert "| `realtime` of True | 8 of 8 |" in out
    assert f"| partitions not listed | {UNLISTED} | {', '.join(map(str, qr.AGES))} |" in out
    assert (
        f"| no capture every instrument shares | {UNSHARED} | 10, 15, 30, 45, 60, 75, 90, 105 |"
        in out
    )
    assert "stand-in on 5 of 5 sessions. It states the normal status 'Normal', and a" in out
    # GLD's quote is a minute old at the decision on three sessions, and eight on one.
    for i in BOTH:
        assert f"| {i} | 4 | 60.000 | 60.000 | 480.000 | 59.000 | 59.000 | 479.000 |" in out
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
    assert "a stand-in on 4 of 5 sessions." in qr.render(m)
    assert days(m, 900, qr.SPREADS[-1], 0)[FLAGS] == {"crossed, USO", "status not normal, GLD"}


def test_a_session_the_copy_does_not_hold_stops_the_run(lake: Path, tmp_path: Path) -> None:
    past = DAYS[FIRST + 5]
    code, out, err = run(lake, tmp_path, "--normal-status", "Normal", "--through", str(past))
    assert (code, out) == (1, "")
    assert err == (
        f"quote_refusals: the manifest lists no sealed quotes of GLD, USO for {past}, so a "
        "decision that day would not take the sealed read\n"
    )


@pytest.mark.parametrize("ticker", BOTH)
def test_a_damaged_partition_stops_the_run(lake: Path, tmp_path: Path, ticker: str) -> None:
    path = lake / part_key(ticker, PLAIN)
    path.write_bytes(path.read_bytes() + b"x")
    code, out, err = run(lake, tmp_path, "--normal-status", "Normal")
    assert (code, out) == (1, "") and err.startswith(f"quote_refusals: on {PLAIN}, ")
    assert "SHA-256" in err and str(lake) not in err


def test_the_tracked_row_on_every_session_says_so(lake: Path, tmp_path: Path) -> None:
    kind = QuoteKind("consolidated quote", "consolidated sale", "consolidated", "shares", "Normal")
    kinds = QuoteKinds((QuoteKindRow(PLAIN, QUOTE_SOURCE, PLAIN, LAST, kind, "a test"),))
    code, out, err = run(lake, tmp_path, kinds=kinds)
    assert (code, err) == (0, "")
    assert "The tracked row of quote_kinds.csv covers every session." in out
    assert "stand-in on" not in out


def test_a_reason_both_sides_give_counts_once(measured: qr.Measurement) -> None:
    (day,) = [d for d in measured.days if d.session.day == PLAIN]
    zero = replace(day.widest.quotes[GLD], bid=Decimal(0), ask=Decimal(0))
    read = replace(day.widest, quotes={**day.widest.quotes, GLD: zero})
    bare = replace(day, reads={age: read for age in qr.AGES})
    found = bare.refused(900, qr.SPREADS[-1], 0)
    assert found[("a side missing or not above zero", "GLD")] == 1


def test_a_missing_partition_stops_the_run(lake: Path, tmp_path: Path) -> None:
    (lake / part_key("USO", PLAIN)).unlink()
    code, out, err = run(lake, tmp_path, "--normal-status", "Normal")
    assert (code, out) == (1, "") and err.startswith(f"quote_refusals: on {PLAIN}, ")
    assert err.endswith("so the data is not ready\n")


@pytest.mark.parametrize("n", [1, 5, 18, 34, 600])
def test_the_bound_is_rounded_up(n: int) -> None:
    # 1 − 0.05 ** (1 / n): 0.95, 0.4507, 0.1533, 0.0843 and 0.0050.
    assert qr._bound(n) == {1: "95.0%", 5: "45.1%", 18: "15.4%", 34: "8.5%", 600: "0.5%"}[n]


def test_a_fault_of_the_read_counts_once_for_its_session(measured: qr.Measurement) -> None:
    (day,) = [d for d in measured.days if d.session.day == PLAIN]
    twice = ("q/GLD is quarantined in marketlake", "q/USO is quarantined in marketlake")
    read = replace(day.widest, quotes={}, faults=twice)
    bare = replace(day, reads={age: read for age in qr.AGES})
    assert bare.refused(900, qr.SPREADS[-1], 0) == {("quarantined", ""): 1}


def test_a_read_with_no_fault_and_no_quote_refuses(measured: qr.Measurement) -> None:
    (day,) = [d for d in measured.days if d.session.day == PLAIN]
    empty = replace(day.widest, quotes={}, faults=())
    bare = replace(day, reads={age: empty for age in qr.AGES})
    assert set(bare.refused(900, qr.SPREADS[-1], 0)) == {("no quote", "GLD"), ("no quote", "USO")}


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


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (
            ["--from", "2012-04-14", "--through", "2012-04-15"],
            "no session falls from 2012-04-14 to 2012-04-15",
        ),
        (["--from", "20120410"], "argument --from: '20120410' is no date"),
    ],
)
def test_each_refusal_says_why(lake: Path, tmp_path: Path, args: list[str], said: str) -> None:
    code, out, err = run(lake, tmp_path, "--normal-status", "Normal", *args)
    assert (code, out, err) == (1, "", f"quote_refusals: {said}\n")


def test_an_error_names_its_type_and_no_folder(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError(f"{lake}/manifest.jsonl and /elsewhere/x went missing")

    monkeypatch.setattr(qr, "measure", broken)
    code, out, err = run(lake, tmp_path, "--normal-status", "Normal")
    assert (code, out) == (1, "")
    assert err == "quote_refusals: OSError: <lake>/manifest.jsonl and <path> went missing\n"


def test_a_session_with_no_fault_says_so(lake: Path, tmp_path: Path) -> None:
    code, out, err = run(lake, tmp_path, "--normal-status", "Normal", "--through", str(PLAIN))
    assert (code, err) == (0, "") and "The read gave no fault at any age." in out


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


# One fault text of the read's for each class, as data's read words it.
READ_FAULTS = [
    (
        "the manifest lists q/GLD and not q/USO, and a sealed read never opens a segment",
        "partitions not listed",
    ),
    (
        "no capture by 2026-11-02T20:39:45+00:00 holds a row of every instrument: GLD has 5",
        "no capture every instrument shares",
    ),
    ("q/GLD holds no row of the session's equity close", "no row of the equity close"),
    ("q/GLD is quarantined in marketlake", "quarantined"),
    ("GLD has 2 rows captured at 2026-11-02T20:39:01+00:00, in q/GLD", "two rows in one capture"),
    (
        "q/GLD has no row at 2026-11-02T20:39:00+00:00, and 3 snap_ts values in it cannot be read",
        "unreadable snap_ts values",
    ),
    ("q/GLD could not be linked: OSError", "a partition that could not be linked or read"),
    (
        "q/GLD could not be read at 2026-11-02T20:39:00+00:00: ValueError",
        "a partition that could not be linked or read",
    ),
    ("q/GLD holds a row of ticker 'SPY' at 2026-11-02T20:39:00+00:00", "a row of another ticker"),
    (
        "GLD on 2026-11-02 has bid as string, and marketlake publishes double",
        "a column of another type",
    ),
    (
        "GLD on 2026-11-02 has a fetch_end_ts 'x' that is no time",
        "a capture stamp that is no time",
    ),
    (
        "NOPE has no security master row valid on 2026-11-02, so no ticker names it",
        "no one ticker for an instrument",
    ),
    ("GLD has 2 security master rows on 2026-11-02", "no one ticker for an instrument"),
    ("marketlake:quotes has 2 quote-kind rows on 2026-11-02", "no one quote-kind row"),
]


@pytest.mark.parametrize(("fault", "reason"), READ_FAULTS)
def test_every_fault_of_the_read_is_classed(fault: str, reason: str) -> None:
    assert qr.read_reason(fault) == reason


def test_every_class_is_tested_and_an_unknown_reason_is_said() -> None:
    assert {r for _, _, r in REASONS} == {name for name, _ in qr._QUOTE_REASONS}
    assert {r for _, r in READ_FAULTS} == {name for name, _ in qr._READ_REASONS}
    assert qr.quote_reason("something new") == qr.UNCLASSED
    assert qr.read_reason("something new") == "another fault of the read"
    assert InstrumentId("GLD") == GLD
