"""The receive loop: its pool, its order rule, a handler's failure and the heartbeat.

docs/design.md's `### How a component learns of a change` holds the rules,
and #81's acceptance names the first three tests below.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from ithildinruntime.core import (
    BlobRef,
    DecisionResult,
    EventKind,
    Halt,
    Retry,
    StoreUnavailable,
    output_key,
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

    failures = _run_until_it_fails(made, feed_after_the_start_check)
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert alarms.reasons and "could not answer its queue" in alarms.reasons[0]


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

    failures = _run_until_it_fails(made, fill_the_pool_then_send_one_more)
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert alarms.reasons and "could not answer its queue" in alarms.reasons[0]


def test_a_halt_that_cannot_be_stored_names_the_worker_in_loop_failed(tmp_path) -> None:
    alarms = Alarms()

    def alpha(item: Item, report):
        raise ValueError("broken")

    folder, the_plan, store, made, pipe = _alpha_loop(
        tmp_path, alpha, alarms, store_wrapper=RefusingResults, decisions=1
    )
    made._storage._retry = Retry(timedelta(seconds=0.2), 0.05, 0.05)
    _feed(storage(folder.store(), alarms), the_plan)
    failures = _run_until_it_fails(made)
    pipe.close()
    assert "alpha-1" in str(failures[0]) and "was not stored" in str(failures[0])
