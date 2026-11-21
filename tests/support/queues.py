"""The queue suite's stand-in stages and the harness that runs their receive loops.

Four stand-in components pass small numbers to each other, as the workers
pass forecasts and books.

1. `feed` reads nothing and writes its decision's number.
2. `alpha` and `risk` each read `feed`, and decide side by side.
3. `book` reads both and its own result for the decision before, so it keeps
   the plan's order.

Each runs its own receive loop on a thread, over the folder and named pipes
or over S3 and SQS on moto's server, with the routes generated from the
stand-in table. That is the same table the routes file for the test
environment's queues will be generated from.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
from botocore.config import Config

from ithildinruntime.adapters import Faults, FolderStore, PipeQueue, S3Store, SqsQueue
from ithildinruntime.core import (
    BlobHeader,
    DecisionResult,
    EventKind,
    Mode,
    OutputFile,
    Plan,
    PlanDecision,
    Retry,
    StageKind,
    Storage,
    encode_output,
    event_body,
    output_key,
    result_key,
)
from ithildinruntime.core.records import _Record
from ithildinruntime.runtime.loop import (
    Hears,
    Item,
    ReceiveLoop,
    Stage,
    Table,
    event_pattern,
    routes,
)
from tests.support.stores import EMULATOR_KEYS, REGION

DEPLOYMENT = "ci"
RUN = "2026-11-02"
RELEASE = "sha256:release"
CONFIG = "0" * 64
FIRST = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)

TABLE = Table(
    stages=(
        Stage("feed", StageKind.DETERMINISTIC),
        Stage("alpha", StageKind.DETERMINISTIC, ("feed",)),
        Stage("risk", StageKind.DETERMINISTIC, ("feed",)),
        Stage("book", StageKind.DETERMINISTIC, ("alpha", "risk"), keeps_order=True),
    ),
    hears=(Hears("book", EventKind.CREATED, "runs/{deployment}/*/stop.json"),),
)
COMPONENTS = ("feed", "alpha", "risk", "book")


@dataclass(frozen=True)
class Value(_Record):
    number: int


def plan(decisions: int) -> Plan:
    return Plan(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        mode=Mode.PAPER,
        release=RELEASE,
        decisions=tuple(
            PlanDecision(FIRST + timedelta(minutes=n), ("standin",)) for n in range(decisions)
        ),
    )


class SystemTimer:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def after(self, seconds: float) -> object:
        raise NotImplementedError


class Alarms:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def alarm(self, reason: str) -> None:
        self.reasons.append(reason)

    def heartbeat(self) -> None:
        pass


def storage(store, alarms: Alarms) -> Storage:
    return Storage(
        store,
        timer=SystemTimer(),
        pause=time.sleep,
        retry=Retry(timedelta(seconds=5), 0.05, 0.5),
        alerts=alarms,
        owner_step="nothing to do: the stand-in stages trade nothing",
    )


def header(stage: str, at: datetime, inputs: dict[str, str]) -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        stage=stage,
        kind="deterministic",
        decision_at=at,
        inputs=inputs,
        release=RELEASE,
        config_hash=CONFIG,
    )


def read_value(store: Storage, stage: str, at: datetime) -> tuple[int, dict[str, str]]:
    key = result_key(DEPLOYMENT, RUN, at, stage)
    result = store.read_result(key, at)
    assert result is not None, f"{key} was read before it was stored"
    (listed,) = result.outputs
    output = store.read_output(key, result, listed, at, Value)
    return output.entries[0].number, {listed.key: listed.sha256}


def write_value(
    store: Storage, item: Item, number: int, inputs: dict[str, str], report: Callable[[], None]
) -> DecisionResult:
    key = output_key(DEPLOYMENT, RUN, item.decision_at, item.stage, "value")
    head = header(item.stage, item.decision_at, inputs)
    data = encode_output(Value, OutputFile(head, (Value(number),), False))
    stored = store.put(StageKind.DETERMINISTIC, key, data)
    report()
    return DecisionResult(head, (stored.ref,), None)


@dataclass
class Calls:
    """Each item every handler ran, and how many handlers ran at once."""

    counts: Counter = field(default_factory=Counter)
    order: list[Item] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, item: Item) -> None:
        with self.lock:
            self.counts[item] += 1
            self.order.append(item)


def handlers(
    store: Storage, the_plan: Plan, calls: Calls, jitter: random.Random | None = None
) -> dict[str, Callable]:
    """The stand-in stages' handlers, each pausing a random moment to shuffle them."""
    decisions = [d.at for d in the_plan.decisions]
    lock = threading.Lock()

    def pause() -> None:
        if jitter is not None:
            with lock:
                wait = jitter.uniform(0, 0.02)
            time.sleep(wait)

    def feed(item: Item, report: Callable[[], None]) -> DecisionResult:
        calls.add(item)
        pause()
        return write_value(store, item, decisions.index(item.decision_at) + 1, {}, report)

    def alpha(item: Item, report: Callable[[], None]) -> DecisionResult:
        calls.add(item)
        number, inputs = read_value(store, "feed", item.decision_at)
        pause()
        return write_value(store, item, number * 2, inputs, report)

    def risk(item: Item, report: Callable[[], None]) -> DecisionResult:
        calls.add(item)
        number, inputs = read_value(store, "feed", item.decision_at)
        pause()
        return write_value(store, item, number + 10, inputs, report)

    def book(item: Item, report: Callable[[], None]) -> DecisionResult:
        calls.add(item)
        a, from_alpha = read_value(store, "alpha", item.decision_at)
        r, from_risk = read_value(store, "risk", item.decision_at)
        inputs = {**from_alpha, **from_risk}
        index = decisions.index(item.decision_at)
        before = 0
        if index:
            before, earlier = read_value(store, "book", decisions[index - 1])
            inputs.update(earlier)
        pause()
        return write_value(store, item, before + a + r, inputs, report)

    return {"feed": feed, "alpha": alpha, "risk": risk, "book": book}


