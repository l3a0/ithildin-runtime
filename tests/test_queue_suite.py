"""The queue suite: the named pipe fed by the folder, and SQS fed by S3 through EventBridge.

docs/architecture/10-quality.md's `### How CI runs them` lists what it holds.
Each case that both queues guarantee runs once over the folder and its named
pipes, and once over S3, EventBridge and SQS on moto's server, with the rules
generated from the stand-in table loaded into the emulator rather than routed
by hand. The cases a pipe cannot keep, extending, deleting and returning a
message, run on SQS alone.

The run against the test environment's queues on each push to `main` waits
on #78's queues.
"""

from __future__ import annotations

import itertools
import json
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from ithildinruntime.adapters import Faults
from ithildinruntime.adapters.pipe import PipeQueue, send
from ithildinruntime.core import EventKind, event_body, output_key, result_key
from ithildinruntime.runtime.loop import Item
from tests.support import queues as q

_names = itertools.count()


@pytest.fixture
def cloud(emulator: str) -> q.Cloud:
    return q.Cloud.make(emulator, f"queue-suite-{next(_names)}", visibility=30)


@pytest.fixture(params=["folder", "sqs"])
def side(request: pytest.FixtureRequest, tmp_path: Path) -> Side:
    if request.param == "folder":
        return FolderSide(q.Folder(tmp_path / "store", tmp_path / "queues"))
    return CloudSide(request.getfixturevalue("cloud"))


