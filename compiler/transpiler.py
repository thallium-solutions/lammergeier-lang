#!/usr/bin/env python3
"""
Transpiler: Lammergeier Lang AST → Go source code.

This is the main entry point. The transpiler is composed of several mixin classes:
  - HelpersMixin:            naming, type conversion, utilities
  - StatementVisitorMixin:   if/for/while/try/match/with/assign/return/...
  - ExpressionVisitorMixin:  _expr_to_go, function calls, f-strings, comprehensions
  - DefinitionVisitorMixin:  funcdef, classdef, interfacedef

The preprocessor (preprocess_go_blocks) lives in compiler.preprocessor.
Constants (TYPE_MAP, PYTHON_EXCEPTIONS, etc.) live in compiler.constants.
"""

from __future__ import annotations
from lark import Tree, Token
from typing import List, Optional, Tuple, Dict, Set, Any
from contextlib import contextmanager

from compiler.constants import STMT_NODES, PYTHON_EXCEPTIONS
from compiler.preprocessor import preprocess_go_blocks  # re-export
from compiler.visitors.helpers import HelpersMixin
from compiler.visitors.statements import StatementVisitorMixin
from compiler.visitors.expressions import ExpressionVisitorMixin
from compiler.visitors.definitions import DefinitionVisitorMixin


# Names of scoped context attributes kept on GoTranspiler. The `_scoped`
# context manager saves and restores any of these when a visitor enters
# a nested construct (method body, call head, try block, etc.). Keeping
# them in one list documents the intent and makes it easy to audit which
# flags have block-scope semantics vs global lifetime.
SCOPED_CONTEXT_ATTRS: tuple[str, ...] = (
    "_in_funccall_head",
    "_self_replacement",
    "_at_top_level",
    "current_class",
    "_in_try_func",
    "_in_try_iife",
    "_in_except_handler",
    "_in_async_func",
    "_async_chan_name",
    "_ternary_go_type",
    "_while_else_break",
    "_for_else_break",
    "_in_generator",
    "_generator_chan",
    "_propagate_cast_hint",
    # The Go return type of the *currently-being-emitted* Lam function.
    # Used to rewrite bare ``return`` statements found inside ``go!``
    # blocks of typed functions to ``return <zero-value>`` so the
    # author doesn't have to know the Go signature constraints.
    "_current_return_type",
)


