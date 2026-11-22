"""The queue suite: the named pipe fed by the folder, and SQS fed by S3 through EventBridge.

docs/architecture/10-quality.md's `### How CI runs them` lists what it holds.
Each case that both queues guarantee runs over the folder and its named pipes,
over S3, EventBridge and SQS on moto's server, with the rules generated from
the stand-in table loaded into the emulator rather than routed by hand, and,
marked `aws`, over the test environment's bucket and queues. The cases a pipe
cannot keep, extending, deleting and returning a message, run on SQS alone.

Events repeated, delayed, reordered and dropped at random come from the
folder's seeded `Faults`, so those cases run over the folder alone, and so do
the pipe's own cases.

On the test queues, which ci.yml's `aws` job reaches on each push to `main`:

1. Every case shares the bucket and the queues, so each case's keys sit under
   the run's date and an hour of the case's own, which `_AWS_CASES` numbers,
   and every read keeps the case's own keys and deletes the rest.
2. The case for events dropped while a reader is down receives and deletes
   them, on moto too, because SQS allows one purge a minute and the job's own
   purge has just spent it.
3. The case that repeats an event while its decision is in a handler skips,
   because S3 cannot be made to repeat one and the test role may not send.
4. Every wait for a delivery allows `q.DELIVERY_SECONDS`, and every check that
   nothing else arrives reads on for as long. Once one wait has ended with no
   delivery, every later case fails at once with its line.
5. The delivery case prints how long S3's events took to reach their queues.
"""

from __future__ import annotations

import inspect
import itertools
import json
import multiprocessing
import os
import statistics
import threading
import time
from pathlib import Path

import pytest

from ithildinruntime.adapters import Faults
from ithildinruntime.adapters.pipe import PipeQueue, send
from ithildinruntime.core import (
    EventKind,
    QueueUnavailable,
    event_body,
    output_key,
    result_key,
    run_stop_key,
)
from ithildinruntime.runtime.loop import Item
from tests.support import queues as q

_names = itertools.count()

# The cases that run on the test queues, each numbered by its place here, which
# gives it the hour its keys sit in. Add a case here before marking it `aws`.
_AWS_CASES = (
    "test_a_write_reaches_every_reader_its_routes_name_and_no_other",
    "test_events_dropped_while_a_reader_is_down_are_recovered_when_it_starts",
    "test_s3_skips_an_announcement_because_it_announced_the_blob_itself",
    "test_a_message_whose_item_is_in_a_handler_is_not_run_twice",
    "test_the_loop_never_holds_more_messages_than_it_has_free_handlers",
    "test_a_hung_handler_stops_the_heartbeat",
    "test_a_handler_slower_than_the_visibility_timeout_keeps_its_message_hidden",
    "test_stopping_returns_an_unfinished_message_at_once",
    "test_a_message_whose_instance_dies_returns_to_the_queue",
    "test_the_sqs_events_parse_like_the_folders",
)

_AWS = pytest.param("aws", marks=pytest.mark.aws)


def _make_cloud(request: pytest.FixtureRequest, kind: str) -> q.Cloud:
    if kind == "sqs":
        return q.Cloud.make(request.getfixturevalue("emulator"), f"queue-suite-{next(_names)}")
    name = request.node.originalname
    if name not in _AWS_CASES:
        pytest.fail(f"{name} runs on the test queues, so _AWS_CASES must give it an hour")
    if (earlier := q.undelivered()) is not None:
        # Every case waits on the same deliveries, so one more would only spend
        # its own bound before failing for the same reason.
        pytest.fail(f"An earlier case received no delivery, so this one did not wait: {earlier}")
    try:
        return q.Cloud.test_environment(q.case_from_environment(_AWS_CASES.index(name)))
    except (RuntimeError, ValueError) as error:
        pytest.fail(str(error))


@pytest.fixture(params=["sqs", _AWS])
def cloud(request: pytest.FixtureRequest) -> q.Cloud:
    return _make_cloud(request, request.param)


@pytest.fixture(params=["folder", "sqs", _AWS])
def side(request: pytest.FixtureRequest, tmp_path: Path) -> Side:
    if request.param == "folder":
        return FolderSide(q.Folder(tmp_path / "store", tmp_path / "queues"))
    return CloudSide(_make_cloud(request, request.param))


