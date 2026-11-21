"""The storage contract in `core`: the rule for each kind of stage, the outage, and the reader.

Each test runs against the folder and against S3 on moto, through the `spec`
fixture. A store wrapped in `Flaky` stops answering or loses a reply on
demand, which is how the outage and the lost reply are shown.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from ithildinruntime import core
from ithildinruntime.core import (
    BlobHeader,
    BlobRef,
    DecisionResult,
    Halt,
    Instrument,
    InstrumentId,
    OutputFile,
    Retry,
    StageKind,
    Storage,
    Stored,
    StoreUnavailable,
    decode_output,
    encode_output,
    sha256_of,
)
from tests.support.stores import StoreSpec

AT = datetime(2026, 11, 2, 20, 40, tzinfo=UTC)
RUN = "2026-11-02"
RETRY = Retry(deadline=timedelta(seconds=30), pause=1.0, longest_pause=8.0, attempts=100)
OWNER_STEP = "check the broker's console for open orders"


class FakeTimer:
    def __init__(self) -> None:
        self.at = AT
        self.pauses: list[float] = []

    def now(self) -> datetime:
        return self.at

    def after(self, seconds: float) -> object:
        raise NotImplementedError

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.at += timedelta(seconds=seconds)


class Alarms:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def alarm(self, reason: str) -> None:
        self.reasons.append(reason)

    def heartbeat(self) -> None:
        pass


class Flaky:
    """A store that answers nothing while `down`, and loses the next create's reply on `lose`."""

    def __init__(self, store) -> None:
        self.store = store
        self.down = False
        self.lose = False

    def _check(self, key: str) -> None:
        if self.down:
            raise StoreUnavailable(key, "503 SlowDown: the endpoint is blocked")

    def create(self, key, data, sha256):
        self._check(key)
        tag = self.store.create(key, data, sha256)
        if self.lose:
            self.lose = False
            raise StoreUnavailable(key, "no answer: the connection closed")
        return tag

    def get(self, key):
        self._check(key)
        return self.store.get(key)

    def head(self, key):
        self._check(key)
        return self.store.head(key)

    def list(self, prefix, after):
        self._check(prefix)
        return self.store.list(prefix, after)

    def replace_if_match(self, key, data, sha256, etag):
        self._check(key)
        return self.store.replace_if_match(key, data, sha256, etag)


def _storage(store) -> tuple[Storage, FakeTimer, Alarms]:
    timer, alarms = FakeTimer(), Alarms()
    storage = Storage(
        store, timer=timer, pause=timer.pause, retry=RETRY, alerts=alarms, owner_step=OWNER_STEP
    )
    return storage, timer, alarms


def _header(decision_at: datetime = AT, stage: str = "data") -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment="ci",
        run=RUN,
        stage=stage,
        kind="output",
        decision_at=decision_at,
        inputs={},
        release="sha256:release",
        config_hash="0" * 64,
    )


def _snapshot_key() -> str:
    return core.output_key("ci", RUN, AT, "data", "snapshot")


def _attempt():
    return partial(core.attempt_key, "ci", RUN, AT, "data", "snapshot")


def _output(entries, trade_nothing=False, decision_at=AT) -> bytes:
    return encode_output(Instrument, OutputFile(_header(decision_at), entries, trade_nothing))


USO = Instrument(InstrumentId("USO"), "USO")
GLD = Instrument(InstrumentId("GLD"), "GLD")


# A deterministic stage


def test_a_deterministic_rewrite_of_the_same_bytes_is_done(spec: StoreSpec) -> None:
    storage, _, alarms = _storage(spec.open())
    key = core.output_key("ci", RUN, AT, "alpha", "forecasts")
    first = storage.put(StageKind.DETERMINISTIC, key, b"forecasts")
    again = storage.put(StageKind.DETERMINISTIC, key, b"forecasts")
    assert first == again == Stored(BlobRef(key, sha256_of(b"forecasts")), first.ref)
    assert alarms.reasons == []


def test_a_step_that_puts_the_clock_time_in_its_output_halts(spec: StoreSpec) -> None:
    storage, timer, _ = _storage(spec.open())
    key = core.output_key("ci", RUN, AT, "alpha", "forecasts")

    def step() -> bytes:
        return f"forecast written at {timer.now().isoformat()}".encode()

    one = step()
    storage.put(StageKind.DETERMINISTIC, key, one)
    timer.pause(1)
    two = step()
    with pytest.raises(Halt) as halted:
        storage.put(StageKind.DETERMINISTIC, key, two)
    reason = halted.value.reason
    assert key in reason and sha256_of(one) in reason and sha256_of(two) in reason
    assert "not deterministic" in reason
    assert storage.unstored() == ()


