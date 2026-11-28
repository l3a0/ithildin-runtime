"""The receive loop: its pool, its order rule, a handler's failure and the heartbeat.

docs/design.md's `### How a component learns of a change` holds the rules,
and #81's acceptance names the first three tests below.
"""

from __future__ import annotations

import dataclasses
import threading
from asyncio import CancelledError
from datetime import UTC, datetime, timedelta

import pytest

from ithildinruntime.adapters import WallClock
from ithildinruntime.core import (
    Alarm,
    BlobRef,
    DecisionResult,
    EventKind,
    Halt,
    Progress,
    Retry,
    Stopped,
    StoreUnavailable,
    output_key,
    result_key,
)
from ithildinruntime.runtime.loop import Item, LoopFailed, ReceiveLoop
from tests.support.clocks import never_asked
from tests.support.queues import (
    COMPONENTS,
    DEPLOYMENT,
    FIRST,
    RUN,
    TABLE,
    Alarms,
    Calls,
    Folder,
    Refusing,
    StubQueue,
    SystemTimer,
    handlers,
    header,
    loop,
    plan,
    run_folder,
    running,
    storage,
)


def test_pool_sizes_one_and_four_write_the_same_bytes(tmp_path) -> None:
    one, calls_one = run_folder(tmp_path / "one", decisions=8, pool_size=1)
    four, calls_four = run_folder(tmp_path / "four", decisions=8, pool_size=4, seed=11)
    assert one == four
    assert len(one) == 8 * len(TABLE.stages) * 2
    for calls in (calls_one, calls_four):
        assert set(calls.counts.values()) == {1}
        books = [item.decision_at for item in calls.order if item.stage == "book"]
        assert books == [d.at for d in plan(8).decisions]
    assert set(COMPONENTS) == {s.component for s in TABLE.stages}


# A handler's failure stays in its item.


def _alpha_loop(tmp_path, alpha, alarms, store_wrapper=None, pool_size=2, decisions=2):
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    the_plan = plan(decisions)
    base = folder.store()
    store = storage(store_wrapper(base) if store_wrapper else base, alarms)
    pipe = folder.queue("alpha")
    made = loop(
        "alpha",
        the_plan=the_plan,
        queue=pipe,
        store=store,
        bucket=folder.bucket,
        alarms=alarms,
        handlers_by_stage={"alpha": alpha},
        pool_size=pool_size,
    )
    return folder, the_plan, store, made, pipe


def _feed(store, the_plan) -> None:
    feed = handlers(store, the_plan, Calls())["feed"]
    for decision in the_plan.decisions:
        item = Item(decision.at, "feed")
        store.put_result(result_key(DEPLOYMENT, RUN, decision.at, "feed"), feed(item, lambda: None))


def test_a_handler_that_raises_halts_its_decision_while_another_finishes_its_own(
    tmp_path,
) -> None:
    alarms = Alarms()
    halted = threading.Event()
    holder: dict = {}

    def alpha(item: Item, report):
        if item.decision_at == FIRST + timedelta(minutes=1):
            raise ValueError("the stand-in model broke")
        # The first decision finishes only after the second has halted.
        assert halted.wait(10)
        return handlers(holder["store"], holder["plan"], Calls())["alpha"](item, report)

    folder, the_plan, store, made, pipe = _alpha_loop(tmp_path, alpha, alarms)
    holder.update(store=store, plan=the_plan)
    second = result_key(DEPLOYMENT, RUN, FIRST + timedelta(minutes=1), "alpha")
    first = result_key(DEPLOYMENT, RUN, FIRST, "alpha")
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        assert started.wait_for(lambda: (folder.root / second).exists(), 10)
        halted.set()
        assert started.wait_for(lambda: (folder.root / first).exists(), 10)
    pipe.close()
    result = store.read_result(second, FIRST + timedelta(minutes=1))
    assert result.halt is not None and result.outputs == ()
    reason = result.halt.reason
    for part in ("alpha-1", "stage alpha", "20261102T150100Z", "ValueError", "model broke"):
        assert part in reason
    assert alarms.reasons == [reason]
    assert store.read_result(first, FIRST).halt is None


def test_a_handler_that_raises_halt_stores_it_without_an_alarm(tmp_path) -> None:
    alarms = Alarms()

    def alpha(item: Item, report):
        raise Halt("feed missing for the stand-in session")

    folder, the_plan, store, made, pipe = _alpha_loop(tmp_path, alpha, alarms, decisions=1)
    key = result_key(DEPLOYMENT, RUN, FIRST, "alpha")
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        assert started.wait_for(lambda: (folder.root / key).exists(), 10)
    pipe.close()
    assert store.read_result(key, FIRST).halt.reason == "feed missing for the stand-in session"
    assert alarms.reasons == []


@pytest.mark.parametrize(
    ("stage", "minutes", "named"),
    [
        ("risk", 0, "result of stage risk"),
        ("alpha", 1, "result of stage alpha at 20261102T150100Z"),
    ],
    ids=["another-stage", "another-decision"],
)
def test_a_result_for_another_item_is_stored_as_a_halt(tmp_path, stage, minutes, named) -> None:
    alarms = Alarms()
    holder: dict = {}

    def alpha(item: Item, report):
        result = handlers(holder["store"], holder["plan"], Calls())["alpha"](item, report)
        at = item.decision_at + timedelta(minutes=minutes)
        # A ref under another decision's prefix, which no halt of this item may name.
        stray = BlobRef(
            output_key(DEPLOYMENT, RUN, FIRST + timedelta(minutes=5), "alpha", "x"), "0"
        )
        return DecisionResult(header(stage, at, {}), (*result.outputs, stray), None)

    folder, the_plan, store, made, pipe = _alpha_loop(tmp_path, alpha, alarms, decisions=1)
    holder.update(store=store, plan=the_plan)
    key = result_key(DEPLOYMENT, RUN, FIRST, "alpha")
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        assert started.wait_for(lambda: (folder.root / key).exists(), 10)
    pipe.close()
    halt = store.read_result(key, FIRST).halt
    assert named in halt.reason
    # The halt names the output the handler stored under its own decision.
    assert [ref.key.rsplit("/", 1)[1] for ref in halt.found_outputs] == ["value.json"]


class RefusingResults:
    """A store that takes every write but an alpha result, until a case lets it."""

    def __init__(self, store, only: str = "/alpha/result.json") -> None:
        self._store = store
        self.down = True
        self.only = only
        self.tries = 0

    def create(self, key, data, sha256):
        if self.down and key.endswith(self.only):
            self.tries += 1
            raise StoreUnavailable(key, "503 SlowDown")
        return self._store.create(key, data, sha256)

    def __getattr__(self, name):
        return getattr(self._store, name)


def _run_in_thread(made: ReceiveLoop) -> tuple[threading.Thread, list[BaseException]]:
    failures: list[BaseException] = []

    def run() -> None:
        try:
            made.run()
        except BaseException as error:  # noqa: BLE001 - the test reads it
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, failures


def _wait(check, timeout: float) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def _holds_alive(thread: threading.Thread, beats: list) -> bool:
    """Whether the loop's thread lives and beats on for half a second."""
    import time

    before = len(beats)
    time.sleep(0.5)
    return thread.is_alive() and len(beats) > before


@pytest.mark.parametrize("ends", ["raises", "raises-a-halt", "returns-a-halt", "returns-a-result"])
def test_a_halt_that_cannot_be_stored_holds_the_process_until_s3_takes_it(
    tmp_path, ends: str
) -> None:
    """A handler's own halt, and the halt that replaces a result S3 refused, are both held."""
    refusing: list = []
    holder: dict = {}

    def wrap(base):
        refusing.append(RefusingResults(base))
        return refusing[0]

    def alpha(item: Item, report):
        if ends == "raises":
            raise ValueError("broken")
        if ends == "raises-a-halt":
            raise Halt("the handler's own halt")
        if ends == "returns-a-halt":
            record = made._storage.halt_record(Halt("the handler's own halt"))
            return DecisionResult(header("alpha", item.decision_at, {}), (), record)
        return handlers(holder["store"], holder["plan"], Calls())["alpha"](item, report)

    beats: list = []
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=1,
        extend_every=0.1,
        tick=0.02,
        beat=lambda: beats.append(None),
    )
    holder.update(store=store, plan=the_plan)
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)

    def holding() -> list[str]:
        return [r for r in alarms.reasons if "S3 has not stored" in r]

    assert _wait(holding, 10)
    assert _key("alpha") in holding()[0]
    assert "because the halt for" in holding()[0] and "alpha-1 holds 1 blob " in holding()[0]
    assert "unless the host kills it first, which loses them" in holding()[0]
    # Scoped to the component and not the instance, so every instance of alpha pages once.
    assert "loop.holding:alpha" in [a.cause for a in alarms.alarms]
    if ends == "raises":
        # The handler's own error is sent even though its halt is not stored.
        assert any("did not handle: ValueError: broken" in r for r in alarms.reasons)
    # Alive and beating, so the host's watchdog leaves it, while S3 refuses the halt.
    tries = refusing[0].tries
    assert _holds_alive(thread, beats) and not failures
    # One quiet try each 0.1 s interval, about five in the half second, never
    # one straight after another.
    assert 1 <= refusing[0].tries - tries <= 10
    assert not (folder.root / _key("alpha")).exists()
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert "alpha/result.json" in str(failures[0])
    assert "alpha-1" in str(failures[0]) and "was not stored" in str(failures[0])
    assert store.read_result(_key("alpha"), FIRST).halt is not None
    assert len(holding()) == 1
    # Each retry is quiet, so the outage alarms once for each write the
    # handler's end made, however long it lasts: the halt alone, or the
    # result and the halt that replaces it.
    writes = 2 if ends == "returns-a-result" else 1
    assert len([r for r in alarms.reasons if r.startswith("S3 did not store")]) == writes
    # What the item had ended with goes out beside the outage's own alarm.
    (detail,) = [r for r in alarms.reasons if "The item had ended with" in r]
    assert "S3 did not store" in detail and "because" not in detail
    ended = {
        "raises": "ValueError: broken",
        "raises-a-halt": "Halt: the handler's own halt",
        "returns-a-halt": "Halt: the handler's own halt",
        "returns-a-result": "a result that could not be stored: Outage: S3 did not store",
    }[ends]
    assert f"The item had ended with {ended}" in detail


def test_a_stop_waits_until_s3_stores_each_capture_the_process_holds(tmp_path) -> None:
    from ithildinruntime.core import StageKind, output_key

    refusing: list = []

    def wrap(base):
        refusing.append(Refusing(base, creates=("/alpha/capture.json",)))
        return refusing[0]

    holder: dict = {}
    capture = output_key(DEPLOYMENT, RUN, FIRST, "alpha", "capture")

    def alpha(item: Item, report):
        holder["store"].put(StageKind.CAPTURING, capture, b"what the broker said")
        raise AssertionError("put should halt while S3 refuses the capture")

    beats: list = []
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=1,
        extend_every=0.1,
        tick=0.02,
        beat=lambda: beats.append(None),
    )
    holder["store"] = made._storage
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: (folder.root / _key("alpha")).exists(), 10)
    made.stop()
    assert _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 10)
    assert _holds_alive(thread, beats)
    holding = [r for r in alarms.reasons if "S3 has not stored" in r]
    assert len(holding) == 1 and capture in holding[0]
    assert "because it was asked to stop" in holding[0]
    assert not (folder.root / capture).exists()
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and not failures
    assert (folder.root / capture).exists() and made._storage.unstored() == ()


class AnswersCaptureWithAnError(RefusingResults):
    """S3 answers the capture with an error, not a refusal to answer, until a case lets it."""

    def create(self, key, data, sha256):
        if self.down and key.endswith("/alpha/capture.json"):
            self.tries += 1
            raise PermissionError(f"AccessDenied on {key}")
        return self._store.create(key, data, sha256)


