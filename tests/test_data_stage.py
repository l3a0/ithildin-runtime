"""Data's stage, its clocks and its entry point.

Acceptance tests 18 and 19 on #5: a reread keeps the first capture and stores
the second under its attempt number with an alarm that says what differed,
a redelivery judges the first capture, which every later stage reads, and
the wait for the decision time keeps the heartbeat beating under the
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
    ClockReading,
    Halt,
    InstrumentId,
    Mode,
    Retry,
    Snapshot,
    StageKind,
    Stopped,
    Storage,
    attempt_key,
    decode_output,
    output_key,
    parse_strategy,
    sha256_of,
)
from ithildinruntime.data import DataStage, LakeSource, Mirror, differences, spikes
from ithildinruntime.data.reference import PriceKinds
from ithildinruntime.runtime.data_worker import DATA_ROW, data_buckets, data_handler
from ithildinruntime.runtime.loop import Item, Table
from tests.support import lake as lakes
from tests.support.clocks import never_asked
from tests.support.quotes import quote
from tests.support.splits import NO_SPLITS
from tests.test_data_source import FIRST, LAST, Clock, Timer, before, close, settings, unlist
from tests.test_data_spikes import bar
from tests.test_storage_contract import Alarms

CHAN = parse_strategy("chan", b'instruments = ["GLD", "USO"]\n')
OTHER = parse_strategy("other", b'instruments = ["SPY", "QQQ"]\n')
TABLE = Table(stages=(DATA_ROW,), strategies=(CHAN, OTHER))
# The same two strategies as files data's settings can be built from.
DATA = b"[data]\nlookback = 40\nstale_lag = 1\nspike_multiple = 100\n[data.first_sessions]\n"
READS_CHAN = parse_strategy(
    "chan", b'instruments = ["GLD", "USO"]\n' + DATA + b"GLD = 2006-04-26\nUSO = 2006-04-26\n"
)
OTHER_FILE = b'instruments = ["SPY", "QQQ"]\n' + DATA + b"SPY = 1993-01-29\nQQQ = 1999-03-10\n"
READS = Table(stages=(DATA_ROW,), strategies=(READS_CHAN, parse_strategy("other", OTHER_FILE)))

RUN = "2012-04-09"
DEPLOYMENT = "test"
MEASURED_NOTHING = ClockReading(close(LAST), None)


class Reports:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1

    def wait_for_stop(self, timeout: float) -> bool:
        raise AssertionError("no clock here waits")


def stage(
    lake: Path, tmp_path: Path, clock=None, measured: bool = False, **changes
) -> tuple[DataStage, Storage, Alarms]:
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
        live=FolderBucket(lake.with_name("no-live")),
        clock=clock,
        timer=Timer(),
        measured=measured,
    )
    made = DataStage(
        source,
        storage,
        clock,
        deployment=DEPLOYMENT,
        run=RUN,
        release="r1",
        config_hashes=TABLE.hashes_for("data"),
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
    # The price-kind contract states Yahoo's kind for both Chan vintages, so nothing halts.
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


@pytest.mark.parametrize(
    ("row", "rule"),
    [
        ({"open": 15.937, "high": 15.937, "low": 15.937, "close": 15.937}, spikes.MOVE),
        ({"close": 150.0}, spikes.BELOW_LOW),
    ],
)
def test_a_flag_that_alerts_is_stored_with_the_snapshot_and_halts_nothing(
    lake: Path, tmp_path: Path, row: dict[str, float], rule: str
) -> None:
    lakes.with_row(lake, "GLD", LAST, **row)
    made, _, _ = stage(lake, tmp_path, spike_multiple=Decimal(10))
    result = made(Item(close(LAST), "data"), Reports())
    (ref,) = result.outputs
    stored = decode_output((tmp_path / "store" / ref.key).read_bytes(), Snapshot)
    assert [f.rule for f in stored.entries[0].flags[InstrumentId("GLD")]] == [rule]
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
    assert reason.endswith(
        "Only the read time or the clock's reading differs, as on a redelivery after a crash"
    )


def test_18_a_reread_after_a_changed_bar_says_the_bars_differ(lake: Path, tmp_path: Path) -> None:
    made, _, alarms = stage(lake, tmp_path, clock=Ticking())
    made(Item(close(LAST), "data"), Reports())
    # A restated close inside the bar's range, so no spike flag changes. The
    # manifest records the new partition, so its hash in the ledgers changes too.
    lakes.with_row(lake, "GLD", LAST, close=159.38)
    third = made(Item(close(LAST), "data"), Reports())
    assert third.outputs[1].key == key(2)
    (reason,) = alarms.reasons
    assert reason.endswith("These differ: bars, ledgers")


def test_18_a_redelivery_halts_on_the_contradicting_capture_that_counts(
    lake: Path, tmp_path: Path
) -> None:
    """The decision trades from the first capture, so its halt follows the first capture."""
    original = (lake / lakes.partition_key("GLD", LAST)).read_bytes()
    lakes.with_row(lake, "GLD", LAST, high=150.0, low=149.0)
    made, _, _ = stage(lake, tmp_path, clock=Ticking())
    first = made(Item(close(LAST), "data"), Reports())
    assert first.halt is not None
    # The process died before the loop stored the result, and marketlake fixed the bar.
    lakes.rewrite(lake, lakes.partition_key("GLD", LAST), original)
    lakes.record(lake, lakes.partition_key("GLD", LAST), rows=1)
    again = made(Item(close(LAST), "data"), Reports())
    assert again.outputs[0].key == key() and again.outputs[1].key == key(2)
    assert again.halt is not None and again.halt.cause == again.outputs[0]
    assert f"{key()}, which counts: " in again.halt.reason
    assert "contradict each other" in again.halt.reason


def test_18_a_redelivery_judges_a_clean_capture_that_counts_however_the_reread_reads(
    lake: Path, tmp_path: Path
) -> None:
    made, _, _ = stage(lake, tmp_path, clock=Ticking())
    assert made(Item(close(LAST), "data"), Reports()).halt is None
    lakes.with_row(lake, "GLD", LAST, high=150.0, low=149.0)
    again = made(Item(close(LAST), "data"), Reports())
    assert again.outputs[1].key == key(2)
    assert again.halt is None


def test_18_a_redelivery_passes_a_capture_that_counts_with_a_close_below_its_low(
    lake: Path, tmp_path: Path
) -> None:
    lakes.with_row(lake, "GLD", LAST, close=150.0)
    made, _, _ = stage(lake, tmp_path, clock=Ticking())
    assert made(Item(close(LAST), "data"), Reports()).halt is None
    again = made(Item(close(LAST), "data"), Reports())
    assert again.outputs[1].key == key(2)
    assert again.halt is None


def test_18_a_redelivery_halts_on_a_stale_capture_that_counts(lake: Path, tmp_path: Path) -> None:
    gone = (lakes.partition_key("USO", LAST), lakes.partition_key("USO", before(LAST)))
    keep = {k: (lake / k).read_bytes() for k in gone}
    for k in gone:
        unlist(lake, k)
    made, _, _ = stage(lake, tmp_path, clock=Ticking())
    assert "USO" in made(Item(close(LAST), "data"), Reports()).halt.reason
    for k, data in keep.items():
        lakes.rewrite(lake, k, data)
        lakes.record(lake, k, rows=1)
    again = made(Item(close(LAST), "data"), Reports())
    assert again.outputs[1].key == key(2)
    assert again.halt is not None
    assert f"{key()}, which counts: USO" in again.halt.reason


def test_18_a_redelivery_halts_when_the_capture_that_counts_cannot_be_read(
    lake: Path, tmp_path: Path
) -> None:
    made, storage, _ = stage(lake, tmp_path, clock=Ticking())
    assert made(Item(close(LAST), "data"), Reports()).halt is None
    storage._read = lambda key, once=False: None  # type: ignore[method-assign]
    again = made(Item(close(LAST), "data"), Reports())
    assert again.outputs[1].key == key(2)
    assert again.halt is not None
    assert again.halt.reason == f"{key()} counts and could not be read to judge it"


def _again(lake: Path, tmp_path: Path, storage: Storage, clock, **changes) -> DataStage:
    """A second stage over the same store, as a process restarted under other settings."""
    kinds = changes.pop("kinds", None)
    release = changes.pop("release", "r1")
    hashes = changes.pop("config_hashes", TABLE.hashes_for("data"))
    source = LakeSource(
        Mirror(FolderBucket(lake), tmp_path / "mirror-again"),
        settings(**changes),
        live=FolderBucket(lake.with_name("no-live")),
        clock=clock,
        timer=Timer(),
        kinds=kinds,
        measured=False,
    )
    return DataStage(
        source,
        storage,
        clock,
        deployment=DEPLOYMENT,
        run=RUN,
        release=release,
        config_hashes=hashes,
    )


@pytest.mark.parametrize(("wrote", "reads"), [("r0", "r1"), ("r1", "r2")])
def test_18_a_redelivery_halts_on_a_clean_capture_another_release_wrote(
    lake: Path, tmp_path: Path, wrote: str, reads: str
) -> None:
    """Only this release's code reads a capture's fields as it wrote them."""
    clock = Ticking()
    _, storage, _ = stage(lake, tmp_path, clock=clock)
    assert (
        _again(lake, tmp_path, storage, clock, release=wrote)(
            Item(close(LAST), "data"), Reports()
        ).halt
        is None
    )
    again = _again(lake, tmp_path, storage, clock, release=reads)(
        Item(close(LAST), "data"), Reports()
    )
    assert again.outputs[1].key == key(2)
    assert again.halt is not None
    assert again.halt.reason == (
        f"{key()} counts and was written by release {wrote!r}, not this release {reads!r}, "
        "so it cannot be judged"
    )


def test_18_a_redelivery_names_the_release_before_any_halt_the_capture_holds(
    lake: Path, tmp_path: Path
) -> None:
    """Another release's contradicting capture halts on the release, not on this reading."""
    clock = Ticking()
    _, storage, _ = stage(lake, tmp_path, clock=clock)
    original = (lake / lakes.partition_key("GLD", LAST)).read_bytes()
    lakes.with_row(lake, "GLD", LAST, high=150.0, low=149.0)
    earlier = _again(lake, tmp_path, storage, clock, release="r0")
    assert earlier(Item(close(LAST), "data"), Reports()).halt is not None
    lakes.rewrite(lake, lakes.partition_key("GLD", LAST), original)
    lakes.record(lake, lakes.partition_key("GLD", LAST), rows=1)
    again = _again(lake, tmp_path, storage, clock)(Item(close(LAST), "data"), Reports())
    assert again.halt is not None
    assert again.halt.reason == (
        f"{key()} counts and was written by release 'r0', not this release 'r1', "
        "so it cannot be judged"
    )


