"""The command that measures the stress move, on lakes of made-up bars, as #116 names."""

from __future__ import annotations

import errno
import hashlib
import io
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from math import ceil
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket
from ithildinruntime.core import (
    Bucket,
    BucketObject,
    Halt,
    StressRecord,
    load_records,
    parse_strategy,
)
from ithildinruntime.data import DataSettings, LakeSource, Mirror, spike_flags
from ithildinruntime.data.mirror import MANIFEST
from ithildinruntime.data.reference import SCHWAB_IMPORT, PriceKinds
from ithildinruntime.data.spikes import MOVE
from ithildinruntime.data.splits import SplitRecord
from ithildinruntime.runtime import stress_move
from tests.support import lake as lakes
from tests.support import made_lake as made
from tests.support import splits as recorded
from tests.test_data_source import GLD, USO, Clock, Counting, Timer, close

FIRST, SEAM, THROUGH = date(2012, 2, 1), date(2012, 4, 10), date(2012, 5, 15)
DAYS = made.days(FIRST, THROUGH)
OLD, NEW = date(2012, 4, 10), date(2026, 11, 22)
# A record of splits whose covered rows hold every session of both legs.
COVERED = recorded.all_covered(["GLD", "USO"])
PLANTED = date(2012, 3, 14)


def _uso() -> dict[date, Decimal]:
    """A quiet walk with one planted fall from 40 to 32 in two sessions, and 36 between."""
    closes = made.walk(DAYS, 2, 38.0, 0.002)
    at = DAYS.index(PLANTED)
    for n, day in enumerate(DAYS):
        if n >= at - 2:
            closes[day] = Decimal(32)
        if n < at - 2:
            closes[day] = min(max(closes[day], Decimal(39)), Decimal(41))
    closes[DAYS[at - 2]], closes[DAYS[at - 1]] = Decimal(40), Decimal(36)
    for n, day in enumerate(DAYS[at + 1 :]):
        closes[day] = Decimal(32) + Decimal(n % 3) / 10
    return closes


USO_WALK = _uso()
GLD_WALK = made.walk(DAYS, 1, 160.0, 0.004)


def _two(ticker: str, walk: dict[date, Decimal], scale: int, seam: date = SEAM) -> list[made.Made]:
    before = {d: c for d, c in walk.items() if d < seam}
    after = {d: c * scale for d, c in walk.items() if d >= seam}
    return [
        made.Made(ticker, made.ONE.format(ticker), OLD, before),
        made.Made(ticker, made.TWO.format(ticker), NEW, after),
    ]


@pytest.fixture(scope="module")
def two_lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    series = [*_two("GLD", GLD_WALK, 1), *_two("USO", USO_WALK, 8)]
    return made.build(tmp_path_factory.mktemp("two") / "marketlake", series)


@pytest.fixture
def lake(two_lake: Path, tmp_path: Path) -> Path:
    return lakes.copy_lake(two_lake, tmp_path / "the-lake", ((FIRST, THROUGH),))


STRATEGY = """\
instruments = ["USO", "GLD"]
[data]
lookback = 60
stale_lag = 1
spike_multiple = 10
[data.first_sessions]
GLD = {first}
USO = {uso}
"""


class Ran:
    """One run of the command, and what it wrote."""

    def __init__(
        self,
        tmp_path: Path,
        lake: Path,
        *args: str,
        first: date = FIRST,
        uso: date | None = None,
        records: str = "",
        kinds: PriceKinds = made.KINDS,
        strategy: str = "chan",
        splits: SplitRecord = COVERED,
    ) -> None:
        self.folder = tmp_path / "the-strategies"
        self.folder.mkdir(exist_ok=True)
        (self.folder / "chan.toml").write_text(STRATEGY.format(first=first, uso=uso or first))
        held = tmp_path / "moves.toml"
        held.write_text(records)
        out, err = io.StringIO(), io.StringIO()
        self.code = stress_move.main(
            ["--lake", str(lake), "--strategy", strategy, *args],
            kinds=kinds,
            records=held,
            strategies=self.folder,
            clock=Clock(),
            out=out,
            err=err,
            splits=splits,
        )
        self.out, self.err = out.getvalue(), err.getvalue()


ARGS = ("--through", THROUGH.isoformat(), "--seam", SEAM.isoformat())


def refused(ran: Ran, why: str, tmp_path: Path) -> None:
    assert ran.code != 0 and ran.out == ""
    assert why in ran.err and ran.err.count("\n") == 1 and ran.err.endswith("\n")
    for folder in (tmp_path, Path(__file__).parents[1], Path.home(), Path("/private/var")):
        # A folder's whole name, since a word such as `<mirror>/roots` can hold root's home.
        assert re.search(re.escape(str(folder)) + r"\b", ran.err) is None
    assert "/T/" not in ran.err and "pytest" not in ran.err


def record(text: str, tmp_path: Path) -> StressRecord:
    """What the command printed, with each `what` filled in, read back."""
    path = tmp_path / "printed.toml"
    path.write_text(text.replace('what = ""', 'what = "made up"'), encoding="utf-8")
    (read,) = load_records(path)
    return read


