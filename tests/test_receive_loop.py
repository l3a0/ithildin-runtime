"""The receive loop: its pool, its order rule, a handler's failure and the heartbeat.

docs/design.md's `### How a component learns of a change` holds the rules,
and #81's acceptance names the first three tests below.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from ithildinruntime.core import DecisionResult, Halt, Retry, StoreUnavailable, result_key
from ithildinruntime.runtime.loop import Item, LoopFailed, ReceiveLoop
from tests.support.queues import (
    COMPONENTS,
    CONFIG,
    DEPLOYMENT,
    FIRST,
    RUN,
    TABLE,
    Alarms,
    Calls,
    Folder,
    SystemTimer,
    handlers,
    header,
    loop,
    plan,
    run_folder,
    running,
    storage,
)


def test_pool_sizes_one_and_four_write_the_same_bytes(tmp_path) -> None:
    one, calls_one = run_folder(tmp_path / "one", decisions=8, pool_size=1)
    four, calls_four = run_folder(tmp_path / "four", decisions=8, pool_size=4, seed=11)
    assert one == four
    assert len(one) == 8 * len(TABLE.stages) * 2
    for calls in (calls_one, calls_four):
        assert set(calls.counts.values()) == {1}
        books = [item.decision_at for item in calls.order if item.stage == "book"]
        assert books == [d.at for d in plan(8).decisions]
    assert set(COMPONENTS) == {s.component for s in TABLE.stages}


# A handler's failure stays in its item.


def _alpha_loop(tmp_path, alpha, alarms, store_wrapper=None, pool_size=2, decisions=2):
    folder = Folder(tmp_path / "store", tmp_path / "queues")
    the_plan = plan(decisions)
    base = folder.store()
    store = storage(store_wrapper(base) if store_wrapper else base, alarms)
    pipe = folder.queue("alpha")
    made = loop(
        "alpha",
        the_plan=the_plan,
        queue=pipe,
        store=store,
        bucket=folder.bucket,
        alarms=alarms,
        handlers_by_stage={"alpha": alpha},
        pool_size=pool_size,
    )
    return folder, the_plan, store, made, pipe


def _feed(store, the_plan) -> None:
    feed = handlers(store, the_plan, Calls())["feed"]
    for decision in the_plan.decisions:
        item = Item(decision.at, "feed")
        store.put_result(result_key(DEPLOYMENT, RUN, decision.at, "feed"), feed(item, lambda: None))


def test_a_handler_that_raises_halts_its_decision_while_another_finishes_its_own(
    tmp_path,
) -> None:
    alarms = Alarms()
    halted = threading.Event()
    holder: dict = {}

    def alpha(item: Item, report):
        if item.decision_at == FIRST + timedelta(minutes=1):
            raise ValueError("the stand-in model broke")
        # The first decision finishes only after the second has halted.
        assert halted.wait(10)
        return handlers(holder["store"], holder["plan"], Calls())["alpha"](item, report)

    folder, the_plan, store, made, pipe = _alpha_loop(tmp_path, alpha, alarms)
    holder.update(store=store, plan=the_plan)
    second = result_key(DEPLOYMENT, RUN, FIRST + timedelta(minutes=1), "alpha")
    first = result_key(DEPLOYMENT, RUN, FIRST, "alpha")
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        assert started.wait_for(lambda: (folder.root / second).exists(), 10)
        halted.set()
        assert started.wait_for(lambda: (folder.root / first).exists(), 10)
    pipe.close()
    result = store.read_result(second, FIRST + timedelta(minutes=1))
    assert result.halt is not None and result.outputs == ()
    reason = result.halt.reason
    for part in ("alpha-1", "stage alpha", "20261102T150100Z", "ValueError", "model broke"):
        assert part in reason
    assert alarms.reasons == [reason]
    assert store.read_result(first, FIRST).halt is None


def test_a_handler_that_raises_halt_stores_it_without_an_alarm(tmp_path) -> None:
    alarms = Alarms()

    def alpha(item: Item, report):
        raise Halt("feed missing for the stand-in session")

    folder, the_plan, store, made, pipe = _alpha_loop(tmp_path, alpha, alarms, decisions=1)
    key = result_key(DEPLOYMENT, RUN, FIRST, "alpha")
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        assert started.wait_for(lambda: (folder.root / key).exists(), 10)
    pipe.close()
    assert store.read_result(key, FIRST).halt.reason == "feed missing for the stand-in session"
    assert alarms.reasons == []


def test_a_result_for_another_item_is_stored_as_a_halt(tmp_path) -> None:
    alarms = Alarms()
    holder: dict = {}

    def alpha(item: Item, report):
        result = handlers(holder["store"], holder["plan"], Calls())["alpha"](item, report)
        return DecisionResult(header("risk", item.decision_at, {}), result.outputs, None)

    folder, the_plan, store, made, pipe = _alpha_loop(tmp_path, alpha, alarms, decisions=1)
    holder.update(store=store, plan=the_plan)
    key = result_key(DEPLOYMENT, RUN, FIRST, "alpha")
    with running({"alpha": made}) as started:
        _feed(store, the_plan)
        assert started.wait_for(lambda: (folder.root / key).exists(), 10)
    pipe.close()
    assert "result of stage risk" in store.read_result(key, FIRST).halt.reason


class RefusingResults:
    """A store that takes every write but an alpha result."""

    def __init__(self, store) -> None:
        self._store = store

    def create(self, key, data, sha256):
        if key.endswith("/alpha/result.json"):
            raise StoreUnavailable(key, "503 SlowDown")
        return self._store.create(key, data, sha256)

    def __getattr__(self, name):
        return getattr(self._store, name)


def test_a_halt_that_cannot_be_stored_ends_the_loop(tmp_path) -> None:
    alarms = Alarms()

    def alpha(item: Item, report):
        raise ValueError("broken")

    folder, the_plan, store, made, pipe = _alpha_loop(
        tmp_path, alpha, alarms, store_wrapper=RefusingResults, decisions=1
    )
    made._storage._retry = Retry(timedelta(seconds=0.2), 0.05, 0.05)
    _feed(storage(folder.store(), alarms), the_plan)
    failures: list[BaseException] = []

    def run() -> None:
        try:
            made.run()
        except BaseException as error:  # noqa: BLE001 - the test reads it
            failures.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(20)
    pipe.close()
    assert not thread.is_alive()
    assert len(failures) == 1 and isinstance(failures[0], LoopFailed)
    assert "alpha/result.json" in str(failures[0])


def _loop_settings(tmp_path) -> dict:
    folder = Folder(tmp_path / "s", tmp_path / "p")
    return dict(
        worker="alpha-1",
        component="alpha",
        table=TABLE,
        plan=plan(1),
        queue=None,
        storage=storage(folder.store(), Alarms()),
        bucket="folder",
        handlers={"alpha": lambda item, report: None},
        alerts=Alarms(),
        timer=SystemTimer(),
        beat=lambda: None,
        config_hash=CONFIG,
    )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"component": "nobody"}, "runs no stage"),
        ({"pool_size": 0}, "at least one handler"),
        ({"handlers": {}}, "a handler for each"),
    ],
)
def test_a_loop_refuses_settings_it_cannot_run(tmp_path, change, message) -> None:
    with pytest.raises(ValueError, match=message):
        ReceiveLoop(**{**_loop_settings(tmp_path), **change})
