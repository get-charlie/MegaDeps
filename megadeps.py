#!/usr/bin/env python3
"""megadeps - analyze the dependencies between the modules of a project.

Usage:
    python megadeps.py tree     [DIR] [--uses | --where] [--root MODULE] [--reverse]
    python megadeps.py diagram  [DIR] [--functions] [--root MODULE]
    python megadeps.py cycles   [DIR]
    python megadeps.py unused   [DIR] [--all]
    python megadeps.py check    [DIR]

Standard library modules are ignored; third-party packages are shown with --external.
Supported languages: Python.

Standard library only. Works on Windows and Linux.
Exit code: 0 = ok, 1 = problems found (cycles, unused code), 2 = error.
"""

import argparse
import ast
import fnmatch
import os
import re
import sys
import sysconfig
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_IGNORES = [
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox",
    "build", "dist", ".eggs", "*.egg-info", "site-packages",
]

COMMANDS = ("tree", "diagram", "cycles", "unused", "check")


@dataclass
class Symbol:
    name: str                # "func", "Class" or "Class.method"
    kind: str                # "function", "class" or "method"
    line: int
    exported: bool = False   # part of the module's public interface (e.g. __all__)
    ignore: bool = False     # never reported as unused (tests, special methods...)
    hint: str = ""           # why an "unused" report would be low confidence


@dataclass
class Dependency:
    source: str
    target: str
    line: int
    names: list = field(default_factory=list)
    lazy: bool = False       # not loaded at import time (inside a function, type-checking only)
    external: bool = False   # third-party package, not part of the project


@dataclass
class Reference:
    source: str
    target: str
    symbol: str
    line: int
    context: str             # enclosing definition ("Class.method"), "" at module level
    call: bool = False
    lazy: bool = False


@dataclass
class Finding:
    kind: str                # "unused-symbol", "unused-import" or "unreachable"
    path: str
    line: int
    message: str
    low_confidence: bool = False
    hint: str = ""


@dataclass
class Module:
    name: str
    path: str                # relative to the project root, "/" separated
    group: str               # package the module belongs to (used by --packages)
    symbols: list = field(default_factory=list)
    dependencies: list = field(default_factory=list)
    references: list = field(default_factory=list)
    name_uses: dict = field(default_factory=dict)    # name -> contexts using it unqualified
    member_uses: dict = field(default_factory=dict)  # name -> contexts using it as obj.name
    findings: list = field(default_factory=list)


@dataclass
class Project:
    root: str
    modules: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)


class Style:
    def __init__(self, color):
        def c(code):
            return code if color else ""
        self.reset = c("\x1b[0m")
        self.bold = c("\x1b[1m")
        self.dim = c("\x1b[38;5;244m")
        self.red = c("\x1b[31m")
        self.green = c("\x1b[32m")
        self.yellow = c("\x1b[33m")
        self.cyan = c("\x1b[36m")


UP, DOWN, LEFT, RIGHT = 1, 2, 4, 8

UNICODE_CHARS = {
    "h": "─", "v": "│", "tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "ml": "├", "mr": "┤",
    "tee_down": "┬", "tee_up": "┴", "down": "▼", "up": "▲",
    "branch": "├── ", "last": "└── ", "pipe": "│   ", "space": "    ",
    "lines": {
        UP: "│", DOWN: "│", UP | DOWN: "│", LEFT: "─", RIGHT: "─", LEFT | RIGHT: "─",
        DOWN | RIGHT: "┌", DOWN | LEFT: "┐", UP | RIGHT: "└", UP | LEFT: "┘",
        UP | DOWN | RIGHT: "├", UP | DOWN | LEFT: "┤", DOWN | LEFT | RIGHT: "┬",
        UP | LEFT | RIGHT: "┴", UP | DOWN | LEFT | RIGHT: "┼",
    },
}

ASCII_CHARS = {
    "h": "-", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+", "ml": "+", "mr": "+",
    "tee_down": "+", "tee_up": "+", "down": "v", "up": "^",
    "branch": "|-- ", "last": "`-- ", "pipe": "|   ", "space": "    ",
    "lines": {
        UP: "|", DOWN: "|", UP | DOWN: "|", LEFT: "-", RIGHT: "-", LEFT | RIGHT: "-",
        DOWN | RIGHT: ".", DOWN | LEFT: ".", UP | RIGHT: "'", UP | LEFT: "'",
        UP | DOWN | RIGHT: "+", UP | DOWN | LEFT: "+", DOWN | LEFT | RIGHT: "+",
        UP | LEFT | RIGHT: "+", UP | DOWN | LEFT | RIGHT: "|",
    },
}


def enable_windows_ansi():
    """Enable ANSI escape sequences on the Windows console. Returns True on success."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def is_ignored(name, relpath, patterns):
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(relpath, p) for p in patterns)


def collect_sources(root, extensions, patterns):
    """Return the relative POSIX paths of the source files under root."""
    root = Path(root)
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(
            d for d in dirnames if not is_ignored(d, (rel_dir / d).as_posix(), patterns))
        for name in sorted(filenames):
            rel = (rel_dir / name).as_posix()
            if name.endswith(extensions) and not is_ignored(name, rel, patterns):
                result.append(rel)
    return result


# Python front end

STANDARD_DECORATORS = {
    "property", "staticmethod", "classmethod", "cached_property", "setter", "getter",
    "deleter", "dataclass", "total_ordering", "unique", "lru_cache", "cache",
    "contextmanager", "asynccontextmanager", "wraps", "final",
}
IGNORED_DECORATORS = {"overload", "abstractmethod", "abstractproperty", "override"}
TEST_HOOKS = {
    "setUp", "tearDown", "setUpClass", "tearDownClass", "setUpModule", "tearDownModule",
    "asyncSetUp", "asyncTearDown",
}
_IDENT_RE = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)*$")
_TERMINATORS = (ast.Return, ast.Raise, ast.Break, ast.Continue)


def _python_stdlib_names():
    names = set(getattr(sys, "stdlib_module_names", None) or ()) | set(sys.builtin_module_names)
    if not getattr(sys, "stdlib_module_names", None):
        paths = sysconfig.get_paths()
        for key in ("stdlib", "platstdlib"):
            libdir = paths.get(key)
            for sub in ("", "lib-dynload"):
                try:
                    entries = os.listdir(os.path.join(libdir, sub)) if libdir else []
                except OSError:
                    continue
                for entry in entries:
                    name = entry.split(".")[0]
                    if name.isidentifier():
                        names.add(name)
    names.add("__future__")
    return names


def _dotted(node):
    """Return ["a", "b", "c"] for the expression a.b.c, or None."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return parts[::-1]
    return None


def _is_type_checking(test):
    parts = _dotted(test)
    return bool(parts) and parts[-1] == "TYPE_CHECKING"