@pytest.mark.parametrize(
    "change", ["every hash", "one hash", "a strategy since added", "a strategy since removed"]
)
def test_18_a_redelivery_halts_on_a_capture_written_under_other_configuration_hashes(
    lake: Path, tmp_path: Path, change: str
) -> None:
    ours = dict(TABLE.hashes_for("data"))
    other = {
        "every hash": {"chan": "d" * 64, "other": "e" * 64},
        "one hash": {**ours, "other": "e" * 64},
        "a strategy since added": {"chan": ours["chan"]},
        "a strategy since removed": {**ours, "third": "f" * 64},
    }[change]
    clock = Ticking()
    _, storage, _ = stage(lake, tmp_path, clock=clock)
    assert (
        _again(lake, tmp_path, storage, clock, config_hashes=other)(
            Item(close(LAST), "data"), Reports()
        ).halt
        is None
    )
    again = _again(lake, tmp_path, storage, clock)(Item(close(LAST), "data"), Reports())
    assert again.halt is not None
    assert again.halt.reason == (
        f"{key()} counts and was written under configuration hashes {other}, "
        f"not this stage's {ours}, so it cannot be judged"
    )


@pytest.mark.parametrize("count", [0, 2])
def test_18_a_redelivery_halts_on_a_capture_that_holds_other_than_one_snapshot(
    lake: Path, tmp_path: Path, count: int
) -> None:
    from ithildinruntime.core import BlobHeader, OutputFile, encode_output

    made, storage, _ = stage(lake, tmp_path, clock=Ticking())
    clean = (
        LakeSource(
            Mirror(FolderBucket(lake), tmp_path / "clean"),
            settings(),
            live=FolderBucket(lake.with_name("no-live")),
            clock=Clock(),
            timer=Timer(),
            measured=False,
        )
        .read(close(LAST))
        .snapshot
    )
    header = BlobHeader(
        1, DEPLOYMENT, RUN, "data", "capturing", close(LAST), {}, "r1", TABLE.hashes_for("data")
    )
    held = encode_output(Snapshot, OutputFile(header, (clean,) * count, False))
    storage._store.create(key(), held, sha256_of(held))
    again = made(Item(close(LAST), "data"), Reports())
    assert again.halt is not None
    assert again.halt.reason == f"{key()} counts and holds {count} snapshots, not one"


