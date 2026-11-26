"""The execution worker's entry point, through the receive loop over a folder.

#145's acceptance 5 and 9 name the cases. The table declares a stand-in send row,
because the send stage is #146's. Every price and quantity is made up.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import boto3
import pytest
from botocore.config import Config

from ithildinruntime.adapters import FolderStore, HistoryClock, PipeQueue, S3Store
from ithildinruntime.core import (
    BOOK_OUTPUT,
    DATA_STAGE,
    RECOVER_STAGE,
    SEND_STAGE,
    SNAPSHOT_OUTPUT,
    BlobHeader,
    BlobRef,
    Book,
    DecisionResult,
    EventKind,
    Halt,
    HaltRecord,
    Mode,
    OrderState,
    OutputFile,
    Plan,
    PlanDecision,
    Retry,
    Route,
    Snapshot,
    StageKind,
    Storage,
    caps_key,
    decode_output,
    encode,
    encode_output,
    output_key,
    result_key,
    sha256_of,
)
from ithildinruntime.execution.brokers.faults import Faults
from ithildinruntime.execution.brokers.simulated import SimulatedBroker
from ithildinruntime.journal import Journal, OpeningEntry, StopFiredEntry
from ithildinruntime.runtime import execution_worker
from ithildinruntime.runtime.data_worker import DATA_ROW
from ithildinruntime.runtime.execution_worker import RECOVER_ROW, ExecutionWorker
from ithildinruntime.runtime.loop import Item, ReceiveLoop, Stage, Table, routes
from tests.support import gates
from tests.support.order_world import Wired, caps_blob, order
from tests.support.queues import Alarms, SystemTimer, running
from tests.support.sim_world import CASH, SimWorld, after, close_of
from tests.test_risk import CHAN, MOVES

ACCOUNT, DEPLOYMENT, RELEASE = "bt-1", "ci", "sha256:release"
DAYS = [date(2026, 11, 2)]
while len(DAYS) < 5:
    DAYS.append(after(DAYS[-1]))
AT = [close_of(day) for day in DAYS[1:]]
RUN = f"{DAYS[1].isoformat()}_{DAYS[4].isoformat()}"
SEND_ROW = Stage(
    SEND_STAGE, StageKind.CAPTURING, inputs=(DATA_STAGE, RECOVER_STAGE), keeps_order=True
)
ROWS = (DATA_ROW, RECOVER_ROW, SEND_ROW)
STAGES = Table(stages=ROWS, strategies=(CHAN,))


def header(stage: str, at: datetime) -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        stage=stage,
        kind="capturing",
        decision_at=at,
        inputs={},
        release=RELEASE,
        config_hashes=STAGES.hashes_for(stage),
    )


def key(stage: str, n: int) -> str:
    return result_key(DEPLOYMENT, RUN, AT[n], stage)


@dataclass
class Case:
    """One deployment's folder, with data's results stored and the account opened."""

    root: Path
    decisions: int = 2
    alarms: Alarms = field(default_factory=Alarms)

    def __post_init__(self) -> None:
        self.pipes = self.root / "queues"
        self.folder = self.root / "store"
        self.queue = PipeQueue(self.pipes, "execution", wait_seconds=0.2)
        self.store = FolderStore(
            self.folder, bucket="folder", routes=routes(STAGES, DEPLOYMENT), pipes=self.pipes
        )
        # One that stores and announces nothing, as an event that is lost reads.
        self.silent = FolderStore(self.folder, bucket="folder")
        self.storage = self.storage_over(self.store)
        closes = {i: dict.fromkeys(DAYS, 100) for i in (gates.GLD, gates.USO)}
        self.world = SimWorld(closes)

    def storage_over(self, store: FolderStore) -> Storage:
        return Storage(
            store,
            timer=SystemTimer(),
            pause=lambda s: None,
            retry=Retry(timedelta(seconds=2), 0.05, 0.2),
            alerts=self.alarms,
            owner_step="the broker's site",
        )

    def opened(self) -> None:
        clock = HistoryClock(readings=None)
        clock.wait_until(close_of(DAYS[0]), lambda: None)  # type: ignore[arg-type]
        journal = Journal(self.silent, ACCOUNT, "a" * 32)
        journal.record(journal.view(), OpeningEntry(ACCOUNT, CASH), close_of(DAYS[0]))
        SimulatedBroker.open(self.silent, ACCOUNT, CASH)
        blob = caps_blob(self.world.caps, ACCOUNT)
        self.silent.create(caps_key(ACCOUNT), blob, sha256_of(blob))

    def data(self, n: int, store: FolderStore | None = None) -> None:
        head = header(DATA_STAGE, AT[n])
        made = self.world.snapshot(AT[n])
        blob = encode_output(Snapshot, OutputFile(head, (made,), False))
        ref = BlobRef(
            output_key(DEPLOYMENT, RUN, AT[n], DATA_STAGE, SNAPSHOT_OUTPUT), sha256_of(blob)
        )
        target = store or self.store
        target.create(ref.key, blob, ref.sha256)
        self.result(DecisionResult(head, (ref,), None), n, DATA_STAGE, target)

    def result(
        self, result: DecisionResult, n: int, stage: str, store: FolderStore | None = None
    ) -> None:
        blob = encode(result)
        (store or self.store).create(key(stage, n), blob, sha256_of(blob))

    def sent(self, n: int, store: FolderStore | None = None, halt: str | None = None) -> None:
        record = None if halt is None else HaltRecord(halt, None, (), (), ())
        self.result(DecisionResult(header(SEND_STAGE, AT[n]), (), record), n, SEND_STAGE, store)

    def worker(self, mode: object = Mode.BACKTEST, **changes: object) -> ExecutionWorker:
        given: dict[str, object] = {
            "mode": mode,
            "store": self.store,
            "storage": self.storage,
            "account": ACCOUNT,
            "clock": HistoryClock(readings=None),
            "alerts": self.alarms,
            "retry": Retry(timedelta(seconds=2), 0.05, 0.2),
            "pause": lambda s: None,
            "deployment": DEPLOYMENT,
            "run": RUN,
            "release": RELEASE,
            "table": STAGES,
            "stress_moves": MOVES,
        }
        return ExecutionWorker(**given | changes)  # type: ignore[arg-type]

    def loop(self, handlers: dict[str, Callable], table: Table = STAGES) -> ReceiveLoop:
        deciding = tuple(PlanDecision(at, (CHAN.name,)) for at in AT[: self.decisions])
        return ReceiveLoop(
            worker="execution-1",
            component="execution",
            table=table,
            plan=Plan(1, DEPLOYMENT, RUN, Mode.BACKTEST, RELEASE, deciding),
            queue=self.queue,
            storage=self.storage,
            bucket="folder",
            handlers=handlers,  # type: ignore[arg-type]
            alerts=self.alarms,
            timer=SystemTimer(),
            beat=lambda: None,
            tick=0.05,
            stop_timeout=5.0,
        )

    def stored(self, stage: str, n: int) -> bool:
        return (self.folder / key(stage, n)).exists()

    def read(self, stage: str, n: int) -> DecisionResult:
        found = self.storage.read_result(key(stage, n), AT[n])
        assert found is not None
        return found


@pytest.fixture
def case(tmp_path: Path) -> Iterator[Case]:
    made = Case(tmp_path)
    yield made
    made.queue.close()


def send(item: Item, report: Callable[[], None]) -> DecisionResult:
    """A stand-in send stage, which sends nothing."""
    return DecisionResult(header(SEND_STAGE, item.decision_at), (), None)


# Acceptance 9: each mode


def test_9_a_backtest_over_a_folder_recovers_through_the_loop(case: Case, monkeypatch) -> None:
    built: list[tuple] = []

    def simulated(*args: object) -> SimulatedBroker:
        built.append(args)
        return SimulatedBroker(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(execution_worker, "SimulatedBroker", simulated)
    case.opened()
    worker = case.worker()
    assert set(worker.handlers) == {RECOVER_STAGE, SEND_STAGE}
    case.data(0)
    case.data(1)
    with running({"execution": case.loop({**worker.handlers, SEND_STAGE: send})}) as started:
        assert started.wait_for(lambda: case.stored(SEND_STAGE, 1), 20)
    for n in (0, 1):
        result = case.read(RECOVER_STAGE, n)
        assert result.halt is None, result.halt
        (ref,) = [r for r in result.outputs if r.key.endswith(f"/{BOOK_OUTPUT}.json")]
        blob = (case.folder / ref.key).read_bytes()
        (book,) = decode_output(blob, Book).entries
        assert (book.cash, dict(book.positions_by_strategy)) == (CASH, {})
    assert case.alarms.reasons == []
    # One broker a decision over the one log, at the decision's prices, with no fault.
    assert len(built) == 2
    log, clock, prices, faults = built[0]
    assert built[1][0] is log and log.account == ACCOUNT
    assert [made[2].decision_at for made in built] == AT[:2]
    assert all(made[3] == Faults() for made in built)
    assert isinstance(clock, HistoryClock) and faults.events == faults.reads == faults.orders == {}


SHADOW = "#147, #148, #149, #150, #151 and #155"


def s3_store() -> S3Store:
    """A store in S3 that nothing here calls."""
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="emulator",
        aws_secret_access_key="emulator",
        config=Config(retries={"total_max_attempts": 1}),
    )
    return S3Store(client, "no-bucket")


MODES = {
    # Each names the parts still open. A part that closes is taken out here and in the reason.
    "shadow": (Mode.SHADOW, f"no broker is built on shadow until {SHADOW} are closed"),
    "paper": (
        Mode.PAPER,
        f"no broker is built on paper until #153 is, once {SHADOW} are closed",
    ),
    "live": (
        Mode.LIVE,
        "no broker is built on live, which only the owner switches an account to",
    ),
    "unknown": (
        "sandbox",
        "no broker is built: the mode is none of backtest, shadow, paper and live",
    ),
    "no mode": (
        None,
        "no broker is built: the mode is none of backtest, shadow, paper and live",
    ),
    "a backtest over s3": (
        Mode.BACKTEST,
        "no broker is built for a backtest over any store but a folder, since nothing its account "
        "stores elsewhere can be deleted",
    ),
    "a backtest over another store": (
        Mode.BACKTEST,
        "no broker is built for a backtest over any store but a folder, since nothing its account "
        "stores elsewhere can be deleted",
    ),
}


@pytest.mark.parametrize("name", MODES)
def test_9_every_other_mode_halts_each_decision_and_builds_no_broker(
    case: Case, monkeypatch, name: str
) -> None:
    mode, reason = MODES[name]

    def built(*args: object) -> object:
        raise AssertionError("a broker was built")

    monkeypatch.setattr(execution_worker, "SimulatedBroker", built)
    monkeypatch.setattr(execution_worker.SimulatedAccount, "load", built)
    case.opened()
    stores = {"a backtest over s3": s3_store, "a backtest over another store": Wired}
    changes = {"store": stores[name]()} if name in stores else {}
    worker = case.worker(mode, **changes)
    assert isinstance(worker._brokers, Halt) and worker._brokers.reason == reason
    case.data(0)
    case.data(1)
    with running({"execution": case.loop({**worker.handlers, SEND_STAGE: send})}) as started:
        assert started.wait_for(lambda: case.stored(SEND_STAGE, 1), 20)
    for n in (0, 1):
        result = case.read(RECOVER_STAGE, n)
        assert result.halt is not None and result.halt.reason == reason
        assert (result.outputs, dict(result.header.inputs)) == ((), {})
    # Nothing was journaled, and no page of the loop's own went out.
    assert case.alarms.reasons == []
    assert [p.name for p in (case.folder / "journal" / ACCOUNT).iterdir()] == ["000000000001.json"]


def test_9_the_reason_of_each_mode_names_parts_that_are_parts_of_execution() -> None:
    """The list is typed once here and once in the worker, so a part dropped from one shows."""
    assert execution_worker._SHADOW == SHADOW
    assert execution_worker._PAPER == "#153"


def test_9_two_starts_of_one_run_draw_two_writers(case: Case) -> None:
    case.opened()
    writers = []
    for _ in range(2):
        worker = case.worker()
        journals = worker.account._shared.journal._writer
        log = worker._brokers(AT[0])._log  # type: ignore[operator]
        writers += [journals, log.writer]
    assert len(set(writers)) == 4
    for writer in writers:
        assert len(writer) == 32 and set(writer) <= set("0123456789abcdef")
        assert DEPLOYMENT not in writer and ACCOUNT not in writer


def test_9_no_account_has_a_default() -> None:
    with pytest.raises(TypeError, match="account"):
        ExecutionWorker(mode=Mode.BACKTEST)  # type: ignore[call-arg]


@pytest.mark.parametrize("account", ["", "a/b", ".."])
def test_9_an_alias_that_names_no_account_halts_each_decision_and_not_the_start(
    case: Case, account: str
) -> None:
    worker = case.worker(account=account)
    case.data(0, case.silent)
    result = worker.recover(Item(AT[0], RECOVER_STAGE), _Quiet())
    assert result.halt is not None and "is no alias a journal is kept under" in result.halt.reason


def test_9_stress_records_that_cannot_be_read_stop_no_start(case: Case, tmp_path: Path) -> None:
    """The gate halts each order on them. Recover reads none."""
    case.opened()
    worker = case.worker(stress_moves=tmp_path / "none.toml")
    case.data(0, case.silent)
    assert worker.recover(Item(AT[0], RECOVER_STAGE), _Quiet()).halt is None


class _Quiet:
    def __call__(self) -> None:
        pass

    def wait_for_stop(self, timeout: float) -> bool:
        raise AssertionError("a history clock waits for nothing")


# The end of the loop


class Looped:
    """A loop that ends as a test says."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.ran = 0

    def run(self) -> None:
        self.ran += 1
        if self.error is not None:
            raise self.error


