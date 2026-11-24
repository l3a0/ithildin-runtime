"""The alpha stage over the vintages the replication reads, for #6's acceptance 1 and 2.

Chan's Bollinger band on GLD and USO is pinned in quantitative-trading at
`19aadbeb`, over 1,480 rows from 2006-05-24 to 2012-04-09. Its first 19 rows
hold nothing, and the run here decides at the other 1,461, from 2006-06-21, at
each session's close. Every figure over these vintages is exploratory, so this
checks the engine against a known answer and shows no edge.

The lake is read once, through `LakeSource`, for every session, and each
decision's snapshot is cut from that read. The cut is the data module's rule,
and its own tests hold it.
"""

from __future__ import annotations

import hashlib
import statistics
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderBucket, FolderStore
from ithildinruntime.alpha import build_model
from ithildinruntime.core import (
    STRATEGIES,
    BlobHeader,
    DecisionResult,
    Forecast,
    Halt,
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
from tests.support.chan_return import chan_figures
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
# The SHA-256 of `chan.bollinger`'s own units, run at 19aadbeb, one line a decision.
UNITS_SHA256 = "5c7e77fd9f7632a0ca1f6cd0e10246c19321735f4747eecac799d459b646bca3"


@pytest.fixture(scope="module")
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
        ),
        clock=Clock(),
        timer=Timer(),
    )
    return source.snapshot(sessions[-1].close)


def cut(whole: Snapshot, n: int, lookback: int) -> Snapshot:
    """What a decision at session `n`'s close reads."""
    window = slice(max(0, n - lookback + 1), n + 1)
    bars = {i: {"1d": by["1d"][window]} for i, by in whole.bars.items()}
    return replace(whole, decision_at=bars[GLD]["1d"][-1].end, bars=bars)


type Decided = tuple[date, Forecast, tuple[Signal, ...]]


@pytest.fixture(scope="module")
def run(every_session: Snapshot, tmp_path_factory: pytest.TempPathFactory) -> list[Decided]:
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


def held_spells(units: Sequence[int]) -> tuple[list[int], int]:
    """Each position's sessions, counted flat to flat, and the reversals inside them."""
    spells, current, reversals, before = [], 0, 0, 0
    for unit in units:
        if unit:
            current += 1
            reversals += before == -unit
        elif current:
            spells.append(current)
            current = 0
        before = unit
    return (spells + [current] if current else spells), reversals


def test_the_units_are_the_replications_on_every_decision(every_session, run) -> None:
    assert len(every_session.bars[GLD]["1d"]) == len(every_session.bars[USO]["1d"]) == 1500
    assert len(run) == 1461 and (run[0][0], run[-1][0]) == (FIRST_DECISION, lakes.LAST_SESSION)
    units = [forecast.direction for _, forecast, _ in run]
    assert (units.count(-1), units.count(0), units.count(1)) == (547, 315, 599)
    lines = "".join(
        f"{day.isoformat()},{unit}\n" for (day, _, _), unit in zip(run, units, strict=True)
    )
    assert hashlib.sha256(lines.encode()).hexdigest() == UNITS_SHA256


def test_the_positions_the_units_make(run) -> None:
    units = [forecast.direction for _, forecast, _ in run]
    spells, reversals = held_spells(units)
    assert (len(spells), statistics.median(spells), max(spells)) == (77, 14, 40)
    assert units[-1] != 0 and reversals == 9
    # The 162 changes of unit the replication pins: 77 entries, 76 exits and 9 reversals.
    changes = sum(a != b for a, b in zip([0, *units], units, strict=False))
    assert changes == 162


def test_the_longest_walk_stays_inside_the_lookback(run) -> None:
    days = SESSIONS.between(lakes.FIRST_SESSION, lakes.LAST_SESSION)
    place = {session.day: n for n, session in enumerate(days)}
    reads = [place[unit.last_session] - place[unit.first_session] + 1 for _, _, (*_, unit) in run]
    # 17 z-scores, the first of which reads 39 sessions and each other one more.
    assert max(reads) == 55
    longest = [day for (day, _, _), n in zip(run, reads, strict=True) if n == 55]
    assert longest == [date(2010, 3, 3), date(2012, 3, 27)]


def test_a_lookback_of_40_halts_where_it_cannot_settle_and_is_never_wrong(
    every_session, run
) -> None:
    model = build_model(CHAN)
    days = [bar.session for bar in every_session.bars[GLD]["1d"]]
    halted = wrong = 0
    for day, forecast, _ in run:
        try:
            made = model.forecast(cut(every_session, days.index(day), 40))
        except Halt as halt:
            assert "cannot tell which unit it holds" in halt.reason
            halted += 1
        else:
            wrong += made.forecasts != (forecast,)
    assert (halted, wrong) == (513, 0)


def test_chans_return_on_the_stored_forecasts_is_the_replications(every_session, run) -> None:
    closes = {
        i: {bar.session: bar.close for bar in by["1d"]} for i, by in every_session.bars.items()
    }
    days = sorted(closes[GLD])
    first = days.index(FIRST_DECISION)
    rows = [(day, {}) for day in days[first - ROWS_BEFORE : first]]
    rows += [
        (day, {i: forecast.direction * leg for i, leg in forecast.basket.legs.items()})
        for day, forecast, _ in run
    ]
    assert len(rows) == 1480 and rows[0][0] == date(2006, 5, 24)
    apr, sharpe = chan_figures(rows, closes)
    assert apr == pytest.approx(0.17824928, abs=5e-9, rel=0)
    assert sharpe == pytest.approx(0.96467289, abs=5e-9, rel=0)
