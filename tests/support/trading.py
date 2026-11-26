"""The replication traded through every stage, the order path and the simulated broker, once.

Test 1 of #4 runs here. Each of the 1,461 decisions of
`tests/support/replication.py` has its snapshot cut and stored as data's result,
and then the cost, alpha, recover, risk, portfolio and send handlers run in that
order over one folder store, the history clock and the simulated broker. Each
handler's result is stored as the receive loop stores it. One more recover runs
at the close−20 of 2012-04-10, the session after the last, over a snapshot whose
newest bar is 2012-04-09's, so the last decision's orders fill.

Every figure is exploratory. A fill at the close its signal read is a price no
live order reaches. It is the replication's own assumption, named in each fill's
`basis`, so no number here is evidence of an edge.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ithildinruntime.adapters import FolderStore, HistoryClock
from ithildinruntime.core import (
    BOOK_OUTPUT,
    DATA_STAGE,
    PORTFOLIO_STAGE,
    RECOVER_STAGE,
    RISK_STAGE,
    SEND_STAGE,
    SIZING_OUTPUT,
    STRATEGIES,
    TARGET_OUTPUT,
    BeforeClose,
    BlobHeader,
    Book,
    DecisionResult,
    HaltRecord,
    HardCaps,
    Mode,
    OutputFile,
    Retry,
    SendOutcome,
    Sizing,
    Snapshot,
    StageKind,
    Storage,
    TargetPortfolio,
    caps_key,
    decode_output,
    encode_output,
    load_sessions,
    output_key,
    parse_strategy,
    result_key,
    sha256_of,
    to_utc,
)
from ithildinruntime.execution.brokers.faults import Faults
from ithildinruntime.portfolio import gross
from ithildinruntime.runtime.alpha_worker import alpha_handlers, alpha_rows
from ithildinruntime.runtime.cost_worker import cost_handlers, cost_rows
from ithildinruntime.runtime.data_worker import DATA_ROW
from ithildinruntime.runtime.execution_worker import (
    RECOVER_ROW,
    SEND_ROW,
    ExecutionWorker,
    open_backtest,
)
from ithildinruntime.runtime.loop import Item, Table
from ithildinruntime.runtime.portfolio_worker import portfolio_handler, portfolio_row
from ithildinruntime.runtime.risk_worker import RISK_ROW, risk_handler
from tests.support import gates
from tests.support.order_world import caps_blob
from tests.support.replication import FIRST_DECISION, GLD, LOOKBACK, cut
from tests.test_data_source import Timer
from tests.test_storage_contract import Alarms

ACCOUNT, DEPLOYMENT, RELEASE = "bt-chan", "ci", "sha256:release"
RUN = "2006-06-21_2012-04-09"
CASH = Decimal(1_000_000)
STEP = Decimal("0.000000001")
SESSIONS = load_sessions()
LOOSE = gates.LOOSE
# The stress move read from 2006 to 2026, which binds nothing at this G.
MOVE = gates.d("0.347")
# The day after the last decision, whose recover settles that decision's orders.
AFTER = date(2012, 4, 10)
NAME = "chan_bollinger"

# The tracked strategy file as every environment reads it, with its G and its limits.
STRATEGY = parse_strategy(NAME, (STRATEGIES / f"{NAME}.toml").read_bytes())
G = gross(STRATEGY)

# One made-up stress record each half year from 2006-06-20, since a record serves a decision at
# most 365 days after it. The owner rewrites the caps to the newest, and
# the backtest does the same before each decision that needs it.
THROUGHS = (date(2006, 6, 20), *(date(y, m, 2) for y in range(2007, 2013) for m in (1, 7)))


def vintage(through: date) -> str:
    return hashlib.sha256(through.isoformat().encode()).hexdigest()


def _series(leg: str, day: str) -> str:
    return f"""[[record.series]]
instrument = "{leg}"
first = 2006-04-26
last = {day}
basis = "adjusted"
source = "made-up"
source_as_of = 2026-11-22"""


def _record(through: date) -> str:
    day = through.isoformat()
    return f"""