def test_the_stage_refuses_an_empty_release(lake: Path, tmp_path: Path) -> None:
    _, storage, _ = stage(lake, tmp_path)
    with pytest.raises(ValueError, match="names its release"):
        _again(lake, tmp_path, storage, Ticking(), release="")


def test_18_a_redelivery_halts_on_a_missing_capture_that_counts(lake: Path, tmp_path: Path) -> None:
    gap = lakes.partition_key("USO", before(LAST, 2))
    keep = (lake / gap).read_bytes()
    unlist(lake, gap)
    made, _, _ = stage(lake, tmp_path, clock=Ticking())
    assert "missing" in made(Item(close(LAST), "data"), Reports()).halt.reason
    lakes.rewrite(lake, gap, keep)
    lakes.record(lake, gap, rows=1)
    again = made(Item(close(LAST), "data"), Reports())
    assert again.outputs[1].key == key(2)
    assert again.halt is not None
    assert again.halt.reason == f"{key()}, which counts: USO missing in the stored snapshot"


def test_18_a_redelivery_halts_on_an_unstated_capture_that_counts(
    lake: Path, tmp_path: Path
) -> None:
    clock = Ticking()
    made, storage, _ = stage(lake, tmp_path, clock=clock)
    silent = _again(lake, tmp_path, storage, clock, kinds=PriceKinds(()))
    assert "unstated" in silent(Item(close(LAST), "data"), Reports()).halt.reason
    again = made(Item(close(LAST), "data"), Reports())
    assert again.outputs[1].key == key(2)
    assert again.halt is not None
    assert again.halt.reason.startswith(f"{key()}, which counts: GLD's bar for ")
    assert "has unstated price kind, venues, delay, volume counting" in again.halt.reason