class Side:
    """One pair of a store and its queues, as a case drives it."""

    bucket: str
    case: q.Case
    # The components with a queue on this side.
    readers: tuple[str, ...]
    on_aws = False
    # How long a case reads on to show that nothing else arrives.
    settle = 0.5

    def store(self):
        raise NotImplementedError

    def queue(self, component: str):
        raise NotImplementedError

    def heard(self, component: str) -> list[str]:
        """The keys of the case's events waiting for a component, taken off its queue."""
        raise NotImplementedError

    def heard_together(self, seconds: float, enough=lambda heard: False) -> dict[str, list[str]]:
        """Read every reader's queue in turn, for `seconds` or until `enough` holds."""
        heard: dict[str, list[str]] = {reader: [] for reader in self.readers}
        end = time.monotonic() + seconds
        while time.monotonic() < end and not enough(heard):
            for reader in self.readers:
                heard[reader] += self.heard(reader)
        return heard

    def repeat(self, key: str) -> None:
        """Deliver a stored blob's event again, as a store repeats one."""
        raise NotImplementedError

    def close(self) -> None:
        pass


class FolderSide(Side):
    def __init__(self, folder: q.Folder) -> None:
        self.folder = folder
        self.bucket = folder.bucket
        self.case = q.Case()
        self.readers = q.COMPONENTS
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
        self.case = cloud.case
        self.readers = tuple(cloud.urls)
        self.on_aws = cloud.on_aws
        self.settle = cloud.settle
        # Seconds from each event S3 sent to SQS taking its message.
        self.delays: list[float] = []

    def store(self):
        return self.cloud.store()

    def queue(self, component: str):
        return self.cloud.queue(component)

    def heard(self, component: str) -> list[str]:
        batch = self.cloud.receive(component)
        self.cloud.delete(component, batch)
        self.delays.extend(m.delay() for m in batch)
        return [m.key for m in batch]

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


def test_a_write_reaches_every_reader_its_routes_name_and_no_other(
    side: Side, capsys: pytest.CaptureFixture
) -> None:
    for component in side.readers:
        side.queue(component)
    store = side.store()
    case = side.case
    written = {
        case.result("feed"): {"alpha", "risk"},
        case.result("alpha"): {"book"},
        case.result("risk"): {"book"},
        case.result("book"): {"book"},
        run_stop_key(q.DEPLOYMENT, case.run): {"book"},
        output_key(q.DEPLOYMENT, case.run, case.at(), "feed", "value"): set(),
    }
    if not side.on_aws:
        # The test role writes only under runs/ci/, so this key is the folder's and moto's.
        written[result_key("other", case.run, case.at(), "feed")] = set()
    for key in written:
        _write(store, key)
    expected = {
        reader: sorted(k for k, readers in written.items() if reader in readers)
        for reader in side.readers
    }

    def every_one(heard: dict[str, list[str]]) -> bool:
        return all(set(expected[r]) <= set(heard[r]) for r in side.readers)

    heard = side.heard_together(q.DELIVERY_SECONDS, every_one)
    for reader in side.readers:
        unheard = set(expected[reader]) - set(heard[reader])
        assert not unheard, q.missing(reader, unheard, q.DELIVERY_SECONDS)
    # Every expected event has arrived, so one to a reader the routes do not name
    # has had as long. Every queue is read on for one more window, together.
    late = side.heard_together(side.settle)
    side.close()
    # S3 and EventBridge deliver at least once, so a key heard twice by its own
    # reader is no fault. A key heard by any other reader is.
    heard = {reader: sorted(set(heard[reader] + late[reader])) for reader in side.readers}
    assert heard == expected
    if side.on_aws:
        delays = side.delays
        with capsys.disabled():
            print(
                f"\nS3 to SQS, over {len(delays)} messages: median "
                f"{statistics.median(delays):.1f} s, largest {max(delays):.1f} s. "
                "An event's time is in whole seconds."
            )


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
        key = result_key(q.DEPLOYMENT, the_plan.run, decision.at, "feed")
        store.put_result(key, feed(item, _no))


def _no() -> None:
    pass