def owing(case: Case, refuse: bool) -> tuple[ExecutionWorker, Wired, list[str]]:
    """A worker that holds an answer its journal has not taken, over a store in memory.

    The third value is the list of what the account's alarms said.
    """
    store = Wired()
    worker = case.worker(store=store)
    planner = SimWorld({i: dict.fromkeys(DAYS, 100) for i in (gates.GLD, gates.USO)}, store=store)
    (made,) = planner.decide(AT[0], (order(1, 5, gates.GLD, at=AT[0]),))
    refused = f"journal/{planner.planner._account}/{len(planner_rows(store)) + 4:012d}.json"
    store.refuse(("create", refused))
    with pytest.raises(Halt, match="did not take the ack"):
        planner.send(made)
    assert planner.account.unstored()
    # The process under test holds it, and its account is the one that sent.
    worker.account = planner.account
    if not refuse:
        store.before = lambda n, op, key: None
    planner.alarms.sent.clear()
    return worker, store, planner.alarms.sent


def planner_rows(store: Wired) -> list[str]:
    return [k for k in store.blobs if k.startswith("journal/")]


ACK = "the ack of ith-chan-20261103T210000Z-01, which was sent"
STEP = "The owner's step: the broker's site"


@pytest.mark.parametrize("error", [None, RuntimeError("the loop failed")])
def test_10_an_answer_still_owed_is_stored_once_the_loop_ends(case: Case, error) -> None:
    worker, store, said = owing(case, refuse=False)
    loop = Looped(error)
    if error is None:
        worker.run(loop)  # type: ignore[arg-type]
    else:
        with pytest.raises(RuntimeError, match="the loop failed"):
            worker.run(loop)  # type: ignore[arg-type]
    assert loop.ran == 1 and worker.account.unstored() == ()
    (held,) = Journal(store, "sim-1").view().live.values()
    assert held.state is OrderState.ACKNOWLEDGED
    # It paged ahead of the tries, which can outlast the host's stop, naming what it owed.
    (first,) = said
    assert (
        first.startswith(f"account sim-1: the process is ending before the journal holds {ACK}")
        and STEP in first
    )


