#!/usr/bin/env python3
"""Pre-emission semantic checker.

Catches Lam-level mistakes before the Go transpiler runs, producing
errors mapped back to the original .lam source. Each check is
conservative: ambiguous cases are *skipped* rather than flagged so the
checker only ever surfaces high-confidence violations.

Currently covered:

- **Undefined names** in expression position, with a "did you mean"
  suggestion when an existing in-scope name is within edit distance.
  The checker collects every name defined anywhere in a scope
  (assignments, parameters, for-targets, ``with ... as`` clauses,
  comprehensions, except bindings, generic type parameters, top-level
  imports/funcs/classes) and then flags references that don't resolve
  against builtins, imported modules, or the scope chain.
- **Duplicate top-level declarations**: two classes / interfaces with
  the same name, or two non-overloaded functions with the same name
  and arity.
- **Duplicate class members**: a class can't declare the same field
  or method name twice in the same body.
- **Misplaced flow statements**: ``break`` / ``continue`` outside a
  loop, ``return`` outside a function.
- **Shadowing builtins / imported modules**: assigning to ``print``,
  ``len``, or a just-imported ``lamstrings`` is almost always a bug,
  so the checker flags it as an error. (Parameter-level shadowing is
  intentionally allowed because of how often real code uses names
  like ``type``.)
- **Unreachable code** after an unconditional ``return`` / ``break``
  / ``continue`` / ``raise`` at statement level.
- **Go-only reserved identifiers**: defining a variable / parameter whose name
  is a Go keyword (``chan``, ``defer``, ``select``, …) would generate a Go
  build error whose line points at synthetic Go output; the checker surfaces it
  at the .lam location instead. Declaration names that lower to safe Go symbols
  are allowed, such as class methods named ``select`` lowering to ``Select``.

The checker walks the parse tree directly without invoking the
transpiler, so it's cheap to run on every build.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from difflib import get_close_matches
from pathlib import Path
from typing import Iterable, List, Optional, Set

from lark import Tree, Token

from compiler.ast_builder import build_module
from compiler.ast_nodes import ClassDecl, FuncDecl, ImportDecl, InterfaceDecl, Module, VarDecl
from compiler.constants import DUNDER_OPS, OP_TO_DUNDER, PYTHON_EXCEPTIONS
from compiler.diagnostics import (
    Diagnostic,
    DiagnosticSeverity,
    SourceSpan,
    semantic_code,
)
from compiler.modules import WorkspaceIndex
from compiler.typesys import (
    DictType,
    FuncType,
    GenericType,
    ListType,
    NamedType,
    Type,
    TypeParseError,
    UnionType,
    is_assignable,
    parse_type,
    render_type,
)


# F-strings are parsed as a single ``FSTRING`` token (the grammar
# treats the whole literal opaquely), so the expression walker
# below never visits the identifiers inside ``{...}`` slots.
# The pattern below matches every Python-style identifier so the
# unused-binding pass can mark referenced names regardless.
_FSTRING_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GO_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_GO_SELF_RE = re.compile(r"\bself\b")
_GO_RECEIVER_ALIAS_RE = re.compile(r"\bs\s*\.")
_GO_LOCAL_S_DECL_RE = re.compile(r"(?:^|[;{\n])\s*(?:var\s+s\b|s\s*:=)")

_ANSI = {
    "red": "\033[31m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def _color(text: str, color: str) -> str:
    return f"{_ANSI.get(color, '')}{text}{_ANSI['reset']}"


def _diagnostic_color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("LAM_COLOR", "").lower() in {"1", "true", "always"}:
        return True
    if os.environ.get("LAM_COLOR", "").lower() in {"0", "false", "never"}:
        return False
    return sys.stderr.isatty()


# ─── Go-only reserved identifiers ────────────────────────────────
#
# These are keywords in Go but not in Lammergeier, so nothing stops
# the user from naming a variable ``chan`` or ``defer`` today. The
# transpiler would happily emit it and the Go compiler would then
# spit out a "syntax error: unexpected defer" message whose file /
# line points at generated Go rather than the user's source. The
# semantic checker flags this up front with a diagnostic that
# references the *.lam* location.
#
# We intentionally do NOT include identifiers that are keywords in
# *both* languages (``if``, ``for``, ``return``, …) — Lam's parser
# rejects those already. The set below is the pure-Go delta.
GO_ONLY_KEYWORDS: Set[str] = {
    "chan", "defer", "fallthrough", "go", "goto", "select",
    "switch", "package",
    # Predeclared identifiers that, while not strictly reserved,
    # still cause nasty shadowing bugs if re-bound at Lam level
    # because the transpiler emits them verbatim.
    "iota", "any",
}

COMPILER_RESERVED_PREFIXES: tuple[str, ...] = ("__lam", "__q")

BUILTIN_TYPES: Set[str] = {
    "None", "any", "bool", "str", "string", "bytes", "int", "int8",
    "int16", "int32", "int64", "uint", "uint8", "uint16", "uint32",
    "uint64", "float", "float32", "float64", "byte", "rune",
    "list", "dict", "set", "tuple", "func", "Result", "Option", "File",
    "Error",
}

LAM_KEYWORDS: Set[str] = {
    "and", "as", "async", "await", "break", "case", "catch", "class",
    "const", "continue", "defer", "del", "do", "elif", "else", "finally",
    "for", "from", "func", "global", "if", "import", "in", "interface",
    "is", "lambda", "match", "nonlocal", "not", "or", "pass", "private",
    "raise", "return", "static", "throw", "try", "while", "with", "yield",
}


# ─── Builtins ────────────────────────────────────────────────────
#
# Names recognised by the transpiler as bare-call builtins (see
# ``compiler/visitors/expressions.py``) plus type names that double as
# constructors and the handful of stdlib modules / classes whose
# capitalisation clashes with user names.

BUILTIN_FUNCS: Set[str] = {
    # I/O
    "print", "input", "open", "exit",
    # Containers / iteration
    "len", "range", "enumerate", "sorted", "reversed",
    "append", "format",
    # Type constructors / casts
    "str", "string", "int", "float", "bool", "bytes",
    "int8", "int16", "int32", "int64", "uint", "uint8", "uint16",
    "uint32", "uint64", "float32", "float64", "byte", "rune",
    "list", "dict", "set", "tuple",
    # Reflection / runtime
    "type", "isinstance", "repr", "abs", "max", "min",
    # Go-flavoured passthroughs
    "panic", "recover", "make", "new", "cap", "copy", "delete", "close",
    # Internal sentinels (transpiler-injected)
    "__go_block__", "__go_inline__",
}

BUILTIN_CONSTANTS: Set[str] = {
    "None", "True", "False", "self", "cls",
    # ``LAMMERGEIER.<name>`` is a stable namespace for compiler /
    # user-defined names accessible from inside ``go!`` blocks (and,
    # after the post-parse pass, from regular Lam code too — the
    # transpiler resolves the tail at emit time). Register the bare
    # ``LAMMERGEIER`` identifier so attribute access on it doesn't
    # trip the undefined-name guard.
    "LAMMERGEIER",
    # Lowercase aliases: the transpiler emits them verbatim and Go
    # accepts them as boolean/nil literals, so existing code uses
    # them interchangeably with the capitalised forms.
    "true", "false", "nil", "null",
    # Loop / discard sentinel
    "_",
}

INTEGER_CAST_FUNCS: Set[str] = {
    "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64",
    "byte", "rune",
}

FLOAT_CAST_FUNCS: Set[str] = {"float", "float32", "float64"}

BUILTIN_CAST_FUNCS: Set[str] = {
    "str", "string", "bool", *INTEGER_CAST_FUNCS, *FLOAT_CAST_FUNCS,
}

# Standard-library module names that get auto-imported via ``from
# lamX import Foo``. We don't enforce that the imported class actually
# exists in the module — the transpile pass already handles missing
# imports by failing loudly — but we register the module *prefix*
# so attribute access (``lamtime.Time.now()``) doesn't false-positive.
STDLIB_MODULES: Set[str] = {
    # Core / numeric / data
    "lammath", "lamstrings", "lamtime", "lamconv", "lamos",
    "lamre", "lamjson", "lamhttp", "lamrandom", "lamhash",
    "lampath", "lamsort", "lamstats", "lamsys", "lamenv",
    "lamlog", "lamcsv", "lamerr", "lamerrors", "lamfmt",
    "lamtest", "lamtypes", "lamio", "lamnet", "lamsecurity",
    "lamunicode", "lamurl", "lambase64", "lamcompress",
    "lamcrypto", "lamdatetime", "lamarray", "lamdata",
    "lamiter", "lamuuid", "lamcache", "lambytes",
    "lamtemplate", "lamratelimit", "lamretry", "lamexec",
    # Collections / concurrency
    "lamset", "lamqueue", "lamstack", "lamheap", "lamdeque",
    "lamcollections", "lamfunctools", "lamitertools",
    "lamthreading", "lamasync", "lamconcurrency", "lamactor",
    # Database / migrations / messaging
    "lamdb", "lammigrate", "lamredis", "lamemcached",
    "lamcron", "lamsmtp",
    # Web stack
    "lamserver", "lamserver_ws", "lamserver_plugins",
    "lamserver_tus", "lamschema", "lamjwt", "lamprotobuf",
    "lamcli",
}


# ─── Error model ─────────────────────────────────────────────────


@dataclass
class SemanticError:
    """A single semantic violation, location-tagged for printing.

    ``severity`` is ``"error"`` (default — aborts the build) or
    ``"warning"`` (advisory — printed but does not abort). The
    distinction lets the checker surface lint-class issues like
    *unused import* / *unused parameter* without blocking
    transpilation.
    """

    line: int
    col: int
    message: str
    kind: str  # "undefined" | "duplicate" | "flow" | "unused" | …
    severity: str = "error"

    @property
    def is_warning(self) -> bool:
        return self.severity == "warning"

    def to_diagnostic(self, path: str | os.PathLike[str] | None = None) -> Diagnostic:
        return Diagnostic(
            code=semantic_code(self.kind),
            severity=DiagnosticSeverity.WARNING if self.is_warning else DiagnosticSeverity.ERROR,
            message=self.message,
            span=SourceSpan(
                file=Path(path) if path else None,
                line=max(1, self.line),
                col=max(1, self.col),
            ),
        )

    def format(self, source_lines: List[str], path: str, *, color: bool = False) -> str:
        """Render this error with a 3-line source snippet."""
        severity = "warning" if self.is_warning else "error"
        tag = f"{severity}[{self.kind}]"
        if color:
            tag = _color(tag, "yellow" if self.is_warning else "red")
        lines = [f"  line {self.line}: {tag}: {self.message}"]
        start = max(0, self.line - 2)
        end = min(len(source_lines), self.line + 1)
        for i in range(start, end):
            marker = ">>>" if i == self.line - 1 else "   "
            if color and marker == ">>>":
                marker = _color(marker, "cyan")
            lines.append(f"    {marker} {i + 1:4d} | {source_lines[i]}")
        return "\n".join(lines)


# ─── Checker ─────────────────────────────────────────────────────


@dataclass
class _Scope:
    """Names visible at one lexical level.

    ``kind`` distinguishes module / function / class / block scopes so
    statements like ``return`` and ``break`` know whether they're
    legal. ``is_loop`` is set on the for/while body scope.
    ``const_names`` is the subset of ``names`` declared via ``const``;
    any later assignment that targets one of these is rejected.
    ``used_names`` is populated by :meth:`SemanticChecker._is_resolved`
    every time a reference resolves against this scope; the unused-
    binding warnings cross-reference it against ``names`` at scope
    pop time. ``param_nodes`` is the subset of names introduced as
    function parameters along with the parse-tree node that carries
    their location, so an unused-parameter warning can point at the
    parameter itself rather than at the function header.
    """

    kind: str
    names: Set[str] = field(default_factory=set)
    const_names: Set[str] = field(default_factory=set)
    used_names: Set[str] = field(default_factory=set)
    definite_names: Set[str] = field(default_factory=set)
    param_nodes: dict = field(default_factory=dict)  # name -> Tree/Token
    var_types: dict = field(default_factory=dict)  # name -> type root
    is_loop: bool = False
    # Only meaningful on ``function`` scopes: True when the function
    # declares ``-> Result`` (or an equivalent that accepts the ``?``
    # propagation operator's ``*Result`` return value). Consulted by
    # the ``?``-in-non-Result warning so nested ``func`` / ``lambda``
    # scopes search the nearest enclosing function scope and not the
    # module.
    returns_result: bool = False
    binding_nodes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class _CallableSig:
    name: str
    required_pos: int
    max_pos: Optional[int]
    params: tuple[str, ...]
    accepts_kwargs: bool = False
    param_types: tuple[str, ...] = ()
    generic_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class _MethodShape:
    name: str
    param_types: tuple[str, ...]
    return_type: str
    param_type_texts: tuple[str, ...] = ()
    return_type_text: str = ""


@dataclass
class _ImportedCallMetadata:
    func_sigs: dict[str, list[_CallableSig]] = field(default_factory=dict)
    func_return_types: dict[str, set[str]] = field(default_factory=dict)
    func_return_type_texts: dict[str, set[str]] = field(default_factory=dict)
    method_return_type_texts: dict[str, set[str]] = field(default_factory=dict)
    method_sigs: dict[str, list[_CallableSig]] = field(default_factory=dict)
    interface_methods: dict[str, dict[str, _MethodShape]] = field(default_factory=dict)
    private_functions: set[str] = field(default_factory=set)
    constructor_sigs: dict[str, list[_CallableSig]] = field(default_factory=dict)
    class_members: dict[str, set[str]] = field(default_factory=dict)
    class_fields: dict[str, set[str]] = field(default_factory=dict)
    class_static_methods: dict[str, set[str]] = field(default_factory=dict)
    class_instance_methods: dict[str, set[str]] = field(default_factory=dict)
    class_method_shapes: dict[str, dict[str, _MethodShape]] = field(default_factory=dict)
    class_bases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    class_private_methods: dict[str, set[str]] = field(default_factory=dict)


class SemanticChecker:
    """Walks a parsed Lam tree and accumulates :class:`SemanticError`."""

    def __init__(
        self,
        *,
        extra_known_names: Optional[Iterable[str]] = None,
        go_blocks: Optional[dict[str, str]] = None,
        imported_func_sigs: Optional[dict[str, list[_CallableSig]]] = None,
        imported_func_return_types: Optional[dict[str, set[str]]] = None,
        imported_func_return_type_texts: Optional[dict[str, set[str]]] = None,
        imported_method_return_type_texts: Optional[dict[str, set[str]]] = None,
        imported_method_sigs: Optional[dict[str, list[_CallableSig]]] = None,
        imported_interface_methods: Optional[dict[str, dict[str, _MethodShape]]] = None,
        imported_private_functions: Optional[Iterable[str]] = None,
        imported_constructor_sigs: Optional[dict[str, list[_CallableSig]]] = None,
        imported_class_members: Optional[dict[str, set[str]]] = None,
        imported_class_fields: Optional[dict[str, set[str]]] = None,
        imported_class_static_methods: Optional[dict[str, set[str]]] = None,
        imported_class_instance_methods: Optional[dict[str, set[str]]] = None,
        imported_class_method_shapes: Optional[dict[str, dict[str, _MethodShape]]] = None,
        imported_class_bases: Optional[dict[str, tuple[str, ...]]] = None,
        imported_class_private_methods: Optional[dict[str, set[str]]] = None,
    ):
        # Names provided externally — typically from imported libraries
        # whose own top-level definitions the caller has already parsed.
        self._extra_known: Set[str] = set(extra_known_names or ())
        self.errors: List[SemanticError] = []
        self._scopes: List[_Scope] = []
        # Top-level import bindings — collected during pre-scan, then
        # cross-referenced with the module scope's ``used_names`` at
        # the end of :meth:`check` to surface unused-import warnings.
        # Each entry is ``(local_binding, source_node)``. The node
        # supplies the location for the diagnostic.
        self._import_records: List[tuple] = []
        # Functions / methods declared in this file whose signature
        # promises a non-void return. Cross-referenced when an
        # expression statement drops the call's value, so the
        # ``dropped-return-value`` warning only fires on call sites
        # where we actually know something will be returned. Populated
        # during the top-level collection pass (see
        # ``_collect_module_defs``).
        self._nonvoid_funcs: Set[str] = set()
        # ``"Class.method"`` keys for non-void methods and static
        # methods. Covers both kinds uniformly; we only need the flag,
        # not the dispatch kind.
        self._nonvoid_methods: Set[str] = set()
        self._func_sigs: dict[str, list[_CallableSig]] = {
            name: list(sigs)
            for name, sigs in (imported_func_sigs or {}).items()
        }
        self._func_return_types: dict[str, set[str]] = {
            name: set(types)
            for name, types in (imported_func_return_types or {}).items()
        }
        self._func_return_type_texts: dict[str, set[str]] = {}
        self._method_return_type_texts: dict[str, set[str]] = {
            name: set(types)
            for name, types in (imported_method_return_type_texts or {}).items()
        }
        for name, types in (imported_func_return_type_texts or {}).items():
            self._func_return_type_texts[name] = set(types)
        for name, return_types in self._func_return_types.items():
            if any(type_name and type_name != "None" for type_name in return_types):
                if "." in name:
                    self._nonvoid_methods.add(name)
                else:
                    self._nonvoid_funcs.add(name)
        self._method_sigs: dict[str, list[_CallableSig]] = {
            name: list(sigs)
            for name, sigs in (imported_method_sigs or {}).items()
        }
        self._external_private_functions: set[str] = set(imported_private_functions or ())
        self._constructor_sigs: dict[str, list[_CallableSig]] = {
            name: list(sigs)
            for name, sigs in (imported_constructor_sigs or {}).items()
        }
        self._class_members: dict[str, set[str]] = {
            name: set(members)
            for name, members in (imported_class_members or {}).items()
        }
        self._class_fields: dict[str, set[str]] = {
            name: set(fields)
            for name, fields in (imported_class_fields or {}).items()
        }
        self._class_static_methods: dict[str, set[str]] = {
            name: set(methods)
            for name, methods in (imported_class_static_methods or {}).items()
        }
        self._class_instance_methods: dict[str, set[str]] = {
            name: set(methods)
            for name, methods in (imported_class_instance_methods or {}).items()
        }
        self._class_private_methods: dict[str, set[str]] = {}
        self._external_class_private_methods: dict[str, set[str]] = {
            name: set(methods)
            for name, methods in (imported_class_private_methods or {}).items()
        }
        self._current_class_stack: list[str] = []
        self._interface_methods: dict[str, dict[str, _MethodShape]] = {
            name: dict(methods)
            for name, methods in (imported_interface_methods or {}).items()
        }
        self._class_method_shapes: dict[str, dict[str, _MethodShape]] = {
            name: dict(methods)
            for name, methods in (imported_class_method_shapes or {}).items()
        }
        self._class_bases: dict[str, tuple[str, ...]] = {
            name: tuple(bases)
            for name, bases in (imported_class_bases or {}).items()
        }
        self._class_base_aliases: dict[str, dict[str, str]] = {}
        self._class_type_params: dict[str, tuple[str, ...]] = {}
        for class_name, methods in self._class_method_shapes.items():
            for method_name, shape in methods.items():
                if shape.return_type:
                    self._nonvoid_methods.add(f"{class_name}.{method_name}")
        self._func_param_types: dict[str, list[tuple[str, ...]]] = {}
        self._ast_classes: dict[str, ClassDecl] = {}
        self._go_blocks: dict[str, str] = dict(go_blocks or {})
        self._has_lamerrors_result_import = False
        self._has_aliased_lamerrors_result_import = False

    # ─── Public API ────────────────────────────────────────────

    def check(self, tree: Tree, *, ast_module: Module | None = None) -> List[SemanticError]:
        """Run all checks against the parse tree and return any errors."""
        # Module scope holds top-level function names, class names,
        # and import bindings. We collect these up front so forward
        # references across the file don't false-positive.
        self._has_lamerrors_result_import = False
        self._has_aliased_lamerrors_result_import = False
        module = _Scope(kind="module")
        module.names |= (BUILTIN_FUNCS | BUILTIN_CONSTANTS | STDLIB_MODULES
                         | PYTHON_EXCEPTIONS)
        module.names |= self._extra_known
        self._import_records = []
        self._collect_module_defs(tree, module, ast_module=ast_module)
        module.definite_names |= set(module.names)
        self._scopes = [module]
        try:
            self._walk_suite_stmts(self._suite_stmts(tree))
            # Top-level unused-import warnings, emitted after the
            # full walk so every reference has had a chance to mark
            # ``module.used_names``. Names whose binding is the
            # underscore (``from x import _``) are an explicit
            # opt-out so we never warn on them.
            self._emit_unused_import_warnings(module)
        finally:
            self._scopes = []
        return self.errors

    # ─── Scope collection ─────────────────────────────────────

    def _collect_module_defs(
        self,
        tree: Tree,
        scope: _Scope,
        *,
        ast_module: Module | None = None,
    ) -> None:
        """Pre-scan the top level for funcs/classes/imports.

        Additionally flags **duplicate top-level declarations** —
        two classes, interfaces, or imports with the same name, and
        two functions sharing the same name *and* arity (Lam allows
        function overloading by arity, so ``func f(x)`` and ``func
        f(x, y)`` must coexist without a diagnostic; only same-arity
        redefinitions count as duplicates).

        Imports and top-level ``const`` / ``assign`` are also
        tracked so a user can't shadow an imported module with a
        later assignment — that's checked in :meth:`_visit_stmt`
        via :attr:`_module_seen`.
        """
        # Track every kind+arity tuple seen at the top level so we
        # can flag a second definition. The value is the *node*
        # whose location we'd cite as the "original" if needed.
        self._module_seen: dict = {}   # name → ("import"|"class"|"func"), used by shadow check
        self._ast_classes = {
            decl.name: decl
            for decl in (ast_module.body if ast_module is not None else [])
            if isinstance(decl, ClassDecl)
        }
        for child in tree.children:
            if not isinstance(child, Tree):
                continue
            self._collect_top_decl_metadata(child)
        if ast_module is not None:
            self._collect_top_decl_with_ast(ast_module, scope)
            return

        seen_func: dict = {}   # (name, arity) → node
        seen_class: dict = {}  # name → node
        seen_import: Set[str] = set()
        seen_go_symbol: dict[str, tuple[str, str]] = {}
        for child in tree.children:
            if isinstance(child, Tree):
                self._collect_top_decl_with_dupe_check(
                    child, scope, seen_func, seen_class, seen_import, seen_go_symbol,
                )

    def _collect_top_decl_with_ast(self, module: Module, scope: _Scope) -> None:
        seen_func: dict = {}
        seen_class: dict = {}
        seen_import: Set[str] = set()
        seen_go_symbol: dict[str, tuple[str, str]] = {}
        for decl in module.body:
            if isinstance(decl, FuncDecl):
                name = decl.name
                if not name:
                    continue
                if self._module_seen.get(name) == "import":
                    self._error(
                        decl.span, "import",
                        f"function `{name}` conflicts with an imported binding of the same name",
                    )
                scope.names.add(name)
                sig = tuple(param.type_ref.name if param.type_ref is not None else "any"
                            for param in decl.params)
                key = (name, len(decl.params), sig)
                if key in seen_func:
                    arity = len(decl.params)
                    self._error(
                        decl.span, "duplicate",
                        f"duplicate function `{name}` with {arity} "
                        f"parameter{'s' if arity != 1 else ''} of the "
                        f"same type signature (overloading requires "
                        f"a different arity or different parameter types)",
                    )
                else:
                    seen_func[key] = decl
                self._record_go_symbol_collision(
                    decl.span,
                    seen_go_symbol,
                    "function",
                    name,
                    self._go_function_symbol(name),
                )
                self._module_seen[name] = "func"
            elif isinstance(decl, ClassDecl):
                name = decl.name
                if not name:
                    continue
                if self._module_seen.get(name) == "import":
                    self._error(
                        decl.span, "import",
                        f"class `{name}` conflicts with an imported binding of the same name",
                    )
                scope.names.add(name)
                if name in seen_class:
                    self._error(decl.span, "duplicate", f"duplicate class `{name}`")
                else:
                    seen_class[name] = decl
                self._record_go_symbol_collision(
                    decl.span,
                    seen_go_symbol,
                    "class",
                    name,
                    self._go_public_name(name),
                )
                self._module_seen[name] = "class"
            elif isinstance(decl, InterfaceDecl):
                name = decl.name
                if not name:
                    continue
                if self._module_seen.get(name) == "import":
                    self._error(
                        decl.span, "import",
                        f"interface `{name}` conflicts with an imported binding of the same name",
                    )
                scope.names.add(name)
                if name in seen_class:
                    self._error(
                        decl.span,
                        "duplicate",
                        f"duplicate interface `{name}` "
                        f"(conflicts with an earlier class/interface)",
                    )
                else:
                    seen_class[name] = decl
                self._record_go_symbol_collision(
                    decl.span,
                    seen_go_symbol,
                    "interface",
                    name,
                    self._go_public_name(name),
                )
                self._module_seen[name] = "class"
            elif isinstance(decl, ImportDecl):
                for binding in decl.bindings:
                    name = binding.name
                    self._check_go_reserved(binding.span, name)
                    scope.names.add(name)
                    self._check_import_binding_conflict(binding.span, name)
                    if name in seen_import:
                        self._error(binding.span, "duplicate", f"duplicate import `{name}`")
                    else:
                        seen_import.add(name)
                    self._module_seen[name] = "import"
                    if name != "_":
                        self._import_records.append((name, binding.span))
            elif isinstance(decl, VarDecl):
                name = decl.name
                if not name:
                    continue
                scope.names.add(name)
                if decl.is_const:
                    if self._module_seen.get(name) == "import":
                        self._error(
                            decl.span, "import",
                            f"constant `{name}` conflicts with an imported binding of the same name",
                        )
                    self._module_seen[name] = "const"

    def _record_go_symbol_collision(
        self,
        node,
        seen_go_symbol: dict[str, tuple[str, str]],
        kind: str,
        name: str,
        go_symbol: str,
    ) -> None:
        if not name or not go_symbol:
            return
        previous = seen_go_symbol.get(go_symbol)
        if previous is None:
            seen_go_symbol[go_symbol] = (kind, name)
            return
        prev_kind, prev_name = previous
        if prev_name == name and prev_kind == kind:
            return
        self._error(
            node,
            "name",
            f"{prev_kind} `{prev_name}` and {kind} `{name}` both lower to Go symbol `{go_symbol}`",
        )

    @staticmethod
    def _go_function_symbol(name: str) -> str:
        return "main" if name == "main" else SemanticChecker._go_public_name(name)

    @staticmethod
    def _go_public_name(name: str) -> str:
        if not name:
            return name
        if name.startswith("__") and name.endswith("__"):
            clean = name.strip("_")
            return clean[0].upper() + clean[1:] if clean else name
        if name.startswith("_"):
            return name
        return name[0].upper() + name[1:]

    def _collect_top_decl_metadata(self, node: Tree) -> None:
        d = node.data
        if d == "funcdef":
            name = self._funcdef_name(node)
            if name:
                if self._funcdef_has_nonvoid_return(node):
                    self._nonvoid_funcs.add(name)
                sig = self._func_signature(node, name)
                if sig is not None:
                    self._func_sigs.setdefault(name, []).append(sig)
                return_type = self._funcdef_return_type_root(node)
                if return_type:
                    self._func_return_types.setdefault(name, set()).add(return_type)
                return_type_text = self._funcdef_return_type_text(node)
                if return_type_text:
                    self._func_return_type_texts.setdefault(name, set()).add(return_type_text)
                self._func_param_types.setdefault(name, []).append(
                    self._param_root_types(self._funcdef_params(node), skip_self=False)
                )
        elif d == "classdef":
            name = self._classdef_name(node)
            if name:
                self._class_type_params[name] = tuple(self._funcdef_type_params(node))
                self._class_bases[name] = tuple(self._classdef_base_names(node))
                self._class_base_aliases[name] = self._classdef_base_aliases(node)
                self._collect_class_members(name, node)
                self._collect_class_method_shapes(name, node)
                self._collect_constructor_sigs(name, node)
                self._collect_nonvoid_methods(name, node)
        elif d == "interfacedef":
            if node.children and isinstance(node.children[0], Tree):
                name = self._name_text(node.children[0])
                if name:
                    self._interface_methods[name] = self._collect_interface_methods(node)
        elif d in ("import_from", "import_name"):
            self._record_support_import(node)
        elif d == "decorated":
            for child in node.children:
                if isinstance(child, Tree) and child.data in {"classdef", "funcdef"}:
                    self._collect_top_decl_metadata(child)
        elif d in {"simple_stmt", "import_stmt"}:
            for child in node.children:
                if isinstance(child, Tree):
                    self._collect_top_decl_metadata(child)

    def _collect_top_decl_with_dupe_check(
        self,
        node: Tree,
        scope: _Scope,
        seen_func: dict,
        seen_class: dict,
        seen_import: Set[str],
        seen_go_symbol: dict[str, tuple[str, str]],
    ) -> None:
        """Mirror of :meth:`_collect_top_decl` that also emits
        duplicate-decl diagnostics. We keep the original as a
        separate method so other collection sites (e.g. nested
        ``simple_stmt``) can reuse the silent collector when a
        dupe check doesn't apply."""
        d = node.data
        if d == "funcdef":
            name = self._funcdef_name(node)
            if name:
                if self._module_seen.get(name) == "import":
                    self._error(
                        node, "import",
                        f"function `{name}` conflicts with an imported binding of the same name",
                    )
                scope.names.add(name)
                params_node = self._funcdef_params(node)
                arity = len(self._param_names(params_node))
                # Dupe key is ``(name, arity, type-signature)`` so two
                # definitions at the same arity whose parameter types
                # differ (``foo(int)`` vs ``foo(string)``) are accepted
                # as overloads. When the user didn't annotate every
                # slot, the signature collapses to all-``any`` and the
                # original same-name/same-arity collision rule still
                # fires — that keeps the diagnostic stable for
                # un-annotated code.
                sig = self._param_type_sig(params_node)
                key = (name, arity, sig)
                if key in seen_func:
                    self._error(
                        node, "duplicate",
                        f"duplicate function `{name}` with {arity} "
                        f"parameter{'s' if arity != 1 else ''} of the "
                        f"same type signature (overloading requires "
                        f"a different arity or different parameter types)",
                    )
                else:
                    seen_func[key] = node
                self._record_go_symbol_collision(
                    node,
                    seen_go_symbol,
                    "function",
                    name,
                    self._go_function_symbol(name),
                )
                self._module_seen[name] = "func"
                if self._funcdef_has_nonvoid_return(node):
                    self._nonvoid_funcs.add(name)
                sig = self._func_signature(node, name)
                if sig is not None:
                    self._func_sigs.setdefault(name, []).append(sig)
                return_type = self._funcdef_return_type_root(node)
                if return_type:
                    self._func_return_types.setdefault(name, set()).add(return_type)
                return_type_text = self._funcdef_return_type_text(node)
                if return_type_text:
                    self._func_return_type_texts.setdefault(name, set()).add(return_type_text)
                self._func_param_types.setdefault(name, []).append(
                    self._param_root_types(params_node, skip_self=False)
                )
        elif d == "classdef":
            name = self._classdef_name(node)
            if name:
                if self._module_seen.get(name) == "import":
                    self._error(
                        node, "import",
                        f"class `{name}` conflicts with an imported binding of the same name",
                    )
                scope.names.add(name)
                if name in seen_class:
                    self._error(
                        node, "duplicate",
                        f"duplicate class `{name}`",
                    )
                else:
                    seen_class[name] = node
                self._record_go_symbol_collision(
                    node,
                    seen_go_symbol,
                    "class",
                    name,
                    self._go_public_name(name),
                )
                self._module_seen[name] = "class"
                self._class_type_params[name] = tuple(self._funcdef_type_params(node))
                self._class_bases[name] = tuple(self._classdef_base_names(node))
                self._class_base_aliases[name] = self._classdef_base_aliases(node)
                self._collect_class_members(name, node)
                self._collect_class_method_shapes(name, node)
                self._collect_constructor_sigs(name, node)
                # Harvest the class's non-void methods (instance +
                # static) so a dropped-return call on a method with a
                # declared return type warns just like a bare func.
                self._collect_nonvoid_methods(name, node)
        elif d == "interfacedef":
            if node.children and isinstance(node.children[0], Tree):
                n = self._name_text(node.children[0])
                if self._module_seen.get(n) == "import":
                    self._error(
                        node, "import",
                        f"interface `{n}` conflicts with an imported binding of the same name",
                    )
                scope.names.add(n)
                if n in seen_class:
                    self._error(
                        node, "duplicate",
                        f"duplicate interface `{n}` "
                        f"(conflicts with an earlier class/interface)",
                    )
                else:
                    seen_class[n] = node
                self._record_go_symbol_collision(
                    node,
                    seen_go_symbol,
                    "interface",
                    n,
                    self._go_public_name(n),
                )
                self._module_seen[n] = "class"
                self._interface_methods[n] = self._collect_interface_methods(node)
        elif d == "decorated":
            for c in node.children:
                if isinstance(c, Tree) and c.data in ("classdef", "funcdef"):
                    self._collect_top_decl_with_dupe_check(
                        c, scope, seen_func, seen_class, seen_import, seen_go_symbol,
                    )
        elif d in ("import_from", "import_name"):
            self._record_support_import(node)
            for n in self._import_bindings(node):
                self._check_go_reserved(node, n)
                scope.names.add(n)
                self._check_import_binding_conflict(node, n)
                if n in seen_import:
                    self._error(
                        node, "duplicate",
                        f"duplicate import `{n}`",
                    )
                else:
                    seen_import.add(n)
                self._module_seen[n] = "import"
                # Track for unused-import warnings; the node carries
                # the line/column we'll cite if the binding is never
                # referenced.
                if n != "_":
                    self._import_records.append((n, node))
        elif d == "import_stmt":
            for inner in node.children:
                if isinstance(inner, Tree):
                    self._collect_top_decl_with_dupe_check(
                        inner, scope, seen_func, seen_class, seen_import, seen_go_symbol,
                    )
        elif d in ("assign_stmt", "annassign", "assign"):
            for n in self._assign_targets(node):
                scope.names.add(n)
        elif d == "const_stmt":
            n = self._const_target_name(node)
            if n:
                if self._module_seen.get(n) == "import":
                    self._error(
                        node, "import",
                        f"constant `{n}` conflicts with an imported binding of the same name",
                    )
                scope.names.add(n)
                self._module_seen[n] = "const"
        elif d == "simple_stmt":
            for sub in node.children:
                if isinstance(sub, Tree):
                    self._collect_top_decl_with_dupe_check(
                        sub, scope, seen_func, seen_class, seen_import, seen_go_symbol,
                    )

    def _collect_top_decl(self, node: Tree, scope: _Scope) -> None:
        d = node.data
        if d == "funcdef":
            name = self._funcdef_name(node)
            if name:
                scope.names.add(name)
        elif d == "classdef":
            name = self._classdef_name(node)
            if name:
                scope.names.add(name)
        elif d == "interfacedef":
            if node.children and isinstance(node.children[0], Tree):
                scope.names.add(self._name_text(node.children[0]))
        elif d == "decorated":
            # ``@decorator class/func``
            for c in node.children:
                if isinstance(c, Tree) and c.data in ("classdef", "funcdef"):
                    self._collect_top_decl(c, scope)
        elif d in ("import_from", "import_name"):
            for n in self._import_bindings(node):
                scope.names.add(n)
        elif d == "import_stmt":
            for inner in node.children:
                if isinstance(inner, Tree):
                    self._collect_top_decl(inner, scope)
        elif d in ("assign_stmt", "annassign", "assign"):
            for n in self._assign_targets(node):
                scope.names.add(n)
        elif d == "const_stmt":
            # Register the name so forward references resolve, but
            # *don't* add to ``const_names`` — that flag is set when
            # the visitor reaches the statement, so a redeclaration
            # check can distinguish "first decl" from "duplicate".
            n = self._const_target_name(node)
            if n:
                scope.names.add(n)
        elif d == "simple_stmt":
            for sub in node.children:
                if isinstance(sub, Tree):
                    self._collect_top_decl(sub, scope)

    # ─── Statement walker ──────────────────────────────────────

    def _visit_stmt(self, node) -> None:
        if not isinstance(node, Tree):
            return
        d = node.data

        if d == "funcdef":
            name = self._funcdef_name(node)
            if name:
                self._check_go_reserved_funcdef(node, name)
                self._mark_definite(name)
            self._visit_funcdef(node)
            return
        if d == "classdef":
            name = self._classdef_name(node)
            if name:
                self._check_go_reserved_type_decl(node, name)
                self._mark_definite(name)
            self._visit_classdef(node)
            return
        if d == "decorated":
            self._check_decorators(node)
            for c in node.children:
                if isinstance(c, Tree):
                    self._visit_stmt(c)
            return
        if d == "interfacedef":
            if node.children and isinstance(node.children[0], Tree):
                self._check_go_reserved_type_decl(
                    node.children[0],
                    self._name_text(node.children[0]),
                )
            return  # No expressions inside; nothing to check.

        if d in ("import_from", "import_name"):
            self._record_support_import(node)
            for n in self._import_bindings(node):
                self._mark_definite(n)
            return
        if d == "import_stmt":
            for child in node.children:
                if isinstance(child, Tree):
                    self._visit_stmt(child)
            return

        # Bare assignment-like statements at module/function level may
        # introduce new names — record them before checking expressions
        # so the same line ``x = x + 1`` is accepted (forward use of
        # ``x`` from an enclosing scope).
        if d in ("assign_stmt", "annassign", "assign", "augassign"):
            self._check_assignment_interface_conformance(node)
            self._check_type_annotations(node)
            self._check_assignment_value_types(node)
            self._check_destructuring_shape(node)
            for n in self._assign_targets(node):
                if self._is_const(n):
                    self._error(
                        node, "const",
                        f"cannot reassign constant `{n}`",
                    )
                self._check_shadow_builtin_or_import(node, n)
                if self._inside_class_member_scope():
                    self._check_compiler_reserved_prefix(node, n)
                else:
                    self._check_go_reserved(node, n)
                if self._assignment_declares_current_scope(node, n):
                    self._declare(n)
                    self._record_binding_node(n, node)
            self._record_assignment_types(node)
            self._visit_assignment_values(node)
            for n in self._assign_targets(node):
                self._mark_definite(n)
            return

        if d == "const_stmt":
            self._visit_const_stmt(node)
            return

        if d == "for_stmt":
            self._visit_for(node)
            return
        if d == "while_stmt":
            self._visit_while(node)
            return
        if d == "try_finally":
            # ``try suite finally`` shorthand — same shape minus catch.
            self._visit_try(node)
            return
        if d == "if_stmt":
            self._visit_if(node)
            return
        if d == "match_stmt":
            self._visit_match(node)
            return
        if d == "try_stmt":
            self._visit_try(node)
            return
        if d == "do_stmt":
            self._visit_do(node)
            return
        if d == "with_stmt":
            self._visit_with(node)
            return

        if d in ("break_stmt", "continue_stmt"):
            if not any(s.is_loop for s in self._scopes):
                kw = "break" if d == "break_stmt" else "continue"
                self._error(
                    node, "flow",
                    f"`{kw}` can only be used inside a loop",
                )
            return
        if d == "return_stmt":
            if not any(s.kind == "function" for s in self._scopes):
                self._error(node, "flow", "`return` can only be used inside a function")
            self._visit_expr_subtree(node)
            return

        if d == "expr_stmt":
            # A bare expression statement whose top expression is a
            # call to a function / static method we *know* returns a
            # non-void value is almost always a bug — the caller
            # meant to assign the result somewhere. We emit a
            # warning, not an error, so incremental / REPL-style code
            # still compiles. The opt-out is the standard Lam idiom:
            # ``_ = fn(x)`` (handled as ``assign_stmt``, never reaches
            # this branch).
            self._check_dropped_return(node)
            self._visit_expr_subtree(node)
            return

        if d in ("raise_stmt", "del_stmt", "assert_stmt",
                 "yield_expr", "defer_stmt"):
            self._visit_expr_subtree(node)
            return

        if d == "simple_stmt":
            for sub in node.children:
                self._visit_stmt(sub)
            return

        # Fall-through: still inspect any nested expressions.
        self._visit_expr_subtree(node)

    # ─── Sub-walkers (placeholders, fleshed out in next steps) ──

    def _visit_funcdef(self, node: Tree) -> None:
        # Function bodies get their own scope. Pre-collect every name
        # assigned anywhere in the body so forward references inside
        # the function don't false-positive (Lam permits referring to
        # a local that's introduced later in the same block).
        scope = _Scope(kind="function")
        scope.returns_result = self._funcdef_returns_result(node)
        if self._current_class_stack and not self._funcdef_is_static(node):
            scope.names.add("self")
            scope.names.add("cls")
            scope.definite_names.update({"self", "cls"})
        for tp in self._funcdef_type_params(node):
            scope.names.add(tp)
            scope.definite_names.add(tp)
        params_node = self._funcdef_params(node)
        # Capture parameter -> node map up front so an unused-parameter
        # warning can cite the parameter itself (not the function
        # header). ``_param_node_map`` mirrors ``_param_names`` shape
        # but returns ``{name: node}`` for diagnostic locations.
        param_node_map = self._param_node_map(params_node)
        for p in self._param_names(params_node):
            self._check_go_reserved(param_node_map.get(p, node), p)
            scope.names.add(p)
            scope.definite_names.add(p)
            if p in param_node_map:
                scope.param_nodes[p] = param_node_map[p]
        scope.var_types.update(self._param_type_map(params_node))
        suite_node = self._suite_node(node)
        if suite_node is not None:
            self._collect_block_defs(suite_node, scope)
        self._scopes.append(scope)
        try:
            self._check_method_receiver_declaration(node, suite_node)
            self._check_type_annotations(node)
            if suite_node is not None:
                self._walk_suite_stmts(self._suite_stmts(suite_node))
                self._check_function_returns(node, suite_node)
            self._emit_unused_param_warnings(scope, params_node)
            self._emit_unused_local_warnings(scope)
        finally:
            self._scopes.pop()

    def _is_nested_funcdef(self, node: Tree) -> bool:
        if not self._scopes:
            return False
        return any(scope.kind in {"function", "block"} for scope in self._scopes)

    def _emit_unused_param_warnings(self, scope: _Scope, params_node) -> None:
        """Emit a warning for every function parameter that the body
        never references. Pythonic opt-outs:

        * a leading underscore on the parameter name
          (``def f(_unused, …)``) is the canonical "I know, I want it
          there for shape" marker;
        * a single bare ``_`` is the discard sentinel and is also
          skipped;
        * the implicit ``self`` / ``cls`` receivers never warn.

        Any parameter the body genuinely uses — even via ``_ =
        param`` to silence Go's "declared and not used" — is
        marked through :meth:`_is_resolved` and won't trip this
        check. Interface stub bodies (``func f(x): pass``) still
        get the warning, which is intentional: an interface stub
        belongs in an ``interface`` block, not as an unimplemented
        function.
        """
        for name, pnode in scope.param_nodes.items():
            if name in scope.used_names:
                continue
            if not name or name.startswith("_") or name in ("self", "cls"):
                continue
            self._warning(
                pnode if pnode is not None else params_node,
                "unused",
                f"unused parameter `{name}` "
                f"(prefix with `_` to silence)",
            )

    def _emit_unused_local_warnings(self, scope: _Scope) -> None:
        for name, node in scope.binding_nodes.items():
            if name in scope.used_names:
                continue
            if not name or name == "_" or name.startswith("_"):
                continue
            if name in scope.param_nodes or name in scope.const_names:
                continue
            self._warning(
                node,
                "unused",
                f"unused local `{name}` (prefix with `_` to silence)",
            )

    def _check_method_receiver_declaration(self, node: Tree, suite_node) -> None:
        if not self._current_class_stack:
            return
        class_name = self._current_class_stack[-1]
        method = self._funcdef_name(node)
        if not method:
            return
        params = self._param_names(self._funcdef_params(node))
        first = params[0] if params else ""
        is_static = self._funcdef_is_static(node)
        if is_static:
            if first == "self":
                self._warning(
                    node, "method",
                    f"static method `{class_name}.{method}` should not take `self`; remove `static` or remove the `self` parameter",
                )
            return
        if method in {"init", "__init__"} and first != "self":
            self._warning(
                node, "method",
                f"constructor `{class_name}.{method}` is missing `self` as its first parameter",
            )
            return
        if first != "self" and self._suite_uses_self(suite_node):
            self._warning(
                node, "method",
                f"method `{class_name}.{method}` uses `self` but does not declare `self` as its first parameter",
            )

    def _suite_uses_self(self, node) -> bool:
        if not isinstance(node, Tree):
            return False
        if node.data == "var" and node.children:
            return self._name_text(node.children[0]) == "self"
        if node.data in {"funcdef", "classdef", "lambdef"}:
            return False
        return any(self._suite_uses_self(child) for child in node.children)

    def _visit_classdef(self, node: Tree) -> None:
        # Class scope is mostly a holder for nested method visits — the
        # method-name space is checked separately for duplicates.
        suite_node = self._suite_node(node)

        # Duplicate-member check.
        class_name = self._classdef_name(node)
        ast_class = self._ast_classes.get(class_name)
        seen: Set[str] = set()
        if ast_class is not None:
            for member in [*ast_class.fields, *ast_class.methods]:
                name = member.name
                if name and name in seen:
                    self._error(member.span, "duplicate",
                                f"duplicate class member `{name}`")
                if name:
                    seen.add(name)
        elif suite_node is not None:
            for stmt in self._suite_stmts(suite_node):
                if not isinstance(stmt, Tree):
                    continue
                if stmt.data == "funcdef":
                    name = self._funcdef_name(stmt)
                elif stmt.data in ("annassign", "assign_stmt", "assign"):
                    targets = self._assign_targets(stmt)
                    name = targets[0] if targets else ""
                else:
                    name = ""
                if name and name in seen:
                    self._error(stmt, "duplicate",
                                f"duplicate class member `{name}`")
                if name:
                    seen.add(name)

        if class_name:
            self._check_inherited_class_shape(node, class_name)

        # Register class-level type-parameter names so methods see them
        # even though they aren't in any per-method scope yet.
        scope = _Scope(kind="class")
        scope.names.add("self")
        for alias, base in self._class_base_aliases.get(class_name, {}).items():
            scope.names.add(alias)
            scope.var_types[alias] = base
            scope.definite_names.add(alias)
        for tp in self._funcdef_type_params(node):
            scope.names.add(tp)
        self._current_class_stack.append(class_name)
        self._scopes.append(scope)
        try:
            if suite_node is not None:
                self._walk_suite_stmts(self._suite_stmts(suite_node))
        finally:
            self._scopes.pop()
            self._current_class_stack.pop()

    def _visit_if(self, node: Tree) -> None:
        branches, else_suite = self._if_branches(node)
        incoming = self._definite_snapshot()
        incoming_types = self._type_snapshot()

        for cond, _suite in branches:
            if cond is not None:
                self._restore_definite(incoming)
                self._restore_types(incoming_types)
                self._visit_expr_subtree(cond)

        exits: list[list[set[str]]] = []
        type_exits: list[list[dict[str, str]]] = []
        for _cond, suite in branches:
            self._restore_definite(incoming)
            self._restore_types(incoming_types)
            self._walk_suite_stmts(self._suite_stmts(suite))
            if not self._suite_definitely_terminates(suite):
                exits.append(self._definite_snapshot())
                type_exits.append(self._type_snapshot())

        if else_suite is not None:
            self._restore_definite(incoming)
            self._restore_types(incoming_types)
            self._walk_suite_stmts(self._suite_stmts(else_suite))
            if not self._suite_definitely_terminates(else_suite):
                exits.append(self._definite_snapshot())
                type_exits.append(self._type_snapshot())
        else:
            exits.append(incoming)
            type_exits.append(incoming_types)

        self._restore_definite(self._intersect_definite_snapshots(exits, incoming))
        self._restore_types(self._intersect_type_snapshots(type_exits, incoming_types))

    def _if_branches(self, node: Tree) -> tuple[list[tuple[Tree | None, Tree]], Tree | None]:
        branches: list[tuple[Tree | None, Tree]] = []
        else_suite = None
        pending_cond = None
        children = [c for c in node.children if isinstance(c, Tree)]
        for child in children:
            if child.data == "suite":
                if pending_cond is None and not branches:
                    branches.append((None, child))
                elif pending_cond is not None:
                    branches.append((pending_cond, child))
                    pending_cond = None
                else:
                    else_suite = child
            elif child.data == "elifs":
                for elif_node in child.children:
                    if isinstance(elif_node, Tree):
                        elif_branches, _ = self._if_branches(elif_node)
                        branches.extend(elif_branches)
            elif child.data == "elif_":
                elif_cond = None
                for sub in child.children:
                    if not isinstance(sub, Tree):
                        continue
                    if sub.data == "suite":
                        branches.append((elif_cond, sub))
                    else:
                        elif_cond = sub
            else:
                pending_cond = child
        return branches, else_suite

    def _visit_for(self, node: Tree) -> None:
        # Grammar:  ``for_stmt: "for" for_target "in" testlist suite ["else" suite]``
        target_node = node.children[0] if node.children else None
        iterable = node.children[1] if len(node.children) > 1 else None
        bodies = [c for c in node.children[2:] if isinstance(c, Tree) and c.data == "suite"]
        if iterable is not None:
            self._visit_expr_subtree(iterable)
        incoming = self._definite_snapshot()
        scope = _Scope(kind="block", is_loop=True)
        for n in self._for_target_names(target_node):
            self._check_go_reserved(target_node if isinstance(target_node, Tree) else node, n)
            scope.names.add(n)
            scope.definite_names.add(n)
        if bodies:
            self._collect_block_defs(bodies[0], scope)
        self._scopes.append(scope)
        try:
            if bodies:
                self._walk_suite_stmts(self._suite_stmts(bodies[0]))
            self._emit_unused_local_warnings(scope)
        finally:
            self._scopes.pop()
            self._restore_definite(incoming)
        if len(bodies) > 1:
            self._walk_suite_stmts(self._suite_stmts(bodies[1]))

    def _visit_while(self, node: Tree) -> None:
        cond = node.children[0] if node.children else None
        bodies = [c for c in node.children[1:] if isinstance(c, Tree) and c.data == "suite"]
        if cond is not None:
            self._visit_expr_subtree(cond)
        incoming = self._definite_snapshot()
        scope = _Scope(kind="block", is_loop=True)
        if bodies:
            self._collect_block_defs(bodies[0], scope)
        self._scopes.append(scope)
        try:
            if bodies:
                self._walk_suite_stmts(self._suite_stmts(bodies[0]))
            self._emit_unused_local_warnings(scope)
        finally:
            self._scopes.pop()
            self._restore_definite(incoming)
        if len(bodies) > 1:
            self._walk_suite_stmts(self._suite_stmts(bodies[1]))

    def _visit_match(self, node: Tree) -> None:
        # Subject expression first.
        if node.children:
            self._visit_expr_subtree(node.children[0])
        self._check_match_cases(node)

        incoming = self._definite_snapshot()
        incoming_types = self._type_snapshot()
        exits: list[list[set[str]]] = []
        type_exits: list[list[dict[str, str]]] = []
        has_unguarded_wildcard = False

        for c in node.children[1:]:
            if not isinstance(c, Tree) or c.data != "case":
                continue
            pattern, guard, suite = self._case_parts(c)
            self._restore_definite(incoming)
            self._restore_types(incoming_types)

            scope = _Scope(kind="block")
            self._collect_pattern_names(pattern, scope)
            scope.definite_names.update(scope.names)
            if suite is not None:
                self._collect_block_defs(suite, scope)

            self._scopes.append(scope)
            try:
                if guard is not None:
                    self._visit_expr_subtree(guard)
                if suite is not None:
                    self._walk_suite_stmts(self._suite_stmts(suite))
                self._emit_unused_local_warnings(scope)
            finally:
                self._scopes.pop()

            if suite is not None and not self._suite_definitely_terminates(suite):
                exits.append(self._definite_snapshot())
                type_exits.append(self._type_snapshot())
            if guard is None and self._pattern_is_wildcard(pattern):
                has_unguarded_wildcard = True

        if not has_unguarded_wildcard:
            exits.append(incoming)
            type_exits.append(incoming_types)
        self._restore_definite(self._intersect_definite_snapshots(exits, incoming))
        self._restore_types(self._intersect_type_snapshots(type_exits, incoming_types))

    @staticmethod
    def _case_parts(node: Tree) -> tuple[object | None, Tree | None, Tree | None]:
        pattern = node.children[0] if node.children else None
        guard = None
        suite = None
        for child in node.children[1:]:
            if isinstance(child, Tree) and child.data == "suite":
                suite = child
            elif isinstance(child, Tree):
                guard = child
        return pattern, guard, suite

    def _check_match_cases(self, node: Tree) -> None:
        seen_literals: dict[str, Tree] = {}
        wildcard_seen = False
        for case_node in [c for c in node.children[1:] if isinstance(c, Tree) and c.data == "case"]:
            pattern = case_node.children[0] if case_node.children else None
            guard = case_node.children[1] if len(case_node.children) > 1 else None
            if wildcard_seen:
                self._warning(
                    case_node, "match",
                    "case is unreachable because an earlier wildcard `_` matches first",
                )
                continue
            if guard is None:
                literal_key = self._literal_pattern_key(pattern)
                if literal_key:
                    if literal_key in seen_literals:
                        self._warning(
                            case_node, "match",
                            f"duplicate case pattern `{literal_key}`; this case is unreachable",
                        )
                    else:
                        seen_literals[literal_key] = case_node
                if self._pattern_is_wildcard(pattern):
                    wildcard_seen = True

    def _literal_pattern_key(self, node) -> str:
        if not isinstance(node, Tree):
            return ""
        if node.data == "literal_pattern" and node.children:
            return self._literal_pattern_key(node.children[0])
        if node.data == "string" and node.children:
            return str(node.children[0])
        if node.data == "number" and node.children:
            return str(node.children[0])
        if node.data == "const_true":
            return "True"
        if node.data == "const_false":
            return "False"
        if node.data == "const_none":
            return "None"
        return ""

    def _visit_do(self, node: Tree) -> None:
        """Validate ``do { body } catch err { handler }``.

        Body and handler each run in their own block scope. The catch
        ``name`` is mandatory and bound for the handler's scope only.
        """
        if len(node.children) < 3:
            return
        self._require_lamerrors_result(node, "`do/catch`")
        body_suite = node.children[0]
        err_name_node = node.children[1]
        handler_suite = node.children[2]

        if isinstance(body_suite, Tree) and body_suite.data == "suite":
            # The body lowers to a ``*Result``-returning IIFE, so
            # ``?`` propagates into the IIFE rather than the enclosing
            # function. Mark the scope accordingly so the
            # ``?``-in-non-Result warning doesn't misfire here.
            scope = _Scope(kind="block")
            scope.returns_result = True
            self._collect_block_defs(body_suite, scope)
            self._scopes.append(scope)
            try:
                self._walk_suite_stmts(self._suite_stmts(body_suite))
                self._emit_unused_local_warnings(scope)
            finally:
                self._scopes.pop()

        err_name = self._name_text(err_name_node)
        if isinstance(handler_suite, Tree) and handler_suite.data == "suite":
            scope = _Scope(kind="block")
            if err_name:
                scope.names.add(err_name)
                scope.definite_names.add(err_name)
            self._collect_block_defs(handler_suite, scope)
            self._scopes.append(scope)
            try:
                self._walk_suite_stmts(self._suite_stmts(handler_suite))
                self._emit_unused_local_warnings(scope)
            finally:
                self._scopes.pop()

    def _visit_try(self, node: Tree) -> None:
        # Grammar:  ``try_stmt: "try" suite catch_clauses ["else" suite] [finally]``
        # Catch clauses are alternate continuing paths, so assignments
        # made only in the try body are not definite after a catch that
        # can continue without making the same assignment.
        try_suite, catch_nodes, else_suite, finally_suite = self._try_parts(node)
        incoming = self._definite_snapshot()
        incoming_types = self._type_snapshot()
        exits: list[list[set[str]]] = []
        type_exits: list[list[dict[str, str]]] = []

        if try_suite is not None:
            self._restore_definite(incoming)
            self._restore_types(incoming_types)
            self._enter_block_and_visit(try_suite)
            if not self._suite_definitely_terminates(try_suite):
                if else_suite is not None:
                    self._enter_block_and_visit(else_suite)
                    if not self._suite_definitely_terminates(else_suite):
                        exits.append(self._definite_snapshot())
                        type_exits.append(self._type_snapshot())
                else:
                    exits.append(self._definite_snapshot())
                    type_exits.append(self._type_snapshot())

        for catch in catch_nodes:
            self._restore_definite(incoming)
            self._restore_types(incoming_types)
            body_suite = self._visit_catch_clause(catch)
            if body_suite is None or not self._suite_definitely_terminates(body_suite):
                exits.append(self._definite_snapshot())
                type_exits.append(self._type_snapshot())

        if try_suite is None:
            exits.append(incoming)
            type_exits.append(incoming_types)
        elif not catch_nodes and else_suite is None and not exits:
            exits.append(incoming)
            type_exits.append(incoming_types)

        merged = self._intersect_definite_snapshots(exits, incoming)
        self._restore_definite(merged)
        self._restore_types(self._intersect_type_snapshots(type_exits, incoming_types))
        if finally_suite is not None:
            self._enter_block_and_visit(finally_suite)

    @staticmethod
    def _try_parts(node: Tree) -> tuple[Tree | None, list[Tree], Tree | None, Tree | None]:
        try_suite = None
        else_suite = None
        finally_suite = None
        catch_nodes: list[Tree] = []
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "suite":
                if try_suite is None:
                    try_suite = child
                else:
                    else_suite = child
            elif child.data == "catch_clauses":
                catch_nodes.extend(
                    c for c in child.children
                    if isinstance(c, Tree) and c.data == "catch_clause"
                )
            elif child.data == "finally":
                for sub in child.children:
                    if isinstance(sub, Tree) and sub.data == "suite":
                        finally_suite = sub
                        break
        return try_suite, catch_nodes, else_suite, finally_suite

    def _visit_catch_clause(self, node: Tree) -> Tree | None:
        scope = _Scope(kind="block")
        body_suite = None
        # ``catch_clause: "catch" [test ["as" name]] suite``
        children = list(node.children)
        for c in children:
            if isinstance(c, Tree) and c.data == "suite":
                body_suite = c
                break

        # Pre-suite children: at most a type-test ``var`` and an
        # explicit ``as`` alias (a ``name`` tree). The transpiler also
        # treats a single bare-name test that *isn't* a known Python
        # exception as the binding name itself (``catch e``).
        pre = [c for c in children if c is not body_suite and isinstance(c, Tree)]
        type_test = None
        alias = None
        for c in pre:
            if c.data == "name":
                alias = self._name_text(c)
            elif type_test is None:
                type_test = c
        if alias is None and type_test is not None and type_test.data == "var":
            test_name = self._name_text(type_test.children[0]) if type_test.children else ""
            if test_name and test_name not in PYTHON_EXCEPTIONS:
                alias = test_name
                type_test = None
        if alias:
            self._check_go_reserved(node, alias)
            scope.names.add(alias)
            scope.definite_names.add(alias)
        if type_test is not None:
            self._visit_expr_subtree(type_test)
        if body_suite is not None:
            self._collect_block_defs(body_suite, scope)
        self._scopes.append(scope)
        try:
            if body_suite is not None:
                self._walk_suite_stmts(self._suite_stmts(body_suite))
            self._emit_unused_local_warnings(scope)
        finally:
            self._scopes.pop()
        return body_suite

    def _visit_with(self, node: Tree) -> None:
        scope = _Scope(kind="block")
        suite_node = None
        for c in node.children:
            if not isinstance(c, Tree):
                continue
            if c.data == "suite":
                suite_node = c
            elif c.data == "with_items":
                for item in c.children:
                    if isinstance(item, Tree) and item.data == "with_item":
                        # ``test [as name]``
                        for sub in item.children:
                            if isinstance(sub, Tree) and sub.data == "name":
                                name = self._name_text(sub)
                                scope.names.add(name)
                                scope.definite_names.add(name)
                            elif isinstance(sub, Token):
                                # Only the optional ``as`` alias appears
                                # as a token here (the keyword itself is
                                # filtered out by the grammar).
                                name = str(sub)
                                scope.names.add(name)
                                scope.definite_names.add(name)
                        # Also scan the resource expression.
                        if item.children:
                            self._visit_expr_subtree(item.children[0])
        if suite_node is not None:
            self._collect_block_defs(suite_node, scope)
        self._scopes.append(scope)
        try:
            if suite_node is not None:
                self._walk_suite_stmts(self._suite_stmts(suite_node))
            self._emit_unused_local_warnings(scope)
        finally:
            self._scopes.pop()

    def _enter_block_and_visit(self, suite: Tree) -> None:
        scope = _Scope(kind="block")
        self._collect_block_defs(suite, scope)
        self._scopes.append(scope)
        try:
            self._walk_suite_stmts(self._suite_stmts(suite))
            self._emit_unused_local_warnings(scope)
        finally:
            self._scopes.pop()

    # ─── Expression scan ───────────────────────────────────────

    # Tree node kinds that introduce their own private bindings or
    # whose direct children must not be treated as bare names. These
    # we either descend into specially or skip entirely.
    _SKIP_EXPR_NODES = {
        "type_expr", "type_name", "type_generic", "type_func",
        "type_union", "type_none", "type_constraint", "type_param",
        "type_params", "decorator", "decorators",
        # ``go!`` blocks are verbatim Go; their identifiers aren't Lam.
        "go_block", "go_inline",
    }

    def _visit_expr_subtree(self, node) -> None:
        if node is None:
            return
        if isinstance(node, Token):
            return
        if not isinstance(node, Tree):
            return
        d = node.data

        if d in self._SKIP_EXPR_NODES:
            return

        if d == "var":
            name = self._name_text(node.children[0]) if node.children else ""
            if name == "self" and not self._self_is_available():
                self._error(
                    node, "undefined",
                    "`self` is only available inside instance methods",
                )
                return
            if name == "base" and self._current_class_stack and not self._is_resolved(name):
                cls_name = self._current_class_stack[-1]
                bases = self._class_bases.get(cls_name, ())
                if len(bases) > 1:
                    self._error(
                        node,
                        "inheritance",
                        f"`base` is ambiguous in class `{cls_name}`; use a named base alias",
                    )
                    return
            if name and self._is_resolved(name):
                self._check_definite_read(node, name)
            elif name:
                suggestion = self._suggest_name(name)
                msg = f"undefined name `{name}`"
                if suggestion:
                    msg += f" — did you mean `{suggestion}`?"
                self._error(node, "undefined", msg)
            return

        # F-strings: the grammar swallows the whole literal as one
        # ``FSTRING`` token, so the expression walker can't see the
        # identifiers inside ``{...}`` slots through the AST. Without
        # the explicit scrape below, ``f"hello {name}"`` would never
        # mark ``name`` as used and the unused-parameter check would
        # raise a false positive on it. We deliberately *don't*
        # report undefined-name errors from inside a slot — Lark's
        # token positions cover the whole literal, not the
        # interpolation, so any diagnostic would land on a misleading
        # column. Marking-only is enough to dispel the unused-binding
        # warning without weakening the genuine undefined-name check.
        if d == "fstring":
            self._mark_fstring_uses(node)
            return

        if d in ("getattr", "getattr_safe"):
            self._check_member_access(node)
            if node.children:
                self._visit_expr_subtree(node.children[0])
            return

        # ``expr?`` — only well-formed inside a function whose
        # signature declares ``-> Result`` (or inside a
        # ``do { } catch`` block, which has its own Result-returning
        # IIFE scope at emission time). Everywhere else the lowering
        # falls back to a panic on ``Err``; we warn here so the user
        # spots the intent mismatch before surprise runtime panics.
        if d == "propagate":
            if not self._inside_result_block():
                self._require_lamerrors_result(node, "`?`")
            if not self._nearest_returns_result():
                self._warning(
                    node, "flow",
                    "`?` operator used outside a `-> Result` function; "
                    "an `Err` will panic at runtime "
                    "(change the signature to return `Result` "
                    "or handle the error explicitly)",
                )
            # Fall through so the inner expression is still walked.

        # Function calls: we only inspect the callee and the args, but
        # already-handled by recursing into children. Static-method
        # calls like ``Math.sqrt(x)`` and module attribute calls show
        # up as ``getattr`` so the base-name check above suffices.
        if d == "lambdef":
            self._visit_lambda(node)
            return

        if d == "funccall":
            if self._mark_go_marker_uses(node):
                return
            target = self._call_target_name(node)
            if target in self._external_private_functions:
                self._error(
                    node,
                    "visibility",
                    f"private function `{target}` is not visible outside its module",
                )
                self._visit_call_args(node)
                return
            if self._check_method_kind_call(node):
                self._visit_call_args(node)
                return
            self._check_call_shape(node)
            self._check_generic_type_args(node)
            self._check_builtin_cast_call(node)
            self._check_call_interface_args(node)

        if d in ("list_comprehension", "dict_comprehension",
                 "set_comprehension", "tuple_comprehension"):
            self._visit_comprehension(node)
            return

        # Keyword-argument call site: ``f(name=value)`` parses to an
        # ``argvalue`` Tree with two children where the LHS is a bare
        # name referring to a *parameter*, not a runtime variable.
        # Skip the LHS so we don't flag it as undefined.
        if d == "argvalue" and len(node.children) == 2:
            self._visit_expr_subtree(node.children[1])
            return

        self._check_expression_type_errors(node)

        for child in node.children:
            self._visit_expr_subtree(child)

    def _visit_lambda(self, node: Tree) -> None:
        scope = _Scope(kind="function")
        body = None
        for c in node.children:
            if isinstance(c, Tree):
                if c.data == "typed_parameters":
                    param_nodes = self._param_node_map(c)
                    for n in self._param_names(c):
                        self._check_go_reserved(param_nodes.get(n, c), n)
                        scope.names.add(n)
                        scope.definite_names.add(n)
                elif c.data in ("paren_lambda_params", "inline_lambda_params"):
                    # Each child is either a ``typed_lambda_param``
                    # (``x: int``), an untyped ``inline_lambda_param``,
                    # an inline ``name`` tree, or a bare ``NAME`` token.
                    for sub in c.children:
                        if isinstance(sub, Tree):
                            if sub.data in ("typed_lambda_param",
                                            "inline_lambda_param"):
                                if sub.children:
                                    name = self._name_text(sub.children[0])
                                    self._check_go_reserved(sub, name)
                                    scope.names.add(name)
                                    scope.definite_names.add(name)
                            elif sub.data == "name":
                                name = self._name_text(sub)
                                self._check_go_reserved(sub, name)
                                scope.names.add(name)
                                scope.definite_names.add(name)
                        elif isinstance(sub, Token):
                            name = str(sub)
                            self._check_go_reserved(c, name)
                            scope.names.add(name)
                            scope.definite_names.add(name)
                elif c.data == "lambda_return_anno":
                    # Skip — return type doesn't introduce names.
                    pass
                else:
                    body = c
            elif isinstance(c, Token):
                name = str(c)
                self._check_go_reserved(node, name)
                scope.names.add(name)
                scope.definite_names.add(name)
        self._scopes.append(scope)
        try:
            if body is not None:
                if isinstance(body, Tree) and body.data == "suite":
                    # Multi-line lambda body — visit as a regular suite
                    # so declarations + control flow check normally.
                    self._collect_block_defs(body, scope)
                    self._walk_suite_stmts(self._suite_stmts(body))
                else:
                    self._visit_expr_subtree(body)
        finally:
            self._scopes.pop()

    def _visit_comprehension(self, node: Tree) -> None:
        # ``[expr for x in xs if cond]`` — the loop targets become
        # local to the comprehension. The grammar nests them under
        # ``comp_fors > comp_for > <target> <iterable>``, potentially
        # many ``comp_for`` nodes for chained iteration
        # (``for x in xs for y in x``).
        scope = _Scope(kind="block")
        for cf in self._iter_comp_fors(node):
            if cf.children:
                for n in self._for_target_names(cf.children[0]):
                    self._check_go_reserved(cf.children[0], n)
                    scope.names.add(n)
                    scope.definite_names.add(n)
        self._scopes.append(scope)
        try:
            self._check_comprehension_filter_types(node)
            for c in node.children:
                self._visit_expr_subtree(c)
        finally:
            self._scopes.pop()

    def _check_comprehension_filter_types(self, node: Tree) -> None:
        for condition in self._iter_comprehension_filters(node):
            cond_type = self._expr_simple_type(condition)
            if cond_type is None or self._is_any_type(cond_type) or self._is_open_type(cond_type):
                continue
            if self._is_named_type(cond_type, "bool"):
                continue
            self._error(
                condition,
                "type",
                f"comprehension filter must be `bool`, got `{render_type(cond_type)}`",
            )
            return

    @staticmethod
    def _iter_comp_fors(node: Tree):
        """Yield every ``comp_for`` tree found under a comprehension."""
        def _walk(n):
            if not isinstance(n, Tree):
                return
            if n.data == "comp_for":
                yield n
                return
            for c in n.children:
                yield from _walk(c)
        yield from _walk(node)

    @staticmethod
    def _iter_comprehension_filters(node: Tree):
        """Yield every optional ``if`` filter under a comprehension."""
        def _walk(n):
            if not isinstance(n, Tree):
                return
            if n.data == "comprehension":
                if len(n.children) > 2 and isinstance(n.children[2], Tree):
                    yield n.children[2]
                return
            for c in n.children:
                yield from _walk(c)
        yield from _walk(node)

    def _check_member_access(self, node: Tree) -> None:
        if len(node.children) < 2:
            return
        obj = node.children[0]
        attr_node = node.children[1]
        attr = self._name_text(attr_node)
        if not attr:
            return
        if isinstance(obj, Tree) and obj.data == "var" and obj.children:
            receiver = self._name_text(obj.children[0])
            if receiver == "self" and self._current_class_stack:
                cls_name = self._current_class_stack[-1]
                self._check_class_member_access(node, "self", cls_name, attr)
                return
            if receiver in self._class_members:
                if self._check_external_private_member(node, receiver, attr):
                    return
                self._check_class_member_access(node, receiver, receiver, attr)
                return
            receiver_type = self._var_type(receiver)
            if receiver_type in self._class_members:
                if self._check_external_private_member(node, receiver_type, attr):
                    return
                self._check_class_member_access(node, receiver, receiver_type, attr)

    def _check_class_member_access(
        self,
        node: Tree,
        receiver_display: str,
        class_name: str,
        attr: str,
    ) -> None:
        if self._class_has_direct_member(class_name, attr):
            return
        sources = self._inherited_member_sources(class_name, attr)
        if len(sources) == 1:
            return
        if len(sources) > 1:
            self._error(
                node,
                "inheritance",
                f"ambiguous inherited member `{attr}` on class `{class_name}` "
                f"from {self._format_name_list(sources)}; use a named base alias",
            )
            return
        if self._is_operator_alias_member(class_name, attr):
            return
        suggestion = self._suggest_member(class_name, attr)
        msg = f"unknown member `{receiver_display}.{attr}` on class `{class_name}`"
        if suggestion:
            msg += f" — did you mean `{receiver_display}.{suggestion}`?"
        self._error(node, "member", msg)

    def _class_has_direct_member(self, class_name: str, attr: str) -> bool:
        members = self._class_members.get(class_name, set())
        if attr in members:
            return True
        return (
            (attr == "init" and "__init__" in members)
            or (attr == "__init__" and "init" in members)
        )

    def _inherited_member_sources(self, class_name: str, attr: str) -> list[str]:
        sources: list[str] = []
        for base in self._all_base_names(class_name):
            if self._class_has_direct_member(base, attr):
                sources.append(base)
        return list(dict.fromkeys(sources))

    def _check_external_private_member(self, node: Tree, class_name: str, attr: str) -> bool:
        if attr not in self._external_class_private_methods.get(class_name, set()):
            return False
        self._error(
            node,
            "visibility",
            f"private member `{class_name}.{attr}` is not visible outside its module",
        )
        return True

    def _check_method_kind_call(self, call: Tree) -> bool:
        callee = call.children[0] if call.children else None
        if not isinstance(callee, Tree) or callee.data not in {"getattr", "getattr_safe"}:
            return False
        if len(callee.children) < 2:
            return False
        obj = callee.children[0]
        attr_node = callee.children[1]
        method = self._name_text(attr_node)
        if not method:
            return False
        if not isinstance(obj, Tree) or obj.data != "var" or not obj.children:
            receiver = self._expr_dotted_name(obj) if isinstance(obj, Tree) else ""
        else:
            receiver = self._name_text(obj.children[0])
        if receiver in self._class_members:
            if method in self._class_instance_methods.get(receiver, set()):
                self._is_resolved(receiver.split(".", 1)[0])
                self._error(
                    call, "method",
                    f"cannot call instance method `{receiver}.{method}` without an instance",
                )
                return True
            return False
        receiver_type = self._var_type(receiver)
        if receiver_type and method in self._class_static_methods.get(receiver_type, set()):
            self._is_resolved(receiver)
            self._error(
                call, "method",
                f"static method `{receiver_type}.{method}` should be called as `{receiver_type}.{method}(...)`",
            )
            return True
        return False

    def _expr_dotted_name(self, node: Tree) -> str:
        if not isinstance(node, Tree):
            return ""
        if node.data == "var":
            return self._name_text(node.children[0]) if node.children else ""
        if node.data in {"getattr", "getattr_safe"} and len(node.children) >= 2:
            prefix = self._expr_dotted_name(node.children[0])
            attr = self._name_text(node.children[1])
            if prefix and attr:
                return f"{prefix}.{attr}"
        return ""

    def _visit_call_args(self, call: Tree) -> None:
        args_node = call.children[1] if len(call.children) > 1 else None
        self._visit_expr_subtree(args_node)

    def _suggest_member(self, class_name: str, attr: str) -> Optional[str]:
        members = self._class_exposed_members(class_name)
        matches = get_close_matches(attr, list(members), n=1, cutoff=0.75)
        return matches[0] if matches else None

    def _is_operator_alias_member(self, class_name: str, attr: str) -> bool:
        alias = attr[:1].upper() + attr[1:]
        for dunder, go_name in DUNDER_OPS.items():
            if go_name == alias and dunder in self._class_members.get(class_name, set()):
                return True
        return False

    def _check_decorators(self, node: Tree) -> None:
        for deco in self._decorator_nodes(node):
            name = self._decorator_name(deco)
            if name == "private":
                self._error(
                    deco,
                    "decorator",
                    "`@private` is not supported; use `private func ...` "
                    "or `private static func ...` instead",
                )
                return
            display = f"@{name}" if name else "decorator"
            self._error(
                deco,
                "decorator",
                f"{display} decorators are parsed but not supported yet",
            )
            return

    @staticmethod
    def _decorator_nodes(node: Tree) -> list[Tree]:
        out: list[Tree] = []
        if not isinstance(node, Tree) or node.data != "decorated":
            return out
        for child in node.children:
            if isinstance(child, Tree) and child.data == "decorators":
                out.extend(
                    sub for sub in child.children
                    if isinstance(sub, Tree) and sub.data == "decorator"
                )
        return out

    @staticmethod
    def _decorator_name(node: Tree) -> str:
        if not isinstance(node, Tree) or node.data != "decorator":
            return ""
        for child in node.children:
            if isinstance(child, Tree) and child.data == "dotted_name":
                return ".".join(
                    SemanticChecker._name_text(part)
                    for part in child.children
                    if SemanticChecker._name_text(part)
                )
        return ""

    def _check_inherited_class_shape(self, node: Tree, class_name: str) -> None:
        bases = [base for base in self._class_bases.get(class_name, ()) if base]
        if not bases:
            return

        self._check_inheritance_aliases(node, class_name)

        cycle = self._inheritance_cycle(class_name)
        if cycle:
            self._error(
                node,
                "inheritance",
                f"inheritance cycle detected: {' -> '.join(cycle)}",
            )
            return

        unique_bases: list[str] = []
        seen_bases: set[str] = set()
        for base in bases:
            if base in seen_bases:
                self._error(
                    node,
                    "inheritance",
                    f"class `{class_name}` inherits `{base}` more than once",
                )
                continue
            seen_bases.add(base)
            unique_bases.append(base)

        child_methods = self._class_method_shapes.get(class_name, {})
        child_fields = self._class_fields.get(class_name, set())
        child_member_names = self._class_members.get(class_name, set())
        child_method_names = (
            self._class_instance_methods.get(class_name, set())
            | self._class_static_methods.get(class_name, set())
        )

        inherited_member_sources: dict[str, list[str]] = {}
        for base in unique_bases:
            for member in self._class_exposed_members(base):
                if not self._is_constructor_method(member):
                    inherited_member_sources.setdefault(member, []).append(base)

        for base in self._all_base_names(class_name):
            base_fields = self._class_fields.get(base, set())
            base_method_names = (
                self._class_instance_methods.get(base, set())
                | self._class_static_methods.get(base, set())
            )
            base_methods = self._class_method_shapes.get(base, {})

            for method_name in sorted(child_methods):
                if self._is_constructor_method(method_name) or method_name not in base_methods:
                    continue
                self._check_inherited_method_override(
                    node,
                    class_name,
                    base,
                    method_name,
                    child_methods[method_name],
                    base_methods[method_name],
                )

            for field_name in sorted(child_fields & base_method_names):
                self._error(
                    node,
                    "inheritance",
                    f"class `{class_name}` field `{field_name}` conflicts with "
                    f"inherited method `{base}.{field_name}`",
                )

            for method_name in sorted(child_method_names & base_fields):
                if self._is_constructor_method(method_name):
                    continue
                self._error(
                    node,
                    "inheritance",
                    f"class `{class_name}` method `{method_name}` conflicts with "
                    f"inherited field `{base}.{method_name}`",
                )

        if not self._has_explicit_base_aliases(class_name):
            for member, sources in sorted(inherited_member_sources.items()):
                unique_sources = list(dict.fromkeys(sources))
                if len(unique_sources) < 2 or member in child_member_names:
                    continue
                self._error(
                    node,
                    "inheritance",
                    f"class `{class_name}` inherits ambiguous member `{member}` from "
                    f"{self._format_name_list(unique_sources)}; override it in `{class_name}` "
                    "or use named base aliases",
                )

    def _check_inheritance_aliases(self, node: Tree, class_name: str) -> None:
        seen: set[str] = set()
        for alias, _base in self._classdef_base_specs(node):
            if not alias:
                continue
            if alias == "base":
                self._error(
                    node,
                    "inheritance",
                    "inheritance alias `base` is reserved for the default single-parent alias",
                )
                continue
            if alias == "self":
                self._error(
                    node,
                    "inheritance",
                    "inheritance alias `self` is reserved for the current instance",
                )
                continue
            if alias in seen:
                self._error(
                    node,
                    "inheritance",
                    f"duplicate inheritance alias `{alias}` in class `{class_name}`",
                )
            seen.add(alias)

    def _has_explicit_base_aliases(self, class_name: str) -> bool:
        bases = [base for base in self._class_bases.get(class_name, ()) if base]
        if not bases:
            return False
        aliases = self._class_base_aliases.get(class_name, {})
        explicit_aliases = {alias: base for alias, base in aliases.items() if alias != "base"}
        return len(explicit_aliases) == len(bases)

    def _inheritance_cycle(self, class_name: str) -> list[str]:
        path: list[str] = []

        def visit(name: str) -> Optional[list[str]]:
            if name in path:
                idx = path.index(name)
                return [*path[idx:], name]
            path.append(name)
            for base in self._class_bases.get(name, ()):
                found = visit(base)
                if found:
                    return found
            path.pop()
            return None

        return visit(class_name) or []

    def _all_base_names(self, class_name: str) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()

        def visit(name: str) -> None:
            for base in self._class_bases.get(name, ()):
                if not base or base in seen:
                    continue
                seen.add(base)
                out.append(base)
                visit(base)

        visit(class_name)
        return out

    def _class_exposed_members(self, class_name: str) -> set[str]:
        members = set(self._class_members.get(class_name, set()))
        for base in self._all_base_names(class_name):
            members |= set(self._class_members.get(base, set()))
        return members

    def _check_inherited_method_override(
        self,
        node: Tree,
        class_name: str,
        base: str,
        method_name: str,
        child_shape: _MethodShape,
        base_shape: _MethodShape,
    ) -> None:
        child_kind = self._class_method_kind(class_name, method_name)
        base_kind = self._class_method_kind(base, method_name)
        if child_kind and base_kind and child_kind != base_kind:
            self._error(
                node,
                "inheritance",
                f"class `{class_name}` overrides {base_kind} method "
                f"`{base}.{method_name}` with a {child_kind} method",
            )
            return

        if len(child_shape.param_types) != len(base_shape.param_types):
            got = len(child_shape.param_types)
            expected = len(base_shape.param_types)
            self._error(
                node,
                "inheritance",
                f"class `{class_name}` overrides `{base}.{method_name}` with "
                f"{got} parameter{'s' if got != 1 else ''}, expected {expected}",
            )
            return

        child_param_types = self._shape_param_texts(child_shape)
        base_param_types = self._shape_param_texts(base_shape)
        for got, expected in zip(child_param_types, base_param_types):
            if "any" in {got, expected} or got == expected:
                continue
            self._error(
                node,
                "inheritance",
                f"class `{class_name}` overrides `{base}.{method_name}` with "
                f"incompatible parameters: got "
                f"{self._format_param_types(child_param_types)}, expected "
                f"{self._format_param_types(base_param_types)}",
            )
            return

        child_return = self._shape_return_text(child_shape)
        base_return = self._shape_return_text(base_shape)
        if child_return != base_return:
            self._error(
                node,
                "inheritance",
                f"class `{class_name}` overrides `{base}.{method_name}` with "
                f"return `{child_return}`, expected `{base_return}`",
            )

    def _class_method_kind(self, class_name: str, method_name: str) -> str:
        if method_name in self._class_static_methods.get(class_name, set()):
            return "static"
        if method_name in self._class_instance_methods.get(class_name, set()):
            return "instance"
        return ""

    @staticmethod
    def _shape_param_texts(shape: _MethodShape) -> tuple[str, ...]:
        return shape.param_type_texts or shape.param_types

    @staticmethod
    def _shape_return_text(shape: _MethodShape) -> str:
        return shape.return_type_text or shape.return_type or "None"

    @staticmethod
    def _is_constructor_method(name: str) -> bool:
        return name in {"init", "__init__"}

    @staticmethod
    def _format_param_types(types: tuple[str, ...]) -> str:
        return "(" + ", ".join(t or "any" for t in types) + ")"

    @staticmethod
    def _format_name_list(names: list[str]) -> str:
        quoted = [f"`{name}`" for name in names]
        if len(quoted) == 1:
            return quoted[0]
        if len(quoted) == 2:
            return f"{quoted[0]} and {quoted[1]}"
        return ", ".join(quoted[:-1]) + f", and {quoted[-1]}"

    def _check_type_annotations(self, node: Tree) -> None:
        if not isinstance(node, Tree):
            return
        seen: set[int] = set()
        for type_expr in self._iter_type_exprs_for_check(node):
            if type_expr.data != "type_expr" or id(type_expr) in seen:
                continue
            seen.add(id(type_expr))
            root = self._type_root_name(type_expr)
            if not root or self._is_known_type(root):
                continue
            suggestion = self._suggest_type(root)
            if suggestion:
                self._error(
                    type_expr, "type",
                    f"unknown type `{root}` — did you mean `{suggestion}`?",
                )

    def _is_known_type(self, name: str) -> bool:
        if name in BUILTIN_TYPES:
            return True
        if name in self._class_method_shapes or name in self._interface_methods:
            return True
        return self._is_resolved(name)

    def _suggest_type(self, name: str) -> Optional[str]:
        pool = set(BUILTIN_TYPES)
        pool |= set(self._class_method_shapes)
        pool |= set(self._interface_methods)
        for scope in self._scopes:
            pool |= scope.names
        pool.discard(name)
        matches = get_close_matches(name, sorted(pool), n=1, cutoff=0.75)
        return matches[0] if matches else None

    def _iter_type_exprs_for_check(self, node: Tree):
        def walk(cur):
            if not isinstance(cur, Tree):
                return
            if cur.data == "type_expr":
                yield cur
                return
            if cur.data in {"suite", "classdef", "lambdef"} and cur is not node:
                return
            for child in cur.children:
                yield from walk(child)
        yield from walk(node)

    def _check_assignment_interface_conformance(self, node: Tree) -> None:
        if node.data == "assign_stmt":
            for child in node.children:
                if isinstance(child, Tree):
                    self._check_assignment_interface_conformance(child)
            return
        if node.data != "annassign":
            return
        target_type = self._annassign_type_root(node)
        if target_type not in self._interface_methods:
            return
        value_type = self._constructor_value_type(self._annassign_value(node))
        if value_type:
            self._check_class_satisfies_interface(value_type, target_type, node)

    def _check_assignment_value_types(self, node: Tree) -> None:
        if node.data == "assign_stmt":
            for child in node.children:
                if isinstance(child, Tree):
                    self._check_assignment_value_types(child)
            return
        if node.data != "annassign":
            return
        if self._destructuring_target(node) is not None:
            return
        type_node = self._annassign_type_node(node)
        value_node = self._annassign_value(node)
        if type_node is None or value_node is None:
            return
        expected = parse_type(type_node)
        if self._check_annotated_container_literal(node, expected, value_node):
            return
        actual = self._expr_simple_type(value_node)
        if actual is None:
            return
        if self._is_unknown_open_assignment_type(actual):
            return
        if is_assignable(expected, actual) or self._nominal_type_assignable(expected, actual):
            return
        name = next((target for target in self._assign_targets(node) if target), "<target>")
        self._error(
            value_node,
            "type",
            f"cannot assign `{render_type(actual)}` to `{render_type(expected)}` variable `{name}`",
        )

    def _check_destructuring_shape(self, node: Tree) -> None:
        if node.data == "assign_stmt":
            for child in node.children:
                if isinstance(child, Tree):
                    self._check_destructuring_shape(child)
            return
        if node.data not in {"assign", "annassign"}:
            return
        target = self._destructuring_target(node)
        if target is None:
            return
        names = self._for_target_names(target)
        self._check_duplicate_destructuring_targets(node, names)
        expected = len(names)
        actual_types, source = self._destructuring_value_types(self._assign_value(node))
        actual = len(actual_types) if actual_types is not None else None
        annotation_types = self._destructuring_annotation_types(node)
        if annotation_types is not None and len(annotation_types) != expected:
            self._error(
                node,
                "assign",
                f"destructuring annotation has {len(annotation_types)} "
                f"element{'s' if len(annotation_types) != 1 else ''}, "
                f"but target has {expected}",
            )
            return
        if expected > 0 and actual is not None and expected != actual:
            self._error(
                node,
                "assign",
                f"destructuring expects {expected} values, but {source} has {actual}",
            )
            return
        if annotation_types is None or actual_types is None:
            return
        if len(annotation_types) != len(actual_types):
            return
        for name, expected_type, actual_type in zip(names, annotation_types, actual_types):
            if name == "_" or self._is_unknown_type(actual_type):
                continue
            if is_assignable(expected_type, actual_type) or self._nominal_type_assignable(expected_type, actual_type):
                continue
            self._error(
                node,
                "type",
                f"destructuring target `{name}` expects `{render_type(expected_type)}`, "
                f"got `{render_type(actual_type)}`",
            )

    def _check_duplicate_destructuring_targets(self, node: Tree, names: list[str]) -> None:
        seen: set[str] = set()
        for name in names:
            if not name or name == "_":
                continue
            if name in seen:
                self._error(node, "assign", f"duplicate destructuring target `{name}`")
                return
            seen.add(name)

    @staticmethod
    def _is_unknown_type(type_: Type) -> bool:
        return isinstance(type_, NamedType) and type_.name in {"any", "object"}

    def _destructuring_target(self, node: Tree) -> Tree | None:
        if node.data == "assign" and node.children:
            target = node.children[0]
            return target if isinstance(target, Tree) and target.data == "tuple" else None
        if node.data == "annassign":
            for child in node.children:
                if isinstance(child, Tree) and child.data == "type_expr":
                    return None
                if isinstance(child, Tree) and child.data == "tuple":
                    return child
        return None

    def _destructuring_annotation_types(self, node: Tree) -> list[Type] | None:
        if node.data != "annassign":
            return None
        type_node = self._annassign_type_node(node)
        if type_node is None:
            return None
        return self._tuple_element_types(parse_type(type_node))

    def _destructuring_value_types(self, node) -> tuple[list[Type] | None, str]:
        if not isinstance(node, Tree):
            return None, "value"
        if node.data == "tuple":
            return [
                self._expr_simple_type(child) or NamedType("any")
                for child in node.children
                if isinstance(child, Tree)
            ], "tuple literal"
        if node.data == "var" and node.children:
            name = self._name_text(node.children[0])
            return self._tuple_element_types_from_text(self._var_type(name)), f"`{name}`"
        if node.data == "funccall":
            target = self._call_target_name(node)
            return (
                self._tuple_element_types_from_text(self._call_return_type_text(node)),
                f"`{target}()`" if target else "call result",
            )
        value_type = self._expr_simple_type(node)
        if value_type is not None:
            return self._tuple_element_types(value_type), "value"
        return None, "value"

    def _tuple_element_types_from_text(self, type_text: str) -> list[Type] | None:
        if not type_text:
            return None
        try:
            return self._tuple_element_types(parse_type(type_text))
        except Exception:
            return None

    @staticmethod
    def _tuple_element_types(type_: Type) -> list[Type] | None:
        if isinstance(type_, GenericType) and type_.base == "tuple":
            return list(type_.args)
        return None

    def _call_return_type_text(self, node: Tree) -> str:
        if not isinstance(node, Tree) or node.data != "funccall" or not node.children:
            return ""
        target = self._call_target_name(node)
        if not target:
            return ""
        if "." in target:
            receiver, method = target.split(".", 1)
            receiver_type = self._var_type(receiver)
            if receiver_type:
                receiver_method_type = self._single_known_return_type(
                    self._method_return_type_texts.get(f"{receiver_type}.{method}", set())
                )
                if receiver_method_type:
                    return receiver_method_type
        method_type = self._single_known_return_type(
            self._method_return_type_texts.get(target, set())
        )
        if method_type:
            return method_type
        return self._single_known_return_type(self._func_return_type_texts.get(target, set()))

    def _destructuring_value_arity(self, node) -> tuple[int | None, str]:
        types, source = self._destructuring_value_types(node)
        if types is not None:
            return len(types), source
        return None, source

    def _call_target_name(self, node: Tree) -> str:
        if not isinstance(node, Tree) or node.data != "funccall" or not node.children:
            return ""
        callee = node.children[0]
        if isinstance(callee, Tree) and callee.data == "var" and callee.children:
            return self._name_text(callee.children[0])
        if isinstance(callee, Tree) and callee.data in {"getattr", "getattr_safe"}:
            return self._expr_dotted_name(callee)
        return ""

    @staticmethod
    def _tuple_arity_from_type_text(type_text: str) -> int | None:
        if not type_text:
            return None
        try:
            return SemanticChecker._tuple_arity_from_type(parse_type(type_text))
        except Exception:
            return None

    @staticmethod
    def _tuple_arity_from_type(type_: Type) -> int | None:
        if isinstance(type_, GenericType) and type_.base == "tuple":
            return len(type_.args)
        return None

    def _check_const_value_type(self, node: Tree) -> None:
        type_node = self._const_type_node(node)
        value_node = self._const_value_node(node)
        if type_node is None or value_node is None:
            return
        expected = parse_type(type_node)
        if self._check_annotated_container_literal(node, expected, value_node, binding_kind="constant"):
            return
        actual = self._expr_simple_type(value_node)
        if actual is None:
            return
        if is_assignable(expected, actual) or self._nominal_type_assignable(expected, actual):
            return
        name = self._const_target_name(node) or "<const>"
        self._error(
            value_node,
            "type",
            f"cannot assign `{render_type(actual)}` to `{render_type(expected)}` constant `{name}`",
        )

    def _check_annotated_container_literal(
        self,
        node: Tree,
        expected: Type,
        value_node: Tree,
        *,
        binding_kind: str = "variable",
    ) -> bool:
        if isinstance(expected, ListType) and isinstance(value_node, Tree) and value_node.data == "list":
            return self._check_sequence_literal_items(
                node,
                expected.item,
                [child for child in value_node.children if isinstance(child, Tree)],
                "list element",
                binding_kind=binding_kind,
            )
        if isinstance(expected, DictType) and isinstance(value_node, Tree) and value_node.data == "dict":
            for child in value_node.children:
                if not isinstance(child, Tree) or child.data != "key_value" or len(child.children) < 2:
                    continue
                key, value = child.children[0], child.children[1]
                if isinstance(key, Tree) and self._check_container_item_type(
                    node, expected.key, key, "dict key", binding_kind=binding_kind,
                ):
                    return True
                if isinstance(value, Tree) and self._check_container_item_type(
                    node, expected.value, value, "dict value", binding_kind=binding_kind,
                ):
                    return True
        if (isinstance(expected, GenericType)
                and expected.base == "set"
                and expected.args
                and isinstance(value_node, Tree)
                and value_node.data == "set"):
            return self._check_sequence_literal_items(
                node,
                expected.args[0],
                [child for child in value_node.children if isinstance(child, Tree)],
                "set element",
                binding_kind=binding_kind,
            )
        return False

    def _check_sequence_literal_items(
        self,
        node: Tree,
        expected: Type,
        items: list[Tree],
        label: str,
        *,
        binding_kind: str,
    ) -> bool:
        for item in items:
            if self._check_container_item_type(node, expected, item, label, binding_kind=binding_kind):
                return True
        return False

    def _check_container_item_type(
        self,
        node: Tree,
        expected: Type,
        item: Tree,
        label: str,
        *,
        binding_kind: str,
    ) -> bool:
        actual = self._expr_simple_type(item)
        if actual is None:
            return False
        if is_assignable(expected, actual) or self._nominal_type_assignable(expected, actual):
            return False
        name = (
            self._const_target_name(node)
            if binding_kind == "constant"
            else next((target for target in self._assign_targets(node) if target), "<target>")
        ) or "<target>"
        self._error(
            item,
            "type",
            f"{label} for {binding_kind} `{name}` must be `{render_type(expected)}`, got `{render_type(actual)}`",
        )
        return True

    def _check_call_interface_args(self, call: Tree) -> None:
        callee = call.children[0] if call.children else None
        if not isinstance(callee, Tree) or callee.data != "var" or not callee.children:
            return
        func_name = self._name_text(callee.children[0])
        variants = self._func_param_types.get(func_name)
        if not variants:
            return
        arg_nodes = self._call_positional_args(call)
        if not arg_nodes:
            return
        for param_types in variants:
            for idx, iface_name in enumerate(param_types):
                if idx >= len(arg_nodes) or iface_name not in self._interface_methods:
                    continue
                value_type = self._expr_known_type(arg_nodes[idx])
                if value_type:
                    self._check_class_satisfies_interface(value_type, iface_name, arg_nodes[idx])

    def _check_class_satisfies_interface(self, class_name: str, iface_name: str, node: Tree) -> None:
        if class_name not in self._class_method_shapes:
            return
        required = self._interface_methods.get(iface_name, {})
        available = self._class_method_shapes.get(class_name, {})
        for method_name, iface_shape in required.items():
            class_shape = available.get(method_name)
            if class_shape is None:
                self._error(
                    node, "interface",
                    f"class `{class_name}` does not satisfy interface `{iface_name}`: missing method `{method_name}`",
                )
                continue
            if len(class_shape.param_types) != len(iface_shape.param_types):
                self._error(
                    node, "interface",
                    f"class `{class_name}` does not satisfy interface `{iface_name}`: method `{method_name}` has {len(class_shape.param_types)} parameter{'s' if len(class_shape.param_types) != 1 else ''}, expected {len(iface_shape.param_types)}",
                )
                continue
            param_mismatch = False
            for got, expected in zip(
                self._shape_param_texts(class_shape),
                self._shape_param_texts(iface_shape),
            ):
                if "any" in {got, expected} or got == expected:
                    continue
                self._error(
                    node, "interface",
                    f"class `{class_name}` does not satisfy interface `{iface_name}`: "
                    f"method `{method_name}` takes `{got}`, expected `{expected}`",
                )
                param_mismatch = True
                break
            if param_mismatch:
                continue
            got = self._shape_return_text(class_shape)
            expected = self._shape_return_text(iface_shape)
            if got != expected:
                self._error(
                    node, "interface",
                    f"class `{class_name}` does not satisfy interface `{iface_name}`: method `{method_name}` returns `{got}`, expected `{expected}`",
                )

    def _record_assignment_types(self, node: Tree) -> None:
        if node.data == "assign_stmt":
            for child in node.children:
                if isinstance(child, Tree):
                    self._record_assignment_types(child)
            return
        if node.data not in {"annassign", "assign"}:
            return
        if self._record_destructuring_assignment_types(node):
            return
        names = self._assign_targets(node)
        if node.data == "annassign":
            type_node = self._annassign_type_node(node)
            type_name = self._type_name_for_storage(type_node)
        else:
            value_type = self._expr_simple_type(self._assign_value(node))
            type_name = render_type(value_type) if value_type is not None else ""
        if not names or not type_name or not self._scopes:
            return
        for name in names:
            self._type_storage_scope(node, name).var_types[name] = type_name

    def _record_destructuring_assignment_types(self, node: Tree) -> bool:
        target = self._destructuring_target(node)
        if target is None:
            return False
        names = self._for_target_names(target)
        if node.data == "annassign":
            element_types = self._destructuring_annotation_types(node)
        else:
            element_types, _source = self._destructuring_value_types(self._assign_value(node))
        if not names or element_types is None or len(names) != len(element_types) or not self._scopes:
            return True
        for name, type_ in zip(names, element_types):
            if not name or name == "_":
                continue
            self._type_storage_scope(node, name).var_types[name] = render_type(type_)
        return True

    def _expr_known_type(self, node) -> str:
        if not isinstance(node, Tree):
            return ""
        if node.data == "var" and node.children:
            name = self._name_text(node.children[0])
            for scope in reversed(self._scopes):
                if name in scope.var_types:
                    return scope.var_types[name]
            return ""
        if node.data == "funccall":
            return self._call_return_type(node)
        return self._constructor_value_type(node)

    def _var_type(self, name: str) -> str:
        for scope in reversed(self._scopes):
            if name in scope.var_types:
                return scope.var_types[name]
        return ""

    def _constructor_value_type(self, node) -> str:
        if not isinstance(node, Tree) or node.data != "funccall" or not node.children:
            return ""
        callee = node.children[0]
        if isinstance(callee, Tree) and callee.data == "var" and callee.children:
            name = self._name_text(callee.children[0])
            if name in self._class_method_shapes:
                return name
        if isinstance(callee, Tree) and callee.data in {"getattr", "getattr_safe"}:
            name = self._expr_dotted_name(callee)
            if name in self._class_method_shapes:
                return name
        return ""

    def _call_return_type(self, node) -> str:
        if not isinstance(node, Tree) or node.data != "funccall" or not node.children:
            return ""
        constructor_type = self._constructor_value_type(node)
        if constructor_type:
            return constructor_type
        callee = node.children[0]
        target = ""
        if isinstance(callee, Tree) and callee.data == "var" and callee.children:
            target = self._name_text(callee.children[0])
        elif isinstance(callee, Tree) and callee.data in {"getattr", "getattr_safe"}:
            target = self._expr_dotted_name(callee)
        if not target:
            return ""
        method_type = self._method_return_type(target)
        if method_type:
            return method_type
        return self._single_known_return_type(self._func_return_types.get(target, set()))

    def _method_return_type(self, target: str) -> str:
        for class_name in sorted(self._class_method_shapes, key=len, reverse=True):
            prefix = f"{class_name}."
            if not target.startswith(prefix):
                continue
            method_name = target[len(prefix):]
            if "." in method_name:
                continue
            shape = self._class_method_shapes.get(class_name, {}).get(method_name)
            if shape is not None and shape.return_type:
                return shape.return_type
        return ""

    @staticmethod
    def _single_known_return_type(types: set[str]) -> str:
        concrete = {t for t in types if t}
        if len(concrete) == 1:
            return next(iter(concrete))
        return ""

    @staticmethod
    def _annassign_type_root(node: Tree) -> str:
        type_node = SemanticChecker._annassign_type_node(node)
        return SemanticChecker._type_root_name(type_node) if type_node is not None else ""

    @staticmethod
    def _annassign_type_node(node: Tree):
        for child in node.children:
            if isinstance(child, Tree) and child.data == "type_expr":
                return child
        return None

    @staticmethod
    def _annassign_value(node: Tree):
        seen_type = False
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "type_expr":
                seen_type = True
                continue
            if seen_type:
                return child
        return None

    @staticmethod
    def _assign_value(node: Tree):
        if node.data == "annassign":
            return SemanticChecker._annassign_value(node)
        if node.data == "assign" and len(node.children) >= 2:
            for child in reversed(node.children):
                if isinstance(child, Tree):
                    return child
        return None

    @staticmethod
    def _const_type_node(node: Tree):
        for child in node.children[1:]:
            if isinstance(child, Tree) and child.data == "type_expr":
                return child
        return None

    @staticmethod
    def _const_value_node(node: Tree):
        seen_type = False
        for child in node.children[1:]:
            if not isinstance(child, Tree):
                continue
            if child.data == "type_expr":
                seen_type = True
                continue
            if seen_type or SemanticChecker._const_type_node(node) is None:
                return child
        return None

    @staticmethod
    def _call_positional_args(call: Tree) -> list:
        args_node = call.children[1] if len(call.children) > 1 else None
        if not isinstance(args_node, Tree) or args_node.data != "arguments":
            return []
        out: list = []
        for child in args_node.children:
            if isinstance(child, Tree) and child.data == "argvalue" and len(child.children) == 2:
                continue
            if isinstance(child, Tree) and child.data in {"stararg", "starargs", "kwargs"}:
                continue
            out.append(child)
        return out

    def _check_builtin_cast_call(self, call: Tree) -> None:
        name = self._builtin_cast_name(call)
        if not name:
            return
        pos_count, keywords, has_starargs, has_kwargs = self._call_args_shape(call)
        if keywords or has_kwargs:
            self._error(call, "call", f"cast `{name}` does not accept keyword arguments")
            return
        if not has_starargs and pos_count > 1:
            self._error(
                call,
                "call",
                f"cast `{name}` expects at most 1 argument, got {pos_count}",
            )
            return
        args = self._call_positional_args(call)
        if len(args) != 1 or not isinstance(args[0], Tree):
            return
        actual = self._expr_simple_type(args[0])
        if actual is None or self._is_any_type(actual) or self._is_open_type(actual):
            return
        if name in INTEGER_CAST_FUNCS or name in FLOAT_CAST_FUNCS or name == "bool":
            if not self._is_primitive_numeric_type(actual):
                self._error(
                    args[0],
                    "type",
                    f"cast `{name}` expects numeric argument, got `{render_type(actual)}`",
                )
            return
        if name == "string" and not self._string_cast_arg_allowed(actual):
            self._error(
                args[0],
                "type",
                f"cast `string` expects numeric, `str`, or `bytes` argument, got `{render_type(actual)}`",
            )

    def _check_generic_type_args(self, call: Tree) -> None:
        target = self._explicit_generic_call_target(call)
        if not target:
            return
        type_args = self._explicit_type_arg_types(call)
        if type_args is None:
            return
        expected_counts = self._generic_type_arg_counts(target)
        if not expected_counts:
            if target in self._func_sigs or target in self._class_method_shapes:
                self._error(
                    call,
                    "type",
                    f"`{target}` is not generic and does not accept type arguments",
                )
            return
        actual = len(type_args)
        if actual in expected_counts:
            return
        expected = self._format_count_options(expected_counts)
        self._error(
            call,
            "type",
            f"generic `{target}` expects {expected} type argument"
            f"{'' if expected == '1' else 's'}, got {actual}",
        )

    def _builtin_cast_name(self, call: Tree) -> str:
        if not isinstance(call, Tree) or call.data != "funccall" or not call.children:
            return ""
        callee = call.children[0]
        if not isinstance(callee, Tree) or callee.data != "var" or not callee.children:
            return ""
        name = self._name_text(callee.children[0])
        if name not in BUILTIN_CAST_FUNCS:
            return ""
        # A Lam function/class with the same name is a user binding that
        # shadows the built-in. Shadowing is warned elsewhere; avoid
        # layering built-in-only call rules on top of that user call.
        if name in self._func_sigs or name in self._class_method_shapes:
            return ""
        scope = self._resolving_scope(name)
        if scope is not None and (scope.kind != "module" or name in scope.binding_nodes):
            return ""
        return name

    def _explicit_generic_call_target(self, call: Tree) -> str:
        callee = call.children[0] if isinstance(call, Tree) and call.children else None
        if not isinstance(callee, Tree) or callee.data != "getitem" or len(callee.children) < 2:
            return ""
        base = callee.children[0]
        if isinstance(base, Tree) and base.data == "var" and base.children:
            return self._name_text(base.children[0])
        return ""

    def _generic_type_arg_counts(self, target: str) -> set[int]:
        if target in self._class_method_shapes:
            params = self._class_type_params.get(target, ())
            return {len(params)} if params else set()
        counts = {
            len(sig.generic_names)
            for sig in self._func_sigs.get(target, [])
            if sig.generic_names
        }
        return counts

    def _explicit_generic_type_mapping(self, call: Tree, sig: _CallableSig) -> dict[str, Type]:
        if not sig.generic_names:
            return {}
        type_args = self._explicit_type_arg_types(call)
        if type_args is None or len(type_args) != len(sig.generic_names):
            return {}
        return dict(zip(sig.generic_names, type_args))

    def _explicit_type_arg_types(self, call: Tree) -> list[Type] | None:
        callee = call.children[0] if isinstance(call, Tree) and call.children else None
        if not isinstance(callee, Tree) or callee.data != "getitem" or len(callee.children) < 2:
            return None
        texts = [
            text for text in self._explicit_type_arg_texts(callee.children[1])
            if text
        ]
        if not texts:
            return []
        try:
            return [parse_type(text) for text in texts]
        except TypeParseError:
            return None

    def _explicit_type_arg_texts(self, node) -> list[str]:
        if isinstance(node, Tree) and node.data == "subscript_tuple":
            return [self._type_arg_expr_text(child) for child in node.children if child is not None]
        return [self._type_arg_expr_text(node)]

    def _type_arg_expr_text(self, node) -> str:
        if isinstance(node, Token):
            return str(node)
        if not isinstance(node, Tree):
            return ""
        if node.data == "var" and node.children:
            return self._name_text(node.children[0])
        if node.data in {"getattr", "getattr_safe"} and len(node.children) >= 2:
            prefix = self._type_arg_expr_text(node.children[0])
            attr = self._name_text(node.children[1])
            return f"{prefix}.{attr}" if prefix and attr else ""
        if node.data == "getitem" and len(node.children) >= 2:
            base = self._type_arg_expr_text(node.children[0])
            args = ", ".join(self._explicit_type_arg_texts(node.children[1]))
            return f"{base}[{args}]" if base and args else ""
        return ""

    @staticmethod
    def _format_count_options(counts: set[int]) -> str:
        ordered = sorted(counts)
        if not ordered:
            return "0"
        if len(ordered) == 1:
            return str(ordered[0])
        return " or ".join(str(count) for count in ordered)

    # ─── Name resolution ───────────────────────────────────────

    def _is_resolved(self, name: str) -> bool:
        for scope in reversed(self._scopes):
            if name in scope.names:
                # Mark the binding as referenced in whichever scope
                # holds it, so unused-binding warnings can be emitted
                # at scope-pop time. Builtins / stdlib names live
                # only on the module scope, so this also harmlessly
                # records ``print`` etc. — we'll just never check the
                # builtin set against ``used_names`` downstream.
                scope.used_names.add(name)
                return True
        return False

    def _check_definite_read(self, node: Tree, name: str) -> None:
        scope = self._resolving_scope(name)
        if scope is None or scope.kind not in {"function", "block"}:
            return
        if name in scope.definite_names:
            return
        if name in {"_", "self", "cls"}:
            return
        self._error(node, "flow", f"`{name}` may be used before assignment")

    def _resolving_scope(self, name: str) -> _Scope | None:
        for scope in reversed(self._scopes):
            if name in scope.names:
                return scope
        return None

    def _type_storage_scope(self, node: Tree, name: str) -> _Scope:
        if node.data == "annassign":
            return self._scopes[-1]
        return self._resolving_scope(name) or self._scopes[-1]

    def _mark_definite(self, name: str) -> None:
        if not name:
            return
        scope = self._resolving_scope(name)
        if scope is not None:
            scope.definite_names.add(name)

    def _definite_snapshot(self) -> list[set[str]]:
        return [set(scope.definite_names) for scope in self._scopes]

    def _restore_definite(self, snapshot: list[set[str]]) -> None:
        for scope, names in zip(self._scopes, snapshot):
            scope.definite_names = set(names)

    def _type_snapshot(self) -> list[dict[str, str]]:
        return [dict(scope.var_types) for scope in self._scopes]

    def _restore_types(self, snapshot: list[dict[str, str]]) -> None:
        for scope, var_types in zip(self._scopes, snapshot):
            scope.var_types = dict(var_types)

    @staticmethod
    def _intersect_definite_snapshots(
        snapshots: list[list[set[str]]],
        fallback: list[set[str]],
    ) -> list[set[str]]:
        if not snapshots:
            return [set(names) for names in fallback]
        merged: list[set[str]] = []
        max_len = min(len(snap) for snap in snapshots)
        for idx in range(max_len):
            current = set(snapshots[0][idx])
            for snap in snapshots[1:]:
                current &= snap[idx]
            merged.append(current)
        return merged

    @staticmethod
    def _intersect_type_snapshots(
        snapshots: list[list[dict[str, str]]],
        fallback: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        if not snapshots:
            return [dict(types) for types in fallback]
        merged: list[dict[str, str]] = []
        max_len = min(len(snap) for snap in snapshots)
        for idx in range(max_len):
            scope_types: dict[str, str] = {}
            common = set(snapshots[0][idx])
            for snap in snapshots[1:]:
                common &= set(snap[idx])
            for name in common:
                values = {snap[idx][name] for snap in snapshots}
                if len(values) == 1:
                    scope_types[name] = values.pop()
            merged.append(scope_types)
        return merged

    def _nearest_returns_result(self) -> bool:
        """True if the nearest enclosing scope that defines a
        return target (a ``function`` scope, or a block scope
        explicitly marked Result-returning because it lowers to a
        ``do { } catch`` IIFE) declared ``-> Result``.

        Search order walks outward: any intermediate scope flagged
        ``returns_result=True`` short-circuits True (this is how a
        ``do { }`` block wins over the enclosing function), and the
        first enclosing ``function`` scope otherwise decides — its
        return target is the only one a propagated ``*Result`` could
        reach at that depth. If we fall off the stack without ever
        entering a function or a Result-returning block, we're at
        module scope and the answer is unambiguously False.
        """
        for scope in reversed(self._scopes):
            if scope.returns_result:
                return True
            if scope.kind == "function":
                return False
        return False

    def _inside_result_block(self) -> bool:
        """True when the current expression is directly inside a
        Result-returning block scope such as ``do { } catch``.

        Nested function/lambda scopes stop the search because their
        ``?`` propagation target is the nested function, not the outer
        do/catch IIFE.
        """
        for scope in reversed(self._scopes):
            if scope.kind == "function":
                return False
            if scope.returns_result:
                return True
        return False

    def _record_support_import(self, node: Tree) -> None:
        """Record imports required by syntax that lowers to stdlib helpers."""
        if node.data != "import_from":
            return
        if _import_module_name(node) != "lamerrors":
            return
        for imported, local in self._from_import_pairs(node):
            if imported != "Result":
                continue
            if local == "Result":
                self._has_lamerrors_result_import = True
            else:
                self._has_aliased_lamerrors_result_import = True

    def _require_lamerrors_result(self, node, syntax: str) -> None:
        if self._has_lamerrors_result_import:
            return
        if self._has_aliased_lamerrors_result_import:
            self._error(
                node,
                "import",
                f"{syntax} requires unaliased `from lamerrors import Result`; "
                "`Result as ...` is not supported because this syntax lowers "
                "to `Result` helpers",
            )
            return
        self._error(
            node,
            "import",
            f"{syntax} requires `from lamerrors import Result` "
            "because it lowers to `Result` helpers",
        )

    def _check_call_shape(self, call: Tree) -> None:
        target, sigs = self._call_target_sigs(call)
        if not target or not sigs:
            return
        pos_count, keywords, has_starargs, has_kwargs = self._call_args_shape(call)
        duplicate_keywords = self._first_duplicate(keywords)
        if duplicate_keywords:
            self._error(
                call, "call",
                f"duplicate keyword argument `{duplicate_keywords}` in call to `{target}`",
            )
            return
        matching_sigs = [
            sig for sig in sigs
            if self._call_matches_sig(sig, pos_count, keywords, has_starargs, has_kwargs)
        ]
        if matching_sigs:
            self._check_call_arg_types(call, target, matching_sigs)
            return
        for sig in sigs:
            filled_by_pos = set(sig.params[:pos_count])
            repeated = next((kw for kw in keywords if kw in filled_by_pos), None)
            if repeated:
                self._error(
                    call, "call",
                    f"argument `{repeated}` for `{target}` was passed both positionally and by keyword",
                )
                return
        if not has_starargs:
            max_values = [sig.max_pos for sig in sigs if sig.max_pos is not None]
            if max_values and all(pos_count > max_pos for max_pos in max_values):
                max_pos = max(max_values)
                self._error(
                    call, "call",
                    f"too many positional arguments in call to `{target}`: expected at most {max_pos}, got {pos_count}",
                )
                return
        if not has_kwargs:
            for sig in sigs:
                unknown = next((kw for kw in keywords
                                if kw not in sig.params and not sig.accepts_kwargs), None)
                if unknown:
                    suggestion = get_close_matches(unknown, list(sig.params), n=1, cutoff=0.75)
                    msg = f"unknown keyword argument `{unknown}` in call to `{target}`"
                    if suggestion:
                        msg += f" — did you mean `{suggestion[0]}`?"
                    self._error(call, "call", msg)
                    return
        for sig in sigs:
            provided = set(keywords) | set(sig.params[:pos_count])
            missing = [p for p in sig.params[:sig.required_pos] if p not in provided]
            if missing:
                self._error(
                    call, "call",
                    f"missing required argument `{missing[0]}` in call to `{target}`",
                )
                return

    def _call_target_sigs(self, call: Tree) -> tuple[str, list[_CallableSig]]:
        callee = call.children[0] if call.children else None
        if not isinstance(callee, Tree):
            return "", []
        if callee.data == "getitem" and callee.children:
            callee = callee.children[0]
            if not isinstance(callee, Tree):
                return "", []
        if callee.data == "var":
            name = self._name_text(callee.children[0]) if callee.children else ""
            if name in self._class_method_shapes:
                return name, self._constructor_sigs.get(name, [_CallableSig(name, 0, 0, (), False)])
            return name, self._func_sigs.get(name, [])
        if callee.data in {"getattr", "getattr_safe"}:
            receiver_target = self._receiver_method_target(callee)
            if receiver_target:
                if receiver_target.endswith(".init"):
                    class_target = receiver_target.rsplit(".", 1)[0]
                    return receiver_target, self._constructor_sigs.get(class_target, [])
            target = self._expr_dotted_name(callee)
            if target in self._class_method_shapes:
                return target, self._constructor_sigs.get(target, [_CallableSig(target, 0, 0, (), False)])
            return target, self._method_sigs.get(target, [])
        return "", []

    def _receiver_method_target(self, callee: Tree) -> str:
        if not isinstance(callee, Tree) or callee.data not in {"getattr", "getattr_safe"}:
            return ""
        if len(callee.children) < 2:
            return ""
        obj = callee.children[0]
        attr = self._name_text(callee.children[1])
        if not attr:
            return ""
        receiver_type = ""
        if isinstance(obj, Tree) and obj.data == "var" and obj.children:
            receiver_name = self._name_text(obj.children[0])
            if receiver_name == "self" and self._current_class_stack:
                receiver_type = self._current_class_stack[-1]
            else:
                receiver_type = self._var_type(receiver_name)
        if not receiver_type:
            return ""
        if attr in {"init", "__init__"} and self._class_has_direct_member(receiver_type, attr):
            return f"{receiver_type}.init"
        if self._class_has_direct_member(receiver_type, attr):
            return f"{receiver_type}.{attr}"
        sources = self._inherited_member_sources(receiver_type, attr)
        if len(sources) == 1:
            return f"{sources[0]}.{attr}"
        return ""

    def _check_call_arg_types(
        self,
        call: Tree,
        target: str,
        sigs: list[_CallableSig],
    ) -> None:
        attempts = [self._call_type_mismatches(call, sig) for sig in sigs]
        if any(not mismatches for mismatches in attempts):
            return
        mismatch = next((items[0] for items in attempts if items), None)
        if mismatch is None:
            return
        param_name, expected, actual, node = mismatch
        self._error(
            node,
            "type",
            f"argument `{param_name}` to `{target}` has type `{render_type(actual)}`, expected `{render_type(expected)}`",
        )

    def _call_type_mismatches(
        self,
        call: Tree,
        sig: _CallableSig,
    ) -> list[tuple[str, Type, Type, Tree]]:
        if not sig.param_types:
            return []
        generic_mapping = self._explicit_generic_type_mapping(call, sig)
        out: list[tuple[str, Type, Type, Tree]] = []
        for param_name, expected_name, arg_node in self._call_arg_bindings(call, sig):
            if expected_name in {"", "any", "object"}:
                continue
            actual = self._expr_simple_type(arg_node)
            if actual is None:
                continue
            expected = parse_type(expected_name)
            if self._type_mentions_generic(expected, sig.generic_names):
                if not generic_mapping:
                    continue
                expected = self._substitute_generic_type(expected, generic_mapping)
            if is_assignable(expected, actual) or self._nominal_type_assignable(expected, actual):
                continue
            out.append((param_name, expected, actual, arg_node))
        return out

    def _type_mentions_generic(self, type_: Type, names: tuple[str, ...]) -> bool:
        if not names:
            return False
        generic_names = set(names)
        if isinstance(type_, NamedType):
            return type_.name in generic_names
        if isinstance(type_, ListType):
            return self._type_mentions_generic(type_.item, names)
        if isinstance(type_, DictType):
            return (
                self._type_mentions_generic(type_.key, names)
                or self._type_mentions_generic(type_.value, names)
            )
        if isinstance(type_, FuncType):
            return (
                any(self._type_mentions_generic(param, names) for param in type_.params)
                or self._type_mentions_generic(type_.ret, names)
            )
        if isinstance(type_, UnionType):
            return any(self._type_mentions_generic(option, names) for option in type_.options)
        if isinstance(type_, GenericType):
            return any(self._type_mentions_generic(arg, names) for arg in type_.args)
        return False

    def _substitute_generic_type(self, type_: Type, mapping: dict[str, Type]) -> Type:
        if isinstance(type_, NamedType):
            return mapping.get(type_.name, type_)
        if isinstance(type_, ListType):
            return ListType(self._substitute_generic_type(type_.item, mapping))
        if isinstance(type_, DictType):
            return DictType(
                self._substitute_generic_type(type_.key, mapping),
                self._substitute_generic_type(type_.value, mapping),
            )
        if isinstance(type_, FuncType):
            return FuncType(
                tuple(self._substitute_generic_type(param, mapping) for param in type_.params),
                self._substitute_generic_type(type_.ret, mapping),
            )
        if isinstance(type_, UnionType):
            return UnionType(tuple(self._substitute_generic_type(option, mapping)
                                   for option in type_.options))
        if isinstance(type_, GenericType):
            return GenericType(
                type_.base,
                tuple(self._substitute_generic_type(arg, mapping) for arg in type_.args),
            )
        return type_

    def _call_arg_bindings(self, call: Tree, sig: _CallableSig) -> list[tuple[str, str, Tree]]:
        args_node = call.children[1] if len(call.children) > 1 else None
        if not isinstance(args_node, Tree) or args_node.data != "arguments":
            return []
        out: list[tuple[str, str, Tree]] = []
        pos_index = 0
        for child in args_node.children:
            if not isinstance(child, Tree):
                if pos_index < len(sig.params):
                    out.append(self._call_arg_binding(sig, pos_index, child))
                pos_index += 1
                continue
            if child.data == "argvalue" and len(child.children) == 2:
                kw = self._call_keyword_name(child.children[0])
                if kw in sig.params and isinstance(child.children[1], Tree):
                    idx = sig.params.index(kw)
                    out.append(self._call_arg_binding(sig, idx, child.children[1]))
                continue
            if child.data in {"stararg", "starargs", "kwargs"}:
                continue
            if pos_index < len(sig.params):
                out.append(self._call_arg_binding(sig, pos_index, child))
            pos_index += 1
        return [item for item in out if item[2] is not None]

    @staticmethod
    def _call_arg_binding(sig: _CallableSig, index: int, node) -> tuple[str, str, Tree]:
        param_name = sig.params[index] if index < len(sig.params) else "<arg>"
        expected = sig.param_types[index] if index < len(sig.param_types) else "any"
        return (param_name, expected, node)

    @staticmethod
    def _call_args_shape(call: Tree) -> tuple[int, list[str], bool, bool]:
        args_node = call.children[1] if len(call.children) > 1 else None
        pos_count = 0
        keywords: list[str] = []
        has_starargs = False
        has_kwargs = False

        def visit_arg(node) -> None:
            nonlocal pos_count, has_starargs, has_kwargs
            if node is None:
                return
            if not isinstance(node, Tree):
                pos_count += 1
                return
            if node.data == "argvalue":
                if len(node.children) == 2:
                    keywords.append(SemanticChecker._call_keyword_name(node.children[0]))
                else:
                    pos_count += 1
                return
            if node.data == "stararg":
                has_starargs = True
                return
            if node.data == "kwargs":
                has_kwargs = True
                return
            if node.data == "starargs":
                for child in node.children:
                    visit_arg(child)
                return
            if node.data == "arguments":
                for child in node.children:
                    visit_arg(child)
                return
            pos_count += 1

        visit_arg(args_node)
        return pos_count, [kw for kw in keywords if kw], has_starargs, has_kwargs

    @staticmethod
    def _call_keyword_name(node) -> str:
        if isinstance(node, Tree) and node.data == "var" and node.children:
            return SemanticChecker._name_text(node.children[0])
        return SemanticChecker._name_text(node)

    @staticmethod
    def _first_duplicate(values: list[str]) -> str:
        seen: set[str] = set()
        for value in values:
            if value in seen:
                return value
            seen.add(value)
        return ""

    @staticmethod
    def _call_matches_sig(
        sig: _CallableSig,
        pos_count: int,
        keywords: list[str],
        has_starargs: bool,
        has_kwargs: bool,
    ) -> bool:
        if not has_starargs and sig.max_pos is not None and pos_count > sig.max_pos:
            return False
        filled_by_pos = set(sig.params[:pos_count])
        if any(kw in filled_by_pos for kw in keywords):
            return False
        if not has_kwargs and any(kw not in sig.params and not sig.accepts_kwargs
                                  for kw in keywords):
            return False
        provided = set(keywords) | filled_by_pos
        return all(p in provided for p in sig.params[:sig.required_pos])

    def _check_dropped_return(self, expr_stmt: Tree) -> None:
        """Emit a warning when ``expr_stmt`` is a bare function call
        that drops a known non-void return value.

        Covered call shapes:

        * ``foo(...)`` where ``foo`` is a top-level ``func`` declared
          in this file with a non-``None`` return annotation.
        * ``Class.method(...)`` (static or bare-class receiver) where
          ``Class`` is a user-defined class in this file and its
          ``method`` declares a non-void return.

        Intentionally *not* covered:

        * Instance calls ``obj.method()`` — we'd need type inference
          to know the receiver's class, and the current semantic
          walker is deliberately flow-insensitive. The transpiler
          has the inference tables but runs too late to participate
          in a pre-emission warning.
        * Calls to imported / library-defined functions — we'd need
          their return-type map, which isn't shared with this pass.
        * Unannotated functions (``func f() {...}``) — treated as
          void because users who cared about the return would have
          annotated it. Avoids a large false-positive surface on
          early-draft code.

        The opt-out for every shape we *do* cover is the standard
        Lam idiom: ``_ = foo(x)``, which parses as an assignment
        and never reaches this method.
        """
        if not expr_stmt.children:
            return
        expr = expr_stmt.children[0]
        if not isinstance(expr, Tree) or expr.data != "funccall":
            return
        callee = expr.children[0] if expr.children else None
        if not isinstance(callee, Tree):
            return

        # Top-level function call: ``foo(...)``.
        if callee.data == "var":
            name = self._name_text(callee.children[0]) if callee.children else ""
            if name and name in self._nonvoid_funcs:
                self._warning(
                    expr_stmt,
                    "unused",
                    self._dropped_return_warning_message(
                        name,
                        self._single_known_return_type(self._func_return_types.get(name, set())),
                    ),
                )
            return

        # Static / class-qualified call: ``Class.method(...)``.
        if callee.data in {"getattr", "getattr_safe"}:
            key = self._expr_dotted_name(callee)
            if key in self._nonvoid_methods:
                self._warning(
                    expr_stmt,
                    "unused",
                    self._dropped_return_warning_message(key, self._method_return_type(key)),
                )

    @staticmethod
    def _dropped_return_warning_message(target: str, return_type: str) -> str:
        if return_type in {"Result", "Option"}:
            return (
                f"dropped `{return_type}` from `{target}()`; "
                "handle it or assign to `_`"
            )
        return f"dropped return value of `{target}()` (assign to `_` to silence)"

    def _mark_fstring_uses(self, node: Tree) -> None:
        """Scan an ``fstring`` AST node's literal text and mark
        every identifier inside ``{...}`` interpolation slots as
        used (via :meth:`_is_resolved`).

        Without this hook, a parameter referenced only as
        ``f"… {name} …"`` would be invisible to the AST walker
        (the grammar models the entire f-string as one opaque
        token), and the unused-parameter check would raise a
        false positive on it. Format-spec digits and conversion
        flags get scanned too; they almost never collide with
        real binding names, and even if they do the only effect
        is silencing a warning we'd otherwise emit.
        """
        if not node.children:
            return
        raw = str(node.children[0])
        # Strip the ``f"`` / ``f'`` / ``f"""`` / ``f'''`` prefix +
        # matching suffix. The triple-quoted forms are checked
        # first so the single-char prefix branch doesn't shadow
        # them (``f"""`` legitimately starts with ``f"``).
        if raw.startswith('f"""') or raw.startswith("f'''"):
            body = raw[4:-3]
        elif raw.startswith('f"') or raw.startswith("f'"):
            body = raw[2:-1]
        else:
            return

        i, n = 0, len(body)
        while i < n:
            ch = body[i]
            if ch == '{':
                # ``{{`` is an escaped literal brace — skip it
                # whole so we don't treat the second ``{`` as the
                # start of a slot.
                if i + 1 < n and body[i + 1] == '{':
                    i += 2
                    continue
                depth = 1
                j = i + 1
                while j < n and depth > 0:
                    if body[j] == '{':
                        depth += 1
                    elif body[j] == '}':
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if depth != 0:
                    return
                slot = body[i + 1:j]
                for m in _FSTRING_IDENT_RE.finditer(slot):
                    self._is_resolved(m.group(0))
                i = j + 1
            elif ch == '}':
                # Escaped ``}}`` is a literal brace too.
                i += 2 if (i + 1 < n and body[i + 1] == '}') else 1
            else:
                i += 1

    def _mark_go_marker_uses(self, node: Tree) -> bool:
        """Mark visible Lam names referenced by preprocessed ``go!`` text."""
        if not node.children:
            return False
        callee = node.children[0]
        if not isinstance(callee, Tree) or callee.data != "var" or not callee.children:
            return False
        func_name = self._name_text(callee.children[0])
        if func_name not in {"__go_block__", "__go_inline__"}:
            return False
        raw = self._go_blocks.get(self._go_marker_id(node), "")
        self._check_go_boundary_diagnostics(node, raw)
        for match in _GO_IDENT_RE.finditer(raw):
            self._is_resolved(match.group(0))
        return True

    def _check_go_boundary_diagnostics(self, node: Tree, raw: str) -> None:
        if self._self_is_available():
            return
        scan = self._go_text_without_comments_and_strings(raw)
        if _GO_SELF_RE.search(scan):
            self._error(
                node,
                "go-block",
                "`self` is only rewritten inside class methods; use a Go identifier available in this scope",
            )
            return
        if (
            _GO_RECEIVER_ALIAS_RE.search(scan)
            and not _GO_LOCAL_S_DECL_RE.search(scan)
            and not self._name_in_scope("s")
        ):
            self._error(
                node,
                "go-block",
                "`s` is only the receiver alias inside class methods; use the function parameter or define a Go variable",
            )

    def _name_in_scope(self, name: str) -> bool:
        return any(name in scope.names for scope in reversed(self._scopes))

    @staticmethod
    def _go_text_without_comments_and_strings(raw: str) -> str:
        out: list[str] = []
        i = 0
        n = len(raw)
        while i < n:
            if raw.startswith("//", i):
                j = raw.find("\n", i + 2)
                if j == -1:
                    break
                out.append("\n")
                i = j + 1
                continue
            if raw.startswith("/*", i):
                j = raw.find("*/", i + 2)
                if j == -1:
                    break
                out.append("\n" * raw[i:j + 2].count("\n"))
                i = j + 2
                continue
            ch = raw[i]
            if ch in {'"', "'", "`"}:
                quote = ch
                out.append(" ")
                i += 1
                while i < n:
                    cur = raw[i]
                    if cur == "\n":
                        out.append("\n")
                    elif cur == "\\" and quote != "`":
                        i += 1
                    elif cur == quote:
                        i += 1
                        break
                    i += 1
                continue
            out.append(ch)
            i += 1
        return "".join(out)

    @staticmethod
    def _go_marker_id(node: Tree) -> str:
        if len(node.children) < 2:
            return ""
        args_node = node.children[1]
        if not isinstance(args_node, Tree):
            return ""
        for child in args_node.children:
            raw = SemanticChecker._literal_string_text(child)
            if raw:
                return raw
        return ""

    @staticmethod
    def _literal_string_text(node) -> str:
        if isinstance(node, Token):
            text = str(node)
        elif isinstance(node, Tree):
            if node.data in {"string", "literal"} and node.children:
                return SemanticChecker._literal_string_text(node.children[0])
            for child in node.children:
                text = SemanticChecker._literal_string_text(child)
                if text:
                    return text
            return ""
        else:
            return ""
        if len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0]:
            return text[1:-1]
        return text

    def _suggest_name(self, name: str) -> Optional[str]:
        """Return an in-scope name similar to ``name`` (Levenshtein-ish
        via :func:`difflib.get_close_matches`) or ``None`` if nothing
        close enough exists. We only return a suggestion that differs
        from the miss to avoid ``did you mean `x`?`` when the user
        literally typed ``x``.

        The pool is every scope's ``names`` plus the builtin /
        constants / stdlib sets, so suggestions fire for both local
        typos (``prnit`` → ``print``) and misspelt stdlib names
        (``lamstring`` → ``lamstrings``).
        """
        priority_pools = [
            LAM_KEYWORDS,
            BUILTIN_FUNCS | BUILTIN_CONSTANTS,
            STDLIB_MODULES,
        ]
        for pool in priority_pools:
            matches = get_close_matches(name, sorted(pool - {name}), n=1, cutoff=0.75)
            if matches:
                return matches[0]

        pool: Set[str] = set()
        for scope in self._scopes:
            pool |= scope.names
        pool.discard(name)
        matches = get_close_matches(name, sorted(pool), n=1, cutoff=0.75)
        return matches[0] if matches else None

    def _check_shadow_builtin_or_import(self, node: Tree, name: str) -> None:
        """Flag an assignment that would rebind a builtin / constant
        / imported module / top-level class or function at *module*
        scope.

        We only emit when the current scope is the module (global):
        inside a function body, shadowing a builtin is a well-known
        and idiomatic pattern (``type = typeof(x)``), so a diagnostic
        there would generate noise. A global rebinding of ``print``
        is always a bug — the transpiler would emit a ``var print``
        that conflicts with the Go-side helper.
        """
        if not self._scopes or self._scopes[-1].kind != "module":
            return
        if name in BUILTIN_FUNCS:
            self._error(
                node, "shadow",
                f"assignment to builtin `{name}` shadows the built-in "
                f"of the same name",
            )
            return
        # ``_module_seen`` is populated by the dupe-check collector
        # and maps each top-level name to its kind. An assignment
        # that collides with an import / class / func at global
        # scope would generate either a ``redeclared in this block``
        # Go error or silent data corruption (in the func/arity case).
        kind = getattr(self, "_module_seen", {}).get(name)
        if kind == "import":
            self._error(
                node, "shadow",
                f"assignment to `{name}` shadows the imported module "
                f"of the same name",
            )
        elif kind == "class":
            self._error(
                node, "shadow",
                f"assignment to `{name}` shadows the class of the "
                f"same name",
            )
        elif kind == "func":
            self._error(
                node, "shadow",
                f"assignment to `{name}` shadows the function of "
                f"the same name",
            )

    def _check_import_binding_conflict(self, node: Tree, name: str) -> None:
        if not name or name == "_":
            return
        if name in BUILTIN_FUNCS:
            self._error(
                node, "import",
                f"import binding `{name}` shadows the built-in `{name}`",
            )
            return
        if name in BUILTIN_CONSTANTS or name in PYTHON_EXCEPTIONS:
            self._error(
                node, "import",
                f"import binding `{name}` conflicts with the built-in name `{name}`",
            )
            return
        kind = getattr(self, "_module_seen", {}).get(name)
        if kind and kind != "import":
            self._error(
                node, "import",
                f"import binding `{name}` conflicts with the existing {kind} `{name}`",
            )

    def _check_go_reserved(self, node: Tree, name: str) -> None:
        """Flag an identifier that is a Go keyword (or predeclared
        name) and would produce a noisy Go build error if emitted
        verbatim. We allow the assignment to go through and still
        declare the name downstream so the rest of the file still
        checks cleanly — it's only the user-facing identifier that
        needs to change.
        """
        if name in GO_ONLY_KEYWORDS:
            self._error(
                node, "go_reserved",
                f"`{name}` is a reserved identifier in Go and cannot "
                f"be used as a Lam name (the Go compiler would reject "
                f"the emitted code)",
            )
            return
        self._check_compiler_reserved_prefix(node, name)

    def _check_go_reserved_funcdef(self, node: Tree, name: str) -> None:
        if name not in GO_ONLY_KEYWORDS:
            self._check_compiler_reserved_prefix(node, name)
            return
        if self._funcdef_emits_raw_go_name(node):
            self._check_go_reserved(node, name)
            return
        self._check_compiler_reserved_prefix(node, name)

    def _check_go_reserved_type_decl(self, node: Tree, name: str) -> None:
        # Classes and interfaces lower through Go public-name casing
        # (`select` -> `Select`), so Go-only keywords are safe here.
        self._check_compiler_reserved_prefix(node, name)

    def _funcdef_emits_raw_go_name(self, node: Tree) -> bool:
        if self._current_class_stack:
            # Public instance methods are cased (`select` -> `Select`).
            # Static methods are namespaced (`Class_select`) before
            # casing. A private instance method named `select` would
            # still emit raw `select`, so keep that diagnostic.
            return (
                self._funcdef_is_private(node)
                and not self._funcdef_is_static(node)
            )
        if self._is_nested_funcdef(node):
            # Non-generic nested functions emit a closure binding named
            # exactly like the Lam function (`select := func...`).
            return True
        # Top-level public functions are cased (`select` -> `Select`);
        # private top-level functions keep the lowercase raw spelling.
        return self._funcdef_is_private(node)

    def _inside_class_member_scope(self) -> bool:
        return bool(self._scopes and self._scopes[-1].kind == "class")

    def _check_compiler_reserved_prefix(self, node: Tree, name: str) -> None:
        prefix = self._compiler_reserved_prefix(name)
        if prefix:
            self._warning(
                node,
                "name",
                f"`{name}` uses compiler-reserved prefix `{prefix}`; "
                "rename it to avoid collisions with generated code",
            )

    @staticmethod
    def _compiler_reserved_prefix(name: str) -> str:
        if not name:
            return ""
        return next(
            (prefix for prefix in COMPILER_RESERVED_PREFIXES if name.startswith(prefix)),
            "",
        )

    def _record_binding_node(self, name: str, node: Tree) -> None:
        if not self._scopes or not name:
            return
        scope = self._scopes[-1]
        if scope.kind not in {"function", "block"}:
            return
        scope.binding_nodes.setdefault(name, node)

    def _is_const(self, name: str) -> bool:
        """``True`` if ``name`` is currently visible as a const binding."""
        for scope in reversed(self._scopes):
            if name in scope.const_names:
                return True
        return False

    # ─── Unreachable code detection ────────────────────────────

    # Statement kinds that unconditionally transfer control out of
    # the current suite. Anything after one of these at the same
    # block level is dead code the Go compiler would silently accept
    # (Go only flags ``unreachable statement`` in very narrow cases),
    # so we catch it here instead. We intentionally *don't* include
    # ``if``/``match`` even when every branch happens to terminate —
    # detecting that reliably requires a flow-sensitive analysis
    # whose false-positive rate outweighs the benefit for Lam's
    # typical code shapes.
    _FLOW_TERMINATORS: Set[str] = {
        "return_stmt", "break_stmt", "continue_stmt", "raise_stmt",
    }

    def _walk_suite_stmts(self, stmts) -> None:
        """Visit a list of statements, emitting an ``unreachable``
        diagnostic for the first statement that comes after an
        unconditional flow terminator. We only emit once per suite
        — listing every dead statement produces pages of noise in
        the common "commented-out block" case.
        """
        seen_terminator = False
        for stmt in stmts:
            if seen_terminator:
                # Skip ``simple_stmt`` wrappers to point the cursor
                # at the real statement underneath.
                target = stmt
                while (isinstance(target, Tree) and target.data == "simple_stmt"
                       and target.children):
                    sub = target.children[0]
                    if isinstance(sub, Tree):
                        target = sub
                    else:
                        break
                self._error(
                    target if isinstance(target, Tree) else stmt,
                    "unreachable",
                    "unreachable code after flow statement",
                )
                # Still visit it so any errors inside are still
                # caught (e.g. dead code referencing an undefined
                # name is still a problem worth knowing about), but
                # don't keep emitting "unreachable" for every
                # subsequent statement.
                self._visit_stmt(stmt)
                seen_terminator = False  # only report once per suite
                continue
            self._visit_stmt(stmt)
            if isinstance(stmt, Tree):
                if stmt.data in self._FLOW_TERMINATORS:
                    seen_terminator = True
                elif stmt.data == "simple_stmt":
                    for sub in stmt.children:
                        if isinstance(sub, Tree) and sub.data in self._FLOW_TERMINATORS:
                            seen_terminator = True
                            break

    # ─── Const handling ────────────────────────────────────────

    def _visit_const_stmt(self, node: Tree) -> None:
        """Validate a ``const NAME [: TYPE] = EXPR`` declaration.

        Children, in order: ``name`` tree, optional ``type_expr`` (or
        ``None``), and the RHS ``test``. We walk the RHS *before*
        registering the new name so a self-referencing ``const x = x``
        still fails the undefined-name check.
        """
        if not node.children:
            return
        name = self._name_text(node.children[0])
        self._check_type_annotations(node)
        self._check_const_value_type(node)
        for c in node.children[1:]:
            if isinstance(c, Tree):
                self._visit_expr_subtree(c)
        if not name:
            return
        if self._is_const(name):
            self._error(
                node, "const",
                f"redeclaration of constant `{name}`",
            )
            return
        if self._scopes:
            self._scopes[-1].names.add(name)
            self._scopes[-1].const_names.add(name)
            self._record_binding_node(name, node)
            self._mark_definite(name)

    def _visit_assignment_values(self, node: Tree) -> None:
        if not isinstance(node, Tree):
            return
        if node.data in {"assign_stmt", "augassign"}:
            for child in node.children:
                if isinstance(child, Tree):
                    self._visit_assignment_values(child)
            return
        if node.data == "assign":
            for child in node.children[1:]:
                self._visit_expr_subtree(child)
            return
        if node.data == "annassign":
            seen_type = False
            for child in node.children:
                if not isinstance(child, Tree):
                    continue
                if child.data == "type_expr":
                    seen_type = True
                    continue
                if seen_type:
                    self._visit_expr_subtree(child)
            return
        self._visit_expr_subtree(node)

    @staticmethod
    def _const_target_name(node: Tree) -> str:
        """Extract the bound name from a ``const_stmt`` node."""
        if not node.children:
            return ""
        return SemanticChecker._name_text(node.children[0])

    # ─── Block-level def collection ────────────────────────────

    def _collect_block_defs(self, suite: Tree, scope: _Scope) -> None:
        """Pre-scan a suite, registering every binding it introduces."""
        if not isinstance(suite, Tree):
            return
        for stmt in self._suite_stmts(suite):
            self._collect_stmt_defs(stmt, scope)

    def _collect_stmt_defs(self, stmt, scope: _Scope) -> None:
        if not isinstance(stmt, Tree):
            return
        d = stmt.data
        if d in ("assign_stmt", "annassign", "assign", "augassign"):
            for n in self._assign_targets(stmt):
                if self._stmt_def_declares_scope(stmt, scope, n):
                    scope.names.add(n)
            return
        if d == "const_stmt":
            # Mirror the module-scope behaviour: register the name for
            # forward-reference resolution but leave the const flag to
            # ``_visit_const_stmt`` so duplicate declarations can be
            # caught.
            n = self._const_target_name(stmt)
            if n:
                scope.names.add(n)
            return
        if d == "for_stmt":
            target = stmt.children[0] if stmt.children else None
            for n in self._for_target_names(target):
                scope.names.add(n)
            # The loop body's defs are collected when we enter it.
            return
        if d == "with_stmt":
            for c in stmt.children:
                if isinstance(c, Tree) and c.data == "with_items":
                    for item in c.children:
                        if isinstance(item, Tree) and item.data == "with_item":
                            for sub in item.children[1:]:
                                if isinstance(sub, Tree) and sub.data == "name":
                                    scope.names.add(self._name_text(sub))
            return
        if d == "funcdef":
            name = self._funcdef_name(stmt)
            if name:
                scope.names.add(name)
            return
        if d == "classdef":
            name = self._classdef_name(stmt)
            if name:
                scope.names.add(name)
            return
        if d in ("import_from", "import_name"):
            for n in self._import_bindings(stmt):
                scope.names.add(n)
            return
        if d == "import_stmt":
            for inner in stmt.children:
                self._collect_stmt_defs(inner, scope)
            return
        if d == "if_stmt" or d == "while_stmt":
            # Names assigned inside an if/while body are conservatively
            # promoted to the enclosing block so subsequent statements
            # don't false-positive (Lam doesn't enforce strict block
            # scoping for names introduced by control flow).
            for c in stmt.children:
                if isinstance(c, Tree) and c.data == "suite":
                    self._collect_block_defs(c, scope)
            return
        if d == "try_stmt" or d == "try_finally":
            for c in stmt.children:
                if isinstance(c, Tree):
                    if c.data == "suite":
                        self._collect_block_defs(c, scope)
                    elif c.data == "catch_clauses":
                        for cc in c.children:
                            if isinstance(cc, Tree) and cc.data == "catch_clause":
                                for sub in cc.children:
                                    if isinstance(sub, Tree) and sub.data == "suite":
                                        self._collect_block_defs(sub, scope)
                    elif c.data == "finally":
                        for sub in c.children:
                            if isinstance(sub, Tree) and sub.data == "suite":
                                self._collect_block_defs(sub, scope)
            return
        if d == "match_stmt":
            for c in stmt.children:
                if isinstance(c, Tree):
                    for sub in c.children:
                        if isinstance(sub, Tree) and sub.data == "suite":
                            self._collect_block_defs(sub, scope)
            return
        if d == "simple_stmt":
            for sub in stmt.children:
                self._collect_stmt_defs(sub, scope)
            return

    def _stmt_def_declares_scope(self, stmt: Tree, scope: _Scope, name: str) -> bool:
        if not name:
            return False
        if stmt.data == "annassign" or name in scope.names:
            return True
        return not any(name in outer.names for outer in self._scopes)

    def _collect_pattern_names(self, node, scope: _Scope) -> None:
        """Recursively grab capture names from a match pattern."""
        if not isinstance(node, Tree):
            return
        if node.data == "as_pattern" and len(node.children) >= 2:
            tail = node.children[-1]
            if isinstance(tail, Tree) and tail.data == "name":
                scope.names.add(self._name_text(tail))
            elif isinstance(tail, Token):
                scope.names.add(str(tail))
        if node.data in ("capture_pattern", "name_pattern"):
            for c in node.children:
                if isinstance(c, Tree) and c.data == "name":
                    scope.names.add(self._name_text(c))
                elif isinstance(c, Token):
                    scope.names.add(str(c))
        for c in node.children:
            self._collect_pattern_names(c, scope)

    # ─── Funcdef / param helpers ───────────────────────────────

    @staticmethod
    def _funcdef_params(node: Tree) -> Optional[Tree]:
        for c in node.children:
            if isinstance(c, Tree) and c.data == "typed_parameters":
                return c
        return None

    @staticmethod
    def _funcdef_is_private(node: Tree) -> bool:
        return any(isinstance(c, Token) and c.type == "PRIVATE" for c in node.children)

    @staticmethod
    def _funcdef_is_static(node: Tree) -> bool:
        return any(isinstance(c, Token) and c.type == "STATIC_FUNC" for c in node.children)

    def _self_is_available(self) -> bool:
        return any(scope.kind == "function" and "self" in scope.names for scope in reversed(self._scopes))

    def _collect_class_members(self, class_name: str, classdef: Tree) -> None:
        members = self._class_members.setdefault(class_name, set())
        fields = self._class_fields.setdefault(class_name, set())
        static_methods = self._class_static_methods.setdefault(class_name, set())
        instance_methods = self._class_instance_methods.setdefault(class_name, set())
        private_methods = self._class_private_methods.setdefault(class_name, set())
        suite = self._suite_node(classdef)
        if suite is None:
            return
        for stmt in self._suite_stmts(suite):
            if not isinstance(stmt, Tree):
                continue
            target = stmt
            if target.data == "decorated":
                for child in target.children:
                    if isinstance(child, Tree) and child.data == "funcdef":
                        target = child
                        break
            if target.data == "funcdef":
                name = self._funcdef_name(target)
                if name:
                    members.add(name)
                    if self._funcdef_is_static(target):
                        static_methods.add(name)
                    else:
                        instance_methods.add(name)
                    if self._funcdef_is_private(target):
                        private_methods.add(name)
                if name in {"__init__", "init"}:
                    self._collect_init_self_fields(target, members, fields)
                continue
            if target.data in {"annassign", "assign_stmt", "assign"}:
                for name in self._class_field_names_from_stmt(target):
                    members.add(name)
                    fields.add(name)

    def _collect_init_self_fields(
        self,
        funcdef: Tree,
        members: set[str],
        fields: set[str],
    ) -> None:
        suite = self._suite_node(funcdef)
        if suite is None:
            return
        for stmt in self._suite_stmts(suite):
            if not isinstance(stmt, Tree):
                continue
            for name in self._self_field_assignments(stmt):
                members.add(name)
                fields.add(name)

    def _class_field_names_from_stmt(self, node: Tree) -> list[str]:
        names: list[str] = []

        def scan(target) -> None:
            if not isinstance(target, Tree):
                return
            if target.data == "var" and target.children:
                names.append(self._name_text(target.children[0]))
                return
            if target.data == "annassign":
                for child in target.children:
                    if isinstance(child, Tree) and child.data == "type_expr":
                        break
                    scan(child)
                return
            if target.data in {"assign_stmt", "assign"} and target.children:
                scan(target.children[0])

        scan(node)
        return [name for name in names if name]

    def _self_field_assignments(self, node: Tree) -> list[str]:
        names: list[str] = []

        def scan(target) -> None:
            if not isinstance(target, Tree):
                return
            if target.data == "getattr" and len(target.children) >= 2:
                obj = target.children[0]
                if isinstance(obj, Tree) and obj.data == "var" and obj.children:
                    if self._name_text(obj.children[0]) == "self":
                        name = self._name_text(target.children[1])
                        if name:
                            names.append(name)
                return
            if target.data == "annassign":
                for child in target.children:
                    if isinstance(child, Tree) and child.data == "type_expr":
                        break
                    scan(child)
                return
            if target.data in {"assign_stmt", "assign"} and target.children:
                scan(target.children[0])

        scan(node)
        return names

    def _collect_interface_methods(self, node: Tree) -> dict[str, _MethodShape]:
        methods: dict[str, _MethodShape] = {}
        for child in node.children[1:]:
            if not isinstance(child, Tree) or child.data != "interface_method":
                continue
            shape = self._interface_method_shape(child)
            if shape is not None:
                methods[shape.name] = shape
        return methods

    def _collect_class_method_shapes(self, class_name: str, classdef: Tree) -> None:
        methods: dict[str, _MethodShape] = {}
        suite = self._suite_node(classdef)
        if suite is None:
            self._class_method_shapes[class_name] = methods
            return
        for stmt in self._suite_stmts(suite):
            if not isinstance(stmt, Tree):
                continue
            target = stmt
            if target.data == "decorated":
                for child in target.children:
                    if isinstance(child, Tree) and child.data == "funcdef":
                        target = child
                        break
            if target.data != "funcdef":
                continue
            shape = self._funcdef_method_shape(target, skip_self=True)
            if shape is not None:
                methods[shape.name] = shape
        self._class_method_shapes[class_name] = methods

    def _interface_method_shape(self, node: Tree) -> Optional[_MethodShape]:
        name = ""
        params_node = None
        return_type = ""
        return_type_text = ""
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "name" and not name:
                name = self._name_text(child)
            elif child.data == "typed_parameters":
                params_node = child
            elif child.data in {"single_return_type", "multi_return_type"}:
                return_type = self._return_type_root(child)
                return_type_text = render_type(self._return_type_model(child))
        if not name:
            return None
        return _MethodShape(
            name,
            self._param_root_types(params_node, skip_self=False),
            return_type,
            self._param_type_texts(params_node, skip_self=False),
            return_type_text,
        )

    def _funcdef_method_shape(self, node: Tree, *, skip_self: bool) -> Optional[_MethodShape]:
        name = self._funcdef_name(node)
        if not name:
            return None
        return_type = ""
        return_type_text = ""
        for child in node.children:
            if isinstance(child, Tree) and child.data in {"single_return_type", "multi_return_type"}:
                return_type = self._return_type_root(child)
                return_type_text = render_type(self._return_type_model(child))
                break
        return _MethodShape(
            name,
            self._param_root_types(self._funcdef_params(node), skip_self=skip_self),
            return_type,
            self._param_type_texts(self._funcdef_params(node), skip_self=skip_self),
            return_type_text,
        )

    def _collect_constructor_sigs(self, class_name: str, classdef: Tree) -> None:
        sigs: list[_CallableSig] = []
        class_generics = tuple(self._funcdef_type_params(classdef))
        suite = self._suite_node(classdef)
        if suite is not None:
            for stmt in self._suite_stmts(suite):
                if not isinstance(stmt, Tree):
                    continue
                target = stmt
                if target.data == "decorated":
                    for child in target.children:
                        if isinstance(child, Tree) and child.data == "funcdef":
                            target = child
                            break
                if target.data != "funcdef":
                    continue
                name = self._funcdef_name(target)
                if name in {"__init__", "init"}:
                    sigs.append(self._callable_signature_from_params(
                        self._funcdef_params(target),
                        class_name,
                        skip_self=True,
                        generic_names=class_generics,
                    ))
        self._constructor_sigs[class_name] = sigs or [_CallableSig(class_name, 0, 0, (), False)]

    def _collect_nonvoid_methods(self, class_name: str, classdef: Tree) -> None:
        """Walk ``classdef``'s body and record the ``Class.method``
        pairs whose signatures declare a non-void return. We walk
        the full suite so instance + static + private methods are all
        treated the same way (the dropped-return warning doesn't
        care about visibility or receiver shape — only about whether
        the call site is discarding a promised value).
        """
        suite = self._suite_node(classdef)
        if suite is None:
            return
        for stmt in self._suite_stmts(suite):
            if not isinstance(stmt, Tree):
                continue
            # ``decorated`` wraps the real funcdef; unwrap one level.
            fn_node = stmt
            if stmt.data == "decorated":
                for c in stmt.children:
                    if isinstance(c, Tree) and c.data == "funcdef":
                        fn_node = c
                        break
                else:
                    continue
            if fn_node.data != "funcdef":
                continue
            name = self._funcdef_name(fn_node)
            if not name:
                continue
            if self._funcdef_has_nonvoid_return(fn_node):
                self._nonvoid_methods.add(f"{class_name}.{name}")
            return_type_text = self._funcdef_return_type_text(fn_node)
            if return_type_text:
                self._method_return_type_texts.setdefault(f"{class_name}.{name}", set()).add(return_type_text)
            generic_names = tuple(dict.fromkeys(
                [*self._funcdef_type_params(classdef), *self._funcdef_type_params(fn_node)]
            ))
            sig = self._callable_signature_from_params(
                self._funcdef_params(fn_node),
                f"{class_name}.{name}",
                skip_self=False,
                generic_names=generic_names,
            )
            if sig is not None:
                self._method_sigs.setdefault(f"{class_name}.{name}", []).append(sig)

    def _check_function_returns(self, funcdef: Tree, suite_node: Tree) -> None:
        name = self._funcdef_name(funcdef) or "<anonymous>"
        returns = list(self._return_stmts_in(suite_node))
        expected_type = self._funcdef_return_type_root(funcdef)
        expected_type_node = self._funcdef_return_type_node(funcdef)
        expected_model = (
            self._return_type_model(expected_type_node)
            if expected_type_node is not None else None
        )
        if self._funcdef_has_nonvoid_return(funcdef):
            if not returns:
                if self._suite_has_go_block(suite_node) or self._suite_has_yield(suite_node):
                    return
                self._error(
                    funcdef, "return",
                    f"function `{name}` declares a return type but does not return a value",
                )
                return
            if (not self._suite_has_go_block(suite_node)
                    and not self._suite_has_yield(suite_node)
                    and not self._suite_definitely_terminates(suite_node)):
                self._error(
                    funcdef, "return",
                    f"not all paths in `{name}` return a value",
                )
            for ret in returns:
                if not self._return_has_value(ret):
                    self._error(
                        ret, "return",
                        f"`return` in `{name}` must return a value because the function declares a return type",
                    )
                    continue
                actual_model = self._return_value_type(ret)
                actual_type = render_type(actual_model) if actual_model is not None else ""
                expected_name = render_type(expected_model) if expected_model is not None else expected_type
                if (actual_model is not None
                        and expected_model is not None
                        and not is_assignable(expected_model, actual_model)
                        and not self._nominal_type_assignable(expected_model, actual_model)):
                    self._error(
                        ret, "return",
                        f"function `{name}` returns `{actual_type}`, expected `{expected_name}`",
                    )
            return
        if self._funcdef_returns_none(funcdef):
            for ret in returns:
                if self._return_has_value(ret):
                    self._error(
                        ret, "return",
                        f"`return` in `{name}` cannot return a value because the function returns `None`",
                    )

    def _suite_definitely_terminates(self, suite_node: Tree) -> bool:
        for stmt in self._suite_stmts(suite_node):
            if self._stmt_definitely_terminates(stmt):
                return True
        return False

    def _stmt_definitely_terminates(self, node) -> bool:
        if not isinstance(node, Tree):
            return False
        if node.data == "simple_stmt":
            return any(self._stmt_definitely_terminates(child) for child in node.children)
        if node.data in {"return_stmt", "raise_stmt"}:
            return True
        if node.data == "if_stmt":
            return self._if_stmt_definitely_terminates(node)
        if node.data == "match_stmt":
            return self._match_stmt_definitely_terminates(node)
        if node.data == "try_stmt":
            return self._try_stmt_definitely_terminates(node)
        return False

    def _if_stmt_definitely_terminates(self, node: Tree) -> bool:
        suites = []
        direct_suite_count = 0
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "suite":
                direct_suite_count += 1
                suites.append(child)
            elif child.data == "elifs":
                for elif_node in child.children:
                    if not isinstance(elif_node, Tree) or elif_node.data != "elif_":
                        continue
                    elif_suite = next(
                        (sub for sub in elif_node.children
                         if isinstance(sub, Tree) and sub.data == "suite"),
                        None,
                    )
                    if elif_suite is not None:
                        suites.append(elif_suite)
            elif child.data == "elif_":
                elif_suite = next(
                    (sub for sub in child.children
                     if isinstance(sub, Tree) and sub.data == "suite"),
                    None,
                )
                if elif_suite is not None:
                    suites.append(elif_suite)
        if direct_suite_count < 2:
            return False
        return bool(suites) and all(self._suite_definitely_terminates(s) for s in suites)

    def _match_stmt_definitely_terminates(self, node: Tree) -> bool:
        suites = []
        has_wildcard = False
        for child in node.children:
            if not isinstance(child, Tree) or child.data != "case":
                continue
            suite = None
            case_has_wildcard = False
            for sub in child.children:
                if isinstance(sub, Tree) and sub.data == "suite":
                    suite = sub
                elif self._pattern_is_wildcard(sub):
                    case_has_wildcard = True
            if suite is not None:
                suites.append(suite)
            if case_has_wildcard:
                has_wildcard = True
        return has_wildcard and bool(suites) and all(
            self._suite_definitely_terminates(s) for s in suites
        )

    def _try_stmt_definitely_terminates(self, node: Tree) -> bool:
        suites = []
        has_catch = False
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "suite" and not suites:
                suites.append(child)
            elif child.data == "catch_clauses":
                for catch in child.children:
                    if not isinstance(catch, Tree) or catch.data != "catch_clause":
                        continue
                    catch_suite = next(
                        (sub for sub in catch.children
                         if isinstance(sub, Tree) and sub.data == "suite"),
                        None,
                    )
                    if catch_suite is not None:
                        suites.append(catch_suite)
                        has_catch = True
        return has_catch and bool(suites) and all(
            self._suite_definitely_terminates(s) for s in suites
        )

    def _pattern_is_wildcard(self, node) -> bool:
        if isinstance(node, Token):
            return str(node) == "_"
        if not isinstance(node, Tree):
            return False
        if node.data in {"suite", "test"}:
            return False
        if node.data == "any_pattern":
            return True
        if node.data == "name" and node.children:
            return self._name_text(node) == "_"
        return any(self._pattern_is_wildcard(child) for child in node.children)

    def _funcdef_return_type_root(self, node: Tree) -> str:
        for child in node.children:
            if isinstance(child, Tree) and child.data in {"single_return_type", "multi_return_type"}:
                root = self._return_type_root(child)
                return root or "None"
        return ""

    @staticmethod
    def _funcdef_return_type_node(node: Tree):
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "single_return_type":
                for sub in child.children:
                    if isinstance(sub, Tree) and sub.data == "type_expr":
                        return sub
            if child.data == "multi_return_type":
                return child
        return None

    @staticmethod
    def _funcdef_return_type_text(node: Tree) -> str:
        type_node = SemanticChecker._funcdef_return_type_node(node)
        if type_node is None:
            return ""
        return render_type(SemanticChecker._return_type_model(type_node))

    @staticmethod
    def _return_type_model(node: Tree) -> Type:
        if isinstance(node, Tree) and node.data == "multi_return_type":
            return GenericType(
                "tuple",
                tuple(parse_type(child) for child in node.children if isinstance(child, Tree)),
            )
        return parse_type(node)

    def _literal_return_type(self, ret: Tree) -> str:
        value = self._return_value(ret)
        return self._literal_expr_type(value)

    def _return_value_type(self, ret: Tree) -> Type | None:
        value = self._return_value(ret)
        return self._expr_simple_type(value)

    @staticmethod
    def _return_value(ret: Tree):
        for child in ret.children:
            if isinstance(child, Tree):
                return child
        return None

    def _literal_expr_type(self, node) -> str:
        if not isinstance(node, Tree):
            return ""
        if node.data in {"string", "fstring", "string_concat"}:
            return "str"
        if node.data == "const_true" or node.data == "const_false":
            return "bool"
        if node.data == "const_none":
            return "None"
        if node.data == "number":
            for child in node.children:
                if isinstance(child, Token):
                    if child.type in {"FLOAT_NUMBER", "IMAG_NUMBER"}:
                        return "float"
                    return "int"
            return "int"
        return ""

    def _expr_simple_type(self, node) -> Type | None:
        literal_type = self._literal_expr_type(node)
        if literal_type:
            return NamedType(literal_type)
        if not isinstance(node, Tree):
            return None
        if node.data == "var" and node.children:
            name = self._name_text(node.children[0])
            if name in {"true", "false"}:
                return NamedType("bool")
            if name == "nil":
                return NamedType("None")
            type_name = self._var_type(name)
            return parse_type(type_name) if type_name else None
        if node.data == "list":
            return self._list_expr_type(node)
        if node.data == "dict":
            return self._dict_expr_type(node)
        if node.data == "set":
            return self._set_expr_type(node)
        if node.data == "tuple":
            items = [
                self._expr_simple_type(child) or NamedType("any")
                for child in node.children
                if isinstance(child, Tree)
            ]
            return GenericType("tuple", tuple(items))
        if node.data in {"arith_expr", "term"}:
            return self._binary_expr_result_type(node)
        if node.data == "factor":
            return self._factor_result_type(node)
        if node.data == "not_test":
            return NamedType("bool")
        if node.data in {"and_test", "or_test"}:
            return NamedType("bool")
        if node.data == "comparison":
            return NamedType("bool")
        if node.data == "getitem":
            return self._getitem_result_type(node)
        if node.data == "funccall":
            cast_type = self._builtin_cast_result_type(node)
            if cast_type is not None:
                return cast_type
            call_type = self._call_return_type(node)
            if call_type:
                return parse_type(call_type)
        if node.data == "test":
            return self._ternary_result_type(node)
        value_type = self._constructor_value_type(node)
        if value_type:
            return NamedType(value_type)
        return None

    def _check_expression_type_errors(self, node: Tree) -> None:
        if node.data in {"arith_expr", "term"}:
            self._check_binary_expr_types(node)
        elif node.data == "factor":
            self._check_factor_type(node)
        elif node.data == "not_test":
            self._check_not_type(node)
        elif node.data in {"and_test", "or_test"}:
            self._check_boolean_chain_types(node)
        elif node.data == "test":
            self._check_ternary_type(node)
        elif node.data == "comparison":
            self._check_comparison_types(node)
        elif node.data == "getitem":
            self._check_getitem_types(node)

    def _check_factor_type(self, node: Tree) -> None:
        if len(node.children) != 2:
            return
        op = str(node.children[0])
        operand = node.children[1]
        if not isinstance(operand, Tree):
            return
        operand_type = self._expr_simple_type(operand)
        if operand_type is None or self._is_any_type(operand_type) or self._is_open_type(operand_type):
            return
        if op in {"+", "-"}:
            if self._is_numeric_type(operand_type):
                return
            self._error(
                node,
                "type",
                f"operator `{op}` expects numeric operand, got `{render_type(operand_type)}`",
            )
            return
        if op == "~":
            if self._is_named_type(operand_type, "int"):
                return
            self._error(
                node,
                "type",
                f"operator `~` expects `int` operand, got `{render_type(operand_type)}`",
            )

    def _check_not_type(self, node: Tree) -> None:
        if not node.children or not isinstance(node.children[0], Tree):
            return
        operand_type = self._expr_simple_type(node.children[0])
        if operand_type is None or self._is_any_type(operand_type) or self._is_open_type(operand_type):
            return
        if self._is_named_type(operand_type, "bool"):
            return
        self._error(
            node,
            "type",
            f"operator `not` expects `bool`, got `{render_type(operand_type)}`",
        )

    def _check_boolean_chain_types(self, node: Tree) -> None:
        op = "and" if node.data == "and_test" else "or"
        for index, child in enumerate(c for c in node.children if isinstance(c, Tree)):
            child_type = self._expr_simple_type(child)
            if child_type is None or self._is_any_type(child_type) or self._is_open_type(child_type):
                continue
            if self._is_named_type(child_type, "bool"):
                continue
            side = "left" if index == 0 else "right"
            self._error(
                node,
                "type",
                f"{side} operand of `{op}` must be `bool`, got `{render_type(child_type)}`",
            )
            return

    def _check_ternary_type(self, node: Tree) -> None:
        if len(node.children) != 3:
            return
        true_expr, cond_expr, false_expr = node.children
        if isinstance(cond_expr, Tree):
            cond_type = self._expr_simple_type(cond_expr)
            if (cond_type is not None
                    and not self._is_any_type(cond_type)
                    and not self._is_open_type(cond_type)
                    and not self._is_named_type(cond_type, "bool")):
                self._error(
                    cond_expr,
                    "type",
                    f"ternary condition must be `bool`, got `{render_type(cond_type)}`",
                )
                return
        if not isinstance(true_expr, Tree) or not isinstance(false_expr, Tree):
            return
        true_type = self._expr_simple_type(true_expr)
        false_type = self._expr_simple_type(false_expr)
        if true_type is None or false_type is None:
            return
        if self._is_any_type(true_type) or self._is_any_type(false_type):
            return
        if self._is_open_type(true_type) or self._is_open_type(false_type):
            return
        if self._compatible_ternary_types(true_type, false_type) is not None:
            return
        self._error(
            node,
            "type",
            "ternary branches must have compatible types: "
            f"got `{render_type(true_type)}` and `{render_type(false_type)}`",
        )

    def _check_binary_expr_types(self, node: Tree) -> None:
        operands = [child for child in node.children if isinstance(child, Tree)]
        ops = [str(child) for child in node.children if isinstance(child, Token)]
        if len(operands) < 2:
            return
        left_type = self._expr_simple_type(operands[0])
        for op, right in zip(ops, operands[1:]):
            right_type = self._expr_simple_type(right)
            if left_type is not None and right_type is not None:
                if not self._binary_operator_allowed(op, left_type, right_type):
                    self._report_binary_type_error(node, op, left_type, right_type)
                    return
                left_type = self._binary_operator_result_type(op, left_type, right_type)
            else:
                left_type = None

    def _report_binary_type_error(self, node: Tree, op: str, left: Type, right: Type) -> None:
        left_text = render_type(left)
        right_text = render_type(right)
        if op == "+":
            self._error(node, "type", f"cannot add `{left_text}` and `{right_text}`")
            return
        self._error(
            node,
            "type",
            f"operator `{op}` is not defined for `{left_text}` and `{right_text}`",
        )

    def _check_comparison_types(self, node: Tree) -> None:
        operands = [child for child in node.children
                    if isinstance(child, Tree) and child.data != "comp_op"]
        ops = [self._comp_op_text(child) for child in node.children
               if isinstance(child, Tree) and child.data == "comp_op"]
        if len(operands) < 2:
            return
        left_type = self._expr_simple_type(operands[0])
        for op, right in zip(ops, operands[1:]):
            right_type = self._expr_simple_type(right)
            if left_type is not None and right_type is not None:
                if op in {"in", "not in"}:
                    if not self._membership_allowed(left_type, right_type):
                        self._report_membership_type_error(node, op, left_type, right_type)
                        return
                elif op in {"<", ">", "<=", ">="}:
                    if not self._ordered_comparison_allowed(left_type, right_type):
                        self._error(
                            node,
                            "type",
                            f"operator `{op}` is not defined for "
                            f"`{render_type(left_type)}` and `{render_type(right_type)}`",
                        )
                        return
                elif op in {"==", "!="}:
                    if not self._equality_comparison_allowed(left_type, right_type):
                        self._error(
                            node,
                            "type",
                            f"cannot compare `{render_type(left_type)}` and `{render_type(right_type)}`",
                        )
                        return
            left_type = right_type

    @staticmethod
    def _comp_op_text(node: Tree) -> str:
        return " ".join(str(child) for child in node.children if isinstance(child, Token))

    def _ordered_comparison_allowed(self, left: Type, right: Type) -> bool:
        if self._is_any_type(left) or self._is_any_type(right):
            return True
        if self._is_open_type(left) or self._is_open_type(right):
            return True
        if self._is_numeric_type(left) and self._is_numeric_type(right):
            return True
        return self._is_named_type(left, "str") and self._is_named_type(right, "str")

    def _equality_comparison_allowed(self, left: Type, right: Type) -> bool:
        if self._is_any_type(left) or self._is_any_type(right):
            return True
        if self._is_open_type(left) or self._is_open_type(right):
            return True
        return is_assignable(left, right) or is_assignable(right, left)

    def _membership_allowed(self, item: Type, container: Type) -> bool:
        if self._is_any_type(item) or self._is_any_type(container):
            return True
        expected = self._membership_item_type(container)
        if expected is None:
            return False
        if self._is_any_type(expected):
            return True
        if self._is_open_type(item) or self._is_open_type(expected):
            return True
        return is_assignable(expected, item) or is_assignable(item, expected)

    @staticmethod
    def _membership_item_type(container: Type) -> Type | None:
        if isinstance(container, ListType):
            return container.item
        if isinstance(container, DictType):
            return container.key
        if isinstance(container, NamedType) and container.name == "str":
            return NamedType("str")
        return None

    def _report_membership_type_error(self, node: Tree, op: str, item: Type, container: Type) -> None:
        expected = self._membership_item_type(container)
        if expected is None:
            self._error(
                node,
                "type",
                f"right operand of `{op}` must be `list`, `dict`, or `str`, "
                f"got `{render_type(container)}`",
            )
            return
        self._error(
            node,
            "type",
            f"membership test for `{render_type(container)}` expects "
            f"`{render_type(expected)}`, got `{render_type(item)}`",
        )

    def _check_getitem_types(self, node: Tree) -> None:
        if len(node.children) < 2:
            return
        receiver = node.children[0]
        index = node.children[1]
        if not isinstance(receiver, Tree) or not isinstance(index, Tree):
            return
        if index.data == "slice":
            self._check_slice_index_types(node, receiver, index)
            return
        receiver_type = self._expr_simple_type(receiver)
        index_type = self._expr_simple_type(index)
        if receiver_type is None or index_type is None:
            return
        expected = self._getitem_index_type(receiver_type)
        if expected is None:
            return
        if is_assignable(expected, index_type):
            return
        receiver_text = render_type(receiver_type)
        expected_text = render_type(expected)
        actual_text = render_type(index_type)
        if isinstance(receiver_type, ListType):
            msg = f"list index must be `{expected_text}`, got `{actual_text}`"
        elif isinstance(receiver_type, NamedType) and receiver_type.name == "str":
            msg = f"string index must be `{expected_text}`, got `{actual_text}`"
        elif isinstance(receiver_type, DictType):
            msg = f"dict key for `{receiver_text}` must be `{expected_text}`, got `{actual_text}`"
        else:
            return
        self._error(node, "type", msg)

    def _check_slice_index_types(self, node: Tree, receiver, slice_node: Tree) -> None:
        receiver_type = self._expr_simple_type(receiver)
        if receiver_type is None:
            return
        if not self._is_sliceable_type(receiver_type):
            return
        for bound in (child for child in slice_node.children if isinstance(child, Tree) and child.data != "sliceop"):
            bound_type = self._expr_simple_type(bound)
            if bound_type is None:
                continue
            if not self._is_named_type(bound_type, "int"):
                self._error(
                    node,
                    "type",
                    f"slice bounds must be `int`, got `{render_type(bound_type)}`",
                )
                return

    def _list_expr_type(self, node: Tree) -> Type | None:
        item_types = [
            item_type for child in node.children
            if isinstance(child, Tree)
            for item_type in [self._expr_simple_type(child)]
            if item_type is not None
        ]
        if not item_types:
            return None
        first = item_types[0]
        if all(render_type(item_type) == render_type(first) for item_type in item_types):
            return ListType(first)
        return ListType(NamedType("any"))

    def _dict_expr_type(self, node: Tree) -> Type:
        key_types: list[Type] = []
        value_types: list[Type] = []
        for child in node.children:
            if not isinstance(child, Tree) or child.data != "key_value" or len(child.children) < 2:
                continue
            key = child.children[0]
            value = child.children[1]
            if isinstance(key, Tree):
                key_type = self._expr_simple_type(key)
                if key_type is not None:
                    key_types.append(key_type)
            if isinstance(value, Tree):
                value_type = self._expr_simple_type(value)
                if value_type is not None:
                    value_types.append(value_type)
        return DictType(
            self._homogeneous_type_or_any(key_types, NamedType("str")),
            self._homogeneous_type_or_any(value_types, NamedType("any")),
        )

    def _set_expr_type(self, node: Tree) -> Type | None:
        item_types = [
            item_type for child in node.children
            if isinstance(child, Tree)
            for item_type in [self._expr_simple_type(child)]
            if item_type is not None
        ]
        if not item_types:
            return None
        return GenericType("set", (self._homogeneous_type_or_any(item_types, NamedType("any")),))

    @staticmethod
    def _homogeneous_type_or_any(types: list[Type], default: Type) -> Type:
        if not types:
            return default
        first = types[0]
        if all(render_type(type_) == render_type(first) for type_ in types):
            return first
        return NamedType("any")

    def _binary_expr_result_type(self, node: Tree) -> Type | None:
        operands = [child for child in node.children if isinstance(child, Tree)]
        ops = [str(child) for child in node.children if isinstance(child, Token)]
        if not operands:
            return None
        result = self._expr_simple_type(operands[0])
        for op, right in zip(ops, operands[1:]):
            right_type = self._expr_simple_type(right)
            if result is None or right_type is None:
                return None
            if not self._binary_operator_allowed(op, result, right_type):
                return None
            result = self._binary_operator_result_type(op, result, right_type)
        return result

    def _factor_result_type(self, node: Tree) -> Type | None:
        if not node.children:
            return None
        if len(node.children) == 1 and isinstance(node.children[0], Tree):
            return self._expr_simple_type(node.children[0])
        if len(node.children) != 2 or not isinstance(node.children[1], Tree):
            return None
        op = str(node.children[0])
        operand_type = self._expr_simple_type(node.children[1])
        if operand_type is None:
            return None
        if op in {"+", "-"} and self._is_numeric_type(operand_type):
            return operand_type
        if op == "~" and self._is_named_type(operand_type, "int"):
            return operand_type
        return None

    def _ternary_result_type(self, node: Tree) -> Type | None:
        if len(node.children) != 3:
            return None
        true_expr, _cond_expr, false_expr = node.children
        if not isinstance(true_expr, Tree) or not isinstance(false_expr, Tree):
            return None
        true_type = self._expr_simple_type(true_expr)
        false_type = self._expr_simple_type(false_expr)
        if true_type is None or false_type is None:
            return None
        return self._compatible_ternary_types(true_type, false_type)

    def _compatible_ternary_types(self, true_type: Type, false_type: Type) -> Type | None:
        if self._is_any_type(true_type):
            return false_type
        if self._is_any_type(false_type):
            return true_type
        if self._is_open_type(true_type) or self._is_open_type(false_type):
            return None
        if render_type(true_type) == render_type(false_type):
            return true_type
        if is_assignable(false_type, true_type) or self._nominal_type_assignable(false_type, true_type):
            return false_type
        if is_assignable(true_type, false_type) or self._nominal_type_assignable(true_type, false_type):
            return true_type
        return None

    def _binary_operator_allowed(self, op: str, left: Type, right: Type) -> bool:
        if self._is_any_type(left) or self._is_any_type(right):
            return True
        if self._class_binary_operator_result_type(op, left, right) is not None:
            return True
        if op == "+":
            if self._is_numeric_type(left) and self._is_numeric_type(right):
                return True
            if self._is_named_type(left, "str") and self._is_named_type(right, "str"):
                return True
            if isinstance(left, ListType) and isinstance(right, ListType):
                return True
            return False
        if op in {"-", "*", "/", "%"}:
            return self._is_numeric_type(left) and self._is_numeric_type(right)
        return True

    def _binary_operator_result_type(self, op: str, left: Type, right: Type) -> Type | None:
        if self._is_any_type(left):
            return right
        if self._is_any_type(right):
            return left
        class_result = self._class_binary_operator_result_type(op, left, right)
        if class_result is not None:
            return class_result
        if op == "+" and self._is_named_type(left, "str") and self._is_named_type(right, "str"):
            return NamedType("str")
        if op == "+" and isinstance(left, ListType) and isinstance(right, ListType):
            if render_type(left.item) == render_type(right.item):
                return left
            return ListType(NamedType("any"))
        if self._is_numeric_type(left) and self._is_numeric_type(right):
            if self._is_named_type(left, "float") or self._is_named_type(right, "float"):
                return NamedType("float")
            return NamedType("int")
        return None

    def _class_binary_operator_result_type(self, op: str, left: Type, right: Type) -> Type | None:
        dunder = OP_TO_DUNDER.get(op)
        if not dunder or not isinstance(left, NamedType):
            return None
        class_name = left.name
        if class_name not in self._class_method_shapes:
            return None
        shape = self._class_method_shapes.get(class_name, {}).get(dunder)
        if shape is None:
            return None
        if shape.param_types:
            expected = parse_type(shape.param_types[0])
            if (
                not self._is_any_type(expected)
                and not self._is_unknown_open_assignment_type(expected)
                and not is_assignable(expected, right)
                and not self._nominal_type_assignable(expected, right)
            ):
                return None
        return parse_type(shape.return_type) if shape.return_type else left

    def _getitem_result_type(self, node: Tree) -> Type | None:
        if len(node.children) < 2:
            return None
        receiver = node.children[0]
        index = node.children[1]
        if not isinstance(receiver, Tree) or not isinstance(index, Tree):
            return None
        receiver_type = self._expr_simple_type(receiver)
        if receiver_type is None:
            return None
        if index.data == "slice":
            return receiver_type if self._is_sliceable_type(receiver_type) else None
        if isinstance(receiver_type, ListType):
            return receiver_type.item
        if isinstance(receiver_type, DictType):
            return receiver_type.value
        if self._is_named_type(receiver_type, "str"):
            return NamedType("str")
        return None

    @staticmethod
    def _getitem_index_type(receiver_type: Type) -> Type | None:
        if isinstance(receiver_type, ListType):
            return NamedType("int")
        if isinstance(receiver_type, DictType):
            return receiver_type.key
        if isinstance(receiver_type, NamedType) and receiver_type.name == "str":
            return NamedType("int")
        return None

    @staticmethod
    def _is_sliceable_type(type_: Type) -> bool:
        return isinstance(type_, ListType) or (
            isinstance(type_, NamedType) and type_.name == "str"
        )

    @staticmethod
    def _is_any_type(type_: Type) -> bool:
        return isinstance(type_, NamedType) and type_.name in {"any", "object"}

    def _is_unknown_open_assignment_type(self, type_: Type) -> bool:
        if isinstance(type_, NamedType):
            if type_.name in {"list", "dict", "set", "tuple"}:
                return True
            if type_.name in BUILTIN_TYPES:
                return False
            if type_.name in self._class_method_shapes or type_.name in self._interface_methods:
                return False
            return True
        return False

    @staticmethod
    def _is_open_type(type_: Type) -> bool:
        return isinstance(type_, NamedType) and type_.name not in {
            "None", "bool", "str", "string", "bytes",
            "int", "int8", "int16", "int32", "int64",
            "uint", "uint8", "uint16", "uint32", "uint64",
            "float", "float32", "float64", "byte", "rune",
        }

    @staticmethod
    def _is_numeric_type(type_: Type) -> bool:
        return isinstance(type_, NamedType) and type_.name in {"int", "float"}

    @staticmethod
    def _is_primitive_numeric_type(type_: Type) -> bool:
        return isinstance(type_, NamedType) and (
            type_.name in INTEGER_CAST_FUNCS or type_.name in FLOAT_CAST_FUNCS
        )

    def _builtin_cast_result_type(self, call: Tree) -> Type | None:
        name = self._builtin_cast_name(call)
        if not name:
            return None
        if name in INTEGER_CAST_FUNCS:
            return NamedType("int")
        if name in FLOAT_CAST_FUNCS:
            return NamedType("float")
        if name in {"str", "string"}:
            return NamedType("str")
        if name == "bool":
            return NamedType("bool")
        return None

    def _string_cast_arg_allowed(self, type_: Type) -> bool:
        return (
            self._is_primitive_numeric_type(type_)
            or self._is_named_type(type_, "str")
            or self._is_named_type(type_, "string")
            or self._is_named_type(type_, "bytes")
        )

    @staticmethod
    def _is_named_type(type_: Type, name: str) -> bool:
        return isinstance(type_, NamedType) and type_.name == name

    def _nominal_type_assignable(self, expected: Type, actual: Type) -> bool:
        if not isinstance(expected, NamedType) or not isinstance(actual, NamedType):
            return False
        if expected.name == actual.name:
            return True
        if actual.name == "None":
            if expected.name in self._class_method_shapes or expected.name in self._interface_methods:
                return True
            if expected.name not in BUILTIN_TYPES:
                return True
        if expected.name in self._interface_methods and actual.name in self._class_method_shapes:
            return True
        return False

    @staticmethod
    def _literal_type_matches(expected: str, actual: str) -> bool:
        if expected in {"str", "string"}:
            return actual == "str"
        if expected in {"int", "int8", "int16", "int32", "int64",
                        "uint", "uint8", "uint16", "uint32", "uint64",
                        "byte", "rune"}:
            return actual == "int"
        if expected in {"float", "float32", "float64"}:
            return actual in {"int", "float"}
        if expected == "bool":
            return actual == "bool"
        if expected == "None":
            return actual == "None"
        return True

    def _suite_has_go_block(self, node) -> bool:
        if not isinstance(node, Tree):
            return False
        if node.data == "funccall" and node.children:
            callee = node.children[0]
            if isinstance(callee, Tree) and callee.data == "var" and callee.children:
                if self._name_text(callee.children[0]) == "__go_block__":
                    return True
        return any(self._suite_has_go_block(child) for child in node.children)

    def _suite_has_yield(self, node) -> bool:
        if not isinstance(node, Tree):
            return False
        if node.data == "yield_expr":
            return True
        if node.data in {"funcdef", "classdef", "lambdef"}:
            return False
        return any(self._suite_has_yield(child) for child in node.children)

    def _return_stmts_in(self, node):
        if not isinstance(node, Tree):
            return
        if node.data == "return_stmt":
            yield node
            return
        if node.data in {"funcdef", "classdef", "lambdef"}:
            return
        for child in node.children:
            yield from self._return_stmts_in(child)

    @staticmethod
    def _return_has_value(node: Tree) -> bool:
        return any(child is not None for child in node.children)

    @staticmethod
    def _funcdef_has_nonvoid_return(node: Tree) -> bool:
        """True if the funcdef declares a non-void return type.

        Three shapes classify as *non*-void and so make a dropped
        call worth warning about:

        * a single return type that's anything other than ``None``
          (``-> int``, ``-> Result``, ``-> Option[str]``, …);
        * a multi-return tuple (``-> (int, str)``).

        Anything else — no annotation at all, or the explicit
        ``-> None`` form — is treated as void. Unannotated Lam
        functions are a judgement call: they *do* compile to a
        Go function returning whatever Go sees fit (usually
        ``interface{}`` or the zero value), but in practice users
        who cared about the return value would have annotated it,
        so suppressing the warning there avoids a large false-
        positive surface on early-draft code.
        """
        for c in node.children:
            if not isinstance(c, Tree):
                continue
            if c.data == "single_return_type":
                # ``single_return_type > type_expr > type_union >
                # {type_name | type_generic | type_none | …}``.
                # Dig one level deeper than the old form, which
                # looked at ``single_return_type``'s direct child
                # and therefore never saw ``type_none`` at all.
                return not SemanticChecker._return_is_none(c)
            if c.data == "multi_return_type":
                return True
        return False

    @staticmethod
    def _funcdef_returns_none(node: Tree) -> bool:
        for c in node.children:
            if isinstance(c, Tree) and c.data == "single_return_type":
                return SemanticChecker._return_is_none(c)
        return False

    @staticmethod
    def _return_is_none(single_return_type: Tree) -> bool:
        """True if the ``single_return_type`` subtree is
        ``type_none`` — the explicit ``-> None`` form.

        Walks through ``type_expr`` / ``type_union`` wrappers that
        the grammar inserts around the actual type node.
        """
        def scan(node) -> bool:
            if not isinstance(node, Tree):
                return False
            if node.data == "type_none":
                return True
            if node.data in {"single_return_type", "type_expr", "type_union"}:
                return any(scan(child) for child in node.children)
            return False

        return scan(single_return_type)

    @staticmethod
    def _funcdef_returns_result(node: Tree) -> bool:
        """True if the funcdef's single return annotation has
        ``Result`` as its root type (``Result``, ``Result[T]``,
        ``Result[T, E]`` all qualify).

        Multi-return tuples are treated as *not* Result-returning:
        ``?`` in a ``-> (int, str)`` function still can't propagate
        a ``*Result`` value into the Go signature, so the warning
        should still fire there.
        """
        for c in node.children:
            if not isinstance(c, Tree) or c.data != "single_return_type":
                continue
            return SemanticChecker._type_root_name(c) == "Result"
        return False

    @staticmethod
    def _type_root_name(type_node: Tree) -> str:
        """Return the bare root name of a type subtree
        (``Result`` out of ``Result[T]``, ``Option`` out of
        ``Option[str]``, etc.), or ``""`` if we can't reduce to a
        single root.

        The grammar wraps types in ``type_expr`` / ``type_union``
        with ``type_name`` or ``type_generic`` at the leaf. Both
        ``type_name`` and ``type_generic`` have a ``dotted_name``
        as their first child; the first ``name`` underneath is
        the head we're after.
        """
        if isinstance(type_node, Tree):
            stack = [type_node]
            while stack:
                cur = stack.pop(0)
                if not isinstance(cur, Tree):
                    continue
                if cur.data in ("type_name", "type_generic"):
                    for c in cur.children:
                        if isinstance(c, Tree) and c.data == "dotted_name":
                            for sub in c.children:
                                if isinstance(sub, Tree) and sub.data == "name":
                                    return SemanticChecker._name_text(sub)
                                if isinstance(sub, Token):
                                    return str(sub)
                    return ""
                stack[0:0] = [c for c in cur.children if isinstance(c, Tree)]
            return ""
        for descendant in type_node.iter_subtrees():
            if descendant.data in ("type_name", "type_generic"):
                for c in descendant.children:
                    if isinstance(c, Tree) and c.data == "dotted_name":
                        for sub in c.children:
                            if isinstance(sub, Tree) and sub.data == "name":
                                return SemanticChecker._name_text(sub)
                            if isinstance(sub, Token):
                                return str(sub)
                return ""
        return ""

    @staticmethod
    def _funcdef_type_params(node: Tree) -> List[str]:
        for c in node.children:
            if isinstance(c, Tree) and c.data == "type_params":
                names = []
                for tp in c.children:
                    if isinstance(tp, Tree) and tp.data == "type_param":
                        if tp.children:
                            names.append(SemanticChecker._name_text(tp.children[0]))
                return names
        return []

    def _func_signature(self, node: Tree, name: str) -> Optional[_CallableSig]:
        return self._callable_signature_from_params(
            self._funcdef_params(node),
            name,
            skip_self=False,
            generic_names=tuple(self._funcdef_type_params(node)),
        )

    def _callable_signature_from_params(
        self,
        params_node,
        name: str,
        *,
        skip_self: bool,
        generic_names: tuple[str, ...] = (),
    ) -> _CallableSig:
        params: list[str] = []
        param_types: list[str] = []
        required = 0
        max_pos: Optional[int] = 0
        accepts_kwargs = False
        if params_node is None:
            return _CallableSig(name, 0, 0, (), False, (), generic_names)
        for child in params_node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "typed_paramvalue":
                inner = child.children[0] if child.children else None
                if isinstance(inner, Tree) and inner.data == "tuple_typed_param":
                    param_name = "<tuple>"
                else:
                    param_name = self._typed_paramvalue_name(child)
                if skip_self and param_name in {"self", "cls"}:
                    continue
                if param_name:
                    params.append(param_name)
                    param_types.append(self._typed_paramvalue_type_name(child))
                    if max_pos is not None:
                        max_pos += 1
                    if self._typed_paramvalue_is_required(child):
                        required += 1
                continue
            if child.data == "typed_starparams":
                max_pos = None
                poststar = self._poststar_param_names(child)
                params.extend(poststar)
                param_types.extend("any" for _ in poststar)
                accepts_kwargs = accepts_kwargs or self._has_typed_kwargs(child)
                continue
            if child.data == "typed_kwparams":
                accepts_kwargs = True
        return _CallableSig(
            name,
            required,
            max_pos,
            tuple(params),
            accepts_kwargs,
            tuple(param_types),
            generic_names,
        )

    @staticmethod
    def _param_root_types(params_node, *, skip_self: bool) -> tuple[str, ...]:
        if params_node is None or not isinstance(params_node, Tree):
            return ()
        out: list[str] = []
        for child in params_node.children:
            if not isinstance(child, Tree):
                continue
            if child.data != "typed_paramvalue":
                continue
            inner = child.children[0] if child.children else None
            if not isinstance(inner, Tree) or inner.data != "typed_param":
                continue
            name = SemanticChecker._name_text(inner.children[0]) if inner.children else ""
            if skip_self and name in {"self", "cls"}:
                continue
            if len(inner.children) > 1 and isinstance(inner.children[1], Tree):
                out.append(SemanticChecker._type_root_name(inner.children[1]))
            else:
                out.append("any")
        return tuple(out)

    @staticmethod
    def _param_type_texts(params_node, *, skip_self: bool) -> tuple[str, ...]:
        if params_node is None or not isinstance(params_node, Tree):
            return ()
        out: list[str] = []
        for child in params_node.children:
            if not isinstance(child, Tree) or child.data != "typed_paramvalue":
                continue
            inner = child.children[0] if child.children else None
            if not isinstance(inner, Tree) or inner.data != "typed_param":
                continue
            name = SemanticChecker._name_text(inner.children[0]) if inner.children else ""
            if skip_self and name in {"self", "cls"}:
                continue
            type_node = next(
                (sub for sub in inner.children[1:]
                 if isinstance(sub, Tree) and sub.data == "type_expr"),
                None,
            )
            if type_node is not None:
                out.append(render_type(parse_type(type_node)))
            else:
                out.append("any")
        return tuple(out)

    @staticmethod
    def _param_type_map(params_node) -> dict[str, str]:
        if params_node is None or not isinstance(params_node, Tree):
            return {}
        out: dict[str, str] = {}
        for child in params_node.children:
            if not isinstance(child, Tree) or child.data != "typed_paramvalue":
                continue
            inner = child.children[0] if child.children else None
            if not isinstance(inner, Tree) or inner.data != "typed_param":
                continue
            if not inner.children:
                continue
            name = SemanticChecker._name_text(inner.children[0])
            type_node = next(
                (sub for sub in inner.children[1:]
                 if isinstance(sub, Tree) and sub.data == "type_expr"),
                None,
            )
            if name and type_node is not None:
                out[name] = SemanticChecker._type_name_for_storage(type_node)
        return out

    @staticmethod
    def _type_name_for_storage(type_node) -> str:
        if type_node is None:
            return ""
        return render_type(parse_type(type_node))

    @staticmethod
    def _return_type_root(node: Tree) -> str:
        if node.data == "multi_return_type":
            return "tuple"
        if SemanticChecker._return_is_none(node):
            return ""
        return SemanticChecker._type_root_name(node)

    @staticmethod
    def _typed_paramvalue_name(node: Tree) -> str:
        if not node.children:
            return ""
        inner = node.children[0]
        if isinstance(inner, Tree) and inner.data == "typed_param" and inner.children:
            return SemanticChecker._name_text(inner.children[0])
        return ""

    @staticmethod
    def _typed_paramvalue_is_required(node: Tree) -> bool:
        return len(node.children) < 2 or node.children[1] is None

    @staticmethod
    def _typed_paramvalue_type_name(node: Tree) -> str:
        if not node.children:
            return "any"
        inner = node.children[0]
        if not isinstance(inner, Tree) or inner.data != "typed_param":
            return "any"
        type_node = next(
            (child for child in inner.children[1:]
             if isinstance(child, Tree) and child.data == "type_expr"),
            None,
        )
        return SemanticChecker._type_name_for_storage(type_node) or "any"

    @staticmethod
    def _poststar_param_names(node: Tree) -> list[str]:
        names: list[str] = []
        for child in node.iter_subtrees():
            if child.data == "typed_paramvalue":
                name = SemanticChecker._typed_paramvalue_name(child)
                if name:
                    names.append(name)
            elif child.data == "typed_kwparams":
                for sub in child.children:
                    if isinstance(sub, Tree) and sub.data == "typed_param" and sub.children:
                        name = SemanticChecker._name_text(sub.children[0])
                        if name:
                            names.append(name)
        return names

    @staticmethod
    def _has_typed_kwargs(node: Tree) -> bool:
        return any(child.data == "typed_kwparams" for child in node.iter_subtrees())

    @staticmethod
    def _suite_node(node: Tree) -> Optional[Tree]:
        for c in node.children:
            if isinstance(c, Tree) and c.data == "suite":
                return c
        return None

    @staticmethod
    def _param_type_sig(params_node) -> tuple:
        """Return a tuple of canonical type-annotation strings, one
        per positional parameter of ``params_node``.

        Used by :meth:`_collect_top_decl_with_dupe_check` to let two
        same-arity functions coexist as long as their parameter types
        differ. The string form is a depth-first serialisation of the
        type-expression subtree — close enough to a syntactic
        identity check that it won't accept ``int`` as a match for
        ``Int`` or ``list[int]`` as a match for ``list[str]``.

        Parameters with no annotation collapse to the sentinel
        ``"any"`` so two un-annotated overloads still collide, which
        preserves the pre-existing same-name/same-arity diagnostic
        for loosely-typed code.
        """
        if params_node is None or not isinstance(params_node, Tree):
            return ()
        out: list = []
        for child in params_node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "typed_paramvalue":
                inner = child.children[0]
                if isinstance(inner, Tree) and inner.data == "typed_param":
                    if len(inner.children) > 1:
                        out.append(SemanticChecker._render_type_node(inner.children[1]))
                    else:
                        out.append("any")
                elif isinstance(inner, Tree) and inner.data == "tuple_typed_param":
                    out.append(SemanticChecker._render_type_node(inner.children[-1]))
            elif child.data == "typed_starparams":
                # Variadics can't participate in arity-based or
                # type-based overload dispatch — stamp a fixed
                # sentinel so two variadic overloads still collide.
                out.append("...var")
        return tuple(out)

    @staticmethod
    def _render_type_node(node) -> str:
        """Cheap textual fingerprint of a type-expr subtree.

        Not a full Go-type lowering — the semantic checker doesn't
        need that. Just enough to tell ``int`` apart from ``str`` and
        ``list[int]`` apart from ``list[str]`` so the dupe check
        doesn't flag legitimate type overloads.
        """
        if isinstance(node, Token):
            return str(node)
        if not isinstance(node, Tree):
            return "any"
        parts = [node.data]
        for c in node.children:
            parts.append(SemanticChecker._render_type_node(c))
        return "(" + "|".join(parts) + ")"

    @staticmethod
    def _param_names(params_node) -> List[str]:
        if params_node is None or not isinstance(params_node, Tree):
            return []
        names: List[str] = []
        for child in params_node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "typed_paramvalue":
                inner = child.children[0]
                if isinstance(inner, Tree) and inner.data == "typed_param":
                    if inner.children:
                        names.append(SemanticChecker._name_text(inner.children[0]))
                elif isinstance(inner, Tree) and inner.data == "tuple_typed_param":
                    # ``(x, y): tuple[int, int]`` — the visible bindings
                    # are the inner names.
                    for sub in inner.children:
                        if isinstance(sub, Tree) and sub.data == "name":
                            names.append(SemanticChecker._name_text(sub))
            elif child.data in ("typed_starparams", "typed_kwparams"):
                for sub in child.children:
                    if isinstance(sub, Tree) and sub.data == "typed_param":
                        if sub.children:
                            names.append(SemanticChecker._name_text(sub.children[0]))
        return names

    @staticmethod
    def _param_node_map(params_node) -> dict:
        """Mirror of :meth:`_param_names` but returns ``{name: node}``
        so the unused-parameter warning can cite the parameter's own
        location rather than the function header. ``node`` is the
        innermost ``typed_param`` (or ``name`` for tuple destructure
        elements) so its meta carries the correct line/column.
        """
        if params_node is None or not isinstance(params_node, Tree):
            return {}
        out: dict = {}
        for child in params_node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "typed_paramvalue":
                inner = child.children[0]
                if isinstance(inner, Tree) and inner.data == "typed_param":
                    if inner.children:
                        n = SemanticChecker._name_text(inner.children[0])
                        if n:
                            out[n] = inner
                elif isinstance(inner, Tree) and inner.data == "tuple_typed_param":
                    for sub in inner.children:
                        if isinstance(sub, Tree) and sub.data == "name":
                            n = SemanticChecker._name_text(sub)
                            if n:
                                out[n] = sub
            elif child.data in ("typed_starparams", "typed_kwparams"):
                for sub in child.children:
                    if isinstance(sub, Tree) and sub.data == "typed_param":
                        if sub.children:
                            n = SemanticChecker._name_text(sub.children[0])
                            if n:
                                out[n] = sub
        return out

    @staticmethod
    def _for_target_names(target) -> List[str]:
        names: List[str] = []
        if target is None:
            return names
        if isinstance(target, Token):
            names.append(str(target))
            return names
        if not isinstance(target, Tree):
            return names
        d = target.data
        if d in ("typed_for_target", "untyped_for_target"):
            for c in target.children:
                names.extend(SemanticChecker._for_target_names(c))
            return names
        if d == "name":
            names.append(SemanticChecker._name_text(target))
            return names
        if d == "var":
            if target.children:
                names.append(SemanticChecker._name_text(target.children[0]))
            return names
        if d in ("exprlist", "testlist_star_expr", "tuple", "test", "star_expr"):
            for c in target.children:
                names.extend(SemanticChecker._for_target_names(c))
            return names
        # ``a, b: tuple[...]`` annotated tuple destructure: the type
        # annotation child is a ``type_expr`` and we only want the
        # name children before it.
        if d == "annassign":
            for c in target.children:
                if isinstance(c, Tree) and c.data == "type_expr":
                    break
                names.extend(SemanticChecker._for_target_names(c))
        return names

    # ─── Helpers ──────────────────────────────────────────────

    def _declare(self, name: str) -> None:
        if name and self._scopes:
            self._scopes[-1].names.add(name)

    def _assignment_declares_current_scope(self, node: Tree, name: str) -> bool:
        if not name or not self._scopes:
            return False
        if node.data == "annassign" or name in self._scopes[-1].names:
            return True
        return not any(name in scope.names for scope in self._scopes[:-1])

    def _error(self, node, kind: str, msg: str) -> None:
        line, col = self._loc(node)
        self.errors.append(SemanticError(line, col, msg, kind))

    def _warning(self, node, kind: str, msg: str) -> None:
        """Append a non-fatal diagnostic. Same shape as :meth:`_error`
        but with ``severity="warning"`` so the CLI can render it
        without aborting the build."""
        line, col = self._loc(node)
        self.errors.append(SemanticError(line, col, msg, kind, severity="warning"))

    def _emit_unused_import_warnings(self, module_scope: _Scope) -> None:
        """Emit a warning for every top-level import binding the rest
        of the file never references. Underscore (``_``) bindings
        are skipped at collection time — anything that survives is
        a real candidate.

        We deliberately scope this to the module: imports inside a
        function body are rare (and usually intentional, e.g. lazy
        loading), and tracking them at the right scope level would
        require teaching every block walker about import scope —
        more cost than the warning is worth.
        """
        for name, node in self._import_records:
            if name in module_scope.used_names:
                continue
            self._warning(
                node, "unused",
                f"unused import `{name}` "
                f"(remove it, or rebind to `_` to silence)",
            )

    @staticmethod
    def _loc(node) -> tuple:
        """Best-effort source location for a tree/token."""
        if isinstance(node, SourceSpan):
            return (node.line, node.col)
        if isinstance(node, Token):
            return (getattr(node, "line", 0) or 0,
                    getattr(node, "column", 0) or 0)
        if isinstance(node, Tree):
            meta = getattr(node, "meta", None)
            if meta is not None and not getattr(meta, "empty", True):
                return (meta.line, meta.column)
            for child in node.children:
                line, col = SemanticChecker._loc(child)
                if line:
                    return (line, col)
        return (0, 0)

    @staticmethod
    def _suite_stmts(tree) -> list:
        """Top-level statements of a parse tree or suite node."""
        if not isinstance(tree, Tree):
            return []
        if tree.data == "suite":
            stmts = []
            for c in tree.children:
                if isinstance(c, Tree):
                    if c.data == "simple_stmt":
                        stmts.extend(s for s in c.children if isinstance(s, Tree))
                    else:
                        stmts.append(c)
            return stmts
        # Top-level: tree.data is "start" / "file_input" or similar.
        return [c for c in tree.children if isinstance(c, Tree)]

    @staticmethod
    def _name_text(node) -> str:
        """Extract the textual identifier from a ``name`` tree or token."""
        if isinstance(node, Token):
            return str(node)
        if isinstance(node, Tree):
            if node.data == "name" and node.children:
                return SemanticChecker._name_text(node.children[0])
            if node.children:
                return SemanticChecker._name_text(node.children[0])
        return ""

    @staticmethod
    def _funcdef_name(node: Tree) -> str:
        for child in node.children:
            if isinstance(child, Tree) and child.data == "name":
                return SemanticChecker._name_text(child)
        return ""

    @staticmethod
    def _classdef_name(node: Tree) -> str:
        if node.children and isinstance(node.children[0], Tree):
            return SemanticChecker._name_text(node.children[0])
        return ""

    @staticmethod
    def _classdef_base_names(node: Tree) -> list[str]:
        return [base for _alias, base in SemanticChecker._classdef_base_specs(node)]

    @staticmethod
    def _classdef_base_aliases(node: Tree) -> dict[str, str]:
        aliases: dict[str, str] = {}
        specs = SemanticChecker._classdef_base_specs(node)
        if len(specs) == 1 and not specs[0][0] and specs[0][1]:
            aliases["base"] = specs[0][1]
        for alias, base in specs:
            if alias and base:
                aliases[alias] = base
        return aliases

    @staticmethod
    def _classdef_base_specs(node: Tree) -> list[tuple[str, str]]:
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "class_bases":
                specs: list[tuple[str, str]] = []
                for base_node in child.children:
                    if not isinstance(base_node, Tree):
                        continue
                    if base_node.data == "named_class_base":
                        alias = ""
                        base = ""
                        if base_node.children:
                            alias = SemanticChecker._class_base_name(base_node.children[0])
                        if len(base_node.children) > 1:
                            base = SemanticChecker._class_base_name(base_node.children[1])
                        if base:
                            specs.append((alias, base))
                    elif base_node.data == "unnamed_class_base":
                        base = SemanticChecker._class_base_name(base_node)
                        if base:
                            specs.append(("", base))
                return specs
            # Compatibility for parse trees produced by the older
            # grammar, where class bases were ordinary call arguments.
            if child.data == "arguments":
                specs = []
                for arg in child.children:
                    name = SemanticChecker._class_base_name(arg)
                    if name:
                        specs.append(("", name))
                return specs
        return []

    @staticmethod
    def _class_base_name(node) -> str:
        if not isinstance(node, Tree):
            return ""
        if node.data == "var" and node.children:
            return SemanticChecker._name_text(node.children[0])
        if node.data == "name" and node.children:
            return SemanticChecker._name_text(node)
        if node.data == "dotted_name":
            parts = [
                SemanticChecker._name_text(child)
                for child in node.children
                if isinstance(child, Tree)
            ]
            return ".".join(part for part in parts if part)
        if node.data in {"getattr", "getattr_safe"} and len(node.children) >= 2:
            prefix = SemanticChecker._class_base_name(node.children[0])
            attr = SemanticChecker._name_text(node.children[1])
            if prefix and attr:
                return f"{prefix}.{attr}"
        if node.children:
            for child in node.children:
                name = SemanticChecker._class_base_name(child)
                if name:
                    return name
        return ""

    @staticmethod
    def _assign_targets(node: Tree) -> list:
        """Best-effort list of names bound by an assignment node.

        Handles: ``x = y``, ``x: T = y``, ``a, b = pair``,
        ``x, *rest = xs``, augmented assignment (``x += 1`` re-uses
        ``x`` so we still register it). Subscripts and attribute
        targets aren't bindings and are ignored.
        """
        names: list = []

        def _scan(target):
            if isinstance(target, Token):
                names.append(str(target))
                return
            if not isinstance(target, Tree):
                return
            d = target.data
            if d == "name":
                names.append(SemanticChecker._name_text(target))
            elif d == "var":
                if target.children:
                    names.append(SemanticChecker._name_text(target.children[0]))
            elif d in ("testlist_star_expr", "testlist_tuple", "tuple",
                       "exprlist", "test", "star_expr"):
                for c in target.children:
                    _scan(c)
            elif d == "annassign":
                # ``[private] [static] target : type [= value]``
                for c in target.children:
                    if isinstance(c, Token):
                        continue
                    if isinstance(c, Tree) and c.data == "type_expr":
                        break
                    _scan(c)
            elif d in ("assign", "assign_stmt", "augassign"):
                # First child is the LHS; subsequent ones are RHS values
                # (or further LHS in chained ``a = b = c``).
                if target.children:
                    _scan(target.children[0])
                    if d == "assign":
                        # Chained assignment: every middle child is also
                        # a target, only the rightmost is the value.
                        for c in target.children[1:-1]:
                            _scan(c)
            # Anything else (getattr/getitem/funccall) isn't a binding.

        _scan(node)
        return names

    @staticmethod
    def _import_bindings(node: Tree) -> list:
        """Names brought into scope by ``import`` / ``from import``."""
        names: list = []

        def _last_name(dn):
            if not isinstance(dn, Tree):
                return ""
            parts = []
            for c in dn.children:
                parts.append(SemanticChecker._name_text(c))
            return parts[-1] if parts else ""

        if node.data == "import_name":
            for child in node.children:
                if isinstance(child, Tree) and child.data == "dotted_as_names":
                    for das in child.children:
                        if isinstance(das, Tree) and das.data == "dotted_as_name":
                            alias = None
                            for c in das.children[1:]:
                                if isinstance(c, Tree) and c.data == "name":
                                    alias = SemanticChecker._name_text(c)
                                elif isinstance(c, Token):
                                    alias = str(c)
                            if alias:
                                names.append(alias)
                            else:
                                last = _last_name(das.children[0])
                                if last:
                                    names.append(last)
        elif node.data == "import_from":
            # ``from @scope/name import X, Y as Z`` binds X and Z in
            # the current scope just like a plain ``from pkg import``
            # would — the scoped-name node on the left doesn't itself
            # bring a name into scope, so we can ignore it here and
            # only walk ``import_as_names``.
            for child in node.children:
                if isinstance(child, Tree) and child.data == "import_as_names":
                    for ian in child.children:
                        if isinstance(ian, Tree) and ian.data == "import_as_name":
                            alias = None
                            base = ""
                            for c in ian.children:
                                if isinstance(c, Tree) and c.data == "name":
                                    if not base:
                                        base = SemanticChecker._name_text(c)
                                    else:
                                        alias = SemanticChecker._name_text(c)
                                elif isinstance(c, Token):
                                    if not base:
                                        base = str(c)
                                    else:
                                        alias = str(c)
                            names.append(alias or base)
        return names

    @staticmethod
    def _from_import_pairs(node: Tree) -> list[tuple[str, str]]:
        if node.data != "import_from":
            return []
        pairs: list[tuple[str, str]] = []
        for child in node.children:
            if not isinstance(child, Tree) or child.data != "import_as_names":
                continue
            for ian in child.children:
                if not isinstance(ian, Tree) or ian.data != "import_as_name":
                    continue
                imported = ""
                alias = ""
                for c in ian.children:
                    if isinstance(c, Tree) and c.data == "name":
                        if not imported:
                            imported = SemanticChecker._name_text(c)
                        else:
                            alias = SemanticChecker._name_text(c)
                    elif isinstance(c, Token):
                        if not imported:
                            imported = str(c)
                        else:
                            alias = str(c)
                if imported:
                    pairs.append((imported, alias or imported))
        return pairs


