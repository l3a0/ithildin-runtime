"""The receive loop: its pool, its order rule, a handler's failure and the heartbeat.

docs/design.md's `### How a component learns of a change` holds the rules,
and #81's acceptance names the first three tests below.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from ithildinruntime.core import (
    DecisionResult,
    EventKind,
    Halt,
    Retry,
    StoreUnavailable,
    result_key,
)
from ithildinruntime.runtime.loop import Item, LoopFailed, ReceiveLoop
from tests.support.queues import (
    COMPONENTS,
    CONFIG,
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
        return DecisionResult(header(stage, at, {}), result.outputs, None)

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
    """A store that takes every write but an alpha result."""

    def __init__(self, store) -> None:
        self._store = store

    def create(self, key, data, sha256):
        if key.endswith("/alpha/result.json"):
            raise StoreUnavailable(key, "503 SlowDown")
        return self._store.create(key, data, sha256)

    def __getattr__(self, name):
        return getattr(self._store, name)


def test_a_halt_that_cannot_be_stored_ends_the_loop(tmp_path) -> None:
    alarms = Alarms()

    def alpha(item: Item, report):
        raise ValueError("broken")

    folder, the_plan, store, made, pipe = _alpha_loop(
        tmp_path, alpha, alarms, store_wrapper=RefusingResults, decisions=1
    )
    made._storage._retry = Retry(timedelta(seconds=0.2), 0.05, 0.05)
    _feed(storage(folder.store(), alarms), the_plan)
    failures: list[BaseException] = []

    def run() -> None:
        try:
            made.run()
        except BaseException as error:  # noqa: BLE001 - the test reads it
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(20)
    pipe.close()
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert "alpha/result.json" in str(failures[0])


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
        config_hash=CONFIG,
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
    alarms = Alarms()
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
    folder, the_plan, store, made, queue, _ = _stub_loop(
        tmp_path, waiting, decisions=2, pool_size=1
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
    started = running({"alpha": made})
    with started:
        __import__("time").sleep(0.2)
        queue.put(_key("feed"))
        made.stop()
        queue.free.set()
        __import__("time").sleep(0.3)
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

    folder, the_plan, store, made, queue, alarms = _stub_loop(tmp_path, wrap=wrap, decisions=1)
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