def _flat_statements(body):
    """Yield statements, including those nested in if/try/with blocks."""
    for node in body:
        yield node
        if isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)) \
                or type(node).__name__ == "TryStar":
            for name in ("body", "orelse", "finalbody"):
                yield from _flat_statements(getattr(node, name, None) or [])
            for handler in getattr(node, "handlers", None) or []:
                yield from _flat_statements(handler.body)


def _is_stub(body):
    """True for bodies that only declare an interface: docstring, pass, ... or raise NotImplementedError."""
    for stmt in body:
        if isinstance(stmt, ast.Pass) or (isinstance(stmt, ast.Expr)
                                          and isinstance(stmt.value, ast.Constant)):
            continue
        if isinstance(stmt, ast.Raise) and stmt.exc is not None:
            exc = stmt.exc.func if isinstance(stmt.exc, ast.Call) else stmt.exc
            if (_dotted(exc) or [""])[-1] == "NotImplementedError":
                continue
        return False
    return True


def _decorator_name(node):
    if isinstance(node, ast.Call):
        node = node.func
    parts = _dotted(node)
    return parts[-1] if parts else ""


class _Record:
    """One imported name, to detect imports that are never used."""
    __slots__ = ("target", "line", "text", "lazy", "name", "used")

    def __init__(self, target, line, text, lazy):
        self.target, self.line, self.text, self.lazy = target, line, text, lazy
        self.name = ""
        self.used = False


class _Binding:
    """What a local name refers to: a module, a symbol of a module, or a dotted module path."""
    __slots__ = ("kind", "target", "symbol", "records")

    def __init__(self, kind, target, symbol):
        self.kind, self.target, self.symbol = kind, target, symbol
        self.records = []


class _Unit:
    def __init__(self, module, is_pkg, tree):
        self.module, self.is_pkg, self.tree = module, is_pkg, tree
        self.dir = module.name if is_pkg else module.name.rpartition(".")[0]
        self.bindings = {}
        self.records = []
        self.star = []
        self.all_names = set()
        self.top_names = set()
        self.class_bases = {}