def test_a_capture_s3_keeps_answering_with_an_error_holds_the_process_and_is_never_dropped(
    tmp_path,
) -> None:
    from ithildinruntime.core import StageKind, output_key

    refusing: list = []

    def wrap(base):
        refusing.append(AnswersCaptureWithAnError(base))
        return refusing[0]

    holder: dict = {}
    capture = output_key(DEPLOYMENT, RUN, FIRST, "alpha", "capture")

    def alpha(item: Item, report):
        holder["store"].put(StageKind.CAPTURING, capture, b"what the broker said")
        raise AssertionError("put should halt while S3 answers the capture with an error")

    beats: list = []
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=1,
        extend_every=0.1,
        tick=0.02,
        beat=lambda: beats.append(None),
    )
    holder["store"] = made._storage
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: (folder.root / _key("alpha")).exists(), 10)
    made.stop()
    assert _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 10)
    tries = refusing[0].tries
    # It keeps the capture, retrying and beating, however long S3 keeps answering so.
    assert _holds_alive(thread, beats) and _holds_alive(thread, beats)
    assert refusing[0].tries > tries
    assert [ref.key for ref in made._storage.unstored()] == [capture]
    holding = [r for r in alarms.reasons if "S3 has not stored" in r]
    assert len(holding) == 1 and "until a person acts" in holding[0]
    assert not [r for r in alarms.reasons if "could not be stored" in r]
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and not failures
    assert (folder.root / capture).exists() and made._storage.unstored() == ()


@pytest.mark.parametrize("ends", ["stop", "queue-fails"])
def test_a_handler_past_the_stop_timeout_ends_with_the_process_and_the_drain_beats(
    tmp_path, ends: str
) -> None:
    """A stop and a failure each give a handler the stop timeout, and no more."""
    import time

    started = threading.Event()
    finished = threading.Event()

    def alpha(item: Item, report):
        # Works past the stop timeout, reporting as it goes.
        started.set()
        end = time.monotonic() + 3.0
        while time.monotonic() < end:
            report()
            time.sleep(0.02)
        finished.set()
        raise Halt("the stand-in finished too late to matter")

    beats: list[float] = []
    queue = StubQueue()
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        decisions=1,
        queue=queue,
        extend_every=0.1,
        tick=0.02,
        stop_timeout=0.8,
        beat=lambda: beats.append(time.monotonic()),
    )
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert started.wait(10)
    stopped = time.monotonic()
    if ends == "stop":
        made.stop()
    else:
        queue.error = KeyError("ReceiptHandle")
    thread.join(5)
    ended = time.monotonic()
    # The drain gave the handler the stop timeout, and no more, since it held
    # nothing yet. Its decision is done again after the restart.
    assert not thread.is_alive() and not finished.is_set()
    assert len(failures) == (0 if ends == "stop" else 1)
    assert 0.7 < ended - stopped < 2.0
    # The beat went on through the drain while the handler reported.
    during = [b for b in beats if stopped <= b <= ended]
    gaps = [later - earlier for earlier, later in zip([stopped, *during], during, strict=False)]
    assert gaps and max(gaps) < 0.4
    assert not any("S3 has not stored" in r for r in alarms.reasons)


def test_after_a_failure_a_handler_finishing_within_the_stop_timeout_has_its_capture_kept(
    tmp_path,
) -> None:
    from ithildinruntime.core import StageKind, output_key

    refusing: list = []

    def wrap(base):
        refusing.append(Refusing(base, creates=("/alpha/capture.json",)))
        return refusing[0]

    queue = StubQueue()
    holder: dict = {}
    capture = output_key(DEPLOYMENT, RUN, FIRST, "alpha", "capture")
    started = threading.Event()
    go = threading.Event()

    def alpha(item: Item, report):
        started.set()
        while not go.wait(0.02):
            report()
        holder["store"].put(StageKind.CAPTURING, capture, b"what the broker said")
        raise AssertionError("put should halt while S3 refuses the capture")

    beats: list = []
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=1,
        queue=queue,
        extend_every=0.1,
        tick=0.02,
        stop_timeout=5.0,
        beat=lambda: beats.append(None),
    )
    holder["store"] = made._storage
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert started.wait(10)
    # The queue fails while the handler works, so the loop stops receiving.
    queue.error = KeyError("ReceiptHandle")
    assert _wait(lambda: any("KeyError" in r for r in alarms.reasons), 10)
    go.set()
    holding = lambda: [r for r in alarms.reasons if "S3 has not stored" in r]  # noqa: E731
    assert _wait(lambda: holding(), 10) and capture in holding()[0]
    assert "because its queue raised KeyError" in holding()[0]
    assert "because alpha-1" not in holding()[0]
    assert _holds_alive(thread, beats) and not failures
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1
    assert (folder.root / capture).exists() and made._storage.unstored() == ()


class Garbling(RefusingResults):
    """Refuses an alpha result, then, once let, leaves bytes no codec reads at its key."""

    garble = False

    def create(self, key, data, sha256):
        if self.garble and key.endswith("/alpha/result.json"):
            from ithildinruntime.core import sha256_of

            self._store.create(key, b"not json", sha256_of(b"not json"))
            return self._store.create(key, data, sha256)
        return super().create(key, data, sha256)


def test_a_held_halt_that_s3_answers_but_cannot_store_is_dropped_with_its_reason(
    tmp_path,
) -> None:
    refusing: list = []

    def wrap(base):
        refusing.append(Garbling(base))
        return refusing[0]

    def alpha(item: Item, report):
        raise ValueError("broken")

    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, alpha, wrap=wrap, decisions=1, extend_every=0.1, tick=0.02
    )
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 10)
    refusing[0].garble = True
    thread.join(10)
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    dropped = [r for r in alarms.reasons if "could not be stored" in r]
    assert len(dropped) == 1 and _key("alpha") in dropped[0] and "broken" in dropped[0]
    # LoopFailed carries it too, for the host's restart record.
    assert "Halts not stored" in str(failures[0]) and "broken" in str(failures[0])


def test_a_held_halt_whose_key_comes_to_hold_another_result_alarms_with_its_reason(
    tmp_path,
) -> None:
    refusing: list = []

    def wrap(base):
        refusing.append(RefusingResults(base))
        return refusing[0]

    def alpha(item: Item, report):
        raise ValueError("broken")

    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, alpha, wrap=wrap, decisions=1, extend_every=0.1, tick=0.02
    )
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 10)
    # Another writer, such as the supervisor, stores its own halt at the key.
    stage = made._table.stage("alpha")
    theirs = made._halt_result(stage, Item(FIRST, "alpha"), Halt("the supervisor halted it"))
    store.put_result(_key("alpha"), theirs)
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1
    assert store.read_result(_key("alpha"), FIRST).halt.reason == "the supervisor halted it"
    found = [r for r in alarms.reasons if "found another result" in r]
    assert len(found) == 1 and "broken" in found[0]
    assert "which stands: the supervisor halted it" in found[0]


class FailingAlarms(Alarms):
    """An alert channel that refuses the hold's alarm until a case lets it."""

    def __init__(self) -> None:
        super().__init__()
        self.down = True
        self.refused = 0

    def alarm(self, alarm: Alarm) -> None:
        reason = alarm.reason
        if self.down and "S3 has not stored" in reason:
            self.refused += 1
            raise ConnectionError("the alert channel is down")
        super().alarm(alarm)


def test_the_hold_sends_its_alarm_again_until_the_channel_takes_it(tmp_path) -> None:
    refusing: list = []

    def wrap(base):
        refusing.append(RefusingResults(base))
        return refusing[0]

    def alpha(item: Item, report):
        raise ValueError("broken")

    beats: list = []
    alarms = FailingAlarms()
    folder, the_plan, store, made, _, _ = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=1,
        extend_every=0.1,
        tick=0.02,
        alarms=alarms,
        beat=lambda: beats.append(None),
    )
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: alarms.refused >= 2, 10)
    assert _holds_alive(thread, beats)
    alarms.down = False
    assert _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 10)
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1
    assert len([r for r in alarms.reasons if "S3 has not stored" in r]) == 1


def test_a_beat_that_raises_never_ends_the_hold(tmp_path) -> None:
    refusing: list = []

    def wrap(base):
        refusing.append(RefusingResults(base))
        return refusing[0]

    def alpha(item: Item, report):
        raise ValueError("broken")

    def beat() -> None:
        raise OSError("the heartbeat file's disk is full")

    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, alpha, wrap=wrap, decisions=1, extend_every=0.1, tick=0.02, beat=beat
    )
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 10)
    __import__("time").sleep(0.5)
    assert thread.is_alive() and not failures
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1
    assert store.read_result(_key("alpha"), FIRST).halt is not None


def test_one_halt_s3_still_refuses_does_not_hold_back_another(tmp_path) -> None:
    refusing: list = []

    def wrap(base):
        refusing.append(RefusingResults(base))
        return refusing[0]

    def alpha(item: Item, report):
        raise ValueError(f"broken at {item.decision_at}")

    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, alpha, wrap=wrap, decisions=2, extend_every=0.1, tick=0.02
    )
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: len(made._held_keys()) == 2, 10)
    # S3 still refuses the halt the process holds first, and takes the other.
    still, other = made._held_keys()
    refusing[0].only = still
    assert _wait(lambda: made._held_keys() == [still], 10)
    assert thread.is_alive() and (folder.root / other).exists()
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1


class SlowAlarms(Alarms):
    """An alert channel that takes 0.6 seconds over the hold's alarm."""

    def alarm(self, alarm: Alarm) -> None:
        reason = alarm.reason
        if "S3 has not stored" in reason:
            __import__("time").sleep(0.6)
        super().alarm(alarm)


def test_a_slow_alert_channel_never_holds_the_holds_beat_back(tmp_path) -> None:
    import time

    refusing: list = []

    def wrap(base):
        refusing.append(RefusingResults(base))
        return refusing[0]

    def alpha(item: Item, report):
        raise ValueError("broken")

    beats: list[float] = []
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=1,
        extend_every=0.1,
        tick=0.02,
        alarms=SlowAlarms(),
        beat=lambda: beats.append(time.monotonic()),
    )
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 10)
    __import__("time").sleep(0.3)
    first = min(b for b in beats if b >= beats[-1] - 1.5)
    during = [b for b in beats if b >= first]
    gaps = [later - earlier for earlier, later in zip(during, during[1:], strict=False)]
    assert gaps and max(gaps) < 0.4
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1


class BrokenAnnounce(RefusingResults):
    """Refuses an alpha result, and raises a bug's error on every announce."""

    def announce(self, key):
        raise RuntimeError("a bug in the stand-in store")


def test_an_error_in_the_start_check_still_reaches_the_hold(tmp_path) -> None:
    refusing: list = []

    def wrap(base):
        refusing.append(BrokenAnnounce(base))
        return refusing[0]

    def alpha(item: Item, report):
        raise ValueError("broken")

    beats: list = []
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=2,
        extend_every=0.1,
        tick=0.02,
        beat=lambda: beats.append(None),
    )
    _feed(store, the_plan)
    # The second decision's result is already stored, so the start announces it.
    second = Item(FIRST + timedelta(minutes=1), "alpha")
    store.put_result(
        _key("alpha", 1), handlers(store, the_plan, Calls())["alpha"](second, lambda: None)
    )
    thread, failures = _run_in_thread(made)
    # The start check starts the first decision, whose halt S3 refuses, and
    # the announce after it raises.
    assert _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 10)
    assert _holds_alive(thread, beats) and not failures
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1
    assert "a bug in the stand-in store" in str(failures[0])
    # The loop ends as LoopFailed naming both, the first first.
    assert isinstance(failures[0], LoopFailed) and "was not stored" in str(failures[0])
    assert isinstance(failures[0].__cause__, RuntimeError)
    held = [r for r in alarms.reasons if "S3 has not stored" in r]
    # The bug and the refused halt race here, and the alarm names whichever
    # came first, as LoopFailed does.
    bug_first = str(failures[0]).startswith("RuntimeError")
    assert ("because RuntimeError" if bug_first else "because the halt for") in held[0]
    assert store.read_result(_key("alpha"), FIRST).halt is not None


class SlowAnnounce(RefusingResults):
    """Refuses an alpha result, and takes 1.5 seconds over an announce, which then raises."""

    def announce(self, key):
        __import__("time").sleep(1.5)
        raise RuntimeError("a bug in the stand-in store")