def test_events_dropped_while_a_reader_is_down_are_recovered_when_it_starts(
    side: Side,
) -> None:
    the_plan = side.case.plan(3)
    _feed_results(side, the_plan)
    if isinstance(side, FolderSide):
        # No reader holds alpha's pipe, so the folder drops its events.
        assert not (side.folder.pipes / "alpha.pipe").exists()
    else:
        # Take each event off alpha's queue as it arrives, so none waits for the loop.
        # A purge would clear every case's messages, and SQS allows one a minute.
        feeds = {side.case.result("feed", n) for n in range(len(the_plan.decisions))}
        assert side.cloud.drain("alpha", feeds), q.missing("alpha", feeds, q.DELIVERY_SECONDS)
    calls, alarms = q.Calls(), q.Alarms()
    alpha = _alpha_alone(side, the_plan, calls, alarms)
    root_keys = [side.case.result("alpha", n) for n in range(len(the_plan.decisions))]
    store = side.store()
    with q.running({"alpha": alpha}) as running:
        assert running.wait_for(
            lambda: all(_exists(store, k) for k in root_keys), q.DELIVERY_SECONDS
        )
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
    key = cloud.case.result("feed")
    _write(store, key)
    assert cloud.wait_for("alpha", {key}), q.missing("alpha", [key], q.DELIVERY_SECONDS)
    store.announce(key)
    # Read on, so an announcement S3 sent late would be on the queue.
    side = CloudSide(cloud)
    side.readers = ("alpha",)
    assert side.heard_together(cloud.settle) == {"alpha": [key]}


# What both queues guarantee, run through the receive loop.


_gates: list[Gate] = []


@pytest.fixture(autouse=True)
def _open_every_gate():
    """Let every gated handler through when a case ends, passed or failed.

    A handler left waiting keeps its thread alive, so a failed assertion
    would hang the run rather than fail the case.
    """
    yield
    while _gates:
        _gates.pop().go.set()


class Gate:
    """A handler that waits to be let through, counting how many it ran."""

    def __init__(self, report: bool = True) -> None:
        _gates.append(self)
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
    if side.on_aws:
        pytest.skip("S3 cannot be made to repeat an event, and the test role may not send one")
    the_plan = side.case.plan(1)
    gate, alarms = Gate(), q.Alarms()
    alpha = _gated_alpha(side, the_plan, gate, alarms)
    feed_key = side.case.result("feed")
    done_key = side.case.result("alpha")
    store = side.store()
    with q.running({"alpha": alpha}) as running:
        _feed_results(side, the_plan)
        assert gate.started.wait(q.DELIVERY_SECONDS)
        side.repeat(feed_key)
        side.repeat(feed_key)
        time.sleep(1.5)
        gate.go.set()
        assert running.wait_for(lambda: _exists(store, done_key), 10)
        time.sleep(0.5)
    if isinstance(side, CloudSide):
        assert side.cloud.keys_on("alpha") == []
    side.close()
    assert gate.calls.counts == {Item(side.case.at(), "alpha"): 1}


def test_the_loop_never_holds_more_messages_than_it_has_free_handlers(side: Side) -> None:
    the_plan = side.case.plan(6)
    gate, alarms = Gate(), q.Alarms()
    counting = Counting(side.queue("alpha"))
    alpha = _gated_alpha(side, the_plan, gate, alarms, queue=counting, pool_size=2)
    store = side.store()
    keys = [side.case.result("alpha", n) for n in range(len(the_plan.decisions))]
    with q.running({"alpha": alpha}) as running:
        _feed_results(side, the_plan)
        feeds = [side.case.result("feed", n) for n in range(len(the_plan.decisions))]
        assert running.wait_for(lambda: gate.calls.counts.total() >= 2, q.DELIVERY_SECONDS), (
            q.missing("alpha", feeds, q.DELIVERY_SECONDS)
        )
        # Both handlers are held at the gate, so no third decision may start.
        time.sleep(0.5)
        assert gate.calls.counts.total() == 2
        gate.go.set()
        assert running.wait_for(lambda: all(_exists(store, k) for k in keys), q.DELIVERY_SECONDS)
    side.close()
    assert counting.most <= 2 and max(counting.limits) <= 2
    assert set(gate.calls.counts.values()) == {1}