def test_18_a_redelivery_halts_with_every_reason_the_capture_that_counts_holds(
    lake: Path, tmp_path: Path
) -> None:
    gone = (lakes.partition_key("USO", LAST), lakes.partition_key("USO", before(LAST)))
    keep = {k: (lake / k).read_bytes() for k in gone}
    original = (lake / lakes.partition_key("GLD", LAST)).read_bytes()
    for k in gone:
        unlist(lake, k)
    lakes.with_row(lake, "GLD", LAST, high=150.0, low=149.0)
    made, _, _ = stage(lake, tmp_path, clock=Ticking())
    assert made(Item(close(LAST), "data"), Reports()).halt is not None
    for k, data in {**keep, lakes.partition_key("GLD", LAST): original}.items():
        lakes.rewrite(lake, k, data)
        lakes.record(lake, k, rows=1)
    halt = made(Item(close(LAST), "data"), Reports()).halt
    assert halt is not None
    reasons = halt.reason.split("; ")
    assert f"{key()}, which counts: USO stale in the stored snapshot" in reasons
    assert any("contradict each other" in r for r in reasons) and len(reasons) == 2


def test_18_a_redelivery_halts_on_an_instrument_the_capture_that_counts_lacks(
    lake: Path, tmp_path: Path
) -> None:
    clock = Ticking()
    made, storage, _ = stage(lake, tmp_path, clock=clock)
    narrow = _again(lake, tmp_path, storage, clock, instruments=(InstrumentId("GLD"),))
    assert narrow(Item(close(LAST), "data"), Reports()).halt is None
    again = made(Item(close(LAST), "data"), Reports())
    assert again.halt is not None
    assert again.halt.reason == f"{key()}, which counts: USO is not in the stored snapshot"


def test_18_a_redelivery_halts_when_the_bytes_read_back_are_not_the_capture_that_counts(
    lake: Path, tmp_path: Path
) -> None:
    made, storage, _ = stage(lake, tmp_path, clock=Ticking())
    assert made(Item(close(LAST), "data"), Reports()).halt is None
    original = (tmp_path / "store" / key()).read_bytes()
    real = storage._read

    def other(k: str, once: bool = False) -> bytes | None:
        data = real(k, once)
        return data + b" " if k == key() and data is not None else data

    storage._read = other  # type: ignore[method-assign]
    again = made(Item(close(LAST), "data"), Reports())
    assert again.halt is not None
    assert again.halt.reason == (
        f"{key()} counts and was read back with SHA-256 {sha256_of(original + b' ')}, "
        f"not the stored {sha256_of(original)}"
    )


def test_18_a_redelivery_halts_when_the_capture_that_counts_is_no_snapshot(
    lake: Path, tmp_path: Path
) -> None:
    made, storage, _ = stage(lake, tmp_path, clock=Ticking())
    storage._store.create(key(), b"not a snapshot", sha256_of(b"not a snapshot"))
    again = made(Item(close(LAST), "data"), Reports())
    assert again.outputs[0].key == key() and again.outputs[1].key == key(2)
    assert again.halt is not None
    assert again.halt.reason.startswith(f"{key()} counts and is no snapshot the codec reads: ")


