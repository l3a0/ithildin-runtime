"""Who may wait on the clock, so a stop never ends a handler that has written anything.

`Clock.wait_until` raises `Stopped` on a stop, and the loop then stores nothing
for the decision, as docs/design.md's `### How a component learns of a change`
says in item 7. A handler that had already written, such as a send stage
between two orders, would be cut off there. So a stage may wait on the clock
only before it writes, and each module that waits is listed here, where a
change that adds one meets the rule in review. The list names modules and not
the order of their calls, so each listed stage's own tests hold that it waits
first.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "src" / "ithildinruntime"

# Each name, and the modules under `src/ithildinruntime` that may use it.
MAY_USE = {
    # Data and recover each wait for the decision time before they read or write anything.
    "wait_until": {"data/stage.py", "execution/recover.py"},
    # The wall clock waits on it. The loop's `_Progress` defines it.
    "wait_for_stop": {"adapters/clocks.py"},
    # `core` exports it, the wall clock raises it, and the loop catches it.
    "Stopped": {
        "adapters/clocks.py",
        "core/__init__.py",
        "core/halt.py",
        "runtime/loop/receive.py",
    },
}


def uses(tree: ast.AST) -> set[str]:
    """The names in `MAY_USE` a module reads, as a name, an attribute or a string.

    An import is not a use, and neither is a string that only mentions a name,
    such as a docstring. A string that is exactly a name is one, as `getattr`
    takes it.
    """
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add(node.value)
        elif isinstance(node, ast.alias) and node.asname:
            found.add(node.name)
    return found & set(MAY_USE)


def test_only_the_listed_modules_wait_on_the_clock_or_end_on_a_stop() -> None:
    found: dict[str, set[str]] = {name: set() for name in MAY_USE}
    for path in sorted(SOURCE.rglob("*.py")):
        module = path.relative_to(SOURCE).as_posix()
        for name in uses(ast.parse(path.read_text(encoding="utf-8"))):
            found[name].add(module)
    assert found == MAY_USE


@pytest.mark.parametrize(
    "source",
    [
        "clock.wait_until(at, report)",
        "wait = self._clock.wait_until",
        "getattr(clock, 'wait_until')(at, report)",
        "report.wait_for_stop(1.0)",
        "raise Stopped('now')",
        "from ithildinruntime.core import Stopped as Ended",
    ],
)
def test_each_way_to_use_a_name_is_found(source: str) -> None:
    assert uses(ast.parse(source))


def test_a_mention_is_not_a_use() -> None:
    assert not uses(ast.parse('"""It calls `wait_until`."""\nfrom core import Stopped\n'))
