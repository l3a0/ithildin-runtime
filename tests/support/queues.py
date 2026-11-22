"""The queue suite's stand-in stages and the harness that runs their receive loops.

Four stand-in components pass small numbers to each other, as the workers
pass forecasts and books.

1. `feed` reads nothing and writes its decision's number.
2. `alpha` and `risk` each read `feed`, and decide side by side.
3. `book` reads both and its own result for the decision before, so it keeps
   the plan's order.

Each runs its own receive loop on a thread, over the folder and named pipes,
over S3 and SQS on moto's server, or over the test environment's bucket and
queues in AWS. On moto the routes are generated from the stand-in table, and
the routes file for the test environment's queues is generated from the same
table, by `tests/support/routes_file.py`.

On moto each case makes its own bucket and queues. In AWS every case shares
the test bucket and the test queues, so each case's keys sit under a run and
an hour of their own, which `Case` holds. A read of a queue keeps the case's
own keys and deletes every other message, which is an earlier case's.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

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
    decision_part,
    encode_output,
    event_body,
    output_key,
    result_key,
    run_stop_key,
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
from tests.support.aws import DELIVERY_SECONDS, TEST_BUCKET, TEST_QUEUE_PREFIX, aws_client
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
# The components that hear a route. `feed` hears none, so the test environment
# has no queue for it.
READERS = tuple(sorted({route.reader for route in routes(TABLE, DEPLOYMENT)}))

# Whether a wait in this run has already ended with no delivery. In AWS every
# later case would wait out its own bound for the same reason, so it fails at once.
_undelivered: list[str] = []


@dataclass(frozen=True)
class Value(_Record):
    number: int


def plan(decisions: int, run: str = RUN, first: datetime = FIRST) -> Plan:
    return Plan(
        format=1,
        deployment=DEPLOYMENT,
        run=run,
        mode=Mode.PAPER,
        release=RELEASE,
        decisions=tuple(
            PlanDecision(first + timedelta(minutes=n), ("standin",)) for n in range(decisions)
        ),
    )


@dataclass(frozen=True)
class Case:
    """Where one case's keys sit: a run, and the hour its decisions start in.

    A case's decisions are a minute apart from `first`, so they all fall in
    `first`'s hour, and `mine` tells its keys from every other case's in the
    same run. The run's stop key is the run's own rather than a decision's, so
    every case in the run claims it, and only the first case writes it.
    """

    run: str = RUN
    first: datetime = FIRST

    def plan(self, decisions: int) -> Plan:
        if isinstance(decisions, bool) or not 1 <= decisions <= 60:
            raise ValueError(f"a case's {decisions} decisions do not fit in its hour")
        return plan(decisions, self.run, self.first)

    def at(self, n: int = 0) -> datetime:
        return self.first + timedelta(minutes=n)

    def result(self, stage: str, n: int = 0) -> str:
        return result_key(DEPLOYMENT, self.run, self.at(n), stage)

    def mine(self, key: str) -> bool:
        stop = run_stop_key(DEPLOYMENT, self.run)
        # A decision part reads `20261102T150000Z`, so its first 11 characters name the hour.
        hour = stop.removesuffix("stop.json") + decision_part(self.first)[:11]
        return key.startswith(hour) or key == stop


# The day every run's date counts from. The key builders accept only a date as
# a key's run part, so a run's number and attempt become one.
EPOCH = date(2000, 1, 1)
# Run numbers repeat their dates after this many runs, which keeps every date
# inside Python's calendar. The test bucket expires every key within two days,
# so a repeat meets nothing: 20,000 runs is 690 days at the 29 runs a day ci.yml
# saw in its first 25 days, and 69 days at ten times that.
RUN_PERIOD = 20_000


def run_case(number: int, attempt: int, index: int) -> Case:
    """The keys of case `index` in attempt `attempt` of the workflow's run `number`.

    The run is EPOCH plus (`number` mod RUN_PERIOD) × 100 + `attempt` days, and
    the case's first decision is that date's midnight UTC plus `index` hours. So
    no two of RUN_PERIOD numbers in a row, attempts, cases or decisions write
    one key, while the attempt stays under 100, the index under 24 and a case's
    decisions under 60.
    """
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError(f"a run's number {number!r} is not a count from 1")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 99:
        raise ValueError(f"a run's attempt {attempt!r} is not 1 to 99")
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < 24:
        raise ValueError(f"a case's index {index!r} is not 0 to 23")
    day = EPOCH + timedelta(days=(number % RUN_PERIOD) * 100 + attempt)
    return Case(day.isoformat(), datetime(day.year, day.month, day.day, index, tzinfo=UTC))


def case_from_environment(index: int) -> Case:
    """Case `index`'s keys in this run of ci.yml, from what GitHub sets in every step."""
    read = {}
    for name in ("GITHUB_RUN_NUMBER", "GITHUB_RUN_ATTEMPT"):
        value = os.environ.get(name, "")
        if not value.isdigit():
            raise RuntimeError(
                f"{name} is {value!r}, so the case has no keys of its own. "
                "GitHub sets it in ci.yml's aws job, the one place these cases run."
            )
        read[name] = int(value)
    return run_case(read["GITHUB_RUN_NUMBER"], read["GITHUB_RUN_ATTEMPT"], index)


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


