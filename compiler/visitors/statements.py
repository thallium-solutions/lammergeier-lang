#!/usr/bin/env python3
"""Statement visitor methods for the transpiler."""

from __future__ import annotations
from lark import Tree, Token
from typing import Optional, Tuple, List
from compiler.constants import PYTHON_EXCEPTIONS


class StatementVisitorMixin:
    """Handles all statement-level AST nodes."""

    # ─── Top-level / import ────────────────────────────────────

    def _visit_file_input(self, node: Tree):
        for child in node.children:
            if child is not None and isinstance(child, Tree):
                self._visit(child)

    def _visit_import_stmt(self, node: Tree):
        for child in node.children:
            if isinstance(child, Tree):
                self._visit(child)

    def _visit_import_name(self, node: Tree):
        for child in node.children:
            if isinstance(child, Tree) and child.data == "dotted_as_names":
                for das in child.children:
                    if isinstance(das, Tree) and das.data == "dotted_as_name":
                        name = self._dotted_name_to_str(das.children[0])
                        self._lam_imports.append(name)

    def _visit_import_from(self, node: Tree):
        module_name = None
        for child in node.children:
            if isinstance(child, Tree) and child.data == "dotted_name":
                module_name = self._dotted_name_to_str(child)
                break
            # ``scoped_name`` — ``from @alice/lamwebp import ...``.
            # The child is always a single SCOPED_NAME token; the
            # string form (``@scope/name``) is the canonical module
            # key used throughout the resolver.
            if isinstance(child, Tree) and child.data == "scoped_name":
                if child.children:
                    module_name = str(child.children[0])
                break
        if module_name:
            self._lam_imports.append(module_name)

    # ─── Suite ─────────────────────────────────────────────────

    def _visit_suite(self, node, **kw):
        if node is None:
            return
        if not isinstance(node, Tree):
            return
        if node.data != "suite":
            self._visit(node)
            return
        for child in node.children:
            if isinstance(child, Tree):
                self._visit(child)

    def _visit_simple_stmt(self, node: Tree):
        for child in node.children:
            if isinstance(child, Tree):
                self._visit(child)

    # ─── Assignment ────────────────────────────────────────────

    def _visit_assign_stmt(self, node: Tree):
        self._visit(node.children[0])

    def _visit_annassign(self, node: Tree):
        is_private = False
        is_static = False
        children = list(node.children)
        while children and (children[0] is None or (isinstance(children[0], Token) and str(children[0]) in ("private", "static"))):
            if isinstance(children[0], Token):
                if str(children[0]) == "private":
                    is_private = True
                elif str(children[0]) == "static":
                    is_static = True
            children.pop(0)

        target = children[0]
        type_node = children[1]
        value = children[2] if len(children) > 2 else None

        target_str = self._expr_to_go(target)
        go_type = self._type_expr_to_go(type_node)

        raw_type = self._get_raw_type_name(type_node)
        if raw_type and raw_type in self._class_names and "." not in target_str:
            self._var_types[target_str] = raw_type

        if go_type and "." not in target_str:
            self._var_go_types[target_str] = go_type

        if is_private and "." not in target_str:
            target_str = self._go_private_name(target_str)

        if value is not None:
            if isinstance(value, Tree) and value.data == "test" and len(value.children) == 3:
                true_val = self._expr_to_go(value.children[0])
                cond = self._expr_to_go(value.children[1])
                false_val = self._expr_to_go(value.children[2])
                if (
                    "." not in target_str
                    and target_str not in self.declared_vars
                    and not self._is_static_var_go_name(target_str)
                ):
                    self._emit(f"var {target_str} {go_type}")
                    self._declare_var(target_str)
                self._emit(f"if {cond} {{")
                self.indent += 1
                self._emit(f"{target_str} = {true_val}")
                self.indent -= 1
                self._emit(f"}} else {{")
                self.indent += 1
                self._emit(f"{target_str} = {false_val}")
                self.indent -= 1
                self._emit(f"}}")
                return
            # Publish the LHS type so any ``?`` inside ``value`` knows
            # what cast to apply to its substituted ``__qN.Value``.
            with self._scoped(_propagate_cast_hint=go_type):
                value_str = self._typed_value_to_go(value, go_type)
            if "." in target_str:
                self._emit(f"{target_str} = {value_str}")
            elif self._is_static_var_go_name(target_str):
                self._emit(f"{target_str} = {value_str}")
            elif target_str in self.declared_vars:
                self._emit(f"{target_str} = {value_str}")
            else:
                self._emit(f"var {target_str} {go_type} = {value_str}")
                self._declare_var(target_str)
        else:
            if (
                target_str not in self.declared_vars
                and not self._is_static_var_go_name(target_str)
            ):
                self._emit(f"var {target_str} {go_type}")
                self._declare_var(target_str)

    def _visit_const_stmt(self, node: Tree):
        """``const NAME [: TYPE] = EXPR`` — immutable binding.

        We emit a Go ``var`` rather than ``const`` because Lam allows
        any expression on the RHS (function calls, struct literals,
        etc.) while Go's ``const`` requires a compile-time evaluable
        operand. Immutability is enforced by the semantic checker, so
        the storage class is purely a code-gen detail.
        """
        children = list(node.children)
        name_node = children[0]
        type_node = children[1] if len(children) > 1 else None
        value_node = children[2] if len(children) > 2 else None

        target_str = self._get_name(name_node)
        if type_node is not None:
            go_type = self._type_expr_to_go(type_node)
        else:
            go_type = self._infer_type_from_value(value_node) if value_node is not None else ""

        # Track for downstream type-driven decisions (e.g. method
        # dispatch, ternary handling) — the same registries
        # ``annassign`` populates.
        raw_type = self._get_raw_type_name(type_node) if type_node is not None else ""
        if raw_type and raw_type in self._class_names:
            self._var_types[target_str] = raw_type
        if go_type:
            self._var_go_types[target_str] = go_type

        value_str = self._typed_value_to_go(value_node, go_type) if value_node is not None else ""
        if go_type:
            self._emit(f"var {target_str} {go_type} = {value_str}")
        else:
            # No type info anywhere — let Go infer.
            self._emit(f"{target_str} := {value_str}")
        self._declare_var(target_str)

    def _visit_assign(self, node: Tree):
        parts = [c for c in node.children if c is not None]
        if len(parts) >= 2:
            target_str = self._expr_to_go(parts[0])
            value_node = parts[-1]
            # Ternary in plain assign
            if isinstance(value_node, Tree) and value_node.data == "test" and len(value_node.children) == 3:
                true_val = self._expr_to_go(value_node.children[0])
                cond = self._expr_to_go(value_node.children[1])
                false_val = self._expr_to_go(value_node.children[2])
                if (
                    target_str not in self.declared_vars
                    and "." not in target_str
                    and "[" not in target_str
                    and not self._is_static_var_go_name(target_str)
                ):
                    self._emit(f"var {target_str} = {true_val}")
                    self._declare_var(target_str)
                    self._emit(f"if !({cond}) {{")
                    self.indent += 1
                    self._emit(f"{target_str} = {false_val}")
                    self.indent -= 1
                    self._emit(f"}}")
                else:
                    self._emit(f"if {cond} {{")
                    self.indent += 1
                    self._emit(f"{target_str} = {true_val}")
                    self.indent -= 1
                    self._emit(f"}} else {{")
                    self.indent += 1
                    self._emit(f"{target_str} = {false_val}")
                    self.indent -= 1
                    self._emit(f"}}")
                return
            value_str = self._expr_to_go(value_node)
            # If assigning a list or dict/map literal to a struct field,
            # prefer the field's declared Go type so that bare `[]` / `{}`
            # literals keep their intended element type (e.g. a
            # `map[interface{}]bool` field stays a bool map after reset).
            if isinstance(value_node, Tree) and "." in target_str and self.current_class:
                field_name = target_str.split(".")[-1]
                fields = self.class_fields.get(self.current_class, [])
                for fname, ftype in fields:
                    go_fname = self._go_public_name(fname)
                    if go_fname != field_name:
                        continue
                    if value_node.data == "list" and ftype.startswith("[]"):
                        value_str = self._typed_value_to_go(value_node, ftype)
                    elif value_node.data == "dict" and not value_node.children and ftype.startswith("map["):
                        value_str = f"{ftype}{{}}"
                    break
            # Slice assignment
            if isinstance(parts[0], Tree) and parts[0].data == "getitem":
                obj_node = parts[0].children[0]
                idx_node = parts[0].children[1]
                if isinstance(idx_node, Tree) and idx_node.data == "slice":
                    obj_str = self._expr_to_go(obj_node)
                    slice_parts = idx_node.children
                    lo = self._expr_to_go(slice_parts[0]) if len(slice_parts) > 0 and slice_parts[0] is not None else "0"
                    hi = self._expr_to_go(slice_parts[1]) if len(slice_parts) > 1 and slice_parts[1] is not None else f"len({obj_str})"
                    var_name = self._get_name(obj_node.children[0]) if isinstance(obj_node, Tree) and obj_node.data == "var" else obj_str
                    elem_type = self._var_go_types.get(var_name, "")
                    if elem_type.startswith("[]"):
                        elem_type = elem_type[2:]
                    if elem_type and isinstance(value_node, Tree) and value_node.data == "list":
                        elems = [self._expr_to_go(c) for c in value_node.children if c is not None]
                        rhs = f"[]{elem_type}{{{', '.join(elems)}}}"
                    else:
                        rhs = value_str
                    self._emit(f"{obj_str} = append({obj_str}[:{lo}], append({rhs}, {obj_str}[{hi}:]...)...)")
                    return
            # Tuple unpacking
            if isinstance(parts[0], Tree) and parts[0].data == "tuple":
                names = [self._expr_to_go(c) for c in parts[0].children if c is not None]
                self._emit(f"{', '.join(names)} = {value_str}")
                for n in names:
                    if n not in self.declared_vars:
                        self._declare_var(n)
            elif (
                target_str in self.declared_vars
                or "." in target_str
                or "[" in target_str
                or self._is_static_var_go_name(target_str)
            ):
                self._emit(f"{target_str} = {value_str}")
            elif target_str == "_":
                # The blank identifier is always pre-declared in Go;
                # using ``:=`` triggers a "no new variables on left
                # side of :=" build error. Emit a plain assignment so
                # ``_ = someCall()`` works as a discard.
                self._emit(f"_ = {value_str}")
            else:
                self._emit(f"{target_str} := {value_str}")
                self._declare_var(target_str)

    def _visit_augassign(self, node: Tree):
        target_str = self._expr_to_go(node.children[0])
        op = self._get_augassign_op(node.children[1])
        value_str = self._expr_to_go(node.children[2])

        if op == "**=":
            self._need_import("math")
            self._emit(f"{target_str} = math.Pow({target_str}, {value_str})")
        elif op == "//=":
            self._emit(f"{target_str} = int({target_str} / {value_str})")
        else:
            self._emit(f"{target_str} {op} {value_str}")

    # ─── Postfix increment / decrement ─────────────────────────

    def _visit_inc_stmt(self, node: Tree):
        self._emit_inc_dec(node, "__inc__", "Inc", go_op="++")

    def _visit_dec_stmt(self, node: Tree):
        self._emit_inc_dec(node, "__dec__", "Dec", go_op="--")

    def _emit_inc_dec(self, node: Tree, dunder: str,
                      dunder_go: str, *, go_op: str):
        """Shared lowering for ``x++`` / ``x--``. Uses Go's native
        postfix operator for numeric targets, but routes through the
        ``__inc__`` / ``__dec__`` dunder when the target's class
        defines one — emitted as ``x = x.Inc()`` so the method's
        return value replaces the previous binding.

        ``node.children[0]`` is the ``testlist_star_expr`` LHS. We
        only support a single target (``a, b++`` is rejected by
        Go itself, and Lam didn't have a use case to invent a
        tuple-increment shape). If the user writes a tuple on the
        left we fall back to emitting ``x++`` on the first element
        so downstream Go errors surface a clear message.
        """
        target_node = node.children[0]
        target_str = self._expr_to_go(target_node)

        # Class dunder hook — only fires when the target is a plain
        # variable whose tracked class exposes the override. Field
        # or indexed targets (``self.count++``) stay on the native
        # Go path: operator-overloading interop for those would
        # require l-value support we don't have yet, and the
        # native postfix handles the common cases.
        if isinstance(target_node, Tree) and target_node.data == "var":
            var_name = self._get_name(target_node.children[0])
            cls = self._var_types.get(var_name)
            if cls and self._get_dunder_method(cls, dunder):
                self._emit(f"{target_str} = {target_str}.{dunder_go}()")
                return

        self._emit(f"{target_str}{go_op}")

    def _get_augassign_op(self, node) -> str:
        if isinstance(node, Tree) and node.data == "augassign_op":
            return str(node.children[0])
        return str(node) if isinstance(node, Token) else "+="

    # ─── Expression statement ──────────────────────────────────

    def _visit_expr_stmt(self, node: Tree):
        expr_str = self._expr_to_go(node.children[0])
        if expr_str:
            self._emit(expr_str)

    # ─── Return ────────────────────────────────────────────────

    def _visit_return_stmt(self, node: Tree):
        in_except = self._in_except_handler
        in_try = self._in_try_func
        in_try_iife = self._in_try_iife

        if node.children and node.children[0] is not None:
            val_node = node.children[0]
            if isinstance(val_node, Tree) and val_node.data == "testlist_tuple":
                vals = [self._expr_to_go(c) for c in val_node.children if c is not None]
                val = ", ".join(vals)
            elif isinstance(val_node, Tree) and val_node.data == "test" and len(val_node.children) == 3:
                true_val = self._expr_to_go(val_node.children[0])
                cond = self._expr_to_go(val_node.children[1])
                false_val = self._expr_to_go(val_node.children[2])
                self._emit(f"if {cond} {{")
                self.indent += 1
                self._emit(f"return {true_val}")
                self.indent -= 1
                self._emit(f"}} else {{")
                self.indent += 1
                self._emit(f"return {false_val}")
                self.indent -= 1
                self._emit(f"}}")
                return
            else:
                val = self._expr_to_go(val_node)

            if self._in_async_func and self._async_chan_name:
                self._emit(f"{self._async_chan_name} <- {val}")
                self._emit("return")
            elif in_try_iife:
                # Inside the IIFE wrapping a try-with-catch: rewrite
                # ``return X`` to (set retval if typed) +
                # __lamShouldReturn = true + bare-return-from-IIFE.
                # The post-IIFE check forwards the signal to the
                # outer function.
                if in_try:
                    self._emit(f"retval = {val}")
                self._emit("__lamShouldReturn = true")
                self._emit("return")
            elif in_except and in_try:
                self._emit(f"retval = {val}")
                self._emit("return")
            else:
                self._emit(f"return {val}")
        else:
            if in_try_iife:
                self._emit("__lamShouldReturn = true")
            self._emit("return")

    # ─── Simple flow statements ────────────────────────────────

    def _visit_pass_stmt(self, _):
        self._emit("// pass")

    def _visit_break_stmt(self, _):
        if self._while_else_break or self._for_else_break:
            self._emit("_whileBreak = true")
        self._emit("break")

    def _visit_continue_stmt(self, _):
        self._emit("continue")

    def _visit_yield_stmt(self, node: Tree):
        if node.children:
            val = self._expr_to_go(node.children[0])
            if val:
                self._emit(val)

    def _visit_del_stmt(self, node: Tree):
        if node.children:
            target = node.children[0]
            if isinstance(target, Tree) and target.data == "getitem":
                obj = self._expr_to_go(target.children[0])
                idx = self._expr_to_go(target.children[1])
                self._emit(f"delete({obj}, {idx})")
                return
            target_str = self._expr_to_go(target)
            self._emit(f"{target_str} = nil")

    def _visit_global_stmt(self, node: Tree):
        for child in node.children:
            name = self._get_name(child) if isinstance(child, Tree) else str(child) if isinstance(child, Token) else None
            if name:
                self._declare_var(name)

    def _visit_nonlocal_stmt(self, _):
        self._emit("// nonlocal (captured by closure)")

    def _visit_assert_stmt(self, node: Tree):
        cond = self._expr_to_go(node.children[0])
        if len(node.children) > 1 and node.children[1] is not None:
            msg = self._expr_to_go(node.children[1])
            self._emit(f'if !({cond}) {{ panic({msg}) }}')
        else:
            self._emit(f'if !({cond}) {{ panic("assertion failed") }}')

    def _visit_raise_stmt(self, node: Tree):
        if node.children and node.children[0] is not None:
            val = self._expr_to_go(node.children[0])
            self._emit(f"panic({val})")
            return
        # Bare ``raise`` / ``throw`` inside a ``catch`` block should
        # re-raise the exception the catch just recovered, so an outer
        # ``try`` sees the original value instead of a synthetic
        # placeholder. ``r`` is the recover binding introduced by the
        # enclosing ``if r := recover(); r != nil`` guard.
        if self._in_recover_block:
            self._emit("panic(r)")
            return
        # Fallback for a bare ``raise`` outside any catch — there's no
        # active panic to re-raise, so emit a distinctive sentinel.
        self._emit('panic("re-raised")')

    def _visit_defer_stmt(self, node: Tree):
        """`defer expr` — schedules ``expr`` to run on function exit.

        ``expr`` must be a call (Go's ``defer`` only accepts a function
        or method invocation). If the user hands us a bare value we
        wrap it in a no-argument closure so the Go compiler still
        accepts it, matching Go's own ``defer func() { ... }()`` idiom.
        """
        if not node.children or node.children[0] is None:
            return
        expr_node = node.children[0]
        go_expr = self._expr_to_go(expr_node)
        # Wrap non-call expressions so Go doesn't complain.
        if not (isinstance(expr_node, Tree) and expr_node.data == "funccall"):
            self._emit(f"defer func() {{ _ = {go_expr} }}()")
        else:
            self._emit(f"defer {go_expr}")

    # ─── If / Elif / Else ──────────────────────────────────────

    def _visit_if_stmt(self, node: Tree):
        cond = self._expr_to_go(node.children[0])
        self._emit(f"if {cond} {{")
        self.indent += 1
        self._push_scope()
        self._visit_suite(node.children[1])
        self._pop_scope()
        self.indent -= 1

        elifs = node.children[2]
        if isinstance(elifs, Tree) and elifs.data == "elifs":
            for elif_node in elifs.children:
                if isinstance(elif_node, Tree) and elif_node.data == "elif_":
                    econd = self._expr_to_go(elif_node.children[0])
                    self._emit(f"}} else if {econd} {{")
                    self.indent += 1
                    self._push_scope()
                    self._visit_suite(elif_node.children[1])
                    self._pop_scope()
                    self.indent -= 1

        else_suite = node.children[3] if len(node.children) > 3 else None
        if else_suite is not None and isinstance(else_suite, Tree):
            self._emit("} else {")
            self.indent += 1
            self._push_scope()
            self._visit_suite(else_suite)
            self._pop_scope()
            self.indent -= 1
        self._emit("}")

    # ─── While ─────────────────────────────────────────────────

    def _visit_while_stmt(self, node: Tree):
        cond = self._expr_to_go(node.children[0])
        has_else = len(node.children) > 2 and node.children[2] is not None
        if has_else:
            self._emit(f"_whileBreak := false")
        self._emit(f"for {cond} {{")
        self.indent += 1
        self._push_scope()
        with self._scoped(_while_else_break=has_else):
            self._visit_suite(node.children[1])
        self._pop_scope()
        self.indent -= 1
        self._emit("}")
        if has_else:
            self._emit("if !_whileBreak {")
            self.indent += 1
            self._push_scope()
            self._visit_suite(node.children[2])
            self._pop_scope()
            self.indent -= 1
            self._emit("}")

    # ─── For ───────────────────────────────────────────────────

    def _visit_for_stmt(self, node: Tree):
        target_node = node.children[0]
        iterable_node = node.children[1]
        suite_node = node.children[2]
        else_suite = node.children[3] if len(node.children) > 3 and node.children[3] is not None else None

        if isinstance(target_node, Tree):
            if target_node.data in ("typed_for_target", "untyped_for_target"):
                var_name = self._expr_to_go(target_node.children[0])
            else:
                var_name = self._expr_to_go(target_node)
        else:
            var_name = str(target_node)

        if else_suite:
            self._emit("_whileBreak := false")

        enumerate_info = self._check_enumerate_call(iterable_node)
        range_info = self._check_range_call(iterable_node)
        already_declared = var_name in self.declared_vars
        if enumerate_info:
            idx_name, val_name = self._split_tuple_target(target_node)
            iter_str = enumerate_info
            if idx_name and val_name:
                self._emit(f"for {idx_name}, {val_name} := range {iter_str} {{")
                self._declare_var(idx_name)
                self._declare_var(val_name)
            else:
                self._emit(f"for {var_name} := range {iter_str} {{")
                self._declare_var(var_name)
            self.indent += 1
            self._push_scope()
            with self._scoped(_for_else_break=bool(else_suite)):
                self._visit_suite(suite_node)
            self._pop_scope()
            self.indent -= 1
            self._emit("}")
            if else_suite:
                self._emit("if !_whileBreak {")
                self.indent += 1
                self._push_scope()
                self._visit_suite(else_suite)
                self._pop_scope()
                self.indent -= 1
                self._emit("}")
            return
        key_name, val_name = (None, None)
        if range_info:
            start, end, step = range_info
            is_negative_step = step.lstrip().startswith("-") or step.lstrip().startswith("(−") or step.lstrip().startswith("(-")
            cmp_op = ">" if is_negative_step else "<"
            assign_op = "=" if already_declared else ":="
            self._emit(f"for {var_name} {assign_op} {start}; {var_name} {cmp_op} {end}; {var_name} += {step} {{")
        else:
            iter_str = self._expr_to_go(iterable_node)
            is_chan = self._is_generator_call(iterable_node)
            # ``for k, v in mapping`` — when the for-target is a
            # 2-tuple, range over the map directly so both names bind
            # idiomatically. Without this, the catch-all branch below
            # would emit ``for _, "k, v" := range`` (a Go syntax
            # error) because ``var_name`` carries the literal
            # comma-joined expression.
            if not is_chan:
                key_name, val_name = self._split_tuple_target(target_node)
            if is_chan:
                assign_op = "=" if already_declared else ":="
                self._emit(f"for {var_name} {assign_op} range {iter_str} {{")
            elif key_name and val_name:
                k_decl = key_name in self.declared_vars
                v_decl = val_name in self.declared_vars
                assign_op = "=" if (k_decl and v_decl) else ":="
                self._emit(f"for {key_name}, {val_name} {assign_op} range {iter_str} {{")
            elif already_declared:
                self._emit(f"for _, {var_name} = range {iter_str} {{")
            else:
                self._emit(f"for _, {var_name} := range {iter_str} {{")

        self.indent += 1
        self._push_scope()
        if key_name and val_name:
            self._declare_var(key_name)
            self._declare_var(val_name)
        else:
            self._declare_var(var_name)
        with self._scoped(_for_else_break=bool(else_suite)):
            self._visit_suite(suite_node)
        self._pop_scope()
        self.indent -= 1
        self._emit("}")
        if else_suite:
            self._emit("if !_whileBreak {")
            self.indent += 1
            self._push_scope()
            self._visit_suite(else_suite)
            self._pop_scope()
            self.indent -= 1
            self._emit("}")

    def _check_range_call(self, node) -> Optional[Tuple[str, str, str]]:
        if not isinstance(node, Tree) or node.data != "funccall":
            return None
        func = node.children[0]
        args_node = node.children[1] if len(node.children) > 1 else None
        if isinstance(func, Tree) and func.data == "var":
            name = self._get_name(func.children[0])
            if name == "range":
                al = self._get_call_args(args_node)
                if len(al) == 1:
                    return ("0", al[0], "1")
                if len(al) == 2:
                    return (al[0], al[1], "1")
                if len(al) == 3:
                    return (al[0], al[1], al[2])
        return None

    def _check_enumerate_call(self, node) -> Optional[str]:
        if not isinstance(node, Tree) or node.data != "funccall":
            return None
        func = node.children[0]
        args_node = node.children[1] if len(node.children) > 1 else None
        if isinstance(func, Tree) and func.data == "var":
            name = self._get_name(func.children[0])
            if name == "enumerate":
                al = self._get_call_args(args_node)
                if al:
                    return al[0]
        return None

    def _is_generator_call(self, node) -> bool:
        if not isinstance(node, Tree) or node.data != "funccall":
            return False
        func = node.children[0]
        if isinstance(func, Tree) and func.data == "var":
            name = self._get_name(func.children[0])
            return name in self._generator_functions
        return False

    def _split_tuple_target(self, target_node):
        if isinstance(target_node, Tree):
            if target_node.data in ("typed_for_target", "untyped_for_target"):
                inner = target_node.children[0]
                return self._split_tuple_target(inner)
            elif target_node.data in ("tuple", "exprlist"):
                parts = [self._expr_to_go(c) for c in target_node.children if c is not None]
                if len(parts) == 2:
                    return parts[0], parts[1]
        return None, None

    # ─── Try / Except / Finally ────────────────────────────────

    def _visit_try_stmt(self, node: Tree):
        try_suite = node.children[0]
        catch_clauses_node = None
        else_suite = None
        finally_node = None

        for child in node.children[1:]:
            if child is None:
                continue
            if isinstance(child, Tree):
                if child.data == "catch_clauses":
                    catch_clauses_node = child
                elif child.data == "finally":
                    finally_node = child
                elif child.data == "suite":
                    else_suite = child

        # Catch-less try (try/finally only) keeps the legacy
        # function-scoped defer pattern: there's no recovery to
        # short-circuit, so wrapping in an IIFE would only complicate
        # variable scoping for no behavioural gain.
        if catch_clauses_node is None:
            if finally_node is not None:
                self._emit("defer func() {")
                self.indent += 1
                self._push_scope()
                self._visit_suite(finally_node.children[0])
                self._pop_scope()
                self.indent -= 1
                self._emit("}()")
            self._emit("// try")
            self._push_scope()
            self._visit_suite(try_suite)
            self._pop_scope()
            return

        # Try-with-catch: wrap the try body + catch dispatch in an
        # IIFE so a successful recover unwinds *into the IIFE*, the
        # IIFE returns normally, and the surrounding function keeps
        # executing. ``__lamShouldReturn`` is the function-level
        # sentinel that lets ``return X`` inside the IIFE still exit
        # the outer function (the value lands in ``retval`` if the
        # function has a typed return; otherwise the bare return is
        # enough).
        # ``__lamShouldReturn`` / ``__lamPanicked`` are per-function
        # sentinels shared across every ``try``-with-``catch`` block.
        # On the first try we introduce them with ``:=``; on every
        # subsequent try in the *same function* we just reset them
        # with ``=``, since Go forbids redeclaring an existing
        # local. Without this gate, writing two independent
        # ``try/catch`` blocks one after the other produced
        # ``no new variables on left side of :=``.
        self._emit("// try")
        first_try = "__lamShouldReturn" not in self.declared_vars
        op = ":=" if first_try else "="
        self._emit(f"__lamShouldReturn {op} false")
        self._declare_var("__lamShouldReturn")
        self._emit(f"__lamPanicked {op} false")
        self._declare_var("__lamPanicked")
        self._emit("_ = __lamShouldReturn")
        self._emit("_ = __lamPanicked")
        self._emit("func() {")
        self.indent += 1
        self._push_scope()

        # Defers run LIFO — registering the finally first means the
        # catch's recover runs first when a panic unwinds, which is
        # the correct semantics. If the catch re-panics (no clause
        # matched, etc.) the finally still runs because of the
        # second defer.
        if finally_node is not None:
            self._emit("defer func() {")
            self.indent += 1
            self._push_scope()
            self._visit_suite(finally_node.children[0])
            self._pop_scope()
            self.indent -= 1
            self._emit("}()")

        self._emit("defer func() {")
        self.indent += 1
        self._emit("if r := recover(); r != nil {")
        self.indent += 1
        self._emit("__lamPanicked = true")
        self._push_scope()
        self._declare_var("r")
        # Catch bodies are also inside the IIFE: flip ``_in_try_iife``
        # so ``return X`` from a catch handler also funnels through
        # ``__lamShouldReturn`` and propagates to the outer function.
        # ``_in_recover_block`` is flipped too so a bare ``raise``
        # inside the catch re-panics the live ``r`` value instead of
        # emitting a placeholder string.
        with self._scoped(_in_try_iife=True, _in_recover_block=True):
            for i, clause in enumerate(catch_clauses_node.children):
                if isinstance(clause, Tree) and clause.data == "catch_clause":
                    self._emit_catch_clause(clause, i == 0)
        self._pop_scope()
        self.indent -= 1
        self._emit("}")
        self.indent -= 1
        self._emit("}()")

        # Try body — inside the IIFE, with ``_in_try_iife`` flipped
        # so ``return X`` rewrites to (``retval = X;``)
        # ``__lamShouldReturn = true; return``. The bare return
        # exits this IIFE only; the post-IIFE check forwards the
        # signal to the outer function.
        with self._scoped(_in_try_iife=True):
            self._visit_suite(try_suite)

        self._pop_scope()
        self.indent -= 1
        self._emit("}()")

        # ``else`` runs only when no panic was caught — Python
        # semantics. Catch handlers that re-panic skip the else
        # branch because the IIFE's recover sets ``__lamPanicked``.
        if else_suite is not None:
            self._emit("if !__lamPanicked {")
            self.indent += 1
            self._push_scope()
            self._visit_suite(else_suite)
            self._pop_scope()
            self.indent -= 1
            self._emit("}")

        # Propagate any return signalled from inside the IIFE.
        # When the function has a typed return value the named
        # ``retval`` already holds the value; a bare ``return``
        # surfaces it.
        self._emit("if __lamShouldReturn { return }")

    def _visit_try_finally(self, node: Tree):
        try_suite = node.children[0]
        finally_node = node.children[1] if len(node.children) > 1 else None

        if finally_node and isinstance(finally_node, Tree):
            self._emit("defer func() {")
            self.indent += 1
            self._push_scope()
            fin_suite = finally_node.children[0] if finally_node.data == "finally" else finally_node
            self._visit_suite(fin_suite)
            self._pop_scope()
            self.indent -= 1
            self._emit("}()")

        self._emit("// try")
        self._push_scope()
        self._visit_suite(try_suite)
        self._pop_scope()

    def _emit_catch_clause(self, clause: Tree, is_first: bool):
        exception_type = None
        as_name = None
        suite = None

        for child in clause.children:
            if child is None:
                continue
            if isinstance(child, Tree):
                if child.data == "suite":
                    suite = child
                elif child.data == "var":
                    exception_type = self._expr_to_go(child)
                elif child.data == "name":
                    if exception_type is not None:
                        as_name = self._get_name(child)
                    else:
                        exception_type = self._get_name(child)
                else:
                    if exception_type is None:
                        exception_type = self._expr_to_go(child)
            elif isinstance(child, Token):
                if child.type == "NAME":
                    if exception_type is not None and as_name is None:
                        as_name = str(child)
                    elif exception_type is None:
                        exception_type = str(child)

        # Bind the caught panic value as-is (``interface{}``) rather
        # than stringifying it eagerly. This preserves the original
        # object so handlers can:
        #
        #   * ``print(err)`` / ``f"saw: {err}"`` — still works because
        #     ``fmt`` follows ``Stringer`` on the live value (so a
        #     thrown ``MyError(42, "boom")`` with a ``__str__``
        #     renders the same way an eager ``fmt.Sprintf("%v", r)``
        #     bind used to produce).
        #   * ``ve: ValidationError = err`` — the existing annassign
        #     unboxer in ``_typed_value_to_go`` inserts a
        #     ``.(*ValidationError)`` assertion on the rvalue, so the
        #     user can then reach ``ve.field`` / ``ve.reason``.
        #
        # The previous ``fmt.Sprintf("%v", r)`` bind flattened the
        # exception to a ``string`` at the catch site, which made
        # field access impossible without a manual type assertion.
        if as_name:
            self._emit(f'{as_name} := r')
            self._emit(f'_ = {as_name}')
            # Track the binding as ``interface{}`` so downstream
            # ``local: ClassName = {as_name}`` annassigns trigger the
            # class-pointer coercion path.
            self._var_go_types[as_name] = "interface{}"
        elif exception_type and exception_type[0].islower() and exception_type not in PYTHON_EXCEPTIONS:
            as_name = exception_type
            self._emit(f'{as_name} := r')
            self._emit(f'_ = {as_name}')
            self._var_go_types[as_name] = "interface{}"

        if suite:
            self._push_scope()
            if as_name:
                self._declare_var(as_name)
            with self._scoped(_in_except_handler=True):
                self._visit_suite(suite)
            self._pop_scope()

    # ─── Match / Switch ────────────────────────────────────────

    def _visit_match_stmt(self, node: Tree):
        subject = self._expr_to_go(node.children[0])
        self._emit(f"switch {subject} {{")
        for child in node.children[1:]:
            if isinstance(child, Tree) and child.data == "case":
                self._visit_case(child)
        self._emit("}")

    def _visit_case(self, node: Tree):
        pattern = node.children[0]
        guard = node.children[1]
        suite = node.children[2]

        if isinstance(pattern, Tree) and pattern.data == "any_pattern":
            self._emit("default:")
        else:
            pat_str = self._pattern_to_go(pattern)
            self._emit(f"case {pat_str}:")
        self.indent += 1
        self._push_scope()
        self._visit_suite(suite)
        self._pop_scope()
        self.indent -= 1

    def _pattern_to_go(self, node) -> str:
        if not isinstance(node, Tree):
            return str(node) if isinstance(node, Token) else "/* unknown */"
        d = node.data
        if d == "literal_pattern":
            return self._pattern_to_go(node.children[0])
        if d == "number":
            return self._number_to_go(node)
        if d == "string":
            return str(node.children[0])
        if d == "const_none":
            return "nil"
        if d == "const_true":
            return "true"
        if d == "const_false":
            return "false"
        if d == "capture_pattern":
            return str(node.children[0])
        if d == "any_pattern":
            return "default"
        return "/* unknown pattern */"

    # ─── do { } catch err { } ─────────────────────────────────

    def _visit_do_stmt(self, node: Tree):
        """Lower ``do { body } catch err { handler }``.

        The body is wrapped in a Go IIFE returning ``*Result``. Inside
        that closure, ``?`` returns ``*Result`` to the closure rather
        than to the enclosing function — so a propagated error pops
        out as the IIFE's value and we can inspect it locally:

            __rdoN := func() *Result {
                <body>
                return Result_Ok(nil)   // implicit success tail
            }()
            if __rdoN.Error != nil {
                err := __rdoN.Error
                <handler>
            }

        ``Result`` and ``Result_Ok`` come from ``lib/lamerrors.lam``;
        the user must ``from lamerrors import Result`` for the
        emitted Go to compile (we don't auto-inject the import — the
        Go compiler's "undefined: Result" message is clear enough).
        """
        body_suite = node.children[0]
        err_name_node = node.children[1]
        handler_suite = node.children[2]
        err_name = self._get_name(err_name_node)

        self._do_counter += 1
        rdo = f"__rdo{self._do_counter}"

        self._emit(f"{rdo} := func() *Result {{")
        self.indent += 1
        # Push a fresh declared-vars scope so locals introduced inside
        # the IIFE (including ``__qN`` temps) don't leak back into the
        # surrounding function — and so a *second* ``do`` block re-
        # declares its own ``var x int = ...`` rather than treating
        # ``x`` as already declared from a previous IIFE.
        self._push_scope()
        try:
            # Inside the ``do { }`` IIFE, ``?`` propagates to the IIFE
            # itself (which returns ``*Result``), not the enclosing
            # function — so ``_q_propagate_ok`` is True here even if
            # the outer function returns something non-Result.
            with self._scoped(_q_propagate_ok=True):
                if isinstance(body_suite, Tree) and body_suite.data == "suite":
                    for stmt in body_suite.children:
                        if isinstance(stmt, Tree):
                            self._visit(stmt)
                # Implicit success-tail: if control reaches the end of the
                # body without ``?`` short-circuiting, we synthesise an
                # ``Ok(nil)`` so the IIFE always returns a non-nil ``*Result``.
                self._emit("return Result_Ok(nil)")
        finally:
            self._pop_scope()
        self.indent -= 1
        self._emit("}()")

        self._emit(f"if {rdo}.Error != nil {{")
        self.indent += 1
        # Handler scope: ``err_name`` lives only here, plus any locals
        # the handler introduces.
        self._push_scope()
        try:
            self._emit(f"{err_name} := {rdo}.Error")
            self._declare_var(err_name)
            if isinstance(handler_suite, Tree) and handler_suite.data == "suite":
                for stmt in handler_suite.children:
                    if isinstance(stmt, Tree):
                        self._visit(stmt)
        finally:
            self._pop_scope()
        self.indent -= 1
        self._emit("}")

    # ─── With ──────────────────────────────────────────────────

    def _visit_with_stmt(self, node: Tree):
        with_items = node.children[0]
        suite = node.children[1]

        if isinstance(with_items, Tree) and with_items.data == "with_items":
            for item in with_items.children:
                if isinstance(item, Tree) and item.data == "with_item":
                    expr = item.children[0]
                    as_name = item.children[1] if len(item.children) > 1 and item.children[1] is not None else None
                    expr_str = self._expr_to_go(expr)
                    if as_name:
                        name_str = self._get_name(as_name)
                        self._emit(f"{name_str} := {expr_str}")
                        self._emit(f"defer {name_str}.Close()")
                    else:
                        self._emit(f"_withRes := {expr_str}")
                        self._emit(f"defer _withRes.Close()")

        self._emit("{")
        self.indent += 1
        self._push_scope()
        self._visit_suite(suite)
        self._pop_scope()
        self.indent -= 1
        self._emit("}")

    # ─── Decorated ─────────────────────────────────────────────

    def _visit_decorated(self, node: Tree):
        for child in node.children:
            if isinstance(child, Tree) and child.data in ("funcdef", "classdef"):
                self._visit(child)

    # ─── Go! block (preprocessed) ──────────────────────────────

    def _visit_go_block(self, node: Tree):
        pass
