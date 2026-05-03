#!/usr/bin/env python3
"""Definition visitor methods (funcdef, classdef, interfacedef) for the transpiler."""

from __future__ import annotations
from lark import Tree, Token
from typing import List
from compiler.constants import DUNDER_OPS


class DefinitionVisitorMixin:
    """Handles function, class, and interface definitions."""

    # ─── Functions ─────────────────────────────────────────────

    def _visit_funcdef(self, node: Tree):
        (is_private, is_static, is_async, name_node, params_node,
         return_type_node, suite_node, type_params_node) = self._parse_funcdef(node)

        func_name = self._get_name(name_node)
        # Expose the function-level type-parameter names to
        # ``_type_expr_to_go`` so annotations can reference them before
        # the clause is emitted.
        tp_clause, tp_names, _ = self._type_params_to_go(type_params_node)
        prev_generic_names = set(self._generic_names)
        self._generic_names.update(tp_names)

        # ── Nested function definitions ───────────────────────────
        # Go has no nested named functions; ``func name() {}`` inside
        # another function body is a syntax error. We rewrite to a
        # closure assignment ``name := func(params) ret { ... }`` so
        # the natural Lam form keeps working. Captured locals come
        # along for free because Go closures capture by reference.
        is_nested = (not self._at_top_level
                     and not self.current_class
                     and not is_static
                     and not is_async
                     and func_name != "main")
        if is_nested:
            try:
                with self._scoped(_at_top_level=False):
                    self._emit_nested_funcdef(
                        func_name, params_node, return_type_node,
                        suite_node, tp_clause=tp_clause,
                    )
            finally:
                self._generic_names = prev_generic_names
            return

        try:
            return_type = self._resolve_return_type(return_type_node)
            is_multi_return = (isinstance(return_type_node, Tree) and
                               return_type_node.data == "multi_return_type")

            with self._scoped(_at_top_level=False):
                self._visit_funcdef_body(
                    is_private, is_static, is_async,
                    func_name, params_node, return_type_node,
                    return_type, is_multi_return, suite_node,
                    tp_clause=tp_clause,
                )
        finally:
            self._generic_names = prev_generic_names

    def _emit_nested_funcdef(
        self, func_name, params_node, return_type_node, suite_node,
        tp_clause: str = "",
    ):
        """Emit a nested ``func name(...) { ... }`` as ``name := func(...) { ... }``.

        Generic type parameters on nested funcs are not supported — Go
        closures cannot carry their own type parameter list. If
        ``tp_clause`` is non-empty we silently drop it; the outer
        function's generics already cover the typical use case.
        """
        return_type = self._resolve_return_type(return_type_node)
        params_str = self._typed_params_to_go(params_node)
        ret_str = f" {return_type}" if return_type else ""

        # Register the closure name in the *outer* scope so the
        # call-site dispatcher recognises ``inner(...)`` as a local
        # variable call (it would otherwise fall through to the
        # generic ``_go_public_name`` rewrite and emit ``Inner(...)``,
        # which is undefined).
        self._declare_var(func_name)

        self._emit(f"{func_name} := func({params_str}){ret_str} {{")
        self.indent += 1
        self._push_scope()
        if params_node:
            self._declare_params(params_node)
            self._emit_tuple_param_prologue(params_node)
        body_start = len(self.output_lines)
        params_at_start = set(self.declared_vars)
        with self._scoped(_current_return_type=return_type):
            self._visit_suite(suite_node)
        self._emit_unused_local_silencers(body_start, params_at_start)
        self._pop_scope()
        self.indent -= 1
        self._emit("}")
        # Mark as used so Go's "declared and not used" check can't
        # trip on closures that are passed straight to ``srv.onRequest(...)``
        # later — that case is a normal use, but for safety against
        # plugins that capture a closure they never call we emit a
        # ``_ = name`` only when the rest of the suite never references
        # it. The semantic checker doesn't currently track this, so we
        # leave it to the Go compiler to surface unused closures.

    def _visit_funcdef_body(
        self, is_private, is_static, is_async,
        func_name, params_node, return_type_node,
        return_type, is_multi_return, suite_node,
        tp_clause: str = "",
    ):
        if self.current_class:
            if is_static:
                go_name = self._go_public_name(self.current_class) + "_" + func_name
                if is_private:
                    go_name = self._go_private_name(go_name)
                else:
                    go_name = self._go_public_name(go_name)
                params_str = self._typed_params_to_go(params_node, skip_self=True)
                ret_str = f" {return_type}" if return_type else ""
                # Static methods lower to top-level functions, so they
                # have to carry the class's type parameters themselves.
                # Merge the class clause with any method-level one
                # (method-level generics are rare but legal).
                static_clause = self._merge_generic_clauses(
                    self._generic_classes.get(self.current_class, ""),
                    tp_clause,
                )
                self._emit(f"func {go_name}{static_clause}({params_str}){ret_str} {{")
                self.indent += 1
                self._push_scope()
                if params_node:
                    self._declare_params(params_node)
                    self._emit_tuple_param_prologue(params_node)
                body_start = len(self.output_lines)
                params_at_start = set(self.declared_vars)
                self._visit_suite(suite_node)
                self._emit_unused_local_silencers(body_start, params_at_start)
                self._pop_scope()
                self.indent -= 1
                self._emit("}")
                self._emit("")
            elif func_name == "__init__" or func_name == "init":
                self._emit_constructor(func_name, params_node, suite_node)
            elif func_name == "__str__" or func_name == "__repr__":
                self._emit_method("String", "string", params_node, suite_node, tp_clause=tp_clause)
            elif func_name == "__len__":
                self._emit_method("Len", "int", params_node, suite_node, tp_clause=tp_clause)
            elif func_name in DUNDER_OPS:
                go_method = DUNDER_OPS[func_name]
                if self.current_class not in self._class_dunder_methods:
                    self._class_dunder_methods[self.current_class] = {}
                self._class_dunder_methods[self.current_class][func_name] = go_method
                self._emit_method(go_method, return_type, params_node, suite_node, tp_clause=tp_clause)
            else:
                method_name = func_name
                if is_private:
                    method_name = self._go_private_name(func_name)
                else:
                    method_name = self._go_public_name(func_name)
                self._emit_method(method_name, return_type, params_node, suite_node, tp_clause=tp_clause)
            return

        # ── Name resolution ──
        is_overloaded = len(self._overloaded_functions.get(func_name, set())) > 1
        arity = self._count_params(params_node)
        arity_suffix = f"_{arity}" if is_overloaded and func_name != "main" else ""

        if func_name == "main":
            go_name = "main"
        elif is_private:
            go_name = self._go_private_name(func_name) + arity_suffix
        else:
            go_name = self._go_public_name(func_name) + arity_suffix

        params_str = self._typed_params_to_go(params_node)

        # ── Async function → goroutine returning channel ──
        if is_async:
            chan_type = return_type if return_type else "interface{}"
            self._emit(f"func {go_name}{tp_clause}({params_str}) chan {chan_type} {{")
            self.indent += 1
            self._emit(f"ch := make(chan {chan_type}, 1)")
            self._emit(f"go func() {{")
            self.indent += 1
            self._push_scope()
            if params_node:
                self._declare_params(params_node)
                self._emit_tuple_param_prologue(params_node)
            body_start = len(self.output_lines)
            params_at_start = set(self.declared_vars)
            with self._scoped(_in_async_func=True, _async_chan_name="ch"):
                self._visit_suite(suite_node)
            self._emit_unused_local_silencers(body_start, params_at_start)
            self._pop_scope()
            self.indent -= 1
            self._emit(f"}}()")
            self._emit(f"return ch")
            self.indent -= 1
            self._emit("}")
            self._emit("")
            return

        has_try = self._suite_contains_try(suite_node)
        has_yield = self._tree_contains(suite_node, "yield_expr")

        # ── Generator function → goroutine + channel ──
        if has_yield:
            chan_type = return_type if return_type else "interface{}"
            self._emit(f"func {go_name}{tp_clause}({params_str}) chan {chan_type} {{")
            self.indent += 1
            self._emit(f"_ch := make(chan {chan_type})")
            self._emit(f"go func() {{")
            self.indent += 1
            self._emit(f"defer close(_ch)")
            self._push_scope()
            if params_node:
                self._declare_params(params_node)
                self._emit_tuple_param_prologue(params_node)
            body_start = len(self.output_lines)
            params_at_start = set(self.declared_vars)
            with self._scoped(_in_generator=True, _generator_chan="_ch"):
                self._visit_suite(suite_node)
            self._emit_unused_local_silencers(body_start, params_at_start)
            self._pop_scope()
            self.indent -= 1
            self._emit(f"}}()")
            self._emit(f"return _ch")
            self.indent -= 1
            self._emit("}")
            self._emit("")
            return

        if is_multi_return:
            types = self._resolve_multi_return_types(return_type_node)
            ret_str = f" ({', '.join(types)})"
            current_ret = f"({', '.join(types)})"
            self._emit(f"func {go_name}{tp_clause}({params_str}){ret_str} {{")
        elif return_type and has_try:
            ret_str = f" (retval {return_type})"
            current_ret = return_type
            self._emit(f"func {go_name}{tp_clause}({params_str}){ret_str} {{")
            self.indent += 1
            self._push_scope()
            self._declare_var("retval")
            if params_node:
                self._declare_params(params_node)
                self._emit_tuple_param_prologue(params_node)
            body_start = len(self.output_lines)
            params_at_start = set(self.declared_vars)
            with self._scoped(_in_try_func=True, _current_return_type=current_ret):
                self._visit_suite(suite_node)
            self._emit_unused_local_silencers(body_start, params_at_start)
            # Fallback ``return`` so Go's flow analyser is happy even
            # when the try-with-catch IIFE is the last thing in the
            # function. The named ``retval`` already holds the
            # appropriate value (zero for missing-return paths).
            self._emit("return")
            self._pop_scope()
            self.indent -= 1
            self._emit("}")
            self._emit("")
            return
        elif return_type:
            current_ret = return_type
            self._emit(f"func {go_name}{tp_clause}({params_str}) {return_type} {{")
        else:
            current_ret = ""
            self._emit(f"func {go_name}{tp_clause}({params_str}) {{")

        self.indent += 1
        self._push_scope()
        if params_node:
            self._declare_params(params_node)
            self._emit_tuple_param_prologue(params_node)
        body_start = len(self.output_lines)
        params_at_start = set(self.declared_vars)
        with self._scoped(_current_return_type=current_ret):
            self._visit_suite(suite_node)
        self._emit_unused_local_silencers(body_start, params_at_start)
        self._pop_scope()
        self.indent -= 1
        self._emit("}")
        self._emit("")

    def _emit_constructor(self, func_name, params_node, suite_node):
        cls = self.current_class
        go_cls = self._go_public_name(cls)
        params_str = self._typed_params_to_go(params_node, skip_self=True)

        # Generic classes need their type parameters on both the
        # constructor signature and the ``&Box[T]{}`` zero-value.
        class_clause = self._generic_classes.get(cls, "")
        class_args = self._class_type_args(cls)

        self._emit(f"func New{go_cls}{class_clause}({params_str}) *{go_cls}{class_args} {{")
        self.indent += 1
        self._push_scope()
        self._emit(f"s := &{go_cls}{class_args}{{}}")
        if params_node:
            self._declare_params(params_node)
            self._emit_tuple_param_prologue(params_node, skip_self=True)
        self._declare_var("s")
        body_start = len(self.output_lines)
        params_at_start = set(self.declared_vars)
        with self._scoped(_self_replacement="s"):
            self._visit_suite(suite_node)
        self._emit_unused_local_silencers(body_start, params_at_start)
        self._emit("return s")
        self._pop_scope()
        self.indent -= 1
        self._emit("}")
        self._emit("")

    def _emit_method(self, go_method, return_type, params_node, suite_node,
                     tp_clause: str = ""):
        cls = self.current_class
        go_cls = self._go_public_name(cls)
        params_str = self._typed_params_to_go(params_node, skip_self=True)
        ret_str = f" {return_type}" if return_type else ""

        # For generic classes the receiver carries the class's type
        # parameter list (``*Box[T]``) but the declaration names them
        # only once on the class, so methods use ``class_args`` for the
        # receiver and ``tp_clause`` only for method-level generics
        # (rare — most methods inherit from the class).
        class_args = self._class_type_args(cls)
        self._emit(f"func (s *{go_cls}{class_args}) {go_method}{tp_clause}({params_str}){ret_str} {{")
        self.indent += 1
        self._push_scope()
        if params_node:
            self._declare_params(params_node)
            self._emit_tuple_param_prologue(params_node, skip_self=True)
        self._declare_var("s")
        body_start = len(self.output_lines)
        params_at_start = set(self.declared_vars)
        with self._scoped(_self_replacement="s",
                          _current_return_type=return_type or ""):
            self._visit_suite(suite_node)
        self._emit_unused_local_silencers(body_start, params_at_start)
        self._pop_scope()
        self.indent -= 1
        self._emit("}")
        self._emit("")

    def _class_type_args(self, class_name: str) -> str:
        """Return the ``[T, U]`` args string for a generic class's name use.

        Given ``class Box[T any]``, ``Box`` written inside the struct's
        own method signature needs to be spelled ``Box[T]``. This
        helper produces that suffix (or ``""`` when the class isn't
        generic).
        """
        clause = self._generic_classes.get(class_name, "")
        if not clause:
            return ""
        # ``[T any, U comparable]`` -> ``[T, U]``
        inside = clause.strip("[]").split(",")
        names = [p.strip().split()[0] for p in inside if p.strip()]
        return "[" + ", ".join(names) + "]"

    # ─── Class ─────────────────────────────────────────────────

    def _visit_classdef(self, node: Tree):
        name_node = node.children[0]
        suite_node = node.children[-1]
        class_name = self._get_name(name_node)
        go_cls = self._go_public_name(class_name)

        # Detect and register an optional type-parameter clause
        # ``class Box[T any] { ... }``. The clause needs to be visible
        # to all methods (so receivers can say ``*Box[T]``) and to
        # ``_type_expr_to_go`` (so ``T`` in field annotations stays
        # literal).
        tp_node = None
        for c in node.children:
            if isinstance(c, Tree) and c.data == "type_params":
                tp_node = c
                break
        tp_clause, tp_names, _ = self._type_params_to_go(tp_node)

        prev_generic_names = set(self._generic_names)
        if tp_clause:
            self._generic_classes[class_name] = tp_clause
            self._generic_names.update(tp_names)

        try:
            fields = self.class_fields.get(class_name, [])
            self._emit(f"type {go_cls}{tp_clause} struct {{")
            self.indent += 1
            for base in self.class_bases.get(class_name, []):
                go_base = self._go_public_name(base)
                self._emit(f"*{go_base}")
            for field_name, go_type in fields:
                json_tag = field_name
                self._emit(f'{self._go_public_name(field_name)} {go_type} `json:"{json_tag}"`')
            self.indent -= 1
            self._emit("}")
            self._emit("")

            self._emit_static_fields(class_name)

            has_init = False
            with self._scoped(current_class=class_name, _at_top_level=False):
                for child in self._suite_stmts(suite_node):
                    if isinstance(child, Tree):
                        if child.data == "funcdef":
                            _, _, _, fn_node, _, _, _, _ = self._parse_funcdef(child)
                            fn_name = self._get_name(fn_node)
                            if fn_name in ("__init__", "init"):
                                has_init = True
                            self._visit(child)

                if not has_init:
                    class_args = self._class_type_args(class_name)
                    self._emit(f"func New{go_cls}{tp_clause}() *{go_cls}{class_args} {{")
                    self.indent += 1
                    self._emit(f"return &{go_cls}{class_args}{{}}")
                    self.indent -= 1
                    self._emit("}")
                    self._emit("")
        finally:
            self._generic_names = prev_generic_names

    def _emit_static_fields(self, class_name: str) -> None:
        static_fields = self.class_static_fields.get(class_name, [])
        for field_name, go_type, value_node, _is_private in static_fields:
            go_name = self._static_var_go_name(class_name, field_name)
            if value_node is None:
                self._emit(f"var {go_name} {go_type}")
                continue
            with self._scoped(_propagate_cast_hint=go_type):
                value_str = self._typed_value_to_go(value_node, go_type)
            self._emit(f"var {go_name} {go_type} = {value_str}")
        if static_fields:
            self._emit("")

    # ─── Interface ─────────────────────────────────────────────

    def _visit_interfacedef(self, node: Tree):
        name_node = node.children[0]
        iface_name = self._get_name(name_node)
        go_name = self._go_public_name(iface_name)
        self._emit(f"type {go_name} interface {{")
        self.indent += 1
        for child in node.children[1:]:
            if isinstance(child, Tree) and child.data == "interface_method":
                self._emit_interface_method(child)
        self.indent -= 1
        self._emit("}")
        self._emit("")

    def _emit_interface_method(self, node: Tree):
        # Grammar: ``interface_method: FUNC name "(" [typed_parameters] ")"
        # ["->" return_type] ";"`` — the ``FUNC`` keyword token leaks
        # into ``node.children`` because the rule keeps it named, so
        # we drop Token children before picking the method-name node
        # (otherwise ``children[0]`` is the literal ``func`` and
        # every interface method on the emitted Go would be called
        # ``Func``).
        children = [c for c in node.children
                    if c is not None and not isinstance(c, Token)]
        if not children:
            return
        method_name = self._go_public_name(self._get_name(children[0]))
        params_node = children[1] if len(children) > 1 and isinstance(children[1], Tree) and children[1].data == "typed_parameters" else None
        ret_node = None
        for c in children[1:]:
            if isinstance(c, Tree) and c.data in ("single_return_type", "multi_return_type"):
                ret_node = c
                break
        params_str = self._typed_params_to_go(params_node, skip_self=True)
        ret_str = ""
        if ret_node:
            ret_str = " " + self._resolve_return_type(ret_node)
        self._emit(f"{method_name}({params_str}){ret_str}")