[[record]]
strategy = "{NAME}"
instruments = ["GLD", "USO"]
vintage = "{vintage(through)}"
through = {day}
stress_move = "0.34700000"
leg = "USO"
moved_from = 2006-05-01
moved_to = 2006-05-02
one_session = "0.01000000"
one_session_leg = "GLD"
one_session_on = 2006-05-10
left_out = []
flagged = []
manifest = "{"b" * 64}"
measured_on = 2026-11-24
{_series("GLD", day)}
{_series("USO", day)}
"""


RECORDS = "".join(_record(through) for through in THROUGHS)


def caps_on(day: date) -> HardCaps:
    """The caps written by `day`'s decision: limits that never bind, and the newest record."""
    through = max(t for t in THROUGHS if t < day)
    return gates.caps(
        share=gates.d("0.99"),
        stress_move=MOVE,
        stress_low=gates.d("0.1"),
        stress_high=gates.d("0.6"),
        stress_vintage=vintage(through),
        stress_through=through,
        stress_max_age_days=365,
        share_step=STEP,
    )


TABLE = Table(
    stages=(
        DATA_ROW,
        *cost_rows([STRATEGY]),
        *alpha_rows([STRATEGY]),
        RECOVER_ROW,
        RISK_ROW,
        portfolio_row([STRATEGY]),
        SEND_ROW,
    ),
    strategies=(STRATEGY,),
)


@dataclass
class Decided:
    """One decision as test 1 reads it back."""

    day: date
    at: datetime
    target: TargetPortfolio
    sizing: Sizing
    outcome: SendOutcome


@dataclass
class Traded:
    """The whole run: each decision, the store it left, and the books recover stored."""

    decided: list[Decided]
    store: FolderStore
    storage: Storage
    # The book each recover stored, by its decision's session, the planted one's included.
    books: dict[date, Book]


def storage_of(store: FolderStore) -> Storage:
    return Storage(
        store,
        timer=Timer(),
        pause=lambda s: None,
        retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
        alerts=Alarms(),
        owner_step="none",
    )


@dataclass
class SetUp:
    """One run's store and account, opened, and every handler but execution's, in order."""

    store: FolderStore
    storage: Storage
    records: Path
    days: list[date]
    first: int
    handlers: dict[str, Callable]
    order: list[str]


def set_up(every_session: Snapshot, root: Path, store: FolderStore | None = None) -> SetUp:
    """Open test 1's account in a folder store, and build every handler but execution's."""
    store = FolderStore(root / "store") if store is None else store
    storage = storage_of(store)
    records = root / "stress_moves.toml"
    records.write_text(RECORDS)
    days = [bar.session for bar in every_session.bars[GLD]["1d"]]
    first = days.index(FIRST_DECISION)
    opened = SESSIONS.session(days[first - 1]).close
    open_backtest(Mode.BACKTEST, store, ACCOUNT, CASH, opened)
    made = dict(storage=storage, deployment=DEPLOYMENT, run=RUN, release=RELEASE, table=TABLE)
    models: dict[str, Callable] = {**cost_handlers(**made), **alpha_handlers(**made)}
    handlers = {
        **models,
        RISK_STAGE: risk_handler(**made, stress_moves=records),
        PORTFOLIO_STAGE: portfolio_handler(**made, stress_moves=records),
    }
    return SetUp(store, storage, records, days, first, handlers, stage_order(models))


def execution_worker(made: SetUp, alerts: Alarms, faults: Faults | None = None) -> ExecutionWorker:
    """An execution process over the run's store, as test 1 builds it, with `faults` if any."""
    return ExecutionWorker(
        mode=Mode.BACKTEST,
        store=made.store,
        storage=made.storage,
        account=ACCOUNT,
        clock=HistoryClock(readings=None),
        alerts=alerts,
        retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
        pause=lambda s: None,
        deployment=DEPLOYMENT,
        run=RUN,
        release=RELEASE,
        table=TABLE,
        stress_moves=made.records,
        faults=faults,
    )