class PythonAnalyzer:
    name = "python"
    extensions = (".py", ".pyw")

    def __init__(self):
        self.stdlib = _python_stdlib_names()
        self.units = {}
        self.packages = set()
        self.prefixes = set()
        self._external_cache = {}

    def analyze(self, root, patterns):
        root_path = Path(root).resolve()
        prefix = []
        parent = root_path
        while (parent / "__init__.py").is_file() and parent.parent != parent:
            prefix.insert(0, parent.name)
            parent = parent.parent
        src = root_path / "src"
        strip_src = src.is_dir() and not (src / "__init__.py").exists()

        project = Project(str(root_path))
        for rel in collect_sources(root_path, self.extensions, patterns):
            parts = rel[:rel.rfind(".")].split("/")
            if strip_src and parts[0] == "src" and len(parts) > 1:
                parts = parts[1:]
            is_pkg = parts[-1] == "__init__"
            if is_pkg:
                parts = parts[:-1]
            name = ".".join(prefix + parts)
            if not name or name in project.modules:
                continue
            try:
                tree = ast.parse((root_path / rel).read_bytes(), filename=rel)
            except SyntaxError as exc:
                project.errors.append(f"{rel}: {exc.msg} (line {exc.lineno})")
                tree = None
            except (OSError, ValueError, RecursionError) as exc:
                project.errors.append(f"{rel}: {exc}")
                tree = None
            group = name if is_pkg else (name.rpartition(".")[0] or name)
            module = Module(name, rel, group)
            project.modules[name] = module
            self.units[name] = _Unit(module, is_pkg, tree)
            if is_pkg:
                self.packages.add(name)
            pieces = name.split(".")
            for k in range(1, len(pieces)):
                self.prefixes.add(".".join(pieces[:k]))

        units = [u for u in self.units.values() if u.tree is not None]
        for u in units:
            self._scan_imports(u, u.tree, False)
        for u in units:
            self._collect_definitions(u)
        for u in units:
            self._mark_inherited(u)
        for u in units:
            _UsageVisitor(self, u).visit(u.tree)
            self._find_unreachable(u)
        for u in units:
            self._find_unused_imports(u)
        return project

    def _scan_imports(self, u, node, lazy):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self._add_import(u, node, lazy)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            lazy = True
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.body:
                self._scan_imports(u, child, True)
            for child in node.orelse:
                self._scan_imports(u, child, lazy)
            return
        for child in ast.iter_child_nodes(node):
            self._scan_imports(u, child, lazy)

    def _lookup(self, parts, pre):
        for k in range(len(parts), 0, -1):
            candidate = pre + ".".join(parts[:k])
            if candidate in self.units:
                return candidate, pre
        if pre + ".".join(parts) in self.prefixes:
            return None, pre  # namespace package (folder without __init__.py)
        return None

    def _resolve(self, name, u):
        """Map an absolute import to (project module or None, prefix), or None if not ours."""
        if not name:
            return None
        parts = name.split(".")
        found = self._lookup(parts, "")
        if found:
            return found
        if parts[0] in self.stdlib:
            return None
        if u.dir not in self.packages:
            # A script folder is on sys.path when the script runs, so siblings are importable.
            if u.dir:
                found = self._lookup(parts, u.dir + ".")
                if found:
                    return found
            matches = [m for m in self.units if m.endswith("." + name)]
            if len(matches) == 1:
                return matches[0], matches[0][:-len(name)]
        return None

    def _add_external(self, u, name, line, lazy):
        top = name.split(".")[0]
        if top and top not in self.stdlib:
            u.module.dependencies.append(
                Dependency(u.module.name, top, line, [name], lazy, external=True))

    def _bind(self, u, name, kind, target, symbol, record):
        record.name = name
        u.records.append(record)
        old = u.bindings.get(name)
        if old is not None and (old.kind, old.target, old.symbol) == (kind, target, symbol):
            old.records.append(record)
            return
        binding = _Binding(kind, target, symbol)
        binding.records = (old.records if old else []) + [record]
        u.bindings[name] = binding

    def _add_import(self, u, node, lazy):
        line = node.lineno
        src = u.module.name
        if isinstance(node, ast.Import):
            for alias in node.names:
                found = self._resolve(alias.name, u)
                if found is None:
                    self._add_external(u, alias.name, line, lazy)
                    continue
                target, pre = found
                if target:
                    u.module.dependencies.append(Dependency(src, target, line, [alias.name], lazy))
                text = f"'{alias.name}" + (f" as {alias.asname}'" if alias.asname else "'")
                if alias.asname:
                    if target:
                        self._bind(u, alias.asname, "module", target, None,
                                   _Record(target, line, text, lazy))
                else:
                    first = alias.name.split(".")[0]
                    self._bind(u, first, "path", pre + first, None,
                               _Record(target or pre + alias.name, line, text, lazy))
            return

        if node.level:
            base = self._relative_base(u, node)
            if base is None:
                return
            base_mod = base if base in self.units else None
        else:
            found = self._resolve(node.module or "", u)
            if found is None:
                self._add_external(u, node.module or "", line, lazy)
                return
            base_mod, pre = found
            base = base_mod or pre + node.module

        by_target = {}
        for alias in node.names:
            if alias.name == "*":
                if base_mod:
                    by_target.setdefault(base_mod, []).append("*")
                    u.star.append(base_mod)
                continue
            local = alias.asname or alias.name
            text = f"'{alias.name}" + (f" as {alias.asname}'" if alias.asname else "'")
            text += f" from {base or '.'}"
            sub = f"{base}.{alias.name}" if base else alias.name
            if sub in self.units:
                by_target.setdefault(sub, []).append(alias.name)
                self._bind(u, local, "module", sub, None, _Record(sub, line, text, lazy))
            elif base_mod:
                by_target.setdefault(base_mod, []).append(alias.name)
                self._bind(u, local, "symbol", base_mod, alias.name,
                           _Record(base_mod, line, text, lazy))
        for target, names in by_target.items():
            if target != src:
                u.module.dependencies.append(Dependency(src, target, line, names, lazy))

    def _relative_base(self, u, node):
        parts = u.dir.split(".") if u.dir else []
        up = node.level - 1
        if up > len(parts):
            return None
        parts = parts[:len(parts) - up]
        if node.module:
            parts += node.module.split(".")
        return ".".join(parts)

    def resolve_symbol(self, target, symbol, depth=0):
        """Follow re-exports ("from .impl import func" in a package) to the real definition."""
        u = self.units.get(target)
        if u is None or depth > 10 or symbol in u.top_names:
            return target, symbol
        binding = u.bindings.get(symbol)
        if binding is None:
            return target, symbol
        for record in binding.records:
            record.used = True
        if binding.kind == "symbol":
            return self.resolve_symbol(binding.target, binding.symbol, depth + 1)
        if binding.kind == "module":
            return binding.target, None
        return target, symbol

    def resolve_parts(self, u, parts):
        """Resolve a dotted name used in u. Returns (target module, symbol, import records)."""
        binding = u.bindings.get(parts[0])
        if binding is None:
            return None
        rest = list(parts[1:])
        records = binding.records
        if binding.kind == "symbol":
            target, symbol = self.resolve_symbol(binding.target, binding.symbol)
            return target, symbol, records
        if binding.kind == "module":
            target = binding.target
        else:
            target = None
            for k in range(len(parts), 0, -1):
                candidate = ".".join([binding.target] + list(parts[1:k]))
                if candidate in self.units:
                    target, rest = candidate, list(parts[k:])
                    break
            if target is None:
                return None, None, records
            records = [r for r in records if r.target == target
                       or target.startswith(r.target + ".")] or records
        while rest and f"{target}.{rest[0]}" in self.units:
            target += "." + rest.pop(0)
        symbol = rest[0] if rest else None
        if symbol:
            target, symbol = self.resolve_symbol(target, symbol)
        return target, symbol, records

    def _collect_definitions(self, u):
        conftest = u.module.path.rsplit("/", 1)[-1] == "conftest.py"
        for node in _flat_statements(u.tree.body):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                u.top_names.add(node.name)
                self._add_symbol(u, node, node.name, "function", None, conftest)
            elif isinstance(node, ast.ClassDef):
                u.top_names.add(node.name)
                self._add_symbol(u, node, node.name, "class", None, conftest)
                bases = []
                for base in node.bases:
                    if isinstance(base, ast.Subscript):
                        base = base.value
                    bases.append(_dotted(base))
                u.class_bases[node.name] = bases
                for item in _flat_statements(node.body):
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        self._add_symbol(u, item, f"{node.name}.{item.name}", "method",
                                         node.name, conftest)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for name in ast.walk(target):
                        if isinstance(name, ast.Name):
                            u.top_names.add(name.id)
                if any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets) \
                        and isinstance(node.value, (ast.List, ast.Tuple)):
                    for item in node.value.elts:
                        if isinstance(item, ast.Constant) and isinstance(item.value, str):
                            u.all_names.add(item.value)
        for symbol in u.module.symbols:
            if symbol.kind != "method" and symbol.name in u.all_names:
                symbol.exported = True

    def _add_symbol(self, u, node, qualname, kind, cls, conftest):
        name = node.name
        symbol = Symbol(qualname, kind, node.lineno)
        decorators = [_decorator_name(d) for d in node.decorator_list]
        if conftest or (name.startswith("__") and name.endswith("__")) \
                or name.startswith(("test", "Test")) or (cls or "").startswith("Test") \
                or name in TEST_HOOKS or any(d in IGNORED_DECORATORS for d in decorators) \
                or (kind == "method" and _is_stub(node.body)):
            symbol.ignore = True
        elif any(d not in STANDARD_DECORATORS for d in decorators):
            symbol.hint = "decorated"
        elif kind == "function" and name == "main":
            symbol.hint = "possible entry point"
        u.module.symbols.append(symbol)

    def _has_external_base(self, module, cls, seen):
        key = (module, cls)
        if key in self._external_cache:
            return self._external_cache[key]
        if key in seen:
            return False
        seen.add(key)
        u = self.units.get(module)
        result = u is None or cls not in u.class_bases
        if not result:
            for parts in u.class_bases[cls]:
                if parts == ["object"]:
                    continue
                if parts is None:
                    result = True
                elif len(parts) == 1 and parts[0] in u.class_bases:
                    result = self._has_external_base(module, parts[0], seen)
                else:
                    found = self.resolve_parts(u, parts)
                    result = not found or not found[0] or not found[1] \
                        or self._has_external_base(found[0], found[1], seen)
                if result:
                    break
        self._external_cache[key] = result
        return result

    def _mark_inherited(self, u):
        for symbol in u.module.symbols:
            if symbol.kind == "method" and not symbol.ignore and not symbol.hint:
                cls = symbol.name.split(".")[0]
                if self._has_external_base(u.module.name, cls, set()):
                    symbol.hint = "may override an inherited method"

    def _find_unreachable(self, u):
        for node in ast.walk(u.tree):
            for name in ("body", "orelse", "finalbody"):
                stmts = getattr(node, name, None)
                if not isinstance(stmts, list):
                    continue
                for i, stmt in enumerate(stmts[:-1]):
                    if isinstance(stmt, _TERMINATORS):
                        keyword = type(stmt).__name__.lower()
                        u.module.findings.append(Finding(
                            "unreachable", u.module.path, stmts[i + 1].lineno,
                            f"unreachable code after '{keyword}'"))
                        break

    def _find_unused_imports(self, u):
        if u.is_pkg:
            return  # package __init__ files usually import to re-export
        for record in u.records:
            if not record.used and record.name not in u.all_names:
                u.module.findings.append(Finding(
                    "unused-import", u.module.path, record.line, f"unused import {record.text}"))