def test_the_beat_goes_on_while_the_loop_waits_on_its_own_work_after_a_failure(
    tmp_path,
) -> None:
    import time

    refusing: list = []

    def wrap(base):
        refusing.append(SlowAnnounce(base))
        return refusing[0]

    def alpha(item: Item, report):
        raise ValueError("broken")

    beats: list[float] = []
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=2,
        extend_every=0.1,
        tick=0.02,
        beat=lambda: beats.append(time.monotonic()),
    )
    _feed(store, the_plan)
    # The second decision's result is stored, so the start announces it, slowly.
    second = Item(FIRST + timedelta(minutes=1), "alpha")
    store.put_result(
        _key("alpha", 1), handlers(store, the_plan, Calls())["alpha"](second, lambda: None)
    )
    began = time.monotonic()
    thread, failures = _run_in_thread(made)
    holding = lambda: [r for r in alarms.reasons if "S3 has not stored" in r]  # noqa: E731
    assert _wait(holding, 10)
    # The halt failed while the loop waited on the announce, and the beat went on.
    assert "because the halt for" in holding()[0]
    time.sleep(0.6)
    window = [b for b in beats if b >= began]
    ends = [*window, time.monotonic()]
    gaps = [later - earlier for earlier, later in zip([began, *window], ends, strict=False)]
    assert max(gaps) < 0.4
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1


def test_the_drain_beats_while_a_halt_is_held_beside_a_handler_that_does_not_report(
    tmp_path,
) -> None:
    import time

    refusing: list = []

    def wrap(base):
        refusing.append(RefusingResults(base))
        return refusing[0]

    gate = threading.Event()

    def alpha(item: Item, report):
        if item.decision_at != FIRST:
            # Waits without reporting, as a hung handler does.
            gate.wait(20)
        raise ValueError("broken")

    beats: list[float] = []
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=2,
        extend_every=0.1,
        tick=0.02,
        heartbeat_limit=0.2,
        stop_timeout=1.0,
        beat=lambda: beats.append(time.monotonic()),
    )
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    try:
        # The first decision's halt is held, so the loop failed and drains
        # while the second handler hangs.
        assert _wait(lambda: _key("alpha") in made._held_keys(), 10)
        held_at = time.monotonic()
        time.sleep(1.6)
        window = [b for b in beats if b >= held_at]
        ends = [*window, time.monotonic()]
        gaps = [later - earlier for earlier, later in zip([held_at, *window], ends, strict=False)]
        assert max(gaps) < 0.4
    finally:
        gate.set()
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1


class GarblingBrokenAnnounce(Garbling):
    def announce(self, key):
        raise RuntimeError("a bug in the stand-in store")


def test_a_bug_that_ends_the_loop_keeps_a_dropped_halts_reason_in_its_failure(tmp_path) -> None:
    refusing: list = []

    def wrap(base):
        refusing.append(GarblingBrokenAnnounce(base))
        return refusing[0]

    def alpha(item: Item, report):
        raise ValueError("broken")

    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, alpha, wrap=wrap, decisions=2, extend_every=0.1, tick=0.02
    )
    _feed(store, the_plan)
    second = Item(FIRST + timedelta(minutes=1), "alpha")
    store.put_result(
        _key("alpha", 1), handlers(store, the_plan, Calls())["alpha"](second, lambda: None)
    )
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 10)
    refusing[0].garble = True
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1
    assert isinstance(failures[0], LoopFailed)
    said = str(failures[0])
    assert "a bug in the stand-in store" in said and "Halts not stored" in said and "broken" in said


class CaptureAndBrokenAnnounce(Refusing):
    def announce(self, key):
        raise RuntimeError("a bug in the stand-in store")


def test_a_hold_after_a_bug_names_the_bug(tmp_path) -> None:
    from ithildinruntime.core import StageKind, output_key

    refusing: list = []

    def wrap(base):
        refusing.append(CaptureAndBrokenAnnounce(base, creates=("/alpha/capture.json",)))
        return refusing[0]

    holder: dict = {}
    capture = output_key(DEPLOYMENT, RUN, FIRST, "alpha", "capture")

    def alpha(item: Item, report):
        if item.decision_at == FIRST:
            holder["store"].put(StageKind.CAPTURING, capture, b"what the broker said")
        return handlers(holder["base"], holder["plan"], Calls())["alpha"](item, report)

    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, alpha, wrap=wrap, decisions=2, extend_every=0.1, tick=0.02
    )
    holder.update(store=made._storage, base=store, plan=the_plan)
    _feed(store, the_plan)
    second = Item(FIRST + timedelta(minutes=1), "alpha")
    store.put_result(
        _key("alpha", 1), handlers(store, the_plan, Calls())["alpha"](second, lambda: None)
    )
    thread, failures = _run_in_thread(made)
    held = lambda: [r for r in alarms.reasons if "S3 has not stored" in r]  # noqa: E731
    assert _wait(held, 10)
    # No stop and no failure of its own: the bug is why receiving stopped.
    assert "because RuntimeError: a bug in the stand-in store" in held()[0]
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and isinstance(failures[0], RuntimeError)


def _silent_capture_loop(tmp_path, *, refusing_class, beats, **settings):
    """A loop whose first decision's handler stores a capture S3 refuses, then waits
    without reporting, as a hung handler does, until the case lets it go."""
    from ithildinruntime.core import StageKind, output_key

    refusing: list = []

    def wrap(base):
        refusing.append(refusing_class(base, creates=("/alpha/capture.json",)))
        return refusing[0]

    holder: dict = {}
    gate = threading.Event()
    _waiting.append(type("Gate", (), {"go": gate})())
    capture = output_key(DEPLOYMENT, RUN, FIRST, "alpha", "capture")

    def alpha(item: Item, report):
        if item.decision_at == FIRST:
            try:
                holder["store"].put(StageKind.CAPTURING, capture, b"what the broker said")
            except Halt:
                pass
            gate.wait(20)
        return handlers(holder["base"], holder["plan"], Calls())["alpha"](item, report)

    folder, the_plan, store, made, queue, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=2,
        extend_every=0.1,
        tick=0.02,
        heartbeat_limit=0.2,
        beat=lambda: beats.append(__import__("time").monotonic()),
        **settings,
    )
    holder.update(store=made._storage, base=store, plan=the_plan)
    _feed(store, the_plan)
    # The second decision's result is stored, so the start announces it.
    second = Item(FIRST + timedelta(minutes=1), "alpha")
    store.put_result(
        _key("alpha", 1), handlers(store, the_plan, Calls())["alpha"](second, lambda: None)
    )
    return made, refusing, gate, alarms


@pytest.mark.parametrize("ends", ["bug", "stop"])
def test_a_process_that_stopped_receiving_beats_over_a_silent_handler_while_it_holds(
    tmp_path, ends: str
) -> None:
    import time

    beats: list[float] = []
    made, refusing, gate, alarms = _silent_capture_loop(
        tmp_path,
        refusing_class=CaptureAndBrokenAnnounce if ends == "bug" else Refusing,
        beats=beats,
        stop_timeout=0.3,
    )
    thread, failures = _run_in_thread(made)
    try:
        assert _wait(lambda: made._storage.unstored() != (), 10)
        if ends == "stop":
            made.stop()
        assert _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 10)
        at = time.monotonic()
        time.sleep(1.0)
        assert len([b for b in beats if b >= at]) > 10
    finally:
        gate.set()
        refusing[0].down = False
        thread.join(10)
    assert not thread.is_alive()


def test_a_process_still_receiving_beats_over_a_silent_handler_only_while_it_holds(
    tmp_path,
) -> None:
    import time

    beats: list[float] = []
    made, refusing, gate, alarms = _silent_capture_loop(
        tmp_path, refusing_class=Refusing, beats=beats
    )
    thread, failures = _run_in_thread(made)
    try:
        assert _wait(lambda: made._storage.unstored() != (), 10)
        at = time.monotonic()
        time.sleep(1.0)
        # Still receiving, with a capture held, it beats over the silent
        # handler, so the watchdog does not kill it and lose the capture.
        assert thread.is_alive() and not made._stopping
        assert len([b for b in beats if b >= at]) > 10
        refusing[0].down = False
        assert _wait(lambda: made._storage.unstored() == (), 10)
        time.sleep(0.5)
        # Nothing is held now, so the silent handler stops the beat again.
        assert thread.is_alive() and not made._stopping
        assert not [b for b in beats if b >= time.monotonic() - 0.3]
    finally:
        gate.set()
        refusing[0].down = False
        made.stop()
        thread.join(10)


def test_a_stop_with_nothing_held_never_beats_over_a_silent_handler(tmp_path) -> None:
    import time

    started = threading.Event()
    gate = threading.Event()
    _waiting.append(type("Gate", (), {"go": gate})())

    def alpha(item: Item, report):
        started.set()
        gate.wait(20)
        raise Halt("the stand-in was let go")

    beats: list[float] = []
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        decisions=1,
        extend_every=0.1,
        tick=0.02,
        heartbeat_limit=0.2,
        stop_timeout=1.0,
        beat=lambda: beats.append(time.monotonic()),
    )
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert started.wait(10)
    began = time.monotonic()
    made.stop()
    thread.join(5)
    gate.set()
    assert not thread.is_alive()
    # Past the heartbeat limit the silent handler counts as hung, through the drain too.
    assert not [b for b in beats if b > began + 0.45]


class SlowGiveUpAlarms(Alarms):
    """An alert channel that takes a second over the alarm of a queue that failed."""

    def alarm(self, alarm: Alarm) -> None:
        reason = alarm.reason
        if "KeyError" in reason and "S3 has not stored" not in reason:
            __import__("time").sleep(1.0)
        super().alarm(alarm)


def test_a_slow_give_up_alarm_never_holds_the_beat_back(tmp_path) -> None:
    import time

    from ithildinruntime.core import StageKind, output_key

    refusing: list = []

    def wrap(base):
        refusing.append(Refusing(base, creates=("/alpha/capture.json",)))
        return refusing[0]

    queue = StubQueue()
    holder: dict = {}
    capture = output_key(DEPLOYMENT, RUN, FIRST, "alpha", "capture")

    def alpha(item: Item, report):
        holder["store"].put(StageKind.CAPTURING, capture, b"what the broker said")
        raise AssertionError("put should halt while S3 refuses the capture")

    beats: list[float] = []
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=1,
        queue=queue,
        extend_every=0.1,
        tick=0.02,
        alarms=SlowGiveUpAlarms(),
        beat=lambda: beats.append(time.monotonic()),
    )
    holder["store"] = made._storage
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: made._storage.unstored() != (), 10)
    began = time.monotonic()
    queue.error = KeyError("ReceiptHandle")
    time.sleep(1.5)
    window = [b for b in beats if b >= began]
    gaps = [b - a for a, b in zip([began, *window], [*window, time.monotonic()], strict=False)]
    assert max(gaps) < 0.4
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and len(failures) == 1


@pytest.mark.parametrize(
    ("failed_because", "clause"),
    [
        (None, "it was asked to stop"),
        ("alpha-1: the halt for k was not stored", "the halt for k was not stored"),
        ("alpha-1 stopped receiving: KeyError: 'x'", "its queue raised KeyError: 'x'"),
        (
            "alpha-1 stopped receiving: the queue refused every receive for 60 seconds",
            "the queue refused every receive for 60 seconds",
        ),
        ("alpha-1 could not answer its queue: KeyError: 'x'", "it could not answer its queue"),
        ("RuntimeError: a bug", "RuntimeError: a bug"),
    ],
    ids=["stop", "halt", "receive-raised", "deaf", "answer", "bug"],
)
def test_the_hold_says_why_receiving_stopped_as_a_clause(
    tmp_path, failed_because: str | None, clause: str
) -> None:
    _, _, _, made, _, _ = _stub_loop(tmp_path, decisions=1)
    made._failed_because = failed_because
    assert made._why_stopped().startswith(clause)


