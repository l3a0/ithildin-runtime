"""The halt rule on a reading of the clock, the records it reads, and data's halt on it."""

from __future__ import annotations

import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket, HistoryClock
from ithildinruntime.core import (
    ANSWER_BOUND,
    CLOCK_BOUND,
    DRIFT_PPM,
    SYNC_BOUND,
    BarStatus,
    BlobHeader,
    ClockReading,
    CodecError,
    Halt,
    InstrumentId,
    Mode,
    OutputFile,
    Plan,
    PlanDecision,
    Snapshot,
    TimeAnswer,
    clock_faults,
    decode,
    decode_output,
    encode,
    encode_output,
    result_key,
)
from ithildinruntime.data import differences, source
from ithildinruntime.runtime.data_worker import DATA_ROW, data_handler
from ithildinruntime.runtime.loop import Item, ReceiveLoop, Table
from tests.support import lake as lakes
from tests.support.clocks import START, answer, good, slow
from tests.support.queues import Alarms, Folder, SystemTimer, running
from tests.support.queues import storage as loop_storage
from tests.test_data_settings import chan
from tests.test_data_source import LAST, Clock, before, close, make
from tests.test_data_stage import DEPLOYMENT, RUN, Reports, key, stage

SLOW = (
    "the clock is 2.000000 seconds slow against time server near, give or take 0.000011, "
    "past the bound of 1.000000 seconds"
)

# 5. The halt rule.


def test_5_the_bounds() -> None:
    assert CLOCK_BOUND == timedelta(seconds=1)
    assert SYNC_BOUND == timedelta(seconds=300)
    assert ANSWER_BOUND == timedelta(milliseconds=250)
    assert DRIFT_PPM == 500


def two(server: str, **changes: object) -> ClockReading:
    return ClockReading(
        START, tuple(answer(s, **(changes if s == server else {})) for s in ("near", "far"))
    )


@pytest.mark.parametrize("server", ["near", "far"])
@pytest.mark.parametrize(
    ("changes", "said"),
    [
        ({"age_us": 299_999_999}, None),
        ({"age_us": 300_000_000}, None),
        (
            {"age_us": 300_000_001, "failed": "no reply within one second"},
            "time server {}'s last answer is 300.000001 seconds old, past the bound of "
            "300.000000 seconds: no reply within one second",
        ),
        (
            {"age_us": 300_000_001, "offset_us": 9_000_000},
            "time server {}'s last answer is 300.000001 seconds old, past the bound of "
            "300.000000 seconds: no ask has ended since",
        ),
        ({"offset_us": -999_988}, None),
        ({"offset_us": -999_989}, None),
        (
            {"offset_us": -999_990},
            "the clock is 0.999990 seconds slow against time server {}, give or take 0.000011, "
            "past the bound of 1.000000 seconds",
        ),
        ({"offset_us": 999_988}, None),
        ({"offset_us": 999_989}, None),
        (
            {"offset_us": 999_990},
            "the clock is 0.999990 seconds fast against time server {}, give or take 0.000011, "
            "past the bound of 1.000000 seconds",
        ),
        (
            {"offset_us": 0, "error_us": 1_000_001},
            "the clock is 0.000000 seconds from time server {}, give or take 1.000001, "
            "past the bound of 1.000000 seconds",
        ),
        (
            {k: None for k in ("offset_us", "error_us", "age_us", "received")}
            | {"failed": "the name does not resolve"},
            "time server {} has given no answer yet: the name does not resolve",
        ),
        (
            {k: None for k in ("offset_us", "error_us", "age_us", "received")},
            "time server {} has given no answer yet: no ask has ended",
        ),
    ],
)
def test_5_each_bound_for_each_server(server: str, changes, said: str | None) -> None:
    want = () if said is None else (said.format(server),)
    for measured in (True, False):
        assert clock_faults(two(server, **changes), measured=measured) == want


def test_5_both_servers_faults_come_in_the_readings_order() -> None:
    reading = ClockReading(
        START, (answer("z", offset_us=2_000_000), answer("a", age_us=300_000_001))
    )
    first, second = clock_faults(reading, measured=True)
    assert "time server z" in first and "time server a" in second


def test_5_a_reading_that_measured_nothing_faults_only_a_run_that_measures() -> None:
    nothing = ClockReading(START, None)
    assert clock_faults(nothing, measured=False) == ()
    assert clock_faults(nothing, measured=True) == ("no time server was asked",)
    # An absent value is a missing input, and never reads as a run that measures nothing.
    for absent in (None, 0, ""):
        assert clock_faults(nothing, measured=absent) == ("no time server was asked",)  # type: ignore[arg-type]


