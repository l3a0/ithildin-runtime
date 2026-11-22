"""One hash per strategy, through the receive loop and the store.

#102's acceptance names these tests. One worker runs a stage of each of two
strategies and a stage they share, so each test can tell a strategy's own
hash from the shared map.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ithildinruntime.core import (
    BlobHeader,
    DecisionResult,
    Halt,
    Mode,
    OutputFile,
    Plan,
    PlanDecision,
    StageKind,
    encode_output,
    output_key,
    parse_strategy,
    result_key,
)
from ithildinruntime.runtime.loop import Item, ReceiveLoop, Stage, Table
from tests.support.queues import Alarms, Folder, SystemTimer, Value, running, storage

D = StageKind.DETERMINISTIC
DEPLOYMENT, RUN, RELEASE = "ci", "2026-11-02", "sha256:release"
FIRST = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
CHAN = parse_strategy("chan", b'instruments = ["GLD", "USO"]\n')
OTHER = parse_strategy("other", b'instruments = ["SPY", "QQQ"]\n')
STAGES = ("alpha", "alpha/chan", "alpha/other")
TABLE = Table(
    stages=(
        Stage("alpha", D),
        Stage("alpha/chan", D, strategy="chan"),
        Stage("alpha/other", D, strategy="other"),
    ),
    strategies=(CHAN, OTHER),
)
BOTH = {"chan": CHAN.config_hash, "other": OTHER.config_hash}
OWN = {
    "alpha": BOTH,
    "alpha/chan": {"chan": CHAN.config_hash},
    "alpha/other": {"other": OTHER.config_hash},
}


def _plan(*deciding: tuple[str, ...]) -> Plan:
    return Plan(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        mode=Mode.PAPER,
        release=RELEASE,
        decisions=tuple(
            PlanDecision(FIRST + timedelta(minutes=n), strategies)
            for n, strategies in enumerate(deciding)
        ),
    )


def _header(stage: str, at: datetime, hashes) -> BlobHeader:
    return BlobHeader(
        format=1,
        deployment=DEPLOYMENT,
        run=RUN,
        stage=stage,
        kind="deterministic",
        decision_at=at,
        inputs={},
        release=RELEASE,
        config_hashes=hashes,
    )


def _write(store, item: Item, hashes, output_hashes=None) -> DecisionResult:
    key = output_key(DEPLOYMENT, RUN, item.decision_at, item.stage, "value")
    head = _header(item.stage, item.decision_at, output_hashes or hashes)
    stored = store.put(D, key, encode_output(Value, OutputFile(head, (Value(1),), False)))
    return DecisionResult(_header(item.stage, item.decision_at, hashes), (stored.ref,), None)


def _run(tmp_path, the_plan: Plan, make_handler):
    """Run the one worker over every decision, and return each stored result."""
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    alarms = Alarms()
    store = storage(folder.store(), alarms)
    returned: list[DecisionResult] = []

    def handler(stage: str):
        inner = make_handler(stage, store)

        def call(item: Item, report) -> DecisionResult:
            result = inner(item)
            returned.append(result)
            return result

        return call

    made = ReceiveLoop(
        worker="alpha-1",
        component="alpha",
        table=TABLE,
        plan=the_plan,
        queue=folder.queue("alpha"),
        storage=store,
        bucket=folder.bucket,
        handlers={stage: handler(stage) for stage in STAGES},
        alerts=alarms,
        timer=SystemTimer(),
        beat=lambda: None,
        tick=0.05,
        stop_timeout=5.0,
    )
    keys = [result_key(DEPLOYMENT, RUN, d.at, s) for d in the_plan.decisions for s in STAGES]
    with running({"alpha": made}) as started:
        assert started.wait_for(lambda: all((folder.root / k).exists() for k in keys), 20)
    results = {
        (d.at, s): store.read_result(result_key(DEPLOYMENT, RUN, d.at, s), d.at)
        for d in the_plan.decisions
        for s in STAGES
    }
    return results, returned, alarms, store


def test_each_strategys_stage_records_its_own_hash_and_a_shared_stage_records_both(
    tmp_path,
) -> None:
    def make(stage, store):
        return lambda item: _write(store, item, TABLE.hashes_for(stage))

    results, returned, alarms, _ = _run(tmp_path, _plan(("chan", "other")), make)
    assert len(returned) == 3 and all(r.halt is None for r in returned)
    for (_, stage), result in results.items():
        assert result.halt is None
        assert dict(result.header.config_hashes) == OWN[stage]
    assert alarms.reasons == []


def test_each_halt_the_loop_writes_records_its_stages_hashes(tmp_path) -> None:
    def make(stage, store):
        def raise_halt(item):
            raise Halt(f"{stage} has nothing to read")

        return raise_halt

    results, _, _, _ = _run(tmp_path, _plan(("chan", "other")), make)
    for (_, stage), result in results.items():
        assert result.halt is not None and result.halt.reason == f"{stage} has nothing to read"
        assert dict(result.header.config_hashes) == OWN[stage]


WRONG = "e" * 64
WAYS = {
    "one-wrong-value": ("alpha", {"chan": CHAN.config_hash, "other": WRONG}),
    "another-strategys-map": ("alpha/chan", {"other": OTHER.config_hash}),
    "a-strategys-stage-with-the-shared-map": ("alpha/chan", BOTH),
    "a-shared-stage-missing-one": ("alpha", {"chan": CHAN.config_hash}),
}


@pytest.mark.parametrize("way", WAYS)
def test_a_result_with_the_wrong_hashes_is_stored_as_a_halt_with_an_alarm(tmp_path, way) -> None:
    wrong_stage, wrong = WAYS[way]

    def make(stage, store):
        hashes = wrong if stage == wrong_stage else TABLE.hashes_for(stage)
        return lambda item: _write(store, item, hashes)

    results, returned, alarms, _ = _run(tmp_path, _plan(("chan", "other")), make)
    assert all(r.halt is None for r in returned)
    halted = {stage for (_, stage), r in results.items() if r.halt is not None}
    assert halted == {wrong_stage}
    (result,) = [r for (_, s), r in results.items() if s == wrong_stage]
    assert "configuration hashes" in result.halt.reason
    assert dict(result.header.config_hashes) == OWN[wrong_stage]
    assert alarms.reasons == [result.halt.reason]


@pytest.mark.parametrize("way", WAYS)
def test_reading_an_output_whose_hashes_differ_from_its_results_halts(tmp_path, way) -> None:
    stage, wrong = WAYS[way]
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    store = storage(folder.store(announce=False), Alarms())
    item = Item(FIRST, stage)
    result = _write(store, item, TABLE.hashes_for(stage), output_hashes=wrong)
    key = result_key(DEPLOYMENT, RUN, FIRST, stage)
    with pytest.raises(Halt) as caught:
        store.read_output(key, result, result.outputs[0], FIRST, Value)
    reason = caught.value.reason
    assert "names the configuration hashes" in reason
    for digest in (*wrong.values(), *TABLE.hashes_for(stage).values()):
        assert digest in reason


def test_reading_an_output_whose_hashes_match_its_results_passes(tmp_path) -> None:
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    store = storage(folder.store(announce=False), Alarms())
    result = _write(store, Item(FIRST, "alpha"), dict(reversed(list(BOTH.items()))))
    key = result_key(DEPLOYMENT, RUN, FIRST, "alpha")
    assert store.read_output(key, result, result.outputs[0], FIRST, Value).entries == (Value(1),)


def test_a_decision_whose_strategies_differ_from_the_tables_halts_every_stage(tmp_path) -> None:
    called: list[Item] = []

    def make(stage, store):
        def write(item):
            called.append(item)
            return _write(store, item, TABLE.hashes_for(stage))

        return write

    the_plan = _plan(("chan", "other"), ("chan",), ("chan", "other", "stranger"))
    results, _, alarms, _ = _run(tmp_path, the_plan, make)
    assert {item.decision_at for item in called} == {FIRST}
    for stage in STAGES:
        assert results[(FIRST, stage)].halt is None
        missing = results[(FIRST + timedelta(minutes=1), stage)].halt.reason
        assert "20261102T150100Z" in missing and "it leaves out other" in missing
        assert "stranger" not in missing
        extra = results[(FIRST + timedelta(minutes=2), stage)].halt.reason
        assert "20261102T150200Z" in extra
        assert "it lists stranger, which the table does not declare" in extra
        assert "leaves out" not in extra
        assert (
            dict(results[(FIRST + timedelta(minutes=1), stage)].header.config_hashes)
            == (OWN[stage])
        )
    assert alarms.reasons == []


def test_a_loop_refuses_a_table_that_declares_no_strategy(tmp_path) -> None:
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    with pytest.raises(ValueError, match="declares no strategy"):
        ReceiveLoop(
            worker="alpha-1",
            component="alpha",
            table=Table(stages=(Stage("alpha", D),)),
            plan=_plan(("chan",)),
            queue=None,
            storage=storage(folder.store(), Alarms()),
            bucket="folder",
            handlers={"alpha": lambda item, report: None},
            alerts=Alarms(),
            timer=SystemTimer(),
            beat=lambda: None,
        )