def every_result(the_plan: Plan) -> list[str]:
    return [
        result_key(DEPLOYMENT, RUN, d.at, stage.name)
        for d in the_plan.decisions
        for stage in TABLE.stages
    ]


def folder_blobs(root: Path) -> dict[str, bytes]:
    """Every blob the folder holds, by key, leaving out its own working folders."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.relative_to(root).parts[0].startswith(".")
    }


@dataclass
class Running:
    """A set of receive loops, each on its own thread."""

    loops: dict[str, ReceiveLoop]
    threads: dict[str, threading.Thread]
    errors: dict[str, BaseException]

    def wait_for(self, check: Callable[[], bool], timeout: float = 30.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return True
            if self.errors:
                raise AssertionError(f"a loop failed: {self.errors}")
            time.sleep(0.02)
        return False

    def stop(self) -> None:
        for loop in self.loops.values():
            loop.stop()
        for thread in self.threads.values():
            thread.join(10)


def start(loops: dict[str, ReceiveLoop]) -> Running:
    errors: dict[str, BaseException] = {}

    def run(name: str, loop: ReceiveLoop) -> None:
        try:
            loop.run()
        except BaseException as error:  # noqa: BLE001 - the test reads it
            errors[name] = error

    threads = {
        name: threading.Thread(target=run, args=(name, loop), daemon=True, name=name)
        for name, loop in loops.items()
    }
    for thread in threads.values():
        thread.start()
    for loop in loops.values():
        assert loop.wait_started(10)
    return Running(loops, threads, errors)


def loop(
    component: str,
    *,
    the_plan: Plan,
    queue,
    store: Storage,
    bucket: str,
    alarms: Alarms,
    handlers_by_stage: dict[str, Callable],
    pool_size: int = 4,
    beat: Callable[[], None] = lambda: None,
    timer=None,
    **limits: float,
) -> ReceiveLoop:
    return ReceiveLoop(
        worker=f"{component}-1",
        component=component,
        table=TABLE,
        plan=the_plan,
        queue=queue,
        storage=store,
        bucket=bucket,
        handlers={s.name: handlers_by_stage[s.name] for s in TABLE.stages_of(component)},
        alerts=alarms,
        timer=timer or SystemTimer(),
        beat=beat,
        config_hash=CONFIG,
        pool_size=pool_size,
        **{"tick": 0.05, "stop_timeout": 5.0, **limits},
    )


# The folder and its named pipes


@dataclass
class Folder:
    root: Path
    pipes: Path
    bucket: str = "folder"

    def store(self, faults: Faults | None = None, announce: bool = True) -> FolderStore:
        if not announce:
            return FolderStore(self.root, bucket=self.bucket)
        return FolderStore(
            self.root,
            bucket=self.bucket,
            routes=routes(TABLE, DEPLOYMENT),
            pipes=self.pipes,
            faults=faults,
        )

    def queue(self, component: str) -> PipeQueue:
        return PipeQueue(self.pipes, component, wait_seconds=0.2)


def run_folder(
    tmp: Path,
    decisions: int,
    pool_size: int,
    *,
    faults: Faults | None = None,
    seed: int = 7,
    timeout: float = 60.0,
) -> tuple[dict[str, bytes], Calls]:
    """Run every stand-in stage over the folder to the plan's end, and return its blobs."""
    folder = Folder(tmp / "store", tmp / "queues")
    the_plan = plan(decisions)
    calls = Calls()
    jitter = random.Random(seed)
    alarms = Alarms()
    queues = {name: folder.queue(name) for name in COMPONENTS}
    stores = {name: storage(folder.store(faults), alarms) for name in COMPONENTS}
    loops = {
        name: loop(
            name,
            the_plan=the_plan,
            queue=queues[name],
            store=stores[name],
            bucket=folder.bucket,
            alarms=alarms,
            handlers_by_stage=handlers(stores[name], the_plan, calls, jitter),
            pool_size=pool_size,
        )
        for name in COMPONENTS
    }
    running = start(loops)
    try:
        keys = every_result(the_plan)
        done = running.wait_for(lambda: all((folder.root / k).exists() for k in keys), timeout)
    finally:
        running.stop()
        for queue in queues.values():
            queue.close()
    assert done, f"the stand-in stages did not finish: {sorted(calls.counts)}"
    assert alarms.reasons == []
    return folder_blobs(folder.root), calls


