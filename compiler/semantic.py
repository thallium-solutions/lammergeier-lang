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
- **Go-only reserved identifiers**: defining a variable / parameter
  whose name is a Go keyword (``chan``, ``defer``, ``select``, …)
  would generate a Go build error whose line points at synthetic Go
  output; the checker surfaces it at the .lam location instead.

The checker walks the parse tree directly without invoking the
transpiler, so it's cheap to run on every build.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import get_close_matches
from typing import Iterable, List, Optional, Set

from lark import Tree, Token

from compiler.constants import PYTHON_EXCEPTIONS


# F-strings are parsed as a single ``FSTRING`` token (the grammar
# treats the whole literal opaquely), so the expression walker
# below never visits the identifiers inside ``{...}`` slots.
# The pattern below matches every Python-style identifier so the
# unused-binding pass can mark referenced names regardless.
_FSTRING_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


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

    def format(self, source_lines: List[str], path: str) -> str:
        """Render this error with a 3-line source snippet."""
        label = "warning" if self.is_warning else ""
        prefix = f"{label}: " if label else ""
        lines = [f"  line {self.line}: {prefix}{self.message}"]
        start = max(0, self.line - 2)
        end = min(len(source_lines), self.line + 1)
        for i in range(start, end):
            marker = ">>>" if i == self.line - 1 else "   "
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
    param_nodes: dict = field(default_factory=dict)  # name -> Tree/Token
    is_loop: bool = False