@pytest.mark.parametrize("error", [None, RuntimeError("the loop failed")])
def test_10_an_answer_that_is_lost_with_the_process_alarms_and_ends_it_with_an_error(
    case: Case, error
) -> None:
    worker, _, said = owing(case, refuse=True)
    with pytest.raises(Halt, match="the process ends and the journal was not given") as raised:
        worker.run(Looped(error))  # type: ignore[arg-type]
    assert ACK in raised.value.reason
    assert len(said) == 2 and said[1] == f"account sim-1: {raised.value.reason}. {STEP}"
    # What the loop ended with is in the error's context, for the host's restart record.
    assert (raised.value.__context__ is error) if error else True


def test_10_an_alarm_the_channel_will_not_take_is_named_as_the_process_ends(case: Case) -> None:
    worker, _, _ = owing(case, refuse=False)
    worker.account._shared.alerts._alerts.error = RuntimeError("PKFAKEKEY")  # type: ignore[union-attr]
    with pytest.raises(Halt, match="was not sent") as raised:
        worker.run(Looped())  # type: ignore[arg-type]
    assert raised.value.reason.startswith("the process ends and the alarm 'the process is ending")
    assert raised.value.reason.endswith("was not sent")
    assert "PKFAKEKEY" not in raised.value.reason


