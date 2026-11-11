"""The import rule test.

docs/architecture/05-building-blocks.md's `## Package layout and the import
rule` draws which module may import which, and tests/support/import_rule.py
holds that rule as a checker. The package holds only `core` so far, so most
tests here write small trees under `tmp_path`, plant one import, and show
the checker fails it, naming the file, the line, the import and the arrow
the diagram lacks.
"""

from __future__ import annotations

import itertools
import re
import shutil
from pathlib import Path

import pytest

from tests.support.import_rule import ALLOWED, LIBRARIES, NODES, PACKAGE, check, node_of

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "src" / PACKAGE
BUILDING_BLOCKS = REPO / "docs" / "architecture" / "05-building-blocks.md"


def _diagram() -> tuple[dict[str, str], set[tuple[str, str]]]:
    """The package diagram's boxes, by node id and label, and its arrows."""
    text = BUILDING_BLOCKS.read_text(encoding="utf-8")
    section = text.split("### Which module may import which", 1)[1]
    block = section.split("```mermaid", 1)[1].split("```", 1)[0]
    labels = dict(re.findall(r'^\s*(\w+)\["([^"]*)"\]\s*$', block, flags=re.MULTILINE))
    arrows = set(re.findall(r"^\s*(\w+)\s*-->\s*(\w+)\s*$", block, flags=re.MULTILINE))
    return labels, arrows


def _tree(tmp_path: Path) -> Path:
    """A package with every box of the diagram, each an empty package."""
    root = tmp_path / PACKAGE
    root.mkdir()
    (root / "__init__.py").write_text("")
    for path in NODES.values():
        directory = tmp_path.joinpath(*path.split("."))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "__init__.py").write_text("")
    return root


def _plant(root: Path, module: str, source: str) -> Path:
    """Write `source` as the module `module`, given relative to the package."""
    parts = module.split(".")
    path = root.joinpath(*parts[:-1], parts[-1] + ".py")
    if parts[-1] == "__init__":
        path = root.joinpath(*parts)
        path = path.with_suffix(".py")
    path.parent.mkdir(parents=True, exist_ok=True)
    for parent in path.parents:
        if parent == root.parent:
            break
        init = parent / "__init__.py"
        if not init.exists():
            init.write_text("")
    path.write_text(source)
    return path


def _reasons(root: Path) -> list[str]:
    return [str(v) for v in check(root)]


def test_the_package_as_it_stands_keeps_the_rule() -> None:
    assert check(SOURCE) == []


def test_the_checker_reads_core_as_it_stands(tmp_path: Path) -> None:
    """Acceptance 1: `core` imports nothing else from the package, and no library but rfc8785."""
    copy = tmp_path / PACKAGE
    shutil.copytree(SOURCE, copy)
    codec = copy / "core" / "codec.py"
    source = codec.read_text(encoding="utf-8")
    codec.write_text(source + "\nimport ithildinruntime.journal\nimport numpy\n", encoding="utf-8")
    reasons = _reasons(copy)
    assert len(reasons) == 2, reasons
    assert "from `core` to `journal`" in reasons[0]
    assert "`core` imports nothing outside the standard library but rfc8785" in reasons[1]


def test_the_rule_matches_the_diagram() -> None:
    labels, arrows = _diagram()
    assert set(labels) == set(NODES), "a box was added to or removed from the diagram"
    drawn = {(a, b) for a, targets in ALLOWED.items() for b in targets}
    assert arrows == drawn


def test_each_box_names_the_module_path_it_holds() -> None:
    labels, _ = _diagram()
    for node, path in NODES.items():
        first_line = labels[node].split("<br/>")[0]
        named = first_line if first_line.startswith(PACKAGE) else f"{PACKAGE}.{first_line}"
        assert named == path, f"box {node} names {first_line!r}, the checker {path!r}"


def test_a_tree_with_no_imports_passes(tmp_path: Path) -> None:
    assert _reasons(_tree(tmp_path)) == []


# Case 1: a sibling import.
def test_a_sibling_import_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _plant(root, "data.reader", "import os\n\nimport ithildinruntime.alpha\n")
    (reason,) = _reasons(root)
    assert reason == (
        f"{PACKAGE}/data/reader.py:3: `import ithildinruntime.alpha` needs an arrow "
        "from `data` to `alpha`, which the diagram does not draw"
    )