# ─── Public helper ───────────────────────────────────────────────


def render_errors(errors: List[SemanticError], source: str, path: str) -> str:
    """Render only the *errors* in ``errors`` with a header + per-error
    snippets. Warnings are filtered out and rendered separately by
    :func:`render_warnings` so callers can print warnings even when
    there are no errors.
    """
    lines = source.split("\n")
    only_errors = [e for e in errors if not e.is_warning]
    if not only_errors:
        return ""
    color = _diagnostic_color_enabled()
    header = f"error: semantic check failed for {path}"
    if color:
        header = _color(header, "red")
    out = [header]
    for err in only_errors:
        out.append(err.format(lines, path, color=color))
    return "\n".join(out)


def render_warnings(errors: List[SemanticError], source: str, path: str) -> str:
    """Render only the *warnings* in ``errors`` with a header + per-
    warning snippets, or the empty string if there are none. Used by
    the CLI to print advisory diagnostics that should not abort the
    build.
    """
    lines = source.split("\n")
    only_warns = [e for e in errors if e.is_warning]
    if not only_warns:
        return ""
    color = _diagnostic_color_enabled()
    header = (f"warning: semantic check noticed {len(only_warns)} "
              f"issue{'s' if len(only_warns) != 1 else ''} in {path}")
    if color:
        header = _color(header, "yellow")
    out = [header]
    for err in only_warns:
        out.append(err.format(lines, path, color=color))
    return "\n".join(out)


