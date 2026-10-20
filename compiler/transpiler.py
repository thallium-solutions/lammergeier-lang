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
import re
from lark import Tree, Token
from typing import List, Optional, Tuple, Dict, Set, Any
from contextlib import contextmanager

from compiler.constants import (
    COMPILER_GO_PACKAGES, DUNDER_OPS, GO_BINDING_KEYWORDS, STMT_NODES, PYTHON_EXCEPTIONS,
)

# Names ``_go_binding_name`` gives a ``__lam`` suffix (besides ``__lam*`` /
# ``__q*`` temps).
_BINDING_RENAMED = COMPILER_GO_PACKAGES | GO_BINDING_KEYWORDS
from compiler.preprocessor import preprocess_go_blocks  # re-export
from compiler import narrowing as _narrowing
from compiler.index_bounds import mark_nonnegative_locals
from compiler.safe_nav import rewrite_result_safe_nav
from compiler.visitors.helpers import HelpersMixin
from compiler.visitors.statements import StatementVisitorMixin
from compiler.visitors.expressions import ExpressionVisitorMixin
from compiler.visitors.definitions import DefinitionVisitorMixin
from compiler.visitors.patterns import PatternMatchMixin
from compiler.visitors.subtyping import SubtypingMixin
from compiler.visitors.generics import GenericCallMixin
from compiler.visitors.narrowing import NarrowingLowerMixin


# Names of scoped context attributes kept on GoTranspiler. The `_scoped`
# context manager saves and restores any of these when a visitor enters
# a nested construct (method body, call head, try block, etc.). Keeping
# them in one list documents the intent and makes it easy to audit which
# flags have block-scope semantics vs global lifetime.
SCOPED_CONTEXT_ATTRS: tuple[str, ...] = (
    "_in_funccall_head",
    # Lowering an f-string slot parsed on its own (positions are not
    # module positions; ``_report_lam_error`` uses the statement line).
    "_synthetic_positions",
    # Lowering a nested Go function body (lambda, closure): its
    # ``_current_return_type_key`` may be the enclosing function's.
    "_in_nested_callable",
    # Go type a ``yield`` value is shaped as (``-> generator[list[int]]``).
    "_generator_elem_type",
    "_self_replacement",
    # Type-parameter substitution for the arguments of a method called on
    # a generic instance (``reg.add(x)`` with ``reg: Registry[Shape]``).
    "_call_type_subst",
    # Set while lowering a ``match`` guard: the variable a ``?`` stores its
    # propagated error in (see ``PatternMatchMixin._pm_condition``).
    "_guard_q_var",
    "_at_top_level",
    # Set while lowering module-level statements (the module init
    # function): their names are globals, which isinstance never narrows.
    "_nr_module_code",
    # Set while lowering an assignment target: a list element with a
    # non-trivial receiver and a runtime index is written through a pointer.
    "_index_target",
    # Lower bounds of the locals of the function being lowered
    # (``compiler/index_bounds.py``): an index provably non-negative skips
    # the negative-index adjustment.
    "_index_bounds",
    "current_class",
    "_in_try_func",
    "_in_try_iife",
    # Set while emitting the body of a ``catch`` clause (i.e. inside
    # the ``if r := recover(); r != nil { ... }`` block). A bare
    # ``raise`` / ``throw`` with no argument relies on this flag to
    # re-panic the live recover value ``r`` instead of emitting a
    # placeholder string, so the outer ``try`` sees the original
    # exception rather than a synthetic ``"re-raised"`` token.
    "_in_recover_block",
    # Set while emitting code whose enclosing return target can
    # accept a propagated ``*Result`` (i.e. the function declares
    # ``-> Result`` or we're inside a ``do { } catch`` IIFE). Used
    # by the ``?`` lowering to decide whether to emit a plain
    # ``return __qN`` (propagate) or ``panic(__qN)`` (surface the
    # error as an exception) when the ``Err`` branch fires.
    "_q_propagate_ok",
    "_in_except_handler",
    "_in_async_func",
    "_async_chan_name",
    # ``(flag, value_var)`` while emitting a ``do { }`` body: ``return``
    # records the value and leaves the ``*Result`` IIFE (see
    # ``_visit_do_stmt``). ``None`` elsewhere.
    "_do_return_target",
    # Set directly inside a ``do { }`` body (the ``*Result`` IIFE): code
    # leaving it early returns ``nil``. Reset in nested callables.
    "_in_do_body",
    # Set while the class visitor emits the members of a class body: a
    # ``funcdef`` there is a method; one inside a method body is a nested
    # function (a closure).
    "_class_member_level",
    # Named ``*Result`` of the ``do { }`` IIFE a ``?`` inside a nested
    # ``try`` IIFE stores its error in (``""`` when none).
    "_do_q_target",
    # Zero result a ``?`` inside a comprehension closure returns beside its
    # error (``""`` outside such a closure).
    "_comp_q_return",
    "_ternary_go_type",
    "_loop_break_flag",
    "_in_generator",
    "_generator_chan",
    "_propagate_cast_hint",
    "_expected_expr_go_type",
    "_expected_expr_type_key",
    "_generic_names",
    "_nested_generic_functions",
    # The Go return type of the *currently-being-emitted* Lam function.
    # Used to rewrite bare ``return`` statements found inside ``go!``
    # blocks of typed functions to ``return <zero-value>`` so the
    # author doesn't have to know the Go signature constraints.
    "_current_return_type",
    "_current_return_type_key",
)


class _BindingTable(dict):
    """Per-name type table (``_var_go_types`` / ``_var_types``). Bindings are
    recorded under their Go spelling by some emitters and read under their
    Lam spelling by others; for a name the Go side renames (``strings`` ->
    ``strings__lam``, ``type`` -> ``type__lam``) a write stores both
    spellings so either lookup finds it."""

    __slots__ = ()

    @staticmethod
    def _twin(key):
        if isinstance(key, str):
            if key.endswith("__lam"):
                base = key[:-5]
                if base in _BINDING_RENAMED:
                    return base
            elif key in _BINDING_RENAMED:
                return key + "__lam"
        return None

    def __setitem__(self, key, value):
        dict.__setitem__(self, key, value)
        twin = self._twin(key)
        if twin is not None:
            dict.__setitem__(self, twin, value)

    def pop(self, key, *default):
        twin = self._twin(key)
        if twin is not None:
            dict.pop(self, twin, None)
        return dict.pop(self, key, *default)

    def __delitem__(self, key):
        twin = self._twin(key)
        if twin is not None:
            dict.pop(self, twin, None)
        dict.__delitem__(self, key)


def _binding_table_property(attr: str):
    store = "_bt_" + attr

    def getter(self):
        return self.__dict__[store]

    def setter(self, value):
        self.__dict__[store] = value if isinstance(value, _BindingTable) else _BindingTable(value)

    return property(getter, setter)


