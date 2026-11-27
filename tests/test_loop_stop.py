"""The receive loop ends on its own run's stop, as #200's plan and receive.py's item 12 say.

The stand-in stages of tests/support/queues.py run on threads over a folder and
named pipes, with every component hearing the run's stop. Each test waits for
every loop's thread to end on its own, and never stops a loop itself before it
asserts, so a loop that ignored the stop would fail rather than be stopped.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

from ithildinruntime.adapters import FolderStore, PipeQueue
from ithildinruntime.core import EventKind, Halt, result_key, run_stop_key
from ithildinruntime.runtime.loop import Hears, ReceiveLoop, Table, routes
from tests.support import queues as q

# The stand-in table, with every component hearing the run's stop.
TABLE = Table(
    stages=q.TABLE.stages,
    hears=tuple(Hears(c, EventKind.CREATED, "runs/{deployment}/*/stop.json") for c in q.COMPONENTS),
    strategies=q.TABLE.strategies,
)
STOP = run_stop_key(q.DEPLOYMENT, q.RUN)


class World:
    def __init__(self, root: Path, *, announce: bool = True) -> None:
        self.root, self.pipes = root / "store", root / "queues"
        self.alarms = q.Alarms()
        made = {"routes": routes(TABLE, q.DEPLOYMENT), "pipes": self.pipes} if announce else {}
        self.store = FolderStore(self.root, bucket="folder", **made)
        self.storage = q.storage(self.store, self.alarms)
        self.calls = q.Calls()
        self.plan = q.plan(2)

    def loops(self, extend_every: float) -> dict[str, ReceiveLoop]:
        handlers = q.handlers(self.storage, self.plan, self.calls)
        return {
            component: ReceiveLoop(
                worker=f"{component}-1",
                component=component,
                table=TABLE,
                plan=self.plan,
                queue=PipeQueue(self.pipes, component, wait_seconds=0.2),
                storage=self.storage,
                bucket="folder",
                handlers={s.name: handlers[s.name] for s in TABLE.stages_of(component)},
                alerts=self.alarms,
                timer=q.SystemTimer(),
                beat=lambda: None,
                tick=0.05,
                stop_timeout=5.0,
                extend_every=extend_every,
                stop_key=STOP,
            )
            for component in q.COMPONENTS
        }

    def start(self, loops: dict[str, ReceiveLoop]) -> dict[str, threading.Thread]:
        threads = {
            name: threading.Thread(target=loop.run, daemon=True, name=name)
            for name, loop in loops.items()
        }
        for thread in threads.values():
            thread.start()
        for loop in loops.values():
            assert loop.wait_started(10)
        return threads

    def stored(self) -> bool:
        return all(
            (self.root / result_key(q.DEPLOYMENT, q.RUN, d.at, stage)).exists()
            for d in self.plan.decisions
            for stage in (s.name for s in TABLE.stages)
        )

    def write_stop(self, key: str = STOP) -> None:
        self.store.create(key, b"{}", hashlib.sha256(b"{}").hexdigest())


def _ended(threads: dict[str, threading.Thread], within: float) -> list[str]:
    """Each thread still running after `within` seconds, waiting for all of them at once."""
    end = time.monotonic() + within
    for thread in threads.values():
        thread.join(max(0.0, end - time.monotonic()))
    return [name for name, thread in threads.items() if thread.is_alive()]


def _until(check, within: float) -> bool:
    end = time.monotonic() + within
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.02)
    return False


def test_a_heard_stop_ends_every_loop(tmp_path: Path) -> None:
    world = World(tmp_path)
    # The ticker reads the stop only every minute, so only the event can end them sooner.
    threads = world.start(world.loops(extend_every=60.0))
    assert _until(world.stored, 20)
    world.write_stop()
    assert _ended(threads, 10) == []
    assert world.alarms.reasons == []


def test_a_stop_no_event_announced_is_found_by_the_ticker(tmp_path: Path) -> None:
    world = World(tmp_path)
    threads = world.start(world.loops(extend_every=0.2))
    assert _until(world.stored, 20)
    # Written past the folder's announcements, as a dropped event reads.
    FolderStore(world.root, bucket="folder").create(STOP, b"{}", hashlib.sha256(b"{}").hexdigest())
    assert _ended(threads, 10) == []


def test_a_loop_started_after_its_runs_stop_runs_no_handler(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.write_stop()
    threads = world.start(world.loops(extend_every=60.0))
    assert _ended(threads, 10) == []
    assert world.calls.order == []


def test_another_runs_stop_ends_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    loops = world.loops(extend_every=0.2)
    threads = world.start(loops)
    assert _until(world.stored, 20)
    world.write_stop(run_stop_key(q.DEPLOYMENT, "2026-11-03"))
    # Several reads of the ticker later, every loop still runs.
    time.sleep(1.0)
    assert [name for name, t in threads.items() if t.is_alive()] == list(q.COMPONENTS)
    world.write_stop()
    assert _ended(threads, 10) == []


@pytest.mark.parametrize("extend_every", [0.2])
def test_a_store_that_does_not_answer_the_stop_read_ends_nothing(
    tmp_path: Path, extend_every: float, monkeypatch
) -> None:
    world = World(tmp_path)
    loops = world.loops(extend_every=extend_every)
    refused = threading.Event()

    def exists(key: str) -> bool:
        if key == STOP:
            refused.set()
            raise Halt("S3 did not answer")
        return original(key)

    original = world.storage.exists
    monkeypatch.setattr(world.storage, "exists", exists)
    threads = world.start(loops)
    assert _until(refused.is_set, 5)
    time.sleep(0.6)
    assert all(t.is_alive() for t in threads.values())
    for loop in loops.values():
        loop.stop()
    assert _ended(threads, 10) == []
