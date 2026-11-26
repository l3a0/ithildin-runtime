"""A lookback that holds two series of one instrument halts, as #136's acceptance names.

Every bar is made up, in a lake marketlake's own import writes, apart from the
Chan vintages the last case reads. The numbers in each test's name are the
acceptance items' numbers on #136.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket
from ithildinruntime.core import (
    STRATEGIES,
    BarStatus,
    BlobRef,
    Halt,
    Mode,
    Plan,
    PlanDecision,
    Series,
    Snapshot,
    decode,
    decode_output,
    encode,
    load_strategies,
    output_key,
    result_key,
    series_of,
    sha256_of,
)
from ithildinruntime.data import DataSettings, DataStage, LakeSource, Mirror, data_settings
from ithildinruntime.data.reference import PriceKinds
from ithildinruntime.data.source import judge
from ithildinruntime.runtime.alpha_worker import alpha_handlers, alpha_rows
from ithildinruntime.runtime.data_worker import DATA_ROW
from ithildinruntime.runtime.loop import ReceiveLoop, Table
from tests.support import lake as lakes
from tests.support import made_lake as made
from tests.support.queues import Folder, SystemTimer, running
from tests.support.queues import storage as folder_storage
from tests.support.spreads import strategy
from tests.test_data_source import GLD, LIMITS, USO, Clock, Timer, close
from tests.test_storage_contract import Alarms

DAYS = made.days(date(2012, 1, 3), date(2012, 5, 31))
SEAM = date(2012, 4, 10)
AT = DAYS.index(SEAM)
LOOKBACK = 10
OLD, NEW = date(2012, 4, 10), date(2026, 11, 22)
# A day after the made-up price-kind rows were added.
LATER = date(2026, 11, 26)
GLD_WALK = made.walk(DAYS, 1, 160.0)
USO_WALK = made.walk(DAYS, 2, 35.0)
ONE_SERIES = [
    made.Made("GLD", made.ONE.format("GLD"), OLD, GLD_WALK),
    made.Made("USO", made.ONE.format("USO"), OLD, USO_WALK),
]
# The second series of USO is on eight times the scale of its first.
TWO_SERIES = [
    made.Made(ticker, source.format(ticker), as_of, closes)
    for ticker, walk, scale in (("GLD", GLD_WALK, 1), ("USO", USO_WALK, 8))
    for source, as_of, closes in (
        (made.ONE, OLD, {d: c for d, c in walk.items() if d < SEAM}),
        (made.TWO, NEW, {d: c * scale for d, c in walk.items() if d >= SEAM}),
    )
]


@pytest.fixture(scope="module")
def one_lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return made.build(tmp_path_factory.mktemp("one") / "lake", ONE_SERIES)


@pytest.fixture(scope="module")
def two_lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return made.build(tmp_path_factory.mktemp("two") / "lake", TWO_SERIES)


def copied(lake: Path, tmp_path: Path) -> Path:
    return lakes.copy_lake(lake, tmp_path / "bucket", ((DAYS[0], DAYS[-1]),))


def source(
    lake: Path, tmp_path: Path, lookback: int = LOOKBACK, kinds: PriceKinds = made.KINDS
) -> LakeSource:
    return LakeSource(
        Mirror(FolderBucket(lake), tmp_path / "mirror"),
        DataSettings((GLD, USO), lookback, {}, 0, Decimal(100), LIMITS),
        live=FolderBucket(lake.with_name("no-live")),
        clock=Clock(),
        timer=Timer(),
        kinds=kinds,
        measured=False,
    )


def both(lake: Path, tmp_path: Path, day: date) -> tuple[str, ...]:
    """The halts of a read at `day`'s close, which a judge of its stored snapshot gives too."""
    reading = source(lake, tmp_path).read(close(day))
    stored = decode(encode(reading.snapshot), Snapshot)
    assert judge(stored, (GLD, USO), measured=False) == reading.halts
    return reading.halts


def reason(one: date, two: date, before: str, after: str, instrument: str = "USO") -> str:
    return f"{instrument}'s bars for {one} and {two} are of two series, {before} and {after}"