def test_18_a_redelivery_whose_new_read_gives_no_snapshot_halts_with_a_clean_capture_stored(
    lake: Path, tmp_path: Path
) -> None:
    """The read comes before the store, so a read that cannot give a snapshot halts first."""
    made, _, _ = stage(lake, tmp_path, clock=Ticking())
    assert made(Item(close(LAST), "data"), Reports()).halt is None
    lakes.doubled(lake, "GLD", LAST)
    with pytest.raises(Halt, match="GLD holds 2 daily rows"):
        made(Item(close(LAST), "data"), Reports())


def test_18_a_redelivery_halts_on_a_first_session_written_late_since_the_first_capture(
    lake: Path, tmp_path: Path
) -> None:
    """Only the manifest shows it, so a halt beside the stored capture would miss it."""
    clock = Ticking()
    made, storage, _ = stage(lake, tmp_path, clock=clock)
    assert made(Item(close(LAST), "data"), Reports()).halt is None
    held = (tmp_path / "store" / key()).read_bytes()
    late = {InstrumentId("GLD"): before(LAST, 5), InstrumentId("USO"): FIRST}
    again = _again(lake, tmp_path, storage, clock, first_sessions=late)
    with pytest.raises(Halt, match="GLD's first session is written as"):
        again(Item(close(LAST), "data"), Reports())
    assert (tmp_path / "store" / key()).read_bytes() == held
    assert not (tmp_path / "store" / key(2)).exists()


def test_18_a_reread_under_the_history_clock_stores_the_same_bytes(
    lake: Path, tmp_path: Path
) -> None:
    clock = HistoryClock(readings=None)
    made, _, alarms = stage(lake, tmp_path, clock=clock)
    first = made(Item(close(LAST), "data"), Reports())
    second = made(Item(close(LAST), "data"), Reports())
    assert first.outputs == second.outputs
    assert alarms.reasons == []


@pytest.mark.parametrize(
    "names",
    [
        ("bars",),
        ("status",),
        ("flags",),
        ("quotes",),
        ("provisional",),
        ("ledgers",),
        ("rows",),
        ("faults",),
        ("bars", "ledgers"),
        ("partitions",),
        ("segments", "faults"),
        ("status", "ledgers"),
        ("flags", "segments"),
    ],
)
def test_18_the_reread_alarm_names_what_changed_in_the_snapshot(names: tuple[str, ...]) -> None:
    from ithildinruntime.core import (
        BarStatus,
        BlobHeader,
        InstrumentId,
        OutputFile,
        Segment,
        SpikeFlag,
        encode_output,
    )

    hashes = {"chan_bollinger": "c" * 64}
    header = BlobHeader(1, DEPLOYMENT, RUN, "data", "capturing", close(LAST), {}, "r1", hashes)
    base = Snapshot(
        close(LAST), {}, {}, {}, False, {}, (), (), {}, {}, (), None, MEASURED_NOTHING, NO_SPLITS
    )
    gld = InstrumentId("GLD")
    flag = SpikeFlag(LAST, "close", Decimal(1), Decimal(2), "a rule", "a note")
    two = close(LAST) + timedelta(minutes=1)
    values = {
        "status": {gld: BarStatus.STALE},
        "flags": {gld: (flag,)},
        "quotes": {gld: quote(gld, 1, 2, close(LAST))},
        "provisional": True,
        "ledgers": {"manifest.jsonl": "e" * 64},
        "segments": (Segment("journal/a.arrows", "v1", "e" * 64),),
        "partitions": ("quotes/a#sha256=x#version=v",),
        "rows": {gld: ({"bid": "1.0", "ask": None},)},
        "faults": ("a reason",),
        "bars": {gld: {"1d": (bar(0, "100"),)}},
    }
    read = {} if set(names) <= {"bars", "status", "flags", "ledgers"} else {"quotes_read_at": two}
    if "segments" in names:
        base = dataclasses.replace(base, provisional=True, quotes_read_at=two)
    changed = dataclasses.replace(base, **{name: values[name] for name in names}, **read)

    def encoded(snap: Snapshot) -> bytes:
        return encode_output(Snapshot, OutputFile(header, (snap,), False))

    assert (
        differences(encoded(base), encoded(changed), measured=False)
        == f"These differ: {', '.join(names)}"
    )