def header(stage: str, at: datetime, inputs: dict[str, str], run: str = RUN) -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=run,
        stage=stage,
        kind="deterministic",
        decision_at=at,
        inputs=inputs,
        release=RELEASE,
        config_hash=CONFIG,
    )


def read_value(
    store: Storage, stage: str, at: datetime, run: str = RUN
) -> tuple[int, dict[str, str]]:
    key = result_key(DEPLOYMENT, run, at, stage)
    result = store.read_result(key, at)
    assert result is not None, f"{key} was read before it was stored"
    (listed,) = result.outputs
    output = store.read_output(key, result, listed, at, Value)
    return output.entries[0].number, {listed.key: listed.sha256}


def write_value(
    store: Storage,
    item: Item,
    number: int,
    inputs: dict[str, str],
    report: Callable[[], None],
    run: str = RUN,
) -> DecisionResult:
    key = output_key(DEPLOYMENT, run, item.decision_at, item.stage, "value")
    head = header(item.stage, item.decision_at, inputs, run)
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
    run = the_plan.run
    lock = threading.Lock()

    def pause() -> None:
        if jitter is not None:
            with lock:
                wait = jitter.uniform(0, 0.02)
            time.sleep(wait)

    def feed(item: Item, report: Callable[[], None]) -> DecisionResult:
        calls.add(item)
        pause()
        return write_value(store, item, decisions.index(item.decision_at) + 1, {}, report, run)

    def alpha(item: Item, report: Callable[[], None]) -> DecisionResult:
        calls.add(item)
        number, inputs = read_value(store, "feed", item.decision_at, run)
        pause()
        return write_value(store, item, number * 2, inputs, report, run)

    def risk(item: Item, report: Callable[[], None]) -> DecisionResult:
        calls.add(item)
        number, inputs = read_value(store, "feed", item.decision_at, run)
        pause()
        return write_value(store, item, number + 10, inputs, report, run)

    def book(item: Item, report: Callable[[], None]) -> DecisionResult:
        calls.add(item)
        a, from_alpha = read_value(store, "alpha", item.decision_at, run)
        r, from_risk = read_value(store, "risk", item.decision_at, run)
        inputs = {**from_alpha, **from_risk}
        index = decisions.index(item.decision_at)
        before = 0
        if index:
            before, earlier = read_value(store, "book", decisions[index - 1], run)
            inputs.update(earlier)
        pause()
        return write_value(store, item, before + a + r, inputs, report, run)

    return {"feed": feed, "alpha": alpha, "risk": risk, "book": book}