class SemanticChecker:
    """Walks a parsed Lam tree and accumulates :class:`SemanticError`."""

    def __init__(self, *, extra_known_names: Optional[Iterable[str]] = None):
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

    # ─── Public API ────────────────────────────────────────────

    def check(self, tree: Tree) -> List[SemanticError]:
        """Run all checks against the parse tree and return any errors."""
        # Module scope holds top-level function names, class names,
        # and import bindings. We collect these up front so forward
        # references across the file don't false-positive.
        module = _Scope(kind="module")
        module.names |= (BUILTIN_FUNCS | BUILTIN_CONSTANTS | STDLIB_MODULES
                         | PYTHON_EXCEPTIONS)
        module.names |= self._extra_known
        self._import_records = []
        self._collect_module_defs(tree, module)
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

    def _collect_module_defs(self, tree: Tree, scope: _Scope) -> None:
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
        seen_func: dict = {}   # (name, arity) → node
        seen_class: dict = {}  # name → node
        seen_import: Set[str] = set()
        self._module_seen: dict = {}   # name → ("import"|"class"|"func"), used by shadow check
        for child in tree.children:
            if not isinstance(child, Tree):
                continue
            self._collect_top_decl_with_dupe_check(
                child, scope, seen_func, seen_class, seen_import,
            )

    def _collect_top_decl_with_dupe_check(
        self,
        node: Tree,
        scope: _Scope,
        seen_func: dict,
        seen_class: dict,
        seen_import: Set[str],
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
                scope.names.add(name)
                arity = len(self._param_names(self._funcdef_params(node)))
                key = (name, arity)
                if key in seen_func:
                    self._error(
                        node, "duplicate",
                        f"duplicate function `{name}` with {arity} "
                        f"parameter{'s' if arity != 1 else ''} "
                        f"(overloading requires a different arity)",
                    )
                else:
                    seen_func[key] = node
                self._module_seen[name] = "func"
        elif d == "classdef":
            name = self._classdef_name(node)
            if name:
                scope.names.add(name)
                if name in seen_class:
                    self._error(
                        node, "duplicate",
                        f"duplicate class `{name}`",
                    )
                else:
                    seen_class[name] = node
                self._module_seen[name] = "class"
        elif d == "interfacedef":
            if node.children and isinstance(node.children[0], Tree):
                n = self._name_text(node.children[0])
                scope.names.add(n)
                if n in seen_class:
                    self._error(
                        node, "duplicate",
                        f"duplicate interface `{n}` "
                        f"(conflicts with an earlier class/interface)",
                    )
                else:
                    seen_class[n] = node
                self._module_seen[n] = "class"
        elif d == "decorated":
            for c in node.children:
                if isinstance(c, Tree) and c.data in ("classdef", "funcdef"):
                    self._collect_top_decl_with_dupe_check(
                        c, scope, seen_func, seen_class, seen_import,
                    )
        elif d in ("import_from", "import_name"):
            for n in self._import_bindings(node):
                scope.names.add(n)
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
                        inner, scope, seen_func, seen_class, seen_import,
                    )
        elif d in ("assign_stmt", "annassign", "assign"):
            for n in self._assign_targets(node):
                scope.names.add(n)
        elif d == "const_stmt":
            n = self._const_target_name(node)
            if n:
                scope.names.add(n)
        elif d == "simple_stmt":
            for sub in node.children:
                if isinstance(sub, Tree):
                    self._collect_top_decl_with_dupe_check(
                        sub, scope, seen_func, seen_class, seen_import,
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
            self._visit_funcdef(node)
            return
        if d == "classdef":
            self._visit_classdef(node)
            return
        if d == "decorated":
            for c in node.children:
                if isinstance(c, Tree):
                    self._visit_stmt(c)
            return
        if d == "interfacedef":
            return  # No expressions inside; nothing to check.

        # Bare assignment-like statements at module/function level may
        # introduce new names — record them before checking expressions
        # so the same line ``x = x + 1`` is accepted (forward use of
        # ``x`` from an enclosing scope).
        if d in ("assign_stmt", "annassign", "assign", "augassign"):
            for n in self._assign_targets(node):
                if self._is_const(n):
                    self._error(
                        node, "const",
                        f"cannot reassign constant `{n}`",
                    )
                self._check_shadow_builtin_or_import(node, n)
                self._check_go_reserved(node, n)
                self._declare(n)
            self._visit_expr_subtree(node)
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
                    f"`{kw}` outside of a loop",
                )
            return
        if d == "return_stmt":
            if not any(s.kind == "function" for s in self._scopes):
                self._error(node, "flow", "`return` outside of a function")
            self._visit_expr_subtree(node)
            return

        if d in ("raise_stmt", "del_stmt", "assert_stmt", "expr_stmt",
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
        scope.names.add("self")
        for tp in self._funcdef_type_params(node):
            scope.names.add(tp)
        params_node = self._funcdef_params(node)
        # Capture parameter -> node map up front so an unused-parameter
        # warning can cite the parameter itself (not the function
        # header). ``_param_node_map`` mirrors ``_param_names`` shape
        # but returns ``{name: node}`` for diagnostic locations.
        param_node_map = self._param_node_map(params_node)
        for p in self._param_names(params_node):
            scope.names.add(p)
            if p in param_node_map:
                scope.param_nodes[p] = param_node_map[p]
        suite_node = self._suite_node(node)
        if suite_node is not None:
            self._collect_block_defs(suite_node, scope)
        self._scopes.append(scope)
        try:
            if suite_node is not None:
                self._walk_suite_stmts(self._suite_stmts(suite_node))
            self._emit_unused_param_warnings(scope, params_node)
        finally:
            self._scopes.pop()

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

    def _visit_classdef(self, node: Tree) -> None:
        # Class scope is mostly a holder for nested method visits — the
        # method-name space is checked separately for duplicates.
        suite_node = self._suite_node(node)

        # Duplicate-member check.
        seen: Set[str] = set()
        if suite_node is not None:
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

        # Register class-level type-parameter names so methods see them
        # even though they aren't in any per-method scope yet.
        scope = _Scope(kind="class")
        scope.names.add("self")
        for tp in self._funcdef_type_params(node):
            scope.names.add(tp)
        # Methods can reference the class's own field names via ``self.x``.
        # Field references go through getattr lookups (we skip those in
        # ``_visit_expr_subtree``) so we don't need to register them.
        self._scopes.append(scope)
        try:
            if suite_node is not None:
                self._walk_suite_stmts(self._suite_stmts(suite_node))
        finally:
            self._scopes.pop()

    def _visit_if(self, node: Tree) -> None:
        # ``if test suite elifs ["else" suite]`` — Lam doesn't enforce
        # block-level shadowing inside ifs (the def-collection pass
        # already hoisted any local bindings to the enclosing scope),
        # so we walk the bodies in place rather than pushing a new
        # scope. This lets ``return`` / ``break`` / ``continue`` be
        # checked against the *enclosing* function/loop correctly.
        for c in node.children:
            if not isinstance(c, Tree):
                continue
            if c.data == "suite":
                self._walk_suite_stmts(self._suite_stmts(c))
            elif c.data == "elifs":
                for elif_node in c.children:
                    if isinstance(elif_node, Tree):
                        self._visit_if(elif_node)
            elif c.data == "elif_":
                for sub in c.children:
                    if isinstance(sub, Tree):
                        if sub.data == "suite":
                            self._walk_suite_stmts(self._suite_stmts(sub))
                        else:
                            self._visit_expr_subtree(sub)
            else:
                # Test expression.
                self._visit_expr_subtree(c)

    def _visit_for(self, node: Tree) -> None:
        # Grammar:  ``for_stmt: "for" for_target "in" testlist suite ["else" suite]``
        target_node = node.children[0] if node.children else None
        iterable = node.children[1] if len(node.children) > 1 else None
        bodies = [c for c in node.children[2:] if isinstance(c, Tree) and c.data == "suite"]
        if iterable is not None:
            self._visit_expr_subtree(iterable)
        scope = _Scope(kind="block", is_loop=True)
        for n in self._for_target_names(target_node):
            scope.names.add(n)
        if bodies:
            self._collect_block_defs(bodies[0], scope)
        self._scopes.append(scope)
        try:
            if bodies:
                self._walk_suite_stmts(self._suite_stmts(bodies[0]))
        finally:
            self._scopes.pop()
        if len(bodies) > 1:
            self._walk_suite_stmts(self._suite_stmts(bodies[1]))

    def _visit_while(self, node: Tree) -> None:
        cond = node.children[0] if node.children else None
        bodies = [c for c in node.children[1:] if isinstance(c, Tree) and c.data == "suite"]
        if cond is not None:
            self._visit_expr_subtree(cond)
        scope = _Scope(kind="block", is_loop=True)
        if bodies:
            self._collect_block_defs(bodies[0], scope)
        self._scopes.append(scope)
        try:
            if bodies:
                self._walk_suite_stmts(self._suite_stmts(bodies[0]))
        finally:
            self._scopes.pop()
        if len(bodies) > 1:
            self._walk_suite_stmts(self._suite_stmts(bodies[1]))

    def _visit_match(self, node: Tree) -> None:
        # Subject expression first.
        if node.children:
            self._visit_expr_subtree(node.children[0])
        # Each case introduces its own bindings via ``as_pattern`` /
        # capture-style names. Conservatively, we hoist every name
        # found anywhere in any pattern into a single match-scope so
        # the case-body expression check doesn't false-positive.
        scope = _Scope(kind="block")
        for c in node.children[1:]:
            if isinstance(c, Tree):
                self._collect_pattern_names(c, scope)
                self._collect_block_defs(c, scope)
        self._scopes.append(scope)
        try:
            for c in node.children[1:]:
                if isinstance(c, Tree):
                    # Check pattern subexpressions then the suite.
                    for sub in c.children:
                        if isinstance(sub, Tree) and sub.data == "suite":
                            self._walk_suite_stmts(self._suite_stmts(sub))
        finally:
            self._scopes.pop()

    def _visit_do(self, node: Tree) -> None:
        """Validate ``do { body } catch err { handler }``.

        Body and handler each run in their own block scope. The catch
        ``name`` is mandatory and bound for the handler's scope only.
        """
        if len(node.children) < 3:
            return
        body_suite = node.children[0]
        err_name_node = node.children[1]
        handler_suite = node.children[2]

        if isinstance(body_suite, Tree) and body_suite.data == "suite":
            self._enter_block_and_visit(body_suite)

        err_name = self._name_text(err_name_node)
        if isinstance(handler_suite, Tree) and handler_suite.data == "suite":
            scope = _Scope(kind="block")
            if err_name:
                scope.names.add(err_name)
            self._collect_block_defs(handler_suite, scope)
            self._scopes.append(scope)
            try:
                self._walk_suite_stmts(self._suite_stmts(handler_suite))
            finally:
                self._scopes.pop()

    def _visit_try(self, node: Tree) -> None:
        # Grammar:  ``try_stmt: "try" suite catch_clauses ["else" suite] [finally]``
        # All sub-suites get their own block scope. ``catch_clauses``
        # nests one or more ``catch_clause`` children, each of which
        # may bind an ``as e`` name.
        for c in node.children:
            if not isinstance(c, Tree):
                continue
            if c.data == "suite":
                self._enter_block_and_visit(c)
            elif c.data == "catch_clauses":
                for cc in c.children:
                    if isinstance(cc, Tree) and cc.data == "catch_clause":
                        self._visit_catch_clause(cc)
            elif c.data == "finally":
                # ``finally: "finally" suite``
                for sub in c.children:
                    if isinstance(sub, Tree) and sub.data == "suite":
                        self._enter_block_and_visit(sub)

    def _visit_catch_clause(self, node: Tree) -> None:
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
            scope.names.add(alias)
        if type_test is not None:
            self._visit_expr_subtree(type_test)
        if body_suite is not None:
            self._collect_block_defs(body_suite, scope)
        self._scopes.append(scope)
        try:
            if body_suite is not None:
                self._walk_suite_stmts(self._suite_stmts(body_suite))
        finally:
            self._scopes.pop()

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
                                scope.names.add(self._name_text(sub))
                            elif isinstance(sub, Token):
                                # Only the optional ``as`` alias appears
                                # as a token here (the keyword itself is
                                # filtered out by the grammar).
                                scope.names.add(str(sub))
                        # Also scan the resource expression.
                        if item.children:
                            self._visit_expr_subtree(item.children[0])
        if suite_node is not None:
            self._collect_block_defs(suite_node, scope)
        self._scopes.append(scope)
        try:
            if suite_node is not None:
                self._walk_suite_stmts(self._suite_stmts(suite_node))
        finally:
            self._scopes.pop()

    def _enter_block_and_visit(self, suite: Tree) -> None:
        scope = _Scope(kind="block")
        self._collect_block_defs(suite, scope)
        self._scopes.append(scope)
        try:
            self._walk_suite_stmts(self._suite_stmts(suite))
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
            if name and not self._is_resolved(name):
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

        # ``getattr`` on a base name only checks the base — the
        # attribute itself is never a top-level binding.
        if d in ("getattr", "getattr_safe"):
            if node.children:
                self._visit_expr_subtree(node.children[0])
            return

        # Function calls: we only inspect the callee and the args, but
        # already-handled by recursing into children. Static-method
        # calls like ``Math.sqrt(x)`` and module attribute calls show
        # up as ``getattr`` so the base-name check above suffices.
        if d == "lambdef":
            self._visit_lambda(node)
            return

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

        for child in node.children:
            self._visit_expr_subtree(child)

    def _visit_lambda(self, node: Tree) -> None:
        scope = _Scope(kind="function")
        body = None
        for c in node.children:
            if isinstance(c, Tree):
                if c.data == "typed_parameters":
                    for n in self._param_names(c):
                        scope.names.add(n)
                elif c.data in ("paren_lambda_params", "inline_lambda_params"):
                    # Each child is either a ``typed_lambda_param``
                    # (``x: int``), an untyped ``inline_lambda_param``,
                    # an inline ``name`` tree, or a bare ``NAME`` token.
                    for sub in c.children:
                        if isinstance(sub, Tree):
                            if sub.data in ("typed_lambda_param",
                                            "inline_lambda_param"):
                                if sub.children:
                                    scope.names.add(self._name_text(sub.children[0]))
                            elif sub.data == "name":
                                scope.names.add(self._name_text(sub))
                        elif isinstance(sub, Token):
                            scope.names.add(str(sub))
                elif c.data == "lambda_return_anno":
                    # Skip — return type doesn't introduce names.
                    pass
                else:
                    body = c
            elif isinstance(c, Token):
                scope.names.add(str(c))
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
                    scope.names.add(n)
        self._scopes.append(scope)
        try:
            for c in node.children:
                self._visit_expr_subtree(c)
        finally:
            self._scopes.pop()

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
        pool: Set[str] = set()
        for scope in self._scopes:
            pool |= scope.names
        pool |= BUILTIN_FUNCS
        pool |= BUILTIN_CONSTANTS
        pool |= STDLIB_MODULES
        pool.discard(name)
        matches = get_close_matches(name, list(pool), n=1, cutoff=0.75)
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
        # Walk type expression (skipped — type names aren't validated
        # here yet) and RHS for undefined-name checks.
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
                    self._collect_pattern_names(c, scope)
                    for sub in c.children:
                        if isinstance(sub, Tree) and sub.data == "suite":
                            self._collect_block_defs(sub, scope)
            return
        if d == "simple_stmt":
            for sub in stmt.children:
                self._collect_stmt_defs(sub, scope)
            return

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

    @staticmethod
    def _suite_node(node: Tree) -> Optional[Tree]:
        for c in node.children:
            if isinstance(c, Tree) and c.data == "suite":
                return c
        return None

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
    out = [f"error: semantic check failed for {path}"]
    for err in only_errors:
        out.append(err.format(lines, path))
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
    out = [f"warning: semantic check noticed {len(only_warns)} "
           f"issue{'s' if len(only_warns) != 1 else ''} in {path}"]
    for err in only_warns:
        out.append(err.format(lines, path))
    return "\n".join(out)


def has_errors(errors: List[SemanticError]) -> bool:
    """``True`` if any entry in ``errors`` has ``severity=\"error\"``.
    Warnings alone return ``False`` so callers can let the build
    proceed when only advisory diagnostics fired.
    """
    return any(not e.is_warning for e in errors)


def check_source(tree: Tree, *, extra_known_names: Optional[Iterable[str]] = None) -> List[SemanticError]:
    """Convenience wrapper used from the CLI."""
    return SemanticChecker(extra_known_names=extra_known_names).check(tree)