def up(value: Fraction) -> str:
    return f"{Decimal(ceil(value * 10**8)).scaleb(-8):f}"


def test_3_the_record_of_two_series_is_the_one_worked_by_hand(lake: Path, tmp_path: Path) -> None:
    ran = Ran(tmp_path, lake, *ARGS)
    assert (ran.code, ran.err) == (0, "")
    at, seam = DAYS.index(PLANTED), DAYS.index(SEAM)
    lines = ""
    for ticker in ("GLD", "USO"):
        for day in DAYS:
            key = lakes.partition_key(ticker, day)
            sha = hashlib.sha256((lake / key).read_bytes()).hexdigest()
            lines += f"{ticker}\t{day}\t{key}\t{sha}\n"
    left = "".join(
        f'    {{ instrument = "{t}", sessions = {b - a}, moved_from = {DAYS[a]}, '
        f"moved_to = {DAYS[b]} }},\n"
        for t in ("GLD", "USO")
        for a, b in ((seam - 1, seam), (seam - 2, seam), (seam - 1, seam + 1))
    )
    series = "".join(
        f'    {{ instrument = "{t}", first = {a}, last = {b}, basis = "adjusted", '
        f'source = "{s.format(t)}", source_as_of = {as_of} }},\n'
        for t in ("GLD", "USO")
        for s, as_of, a, b in (
            (made.ONE, OLD, FIRST, DAYS[seam - 1]),
            (made.TWO, NEW, SEAM, THROUGH),
        )
    )
    manifest = hashlib.sha256((lake / "manifest.jsonl").read_bytes()).hexdigest()
    assert (
        ran.out
        == f"""\
[[record]]
strategy = "chan"
instruments = ["GLD", "USO"]
vintage = "{hashlib.sha256(lines.encode()).hexdigest()}"
through = 2012-05-15
stress_move = "0.20000000"
leg = "USO"
moved_from = {DAYS[at - 2]}
moved_to = {PLANTED}
one_session = "{up(Fraction(4, 36))}"
one_session_leg = "USO"
one_session_on = {PLANTED}
series = [
{series}]
left_out = [
{left}]
flagged = []
manifest = "{manifest}"
measured_on = 2026-11-22
splits = "{recorded.SHA}"
split_rows = []
"""
    )


def test_3_two_runs_print_the_same_bytes(lake: Path, tmp_path: Path) -> None:
    assert Ran(tmp_path, lake, *ARGS).out == Ran(tmp_path, lake, *ARGS).out


def test_4_what_it_prints_reads_back_equal(lake: Path, tmp_path: Path) -> None:
    ran = Ran(tmp_path, lake, *ARGS)
    made_record = record(ran.out, tmp_path)
    assert made_record.stress_move == Decimal("0.2") and made_record.instruments == (GLD, USO)
    assert [(s.instrument, s.first) for s in made_record.series] == [
        (GLD, FIRST),
        (GLD, SEAM),
        (USO, FIRST),
        (USO, SEAM),
    ]
    again = "\n".join(
        line for line in stress_move._toml(made_record).replace("made up", "").splitlines()
    )
    assert again + "\n" == ran.out


AFTER = DAYS[DAYS.index(PLANTED) + 1]


@pytest.mark.parametrize(
    ("first", "uso", "named"),
    [
        (FIRST, AFTER, "USO"),
        (AFTER, FIRST, "GLD"),
        (AFTER, AFTER, "GLD"),
        (DAYS[1], DAYS[1], "GLD"),
        (FIRST, DAYS[1], "USO"),
        (FIRST, SEAM, "USO"),
        (SEAM, SEAM, "GLD"),
        (FIRST, DAYS[DAYS.index(SEAM) + 1], "USO"),
    ],
    ids=["one", "the-other", "both", "both-by-one", "one-by-one", "seam", "both-seam", "past-seam"],
)
def test_3_a_start_later_than_the_lakes_own_is_refused(
    lake: Path, tmp_path: Path, first: date, uso: date, named: str
) -> None:
    args = ARGS if min(first, uso) < SEAM else ARGS[:2]
    written = first if named == "GLD" else uso
    refused(
        Ran(tmp_path, lake, *args, first=first, uso=uso),
        f"{named}'s first session is written as {written}",
        tmp_path,
    )


@pytest.fixture(scope="module")
def young_lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """GLD in one series from the first day, and USO from past the seam of nothing but itself."""
    series = [
        *_two("GLD", GLD_WALK, 1),
        made.Made(
            "USO", made.TWO.format("USO"), NEW, {d: c for d, c in USO_WALK.items() if d >= YOUNG}
        ),
    ]
    return made.build(tmp_path_factory.mktemp("young") / "marketlake", series)


YOUNG = DAYS[DAYS.index(SEAM) + 4]