# A capturing stage


def test_a_later_capture_that_differs_is_stored_at_its_attempt_key(spec: StoreSpec) -> None:
    storage, _, alarms = _storage(spec.open())
    key = _snapshot_key()
    first = storage.put(StageKind.CAPTURING, key, b"read one", attempt=_attempt())
    assert first == Stored(
        BlobRef(key, sha256_of(b"read one")), BlobRef(key, sha256_of(b"read one"))
    )
    second = storage.put(StageKind.CAPTURING, key, b"read two", attempt=_attempt())
    attempt_2 = core.attempt_key("ci", RUN, AT, "data", "snapshot", 2)
    assert second == Stored(BlobRef(attempt_2, sha256_of(b"read two")), first.ref)
    assert len(alarms.reasons) == 1
    reason = alarms.reasons[0]
    assert key in reason and attempt_2 in reason
    assert sha256_of(b"read one") in reason and sha256_of(b"read two") in reason
    assert storage.put(StageKind.CAPTURING, key, b"read two", attempt=_attempt()) == second
    assert len(alarms.reasons) == 1
    third = storage.put(StageKind.CAPTURING, key, b"read three", attempt=_attempt())
    assert third.ref.key == core.attempt_key("ci", RUN, AT, "data", "snapshot", 3)
    assert third.counts == first.ref
    assert spec.open().get(key) == b"read one"


def test_a_capture_at_a_key_of_its_own_that_differs_halts_and_stays_held(spec: StoreSpec) -> None:
    storage, _, _ = _storage(spec.open())
    key = core.broker_read_key("ci", RUN, AT, 1)
    storage.put(StageKind.CAPTURING, key, b"positions one")
    with pytest.raises(Halt) as halted:
        storage.put(StageKind.CAPTURING, key, b"positions two")
    assert sha256_of(b"positions two") in halted.value.reason
    assert storage.unstored() == (BlobRef(key, sha256_of(b"positions two")),)


def test_a_capture_with_no_attempt_key_left_halts_and_stays_held(spec: StoreSpec) -> None:
    timer, alarms = FakeTimer(), Alarms()
    retry = Retry(deadline=timedelta(seconds=30), pause=1.0, longest_pause=8.0, attempts=3)
    storage = Storage(
        spec.open(), timer=timer, pause=timer.pause, retry=retry, alerts=alarms, owner_step="x"
    )
    for n in range(3):
        storage.put(StageKind.CAPTURING, _snapshot_key(), f"read {n}".encode(), attempt=_attempt())
    with pytest.raises(Halt) as halted:
        storage.put(StageKind.CAPTURING, _snapshot_key(), b"read 3", attempt=_attempt())
    assert "no attempt key left" in halted.value.reason
    assert [ref.sha256 for ref in storage.unstored()] == [sha256_of(b"read 3")]


@pytest.mark.parametrize("stored_first", [False, True], ids=["key free", "key taken"])
def test_two_writers_racing_one_capture_both_land_at_distinct_keys(
    spec: StoreSpec, stored_first: bool
) -> None:
    key = _snapshot_key()
    if stored_first:
        _storage(spec.open())[0].put(StageKind.CAPTURING, key, b"read zero", attempt=_attempt())
    start = threading.Barrier(2)
    landed: dict[bytes, Stored] = {}

    def write(data: bytes) -> None:
        storage, _, _ = _storage(spec.open())
        start.wait()
        landed[data] = storage.put(StageKind.CAPTURING, key, data, attempt=_attempt())

    writers = [threading.Thread(target=write, args=(d,)) for d in (b"read one", b"read two")]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    keys = {stored.ref.key for stored in landed.values()}
    assert len(keys) == 2
    store = spec.open()
    for data, stored in landed.items():
        assert store.get(stored.ref.key) == data
    expected = {core.attempt_key("ci", RUN, AT, "data", "snapshot", n) for n in (2, 3)}
    if not stored_first:
        expected = {key, core.attempt_key("ci", RUN, AT, "data", "snapshot", 2)}
    assert keys == expected


# A write S3 refuses or does not answer


