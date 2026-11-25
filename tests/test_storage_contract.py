"""The storage contract in `core`: the rule for each kind of stage, the outage, and the reader.

Each test runs against the folder and against S3 on moto, through the `spec`
fixture. A store wrapped in `Flaky` stops answering or loses a reply on
demand, which is how the outage and the lost reply are shown.
"""

from __future__ import annotations

import os
import threading
from datetime import UTC, datetime, timedelta
from functools import partial

import pytest

from ithildinruntime import core
from ithildinruntime.core import (
    BlobHeader,
    BlobMissing,
    BlobRef,
    ChecksumMismatch,
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
RETRY = Retry(deadline=timedelta(seconds=30), pause=1.0, longest_pause=8.0)
OWNER_STEP = "check the broker's console for open orders"


class FakeTimer:
    def __init__(self) -> None:
        self.at = AT
        self.elapsed = 0.0
        self.pauses: list[float] = []

    def now(self) -> datetime:
        return self.at

    def monotonic(self) -> float:
        return self.elapsed

    def after(self, seconds: float) -> object:
        raise NotImplementedError

    def pause(self, seconds: float) -> None:
        self.pauses.append(seconds)
        self.at += timedelta(seconds=seconds)
        self.elapsed += seconds


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
        self.down_at: str | None = None
        self.lose = False
        self.corrupt = 0
        self.vanish = False
        self.broken: Exception | None = None

    def _check(self, key: str) -> None:
        if self.down or key == self.down_at:
            raise StoreUnavailable(key, "503 SlowDown: the endpoint is blocked")

    def create(self, key, data, sha256):
        self._check(key)
        if self.broken is not None:
            raise self.broken
        if self.corrupt:
            self.corrupt -= 1
            raise ChecksumMismatch(key, "400 BadDigest: the bytes changed on the way")
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
        if self.vanish:
            raise BlobMissing(key, "404 Not Found")
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
        config_hashes={"chan": "0" * 64},
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
    # A put that finds its capture already at the attempt key alarms again,
    # because it cannot tell that from a try whose reply was lost.
    assert storage.put(StageKind.CAPTURING, key, b"read two", attempt=_attempt()) == second
    assert len(alarms.reasons) == 2 and alarms.reasons[1] == reason
    third = storage.put(StageKind.CAPTURING, key, b"read three", attempt=_attempt())
    assert third.ref.key == core.attempt_key("ci", RUN, AT, "data", "snapshot", 3)
    assert third.counts == first.ref
    assert spec.open().get(key) == b"read one"


def test_explain_is_refused_on_a_deterministic_output(spec) -> None:
    storage, _, _ = _storage(spec.open())
    with pytest.raises(ValueError):
        storage.put(StageKind.DETERMINISTIC, _snapshot_key(), b"one", explain=lambda first: "x")


@pytest.mark.parametrize("error", [RuntimeError, SystemExit])
def test_an_explain_that_raises_leaves_the_alarm_saying_so(spec, error) -> None:
    storage, _, alarms = _storage(spec.open())
    key = _snapshot_key()
    storage.put(StageKind.CAPTURING, key, b"read one", attempt=_attempt())

    def explain(first: bytes) -> str:
        raise error("the codec refused it")

    storage.put(StageKind.CAPTURING, key, b"read two", attempt=_attempt(), explain=explain)
    (reason,) = alarms.reasons
    assert reason.endswith(
        f"The difference could not be described: {error.__name__}: the codec refused it"
    )
    assert storage.unstored() == ()


def test_an_attempts_alarm_says_when_the_first_capture_reads_as_missing(spec) -> None:
    flaky = Flaky(spec.open())
    storage, _, alarms = _storage(flaky)
    key = _snapshot_key()
    storage.put(StageKind.CAPTURING, key, b"read one", attempt=_attempt())
    real = flaky.get
    flaky.get = lambda read: None if read == key else real(read)
    storage.put(StageKind.CAPTURING, key, b"read two", attempt=_attempt(), explain=lambda f: "x")
    (reason,) = alarms.reasons
    assert reason.endswith(f"{key} read as missing, so the difference is not known")


def test_an_attempts_alarm_says_when_the_first_capture_cannot_be_read(spec) -> None:
    """The description reads once, so it sends no outage alarm and waits out no deadline."""
    flaky = Flaky(spec.open())
    storage, timer, alarms = _storage(flaky)
    key = _snapshot_key()
    storage.put(StageKind.CAPTURING, key, b"read one", attempt=_attempt())
    real = flaky.get

    def get(read):
        if read == key:
            raise StoreUnavailable(read, "503 SlowDown: the endpoint is blocked")
        return real(read)

    flaky.get = get
    stored = storage.put(
        StageKind.CAPTURING, key, b"read two", attempt=_attempt(), explain=lambda first: "x"
    )
    assert stored.ref.key == _attempt()(2)
    assert timer.elapsed == 0
    (reason,) = alarms.reasons
    assert "The difference could not be described: StoreUnavailable" in reason


def test_a_capture_at_a_key_of_its_own_that_differs_halts_and_stays_held(spec: StoreSpec) -> None:
    storage, _, _ = _storage(spec.open())
    key = core.broker_read_key("ci", RUN, AT, 1)
    storage.put(StageKind.CAPTURING, key, b"positions one")
    with pytest.raises(Halt) as halted:
        storage.put(StageKind.CAPTURING, key, b"positions two")
    assert sha256_of(b"positions two") in halted.value.reason
    assert storage.unstored() == (BlobRef(key, sha256_of(b"positions two")),)


def test_an_attempt_key_that_repeats_halts_and_the_capture_stays_held(spec: StoreSpec) -> None:
    """Attempt keys are unbounded, as Rule 2 says, and a builder that repeats one halts."""
    storage, _, _ = _storage(spec.open())
    for n in range(12):
        stored = storage.put(
            StageKind.CAPTURING, _snapshot_key(), f"read {n}".encode(), attempt=_attempt()
        )
    assert stored.ref.key == core.attempt_key("ci", RUN, AT, "data", "snapshot", 12)

    def repeat(n: int) -> str:
        # Bounded, so a contract that never checks for a repeat fails here
        # rather than walking forever.
        assert n < 50, "the attempt walk never noticed the repeated key"
        return _snapshot_key()

    with pytest.raises(Halt, match="repeats") as halted:
        storage.put(StageKind.CAPTURING, _snapshot_key(), b"read x", attempt=repeat)
    assert sha256_of(b"read x") in halted.value.reason
    assert [ref.sha256 for ref in storage.unstored()] == [sha256_of(b"read x")]


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
    storage, timer, alarms = _storage(flaky)
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

    # No halt blob reaches monitoring while S3 is down, so each halt alarms itself.
    assert alarms.reasons == [first.value.reason, second.value.reason, deterministic.value.reason]

    pauses = len(timer.pauses)
    assert storage.retry_held() == ()
    assert len(timer.pauses) == pauses
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
    assert len(alarms.reasons) == 2 and "stored at" in alarms.reasons[1]


def test_a_capture_held_at_its_attempt_key_is_named_by_that_key(spec: StoreSpec) -> None:
    flaky = Flaky(spec.open())
    storage, _, _ = _storage(flaky)
    key = _snapshot_key()
    attempt_2 = core.attempt_key("ci", RUN, AT, "data", "snapshot", 2)
    storage.put(StageKind.CAPTURING, key, b"read one", attempt=_attempt())
    flaky.down_at = attempt_2
    with pytest.raises(Halt) as halted:
        storage.put(StageKind.CAPTURING, key, b"read two", attempt=_attempt())
    assert storage.unstored() == (BlobRef(attempt_2, sha256_of(b"read two")),)
    assert attempt_2 in halted.value.reason


def test_a_lost_reply_at_an_attempt_key_still_raises_its_alarm(spec: StoreSpec) -> None:
    flaky = Flaky(spec.open())
    storage, _, alarms = _storage(flaky)
    key = _snapshot_key()
    storage.put(StageKind.CAPTURING, key, b"read one", attempt=_attempt())
    flaky.lose = True
    stored = storage.put(StageKind.CAPTURING, key, b"read two", attempt=_attempt())
    attempt_2 = core.attempt_key("ci", RUN, AT, "data", "snapshot", 2)
    assert stored.ref.key == attempt_2
    assert len(alarms.reasons) == 1 and attempt_2 in alarms.reasons[0]


def test_a_checksum_refusal_is_retried_and_its_capture_held_past_the_deadline(
    spec: StoreSpec,
) -> None:
    flaky = Flaky(spec.open())
    storage, _, _ = _storage(flaky)
    key = _snapshot_key()
    flaky.corrupt = 2
    assert storage.put(StageKind.CAPTURING, key, b"one", attempt=_attempt()).ref.key == key
    flaky.corrupt = 10**6
    other = core.broker_read_key("ci", RUN, AT, 1)
    with pytest.raises(Halt) as halted:
        storage.put(StageKind.CAPTURING, other, b"two")
    assert "BadDigest" in halted.value.reason and other in halted.value.reason
    assert storage.unstored() == (BlobRef(other, sha256_of(b"two")),)
    flaky.corrupt = 0
    assert [s.ref.key for s in storage.retry_held()] == [other]


def test_a_key_refused_as_existing_then_not_found_is_retried_not_dropped(spec: StoreSpec) -> None:
    flaky = Flaky(spec.open())
    storage, _, _ = _storage(flaky)
    key = _snapshot_key()
    storage.put(StageKind.CAPTURING, key, b"one", attempt=_attempt())
    flaky.vanish = True
    with pytest.raises(Halt, match="refused as existing"):
        storage.put(StageKind.CAPTURING, key, b"two", attempt=_attempt())
    assert storage.unstored() == (BlobRef(key, sha256_of(b"two")),)
    flaky.vanish = False
    (stored,) = storage.retry_held()
    assert stored.ref.key == core.attempt_key("ci", RUN, AT, "data", "snapshot", 2)


def test_an_error_no_refusal_names_still_holds_the_capture_and_halts(spec: StoreSpec) -> None:
    flaky = Flaky(spec.open())
    storage, _, _ = _storage(flaky)
    flaky.broken = OSError(28, "No space left on device")
    key = _snapshot_key()
    with pytest.raises(Halt, match="No space left") as halted:
        storage.put(StageKind.CAPTURING, key, b"one", attempt=_attempt())
    assert sha256_of(b"one") in halted.value.reason
    assert storage.unstored() == (BlobRef(key, sha256_of(b"one")),)
    assert storage.retry_held() == ()
    flaky.broken = None
    assert [s.ref.key for s in storage.retry_held()] == [key]
    flaky.broken = OSError(5, "Input/output error")
    with pytest.raises(OSError):
        storage.put(StageKind.DETERMINISTIC, core.output_key("ci", RUN, AT, "alpha", "f"), b"f")


def test_a_capture_is_held_when_its_write_ends_on_a_base_exception(spec: StoreSpec) -> None:
    """The error is raised on, and the capture is stored once the store answers."""
    flaky = Flaky(spec.open())
    storage, _, _ = _storage(flaky)
    flaky.broken = SystemExit("out")
    key = _snapshot_key()
    with pytest.raises(SystemExit):
        storage.put(StageKind.CAPTURING, key, b"one", attempt=_attempt())
    assert storage.unstored() == (BlobRef(key, sha256_of(b"one")),)
    with pytest.raises(SystemExit):
        storage.put(StageKind.DETERMINISTIC, core.output_key("ci", RUN, AT, "alpha", "f"), b"f")
    assert len(storage.unstored()) == 1
    flaky.broken = None
    assert [s.ref.key for s in storage.retry_held()] == [key]


def test_a_store_that_refuses_every_write_halts_and_holds_the_capture(spec: StoreSpec) -> None:
    """A bucket that does not exist, or a folder nobody can write, is an outage, never a gap."""
    if spec.kind == "folder":
        store = spec.open()
        incoming = os.path.join(spec.root, ".incoming")
        os.chmod(incoming, 0o500)
    else:
        store = StoreSpec("s3", endpoint=spec.endpoint, bucket="no-such-bucket").open()
    storage, _, alarms = _storage(store)
    key = _snapshot_key()
    try:
        with pytest.raises(Halt, match="did not store") as halted:
            storage.put(StageKind.CAPTURING, key, b"one", attempt=_attempt())
    finally:
        if spec.kind == "folder":
            os.chmod(incoming, 0o700)
    assert storage.unstored() == (BlobRef(key, sha256_of(b"one")),)
    assert alarms.reasons == [halted.value.reason]
    if spec.kind == "s3":
        with pytest.raises(Halt, match="did not answer a read"):
            storage.read_result(core.result_key("ci", RUN, AT, "data"), AT)


def test_two_handlers_retrying_at_once_store_each_capture_once(spec: StoreSpec) -> None:
    flaky = Flaky(spec.open())
    storage, _, _ = _storage(flaky)
    flaky.down = True
    keys = [core.broker_read_key("ci", RUN, AT, n) for n in range(1, 9)]
    for n, key in enumerate(keys):
        with pytest.raises(Halt):
            storage.put(StageKind.CAPTURING, key, f"read {n}".encode())
    flaky.down = False
    start = threading.Barrier(2)
    results: list = []
    errors: list = []

    def retry() -> None:
        start.wait()
        try:
            results.extend(storage.retry_held())
        except Exception as error:  # noqa: BLE001 - the test reports any error
            errors.append(error)

    handlers = [threading.Thread(target=retry) for _ in range(2)]
    for handler in handlers:
        handler.start()
    for handler in handlers:
        handler.join()
    assert errors == []
    assert sorted(s.ref.key for s in results) == sorted(keys)
    assert storage.unstored() == ()


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


class UnreadableKey(Flaky):
    """Takes every create, and answers a read of a key it holds as `read` says."""

    def __init__(self, store, read: Exception) -> None:
        super().__init__(store)
        self.read = read

    def get(self, key):
        raise self.read


@pytest.mark.parametrize(
    "read",
    [StoreUnavailable("k", "503 SlowDown"), BlobMissing("k", "404 Not Found")],
    ids=["no-answer", "missing"],
)
def test_a_result_tried_once_whose_key_cannot_be_read_is_unavailable_and_quiet(
    spec: StoreSpec, read: Exception
) -> None:
    """A process retrying a halt it holds keeps it, and the first try's alarm stands alone."""
    key = core.result_key("ci", RUN, AT, "alpha")
    base = spec.open()
    ours = DecisionResult(_header(stage="alpha"), (BlobRef("a", "0" * 64),), None)
    _storage(base)[0].put_result(key, ours)
    storage, timer, alarms = _storage(UnreadableKey(base, read))
    halt = core.HaltRecord("the supervisor halted the decision", None, (), (), ())
    began = timer.monotonic()
    with pytest.raises(StoreUnavailable):
        storage.put_result(key, DecisionResult(_header(stage="alpha"), (), halt), once=True)
    # One try each, with no pause between them.
    assert alarms.reasons == [] and timer.monotonic() == began
    with pytest.raises(Halt):
        storage.put_result(key, DecisionResult(_header(stage="alpha"), (), halt))


# The reader's checks


def _listed(spec: StoreSpec, data: bytes) -> tuple[Storage, str, BlobRef]:
    storage, _, _ = _storage(spec.open())
    key = core.output_key("ci", RUN, AT, "data", "instruments")
    ref = storage.put(StageKind.DETERMINISTIC, key, data).ref
    result = DecisionResult(_header(), (ref,), None)
    result_key = core.result_key("ci", RUN, AT, "data")
    storage.put_result(result_key, result)
    return storage, result_key, ref


def _read(storage: Storage, result_key: str, ref: BlobRef):
    return storage.read_output(result_key, storage.read_result(result_key, AT), ref, AT, Instrument)


def test_a_reader_reads_a_listed_output_file(spec: StoreSpec) -> None:
    storage, result_key, ref = _listed(spec, _output([USO, GLD]))
    result = storage.read_result(result_key, AT)
    (listed,) = result.outputs
    assert storage.read_output(result_key, result, listed, AT, Instrument).entries == (USO, GLD)


def test_a_reader_halts_on_a_changed_byte_in_an_output_file(spec: StoreSpec) -> None:
    data = _output([USO])
    storage, result_key, ref = _listed(spec, data)
    spec.overwrite(ref.key, data.replace(b"USO", b"USP"))
    with pytest.raises(Halt) as halted:
        _read(storage, result_key, ref)
    assert ref.key in halted.value.reason and result_key in halted.value.reason
    assert ref.sha256 in halted.value.reason


def test_a_reader_halts_on_a_listed_output_file_that_is_missing(spec: StoreSpec) -> None:
    storage, result_key, ref = _listed(spec, _output([USO]))
    spec.remove(ref.key)
    with pytest.raises(Halt, match="is missing"):
        _read(storage, result_key, ref)


def test_a_reader_halts_on_a_blob_for_the_wrong_decision(spec: StoreSpec) -> None:
    earlier = AT - timedelta(days=1)
    storage, result_key, ref = _listed(spec, _output([USO], decision_at=earlier))
    with pytest.raises(Halt, match="stale") as halted:
        _read(storage, result_key, ref)
    assert ref.key in halted.value.reason
    wrong = DecisionResult(_header(earlier), (), None)
    wrong_key = core.result_key("ci", RUN, AT, "risk")
    spec.open().create(wrong_key, core.encode(wrong), sha256_of(core.encode(wrong)))
    with pytest.raises(Halt, match="stale"):
        storage.read_result(wrong_key, AT)


def test_a_reader_halts_on_an_output_file_with_no_entries_and_no_mark(spec: StoreSpec) -> None:
    storage, result_key, ref = _listed(spec, _output([]))
    with pytest.raises(Halt, match='no entries and no "trade nothing" mark'):
        _read(storage, result_key, ref)


def test_a_trade_nothing_mark_reads_as_hold_and_holds_no_entries(spec: StoreSpec) -> None:
    storage, result_key, ref = _listed(spec, _output([], trade_nothing=True))
    read = _read(storage, result_key, ref)
    assert read.trade_nothing and read.entries == ()
    with pytest.raises(ValueError):
        OutputFile(_header(), (USO,), True)


def test_a_reader_halts_on_a_result_that_holds_a_halt_or_does_not_list_the_file(
    spec: StoreSpec,
) -> None:
    storage, result_key, ref = _listed(spec, _output([USO]))
    halted = DecisionResult(_header(), (ref,), core.HaltRecord("USO stale", None, (), (), ()))
    with pytest.raises(Halt, match="holds a halt: USO stale"):
        storage.read_output(result_key, halted, ref, AT, Instrument)
    other = BlobRef(ref.key.replace("instruments", "other"), ref.sha256)
    with pytest.raises(Halt, match="is not an output file"):
        storage.read_output(result_key, storage.read_result(result_key, AT), other, AT, Instrument)


def test_a_reader_has_no_result_to_read_before_the_stage_writes_one(spec: StoreSpec) -> None:
    storage, _, _ = _storage(spec.open())
    assert storage.read_result(core.result_key("ci", RUN, AT, "alpha"), AT) is None


def test_a_blob_with_a_reader_of_its_own_is_read_or_halts(spec: StoreSpec) -> None:
    flaky = Flaky(spec.open())
    storage, _, alarms = _storage(flaky)
    key = core.caps_key("paper1")
    with pytest.raises(Halt, match="caps.json is missing"):
        storage.read_blob(key)
    assert alarms.reasons == []
    flaky.create(key, b"{ }", sha256_of(b"{ }"))
    assert storage.read_blob(key) == b"{ }"
    flaky.down = True
    with pytest.raises(Halt, match="S3 did not answer a read of accounts/paper1/caps.json"):
        storage.read_blob(key)
    assert len(alarms.reasons) == 1


def test_a_reader_halts_when_s3_does_not_answer_a_read(spec: StoreSpec) -> None:
    flaky = Flaky(spec.open())
    storage, _, alarms = _storage(flaky)
    flaky.down = True
    snapshot = _snapshot_key()
    with pytest.raises(Halt):
        storage.put(StageKind.CAPTURING, snapshot, b"snapshot", attempt=_attempt())
    with pytest.raises(Halt, match="did not answer a read") as halted:
        storage.read_result(core.result_key("ci", RUN, AT, "alpha"), AT)
    reason = halted.value.reason
    assert snapshot in reason and sha256_of(b"snapshot") in reason and OWNER_STEP in reason
    assert alarms.reasons[-1] == reason and len(alarms.reasons) == 2


def test_a_reader_halt_names_each_capture_still_held(spec: StoreSpec) -> None:
    storage, result_key, ref = _listed(spec, _output([USO], decision_at=AT - timedelta(days=1)))
    flaky = Flaky(spec.open())
    holding, _, _ = _storage(flaky)
    flaky.down = True
    with pytest.raises(Halt):
        holding.put(StageKind.CAPTURING, _snapshot_key(), b"snapshot", attempt=_attempt())
    flaky.down = False
    with pytest.raises(Halt, match="stale") as halted:
        holding.read_output(result_key, holding.read_result(result_key, AT), ref, AT, Instrument)
    assert _snapshot_key() in halted.value.reason
    assert sha256_of(b"snapshot") in halted.value.reason


class FailingAlarms(Alarms):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    def alarm(self, reason: str) -> None:
        super().alarm(reason)
        raise self.error


PAGER_ERRORS = [ConnectionError("the pager is unreachable"), SystemExit("the pager is unreachable")]


@pytest.mark.parametrize("error", PAGER_ERRORS, ids=lambda error: type(error).__name__)
def test_an_alarm_that_cannot_be_sent_leaves_a_true_halt(
    spec: StoreSpec, error: BaseException
) -> None:
    """A stored capture is never named as unstored, and an outage halt keeps its reason."""
    flaky = Flaky(spec.open())
    timer, alarms = FakeTimer(), FailingAlarms(error)
    storage = Storage(
        flaky, timer=timer, pause=timer.pause, retry=RETRY, alerts=alarms, owner_step=OWNER_STEP
    )
    key = _snapshot_key()
    attempt_2 = core.attempt_key("ci", RUN, AT, "data", "snapshot", 2)
    storage.put(StageKind.CAPTURING, key, b"read one", attempt=_attempt())
    with pytest.raises(Halt, match="could not be sent") as stored:
        storage.put(StageKind.CAPTURING, key, b"read two", attempt=_attempt())
    assert f"is stored at {attempt_2}" in stored.value.reason
    assert "not stored" not in stored.value.reason
    assert storage.unstored() == ()
    assert flaky.get(attempt_2) == b"read two"

    flaky.down = True
    with pytest.raises(Halt) as outage:
        storage.put(StageKind.CAPTURING, core.broker_read_key("ci", RUN, AT, 1), b"read")
    reason = outage.value.reason
    assert "did not store" in reason and OWNER_STEP in reason
    assert f"The alarm could not be sent: {type(error).__name__}" in reason
    assert alarms.reasons[-1] in reason


@pytest.mark.parametrize("error", PAGER_ERRORS, ids=lambda error: type(error).__name__)
def test_a_held_capture_whose_alarm_fails_on_retry_is_released_as_stored(
    spec: StoreSpec, error: BaseException
) -> None:
    flaky = Flaky(spec.open())
    timer, alarms = FakeTimer(), FailingAlarms(error)
    storage = Storage(
        flaky, timer=timer, pause=timer.pause, retry=RETRY, alerts=alarms, owner_step=OWNER_STEP
    )
    key = _snapshot_key()
    attempt_2 = core.attempt_key("ci", RUN, AT, "data", "snapshot", 2)
    flaky.create(key, b"read one", sha256_of(b"read one"))
    flaky.down_at = attempt_2
    with pytest.raises(Halt):
        storage.put(StageKind.CAPTURING, key, b"read two", attempt=_attempt())
    flaky.down_at = None
    (stored,) = storage.retry_held()
    assert stored.ref == BlobRef(attempt_2, sha256_of(b"read two"))
    assert storage.unstored() == ()
    assert flaky.get(attempt_2) == b"read two"


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
        dict(deadline=timedelta(0), pause=1.0, longest_pause=2.0),
        dict(deadline=timedelta(seconds=1), pause=0.0, longest_pause=2.0),
        dict(deadline=timedelta(seconds=1), pause=3.0, longest_pause=2.0),
    ):
        with pytest.raises(ValueError):
            Retry(**bad)
    assert Retry(deadline=timedelta(seconds=1), pause=2.0, longest_pause=2.0).pause == 2.0