def test_a_bug_before_a_failed_halt_is_named_first(tmp_path) -> None:
    refusing: list = []

    def wrap(base):
        refusing.append(BrokenAnnounce(base))
        return refusing[0]

    gate = threading.Event()
    _waiting.append(type("Gate", (), {"go": gate})())

    def alpha(item: Item, report):
        # Held until the bug has ended the loop, then fails, so its halt is
        # the second cause.
        while not gate.wait(0.02):
            report()
        raise ValueError("broken")

    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, alpha, wrap=wrap, decisions=2, extend_every=0.1, tick=0.02, stop_timeout=5.0
    )
    _feed(store, the_plan)
    second = Item(FIRST + timedelta(minutes=1), "alpha")
    store.put_result(
        _key("alpha", 1), handlers(store, the_plan, Calls())["alpha"](second, lambda: None)
    )
    thread, failures = _run_in_thread(made)
    assert _wait(lambda: (made._failed_because or "").startswith("RuntimeError"), 10)
    gate.set()
    held = lambda: [r for r in alarms.reasons if "S3 has not stored" in r]  # noqa: E731
    assert _wait(held, 10)
    assert "because RuntimeError: a bug in the stand-in store" in held()[0]
    refusing[0].down = False
    thread.join(10)
    assert not thread.is_alive() and isinstance(failures[0], LoopFailed)
    said = str(failures[0])
    assert said.startswith("RuntimeError: a bug in the stand-in store. Then alpha-1: the halt for")
    assert isinstance(failures[0].__cause__, RuntimeError)


def test_a_receive_answered_after_a_bug_returns_its_events_to_the_queue(tmp_path) -> None:
    queue = StubQueue()
    queue.blocking = True
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, decisions=1, queue=queue, extend_every=0.1, tick=0.02
    )

    async def broken(running) -> None:
        raise RuntimeError("a bug in the loop's own code")

    made._on_finished = broken
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    # The receive is open while the start check's handler finishes, and the
    # bug in handling that ends the loop.
    assert queue.entered.wait(10)
    assert _wait(lambda: not thread.is_alive(), 10)
    assert isinstance(failures[0], RuntimeError)
    queue.put(_key("feed"))
    queue.free.set()
    assert _wait(lambda: queue.released == [_key("feed")], 10)
    assert queue.done_keys == []


def _loop_settings(tmp_path) -> dict:
    folder = Folder(tmp_path / "s", tmp_path / "p")
    return dict(
        worker="alpha-1",
        component="alpha",
        table=TABLE,
        plan=plan(1),
        queue=None,
        storage=storage(folder.store(), Alarms()),
        bucket="folder",
        handlers={"alpha": lambda item, report: None},
        alerts=Alarms(),
        timer=SystemTimer(),
        beat=lambda: None,
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"component": "nobody"}, "runs no stage"),
        ({"pool_size": 0}, "at least one handler"),
        ({"handlers": {}}, "a handler for each"),
    ],
)
def test_a_loop_refuses_settings_it_cannot_run(tmp_path, change, message) -> None:
    with pytest.raises(ValueError, match=message):
        ReceiveLoop(**{**_loop_settings(tmp_path), **change})


# The loop over a stub queue, so each case controls what it receives.


def _stub_loop(tmp_path, alpha=None, *, wrap=None, decisions=2, component="alpha", **settings):
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    base = folder.store(announce=False)
    alarms = settings.pop("alarms", None) or Alarms()
    store = storage(wrap(base) if wrap else base, alarms)
    store._retry = Retry(timedelta(seconds=0.2), 0.05, 0.05)
    the_plan = plan(decisions)
    queue = settings.pop("queue", None) or StubQueue()
    by_stage = handlers(storage(base, alarms), the_plan, Calls())
    if alpha is not None:
        by_stage[component] = alpha
    made = loop(
        component,
        the_plan=the_plan,
        queue=queue,
        store=store,
        bucket=folder.bucket,
        alarms=alarms,
        handlers_by_stage=by_stage,
        **settings,
    )
    return folder, the_plan, storage(base, alarms), made, queue, alarms


def _key(stage: str, n: int = 0) -> str:
    return result_key(DEPLOYMENT, RUN, FIRST + timedelta(minutes=n), stage)


_waiting: list[Waiting] = []


@pytest.fixture(autouse=True)
def _let_every_handler_through():
    """Let every waiting handler through when a case ends, so a failure fails rather than hangs."""
    yield
    while _waiting:
        _waiting.pop().go.set()


class Waiting:
    """An alpha handler that waits to be let through, recording each item it starts."""

    def __init__(self, store_and_plan) -> None:
        _waiting.append(self)
        self.go = threading.Event()
        self.started: list[Item] = []
        self.holder = store_and_plan

    def __call__(self, item: Item, report):
        self.started.append(item)
        while not self.go.wait(0.02):
            report()
        store, the_plan = self.holder
        return handlers(store, the_plan, Calls())["alpha"](item, report)


def test_an_error_from_receive_other_than_an_unavailable_queue_ends_the_loop(tmp_path) -> None:
    queue = StubQueue()
    queue.error = KeyError("ReceiptHandle")
    _, _, _, made, _, alarms = _stub_loop(tmp_path, queue=queue)
    failures: list[BaseException] = []

    def run() -> None:
        try:
            made.run()
        except BaseException as error:  # noqa: BLE001 - the test reads it
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and isinstance(failures[0], LoopFailed)
    assert "ReceiptHandle" in str(failures[0])
    assert alarms.reasons and "stopped receiving" in alarms.reasons[0]


@pytest.mark.parametrize(
    ("key", "bucket", "kind"),
    [
        (result_key(DEPLOYMENT, RUN, FIRST, "feed"), "another-bucket", EventKind.CREATED),
        # A route hears feed's result created, never removed.
        (result_key(DEPLOYMENT, RUN, FIRST, "feed"), "folder", EventKind.REMOVED),
        (
            result_key(DEPLOYMENT, "2026-11-03", FIRST, "feed").replace("2026-11-03", "x"),
            "folder",
            EventKind.CREATED,
        ),
    ],
    ids=["another-bucket", "off-every-route", "unbuilt-key"],
)
def test_an_event_the_loop_does_not_want_is_deleted_at_once(tmp_path, key, bucket, kind) -> None:
    holder: list = []
    waiting = Waiting(holder)
    folder, the_plan, store, made, queue, _ = _stub_loop(tmp_path, waiting, decisions=1)
    holder.extend((store, the_plan))
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        queue.put(key, bucket, kind)
        # The event wakes a check, which starts the decision from its keys
        # alone, and the event itself is deleted while the handler still waits.
        assert started.wait_for(lambda: waiting.started and key in queue.done_keys, 5)
        assert not (folder.root / _key("alpha")).exists()
        waiting.go.set()
        assert started.wait_for(lambda: (folder.root / _key("alpha")).exists(), 5)


def test_an_event_for_a_wanted_decision_travels_with_its_handler(tmp_path) -> None:
    holder: list = []
    waiting = Waiting(holder)
    folder, the_plan, store, made, queue, _ = _stub_loop(tmp_path, waiting, decisions=1)
    holder.extend((store, the_plan))
    key = _key("feed")
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        queue.put(key)
        assert started.wait_for(lambda: bool(waiting.started), 5)
        time = __import__("time")
        time.sleep(0.3)
        assert key not in queue.done_keys
        waiting.go.set()
        assert started.wait_for(lambda: key in queue.done_keys, 5)


def test_a_store_that_does_not_answer_never_starts_a_decision_or_deletes_its_event(
    tmp_path,
) -> None:
    refusing: list = []

    def wrap(base):
        refusing.append(Refusing(base, gets=("/feed/result.json",)))
        return refusing[0]

    holder: list = []
    waiting = Waiting(holder)
    waiting.go.set()
    folder, the_plan, store, made, queue, alarms = _stub_loop(
        tmp_path, waiting, wrap=wrap, decisions=1
    )
    holder.extend((store, the_plan))
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        queue.put(_key("feed"))
        assert started.wait_for(lambda: refusing[0].refused > 0, 5)
        __import__("time").sleep(0.6)
        assert waiting.started == []
        assert queue.done_keys == [] and queue.released == []
        # S3 answers again and SQS hands the event out again.
        refusing[0].down = False
        queue.put(_key("feed"))
        assert started.wait_for(lambda: (folder.root / _key("alpha")).exists(), 5)
    assert any("did not answer" in reason for reason in alarms.reasons)


def test_an_event_with_no_free_handler_goes_back_to_the_queue_at_once(tmp_path) -> None:
    holder: list = []
    waiting = Waiting(holder)
    # A receive waits up to two seconds, so the one in flight when the check
    # takes the only handler is the one that hears the second event.
    folder, the_plan, store, made, queue, _ = _stub_loop(
        tmp_path, waiting, decisions=2, pool_size=1, queue=StubQueue(wait=2.0)
    )
    holder.extend((store, the_plan))
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        # The first decision takes the only handler from a check of its keys.
        queue.put(_key("feed", 5))
        assert started.wait_for(lambda: len(waiting.started) == 1, 5)
        queue.put(_key("feed", 1))
        assert started.wait_for(lambda: _key("feed", 1) in queue.released, 5)
        assert _key("feed", 1) not in queue.done_keys
        waiting.go.set()


def test_a_receive_answered_after_stop_returns_its_events(tmp_path) -> None:
    queue = StubQueue()
    queue.blocking = True
    _, _, _, made, _, _ = _stub_loop(tmp_path, queue=queue)
    # `stop` is asked while the receive hands its event back, before the event
    # loop has had a turn, which is the window a stop must still cover.
    queue.on_answer = made.stop
    with running({"alpha": made}) as started:
        assert queue.entered.wait(5)
        queue.put(_key("feed"))
        queue.free.set()
        assert started.wait_for(lambda: bool(queue.released), 5)
    assert queue.released == [_key("feed")] and queue.done_keys == []


def test_a_slow_extend_never_holds_the_heartbeat_back(tmp_path) -> None:
    import time

    beats: list[float] = []
    queue = StubQueue()
    queue.extend_takes = 1.0
    holder: list = []
    waiting = Waiting(holder)
    folder, the_plan, store, made, _, _ = _stub_loop(
        tmp_path,
        waiting,
        decisions=1,
        queue=queue,
        extend_every=0.1,
        heartbeat_limit=0.5,
        beat=lambda: beats.append(time.monotonic()),
    )
    holder.extend((store, the_plan))
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        queue.put(_key("feed"))
        assert started.wait_for(lambda: bool(waiting.started), 5)
        time.sleep(2.0)
        waiting.go.set()
    late = [b for b in beats if b > beats[0] + 0.5]
    gaps = [later - earlier for earlier, later in zip(late, late[1:], strict=False)]
    assert queue.extended and gaps and max(gaps) < 0.5


def test_the_heartbeat_runs_while_a_slow_start_check_reads_and_stop_ends_it(tmp_path) -> None:
    import time

    beats: list[float] = []

    def wrap(base):
        return Refusing(base, gets=("/result.json",))

    started_at = time.monotonic()
    _, _, _, made, _, _ = _stub_loop(
        tmp_path, wrap=wrap, decisions=20, beat=lambda: beats.append(time.monotonic())
    )
    thread = threading.Thread(target=made.run, daemon=True)
    thread.start()
    time.sleep(1.0)
    assert beats and beats[0] - started_at < 0.5
    stopping = time.monotonic()
    made.stop()
    thread.join(5)
    assert not thread.is_alive() and time.monotonic() - stopping < 3


def test_a_capture_held_through_an_outage_is_stored_once_s3_answers(tmp_path) -> None:
    from ithildinruntime.core import StageKind, output_key

    refusing: list = []

    def wrap(base):
        refusing.append(Refusing(base, creates=("/alpha/capture.json",)))
        return refusing[0]

    holder: dict = {}
    capture = output_key(DEPLOYMENT, RUN, FIRST, "alpha", "capture")

    def alpha(item: Item, report):
        holder["store"].put(StageKind.CAPTURING, capture, b"what the broker said")
        raise AssertionError("put should halt while S3 refuses the capture")

    folder, the_plan, store, made, queue, _ = _stub_loop(
        tmp_path, alpha, wrap=wrap, decisions=1, extend_every=0.1
    )
    holder["store"] = made._storage
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        queue.put(_key("feed"))
        assert started.wait_for(lambda: (folder.root / _key("alpha")).exists(), 5)
        assert not (folder.root / capture).exists()
        refusing[0].down = False
        assert started.wait_for(lambda: (folder.root / capture).exists(), 5)
    assert made._storage.unstored() == ()
    halt = store.read_result(_key("alpha"), FIRST).halt
    assert capture in halt.reason and [ref.key for ref in halt.unstored] == [capture]


