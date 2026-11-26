"""A sweep for any way to a broker around the order path (hard limit 2).

It reads source with `ast` and reports two reaches.

1. **In `src/`, `tests/` and `scripts/`**, a reference to one of `CALLS`, as an
   attribute or a match pattern, and one spelled as text to `getattr` and its
   kin or as a key of an object's own dictionary.
2. **In `src/` alone**, the text of one of those names, the class of an entry
   that only the order path records, the key of a journal entry, an import of
   `execution.brokers`, a change to the path from outside it, and, in
   `execution/` and `runtime/`, any call that reaches a name by its text.

`ALLOWED` names each exemption, and one nothing uses fails the test.

It catches the spellings tests/test_order_sweep.py plants, and others pass. It
reads what is written, so no list of spellings is whole, such as a name put
together at run time or a class bound to another name first. The fake broker
catches none of those that replace the gate or the controls, whose approval is
in the journal like any other.
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
BY_TEXT = frozenset({"getattr", "methodcaller", "attrgetter", "getattr_static"})
BY_TEXT |= {"getmembers", "__getattribute__"}
IN_WORKERS = BY_TEXT | {"vars", "dir", "__dict__", "setattr"}
# What only `account.py` and `order_path.py` may name, since each changes what the path does.
INSIDE = frozenset({"Shared", "Unstored", "record_of", "KINDS"})
# A part of the path or the account, by what it is read from, since each is an ordinary word.
PARTS = {
    "cleared": {"shared"},
    "called": {"shared"},
    "_shared": {"path", "account"},
    "_held": {"path"},
}
# The classes whose methods nothing outside them replaces.
WHOLE = frozenset({"OrderPath", "Account", "Controls", "Gate", "Unstored", "Journal"})

_PATH = "src/ithildinruntime/execution/order_path.py"
_JOURNAL = "src/ithildinruntime/journal/"
_ACCOUNT = "src/ithildinruntime/execution/account.py"
_EXECUTION = ("src/ithildinruntime/execution/", "src/ithildinruntime/runtime/")

# Each exemption: where, what, and the hits it lets through, or `None` for any.
ALLOWED: dict[tuple[str, str], frozenset[str] | None] = {
    (_PATH, "call"): frozenset({"_broker", "submit", "_submit"}),
    (_JOURNAL, "call"): frozenset({"_build", "_append"}),
    ("tests/support/broken_paths.py", "call"): frozenset({"_broker", "submit", "_submit"}),
    ("tests/support/relay_broker.py", "call"): frozenset({"submit"}),
    ("tests/test_broker_cases.py", "call"): frozenset({"cancel", "find_order"}),
    ("tests/test_simulated_log.py", "call"): frozenset({"cancel", "find_order"}),
    ("tests/support/journals.py", "call"): frozenset({"_build", "_append"}),
    ("tests/test_journal", "call"): frozenset({"_build", "_append"}),
    # The receive loop's three asyncio tasks, by the names it holds them under.
    ("src/ithildinruntime/runtime/loop/receive.py", "call"): frozenset(
        {"ticker.cancel", "receiver.cancel", "beater.cancel"}
    ),
    (_PATH, "entry"): None,
    (_JOURNAL, "entry"): None,
    (_PATH, "inside"): frozenset({"Shared", "Unstored", "cleared", "called", "record_of"}),
    (_ACCOUNT, "inside"): frozenset({"Shared", "Unstored"}),
    # The one file that builds a broker, by the mode.
    ("src/ithildinruntime/runtime/execution_worker.py", "import"): frozenset(
        {"Faults", "SimulatedAccount", "StoredPrices", "SimulatedBroker"}
    ),
    # The measurement a person runs, which reads the bars and holds no broker and no account.
    ("src/ithildinruntime/runtime/stress_move.py", "reach"): frozenset({"getattr", "vars"}),
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
    inside = where.startswith(_EXECUTION)
    by_text = IN_WORKERS if inside else ()
    for node in ast.walk(ast.parse(source)):
        line = getattr(node, "lineno", 0)
        match node:
            case ast.Attribute(attr=name) if name in CALLS:
                yield Hit(where, line, "call", _said(node))
            case ast.MatchClass(kwd_attrs=names) if CALLS & set(names):
                yield Hit(where, line, "call", sorted(CALLS & set(names))[0])
            case ast.Call(func=func, args=args) if _last(func) in BY_TEXT:
                for text in (a.value for a in args if isinstance(a, ast.Constant)):
                    if CALLS & set(str(text).split(".")):
                        yield Hit(where, line, "call", str(text))
            case ast.Subscript(value=held, slice=ast.Constant(value=str(text))) if text in CALLS:
                if _last(held) == "__dict__" or _last(getattr(held, "func", held)) == "vars":
                    yield Hit(where, line, "call", text)
        if not shipped:
            continue
        match node:
            case ast.Constant(value=str(text)) if CALLS & set(text.split(".")):
                yield Hit(where, line, "text", text)
            case ast.Name(id=name) | ast.Attribute(attr=name) if name in RECORDED:
                yield Hit(where, line, "entry", name)
            case ast.ImportFrom() | ast.Import():
                for named in _imported(node):
                    if named.rpartition(".")[2] in RECORDED:
                        yield Hit(where, line, "entry", named.rpartition(".")[2])
                    if "brokers" in named.split("."):
                        yield Hit(where, line, "import", named)
        yield from _changes(node, where, line)
        if inside:
            match node:
                case ast.Name(id=name) | ast.Attribute(attr=name) if name in INSIDE:
                    yield Hit(where, line, "inside", name)
                case ast.Attribute(value=held, attr=name) if _owner(held) in PARTS.get(name, ()):
                    yield Hit(where, line, "inside", name)
                case ast.ImportFrom():
                    for named in _imported(node):
                        if named.rpartition(".")[2] in INSIDE:
                            yield Hit(where, line, "inside", named.rpartition(".")[2])
        match node:
            case ast.Name(id=name) | ast.Attribute(attr=name) if name in by_text:
                yield Hit(where, line, "reach", name)
            case ast.ImportFrom() | ast.Import():
                for named in _imported(node):
                    if named.rpartition(".")[2] in by_text:
                        yield Hit(where, line, "reach", named.rpartition(".")[2])


def _changes(node: ast.AST, where: str, line: int) -> Iterator[Hit]:
    """A class built on one of `WHOLE`, and a write to an attribute of one or of `_shared`."""
    if isinstance(node, ast.ClassDef):
        for base in node.bases:
            if _last(base) in WHOLE:
                yield Hit(where, line, "change", f"class {node.name}({_last(base)})")
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AugAssign | ast.AnnAssign):
        targets = [node.target]
    for target in targets:
        if isinstance(target, ast.Attribute) and _last(target.value) in WHOLE | {"_shared"}:
            yield Hit(where, line, "change", ast.unparse(target))


def _imported(node: ast.ImportFrom | ast.Import) -> list[str]:
    """Each name an import binds, under the module it comes from."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    return [f"{node.module or ''}.{alias.name}" for alias in node.names]


def _last(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    return func.attr if isinstance(func, ast.Attribute) else None


def _owner(held: ast.expr) -> str:
    """The last word of what an attribute is read from, such as `path` of `self._order_path`."""
    name = _last(held.func if isinstance(held, ast.Call) else held) or ""
    return name.rpartition("_")[2]


def _said(node: ast.Attribute) -> str:
    """The name, with the one it is read from in the receive loop, whose tasks are named."""
    return f"{node.value.id}.{node.attr}" if isinstance(node.value, ast.Name) else node.attr


def exemption(hit: Hit) -> tuple[str, str] | None:
    """The exemption that lets a hit through, where one does."""
    for (start, kind), names in ALLOWED.items():
        if kind != hit.kind or not hit.path.startswith(start):
            continue
        last = hit.what.rpartition(".")[2]
        if names is None or hit.what in names or last in names:
            return (start, kind)
    return None