# S3, EventBridge and SQS on moto's server


@dataclass
class Cloud:
    """One bucket, a queue for each stand-in component, and the generated rules."""

    endpoint: str
    bucket: str
    urls: dict[str, str]

    @classmethod
    def make(cls, endpoint: str, name: str, visibility: int = 30) -> Cloud:
        s3 = client("s3", endpoint)
        s3.create_bucket(Bucket=name)
        s3.put_bucket_notification_configuration(
            Bucket=name, NotificationConfiguration={"EventBridgeConfiguration": {}}
        )
        sqs = client("sqs", endpoint)
        events = client("events", endpoint)
        urls = {}
        for component in COMPONENTS:
            url = sqs.create_queue(
                QueueName=f"{name}-{component}",
                Attributes={"VisibilityTimeout": str(visibility)},
            )["QueueUrl"]
            urls[component] = url
        arns = {
            c: sqs.get_queue_attributes(QueueUrl=u, AttributeNames=["QueueArn"])["Attributes"][
                "QueueArn"
            ]
            for c, u in urls.items()
        }
        for route in routes(TABLE, DEPLOYMENT):
            rule = f"{name}-{route.name}"
            events.put_rule(Name=rule, EventPattern=json.dumps(event_pattern(route, name)))
            events.put_targets(Rule=rule, Targets=[{"Id": "queue", "Arn": arns[route.reader]}])
        return cls(endpoint, name, urls)

    def store(self) -> S3Store:
        return S3Store(
            client(
                "s3",
                self.endpoint,
                Config(retries={"mode": "standard", "total_max_attempts": 1}),
            ),
            self.bucket,
        )

    def queue(self, component: str, visibility: int = 30, wait: int = 1) -> SqsQueue:
        sqs = client("sqs", self.endpoint, Config(read_timeout=wait + 2))
        return SqsQueue(sqs, self.urls[component], visibility_timeout=visibility, wait_seconds=wait)

    def messages(self, component: str) -> list[dict]:
        """Every message waiting on a component's queue, taken off it for a minute."""
        sqs = client("sqs", self.endpoint)
        found: list[dict] = []
        while True:
            reply = sqs.receive_message(
                QueueUrl=self.urls[component],
                MaxNumberOfMessages=10,
                WaitTimeSeconds=1,
                VisibilityTimeout=60,
            )
            batch = reply.get("Messages", [])
            if not batch:
                return found
            found.extend(json.loads(m["Body"]) for m in batch)

    def keys(self) -> set[str]:
        s3 = client("s3", self.endpoint)
        listed = s3.list_objects_v2(Bucket=self.bucket).get("Contents", [])
        return {item["Key"] for item in listed}


def client(service: str, endpoint: str, config: Config | None = None):
    return boto3.client(
        service, endpoint_url=endpoint, region_name=REGION, config=config, **EMULATOR_KEYS
    )


@contextmanager
def running(loops: dict[str, ReceiveLoop]) -> Iterator[Running]:
    started = start(loops)
    try:
        yield started
    finally:
        started.stop()


