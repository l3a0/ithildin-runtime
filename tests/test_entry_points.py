"""Every entry point started with stand-ins, in order and in the wrong order, as #200 asks.

A backtest over a folder is the one mode that builds a broker, so it is the
one where every stage writes outputs rather than halts. Each worker's core,
`run_<worker>` in runtime/entry.py, runs on a thread over one folder store,
its own named pipe and the history clock, and the supervisor's core writes the
plan and the stop. Three decisions at close−20 read a made lake of captured
bars, made-up quotes and a made-up quote-kind row, under the tracked strategy
file, its G and its limits, with a made-up `[data.quotes]` and an entry
threshold low enough that the first decision trades. Every price is made up,
so no figure here says anything of an edge.

Starting the workers before the supervisor and in reverse order must store the
same results, because each stage waits for its inputs' results and never for
another process, as 06-runtime.md says of the reboot that liquidated a book.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket, FolderStore, HistoryClock, PipeQueue, SystemTimer
from ithildinruntime.core import (
    STRATEGIES,
    BeforeClose,
    DecisionResult,
    Mode,
    Retry,
    Storage,
    caps_key,
    decode,
    load_sessions,
    load_strategies,
    result_key,
    run_part,
    sha256_of,
    to_utc,
)
from ithildinruntime.data.reference import QUOTE_SOURCE, QuoteKindRow, QuoteKinds
from ithildinruntime.journal import FillEntry, Journal, record_of
from ithildinruntime.runtime import entry
from ithildinruntime.runtime.data_worker import DataBuckets
from ithildinruntime.runtime.deployment import WORKERS, stage_table
from ithildinruntime.runtime.execution_worker import open_backtest
from ithildinruntime.runtime.loop import routes
from ithildinruntime.runtime.supervisor import EXIT_STOPPED, Supervisor
from tests.support import gates, segments
from tests.support import lake as lakes
from tests.support import made_lake as made
from tests.support.order_world import caps_blob
from tests.support.trading import _record, vintage
from tests.test_captured_bars import BEFORE_CAPTURE, CAPTURED, IMPORTED, WALKS
from tests.test_storage_contract import Alarms

SESSIONS = load_sessions()
DEPLOYMENT, ACCOUNT, RELEASE = "ci", "bt-entry", "sha256:release"
# Three sessions whose decisions read captured bars alone, as test_captured_bars.py's DAY does.
DAYS = [date(2027, 1, 29), date(2027, 2, 1), date(2027, 2, 2)]
RUN = run_part(Mode.BACKTEST, DAYS[0], DAYS[-1])
AT = [to_utc(day, BeforeClose(20), SESSIONS) for day in DAYS]
THROUGH = date(2027, 1, 28)
NAME = "chan_bollinger"
# The tracked file, with an entry low enough that the first decision trades.
TRACKED = (STRATEGIES / f"{NAME}.toml").read_text()
FILE = (
    TRACKED.replace("entry = 1\n", "entry = 0.01\n")
    + "\n[data.quotes]\nmax_age_seconds = 90\nmax_spread_share = 0.02\n"
)
KINDS = QuoteKinds(
    (QuoteKindRow(DAYS[0], QUOTE_SOURCE, DAYS[0], DAYS[-1], segments.KIND, "a test"),)
)
START_ORDER = ("data", "alpha", "cost", "risk", "portfolio", "execution")


@pytest.fixture(scope="module")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("entry") / "lake"
    made.build(
        root,
        [
            made.Made(t, IMPORTED.format(t), date(2026, 10, 31), {d: w[d] for d in BEFORE_CAPTURE})
            for t, w in WALKS.items()
        ],
        {t: {d: w[d] for d in CAPTURED} for t, w in WALKS.items()},
    )
    live = root.with_name("lake-live")
    for day, at in zip(DAYS, AT, strict=True):
        for ticker, walk in WALKS.items():
            mid = walk[day]
            bid, ask = float(mid - Decimal("0.01")), float(mid + Decimal("0.01"))
            minute = at - timedelta(minutes=1)
            lakes.quotes_segment(
                live, ticker, minute, lakes.quote_row(ticker, minute, bid=bid, ask=ask, last=bid)
            )
    return root


class Run:
    """One run of every entry point over its own store and pipes."""

    def __init__(self, root: Path, lake: Path) -> None:
        self.root = root
        folder = root / "strategies"
        folder.mkdir(parents=True)
        (folder / f"{NAME}.toml").write_text(FILE)
        self.table = stage_table(load_strategies(folder, [NAME]))
        self.pipes = root / "pipes"
        self.store = FolderStore(
            root / "store", routes=routes(self.table, DEPLOYMENT), pipes=self.pipes
        )
        self.alarms = Alarms()
        self.storage = Storage(
            self.store,
            timer=SystemTimer(),
            pause=lambda s: None,
            retry=Retry(timedelta(seconds=2), 0.05, 0.2),
            alerts=self.alarms,
            owner_step="nothing to do: a test",
        )
        self.lake = lake
        self.records = root / "stress_moves.toml"
        self.records.write_text(_record(THROUGH))
        self.stop = threading.Event()
        self.ended: dict[str, int] = {}
        self.errors: dict[str, BaseException] = {}
        opened = SESSIONS.session(SESSIONS.before(DAYS[0]).day).close
        open_backtest(Mode.BACKTEST, self.store, ACCOUNT, Decimal(1_000_000), opened)
        caps = gates.caps(
            share=gates.d("0.99"),
            stress_move=gates.d("0.347"),
            stress_vintage=vintage(THROUGH),
            stress_through=THROUGH,
            share_step=Decimal("0.000000001"),
        )
        blob = caps_blob(caps, ACCOUNT)
        self.store.create(caps_key(ACCOUNT), blob, sha256_of(blob))

    def worker(self, component: str) -> entry.Worker:
        # Data reads one decision at a time until #215 fixes the mirror's race between
        # decisions read side by side, which halts one now and then on Linux.
        pool = {"pool_size": 1} if component == "data" else {}
        return entry.Worker(
            mode=Mode.BACKTEST,
            deployment=DEPLOYMENT,
            account=ACCOUNT,
            run=RUN,
            store=self.store,
            storage=self.storage,
            queue=PipeQueue(self.pipes, component, wait_seconds=0.2),
            bucket="folder",
            table=self.table,
            timer=SystemTimer(),
            alerts=self.alarms,
            beat=lambda: None,
            stop=threading.Event(),
            pause=lambda s: None,
            stress_moves=self.records,
            plan_every=0.05,
            # The supervisor's own release file and boot, so the real writer and reader meet.
            release_file=self.root / "local" / "release",
            boot="boot-ci",
            **pool,
        )

    def runs(self) -> dict[str, Callable[[], int]]:
        clock = HistoryClock(readings=None)
        buckets = DataBuckets(
            FolderBucket(self.lake), FolderBucket(self.lake.with_name("lake-live"))
        )
        return {
            "data": lambda: entry.run_data(
                self.worker("data"),
                clock=clock,
                measured=False,
                lake=buckets,
                mirror=self.root / "mirror",
                servers=None,
                quote_kinds=KINDS,
            ),
            "alpha": lambda: entry.run_alpha(self.worker("alpha")),
            "cost": lambda: entry.run_cost(self.worker("cost")),
            "risk": lambda: entry.run_risk(self.worker("risk")),
            "portfolio": lambda: entry.run_portfolio(self.worker("portfolio")),
            "execution": lambda: entry.run_execution(
                self.worker("execution"), clock=HistoryClock(readings=None), servers=None
            ),
            "supervisor": lambda: (
                Supervisor(
                    mode=Mode.BACKTEST,
                    deployment=DEPLOYMENT,
                    account=ACCOUNT,
                    store=self.store,
                    storage=self.storage,
                    table=self.table,
                    schedules={NAME: (BeforeClose(20),)},
                    release=RELEASE,
                    boot="boot-ci",
                    timer=SystemTimer(),
                    alerts=self.alarms,
                    release_file=self.root / "local" / "release",
                    wait=self.stop.wait,
                    tick=0.05,
                    sessions=SESSIONS,
                    span=(DAYS[0], DAYS[-1]),
                )
                .run(DAYS[0])
                .code
            ),
        }

    def start(self, order: tuple[str, ...]) -> dict[str, threading.Thread]:
        made_runs = self.runs()

        def run(name: str) -> None:
            try:
                self.ended[name] = made_runs[name]()
            except BaseException as error:  # noqa: BLE001 - the test reads it
                self.errors[name] = error

        threads = {}
        release = self.root / "local" / "release"
        for name in order:
            threads[name] = threading.Thread(target=run, args=(name,), daemon=True, name=name)
            threads[name].start()
            if name == "supervisor" and name == order[0]:
                # As the host's path unit does: the workers start once the release file is written.
                end = time.monotonic() + 60
                while not release.exists():
                    assert time.monotonic() < end, "the supervisor wrote no release file"
                    assert not self.errors, self.errors
                    time.sleep(0.02)
        return threads

    def result(self, stage: str, at) -> DecisionResult:
        data = (self.root / "store" / result_key(DEPLOYMENT, RUN, at, stage)).read_bytes()
        return decode(data, DecisionResult)

    def blobs(self) -> dict[str, bytes]:
        """Every result and output the stages stored, by key.

        The send stage's claim names its process's writer, and the run's stop
        and the start records the wall time they were written at, so they are
        left out.
        """
        found = {}
        base = self.root / "store" / "runs" / DEPLOYMENT / RUN
        for path in sorted(base.rglob("*.json")):
            key = path.relative_to(base).as_posix()
            if key.startswith("starts/") or key.endswith("/claim.json") or key == "stop.json":
                continue
            found[key] = path.read_bytes()
        return found


def _ran(run: Run, order: tuple[str, ...]) -> None:
    threads = run.start(order)
    # One deadline for them all, so a run that stalls fails in minutes rather than one wait each.
    end = time.monotonic() + 240
    for thread in threads.values():
        thread.join(max(0.0, end - time.monotonic()))
    alive = [name for name, thread in threads.items() if thread.is_alive()]
    # Each ended on its own, on the run's stop, before anything stopped it.
    assert alive == [], (alive, run.errors)
    run.stop.set()
    assert run.errors == {}
    assert run.ended == dict.fromkeys(order, EXIT_STOPPED)


@pytest.fixture(scope="module")
def in_order(lake: Path, tmp_path_factory: pytest.TempPathFactory) -> Run:
    run = Run(tmp_path_factory.mktemp("in-order"), lake)
    _ran(run, ("supervisor", *START_ORDER))
    return run


def test_every_entry_point_stores_a_result_that_is_no_halt_and_the_journal_holds_a_fill(
    in_order: Run,
) -> None:
    for at in AT:
        for stage in (s.name for s in in_order.table.stages):
            result = in_order.result(stage, at)
            assert result.halt is None, (stage, at, result.halt.reason)
    fills = [
        record_of(entry)
        for entry in Journal(in_order.store, ACCOUNT).read()
        if isinstance(record_of(entry), FillEntry)
    ]
    assert fills, "the first decision's orders fill at the second decision's recover"
    assert in_order.alarms.reasons == []
    assert (in_order.root / "local" / "release").read_text() == f"{RELEASE}\n{RUN}\nboot-ci\n"


def test_the_workers_started_before_the_supervisor_and_in_reverse_store_the_same_results(
    in_order: Run, lake: Path, tmp_path: Path
) -> None:
    reverse = Run(tmp_path / "reverse", lake)
    _ran(reverse, (*reversed(START_ORDER), "supervisor"))
    for at in AT:
        for stage in (s.name for s in reverse.table.stages):
            result = reverse.result(stage, at)
            if result.halt is not None:
                pytest.fail(
                    f"{stage} at {at:%Y-%m-%dT%H:%M}Z halted: {result.halt.reason}\n"
                    + "\n".join(reverse.alarms.reasons)
                )
    assert reverse.blobs().keys() == in_order.blobs().keys()
    assert len(in_order.blobs()) > 3 * len(in_order.table.stages)
    differ = [k for k, v in reverse.blobs().items() if in_order.blobs()[k] != v]
    assert differ == []


def test_every_worker_has_an_entry_point_and_a_command() -> None:
    assert set(START_ORDER) == set(WORKERS)
    for worker in WORKERS:
        assert callable(getattr(entry, f"run_{worker}"))
        assert callable(getattr(entry, f"{worker}_main"))