def test_a_result_that_could_not_be_stored_becomes_a_halt_naming_its_outputs(tmp_path) -> None:
    refusing: list = []

    class Once(Refusing):
        """Refuses alpha's own result, and takes the halt that replaces it."""

        def create(self, key, data, sha256):
            if key.endswith("/alpha/result.json") and b'"halt":null' in data:
                from ithildinruntime.core import ChecksumMismatch

                raise ChecksumMismatch(key, "400 BadDigest")
            return self._store.create(key, data, sha256)

    def wrap(base):
        refusing.append(Once(base))
        return refusing[0]

    holder: dict = {}

    def alpha(item: Item, report):
        result = handlers(holder["store"], holder["plan"], Calls())["alpha"](item, report)
        # A ref under another decision's prefix, which no halt of this item may name.
        later = FIRST + timedelta(minutes=5)
        stray = BlobRef(output_key(DEPLOYMENT, RUN, later, "alpha", "x"), "0")
        return DecisionResult(result.header, (*result.outputs, stray), None)

    folder, the_plan, store, made, queue, alarms = _stub_loop(
        tmp_path, alpha, wrap=wrap, decisions=1
    )
    holder.update(store=store, plan=the_plan)
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        queue.put(_key("feed"))
        assert started.wait_for(lambda: (folder.root / _key("alpha")).exists(), 5)
    halt = store.read_result(_key("alpha"), FIRST).halt
    assert halt is not None and "could not store the result" in halt.reason
    assert [ref.key.rsplit("/", 1)[1] for ref in halt.found_outputs] == ["value.json"]


def test_a_stage_that_keeps_the_plans_order_goes_on_after_a_halt(tmp_path) -> None:
    order: list[Item] = []
    holder: dict = {}

    def book(item: Item, report):
        order.append(item)
        if item.decision_at == FIRST + timedelta(minutes=1):
            raise Halt("the stand-in book refused decision 1")
        return handlers(holder["store"], holder["plan"], Calls())["book"](item, report)

    folder, the_plan, store, made, queue, _ = _stub_loop(
        tmp_path, book, decisions=3, component="book"
    )
    holder.update(store=store, plan=the_plan)
    by_stage = handlers(store, the_plan, Calls())
    for stage in ("feed", "alpha", "risk"):
        for decision in the_plan.decisions:
            item = Item(decision.at, stage)
            store.put_result(
                _key(stage, the_plan.decisions.index(decision)), by_stage[stage](item, lambda: None)
            )
    with running({"book": made}) as started:
        assert started.wait_for(lambda: (folder.root / _key("book", 2)).exists(), 5)
    assert order == [Item(d.at, "book") for d in the_plan.decisions]
    # Decision 2 started once decision 1's halt was stored. What its handler
    # makes of a halted book before it is the handler's own business.
    assert store.read_result(_key("book", 1), FIRST + timedelta(minutes=1)).halt is not None


def test_a_queue_that_refuses_a_delete_or_an_extend_leaves_the_loop_running(tmp_path) -> None:
    queue = StubQueue()
    queue.answers_fail = True
    folder, the_plan, store, made, _, _ = _stub_loop(
        tmp_path, decisions=2, queue=queue, extend_every=0.05
    )
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        for n in range(2):
            queue.put(_key("feed", n))
        assert started.wait_for(lambda: (folder.root / _key("alpha", 1)).exists(), 5)
        assert started.wait_for(lambda: len(queue.done_keys) >= 2, 5)
        __import__("time").sleep(0.3)
        assert not started.errors and started.threads["alpha"].is_alive()


def _let_through_once_it_alarms(waiting: Waiting, alarms) -> None:
    """Let a handler that still reports finish once the loop has failed, as the loop waits."""
    assert _wait(lambda: any("could not answer its queue" in r for r in alarms.reasons), 5)
    waiting.go.set()


def _run_until_it_fails(made, then=lambda: None) -> list[BaseException]:
    failures: list[BaseException] = []

    def run() -> None:
        try:
            made.run()
        except BaseException as error:  # noqa: BLE001 - the test reads it
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert made.wait_started(5)
    then()
    thread.join(10)
    assert not thread.is_alive(), "the loop went on"
    return failures


def test_an_error_from_extend_other_than_an_unavailable_queue_ends_the_loop(tmp_path) -> None:
    queue = StubQueue()
    queue.extend_error = KeyError("ReceiptHandle")
    holder: list = []
    waiting = Waiting(holder)
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, waiting, decisions=1, queue=queue, extend_every=0.05
    )
    holder.extend((store, the_plan))

    def feed_after_the_start_check() -> None:
        # The event, not the start check, starts the decision, so its message
        # is held and extended.
        _feed(store, the_plan)
        queue.put(_key("feed"))
        _let_through_once_it_alarms(waiting, alarms)

    failures = _run_until_it_fails(made, feed_after_the_start_check)
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert alarms.reasons and "could not answer its queue" in alarms.reasons[0]


@pytest.mark.parametrize("where", ["receive", "refused", "answer", "bug"])
def test_an_error_whose_text_utf_8_cannot_encode_is_escaped(tmp_path, where: str) -> None:
    """A channel that sends it over a wire takes each alarm that holds it."""
    from ithildinruntime.core import QueueUnavailable

    error = OSError("no such pipe: \udcff")
    queue = StubQueue()
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, decisions=1, queue=queue, deaf_after=0.1
    )

    def feed() -> None:
        if where == "receive":
            queue.error = error
        elif where == "refused":
            queue.error = QueueUnavailable(str(error))
        elif where == "answer":
            queue.done_error = error
        else:

            async def broken(events) -> None:
                raise error

            made._on_events = broken
        _feed(store, the_plan)
        queue.put(_key("feed"))

    failures = _run_until_it_fails(made, feed)
    assert len(failures) == 1
    # A bug in the loop's own code ends it with no alarm.
    assert len(alarms.reasons) == (where != "bug")
    # What the alarms and the hold's alarm say of it.
    for reason in (*alarms.reasons, made._why_stopped()):
        assert "no such pipe: \\udcff" in reason and reason.encode()


def test_a_silent_queues_answer_utf_8_cannot_encode_is_escaped_in_its_alarm(tmp_path) -> None:
    from ithildinruntime.core import QueueUnavailable

    _, _, _, made, _, alarms = _stub_loop(tmp_path, decisions=1, deaf_after=60.0)
    made._unanswered_since = 0.0
    made._alarm_unanswered(61.0, QueueUnavailable("no such pipe: \udcff"))
    (reason,) = alarms.reasons
    assert "no such pipe: \\udcff" in reason and reason.encode()


def test_a_queue_that_refuses_every_receive_ends_the_loop_after_a_bound(tmp_path) -> None:
    from ithildinruntime.core import QueueUnavailable

    queue = StubQueue()
    queue.error = QueueUnavailable("AccessDenied: the role may not receive")
    _, _, _, made, _, alarms = _stub_loop(tmp_path, queue=queue, deaf_after=0.5)
    failures = _run_until_it_fails(made)
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert "refused every receive" in alarms.reasons[0] and "AccessDenied" in alarms.reasons[0]


def test_a_capture_still_held_is_tried_once_more_as_the_loop_stops(tmp_path) -> None:
    from ithildinruntime.core import StageKind

    refusing: list = []

    def wrap(base):
        refusing.append(Refusing(base, creates=("/alpha/capture.json",)))
        return refusing[0]

    holder: dict = {}
    capture = output_key(DEPLOYMENT, RUN, FIRST, "alpha", "capture")

    def alpha(item: Item, report):
        holder["store"].put(StageKind.CAPTURING, capture, b"what the broker said")
        raise AssertionError("put should halt while S3 refuses the capture")

    # The ticker retries only once every 100 seconds, so only the stop's try runs.
    folder, the_plan, store, made, queue, _ = _stub_loop(
        tmp_path, alpha, wrap=wrap, decisions=1, extend_every=100
    )
    holder["store"] = made._storage
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        queue.put(_key("feed"))
        assert started.wait_for(lambda: (folder.root / _key("alpha")).exists(), 5)
        refusing[0].down = False
        __import__("time").sleep(0.3)
        assert not (folder.root / capture).exists()
    assert (folder.root / capture).exists() and made._storage.unstored() == ()


def test_a_start_check_the_store_cut_short_runs_again_once_s3_answers(tmp_path) -> None:
    refusing: list = []

    def wrap(base):
        refusing.append(Refusing(base, gets=("/alpha/result.json",)))
        return refusing[0]

    folder, the_plan, store, made, _, _ = _stub_loop(
        tmp_path, wrap=wrap, decisions=1, extend_every=0.1
    )
    _feed(store, the_plan)
    with running({"alpha": made}) as started:
        assert refusing[0].refused > 0
        refusing[0].down = False
        # No event comes, so only the ticker's second full check starts it.
        assert started.wait_for(lambda: (folder.root / _key("alpha")).exists(), 5)


def test_a_slow_queue_call_never_holds_up_a_check(tmp_path) -> None:
    import time

    queue = StubQueue()
    queue.extend_takes = 1.5
    holder: list = []
    waiting = Waiting(holder)
    folder, the_plan, store, made, _, _ = _stub_loop(
        tmp_path, waiting, decisions=3, queue=queue, extend_every=0.05, pool_size=3
    )
    holder.extend((store, the_plan))
    feed = handlers(store, the_plan, Calls())["feed"]

    def feed_one(n: int) -> None:
        at = the_plan.decisions[n].at
        store.put_result(_key("feed", n), feed(Item(at, "feed"), lambda: None))

    with running({"alpha": made}) as started:
        feed_one(0)
        feed_one(1)
        queue.put(_key("feed", 0))
        queue.put(_key("feed", 1))
        # Both handlers hold a message, and each extend takes a second and a half.
        assert started.wait_for(lambda: len(waiting.started) == 2, 5)
        time.sleep(0.3)
        feed_one(2)
        began = time.monotonic()
        queue.put(_key("feed", 2))
        assert started.wait_for(lambda: len(waiting.started) == 3, 5)
        assert time.monotonic() - began < 0.7


def test_one_refusal_after_a_busy_pool_leaves_the_loop_running(tmp_path) -> None:
    """The refusals count from the first one, not from the queue's last answer."""
    import time

    queue = StubQueue()
    holder: list = []
    waiting = Waiting(holder)
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, waiting, decisions=1, queue=queue, pool_size=1, deaf_after=0.5
    )
    holder.extend((store, the_plan))
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        queue.put(_key("feed"))
        assert started.wait_for(lambda: bool(waiting.started), 5)
        # The only handler is busy past the bound, so the loop asks nothing.
        time.sleep(1.0)
        queue.refusals = 1
        waiting.go.set()
        assert started.wait_for(lambda: (folder.root / _key("alpha")).exists(), 5)
        assert started.wait_for(lambda: queue.refusals == 0, 5)
        time.sleep(0.3)
        assert not started.errors and started.threads["alpha"].is_alive()
    assert alarms.reasons == []


def test_refusals_broken_by_an_answer_never_add_up_to_the_bound(tmp_path) -> None:
    import time

    queue = StubQueue()
    _, _, _, made, _, alarms = _stub_loop(tmp_path, queue=queue, deaf_after=0.3)
    with running({"alpha": made}) as started:
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            # Two refusals take about a tenth of a second, and then it answers.
            queue.refusals = 2
            time.sleep(0.15)
        assert not started.errors and started.threads["alpha"].is_alive()
    assert alarms.reasons == []


class HeldClock:
    """Wall time as it is, and a monotonic reading a case sets by hand."""

    def __init__(self) -> None:
        self.at = 0.0

    def now(self):
        return SystemTimer().now()

    def monotonic(self) -> float:
        return self.at

    def after(self, seconds: float) -> object:
        raise NotImplementedError


@pytest.mark.parametrize(("past", "ends"), [(-0.001, False), (0.0, False), (0.001, True)])
def test_the_loop_gives_up_only_once_the_refusals_last_longer_than_the_bound(
    tmp_path, past, ends
) -> None:
    import time

    from ithildinruntime.core import QueueUnavailable

    clock = HeldClock()
    queue = StubQueue()
    queue.error = QueueUnavailable("AccessDenied: the role may not receive")
    _, _, _, made, _, alarms = _stub_loop(tmp_path, queue=queue, deaf_after=1.0, timer=clock)
    with running({"alpha": made}) as started:
        # The first refusal is at reading 0, and every later one at 1.0 + past.
        assert queue.entered.wait(5)
        time.sleep(0.1)
        clock.at = 1.0 + past
        time.sleep(0.4)
        assert started.threads["alpha"].is_alive() is not ends
    assert bool(alarms.reasons) is ends