@pytest.mark.parametrize(
    ("change", "said"),
    [
        ("config_hashes", "the strategies' configuration hashes"),
        ("one hash", "the strategies' configuration hashes"),
        ("release", "the release"),
        ("format", "the format"),
        ("release and flags", "the release, flags"),
        ("release and ledgers", "the release, ledgers"),
    ],
)
def test_18_the_reread_alarm_names_a_change_in_the_header(change: str, said: str) -> None:
    from ithildinruntime.core import BlobHeader, InstrumentId, OutputFile, SpikeFlag, encode_output

    hashes = {"chan": "c" * 64}
    first = BlobHeader(1, DEPLOYMENT, RUN, "data", "capturing", close(LAST), {}, "r1", hashes)
    snap = Snapshot(
        close(LAST), {}, {}, {}, False, {}, (), (), {}, {}, (), None, MEASURED_NOTHING, NO_SPLITS
    )
    later, later_snap = first, snap
    if change == "config_hashes":
        later = dataclasses.replace(first, config_hashes={**hashes, "other": "d" * 64})
    elif change == "one hash":
        later = dataclasses.replace(first, config_hashes={"chan": "d" * 64})
    elif change == "format":
        later = dataclasses.replace(first, format=3)
    else:
        later = dataclasses.replace(first, release="r2")
    if change == "release and flags":
        flag = SpikeFlag(LAST, "close", Decimal(1), Decimal(2), "a rule", "a note")
        later_snap = dataclasses.replace(snap, flags={InstrumentId("GLD"): (flag,)})
    elif change == "release and ledgers":
        later_snap = dataclasses.replace(snap, ledgers={"manifest.jsonl": "e" * 64})

    def encoded(header: BlobHeader, body: Snapshot) -> bytes:
        return encode_output(Snapshot, OutputFile(header, (body,), False))

    assert (
        differences(encoded(first, snap), encoded(later, later_snap), measured=False)
        == f"These differ: {said}"
    )


@pytest.mark.parametrize(("first", "later"), [(0, 1), (2, 1), (1, 0)])
def test_18_the_reread_alarm_names_a_capture_that_holds_other_than_one_snapshot(
    first: int, later: int
) -> None:
    from ithildinruntime.core import BlobHeader, OutputFile, encode_output

    header = BlobHeader(
        1, DEPLOYMENT, RUN, "data", "capturing", close(LAST), {}, "r1", TABLE.hashes_for("data")
    )
    snap = Snapshot(
        close(LAST), {}, {}, {}, False, {}, (), (), {}, {}, (), None, MEASURED_NOTHING, NO_SPLITS
    )

    def holding(n: int) -> bytes:
        return encode_output(Snapshot, OutputFile(header, (snap,) * n, False))

    assert differences(holding(first), holding(later), measured=False) == (
        f"The captures hold {first} and {later} snapshots, where each should hold one"
    )


# 19. The wait.


class FakeTime:
    """A timer a case advances."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return (self.at - datetime(2000, 1, 1, tzinfo=UTC)).total_seconds()

    def after(self, seconds: float) -> object:
        raise NotImplementedError


class FakeProgress:
    """A handler's `report` on a `FakeTime`, whose loop is asked to stop at `stop_at`."""

    def __init__(self, time: FakeTime, stop_at: datetime | None = None) -> None:
        self.time = time
        self.stop_at = stop_at
        self.stopped = False
        self.reported: list[datetime] = []
        self.waited: list[float] = []

    def __call__(self) -> None:
        self.reported.append(self.time.now())

    def wait_for_stop(self, timeout: float) -> bool:
        # A clock that waits on past the stop would otherwise wait for ever here.
        assert not self.stopped, "the clock waited on after the stop"
        assert timeout > 0, "the clock waited for no time"
        end = self.time.at + timedelta(seconds=timeout)
        self.waited.append(timeout)
        if self.stop_at is not None and self.stop_at <= end:
            self.stopped = True
            self.time.at = max(self.time.at, self.stop_at)
            return True
        self.time.at = end
        return False


def test_19_the_wall_clock_reports_while_it_waits_past_the_heartbeat_limit() -> None:
    start = datetime(2012, 4, 9, 13, 0, tzinfo=UTC)
    time = FakeTime(start)
    clock = WallClock(time, servers=never_asked(), interval=5.0)
    report = FakeProgress(time)
    # The receive loop's heartbeat limit is 30 seconds, and the wait is 7 hours.
    at = start + timedelta(hours=7)
    clock.wait_until(at, report)
    assert time.now() == at
    reported = report.reported
    gaps = [(b - a).total_seconds() for a, b in zip([start, *reported], reported, strict=False)]
    assert max(gaps) <= 5.0
    assert len(reported) == 7 * 3600 / 5