class GoTranspiler(
    HelpersMixin,
    PatternMatchMixin,
    SubtypingMixin,
    GenericCallMixin,
    NarrowingLowerMixin,
    StatementVisitorMixin,
    ExpressionVisitorMixin,
    DefinitionVisitorMixin,
):
    """Walk a Lark tree and produce Go source code."""

    # Exceptions of async tasks that nobody awaited are printed to stderr
    # when ``main`` returns (like Python's "Task exception was never
    # retrieved"); an ``await`` that re-raises the exception retrieves it.
    _ASYNC_RUNTIME_GO = """
var lamAsyncFailures sync.Map

func lamAsyncTaskFailed(r interface{}) *int {
\ttoken := new(int)
\tlamAsyncFailures.Store(token, r)
\treturn token
}

func lamAsyncTaskRetrieved(token *int) {
\tlamAsyncFailures.Delete(token)
}

func lamAsyncReportUnretrieved() {
\tlamAsyncFailures.Range(func(_, r interface{}) bool {
\t\tfmt.Fprintf(os.Stderr, "Task exception was never retrieved: %v\\n", r)
\t\treturn true
\t})
}
"""

    _var_go_types = _binding_table_property("_var_go_types")
    _var_types = _binding_table_property("_var_types")

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
        # Class-body field initializers: ``class -> [(field, go_type, value)]``.
        self.class_field_defaults: Dict[str, List[Tuple[str, str, Any]]] = {}
        self.class_static_fields: Dict[str, List[Tuple[str, str, Any, bool]]] = {}
        self.class_bases: Dict[str, List[str]] = {}
        # ``class IntBox(Box[int])``: class -> {generic base -> type-arg nodes}.
        self._class_base_type_nodes: Dict[str, Dict[str, List[Any]]] = {}
        self.class_base_aliases: Dict[str, Dict[str, str]] = {}
        # Virtual dispatch (see ``_virtual_method_plan``): instance-method
        # funcdefs per local class, the names each class calls on ``self``,
        # and the memoised per-class plan.
        self._local_class_methods: Dict[str, Dict[str, List[Tree]]] = {}
        self._class_self_calls: Dict[str, Set[str]] = {}
        self._virtual_plans: Dict[str, List[Tuple[str, str, List[Tree]]]] = {}
        self._class_virtual_wrappers: Dict[str, Set[str]] = {}
        # Subtyping (``compiler/visitors/subtyping.py``): per-class method
        # dispatch signatures and classes declaring their own ``init``,
        # collected per module, plus the program-wide hierarchy injected
        # by the driver (every module's classes, so a library knows which
        # of its classes the program subclasses).
        self._class_method_sigkeys: Dict[str, Dict[str, Optional[str]]] = {}
        self._class_own_init: Set[str] = set()
        self._program_class_bases: Dict[str, List[str]] = {}
        self._program_method_sigkeys: Dict[str, Dict[str, Optional[str]]] = {}
        self._program_own_init: Set[str] = set()
        self._class_type_param_names: Dict[str, List[str]] = {}
        self._local_class_init_defs: Dict[str, Tree] = {}
        self._program_class_type_params: Dict[str, List[str]] = {}
        self._hier_cache: Dict[str, Any] = {}

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
        # ``_in_recover_block`` is a narrower flag that's only true
        # inside the recover-guard of a catch clause — bare raise
        # uses it to re-panic the live ``r`` value.
        self._in_recover_block: bool = False
        # ``_q_propagate_ok`` is True iff a ``?``-propagated
        # ``return __q{N}`` would compile — i.e. we're inside a
        # ``-> Result`` function or a ``do { } catch`` IIFE. When
        # it's False, ``?`` falls back to ``panic`` so an errant
        # propagation surfaces as an exception instead of a Go
        # "too many return values" build failure.
        self._q_propagate_ok: bool = False
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
        self._generator_elem_type: str = ""
        self._call_type_subst: Dict[str, str] = {}
        self._guard_q_var: str = ""

        # Async function state
        self._in_async_func: bool = False
        self._async_chan_name: Optional[str] = None
        self._do_return_target: Optional[Tuple[str, str]] = None
        self._in_do_body: bool = False
        # Set by the driver on the main module's transpiler when some module
        # of the program has an async callable: the file defines the task
        # failure registry and ``main`` reports unretrieved exceptions.
        self._emit_async_runtime: bool = False
        self._class_member_level: bool = False
        self._do_q_target: str = ""
        self._comp_q_return: str = ""

        # Ternary context type
        self._ternary_go_type: Optional[str] = None

        # While-else / for-else break flag
        # Name of the flag the innermost enclosing loop uses to record a
        # ``break`` (for its ``else`` clause); ``""`` when that loop has no
        # ``else``. Every loop body rebinds it, so an inner loop never
        # writes an outer loop's flag.
        self._loop_break_flag: str = ""

        # Go blocks (preprocessed raw Go code, keyed by id)
        self.go_blocks: Dict[str, str] = go_blocks or {}

        # Stdlib path
        self.stdlib_path = stdlib_path

        # Go module
        self.go_module_name = go_module_name

        # Global-level variable declarations
        self._at_top_level: bool = True
        self._nr_module_code: bool = False
        self._index_target: bool = False
        self._index_bounds: Dict[str, float] = {}
        self._nonneg_shadowed: Set[str] = set()
        self._nr_module_vars: Set[str] = set()

        # Track all user-defined functions
        self._user_functions: Set[str] = set()
        self._private_functions: Set[str] = set()
        self._function_go_names: Dict[str, str] = {}
        # Module namespacing (``compiler/namespacing.py``): Go names chosen by
        # the driver for this module's colliding/aliased functions and consts,
        # plus the AST rewrites for ``as`` aliases and ``module.member``.
        self._link_function_go_names: Dict[str, str] = {}
        self._link_function_aliases: Dict[str, str] = {}
        self._link_const_go_names: Dict[str, str] = {}
        self._namespace_rewrites: Dict[str, str] = {}
        self._namespace_module_access: Dict[str, Dict[str, str]] = {}
        self._namespace_declarations: Dict[str, str] = {}
        self._private_methods: Dict[str, Set[str]] = {}

        # Functions imported from libraries — survive the per-transpile
        # reset so library-imported names still drive the call-site
        # dispatcher (default-arg filling, Go-public-name rewriting).
        self._external_user_functions: Set[str] = set()
        self._external_private_functions: Set[str] = set()
        self._external_instance_methods: Dict[str, Set[str]] = {}
        self._external_field_names: Dict[str, Set[str]] = {}
        self._external_field_types: Dict[str, Dict[str, str]] = {}

        # Top-level go! raw lines
        self._raw_go_top: List[str] = []

        # Operator overloading
        self._class_dunder_methods: Dict[str, Dict[str, str]] = {}

        # Lam library modules this file imports. Populated by the
        # statement visitors as ``from X import ...`` / ``import X``
        # are seen, then consumed by the driver in
        # ``lammergeier.py`` to resolve the transitive library
        # graph before the main transpile pass runs.
        self._lam_imports: List[str] = []

        # Alias map populated from ``from pkg import Foo as Bar``
        # and ``import pkg as p`` lines by
        # :meth:`_collect_import_aliases`. Keys are the local
        # binding (``Bar``); values are the name the library
        # actually exports (``Foo``). The import-alias pre-pass
        # rewrites every referenced identifier through this map
        # so the rest of the pipeline never has to know an alias
        # was involved.
        self._import_aliases: Dict[str, str] = {}

        # Generator state
        self._in_generator = False
        self._generator_chan = None

        # Known interface / class names
        self._interfaces: set = set()
        self._class_names: set = set()
        self._class_go_names: Dict[str, str] = {}
        self._instance_methods: Dict[str, Set[str]] = {}
        self._method_go_names: Dict[Tuple[str, str], str] = {}
        self._field_go_names: Dict[Tuple[str, str], str] = {}

        # Static methods per class
        self._static_methods: Dict[str, Set[str]] = {}
        # Static variables per class. Values are ``var_name -> is_private``.
        self._static_vars: Dict[str, Dict[str, bool]] = {}
        self._static_var_go_names: Set[str] = set()

        # Known generator call keys: top-level names and Class.method keys.
        self._generator_functions: set = set()

        # Variable type tracking
        self._var_types: Dict[str, str] = {}

        # Source-line mapping
        self._source_file = source_file
        self._last_line_directive = 0
        # Lam errors found while lowering (``_report_lam_error``), raised as
        # one ``TranspileDiagnosticError`` once the module is walked.
        self._lam_errors: List[Tuple[int, int, str, str]] = []
        # Set while lowering an f-string slot re-parsed on its own, whose
        # node positions are not positions in the module.
        self._synthetic_positions = False
        self._in_nested_callable = False

        # Default parameter values
        self._func_defaults: Dict[str, List[Tuple[int, Any]]] = {}
        self._func_param_counts: Dict[str, int] = {}

        # Per-function parameter names (in declaration order).
        # Used by the call-site dispatcher to reorder keyword arguments
        # (``f(name="alice", times=3)``) into the positional shape that
        # the emitted Go function actually expects.
        self._func_param_names: Dict[str, List[str]] = {}
        # Per-function Go parameter types, in the same order as
        # ``_func_param_names``. Call sites use this as contextual type
        # information for anonymous collection literals.
        self._func_param_go_types: Dict[str, List[str]] = {}
        # Generic type-parameter names per callable. Call-site contextual
        # lowering uses these to replace list[T] with a literal's inferred
        # concrete slice type rather than leaking T into caller Go.
        self._func_generic_params: Dict[str, Set[str]] = {}
        # ``Class.method`` -> the method's own type parameters, for instance
        # methods declaring them (``func map[U](self, ...)``). Go methods
        # cannot have type parameters, so these lower to package-level
        # generic functions ``Class_Method[T, U](s, ...)``.
        self._generic_instance_methods: Dict[str, List[str]] = {}
        # ``Class.method`` -> Go return type spelled with the class's and the
        # method's type parameters (``*Iter[U]``, ``[]T``), for methods of
        # generic classes and generic methods: call sites substitute them.
        self._generic_method_returns: Dict[str, str] = {}

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
        # Authoritative list of variants for same-arity type-based
        # overload dispatch. Each entry is
        # ``{"arity": int, "sig": tuple[str, ...], "return": str, "suffix": str}``,
        # one per definition in source order. The ``suffix`` field
        # is filled in by ``_finalize_overload_variants`` once the
        # pre-scan has seen every definition so the def site and
        # the call site produce the same mangled Go name.
        self._overload_variants: Dict[str, List[Dict[str, object]]] = {}

        # Variadic functions
        self._variadic_functions: set = set()
        self._var_go_types: Dict[str, str] = {}
        # Payload Go type of locals/parameters annotated ``Result[T]``, so
        # inline ``r.unwrap()`` can assert ``T`` instead of yielding ``any``.
        self._var_result_payloads: Dict[str, str] = {}
        self._tuple_iterable_element_types: Dict[str, List[str]] = {}
        # Lam types of annotated locals/parameters (``typesys`` objects);
        # ``match`` lowering uses them for tuple element types.
        self._var_lam_types: Dict[str, Any] = {}
        # Name of the Go function running this module's top-level
        # statements (``_emit_top_level``); the driver assigns a unique
        # name per module and calls it from ``func init()``.
        self._module_init_name: str = "lamModuleInit_main"
        self._has_module_init: bool = False

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
        self._generic_constraints: Dict[str, str] = {}
        self._nested_generic_functions: Dict[str, Dict[str, Any]] = {}
        self._hoisted_nested_generic_funcs: List[str] = []
        self._nested_generic_counter: int = 0

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
        self._current_return_type_key: str = ""
        self._expected_expr_go_type: str = ""
        self._expected_expr_type_key: str = ""
        self._func_return_go_types: Dict[str, str] = {}
        # Top-level ``async func`` names: their calls yield handles.
        self._async_functions: Set[str] = set()
        # Generator functions declared ``-> generator[T]``: Go type of T.
        self._generator_elem_go_types: Dict[str, str] = {}
        self._result_payload_go_types: Dict[str, str] = {}

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
            if node.data not in ("funcdef", "classdef"):
                # ``f()?.method()`` on a ``Result``: ``(f()?).method()``.
                rewrite_result_safe_nav(node, lambda recv: self._expr_go_type_hint(recv) == "*Result")
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
        self._normalize_match_patterns(tree)
        if self._namespace_rewrites or self._namespace_module_access or self._namespace_declarations:
            from compiler.namespacing import rewrite_tree
            rewrite_tree(
                tree, self._namespace_rewrites, self._namespace_module_access,
                self._namespace_declarations,
            )
        self.output_lines = []
        self.needed_imports = set()
        self.user_go_imports = set()
        self.current_class = None
        self.class_fields = {}
        self.class_field_defaults = {}
        self.class_static_fields = {}
        self.class_bases = {}
        self._class_base_type_nodes = {}
        self.class_base_aliases = {}
        self._local_class_methods = {}
        self._class_self_calls = {}
        self._virtual_plans = {}
        self._class_virtual_wrappers = {}
        self._class_method_sigkeys = {}
        self._class_own_init = set()
        self._class_type_param_names = {}
        self._local_class_init_defs = {}
        self._hier_cache = {}
        self._class_go_names = {}
        self._instance_methods = {}
        self._method_go_names = {}
        self._field_go_names = {}
        self._tuple_iterable_element_types = {}
        self._var_lam_types = {}
        self._has_module_init = False
        self.declared_vars = set()
        self.scope_stack = []
        self._user_functions = set()
        self._private_functions = set()
        self._function_go_names = {}
        self._private_methods = {}
        self._raw_go_top = []
        self._generic_constraints = {}
        self._nested_generic_functions = {}
        self._hoisted_nested_generic_funcs = []
        self._nested_generic_counter = 0
        self._static_var_go_names = {
            self._static_var_go_name(cls, name)
            for cls, fields in self._static_vars.items()
            for name in fields
        }

        # Pass 0: collect user-defined function names
        self._collect_declared_type_names(tree)
        self._finalize_class_go_names()
        self._collect_function_names(tree)
        # Now that every definition has been registered, walk the
        # variant lists once to decide each variant's mangled
        # suffix. Doing this after the recursive collect lets a
        # forward call in an earlier function see the correct
        # mangling for a later definition (source order doesn't
        # constrain call order in Go).
        self._finalize_overload_variants()
        # Functions raw ``go!`` code of this module may call by their Lam
        # spelling (``_resolve_raw_lam_function_names``).
        self._module_function_names = set(self._user_functions)
        self._unaliased_imported_names = self._collect_unaliased_import_names(tree)
        self._raw_go_tree = tree
        # Library-imported functions are also "user functions" from the
        # call-site dispatcher's POV — without this, calls to library
        # functions would bypass default-arg filling.
        self._user_functions.update(self._external_user_functions)
        self._private_functions.update(self._external_private_functions)
        for class_name, methods in self._external_instance_methods.items():
            self._instance_methods.setdefault(class_name, set()).update(methods)
        self._resolve_local_inherited_inits()
        self._finalize_function_go_names()
        self._static_var_go_names = {
            self._static_var_go_name(cls, name)
            for cls, fields in self._static_vars.items()
            for name in fields
        }

        # Pass 1: collect class field info
        self._collect_class_fields(tree)
        self._finalize_member_go_names()

        # Locals that never need the negative-index adjustment.
        self._nonneg_shadowed = self._user_functions & {"len", "range", "enumerate", "abs", "min", "max"}
        self._nr_nonlocal_names = mark_nonnegative_locals(tree, self._nonneg_shadowed)

        # Pass 2: generate code
        body_lines: List[str] = []
        old_lines = self.output_lines
        self.output_lines = body_lines
        self._at_top_level = True

        self._emit_top_level(tree)
        if self._emit_async_runtime:
            body_lines.extend(self._ASYNC_RUNTIME_GO.splitlines())
            for pkg in ("fmt", "os", "sync"):
                self._need_import(pkg)

        self.output_lines = old_lines
        if self._lam_errors:
            from compiler.diagnostics import TranspileDiagnosticError
            raise TranspileDiagnosticError(sorted(self._lam_errors), self._source_file)

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

        if self._hoisted_nested_generic_funcs:
            body_lines = self._hoisted_nested_generic_funcs + [""] + body_lines

        return "\n".join(header_lines + body_lines) + "\n"

    # ─── Top-level statements ──────────────────────────────────
    #
    # Declarations (functions, classes, interfaces, imports, ``const``,
    # globals, ``go!`` blocks) become Go package-level declarations as
    # before. Every other top-level statement (``print(x)``,
    # ``items.append(3)``, assignments to existing globals, loops, ...)
    # runs at program start: the module gets ``func <initName>() { ... }``
    # holding them in file order, and the driver calls every module's init
    # function from one ``func init()`` in import (dependency) order.
    #
    # A global declared *before* the module's first top-level statement
    # keeps its Go package-level initializer (evaluated by Go before any
    # init function runs). A global declared *after* one is declared at
    # package level with its zero value and assigned in file order inside
    # the init function, so it sees the effects of earlier statements.

    _TOPLEVEL_DECL_NODES = frozenset({
        "funcdef", "classdef", "interfacedef", "decorated", "import_stmt",
        "import_name", "import_from", "const_stmt", "go_block", "pass_stmt",
        "global_stmt", "nonlocal_stmt",
    })

    @staticmethod
    def _flatten_top_level(tree: Tree) -> List[Tree]:
        out: List[Tree] = []
        for child in tree.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "simple_stmt":
                out.extend(c for c in child.children if isinstance(c, Tree))
            else:
                out.append(child)
        return out

    def _top_level_global_target(self, stmt: Tree):
        """``(name, type_node, value_node, annotated)`` when ``stmt``
        declares a module global (``x: T = v`` or a first ``x = v``)."""
        node = stmt.children[0] if stmt.data == "assign_stmt" and stmt.children else stmt
        if not isinstance(node, Tree):
            return None
        if node.data == "annassign":
            _private, is_static, children = self._annassign_parts(node)
            target = children[0] if children else None
            if is_static or not (isinstance(target, Tree) and target.data == "var"):
                return None
            return (self._get_name(target.children[0]), children[1] if len(children) > 1 else None,
                    children[2] if len(children) > 2 else None, True)
        if node.data == "assign" and len(node.children) == 2:
            target, value = node.children
            if isinstance(target, Tree) and target.data == "var":
                name = self._get_name(target.children[0])
                if name not in self._top_level_globals and name != "_":
                    return (name, None, value, False)
        return None

    def _is_go_block_marker(self, stmt: Tree) -> bool:
        """``go! { ... }`` at top level (the ``__go_block__("id")`` marker)."""
        if stmt.data != "expr_stmt" or not stmt.children:
            return False
        call = stmt.children[0]
        return (
            isinstance(call, Tree) and call.data == "funccall" and call.children
            and isinstance(call.children[0], Tree) and call.children[0].data == "var"
            and self._get_name(call.children[0].children[0]) == "__go_block__"
        )

    @staticmethod
    def _is_inert_top_level(stmt: Tree) -> bool:
        """A bare string literal (module docstring) does nothing."""
        if stmt.data != "expr_stmt" or len(stmt.children) != 1:
            return False
        inner = stmt.children[0]
        return isinstance(inner, Tree) and inner.data in {"string", "string_concat"}

    def _emit_top_level(self, tree: Tree) -> None:
        self._top_level_globals: Set[str] = set()
        self._nr_module_vars = _narrowing.module_variable_names(tree) | getattr(self, "_nr_nonlocal_names", set())
        init_lines: List[str] = []
        init_declared: Set[str] = set()
        seen_statement = False
        for stmt in self._flatten_top_level(tree):
            if stmt.data in self._TOPLEVEL_DECL_NODES or self._is_go_block_marker(stmt):
                self._visit(stmt)
                continue
            if self._is_inert_top_level(stmt):
                continue
            target = self._top_level_global_target(stmt)
            if target is not None:
                name, type_node, value_node, annotated = target
                if not seen_statement:
                    if annotated:
                        self._visit(stmt)
                    else:
                        self._emit_inferred_global(name, value_node)
                    self._top_level_globals.add(name)
                    continue
                self._declare_deferred_global(name, type_node, value_node)
                self._top_level_globals.add(name)
            else:
                seen_statement = True
                self._declare_flow_globals(stmt)
            self._emit_into_module_init(stmt, init_lines, init_declared)
        if init_lines:
            self._emit(f"func {self._module_init_name}() {{")
            self.output_lines.extend(init_lines)
            self._emit("}")
            self._emit("")
            self._has_module_init = True

    def _global_go_name(self, name: str) -> str:
        if name in self._link_const_go_names:
            return self._link_const_go_names[name]
        return self._go_binding_name(name)

    def _inferred_global_go_type(self, value_node) -> str:
        go_type = self._expr_go_type_hint(value_node) or self._infer_type_from_value(value_node)
        return go_type or "interface{}"

    def _register_global_type(self, go_name: str, go_type: str, type_node=None) -> None:
        if go_type:
            self._var_go_types[go_name] = go_type
        if type_node is not None:
            self._record_lam_type(go_name, type_node)
            raw = self._get_raw_type_name(type_node)
            if raw in self._class_names:
                self._var_types[go_name] = raw
        elif go_type.startswith("*"):
            class_name = go_type[1:].split("[", 1)[0]
            if class_name in self._class_names:
                self._var_types[go_name] = class_name

    def _emit_inferred_global(self, name: str, value_node) -> None:
        """``count = 0`` at top level (before any statement): a global."""
        go_name = self._global_go_name(name)
        go_type = self._inferred_global_go_type(value_node)
        value = self._typed_value_to_go(value_node, go_type) if go_type != "interface{}" else self._expr_to_go(value_node)
        self._emit(f"var {go_name} {go_type} = {value}")
        self._declare_var(go_name)
        self._register_global_type(go_name, go_type)

    def _declare_deferred_global(self, name: str, type_node, value_node) -> None:
        """A global declared after a top-level statement: zero-valued here,
        assigned in file order by the module init function."""
        go_name = self._global_go_name(name)
        if go_name in self.declared_vars:
            return
        go_type = self._type_expr_to_go(type_node) if type_node is not None else self._inferred_global_go_type(value_node)
        self._emit(f"var {go_name} {go_type or 'interface{}'}")
        self._declare_var(go_name)
        self._register_global_type(go_name, go_type, type_node)

    def _declare_flow_globals(self, stmt: Tree) -> None:
        """Names first assigned inside a top-level ``if`` / ``for`` / ...
        are module globals (as in Python), not locals of the init function."""
        if stmt.data == "assign_stmt":
            self._declare_destructured_globals(stmt)
            return
        if stmt.data in {"expr_stmt", "augassign"}:
            return
        for name, go_type in self._flow_assignment_types(stmt).items():
            if name in self._top_level_globals:
                continue
            self._top_level_globals.add(name)
            go_name = self._global_go_name(name)
            if go_name in self.declared_vars:
                continue
            self._emit(f"var {go_name} {go_type or 'interface{}'}")
            self._declare_var(go_name)
            self._register_global_type(go_name, go_type)

    def _declare_destructured_globals(self, stmt: Tree) -> None:
        """``x, y = 1, 2`` / ``a, b = pair()`` at top level: new names are
        module globals typed from the tuple literal or the call's results."""
        node = stmt.children[0] if stmt.children else None
        if not (isinstance(node, Tree) and node.data == "assign" and len(node.children) == 2):
            return
        target, value = node.children
        if not (isinstance(target, Tree) and target.data == "tuple"):
            return
        names = [
            self._get_name(c.children[0]) if isinstance(c, Tree) and c.data == "var" else ""
            for c in target.children
        ]
        types: List[str] = []
        if isinstance(value, Tree) and value.data == "tuple":
            types = [self._inferred_global_go_type(c) for c in value.children if isinstance(c, Tree)]
        elif isinstance(value, Tree) and value.data == "funccall":
            callee = value.children[0]
            if isinstance(callee, Tree) and callee.data == "var":
                ret = self._func_return_go_types.get(self._get_name(callee.children[0]), "")
                if ret.startswith("(") and ret.endswith(")"):
                    types = [t.strip() for t in ret[1:-1].split(",")]
        for index, name in enumerate(names):
            if not name or name == "_" or name in self._top_level_globals:
                continue
            self._top_level_globals.add(name)
            go_name = self._global_go_name(name)
            if go_name in self.declared_vars:
                continue
            go_type = types[index] if index < len(types) and types[index] else "interface{}"
            self._emit(f"var {go_name} {go_type}")
            self._declare_var(go_name)
            self._register_global_type(go_name, go_type)

    def _emit_into_module_init(self, stmt: Tree, init_lines: List[str], init_declared: Set[str]) -> None:
        saved_lines = self.output_lines
        saved_indent = self.indent
        saved_declared = set(self.declared_vars)
        self.output_lines = init_lines
        self.indent = 1
        self.declared_vars = saved_declared | init_declared
        try:
            with self._scoped(_at_top_level=False, _current_return_type="", _current_return_type_key="",
                              _nr_module_code=True):
                self._visit(stmt)
            init_declared.update(self.declared_vars - saved_declared)
        finally:
            self.output_lines = saved_lines
            self.indent = saved_indent
            self.declared_vars = saved_declared

    # ─── Class base helpers ────────────────────────────────────

    def _classdef_base_specs(self, node: Tree) -> List[Tuple[str, str]]:
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "class_bases":
                specs: List[Tuple[str, str]] = []
                for base_node in child.children:
                    if not isinstance(base_node, Tree):
                        continue
                    if base_node.data == "named_class_base":
                        alias = self._class_base_name(base_node.children[0]) if base_node.children else ""
                        base = self._class_base_name(base_node.children[1]) if len(base_node.children) > 1 else ""
                        if base:
                            specs.append((alias, base))
                    elif base_node.data == "unnamed_class_base":
                        base = self._class_base_name(base_node)
                        if base:
                            specs.append(("", base))
                return specs
            if child.data == "arguments":
                specs = []
                for arg in child.children:
                    base = self._class_base_name(arg)
                    if base:
                        specs.append(("", base))
                return specs
        return []

    def _classdef_base_type_args(self, node: Tree) -> Dict[str, List[Any]]:
        """``class IntBox(Box[int])``: base name -> type-argument nodes."""
        out: Dict[str, List[Any]] = {}
        for child in node.children:
            if not isinstance(child, Tree) or child.data != "class_bases":
                continue
            for base_node in child.children:
                if not isinstance(base_node, Tree):
                    continue
                target = base_node.children[-1] if base_node.data == "named_class_base" and base_node.children else base_node
                generic = next(iter(target.find_data("type_generic")), None) if isinstance(target, Tree) else None
                if generic is None or not generic.children:
                    continue
                base = self._dotted_name_to_str(generic.children[0])
                args = [c for c in generic.children[1:] if c is not None]
                if base and args:
                    out[base] = args
        return out

    def _base_type_args(self, class_name: str, base: str) -> str:
        """Go type-argument list (``[int]``) of a generic base, or ``""``."""
        args = getattr(self, "_class_base_type_nodes", {}).get(class_name, {}).get(base)
        if not args:
            return ""
        return "[" + ", ".join(self._type_expr_to_go(a) or "any" for a in args) + "]"

    def _class_base_name(self, node) -> str:
        if not isinstance(node, Tree):
            return ""
        if node.data == "var" and node.children:
            return self._get_name(node.children[0])
        if node.data == "name" and node.children:
            return self._get_name(node)
        if node.data == "dotted_name":
            return ".".join(
                self._get_name(child)
                for child in node.children
                if isinstance(child, Tree)
            )
        if node.data in {"getattr", "getattr_safe"} and len(node.children) >= 2:
            prefix = self._class_base_name(node.children[0])
            attr = self._get_name(node.children[1])
            return f"{prefix}.{attr}" if prefix and attr else ""
        for child in node.children:
            name = self._class_base_name(child)
            if name:
                return name
        return ""

    @staticmethod
    def _base_aliases_from_specs(specs: List[Tuple[str, str]]) -> Dict[str, str]:
        aliases: Dict[str, str] = {}
        if len(specs) == 1 and not specs[0][0] and specs[0][1]:
            aliases["base"] = specs[0][1]
        for alias, base in specs:
            if alias and base:
                aliases[alias] = base
        return aliases

    # ─── Pass 0: Collect function names ────────────────────────

    def _collect_declared_type_names(self, tree: Tree) -> None:
        if not isinstance(tree, Tree):
            return
        if tree.data == "classdef" and tree.children:
            class_name = self._get_name(tree.children[0])
            self._class_names.add(class_name)
            tp_node = next(
                (c for c in tree.children if isinstance(c, Tree) and c.data == "type_params"),
                None,
            )
            if tp_node is not None:
                # Known before any signature is collected, so a parameter
                # typed ``Box[int]`` lowers to ``*Box[int]`` (not
                # ``interface{}``) in the call metadata too.
                tp_clause = self._type_params_to_go(tp_node)[0]
                if tp_clause:
                    self._generic_classes.setdefault(class_name, tp_clause)
            return
        if tree.data == "interfacedef" and tree.children:
            self._interfaces.add(self._get_name(tree.children[0]))
            return
        if tree.data == "funcdef":
            return
        for child in tree.children:
            if isinstance(child, Tree):
                self._collect_declared_type_names(child)

    def _collect_self_method_calls(self, node, out: set) -> None:
        """Add every ``name`` called as ``self.name(...)`` below ``node``."""
        if not isinstance(node, Tree):
            return
        if node.data == "fstring" and node.children:
            # Interpolations are parsed at emission time; scan the raw text.
            out.update(re.findall(r"\bself\.(\w+)\s*\(", str(node.children[0])))
            return
        if node.data == "funccall" and node.children:
            callee = node.children[0]
            if (
                isinstance(callee, Tree)
                and callee.data == "getattr"
                and len(callee.children) >= 2
                and isinstance(callee.children[0], Tree)
                and callee.children[0].data == "var"
                and self._get_name(callee.children[0].children[0]) == "self"
            ):
                out.add(self._get_name(callee.children[1]))
        for child in node.children:
            if isinstance(child, Tree):
                self._collect_self_method_calls(child, out)

    def _generator_elem_go_type(self, return_type_node) -> str:
        """Go type of ``T`` in a ``-> generator[T]`` (or ``iterator[T]``)
        return annotation; ``""`` when absent or dynamic."""
        node = return_type_node
        while isinstance(node, Tree) and node.data in {"single_return_type", "type_expr", "type_union"} and len(node.children) == 1:
            node = node.children[0]
        if not (isinstance(node, Tree) and node.data == "type_generic" and node.children):
            return ""
        base = self._dotted_name_to_str(node.children[0])
        args = [child for child in node.children[1:] if isinstance(child, Tree)]
        if base not in {"generator", "iterator", "Generator", "Iterator"} or len(args) != 1:
            return ""
        elem = self._type_expr_to_go(args[0])
        return "" if not elem or elem == "interface{}" else elem

    def _collect_unaliased_import_names(self, tree: Tree) -> Set[str]:
        """Names bound by ``from m import name`` (without ``as``)."""
        names: Set[str] = set()
        for node in tree.iter_subtrees():
            if node.data != "import_as_name":
                continue
            parts = [self._get_name(child) for child in node.children if isinstance(child, Tree)]
            parts = [part for part in parts if part]
            if len(parts) == 1:
                names.add(parts[0])
        return names

    def _collect_function_names(self, tree: Tree):
        if not isinstance(tree, Tree):
            return
        if tree.data == "funcdef":
            (_, _, is_async, name_node, params_node, return_type_node,
             suite_node, type_params_node) = self._parse_funcdef(tree)
            name = self._get_name(name_node)
            self._user_functions.add(name)
            if is_async:
                self._async_functions.add(name)
            for child in tree.children:
                if isinstance(child, Token) and str(child) == "private":
                    self._private_functions.add(name)
                    break
            if self._tree_contains(suite_node, "yield_expr"):
                self._generator_functions.add(name)
                elem = self._generator_elem_go_type(return_type_node)
                if elem:
                    self._generator_elem_go_types[name] = elem

            _, type_param_names, _ = self._type_params_to_go(type_params_node)
            if type_param_names:
                self._func_generic_params[name] = set(type_param_names)
            generic_names = self._generic_names | set(type_param_names)
            with self._scoped(_generic_names=generic_names):
                variadic = self._has_variadic(params_node) if params_node else False
                if variadic:
                    self._variadic_functions.add(name)
                return_type = self._resolve_return_type(return_type_node)
                return_key = self._overload_return_key(return_type_node)
                if return_type:
                    self._func_return_go_types.setdefault(name, return_type)
                payload_type = self._result_payload_go_type(return_type_node)
                if payload_type:
                    self._result_payload_go_types.setdefault(name, payload_type)
                # Count params and collect defaults.
                arity = self._count_params(params_node)
                self._func_param_counts[name] = arity
                if params_node:
                    self._collect_param_defaults(name, params_node)
                # Track overloading (only for non-variadic). We keep
                # the legacy arity-indexed set alongside the richer
                # variants list because several call sites still check
                # the set to decide whether *any* overload mangling is
                # needed at all.
                if not variadic:
                    if name not in self._overloaded_functions:
                        self._overloaded_functions[name] = set()
                    self._overloaded_functions[name].add(arity)
                    sig = self._param_types_sig(params_node) if params_node else ()
                    meta_key = f"{name}#{self._sig_suffix(sig)}#ret_{self._sig_suffix((return_key or 'void',))}"
                    if payload_type:
                        self._result_payload_go_types.setdefault(meta_key, payload_type)
                    if type_param_names:
                        self._func_generic_params[meta_key] = set(type_param_names)
                    if params_node:
                        self._collect_param_defaults(meta_key, params_node)
                    required = arity - len(self._func_defaults.get(meta_key, []))
                    self._overload_variants.setdefault(name, []).append({
                        "arity": arity,
                        "required": required,
                        "sig": sig,
                        "return": return_type,
                        "return_key": return_key,
                        "suffix": "",
                        "meta_key": meta_key,
                    })

        elif tree.data == "classdef":
            name_node = tree.children[0]
            class_name = self._get_name(name_node)
            self._class_names.add(class_name)
            base_specs = self._classdef_base_specs(tree)
            self.class_bases[class_name] = [base for _alias, base in base_specs]
            self._class_base_type_nodes[class_name] = self._classdef_base_type_args(tree)
            self.class_base_aliases[class_name] = self._base_aliases_from_specs(base_specs)
            class_type_params = next(
                (child for child in tree.children
                 if isinstance(child, Tree) and child.data == "type_params"),
                None,
            )
            _, class_type_param_names, _ = self._type_params_to_go(class_type_params)
            self._class_type_param_names[class_name] = list(class_type_param_names)
            # Scan methods
            suite = tree.children[-1]
            self._local_class_methods.setdefault(class_name, {})
            self._class_self_calls.setdefault(class_name, set())
            for child in self._suite_stmts(suite):
                if isinstance(child, Tree) and child.data == "funcdef":
                    (is_priv, is_stat, _, fn_node, params_node,
                     return_type_node, method_suite,
                     method_type_params) = self._parse_funcdef(child)
                    fn_name = self._get_name(fn_node)
                    if not is_stat:
                        self._collect_self_method_calls(method_suite, self._class_self_calls[class_name])
                        if fn_name not in {"init", "__init__"}:
                            self._local_class_methods[class_name].setdefault(fn_name, []).append(child)
                    if is_stat:
                        self._static_methods.setdefault(class_name, set()).add(fn_name)
                    elif fn_name not in {"init", "__init__"}:
                        self._instance_methods.setdefault(class_name, set()).add(fn_name)
                    if fn_name in {"init", "__init__"} and not is_stat:
                        self._class_own_init.add(class_name)
                        self._local_class_init_defs[class_name] = child
                    elif not is_stat:
                        self._record_method_sigkey(
                            class_name, fn_name, child, class_type_param_names,
                        )
                    if is_priv:
                        self._private_methods.setdefault(class_name, set()).add(fn_name)
                    # Collect defaults for methods (keyed as class_name.method).
                    # Normalise __init__ -> init so constructor calls
                    # (which use the `Class.init` key) pick up defaults too.
                    key_name = "init" if fn_name == "__init__" else fn_name
                    method_key = f"{class_name}.{key_name}"
                    if is_stat and self._tree_contains(method_suite, "yield_expr"):
                        self._generator_functions.add(method_key)
                    _, method_type_param_names, _ = self._type_params_to_go(
                        method_type_params,
                    )
                    declared_generic_names = (
                        set(class_type_param_names) | set(method_type_param_names)
                    )
                    if declared_generic_names:
                        self._func_generic_params[method_key] = declared_generic_names
                    if method_type_param_names and not is_stat and fn_name not in {"init", "__init__"}:
                        self._generic_instance_methods[method_key] = list(method_type_param_names)
                    callable_generic_names = self._generic_names | declared_generic_names
                    with self._scoped(_generic_names=callable_generic_names):
                        return_type = self._resolve_return_type(return_type_node)
                        if declared_generic_names and return_type:
                            self._generic_method_returns[method_key] = return_type
                        return_key = self._overload_return_key(return_type_node)
                        payload_type = self._result_payload_go_type(return_type_node)
                        if payload_type:
                            self._result_payload_go_types.setdefault(method_key, payload_type)
                        skip_self = not is_stat
                        arity = self._count_params(params_node, skip_self=skip_self)
                        self._func_param_counts[method_key] = arity
                        if params_node:
                            self._collect_param_defaults(method_key, params_node, skip_self=skip_self)
                        variadic = self._has_variadic(params_node) if params_node else False
                        if not variadic and fn_name not in {"init", "__init__", "__str__", "__repr__", "__len__", "__json__"} and fn_name not in DUNDER_OPS:
                            self._overloaded_functions.setdefault(method_key, set()).add(arity)
                            sig = self._param_types_sig(params_node, skip_self=skip_self) if params_node else ()
                            meta_key = f"{method_key}#{self._sig_suffix(sig)}#ret_{self._sig_suffix((return_key or 'void',))}"
                            if payload_type:
                                self._result_payload_go_types.setdefault(meta_key, payload_type)
                            if declared_generic_names:
                                self._func_generic_params[meta_key] = declared_generic_names
                            if params_node:
                                self._collect_param_defaults(meta_key, params_node, skip_self=skip_self)
                            required = arity - len(self._func_defaults.get(meta_key, []))
                            self._overload_variants.setdefault(method_key, []).append({
                                "arity": arity,
                                "required": required,
                                "sig": sig,
                                "return": return_type,
                                "return_key": return_key,
                                "suffix": "",
                                "meta_key": meta_key,
                            })
                        # Track the *raw* Lam return-type name so
                        # ``_infer_receiver_class`` can thread the class
                        # forward across chained calls. Only single-return
                        # types referencing a known user class are useful
                        # here; ``list[...]``/``dict[...]``/``str``/etc.
                        # have no class identity and are skipped.
                        raw_return = self._get_raw_type_name(return_type_node) if return_type_node else ""
                        if raw_return:
                            self._method_return_types.setdefault(method_key, raw_return)
                        if (
                            return_type.startswith(("[]", "map["))
                            and not declared_generic_names
                            and len({
                                (tuple(v.get("sig", ())), v.get("return_key"))
                                for v in self._overload_variants.get(method_key, ())
                            }) <= 1
                        ):
                            # Container results (``Iter.toList() -> list[any]``)
                            # under a ``go:`` key, for expression type hints.
                            self._method_return_types.setdefault(f"go:{method_key}", return_type)
        elif tree.data == "interfacedef":
            name_node = tree.children[0]
            interface_name = self._get_name(name_node)
            self._interfaces.add(interface_name)
            for child in tree.children[1:]:
                if not isinstance(child, Tree) or child.data != "interface_method":
                    continue
                parts = [part for part in child.children if isinstance(part, Tree)]
                if not parts:
                    continue
                method_name = self._get_name(parts[0])
                params_node = next((part for part in parts[1:] if part.data == "typed_parameters"), None)
                return_node = next((part for part in parts[1:] if part.data in {"single_return_type", "multi_return_type"}), None)
                return_type = self._resolve_return_type(return_node)
                return_key = self._overload_return_key(return_node)
                method_key = f"{interface_name}.{method_name}"
                arity = self._count_params(params_node, skip_self=True)
                sig = self._param_types_sig(params_node, skip_self=True) if params_node else ()
                # Parameter names and declared defaults: a call through the
                # interface (``n.name()``) fills them and reorders keywords.
                self._func_param_counts[method_key] = arity
                if params_node:
                    self._collect_param_defaults(method_key, params_node, skip_self=True)
                self._overloaded_functions.setdefault(method_key, set()).add(arity)
                self._overload_variants.setdefault(method_key, []).append({
                    "arity": arity,
                    "required": arity - len(self._func_defaults.get(method_key, [])),
                    "sig": sig,
                    "return": return_type,
                    "return_key": return_key,
                    "suffix": "",
                    "meta_key": method_key,
                })

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

    def _finalize_class_go_names(self) -> None:
        claimed: dict[str, str] = {}
        names = self._class_names | self._interfaces
        for name in sorted(names, key=lambda item: (not item[:1].isupper(), item)):
            preferred = self._go_public_name(name)
            candidate = preferred
            index = 2
            while candidate in claimed and claimed[candidate] != name:
                candidate = f"{preferred}__lam{index}"
                index += 1
            self._class_go_names[name] = candidate
            claimed[candidate] = name

    def _finalize_function_go_names(self) -> None:
        claimed = {
            go_name: f"type:{name}"
            for name, go_name in self._class_go_names.items()
        }
        for class_name, methods in self._static_methods.items():
            go_class = self._go_class_name(class_name)
            for method in methods:
                preferred = f"{go_class}_{method}"
                if method in self._private_methods.get(class_name, set()):
                    preferred = self._go_binding_name(self._go_private_name(preferred))
                claimed[preferred] = f"static-method:{class_name}.{method}"
        for class_name, fields in self._static_vars.items():
            for field in fields:
                claimed[self._static_var_go_name(class_name, field)] = f"static-field:{class_name}.{field}"
        for name, go_name in self._link_function_go_names.items():
            self._function_go_names[name] = go_name
            for variant in self._overload_variants.get(name, []) or [{"suffix": ""}]:
                claimed[go_name + str(variant.get("suffix", ""))] = f"function:{name}"
        ordered = sorted(
            self._user_functions,
            key=lambda item: (item not in self._overload_variants, item[:1].isupper(), item),
        )
        for name in ordered:
            if name in self._function_go_names:
                continue
            if name == "main":
                preferred = "main"
            elif name in self._private_functions:
                preferred = self._go_binding_name(self._go_private_name(name))
            else:
                preferred = self._go_public_name(name)
            preferred = self._go_binding_name(preferred)
            suffixes = {
                str(variant.get("suffix", ""))
                for variant in self._overload_variants.get(name, [])
            } or {""}
            candidate = preferred
            index = 2
            owner = f"function:{name}"
            while any(
                candidate + suffix in claimed and claimed[candidate + suffix] != owner
                for suffix in suffixes
            ):
                candidate = f"{preferred}__lam{index}"
                index += 1
            self._function_go_names[name] = candidate
            for suffix in suffixes:
                claimed[candidate + suffix] = owner
        # ``from m import f`` and ``from m import f as g`` in one module: ``g``
        # follows ``f``'s final Go name (``F__lam2`` when ``F`` is taken).
        for alias, canonical in self._link_function_aliases.items():
            if canonical in self._function_go_names and alias in self._function_go_names:
                self._function_go_names[alias] = self._function_go_names[canonical]

    def _finalize_member_go_names(self) -> None:
        for class_name in self._class_names:
            entries: list[tuple[str, str, str]] = []
            for method in self._instance_methods.get(class_name, set()):
                if method in DUNDER_OPS:
                    preferred = DUNDER_OPS[method]
                elif method in self._private_methods.get(class_name, set()):
                    preferred = self._go_binding_name(self._go_private_name(method))
                elif method in {"__str__", "__repr__"}:
                    preferred = "String"
                elif method == "__json__":
                    preferred = "LamJSON"
                elif method == "__len__":
                    preferred = "Len"
                else:
                    preferred = self._go_public_name(method)
                entries.append(("method", method, preferred))
            fields = {name for name, _go_type in self.class_fields.get(class_name, [])}
            fields.update(self._external_field_names.get(class_name, set()))
            entries.extend(("field", field, self._go_public_name(field)) for field in fields)
            claimed: dict[str, tuple[str, str]] = {}
            for kind, name, preferred in sorted(
                entries,
                key=lambda item: (item[1] != "__json__", not item[1][:1].isupper(), item[2], item[0], item[1]),
            ):
                candidate = preferred
                index = 2
                while candidate in claimed and claimed[candidate] != (kind, name):
                    candidate = f"{preferred}__lam{index}"
                    index += 1
                if kind == "method":
                    self._method_go_names[(class_name, name)] = candidate
                else:
                    self._field_go_names[(class_name, name)] = candidate
                claimed[candidate] = (kind, name)

    def _finalize_overload_variants(self) -> None:
        """Assign each overload variant a stable mangled suffix.

        Runs once, after ``_collect_function_names`` has visited
        every ``funcdef`` in the source and populated
        ``_overload_variants``. The policy is:

        * **Single variant** — no suffix. The Go function keeps
          its plain public/private name. This is the vast majority
          of definitions and the compiler never has to dispatch
          at the call site.
        * **Multiple variants, all with distinct arities** —
          suffix is ``_{arity}``. Same as the legacy arity-only
          overload path; keeping the shape stable means user-
          facing tooling (stack traces, ``go tool pprof`` output)
          looks the same as before for arity-only overloads.
        * **Multiple variants, some sharing an arity** — suffix
          is ``_{arity}_{sig_suffix}`` for every parameter-distinct variant.
          Variants sharing parameters add ``_ret_{return_suffix}`` so return-
          type-only overloads remain distinct Go symbols. Variants at *other*
          arities still get the shorter ``_{arity}`` form.

        The suffix is written back into each variant dict's
        ``suffix`` field so def-site emission and call-site
        dispatch share a single lookup. The function is
        idempotent: running it twice on the same transpiler
        instance produces the same suffixes (useful for repeat
        compiles from the same driver).
        """
        for name, variants in list(self._overload_variants.items()):
            unique: list[dict] = []
            seen: set[tuple] = set()
            for variant in variants:
                identity = (
                    int(variant.get("arity", 0)),
                    int(variant.get("required", variant.get("arity", 0))),
                    tuple(variant.get("sig") or ()),
                    str(variant.get("return_key") or variant.get("return") or ""),
                )
                if identity in seen:
                    continue
                seen.add(identity)
                unique.append(variant)
            self._overload_variants[name] = unique

        for _ in range(max(1, len(self.class_bases))):
            changed = False
            for class_name, bases in self.class_bases.items():
                for base in bases:
                    prefix = f"{base}."
                    for base_key, base_variants in list(self._overload_variants.items()):
                        if not base_key.startswith(prefix):
                            continue
                        method_name = base_key[len(prefix):]
                        if "." in method_name or method_name not in self._instance_methods.get(base, set()):
                            continue
                        child_key = f"{class_name}.{method_name}"
                        child_variants = self._overload_variants.setdefault(child_key, [])
                        own_signatures = {
                            (tuple(v.get("sig") or ()), str(v.get("return_key") or v.get("return") or ""))
                            for v in child_variants
                        }
                        for variant in base_variants:
                            signature = (
                                tuple(variant.get("sig") or ()),
                                str(variant.get("return_key") or variant.get("return") or ""),
                            )
                            if signature in own_signatures:
                                continue
                            child_variants.append(dict(variant, suffix=""))
                            own_signatures.add(signature)
                            changed = True
                        if child_variants:
                            self._instance_methods.setdefault(class_name, set()).add(method_name)
                            self._overloaded_functions[child_key] = {
                                int(v.get("arity", 0)) for v in child_variants
                            }
            if not changed:
                break
        for name, variants in self._overload_variants.items():
            if name == "main":
                # ``main`` is fixed by Go's runtime; we never mangle it
                # even if the user somehow overloads it (the semantic
                # checker already rejects that, so this is defensive).
                for v in variants:
                    v["suffix"] = ""
                continue
            if len(variants) <= 1:
                for v in variants:
                    v["suffix"] = ""
                continue
            # Count how many variants share each arity so we know
            # which variants need the longer ``_{arity}_{sig}``
            # form and which can stick with the plain ``_{arity}``.
            arity_counts: Dict[int, int] = {}
            signature_counts: Dict[Tuple[int, Tuple[str, ...]], int] = {}
            for v in variants:
                a = int(v["arity"])  # type: ignore[arg-type]
                signature = tuple(v.get("sig") or ())
                arity_counts[a] = arity_counts.get(a, 0) + 1
                signature_counts[(a, signature)] = signature_counts.get((a, signature), 0) + 1
            for v in variants:
                a = int(v["arity"])  # type: ignore[arg-type]
                signature = tuple(v.get("sig") or ())
                if signature_counts[(a, signature)] > 1:
                    return_suffix = self._sig_suffix((str(v.get("return_key") or v.get("return") or "void"),))
                    v["suffix"] = f"_{a}_{self._sig_suffix(signature)}_ret_{return_suffix}"
                elif arity_counts[a] > 1:
                    v["suffix"] = f"_{a}_{self._sig_suffix(signature)}"
                else:
                    v["suffix"] = f"_{a}"

    def _overload_variant_for_sig(self, name: str, sig, expected_return: str = ""):
        variants = self._overload_variants.get(name) or []
        actual = tuple(sig)
        candidates = [
            variant for variant in variants
            if tuple(variant.get("sig") or ()) == actual
        ]
        if not candidates:
            for variant in variants:
                expected = tuple(variant.get("sig") or ())
                required = int(variant.get("required", len(expected)))
                matches = all(
                    expected_type == actual_type
                    or expected_type == "LamJSON" and (
                        actual_type.startswith("map[")
                        or actual_type.startswith("[]")
                        or actual_type in {"bool", "int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16", "uint32", "uint64", "float32", "float64"}
                    )
                    for expected_type, actual_type in zip(expected, actual)
                )
                if required <= len(actual) <= len(expected) and matches:
                    candidates.append(variant)
        if not candidates:
            # ``fail(2.5, {"a": 1})`` against ``fail(str, dict[str, any])`` /
            # ``fail(float, dict[str, any])``: the variant every argument can
            # be passed to (a literal collection to an ``any``-element one,
            # an int to a float, anything to ``any``), the closest first.
            best_score = -1
            for variant in variants:
                expected = tuple(variant.get("sig") or ())
                required = int(variant.get("required", len(expected)))
                if not required <= len(actual) <= len(expected):
                    continue
                scores = [self._overload_arg_score(e, a) for e, a in zip(expected, actual)]
                if any(score < 0 for score in scores):
                    continue
                if sum(scores) > best_score:
                    best_score = sum(scores)
                    candidates = [variant]
        if not candidates:
            candidates = [
                variant for variant in variants
                if int(variant.get("required", variant.get("arity", 0)))
                <= len(actual)
                <= int(variant.get("arity", 0))
            ]
        contextual_return = (
            expected_return
            or self._expected_expr_type_key
            or self._expected_expr_go_type
            or ""
        ).strip()
        if contextual_return and contextual_return != "interface{}":
            for variant in candidates:
                return_key = str(variant.get("return_key") or variant.get("return") or "")
                if return_key == contextual_return:
                    return variant
        return candidates[0] if candidates else None

    _OVERLOAD_NUMERIC = frozenset({
        "int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16", "uint32", "uint64",
    })

    def _overload_arg_score(self, expected: str, actual: str) -> int:
        """How well an argument of Go type ``actual`` fits a parameter of
        type ``expected``: 3 exact, 2 by conversion (int to float, a
        collection literal to ``any`` elements), 1 to ``any`` or an unknown
        argument, -1 when it cannot be passed."""
        expected, actual = (expected or "").strip(), (actual or "").strip()
        if expected == actual:
            return 3
        if actual in {"", "interface{}"} or expected == "interface{}":
            return 1
        if expected in {"float64", "float32"} and actual in self._OVERLOAD_NUMERIC:
            return 2
        if expected.startswith("[]") and actual.startswith("[]"):
            return 2 if self._overload_arg_score(expected[2:], actual[2:]) >= 1 else -1
        if expected.startswith("map[") and actual.startswith("map["):
            exp_key, exp_value = self._split_go_map_type(expected)
            act_key, act_value = self._split_go_map_type(actual)
            if exp_key == act_key and self._overload_arg_score(exp_value, act_value) >= 1:
                return 2
            return -1
        return -1

    def _overload_suffix_for_sig(self, name: str, sig, expected_return: str = ""):
        """Return the Go name suffix for the best variant of ``name``.

        Definition sites match the complete parameter signature. Call
        sites may match a unique typed prefix when the remaining
        parameters have defaults. The arity fallback preserves legacy
        dispatch when argument types cannot be inferred.
        """
        variant = self._overload_variant_for_sig(name, sig, expected_return)
        if variant is not None:
            return variant.get("suffix", "") or ""
        if len(self._overloaded_functions.get(name, set())) > 1:
            return f"_{len(sig)}"
        return ""

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
        go_types: List[str] = []
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
                    if len(pnode.children) > 1:
                        go_types.append(self._type_expr_to_go(pnode.children[1]))
                    else:
                        go_types.append("interface{}")
                    param_idx += 1
                    total += 1
                elif isinstance(pnode, Tree) and pnode.data == "tuple_typed_param":
                    type_node = pnode.children[-1]
                    names.append("")
                    go_types.append(self._type_expr_to_go(type_node))
                    param_idx += 1
                    total += 1
            elif isinstance(child, Tree) and child.data == "typed_param":
                pname = self._get_name(child.children[0])
                if skip_self and pname == "self":
                    continue
                names.append(pname or "")
                if len(child.children) > 1:
                    go_types.append(self._type_expr_to_go(child.children[1]))
                else:
                    go_types.append("interface{}")
                param_idx += 1
                total += 1
            elif isinstance(child, Tree) and child.data == "typed_starparams":
                star_param = child.children[0] if child.children else None
                names.append("")
                if (
                    isinstance(star_param, Tree)
                    and star_param.data == "typed_param"
                    and len(star_param.children) > 1
                ):
                    go_types.append("..." + self._type_expr_to_go(star_param.children[1]))
                else:
                    go_types.append("...interface{}")
                param_idx += 1
                total += 1
        if defaults:
            self._func_defaults[func_name] = defaults
        self._func_param_counts[func_name] = total
        if names:
            self._func_param_names[func_name] = names
            self._func_param_go_types[func_name] = go_types

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
                # ``secret: int = 42`` in the class body: an instance
                # field default, applied by every constructor.
                value_node = children[2] if len(children) > 2 else None
                if value_node is not None:
                    defaults = self.class_field_defaults.setdefault(class_name, [])
                    if field_name not in [f for f, _, _ in defaults]:
                        defaults.append((field_name, go_type, value_node))
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
        base = f"{self._go_class_name(class_name)}_{var_name}"
        is_private = self._static_vars.get(class_name, {}).get(var_name, False)
        if is_private:
            return self._go_binding_name(self._go_private_name(base))
        return self._go_public_name(base)

    def _is_static_var_go_name(self, name: str) -> bool:
        return name in self._static_var_go_names