def test_10_an_answer_and_its_alarm_both_lost_are_named_as_the_process_ends(case: Case) -> None:
    worker, _, _ = owing(case, refuse=True)
    worker.account._shared.alerts._alerts.error = RuntimeError("PKFAKEKEY")  # type: ignore[union-attr]
    with pytest.raises(Halt) as raised:
        worker.run(Looped())  # type: ignore[arg-type]
    said = raised.value.reason
    assert f"the journal was not given {ACK}" in said
    assert "the alarm 'the process is ending before the journal holds" in said
    assert "the alarm 'the process ends and the journal was not given" in said
    assert "PKFAKEKEY" not in said


def test_10_a_last_settle_that_fails_with_nothing_owed_ends_with_an_error(case: Case) -> None:
    worker = case.worker()

    def broken(report: Callable[[], None], **_: object) -> None:
        raise RuntimeError("PKFAKEKEY")

    worker.account.settle = broken  # type: ignore[method-assign]
    with pytest.raises(Halt) as raised:
        worker.run(Looped())  # type: ignore[arg-type]
    said = "the process ends and its last settle failed: RuntimeError. The owner's step: "
    assert raised.value.reason == f"{said}the broker's site"
    assert case.alarms.reasons == [f"account {ACCOUNT}: {raised.value.reason}"]


