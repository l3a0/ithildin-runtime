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
from tests.test_data_source import GLD, USO, Clock, Timer, close
from tests.test_storage_contract import Alarms

DAYS = made.days(date(2012, 1, 3), date(2012, 5, 31))
SEAM = date(2012, 4, 10)
AT = DAYS.index(SEAM)
LOOKBACK = 10
OLD, NEW = date(2012, 4, 10), date(2026, 11, 22)
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
        DataSettings((GLD, USO), lookback, {}, 0, Decimal(100)),
        clock=Clock(),
        timer=Timer(),
        kinds=kinds,
    )


def both(lake: Path, tmp_path: Path, day: date) -> tuple[str, ...]:
    """The halts of a read at `day`'s close, which a judge of its stored snapshot gives too."""
    reading = source(lake, tmp_path).read(close(day))
    stored = decode(encode(reading.snapshot), Snapshot)
    assert judge(stored, (GLD, USO)) == reading.halts
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
    assert judge(decode(encode(reading.snapshot), Snapshot), (GLD, USO)) == reading.halts


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


def test_2_captured_bars_throughout_add_no_halt(tmp_path: Path) -> None:
    captured = made.days(date(2026, 11, 2), date(2026, 11, 20))
    lake = made.build(
        tmp_path / "lake",
        ONE_SERIES,
        {"GLD": made.walk(captured, 3, 300.0), "USO": made.walk(captured, 4, 70.0)},
    )
    assert both(lake, tmp_path, captured[-1]) == ()
    bar = source(lake, tmp_path).read(close(captured[-1])).snapshot.bars[USO]["1d"][-1]
    assert series_of(bar) == ("raw", None, None)
    assert str(series_of(bar)) == "raw from no source as of no date"


# 3. The decisions a lookback crosses the change on.


def crossing() -> tuple[str, ...]:
    return tuple(
        reason(
            DAYS[AT - 1],
            SEAM,
            f"adjusted from {made.ONE.format(t)} as of 2012-04-10",
            f"adjusted from {made.TWO.format(t)} as of 2026-11-22",
            t,
        )
        for t in ("GLD", "USO")
    )


def test_3_only_the_decisions_whose_lookback_crosses_halt(two_lake: Path, tmp_path: Path) -> None:
    src = source(two_lake, tmp_path)
    halts = {n: src.read(close(DAYS[AT + n])).halts for n in range(-1, LOOKBACK)}
    assert halts[-1] == halts[LOOKBACK - 1] == ()
    assert [halts[n] for n in range(LOOKBACK - 1)] == [crossing()] * (LOOKBACK - 1)


def test_3_a_decision_before_the_close_crosses_one_session_later(
    two_lake: Path, tmp_path: Path
) -> None:
    """Its lookback ends at the session before, as a close−20 decision's does."""
    src = source(two_lake, tmp_path)
    early = timedelta(minutes=20)
    halts = {n: src.read(close(DAYS[AT + n]) - early).halts for n in (0, 1, LOOKBACK - 1, LOOKBACK)}
    assert halts[0] == halts[LOOKBACK] == ()
    assert halts[1] == halts[LOOKBACK - 1] == crossing()


def test_3_the_tracked_strategy_first_passes_the_start_of_capture_on_2027_01_29(
    tmp_path: Path,
) -> None:
    """Its lookback, read at close−20, as docs/design.md quotes the dates."""
    (tracked,) = load_strategies(STRATEGIES, ["chan_bollinger"])
    settings = data_settings([tracked])
    imported = made.days(date(2026, 7, 1), date(2026, 10, 30))
    captured = made.days(date(2026, 11, 2), date(2027, 1, 29))
    walks = {"GLD": made.walk(imported, 5, 300.0), "USO": made.walk(imported, 6, 70.0)}
    lake = made.build(
        tmp_path / "lake",
        [made.Made(t, made.ONE.format(t), date(2026, 10, 30), w) for t, w in walks.items()],
        {t: made.walk(captured, n, 100.0) for n, t in enumerate(walks)},
    )
    src = LakeSource(
        Mirror(FolderBucket(lake), tmp_path / "mirror"),
        settings,
        clock=Clock(),
        timer=Timer(),
        kinds=made.KINDS,
    )
    early = timedelta(minutes=20)
    halting = [d for d in captured if src.read(close(d) - early).halts]
    assert (halting[0], halting[-1]) == (date(2026, 11, 3), date(2027, 1, 28))
    assert len(halting) == settings.lookback - 1 == 59
    assert captured[-1] not in halting


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
    assert judge(every_session, (GLD, USO)) == ()
    for instrument in (GLD, USO):
        held = every_session.bars[instrument]["1d"]
        assert len(held) == 1500 and len({series_of(bar) for bar in held}) == 1