BASE = f"adjusted from {made.ONE.format('USO')} as of 2012-04-10"
OTHER = f"adjusted from {made.ONE.format('USO')} as of 2012-04-11"

# 1. Each field alone, and where in the lookback the change sits.

CHANGED = {
    "source": (
        {"source": made.TWO.format("USO")},
        f"adjusted from {made.TWO.format('USO')} as of 2012-04-10",
    ),
    "source_as_of": ({"as_of": "2012-04-11"}, OTHER),
    "basis": ({"basis": "raw"}, f"raw from {made.ONE.format('USO')} as of 2012-04-10"),
}


def test_1_a_series_is_a_basis_a_source_and_its_date(one_lake: Path, tmp_path: Path) -> None:
    bar = source(one_lake, tmp_path).read(close(SEAM)).snapshot.bars[USO]["1d"][-1]
    assert series_of(bar) == Series("adjusted", made.ONE.format("USO"), OLD)
    assert series_of(replace(bar, basis="raw")) == ("raw", made.ONE.format("USO"), OLD)
    assert series_of(replace(bar, source=None)) == ("adjusted", None, OLD)
    assert series_of(replace(bar, source_as_of=None)) == ("adjusted", made.ONE.format("USO"), None)
    # No other field is read, such as the partition, which differs on every bar.
    assert series_of(replace(bar, close=Decimal(1), origin="x", vintage_id="y")) == series_of(bar)


@pytest.mark.parametrize("name", CHANGED)
def test_1_each_field_alone_halts(one_lake: Path, tmp_path: Path, name: str) -> None:
    changes, shown = CHANGED[name]
    lake = copied(one_lake, tmp_path)
    made.of_series(lake, "USO", SEAM, **changes)
    assert both(lake, tmp_path, SEAM) == (reason(DAYS[AT - 1], SEAM, BASE, shown),)


RAW = f"raw from {made.ONE.format('USO')} as of 2012-04-10"
CAPTURED = "raw from no source as of no date"


def test_1_a_basis_marketlake_does_not_know_halts(one_lake: Path, tmp_path: Path) -> None:
    """marketlake's refusal is the only guard on a window that is all of one unknown basis."""
    lake = copied(one_lake, tmp_path)
    made.of_series(lake, "USO", SEAM, basis="half-adjusted")
    with pytest.raises(Halt, match="half-adjusted"):
        source(lake, tmp_path).read(close(SEAM))


@pytest.mark.parametrize("traded", [False, True], ids=["adjusted", "raw"])
def test_1_a_captured_bar_between_imported_ones_halts_both_ways(
    one_lake: Path, tmp_path: Path, traded: bool
) -> None:
    """A change is a change whichever bar comes first, and where both bars are `raw`."""
    lake = copied(one_lake, tmp_path)
    window = DAYS[AT - LOOKBACK + 1 : AT + 1]
    if traded:
        for day in window:
            made.of_series(lake, "USO", day, basis="raw")
    lakes.captured(lake, "USO", window[4], fetch_ts=close(window[4]).isoformat())
    imported = RAW if traded else BASE
    assert both(lake, tmp_path, window[-1]) == (
        reason(window[3], window[4], imported, CAPTURED),
        reason(window[4], window[5], CAPTURED, imported),
    )


def test_1_an_undated_import_is_of_another_series_than_a_dated_one(
    one_lake: Path, tmp_path: Path
) -> None:
    """marketlake refuses an undated import only where its basis is `adjusted`."""
    lake = copied(one_lake, tmp_path)
    window = DAYS[AT - LOOKBACK + 1 : AT + 1]
    for day in window:
        made.of_series(lake, "USO", day, basis="raw", undated=day < window[4])
    undated = f"raw from {made.ONE.format('USO')} as of no date"
    assert both(lake, tmp_path, window[-1]) == (reason(window[3], window[4], undated, RAW),)


@pytest.mark.parametrize("at", [1, 5, LOOKBACK - 1], ids=["oldest", "middle", "newest"])
def test_1_a_change_at_any_pair_of_the_lookback_halts(
    one_lake: Path, tmp_path: Path, at: int
) -> None:
    lake = copied(one_lake, tmp_path)
    window = DAYS[AT - LOOKBACK + 1 : AT + 1]
    for day in window[at:]:
        made.of_series(lake, "USO", day, as_of="2012-04-11")
    assert both(lake, tmp_path, window[-1]) == (reason(window[at - 1], window[at], BASE, OTHER),)


