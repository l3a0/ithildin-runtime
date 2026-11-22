"""Data's stage, its clocks and its entry point.

Acceptance tests 18 and 19 on #5: a reread keeps the first capture and stores
the second under its attempt number with an alarm that says what differed,
and the wait for the decision time keeps the heartbeat beating under the
wall clock and returns at once under the history clock.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket, FolderStore, HistoryClock, S3Bucket, WallClock
from ithildinruntime.core import (
    Halt,
    InstrumentId,
    Mode,
    Retry,
    Snapshot,
    StageKind,
    Storage,
    attempt_key,
    decode_output,
    output_key,
)
from ithildinruntime.data import DataStage, LakeSource, Mirror, differences, spikes
from ithildinruntime.runtime.data_worker import DATA_ROW, data_bucket, data_handler
from ithildinruntime.runtime.loop import Item, Table
from tests.support import lake as lakes
from tests.test_data_source import LAST, Clock, Timer, before, close, settings, unlist
from tests.test_storage_contract import Alarms

RUN = "2012-04-09"
DEPLOYMENT = "test"


class Reports:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


def stage(lake: Path, tmp_path: Path, clock=None, **changes) -> tuple[DataStage, Storage, Alarms]:
    alarms = Alarms()
    storage = Storage(
        FolderStore(tmp_path / "store"),
        timer=Timer(),
        pause=lambda s: None,
        retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
        alerts=alarms,
        owner_step="none",
    )
    clock = clock or Clock()
    source = LakeSource(
        Mirror(FolderBucket(lake), tmp_path / "mirror"),
        settings(**changes),
        clock=clock,
        timer=Timer(),
    )
    made = DataStage(
        source,
        storage,
        clock,
        deployment=DEPLOYMENT,
        run=RUN,
        release="r1",
        config_hash="c1",
    )
    return made, storage, alarms


def key(n: int = 1) -> str:
    if n == 1:
        return output_key(DEPLOYMENT, RUN, close(LAST), "data", "snapshot")
    return attempt_key(DEPLOYMENT, RUN, close(LAST), "data", "snapshot", n)


def test_the_stage_stores_the_snapshot_and_names_it_in_its_result(
    lake: Path, tmp_path: Path
) -> None:
    made, _, alarms = stage(lake, tmp_path)
    result = made(Item(close(LAST), "data"), Reports())
    (ref,) = result.outputs
    assert ref.key == key()
    data = (tmp_path / "store" / ref.key).read_bytes()
    stored = decode_output(data, Snapshot)
    assert stored.header.stage == "data" and stored.header.kind == "capturing"
    assert len(stored.entries[0].bars) == 2
    # The price-kind contract states Yahoo's kind for both vintages, so nothing halts.
    assert result.halt is None
    assert alarms.reasons == []


def test_a_contradicting_bar_stores_the_snapshot_with_its_flag_then_halts(
    lake: Path, tmp_path: Path
) -> None:
    lakes.with_row(lake, "GLD", LAST, high=150.0, low=149.0)
    made, _, _ = stage(lake, tmp_path)
    result = made(Item(close(LAST), "data"), Reports())
    (ref,) = result.outputs
    stored = decode_output((tmp_path / "store" / ref.key).read_bytes(), Snapshot)
    assert InstrumentId("GLD") in stored.entries[0].flags
    assert result.halt is not None and result.halt.cause == ref
    assert "contradict each other" in result.halt.reason


def test_a_move_flag_stores_the_snapshot_with_its_flag_and_halts_nothing(
    lake: Path, tmp_path: Path
) -> None:
    lakes.with_row(lake, "GLD", LAST, open=15.937, high=15.937, low=15.937, close=15.937)
    made, _, _ = stage(lake, tmp_path, spike_multiple=Decimal(10))
    result = made(Item(close(LAST), "data"), Reports())
    (ref,) = result.outputs
    stored = decode_output((tmp_path / "store" / ref.key).read_bytes(), Snapshot)
    assert [f.rule for f in stored.entries[0].flags[InstrumentId("GLD")]] == [spikes.MOVE]
    assert result.halt is None


def test_a_stale_bar_stores_the_snapshot_then_halts_naming_it(lake: Path, tmp_path: Path) -> None:
    gone = lakes.partition_key("USO", LAST)
    unlist(lake, gone)
    unlist(lake, lakes.partition_key("USO", before(LAST)))
    made, _, _ = stage(lake, tmp_path)
    result = made(Item(close(LAST), "data"), Reports())
    assert result.outputs and result.halt is not None
    assert "USO stale for the decision" in result.halt.reason


def test_a_read_that_cannot_give_a_snapshot_raises_and_stores_nothing(
    lake: Path, tmp_path: Path
) -> None:
    (lake / "quarantine.jsonl").unlink()
    made, _, _ = stage(lake, tmp_path)
    with pytest.raises(Halt, match="quarantine.jsonl is a reader file"):
        made(Item(close(LAST), "data"), Reports())
    assert not (tmp_path / "store" / key()).exists()


# 18. A reread.


class Ticking(Clock):
    """A clock whose read time moves on a second at each read, as a wall clock's does."""

    def now(self) -> datetime:
        self.at += timedelta(seconds=1)
        return self.at


