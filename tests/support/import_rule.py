"""The import rule, as a checker that reads a package's imports with `ast`.

docs/architecture/05-building-blocks.md's `## Package layout and the import
rule` draws which module may import which. No worker module imports another,
the shared code sits below them in `core` and `journal`, and only `runtime`
puts the modules together. `ALLOWED` below holds every arrow the diagram
draws, and tests/test_import_rule.py fails when the two differ.

The checker resolves every import to its full module path before it checks
it, so `from ..alpha import x`, `from .. import alpha` and
`from ithildinruntime import alpha` all read as an import of `alpha`. It
matches the most specific module path first, so `execution.brokers` is its
own box rather than part of `execution`. It also refuses six things the
arrows alone would miss.

1. An import inside a function body, which a reader of the top level misses.
   The checker reads every import in the file.
2. An import `ast` cannot follow. The checker refuses the modules that
   import by a string, `importlib`, `pkgutil`, `runpy` and `builtins`, under
   any name they are imported as. It also refuses the names `__import__`,
   `import_module` and `__builtins__`, the builtins `exec` and `eval`, and
   `sys.modules` spelled that way. `importlib.resources` and `importlib.metadata`
   are refused with the rest of `importlib`, so package data is read through
   `Path(__file__)`, as `core/sessions.py` reads its table. A route that needs
   none of these, such as code built from a string some other way, is out of
   its reach.
3. An import of a package the diagram does not name, so a new `common`
   package cannot become a route between workers.
4. An import in a package's `__init__.py` that a box beneath it may not
   make. Importing `runtime.loop` runs `runtime/__init__.py`, so that file is
   held to the loop's rule as well as its own. For the same reason
   `execution/__init__.py` keeps `execution.brokers`' rule, and may import
   only `core`.
5. A name re-exported from one module and imported through it, such as
   `journal` imported from `execution`. The checker follows the name back to
   the module that defines it.
6. An outside library imported outside the one box that may import it.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "ithildinruntime"

# Each box of the diagram, by its node id in the Mermaid source, and the
# module path it holds.
NODES: dict[str, str] = {
    "core": f"{PACKAGE}.core",
    "journal": f"{PACKAGE}.journal",
    "adapters": f"{PACKAGE}.adapters",
    "data": f"{PACKAGE}.data",
    "alpha": f"{PACKAGE}.alpha",
    "risk": f"{PACKAGE}.risk",
    "cost": f"{PACKAGE}.cost",
    "portfolio": f"{PACKAGE}.portfolio",
    "execution": f"{PACKAGE}.execution",
    "brokers": f"{PACKAGE}.execution.brokers",
    "monitoring": f"{PACKAGE}.monitoring",
    "runtime": f"{PACKAGE}.runtime",
    "loop": f"{PACKAGE}.runtime.loop",
    "supervisor": f"{PACKAGE}.runtime.supervisor",
    "tool": f"{PACKAGE}.runtime.ithildin",
}

WORKERS = ("data", "alpha", "risk", "cost", "portfolio", "execution", "monitoring")

# Every arrow the diagram draws: each box, and the boxes it may import.
ALLOWED: dict[str, frozenset[str]] = {
    "core": frozenset(),
    "journal": frozenset({"core"}),
    "adapters": frozenset({"core"}),
    "data": frozenset({"core"}),
    "alpha": frozenset({"core"}),
    "risk": frozenset({"core"}),
    "cost": frozenset({"core"}),
    "portfolio": frozenset({"core"}),
    "execution": frozenset({"core", "journal"}),
    "brokers": frozenset({"core"}),
    "monitoring": frozenset({"core", "journal"}),
    "runtime": frozenset({"core", "adapters", "brokers", "loop", "supervisor", *WORKERS}),
    "loop": frozenset({"core"}),
    "supervisor": frozenset({"core", "journal", "adapters", "loop"}),
    "tool": frozenset({"core", "adapters", "journal"}),
}

# Each outside library, by the name Python imports it as, and the one box that
# may import it. marketlake installs as `marketlake` and imports as `lake`,
# and alpaca-py imports as `alpaca`.
LIBRARIES: dict[str, str] = {
    "boto3": "adapters",
    "botocore": "adapters",
    "lake": "data",
    "alpaca": "brokers",
    "alpaca_trade_api": "brokers",
    # The one codec writes every blob, so only `core` writes canonical JSON.
    "rfc8785": "core",
}

# `core` imports nothing outside the standard library but these.
CORE_LIBRARIES = frozenset({"rfc8785"})

# Modules that import by a string, refused anywhere in the package.
IMPORTERS = frozenset({"importlib", "pkgutil", "runpy", "builtins"})
# Names that import by a string. `exec` and `eval` count only as the bare
# builtins, so a model's own `.eval()` passes.
DYNAMIC = frozenset({"import_module", "__import__", "__builtins__"})
BUILTINS = frozenset({"exec", "eval"})
RUN_TIME = "imports at run time, which the import test cannot follow"


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    statement: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: `{self.statement}` {self.reason}"


@dataclass(frozen=True)
class _Module:
    name: str
    path: Path
    is_package: bool
    tree: ast.Module


def node_of(module: str) -> str | None:
    """The box that holds a module, matching the most specific path first."""
    best: tuple[int, str] | None = None
    for node, path in NODES.items():
        if module == path or module.startswith(path + "."):
            if best is None or len(path) > best[0]:
                best = (len(path), node)
    return best[1] if best else None


def check(root: Path) -> list[Violation]:
    """Every import under the package at `root` that the diagram does not draw."""
    return _Checker(root).run()


class _Checker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.modules: dict[str, _Module] = {}
        for path in sorted(root.rglob("*.py")):
            parts = list(path.relative_to(root.parent).with_suffix("").parts)
            is_package = parts[-1] == "__init__"
            if is_package:
                parts.pop()
            name = ".".join(parts)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            self.modules[name] = _Module(name, path, is_package, tree)
        self._exports: dict[str, dict[str, str]] = {}
        self._visiting: set[str] = set()

    def run(self) -> list[Violation]:
        found: list[Violation] = []
        for module in self.modules.values():
            found.extend(self._check_module(module))
        return found

    def _importers(self, module: _Module) -> list[str | None]:
        """The boxes whose rule this file must keep.

        A package's `__init__.py` runs whenever a module beneath it is
        imported, so it keeps the rule of every box beneath it too.
        """
        boxes: list[str | None] = [node_of(module.name)]
        if module.is_package:
            for node, path in NODES.items():
                if path.startswith(module.name + ".") and node not in boxes:
                    boxes.append(node)
        return boxes

    def _check_module(self, module: _Module) -> Iterator[Violation]:
        importers = self._importers(module)
        where = module.path.relative_to(self.root.parent).as_posix()
        for stmt in ast.walk(module.tree):
            if isinstance(stmt, ast.Name | ast.Attribute):
                if _imports_by_a_string(stmt):
                    yield Violation(where, stmt.lineno, ast.unparse(stmt), RUN_TIME)
                continue
            if not isinstance(stmt, ast.Import | ast.ImportFrom):
                continue
            text = ast.unparse(stmt)
            if isinstance(stmt, ast.ImportFrom):
                named = [stmt.module or ""] if not stmt.level else []
                if any(alias.name in DYNAMIC | BUILTINS for alias in stmt.names):
                    yield Violation(where, stmt.lineno, text, RUN_TIME)
            else:
                named = [alias.name for alias in stmt.names]
            if any(name.split(".")[0] in IMPORTERS for name in named):
                yield Violation(where, stmt.lineno, text, RUN_TIME)
            try:
                internal, outside = self._targets(module, stmt)
            except _Unresolved as error:
                yield Violation(where, stmt.lineno, text, str(error))
                continue
            own = importers[0]
            for importer in importers:
                inherited = (
                    ""
                    if importer == own
                    else f", and this file runs whenever `{NODES[importer]}` is imported"
                    if importer is not None
                    else ""
                )
                for target in internal:
                    reason = self._refuse(importer, target)
                    if reason:
                        yield Violation(where, stmt.lineno, text, reason + inherited)
                for library in outside:
                    reason = self._refuse_library(importer, library)
                    if reason:
                        yield Violation(where, stmt.lineno, text, reason + inherited)

    def _refuse(self, importer: str | None, target: str) -> str | None:
        box = node_of(target)
        if importer is None:
            return (
                f"imports `{target}` from a module no box of the diagram holds, "
                "which may import nothing from the package"
            )
        if box is None:
            return f"imports `{target}`, which no box of the diagram holds"
        if box == importer or box in ALLOWED[importer]:
            return None
        return f"needs an arrow from `{importer}` to `{box}`, which the diagram does not draw"

    def _refuse_library(self, importer: str | None, library: str) -> str | None:
        owner = LIBRARIES.get(library)
        if owner is not None and importer != owner:
            return f"imports `{library}`, which only `{owner}` may import"
        if importer == "core" and owner is None:
            if library not in sys.stdlib_module_names and library not in CORE_LIBRARIES:
                return (
                    f"imports `{library}`, and `core` imports nothing outside the "
                    f"standard library but {', '.join(sorted(CORE_LIBRARIES))}"
                )
        return None

    def _base(self, module: _Module, level: int) -> str:
        package = module.name if module.is_package else module.name.rpartition(".")[0]
        parts = package.split(".")
        if level - 1 >= len(parts):
            raise _Unresolved("climbs above the package")
        return ".".join(parts[: len(parts) - (level - 1)])

    def _targets(
        self, module: _Module, stmt: ast.Import | ast.ImportFrom
    ) -> tuple[set[str], set[str]]:
        """The modules of the package an import reaches, and the outside libraries."""
        internal: set[str] = set()
        outside: set[str] = set()
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                self._sort(alias.name, internal, outside)
            return internal, outside
        if stmt.level:
            base = self._base(module, stmt.level)
            source = f"{base}.{stmt.module}" if stmt.module else base
        else:
            source = stmt.module or ""
        if not _inside(source):
            self._sort(source, internal, outside)
            return internal, outside
        for alias in stmt.names:
            if alias.name == "*":
                internal.add(source)
                internal.update(self._exports_of(source).values())
                continue
            candidate = f"{source}.{alias.name}"
            if candidate in self.modules:
                internal.add(candidate)
            else:
                internal.add(source)
                internal.add(self._origin(source, alias.name, set()))
        return internal, outside

    def _sort(self, name: str, internal: set[str], outside: set[str]) -> None:
        if _inside(name):
            internal.add(name)
        else:
            outside.add(name.split(".")[0])

    def _origin(self, source: str, name: str, seen: set[tuple[str, str]]) -> str:
        """The module that defines `name`, following re-exports through `source`."""
        if (source, name) in seen:
            return source
        seen.add((source, name))
        bound = self._bindings_of(source).get(name)
        if bound is None:
            return source
        kind, target, attr = bound
        if kind == "module":
            return target
        return self._origin(target, attr, seen) if target in self.modules else target

    def _exports_of(self, source: str) -> dict[str, str]:
        if source not in self._exports:
            self._exports[source] = {
                name: self._origin(source, name, set()) for name in self._bindings_of(source)
            }
        return self._exports[source]

    def _bindings_of(self, source: str) -> dict[str, tuple[str, str, str]]:
        """Each name a module binds by importing it, outside its functions and classes."""
        module = self.modules.get(source)
        if module is None:
            return {}
        bindings: dict[str, tuple[str, str, str]] = {}
        for stmt in _top_level(module.tree):
            if isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if _inside(alias.name):
                        if alias.asname:
                            bindings[alias.asname] = ("module", alias.name, "")
                        else:
                            top = alias.name.split(".")[0]
                            bindings[top] = ("module", top, "")
            elif isinstance(stmt, ast.ImportFrom):
                try:
                    base = self._base(module, stmt.level) if stmt.level else ""
                except _Unresolved:
                    continue
                if stmt.level:
                    origin = f"{base}.{stmt.module}" if stmt.module else base
                else:
                    origin = stmt.module or ""
                if not _inside(origin):
                    continue
                for alias in stmt.names:
                    if alias.name == "*":
                        if origin not in self._visiting:
                            self._visiting.add(origin)
                            for name in self._bindings_of(origin):
                                bindings[name] = ("name", origin, name)
                            self._visiting.discard(origin)
                        continue
                    bound = alias.asname or alias.name
                    candidate = f"{origin}.{alias.name}"
                    if candidate in self.modules:
                        bindings[bound] = ("module", candidate, "")
                    else:
                        bindings[bound] = ("name", origin, alias.name)
        return bindings


def _imports_by_a_string(node: ast.Name | ast.Attribute) -> bool:
    if isinstance(node, ast.Name):
        return node.id in DYNAMIC | BUILTINS
    if node.attr in DYNAMIC:
        return True
    return node.attr == "modules" and isinstance(node.value, ast.Name) and node.value.id == "sys"


class _Unresolved(Exception):
    pass


def _inside(name: str) -> bool:
    return name == PACKAGE or name.startswith(PACKAGE + ".")


def _top_level(tree: ast.Module) -> Iterator[ast.stmt]:
    """Every statement outside a function or class body, through `if` and `try`."""
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        stmt = stack.pop()
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        yield stmt
        for field in ("body", "orelse", "finalbody", "handlers"):
            for child in getattr(stmt, field, []):
                if isinstance(child, ast.ExceptHandler):
                    stack.extend(child.body)
                elif isinstance(child, ast.stmt):
                    stack.append(child)