def test_a_write_halts_once_its_pauses_reach_the_deadline_exactly(spec: StoreSpec) -> None:
    """The deadline itself counts as reached, so pauses of 1 and 2 seconds end a 3-second retry."""
    flaky = Flaky(spec.open())
    timer, alarms = FakeTimer(), Alarms()
    retry = Retry(deadline=timedelta(seconds=3), pause=1.0, longest_pause=2.0)
    storage = Storage(
        flaky, timer=timer, pause=timer.pause, retry=retry, alerts=alarms, owner_step="x"
    )
    flaky.down = True
    with pytest.raises(Halt):
        storage.put(StageKind.CAPTURING, _snapshot_key(), b"snapshot", attempt=_attempt())
    assert timer.pauses == [1.0, 2.0]


def test_a_deterministic_output_takes_no_attempt_keys(spec: StoreSpec) -> None:
    storage, _, _ = _storage(spec.open())
    with pytest.raises(ValueError):
        storage.put(StageKind.DETERMINISTIC, _snapshot_key(), b"x", attempt=_attempt())


def test_a_wall_clock_stepped_back_does_not_stretch_the_deadline(spec: StoreSpec) -> None:
    """The deadline reads the monotonic clock, so a stepped wall clock moves it nowhere."""
    flaky = Flaky(spec.open())
    storage, timer, _ = _storage(flaky)
    flaky.down = True
    pause = timer.pause

    def pause_and_step_back(seconds: float) -> None:
        if len(timer.pauses) > 50:
            raise AssertionError("the wall clock stepped back kept the deadline away")
        pause(seconds)
        timer.at -= timedelta(minutes=10)

    storage._pause = pause_and_step_back
    with pytest.raises(Halt):
        storage.put(StageKind.CAPTURING, _snapshot_key(), b"snapshot", attempt=_attempt())
    assert RETRY.deadline.total_seconds() <= timer.elapsed < RETRY.deadline.total_seconds() + 8


def test_announce_says_whether_the_store_took_the_call() -> None:
    class Refusing:
        def announce(self, key: str) -> None:
            raise StoreUnavailable(key, "no answer")

    class Taking:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def announce(self, key: str) -> None:
            self.keys.append(key)

    taking = Taking()
    assert _storage(taking)[0].announce("runs/ci/x/result.json") is True
    assert taking.keys == ["runs/ci/x/result.json"]
    assert _storage(Refusing())[0].announce("runs/ci/x/result.json") is False