# Case 2: execution.brokers outside runtime, execution itself included.
@pytest.mark.parametrize("importer", ["execution.order_path", "data.reader", "runtime.loop.run"])
def test_brokers_outside_runtime_fails(tmp_path: Path, importer: str) -> None:
    root = _tree(tmp_path)
    _plant(root, importer, "from ithildinruntime.execution.brokers import Alpaca\n")
    (reason,) = _reasons(root)
    assert "to `brokers`, which the diagram does not draw" in reason


def test_runtime_may_import_brokers(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _plant(root, "runtime.workers", "from ithildinruntime.execution.brokers import Alpaca\n")
    assert _reasons(root) == []


def test_brokers_is_matched_before_execution() -> None:
    assert node_of(f"{PACKAGE}.execution.brokers.alpaca") == "brokers"
    assert node_of(f"{PACKAGE}.execution.order_path") == "execution"
    assert node_of(f"{PACKAGE}.runtime.supervisor.leases") == "supervisor"
    assert node_of(f"{PACKAGE}.runtime.workers") == "runtime"
    assert node_of(PACKAGE) is None


# Case 3: one import written each way ast reports it differently.
@pytest.mark.parametrize(
    ("module", "statement"),
    [
        ("data.reader", "from ..alpha import x\n"),
        ("data.reader", "from .. import alpha\n"),
        ("data.reader", "from ithildinruntime import alpha\n"),
        ("data.reader", "import ithildinruntime.alpha.model as m\n"),
        ("data.inner.reader", "from ...alpha.model import x\n"),
    ],
)
def test_each_spelling_of_a_sibling_import_fails(
    tmp_path: Path, module: str, statement: str
) -> None:
    root = _tree(tmp_path)
    _plant(root, "alpha.model", "")
    _plant(root, module, statement)
    reasons = _reasons(root)
    assert reasons and all("from `data` to `alpha`" in r for r in reasons), reasons


def test_a_relative_import_inside_a_box_passes(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _plant(root, "data.reader", "from . import bars\nfrom .bars import Bar\n")
    _plant(root, "data.bars", "from ithildinruntime.core import Bar\n")
    assert _reasons(root) == []


def test_a_relative_import_above_the_package_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _plant(root, "data.reader", "from .... import x\n")
    (reason,) = _reasons(root)
    assert "climbs above the package" in reason


# Case 4: a package the diagram does not name.
def test_a_package_the_diagram_does_not_name_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _plant(root, "common.shared", "from ithildinruntime.alpha import x\n")
    _plant(root, "data.reader", "from ithildinruntime.common.shared import x\n")
    reasons = _reasons(root)
    assert any(
        r.startswith(f"{PACKAGE}/data/reader.py:1:")
        and "`ithildinruntime.common.shared`, which no box of the diagram holds" in r
        for r in reasons
    ), reasons
    assert any(r.startswith(f"{PACKAGE}/common/shared.py:1:") for r in reasons), reasons


# Case 5: an import in the package root's __init__.py or in runtime's.
@pytest.mark.parametrize(
    ("module", "statement"),
    [
        ("__init__", "from ithildinruntime import core\n"),
        ("__init__", "from . import data\n"),
        ("runtime.__init__", "from ithildinruntime import data\n"),
        ("runtime.__init__", "from ithildinruntime import journal\n"),
    ],
)
def test_an_import_in_a_parent_packages_init_fails(
    tmp_path: Path, module: str, statement: str
) -> None:
    root = _tree(tmp_path)
    _plant(root, module, statement)
    assert _reasons(root), "the checker passed an import every box beneath must refuse"


def test_runtimes_init_may_import_core(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _plant(root, "runtime.__init__", "from ithildinruntime.core import Plan\n")
    assert _reasons(root) == []


# Case 6: an import the ast cannot follow.
@pytest.mark.parametrize(
    "source",
    [
        "import importlib\nm = importlib.import_module('ithildinruntime.alpha')\n",
        "from importlib import import_module\n",
        "from importlib import import_module as load\nload('ithildinruntime.alpha')\n",
        "m = __import__('ithildinruntime.alpha')\n",
        "import builtins\nm = builtins.__import__('os')\n",
    ],
)
def test_a_run_time_import_fails(tmp_path: Path, source: str) -> None:
    root = _tree(tmp_path)
    _plant(root, "data.reader", source)
    reasons = _reasons(root)
    assert reasons and all("imports at run time" in r for r in reasons), reasons


# Case 7: an import inside a function body.
def test_an_import_inside_a_function_fails(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _plant(
        root,
        "data.reader",
        "def read():\n    if True:\n        from ithildinruntime import alpha\n    return alpha\n",
    )
    (reason,) = _reasons(root)
    assert reason.startswith(f"{PACKAGE}/data/reader.py:3: `from ithildinruntime import alpha`")


# Case 8: adapters from outside runtime.
@pytest.mark.parametrize("importer", ["data.reader", "execution.order_path", "runtime.loop.run"])
def test_adapters_outside_runtime_fails(tmp_path: Path, importer: str) -> None:
    root = _tree(tmp_path)
    _plant(root, importer, "from ithildinruntime.adapters import S3\n")
    (reason,) = _reasons(root)
    assert "to `adapters`, which the diagram does not draw" in reason


# Case 9: a worker module from runtime.loop or the supervisor.
@pytest.mark.parametrize("importer", ["runtime.loop.run", "runtime.supervisor.main"])
def test_a_worker_from_the_loop_or_the_supervisor_fails(tmp_path: Path, importer: str) -> None:
    root = _tree(tmp_path)
    _plant(root, importer, "from ithildinruntime.data import reader\n")
    (reason,) = _reasons(root)
    assert "to `data`, which the diagram does not draw" in reason


# Case 10: an outside library outside the one box that may import it.
@pytest.mark.parametrize(
    ("statement", "owner", "importer"),
    [
        ("import boto3\n", "adapters", "data.reader"),
        ("from botocore.exceptions import ClientError\n", "adapters", "runtime.workers"),
        ("import lake.read\n", "data", "alpha.model"),
        ("from alpaca.trading.client import TradingClient\n", "brokers", "execution.order_path"),
    ],
)
def test_an_outside_library_elsewhere_fails(
    tmp_path: Path, statement: str, owner: str, importer: str
) -> None:
    root = _tree(tmp_path)
    _plant(root, importer, statement)
    (reason,) = _reasons(root)
    library = statement.split()[1].split(".")[0]
    assert f"imports `{library}`, which only `{owner}` may import" in reason


@pytest.mark.parametrize(("library", "owner"), sorted(LIBRARIES.items()))
def test_an_outside_library_in_its_box_passes(tmp_path: Path, library: str, owner: str) -> None:
    root = _tree(tmp_path)
    module = NODES[owner].removeprefix(f"{PACKAGE}.") + ".client"
    _plant(root, module, f"import {library}\n")
    assert _reasons(root) == []


def test_core_imports_only_the_standard_library_and_rfc8785(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _plant(root, "core.codec", "import json\nimport rfc8785\nfrom decimal import Decimal\n")
    assert _reasons(root) == []
    _plant(root, "core.frames", "import pandas\n")
    (reason,) = _reasons(root)
    assert "`core` imports nothing outside the standard library but rfc8785" in reason


# Case 11: journal from anything but its four importers.
@pytest.mark.parametrize(
    "importer", ["risk.model", "data.reader", "runtime.workers", "runtime.loop.run"]
)
def test_journal_from_elsewhere_fails(tmp_path: Path, importer: str) -> None:
    root = _tree(tmp_path)
    _plant(root, importer, "from ithildinruntime.journal import Journal\n")
    (reason,) = _reasons(root)
    assert "to `journal`, which the diagram does not draw" in reason


@pytest.mark.parametrize(
    "importer",
    [
        "execution.order_path",
        "monitoring.report",
        "runtime.supervisor.main",
        "runtime.ithildin.main",
    ],
)
def test_journal_from_its_four_importers_passes(tmp_path: Path, importer: str) -> None:
    root = _tree(tmp_path)
    _plant(root, importer, "from ithildinruntime.journal import Journal\n")
    assert _reasons(root) == []


# Case 12: the supervisor and the tool reach nothing beyond their arrows.
@pytest.mark.parametrize(
    ("importer", "statement"),
    [
        ("runtime.supervisor.main", "from ithildinruntime.execution.brokers import Alpaca\n"),
        ("runtime.supervisor.main", "from ithildinruntime.runtime import workers\n"),
        ("runtime.supervisor.main", "from ithildinruntime.runtime.ithildin import main\n"),
        ("runtime.ithildin.main", "from ithildinruntime.execution.brokers import Alpaca\n"),
        ("runtime.ithildin.main", "from ithildinruntime.execution import OrderPath\n"),
        ("runtime.ithildin.main", "from ithildinruntime.runtime.workers import execution\n"),
        ("runtime.ithildin.main", "from ithildinruntime.runtime.loop import run\n"),
    ],
)
def test_the_supervisor_and_the_tool_stay_narrow(
    tmp_path: Path, importer: str, statement: str
) -> None:
    root = _tree(tmp_path)
    _plant(root, "runtime.workers", "")
    _plant(root, importer, statement)
    (reason,) = _reasons(root)
    assert "which the diagram does not draw" in reason


def test_the_supervisor_may_import_its_own_submodules(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _plant(root, "runtime.supervisor.leases", "from ithildinruntime.core import Lease\n")
    _plant(root, "runtime.supervisor.main", "from . import leases\nfrom .leases import x\n")
    assert _reasons(root) == []


# Case 13: journal re-exported from execution or monitoring.
@pytest.mark.parametrize(
    ("exporter", "export", "statement"),
    [
        (
            "execution.__init__",
            "from ithildinruntime import journal\n",
            "from ithildinruntime.execution import journal\n",
        ),
        (
            "monitoring.__init__",
            "from ithildinruntime.journal import Journal\n",
            "from ithildinruntime.monitoring import Journal\n",
        ),
        (
            "execution.order_path",
            "from ithildinruntime.journal import Journal as J\n",
            "from ithildinruntime.execution.order_path import J\n",
        ),
        (
            "monitoring.__init__",
            "from .chain import *\n",
            "from ithildinruntime.monitoring import *\n",
        ),
    ],
)
def test_journal_re_exported_and_imported_elsewhere_fails(
    tmp_path: Path, exporter: str, export: str, statement: str
) -> None:
    root = _tree(tmp_path)
    _plant(root, "monitoring.chain", "from ithildinruntime.journal import Journal\n")
    _plant(root, exporter, export)
    _plant(root, "runtime.workers", statement)
    reasons = [r for r in _reasons(root) if "runtime/workers.py" in r]
    assert any("from `runtime` to `journal`" in r for r in reasons), _reasons(root)


# Case 14: every edge the diagram leaves out, generated rather than listed.
PAIRS = [(a, b) for a, b in itertools.permutations(NODES, 2)]


@pytest.fixture(scope="module")
def base_tree(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _tree(tmp_path_factory.mktemp("base"))


@pytest.mark.parametrize(("importer", "imported"), PAIRS)
def test_every_pair_of_boxes_follows_the_diagram(
    base_tree: Path, tmp_path: Path, importer: str, imported: str
) -> None:
    root = tmp_path / PACKAGE
    shutil.copytree(base_tree, root)
    module = NODES[importer].removeprefix(f"{PACKAGE}.") + ".planted"
    _plant(root, module, f"import {NODES[imported]}\n")
    reasons = _reasons(root)
    if imported in ALLOWED[importer]:
        assert reasons == []
    else:
        (reason,) = reasons
        assert f"needs an arrow from `{importer}` to `{imported}`" in reason


def test_a_chain_fails_at_its_first_missing_arrow(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    _plant(root, "runtime.supervisor.main", "from ithildinruntime.runtime.loop import run\n")
    _plant(root, "runtime.loop.run", "from ithildinruntime.runtime.workers import execution\n")
    _plant(root, "runtime.workers.execution", "from ithildinruntime.execution import brokers\n")
    (reason,) = _reasons(root)
    assert reason.startswith(f"{PACKAGE}/runtime/loop/run.py:1:")
    assert "from `loop` to `runtime`" in reason