def has_errors(errors: List[SemanticError]) -> bool:
    """``True`` if any entry in ``errors`` has ``severity=\"error\"``.
    Warnings alone return ``False`` so callers can let the build
    proceed when only advisory diagnostics fired.
    """
    return any(not e.is_warning for e in errors)


def _tree_loc(node) -> tuple[int, int]:
    meta = getattr(node, "meta", None)
    if meta is not None and not getattr(meta, "empty", True):
        return int(getattr(meta, "line", 1) or 1), int(getattr(meta, "column", 1) or 1)
    return 1, 1


def _import_module_name(node: Tree) -> str:
    for child in node.children:
        if isinstance(child, Tree) and child.data == "dotted_name":
            parts: list[str] = []
            for item in child.children:
                if isinstance(item, Tree):
                    parts.append(SemanticChecker._name_text(item))
                else:
                    parts.append(str(item))
            return ".".join(p for p in parts if p)
        if isinstance(child, Tree) and child.data == "scoped_name" and child.children:
            return str(child.children[0])
    return ""


def _from_import_symbols(node: Tree) -> list[tuple[str, str, Tree]]:
    out: list[tuple[str, str, Tree]] = []
    for child in node.children:
        if not isinstance(child, Tree) or child.data != "import_as_names":
            continue
        for item in child.children:
            if not isinstance(item, Tree) or item.data != "import_as_name":
                continue
            names = [c for c in item.children if isinstance(c, Tree) and c.data == "name"]
            if not names:
                continue
            requested = SemanticChecker._name_text(names[0])
            if requested == "*":
                continue
            local = requested
            if len(names) > 1:
                local = SemanticChecker._name_text(names[1])
            if requested:
                out.append((requested, local, item))
    return out