def test_a_lost_reply_on_a_write_that_landed_is_done(spec: StoreSpec) -> None:
    flaky = Flaky(spec.open())
    storage, _, alarms = _storage(flaky)
    flaky.lose = True
    key = _snapshot_key()
    stored = storage.put(StageKind.CAPTURING, key, b"read one", attempt=_attempt())
    assert stored.ref == BlobRef(key, sha256_of(b"read one"))
    assert alarms.reasons == [] and storage.unstored() == ()
    flaky.lose = True
    out = core.output_key("ci", RUN, AT, "alpha", "forecasts")
    assert storage.put(StageKind.DETERMINISTIC, out, b"forecasts").ref.key == out


def test_a_halt_over_an_outage_names_each_capture_and_stores_them_once_s3_answers(
    spec: StoreSpec,
) -> None:
    flaky = Flaky(spec.open())
    storage, timer, _ = _storage(flaky)
    flaky.down = True
    snapshot = _snapshot_key()
    read = core.broker_read_key("ci", RUN, AT, 1)

    with pytest.raises(Halt) as first:
        storage.put(StageKind.CAPTURING, snapshot, b"snapshot", attempt=_attempt())
    assert timer.at - AT >= RETRY.deadline
    assert timer.pauses[:4] == [1.0, 2.0, 4.0, 8.0] and max(timer.pauses) == 8.0
    assert snapshot in first.value.reason and sha256_of(b"snapshot") in first.value.reason
    assert "503 SlowDown" in first.value.reason and OWNER_STEP in first.value.reason

    with pytest.raises(Halt) as second:
        storage.put(StageKind.CAPTURING, read, b"broker read")
    for key, data in ((snapshot, b"snapshot"), (read, b"broker read")):
        assert key in second.value.reason and sha256_of(data) in second.value.reason

    held = (BlobRef(snapshot, sha256_of(b"snapshot")), BlobRef(read, sha256_of(b"broker read")))
    record = storage.halt_record(second.value)
    assert record.unstored == held and record.reason == second.value.reason
    assert core.decode(core.encode(record), core.HaltRecord) == record

    out = core.output_key("ci", RUN, AT, "alpha", "forecasts")
    with pytest.raises(Halt) as deterministic:
        storage.put(StageKind.DETERMINISTIC, out, b"forecasts")
    assert snapshot in deterministic.value.reason
    assert storage.unstored() == held

    assert storage.retry_held() == ()
    assert storage.unstored() == held

    flaky.down = False
    stored = storage.retry_held()
    assert {s.ref for s in stored} == set(held)
    assert storage.unstored() == ()
    assert flaky.get(snapshot) == b"snapshot" and flaky.get(read) == b"broker read"


def test_a_held_capture_that_differs_from_the_stored_one_lands_at_its_attempt_key(
    spec: StoreSpec,
) -> None:
    flaky = Flaky(spec.open())
    storage, _, alarms = _storage(flaky)
    key = _snapshot_key()
    storage.put(StageKind.CAPTURING, key, b"read one", attempt=_attempt())
    flaky.down = True
    with pytest.raises(Halt):
        storage.put(StageKind.CAPTURING, key, b"read two", attempt=_attempt())
    flaky.down = False
    (stored,) = storage.retry_held()
    assert stored.ref.key == core.attempt_key("ci", RUN, AT, "data", "snapshot", 2)
    assert stored.counts == BlobRef(key, sha256_of(b"read one"))
    assert len(alarms.reasons) == 1


def test_a_decision_result_is_created_once_and_the_stored_one_is_returned(
    spec: StoreSpec,
) -> None:
    storage, _, _ = _storage(spec.open())
    key = core.result_key("ci", RUN, AT, "alpha")
    ours = DecisionResult(_header(stage="alpha"), (BlobRef("a", "0" * 64),), None)
    assert storage.put_result(key, ours) == ours
    assert storage.put_result(key, ours) == ours
    halt = core.HaltRecord("the supervisor halted the decision", None, (), (), ())
    theirs = DecisionResult(_header(stage="alpha"), (), halt)
    assert storage.put_result(key, theirs) == ours
    assert storage.read_result(key, AT) == ours


# The reader's checks


def _listed(spec: StoreSpec, data: bytes) -> tuple[Storage, str, BlobRef]:
    storage, _, _ = _storage(spec.open())
    key = core.output_key("ci", RUN, AT, "data", "instruments")
    ref = storage.put(StageKind.DETERMINISTIC, key, data).ref
    result = DecisionResult(_header(), (ref,), None)
    result_key = core.result_key("ci", RUN, AT, "data")
    storage.put_result(result_key, result)
    return storage, result_key, ref