def test_a_hung_handler_stops_the_heartbeat(side: Side) -> None:
    the_plan = side.case.plan(2)
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
    with q.running({"alpha": alpha}) as running:
        time.sleep(0.5)
        assert beats, "an idle loop beats"
        _feed_results(side, the_plan)
        assert hung.started.wait(q.DELIVERY_SECONDS), q.missing(
            "alpha", [side.case.result("feed")], q.DELIVERY_SECONDS
        )
        time.sleep(1.0)
        stopped_at = time.monotonic()
        time.sleep(1.0)
        assert not [b for b in beats if b > stopped_at], "a hung handler kept the heartbeat"
        hung.go.set()
        # The handler reads and writes the store before it reports, which on S3
        # takes longer than on the folder.
        assert running.wait_for(
            lambda: any(b > stopped_at + 1.0 for b in beats), q.DELIVERY_SECONDS
        ), "the heartbeat came back"
    side.close()


# What only SQS keeps: extending, deleting and returning a message.


# Each queue's own visibility timeout is 30 seconds, here as in the test
# environment, so a case that needs a short one passes it on each receive and
# each extend, as `SqsQueue` does.


def test_a_handler_slower_than_the_visibility_timeout_keeps_its_message_hidden(
    cloud: q.Cloud,
) -> None:
    side = CloudSide(cloud)
    the_plan = cloud.case.plan(1)
    gate, alarms = Gate(), q.Alarms()
    settings = {"extend_every": 0.5}
    # Both instances start before the feed's result exists, so neither finds
    # the decision by its keys, and only the message can hand it to one.
    first = _gated_alpha(side, the_plan, gate, alarms, queue=cloud.queue("alpha", 2), **settings)
    second = _gated_alpha(side, the_plan, gate, alarms, queue=cloud.queue("alpha", 2), **settings)
    done_key = cloud.case.result("alpha")
    store = cloud.store()
    with q.running({"first": first, "second": second}) as running:
        _feed_results(side, the_plan)
        assert gate.started.wait(q.DELIVERY_SECONDS), q.missing(
            "alpha", [cloud.case.result("feed")], q.DELIVERY_SECONDS
        )
        # Three visibility timeouts pass while the handler works.
        time.sleep(6)
        gate.go.set()
        assert running.wait_for(lambda: _exists(store, done_key), q.DELIVERY_SECONDS)
    # Past one more visibility timeout with both loops stopped, so a message left
    # undeleted is back on the queue rather than received again.
    time.sleep(2.5)
    assert gate.calls.counts == {Item(cloud.case.at(), "alpha"): 1}
    assert cloud.keys_on("alpha") == []


def test_stopping_returns_an_unfinished_message_at_once(cloud: q.Cloud) -> None:
    side = CloudSide(cloud)
    the_plan = cloud.case.plan(1)
    stuck, alarms = Gate(), q.Alarms()
    first = _gated_alpha(side, the_plan, stuck, alarms, stop_timeout=0.2)
    feed_key = cloud.case.result("feed")
    try:
        with q.running({"first": first}):
            _feed_results(side, the_plan)
            assert stuck.started.wait(q.DELIVERY_SECONDS), q.missing(
                "alpha", [feed_key], q.DELIVERY_SECONDS
            )
        # The loop's receive hides a message for 30 seconds, so only its release
        # brings it back this soon.
        assert cloud.keys_on("alpha") == [feed_key]
    finally:
        stuck.go.set()


def test_a_message_whose_instance_dies_returns_to_the_queue(cloud: q.Cloud, tmp_path) -> None:
    side = CloudSide(cloud)
    case = cloud.case
    the_plan = case.plan(1)
    marker = tmp_path / "started"
    context = multiprocessing.get_context("spawn")
    child = context.Process(
        target=q.hang_alpha,
        args=(
            cloud.endpoint,
            cloud.bucket,
            cloud.urls["alpha"],
            str(marker),
            case.run,
            case.first.isoformat(),
        ),
    )
    child.start()
    try:
        assert q.wait_for_file(marker, 60), "the child's loop never started"
        _feed_results(side, the_plan)
        assert q.wait_for_file(marker.with_suffix(".item"), q.DELIVERY_SECONDS), q.missing(
            "alpha", [case.result("feed")], q.DELIVERY_SECONDS
        )
        # The child extends its message while its handler works, so after two
        # visibility timeouts the message is still its own.
        time.sleep(4)
        assert cloud.keys_on("alpha") == []
        os.kill(child.pid, 9)
        child.join(10)
    finally:
        if child.is_alive():
            child.kill()
    # Nothing extends the message once its instance is dead, so it returns within
    # the 2 seconds of its last extend. It is already on the queue, so no
    # delivery bound applies, and 10 seconds is the 2 with room for a slow read.
    feed_key = case.result("feed")
    returned: list[str] = []
    end = time.monotonic() + 10
    while time.monotonic() < end and feed_key not in returned:
        returned += cloud.keys_on("alpha")
    assert returned == [feed_key], "the dead instance's message did not return within 10 seconds"