def test_10_a_failed_last_settle_whose_alarm_is_refused_says_so(case: Case) -> None:
    worker = case.worker()

    def broken(report: Callable[[], None], **_: object) -> None:
        raise RuntimeError("PKFAKEKEY")

    def refused(reason: str) -> None:
        raise RuntimeError("PKFAKEKEY")

    worker.account.settle = broken  # type: ignore[method-assign]
    case.alarms.alarm = refused  # type: ignore[method-assign]
    with pytest.raises(Halt) as raised:
        worker.run(Looped())  # type: ignore[arg-type]
    said = "the process ends and its last settle failed: RuntimeError. The owner's step: "
    assert raised.value.reason == f"{said}the broker's site. The alarm that says so was not sent"


def test_10_an_error_that_is_no_halt_at_the_end_names_its_type_alone(case: Case) -> None:
    worker, _, said = owing(case, refuse=True)

    def broken(report: Callable[[], None], **_: object) -> None:
        raise RuntimeError("PKFAKEKEY")

    worker.account.settle = broken  # type: ignore[method-assign]
    with pytest.raises(Halt, match=": RuntimeError$") as raised:
        worker.run(Looped())  # type: ignore[arg-type]
    assert "PKFAKEKEY" not in raised.value.reason + str(said)


def test_10_an_account_that_owes_nothing_ends_quietly_under_the_history_clock(case: Case) -> None:
    """The thread that ends the loop has waited for no decision, so its clock has no time."""
    worker = case.worker()
    worker.run(Looped())  # type: ignore[arg-type]
    assert case.alarms.reasons == []


def test_10_an_account_that_cannot_be_used_loses_nothing_as_the_process_ends(case: Case) -> None:
    worker = case.worker(account="a/b")
    worker.run(Looped())  # type: ignore[arg-type]
    assert case.alarms.reasons == []


@pytest.mark.parametrize(
    "changed",
    [
        {"follows": ()},
        {"keeps_order": False},
        {"kind": StageKind.DETERMINISTIC},
        {"inputs": ()},
    ],
)
def test_9_a_table_whose_recover_row_is_another_halts_each_decision(
    case: Case, changed: dict
) -> None:
    case.opened()
    rows = (DATA_ROW, replace(RECOVER_ROW, **changed), SEND_ROW)
    worker = case.worker(table=Table(stages=rows, strategies=(CHAN,)))
    case.data(0, case.silent)
    result = worker.recover(Item(AT[0], RECOVER_STAGE), _Quiet())
    assert result.halt is not None
    assert result.halt.reason == (
        "the table's row of execution/recover is not the one it declares, so set recover's row "
        "in the deployment's stage table to ithildinruntime.runtime.execution_worker.RECOVER_ROW, "
        "which captures, reads data, keeps the plan's order and follows execution/send"
    )