# 6. The records.


@pytest.mark.parametrize(
    "changes",
    [
        {"server": ""},
        {"server": 1},
        {"offset_us": True},
        {"error_us": False},
        {"age_us": True},
        {"offset_us": 1.0},
        {"offset_us": timedelta(0)},
        {"error_us": -1},
        {"age_us": -1},
        {"offset_us": None},
        {"error_us": None},
        {"age_us": None},
        {"received": None},
        {"offset_us": None, "error_us": None, "age_us": None},
        {"received": "24" * 47},
        {"received": "ZZ" * 48},
        {"received": "AB" * 48},
        {"received": "24" * 49},
        {"received": b"x" * 48},
        {"failed": ""},
        {"failed": 1},
    ],
)
def test_6_an_answer_refuses_a_bad_value(changes) -> None:
    with pytest.raises(ValueError):
        answer(**changes)


@pytest.mark.parametrize(
    ("at", "answers"),
    [
        (START, ()),
        (START, (answer(),)),
        (START, 7),
        (START, (answer(), "far")),
        (START, (answer(), answer())),
        (START, (answer("far"), answer(), answer("far"))),
        (datetime(2012, 4, 9), None),
        (START.astimezone(timezone(timedelta(hours=1))), None),
        ("2012", None),
    ],
)
def test_6_a_reading_refuses_a_bad_value(at, answers) -> None:
    with pytest.raises(ValueError, match="^a clock (reading holds|is read at a UTC time)"):
        ClockReading(at, answers)


NONE_YET = TimeAnswer("far", None, None, None, None, "no reply within one second")


@pytest.mark.parametrize(
    "reading",
    [good(), slow(), ClockReading(START, None), ClockReading(START, (answer(), NONE_YET))],
)
def test_6_a_reading_round_trips_the_codec(reading: ClockReading) -> None:
    assert decode(encode(reading), ClockReading) == reading


def test_6_the_codec_refuses_an_answer_the_record_refuses() -> None:
    data = encode(answer(error_us=11)).replace(b'"error_us":11', b'"error_us":-11')
    with pytest.raises(CodecError):
        decode(data, TimeAnswer)


# 7. The history clock.


def test_7_the_history_clock_measures_nothing() -> None:
    clock = HistoryClock(readings=None)
    clock.wait_until(close(LAST), Reports())
    assert clock.reading() == ClockReading(close(LAST), None)


def test_7_the_history_clock_has_no_default() -> None:
    with pytest.raises(TypeError):
        HistoryClock()  # type: ignore[call-arg]


def test_7_the_history_clock_hands_back_a_recorded_reading_or_halts() -> None:
    earlier = close(before(LAST))
    # A session's reading is taken after its decision.
    recorded = good(earlier + timedelta(seconds=9))
    clock = HistoryClock(readings={earlier: recorded})
    clock.wait_until(earlier, Reports())
    assert clock.reading() == recorded
    for lacking in (clock, HistoryClock(readings={})):
        lacking.wait_until(close(LAST), Reports())
        with pytest.raises(Halt, match="no reading of the clock is recorded for the decision at"):
            lacking.reading()


def test_7_each_thread_reads_its_own_decision() -> None:
    times = [close(before(LAST)), close(LAST)]
    clock = HistoryClock(readings={t: good(t) for t in times})
    barrier, seen = threading.Barrier(2), {}

    def run(at: datetime) -> None:
        clock.wait_until(at, Reports())
        barrier.wait(5)
        seen[at] = clock.reading().read_at

    threads = [threading.Thread(target=run, args=(t,)) for t in times]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert seen == {t: t for t in times}


@pytest.mark.parametrize("readings", [None, {close(LAST): good(close(LAST))}])
def test_7_a_rerun_stores_the_same_bytes(lake: Path, tmp_path: Path, readings) -> None:
    stored = []
    for name in ("one", "two"):
        made, _, _ = stage(lake, tmp_path / name, clock=HistoryClock(readings=readings))
        assert made(Item(close(LAST), "data"), Reports()).halt is None
        stored.append((tmp_path / name / "store" / key()).read_bytes())
    assert stored[0] == stored[1]


# 8. Data's stage.


class Reads(Clock):
    """A wall clock's read time, with the readings a case hands in, one per decision read."""

    def __init__(self, *readings: ClockReading) -> None:
        super().__init__()
        self.readings = list(readings)

    def now(self) -> datetime:
        self.at += timedelta(seconds=1)
        return self.at

    def reading(self) -> ClockReading:
        return self.readings.pop(0)