def trade(
    every_session: Snapshot, root: Path, *, stop: Callable[[int], bool] = lambda n: False
) -> Traded:
    """Run test 1's decisions in order, the planted recover last, and read back what each left.

    `stop` ends the run before the decision it names by its index, for a test
    that needs only the first few.
    """
    made = set_up(every_session, root)
    store, storage, days, first = made.store, made.storage, made.days, made.first
    written: list[HardCaps] = []
    worker = execution_worker(made, Alarms())
    order = made.order
    handlers = {**made.handlers, RECOVER_STAGE: worker.recover, SEND_STAGE: worker.send}
    decided: list[Decided] = []
    books: dict[date, Book] = {}
    for n in range(first, len(days)):
        if stop(n - first):
            break
        held = cut(every_session, n, LOOKBACK)
        _caps(store, caps_on(days[n]), written)
        _data(storage, held)
        for name in order:
            result = _run(storage, handlers[name], name, held.decision_at)
            assert result.halt is None, (days[n], name, result.halt.reason)
        decided.append(_read(storage, days[n], held.decision_at))
        books[days[n]] = _book(storage, held.decision_at)
    else:
        # The planted recover, the first of its session, which settles the last orders.
        at = to_utc(AFTER, BeforeClose(20))
        held = replace(cut(every_session, len(days) - 1, LOOKBACK), decision_at=at)
        _data(storage, held)
        result = _run(storage, worker.recover, RECOVER_STAGE, at)
        assert result.halt is None, result.halt.reason
        books[AFTER] = _book(storage, at)
    return Traded(decided, store, storage, books)


def _caps(store: FolderStore, caps: HardCaps, written: list[HardCaps]) -> None:
    """Write the caps blob where it is not these caps, as the owner rewrites it by hand."""
    if written and written[-1] == caps:
        return
    blob, key = caps_blob(caps, ACCOUNT), caps_key(ACCOUNT)
    if written:
        store.replace_if_match(key, blob, sha256_of(blob), store.head(key).etag)
    else:
        store.create(key, blob, sha256_of(blob))
    written.append(caps)


def stage_order(models: Mapping[str, Callable]) -> list[str]:
    """Every stage after data in the order test 1 runs them: each model's, then execution's."""
    return [*models, RECOVER_STAGE, RISK_STAGE, PORTFOLIO_STAGE, SEND_STAGE]


def _data(storage: Storage, held: Snapshot, halt: HaltRecord | None = None) -> None:
    """Store data's snapshot and result, as the data stage would for this decision.

    `halt` is the result's halt, where data's own judgement of `held` found one.
    """
    at = held.decision_at
    header = BlobHeader(
        1, DEPLOYMENT, RUN, DATA_STAGE, "capturing", at, {}, RELEASE, TABLE.hashes_for(DATA_STAGE)
    )
    file = encode_output(Snapshot, OutputFile(header, (held,), False))
    key = output_key(DEPLOYMENT, RUN, at, DATA_STAGE, "snapshot")
    ref = storage.put(StageKind.CAPTURING, key, file).ref
    storage.put_result(
        result_key(DEPLOYMENT, RUN, at, DATA_STAGE), DecisionResult(header, (ref,), halt)
    )


def _run(storage: Storage, handler: Callable, name: str, at: datetime) -> DecisionResult:
    result = handler(Item(at, name), _Report())
    return storage.put_result(result_key(DEPLOYMENT, RUN, at, name), result)


def _read(storage: Storage, day: date, at: datetime) -> Decided:
    def one(stage: str, name: str, kind: type):
        key = result_key(DEPLOYMENT, RUN, at, stage)
        result = storage.read_result(key, at)
        assert result is not None
        listed = output_key(DEPLOYMENT, RUN, at, stage, name)
        (ref,) = [r for r in result.outputs if r.key == listed]
        (entry,) = storage.read_output(key, result, ref, at, kind).entries
        return entry

    return Decided(
        day,
        at,
        one(PORTFOLIO_STAGE, TARGET_OUTPUT, TargetPortfolio),
        one(PORTFOLIO_STAGE, SIZING_OUTPUT, Sizing),
        one(SEND_STAGE, "outcome", SendOutcome),
    )


def _book(storage: Storage, at: datetime) -> Book:
    key = output_key(DEPLOYMENT, RUN, at, RECOVER_STAGE, BOOK_OUTPUT)
    (book,) = decode_output(storage.read_blob(key), Book).entries
    return book


class _Report:
    """The report a handler is handed, which no stop ends."""

    def __call__(self) -> None:
        pass

    def wait_for_stop(self, timeout: float) -> bool:
        raise AssertionError("the history clock waits for nothing")


__all__ = [
    "CASH",
    "STRATEGY",
    "TABLE",
    "Decided",
    "SetUp",
    "Traded",
    "execution_worker",
    "set_up",
    "stage_order",
    "trade",
]