def every_result(the_plan: Plan) -> list[str]:
    return [
        result_key(DEPLOYMENT, the_plan.run, d.at, stage.name)
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


# S3, EventBridge and SQS, on moto's server or in the test environment


@dataclass(frozen=True)
class Message:
    """One message a case read: its event's key, its body, its handle and when SQS took it."""

    key: str
    body: dict
    handle: str
    sent_ms: int

    def delay(self) -> float:
        """Seconds from the event's time, in whole seconds, to SQS taking its message."""
        at = datetime.fromisoformat(self.body["time"].replace("Z", "+00:00"))
        return self.sent_ms / 1000 - at.timestamp()


@dataclass
class Cloud:
    """One bucket, a queue for each reader, and the rules that route to them.

    `endpoint` is moto's server, or None for the test environment in AWS, whose
    bucket, queues and rules infra/test applies from infra/test/routes.json.
    """

    endpoint: str | None
    bucket: str
    # Left out of the repr, because a queue's URL names the account and pytest
    # prints a failing call's object in the public job log.
    urls: dict[str, str] = field(repr=False)
    case: Case = field(default_factory=Case)
    # Whether other cases share these queues, as every case shares the test
    # environment's. On moto each case makes queues of its own.
    shared: bool = False

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

    @classmethod
    def test_environment(cls, case: Case) -> Cloud:
        """The test bucket and each reader's test queue, which every case shares."""
        sqs = client("sqs", None)
        urls = {}
        for reader in READERS:
            name = f"{TEST_QUEUE_PREFIX}-{reader}"
            try:
                urls[reader] = sqs.get_queue_url(QueueName=name)["QueueUrl"]
            except ClientError as error:
                if "NonExistentQueue" not in str(error):
                    raise
                raise RuntimeError(
                    f"Queue {name} does not exist. If this merge changed "
                    "infra/test/routes.json, infra.yml's apply may not have finished."
                ) from None
        return cls(None, TEST_BUCKET, urls, case, shared=True)

    @property
    def on_aws(self) -> bool:
        return self.endpoint is None

    @property
    def settle(self) -> float:
        """How long a case reads on to show that nothing else arrives.

        On moto a delivery takes well under a second. In AWS a late delivery
        has as long as the delivery bound.
        """
        return DELIVERY_SECONDS if self.on_aws else 0.5

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

    def receive(self, component: str, wait: int = 1) -> list[Message]:
        """One receive from a component's queue, keeping the case's own messages.

        Where cases share the queue, as in AWS, a message of another case is
        deleted. On moto each case has queues of its own, so every message is
        the case's, a misrouted one included. The case's messages come back
        hidden for a minute, so the caller deletes or returns them.
        """
        return self._receive(component, wait)[0]

    def _receive(self, component: str, wait: int = 1) -> tuple[list[Message], bool]:
        """`receive`, and whether SQS returned any message at all."""
        sqs = client("sqs", self.endpoint)
        url = self.urls[component]
        reply = sqs.receive_message(
            QueueUrl=url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=wait,
            VisibilityTimeout=60,
            AttributeNames=["SentTimestamp"],
        )
        mine = []
        received = reply.get("Messages", [])
        for raw in received:
            body = json.loads(raw["Body"])
            key = body.get("detail", {}).get("object", {}).get("key", "")
            if not self.shared or self.case.mine(key):
                sent = int(raw.get("Attributes", {}).get("SentTimestamp", "0"))
                mine.append(Message(key, body, raw["ReceiptHandle"], sent))
            else:
                sqs.delete_message(QueueUrl=url, ReceiptHandle=raw["ReceiptHandle"])
        return mine, bool(received)

    def delete(self, component: str, messages: list[Message]) -> None:
        sqs = client("sqs", self.endpoint)
        for message in messages:
            sqs.delete_message(QueueUrl=self.urls[component], ReceiptHandle=message.handle)

    def give_back(self, component: str, messages: list[Message]) -> None:
        """Make messages visible again at once, as if nothing had received them."""
        sqs = client("sqs", self.endpoint)
        for message in messages:
            sqs.change_message_visibility(
                QueueUrl=self.urls[component], ReceiptHandle=message.handle, VisibilityTimeout=0
            )

    def messages(self, component: str) -> list[dict]:
        """The case's messages waiting on a component's queue, deleted as they are read.

        It reads until a receive returns nothing at all, because one that held
        only another case's messages says nothing about the case's own.
        """
        found: list[dict] = []
        while True:
            batch, any_at_all = self._receive(component)
            self.delete(component, batch)
            found.extend(m.body for m in batch)
            if not any_at_all:
                return found

    def keys_on(self, component: str) -> list[str]:
        return [m["detail"]["object"]["key"] for m in self.messages(component)]

    def wait_for(self, component: str, keys: set[str], timeout: float = DELIVERY_SECONDS) -> bool:
        """Wait until a queue has held every one of `keys`, leaving each message on it.

        Each message is received once and then made visible again, so the wait
        spends one of the queue's three receives on it. A queue's count of
        messages cannot stand in, because in AWS it counts every case's.
        """
        seen: set[str] = set()
        held: list[Message] = []
        end = time.monotonic() + timeout
        try:
            while time.monotonic() < end and not keys <= seen:
                for message in self.receive(component):
                    held.append(message)
                    seen.add(message.key)
        finally:
            self.give_back(component, held)
        return keys <= seen

    def drain(self, component: str, keys: set[str], timeout: float = DELIVERY_SECONDS) -> bool:
        """Delete a queue's messages for `keys` as they arrive, as a purge would.

        A purge clears every case's messages, and SQS allows one a minute,
        which the job's own purge has just spent.
        """
        seen: set[str] = set()
        end = time.monotonic() + timeout
        while time.monotonic() < end and not keys <= seen:
            batch = self.receive(component)
            self.delete(component, batch)
            seen.update(m.key for m in batch)
        return keys <= seen


def client(service: str, endpoint: str | None, config: Config | None = None):
    """A client for moto's server, or with no endpoint `aws_client`'s for AWS itself."""
    if endpoint is None:
        return aws_client(service, config)
    return boto3.client(
        service, endpoint_url=endpoint, region_name=REGION, config=config, **EMULATOR_KEYS
    )


def missing(reader: str, keys: list[str] | set[str], seconds: float) -> str:
    """What a case says when an event it waits for never reaches a queue.

    An assert builds its message only when it fails, so calling this also
    records that a delivery failed, which `undelivered` reads.
    """
    said = (
        f"{reader}'s queue received nothing for {', '.join(sorted(keys))} in {seconds} "
        "seconds. If this merge changed infra/test/routes.json, infra.yml's apply may "
        "not have finished."
    )
    _undelivered.append(said)
    return said


def undelivered() -> str | None:
    """The first missing delivery this run recorded, or None."""
    return _undelivered[0] if _undelivered else None


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


def hang_alpha(
    endpoint: str | None, bucket: str, url: str, marker: str, run: str, first: str
) -> None:
    """Run an alpha loop whose handler never ends, in a process the test kills.

    A spawned child inherits the environment, so in the test environment it
    reaches AWS with the job's own credentials.
    """
    case = Case(run, datetime.fromisoformat(first))
    cloud = Cloud(endpoint, bucket, {"alpha": url}, case)
    the_plan = case.plan(1)
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
        self.release_error: BaseException | None = None
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
        if self.release_error is not None:
            raise self.release_error


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
