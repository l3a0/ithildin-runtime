"""The replication's 1,461 decisions, read and forecast once per session.

quantitative-trading pins Chan's Bollinger band on GLD and USO at `19aadbeb`,
over 1,480 rows from 2006-05-24 to 2012-04-09. Its first 19 rows hold nothing,
and the run here decides at the other 1,461, from 2006-06-21, at each session's
close. The setup takes over a minute, so every module that reads it shares the
two fixtures here through `tests/conftest.py`. Every figure over these vintages
is exploratory.

The lake is read once, through `LakeSource`, for every session, and each
decision's snapshot is cut from that read. The cut is the data module's rule,
and its own tests hold it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket, FolderStore
from ithildinruntime.core import (
    STRATEGIES,
    BlobHeader,
    DecisionResult,
    Forecast,
    InstrumentId,
    OutputFile,
    Retry,
    Signal,
    Snapshot,
    StageKind,
    Storage,
    encode_output,
    load_sessions,
    load_strategies,
    output_key,
    result_key,
)
from ithildinruntime.data import DataSettings, LakeSource, Mirror
from ithildinruntime.runtime.alpha_worker import alpha_handlers, alpha_rows
from ithildinruntime.runtime.data_worker import DATA_ROW
from ithildinruntime.runtime.loop import Item, Table
from tests.support import lake as lakes
from tests.test_data_source import Clock, Timer
from tests.test_storage_contract import Alarms

GLD, USO = InstrumentId("GLD"), InstrumentId("USO")
SESSIONS = load_sessions()
(CHAN,) = load_strategies(STRATEGIES, ["chan_bollinger"])
TABLE = Table(stages=(DATA_ROW, *alpha_rows([CHAN])), strategies=(CHAN,))
DEPLOYMENT, RUN, RELEASE = "ci", "2006-06-21_2012-04-09", "sha256:release"
STAGE = f"alpha/{CHAN.name}"
LOOKBACK = 60
FIRST_DECISION = date(2006, 6, 21)
# The replication's rows before the first decision, which hold nothing.
ROWS_BEFORE = 19


@pytest.fixture(scope="session")
def every_session(built_lake: Path, tmp_path_factory: pytest.TempPathFactory) -> Snapshot:
    """Both series whole, as data reads them at the last session's close."""
    sessions = SESSIONS.between(lakes.FIRST_SESSION, lakes.LAST_SESSION)
    source = LakeSource(
        Mirror(FolderBucket(built_lake), tmp_path_factory.mktemp("mirror")),
        DataSettings(
            instruments=(GLD, USO),
            lookback=len(sessions),
            first_sessions={GLD: lakes.FIRST_SESSION, USO: lakes.FIRST_SESSION},
            stale_lag=0,
            # Far past any move in either series, so no bar is flagged.
            spike_multiple=Decimal(100),
            quotes={},
        ),
        live=FolderBucket(built_lake.with_name("no-live")),
        clock=Clock(),
        timer=Timer(),
        measured=False,
    )
    return source.snapshot(sessions[-1].close)


def cut(whole: Snapshot, n: int, lookback: int) -> Snapshot:
    """What a decision at session `n`'s close reads."""
    window = slice(max(0, n - lookback + 1), n + 1)
    bars = {i: {"1d": by["1d"][window]} for i, by in whole.bars.items()}
    return replace(whole, decision_at=bars[GLD]["1d"][-1].end, bars=bars)


type Decided = tuple[date, Forecast, tuple[Signal, ...]]


@pytest.fixture(scope="session")
def replicated(every_session: Snapshot, tmp_path_factory: pytest.TempPathFactory) -> list[Decided]:
    """What the stage stored at each decision, read back as portfolio construction will."""
    storage = Storage(
        FolderStore(tmp_path_factory.mktemp("store")),
        timer=Timer(),
        pause=lambda s: None,
        retry=Retry(deadline=timedelta(seconds=1), pause=0.1, longest_pause=0.1),
        alerts=Alarms(),
        owner_step="none",
    )
    (stage,) = alpha_handlers(
        storage=storage, deployment=DEPLOYMENT, run=RUN, release=RELEASE, table=TABLE
    ).values()
    days = [bar.session for bar in every_session.bars[GLD]["1d"]]
    decided = []
    for n in range(days.index(FIRST_DECISION), len(days)):
        held = cut(every_session, n, LOOKBACK)
        at = held.decision_at
        header = BlobHeader(
            1, DEPLOYMENT, RUN, "data", "capturing", at, {}, RELEASE, TABLE.hashes_for("data")
        )
        file = encode_output(Snapshot, OutputFile(header, (held,), False))
        key = output_key(DEPLOYMENT, RUN, at, "data", "snapshot")
        ref = storage.put(StageKind.CAPTURING, key, file).ref
        storage.put_result(
            result_key(DEPLOYMENT, RUN, at, "data"), DecisionResult(header, (ref,), None)
        )
        result = stage(Item(at, STAGE), lambda: None)
        assert result.halt is None, (days[n], result.halt)
        own = result_key(DEPLOYMENT, RUN, at, STAGE)
        storage.put_result(own, result)
        read = storage.read_result(own, at)
        assert read is not None
        (forecast,) = storage.read_output(own, read, read.outputs[0], at, Forecast).entries
        signals = storage.read_output(own, read, read.outputs[1], at, Signal).entries
        decided.append((days[n], forecast, signals))
    return decided