@pytest.mark.parametrize("stopped", [False, True])
@pytest.mark.parametrize("past", [timedelta(0), timedelta(microseconds=1), timedelta(hours=1)])
def test_19_the_wall_clock_returns_at_once_once_the_time_has_passed(
    stopped: bool, past: timedelta
) -> None:
    """A wait that starts at or after the time returns, whether or not a stop was asked."""
    at = datetime(2012, 4, 9, 20, 0, tzinfo=UTC)
    time = FakeTime(at + past)
    report = FakeProgress(time, at if stopped else None)
    WallClock(time, servers=never_asked()).wait_until(at, report)
    assert report.waited == [] and time.now() == at + past


def test_19_the_wall_clock_waits_out_the_last_microsecond() -> None:
    at = datetime(2012, 4, 9, 20, 0, tzinfo=UTC)
    time = FakeTime(at - timedelta(microseconds=1))
    report = FakeProgress(time)
    WallClock(time, servers=never_asked()).wait_until(at, report)
    assert report.waited == [1e-06] and time.now() == at


def test_19_the_wall_clock_never_waits_past_the_time() -> None:
    start = datetime(2012, 4, 9, 13, 0, tzinfo=UTC)
    time = FakeTime(start)
    report = FakeProgress(time)
    WallClock(time, servers=never_asked(), interval=5.0).wait_until(
        start + timedelta(seconds=12), report
    )
    assert report.waited == [5.0, 5.0, 2.0] and time.now() == start + timedelta(seconds=12)


def test_a_stop_ends_the_wall_clocks_wait_at_once() -> None:
    start = datetime(2012, 4, 9, 13, 0, tzinfo=UTC)
    time = FakeTime(start)
    stop_at = start + timedelta(seconds=12)
    report = FakeProgress(time, stop_at)
    with pytest.raises(Stopped, match="2012-04-09T20:00:00"):
        WallClock(time, servers=never_asked(), interval=5.0).wait_until(
            start + timedelta(hours=7), report
        )
    assert time.now() == stop_at
    assert report.waited == [5.0, 5.0, 5.0]


def test_a_stop_already_asked_ends_the_wall_clocks_wait_before_any_time_passes() -> None:
    start = datetime(2012, 4, 9, 13, 0, tzinfo=UTC)
    time = FakeTime(start)
    with pytest.raises(Stopped):
        WallClock(time, servers=never_asked()).wait_until(
            start + timedelta(hours=7), FakeProgress(time, start)
        )
    assert time.now() == start


def test_the_history_clock_ignores_a_stop() -> None:
    at = close(LAST)
    clock = HistoryClock(readings=None)
    clock.wait_until(at, FakeProgress(FakeTime(at), at))
    assert clock.now() == at


def test_a_stop_while_datas_stage_waits_reads_and_stores_nothing(
    lake: Path, tmp_path: Path
) -> None:
    class Stopping(Clock):
        def wait_until(self, at, report) -> None:
            raise Stopped("asked to stop")

    made, _, alarms = stage(lake, tmp_path, clock=Stopping())
    reads: list[datetime] = []
    made._source.read = lambda at, report: reads.append(at)
    with pytest.raises(Stopped):
        made(Item(close(LAST), "data"), Reports())
    assert reads == []
    assert not [p for p in (tmp_path / "store").rglob("*") if p.is_file()]
    assert alarms.reasons == []
    # The decision runs again at the next start, as if for the first time.
    again, _, alarms = stage(lake, tmp_path)
    result = again(Item(close(LAST), "data"), Reports())
    assert result.halt is None and len(result.outputs) == 1 and alarms.reasons == []


def test_19_the_history_clock_returns_at_once_and_reads_the_decision_time() -> None:
    clock = HistoryClock(readings=None)
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
        HistoryClock(readings=None).wait_until(datetime(2012, 4, 9, 16, 0), Reports())


# The entry point.


def test_the_entry_point_adds_datas_row_as_a_capturing_stage_with_no_inputs() -> None:
    assert DATA_ROW.name == "data"
    assert DATA_ROW.kind is StageKind.CAPTURING
    assert DATA_ROW.inputs == ()
    Table((DATA_ROW,))


def test_a_backtest_reads_two_folders_and_every_other_mode_two_prefixes(tmp_path: Path) -> None:
    for root, key in (("lake", "manifest.jsonl"), ("lake-live", "journal/x.arrows")):
        path = tmp_path / root / key
        path.parent.mkdir(parents=True)
        path.write_bytes(root.encode())
    lake, live = data_buckets(Mode.BACKTEST, folder=tmp_path / "lake")
    assert isinstance(lake, FolderBucket) and isinstance(live, FolderBucket)
    assert lake.read("manifest.jsonl").data == b"lake"
    assert live.list("journal/") == ["journal/x.arrows"]
    assert live.read("journal/x.arrows").data == b"lake-live"
    for mode in (Mode.SHADOW, Mode.PAPER, Mode.LIVE):
        buckets = data_buckets(mode, client=object(), bucket="marketlake", prefix="a/lake")
        assert all(isinstance(b, S3Bucket) for b in buckets)
        assert [b._root for b in buckets] == ["a/lake/", "a/lake-live/"]