def test_1_two_changes_give_two_reasons(one_lake: Path, tmp_path: Path) -> None:
    """The second goes back to the first series, so the lookback's two ends agree."""
    lake = copied(one_lake, tmp_path)
    window = DAYS[AT - LOOKBACK + 1 : AT + 1]
    for day in window[3:6]:
        made.of_series(lake, "USO", day, as_of="2012-04-11")
    assert both(lake, tmp_path, window[-1]) == (
        reason(window[2], window[3], BASE, OTHER),
        reason(window[5], window[6], OTHER, BASE),
    )


def test_1_a_change_across_a_missing_session_halts_beside_it(tmp_path: Path) -> None:
    """The bars either side of a gap are consecutive bars, so the missing session and the
    change are each a reason."""
    gap = DAYS[AT - 1 : AT + 1]
    cut = [
        m._replace(closes={d: c for d, c in m.closes.items() if d not in gap}) for m in TWO_SERIES
    ]
    reading = source(made.build(tmp_path / "lake", cut), tmp_path).read(close(DAYS[AT + 2]))
    stored = decode(encode(reading.snapshot), Snapshot)
    for reasons in (reading.halts, judge(stored, (GLD, USO), measured=False)):
        assert [r for r in reasons if "two series" in r] == list(crossing(AT - 2, AT + 1))
        assert len([r for r in reasons if " missing " in r]) == 2


def test_1_a_change_comes_after_a_contradiction_and_an_unstated_kind(
    two_lake: Path, tmp_path: Path
) -> None:
    lake = copied(two_lake, tmp_path)
    lakes.with_row(lake, "USO", SEAM, high=1.0, low=1.0)
    reading = source(lake, tmp_path, kinds=PriceKinds(())).read(close(SEAM))
    gold, oil = (
        [r for r in reading.halts if r.startswith(f"{instrument}'s")] for instrument in (GLD, USO)
    )
    assert [("unstated" in r, "two series" in r) for r in gold] == [(True, False), (False, True)]
    assert ["contradict" in r for r in oil] == [True] * (len(oil) - 2) + [False, False]
    assert len(oil) > 2 and "unstated" in oil[-2] and "two series" in oil[-1]
    assert (
        judge(decode(encode(reading.snapshot), Snapshot), (GLD, USO), measured=False)
        == reading.halts
    )


def test_1_a_change_leaves_each_status_fresh_and_the_port_refuses_it(
    two_lake: Path, tmp_path: Path
) -> None:
    src = source(two_lake, tmp_path)
    reading = src.read(close(SEAM))
    assert set(reading.snapshot.status.values()) == {BarStatus.FRESH}
    with pytest.raises(Halt) as halt:
        src.snapshot(close(SEAM))
    assert str(halt.value) == "; ".join(reading.halts)


# 2. What adds no halt.


def test_2_one_series_throughout_adds_no_halt(one_lake: Path, tmp_path: Path) -> None:
    """The two legs' series differ by their source, and neither is compared with the other."""
    assert both(one_lake, tmp_path, SEAM) == ()


def test_2_two_price_kinds_within_one_series_add_no_halt(one_lake: Path, tmp_path: Path) -> None:
    """A series is no price kind, and test_data_one_scale.py holds the window of two that reads."""
    rows = made.KINDS.rows
    other = replace(made.STATED, price_kind="last sale")
    kinds = PriceKinds(
        (*rows, *(replace(r, first_session=SEAM, kind=other, added=LATER) for r in rows))
    )
    reading = source(one_lake, tmp_path, kinds=kinds).read(close(SEAM))
    held = reading.snapshot.bars[USO]["1d"]
    assert {bar.price_kind for bar in held} == {"official close", "last sale"}
    assert reading.halts == judge(reading.snapshot, (GLD, USO), measured=False) == ()