def test_3_a_leg_that_begins_after_a_seam_is_measured_from_its_start_and_cuts_no_other(
    young_lake: Path, tmp_path: Path
) -> None:
    ran = Ran(tmp_path, young_lake, *ARGS, uso=YOUNG)
    assert ran.err == ""
    made_record = record(ran.out, tmp_path)
    assert [(s.instrument, s.first) for s in made_record.series] == [
        (GLD, FIRST),
        (GLD, SEAM),
        (USO, YOUNG),
    ]
    assert {m.instrument for m in made_record.left_out} == {GLD}
    closes = list(GLD_WALK.values())
    seam = DAYS.index(SEAM)
    largest = max(
        (Fraction(abs(b - a)) / Fraction(a), n)
        for n, (a, b) in enumerate(zip(closes, closes[2:], strict=False))
        if not n < seam <= n + 2
    )
    young = [c * 1 for d, c in USO_WALK.items() if d >= YOUNG]
    own = max(Fraction(abs(b - a)) / Fraction(a) for a, b in zip(young, young[2:], strict=False))
    assert f"{made_record.stress_move:f}" == up(max(largest[0], own))
    # A leg with no bar by the last session is refused.
    before = Ran(tmp_path, young_lake, "--through", str(SEAM), "--seam", str(SEAM), uso=YOUNG)
    refused(before, "USO has no bar, so its moves cannot be measured", tmp_path)


def test_3_a_leg_that_begins_on_the_last_session_before_a_seam_keeps_that_bar(
    tmp_path: Path,
) -> None:
    eve = DAYS[DAYS.index(SEAM) - 1]
    late = {d: c for d, c in USO_WALK.items() if d >= eve}
    series = [*_two("GLD", GLD_WALK, 1), *_two("USO", late, 8)]
    root = made.build(tmp_path / "the-lake", series)
    ran = Ran(tmp_path, root, *ARGS, uso=eve)
    assert ran.err == ""
    made_record = record(ran.out, tmp_path)
    assert [(s.first, s.last) for s in made_record.series if s.instrument == USO] == [
        (eve, eve),
        (SEAM, THROUGH),
    ]
    # Its bar of that session is in the hash, and each move from it is left out.
    assert [(m.sessions, m.moved_from) for m in made_record.left_out if m.instrument == USO] == [
        (1, eve),
        (2, eve),
    ]


def test_3_another_day_changes_the_manifest_and_the_date_alone(lake: Path, tmp_path: Path) -> None:
    one = Ran(tmp_path, lake, *ARGS).out.splitlines()
    lakes.record(lake, lakes.QUARANTINE)
    two = Ran(tmp_path, lake, *ARGS).out.splitlines()
    assert [a.split(" = ")[0] for a, b in zip(one, two, strict=True) if a != b] == ["manifest"]


def test_3_one_refusals_whole_line(lake: Path, tmp_path: Path) -> None:
    manifest = lake / "manifest.jsonl"
    manifest.unlink()
    manifest.symlink_to(manifest)
    ran = Ran(tmp_path, lake, *ARGS)
    assert ran.out == "" and ran.err == (
        "stress_move: manifest.jsonl could not be downloaded: StoreUnavailable: manifest.jsonl: "
        "OSError: [Errno 62] Too many levels of symbolic links: '<lake>/manifest.jsonl'\n"
    ).replace("Errno 62", f"Errno {errno.ELOOP}")


def test_3_there_is_no_option_for_the_start(lake: Path, tmp_path: Path) -> None:
    refused(Ran(tmp_path, lake, *ARGS, "--start", "2012-03-01"), "unrecognized", tmp_path)


SATURDAY = "2012-04-14"
PLACED = {
    "a-through-on-no-session": (("--through", SATURDAY), "--through is 2012-04-14"),
    "a-through-before-the-start": (("--through", "2012-01-31"), "not after the start"),
    "a-through-at-the-start": (("--through", "2012-02-01"), "not after the start"),
    "a-through-of-no-date": (("--through", "soon"), "'soon' is no date"),
    "a-through-of-another-form": (("--through", "20120515"), "'20120515' is no date"),
    "no-through": ((), "--through"),
    "a-through-the-table-ends-at": (
        ("--through", str(made.DAYS[-1]), "--seam", SEAM.isoformat()),
        f"the session table holds no session after {made.DAYS[-1]}",
    ),
    "a-through-past-the-table": (("--through", "2099-01-02"), "outside the session table"),
    **{
        name: (("--through", "2012-05-15", *(a for s in seams for a in ("--seam", s))), why)
        for name, seams, why in (
            ("a-seam-on-no-session", (SATURDAY,), "--seam is 2012-04-14"),
            ("a-seam-repeated", ("2012-04-10", "2012-04-10"), "not after 2012-04-10"),
            ("a-seam-out-of-order", ("2012-04-10", "2012-04-09"), "not after 2012-04-10"),
            ("a-seam-at-the-start", ("2012-02-01",), "not after 2012-02-01"),
            ("a-seam-before-the-start", ("2012-01-31",), "not after 2012-02-01"),
            ("a-seam-after-the-through", ("2012-04-10", "2012-05-16"), "at or before 2012-05-15"),
        )
    },
}


@pytest.mark.parametrize("name", PLACED)
def test_3_an_argument_out_of_place_is_refused(lake: Path, tmp_path: Path, name: str) -> None:
    args, why = PLACED[name]
    refused(Ran(tmp_path, lake, *args), why, tmp_path)