def test_an_error_from_a_delete_other_than_an_unavailable_queue_ends_the_loop(tmp_path) -> None:
    queue = StubQueue()
    queue.done_error = KeyError("ReceiptHandle")
    folder, the_plan, store, made, _, alarms = _stub_loop(tmp_path, decisions=1, queue=queue)

    def feed_after_the_start_check() -> None:
        _feed(store, the_plan)
        queue.put(_key("feed"))

    failures = _run_until_it_fails(made, feed_after_the_start_check)
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert "could not answer its queue" in str(failures[0])
    assert alarms.reasons and "could not answer its queue" in alarms.reasons[0]


def test_refusals_before_a_busy_pool_do_not_count_across_it(tmp_path) -> None:
    """The queue refuses, the pool fills with no answer between, then one more refusal."""
    import time

    from ithildinruntime.core import QueueUnavailable

    refusing: list = []

    def wrap(base):
        refusing.append(Refusing(base, gets=("/alpha/result.json",)))
        return refusing[0]

    clock = HeldClock()
    queue = StubQueue()
    queue.error = QueueUnavailable("ServiceUnavailable: the queue is down")
    holder: list = []
    waiting = Waiting(holder)
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        waiting,
        wrap=wrap,
        decisions=1,
        queue=queue,
        pool_size=1,
        deaf_after=1.0,
        extend_every=0.1,
        timer=clock,
    )
    holder.extend((store, the_plan))
    _feed(store, the_plan)
    with running({"alpha": made}) as started:
        # The start check meets a store that does not answer, and the queue
        # refuses every receive from reading 0.
        assert started.wait_for(lambda: refusing[0].refused > 0 and queue.entered.is_set(), 5)
        refusing[0].down = False
        # The ticker's recheck, not an answer from the queue, fills the pool.
        clock.at = 0.5
        assert started.wait_for(lambda: bool(waiting.started), 5)
        clock.at = 5.0
        queue.error = None
        queue.refusals = 1
        waiting.go.set()
        assert started.wait_for(lambda: queue.refusals == 0, 5)
        time.sleep(0.3)
        assert not started.errors and started.threads["alpha"].is_alive()
    # The store's own outage alarms, and nothing says the queue stopped answering.
    assert not [reason for reason in alarms.reasons if "stopped receiving" in reason]


def test_an_error_from_a_release_other_than_an_unavailable_queue_ends_the_loop(
    tmp_path,
) -> None:
    import time

    # A receive waits up to two seconds, so the one in flight when the check
    # takes the only handler is the one that hears the second event.
    queue = StubQueue(wait=2.0)
    queue.release_error = KeyError("ReceiptHandle")
    holder: list = []
    waiting = Waiting(holder)
    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, waiting, decisions=2, queue=queue, pool_size=1
    )
    holder.extend((store, the_plan))

    def fill_the_pool_then_send_one_more() -> None:
        _feed(store, the_plan)
        # The first decision takes the only handler from a check of its keys,
        # so the second's event has no handler and goes back to the queue.
        queue.put(_key("feed", 5))
        end = time.monotonic() + 5
        while not waiting.started and time.monotonic() < end:
            time.sleep(0.01)
        queue.put(_key("feed", 1))
        _let_through_once_it_alarms(waiting, alarms)

    failures = _run_until_it_fails(made, fill_the_pool_then_send_one_more)
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert alarms.reasons and "could not answer its queue" in alarms.reasons[0]


def test_a_deaf_queue_alarms_on_time_while_a_backlog_keeps_the_pool_full(tmp_path) -> None:
    """A full pool clears the count that ends the loop, never the alarm."""
    import time

    from ithildinruntime.core import QueueUnavailable

    queue = StubQueue()
    queue.error = QueueUnavailable("ServiceUnavailable: the queue is down")
    holder: dict = {}

    def slow_alpha(item: Item, report):
        time.sleep(0.2)
        report()
        return handlers(holder["store"], holder["plan"], Calls())["alpha"](item, report)

    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path, slow_alpha, decisions=30, queue=queue, pool_size=1, deaf_after=0.5
    )
    holder.update(store=store, plan=the_plan)
    _feed(store, the_plan)
    with running({"alpha": made}) as started:
        assert started.wait_for(lambda: any("answered no receive" in r for r in alarms.reasons), 3)
        # Half a second more of refusals, a dozen of them, pages no second time.
        time.sleep(0.5)
        # The backlog is still running, so the loop has not ended yet.
        assert started.threads["alpha"].is_alive()
        assert not (folder.root / _key("alpha", 29)).exists()
    assert sum("answered no receive" in r for r in alarms.reasons) == 1


# A stop reaches a handler that waits.


class WaitsForTheStop:
    """An alpha handler that waits on the loop's stop, then raises `Stopped`."""

    def __init__(self, until: threading.Event | None = None) -> None:
        # What the handler waits for between the stop and its end.
        self.until = until
        self.started: list[Item] = []
        self.entered = threading.Event()
        self.returned = threading.Event()

    def __call__(self, item: Item, report: Progress) -> DecisionResult:
        import time

        self.started.append(item)
        self.entered.set()
        try:
            end = time.monotonic() + 10
            while time.monotonic() < end:
                report()
                if report.wait_for_stop(0.5):
                    if self.until is not None:
                        assert self.until.wait(10)
                    raise Stopped("asked to stop")
            raise AssertionError("no stop came")
        finally:
            self.returned.set()


def _waiting_on_an_event(made, store, the_plan, queue, entered: threading.Event) -> None:
    """Start the one decision from its event, so its handler holds a message."""
    assert made.wait_started(10)
    _feed(store, the_plan)
    queue.put(_key("feed"))
    assert entered.wait(10)
    # The message is attached on the event loop's thread, just after the start.
    assert _wait(lambda: all(r.messages for r in list(made._running.values())), 5)


@pytest.mark.parametrize("ends", ["stop", "queue-fails", "bug"])
def test_a_stop_ends_a_wall_clocks_wait_and_leaves_the_decision_for_later(
    tmp_path, ends: str
) -> None:
    import time

    entered = threading.Event()
    returned: list[float] = []

    def alpha(item: Item, report: Progress):
        entered.set()
        try:
            # The next report is 5 seconds off, so only the stop wakes it sooner.
            clock = WallClock(SystemTimer(), servers=never_asked(), interval=5.0)
            clock.wait_until(datetime.now(UTC) + timedelta(seconds=8), report)
        except Exception as error:
            # A handler's own catch never turns a stop into a halt.
            raise Halt("the wait failed") from error
        finally:
            returned.append(time.monotonic())
        raise AssertionError("the wait was not stopped")

    folder, the_plan, store, made, queue, alarms = _stub_loop(tmp_path, alpha, decisions=1)
    thread, failures = _run_in_thread(made)
    _waiting_on_an_event(made, store, the_plan, queue, entered)
    time.sleep(0.3)
    assert not returned
    asked = time.monotonic()
    if ends == "stop":
        made.stop()
    elif ends == "queue-fails":
        queue.error = KeyError("ReceiptHandle")
    else:

        async def broken(events) -> None:
            raise RuntimeError("a bug in the loop's own code")

        made._on_events = broken
        queue.put(_key("risk"))
    thread.join(4)
    assert not thread.is_alive() and returned and returned[0] - asked < 2.0
    assert queue.released == [_key("feed")] and queue.done_keys == []
    assert not (folder.root / _key("alpha")).exists()
    if ends == "stop":
        assert not failures and alarms.reasons == []
    elif ends == "bug":
        assert len(failures) == 1 and isinstance(failures[0], RuntimeError)
        # Whatever a bug alarms, nothing alarms for the stopped handler.
        assert not any("stage alpha" in r for r in alarms.reasons)
    else:
        assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
        assert len(alarms.reasons) == 1 and "stopped receiving" in alarms.reasons[0]
    # The restarted process decides it.
    _, _, _, again, _, _ = _stub_loop(tmp_path, decisions=1)
    with running({"alpha": again}) as started:
        assert started.wait_for(lambda: (folder.root / _key("alpha")).exists(), 5)
    assert store.read_result(_key("alpha"), FIRST).halt is None


def test_a_handler_ending_before_the_loop_hears_the_stop_is_released_and_not_started_again(
    tmp_path,
) -> None:
    """`stop` asks on its caller's thread, and the event loop hears it a moment later."""
    import time

    alpha = WaitsForTheStop()
    folder, the_plan, store, made, queue, alarms = _stub_loop(tmp_path, alpha, decisions=1)
    thread, failures = _run_in_thread(made)
    _waiting_on_an_event(made, store, the_plan, queue, alpha.entered)
    # The window alone: asked, and not yet heard.
    made._stop_asked.set()
    assert _wait(lambda: queue.released == [_key("feed")], 5)
    assert _wait(lambda: not made._running, 5)
    # The check after the finished handler would have started it again by now.
    time.sleep(0.3)
    assert len(alpha.started) == 1 and thread.is_alive()
    made.stop()
    thread.join(5)
    assert not thread.is_alive() and not failures
    assert queue.released == [_key("feed")] and queue.done_keys == []
    assert not (folder.root / _key("alpha")).exists() and alarms.reasons == []


def test_a_handler_ending_on_a_stop_inside_the_drain_is_released(tmp_path) -> None:
    draining = threading.Event()
    alpha = WaitsForTheStop(until=draining)
    folder, the_plan, store, made, queue, alarms = _stub_loop(tmp_path, alpha, decisions=1)
    thread, failures = _run_in_thread(made)
    _waiting_on_an_event(made, store, the_plan, queue, alpha.entered)
    drain = made._drain

    async def drains() -> None:
        draining.set()
        await drain()

    made._drain = drains
    made.stop()
    thread.join(5)
    assert not thread.is_alive() and not failures and alpha.returned.is_set()
    assert queue.released == [_key("feed")] and queue.done_keys == []
    assert len(alpha.started) == 1
    assert not (folder.root / _key("alpha")).exists() and alarms.reasons == []


def test_a_handler_that_finishes_after_a_stop_stores_its_result_and_reports(tmp_path) -> None:
    """A stop cuts short only a wait, so work in hand ends as it would have without one."""
    import time

    entered = threading.Event()
    stopped = threading.Event()
    holder: list = []

    def alpha(item: Item, report: Progress):
        entered.set()
        assert stopped.wait(10) and report.wait_for_stop(5)
        # Works on past the heartbeat limit, reporting, which a stop never ends.
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            report()
            time.sleep(0.02)
        ended.append(time.monotonic())
        return handlers(*holder, Calls())["alpha"](item, report)

    ended: list[float] = []
    beats: list[float] = []
    _, the_plan, store, made, queue, alarms = _stub_loop(
        tmp_path,
        alpha,
        decisions=1,
        tick=0.02,
        heartbeat_limit=0.4,
        beat=lambda: beats.append(time.monotonic()),
    )
    holder.extend((store, the_plan))
    thread, failures = _run_in_thread(made)
    _waiting_on_an_event(made, store, the_plan, queue, entered)
    asked = time.monotonic()
    made.stop()
    stopped.set()
    thread.join(5)
    assert not thread.is_alive() and not failures
    assert queue.done_keys == [_key("feed")] and queue.released == []
    assert store.read_result(_key("alpha"), FIRST).halt is None and alarms.reasons == []
    # The reports after the stop count, so the beat goes on while the handler works.
    assert [b for b in beats if asked + 0.8 < b < ended[0]]


class StopsDuringARead(RefusingResults):
    """A store that asks a loop to stop as it answers a read of the feed's result."""

    made: ReceiveLoop | None = None

    def get(self, key):
        if self.made is not None and key.endswith("/feed/result.json"):
            self.made._stop_asked.set()
        return self._store.get(key)