NO_ANSWER = ClockReading(START, (NONE_YET, answer("near")))
OLD = two("far", age_us=300_000_001)
FAST = two("near", offset_us=1_000_000)
NOTHING = ClockReading(START, None)


@pytest.mark.parametrize("reading", [slow(), FAST, OLD, NO_ANSWER, NOTHING])
def test_8_a_fault_halts_the_decision_with_its_snapshot_stored(
    lake: Path, tmp_path: Path, reading: ClockReading
) -> None:
    lakes.with_row(lake, "GLD", LAST, high=150.0, low=149.0)
    made, _, _ = stage(lake, tmp_path, clock=Reads(reading), measured=True)
    result = made(Item(close(LAST), "data"), Reports())
    (ref,) = result.outputs
    stored = decode_output((tmp_path / "store" / ref.key).read_bytes(), Snapshot)
    assert stored.header.format == 3
    assert stored.entries[0].clock == reading
    assert result.halt is not None and result.halt.cause == ref
    # The clock's fault comes first.
    (fault,) = clock_faults(reading, measured=True)
    assert result.halt.reason.startswith(f"{fault}; GLD's bar for")


def test_8_a_redelivery_judges_the_clock_of_the_capture_that_counts(
    lake: Path, tmp_path: Path
) -> None:
    lakes.with_row(lake, "GLD", LAST, high=150.0, low=149.0)
    made, _, alarms = stage(lake, tmp_path, clock=Reads(slow(), good()), measured=True)
    made(Item(close(LAST), "data"), Reports())
    again = made(Item(close(LAST), "data"), Reports())
    assert again.halt is not None and again.halt.cause == again.outputs[0]
    assert again.halt.reason.startswith(f"{key()}, which counts: {SLOW}; {key()}, which counts: ")
    (reason,) = alarms.reasons
    assert reason.endswith("These differ: whether the clock was past a bound")


def test_8_the_clock_is_named_last() -> None:
    later = _capture(slow(), status={InstrumentId("GLD"): BarStatus.STALE})
    assert differences(_capture(good()), later, measured=True) == (
        "These differ: status, whether the clock was past a bound"
    )


def test_8_a_redelivery_on_a_bad_clock_passes_a_good_capture(lake: Path, tmp_path: Path) -> None:
    made, _, alarms = stage(lake, tmp_path, clock=Reads(good(), slow()))
    assert made(Item(close(LAST), "data"), Reports()).halt is None
    again = made(Item(close(LAST), "data"), Reports())
    assert again.halt is None and again.outputs[1].key == key(2)
    (reason,) = alarms.reasons
    assert reason.endswith("These differ: whether the clock was past a bound")


def test_8_the_reading_is_taken_after_the_last_answer_is_read(
    lake: Path, tmp_path: Path, monkeypatch
) -> None:
    order: list[str] = []
    real = source.load_bars

    def noted(*args, **kwargs):
        order.append("bars")
        return real(*args, **kwargs)

    class Noted(Reads):
        def reading(self) -> ClockReading:
            order.append("clock")
            return super().reading()

    monkeypatch.setattr(source, "load_bars", noted)
    made, _, _ = stage(lake, tmp_path, clock=Noted(good()))
    made(Item(close(LAST), "data"), Reports())
    assert order.count("clock") == 1 and order[-1] == "clock" and "bars" in order


def test_8_the_ports_read_raises_on_a_fault(lake: Path, tmp_path: Path) -> None:
    src = make(lake, tmp_path)
    src._clock = Reads(slow())
    with pytest.raises(Halt) as raised:
        src.snapshot(close(LAST))
    assert raised.value.reason == SLOW


def test_8_a_redelivery_over_a_capture_that_measured_nothing_follows_the_run(
    lake: Path, tmp_path: Path
) -> None:
    for measured in (False, True):
        where = tmp_path / str(measured)
        made, _, alarms = stage(lake, where, clock=Reads(NOTHING, good()), measured=measured)
        made(Item(close(LAST), "data"), Reports())
        again = made(Item(close(LAST), "data"), Reports())
        assert (again.halt is not None) is measured
        (reason,) = alarms.reasons
        assert reason.endswith(BOUND if measured else ONLY)