def test_3_a_seam_at_the_last_session_is_read(lake: Path, tmp_path: Path) -> None:
    ran = Ran(tmp_path, lake, "--through", SEAM.isoformat(), "--seam", SEAM.isoformat())
    assert ran.err == ""
    assert record(ran.out, tmp_path).series[-1].last == SEAM


def test_3_a_start_that_is_no_session_is_refused(lake: Path, tmp_path: Path) -> None:
    refused(
        Ran(tmp_path, lake, *ARGS, first=date(2012, 2, 4)), "GLD's start is 2012-02-04", tmp_path
    )


def test_3_a_start_on_the_calendars_first_session_is_refused(lake: Path, tmp_path: Path) -> None:
    """Data can list no partition before it, so nothing could say that it is late."""
    first = made.DAYS[0]
    refused(
        Ran(tmp_path, lake, *ARGS, uso=first),
        f"USO's start is {first}, the session table's first",
        tmp_path,
    )
    # The session after it is asked for, and data says what it lacks there.
    refused(Ran(tmp_path, lake, *ARGS, uso=made.DAYS[1]), "no security master row", tmp_path)


def test_3_a_leg_that_starts_after_the_last_session_is_refused(lake: Path, tmp_path: Path) -> None:
    after = made.days(THROUGH, date(2012, 5, 31))[1]
    refused(Ran(tmp_path, lake, *ARGS, uso=after), "cuts the decision's lookback", tmp_path)


def test_3_a_seam_nobody_named_is_refused(lake: Path, tmp_path: Path) -> None:
    ran = Ran(tmp_path, lake, "--through", THROUGH.isoformat())
    refused(ran, "are of two series", tmp_path)


def test_3_the_change_data_passes_is_refused_until_its_seam_is_named(tmp_path: Path) -> None:
    """Data reads Schwab's import and the captured bars after it as one scale, #175's pair.

    The command still asks for that seam, and leaves out every move across it.
    """
    seam = date(2026, 11, 2)
    days = made.days(date(2026, 9, 4), date(2026, 11, 30))
    walks = {"GLD": made.walk(days, 1, 300.0), "USO": made.walk(days, 2, 70.0)}
    series = [
        made.Made(t, SCHWAB_IMPORT, seam, {d: c for d, c in w.items() if d < seam})
        for t, w in walks.items()
    ]
    captured = {t: {d: c for d, c in w.items() if d >= seam} for t, w in walks.items()}
    root = made.build(tmp_path / "the-lake", series, captured)
    kinds = made.kinds(SCHWAB_IMPORT)
    through = ("--through", str(days[-1]))
    refused(
        Ran(tmp_path, root, *through, first=days[0], kinds=kinds),
        f"GLD's bars for 2026-10-30 and {seam} are of two series, adjusted from "
        f"{SCHWAB_IMPORT} as of {seam} and raw from no source as of no date, at a seam "
        "nobody named",
        tmp_path,
    )
    ran = Ran(tmp_path, root, *through, "--seam", str(seam), first=days[0], kinds=kinds)
    assert ran.err == ""
    left = record(ran.out, tmp_path).left_out
    assert {m.instrument for m in left} == {GLD, USO}
    assert all(m.moved_from < seam <= m.moved_to for m in left)


def test_3_a_named_seam_at_which_no_leg_changes_is_refused(lake: Path, tmp_path: Path) -> None:
    ran = Ran(tmp_path, lake, *ARGS[:2], "--seam", "2012-03-01", *ARGS[2:])
    refused(ran, "no leg changes series at the seam 2012-03-01", tmp_path)


def test_3_a_seam_one_leg_changes_at_leaves_nothing_out_on_the_other(tmp_path: Path) -> None:
    series = [made.Made("GLD", made.ONE.format("GLD"), OLD, GLD_WALK), *_two("USO", USO_WALK, 8)]
    root = made.build(tmp_path / "the-lake", series)
    ran = Ran(tmp_path, root, *ARGS)
    assert ran.err == ""
    assert {m.instrument for m in record(ran.out, tmp_path).left_out} == {USO}


def test_3_an_unstated_kind_and_a_missing_session_are_refused(lake: Path, tmp_path: Path) -> None:
    refused(Ran(tmp_path, lake, *ARGS, kinds=made.kinds()), "has unstated price kind", tmp_path)
    refused(Ran(tmp_path, lake, *ARGS, first=date(2012, 1, 31)), "no bar from 2012-01-31", tmp_path)


def test_3_a_bar_behind_at_the_last_session_is_refused_whatever_lag_the_file_writes(
    lake: Path, tmp_path: Path
) -> None:
    """The file writes a lag of 1, under which the record would name a session nobody read."""
    (lake / lakes.partition_key("USO", THROUGH)).unlink()
    manifest = lake / "manifest.jsonl"
    key = lakes.partition_key("USO", THROUGH)
    lines = manifest.read_text().splitlines(keepends=True)
    manifest.write_text("".join(line for line in lines if f'"{key}"' not in line))
    refused(Ran(tmp_path, lake, *ARGS), "USO stale for the decision", tmp_path)