class Side:
    """One of the two pairs of a store and its queues, as a case drives it."""

    bucket: str

    def store(self):
        raise NotImplementedError

    def queue(self, component: str):
        raise NotImplementedError

    def heard(self, component: str) -> list[str]:
        """The keys of every event waiting for a component."""
        raise NotImplementedError

    def repeat(self, key: str) -> None:
        """Deliver a stored blob's event again, as a store repeats one."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class FolderSide(Side):
    def __init__(self, folder: q.Folder) -> None:
        self.folder = folder
        self.bucket = folder.bucket
        self.pipes: dict[str, PipeQueue] = {}

    def store(self):
        return self.folder.store()

    def queue(self, component: str) -> PipeQueue:
        if component not in self.pipes:
            self.pipes[component] = self.folder.queue(component)
        return self.pipes[component]

    def heard(self, component: str) -> list[str]:
        found = []
        while events := self.queue(component).receive(10):
            found.extend(e.key for e in events)
        return found

    def repeat(self, key: str) -> None:
        self.store().announce(key)

    def close(self) -> None:
        for pipe in self.pipes.values():
            pipe.close()


class CloudSide(Side):
    def __init__(self, cloud: q.Cloud) -> None:
        self.cloud = cloud
        self.bucket = cloud.bucket

    def store(self):
        return self.cloud.store()

    def queue(self, component: str):
        return self.cloud.queue(component)

    def heard(self, component: str) -> list[str]:
        return [m["detail"]["object"]["key"] for m in self.cloud.messages(component)]

    def repeat(self, key: str) -> None:
        # The test role holds no SendMessage, so on the test queues this case
        # needs S3's own repeats. On the emulator the suite sends the copy.
        body = event_body(self.bucket, key, EventKind.CREATED, "repeat", "0")
        sqs = q.client("sqs", self.cloud.endpoint)
        for component in q.COMPONENTS:
            route_keys = [r for r in q.routes(q.TABLE, q.DEPLOYMENT) if r.reader == component]
            if any(r.matches(EventKind.CREATED, key) for r in route_keys):
                sqs.send_message(QueueUrl=self.cloud.urls[component], MessageBody=body.decode())


def _write(store, key: str, data: bytes = b"{}") -> None:
    from ithildinruntime.core import sha256_of

    store.create(key, data, sha256_of(data))


AT = q.FIRST


# A write reaches the queue of every reader its routes name, and no other.


def test_a_write_reaches_every_reader_its_routes_name_and_no_other(side: Side) -> None:
    for component in q.COMPONENTS:
        side.queue(component)
    store = side.store()
    written = {
        result_key(q.DEPLOYMENT, q.RUN, AT, "feed"): {"alpha", "risk"},
        result_key(q.DEPLOYMENT, q.RUN, AT, "alpha"): {"book"},
        result_key(q.DEPLOYMENT, q.RUN, AT, "risk"): {"book"},
        result_key(q.DEPLOYMENT, q.RUN, AT, "book"): {"book"},
        f"runs/{q.DEPLOYMENT}/{q.RUN}/stop.json": {"book"},
        output_key(q.DEPLOYMENT, q.RUN, AT, "feed", "value"): set(),
        result_key("other", q.RUN, AT, "feed"): set(),
    }
    for key in written:
        _write(store, key)
    time.sleep(0.5)
    heard = {component: sorted(side.heard(component)) for component in q.COMPONENTS}
    side.close()
    expected = {
        component: sorted(k for k, readers in written.items() if component in readers)
        for component in q.COMPONENTS
    }
    assert heard == expected


# An event can arrive twice, late, out of order or not at all, and a reader
# still handles each decision once, in the plan's order.


def _run_with_restarts(tmp: Path, faults: Faults, decisions: int) -> tuple[dict, q.Calls, int]:
    """Run the stand-in stages to the end, restarting every loop whenever they go quiet."""
    folder = q.Folder(tmp / "store", tmp / "queues")
    the_plan = q.plan(decisions)
    calls = q.Calls()
    alarms = q.Alarms()
    keys = q.every_result(the_plan)
    restarts = -1
    while restarts < 10:
        restarts += 1
        pipes = {name: folder.queue(name) for name in q.COMPONENTS}
        stores = {name: q.storage(folder.store(faults), alarms) for name in q.COMPONENTS}
        loops = {
            name: q.loop(
                name,
                the_plan=the_plan,
                queue=pipes[name],
                store=stores[name],
                bucket=folder.bucket,
                alarms=alarms,
                handlers_by_stage=q.handlers(stores[name], the_plan, calls),
            )
            for name in q.COMPONENTS
        }
        with q.running(loops) as running:
            last = -1
            while True:
                stored = sum((folder.root / k).exists() for k in keys)
                if stored == len(keys) or stored == last:
                    break
                last = stored
                running.wait_for(lambda: False, timeout=1.0)
        for pipe in pipes.values():
            pipe.close()
        if all((folder.root / k).exists() for k in keys):
            break
    assert alarms.reasons == []
    return q.folder_blobs(folder.root), calls, restarts


def test_repeated_late_reordered_and_lost_events_run_each_decision_once_in_order(
    tmp_path: Path,
) -> None:
    clean, _ = q.run_folder(tmp_path / "clean", decisions=10, pool_size=4)
    faults = Faults(seed=3, drop=0.3, repeat=0.5, delay=(0.0, 0.15))
    blobs, calls, restarts = _run_with_restarts(tmp_path / "faulty", faults, decisions=10)
    assert blobs == clean
    assert set(calls.counts.values()) == {1}
    books = [item.decision_at for item in calls.order if item.stage == "book"]
    assert books == [d.at for d in q.plan(10).decisions]


def test_a_reader_that_heard_no_event_recovers_by_its_keys_when_it_restarts(
    tmp_path: Path,
) -> None:
    clean, _ = q.run_folder(tmp_path / "clean", decisions=4, pool_size=4)
    blobs, calls, restarts = _run_with_restarts(
        tmp_path / "silent", Faults(seed=1, drop=1.0), decisions=4
    )
    assert blobs == clean and set(calls.counts.values()) == {1}
    # With every event dropped, only a restart's check of every decision moves
    # a reader that started before its inputs were stored.
    assert restarts >= 1


def test_the_folders_faults_drop_and_repeat_events(tmp_path: Path) -> None:
    key = result_key(q.DEPLOYMENT, q.RUN, AT, "feed")
    heard = {}
    for name, faults in {
        "dropped": Faults(seed=1, drop=1.0),
        "repeated": Faults(seed=1, repeat=1.0),
        "late": Faults(seed=1, delay=(0.3, 0.3)),
    }.items():
        folder = q.Folder(tmp_path / name / "store", tmp_path / name / "queues")
        alpha = folder.queue("alpha")
        _write(folder.store(faults), key)
        # Each receive waits 0.2 seconds, so the late event comes in the second.
        heard[name] = [len(alpha.receive(10)), len(alpha.receive(10))]
        alpha.close()
    assert heard == {"dropped": [0, 0], "repeated": [2, 0], "late": [0, 1]}


# Dropping events while a reader is down.


def _alpha_alone(side: Side, the_plan, calls: q.Calls, alarms: q.Alarms):
    store = q.storage(side.store(), alarms)
    return q.loop(
        "alpha",
        the_plan=the_plan,
        queue=side.queue("alpha"),
        store=store,
        bucket=side.bucket,
        alarms=alarms,
        handlers_by_stage=q.handlers(store, the_plan, calls),
    )


def _feed_results(side: Side, the_plan) -> None:
    store = q.storage(side.store(), q.Alarms())
    feed = q.handlers(store, the_plan, q.Calls())["feed"]
    for decision in the_plan.decisions:
        item = Item(decision.at, "feed")
        store.put_result(result_key(q.DEPLOYMENT, q.RUN, decision.at, "feed"), feed(item, _no))


def _no() -> None:
    pass


def test_events_dropped_while_a_reader_is_down_are_recovered_when_it_starts(
    side: Side,
) -> None:
    the_plan = q.plan(3)
    if isinstance(side, FolderSide):
        # No reader holds alpha's pipe, so the folder drops its events.
        _feed_results(side, the_plan)
        assert not (side.folder.pipes / "alpha.pipe").exists()
    else:
        _feed_results(side, the_plan)
        time.sleep(0.5)
        q.client("sqs", side.cloud.endpoint).purge_queue(QueueUrl=side.cloud.urls["alpha"])
    calls, alarms = q.Calls(), q.Alarms()
    alpha = _alpha_alone(side, the_plan, calls, alarms)
    root_keys = [result_key(q.DEPLOYMENT, q.RUN, d.at, "alpha") for d in the_plan.decisions]
    store = side.store()
    with q.running({"alpha": alpha}) as running:
        assert running.wait_for(lambda: all(_exists(store, k) for k in root_keys), 20)
    side.close()
    assert sorted(calls.counts) == [Item(d.at, "alpha") for d in the_plan.decisions]


def _exists(store, key: str) -> bool:
    from ithildinruntime.core import BlobMissing

    try:
        store.head(key)
    except BlobMissing:
        return False
    return True


# A writer killed between its write and the folder's delivery announces again.


def test_a_writer_killed_before_delivery_announces_its_result_when_it_restarts(
    tmp_path: Path,
) -> None:
    folder = q.Folder(tmp_path / "store", tmp_path / "queues")
    side = FolderSide(folder)
    the_plan = q.plan(1)
    calls, alarms = q.Calls(), q.Alarms()
    alpha = _alpha_alone(side, the_plan, calls, alarms)
    key = result_key(q.DEPLOYMENT, q.RUN, AT, "alpha")
    with q.running({"alpha": alpha}) as running:
        # The feed writer stores its result and dies before the folder delivers it.
        silent = q.storage(folder.store(announce=False), alarms)
        feed = q.handlers(silent, the_plan, q.Calls())["feed"]
        silent.put_result(result_key(q.DEPLOYMENT, q.RUN, AT, "feed"), feed(Item(AT, "feed"), _no))
        assert not running.wait_for(lambda: (folder.root / key).exists(), 1.0)
        # The writer restarts: its loop finds its result stored and announces it.
        feed_store = q.storage(folder.store(), alarms)
        restarted = q.loop(
            "feed",
            the_plan=the_plan,
            queue=side.queue("feed"),
            store=feed_store,
            bucket=folder.bucket,
            alarms=alarms,
            handlers_by_stage=q.handlers(feed_store, the_plan, calls),
        )
        with q.running({"feed": restarted}):
            assert running.wait_for(lambda: (folder.root / key).exists(), 10)
    side.close()
    assert calls.counts == {Item(AT, "alpha"): 1}


def test_s3_skips_an_announcement_because_it_announced_the_blob_itself(cloud: q.Cloud) -> None:
    store = cloud.store()
    key = result_key(q.DEPLOYMENT, q.RUN, AT, "feed")
    _write(store, key)
    store.announce(key)
    time.sleep(0.5)
    assert [m["detail"]["object"]["key"] for m in cloud.messages("alpha")] == [key]


# What both queues guarantee, run through the receive loop.


class Gate:
    """A handler that waits to be let through, counting how many it ran."""

    def __init__(self, report: bool = True) -> None:
        self.go = threading.Event()
        self.started = threading.Event()
        self.report = report
        self.calls = q.Calls()

    def __call__(self, store, the_plan):
        inner = q.handlers(store, the_plan, q.Calls())["alpha"]

        def alpha(item: Item, report):
            self.calls.add(item)
            self.started.set()
            while not self.go.wait(0.05):
                if self.report:
                    report()
            return inner(item, report)

        return alpha


class Counting:
    """A queue that counts the messages the loop holds, received and not yet answered."""

    def __init__(self, queue) -> None:
        self.queue = queue
        self.held = 0
        self.most = 0
        self.limits: list[int] = []
        self.lock = threading.Lock()

    def receive(self, limit):
        events = self.queue.receive(limit)
        with self.lock:
            self.limits.append(limit)
            self.held += len(events)
            self.most = max(self.most, self.held)
        return events

    def _answer(self, call, event) -> None:
        with self.lock:
            self.held -= 1
        call(event)

    def done(self, event) -> None:
        self._answer(self.queue.done, event)

    def extend(self, event) -> None:
        self.queue.extend(event)

    def release(self, event) -> None:
        self._answer(self.queue.release, event)


def _gated_alpha(side: Side, the_plan, gate: Gate, alarms: q.Alarms, **settings):
    store = q.storage(side.store(), alarms)
    queue = settings.pop("queue", None) or side.queue("alpha")
    return q.loop(
        "alpha",
        the_plan=the_plan,
        queue=queue,
        store=store,
        bucket=side.bucket,
        alarms=alarms,
        handlers_by_stage={"alpha": gate(store, the_plan)},
        **settings,
    )


def test_a_message_whose_item_is_in_a_handler_is_not_run_twice(side: Side) -> None:
    the_plan = q.plan(1)
    gate, alarms = Gate(), q.Alarms()
    alpha = _gated_alpha(side, the_plan, gate, alarms)
    feed_key = result_key(q.DEPLOYMENT, q.RUN, AT, "feed")
    done_key = result_key(q.DEPLOYMENT, q.RUN, AT, "alpha")
    store = side.store()
    with q.running({"alpha": alpha}) as running:
        _feed_results(side, the_plan)
        assert gate.started.wait(10)
        side.repeat(feed_key)
        side.repeat(feed_key)
        time.sleep(1.5)
        gate.go.set()
        assert running.wait_for(lambda: _exists(store, done_key), 10)
        time.sleep(0.5)
    if isinstance(side, CloudSide):
        assert side.cloud.messages("alpha") == []
    side.close()
    assert gate.calls.counts == {Item(AT, "alpha"): 1}


def test_the_loop_never_holds_more_messages_than_it_has_free_handlers(side: Side) -> None:
    the_plan = q.plan(6)
    gate, alarms = Gate(), q.Alarms()
    counting = Counting(side.queue("alpha"))
    alpha = _gated_alpha(side, the_plan, gate, alarms, queue=counting, pool_size=2)
    store = side.store()
    keys = [result_key(q.DEPLOYMENT, q.RUN, d.at, "alpha") for d in the_plan.decisions]
    with q.running({"alpha": alpha}) as running:
        _feed_results(side, the_plan)
        time.sleep(1.5)
        assert gate.calls.counts.total() == 2
        gate.go.set()
        assert running.wait_for(lambda: all(_exists(store, k) for k in keys), 20)
    side.close()
    assert counting.most <= 2 and max(counting.limits) <= 2
    assert set(gate.calls.counts.values()) == {1}


def test_a_hung_handler_stops_the_heartbeat(side: Side) -> None:
    the_plan = q.plan(2)
    beats: list[float] = []
    hung, alarms = Gate(report=False), q.Alarms()
    alpha = _gated_alpha(
        side,
        the_plan,
        hung,
        alarms,
        pool_size=2,
        heartbeat_limit=0.3,
        beat=lambda: beats.append(time.monotonic()),
    )
    with q.running({"alpha": alpha}):
        time.sleep(0.5)
        assert beats, "an idle loop beats"
        _feed_results(side, the_plan)
        assert hung.started.wait(10)
        time.sleep(1.0)
        stopped_at = time.monotonic()
        time.sleep(1.0)
        assert not [b for b in beats if b > stopped_at], "a hung handler kept the heartbeat"
        hung.go.set()
        time.sleep(1.0)
        assert [b for b in beats if b > stopped_at + 1.0], "the heartbeat came back"
    side.close()


# What only SQS keeps: extending, deleting and returning a message.


def test_a_handler_slower_than_the_visibility_timeout_keeps_its_message_hidden(
    emulator: str,
) -> None:
    cloud = q.Cloud.make(emulator, f"queue-suite-{next(_names)}", visibility=2)
    side = CloudSide(cloud)
    the_plan = q.plan(1)
    gate, alarms = Gate(), q.Alarms()
    settings = {"extend_every": 0.5}
    # Both instances start before the feed's result exists, so neither finds
    # the decision by its keys, and only the message can hand it to one.
    first = _gated_alpha(side, the_plan, gate, alarms, queue=cloud.queue("alpha", 2), **settings)
    second = _gated_alpha(side, the_plan, gate, alarms, queue=cloud.queue("alpha", 2), **settings)
    done_key = result_key(q.DEPLOYMENT, q.RUN, AT, "alpha")
    store = cloud.store()
    with q.running({"first": first, "second": second}) as running:
        _feed_results(side, the_plan)
        assert gate.started.wait(10)
        # Three visibility timeouts pass while the handler works.
        time.sleep(6)
        gate.go.set()
        assert running.wait_for(lambda: _exists(store, done_key), 10)
        time.sleep(0.5)
    assert gate.calls.counts == {Item(AT, "alpha"): 1}
    assert cloud.messages("alpha") == []


def test_stopping_returns_an_unfinished_message_at_once(cloud: q.Cloud) -> None:
    side = CloudSide(cloud)
    the_plan = q.plan(1)
    stuck, alarms = Gate(), q.Alarms()
    first = _gated_alpha(side, the_plan, stuck, alarms, stop_timeout=0.2)
    feed_key = result_key(q.DEPLOYMENT, q.RUN, AT, "feed")
    try:
        with q.running({"first": first}):
            _feed_results(side, the_plan)
            assert stuck.started.wait(10)
        # The queue hides a message for 30 seconds, so only the loop's release
        # brings it back this soon.
        assert [m["detail"]["object"]["key"] for m in cloud.messages("alpha")] == [feed_key]
    finally:
        stuck.go.set()


def test_a_message_whose_instance_dies_returns_to_the_queue(emulator: str, tmp_path) -> None:
    cloud = q.Cloud.make(emulator, f"queue-suite-{next(_names)}", visibility=2)
    side = CloudSide(cloud)
    the_plan = q.plan(1)
    marker = tmp_path / "started"
    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=q.hang_alpha, args=(cloud.endpoint, cloud.bucket, cloud.urls["alpha"], str(marker))
    )
    child.start()
    try:
        assert q.wait_for_file(marker, 60), "the child's loop never started"
        _feed_results(side, the_plan)
        assert q.wait_for_file(marker.with_suffix(".item"), 30)
        # The child extends its message while its handler works, so after two
        # visibility timeouts the message is still its own.
        time.sleep(4)
        assert cloud.messages("alpha") == []
        os.kill(child.pid, 9)
        child.join(10)
    finally:
        if child.is_alive():
            child.kill()
    # Nothing extends the message once its instance is dead, so it returns.
    feed_key = result_key(q.DEPLOYMENT, q.RUN, AT, "feed")
    time.sleep(2.5)
    assert [m["detail"]["object"]["key"] for m in cloud.messages("alpha")] == [feed_key]


# The pipe itself.


def test_a_pipe_with_no_reader_drops_the_event(tmp_path: Path) -> None:
    assert send(tmp_path, "alpha", b"{}") is False
    os.mkfifo(tmp_path / "alpha.pipe")
    assert send(tmp_path, "alpha", b"{}") is False


def test_two_readers_on_one_pipe_each_take_whole_events(tmp_path: Path) -> None:
    first = PipeQueue(tmp_path, "alpha", wait_seconds=0.2)
    second = PipeQueue(tmp_path, "alpha", wait_seconds=0.2)
    # macOS holds 16 records in a pipe, so the case stays under that.
    keys = [f"runs/ci/{q.RUN}/k{n:03d}/feed/result.json" for n in range(15)]
    for key in keys:
        assert send(tmp_path, "alpha", event_body("folder", key, EventKind.CREATED, "t", "s"))
    got = []
    for reader in itertools.cycle((first, second)):
        events = reader.receive(3)
        if not events and len(got) == len(keys):
            break
        assert len(events) <= 3
        got.extend(events)
    first.close()
    second.close()
    assert sorted(e.key for e in got) == keys
    assert all(e.kind is EventKind.CREATED for e in got)


def test_a_full_pipe_drops_the_event(tmp_path: Path) -> None:
    reader = PipeQueue(tmp_path, "alpha", wait_seconds=0.2)
    body = event_body("folder", "runs/ci/x/feed/result.json", EventKind.CREATED, "t", "s")
    sent = [send(tmp_path, "alpha", body) for _ in range(1000)]
    reader.close()
    assert sent[0] and not all(sent)


def test_an_event_too_long_for_one_record_is_refused_before_the_write(tmp_path: Path) -> None:
    folder = q.Folder(tmp_path / "store", tmp_path / "queues")
    key = f"runs/{q.DEPLOYMENT}/{q.RUN}/{'x' * 400}/feed/result.json"
    with pytest.raises(ValueError, match="record"):
        folder.store().create(key, b"{}", "0" * 64)
    assert not (folder.root / "runs").exists()


def test_a_folder_that_announces_needs_the_queue_folder(tmp_path: Path) -> None:
    from ithildinruntime.adapters import FolderStore

    with pytest.raises(ValueError, match="queue folder"):
        FolderStore(tmp_path, routes=q.routes(q.TABLE, q.DEPLOYMENT))


def test_the_sqs_events_parse_like_the_folders(cloud: q.Cloud) -> None:
    store = cloud.store()
    key = result_key(q.DEPLOYMENT, q.RUN, AT, "feed")
    _write(store, key)
    time.sleep(0.5)
    (event,) = cloud.queue("alpha").receive(10)
    assert (event.bucket, event.key, event.kind) == (cloud.bucket, key, EventKind.CREATED)
    assert event.etag and event.sequencer
    body = json.loads(event_body(cloud.bucket, key, EventKind.CREATED, "t", "s"))
    assert body["detail"]["object"]["key"] == key