def _dotted_name_text(node: Tree) -> str:
    if not isinstance(node, Tree) or node.data != "dotted_name":
        return ""
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, Tree):
            text = SemanticChecker._name_text(child)
        else:
            text = str(child)
        if text:
            parts.append(text)
    return ".".join(parts)


def _import_name_modules(node: Tree) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if node.data != "import_name":
        return out
    for child in node.children:
        if not isinstance(child, Tree) or child.data != "dotted_as_names":
            continue
        for item in child.children:
            if not isinstance(item, Tree) or item.data != "dotted_as_name":
                continue
            module = ""
            alias = ""
            for part in item.children:
                if isinstance(part, Tree) and part.data == "dotted_name":
                    module = _dotted_name_text(part)
                elif isinstance(part, Tree) and part.data == "name":
                    alias = SemanticChecker._name_text(part)
                elif isinstance(part, Token):
                    alias = str(part)
            if module:
                local = alias or module.rsplit(".", 1)[-1].rsplit("/", 1)[-1]
                out.append((module, local))
    return out


def _iter_import_from_nodes(node: Tree):
    if not isinstance(node, Tree):
        return
    if node.data == "import_from":
        yield node
    for child in node.children:
        if isinstance(child, Tree):
            yield from _iter_import_from_nodes(child)