# Where a case's keys sit on the test queues.


def test_no_two_runs_attempts_cases_or_decisions_share_a_key() -> None:
    keys = set()
    tuples = 0
    for number in (1, 2, 3, 99, 100, 101, 5000):
        for attempt in (1, 2, 98, 99):
            for index in (0, 1, 22, 23):
                case = q.run_case(number, attempt, index)
                for n in (0, 1, 30, 59):
                    keys.add(case.result("feed", n))
                    tuples += 1
    assert len(keys) == tuples


def test_a_case_reads_its_own_keys_and_no_other_cases() -> None:
    first, second = q.run_case(7, 1, 0), q.run_case(7, 1, 1)
    retry = q.run_case(7, 2, 0)
    assert first.run == "2001-12-02" and first.first.hour == 0 and second.first.hour == 1
    assert first.mine(first.result("book", 59))
    assert first.mine(run_stop_key(q.DEPLOYMENT, first.run))
    assert not first.mine(second.result("feed"))
    assert not first.mine(retry.result("feed"))
    assert not first.mine(f"runs/ci/{first.run}/smoke/x.json")
    with pytest.raises(ValueError, match="hour"):
        first.plan(61)


@pytest.mark.parametrize(
    ("number", "attempt", "index", "said"),
    [
        (0, 1, 0, "number"),
        (True, 1, 0, "number"),
        (1, 0, 0, "attempt"),
        (1, 100, 0, "attempt"),
        (1, 1, 24, "index"),
        (1, 1, -1, "index"),
    ],
)
def test_a_run_outside_the_scheme_is_refused(
    number: int, attempt: int, index: int, said: str
) -> None:
    with pytest.raises(ValueError, match=said):
        q.run_case(number, attempt, index)


def test_a_case_holds_from_one_decision_to_sixty() -> None:
    assert len(q.Case().plan(60).decisions) == 60
    for decisions in (0, 61, True):
        with pytest.raises(ValueError, match="hour"):
            q.Case().plan(decisions)


def test_run_numbers_repeat_their_dates_only_after_a_period() -> None:
    period = q.RUN_PERIOD
    assert q.run_case(period + 5, 1, 0) == q.run_case(5, 1, 0)
    assert len({q.run_case(n, 1, 0).run for n in range(period - 50, period + 50)}) == 100
    # The largest number and attempt still give a date.
    assert q.run_case(period - 1, 99, 23).first.year < 9999


@pytest.mark.parametrize("missing", ["GITHUB_RUN_NUMBER", "GITHUB_RUN_ATTEMPT"])
def test_a_case_outside_a_github_run_says_so(monkeypatch, missing: str) -> None:
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "12")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        q.case_from_environment(0)


def test_a_case_in_a_github_run_takes_its_number_attempt_and_index(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_RUN_NUMBER", "12")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "3")
    assert q.case_from_environment(2) == q.run_case(12, 3, 2)


def test_every_case_on_the_test_queues_has_an_hour_of_its_own() -> None:
    """Every case that takes `side` or `cloud` runs on the test queues, so each needs an hour."""
    assert len(set(_AWS_CASES)) == len(_AWS_CASES) <= 24
    sided = {
        name
        for name, case in globals().items()
        if name.startswith("test_")
        and callable(case)
        and {"side", "cloud"} & set(inspect.signature(case).parameters)
    }
    assert sided == set(_AWS_CASES)


def test_the_test_environments_client_ignores_an_endpoint_the_environment_sets(
    monkeypatch,
) -> None:
    """A green run on the test queues cannot come from an endpoint a variable points elsewhere."""
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "none")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "none")
    for service in ("s3", "sqs"):
        made = q.client(service, None)
        assert made.meta.endpoint_url.endswith(".amazonaws.com")


def test_the_test_environments_client_refuses_a_host_outside_aws(monkeypatch) -> None:
    from tests.support import aws

    real = aws.boto3.client

    def elsewhere(*args, **kwargs):
        return real(*args, endpoint_url="http://127.0.0.1:1", **kwargs)

    monkeypatch.setattr(aws.boto3, "client", elsewhere)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "none")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "none")
    with pytest.raises(RuntimeError, match="not AWS"):
        q.client("sqs", None)


