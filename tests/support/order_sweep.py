"""A sweep for any way to a broker around the order path (hard limit 2).

It reads source with `ast` and reports two reaches.

1. **In `src/`, `tests/` and `scripts/`**, a reference to one of `CALLS`, as an
   attribute or a match pattern, and one spelled as text to `getattr` and its
   kin.
2. **In `src/` alone**, the text of one of those names, the class of an entry
   that only the order path records, the key of a journal entry, an import of
   `execution.brokers`, and, in `execution/` and `runtime/`, any call that
   reaches a name by its text.

`ALLOWED` names each exemption, and one nothing uses fails the test. The sweep
reads what is written. Code that builds a name at run time, such as from two
halves of a string handed to a `getattr` in a test, is out of its reach, and so
is a name bound by a package outside these folders.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import NamedTuple

__all__ = ["ALLOWED", "CALLS", "Hit", "found_in", "sweep"]

CALLS = frozenset({"submit", "_submit", "cancel", "find_order", "_broker", "_build", "_append"})
# The entries the order path alone records, and the key one is stored at.
RECORDED = frozenset(
    {
        "IntentEntry",
        "VerdictEntry",
        "SendingEntry",
        "AckEntry",
        "InDoubtEntry",
        "AbandonedEntry",
        "journal_entry_key",
    }
)
# What reaches a name by its text, so the sweep could not read which.
BY_TEXT = frozenset({"getattr", "methodcaller", "attrgetter"})
IN_WORKERS = BY_TEXT | {"vars", "dir", "__dict__", "__getattribute__"}

_PATH = "src/ithildinruntime/execution/order_path.py"
_JOURNAL = "src/ithildinruntime/journal/"
_WORKER = "src/ithildinruntime/runtime/execution_worker.py"

# Each exemption: where, what, and the hits it lets through, or `None` for any.
ALLOWED: dict[tuple[str, str], frozenset[str] | None] = {
    (_PATH, "call"): frozenset({"_broker", "submit", "_submit"}),
    (_JOURNAL, "call"): frozenset({"_build", "_append"}),
    ("tests/support/broken_paths.py", "call"): frozenset({"_broker", "submit", "_submit"}),
    ("tests/support/journals.py", "call"): frozenset({"_build", "_append"}),
    ("tests/test_journal", "call"): frozenset({"_build", "_append"}),
    # The receive loop's three asyncio tasks, by the names it holds them under.
    ("src/ithildinruntime/runtime/loop/receive.py", "call"): frozenset(
        {"ticker.cancel", "receiver.cancel", "beater.cancel"}
    ),
    (_PATH, "entry"): None,
    (_JOURNAL, "entry"): None,
}


class Hit(NamedTuple):
    path: str
    line: int
    kind: str
    what: str


def sweep(root: Path) -> list[Hit]:
    """Every hit in the repository at `root`, the exempt ones included."""
    hits: list[Hit] = []
    for folder in ("src", "tests", "scripts"):
        for path in sorted((root / folder).rglob("*.py")):
            where = path.relative_to(root).as_posix()
            hits.extend(found_in(path.read_text(encoding="utf-8"), where))
    return hits


def found_in(source: str, where: str) -> Iterator[Hit]:
    """Every hit in one file's source, as if it stood at `where`."""
    shipped = where.startswith("src/")
    by_text = IN_WORKERS if shipped and ("/execution/" in where or "/runtime/" in where) else ()
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        match node:
            case ast.Attribute(attr=name) if name in CALLS:
                yield Hit(where, line, "call", _said(node))
            case ast.MatchClass(kwd_attrs=names) if CALLS & set(names):
                yield Hit(where, line, "call", sorted(CALLS & set(names))[0])
            case ast.Call(func=func, args=args) if _last(func) in BY_TEXT:
                for text in (a.value for a in args if isinstance(a, ast.Constant)):
                    if text in CALLS:
                        yield Hit(where, line, "call", str(text))
        if not shipped:
            continue
        match node:
            case ast.Constant(value=str(text)) if text in CALLS:
                yield Hit(where, line, "text", text)
            case ast.Name(id=name) | ast.Attribute(attr=name) if name in RECORDED:
                yield Hit(where, line, "entry", name)
            case ast.ImportFrom() | ast.Import():
                for named in _imported(node):
                    if named.rpartition(".")[2] in RECORDED:
                        yield Hit(where, line, "entry", named.rpartition(".")[2])
                    if "brokers" in named.split("."):
                        yield Hit(where, line, "import", named)
        match node:
            case ast.Name(id=name) | ast.Attribute(attr=name) if name in by_text:
                yield Hit(where, line, "reach", name)
            case ast.ImportFrom() | ast.Import():
                for named in _imported(node):
                    if named.rpartition(".")[2] in by_text:
                        yield Hit(where, line, "reach", named.rpartition(".")[2])


def _imported(node: ast.ImportFrom | ast.Import) -> list[str]:
    """Each name an import binds, under the module it comes from."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return [f"{node.module or ''}.{alias.name}" for alias in node.names]


def _last(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _said(node: ast.Attribute) -> str:
    """The name, with the one it is read from in the receive loop, whose tasks are named."""
    return f"{node.value.id}.{node.attr}" if isinstance(node.value, ast.Name) else node.attr


def exemption(hit: Hit) -> tuple[str, str] | None:
    """The exemption that lets a hit through, where one does."""
    if hit.kind == "import" and hit.path == _WORKER:
        return (_WORKER, "import")
    for (start, kind), names in ALLOWED.items():
        if kind != hit.kind or not hit.path.startswith(start):
            continue
        last = hit.what.rpartition(".")[2]
        if names is None or hit.what in names or last in names:
            return (start, kind)
    return None