def _iter_import_name_nodes(node: Tree):
    if not isinstance(node, Tree):
        return
    if node.data == "import_name":
        yield node
    for child in node.children:
        if isinstance(child, Tree):
            yield from _iter_import_name_nodes(child)


def _check_imported_exports(
    tree: Tree,
    *,
    module_index: WorkspaceIndex,
    source_path: Path,
) -> list[SemanticError]:
    errors: list[SemanticError] = []
    for node in _iter_import_from_nodes(tree):
        module = _import_module_name(node)
        if not module:
            continue
        mod_path = module_index.resolve_module(source_path, module)
        if mod_path is None:
            continue
        try:
            facts = module_index.facts_by_path.get(mod_path) or module_index.update_file(mod_path)
        except OSError:
            continue
        exports = set(facts.exports)
        for requested, local, symbol_node in _from_import_symbols(node):
            export = facts.exports.get(requested)
            if export is not None and _export_is_private(export):
                alias_note = f" as `{local}`" if local != requested else ""
                line, col = _tree_loc(symbol_node)
                errors.append(SemanticError(
                    line,
                    col,
                    f"private symbol `{requested}` from module `{module}` is not visible for import{alias_note}",
                    "visibility",
                ))
                continue
            if requested in exports:
                continue
            alias_note = f" as `{local}`" if local != requested else ""
            msg = (
                f"import resolution failed: module `{module}` does not export "
                f"`{requested}`{alias_note}"
            )
            suggestion = get_close_matches(requested, sorted(exports), n=1, cutoff=0.72)
            if suggestion:
                msg += f"\n  help: did you mean `{suggestion[0]}`?"
            if exports:
                exported = "\n".join(f"    - {name}" for name in sorted(exports))
                msg += f"\n  exported by `{module}`:\n{exported}"
            else:
                msg += f"\n  help: `{module}` has no exported Lam symbols."
            line, col = _tree_loc(symbol_node)
            errors.append(SemanticError(line, col, msg, "import"))
    return errors