def wait_for_file(path: Path | str, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if Path(path).exists():
            return True
        time.sleep(0.05)
    return False


def hang_alpha(endpoint: str, bucket: str, url: str, marker: str) -> None:
    """Run an alpha loop whose handler never ends, in a process the test kills."""
    cloud = Cloud(endpoint, bucket, {"alpha": url})
    the_plan = plan(1)
    alarms = Alarms()
    store = storage(cloud.store(), alarms)

    def alpha(item: Item, report: Callable[[], None]) -> DecisionResult:
        Path(marker).with_suffix(".item").write_text(item.stage)
        while True:
            report()
            time.sleep(0.1)

    hanging = loop(
        "alpha",
        the_plan=the_plan,
        queue=cloud.queue("alpha", visibility=2),
        store=store,
        bucket=bucket,
        alarms=alarms,
        handlers_by_stage={"alpha": alpha},
        pool_size=1,
        extend_every=0.5,
    )
    start({"alpha": hanging})
    Path(marker).write_text("started")
    while True:
        time.sleep(1)


class StubQueue:
    """A queue a case fills by hand, recording every answer the loop gives.

    `receive` waits up to `wait` seconds for an event, and `blocking` makes it
    wait until `free` is set, so a case can hold a receive open across `stop`.
    """

    def __init__(self, wait: float = 0.05) -> None:
        self.pending: list = []
        self.done_keys: list[str] = []
        self.released: list[str] = []
        self.extended: list[str] = []
        self.limits: list[int] = []
        self.wait = wait
        self.error: BaseException | None = None
        self.extend_takes = 0.0
        self.answers_fail = False
        self.extend_error: BaseException | None = None
        self.done_error: BaseException | None = None
        # How many receives in a row refuse before the queue answers again.
        self.refusals = 0
        self.entered = threading.Event()
        # Called as a receive hands back events, so a case can act in that window.
        self.on_answer: Callable[[], None] | None = None
        self.blocking = False
        self.free = threading.Event()
        self.lock = threading.Condition()
        self._handles = iter(range(1, 1_000_000))

    def put(self, key: str, bucket: str = "folder", kind: EventKind = EventKind.CREATED) -> None:
        from ithildinruntime.core import parse_event

        body = event_body(bucket, key, kind, "tag", "0")
        with self.lock:
            self.pending.append(parse_event(body, f"stub-{next(self._handles)}"))
            self.lock.notify_all()

    def receive(self, limit: int):
        self.entered.set()
        if self.refusals:
            self.refusals -= 1
            from ithildinruntime.core import QueueUnavailable

            raise QueueUnavailable("ServiceUnavailable: one refusal")
        if self.error is not None:
            raise self.error
        if self.blocking:
            self.free.wait(10)
        with self.lock:
            self.limits.append(limit)
            if not self.pending:
                self.lock.wait(self.wait)
            taken, self.pending = self.pending[:limit], self.pending[limit:]
        if taken and self.on_answer is not None:
            self.on_answer()
        return tuple(taken)

    def done(self, event) -> None:
        self.done_keys.append(event.key)
        if self.done_error is not None:
            raise self.done_error
        self._refuse()

    def extend(self, event) -> None:
        time.sleep(self.extend_takes)
        self.extended.append(event.key)
        if self.extend_error is not None:
            raise self.extend_error
        self._refuse()

    def _refuse(self) -> None:
        if self.answers_fail:
            from ithildinruntime.core import QueueUnavailable

            raise QueueUnavailable("ReceiptHandleIsInvalid: the receipt has expired")

    def release(self, event) -> None:
        self.released.append(event.key)


class Refusing:
    """A store that answers nothing for keys a case names, until the case lets it."""

    def __init__(self, store, *, gets: tuple[str, ...] = (), creates: tuple[str, ...] = ()):
        self._store = store
        self.gets = set(gets)
        self.creates = set(creates)
        self.down = True
        self.refused = 0

    def _refuse(self, key: str, names: set[str]) -> None:
        from ithildinruntime.core import StoreUnavailable

        if self.down and any(key.endswith(n) for n in names):
            self.refused += 1
            raise StoreUnavailable(key, "503 SlowDown: the endpoint is blocked")

    def create(self, key, data, sha256):
        self._refuse(key, self.creates)
        return self._store.create(key, data, sha256)

    def get(self, key):
        self._refuse(key, self.gets)
        return self._store.get(key)

    def head(self, key):
        self._refuse(key, self.gets)
        return self._store.head(key)

    def __getattr__(self, name):
        return getattr(self._store, name)