def test_a_missing_folder_beside_the_lake_lists_nothing(tmp_path: Path) -> None:
    (tmp_path / "lake").mkdir()
    assert data_buckets(Mode.BACKTEST, folder=tmp_path / "lake").live.list("journal/") == []


S3 = {"client": object(), "bucket": "marketlake", "prefix": "lake"}


@pytest.mark.parametrize(
    "wrong",
    [
        *({k: v for k, v in S3.items() if k != missing} for missing in S3),
        S3 | {"prefix": ""},
        S3 | {"bucket": ""},
        S3 | {"folder": "/lake"},
        {"folder": "/lake"},
    ],
)
def test_every_mode_but_a_backtest_names_a_client_a_bucket_and_a_prefix(wrong: dict) -> None:
    for mode in (Mode.SHADOW, Mode.PAPER, Mode.LIVE):
        with pytest.raises(ValueError, match="through a client, its name and its prefix"):
            data_buckets(mode, **wrong)


@pytest.mark.parametrize(
    "wrong", [S3, {}, {"folder": "/lake", "prefix": "lake"}, {"folder": "/lake", "client": 1}]
)
def test_a_backtest_names_a_folder_alone(wrong: dict) -> None:
    with pytest.raises(ValueError, match="from a folder, and only that"):
        data_buckets(Mode.BACKTEST, **wrong)


@pytest.mark.parametrize("folder", [".", "..", "a/..", "/"])
def test_a_folder_with_no_name_has_none_beside_it(folder: str) -> None:
    with pytest.raises(ValueError, match="names no folder"):
        data_buckets(Mode.BACKTEST, folder=folder)


def test_a_change_to_one_strategys_file_leaves_every_other_strategys_hash_in_datas_header(
    lake: Path, tmp_path: Path
) -> None:
    """Data's stage is shared, so its header names each strategy's own hash."""
    changed = parse_strategy("other", OTHER_FILE + b"# a new version\n")
    after = Table(stages=(DATA_ROW,), strategies=(READS_CHAN, changed))
    tables = {"before": READS, "after": after}
    headers = {}
    for name, table in tables.items():
        _, storage, _ = stage(lake, tmp_path / name)
        handler = data_handler(
            bucket=FolderBucket(lake),
            live=FolderBucket(tmp_path / "no-live"),
            mirror=tmp_path / name / "mirror",
            storage=storage,
            clock=Clock(),
            timer=Timer(),
            deployment=DEPLOYMENT,
            run=RUN,
            release="r1",
            table=table,
            measured=False,
        )
        result = handler(Item(close(LAST), "data"), Reports())
        stored = decode_output(
            (tmp_path / name / "store" / result.outputs[0].key).read_bytes(), Snapshot
        )
        assert dict(stored.header.config_hashes) == dict(result.header.config_hashes)
        # The lake holds neither of the second strategy's instruments, which data now reads.
        assert result.halt is not None
        assert "SPY missing for the decision" in result.halt.reason
        assert "QQQ missing for the decision" in result.halt.reason
        headers[name] = stored.header
    assert dict(headers["before"].config_hashes) == dict(READS.hashes_for("data"))
    assert headers["before"].config_hashes["chan"] == headers["after"].config_hashes["chan"]
    assert headers["after"].config_hashes["other"] == changed.config_hash
    assert headers["before"].config_hashes["other"] != changed.config_hash


def test_the_entry_points_handler_reads_through_the_bucket_it_is_given(
    lake: Path, tmp_path: Path
) -> None:
    made, storage, _ = stage(lake, tmp_path)
    handler = data_handler(
        bucket=FolderBucket(lake),
        live=FolderBucket(tmp_path / "no-live"),
        mirror=tmp_path / "other-mirror",
        storage=storage,
        clock=Clock(),
        timer=Timer(),
        deployment=DEPLOYMENT,
        run=RUN,
        release="r1",
        table=READS,
        measured=False,
    )
    result = handler(Item(close(LAST), "data"), Reports())
    assert result.outputs[0].key == key()
    assert (tmp_path / "other-mirror" / "objects").is_dir()