class _UsageVisitor(ast.NodeVisitor):
    def __init__(self, analyzer, unit):
        self.analyzer = analyzer
        self.unit = unit
        self.module = unit.module
        self.stack = []
        self.calls = set()

    def _use(self, table, name):
        table.setdefault(name, set()).add(".".join(self.stack))

    def visit_FunctionDef(self, node):
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)
        self.stack.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        for expr in node.decorator_list + node.bases + node.keywords:
            self.visit(expr)
        self.stack.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.stack.pop()

    def visit_Call(self, node):
        self.calls.add(id(node.func))
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Store):
            return
        self._use(self.module.name_uses, node.id)
        self._link([node.id], node, True)

    def visit_Attribute(self, node):
        parts = _dotted(node)
        if parts is None:
            self._use(self.module.member_uses, node.attr)
            self.generic_visit(node)
            return
        self._use(self.module.name_uses, parts[0])
        for part in parts[1:]:
            self._use(self.module.member_uses, part)
        self._link(parts, node, True)

    def visit_Constant(self, node):
        # Strings such as "Session" in annotations or getattr(obj, "name") count as uses.
        if isinstance(node.value, str) and len(node.value) <= 200 and _IDENT_RE.match(node.value):
            parts = node.value.split(".")
            self._use(self.module.name_uses, parts[0])
            for part in parts:
                self._use(self.module.member_uses, part)
            self._link(parts, node, False)

    def _link(self, parts, node, record):
        found = self.analyzer.resolve_parts(self.unit, parts)
        if found is None:
            if record and len(parts) == 1:
                for target in self.unit.star:
                    if parts[0] in self.analyzer.units[target].top_names:
                        self._reference(target, parts[0], node, False)
                        break
            return
        target, symbol, records = found
        for r in records:
            r.used = True
        if record and target and symbol and target != self.module.name:
            self._reference(target, symbol, node, all(r.lazy for r in records))

    def _reference(self, target, symbol, node, lazy):
        self.module.references.append(Reference(
            self.module.name, target, symbol, node.lineno, ".".join(self.stack),
            id(node) in self.calls, lazy))


ANALYZERS = {"python": PythonAnalyzer}


def pick_analyzer(root, patterns, lang):
    if lang != "auto":
        return ANALYZERS[lang]()
    counts = {name: len(collect_sources(root, cls.extensions, patterns))
              for name, cls in ANALYZERS.items()}
    best = max(counts, key=counts.get)
    return ANALYZERS[best]() if counts[best] else None


# Language-independent analysis

class Edge:
    __slots__ = ("deps", "refs")

    def __init__(self):
        self.deps = []
        self.refs = []

    @property
    def lazy(self):
        items = self.deps or self.refs
        return bool(items) and all(item.lazy for item in items)


class Graph:
    def __init__(self):
        self.nodes = {}   # name -> "module" or "external"
        self.out = {}     # name -> {target: Edge}

    def add_node(self, name, kind="module"):
        self.nodes.setdefault(name, kind)
        self.out.setdefault(name, {})

    def edge(self, src, dst):
        return self.out[src].setdefault(dst, Edge())

    def reversed_out(self):
        rev = {n: {} for n in self.nodes}
        for src, targets in self.out.items():
            for dst, e in targets.items():
                rev[dst][src] = e
        return rev

    def subgraph(self, keep):
        g = Graph()
        for n in sorted(keep):
            g.add_node(n, self.nodes[n])
        for n in keep:
            for dst, e in self.out[n].items():
                if dst in keep:
                    g.out[n][dst] = e
        return g


def build_graph(project, lazy=True, external=False, grouped=False):
    def key(name):
        return project.modules[name].group if grouped else name

    g = Graph()
    for name in sorted(project.modules):
        g.add_node(key(name))
    for module in project.modules.values():
        src = key(module.name)
        for dep in module.dependencies:
            if dep.lazy and not lazy:
                continue
            if dep.external:
                if not external:
                    continue
                g.add_node(dep.target, "external")
                dst = dep.target
            else:
                dst = key(dep.target)
            if dst != src:
                g.edge(src, dst).deps.append(dep)
        for ref in module.references:
            if ref.lazy and not lazy:
                continue
            dst = key(ref.target)
            if dst != src:
                g.edge(src, dst).refs.append(ref)
    return g


def reachable(adjacency, start):
    seen = {start}
    queue = deque([start])
    while queue:
        for nxt in adjacency.get(queue.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def strongly_connected(nodes, out):
    """Tarjan's algorithm, iterative. Returns a list of components."""
    index, low, on_stack, stack, result = {}, {}, set(), [], []
    counter = 0
    for start in sorted(nodes):
        if start in index:
            continue
        index[start] = low[start] = counter
        counter += 1
        stack.append(start)
        on_stack.add(start)
        work = [(start, iter(sorted(out.get(start, ()))))]
        while work:
            v, children = work[-1]
            for w in children:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(sorted(out.get(w, ())))))
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
                if low[v] == index[v]:
                    component = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == v:
                            break
                    result.append(component)
    return result


def _cycle_path(start, members, out):
    """Shortest path start -> ... -> start inside the component."""
    prev = {}
    queue = deque([start])
    seen = {start}
    while queue:
        v = queue.popleft()
        for w in sorted(out.get(v, ())):
            if w not in members:
                continue
            if w == start:
                path = [v]
                while path[-1] != start:
                    path.append(prev[path[-1]])
                return path[::-1] + [start]
            if w not in seen:
                seen.add(w)
                prev[w] = v
                queue.append(w)
    return [start, start]


def find_cycles(graph):
    """Return [(members, path, lazy_only)], where path is a concrete cycle a -> b -> a."""
    strict_out = {u: {v for v, e in targets.items() if not e.lazy}
                  for u, targets in graph.out.items()}
    strict = [set(c) for c in strongly_connected(graph.nodes, strict_out) if len(c) > 1]
    cycles = []
    for component in strongly_connected(graph.nodes, graph.out):
        if len(component) < 2:
            continue
        members = set(component)
        inner = [s for s in strict if s <= members]
        if inner:
            path = _cycle_path(min(inner[0]), inner[0], strict_out)
        else:
            path = _cycle_path(min(members), members, graph.out)
        cycles.append((sorted(members), path, not inner))
    cycles.sort(key=lambda c: (c[2], len(c[1]), c[1]))
    return cycles


def find_unused_symbols(project):
    references = defaultdict(set)
    for module in project.modules.values():
        for ref in module.references:
            if ref.symbol:
                references[(ref.target, ref.symbol)].add((ref.source, ref.context))
    members = defaultdict(set)
    for module in project.modules.values():
        for name, contexts in module.member_uses.items():
            members[name].update((module.name, c) for c in contexts)

    findings = []
    for module in project.modules.values():
        for symbol in module.symbols:
            if symbol.ignore or symbol.exported:
                continue

            def outside(mod, context):
                # Recursive calls and self references do not count as uses.
                return not (mod == module.name and (
                    context == symbol.name or context.startswith(symbol.name + ".")))

            short = symbol.name.rsplit(".", 1)[-1]
            used = any(outside(m, c) for m, c in members.get(short, ()))
            if not used and symbol.kind != "method":
                used = any(outside(module.name, c) for c in module.name_uses.get(symbol.name, ())) \
                    or any(outside(m, c) for m, c in references.get((module.name, symbol.name), ()))
            if not used:
                findings.append(Finding(
                    "unused-symbol", module.path, symbol.line,
                    f"unused {symbol.kind} '{symbol.name}'", bool(symbol.hint), symbol.hint))
    return findings