def test_18_a_reread_keeps_the_first_capture_and_says_only_the_read_time_differs(
    lake: Path, tmp_path: Path
) -> None:
    made, _, alarms = stage(lake, tmp_path, clock=Ticking())
    first = made(Item(close(LAST), "data"), Reports())
    second = made(Item(close(LAST), "data"), Reports())
    assert first.outputs == second.outputs[:1]
    assert second.outputs[1].key == key(2)
    (reason,) = alarms.reasons
    assert key(2) in reason
    assert reason.endswith("Only the read time differs, as on a redelivery after a crash")


def test_18_a_reread_after_a_changed_bar_says_the_bars_differ(lake: Path, tmp_path: Path) -> None:
    made, _, alarms = stage(lake, tmp_path, clock=Ticking())
    made(Item(close(LAST), "data"), Reports())
    # A restated close inside the bar's range, so no spike flag changes. The
    # manifest records the new partition, so its hash in the ledgers changes too.
    lakes.with_row(lake, "GLD", LAST, close=159.38)
    third = made(Item(close(LAST), "data"), Reports())
    assert third.outputs[1].key == key(2)
    (reason,) = alarms.reasons
    assert reason.endswith("These differ, as on a restatement by marketlake: bars, ledgers")


def test_18_a_reread_under_the_history_clock_stores_the_same_bytes(
    lake: Path, tmp_path: Path
) -> None:
    clock = HistoryClock()
    made, _, alarms = stage(lake, tmp_path, clock=clock)
    first = made(Item(close(LAST), "data"), Reports())
    second = made(Item(close(LAST), "data"), Reports())
    assert first.outputs == second.outputs
    assert alarms.reasons == []


@pytest.mark.parametrize("name", ["status", "flags"])
def test_18_the_reread_alarm_names_a_change_in_status_or_flags(name: str) -> None:
    from ithildinruntime.core import BarStatus, BlobHeader, OutputFile, SpikeFlag, encode_output

    header = BlobHeader(1, DEPLOYMENT, RUN, "data", "capturing", close(LAST), {}, "r1", "c1")
    base = Snapshot(close(LAST), {}, {}, {}, False, {}, (), {})
    from ithildinruntime.core import InstrumentId

    gld = InstrumentId("GLD")
    if name == "status":
        changed = dataclasses.replace(base, status={gld: BarStatus.STALE})
    else:
        flag = SpikeFlag(LAST, "close", Decimal(1), Decimal(2), "a rule", "a note")
        changed = dataclasses.replace(base, flags={gld: (flag,)})

    def encoded(snap: Snapshot) -> bytes:
        return encode_output(Snapshot, OutputFile(header, (snap,), False))

    assert differences(encoded(base), encoded(changed)).endswith(f"marketlake: {name}")


# 19. The wait.


class FakeTime:
    """A timer and a sleep that advance together."""

    def __init__(self, at: datetime) -> None:
        self.at = at
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return (self.at - datetime(2000, 1, 1, tzinfo=UTC)).total_seconds()

    def after(self, seconds: float) -> object:
        raise NotImplementedError

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += timedelta(seconds=seconds)