def _export_is_private(export) -> bool:
    detail = getattr(export, "detail", "") or ""
    return detail.lstrip().startswith("private ")


def _imported_call_metadata(
    tree: Tree,
    *,
    module_index: WorkspaceIndex,
    source_path: Path,
) -> _ImportedCallMetadata:
    metadata = _ImportedCallMetadata()
    parsed_cache: dict[Path, Tree | None] = {}
    helper = SemanticChecker()

    def module_tree(path: Path) -> Tree | None:
        path = path.resolve()
        if path in parsed_cache:
            return parsed_cache[path]
        try:
            source = path.read_text(encoding="utf-8")
            from compiler.lammergeier import create_parser, preprocess_for_parse
            parse_input = preprocess_for_parse(source)
            parsed_cache[path] = create_parser().parse(parse_input.source)
        except Exception:
            parsed_cache[path] = None
        return parsed_cache[path]

    def collect_funcdefs(node: Tree, name: str):
        for stmt in helper._suite_stmts(node):
            if not isinstance(stmt, Tree):
                continue
            target = stmt
            if stmt.data == "decorated":
                target = next(
                    (child for child in stmt.children
                     if isinstance(child, Tree) and child.data == "funcdef"),
                    stmt,
                )
            if isinstance(target, Tree) and target.data == "funcdef":
                if helper._funcdef_name(target) == name:
                    yield target

    def collect_classdefs(node: Tree, name: str):
        for stmt in helper._suite_stmts(node):
            if not isinstance(stmt, Tree):
                continue
            target = stmt
            if stmt.data == "decorated":
                target = next(
                    (child for child in stmt.children
                     if isinstance(child, Tree) and child.data == "classdef"),
                    stmt,
                )
            if isinstance(target, Tree) and target.data == "classdef":
                if helper._classdef_name(target) == name:
                    yield target

    def collect_interfacedefs(node: Tree, name: str):
        for stmt in helper._suite_stmts(node):
            if not isinstance(stmt, Tree):
                continue
            if stmt.data == "decorated":
                continue
            if isinstance(stmt, Tree) and stmt.data == "interfacedef":
                if stmt.children and helper._name_text(stmt.children[0]) == name:
                    yield stmt

    def remap_return_type(type_name: str, class_aliases: dict[str, str]) -> str:
        if not type_name:
            return ""
        return class_aliases.get(type_name, type_name)

    def remap_type_text(type_text: str, class_aliases: dict[str, str]) -> str:
        if not type_text or not class_aliases:
            return type_text
        try:
            mapping = {
                name: NamedType(alias)
                for name, alias in class_aliases.items()
            }
            return render_type(helper._substitute_generic_type(parse_type(type_text), mapping))
        except Exception:
            return type_text

    def remap_base_name(base: str, class_aliases: dict[str, str]) -> str:
        if not base:
            return ""
        return class_aliases.get(base, base)

    def record_func_return_type(funcdef: Tree, local: str, class_aliases: dict[str, str]) -> None:
        return_type = remap_return_type(helper._funcdef_return_type_root(funcdef), class_aliases)
        if return_type:
            metadata.func_return_types.setdefault(local, set()).add(return_type)
        return_type_text = helper._funcdef_return_type_text(funcdef)
        if return_type_text:
            metadata.func_return_type_texts.setdefault(local, set()).add(
                remap_type_text(return_type_text, class_aliases)
            )

    def collect_imported_class(
        classdef: Tree,
        local: str,
        class_aliases: dict[str, str],
    ) -> None:
        class_helper = SemanticChecker()
        class_helper._collect_class_members(local, classdef)
        class_helper._collect_class_method_shapes(local, classdef)
        class_helper._collect_constructor_sigs(local, classdef)
        class_helper._collect_nonvoid_methods(local, classdef)
        original_name = helper._classdef_name(classdef)
        aliases = dict(class_aliases)
        if original_name:
            aliases[original_name] = local
        bases = tuple(
            remap_base_name(base, aliases)
            for base in helper._classdef_base_names(classdef)
            if remap_base_name(base, aliases)
        )
        if bases:
            metadata.class_bases[local] = bases
        if local in class_helper._constructor_sigs:
            metadata.constructor_sigs[local] = list(class_helper._constructor_sigs[local])
        if local in class_helper._class_members:
            metadata.class_members[local] = set(class_helper._class_members[local])
        if local in class_helper._class_fields:
            metadata.class_fields[local] = set(class_helper._class_fields[local])
        if local in class_helper._class_static_methods:
            metadata.class_static_methods[local] = set(class_helper._class_static_methods[local])
        if local in class_helper._class_instance_methods:
            metadata.class_instance_methods[local] = set(class_helper._class_instance_methods[local])
        if local in class_helper._class_private_methods:
            metadata.class_private_methods[local] = set(class_helper._class_private_methods[local])
        if local in class_helper._class_method_shapes:
            metadata.class_method_shapes[local] = {
                method_name: _MethodShape(
                    shape.name,
                    shape.param_types,
                    remap_return_type(shape.return_type, aliases),
                    tuple(remap_type_text(type_text, aliases) for type_text in shape.param_type_texts),
                    remap_type_text(shape.return_type_text, aliases),
                )
                for method_name, shape in class_helper._class_method_shapes[local].items()
            }
        prefix = f"{local}."
        for name, sigs in class_helper._method_sigs.items():
            if name.startswith(prefix):
                metadata.method_sigs.setdefault(name, []).extend(sigs)
        for name, return_types in class_helper._method_return_type_texts.items():
            if name.startswith(prefix):
                metadata.method_return_type_texts.setdefault(name, set()).update(
                    remap_type_text(return_type, aliases) for return_type in return_types
                )

    def collect_imported_interface(interfacedef: Tree, local: str) -> None:
        metadata.interface_methods[local] = helper._collect_interface_methods(interfacedef)

    for node in _iter_import_from_nodes(tree):
        module = _import_module_name(node)
        if not module:
            continue
        mod_path = module_index.resolve_module(source_path, module)
        if mod_path is None:
            continue
        facts = module_index.facts_by_path.get(mod_path)
        if facts is None:
            try:
                facts = module_index.update_file(mod_path)
            except OSError:
                continue
        imported_tree = module_tree(mod_path)
        if imported_tree is None:
            continue
        class_aliases = {
            requested: local
            for requested, local, _symbol_node in _from_import_symbols(node)
            if (facts.exports.get(requested) is not None
                and facts.exports[requested].kind == "class")
        }
        for requested, local, _symbol_node in _from_import_symbols(node):
            export = facts.exports.get(requested)
            if export is None:
                continue
            if export.kind == "function":
                if _export_is_private(export):
                    metadata.private_functions.add(local)
                    continue
                for funcdef in collect_funcdefs(imported_tree, requested):
                    sig = helper._func_signature(funcdef, local)
                    if sig is not None:
                        metadata.func_sigs.setdefault(local, []).append(sig)
                    record_func_return_type(funcdef, local, class_aliases)
            elif export.kind == "class":
                for classdef in collect_classdefs(imported_tree, requested):
                    collect_imported_class(classdef, local, class_aliases)
            elif export.kind == "interface":
                for interfacedef in collect_interfacedefs(imported_tree, requested):
                    collect_imported_interface(interfacedef, local)

    for node in _iter_import_name_nodes(tree):
        for module, local in _import_name_modules(node):
            mod_path = module_index.resolve_module(source_path, module)
            if mod_path is None:
                continue
            facts = module_index.facts_by_path.get(mod_path)
            if facts is None:
                try:
                    facts = module_index.update_file(mod_path)
                except OSError:
                    continue
            imported_tree = module_tree(mod_path)
            if imported_tree is None:
                continue
            class_aliases = {
                export.name: f"{local}.{export.name}"
                for export in facts.exports.values()
                if export.kind == "class"
            }
            for export in facts.exports.values():
                target = f"{local}.{export.name}"
                if export.kind == "function":
                    if _export_is_private(export):
                        metadata.private_functions.add(target)
                        continue
                    for funcdef in collect_funcdefs(imported_tree, export.name):
                        sig = helper._func_signature(funcdef, target)
                        if sig is not None:
                            metadata.method_sigs.setdefault(target, []).append(sig)
                        record_func_return_type(funcdef, target, class_aliases)
                elif export.kind == "class":
                    for classdef in collect_classdefs(imported_tree, export.name):
                        collect_imported_class(classdef, target, class_aliases)
                elif export.kind == "interface":
                    for interfacedef in collect_interfacedefs(imported_tree, export.name):
                        collect_imported_interface(interfacedef, target)
    return metadata