class Moving(Counting):
    """A bucket whose manifest gains a line before its read of it numbered `before`."""

    def __init__(self, lake: Path, before: int) -> None:
        super().__init__(FolderBucket(lake))
        self._lake, self._before = lake, before

    def read(self, key: str) -> BucketObject:
        if key == MANIFEST and self.reads.count(key) == self._before - 1:
            lakes.record(self._lake, lakes.QUARANTINE)
        return super().read(key)


def measured(bucket: Bucket, tmp_path: Path) -> str:
    return stress_move.measure(
        bucket,
        tmp_path / "mirror",
        parse_strategy("chan", STRATEGY.format(first=FIRST, uso=FIRST).encode()),
        THROUGH,
        [SEAM],
        made.KINDS,
        (),
        clock=Clock(),
        timer=Timer(),
        splits=COVERED,
    )


@pytest.mark.parametrize("before", [2, 3, 4], ids=["the-starts", "a-span", "the-spans"])
def test_3_two_reads_whose_manifests_differ_are_refused(
    lake: Path, tmp_path: Path, before: int
) -> None:
    """Two reads at the starts and then one of each span, so four of the manifest."""
    with pytest.raises(Halt, match="^manifest.jsonl changed between two reads"):
        measured(Moving(lake, before), tmp_path)


def test_3_a_manifest_that_moves_before_the_first_read_or_after_the_last_refuses_nothing(
    lake: Path, tmp_path: Path
) -> None:
    bucket = Counting(FolderBucket(lake))
    assert measured(bucket, tmp_path) == Ran(tmp_path, lake, *ARGS).out
    assert bucket.reads.count(MANIFEST) == 4
    for before in (1, 5):
        assert measured(Moving(lake, before), tmp_path / str(before))


def _unreadable(lake: Path) -> None:
    """A partition that is a link to itself, which no read opens."""
    path = lake / lakes.partition_key("USO", DAYS[3])
    path.unlink()
    path.symlink_to(path)


def _torn(lake: Path) -> None:
    with open(lake / "manifest.jsonl", "a") as handle:
        handle.write('{"partition": "bars/')


def _torn_line(lake: Path) -> None:
    manifest = lake / "manifest.jsonl"
    data = manifest.read_text()
    manifest.write_text(data[:200] + "\n" + data)


def _corrupt(lake: Path) -> None:
    key = lakes.partition_key("USO", DAYS[3])
    lakes.rewrite(lake, key, b"PAR1 not a parquet file PAR1")
    lakes.record(lake, key, rows=1)


def _no_ledger(lake: Path) -> None:
    (lake / lakes.QUARANTINE).unlink()


BAD_KEY = lakes.partition_key("USO", DAYS[3])
BROKEN: dict[str, tuple[Callable[[Path], None], str]] = {
    "unreadable": (_unreadable, f"Too many levels of symbolic links: '<lake>/{BAD_KEY}'"),
    "torn": (_torn, "manifest.jsonl does not end in a newline"),
    "torn-inside": (_torn_line, "manifest.jsonl cannot be read: TornLedger: <mirror>/roots/"),
    "corrupt": (_corrupt, "Could not open Parquet input source '<mirror>/roots/"),
    "no-ledger": (_no_ledger, "quarantine.jsonl is a reader file the bucket does not hold"),
}


@pytest.mark.parametrize("name", BROKEN)
def test_3_a_broken_lake_is_refused_on_one_line_naming_no_folder(
    lake: Path, tmp_path: Path, name: str
) -> None:
    broke, why = BROKEN[name]
    broke(lake)
    refused(Ran(tmp_path, lake, *ARGS), why, tmp_path)


def test_3_the_mirror_is_removed_after_a_run_and_after_a_refusal(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = tmp_path / "held"
    held.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(held))
    assert Ran(tmp_path, lake, *ARGS).code == 0 and list(held.iterdir()) == []
    _corrupt(lake)
    assert Ran(tmp_path, lake, *ARGS).code == 1 and list(held.iterdir()) == []


def test_3_a_lake_that_is_a_link_to_itself_names_no_folder(tmp_path: Path) -> None:
    loop = tmp_path / "My Lakes" / "loop"
    loop.parent.mkdir()
    loop.symlink_to(loop)
    ran = Ran(tmp_path, loop, *ARGS)
    refused(ran, "<lake>", tmp_path)
    assert "Lakes" not in ran.err