def collect_findings(project):
    findings = find_unused_symbols(project)
    for module in project.modules.values():
        findings.extend(module.findings)
    findings.sort(key=lambda f: (f.path, f.line, f.message))
    return findings


# Output

def _uses_text(edge):
    names = {}
    for ref in edge.refs:
        names[ref.symbol] = names.get(ref.symbol, False) or ref.call
    return ", ".join(f"{n}()" if call else n for n, call in sorted(names.items()))


def _where_rows(edge, project):
    rows = set()
    for ref in edge.refs:
        name = f"{ref.symbol}()" if ref.call else ref.symbol
        where = f"{project.modules[ref.source].path}:{ref.line}"
        context = f"in {ref.context}" if ref.context else "at module level"
        rows.add((project.modules[ref.source].path, ref.line, name, where, context))
    rows = sorted(rows)
    if not rows:
        return []
    w1 = max(len(r[2]) for r in rows)
    w2 = max(len(r[3]) for r in rows)
    return [f"{r[2]:<{w1}}  {r[3]:<{w2}}  {r[4]}" for r in rows]


def render_tree(graph, project, roots, reverse, detail, max_depth, chars, s):
    adjacency = graph.reversed_out() if reverse else graph.out
    lines = []
    printed = set()
    expanded = set()

    def label(node):
        if graph.nodes[node] == "external":
            return f"{s.dim}{node} (external){s.reset}"
        return f"{s.bold}{node}{s.reset}"

    def walk(node, prefix, path, depth):
        expanded.add(node)
        children = sorted(adjacency[node])
        for i, child in enumerate(children):
            printed.add(child)
            last = i == len(children) - 1
            cont = prefix + (chars["space"] if last else chars["pipe"])
            edge = adjacency[node][child]
            text = label(child)
            if edge.lazy:
                text += f" {s.dim}(lazy){s.reset}"
            has_children = bool(adjacency[child])
            note = ""
            if child in path:
                note = f" {s.yellow}(cycle){s.reset}"
            elif child in expanded and has_children:
                note = f" {s.dim}(see above){s.reset}"
            elif max_depth and depth >= max_depth and has_children:
                note = f" {s.dim}...{s.reset}"
            if detail == "uses":
                uses = _uses_text(edge)
                if uses:
                    note += f"  {s.dim}uses:{s.reset} {s.cyan}{uses}{s.reset}"
            lines.append(prefix + (chars["last"] if last else chars["branch"]) + text + note)
            expand = has_children and not (child in path or child in expanded
                                           or (max_depth and depth >= max_depth))
            if detail == "where":
                bar = chars["v"] + " " if expand else "  "
                for row in _where_rows(edge, project):
                    lines.append(f"{cont}{bar}{s.dim}{row}{s.reset}")
            if expand:
                walk(child, cont, path | {child}, depth + 1)

    def tree(root):
        printed.add(root)
        lines.append(label(root))
        if adjacency[root]:
            walk(root, "", {root}, 1)
        else:
            lines.append(f"{s.dim}(no dependencies){s.reset}")
        lines.append("")

    if roots:
        for root in roots:
            tree(root)
        return lines[:-1]

    targets = {child for children in adjacency.values() for child in children}
    standalone = []
    for node in sorted(graph.nodes):
        if node in targets:
            continue
        if adjacency[node]:
            tree(node)
        else:
            standalone.append(node)
            printed.add(node)
    for node in sorted(graph.nodes):
        if node not in printed:
            tree(node)
    if standalone:
        lines.append(f"{s.dim}Standalone (no dependencies):{s.reset} " + ", ".join(standalone))
    elif lines:
        lines.pop()
    return lines


class _Node:
    __slots__ = ("name", "lines", "layer", "external", "x", "w", "h")

    def __init__(self, name, lines, layer, external=False):
        self.name, self.lines, self.layer, self.external = name, lines, layer, external
        self.x = self.w = self.h = 0

    @property
    def dummy(self):
        return self.lines is None


class _Segment:
    """Part of an edge between two consecutive layers."""
    __slots__ = ("upper", "lower", "rev", "lazy", "last", "col", "track")

    def __init__(self, upper, lower, rev, lazy, last):
        self.upper, self.lower, self.rev, self.lazy, self.last = upper, lower, rev, lazy, last
        self.col = 0
        self.track = None


def _back_edges(nodes, out):
    """Edges that close a cycle in a depth-first search; reversing them yields a DAG."""
    targets = {v for vs in out.values() for v in vs}
    order = [n for n in nodes if n not in targets] + [n for n in nodes if n in targets]
    state, back = {}, set()
    for start in order:
        if start in state:
            continue
        state[start] = 1
        stack = [(start, iter(sorted(out[start])))]
        while stack:
            v, children = stack[-1]
            for w in children:
                if w not in state:
                    state[w] = 1
                    stack.append((w, iter(sorted(out[w]))))
                    break
                if state[w] == 1:
                    back.add((v, w))
            else:
                state[v] = 2
                stack.pop()
    return back


def _assign_layers(nodes, edges):
    succ, preds = defaultdict(list), defaultdict(list)
    indegree = Counter()
    for a, b in edges:
        succ[a].append(b)
        preds[b].append(a)
        indegree[b] += 1
    layer = {n: 0 for n in nodes}
    queue = deque(n for n in nodes if indegree[n] == 0)
    while queue:
        v = queue.popleft()
        for w in succ[v]:
            layer[w] = max(layer[w], layer[v] + 1)
            indegree[w] -= 1
            if indegree[w] == 0:
                queue.append(w)
    for n in nodes:
        if not preds[n] and succ[n]:
            layer[n] = min(layer[w] for w in succ[n]) - 1
    used = sorted(set(layer.values()))
    remap = {old: new for new, old in enumerate(used)}
    return {n: remap[layer[n]] for n in nodes}