def test_19_the_wall_clock_reports_while_it_waits_past_the_heartbeat_limit() -> None:
    start = datetime(2012, 4, 9, 13, 0, tzinfo=UTC)
    time = FakeTime(start)
    clock = WallClock(time, sleep=time.sleep, interval=5.0)
    reported: list[datetime] = []
    # The receive loop's heartbeat limit is 30 seconds, and the wait is 7 hours.
    at = start + timedelta(hours=7)
    clock.wait_until(at, lambda: reported.append(time.now()))
    assert time.now() == at
    gaps = [(b - a).total_seconds() for a, b in zip([start, *reported], reported, strict=False)]
    assert max(gaps) <= 5.0
    assert len(reported) == 7 * 3600 / 5


def test_19_the_wall_clock_returns_at_once_once_the_time_has_passed() -> None:
    time = FakeTime(datetime(2012, 4, 9, 21, 0, tzinfo=UTC))
    clock = WallClock(time, sleep=time.sleep)
    clock.wait_until(datetime(2012, 4, 9, 20, 0, tzinfo=UTC), lambda: None)
    assert time.slept == []


def test_19_the_history_clock_returns_at_once_and_reads_the_decision_time() -> None:
    clock = HistoryClock()
    reports = Reports()
    clock.wait_until(close(LAST), reports)
    assert reports.count == 0
    assert clock.now() == close(LAST)


def test_19_the_stage_hands_the_loops_report_to_the_clock(lake: Path, tmp_path: Path) -> None:
    seen: list[object] = []

    class Spy(Clock):
        def wait_until(self, at, report) -> None:
            seen.append((at, report))

    made, _, _ = stage(lake, tmp_path, clock=Spy())
    reports = Reports()
    made(Item(close(LAST), "data"), reports)
    assert seen == [(close(LAST), reports)]
    assert reports.count >= 2


def test_a_clock_waits_only_until_a_utc_time() -> None:
    with pytest.raises(ValueError):
        HistoryClock().wait_until(datetime(2012, 4, 9, 16, 0), lambda: None)


def test_neither_clock_reports_an_offset_nothing_measured() -> None:
    time = FakeTime(datetime(2012, 4, 9, tzinfo=UTC))
    for clock in (HistoryClock(), WallClock(time)):
        with pytest.raises(NotImplementedError):
            clock.offset()
        with pytest.raises(NotImplementedError):
            clock.sync_age()


# The entry point.


def test_the_entry_point_adds_datas_row_as_a_capturing_stage_with_no_inputs() -> None:
    assert DATA_ROW.name == "data"
    assert DATA_ROW.kind is StageKind.CAPTURING
    assert DATA_ROW.inputs == ()
    Table((DATA_ROW,))


def test_a_backtest_reads_a_folder_and_every_other_mode_reads_s3(tmp_path: Path) -> None:
    assert isinstance(data_bucket(Mode.BACKTEST, folder=tmp_path), FolderBucket)
    for mode in (Mode.SHADOW, Mode.PAPER, Mode.LIVE):
        assert isinstance(data_bucket(mode, client=object(), bucket="marketlake"), S3Bucket)
        with pytest.raises(ValueError):
            data_bucket(mode, folder=tmp_path)
    with pytest.raises(ValueError):
        data_bucket(Mode.BACKTEST, client=object(), bucket="marketlake")


def test_the_entry_points_handler_reads_through_the_bucket_it_is_given(
    lake: Path, tmp_path: Path
) -> None:
    made, storage, _ = stage(lake, tmp_path)
    handler = data_handler(
        bucket=FolderBucket(lake),
        mirror=tmp_path / "other-mirror",
        settings=settings(),
        storage=storage,
        clock=Clock(),
        timer=Timer(),
        deployment=DEPLOYMENT,
        run=RUN,
        release="r1",
        config_hash="c1",
    )
    result = handler(Item(close(LAST), "data"), Reports())
    assert result.outputs[0].key == key()
    assert (tmp_path / "other-mirror" / "objects").is_dir()