def test_9_a_table_whose_send_row_is_another_halts_send_alone(case: Case) -> None:
    # This file's own row of send reads data and recover alone, so it is not the worker's.
    case.opened()
    worker = case.worker()
    case.data(0, case.silent)
    assert worker.send is not None
    result = worker.send(Item(AT[0], SEND_STAGE), _Quiet())
    assert result.halt is not None
    assert result.halt.reason.startswith(
        "the table's row of execution/send is not the one it declares, so set send's row in the "
        "deployment's stage table to ithildinruntime.runtime.execution_worker.SEND_ROW"
    )
    # Recover still runs, since its row is the one the worker declares.
    assert worker.recover(Item(AT[0], RECOVER_STAGE), _Quiet()).halt is None


# Acceptance 5: the order of stages


@dataclass
class Order:
    """Stand-in handlers that note each start and the results stored by then."""

    case: Case
    starts: list[tuple[str, int, bool]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    halts: frozenset[int] = frozenset()

    def recover(self, item: Item, report: Callable[[], None]) -> DecisionResult:
        n = AT.index(item.decision_at)
        with self.lock:
            self.starts.append((item.stage, n, n == 0 or self.case.stored(SEND_STAGE, n - 1)))
        return DecisionResult(header(RECOVER_STAGE, item.decision_at), (), None)

    def send(self, item: Item, report: Callable[[], None]) -> DecisionResult:
        n = AT.index(item.decision_at)
        with self.lock:
            self.starts.append((item.stage, n, self.case.stored(RECOVER_STAGE, n)))
        if n in self.halts:
            raise Halt("the stand-in send stage halts")
        return send(item, report)

    @property
    def handlers(self) -> dict[str, Callable]:
        return {RECOVER_STAGE: self.recover, SEND_STAGE: self.send}


@contextmanager
def ordered(case: Case, halts: frozenset[int] = frozenset()) -> Iterator[tuple[Order, object]]:
    order_ = Order(case, halts=halts)
    with running({"execution": case.loop(order_.handlers)}) as started:
        yield order_, started


def test_5_recover_starts_once_the_send_before_it_is_stored(case: Case) -> None:
    case.decisions = 4
    with ordered(case, frozenset({1})) as (order_, started):
        # Every data result is there at once, so every decision is ready but for the order.
        for n in reversed(range(4)):
            case.data(n)
        assert started.wait_for(lambda: case.stored(SEND_STAGE, 3), 20)
    assert order_.starts == [
        (stage, n, True) for n in range(4) for stage in (RECOVER_STAGE, SEND_STAGE)
    ]
    # One of the sends halted, and the next decision went on.
    assert case.read(SEND_STAGE, 1).halt is not None


@contextmanager
def recovering(case: Case) -> Iterator[tuple[list[int], object]]:
    """A loop whose send handler never finishes, so each send result comes from elsewhere."""
    starts: list[int] = []
    release = threading.Event()

    def recover(item: Item, report: Callable[[], None]) -> DecisionResult:
        starts.append(AT.index(item.decision_at))
        return DecisionResult(header(RECOVER_STAGE, item.decision_at), (), None)

    def hung(item: Item, report: Callable[[], None]) -> DecisionResult:
        while not release.wait(0.05):
            report()
        return send(item, report)

    try:
        with running({"execution": case.loop({RECOVER_STAGE: recover, SEND_STAGE: hung})}) as s:
            yield starts, s
            release.set()
    finally:
        release.set()


@pytest.mark.parametrize("halt", [None, "another instance's send halted"])
def test_5_a_send_result_another_process_stored_starts_the_recover_after_it(
    case: Case, halt: str | None
) -> None:
    with recovering(case) as (starts, started):
        case.data(0)
        case.data(1)
        assert started.wait_for(lambda: case.stored(RECOVER_STAGE, 0), 10)
        # The second decision's data is stored and nothing starts it yet.
        assert not started.wait_for(lambda: len(starts) > 1, 0.5)
        case.sent(0, halt=halt)
        assert started.wait_for(lambda: starts == [0, 1], 10)


def test_5_with_the_event_lost_the_next_event_starts_it(case: Case) -> None:
    with recovering(case) as (starts, started):
        case.data(0)
        case.data(1)
        assert started.wait_for(lambda: case.stored(RECOVER_STAGE, 0), 10)
        case.sent(0, case.silent)
        # Another event of this component's, about a result already stored, only wakes it.
        case.store.announce(key(DATA_STAGE, 1))
        assert started.wait_for(lambda: starts == [0, 1], 10)


def test_5_a_loop_that_starts_after_the_send_was_stored_starts_it(case: Case) -> None:
    case.data(0, case.silent)
    case.data(1, case.silent)
    case.result(
        DecisionResult(header(RECOVER_STAGE, AT[0]), (), None), 0, RECOVER_STAGE, case.silent
    )
    case.sent(0, case.silent)
    with recovering(case) as (starts, started):
        assert started.wait_for(lambda: starts == [1], 10)


def test_5_the_routes_bring_recover_the_results_it_reads_and_follows() -> None:
    heard = {
        r.key
        for r in routes(Table(stages=ROWS, strategies=(CHAN,)), "ci")
        if r.reader == "execution"
    }
    assert heard == {
        "runs/ci/*/data/result.json",
        "runs/ci/*/execution/recover/result.json",
        "runs/ci/*/execution/send/result.json",
    }
    follower = Stage("after", StageKind.DETERMINISTIC, follows=(SEND_STAGE,))
    only = routes(Table(stages=(*ROWS, follower), strategies=(CHAN,)), "ci")
    assert Route("after", EventKind.CREATED, "runs/ci/*/execution/send/result.json") in only
    # A stage that follows another and keeps no order of its own hears no result of its own.
    assert not [r for r in only if r.reader == "after" and "after/result" in r.key]


def test_5_the_row_is_the_one_the_contract_gives() -> None:
    assert RECOVER_ROW == Stage(
        "execution/recover",
        StageKind.CAPTURING,
        inputs=("data",),
        keeps_order=True,
        follows=("execution/send",),
    )


@pytest.mark.parametrize(
    ("rows", "said"),
    [
        ((DATA_ROW, RECOVER_ROW), "follows execution/send, which no row declares"),
        (
            (DATA_ROW, replace(RECOVER_ROW, follows=(RECOVER_STAGE,)), SEND_ROW),
            "execution/recover follows itself",
        ),
    ],
)
def test_5_the_table_refuses_a_follow_it_cannot_hold(rows: tuple, said: str) -> None:
    with pytest.raises(ValueError, match=said):
        Table(stages=rows, strategies=(CHAN,))


def test_5_two_stages_that_follow_each_other_are_no_cycle() -> None:
    """Each waits for the other's result of the decision before, which is always further back."""
    one = Stage("one", StageKind.DETERMINISTIC, follows=("two",))
    two = Stage("two", StageKind.DETERMINISTIC, follows=("one",))
    assert Table(stages=(one, two), strategies=(CHAN,)).stage("one").follows == ("two",)


def test_5_a_stage_that_follows_and_keeps_no_order_starts_at_its_event(case: Case) -> None:
    """It waits for the followed result alone, and an earlier decision not ready holds it back not.

    A check goes no further than the first decision that is not ready, so only the
    event's own decision starts here.
    """
    case.decisions = 3
    follower = Stage("execution/after", StageKind.DETERMINISTIC, follows=(DATA_STAGE,))
    table = Table(stages=(DATA_ROW, follower), strategies=(CHAN,))
    case.store._routes = routes(table, DEPLOYMENT)
    starts: list[int] = []

    def after_(item: Item, report: Callable[[], None]) -> DecisionResult:
        starts.append(AT.index(item.decision_at))
        head = replace(header(DATA_STAGE, item.decision_at), stage="execution/after")
        return DecisionResult(head, (), None)

    with running({"execution": case.loop({"execution/after": after_}, table)}) as started:
        # The first decision of a plan follows nothing.
        assert started.wait_for(lambda: starts == [0], 10)
        # Decision 2 follows decision 1's result, and decision 1 still waits for decision 0's.
        case.data(1)
        assert started.wait_for(lambda: starts == [0, 2], 10)
        case.data(0)
        assert started.wait_for(lambda: starts == [0, 2, 1], 10)


def test_10_the_last_settle_journals_a_firing_the_process_holds(case: Case) -> None:
    """A firing held in memory alone is tried once more, and the journal takes it."""
    case.opened()
    worker = case.worker()
    latch = worker.account._shared.latch
    latch._pending.append(
        StopFiredEntry("a" * 32, DEPLOYMENT, RUN, AT[0], None, "a firing no write took")
    )
    worker.run(Looped())  # type: ignore[arg-type]
    assert latch.held() == ()
    standing = worker.account.view(lambda: None).stop.standing
    assert standing.entry.firing == "a" * 32 and standing.stored is not None
    assert any("the loss stop's firing" in reason for reason in case.alarms.reasons)


def test_10_a_firing_that_may_end_with_the_process_pages_to_hold_the_account(case: Case) -> None:
    case.opened()
    worker = case.worker()
    latch = worker.account._shared.latch
    latch._pending.append(StopFiredEntry("a" * 32, DEPLOYMENT, RUN, AT[0], None, "no write took"))
    latch.complete = lambda report: None  # type: ignore[method-assign]
    with pytest.raises(Halt) as raised:
        worker.run(Looped())  # type: ignore[arg-type]
    hold = "pull the account's kill switch at once"
    assert hold in raised.value.reason
    assert all(hold in reason for reason in case.alarms.reasons)


def test_10_the_hold_step_follows_what_is_still_owed_after_the_last_settle(case: Case) -> None:
    """A firing the last settle journals drops the step, and one it leaves owed keeps it."""
    hold = "pull the account's kill switch at once"
    for before, stays in ((True, False), (False, True)):
        worker, _, sent = owing(case, refuse=True)
        latch = worker.account._shared.latch
        entry = StopFiredEntry("b" * 32, DEPLOYMENT, RUN, AT[0], None, "no write took")
        if before:
            latch._pending.append(entry)

        def settled(report, before=before, latch=latch, entry=entry):  # type: ignore[no-untyped-def]
            # The journal takes the firing held before, or a firing comes during the settle.
            latch._pending[:] = [] if before else [entry]

        latch.complete = settled  # type: ignore[method-assign]
        with pytest.raises(Halt) as raised:
            worker.run(Looped())  # type: ignore[arg-type]
        assert (hold in raised.value.reason) is stays
        assert (hold in sent[-1]) is stays and (hold in sent[0]) is before
        assert ACK in raised.value.reason


def test_10_a_loop_that_holds_another_storage_than_the_account_is_not_run(case: Case) -> None:
    """A stop's blob the account holds keeps the process alive only through the loop's own."""
    worker = case.worker()
    loop = Looped()
    loop.storage = object()  # type: ignore[attr-defined]
    with pytest.raises(Halt, match="another storage contract than the account"):
        worker.run(loop)  # type: ignore[arg-type]
    assert loop.ran == 0
    loop.storage = worker.account._shared.storage  # type: ignore[attr-defined]
    worker.run(loop)  # type: ignore[arg-type]
    assert loop.ran == 1


def test_10_a_real_loop_over_another_storage_than_the_account_is_not_run(case: Case) -> None:
    """The real loop names its own storage, so the check above cannot pass it by."""
    worker = case.worker(storage=case.storage_over(case.store))
    loop = case.loop({RECOVER_STAGE: worker.recover, SEND_STAGE: worker.send})
    assert loop.storage is case.storage
    # Stopped before it runs, so a check that let it through fails here rather than hangs.
    loop.stop()
    with pytest.raises(Halt, match="another storage contract than the account") as raised:
        worker.run(loop)
    step = "The owner's step: build the worker's loop and its account over one storage contract"
    assert raised.value.reason.endswith(step)
    assert case.alarms.reasons == [f"account {ACCOUNT}: {raised.value.reason}"]
    worker._alerts = type("Refused", (), {"alarm": lambda self, reason: 1 / 0})()
    with pytest.raises(Halt, match=f"{step}. The alarm could not be sent$"):
        worker.run(loop)