def render_diagram(graph, contents, chars, s):
    """Draw the graph as boxes joined by arrows. Returns (lines, edges drawn upwards)."""
    names = sorted(graph.nodes)
    if not names:
        return [], []
    back = _back_edges(names, graph.out)
    dag = []
    for u in names:
        for v in sorted(graph.out[u]):
            lazy = graph.out[u][v].lazy
            dag.append((v, u, True, lazy) if (u, v) in back else (u, v, False, lazy))
    layer = _assign_layers(names, [(a, b) for a, b, _, _ in dag])

    nodes = {n: _Node(n, [n] + list(contents.get(n, ())), layer[n],
                      graph.nodes[n] == "external") for n in names}
    segments = []
    for a, b, rev, lazy in dag:
        chain = [a]
        for level in range(layer[a] + 1, layer[b]):
            dummy = f"\0{len(nodes)}"
            nodes[dummy] = _Node(dummy, None, level)
            chain.append(dummy)
        chain.append(b)
        for i in range(len(chain) - 1):
            segments.append(_Segment(chain[i], chain[i + 1], rev, lazy, i == len(chain) - 2))

    incoming = Counter(seg.lower for seg in segments)
    out_kinds = defaultdict(set)
    preds, succs = defaultdict(list), defaultdict(list)
    for seg in segments:
        out_kinds[seg.upper].add(seg.rev)
        preds[seg.lower].append(seg.upper)
        succs[seg.upper].append(seg.lower)
    for node in nodes.values():
        if node.dummy:
            node.w, node.h = 1, 0
            continue
        node.w = max(max(len(t) for t in node.lines) + 4, 4 * incoming[node.name] + 1,
                     7 if len(out_kinds[node.name]) == 2 else 0)
        node.h = len(node.lines) + 2 + (1 if len(node.lines) > 1 else 0)

    count = max(layer.values()) + 1
    layers = [[] for _ in range(count)]
    for name in sorted(nodes, key=lambda n: (nodes[n].dummy, n)):
        layers[nodes[name].layer].append(name)

    # Order nodes inside each layer to reduce crossings (barycenter heuristic).
    pos = {n: i for lay in layers for i, n in enumerate(lay)}
    for it in range(8):
        down = it % 2 == 0
        neighbors = preds if down else succs
        for i in (range(1, count) if down else range(count - 2, -1, -1)):
            def barycenter(n):
                ps = [pos[m] for m in neighbors[n]]
                return sum(ps) / len(ps) if ps else pos[n]
            layers[i].sort(key=barycenter)
            for j, n in enumerate(layers[i]):
                pos[n] = j

    gap = 2
    for lay in layers:
        x = 0
        for n in lay:
            nodes[n].x = x
            x += nodes[n].w + gap

    def center(n):
        return nodes[n].x + nodes[n].w // 2

    # Move nodes towards their neighbors; the average of a left-packed and a
    # right-packed placement keeps the spacing between nodes.
    for it in range(8):
        down = it % 2 == 0
        neighbors = preds if down else succs
        for i in (range(1, count) if down else range(count - 2, -1, -1)):
            lay = layers[i]
            want = {}
            for n in lay:
                cs = [center(m) for m in neighbors[n]]
                want[n] = sum(cs) // len(cs) - nodes[n].w // 2 if cs else nodes[n].x
            left, cur = {}, None
            for n in lay:
                left[n] = want[n] if cur is None else max(want[n], cur)
                cur = left[n] + nodes[n].w + gap
            right, cur = {}, None
            for n in reversed(lay):
                right[n] = want[n] if cur is None else min(want[n], cur - nodes[n].w)
                cur = right[n] - gap
            for n in lay:
                nodes[n].x = (left[n] + right[n]) // 2
    shift = min(node.x for node in nodes.values())
    for node in nodes.values():
        node.x -= shift

    def port(name, rev):
        node = nodes[name]
        if node.dummy:
            return node.x
        c = node.x + node.w // 2
        return c + 2 if rev and len(out_kinds[name]) == 2 else c

    channels = [defaultdict(list) for _ in range(count)]
    arriving_at = {}
    for seg in segments:
        channels[nodes[seg.upper].layer][(seg.upper, seg.rev)].append(seg)
        arriving_at[seg.lower] = seg

    # Make long edges straight where there is room.
    for i in range(1, count):
        lay = layers[i]
        for j, name in enumerate(lay):
            if not nodes[name].dummy:
                continue
            seg = arriving_at[name]
            target = port(seg.upper, seg.rev)
            lo = nodes[lay[j - 1]].x + nodes[lay[j - 1]].w + gap if j else target
            hi = nodes[lay[j + 1]].x - gap - 1 if j + 1 < len(lay) else target
            if lo <= target <= hi:
                nodes[name].x = target

    def forbidden(groups, ports, own, near):
        # Columns a line arriving from group "own" must avoid, so that separate lines
        # never touch (they would look joined). With near=True, also keep one column apart.
        result = set()
        for g, p in ports.items():
            if g != own or len(groups[g]) > 1:
                result.update((p - 1, p, p + 1) if near else (p,))
            elif near:
                result.update((p - 1, p + 1))
        return result

    for i in range(count - 1):
        groups = channels[i]
        ports = {g: port(*g) for g in groups}
        moved = True
        while moved:
            moved = False
            for g, segs in groups.items():
                for seg in segs:
                    lower = nodes[seg.lower]
                    if lower.dummy and lower.x in forbidden(groups, ports, g, True):
                        lay = layers[i + 1]
                        for n in lay[lay.index(seg.lower):]:
                            nodes[n].x += 1
                        moved = True
        for g, segs in groups.items():
            for seg in segs:
                if nodes[seg.lower].dummy:
                    seg.col = nodes[seg.lower].x
        for name in layers[i + 1]:
            node = nodes[name]
            if node.dummy:
                continue
            arriving = sorted((seg for segs in groups.values() for seg in segs
                               if seg.lower == name), key=lambda sg: port(sg.upper, sg.rev))
            candidates = list(range(node.x + 1, node.x + node.w - 1))
            taken = set()
            for seg in arriving:
                p = port(seg.upper, seg.rev)
                near = forbidden(groups, ports, (seg.upper, seg.rev), True)
                strict = forbidden(groups, ports, (seg.upper, seg.rev), False)
                close = {c + d for c in taken for d in (-1, 1)}
                options = [c for c in candidates if c not in taken | close | near] \
                    or [c for c in candidates if c not in taken | near] \
                    or [c for c in candidates if c not in taken | strict] \
                    or [c for c in candidates if c not in taken] or candidates
                seg.col = min(options, key=lambda c: (abs(c - p), c))
                taken.add(seg.col)

    heights, channel_heights = [], []
    for i in range(count):
        real = [nodes[n].h for n in layers[i] if not nodes[n].dummy]
        heights.append(max(real) if real else 1)
        groups = channels[i]
        spans = []
        for g, segs in groups.items():
            p = port(*g)
            cols = [seg.col for seg in segs]
            if len(segs) == 1 and cols[0] == p:
                continue
            spans.append((min(cols + [p]), max(cols + [p]), g))
        row_ends = []
        for start, end, g in sorted(spans):
            for r, last_end in enumerate(row_ends):
                if last_end + 1 < start:
                    row_ends[r] = end
                    break
            else:
                r = len(row_ends)
                row_ends.append(end)
            for seg in groups[g]:
                seg.track = r
        channel_heights.append(len(row_ends) + 2 if groups else 1)
    channel_heights[-1] = 0

    tops, y = [], 0
    for i in range(count):
        tops.append(y)
        y += heights[i] + channel_heights[i]
    height = y
    width = max(node.x + node.w for node in nodes.values()) + 1
    bits = [[0] * width for _ in range(height)]
    text = [[None] * width for _ in range(height)]
    color = [[""] * width for _ in range(height)]

    def vline(c, r1, r2, col):
        for r in range(r1, r2 + 1):
            bits[r][c] |= (UP if r > r1 else 0) | (DOWN if r < r2 else 0)
            color[r][c] = col

    def hline(r, c1, c2, col):
        a, b = min(c1, c2), max(c1, c2)
        for c in range(a, b + 1):
            bits[r][c] |= (LEFT if c > a else 0) | (RIGHT if c < b else 0)
            color[r][c] = col

    def put(r, c, ch, col=""):
        text[r][c] = ch
        color[r][c] = col

    marks = []
    for seg in sorted(segments, key=lambda sg: (2 if sg.rev else 0 if sg.lazy else 1)):
        upper, lower = nodes[seg.upper], nodes[seg.lower]
        i = upper.layer
        col = s.red if seg.rev else s.dim if seg.lazy else ""
        p = port(seg.upper, seg.rev)
        start = tops[i] if upper.dummy else tops[i] + upper.h - 1
        first_row = tops[i] + heights[i]
        end = tops[i + 1]
        if seg.track is None:
            vline(p, start, end, col)
        else:
            row = first_row + 1 + seg.track
            vline(p, start, row, col)
            hline(row, p, seg.col, col)
            vline(seg.col, row, end, col)
        if not upper.dummy:
            marks.append((start + 1, p, chars["up"], col) if seg.rev
                         else (start, p, chars["tee_down"], col))
        if not lower.dummy:
            marks.append((end, seg.col, chars["tee_up"], col) if seg.rev
                         else (end - 1, seg.col, chars["down"], col))

    for node in nodes.values():
        if node.dummy:
            continue
        top, left, w, h = tops[node.layer], node.x, node.w, node.h
        border = s.dim if node.external else ""
        put(top, left, chars["tl"], border)
        put(top, left + w - 1, chars["tr"], border)
        put(top + h - 1, left, chars["bl"], border)
        put(top + h - 1, left + w - 1, chars["br"], border)
        for c in range(left + 1, left + w - 1):
            put(top, c, chars["h"], border)
            put(top + h - 1, c, chars["h"], border)
        for r in range(top + 1, top + h - 1):
            put(r, left, chars["v"], border)
            put(r, left + w - 1, chars["v"], border)
            for c in range(left + 1, left + w - 1):
                put(r, c, " ")
        rows = [node.lines[0].center(w - 2)]
        if len(node.lines) > 1:
            put(top + 2, left, chars["ml"], border)
            put(top + 2, left + w - 1, chars["mr"], border)
            for c in range(left + 1, left + w - 1):
                put(top + 2, c, chars["h"], border)
            rows += [None] + [" " + t for t in node.lines[1:]]
        for k, row in enumerate(rows):
            if row is None:
                continue
            style = (s.dim if node.external else s.bold) if k == 0 else ""
            for j, ch in enumerate(row):
                put(top + 1 + k, left + 1 + j, ch, style)
    for r, c, ch, col in marks:
        put(r, c, ch, col)

    lines = []
    line_chars = chars["lines"]
    for r in range(height):
        last = width - 1
        while last >= 0 and text[r][last] in (None, " ") and not bits[r][last]:
            last -= 1
        pieces, current = [], ""
        for c in range(last + 1):
            ch = text[r][c] or line_chars.get(bits[r][c], " ")
            if color[r][c] != current:
                pieces.append(s.reset if current else "")
                pieces.append(color[r][c])
                current = color[r][c]
            pieces.append(ch)
        if current:
            pieces.append(s.reset)
        lines.append("".join(pieces))
    upward = sorted((u, v) for u, v in back)
    return lines, upward