def test_2_captured_bars_throughout_add_no_halt(tmp_path: Path) -> None:
    captured = made.days(date(2026, 11, 2), date(2026, 11, 20))
    lake = made.build(
        tmp_path / "lake",
        ONE_SERIES,
        {"GLD": made.walk(captured, 3, 300.0), "USO": made.walk(captured, 4, 70.0)},
    )
    # A captured bar lands after its close, so this session's is stale at the close.
    following = made.DAYS[made.DAYS.index(captured[-1]) + 1]
    at = made.TABLE.session(following).open - timedelta(microseconds=1)
    reading = source(lake, tmp_path).read(at)
    assert (
        reading.halts
        == judge(decode(encode(reading.snapshot), Snapshot), (GLD, USO), measured=False)
        == ()
    )
    bar = reading.snapshot.bars[USO]["1d"][-1]
    assert bar.session == captured[-1]
    assert series_of(bar) == ("raw", None, None)
    assert str(series_of(bar)) == "raw from no source as of no date"


# 3. The decisions a lookback crosses the change on.


def crossing(before: int = AT - 1, after: int = AT) -> tuple[str, ...]:
    """The reasons of both legs where these sessions' bars are either side of the change."""
    return tuple(
        reason(
            DAYS[before],
            DAYS[after],
            f"adjusted from {made.ONE.format(t)} as of 2012-04-10",
            f"adjusted from {made.TWO.format(t)} as of 2026-11-22",
            t,
        )
        for t in ("GLD", "USO")
    )


@pytest.mark.parametrize("lookback", [1, 2, LOOKBACK])
def test_3_only_the_decisions_whose_lookback_crosses_halt(
    two_lake: Path, tmp_path: Path, lookback: int
) -> None:
    """A lookback of one bar never crosses, and one of two crosses on the change alone."""
    src = source(two_lake, tmp_path, lookback)
    halts = {n: src.read(close(DAYS[AT + n])).halts for n in range(-1, lookback + 1)}
    assert halts[-1] == halts[lookback - 1] == halts[lookback] == ()
    assert [halts[n] for n in range(lookback - 1)] == [crossing()] * (lookback - 1)


def test_3_a_decision_before_the_close_crosses_one_session_later(
    two_lake: Path, tmp_path: Path
) -> None:
    """Its lookback ends at the session before, as a close−20 decision's does.

    It halts for its quotes too, since no segment is here to read.
    """
    src = source(two_lake, tmp_path)
    early = timedelta(minutes=20)
    halts = {
        n: tuple(h for h in src.read(close(DAYS[AT + n]) - early).halts if "series" in h)
        for n in (0, 1, LOOKBACK - 1, LOOKBACK)
    }
    assert halts[0] == halts[LOOKBACK] == ()
    assert halts[1] == halts[LOOKBACK - 1] == crossing()


def test_3_the_tracked_strategy_halts_at_close_20_from_2026_09_08_to_2027_01_28(
    tmp_path: Path,
) -> None:
    """GLD and USO's two changes, with the import from a made-up source, so both halt.

    docs/design.md quotes the first span. The tracked import, from schwab:pricehistory
    as of 2026-11-02, reads at the second, which tests/test_data_one_scale.py pins.
    """
    (tracked,) = load_strategies(STRATEGIES, ["chan_bollinger"])
    # The quotes are made up, so the maximum age and spread they are judged under are too.
    settings = replace(data_settings([tracked]), quotes=LIMITS)
    seams = (date(2026, 9, 4), date(2026, 11, 2))
    imported = made.days(date(2026, 5, 1), date(2026, 10, 30))
    captured = made.days(seams[1], date(2027, 1, 29))
    lake = made.build(
        tmp_path / "lake",
        [
            made.Made(t, name.format(t), as_of, {d: c for d, c in walk.items() if pick(d)})
            for t, walk in (
                ("GLD", made.walk(imported, 5, 300.0)),
                ("USO", made.walk(imported, 6, 70.0)),
            )
            for name, as_of, pick in (
                (made.ONE, date(2026, 11, 22), lambda d: d < seams[0]),
                (made.TWO, date(2026, 10, 31), lambda d: d >= seams[0]),
            )
        ],
        {"GLD": made.walk(captured, 7, 300.0), "USO": made.walk(captured, 8, 70.0)},
    )
    src = LakeSource(
        Mirror(FolderBucket(lake), tmp_path / "mirror"),
        settings,
        live=FolderBucket(lake.with_name("no-live")),
        clock=Clock(),
        timer=Timer(),
        kinds=made.KINDS,
        measured=False,
    )
    early = timedelta(minutes=20)
    crossed = {
        seam: [
            day
            for day in made.days(date(2026, 9, 1), captured[-1])
            if any(f"and {seam} are of two series" in r for r in src.read(close(day) - early).halts)
        ]
        for seam in seams
    }
    spans = [(days[0], days[-1], len(days)) for days in crossed.values()]
    assert settings.lookback == 60
    assert spans == [
        (date(2026, 9, 8), date(2026, 11, 30), 59),
        (date(2026, 11, 3), date(2027, 1, 28), 59),
    ]
    # Its quotes halt it, since no segment is here to read, and no change of series does.
    assert not any("two series" in r for r in src.read(close(captured[-1]) - early).halts)