@pytest.mark.parametrize("measured", [True, False, None])
def test_8_the_entry_point_hands_measured_to_the_read_and_to_a_redelivery(
    lake: Path, tmp_path: Path, measured: bool | None
) -> None:
    class Unmeasured(Reads):
        def reading(self) -> ClockReading:
            return NOTHING

    handler = data_handler(
        bucket=FolderBucket(lake),
        live=FolderBucket(lake.with_name("no-live")),
        mirror=tmp_path / "mirror",
        storage=loop_storage(Folder(tmp_path / "store", tmp_path / "queues").store(), Alarms()),
        clock=Unmeasured(),
        timer=SystemTimer(),
        measured=measured,  # type: ignore[arg-type]
        deployment=DEPLOYMENT,
        run=RUN,
        release="r1",
        table=Table(stages=(DATA_ROW,), strategies=(chan(),)),
    )
    first = handler(Item(close(LAST), "data"), Reports())
    again = handler(Item(close(LAST), "data"), Reports())
    assert len(again.outputs) == 2
    # An absent one halts as a run that measures does.
    if measured is not False:
        assert first.halt.reason == "no time server was asked"
        assert again.halt.reason == f"{key()}, which counts: no time server was asked"
    else:
        assert first.halt is None and again.halt is None


BOUND = "These differ: whether the clock was past a bound"
ONLY = "Only the read time or the clock's reading differs, as on a redelivery after a crash"


@pytest.mark.parametrize(
    ("first", "later", "said"),
    [
        (good(), two("near", offset_us=1), ONLY),
        (good(), slow(), BOUND),
        (slow(), good(), BOUND),
        (slow(), OLD, ONLY),
    ],
)
def test_8_differences_names_a_clock_past_a_bound(first, later, said: str) -> None:
    assert differences(*(_capture(c) for c in (first, later)), measured=True) == said


def _capture(clock: ClockReading, status=()) -> bytes:
    header = BlobHeader(
        3, DEPLOYMENT, RUN, "data", "capturing", close(LAST), {}, "r1", {"chan": "c" * 64}
    )
    snap = Snapshot(close(LAST), {}, {}, dict(status), False, {}, (), {}, {}, (), None, clock)
    return encode_output(Snapshot, OutputFile(header, (snap,), False))


# 9. Through the receive loop, in a backtest.


class Opened(socket.socket):
    """Notes each datagram socket, because the sampler swallows whatever an ask raises."""

    datagrams: list[int] = []

    def __init__(self, family=-1, type=-1, proto=-1, fileno=None) -> None:
        super().__init__(family, type, proto, fileno)
        if self.type == socket.SOCK_DGRAM:
            self.datagrams.append(self.fileno())


def test_9_a_backtest_halts_on_a_recorded_fault_through_the_loop(
    lake: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(Opened, "datagrams", [])
    monkeypatch.setattr(socket, "socket", Opened)
    strategy = chan()
    table = Table(stages=(DATA_ROW,), strategies=(strategy,))
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    alarms = Alarms()
    store = loop_storage(folder.store(), alarms)
    ok, bad, lacking = close(before(LAST, 2)), close(before(LAST)), close(LAST)
    handler = data_handler(
        bucket=FolderBucket(lake),
        live=FolderBucket(lake.with_name("no-live")),
        mirror=tmp_path / "mirror",
        storage=store,
        clock=HistoryClock(readings={ok: good(ok), bad: slow(bad)}),
        timer=SystemTimer(),
        measured=True,
        deployment=DEPLOYMENT,
        run=RUN,
        release="r1",
        table=table,
    )
    plan = Plan(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        mode=Mode.BACKTEST,
        release="r1",
        decisions=tuple(PlanDecision(at, (strategy.name,)) for at in (ok, bad, lacking)),
    )
    made = ReceiveLoop(
        worker="data-1",
        component="data",
        table=table,
        plan=plan,
        queue=folder.queue("data"),
        storage=store,
        bucket=folder.bucket,
        handlers={"data": handler},
        alerts=alarms,
        timer=SystemTimer(),
        beat=lambda: None,
        tick=0.05,
        stop_timeout=5.0,
    )
    keys = {at: result_key(DEPLOYMENT, RUN, at, "data") for at in (ok, bad, lacking)}
    with running({"data": made}) as started:
        assert started.wait_for(lambda: all((folder.root / k).exists() for k in keys.values()), 20)
    results = {at: store.read_result(k, at) for at, k in keys.items()}
    assert results[ok].halt is None
    assert results[bad].halt.reason == SLOW and len(results[bad].outputs) == 1
    assert "no reading of the clock is recorded" in results[lacking].halt.reason
    assert results[lacking].outputs == ()
    assert Opened.datagrams == []
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).close()
    assert len(Opened.datagrams) == 1