def box_contents(project, graph, grouped):
    contents = {}
    if grouped:
        for module in project.modules.values():
            short = module.name[len(module.group) + 1:] if module.name != module.group \
                else "(package)"
            contents.setdefault(module.group, []).append(short)
        return {k: sorted(v) for k, v in contents.items()}
    for module in project.modules.values():
        rows = []
        for symbol in module.symbols:
            if symbol.kind == "class":
                rows.append(symbol.name)
            elif symbol.kind == "method":
                rows.append("  " + symbol.name.split(".", 1)[1] + "()")
            else:
                rows.append(symbol.name + "()")
        contents[module.name] = rows
    return contents


def find_node(graph, project, query, grouped):
    if query in graph.nodes:
        return query
    matches = [n for n in graph.nodes if n.endswith("." + query)]
    if not matches:
        wanted = query.replace("\\", "/").lstrip("./")
        for module in project.modules.values():
            if module.path == wanted or module.path.endswith("/" + wanted):
                matches.append(module.group if grouped else module.name)
    matches = sorted(set(matches))
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError(f"'{query}' is ambiguous: {', '.join(matches)}")
    raise ValueError(f"module '{query}' not found")


def print_cycles(cycles, graph, project, s):
    for number, (members, path, lazy_only) in enumerate(cycles, 1):
        title = " -> ".join(path)
        note = f" {s.dim}(only through lazy imports){s.reset}" if lazy_only else ""
        print(f"{s.red}{s.bold}Cycle {number}:{s.reset} {s.red}{title}{s.reset}{note}")
        if len(members) > len(path) - 1:
            print(f"  {s.dim}{len(members)} modules involved: {', '.join(members)}{s.reset}")
        rows = []
        for u, v in zip(path, path[1:]):
            edge = graph.out[u][v]
            items = [(project.modules[d.source].path, d.line, f"imports {d.target}", d.lazy)
                     for d in edge.deps]
            if not items:
                items = [(project.modules[r.source].path, r.line,
                          f"uses {r.target}.{r.symbol}", r.lazy) for r in edge.refs]
            items.sort()
            for path_, line, what, lazy in items[:3]:
                rows.append((f"{path_}:{line}", what + (" (lazy)" if lazy else "")))
            if len(items) > 3:
                rows.append(("", f"... and {len(items) - 3} more"))
        width = max(len(r[0]) for r in rows) if rows else 0
        for where, what in rows:
            print(f"  {s.cyan}{where:<{width}}{s.reset}  {what}")
        print()


