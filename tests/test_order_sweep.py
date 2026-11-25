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
    "a path that resizes": (
        IN_EXECUTION,
        "class Resized(OrderPath):\n    def _just_sending(self, *given): ...",
    ),
    "a gate that always approves": (IN_RUNTIME, "class Yes(risk.Gate):\n    pass"),
    "a path with its check replaced": (IN_EXECUTION, "OrderPath._asked = mine"),
    "a control replaced": (IN_MONITORING, "Controls.stopping = lambda *given: None"),
    "a path's parts rebuilt": (IN_EXECUTION, "OrderPath(replace(Shared(), gate=g), b, r)"),
    "a path's parts imported": (
        IN_RUNTIME,
        "from ithildinruntime.execution.order_path import Shared",
    ),
    "a path's gate replaced": (IN_RUNTIME, "path._shared.gate = yes"),
    "an acknowledgement set by hand": (IN_EXECUTION, "shared.cleared[6] = ack"),
    "an entry's class by its kind": (IN_EXECUTION, "KINDS['sending'](name)"),
    "an entry read back to be recorded": (IN_EXECUTION, "journal.record(v, record_of(e), at)"),
    "a name by its path": (IN_TESTS, "attrgetter('path._broker')(world)"),
    "a name in a dictionary": (IN_TESTS, "path.__dict__['_broker'].x()"),
    "a name in an object's vars": (IN_SCRIPTS, "vars(path)['_broker']"),
    "a name got past its object": (IN_TESTS, "object.__getattribute__(broker, 'submit')"),
    "a static getter": (IN_TESTS, "inspect.getattr_static(broker, 'submit')"),
    "a name kept as a path": (IN_MONITORING, "attrgetter('_broker.submit')(path)"),
    "an object's members": (IN_EXECUTION, "inspect.getmembers(broker)"),
    "a method set": (IN_RUNTIME, "setattr(OrderPath, name, mine)"),
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


def test_3_the_order_path_holds_one_call_of_a_brokers_submit_and_one_of_its_own() -> None:
    """An exemption names a call and not how many, so a second route would pass it."""
    where = "src/ithildinruntime/execution/order_path.py"
    hits = [h.what for h in found_in((REPO / where).read_text(), where) if h.kind == "call"]
    assert sorted(h for h in hits if h.endswith("submit")) == ["self._submit", "submit"]
    # The one is written once, and every read goes through `_called`.
    assert hits.count("self._broker") == 4


def test_3_what_is_out_of_the_sweeps_reach() -> None:
    """The three ways the sweep's docstring names, each still out of it."""
    for where, source in (
        (IN_TESTS, "getattr(broker, 'sub' + 'mit')(intent)"),
        (IN_TESTS, "N = 'submit'\ngetattr(broker, N)(intent)"),
        (IN_MONITORING, "getattr(broker, 'sub' + 'mit')(intent)"),
        (IN_MONITORING, "store.create(f'journal/{a}/{n:012d}.json', data, sha)"),
        (IN_MONITORING, "journal.record(view, replace(old, client_order_id=n), at)"),
    ):
        assert not list(found_in(source, where)), source