def _shared(emulator: str, case: q.Case) -> tuple[q.Cloud, object]:
    """A queue on moto that cases share, as the test environment's are, and a client to fill it."""
    made = q.Cloud.make(emulator, f"queue-suite-{next(_names)}")
    shared = q.Cloud(emulator, made.bucket, made.urls, case, shared=True)
    return shared, q.client("sqs", emulator)


def _send(sqs, url: str, key: str) -> None:
    body = {"time": "2026-01-01T00:00:00Z", "detail": {"object": {"key": key}}}
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps(body))


def test_a_read_of_a_shared_queue_keeps_the_cases_keys_and_deletes_the_rest(
    emulator: str,
) -> None:
    case, other = q.run_case(7, 1, 1), q.run_case(7, 1, 0)
    cloud, sqs = _shared(emulator, case)
    url = cloud.urls["alpha"]
    _send(sqs, url, other.result("feed"))
    _send(sqs, url, case.result("feed"))
    assert cloud.keys_on("alpha") == [case.result("feed")]
    left = sqs.receive_message(QueueUrl=url, WaitTimeSeconds=1, VisibilityTimeout=0)
    assert left.get("Messages", []) == []


def test_a_read_of_a_shared_queue_reads_past_a_batch_of_other_cases(emulator: str) -> None:
    case, other = q.run_case(7, 1, 7), q.run_case(7, 1, 6)
    cloud, sqs = _shared(emulator, case)
    for n in range(10):
        _send(sqs, cloud.urls["alpha"], other.result("feed", n))
    _send(sqs, cloud.urls["alpha"], case.result("feed"))
    assert cloud.keys_on("alpha") == [case.result("feed")]


def test_a_read_of_a_cases_own_queue_keeps_every_message(emulator: str) -> None:
    """On moto a misrouted message is the case's, so the delivery case can see it."""
    made = q.Cloud.make(emulator, f"queue-suite-{next(_names)}")
    sqs = q.client("sqs", emulator)
    _send(sqs, made.urls["alpha"], "runs/other/2026-11-02/x/feed/result.json")
    assert made.keys_on("alpha") == ["runs/other/2026-11-02/x/feed/result.json"]


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


def test_a_record_holds_a_body_of_up_to_511_bytes() -> None:
    from ithildinruntime.adapters.pipe import RECORD, record

    assert RECORD == 512
    assert len(record(b"x" * 511)) == 512
    with pytest.raises(ValueError, match="record"):
        record(b"x" * 512)


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
    key = cloud.case.result("feed")
    _write(store, key)
    queue = cloud.queue("alpha")
    found = []
    end = time.monotonic() + q.DELIVERY_SECONDS
    while not found and time.monotonic() < end:
        for event in queue.receive(10):
            # An earlier case's message is deleted with the case's own.
            if event.key == key:
                found.append(event)
            queue.done(event)
    assert found, q.missing("alpha", [key], q.DELIVERY_SECONDS)
    (event,) = found
    assert (event.bucket, event.key, event.kind) == (cloud.bucket, key, EventKind.CREATED)
    assert event.etag and event.sequencer
    body = json.loads(event_body(cloud.bucket, key, EventKind.CREATED, "t", "s"))
    assert body["detail"]["object"]["key"] == key


def test_closing_a_pipe_waits_for_a_receive_and_leaves_no_descriptor_behind(
    tmp_path: Path,
) -> None:
    """A receive woken by `close` returns before its descriptor is closed.

    Closing the descriptor under a waiting receive let Linux give its number to
    the next file opened, and the receive then read that file's bytes.
    """
    reader = PipeQueue(tmp_path, "alpha", wait_seconds=5)
    got: list = []
    waiting = threading.Thread(target=lambda: got.append(reader.receive(1)))
    waiting.start()
    time.sleep(0.2)
    started = time.monotonic()
    reader.close()
    waiting.join(2)
    assert not waiting.is_alive() and got == [()]
    assert time.monotonic() - started < 2
    blob = tmp_path / "blob"
    blob.write_bytes(b"x" * 401)
    assert blob.read_bytes() == b"x" * 401
    with pytest.raises(QueueUnavailable):
        reader.receive(1)