def print_findings(findings, show_low, s):
    high = [f for f in findings if not f.low_confidence]
    low = [f for f in findings if f.low_confidence]

    def show(items, with_hint):
        width = max(len(f"{f.path}:{f.line}") for f in items)
        for f in items:
            where = f"{f.path}:{f.line}"
            hint = f" {s.dim}({f.hint}){s.reset}" if with_hint and f.hint else ""
            print(f"{s.cyan}{where:<{width}}{s.reset}  {s.yellow}{f.message}{s.reset}{hint}")

    if high:
        show(high, False)
    if low:
        if show_low:
            print(f"\n{s.bold}Possibly unused (low confidence):{s.reset}")
            show(low, True)
        else:
            print(f"{s.dim}{len(low)} possibly unused symbol(s) not shown "
                  f"(decorated, entry points or inherited methods); use --all to see them.{s.reset}")
    kinds = Counter(f.kind for f in high)
    parts = []
    for kind, word in (("unused-symbol", "unused symbol"), ("unused-import", "unused import"),
                       ("unreachable", "unreachable block")):
        if kinds[kind]:
            parts.append(f"{kinds[kind]} {word}{'s' if kinds[kind] != 1 else ''}")
    return high, parts


def cmd_tree(args, project, graph, s, chars):
    roots = [find_node(graph, project, args.root, args.packages)] if args.root else None
    detail = "uses" if args.uses else "where" if args.where else None
    for line in render_tree(graph, project, roots, args.reverse, detail, args.depth, chars, s):
        print(line)
    return 0


def cmd_diagram(args, project, graph, s, chars):
    if args.root:
        root = find_node(graph, project, args.root, args.packages)
        graph = graph.subgraph(reachable(graph.out, root))
    contents = box_contents(project, graph, args.packages) if args.functions else {}
    lines, upward = render_diagram(graph, contents, chars, s)
    for line in lines:
        print(line)
    print()
    print(f"{s.dim}Arrows point to the imported module.{s.reset}")
    if upward:
        print(f"{s.red}Cycle edges drawn upwards ({chars['up']}):{s.reset} "
              + ", ".join(f"{u} -> {v}" for u, v in upward))
    return 0


def cmd_cycles(args, project, graph, s, chars):
    cycles = find_cycles(graph)
    if not cycles:
        print(f"{s.bold}No cycles found.{s.reset}")
        return 0
    print_cycles(cycles, graph, project, s)
    print(f"{s.bold}{len(cycles)} cycle{'s' if len(cycles) != 1 else ''} found.{s.reset}")
    return 1


def cmd_unused(args, project, graph, s, chars):
    high, parts = print_findings(collect_findings(project), args.all, s)
    if not high:
        print(f"{s.bold}No unused code found.{s.reset}")
        return 0
    print(f"\n{s.bold}Summary:{s.reset} " + ", ".join(parts))
    return 1


def cmd_check(args, project, graph, s, chars):
    cycles = find_cycles(graph)
    print(f"{s.bold}Cycles{s.reset}")
    if cycles:
        print_cycles(cycles, graph, project, s)
    else:
        print("No cycles found.\n")
    print(f"{s.bold}Unused code{s.reset}")
    high, parts = print_findings(collect_findings(project), False, s)
    if not high:
        print("No unused code found.")
    summary = [f"{len(cycles)} cycle{'s' if len(cycles) != 1 else ''}"] + parts
    print(f"\n{s.bold}Summary:{s.reset} " + ", ".join(summary))
    return 1 if cycles or high else 0


def parse_args(argv):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or (argv[0] not in COMMANDS and argv[0] not in ("-h", "--help")):
        argv.insert(0, "tree")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("path", nargs="?", default=".",
                        help="project directory (default: current directory)")
    common.add_argument("--lang", choices=["auto"] + sorted(ANALYZERS), default="auto",
                        help="source language (default: auto)")
    common.add_argument("--packages", action="store_true",
                        help="group modules by package")
    common.add_argument("--external", action="store_true",
                        help="show third-party packages")
    common.add_argument("--exclude-lazy", action="store_true",
                        help="ignore imports made inside functions or only for type checking")
    common.add_argument("--color", choices=["auto", "always", "never"], default="auto",
                        help="use colors (default: auto)")
    common.add_argument("--no-color", action="store_true", help="same as --color never")
    common.add_argument("--ascii", action="store_true",
                        help="draw with plain ASCII characters instead of Unicode")
    common.add_argument("-i", "--ignore", action="append", default=[], metavar="PATTERN",
                        help="glob pattern of files or folders to ignore (repeatable)")
    common.add_argument("--no-default-ignores", action="store_true",
                        help="do not ignore .git, venv, build, etc.")

    ap = argparse.ArgumentParser(
        prog="megadeps",
        description="Analyze the dependencies between the modules of a project.",
        epilog="Without a command, 'tree' is used. "
               "Exit code: 0 = ok, 1 = problems found, 2 = error.")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("tree", parents=[common], help="print the dependency tree (default)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--uses", action="store_true", help="also show which symbols are used")
    mode.add_argument("--where", action="store_true", help="also show where each symbol is used")
    p.add_argument("--root", metavar="MODULE", help="start the tree at this module")
    p.add_argument("--reverse", action="store_true",
                   help="show who depends on each module instead")
    p.add_argument("--depth", type=int, default=0, metavar="N", help="maximum depth")

    p = sub.add_parser("diagram", parents=[common], help="draw a diagram with boxes and arrows")
    p.add_argument("--functions", action="store_true",
                   help="list the functions and classes inside each box")
    p.add_argument("--root", metavar="MODULE",
                   help="only draw this module and what it depends on")

    sub.add_parser("cycles", parents=[common], help="report circular dependencies")

    p = sub.add_parser("unused", parents=[common],
                       help="report unused functions, classes, imports and unreachable code")
    p.add_argument("--all", action="store_true", help="also show low-confidence results")

    sub.add_parser("check", parents=[common], help="cycles + unused code, for CI")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not os.path.isdir(args.path):
        print(f"megadeps: '{args.path}' is not a directory", file=sys.stderr)
        return 2

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

    mode = "never" if args.no_color else args.color
    if mode == "auto":
        use_color = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    else:
        use_color = mode == "always"
    if use_color and not enable_windows_ansi() and mode == "auto":
        use_color = False
    s = Style(use_color)
    chars = ASCII_CHARS if args.ascii else UNICODE_CHARS

    patterns = list(args.ignore)
    if not args.no_default_ignores:
        patterns += DEFAULT_IGNORES

    analyzer = pick_analyzer(args.path, patterns, args.lang)
    if analyzer is None:
        print(f"megadeps: no source files found in '{args.path}'", file=sys.stderr)
        return 2
    project = analyzer.analyze(args.path, patterns)
    for error in project.errors:
        print(f"megadeps: warning: could not parse {error}", file=sys.stderr)

    graph = build_graph(project, lazy=not args.exclude_lazy, external=args.external,
                        grouped=args.packages)
    command = {"tree": cmd_tree, "diagram": cmd_diagram, "cycles": cmd_cycles,
               "unused": cmd_unused, "check": cmd_check}[args.command]
    try:
        return command(args, project, graph, s, chars)
    except ValueError as exc:
        print(f"megadeps: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        # e.g. "megadeps tree | head"
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
        sys.exit(1)