@pytest.mark.parametrize("woken_by", ["event", "check"])
def test_a_stop_asked_while_the_loop_reads_keys_starts_nothing(tmp_path, woken_by: str) -> None:
    stores: list[StopsDuringARead] = []

    def wrap(base):
        stores.append(StopsDuringARead(base))
        return stores[0]

    alpha = WaitsForTheStop()
    _, the_plan, store, made, queue, _ = _stub_loop(tmp_path, alpha, wrap=wrap, decisions=1)
    thread, failures = _run_in_thread(made)
    assert made.wait_started(10)
    _feed(store, the_plan)
    stores[0].made = made
    # Both events wake a check. Only the feed's names a decision to start first.
    key = _key("feed") if woken_by == "event" else _key("risk")
    queue.put(key)
    assert _wait(lambda: key in queue.released + queue.done_keys, 5)
    assert _wait(made._stop_asked.is_set, 5)
    # A handler the check started would have begun by now.
    assert not alpha.entered.wait(0.3)
    made.stop()
    thread.join(5)
    assert not thread.is_alive() and not failures
    assert alpha.started == []
    if woken_by == "event":
        assert queue.released == [key] and queue.done_keys == []


def test_a_handler_that_raises_stopped_with_no_stop_asked_halts_and_alarms(tmp_path) -> None:
    started: list[Item] = []

    def alpha(item: Item, report: Progress):
        started.append(item)
        raise Stopped("a bug")

    folder, the_plan, store, made, queue, alarms = _stub_loop(tmp_path, alpha, decisions=1)
    with running({"alpha": made}) as loops:
        _feed(store, the_plan)
        queue.put(_key("feed"))
        assert loops.wait_for(lambda: _key("feed") in queue.done_keys, 5)
    assert len(started) == 1 and queue.released == []
    reason = store.read_result(_key("alpha"), FIRST).halt.reason
    assert "an error it did not handle: Stopped: a bug, and no stop was asked" in reason
    assert alarms.reasons == [reason]


class AnotherBase(BaseException):
    """One no list of named types can match."""


@pytest.mark.parametrize("asked", [False, True], ids=["running", "stopping"])
@pytest.mark.parametrize(
    "raised",
    [SystemExit("out"), KeyboardInterrupt("out"), CancelledError("out"), AnotherBase("out")],
    ids=lambda error: type(error).__name__,
)
def test_a_handler_that_ends_on_another_base_exception_halts_and_alarms(
    tmp_path, raised: BaseException, asked: bool
) -> None:
    """One start, one halt and one alarm, whether or not the loop was asked to stop."""
    import time

    started: list[Item] = []

    def alpha(item: Item, report: Progress):
        started.append(item)
        if asked:
            assert report.wait_for_stop(10)
        raise raised

    folder, the_plan, store, made, queue, alarms = _stub_loop(tmp_path, alpha, decisions=1)
    with running({"alpha": made}) as loops:
        _feed(store, the_plan)
        queue.put(_key("feed"))
        if asked:
            assert loops.wait_for(lambda: started, 5)
            assert _wait(lambda: all(r.messages for r in list(made._running.values())), 5)
        else:
            assert loops.wait_for(lambda: _key("feed") in queue.done_keys, 5)
            # A check would have started it again by now.
            time.sleep(0.3)
    assert len(started) == 1
    assert queue.done_keys == [_key("feed")] and queue.released == []
    assert (folder.root / _key("alpha")).exists()
    reason = store.read_result(_key("alpha"), FIRST).halt.reason
    said = f"{type(raised).__name__}: out"
    for part in ("alpha-1", "stage alpha", "20261102T150000Z", f"did not handle: {said}"):
        assert part in reason
    assert alarms.reasons == [reason]


class BrokenText(Exception):
    """An error whose text cannot be read."""

    def __str__(self) -> str:
        raise RuntimeError("no text")


class BrokenStop(Stopped):
    """A stop whose text cannot be read."""

    def __str__(self) -> str:
        raise RuntimeError("no text")


NOT_STORED = "could not store the result of stage alpha at decision 20261102T150000Z: CodecError"
# Each fault, and what its halt says of it.
HARD_TO_RECORD = {
    "outputs-not-refs": NOT_STORED,
    "outputs-bad-ref": NOT_STORED,
    "outputs-none": NOT_STORED,
    "outputs-one-bad": NOT_STORED,
    "outputs-a-set": NOT_STORED,
    "error-text": "BrokenText, whose text could not be read",
    "stray-stop-text": "BrokenStop, whose text could not be read, and no stop was asked",
    "not-utf-8": "ValueError: no such file: \\udcff",
    "returned-not-utf-8": "no such file: \\udcff",
    "channel-down": "ValueError: the handler's own error",
    "channel-exits": "ValueError: the handler's own error",
}


@pytest.mark.parametrize("broken", HARD_TO_RECORD)
def test_a_handlers_fault_that_is_hard_to_record_still_stores_a_halt(tmp_path, broken: str) -> None:
    started: list[Item] = []
    own = BlobRef(output_key(DEPLOYMENT, RUN, FIRST, "alpha", "x"), "0")

    class Channel(Alarms):
        def alarm(self, alarm: Alarm) -> None:
            reason = alarm.reason
            # As a channel that sends text over a wire does.
            reason.encode()
            super().alarm(alarm)
            if broken == "channel-down":
                raise RuntimeError("no channel")
            if broken == "channel-exits":
                raise SystemExit("no channel")

    def alpha(item: Item, report: Progress):
        started.append(item)
        if broken == "error-text":
            raise BrokenText
        if broken == "stray-stop-text":
            raise BrokenStop
        if broken == "returned-not-utf-8":
            record = made._storage.halt_record(Halt("no such file: \udcff"))
            return DecisionResult(header("alpha", item.decision_at, {}), (), record)
        if broken == "not-utf-8":
            # As in a file name Python could not decode.
            raise ValueError("no such file: \udcff")
        if broken.startswith("channel"):
            raise ValueError("the handler's own error")
        outputs, halt = {
            "outputs-not-refs": (("x",), None),
            "outputs-bad-ref": ((BlobRef(None, "0"),), None),
            "outputs-none": (None, None),
            "outputs-one-bad": ((own, BlobRef(own.key, None)), None),
            "outputs-a-set": (frozenset({own}), None),
        }[broken]
        return DecisionResult(header("alpha", item.decision_at, {}), outputs, halt)

    _, the_plan, store, made, queue, alarms = _stub_loop(
        tmp_path, alpha, decisions=1, alarms=Channel()
    )
    with running({"alpha": made}) as loops:
        _feed(store, the_plan)
        queue.put(_key("feed"))
        assert loops.wait_for(lambda: _key("feed") in queue.done_keys + queue.released, 5)
        assert not loops.errors
    assert len(started) == 1 and queue.released == []
    halt = store.read_result(_key("alpha"), FIRST).halt
    # A halt names the item's own outputs, in whatever the handler put them.
    assert halt is not None
    named = broken in ("outputs-a-set", "outputs-one-bad")
    assert halt.found_outputs == ((own,) if named else ())
    assert HARD_TO_RECORD[broken] in halt.reason
    if broken == "returned-not-utf-8":
        assert alarms.reasons == [] and halt.reason == HARD_TO_RECORD[broken]
    elif broken.startswith("outputs"):
        # Refused by the codec inside the store, which the stored halt alone says.
        assert alarms.reasons == []
    else:
        assert f"did not handle: {HARD_TO_RECORD[broken]}" in halt.reason
        assert len(alarms.reasons) == 1


@pytest.mark.parametrize(
    "raised", [SystemExit("no store"), Stopped("no store")], ids=["exit", "stop"]
)
@pytest.mark.parametrize("ends", ["raises", "returns-a-halt", "returns-a-result"])
def test_a_halt_is_held_whatever_the_store_raised(tmp_path, raised, ends: str) -> None:
    """A restart that lost it could store an output where the stage had halted."""
    started: list[Item] = []
    down = threading.Event()
    down.set()
    holder: list = []
    beats: list = []

    def alpha(item: Item, report: Progress):
        started.append(item)
        if ends == "raises":
            raise Halt("the handler's own halt")
        if ends == "returns-a-halt":
            record = made._storage.halt_record(Halt("the handler's own halt"))
            return DecisionResult(header("alpha", item.decision_at, {}), (), record)
        return handlers(*holder, Calls())["alpha"](item, report)

    _, the_plan, store, made, queue, alarms = _stub_loop(
        tmp_path,
        alpha,
        decisions=1,
        extend_every=0.05,
        tick=0.02,
        beat=lambda: beats.append(None),
    )
    holder.extend((store, the_plan))
    put = made._storage.put_result

    def refuses(key, *args, **kwargs):
        if down.is_set():
            raise raised
        return put(key, *args, **kwargs)

    made._storage.put_result = refuses
    thread, failures = _run_in_thread(made)
    assert made.wait_started(10)
    _feed(store, the_plan)
    queue.put(_key("feed"))
    held = _wait(lambda: any("holds 1 blob" in r for r in alarms.reasons), 5)
    alive = _holds_alive(thread, beats)
    down.clear()
    thread.join(5)
    assert len(started) == 1 and held and alive and not thread.is_alive()
    reason = store.read_result(_key("alpha"), FIRST).halt.reason
    if ends == "returns-a-result":
        ended = f"a result that could not be stored: {type(raised).__name__}: no store"
        assert f"{type(raised).__name__}: no store" in reason
    else:
        ended = "Halt: the handler's own halt"
        assert reason == "the handler's own halt"
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    # What the store raised and what the item had ended with, which a host that
    # kills the process would otherwise lose, in an alarm of its own.
    (hold,) = [r for r in alarms.reasons if "holds 1 blob" in r]
    (other,) = [r for r in alarms.reasons if r != hold]
    for said in (f"was not stored: {type(raised).__name__}: no store", f"had ended with {ended}"):
        assert said in other and said in str(failures[0])
        # The hold's alarm holds no text but the loop's own, which every channel takes.
        assert said not in hold
    # A held halt is this process's to store, so its event is not handed on.
    assert queue.done_keys == [_key("feed")] and queue.released == []


def _exits(*creates: str):
    """A wrap whose store ends on `SystemExit` at each of `creates`, and its one store."""
    made: list[Refusing] = []

    def wrap(base):
        made.append(Refusing(base, creates=creates))
        made[0].error = SystemExit("no write")
        return made[0]

    return wrap, made


def test_a_capture_whose_write_exits_is_held_and_retried_while_the_loop_goes_on(tmp_path) -> None:
    from ithildinruntime.core import StageKind

    capture = output_key(DEPLOYMENT, RUN, FIRST, "alpha", "capture")
    wrap, refusing = _exits("/alpha/capture.json")
    started: list[Item] = []
    beats: list = []

    def alpha(item: Item, report: Progress):
        started.append(item)
        made._storage.put(StageKind.CAPTURING, capture, b"what the broker said")
        raise AssertionError("the write should exit")

    folder, the_plan, store, made, queue, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        decisions=1,
        extend_every=0.05,
        tick=0.02,
        beat=lambda: beats.append(None),
    )
    thread, failures = _run_in_thread(made)
    assert made.wait_started(10)
    _feed(store, the_plan)
    queue.put(_key("feed"))
    assert _wait(lambda: (folder.root / _key("alpha")).exists(), 5)
    halt = store.read_result(_key("alpha"), FIRST).halt
    assert "did not handle: SystemExit: no write" in halt.reason
    assert [ref.key for ref in halt.unstored] == [capture]
    assert alarms.reasons == [halt.reason] and f"Captures not yet stored: {capture}" in halt.reason
    # The ticker's retries exit too, and the loop beats on.
    assert _wait(lambda: refusing[0].refused >= 3, 5)
    assert _holds_alive(thread, beats) and not failures
    refusing[0].down = False
    assert _wait(lambda: (folder.root / capture).exists(), 5)
    made.stop()
    thread.join(5)
    assert not thread.is_alive() and not failures and len(started) == 1