class GoTranspiler(
    HelpersMixin,
    StatementVisitorMixin,
    ExpressionVisitorMixin,
    DefinitionVisitorMixin,
):
    """Walk a Lark tree and produce Go source code."""

    def __init__(self, go_blocks: Optional[Dict[str, str]] = None,
                 stdlib_path: Optional[str] = None,
                 go_module_name: str = "lamc/app",
                 source_file: Optional[str] = None):
        self.indent: int = 0
        self.output_lines: List[str] = []

        # Import tracking
        self.needed_imports: Set[str] = set()
        self.user_go_imports: Set[str] = set()

        # Class tracking
        self.current_class: Optional[str] = None
        self.class_fields: Dict[str, List[Tuple[str, str]]] = {}
        self.class_static_fields: Dict[str, List[Tuple[str, str, Any, bool]]] = {}
        self.class_bases: Dict[str, List[str]] = {}

        # Variable tracking
        self.declared_vars: Set[str] = set()
        self.scope_stack: List[Set[str]] = []

        # Init param types for field inference
        self._init_param_types: Dict[str, str] = {}

        # Try/except context
        self._in_try_func: bool = False
        # ``_in_try_iife`` is set while emitting code that lives
        # inside the IIFE that wraps a try-with-catch. Bare ``return
        # X`` inside this IIFE must set ``__lamShouldReturn = true``
        # and (when the function has a typed return) ``retval = X``,
        # then bare-return *from the IIFE*. After the IIFE completes,
        # the function checks ``__lamShouldReturn`` to decide whether
        # to propagate the return.
        self._in_try_iife: bool = False
        self._in_except_handler: bool = False

        # Self replacement for methods
        self._self_replacement: Optional[str] = None

        # Set while resolving the callee of a function call, so bare
        # function identifiers are emitted raw (the call site will rename
        # them as needed). Without this guard, argument-position
        # identifiers get rewritten to Go public names (desired), but
        # call-site identifiers would also get rewritten and then fail
        # the `name in _user_functions` lookup downstream.
        self._in_funccall_head: bool = False

        # Async function state
        self._in_async_func: bool = False
        self._async_chan_name: Optional[str] = None

        # Ternary context type
        self._ternary_go_type: Optional[str] = None

        # While-else / for-else break flag
        self._while_else_break: bool = False
        self._for_else_break: bool = False

        # Go blocks (preprocessed raw Go code, keyed by id)
        self.go_blocks: Dict[str, str] = go_blocks or {}

        # Stdlib path
        self.stdlib_path = stdlib_path

        # Go module
        self.go_module_name = go_module_name

        # Global-level variable declarations
        self._at_top_level: bool = True

        # Track all user-defined functions
        self._user_functions: Set[str] = set()
        self._private_functions: Set[str] = set()
        self._private_methods: Dict[str, Set[str]] = {}

        # Functions imported from libraries — survive the per-transpile
        # reset so library-imported names still drive the call-site
        # dispatcher (default-arg filling, Go-public-name rewriting).
        self._external_user_functions: Set[str] = set()
        self._external_private_functions: Set[str] = set()

        # Top-level go! raw lines
        self._raw_go_top: List[str] = []

        # Operator overloading
        self._class_dunder_methods: Dict[str, Dict[str, str]] = {}

        # Custom tpy library imports
        self._tpy_imports: List[str] = []

        # Generator state
        self._in_generator = False
        self._generator_chan = None

        # Known interface / class names
        self._interfaces: set = set()
        self._class_names: set = set()

        # Static methods per class
        self._static_methods: Dict[str, Set[str]] = {}
        # Static variables per class. Values are ``var_name -> is_private``.
        self._static_vars: Dict[str, Dict[str, bool]] = {}
        self._static_var_go_names: Set[str] = set()

        # Known generator function names
        self._generator_functions: set = set()

        # Variable type tracking
        self._var_types: Dict[str, str] = {}

        # Source-line mapping
        self._source_file = source_file
        self._last_line_directive = 0

        # Default parameter values
        self._func_defaults: Dict[str, List[Tuple[int, Any]]] = {}
        self._func_param_counts: Dict[str, int] = {}

        # Per-function parameter names (in declaration order).
        # Used by the call-site dispatcher to reorder keyword arguments
        # (``f(name="alice", times=3)``) into the positional shape that
        # the emitted Go function actually expects.
        self._func_param_names: Dict[str, List[str]] = {}

        # Method return types — keyed as ``Class.method`` and used by
        # ``_infer_receiver_class`` to thread receiver-class info
        # through chained fluent calls (so ``db.table(...).where(...)``
        # resolves ``where`` against ``QueryBuilder`` even when the
        # intermediate value is never assigned to a typed variable).
        # Values are the *raw* Lam class name (e.g. ``QueryBuilder``)
        # so cross-library reuse keeps working.
        self._method_return_types: Dict[str, str] = {}

        # Overloaded functions
        self._overloaded_functions: Dict[str, set] = {}

        # Variadic functions
        self._variadic_functions: set = set()
        self._var_go_types: Dict[str, str] = {}

        # Generic type parameters.
        # ``_generic_classes[C]`` holds the Go type-parameter clause
        # (e.g. ``[T any, U comparable]``) for a generic class ``C`` so
        # its methods can inherit the clause on their receivers and so
        # ``_type_expr_to_go`` can refer to ``C[T]`` inside the body.
        # ``_generic_names`` holds the bare names of type parameters
        # currently in lexical scope (both class- and function-level);
        # uses in type annotations preserve them verbatim instead of
        # trying to resolve them as user classes.
        self._generic_classes: Dict[str, str] = {}
        self._generic_names: set = set()

        # ``?`` propagation operator state. ``_q_temp_counter`` produces
        # fresh ``__qN`` names for the temporaries each ``?`` introduces.
        # ``_propagate_cast_hint`` is the Go type the enclosing
        # annassign expects (or ``""``) — we wrap ``__q.Value`` with a
        # typed assertion when it's set so ``n: int = parseInt(s)?``
        # produces ``var n int = __q1.Value.(int)`` rather than an
        # untyped ``interface{}`` rvalue Go's compiler can't accept.
        self._q_temp_counter: int = 0
        self._propagate_cast_hint: str = ""

        # Go return type of the currently-emitted Lam function (set by
        # the funcdef visitor before walking the body). Empty string
        # means "no declared return", in which case ``return`` is a
        # complete Go statement on its own and no rewriting is needed.
        self._current_return_type: str = ""

        # ``do { } catch err { }`` block state. Each occurrence wraps
        # its body in a Go IIFE returning ``*Result``; the counter
        # produces fresh ``__rdoN`` names for the per-block result
        # variable.
        self._do_counter: int = 0

    # ─── Scoped context helper ────────────────────────────────

    @contextmanager
    def _scoped(self, **overrides):
        """Temporarily override scoped context attrs, restore on exit.

        Example:

            with self._scoped(_in_funccall_head=True):
                func_str = self._expr_to_go(func)

        Only attributes listed in ``SCOPED_CONTEXT_ATTRS`` are accepted;
        passing anything else raises ``AttributeError`` so we don't
        silently mis-scope a flag that wasn't intended to be saved.
        """
        saved: dict[str, Any] = {}
        for attr in overrides:
            if attr not in SCOPED_CONTEXT_ATTRS:
                raise AttributeError(
                    f"{attr!r} is not in SCOPED_CONTEXT_ATTRS; add it there "
                    "if it legitimately needs scoped save/restore."
                )
            saved[attr] = getattr(self, attr)
        try:
            for attr, value in overrides.items():
                setattr(self, attr, value)
            yield self
        finally:
            for attr, value in saved.items():
                setattr(self, attr, value)

    # ─── Visitor dispatch ──────────────────────────────────────

    def _visit(self, node):
        if node is None:
            return
        if isinstance(node, Token):
            return str(node)
        if node.data in STMT_NODES:
            self._emit_line_directive(node)
        handler = getattr(self, f"_visit_{node.data}", None)
        if handler:
            return handler(node)
        results = []
        for child in node.children:
            if isinstance(child, Tree):
                r = self._visit(child)
                if r:
                    results.append(r)
        return " ".join(results) if results else ""

    # ─── Main entry ────────────────────────────────────────────

    def transpile(self, tree: Tree) -> str:
        """Transpile a full file_input tree to Go source."""
        self.output_lines = []
        self.needed_imports = set()
        self.user_go_imports = set()
        self.current_class = None
        self.class_fields = {}
        self.class_static_fields = {}
        self.class_bases = {}
        self.declared_vars = set()
        self.scope_stack = []
        self._user_functions = set()
        self._private_functions = set()
        self._private_methods = {}
        self._raw_go_top = []
        self._static_var_go_names = {
            self._static_var_go_name(cls, name)
            for cls, fields in self._static_vars.items()
            for name in fields
        }

        # Pass 0: collect user-defined function names
        self._collect_function_names(tree)
        # Library-imported functions are also "user functions" from the
        # call-site dispatcher's POV — without this, calls to library
        # functions would bypass default-arg filling.
        self._user_functions.update(self._external_user_functions)
        self._private_functions.update(self._external_private_functions)

        # Reject silent name collisions before they get to ``go build``.
        # Lam title-cases public function names to follow Go's export
        # convention, which can clash with a same-spelled class. The
        # generated Go would then refuse to compile with ``XYZ
        # redeclared in this block``. Catch it here with a clear
        # message — the only fix is to rename one of the symbols.
        for fn in self._user_functions:
            if fn == "main" or fn in self._private_functions:
                continue
            go_name = self._go_public_name(fn)
            if go_name in self._class_names:
                raise RuntimeError(
                    f"name collision: function '{fn}' compiles to Go "
                    f"identifier '{go_name}' which is already used by "
                    f"class '{go_name}'. Rename one of them — e.g. "
                    f"use a verb-ish function name like 'new{go_name}' "
                    f"or 'make{go_name}'."
                )

        # Pass 1: collect class field info
        self._collect_class_fields(tree)

        # Pass 2: generate code
        body_lines: List[str] = []
        old_lines = self.output_lines
        self.output_lines = body_lines
        self._at_top_level = True

        for child in tree.children:
            if child is None:
                continue
            if isinstance(child, Tree):
                self._visit(child)

        self.output_lines = old_lines

        # Build the final file
        header_lines = ["package main", ""]

        for bid, raw in self.go_blocks.items():
            if "fmt." in raw:
                self.needed_imports.add("fmt")

        all_imports = set()
        for pkg in self.needed_imports:
            all_imports.add(f'"{pkg}"')
        for pkg in self.user_go_imports:
            if '"' in pkg:
                all_imports.add(pkg)
            else:
                all_imports.add(f'"{pkg}"')

        if all_imports:
            header_lines.append("import (")
            for imp in sorted(all_imports):
                header_lines.append(f"\t{imp}")
            header_lines.append(")")
            header_lines.append("")

        for line in self._raw_go_top:
            header_lines.append(line)
        if self._raw_go_top:
            header_lines.append("")

        return "\n".join(header_lines + body_lines) + "\n"

    # ─── Pass 0: Collect function names ────────────────────────

    def _collect_function_names(self, tree: Tree):
        if not isinstance(tree, Tree):
            return
        if tree.data == "funcdef":
            _, _, _, name_node, params_node, _, _, _ = self._parse_funcdef(tree)
            name = self._get_name(name_node)
            self._user_functions.add(name)
            for child in tree.children:
                if isinstance(child, Token) and str(child) == "private":
                    self._private_functions.add(name)
                    break
            # Check for generator
            suite_node = tree.children[-1]
            if self._tree_contains(suite_node, "yield_expr"):
                self._generator_functions.add(name)
            # Check variadic
            variadic = self._has_variadic(params_node) if params_node else False
            if variadic:
                self._variadic_functions.add(name)
            # Count params and collect defaults
            arity = self._count_params(params_node)
            self._func_param_counts[name] = arity
            if params_node:
                self._collect_param_defaults(name, params_node)
            # Track overloading (only for non-variadic)
            if not variadic:
                if name not in self._overloaded_functions:
                    self._overloaded_functions[name] = set()
                self._overloaded_functions[name].add(arity)

        elif tree.data == "classdef":
            name_node = tree.children[0]
            class_name = self._get_name(name_node)
            self._class_names.add(class_name)
            # Collect base classes
            for child in tree.children[1:]:
                if isinstance(child, Tree) and child.data == "arguments":
                    for arg in child.children:
                        if isinstance(arg, Tree):
                            base = self._expr_to_go(arg)
                            if base:
                                self.class_bases.setdefault(class_name, []).append(base)
            # Scan methods
            suite = tree.children[-1]
            for child in self._suite_stmts(suite):
                if isinstance(child, Tree) and child.data == "funcdef":
                    (is_priv, is_stat, _, fn_node, params_node,
                     return_type_node, _, _) = self._parse_funcdef(child)
                    fn_name = self._get_name(fn_node)
                    if is_stat:
                        self._static_methods.setdefault(class_name, set()).add(fn_name)
                    if is_priv:
                        self._private_methods.setdefault(class_name, set()).add(fn_name)
                    # Collect defaults for methods (keyed as class_name.method).
                    # Normalise __init__ -> init so constructor calls
                    # (which use the `Class.init` key) pick up defaults too.
                    key_name = "init" if fn_name == "__init__" else fn_name
                    method_key = f"{class_name}.{key_name}"
                    arity = self._count_params(params_node, skip_self=True)
                    self._func_param_counts[method_key] = arity
                    if params_node:
                        self._collect_param_defaults(method_key, params_node, skip_self=True)
                    # Track the *raw* Lam return-type name so
                    # ``_infer_receiver_class`` can thread the class
                    # forward across chained calls. Only single-return
                    # types referencing a known user class are useful
                    # here; ``list[...]``/``dict[...]``/``str``/etc.
                    # have no class identity and are skipped.
                    raw_return = self._get_raw_type_name(return_type_node) if return_type_node else ""
                    if raw_return:
                        self._method_return_types[method_key] = raw_return
        elif tree.data == "interfacedef":
            name_node = tree.children[0]
            self._interfaces.add(self._get_name(name_node))

        # Don't recurse into class / interface bodies — their methods
        # are already harvested above. Recursing would dump every
        # ``static func Ok(...)`` inside ``class Result`` into
        # ``_user_functions`` as if it were a top-level function,
        # which corrupts call-site dispatch downstream (the main
        # transpiler now reads imported libs' user-functions for
        # cross-library default-arg filling).
        if tree.data in ("classdef", "interfacedef"):
            return

        # Don't recurse into function bodies either — nested ``func``
        # declarations are emitted as Go closures (see
        # ``_emit_nested_funcdef``) and must NOT be dumped into
        # ``_user_functions`` or the call-site dispatcher will rewrite
        # ``inner(...)`` to ``Inner(...)`` and the closure name
        # disappears.
        if tree.data == "funcdef":
            return

        for child in tree.children:
            if isinstance(child, Tree):
                self._collect_function_names(child)

    def _count_params(self, params_node, skip_self=False) -> int:
        if not isinstance(params_node, Tree) or params_node.data != "typed_parameters":
            return 0
        count = 0
        for child in params_node.children:
            if child is None:
                continue
            if isinstance(child, Tree) and child.data == "typed_paramvalue":
                param = child.children[0]
                if isinstance(param, Tree) and param.data == "typed_param":
                    name = self._get_name(param.children[0])
                    if skip_self and name == "self":
                        continue
                count += 1
            elif isinstance(child, Tree) and child.data == "typed_starparams":
                count += 1
        return count

    def _has_variadic(self, params_node) -> bool:
        if not isinstance(params_node, Tree) or params_node.data != "typed_parameters":
            return False
        for child in params_node.children:
            if isinstance(child, Tree) and child.data == "typed_starparams":
                return True
        return False

    def _collect_param_defaults(self, func_name: str, params_node, skip_self=False):
        if not isinstance(params_node, Tree) or params_node.data != "typed_parameters":
            return
        param_idx = 0
        total = 0
        defaults = []
        # Track parameter names in declaration order for keyword-arg
        # dispatch. ``*args`` / ``**kwargs`` collapse to the synthetic
        # name "" so positional indices still line up — keyword args
        # can't address the variadic slot, which matches Python's
        # behaviour.
        names: List[str] = []
        for child in params_node.children:
            if child is None:
                continue
            if isinstance(child, Tree) and child.data == "typed_paramvalue":
                pnode = child.children[0]
                if isinstance(pnode, Tree) and pnode.data == "typed_param":
                    pname = self._get_name(pnode.children[0])
                    if skip_self and pname == "self":
                        continue
                    default_node = child.children[1] if len(child.children) > 1 else None
                    if default_node is not None:
                        defaults.append((param_idx, default_node))
                    names.append(pname or "")
                    param_idx += 1
                    total += 1
            elif isinstance(child, Tree) and child.data == "typed_param":
                pname = self._get_name(child.children[0])
                if skip_self and pname == "self":
                    continue
                names.append(pname or "")
                param_idx += 1
                total += 1
            elif isinstance(child, Tree) and child.data == "typed_starparams":
                names.append("")
                param_idx += 1
                total += 1
        if defaults:
            self._func_defaults[func_name] = defaults
        self._func_param_counts[func_name] = total
        if names:
            self._func_param_names[func_name] = names

    # ─── Pass 1: Collect class fields ──────────────────────────

    def _collect_class_fields(self, tree: Tree):
        if not isinstance(tree, Tree):
            return
        if tree.data == "classdef":
            name_node = tree.children[0]
            class_name = self._get_name(name_node)
            if class_name not in self.class_fields:
                self.class_fields[class_name] = []
            if class_name not in self.class_static_fields:
                self.class_static_fields[class_name] = []
            # Register generic classes up front so init-parameter and
            # field-type lowering (which runs as part of field
            # collection) can see ``T`` as a type-param name rather
            # than an unknown user class. ``_generic_classes`` is
            # a compile-wide map that the second pass relies on;
            # ``_generic_names`` is scoped to this class's field walk
            # so sibling classes don't accidentally see each other's
            # parameter names.
            tp_node = None
            for c in tree.children:
                if isinstance(c, Tree) and c.data == "type_params":
                    tp_node = c
                    break
            tp_names: list = []
            if tp_node is not None:
                tp_clause, tp_names, _ = self._type_params_to_go(tp_node)
                if tp_clause:
                    self._generic_classes[class_name] = tp_clause
            prev_generic_names = set(self._generic_names)
            if tp_names:
                self._generic_names.update(tp_names)
            try:
                suite = tree.children[-1]
                for child in self._suite_stmts(suite):
                    if isinstance(child, Tree):
                        if child.data == "funcdef":
                            _, _, _, fn_node, _, _, _, _ = self._parse_funcdef(child)
                            fn_name = self._get_name(fn_node)
                            if fn_name in ("__init__", "init"):
                                self._collect_init_fields(class_name, child)
                        elif child.data in ("annassign", "assign_stmt"):
                            self._extract_field_from_stmt(class_name, child)
            finally:
                self._generic_names = prev_generic_names
        for child in tree.children:
            if isinstance(child, Tree):
                self._collect_class_fields(child)

    def _collect_init_fields(self, class_name: str, funcdef: Tree):
        _, _, _, _, params_node, _, suite_node, _ = self._parse_funcdef(funcdef)
        # Build param type map for this init
        self._init_param_types = {}
        if params_node and isinstance(params_node, Tree):
            for child in params_node.children:
                if isinstance(child, Tree) and child.data == "typed_paramvalue":
                    param = child.children[0]
                    if isinstance(param, Tree) and param.data == "typed_param":
                        pname = self._get_name(param.children[0])
                        if pname != "self" and len(param.children) > 1:
                            ptype = self._type_expr_to_go(param.children[1])
                            self._init_param_types[pname] = ptype

        for stmt in self._suite_stmts(suite_node):
            if isinstance(stmt, Tree):
                if stmt.data == "annassign":
                    self._extract_field_from_assign(class_name, stmt)
                elif stmt.data == "assign_stmt":
                    for child in stmt.children:
                        if isinstance(child, Tree):
                            self._extract_field_from_assign(class_name, child)

    def _extract_field_from_stmt(self, class_name: str, node: Tree):
        if node.data == "assign_stmt":
            inner = node.children[0]
            if isinstance(inner, Tree):
                self._extract_field_from_assign(class_name, inner)
        elif node.data in ("annassign", "assign"):
            self._extract_field_from_assign(class_name, node)

    def _extract_field_from_assign(self, class_name: str, node: Tree):
        if node.data == "annassign":
            is_private, is_static, children = self._annassign_parts(node)
            if is_static:
                self._extract_static_var_from_annassign(
                    class_name, children, is_private,
                )
                return
            if not children:
                return
            target = children[0]
            type_node = children[1] if len(children) > 1 else None
            if isinstance(target, Tree) and target.data == "getattr":
                obj = target.children[0]
                if isinstance(obj, Tree) and obj.data == "var":
                    if self._get_name(obj.children[0]) == "self":
                        field_name = self._get_name(target.children[1])
                        go_type = self._type_expr_to_go(type_node) if type_node else "interface{}"
                        existing = [f for f, _ in self.class_fields.get(class_name, [])]
                        if field_name not in existing:
                            self.class_fields[class_name].append((field_name, go_type))
            elif isinstance(target, Tree) and target.data == "var":
                field_name = self._get_name(target.children[0])
                go_type = self._type_expr_to_go(type_node) if type_node else "interface{}"
                existing = [f for f, _ in self.class_fields.get(class_name, [])]
                if field_name not in existing:
                    self.class_fields[class_name].append((field_name, go_type))
        elif node.data == "assign":
            target = node.children[0]
            if isinstance(target, Tree) and target.data == "getattr":
                obj = target.children[0]
                if isinstance(obj, Tree) and obj.data == "var":
                    if self._get_name(obj.children[0]) == "self":
                        field_name = self._get_name(target.children[1])
                        go_type = "interface{}"
                        if len(node.children) > 1:
                            go_type = self._infer_type_from_value(node.children[1])
                        existing = [f for f, _ in self.class_fields.get(class_name, [])]
                        if field_name not in existing:
                            self.class_fields[class_name].append((field_name, go_type))

    def _annassign_parts(self, node: Tree) -> Tuple[bool, bool, List]:
        """Return ``(private, static, children_without_modifiers)``."""
        is_private = False
        is_static = False
        children = list(node.children)
        while children and (
            children[0] is None
            or (
                isinstance(children[0], Token)
                and str(children[0]) in ("private", "static")
            )
        ):
            if isinstance(children[0], Token):
                tok = str(children[0])
                if tok == "private":
                    is_private = True
                elif tok == "static":
                    is_static = True
            children.pop(0)
        return is_private, is_static, children

    def _extract_static_var_from_annassign(
        self, class_name: str, children: List, is_private: bool,
    ) -> None:
        if len(children) < 2:
            return
        target = children[0]
        type_node = children[1]
        value_node = children[2] if len(children) > 2 else None
        if not (isinstance(target, Tree) and target.data == "var"):
            return
        var_name = self._get_name(target.children[0])
        if not var_name:
            return
        go_type = self._type_expr_to_go(type_node) if type_node else "interface{}"
        existing = {
            name for name, _, _, _ in self.class_static_fields.get(class_name, [])
        }
        self._static_vars.setdefault(class_name, {})[var_name] = bool(is_private)
        go_name = self._static_var_go_name(class_name, var_name)
        self._static_var_go_names.add(go_name)
        raw_type = self._get_raw_type_name(type_node)
        if raw_type and raw_type in self._class_names:
            self._var_types[go_name] = raw_type
        if go_type:
            self._var_go_types[go_name] = go_type
        if var_name not in existing:
            self.class_static_fields.setdefault(class_name, []).append(
                (var_name, go_type, value_node, bool(is_private))
            )

    def _static_var_go_name(self, class_name: str, var_name: str) -> str:
        base = f"{self._go_public_name(class_name)}_{var_name}"
        is_private = self._static_vars.get(class_name, {}).get(var_name, False)
        if is_private:
            return self._go_private_name(base)
        return self._go_public_name(base)

    def _is_static_var_go_name(self, name: str) -> bool:
        return name in self._static_var_go_names