def test_a_reader_reads_a_listed_output_file(spec: StoreSpec) -> None:
    storage, result_key, ref = _listed(spec, _output([USO, GLD]))
    (listed,) = storage.read_result(result_key, AT).outputs
    assert storage.read_output(result_key, listed, AT, Instrument).entries == (USO, GLD)


def test_a_reader_halts_on_a_changed_byte_in_an_output_file(spec: StoreSpec) -> None:
    data = _output([USO])
    storage, result_key, ref = _listed(spec, data)
    spec.overwrite(ref.key, data.replace(b"USO", b"USP"))
    with pytest.raises(Halt) as halted:
        storage.read_output(result_key, ref, AT, Instrument)
    assert ref.key in halted.value.reason and result_key in halted.value.reason
    assert ref.sha256 in halted.value.reason


def test_a_reader_halts_on_a_listed_output_file_that_is_missing(spec: StoreSpec) -> None:
    storage, result_key, ref = _listed(spec, _output([USO]))
    spec.remove(ref.key)
    with pytest.raises(Halt, match="is missing"):
        storage.read_output(result_key, ref, AT, Instrument)


def test_a_reader_halts_on_a_blob_for_the_wrong_decision(spec: StoreSpec) -> None:
    earlier = AT - timedelta(days=1)
    storage, result_key, ref = _listed(spec, _output([USO], decision_at=earlier))
    with pytest.raises(Halt, match="stale") as halted:
        storage.read_output(result_key, ref, AT, Instrument)
    assert ref.key in halted.value.reason
    wrong = DecisionResult(_header(earlier), (), None)
    wrong_key = core.result_key("ci", RUN, AT, "risk")
    spec.open().create(wrong_key, core.encode(wrong), sha256_of(core.encode(wrong)))
    with pytest.raises(Halt, match="stale"):
        storage.read_result(wrong_key, AT)


def test_a_reader_halts_on_an_output_file_with_no_entries_and_no_mark(spec: StoreSpec) -> None:
    storage, result_key, ref = _listed(spec, _output([]))
    with pytest.raises(Halt, match='no entries and no "trade nothing" mark'):
        storage.read_output(result_key, ref, AT, Instrument)


def test_a_trade_nothing_mark_reads_as_hold_and_holds_no_entries(spec: StoreSpec) -> None:
    storage, result_key, ref = _listed(spec, _output([], trade_nothing=True))
    read = storage.read_output(result_key, ref, AT, Instrument)
    assert read.trade_nothing and read.entries == ()
    with pytest.raises(ValueError):
        OutputFile(_header(), (USO,), True)


def test_a_reader_has_no_result_to_read_before_the_stage_writes_one(spec: StoreSpec) -> None:
    storage, _, _ = _storage(spec.open())
    assert storage.read_result(core.result_key("ci", RUN, AT, "alpha"), AT) is None


def test_a_reader_halts_when_s3_does_not_answer_a_read(spec: StoreSpec) -> None:
    flaky = Flaky(spec.open())
    storage, _, _ = _storage(flaky)
    flaky.down = True
    with pytest.raises(Halt, match="did not answer a read"):
        storage.read_result(core.result_key("ci", RUN, AT, "alpha"), AT)


# The output file's form


def test_an_output_file_has_a_header_and_a_body_holding_entries_and_the_mark() -> None:
    data = _output([USO])
    assert data.startswith(
        b'{"body":{"entries":[{"id":"USO","symbol":"USO"}],"trade_nothing":false}'
    )
    assert b'"header":{' in data
    assert decode_output(data, Instrument) == OutputFile(_header(), (USO,), False)
    bad = data.replace(b'"trade_nothing":false', b'"trade_nothing":true')
    with pytest.raises(core.CodecError):
        decode_output(bad, Instrument)


def test_a_retry_refuses_a_deadline_or_pauses_that_cannot_work() -> None:
    for bad in (
        dict(deadline=timedelta(0), pause=1.0, longest_pause=2.0, attempts=5),
        dict(deadline=timedelta(seconds=1), pause=0.0, longest_pause=2.0, attempts=5),
        dict(deadline=timedelta(seconds=1), pause=3.0, longest_pause=2.0, attempts=5),
        dict(deadline=timedelta(seconds=1), pause=1.0, longest_pause=2.0, attempts=1),
    ):
        with pytest.raises(ValueError):
            Retry(**bad)


def test_a_deterministic_output_takes_no_attempt_keys(spec: StoreSpec) -> None:
    storage, _, _ = _storage(spec.open())
    with pytest.raises(ValueError):
        storage.put(StageKind.DETERMINISTIC, _snapshot_key(), b"x", attempt=_attempt())