@pytest.mark.parametrize("stuck", ["capture", "another-halt", "channel"])
def test_a_held_halt_is_stored_whatever_else_the_hold_meets(tmp_path, stuck: str) -> None:
    from ithildinruntime.core import StageKind

    capture = output_key(DEPLOYMENT, RUN, FIRST, "alpha", "capture")
    wrap, refusing = _exits(capture, _key("alpha", 0), _key("alpha", 1))
    both = threading.Barrier(2)

    class Channel(Alarms):
        def alarm(self, alarm: Alarm) -> None:
            reason = alarm.reason
            super().alarm(alarm)
            if stuck == "channel" and refusing[0].down and "S3 has not stored" in reason:
                raise SystemExit("no channel")

    def alpha(item: Item, report: Progress):
        both.wait(5)
        if item.decision_at != FIRST:
            # Held second, so each try meets the first decision's halt first.
            assert _wait(lambda: made._unstored_halts, 5)
        if stuck == "capture" and item.decision_at == FIRST:
            made._storage.put(StageKind.CAPTURING, capture, b"what the broker said")
        raise Halt("the handler's own halt")

    folder, the_plan, store, made, _, alarms = _stub_loop(
        tmp_path,
        alpha,
        wrap=wrap,
        alarms=Channel(),
        decisions=2,
        pool_size=2,
        extend_every=0.05,
        tick=0.02,
    )
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    held = _wait(lambda: any("S3 has not stored" in r for r in alarms.reasons), 5)
    refusing[0].creates.discard(_key("alpha", 1))
    stored = _wait(lambda: (folder.root / _key("alpha", 1)).exists(), 5)
    alive = thread.is_alive()
    refusing[0].down = False
    thread.join(5)
    assert held and stored and alive and not thread.is_alive()
    for n in range(2):
        at = FIRST + timedelta(minutes=n)
        assert store.read_result(_key("alpha", n), at).halt is not None
    assert (folder.root / capture).exists() == (stuck == "capture")
    # `LoopFailed` names the first item alone, so each item's own alarm is its record.
    for n in range(2):
        (detail,) = [r for r in alarms.reasons if f"{_key('alpha', n)} was not stored: " in r]
        assert "SystemExit: no write" in detail and "had ended with" in detail
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)


@pytest.mark.parametrize(
    ("ends", "refusals"), [("returns-a-result", 1), ("returns-a-result", 2), ("raises", 1)]
)
def test_a_result_whose_refusal_cannot_be_read_still_stores_a_halt(
    tmp_path, ends: str, refusals: int
) -> None:
    """A result refused once has a halt in its place. A halt refused once is held."""
    held = (ends, refusals) != ("returns-a-result", 1)
    left = [BrokenText()] * refusals
    holder: list = []

    def alpha(item: Item, report: Progress):
        if ends == "raises":
            raise Halt("the handler's own halt")
        return handlers(*holder, Calls())["alpha"](item, report)

    _, the_plan, store, made, queue, _ = _stub_loop(tmp_path, alpha, decisions=1)
    holder.extend((store, the_plan))
    put = made._storage.put_result

    def refuses(*args, **kwargs):
        if left:
            raise left.pop()
        return put(*args, **kwargs)

    made._storage.put_result = refuses
    thread, failures = _run_in_thread(made)
    assert made.wait_started(10)
    _feed(store, the_plan)
    queue.put(_key("feed"))
    assert _wait(lambda: _key("feed") in queue.done_keys + queue.released, 5)
    if held:
        assert _wait(lambda: not thread.is_alive(), 5)
    made.stop()
    thread.join(5)
    assert not thread.is_alive() and queue.released == []
    said = "BrokenText, whose text could not be read"
    reason = store.read_result(_key("alpha"), FIRST).halt.reason
    assert reason == "the handler's own halt" if ends == "raises" else said in reason
    assert [said in str(f) for f in failures] == [True] * held


def test_a_held_halt_whose_refusal_cannot_be_read_is_dropped_with_its_alarm(tmp_path) -> None:
    _, _, _, made, _, alarms = _stub_loop(tmp_path, decisions=1)
    item = Item(FIRST, "alpha")
    held = made._halt_result(TABLE.stage("alpha"), item, Halt("the handler's own halt"))
    made._unstored_halts[_key("alpha")] = held

    def refuses(*args, **kwargs):
        raise BrokenText

    made._storage.put_result = refuses
    made._store_held()
    assert made._unstored_halts == {}
    (reason,) = alarms.reasons
    assert "could not be stored: BrokenText, whose text could not be read" in reason
    assert "Its reason: the handler's own halt" in reason


def test_a_halt_whose_record_the_codec_refuses_ends_the_loop_with_its_reason_alarmed(
    tmp_path,
) -> None:
    """A record built by hand, so its reason is not escaped as `halt_record` escapes one."""
    started: list[Item] = []

    class Channel(Alarms):
        def alarm(self, alarm: Alarm) -> None:
            reason = alarm.reason
            reason.encode()
            super().alarm(alarm)

    def alpha(item: Item, report: Progress):
        started.append(item)
        record = made._storage.halt_record(Halt("why"))
        record = dataclasses.replace(record, reason="why \udcff")
        return DecisionResult(header("alpha", item.decision_at, {}), (), record)

    folder, the_plan, store, made, queue, alarms = _stub_loop(
        tmp_path, alpha, decisions=1, alarms=Channel()
    )
    thread, failures = _run_in_thread(made)
    assert made.wait_started(10)
    _feed(store, the_plan)
    queue.put(_key("feed"))
    thread.join(5)
    assert not thread.is_alive() and len(started) == 1
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert not (folder.root / _key("alpha")).exists()
    for part in ("The item had ended with Halt: why \\udcff", "Its reason: why \\udcff"):
        assert len([r for r in alarms.reasons if part in r]) == 1 and part in str(failures[0])


def _break_the_work(made: ReceiveLoop, raised: BaseException) -> None:
    """Make the loop's own work in a handler's thread raise, by the building of every halt."""

    def no_halt(*args, **kwargs):
        raise raised

    made._storage.halt_record = no_halt


@pytest.mark.parametrize(
    ("raised", "said"),
    [
        (RuntimeError("no halt"), "RuntimeError: no halt"),
        (SystemExit("no halt"), "SystemExit: no halt"),
        # No stop was asked, so it is an error like any other.
        (Stopped("no halt"), "Stopped: no halt"),
        (BrokenText(), "BrokenText, whose text could not be read"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else "",
)
def test_an_error_in_the_loops_own_work_on_an_item_ends_the_loop(
    tmp_path, raised: BaseException, said: str
) -> None:
    """One start, then `LoopFailed`, with the item's event back on its queue."""
    started: list[Item] = []
    failed = threading.Event()
    holder: list = []

    def alpha(item: Item, report: Progress):
        started.append(item)
        if item.decision_at == FIRST:
            # Both handlers hold a message before either ends.
            assert _wait(lambda: len(started) == 2, 5)
            assert _wait(lambda: all(r.messages for r in list(made._running.values())), 5)
            raise ValueError("the handler's own error")
        # The other handler ends in the drain, and its result is stored.
        failed.wait(5)
        return handlers(*holder, Calls())["alpha"](item, report)

    folder, the_plan, store, made, queue, alarms = _stub_loop(
        tmp_path, alpha, decisions=2, pool_size=2
    )
    holder.extend((store, the_plan))
    _break_the_work(made, raised)
    fail = made._fail

    def fails(*args):
        fail(*args)
        failed.set()

    made._fail = fails
    thread, failures = _run_in_thread(made)
    assert made.wait_started(10)
    _feed(store, the_plan)
    # One receive takes both, so each handler starts from its own event.
    with queue.lock:
        queue.put(_key("feed", 0))
        queue.put(_key("feed", 1))
    ended = _wait(lambda: not thread.is_alive(), 8)
    made.stop()
    thread.join(5)
    assert len(started) == 2
    assert ended and not thread.is_alive()
    assert queue.released == [_key("feed", 0)] and queue.done_keys == [_key("feed", 1)]
    assert not (folder.root / _key("alpha", 0)).exists()
    assert store.read_result(_key("alpha", 1), FIRST + timedelta(minutes=1)).halt is None
    reason = (
        f"alpha-1 could not finish stage alpha at decision 20261102T150000Z: {said}. "
        "The item had ended with ValueError: the handler's own error"
    )
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert str(failures[0]) == reason
    # The handler's own error is sent first, as for any error it did not handle.
    assert len(alarms.reasons) == 2 and alarms.reasons[1] == reason
    assert "did not handle: ValueError: the handler's own error" in alarms.reasons[0]
    # The restarted process decides it.
    _, _, _, again, _, _ = _stub_loop(tmp_path, decisions=2)
    with running({"alpha": again}) as loops:
        assert loops.wait_for(lambda: (folder.root / _key("alpha", 0)).exists(), 5)
    assert store.read_result(_key("alpha", 0), FIRST).halt is None


@pytest.mark.parametrize("ends", ["halts", "returns-a-result"])
@pytest.mark.parametrize(
    ("raised", "said"),
    [(RuntimeError("no halt"), "RuntimeError: no halt"), (Stopped("no halt"), "Stopped: no halt")],
    ids=["error", "stray-stop"],
)
def test_an_error_in_the_loops_own_work_inside_the_drain_ends_the_run_with_it(
    tmp_path, raised: BaseException, said: str, ends: str
) -> None:
    """A stop does not hide it, and the halt's own reason goes out though its blob does not."""
    entered = threading.Event()

    def alpha(item: Item, report: Progress):
        entered.set()
        assert report.wait_for_stop(10)
        if ends == "halts":
            raise Halt("the handler's own halt")
        return handlers(*holder, Calls())["alpha"](item, report)

    def no_store(*args, **kwargs):
        raise RuntimeError("no store")

    holder: list = []
    folder, the_plan, store, made, queue, alarms = _stub_loop(tmp_path, alpha, decisions=1)
    holder.extend((store, the_plan))
    _break_the_work(made, raised)
    if ends == "returns-a-result":
        made._storage.put_result = no_store
        ended = "a result that could not be stored: RuntimeError: no store"
    else:
        ended = "Halt: the handler's own halt"
    thread, failures = _run_in_thread(made)
    _waiting_on_an_event(made, store, the_plan, queue, entered)
    made.stop()
    thread.join(5)
    assert not thread.is_alive()
    reason = (
        "alpha-1 could not finish stage alpha at decision 20261102T150000Z: "
        f"{said}. The item had ended with {ended}"
    )
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert str(failures[0]) == reason and alarms.reasons == [reason]
    # What the hold's alarm would say, which is the loop's own words alone.
    assert made._why_stopped() == "it could not finish stage alpha at decision 20261102T150000Z"
    assert queue.released == [_key("feed")] and queue.done_keys == []
    assert not (folder.root / _key("alpha")).exists()


def test_an_interrupt_of_the_process_stops_a_waiting_handler(tmp_path) -> None:
    """Ctrl-C reaches the main thread alone, and its drain asks each handler to stop."""
    import os
    import signal
    import time

    alpha = WaitsForTheStop()
    folder, the_plan, store, made, queue, alarms = _stub_loop(tmp_path, alpha, decisions=1)

    def interrupt() -> None:
        try:
            _waiting_on_an_event(made, store, the_plan, queue, alpha.entered)
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(5)
        finally:
            # A signal that did not arrive fails the test rather than hanging it.
            made.stop()

    helper = threading.Thread(target=interrupt, daemon=True)
    helper.start()
    # `asyncio.run` takes the signal only in the main thread, where pytest runs a
    # test, and only over Python's own handler, which a background shell removes.
    before = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            made.run()
    finally:
        signal.signal(signal.SIGINT, before)
    assert len(alpha.started) == 1 and alpha.returned.is_set()
    assert queue.released == [_key("feed")] and queue.done_keys == []
    assert not (folder.root / _key("alpha")).exists() and alarms.reasons == []


def test_a_handlers_report_reads_the_stop_and_cannot_ask_it(tmp_path) -> None:
    seen: list[tuple[Progress, bool]] = []
    entered = threading.Event()

    def alpha(item: Item, report: Progress):
        seen.append((report, report.wait_for_stop(0)))
        entered.set()
        assert report.wait_for_stop(10)
        raise Stopped("asked to stop")

    _, the_plan, store, made, _, _ = _stub_loop(tmp_path, alpha, decisions=1)
    _feed(store, the_plan)
    thread, failures = _run_in_thread(made)
    assert entered.wait(10)
    report, asked_first = seen[0]
    assert asked_first is False
    for offered in (report, Progress):
        assert [name for name in dir(offered) if not name.startswith("_")] == ["wait_for_stop"]
    made.stop()
    thread.join(5)
    assert not thread.is_alive() and not failures and report.wait_for_stop(0) is True