# 4. Through the receive loop.

CHAN = strategy()
TABLE = Table(stages=(DATA_ROW, *alpha_rows([CHAN])), strategies=(CHAN,))
DEPLOYMENT, RUN, RELEASE = "ci", "2012-04-10", "sha256:release"


def test_4_the_loop_stores_the_snapshot_then_the_halt_and_alpha_passes_it_on(
    two_lake: Path, tmp_path: Path
) -> None:
    at = close(SEAM)
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    alarms = Alarms()
    storage = folder_storage(folder.store(announce=False), alarms)
    plan = Plan(1, DEPLOYMENT, RUN, Mode.PAPER, RELEASE, (PlanDecision(at, (CHAN.name,)),))
    data = DataStage(
        source(two_lake, tmp_path, 40),
        storage,
        Clock(),
        deployment=DEPLOYMENT,
        run=RUN,
        release=RELEASE,
        config_hashes=TABLE.hashes_for("data"),
    )
    alpha = alpha_handlers(
        storage=storage, deployment=DEPLOYMENT, run=RUN, release=RELEASE, table=TABLE
    )

    def loop(component: str, handlers) -> ReceiveLoop:
        return ReceiveLoop(
            worker=f"{component}-1",
            component=component,
            table=TABLE,
            plan=plan,
            queue=folder.queue(component),
            storage=storage,
            bucket=folder.bucket,
            handlers=handlers,
            alerts=alarms,
            timer=SystemTimer(),
            beat=lambda: None,
            tick=0.05,
            stop_timeout=5.0,
        )

    stages = (("data", {"data": data}, "data"), ("alpha", alpha, f"alpha/{CHAN.name}"))
    keys = [result_key(DEPLOYMENT, RUN, at, stage) for _, _, stage in stages]
    # Each loop finds its decision in the check it makes as it starts.
    for (component, handlers, _), key in zip(stages, keys, strict=True):
        with running({component: loop(component, handlers)}) as started:
            assert started.wait_for(lambda k=key: (folder.root / k).exists(), 20)
    result = storage.read_result(keys[0], at)
    assert all(r in result.halt.reason for r in crossing())
    key = output_key(DEPLOYMENT, RUN, at, "data", "snapshot")
    assert [ref.key for ref in result.outputs] == [key]
    stored = decode_output((folder.root / key).read_bytes(), Snapshot)
    assert len(stored.entries[0].bars[USO]["1d"]) == 40
    passed = storage.read_result(keys[1], at)
    assert passed.outputs == ()
    assert passed.halt.cause == BlobRef(keys[0], sha256_of((folder.root / keys[0]).read_bytes()))
    assert passed.halt.reason == f"{keys[0]} holds a halt: {result.halt.reason}"


# 5. The replication's read.


def test_5_the_replications_one_read_of_both_chan_vintages_gains_no_reason(
    every_session: Snapshot,
) -> None:
    """The fixture reads through the port, which raises on any reason, so it holds the read."""
    assert judge(every_session, (GLD, USO), measured=False) == ()
    for instrument in (GLD, USO):
        held = every_session.bars[instrument]["1d"]
        assert len(held) == 1500 and len({series_of(bar) for bar in held}) == 1
