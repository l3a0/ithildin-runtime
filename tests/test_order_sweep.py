"""Hard limit 2 as a sweep: nothing in the tree reaches a broker around the order path.

#143's acceptance 3. Each way around is planted as text the sweep parses, so
no test here calls what it looks for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.order_sweep import ALLOWED, exemption, found_in, sweep

REPO = Path(__file__).resolve().parent.parent
IN_EXECUTION = "src/ithildinruntime/execution/send.py"
IN_RUNTIME = "src/ithildinruntime/runtime/execution_worker.py"
IN_MONITORING = "src/ithildinruntime/monitoring/report.py"
IN_TESTS = "tests/test_send.py"
IN_SCRIPTS = "scripts/flatten.py"

# One call to the broker, in the ways it could be written.
WAYS: dict[str, tuple[str, str]] = {
    "a broker called by hand": (IN_EXECUTION, "broker.submit(intent)"),
    "a broker called in a test": (IN_TESTS, "broker.submit(intent)"),
    "a broker called in a script": (IN_SCRIPTS, "Broker().submit(intent)"),
    "the private submit": (IN_TESTS, "path._submit(ack)"),
    "a path's broker": (IN_TESTS, "path._broker"),
    "a bound method kept": (IN_EXECUTION, "go = broker.submit\ngo(intent)"),
    "a cancel": (IN_RUNTIME, "broker.cancel('b-1')"),
    "a search for an order": (IN_MONITORING, "broker.find_order(intent, since, {})"),
    "a match pattern": (
        IN_EXECUTION,
        "match broker:\n    case Broker(submit=go):\n        go(intent)",
    ),
    "a name by its text": (IN_TESTS, "getattr(broker, 'submit')(intent)"),
    "a method by its text": (IN_TESTS, "operator.methodcaller('submit', intent)(broker)"),
    "a getter by its text": (IN_TESTS, "attrgetter('_broker')(path)"),
    "a name kept as text": (IN_MONITORING, "NAME = 'submit'"),
    "any getattr in execution": (IN_EXECUTION, "getattr(broker, 'sub' + 'mit')(intent)"),
    "any getattr in runtime": (IN_RUNTIME, "getattr(broker, name)(intent)"),
    "an imported getter": (IN_EXECUTION, "from operator import attrgetter as at"),
    "an object's own names": (IN_EXECUTION, "vars(broker)[name](intent)"),
    "an object's listed names": (IN_RUNTIME, "[n for n in dir(broker)]"),
    "an object's dictionary": (IN_RUNTIME, "broker.__dict__[name]"),
    "an entry built around the path": (
        IN_EXECUTION,
        "journal.record(view, SendingEntry(name), at)",
    ),
    "an entry's class imported": (IN_RUNTIME, "from ithildinruntime.journal import VerdictEntry"),
    "an entry's class through its package": (IN_EXECUTION, "journal.AbandonedEntry(name, 'x')"),
    "an entry written to the store": (IN_EXECUTION, "store.create(journal_entry_key(a, 7), d, s)"),
    "an entry appended around the fold": (IN_EXECUTION, "journal._append(journal._build(b, at))"),
    "a broker imported by a stage": (
        IN_EXECUTION,
        "from ithildinruntime.execution.brokers.simulated import SimulatedBroker",
    ),
    "a broker imported by its package": (IN_EXECUTION, "from . import brokers"),
    "a broker imported by another worker": (
        "src/ithildinruntime/runtime/risk_worker.py",
        "import ithildinruntime.execution.brokers",
    ),
}


def test_3_nothing_in_the_tree_reaches_a_broker_around_the_path() -> None:
    hits = [hit for hit in sweep(REPO) if exemption(hit) is None]
    assert not hits, "\n".join(f"{h.path}:{h.line}: {h.kind} {h.what}" for h in hits)


def test_3_every_exemption_is_used_and_so_is_each_name_in_it() -> None:
    hits = sweep(REPO)
    used = {exemption(hit) for hit in hits}
    assert set(ALLOWED) <= used, set(ALLOWED) - used
    for (start, kind), names in ALLOWED.items():
        mine = [h for h in hits if h.kind == kind and h.path.startswith(start)]
        seen = {h.what for h in mine} | {h.what.rpartition(".")[2] for h in mine}
        assert names is None or names <= seen, (start, names - seen)


@pytest.mark.parametrize("way", WAYS)
def test_3_each_way_around_is_caught(way: str) -> None:
    where, source = WAYS[way]
    hits = [hit for hit in found_in(source, where) if exemption(hit) is None]
    assert hits, f"{way} passes the sweep"


def test_3_the_receive_loops_tasks_are_exempt_by_name_and_no_other_is() -> None:
    loop = "src/ithildinruntime/runtime/loop/receive.py"
    for task in ("ticker", "receiver", "beater"):
        (hit,) = found_in(f"{task}.cancel()", loop)
        assert exemption(hit) is not None
    for other in ("broker.cancel('b-1')", "self._held.cancel('b-1')", "ticker.submit(intent)"):
        assert [h for h in found_in(other, loop) if exemption(h) is None], other


def test_3_the_execution_workers_entry_point_alone_imports_a_broker() -> None:
    source = "from ithildinruntime.execution.brokers import simulated"
    assert all(exemption(hit) for hit in found_in(source, IN_RUNTIME))
    assert [h for h in found_in(source, IN_EXECUTION) if exemption(h) is None]


def test_3_what_is_out_of_the_sweeps_reach() -> None:
    """A name a test puts together at run time. The fake broker's refusals are what catch it."""
    assert not list(found_in("getattr(broker, 'sub' + 'mit')(intent)", IN_TESTS))