def check_source(
    tree: Tree,
    *,
    extra_known_names: Optional[Iterable[str]] = None,
    go_blocks: Optional[dict[str, str]] = None,
    module_index: WorkspaceIndex | None = None,
    source_path: Path | None = None,
) -> List[SemanticError]:
    """Convenience wrapper used from the CLI."""
    ast_module = build_module(tree)
    imported_calls = _ImportedCallMetadata()
    if module_index is not None and source_path is not None:
        imported_calls = _imported_call_metadata(
            tree,
            module_index=module_index,
            source_path=source_path,
        )
    errors = SemanticChecker(
        extra_known_names=extra_known_names,
        go_blocks=go_blocks,
        imported_func_sigs=imported_calls.func_sigs,
        imported_func_return_types=imported_calls.func_return_types,
        imported_func_return_type_texts=imported_calls.func_return_type_texts,
        imported_method_return_type_texts=imported_calls.method_return_type_texts,
        imported_method_sigs=imported_calls.method_sigs,
        imported_interface_methods=imported_calls.interface_methods,
        imported_private_functions=imported_calls.private_functions,
        imported_constructor_sigs=imported_calls.constructor_sigs,
        imported_class_members=imported_calls.class_members,
        imported_class_fields=imported_calls.class_fields,
        imported_class_static_methods=imported_calls.class_static_methods,
        imported_class_instance_methods=imported_calls.class_instance_methods,
        imported_class_method_shapes=imported_calls.class_method_shapes,
        imported_class_bases=imported_calls.class_bases,
        imported_class_private_methods=imported_calls.class_private_methods,
    ).check(
        tree,
        ast_module=ast_module,
    )
    if module_index is not None and source_path is not None:
        errors.extend(_check_imported_exports(
            tree,
            module_index=module_index,
            source_path=source_path,
        ))
    return errors