def test_3_a_record_stdout_cannot_hold_is_refused_on_one_line(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = io.BytesIO()
    monkeypatch.setattr(stress_move, "_toml", lambda record: "é\n")
    out, err = io.TextIOWrapper(held, encoding="ascii"), io.StringIO()
    Ran(tmp_path, lake, *ARGS)
    code = stress_move.main(
        ["--lake", str(lake), "--strategy", "chan", *ARGS],
        kinds=made.KINDS,
        records=tmp_path / "moves.toml",
        strategies=tmp_path / "the-strategies",
        out=out,
        err=err,
    )
    out.flush()
    assert (code, held.getvalue()) == (1, b"")
    assert err.getvalue().startswith("stress_move: UnicodeEncodeError")
    assert err.getvalue().count("\n") == 1


@pytest.mark.parametrize("zone", ["AAA12", "BBB-14"])
def test_3_a_run_by_hand_dates_the_record_by_the_machines_clock_in_utc(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request, zone: str
) -> None:
    """The entry point's own clock. At any instant one of the two zones, 12 hours behind
    UTC and 14 ahead, is on another date."""
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    request.addfinalizer(time.tzset)
    request.addfinalizer(monkeypatch.undo)
    Ran(tmp_path, lake, *ARGS)
    out = io.StringIO()
    before = datetime.now(UTC).date()
    code = stress_move.main(
        ["--lake", str(lake), "--strategy", "chan", *ARGS],
        kinds=made.KINDS,
        records=tmp_path / "moves.toml",
        strategies=tmp_path / "the-strategies",
        out=out,
    )
    assert code == 0
    measured = record(out.getvalue(), tmp_path).measured_on
    assert before <= measured <= datetime.now(UTC).date()


def test_3_the_command_exits_nonzero_on_a_refusal(tmp_path: Path) -> None:
    """As what-a-person-runs.md gives it, on a folder that holds no lake."""
    ran = subprocess.run(
        [
            *(sys.executable, "-m", "ithildinruntime.runtime.stress_move"),
            *("--lake", str(tmp_path), "--strategy", "chan_bollinger", "--through", "2012-04-09"),
        ],
        capture_output=True,
        text=True,
    )
    assert (ran.returncode, ran.stdout) == (1, "")
    assert ran.stderr == "stress_move: manifest.jsonl is not in the bucket\n"


def test_3_a_file_nobody_may_read_is_refused(lake: Path, tmp_path: Path) -> None:
    path = lake / "manifest.jsonl"
    path.chmod(0)
    try:
        if os.geteuid() != 0:
            refused(Ran(tmp_path, lake, *ARGS), "PermissionError", tmp_path)
    finally:
        path.chmod(0o644)


def test_3_an_absent_strategy_file_lake_or_record_file_is_refused(
    lake: Path, tmp_path: Path
) -> None:
    refused(Ran(tmp_path, lake, *ARGS, strategy="nobody"), "strategy nobody has no", tmp_path)
    refused(Ran(tmp_path, tmp_path / "elsewhere", *ARGS), "manifest.jsonl", tmp_path)
    refused(Ran(tmp_path, lake, *ARGS, records="x = 1\n"), "moves.toml holds 'x'", tmp_path)


def test_3_any_other_exception_is_refused(
    lake: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> None:
        raise RuntimeError(f"two\nlines in {lake / 'x'} and /opt/lib/a.py, or 1/2")

    monkeypatch.setattr(stress_move, "largest_move", fail)
    refused(
        Ran(tmp_path, lake, *ARGS),
        "RuntimeError: two lines in <lake>/x and <path>, or 1/2\n",
        tmp_path,
    )


# Against the records already kept.


def printed(lake: Path, tmp_path: Path) -> str:
    return Ran(tmp_path, lake, *ARGS).out.replace('what = ""', 'what = "made up"')


TIED = DAYS[DAYS.index(PLANTED) + 10]


def _spike(*, tie: bool = False, after: int = 41) -> dict[date, Decimal]:
    """A quiet walk near 40 with one session's rise to 52 that the sessions around it offset.

    Over one session it rises 30%, from 40 to 52. No two sessions move as far, because
    the session before it rose from 46 and the one after falls to `after`. With `tie`, it
    later rises 30% over two sessions too, from 40 through 46 to 52 at `TIED`.
    """
    closes = {day: min(max(c, Decimal(39)), Decimal(41)) for day, c in USO_WALK.items()}
    at = DAYS.index(PLANTED)
    for n, price in ((-2, 46), (-1, 40), (0, 52), (1, after), (2, after)):
        closes[DAYS[at + n]] = Decimal(price)
    if tie:
        at = DAYS.index(TIED)
        for n, price in ((-3, 40), (-2, 40), (-1, 46), (0, 52), (1, 52), (2, 52), (3, 52)):
            closes[DAYS[at + n]] = Decimal(price)
    return closes


def test_3_a_one_session_move_larger_than_every_two_session_one_sets_m(tmp_path: Path) -> None:
    """m is the larger of the two lengths, and the record names the session that set it."""
    walks = (("GLD", GLD_WALK), ("USO", _spike()))
    series = [made.Made(t, made.ONE.format(t), OLD, w) for t, w in walks]
    root = made.build(tmp_path / "the-lake", series)
    ran = Ran(tmp_path, root, "--through", THROUGH.isoformat())
    assert (ran.code, ran.err) == (0, "")
    made_record = record(ran.out, tmp_path)
    before = DAYS[DAYS.index(PLANTED) - 1]
    assert (made_record.stress_move, made_record.leg) == (Decimal("0.3"), USO)
    assert (made_record.moved_from, made_record.moved_to) == (before, PLANTED)
    assert (made_record.one_session, made_record.one_session_on) == (Decimal("0.3"), PLANTED)
    # A run beside its own record counts the one-session move that set it, and says nothing.
    filled = ran.out.replace('what = ""', 'what = "made up"')
    again = Ran(tmp_path, root, "--through", THROUGH.isoformat(), records=filled)
    assert again.out == ran.out


def test_3_the_leg_that_set_m_over_one_session_is_named_over_anothers_two_session_move(
    tmp_path: Path,
) -> None:
    """GLD's one-session rise of 30% sets m, though USO's 20% fall over two sessions is the larger.

    GLD falls back only to 44 after it, so none of GLD's two-session moves passes 16%.
    """
    walks = (("GLD", _spike(after=44)), ("USO", USO_WALK))
    series = [made.Made(t, made.ONE.format(t), OLD, w) for t, w in walks]
    ran = Ran(tmp_path, made.build(tmp_path / "the-lake", series), "--through", str(THROUGH))
    made_record = record(ran.out, tmp_path)
    before = DAYS[DAYS.index(PLANTED) - 1]
    assert (made_record.stress_move, made_record.leg) == (Decimal("0.3"), GLD)
    assert (made_record.moved_from, made_record.moved_to) == (before, PLANTED)
    assert made_record.one_session_leg == GLD


def test_3_a_one_session_move_that_ties_a_two_session_one_names_the_two(tmp_path: Path) -> None:
    walks = (("GLD", GLD_WALK), ("USO", _spike(tie=True)))
    series = [made.Made(t, made.ONE.format(t), OLD, w) for t, w in walks]
    ran = Ran(tmp_path, made.build(tmp_path / "the-lake", series), "--through", str(THROUGH))
    made_record = record(ran.out, tmp_path)
    assert made_record.stress_move == made_record.one_session == Decimal("0.3")
    assert (made_record.moved_from, made_record.moved_to) == (DAYS[DAYS.index(TIED) - 2], TIED)
    assert made_record.one_session_on == PLANTED


def test_3_a_record_below_any_of_the_strategys_says_so_above_it(lake: Path, tmp_path: Path) -> None:
    earlier = printed(lake, tmp_path).replace('"0.20000000"', '"0.20000001"')
    plain = Ran(tmp_path, lake, *ARGS).out
    vintage = record(plain, tmp_path).vintage
    withdrawn = earlier + 'withdrawn = "x"\n'
    narrower = "".join(
        line
        for line in earlier.replace('["GLD", "USO"]', '["USO"]').splitlines(keepends=True)
        if '{ instrument = "GLD"' not in line or "basis" not in line
    )
    for other in (earlier, withdrawn, narrower):
        ran = Ran(tmp_path, lake, *ARGS, records=other)
        which = "the withdrawn record" if other is withdrawn else "the record"
        assert ran.code == 0 and ran.out == (
            f"# {which} of vintage {vintage}, with a stress_move of 0.20000001, is larger\n{plain}"
        )
    # One equal to it and another strategy's say nothing.
    same = earlier.replace("0.20000001", "0.20000000")
    for other in (same, earlier.replace('"chan"', '"other"')):
        assert Ran(tmp_path, lake, *ARGS, records=other).out == plain


def test_3_a_record_that_leaves_an_earlier_ones_move_out_says_so(
    lake: Path, tmp_path: Path
) -> None:
    """A correction under a new source puts a seam inside the move, or the run ends before it."""
    earlier = printed(lake, tmp_path)
    before = Ran(tmp_path, lake, "--through", "2012-03-12", records=earlier)
    for day in DAYS[DAYS.index(PLANTED) :]:
        if day < SEAM:
            made.of_series(lake, "USO", day, as_of="2012-04-11")
    ran = Ran(tmp_path, lake, *ARGS[:2], "--seam", str(PLANTED), *ARGS[2:], records=earlier)
    vintage = record(earlier, tmp_path).vintage
    for one in (ran, before):
        assert one.code == 0
        # One line for the record, which is larger too.
        said, rest = one.out.split("\n", 1)
        assert said == (
            f"# the record of vintage {vintage}, with a stress_move of 0.20000000, is larger and "
            "was set by USO's move from 2012-03-12 to 2012-03-14, which this run did not count"
        )
        assert rest.startswith("[[record]]\n")


# A split inside captured bars.


def test_3_a_split_in_captured_bars_is_listed_and_a_read_across_the_seam_is_blind_to_it(
    tmp_path: Path,
) -> None:
    first, seam = date(2026, 8, 3), date(2026, 11, 2)
    days = made.days(first, date(2026, 11, 20))
    split = days[days.index(seam) + 8]
    walks = {"GLD": made.walk(days, 1, 300.0), "USO": made.walk(days, 2, 70.0)}
    # A rise of 15% in GLD before the seam, which the move rule flags as well.
    rose = days[days.index(seam) - 9]
    walks["GLD"] = {
        d: (c * Decimal("1.15")).quantize(Decimal("0.01")) if d >= rose else c
        for d, c in walks["GLD"].items()
    }
    captured = {
        t: {
            d: (c * 8 / (2 if t == "USO" and d >= split else 1)).quantize(Decimal("0.01"))
            for d, c in w.items()
            if d >= seam
        }
        for t, w in walks.items()
    }
    series = [
        made.Made(t, made.ONE.format(t), NEW, {d: c for d, c in w.items() if d < seam})
        for t, w in walks.items()
    ]
    root = made.build(tmp_path / "the-lake", series, captured)
    ran = Ran(tmp_path, root, "--through", str(days[-1]), "--seam", str(seam), first=first)
    assert ran.err == ""
    made_record = record(ran.out, tmp_path)
    assert [(f.instrument, f.session, f.field, f.rule) for f in made_record.flagged] == [
        (GLD, rose, "close", MOVE),
        (USO, split, "close", MOVE),
    ]
    assert ran.out.count('what = ""') == 2
    assert (made_record.leg, made_record.stress_move > Decimal("0.4")) == (USO, True)
    assert made_record.series[-1].series == ("raw", None, None)
    assert "source" not in ran.out.split("series = [")[1].splitlines()[2]
    whole = LakeSource(
        Mirror(FolderBucket(root), tmp_path / "mirror"),
        DataSettings((GLD, USO), len(days), {}, 0, Decimal(10), {}),
        live=FolderBucket(root.with_name("no-live")),
        clock=Clock(),
        timer=Timer(),
        measured=False,
        kinds=made.KINDS,
        splits=COVERED,
    ).read(close(days[-1]))
    flagged = spike_flags(whole.snapshot.bars[USO]["1d"], Decimal(10))
    assert split not in [f.session for f in flagged] and seam in [f.session for f in flagged]


# 13 of #189. The record of splits a stress record over captured bars rests on.


def _captured_lake(tmp_path: Path) -> tuple[Path, list[date], date]:
    """Schwab's import to 2026-10-30, then captured bars of GLD and USO to 2026-11-30."""
    seam = date(2026, 11, 2)
    days = made.days(date(2026, 9, 4), date(2026, 11, 30))
    walks = {"GLD": made.walk(days, 1, 300.0), "USO": made.walk(days, 2, 70.0)}
    series = [
        made.Made(t, SCHWAB_IMPORT, seam, {d: c for d, c in w.items() if d < seam})
        for t, w in walks.items()
    ]
    captured = {t: {d: c for d, c in w.items() if d >= seam} for t, w in walks.items()}
    return made.build(tmp_path / "the-lake", series, captured), days, seam


def test_13_a_pair_of_captured_bars_past_the_last_covered_row_is_refused(tmp_path: Path) -> None:
    """Even where a sealed day's quote covers it, as it would for a decision."""
    from tests.support.sealed import close_row, partition

    root, days, seam = _captured_lake(tmp_path)
    table = made.TABLE
    for ticker in ("GLD", "USO"):
        stored = {
            d: float(made.pq.read_table(root / lakes.partition_key(ticker, d))["close"][0].as_py())
            for d in days
            if d >= seam
        }
        for day in [d for d in days if d > date(2026, 11, 19)]:
            prior = stored[table.before(day).day]
            row = {**close_row(ticker, table.session(day).close), "close_price": prior}
            partition(root, ticker, row, day=day)

    short = recorded.record(
        recorded.covered("GLD", days[0], date(2026, 11, 19), line=1),
        recorded.covered("USO", days[0], date(2026, 11, 19), line=2),
    )
    ran = Ran(
        tmp_path,
        root,
        "--through",
        str(days[-1]),
        "--seam",
        str(seam),
        first=days[0],
        kinds=made.kinds(SCHWAB_IMPORT),
        splits=short,
    )
    refused(ran, "2026-11-19 and 2026-11-20", tmp_path)
    assert "no covered row" in ran.err


def test_13_a_split_row_inside_the_span_on_raw_bars_is_refused(tmp_path: Path) -> None:
    root, days, seam = _captured_lake(tmp_path)
    inside = recorded.record(*COVERED.rows, recorded.split("USO", date(2026, 11, 16), 2, 1, line=9))
    ran = Ran(
        tmp_path,
        root,
        "--through",
        str(days[-1]),
        "--seam",
        str(seam),
        first=days[0],
        kinds=made.kinds(SCHWAB_IMPORT),
        splits=inside,
    )
    said = "USO has a recorded split of 2 new shares for 1 old from 2026-11-16, at data/splits.csv"
    refused(ran, f"{said} line 9", tmp_path)


def test_13_a_record_names_the_files_sha256_and_each_split_row_its_legs_hold(
    lake: Path, tmp_path: Path
) -> None:
    """A split row before the span, which the adjusted series already hold, refuses nothing."""
    held = recorded.record(*COVERED.rows, recorded.split("USO", date(2011, 5, 2), 1, 8, line=3))
    ran = Ran(tmp_path, lake, *ARGS, splits=held)
    assert ran.err == ""
    made_record = record(ran.out, tmp_path)
    assert made_record.splits == recorded.SHA
    assert [
        (r.instrument, r.first_session, r.new_shares, r.old_shares) for r in made_record.split_rows
    ] == [(USO, date(2011, 5, 2), 1, 8)]


def test_13_chan_bollingers_record_measured_before_the_file_existed_still_reads() -> None:
    from ithildinruntime.core import STRESS_MOVES

    (tracked,) = load_records(STRESS_MOVES)
    assert (tracked.splits, tracked.split_rows) == (None, ())
