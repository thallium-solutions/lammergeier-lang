#!/usr/bin/env python3
"""Statement visitor methods for the transpiler."""

from __future__ import annotations
import re
from lark import Tree, Token

from compiler import narrowing as _narrowing
from typing import Optional, Tuple, List
from compiler.constants import PYTHON_EXCEPTIONS, generator_go_elem
from compiler.typesys import render_type as render_type_text


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
                        head = das.children[0]
                        if isinstance(head, Tree) and head.data in {"scoped_name", "relative_module"}:
                            name = str(head.children[0]) if head.children else ""
                        else:
                            name = self._dotted_name_to_str(head)
                        if name:
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
        self._visit_stmt_list([c for c in node.children if isinstance(c, Tree)])

    def _visit_stmt_list(self, stmts: List[Tree]) -> None:
        for index, child in enumerate(stmts):
            self._visit(child)
            if child.data != "if_stmt" or index + 1 >= len(stmts):
                continue
            # ``if not isinstance(x, T) { return }``: the rest of the block
            # sees ``x`` as a ``T``, in a Go block holding the shadow.
            rest = stmts[index + 1:]
            plan = self._nr_plan(_narrowing.guard_facts(child), rest)
            if not plan:
                continue
            self._emit("{")
            self.indent += 1
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            self._nr_emit_binds(plan)
            with self._nr_registered(plan):
                self._visit_stmt_list(rest)
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
            self.indent -= 1
            self._emit("}")
            return

    def _visit_simple_stmt(self, node: Tree):
        for child in node.children:
            if isinstance(child, Tree):
                self._visit(child)

    # ─── Assignment ────────────────────────────────────────────

    def _visit_assign_stmt(self, node: Tree):
        self._visit(node.children[0])

    def _is_declared_or_nonlocal_target(self, target_str: str) -> bool:
        return (
            "." in target_str
            or self._is_static_var_go_name(target_str)
            or target_str in self.declared_vars
        )

    def _emit_decl_or_assign(self, target_str: str, go_type: str, value_str: str):
        if self._is_declared_or_nonlocal_target(target_str):
            self._emit(f"{target_str} = {value_str}")
        else:
            self._emit(f"var {target_str} {go_type} = {value_str}")
            self._declare_var(target_str)

    def _record_inferred_target_type(self, target_str: str, value_node) -> None:
        if not target_str or target_str == "_" or "." in target_str or "[" in target_str:
            return
        class_name = self._infer_receiver_class(value_node)
        if class_name and class_name in self._interfaces and class_name not in self._class_names:
            # ``conn = requestDb()`` with ``requestDb() -> Sql``: the local
            # is the interface (its methods' results are known).
            if target_str not in self.declared_vars or self._var_types.get(target_str) == class_name:
                self._var_types[target_str] = class_name
                self._var_go_types[target_str] = self._go_class_name(class_name)
            return
        if not class_name or class_name not in self._class_names:
            return
        if target_str in self.declared_vars and self._var_go_types.get(target_str) == "interface{}":
            # ``last: any``; ``last = b``: the variable stays an ``any`` (a
            # later ``last = r.error`` must not be asserted to ``*Box``).
            return
        declared = self._var_types.get(target_str, "")
        if declared and target_str in self.declared_vars and self._hier_is_subclass(class_name, declared):
            # ``shape = Circle(2)`` keeps the declared base type.
            return
        self._var_types[target_str] = class_name
        self._var_go_types[target_str] = f"*{self._go_class_name(class_name)}"

    _LAM_TYPE_NODE_CACHE: dict = {}

    def _lam_type_text_node(self, type_text: str):
        """Parse a Lam type spelled as text (``dict[str, int]``) into the
        grammar's type node, or ``None``."""
        cache = StatementVisitorMixin._LAM_TYPE_NODE_CACHE
        if type_text in cache:
            return cache[type_text]
        result = None
        try:
            from compiler.lammergeier import create_parser as _cp
            tree = _cp().parse(f"lamInferredT: {type_text} = 0;\n")
            for sub in tree.iter_subtrees():
                if sub.data == "annassign":
                    result = next(
                        (c for c in sub.children if isinstance(c, Tree) and c.data in {"type_expr", "type_union", "type_name", "type_generic", "type_func"}),
                        None,
                    )
                    break
        except Exception:
            result = None
        cache[type_text] = result
        return result

    def _semantic_inferred_go_type(self, node: Tree):
        """``(type_node, go_type)`` the semantic checker inferred for the
        first plain assignment ``node`` (``name = value``), or ``None``
        when unknown or not concrete enough to declare."""
        type_text = getattr(node, "_lam_inferred_type", "")
        if not type_text or re.search(r"\b(any|unknown|object|None|T\d*)\b", type_text):
            return None
        # A class the checker knows under a name this module does not bind
        # (``HttpProblem`` returned by another module's method, a
        # ``kit.Problem`` it shadows): no declaration, Go infers it.
        known = self._class_names | self._interfaces | set(self._generic_names) | {"Result", "Error"}
        if any(
            (name[0].isupper() or "." in name) and name not in known
            for name in re.findall(r"[A-Za-z_][\w.]*", type_text)
        ):
            return None
        type_node = self._lam_type_text_node(type_text)
        if type_node is None:
            return None
        go_type = self._type_expr_to_go(type_node)
        if not go_type or "interface{}" in go_type:
            return None
        return type_node, go_type

    def _bare_generic_local_go_type(self, node: Tree, go_type: str, value):
        """``(go_type, type_node)`` of a local annotated with a bare generic
        class (``it: Iter``), from the semantic checker's inferred type or
        the initializer's Go type; ``None`` otherwise."""
        bare = go_type[1:] if go_type.startswith("*") else ""
        cls = next(
            (name for name in self._class_names
             if self._go_class_name(name) == bare and self._generic_classes.get(name)),
            "",
        )
        if not cls:
            return None
        type_text = getattr(node, "_lam_inferred_type", "")
        type_node = self._lam_type_text_node(type_text) if type_text else None
        candidate = self._type_expr_to_go(type_node) if type_node is not None else ""
        if not candidate.startswith(go_type + "["):
            candidate = self._expr_go_type_hint(value) or ""
            type_node = None
        if not candidate.startswith(go_type + "["):
            return None
        return candidate, type_node if type_node is not None else node.children[1]

    def _expected_value_lam_for(self, value, type_node):
        """``(call_node, lam_type)``: the call that is the value of an
        annotated assignment (``r: Result[int] = make()``, or the operand
        of ``n: int = make()?``, expected to be a ``Result[int]``) and the
        Lam type it must have; it binds type parameters nothing else
        determines (``_lam_generic_call_subst``). ``None`` otherwise."""
        from compiler.typesys import GenericType
        lam_type = self._parsed_lam_type(type_node)
        if lam_type is None or not isinstance(value, Tree):
            return None
        if value.data in {"list", "set", "dict"}:
            # ``xs: list[Box[int]] = [Box([])]``: the items' calls expect
            # the item type.
            self._note_expected_call_lam(value, lam_type)
            return None
        if value.data == "propagate" and value.children:
            value = value.children[0]
            lam_type = GenericType("Result", (lam_type,))
        if value.data != "funccall":
            return None
        return (value, lam_type)

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

        target_str = self._assign_target_to_go(target)
        go_type = self._type_expr_to_go(type_node)
        if value is not None:
            # ``it: Iter = Iter.range_(0, 3)``: a bare generic class takes
            # the initializer's type arguments (``*Iter[int]``).
            inferred_generic = self._bare_generic_local_go_type(node, go_type, value)
            if inferred_generic:
                go_type, type_node = inferred_generic
        overload_type_key = self._overload_return_key(type_node)
        self._check_int_true_division(value, go_type)
        if value is not None:
            from compiler.constants import lam_simple_expr_text
            self._check_literal_go_shape(
                value, go_type,
                f"cannot assign `{{actual}}` to `{{expected}}` variable `{lam_simple_expr_text(target) or '<target>'}`",
            )

        raw_type = self._get_raw_type_name(type_node)
        if raw_type and raw_type in (self._class_names | self._interfaces) and "." not in target_str:
            # A class or an interface (``n: Named``): method calls on it
            # use the declared signature (defaults, keyword arguments).
            self._var_types[target_str] = raw_type
        elif "." not in target_str:
            # ``out: str`` in one method must not inherit ``out: Box`` from
            # another (the registry is keyed by name).
            self._var_types.pop(target_str, None)

        if go_type and "." not in target_str:
            self._var_go_types[target_str] = go_type
            self._record_lam_type(target_str, type_node)
            self._note_result_payload(target_str, type_node)
            tuple_types = self._list_tuple_element_types(type_node)
            if tuple_types:
                self._tuple_iterable_element_types[target_str] = tuple_types

        if is_private and "." not in target_str:
            target_str = self._go_private_name(target_str)
        if self._at_top_level and target_str in self._link_const_go_names:
            # Module-level variable namespaced by ``compiler/namespacing.py``.
            target_str = self._link_const_go_names[target_str]

        if value is not None:
            if (
                isinstance(value, Tree) and value.data == "test" and len(value.children) == 3
                and not self._nr_ternary_narrows(value)
                and not (self._holds_propagate(value.children[0]) or self._holds_propagate(value.children[2]))
            ):
                with self._scoped(_expected_expr_type_key=overload_type_key):
                    true_val = self._typed_value_to_go(value.children[0], go_type, overload_type_key)
                    cond = self._expr_to_go(value.children[1])
                    false_val = self._typed_value_to_go(value.children[2], go_type, overload_type_key)
                if not self._is_declared_or_nonlocal_target(target_str):
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
            with self._scoped(
                _propagate_cast_hint=go_type,
                _expected_expr_type_key=overload_type_key,
                _expected_value_lam=self._expected_value_lam_for(value, type_node),
            ):
                value_str = self._typed_value_to_go(value, go_type, overload_type_key)
            self._emit_decl_or_assign(target_str, go_type, value_str)
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

        target_str = self._link_const_go_names.get(
            self._get_name(name_node), self._go_binding_name(self._get_name(name_node))
        )
        if type_node is not None:
            go_type = self._type_expr_to_go(type_node)
        else:
            go_type = self._infer_type_from_value(value_node) if value_node is not None else ""

        # Track for downstream type-driven decisions (e.g. method
        # dispatch, ternary handling) — the same registries
        # ``annassign`` populates.
        raw_type = self._get_raw_type_name(type_node) if type_node is not None else ""
        if raw_type and raw_type in (self._class_names | self._interfaces):
            self._var_types[target_str] = raw_type
        elif type_node is not None:
            self._var_types.pop(target_str, None)
        if go_type:
            self._var_go_types[target_str] = go_type

        if type_node is None and value_node is not None:
            self._record_inferred_target_type(target_str, value_node)
        value_str = self._typed_value_to_go(value_node, go_type) if value_node is not None else ""
        if go_type:
            self._emit(f"var {target_str} {go_type} = {value_str}")
        else:
            # No type info anywhere — let Go infer.
            self._emit(f"{target_str} := {value_str}")
        self._declare_var(target_str)

    def _assign_target_to_go(self, target) -> str:
        """Go name of an assignment target. A plain name is a variable even
        when a function of the program has that name (``one: Server = ...``
        next to ``func one()``): its declaration and its uses must agree."""
        if isinstance(target, Tree) and target.data == "var" and target.children:
            name = self._get_name(target.children[0])
            go_name = self._go_binding_name(name)
            if (
                name in self._user_functions and go_name not in self.declared_vars
                and name not in self._link_const_go_names and not self._parent_alias_expr(name)
            ):
                return go_name
        return self._expr_to_go(target)

    def _visit_assign(self, node: Tree):
        parts = [c for c in node.children if c is not None]
        if len(parts) >= 2:
            target_node = parts[0]
            value_node = parts[-1]
            if isinstance(target_node, Tree) and target_node.data == "getitem":
                obj_node, index_node = target_node.children[0], target_node.children[1]
                if self._expr_go_type_hint(obj_node) == "LamJSON":
                    value_str = self._typed_value_to_go(value_node, "LamJSON")
                    self._emit(f"lamJSONSet({self._expr_to_go(obj_node)}, {self._expr_to_go(index_node)}, {value_str})")
                    return
                class_name = self._infer_expr_class(obj_node)
                method = self._get_dunder_method(class_name, "__setitem__") if class_name else ""
                if method:
                    method_key = f"{class_name}.__setitem__"
                    param_types = self._func_param_go_types.get(method_key, [])
                    key_type = param_types[0] if param_types else "interface{}"
                    value_type = param_types[1] if len(param_types) > 1 else "interface{}"
                    key_str = self._typed_value_to_go(index_node, key_type)
                    value_str = self._typed_value_to_go(value_node, value_type)
                    self._emit(f"{self._expr_to_go(obj_node)}.{method}({key_str}, {value_str})")
                    return
            if isinstance(target_node, Tree) and target_node.data == "getattr":
                if len(parts) == 2:
                    # ``self.items = make()``: the field's declared type
                    # binds ``make``'s ``T``.
                    self._note_expected_call_lam(value_node, self._field_expected_lam(target_node))
                obj_node = target_node.children[0]
                if self._expr_go_type_hint(obj_node) == "LamJSON":
                    attr = self._get_name(target_node.children[1])
                    value_str = self._typed_value_to_go(value_node, "LamJSON")
                    self._emit(f'lamJSONSet({self._expr_to_go(obj_node)}, "{attr}", {value_str})')
                    return
            if isinstance(target_node, Tree) and target_node.data == "getitem" and len(parts) == 2 and target_node.children:
                # ``d[k] = Box([])`` / ``xs[i] = make()``: the item type of
                # the collection binds the call's type parameters.
                self._note_expected_go_item(
                    value_node, self._expr_go_type_hint(target_node) or "", target_node.children[0],
                )
            with self._scoped(_index_target=True):
                target_str = self._assign_target_to_go(target_node)
            self._record_inferred_target_type(target_str, value_node)
            if self._emit_semantic_typed_assign(node, target_node, target_str, value_node):
                return
            target_go_type = self._expr_go_type_hint(parts[0])
            # Ternary in plain assign
            if (
                isinstance(value_node, Tree) and value_node.data == "test" and len(value_node.children) == 3
                and not self._nr_ternary_narrows(value_node)
                and not (self._holds_propagate(value_node.children[0]) or self._holds_propagate(value_node.children[2]))
            ):
                if not target_go_type and target_str not in self.declared_vars:
                    # ``s = Square() if c else Shape()``: the least common
                    # base class, so calls on ``s`` dispatch virtually.
                    common = self._ternary_common_go_type(value_node)
                    common_class = self._hier_class_from_go_type(common)
                    branch_types = {
                        self._expr_go_type_hint(b) or self._infer_type_from_value(b) or ""
                        for b in (value_node.children[0], value_node.children[2])
                    }
                    if common_class and len(branch_types) > 1:
                        target_go_type = common
                        self._var_go_types[target_str] = common
                        self._var_types[target_str] = common_class
                if target_go_type:
                    true_val = self._typed_value_to_go(value_node.children[0], target_go_type)
                    false_val = self._typed_value_to_go(value_node.children[2], target_go_type)
                else:
                    true_val = self._expr_to_go(value_node.children[0])
                    false_val = self._expr_to_go(value_node.children[2])
                cond = self._expr_to_go(value_node.children[1])
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
            if target_go_type:
                with self._scoped(_propagate_cast_hint=target_go_type):
                    value_str = self._typed_value_to_go(value_node, target_go_type)
            else:
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
                    rhs = value_str
                    self._emit(f"{obj_str} = append({obj_str}[:{lo}], append({rhs}, {obj_str}[{hi}:]...)...)")
                    return
            # Tuple unpacking
            if isinstance(parts[0], Tree) and parts[0].data == "tuple":
                names = [self._expr_to_go(c) for c in parts[0].children if c is not None]
                value_hint = self._expr_go_type_hint(value_node) or ""
                if isinstance(value_node, Tree) and value_node.data in {"tuple", "list"}:
                    values = [self._expr_to_go(child) for child in value_node.children if child is not None]
                    unpacked_value = ", ".join(values)
                elif value_hint.startswith("[]"):
                    # ``a, b = t`` with ``t`` a tuple (or list) value: index it.
                    self._q_temp_counter += 1
                    tmp = f"__lamTup{self._q_temp_counter}"
                    self._emit(f"{tmp} := {value_str}")
                    elem_types = self._tuple_value_element_go_types(value_node, len(names))
                    values = []
                    for index, _name in enumerate(names):
                        cast = elem_types[index] if index < len(elem_types) else ""
                        item = f"{tmp}[{index}]"
                        if cast and value_hint == "[]interface{}":
                            item = f"{item}.({cast})"
                        values.append(item)
                    for name, cast in zip(names, elem_types):
                        if cast and name != "_":
                            self._var_go_types[name] = cast
                    unpacked_value = ", ".join(values)
                else:
                    unpacked_value = value_str
                has_new = any(name != "_" and name not in self.declared_vars for name in names)
                op = ":=" if has_new else "="
                self._emit(f"{', '.join(names)} {op} {unpacked_value}")
                for n in names:
                    if n != "_" and n not in self.declared_vars:
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
                if isinstance(value_node, Tree) and value_node.data == "set" and not target_go_type:
                    # ``flags = {"new", "active"}``: keep the inferred
                    # ``map[T]bool`` so ``for f in flags`` ranges over keys.
                    set_type = self._infer_type_from_value(value_node)
                    if self._is_go_set_type(set_type):
                        value_str = self._typed_set_literal_to_go(value_node, set_type)
                        self._var_go_types[target_str] = set_type
                elif (
                    isinstance(value_node, Tree) and value_node.data == "tuple"
                    and not target_go_type and not self._contains_propagate(value_node)
                ):
                    # ``t = (1, "a")``: one tuple value (``[]interface{}``).
                    value_str = self._typed_value_to_go(value_node, "[]interface{}")
                    self._var_go_types[target_str] = "[]interface{}"
                    inferred_text = getattr(node, "_lam_inferred_type", "")
                    inferred_node = self._lam_type_text_node(inferred_text) if inferred_text else None
                    if inferred_node is not None:
                        self._record_lam_type(target_str, inferred_node)
                elif (
                    isinstance(value_node, Tree) and value_node.data == "list"
                    and not target_go_type and not self._contains_propagate(value_node)
                ):
                    # ``handles = [slow(1), slow(2)]``: a list of same-typed
                    # elements keeps that element type.
                    list_type = self._infer_type_from_value(value_node)
                    if list_type.startswith("[]") and "interface{}" not in list_type:
                        value_str = self._typed_list_literal_to_go(value_node, list_type)
                        self._var_go_types[target_str] = list_type
                if not target_go_type and target_str not in self._var_go_types:
                    # ``n = user?.name`` (``*string``), ``h = slow(1)``
                    # (an async handle), ``f = maker()``: keep the shape.
                    value_hint = self._expr_go_type_hint(value_node) or ""
                    if not value_hint and isinstance(value_node, Tree) and value_node.data in {"dict", "list"}:
                        value_hint = self._infer_type_from_value(value_node) or ""
                    if not value_hint and isinstance(value_node, Tree) and value_node.data == "lambdef":
                        # ``f = lambda x: x``: the signature of the emitted
                        # literal (``func(interface{}) interface{}``), so a
                        # call's ``any`` result converts where a type is
                        # expected.
                        value_hint = self._func_literal_go_type(value_str)
                    if not value_hint and isinstance(value_node, Tree) and (
                        self._is_dynamic_any_value(value_node)
                        or (
                            value_node.data == "propagate" and value_node.children
                            and self._propagated_payload_go_type(value_node.children[0]) == "interface{}"
                        )
                    ):
                        # ``value = load()?`` on a ``Result[any]``, ``v =
                        # same(a, b)`` bound to ``any``: an ``interface{}``
                        # local, converted where a concrete type is expected.
                        value_hint = "interface{}"
                    if value_hint:
                        self._var_go_types[target_str] = value_hint
                self._emit(f"{target_str} := {value_str}")
                self._declare_var(target_str)

    @staticmethod
    def _func_literal_go_type(go_src: str) -> str:
        """``func(interface{}) interface{}`` for the Go function literal
        ``func(x interface{}) interface{} { ... }``; ``""`` when its header
        is not a plain parameter list."""
        # The result type may contain ``{}`` (``interface{}``); the body's
        # brace is the first ``{`` not closed right away.
        match = re.match(r"func\(([^()]*)\)\s*((?:[^{]|\{\})*?)\s*\{(?!\})", go_src or "")
        if not match:
            return ""
        params = []
        for part in [p.strip() for p in match.group(1).split(",") if p.strip()]:
            pieces = part.split(None, 1)
            if len(pieces) != 2:
                return ""
            params.append(pieces[1])
        ret = match.group(2).strip()
        if ret.startswith("("):
            return ""
        return f"func({', '.join(params)}) {ret}".rstrip()

    def _emit_semantic_typed_assign(self, node: Tree, target_node, target_str: str, value_node) -> bool:
        """``name = value`` declaring ``name``: when the semantic checker
        inferred a concrete type, record it (so later uses dispatch on it)
        and, if the value's Go shape is not already that type, lower it
        like ``name: T = value``."""
        if not (isinstance(target_node, Tree) and target_node.data == "var"):
            return False
        if (
            target_str == "_"
            or target_str in self.declared_vars
            or "." in target_str
            or self._is_static_var_go_name(target_str)
        ):
            return False
        inferred = self._semantic_inferred_go_type(node)
        if inferred is None:
            # ``r = conn.query(q)`` with a ``Result[list[dict[str, any]]]``
            # result: the local stays a ``*Result`` but keeps its payload,
            # so ``r.unwrap()[0]`` / ``r?`` know the rows' type.
            # A name reused by another function must not keep a payload.
            self._var_result_payloads.pop(target_str, None)
            type_text = getattr(node, "_lam_inferred_type", "") or ""
            type_node = None
            if type_text.startswith("Result[") and "unknown" not in type_text:
                type_node = self._lam_type_text_node(type_text)
            if type_node is not None:
                self._note_result_payload(target_str, type_node)
            elif isinstance(value_node, Tree) and value_node.data == "funccall":
                payload = self._propagated_payload_go_type(value_node)
                if payload and payload != "interface{}":
                    self._var_result_payloads[target_str] = payload
            return False
        if self._contains_async_handle_call(value_node):
            return False
        type_node, go_type = inferred
        own = self._expr_go_type_hint(value_node) or self._infer_type_from_value(value_node) or ""
        raw_type = self._get_raw_type_name(type_node)
        if raw_type and raw_type in (self._class_names | self._interfaces):
            self._var_types[target_str] = raw_type
        else:
            self._var_types.pop(target_str, None)
        literal = isinstance(value_node, Tree) and value_node.data in {
            "list", "dict", "set", "list_comprehension", "dict_comprehension", "set_comprehension",
        }
        if own == go_type:
            value_str = self._typed_value_to_go(value_node, go_type) if literal else self._expr_to_go(value_node)
            decl = f"{target_str} := {value_str}"
        else:
            with self._scoped(_propagate_cast_hint=go_type):
                value_str = self._typed_value_to_go(value_node, go_type)
            decl = f"var {target_str} {go_type} = {value_str}"
        self._var_go_types[target_str] = go_type
        self._record_lam_type(target_str, type_node)
        self._note_result_payload(target_str, type_node)
        tuple_types = self._list_tuple_element_types(type_node)
        if tuple_types:
            self._tuple_iterable_element_types[target_str] = tuple_types
        self._emit(decl)
        self._declare_var(target_str)
        return True

    def _tuple_value_element_go_types(self, value_node, count: int) -> list:
        """Go types of the positions of a tuple-typed value (``""`` when
        unknown)."""
        from compiler.typesys import GenericType as _Generic
        lam = None
        if isinstance(value_node, Tree) and value_node.data == "var" and value_node.children:
            lam = self._var_lam_types.get(self._get_name(value_node.children[0]))
        if isinstance(lam, _Generic) and lam.base == "tuple":
            out = []
            for arg in lam.args:
                type_node = self._lam_type_text_node(render_type_text(arg))
                go = self._type_expr_to_go(type_node) if type_node is not None else ""
                out.append(go if go and "interface{}" not in go else "")
            return out
        return [""] * count

    def _contains_async_handle_call(self, node) -> bool:
        """Whether ``node`` calls an ``async func`` without awaiting it
        (the value is a handle, which the semantic types do not model)."""
        if not isinstance(node, Tree) or node.data in {"await_call", "lambdef"}:
            return False
        if node.data == "funccall" and node.children:
            callee = node.children[0]
            if isinstance(callee, Tree) and callee.data == "var" and callee.children:
                if self._get_name(callee.children[0]) in self._async_functions:
                    return True
            if isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) >= 2:
                # ``a.run(1)`` / ``Worker.make(2)`` with an async method.
                receiver = callee.children[0]
                class_name = self._infer_receiver_class(receiver)
                if not class_name and isinstance(receiver, Tree) and receiver.data == "var" and receiver.children:
                    candidate = self._get_name(receiver.children[0])
                    class_name = candidate if candidate in self._class_names else ""
                if class_name and self._async_method_handle_go_type(class_name, self._get_name(callee.children[1])):
                    return True
        return any(self._contains_async_handle_call(c) for c in node.children)

    def _visit_augassign(self, node: Tree):
        with self._scoped(_index_target=True):
            target_str = self._expr_to_go(node.children[0])
        op = self._get_augassign_op(node.children[1])
        markup = self._markup_augassign_to_go(target_str, node.children[0], op, node.children[2]) if isinstance(node.children[0], Tree) else None
        if markup is not None:
            self._emit(markup)
            return
        # ``total += f()?``: the ``?`` payload has the target's type.
        target_type = self._expr_go_type_hint(node.children[0]) if isinstance(node.children[0], Tree) else ""
        set_op = {"|=": "|", "&=": "&", "-=": "-"}.get(op)
        target_set = target_type if set_op and self._is_go_set_type(target_type) else ""
        if target_set and self._is_lam_set_value(target_str, target_set):
            # ``tags |= other`` / ``&=`` / ``-=`` on a set.
            value = self._typed_value_to_go(node.children[2], target_set)
            self._emit(f"{target_str} = {self._set_binop_to_go(set_op, target_str, value, target_set)}")
            return
        if op == "+=" and target_type.startswith("[]") and isinstance(node.children[2], Tree):
            # ``xs += ys``: extend with the items of the other list (Python's
            # in-place ``+=``; the right operand is not modified).
            value_node = node.children[2]
            value = self._slice_to_go_type(
                self._expr_to_go(value_node), self._list_operand_go_type(value_node), target_type, value_node,
            ) if (
                value_node.data in self._LIST_LITERAL_NODES or self._list_operand_go_type(value_node)
            ) else ""
            if value:
                self._emit(f"{target_str} = append({target_str}, {value}...)")
                return
        if op in {"/=", "//=", "%="} and isinstance(node.children[0], Tree) and isinstance(node.children[2], Tree) \
                and not self._infer_expr_class(node.children[0]):
            # Python division: ``x = x <op> v`` through the same lowering as
            # the binary operator (Go's own ``x /= v`` when it agrees).
            target_node = node.children[0]
            left = self._expr_to_go(target_node)
            left_kind = self._division_operand_kind(target_node)
            expr, _kind, native = self._division_step_to_go(left, left_kind, target_node, op[:-1], node.children[2])
            if native:
                value = self._expr_to_go(node.children[2])
                if isinstance(node.children[2], Tree) and node.children[2].data in {"arith_expr", "term"}:
                    value = f"({value})"
                self._emit(f"{target_str} {'%' if op == '%=' else '/'}= {value}")
            else:
                self._emit(f"{target_str} = {expr}")
            return
        if target_type in {"float64", "float32"} and op in {"+=", "-=", "*="}:
            # ``total += n`` with ``n: int``: the int widens.
            value_str = self._typed_value_to_go(node.children[2], target_type)
        elif target_type and target_type != "interface{}":
            with self._scoped(_propagate_cast_hint=target_type):
                value_str = self._expr_to_go(node.children[2])
        else:
            value_str = self._expr_to_go(node.children[2])

        if op == "**=":
            self._need_import("math")
            if target_type in self._NUMERIC_GO_TYPES and target_type != "float64":
                # ``x **= 2`` on an int stays an int.
                self._emit(f"{target_str} = {target_type}(math.Pow(float64({target_str}), float64({value_str})))")
            else:
                self._emit(f"{target_str} = math.Pow({target_str}, float64({value_str}))")
        elif op == "//=":
            self._emit(f"{target_str} = int({target_str} / {value_str})")
        elif op == "*=" and target_type == "string":
            # ``s *= 3``: string repetition.
            self._need_import("strings")
            self._emit(f"{target_str} = strings.Repeat({target_str}, max({value_str}, 0))")
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
        with self._scoped(_index_target=True):
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
        expr = node.children[0]
        # A bare call statement discards its value: lower it without the
        # payload assertion added to inline ``unwrap()`` (Go rejects a type
        # assertion used as a statement).
        if isinstance(expr, Tree) and expr.data == "funccall":
            expr_str = self._funccall_to_go(expr)
        else:
            expr_str = self._expr_to_go(expr)
        # ``f()?`` as a statement: the propagate lowering already emitted
        # ``__qN := f()`` plus the early-return guard; the remaining payload
        # expression has no use and Go rejects it ("is not used").
        if isinstance(expr, Tree) and expr.data == "propagate":
            return
        if expr_str:
            self._emit(expr_str)

    # ─── Return ────────────────────────────────────────────────

    _GO_TO_LAM_SCALARS = {
        "string": "str", "int": "int", "float64": "float", "bool": "bool",
        "LamJSON": "json", "interface{}": "any", "[]byte": "bytes", "*Result": "Result",
    }

    def _go_type_lam_spelling(self, go_type: str) -> str:
        """``map[string][]int`` -> ``dict[str, list[int]]`` (for diagnostics)."""
        if go_type in self._GO_TO_LAM_SCALARS:
            return self._GO_TO_LAM_SCALARS[go_type]
        if go_type.startswith("[]"):
            return f"list[{self._go_type_lam_spelling(go_type[2:])}]"
        if go_type.startswith("map[") and "]" in go_type:
            key, _, value = go_type[4:].partition("]")
            return f"dict[{self._go_type_lam_spelling(key)}, {self._go_type_lam_spelling(value)}]"
        if go_type.startswith("*"):
            return go_type[1:]
        return go_type

    def _check_result_ok_return(self, val_node, return_type_key: str) -> None:
        """``return Result.Ok(Json.encode(v))`` in a ``-> Result[str]``
        wraps a ``Result`` where the caller expects a ``str`` and panics on
        unwrap. The semantic checker reports it for the compiled file; this
        reports it for imported modules with the same wording."""
        if self._in_nested_callable or not (return_type_key.startswith("*Result[") and return_type_key.endswith("]")):
            return
        payload = return_type_key[len("*Result["):-1]
        concrete = (
            payload in self._GO_TO_LAM_SCALARS or payload.startswith(("[]", "map[", "*"))
        )
        if not concrete or payload in {"interface{}", "*Result"}:
            return
        if not (isinstance(val_node, Tree) and val_node.data == "funccall" and len(val_node.children) >= 2):
            return
        callee = val_node.children[0]
        if not (
            isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) >= 2
            and isinstance(callee.children[0], Tree) and callee.children[0].data == "var"
            and self._get_name(callee.children[0].children[0]) == "Result"
            and self._get_name(callee.children[1]) == "Ok"
            and "Result" not in self.declared_vars
        ):
            return
        args = self._get_raw_call_args(val_node.children[1])
        if len(args) != 1 or not isinstance(args[0], Tree) or args[0].data == "argvalue":
            return
        arg = args[0]
        # ``Result.Ok(n / 2)`` in a ``-> Result[int]``: a float payload.
        self._check_int_true_division(arg, payload)
        if self._expr_go_type_hint(arg) != "*Result":
            return
        from compiler.constants import lam_simple_expr_text

        inner = self._result_payload_of(arg)
        actual = f"Result[{self._go_type_lam_spelling(inner)}]" if inner and inner != "interface{}" else "Result"
        lam_payload = self._go_type_lam_spelling(payload)
        message = (
            f"`Result.Ok(...)` wraps a value of type `{actual}`, but the enclosing function returns "
            f"`Result[{lam_payload}]` (payload `{lam_payload}`)"
        )
        if not inner or inner in {payload, "interface{}"}:
            arg_text = lam_simple_expr_text(arg)
            propagate = f"`{arg_text}?`" if arg_text else "`?`"
            message += f"; propagate the inner result with {propagate} or return it directly"
        self._report_lam_error(val_node, "return", message)

    _INT_GO_TYPES = frozenset({
        "int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16", "uint32", "uint64",
        "uintptr", "byte", "rune",
    })

    def _check_int_true_division(self, value, go_type: str) -> None:
        """``n: int = a / b`` / ``return a / b`` in an ``-> int`` function
        with int operands: ``/`` is a float division, so this would fail in
        ``go build``. Reported as a Lam diagnostic in every module (the
        semantic checker only sees the compiled file)."""
        if go_type not in self._INT_GO_TYPES or not isinstance(value, Tree) or value.data != "term":
            return
        if "/" not in {str(op) for op in value.children[1::2]}:
            return
        kinds = [self._division_operand_kind(child) for child in value.children[0::2]]
        if any(self._is_float_kind(kind) or kind in {"", "param"} for kind in kinds):
            return
        self._report_lam_error(
            value, "type",
            "`/` always gives a `float`; use `//` for integer division "
            f"(the target is `{go_type}`)",
        )

    def _visit_return_stmt(self, node: Tree):
        if node.children and node.children[0] is not None:
            val_node = node.children[0]
            return_type = getattr(self, "_current_return_type", "") or ""
            return_type_key = getattr(self, "_current_return_type_key", "") or return_type
            self._check_result_ok_return(val_node, return_type_key)
            self._check_int_true_division(val_node, return_type)
            if not self._in_nested_callable and not self._in_generator and self._lam_func_names:
                self._check_literal_go_shape(
                    val_node, return_type,
                    f"function `{self._lam_func_names[-1]}` returns `{{actual}}`, expected `{{expected}}`",
                )
            if isinstance(val_node, Tree) and val_node.data == "testlist_tuple":
                expected_types = self._split_go_tuple_type(return_type)
                vals = []
                for i, c in enumerate(child for child in val_node.children if child is not None):
                    expected = expected_types[i] if i < len(expected_types) else ""
                    vals.append(self._typed_value_to_go(c, expected) if expected else self._expr_to_go(c))
                val = ", ".join(vals)
            elif (
                isinstance(val_node, Tree) and val_node.data == "test" and len(val_node.children) == 3
                and not self._nr_ternary_narrows(val_node)
                and not (self._holds_propagate(val_node.children[0]) or self._holds_propagate(val_node.children[2]))
            ):
                if return_type:
                    with self._scoped(_expected_expr_type_key=return_type_key):
                        true_val = self._typed_value_to_go(val_node.children[0], return_type, return_type_key)
                        false_val = self._typed_value_to_go(val_node.children[2], return_type, return_type_key)
                else:
                    true_val = self._expr_to_go(val_node.children[0])
                    false_val = self._expr_to_go(val_node.children[2])
                cond = self._expr_to_go(val_node.children[1])
                self._emit(f"if {cond} {{")
                self.indent += 1
                self._emit_return_value(true_val)
                self.indent -= 1
                self._emit(f"}} else {{")
                self.indent += 1
                self._emit_return_value(false_val)
                self.indent -= 1
                self._emit(f"}}")
                return
            else:
                if return_type:
                    with self._scoped(
                        _propagate_cast_hint=return_type,
                        _expected_expr_type_key=return_type_key,
                    ):
                        val = self._typed_value_to_go(val_node, return_type, return_type_key)
                else:
                    val = self._expr_to_go(val_node)
            self._emit_return_value(val)
        else:
            self._emit_return_value(None)

    def _emit_return_value(self, val: Optional[str]) -> None:
        """Emit the Go for ``return [val]`` in the current lowering context.

        * inside a ``do { }`` body (a ``*Result`` IIFE): record the value
          and a flag, then leave the IIFE; the code after the IIFE performs
          the real return (see ``_visit_do_stmt``);
        * inside an async goroutine: publish the result on the handle;
        * inside a try-with-catch IIFE: set ``retval`` (typed functions)
          and ``__lamShouldReturn``, then leave the IIFE.
        """
        if self._do_return_target:
            flag, var = self._do_return_target
            if val is not None and var:
                self._emit(f"{var} = {val}")
            self._emit(f"{flag} = true")
            if self._in_try_iife:
                # A try IIFE inside the ``do`` body: leave it; the code after
                # the IIFE leaves the ``do`` closure (``return nil``).
                self._emit("__lamShouldReturn = true")
                self._emit("return")
                return
            self._emit("return nil")
            return
        if self._in_async_func and self._async_chan_name:
            self._emit_async_result(val)
            if self._in_try_iife:
                self._emit("__lamShouldReturn = true")
            self._emit("return")
            return
        if val is None:
            if self._in_try_iife:
                self._emit("__lamShouldReturn = true")
            self._emit("return")
            return
        if self._in_try_iife:
            # Inside the IIFE wrapping a try-with-catch: rewrite
            # ``return X`` to (set retval if typed) +
            # __lamShouldReturn = true + bare-return-from-IIFE.
            # The post-IIFE check forwards the signal to the
            # outer function.
            if self._in_try_func:
                self._emit(f"{self._try_result_targets()} = {val}")
            self._emit("__lamShouldReturn = true")
            self._emit("return")
        elif self._in_except_handler and self._in_try_func:
            self._emit(f"{self._try_result_targets()} = {val}")
            self._emit("return")
        else:
            self._emit(f"return {val}")

    def _try_result_targets(self) -> str:
        """The named results a ``return`` inside a try IIFE assigns."""
        return ", ".join(self._try_result_names(self._current_return_type or ""))

    def _emit_async_result(self, val: Optional[str]) -> None:
        """Publish an async callable's outcome on its one-slot handle.

        The handle carries ``func() T`` thunks: ``await`` calls the thunk,
        which returns the value, or re-raises an exception the goroutine
        recovered (see ``_emit_async_callable``).
        """
        ch = self._async_chan_name
        return_type = self._current_return_type or ""
        if val is None or not return_type:
            self._emit(f"{ch} <- func() {{}}")
            return
        self._emit("{")
        self.indent += 1
        self._emit(f"var __lamAsyncV {return_type} = {val}")
        self._emit(f"{ch} <- func() {return_type} {{ return __lamAsyncV }}")
        self.indent -= 1
        self._emit("}")

    # ─── Simple flow statements ────────────────────────────────

    def _visit_pass_stmt(self, _):
        self._emit("// pass")

    def _visit_break_stmt(self, _):
        if self._loop_break_flag:
            self._emit(f"{self._loop_break_flag} = true")
        self._emit_loop_jump("break")

    # Values of a ``_loop_jump`` flag: the jump a closure left with.
    _LOOP_JUMP_CODES = {"break": 1, "continue": 2}

    def _emit_loop_jump(self, kind: str) -> None:
        """Emit ``break`` / ``continue``; inside a closure between the jump
        and its loop (``_loop_jump``), record it and leave the closure."""
        if self._loop_jump:
            flag, leave = self._loop_jump
            self._emit(f"{flag} = {self._LOOP_JUMP_CODES[kind]}")
            self._emit(leave)
            return
        self._emit(kind)

    @staticmethod
    def _own_loop_jumps(node) -> set:
        """Kinds (``"break"`` / ``"continue"``) of the jumps in ``node``
        that target a loop enclosing ``node`` (not a nested loop or
        function)."""
        found: set = set()

        def walk(n) -> None:
            if not isinstance(n, Tree):
                return
            if n.data == "break_stmt":
                found.add("break")
                return
            if n.data == "continue_stmt":
                found.add("continue")
                return
            if n.data in {"funcdef", "classdef", "lambdef"}:
                return
            children = n.children
            if n.data in {"for_stmt", "while_stmt"}:
                # Only a nested loop's ``else`` block jumps in the outer loop.
                children = [c for c in n.children if isinstance(c, Tree) and c.data == "suite"][1:]
            for child in children:
                walk(child)

        walk(node)
        return found

    def _declare_loop_jump(self, suites) -> Optional[tuple]:
        """Declare a fresh jump flag for a closure lowering ``suites`` when
        they hold a ``break`` / ``continue`` of an enclosing loop; return
        ``(flag, kinds)``."""
        kinds: set = set()
        for suite in suites:
            kinds |= self._own_loop_jumps(suite)
        if not kinds:
            return None
        self._loop_jump_counter = getattr(self, "_loop_jump_counter", 0) + 1
        flag = f"__lamJump{self._loop_jump_counter}"
        self._emit(f"{flag} := 0")
        return flag, kinds

    def _emit_loop_jump_dispatch(self, jump: Optional[tuple]) -> None:
        """After the closure: perform the jump it recorded in its flag."""
        if not jump:
            return
        flag, kinds = jump
        for kind, code in self._LOOP_JUMP_CODES.items():
            if kind not in kinds:
                continue
            self._emit(f"if {flag} == {code} {{")
            self.indent += 1
            if kind == "break":
                self._visit_break_stmt(None)
            else:
                self._visit_continue_stmt(None)
            self.indent -= 1
            self._emit("}")

    def _new_loop_break_flag(self) -> str:
        """Fresh per-loop flag recording a ``break`` for ``for``/``while``-``else``.

        Each loop gets its own name so nested or sequential loop-else
        statements in one function neither shadow nor redeclare each other.
        """
        self._loop_break_counter = getattr(self, "_loop_break_counter", 0) + 1
        return f"_lamLoopBreak{self._loop_break_counter}"

    def _visit_continue_stmt(self, _):
        self._emit_loop_jump("continue")

    def _visit_yield_stmt(self, node: Tree):
        """``yield v`` hands ``v`` to the consumer; when the consumer has
        stopped (a loop left early, a generator stopped or collected) the
        body returns from there, running its ``finally`` blocks and
        ``with`` closes on the way out (Python's ``GeneratorExit``)."""
        if not node.children:
            return
        expr = node.children[0]
        if not (self._in_generator and self._generator_chan):
            val = self._expr_to_go(expr)
            if val:
                self._emit(val)
            return
        if isinstance(expr, Tree) and expr.data == "yield_from":
            self._emit_yield_from(expr)
            return
        self._emit(f"if !{self._expr_to_go(expr)} {{")
        self.indent += 1
        self._emit_return_value(None)
        self.indent -= 1
        self._emit("}")

    def _emit_yield_from(self, node: Tree) -> None:
        """``yield from src``: every value of ``src`` (a generator, a list,
        any iterable) in turn, as ``for v in src { yield v }``; stopping
        the outer generator stops one ``src`` call it is running."""
        self._q_temp_counter += 1
        item = f"__lamYieldFrom{self._q_temp_counter}"
        item_var = Tree("var", [Tree("name", [Token("NAME", item)])])
        loop = Tree("for_stmt", [
            Tree("untyped_for_target", [item_var]),
            node.children[0],
            Tree("suite", [Tree("yield_stmt", [Tree("yield_expr", [item_var])])]),
            None,
        ])
        self._visit_for_stmt(loop)

    def _visit_del_stmt(self, node: Tree):
        if node.children:
            target = node.children[0]
            if isinstance(target, Tree) and target.data == "getitem":
                obj_node = target.children[0]
                obj_type = self._expr_go_type_hint(obj_node) or ""
                if obj_type.startswith("[]"):
                    self._emit_del_list_items(obj_node, target.children[1], obj_type)
                    return
                obj = self._expr_to_go(obj_node)
                idx = self._expr_to_go(target.children[1])
                if obj_type == "LamJSON":
                    self._emit(f"lamJSONDelete({obj}, {idx})")
                    return
                self._emit(f"delete({obj}, {idx})")
                return
            target_str = self._expr_to_go(target)
            self._emit(f"{target_str} = nil")

    def _emit_del_list_items(self, obj_node, index_node, list_type: str) -> None:
        """``del xs[i]`` / ``del xs[a:b]`` / ``del xs[a:b:k]`` on a list,
        like Python: a negative index counts from the end (out of range is
        an ``IndexError``), slice bounds are clamped. The kept items go to a
        new slice, so a view taken before (``ys = xs[:]`` shares the Go
        backing array) is left intact."""
        with self._scoped(_index_target=True):
            obj = self._expr_to_go(obj_node)
        if not (isinstance(index_node, Tree) and index_node.data == "slice"):
            idx = self._typed_value_to_go(index_node, "int")
            self._emit(
                f"{obj} = func(s {list_type}, i int) {list_type} {{ if i < 0 {{ i += len(s) }}; _ = s[i]; "
                f"return append(append(make({list_type}, 0, len(s)-1), s[:i]...), s[i+1:]...) }}({obj}, {idx})"
            )
            return
        parts = [c for c in index_node.children if not (isinstance(c, Tree) and c.data == "sliceop")]
        lo_node = parts[0] if parts else None
        hi_node = parts[1] if len(parts) > 1 else None
        step_node = None
        for c in index_node.children:
            if isinstance(c, Tree) and c.data == "sliceop":
                step_node = next((sc for sc in c.children if sc is not None), None)
        lo = self._typed_value_to_go(lo_node, "int") if lo_node is not None else ""
        hi = self._typed_value_to_go(hi_node, "int") if hi_node is not None else ""
        if step_node is None:
            # Contiguous: shift the tail down in place.
            clamp = "if {v} < 0 {{ {v} += n; if {v} < 0 {{ {v} = 0 }} }} else if {v} > n {{ {v} = n }}; "
            body = "n := len(s); "
            body += f"lo := {lo}; " + clamp.format(v="lo") if lo else "lo := 0; "
            body += f"hi := {hi}; " + clamp.format(v="hi") if hi else "hi := n; "
            body += f"if hi < lo {{ hi = lo }}; return append(append(make({list_type}, 0, n-(hi-lo)), s[:lo]...), s[hi:]...)"
            self._emit(f"{obj} = func(s {list_type}) {list_type} {{ {body} }}({obj})")
            return
        step = self._typed_value_to_go(step_node, "int")
        # Stepped: Python's ``slice.indices``, then keep the unmarked items.
        self._need_import("fmt")
        body = (
            f"n := len(s); step := {step}; "
            'if step == 0 { panic(fmt.Sprintf("ValueError: slice step cannot be zero")) }; '
            "lower, upper := 0, n; if step < 0 { lower, upper = -1, n-1 }; "
            "norm := func(v int) int { if v < 0 { v += n; if v < lower { v = lower } } else if v > upper { v = upper }; return v }; _ = norm; "
        )
        body += f"lo := norm({lo}); " if lo else "lo := lower; if step > 0 { lo = 0 } else { lo = upper }; "
        body += f"hi := norm({hi}); " if hi else "hi := upper; if step < 0 { hi = lower }; "
        body += (
            "drop := make([]bool, n); "
            "for i := lo; (step > 0 && i < hi) || (step < 0 && i > hi); i += step { drop[i] = true }; "
            f"out := make({list_type}, 0, n); for i, v := range s {{ if !drop[i] {{ out = append(out, v) }} }}; return out"
        )
        self._emit(f"{obj} = func(s {list_type}) {list_type} {{ {body} }}({obj})")

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

    def _flow_assignment_types(self, node) -> dict[str, str]:
        found: dict[str, str] = {}

        def record(name: str, go_type: str) -> None:
            if not name or name == "_" or "." in name or "[" in name:
                return
            go_type = go_type or "interface{}"
            previous = found.get(name)
            found[name] = go_type if previous in {None, go_type} else "interface{}"

        def walk(cur) -> None:
            if not isinstance(cur, Tree) or cur.data in {"funcdef", "classdef", "lambdef"}:
                return
            if cur.data == "annassign":
                children = [child for child in cur.children if isinstance(child, Tree)]
                target = children[0] if children else None
                type_node = next((child for child in children if child.data == "type_expr"), None)
                if isinstance(target, Tree) and target.data == "var" and target.children:
                    record(self._get_name(target.children[0]), self._type_expr_to_go(type_node))
                return
            if cur.data == "assign" and len(cur.children) >= 2:
                target = cur.children[0]
                value = cur.children[-1]
                if isinstance(target, Tree) and target.data == "var" and target.children:
                    record(self._get_name(target.children[0]), self._infer_type_from_value(value))
                return
            for child in cur.children:
                walk(child)

        walk(node)
        return found

    def _flow_suite_terminates(self, node) -> bool:
        if not isinstance(node, Tree):
            return False
        for stmt in node.children if node.data == "suite" else [node]:
            if not isinstance(stmt, Tree):
                continue
            if stmt.data == "simple_stmt" and any(
                isinstance(child, Tree) and child.data in {"return_stmt", "raise_stmt"}
                for child in stmt.children
            ):
                return True
            if stmt.data in {"return_stmt", "raise_stmt"}:
                return True
        return False

    def _hoist_flow_locals(self, suites: List[Tree]) -> None:
        continuing = [suite for suite in suites if not self._flow_suite_terminates(suite)]
        if not continuing:
            return
        assignments = [self._flow_assignment_types(suite) for suite in continuing]
        names = set(assignments[0])
        for mapping in assignments[1:]:
            names &= set(mapping)
        for name in sorted(names):
            if name in self.declared_vars:
                continue
            types = {mapping[name] for mapping in assignments}
            go_type = next(iter(types)) if len(types) == 1 else "interface{}"
            if go_type == "interface{}":
                continue
            self._emit(f"var {name} {go_type}")
            self._declare_var(name)
            self._var_go_types[name] = go_type
            if go_type.startswith("*"):
                class_name = go_type[1:].split("[", 1)[0]
                if class_name in self._class_names:
                    self._var_types[name] = class_name

    def _visit_if_stmt(self, node: Tree):
        branch_suites = [node.children[1]] if len(node.children) > 1 and isinstance(node.children[1], Tree) else []
        elifs = node.children[2] if len(node.children) > 2 else None
        if isinstance(elifs, Tree):
            branch_suites.extend(
                child.children[1]
                for child in elifs.children
                if isinstance(child, Tree) and len(child.children) > 1 and isinstance(child.children[1], Tree)
            )
        else_suite = node.children[3] if len(node.children) > 3 else None
        if isinstance(else_suite, Tree):
            branch_suites.append(else_suite)
            self._hoist_flow_locals(branch_suites)
        # isinstance narrowing (``compiler/visitors/narrowing.py``): each
        # branch binds the shadows of the facts its condition establishes.
        branch_facts, else_facts = _narrowing.if_branch_facts(node)
        cond = self._expr_to_go(node.children[0])
        self._emit(f"if {cond} {{")
        self.indent += 1
        self._push_scope()
        body_start, vars_at_start = self._scope_unused_tracking_start()
        self._nr_emit_region(branch_facts[0] if branch_facts else [], [node.children[1]],
                             lambda: self._visit_suite(node.children[1]))
        self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
        self.indent -= 1

        elifs = node.children[2]
        nested = 0
        branch_index = 0
        if isinstance(elifs, Tree) and elifs.data == "elifs":
            for elif_node in elifs.children:
                if isinstance(elif_node, Tree) and elif_node.data == "elif_":
                    branch_index += 1
                    if self._contains_propagate(elif_node.children[0]):
                        # ``elif f()? {``: the ``?`` statements belong to this
                        # branch, so it becomes ``else { ...; if cond {``.
                        self._emit("} else {")
                        self.indent += 1
                        nested += 1
                        econd = self._expr_to_go(elif_node.children[0])
                        self._emit(f"if {econd} {{")
                    else:
                        econd = self._expr_to_go(elif_node.children[0])
                        self._emit(f"}} else if {econd} {{")
                    self.indent += 1
                    self._push_scope()
                    body_start, vars_at_start = self._scope_unused_tracking_start()
                    facts = branch_facts[branch_index] if branch_index < len(branch_facts) else []
                    self._nr_emit_region(facts, [elif_node.children[1]],
                                         lambda: self._visit_suite(elif_node.children[1]))
                    self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
                    self.indent -= 1

        else_suite = node.children[3] if len(node.children) > 3 else None
        if else_suite is not None and isinstance(else_suite, Tree):
            self._emit("} else {")
            self.indent += 1
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            self._nr_emit_region(else_facts, [else_suite], lambda: self._visit_suite(else_suite))
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
            self.indent -= 1
        self._emit("}")
        for _ in range(nested):
            self.indent -= 1
            self._emit("}")

    # ─── While ─────────────────────────────────────────────────

    @staticmethod
    def _contains_propagate(node) -> bool:
        if not isinstance(node, Tree):
            return False
        if node.data == "propagate":
            return True
        if node.data in {"lambdef", "funcdef"}:
            return False
        return any(StatementVisitorMixin._contains_propagate(child) for child in node.children)

    def _visit_while_stmt(self, node: Tree):
        has_else = len(node.children) > 2 and node.children[2] is not None
        break_flag = self._new_loop_break_flag() if has_else else ""
        if has_else:
            self._declare_loop_break_flag(break_flag, node.children[1])
        if self._contains_propagate(node.children[0]):
            # ``while f()? {``: the ``?`` statements must run before every
            # test, so the condition moves inside the loop.
            self._emit("for {")
            self.indent += 1
            cond = self._expr_to_go(node.children[0])
            self._emit(f"if !({cond}) {{ break }}")
        else:
            cond = self._expr_to_go(node.children[0])
            self._emit(f"for {cond} {{")
            self.indent += 1
        self._push_scope()
        body_start, vars_at_start = self._scope_unused_tracking_start()
        with self._scoped(_loop_break_flag=break_flag, _loop_jump=None):
            # ``while isinstance(x, T)``: the body sees ``x`` as a ``T``.
            self._nr_emit_region(
                _narrowing.condition_facts(node.children[0], True),
                [node.children[1], node.children[0]],
                lambda: self._visit_suite(node.children[1]),
            )
        self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
        self.indent -= 1
        self._emit("}")
        if has_else:
            self._emit(self._loop_else_header(break_flag))
            self.indent += 1
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            self._visit_suite(node.children[2])
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
            self.indent -= 1
            self._emit("}")

    # ─── For ───────────────────────────────────────────────────

    def _flow_iterable_statically_nonempty(self, node) -> bool:
        if not isinstance(node, Tree):
            return False
        if node.data in {"list", "tuple", "set", "dict"}:
            return bool(node.children)
        if node.data != "funccall" or not node.children:
            return False
        callee = node.children[0]
        if not isinstance(callee, Tree) or callee.data != "var" or self._get_name(callee.children[0]) != "range":
            return False
        values = [self._flow_integer_literal(arg) for arg in self._get_raw_call_args(node.children[1] if len(node.children) > 1 else None)]
        if not values or any(value is None for value in values) or len(values) > 3:
            return False
        try:
            return len(range(*[int(value) for value in values])) > 0
        except ValueError:
            return False

    @staticmethod
    def _flow_integer_literal(node) -> int | None:
        if not isinstance(node, Tree):
            return None
        if node.data == "number" and node.children:
            try:
                return int(str(node.children[0]))
            except ValueError:
                return None
        if node.data == "factor" and len(node.children) == 2:
            value = StatementVisitorMixin._flow_integer_literal(node.children[1])
            if value is not None and str(node.children[0]) in {"+", "-"}:
                return value if str(node.children[0]) == "+" else -value
        return None

    @staticmethod
    def _flow_contains_loop_exit(node) -> bool:
        if not isinstance(node, Tree):
            return False
        if node.data in {"break_stmt", "continue_stmt"}:
            return True
        if node.data in {"funcdef", "classdef", "lambdef"}:
            return False
        return any(StatementVisitorMixin._flow_contains_loop_exit(child) for child in node.children)

    def _uniform_dict_literal_type(self, node) -> str:
        """``map[string]T`` for a dict literal with string keys whose values
        all have the same concrete Go type, else ``""``."""
        if not (isinstance(node, Tree) and node.data == "dict"):
            return ""
        value_types = set()
        for child in node.children:
            if not (isinstance(child, Tree) and child.data == "key_value" and len(child.children) >= 2):
                return ""
            if self._infer_type_from_value(child.children[0]) != "string":
                return ""
            value_types.add(self._expr_go_type_hint(child.children[1]) or self._infer_type_from_value(child.children[1]) or "")
        if len(value_types) != 1:
            # ``{"a": Square(), "b": Shape()}``: the least common base class.
            common = self._hier_common_base_go_type(value_types)
            return f"map[string]{common}" if common else ""
        value_type = value_types.pop()
        if not value_type or value_type == "interface{}":
            return ""
        return f"map[string]{value_type}"

    _NO_TYPE = object()

    def _visit_for_stmt(self, node: Tree):
        # A loop variable the loop declares (``for _, c := range``) lives
        # in the loop: it starts without the type of an earlier same-named
        # binding (``for c in rows`` before ``for c in text.split(",")``)
        # and its type does not outlive the loop.
        fresh = [
            go_name for go_name in (
                self._go_binding_name(name) for name in _narrowing.target_names(node.children[0])
            )
            if go_name != "_" and go_name not in self.declared_vars
        ]
        saved = {
            name: (
                self._var_go_types.pop(name, self._NO_TYPE),
                self._var_types.pop(name, self._NO_TYPE),
                self._var_result_payloads.pop(name, self._NO_TYPE),
            )
            for name in fresh
        }
        try:
            self._visit_for_stmt_inner(node)
        finally:
            for name, (go_type, class_name, payload) in saved.items():
                for table, value in (
                    (self._var_go_types, go_type), (self._var_types, class_name),
                    (self._var_result_payloads, payload),
                ):
                    if value is self._NO_TYPE:
                        table.pop(name, None)
                    else:
                        table[name] = value

    def _visit_for_stmt_inner(self, node: Tree):
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

        # The tracked type of a name first: ``_infer_type_from_value`` also
        # consults constructor parameter types, which another class's
        # ``init(self, rows: ...)`` can make stale for a local ``rows``.
        hint = self._expr_go_type_hint(iterable_node) if isinstance(iterable_node, Tree) and iterable_node.data == "var" else ""
        iterable_type = hint if hint and hint != "interface{}" else self._infer_type_from_value(iterable_node)
        if iterable_type in {"", "interface{}"} and isinstance(iterable_node, Tree) and iterable_node.data in {"funccall", "getattr"}:
            # ``for it in Bag().all()`` / ``for it in self.items``.
            iterable_type = self._expr_go_type_hint(iterable_node) or iterable_type
        if isinstance(iterable_node, Tree) and iterable_node.data == "list":
            # ``for q in [Queue.make(), other.queue()]``: elements whose
            # expression type is known (calls, members, unwraps) and shared;
            # elements of different known types iterate as ``interface{}``.
            iterable_type = self._list_literal_elem_hint_type(iterable_node, iterable_type)
        is_range = (
            isinstance(iterable_node, Tree)
            and iterable_node.data == "funccall"
            and bool(iterable_node.children)
            and isinstance(iterable_node.children[0], Tree)
            and iterable_node.children[0].data == "var"
            and self._get_name(iterable_node.children[0].children[0]) == "range"
        )
        if is_range:
            self._var_go_types[var_name] = "int"
        elif iterable_type.startswith("[]"):
            self._var_go_types[var_name] = iterable_type[2:]
            if iterable_type == "[]*Result":
                # ``for r in rows`` with ``rows: list[Result[int]]``:
                # ``r.unwrap()`` is an ``int``.
                payload = self._iterable_result_payload_go_type(iterable_node)
                if payload and payload != "interface{}":
                    self._var_result_payloads[var_name] = payload
        if self._flow_iterable_statically_nonempty(iterable_node) and not self._flow_contains_loop_exit(suite_node):
            self._hoist_flow_locals([suite_node])

        break_flag = self._new_loop_break_flag() if else_suite else ""
        if else_suite:
            self._declare_loop_break_flag(break_flag, suite_node)

        enumerate_info = self._check_enumerate_call(iterable_node)
        range_info = self._check_range_call(iterable_node)
        already_declared = var_name in self.declared_vars
        if enumerate_info:
            idx_name, val_name = self._split_tuple_target(target_node)
            iter_str = enumerate_info
            # ``enumerate(xs, start)``: the index counts from ``start``.
            start_go = self._enumerate_start_go(
                iterable_node, lambda name, value: self._emit(f"{name} := {value}"),
            ) if idx_name and idx_name != "_" else ""
            range_idx = f"{idx_name}__lamRaw" if start_go else idx_name
            raw_src = self._get_raw_call_args(iterable_node.children[1] if len(iterable_node.children) > 1 else None)
            src_node = raw_src[0] if raw_src else None
            src_type = (self._expr_go_type_hint(src_node) or self._infer_type_from_value(src_node) or "") if src_node is not None else ""
            if (
                isinstance(src_node, Tree) and src_node.data == "list"
                and src_type.startswith("[]") and src_type != "[]interface{}"
            ):
                # ``enumerate(["p", "q"])``: range over the typed slice.
                iter_str = self._typed_value_to_go(src_node, src_type)
            loop_names: list = []
            start_bind = f"\t{idx_name} := {range_idx} + {start_go}" if start_go else ""
            if idx_name and val_name and src_type == "string" and val_name != "_":
                # ``enumerate(text)``: character positions and one-character strs.
                self._emit(f"for {range_idx}, {val_name}Rune := range []rune({iter_str}) {{")
                if start_bind:
                    self._emit(start_bind)
                self._emit(f"\t{val_name} := string({val_name}Rune)")
                loop_names.append(idx_name)
                loop_names.append(val_name)
                if idx_name != "_":
                    self._var_go_types[idx_name] = "int"
                self._var_go_types[val_name] = "string"
            elif idx_name and val_name:
                self._emit(f"for {range_idx}, {val_name} := range {iter_str} {{")
                if start_bind:
                    self._emit(start_bind)
                loop_names.append(idx_name)
                loop_names.append(val_name)
                if idx_name != "_":
                    self._var_go_types[idx_name] = "int"
                if val_name != "_" and src_type.startswith("[]"):
                    self._var_go_types[val_name] = src_type[2:]
            else:
                self._emit(f"for {var_name} := range {iter_str} {{")
                loop_names.append(var_name)
            self.indent += 1
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            # Declared inside the loop scope, so an unused index is
            # silenced in the loop body (``for i, x in enumerate(xs)``).
            for loop_name in loop_names:
                self._declare_var(loop_name)
            with self._scoped(_loop_break_flag=break_flag, _loop_jump=None):
                self._visit_suite(suite_node)
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
            self.indent -= 1
            self._emit("}")
            if else_suite:
                self._emit(self._loop_else_header(break_flag))
                self.indent += 1
                self._push_scope()
                body_start, vars_at_start = self._scope_unused_tracking_start()
                self._visit_suite(else_suite)
                self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
                self.indent -= 1
                self._emit("}")
            return
        key_name, val_name = (None, None)
        string_char_bind = ""
        if range_info:
            start, end, step = range_info
            is_negative_step = step.lstrip().startswith("-") or step.lstrip().startswith("(−") or step.lstrip().startswith("(-")
            cmp_op = ">" if is_negative_step else "<"
            assign_op = "=" if already_declared else ":="
            if var_name == "_":
                # Go has no ``_`` loop counter; the value is unused anyway.
                self._q_temp_counter += 1
                var_name = f"_lamRangeIndex{self._q_temp_counter}"
                assign_op = ":="
            self._emit(f"for {var_name} {assign_op} {start}; {var_name} {cmp_op} {end}; {var_name} += {step} {{")
        else:
            dict_literal_type = self._uniform_dict_literal_type(iterable_node)
            if (
                isinstance(iterable_node, Tree)
                and iterable_node.data == "list"
                and iterable_type.startswith("[]")
                and iterable_type != "[]interface{}"
            ):
                # Range over the typed slice the loop variable was typed from.
                iter_str = self._typed_value_to_go(iterable_node, iterable_type)
            elif dict_literal_type:
                # ``for k, v in {"a": "x"}``: a typed map, so ``v`` is a string.
                iter_str = self._typed_dict_literal_to_go(iterable_node, dict_literal_type)
            elif (
                isinstance(iterable_node, Tree)
                and iterable_node.data == "propagate"
                and (iterable_type.startswith("[]") or iterable_type.startswith("map["))
                and iterable_type not in ("[]interface{}", "map[string]interface{}")
            ):
                # ``for x in self.items()?``: the payload has the iterable's type.
                with self._scoped(_propagate_cast_hint=iterable_type):
                    iter_str = self._expr_to_go(iterable_node)
            else:
                iter_str = self._expr_to_go(iterable_node)
            is_chan = self._is_generator_call(iterable_node) or self._is_chan_iterable(iterable_node)
            if is_chan:
                iter_str += self._generator_range_method(iterable_node)
            # ``for k, v in mapping`` — when the for-target is a
            # 2-tuple, range over the map directly so both names bind
            # idiomatically. Without this, the catch-all branch below
            # would emit ``for _, "k, v" := range`` (a Go syntax
            # error) because ``var_name`` carries the literal
            # comma-joined expression.
            if not is_chan:
                key_name, val_name = self._split_tuple_target(target_node)
            destructure = self._for_tuple_destructure_types(target_node, iterable_node, is_chan, dict_literal_type)
            if destructure is not None:
                targets, tuple_types = destructure
                self._q_temp_counter += 1
                pair_name = f"_lamForTuple{self._q_temp_counter}"
                if isinstance(iterable_node, Tree) and iterable_node.data == "list" and not iterable_type.startswith("[][]"):
                    iter_str = self._typed_value_to_go(iterable_node, "[][]interface{}")
                if is_chan:
                    self._emit(f"for {pair_name} := range {iter_str} {{")
                else:
                    self._emit(f"for _, {pair_name} := range {iter_str} {{")
                self.indent += 1
                self._push_scope()
                body_start, vars_at_start = self._scope_unused_tracking_start()
                for index, target in enumerate(targets):
                    if target == "_":
                        continue
                    elem_type = tuple_types[index] if index < len(tuple_types) else ""
                    cast = f".({elem_type})" if elem_type else ""
                    op = "=" if target in self.declared_vars else ":="
                    self._emit(f"{target} {op} {pair_name}[{index}]{cast}")
                    self._declare_var(target)
                    if elem_type:
                        self._var_go_types[target] = elem_type
                with self._scoped(_loop_break_flag=break_flag, _loop_jump=None):
                    self._visit_suite(suite_node)
                self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
                self.indent -= 1
                self._emit("}")
                self._emit_loop_else(else_suite, break_flag)
                return
            if is_chan:
                assign_op = "=" if already_declared else ":="
                self._bind_generator_loop_target(var_name, iterable_node)
                self._emit(f"for {var_name} {assign_op} range {iter_str} {{")
            elif key_name and val_name:
                k_decl = key_name in self.declared_vars
                v_decl = val_name in self.declared_vars
                # ``for k, v in vars`` over a ``dict[str, str]`` (parameter,
                # local, field or literal): both names have the map's key and
                # value types, so ``v if c else k`` stays a ``string``.
                map_type = dict_literal_type or (iterable_type or "").strip()
                key_type, value_type = self._split_go_map_type(map_type)
                for name, go_type, declared in ((key_name, key_type, k_decl), (val_name, value_type, v_decl)):
                    if name != "_" and not declared and go_type and go_type != "interface{}":
                        self._var_go_types[name] = go_type
                assign_op = "=" if (k_decl and v_decl) else ":="
                self._emit(f"for {key_name}, {val_name} {assign_op} range {iter_str} {{")
            elif self._range_single_target_uses_key(iterable_node):
                map_type = (self._expr_go_type_hint(iterable_node) or iterable_type or "").strip()
                key_type, _value_type = self._split_go_map_type(map_type)
                if key_type and key_type != "interface{}":
                    self._var_go_types[var_name] = key_type
                assign_op = "=" if already_declared else ":="
                self._emit(f"for {var_name} {assign_op} range {iter_str} {{")
            elif self._expr_go_type_hint(iterable_node) == "LamJSON":
                # ``for v in payload.tags``: a JSON array yields ``json`` items.
                self._emit(f"for _, {var_name}Raw := range lamJSONToList({iter_str}) {{")
                string_char_bind = f"{var_name} {'=' if already_declared else ':='} lamJSONWrapRaw({var_name}Raw)"
                self._var_go_types[var_name] = "LamJSON"
            elif self._iterates_string(iterable_node):
                # Lam yields one-character strings, Go ranges over runes.
                self._emit(f"for _, {var_name}Rune := range {iter_str} {{")
                string_char_bind = f"{var_name} {'=' if already_declared else ':='} string({var_name}Rune)"
                self._var_go_types[var_name] = "string"
            elif already_declared:
                self._emit(f"for _, {var_name} = range {iter_str} {{")
            else:
                self._emit(f"for _, {var_name} := range {iter_str} {{")

        self.indent += 1
        if string_char_bind:
            self._emit(string_char_bind)
        self._push_scope()
        body_start, vars_at_start = self._scope_unused_tracking_start()
        if key_name and val_name:
            self._declare_var(key_name)
            self._declare_var(val_name)
        else:
            self._declare_var(var_name)
        with self._scoped(_loop_break_flag=break_flag, _loop_jump=None):
            self._visit_suite(suite_node)
        self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
        self.indent -= 1
        self._emit("}")
        if else_suite:
            self._emit(self._loop_else_header(break_flag))
            self.indent += 1
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            self._visit_suite(else_suite)
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
            self.indent -= 1
            self._emit("}")

    def _declare_loop_break_flag(self, break_flag: str, body) -> None:
        """Declare the loop-else flag, unless the body never breaks out of
        this loop: then the ``else`` block always runs and is emitted as a
        plain block, so a ``return`` in it terminates the function for Go."""
        from compiler.semantic import SemanticChecker
        if SemanticChecker._suite_contains_own_break(body):
            self._emit(f"{break_flag} := false")
        else:
            self.__dict__.setdefault("_unbroken_loop_flags", set()).add(break_flag)

    def _loop_else_header(self, break_flag: str) -> str:
        if break_flag in self.__dict__.get("_unbroken_loop_flags", set()):
            return "{"
        return f"if !{break_flag} {{"

    def _emit_loop_else(self, else_suite, break_flag: str) -> None:
        if not else_suite:
            return
        self._emit(self._loop_else_header(break_flag))
        self.indent += 1
        self._push_scope()
        body_start, vars_at_start = self._scope_unused_tracking_start()
        self._visit_suite(else_suite)
        self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
        self.indent -= 1
        self._emit("}")

    def _list_literal_elem_hint_type(self, node, inferred: str) -> str:
        """The ``[]T`` to iterate the list literal ``node`` as: ``inferred``
        unless the elements' expression types say otherwise (one shared
        concrete ``T``, or ``[]interface{}`` when two known types differ)."""
        children = [c for c in node.children if c is not None]
        # ``for n in [["a"], []]``: the empty literal takes its siblings' type.
        types = {
            (self._expr_go_type_hint(c) or self._infer_type_from_value(c) or "").strip()
            for c in children if not self._is_empty_collection_literal(c)
        }
        if types and len(types) == 1:
            only = next(iter(types))
            if any(
                self._is_empty_collection_literal(c) and not self._empty_literal_fits(c, only)
                for c in children
            ):
                types.add("[]interface{}")
        known = {t for t in types if t not in {"", "interface{}", "any"}}
        if len(known) > 1:
            # ``for s in [Square(), Shape()]``: the least common base class.
            common = self._hier_common_base_go_type(known) if known == types else ""
            return "[]" + common if common else "[]interface{}"
        if len(known) == 1 and len(types) == 1:
            return "[]" + known.pop()
        return inferred

    def _literal_tuple_element_types(self, node) -> List[str]:
        """Shared per-position Go types of a list literal of tuple literals."""
        if not (isinstance(node, Tree) and node.data == "list" and node.children):
            return []
        shapes = []
        for item in node.children:
            if not (isinstance(item, Tree) and item.data == "tuple"):
                return []
            shapes.append(tuple(
                self._infer_type_from_value(c) for c in item.children if c is not None
            ))
        if any(shape != shapes[0] for shape in shapes):
            return []
        return [t if t != "interface{}" else "" for t in shapes[0]]

    def _for_tuple_destructure_types(self, target_node, iterable_node, is_chan: bool,
                                     dict_literal_type: str):
        """``(targets, element_types)`` when ``for a, b in xs`` must unpack.

        Applies to tuple targets over slices whose elements are tuples or
        nested slices (``list[tuple[str, int]]``, ``list[list[int]]``, a
        list literal of tuples) and over generators. Maps keep binding
        key/value. ``element_types`` holds the Go assertion per position
        (``""`` = none needed or unknown).
        """
        targets = self._comprehension_tuple_targets(target_node)
        if len(targets) < 2 or dict_literal_type:
            return None
        if self._range_single_target_uses_key(iterable_node):
            return None
        iter_name = (
            self._get_name(iterable_node.children[0])
            if isinstance(iterable_node, Tree) and iterable_node.data == "var" else ""
        )
        registered = self._tuple_iterable_element_types.get(iter_name, []) if iter_name else []
        literal_types = self._literal_tuple_element_types(iterable_node)
        go_type = (self._expr_go_type_hint(iterable_node)
                   or self._infer_type_from_value(iterable_node) or "").strip()
        if is_chan:
            return targets, []
        if registered:
            return targets, [t if t != "interface{}" else "" for t in registered]
        if literal_types:
            return targets, literal_types
        if go_type.startswith("[][]"):
            # Nested typed slices already yield typed elements; tuples
            # without a known shape yield ``interface{}`` positions.
            return targets, []
        return None

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

    def _bind_generator_loop_target(self, var_name: str, iterable_node) -> None:
        """``for batch in Csv.batches(path)``: the target has the declared
        element type of the generator (``*Result`` with its payload
        ``[][]string``, a class, ``generator[T]``'s ``T``), so
        ``batch.unwrap()`` / ``it.name`` are typed."""
        if var_name == "_":
            return
        elem = (self._expr_go_type_hint(iterable_node) or "").strip()
        if elem.startswith("chan "):
            elem = elem[5:].strip()
        elif generator_go_elem(elem) is not None:
            elem = generator_go_elem(elem)
        if elem and elem != "interface{}":
            self._var_go_types[var_name] = elem
            if elem.startswith("*"):
                class_name = self._hier_class_from_go_type(elem) or elem[1:].split("[", 1)[0]
                if class_name in self._class_names:
                    self._var_types[var_name] = class_name
        elem_payload = self._iterable_result_payload_go_type(iterable_node)
        if elem_payload and elem_payload != "interface{}" and elem in ("", "*Result"):
            # ``for r in src.each()`` with ``-> generator[Result[int]]``.
            self._var_go_types[var_name] = "*Result"
            self._var_result_payloads[var_name] = elem_payload
        elif elem == "*Result":
            payload = self._propagated_payload_go_type(iterable_node)
            if payload and payload != "interface{}":
                self._var_result_payloads[var_name] = payload

    def _is_chan_iterable(self, node) -> bool:
        """``for c in g`` with ``g: generator[Circle]`` (a
        ``*lamGenerator[*Circle]`` parameter, local or field) or a channel:
        one value per resume / receive."""
        hint = (self._expr_go_type_hint(node) or "").strip()
        return hint.startswith("chan ") or generator_go_elem(hint) is not None

    def _generator_range_method(self, node) -> str:
        """What a ``for`` loop or a comprehension ranges over for the
        generator ``node``: a generator call the loop owns ends when the
        loop is left (``F().Consume``); a generator value (variable, field,
        parameter) stays resumable (``g.Resume``). A channel is ranged
        over directly."""
        if self._is_generator_call(node):
            return ".Consume"
        if generator_go_elem(self._expr_go_type_hint(node) or "") is not None:
            return ".Resume"
        return ""

    def _generator_iter_elem_go_type(self, node) -> str:
        """Go type of the values received from the generator ``node`` (a
        call or a ``generator[T]`` value); ``""`` when unknown or dynamic."""
        hint = (self._expr_go_type_hint(node) or "").strip()
        if hint.startswith("chan "):
            hint = hint[5:].strip()
        elif generator_go_elem(hint) is not None:
            hint = generator_go_elem(hint)
        return "" if hint in {"", "interface{}"} else hint

    def _is_generator_call(self, node) -> bool:
        if not isinstance(node, Tree) or node.data != "funccall":
            return False
        func = node.children[0]
        if isinstance(func, Tree) and func.data == "var":
            name = self._get_name(func.children[0])
            if name in self._nested_generator_elems and name in self.declared_vars:
                return True
            return name in self._generator_functions
        if isinstance(func, Tree) and func.data == "getattr" and len(func.children) >= 2:
            receiver = func.children[0]
            method_name = self._get_name(func.children[1])
            if isinstance(receiver, Tree) and receiver.data == "var" and receiver.children:
                class_name = self._get_name(receiver.children[0])
                if f"{class_name}.{method_name}" in self._generator_functions:
                    return True
            # ``for w in src.words()`` / ``Source(xs).words()``: an
            # instance-method generator of the receiver's class (or a base).
            receiver_class = self._infer_receiver_class(receiver)
            if receiver_class:
                for cls in [receiver_class] + self._hier_ancestors(receiver_class):
                    if f"{cls}.{method_name}" in self._generator_functions:
                        return True
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

        self._emit_try(try_suite, catch_clauses_node, else_suite, finally_node)

    def _visit_try_finally(self, node: Tree):
        try_suite = node.children[0]
        finally_node = node.children[1] if len(node.children) > 1 else None
        if isinstance(finally_node, Tree) and finally_node.data != "finally":
            finally_node = Tree("finally", [finally_node])
        self._emit_try(try_suite, None, None, finally_node if isinstance(finally_node, Tree) else None)

    def _emit_try(self, try_suite, catch_clauses_node, else_suite, finally_node, with_items=None) -> None:
        """Lower ``try`` / ``catch`` / ``else`` / ``finally``.

        The try body runs in an IIFE whose deferred functions are the
        ``finally`` block and (with catch clauses) the recover dispatch, so
        ``finally`` runs when the ``try`` statement ends (also once per loop
        iteration) and a recovered exception continues after the statement.
        ``return`` / ``break`` / ``continue`` inside the closures record
        themselves and leave the IIFE; the code after it performs them.

        ``with_items`` (a ``with`` statement, ``try { body } finally {
        close }``): the resources are opened at the start of the IIFE, each
        followed by the ``defer`` closing it, so they close when the block
        ends, in reverse order, whichever way it is left.
        """
        clauses = [
            clause for clause in (catch_clauses_node.children if catch_clauses_node is not None else [])
            if isinstance(clause, Tree) and clause.data == "catch_clause"
        ]
        finally_suite = finally_node.children[0] if finally_node is not None else None
        if not clauses and finally_suite is None and with_items is None:
            self._emit("// try")
            self._push_scope()
            body_start, vars_at_start = self._scope_unused_tracking_start()
            self._visit_suite(try_suite)
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
            return

        # A local assigned on every continuing path (the try body and
        # each catch body) is declared before the IIFE so it survives it
        # (``try { v: int = f() } catch e { v: int = -1 }; return v``).
        catch_suites = [
            sub
            for clause in clauses
            for sub in clause.children
            if isinstance(sub, Tree) and sub.data == "suite"
        ]
        if with_items is None:
            # (The locals of a ``with`` block stay in the block.)
            self._hoist_flow_locals([try_suite] + catch_suites)
        # With both ``else`` and ``finally``, ``else`` runs inside the IIFE
        # holding the ``finally`` defer (Python order: body, else, finally)
        # but outside the recover dispatch (its exceptions are not handled by
        # the statement's own clauses).
        nested_else = else_suite is not None and finally_suite is not None
        jump = self._declare_loop_jump(
            [try_suite, finally_suite] + catch_suites + ([else_suite] if nested_else else [])
        )
        loop_jump = (jump[0], "return") if jump else None
        # ``else`` runs only when no exception was caught: a flag of this
        # statement (a nested ``try`` in the body catching its own exception
        # must not skip it).
        caught = ""
        if else_suite is not None and clauses:
            self._try_counter = getattr(self, "_try_counter", 0) + 1
            caught = f"__lamCaught{self._try_counter}"
            self._emit(f"{caught} := false")

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
        self._emit("// with" if with_items is not None else "// try")
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
        iife_body_start, iife_vars_at_start = self._scope_unused_tracking_start()

        # Defers run LIFO — registering the finally first means the
        # catch's recover runs first when a panic unwinds, which is
        # the correct semantics. If the catch re-panics (no clause
        # matched, etc.) the finally still runs because of the
        # second defer.
        if finally_suite is not None:
            self._emit_finally_defer(finally_suite, jump)

        if nested_else:
            # The body and its recover dispatch get an IIFE of their own.
            self._emit("func() {")
            self.indent += 1
            self._push_scope()
            inner_body_start, inner_vars_at_start = self._scope_unused_tracking_start()

        if clauses:
            self._emit("defer func() {")
            self.indent += 1
            self._emit("if r := recover(); r != nil {")
            self.indent += 1
            self._emit("__lamPanicked = true")
            if caught:
                self._emit(f"{caught} = true")
            self._push_scope()
            self._declare_var("r")
            recover_body_start, recover_vars_at_start = self._scope_unused_tracking_start()
            # Catch bodies are also inside the IIFE: flip ``_in_try_iife``
            # so ``return X`` from a catch handler also funnels through
            # ``__lamShouldReturn`` and propagates to the outer function.
            # ``_in_recover_block`` is flipped too so a bare ``raise``
            # inside the catch re-panics the live ``r`` value instead of
            # emitting a placeholder string.
            with self._scoped(_in_try_iife=True, _in_recover_block=True, _loop_jump=loop_jump):
                self._emit_catch_dispatch(clauses)
            self._pop_scope_with_unused_local_silencers(recover_body_start, recover_vars_at_start)
            self.indent -= 1
            self._emit("}")
            self.indent -= 1
            self._emit("}()")

        # Try body — inside the IIFE, with ``_in_try_iife`` flipped
        # so ``return X`` rewrites to (``retval = X;``)
        # ``__lamShouldReturn = true; return``. The bare return
        # exits this IIFE only; the post-IIFE check forwards the
        # signal to the outer function.
        with self._scoped(_in_try_iife=True, _loop_jump=loop_jump):
            if with_items is not None:
                self._emit_with_items(with_items)
            self._visit_suite(try_suite)

        if nested_else:
            self._pop_scope_with_unused_local_silencers(inner_body_start, inner_vars_at_start)
            self.indent -= 1
            self._emit("}()")
            # A return / loop jump out of the body or a handler skips
            # ``else``: leave the outer IIFE (``finally`` runs).
            leave_cond = "__lamShouldReturn" + (f" || {jump[0]} != 0" if jump else "")
            self._emit(f"if {leave_cond} {{ return }}")
            self._emit_else_suite(else_suite, caught, in_iife=True, loop_jump=loop_jump)

        self._pop_scope_with_unused_local_silencers(iife_body_start, iife_vars_at_start)
        self.indent -= 1
        self._emit("}()")

        # Propagate any return signalled from inside the IIFE.
        # When the function has a typed return value the named
        # ``retval`` already holds the value; a bare ``return``
        # surfaces it.
        if (self._do_return_target or self._in_do_body) and not self._in_try_iife:
            # Directly inside a ``do`` body (a ``*Result`` closure): the
            # recorded return happens after the ``do`` IIFE; a ``?`` error
            # is the closure's result.
            self._emit(f"if __lamShouldReturn {{ return {self._do_q_target or 'nil'} }}")
        else:
            self._emit("if __lamShouldReturn { return }")
        self._emit_loop_jump_dispatch(jump)

        # Without ``finally``, ``else`` follows the IIFE: it runs only when
        # the body completed (no exception caught, no return or loop jump),
        # and its exceptions are not handled by the statement's clauses.
        if else_suite is not None and not nested_else:
            self._emit_else_suite(else_suite, caught, in_iife=False, loop_jump=self._loop_jump)

    def _emit_else_suite(self, else_suite, caught: str, *, in_iife: bool, loop_jump) -> None:
        if caught:
            self._emit(f"if !{caught} {{")
        else:
            self._emit("{")
        self.indent += 1
        self._push_scope()
        body_start, vars_at_start = self._scope_unused_tracking_start()
        if in_iife:
            with self._scoped(_in_try_iife=True, _loop_jump=loop_jump):
                self._visit_suite(else_suite)
        else:
            self._visit_suite(else_suite)
        self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
        self.indent -= 1
        self._emit("}")

    @staticmethod
    def _suite_has_own_return(node) -> bool:
        """A ``return`` (or a ``?``, which returns) in ``node`` that leaves
        the enclosing function (not one of a nested function or lambda)."""
        if not isinstance(node, Tree):
            return False
        if node.data in {"return_stmt", "propagate"}:
            return True
        if node.data in {"funcdef", "classdef", "lambdef"}:
            return False
        return any(StatementVisitorMixin._suite_has_own_return(c) for c in node.children)

    def _emit_finally_defer(self, finally_suite, jump) -> None:
        """The ``defer`` running a ``finally`` block.

        A ``return`` / ``break`` / ``continue`` in ``finally`` (Python
        semantics) cancels an exception that is propagating — the defer
        recovers it first and re-raises it only when the block completes
        normally — and replaces a pending ``return`` or loop jump of the
        body (a jump clears ``__lamShouldReturn``; a ``return`` overwrites
        the named result). A ``try`` inside the block keeps a pending
        ``return`` of the body (it resets the shared flag).
        """
        exits = bool(self._own_loop_jumps(finally_suite)) or self._suite_has_own_return(finally_suite)
        nested_try = any(True for _ in finally_suite.find_pred(
            lambda t: t.data in {"try_stmt", "try_finally", "with_stmt"}
        ))
        self._try_counter = getattr(self, "_try_counter", 0) + 1
        n = self._try_counter
        self._emit("defer func() {")
        self.indent += 1
        self._push_scope()
        finally_body_start, finally_vars_at_start = self._scope_unused_tracking_start()
        if exits:
            self._emit(f"__lamFinExc{n} := recover()")
        if nested_try:
            self._emit(f"__lamFinRet{n} := __lamShouldReturn")
        leave = "__lamShouldReturn = false; return"
        fin_jump = (jump[0], leave) if jump else None
        # A ``return`` / loop jump in ``finally`` leaves the deferred
        # function and is performed after the IIFE, like one in the body.
        with self._scoped(_in_try_iife=True, _loop_jump=fin_jump):
            self._visit_suite(finally_suite)
        if nested_try:
            self._emit(f"__lamShouldReturn = __lamShouldReturn || __lamFinRet{n}")
        if exits:
            self._emit(f"if __lamFinExc{n} != nil {{ panic(__lamFinExc{n}) }}")
        self._pop_scope_with_unused_local_silencers(finally_body_start, finally_vars_at_start)
        self.indent -= 1
        self._emit("}()")

    # Builtin exception families: ``catch Parent`` also catches every
    # name listed for it (Python's hierarchy, restricted to the names Lam
    # knows). ``Exception`` / ``BaseException`` catch everything.
    _BUILTIN_EXCEPTION_FAMILIES = {
        "ArithmeticError": ("ArithmeticError", "ZeroDivisionError", "OverflowError"),
        "LookupError": ("LookupError", "KeyError", "IndexError"),
        "OSError": ("OSError", "IOError", "FileNotFoundError"),
        "IOError": ("OSError", "IOError", "FileNotFoundError"),
        "RuntimeError": ("RuntimeError", "NotImplementedError"),
        "ValueError": ("ValueError", "ParseError"),
    }
    _CATCH_ALL_EXCEPTIONS = ("Exception", "BaseException")

    def _parse_catch_clause(self, clause: Tree):
        """Return ``(type_name, type_is_go_expr, as_name, suite)``.

        ``type_name`` is the raw Lam name of the caught type (``None`` for
        a bare ``catch``); ``catch e`` (a lowercase, non-builtin name with
        no ``as``) is a catch-all binding ``e``.
        """
        exception_type = None
        type_is_go_expr = False
        as_name = None
        suite = None

        for child in clause.children:
            if child is None:
                continue
            if isinstance(child, Tree):
                if child.data == "suite":
                    suite = child
                elif child.data == "var":
                    exception_type = self._get_name(child.children[0])
                elif child.data == "name":
                    if exception_type is not None:
                        as_name = self._get_name(child)
                    else:
                        exception_type = self._get_name(child)
                else:
                    if exception_type is None:
                        exception_type = self._expr_to_go(child)
                        type_is_go_expr = True
            elif isinstance(child, Token):
                if child.type == "NAME":
                    if exception_type is not None and as_name is None:
                        as_name = str(child)
                    elif exception_type is None:
                        exception_type = str(child)

        # ``catch e`` / ``catch _e`` (a lowercase or ``_``-prefixed name that
        # is not a known exception, class or interface) binds the value.
        if (as_name is None and exception_type and not type_is_go_expr
                and (exception_type[0].islower() or exception_type[0] == "_")
                and exception_type not in PYTHON_EXCEPTIONS
                and exception_type not in self._class_names
                and exception_type not in self._interfaces):
            as_name = exception_type
            exception_type = None
        return exception_type, type_is_go_expr, as_name, suite

    def _class_and_local_descendants(self, class_name: str) -> List[str]:
        """``class_name`` plus every class of this module deriving from it."""
        out = [class_name]
        changed = True
        while changed:
            changed = False
            for child, bases in self.class_bases.items():
                if child not in out and any(b in out for b in bases):
                    out.append(child)
                    changed = True
        return out

    def _catch_condition(self, exception_type: Optional[str], type_is_go_expr: bool) -> Optional[str]:
        """Go condition selecting a catch clause, or ``None`` for catch-all.

        Builtin exceptions are raised as ``"Name: message"`` strings and Go
        runtime faults surface as ``runtime.Error``; both are classified
        once per recover into ``__lamExcKind``. Class exceptions match on
        the dynamic pointer type (including subclasses declared in the same
        module); interfaces match on method sets.
        """
        if exception_type is None or exception_type in self._CATCH_ALL_EXCEPTIONS:
            return None
        if not type_is_go_expr and exception_type in PYTHON_EXCEPTIONS | {"ParseError"}:
            names = self._BUILTIN_EXCEPTION_FAMILIES.get(exception_type, (exception_type,))
            return " || ".join(f'__lamExcKind == "{n}"' for n in names)
        if type_is_go_expr:
            return f"func() bool {{ _, ok := r.(*{exception_type}); return ok }}()"
        if exception_type in self._interfaces:
            go_iface = self._go_class_name(exception_type)
            return f"func() bool {{ _, ok := r.({go_iface}); return ok }}()"
        if self._hier_dynamic_target(exception_type):
            # The raised object (or the most-derived object behind a
            # base-typed one) is an ``exception_type`` or a subclass of it,
            # wherever the subclass is declared.
            return (
                f"func() bool {{ _, ok := {self._hier_dyn_value_go('r')}"
                f".({self._hier_is_iface(exception_type)}); return ok }}()"
            )
        types = ", ".join(
            "*" + self._go_class_name(name)
            for name in self._class_and_local_descendants(exception_type)
            if not self._generic_classes.get(name)
        )
        if not types:
            return None
        return f"func() bool {{ switch r.(type) {{ case {types}: return true }}; return false }}()"

    def _emit_exception_kind(self) -> None:
        self._need_import("strings")
        self._need_import("runtime")
        self._emit("__lamExcKind := func(v interface{}) string {")
        self.indent += 1
        self._emit("switch x := v.(type) {")
        self._emit("case string:")
        self._emit("\tif i := strings.IndexByte(x, ':'); i >= 0 { return x[:i] }")
        self._emit("\treturn x")
        self._emit("case runtime.Error:")
        self._emit("\tm := x.Error()")
        self._emit('\tif strings.Contains(m, "divide by zero") { return "ZeroDivisionError" }')
        self._emit('\tif strings.Contains(m, "out of range") { return "IndexError" }')
        self._emit('\tif strings.Contains(m, "nil pointer") || strings.Contains(m, "nil map") { return "AttributeError" }')
        self._emit('\tif strings.Contains(m, "interface conversion") { return "TypeError" }')
        self._emit('\treturn "RuntimeError"')
        self._emit("}")
        self._emit('return ""')
        self.indent -= 1
        self._emit("}(r)")

    def _emit_catch_dispatch(self, clauses: List[Tree]) -> None:
        """Emit the clause chain inside ``if r := recover(); r != nil``.

        Only the first clause whose type matches runs. Without a
        catch-all clause, an unmatched exception is re-raised so outer
        ``try`` blocks (and the ``finally`` defer) still see it.
        """
        parsed = []
        for clause in clauses:
            exception_type, is_go_expr, as_name, suite = self._parse_catch_clause(clause)
            cond = self._catch_condition(exception_type, is_go_expr)
            class_type = exception_type if (cond is not None and not is_go_expr) else None
            parsed.append((cond, as_name, suite, class_type))
            if cond is None:
                break  # later clauses are unreachable
        if any(cond and "__lamExcKind" in cond for cond, _, _, _ in parsed):
            self._emit_exception_kind()
        if parsed and parsed[0][0] is None:
            _, as_name, suite, _ = parsed[0]
            self._emit_catch_clause(as_name, suite)
            return
        for i, (cond, as_name, suite, class_type) in enumerate(parsed):
            if cond is None:
                self._emit("} else {")
            else:
                self._emit(f"{'if' if i == 0 else '} else if'} {cond} {{")
            self.indent += 1
            self._emit_catch_clause(as_name, suite, class_type)
            self.indent -= 1
        if parsed[-1][0] is not None:
            self._emit("} else {")
            self._emit("\tpanic(r)")
        self._emit("}")

    def _do_catch_error_class(self) -> str:
        """``Error`` when the program links lamerrors' ``Error`` (the
        ``do/catch`` binding type) and this module declares no class of
        that name; ``""`` keeps the raw value."""
        if "Error" not in self._class_names or "Error" in self.class_bases:
            return ""
        if self._func_param_counts.get("Error.init") != 3:
            return ""
        return "Error"

    def _catch_binding_class(self, exception_type: Optional[str]) -> str:
        """The user class a ``catch Cls as e`` binding is typed as."""
        if (
            not exception_type
            or exception_type not in self._class_names
            or exception_type in self._interfaces
            or exception_type in PYTHON_EXCEPTIONS
            or self._generic_classes.get(exception_type)
            or self._class_type_param_names_of(exception_type)
        ):
            return ""
        return exception_type

    def _emit_catch_clause(self, as_name: Optional[str], suite: Optional[Tree],
                           exception_type: Optional[str] = None):

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
        if as_name == "_":
            # ``catch _`` / ``catch ValueError as _``: nothing is bound.
            as_name = None
        binding_class = self._catch_binding_class(exception_type) if as_name else ""
        if as_name and binding_class:
            # ``catch AppError as e``: the clause matched, so bind the
            # caught object typed as the class (a subclass instance, also
            # from another module, binds its ``AppError`` view) and
            # ``e.message`` / ``e.method()`` work directly.
            as_name = self._go_binding_name(as_name)
            go_cls = self._go_class_name(binding_class)
            if self._hier_dynamic_target(binding_class):
                self._emit(f"{as_name}, _ := {self._hier_downcast_go('r', '', binding_class)}")
            else:
                self._emit(f"{as_name}, _ := r.(*{go_cls})")
            self._emit(f'_ = {as_name}')
            self._var_go_types[as_name] = f"*{go_cls}"
            self._var_types[as_name] = binding_class
        elif as_name:
            as_name = self._go_binding_name(as_name)
            self._emit(f'{as_name} := r')
            self._emit(f'_ = {as_name}')
            # Track the binding as ``interface{}`` so downstream
            # ``local: ClassName = {as_name}`` annassigns trigger the
            # class-pointer coercion path.
            self._var_go_types[as_name] = "interface{}"
            self._var_types.pop(as_name, None)

        if suite:
            self._push_scope()
            if as_name:
                self._declare_var(as_name)
            with self._scoped(_in_except_handler=True):
                body_start, vars_at_start = self._scope_unused_tracking_start()
                self._visit_suite(suite)
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)

    # ─── Match / Switch ────────────────────────────────────────

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
        the semantic checker requires ``from lamerrors import Result``
        before this emitter runs, so missing support imports point at
        Lam source instead of generated Go.
        """
        body_suite = node.children[0]
        err_name_node = node.children[1]
        handler_suite = node.children[2]
        err_name = self._go_binding_name(self._get_name(err_name_node))

        self._do_counter += 1
        rdo = f"__rdo{self._do_counter}"

        # ``return`` inside the body must leave the *enclosing* function,
        # but the body is a ``*Result`` closure: record the value and a
        # flag, leave the closure, then return for real afterwards.
        return_type = self._current_return_type or ""
        do_return_target = None
        if self._tree_contains(body_suite, "return_stmt") and not return_type.startswith("("):
            ret_flag = f"__lamDoRet{self._do_counter}"
            ret_var = f"__lamDoRetV{self._do_counter}" if return_type else ""
            self._emit(f"{ret_flag} := false")
            if ret_var:
                self._emit(f"var {ret_var} {return_type}")
            do_return_target = (ret_flag, ret_var)

        # A ``?`` inside a ``try`` of the body leaves the try IIFE first: it
        # stores its error in the closure's named result.
        # ``break`` / ``continue`` of an enclosing loop in the body leave
        # the closure (``return nil``) and are performed after it.
        jump = self._declare_loop_jump([body_suite])
        do_q_target = ""
        if any(
            self._tree_contains(t, "propagate")
            for t in (body_suite.iter_subtrees() if isinstance(body_suite, Tree) else [])
            if t.data in ("try_stmt", "try_finally", "with_stmt")
        ):
            do_q_target = f"__lamDoErr{self._do_counter}"
            self._emit(f"{rdo} := func() ({do_q_target} *Result) {{")
        else:
            self._emit(f"{rdo} := func() *Result {{")
        self.indent += 1
        # Push a fresh declared-vars scope so locals introduced inside
        # the IIFE (including ``__qN`` temps) don't leak back into the
        # surrounding function — and so a *second* ``do`` block re-
        # declares its own ``var x int = ...`` rather than treating
        # ``x`` as already declared from a previous IIFE.
        self._push_scope()
        body_start, vars_at_start = self._scope_unused_tracking_start()
        try:
            # Inside the ``do { }`` IIFE, ``?`` propagates to the IIFE
            # itself (which returns ``*Result``), not the enclosing
            # function — so ``_q_propagate_ok`` is True here even if
            # the outer function returns something non-Result.
            boundary = self._callable_boundary_context()
            boundary["_do_return_target"] = do_return_target
            boundary["_in_do_body"] = True
            boundary["_do_q_target"] = do_q_target
            boundary["_loop_jump"] = (jump[0], "return nil") if jump else None
            # A ``return`` in the body returns from the enclosing function.
            boundary["_in_nested_callable"] = self._in_nested_callable
            with self._scoped(_q_propagate_ok=True, **boundary):
                if isinstance(body_suite, Tree) and body_suite.data == "suite":
                    for stmt in body_suite.children:
                        if isinstance(stmt, Tree):
                            self._visit(stmt)
                # Implicit success-tail: if control reaches the end of the
                # body without ``?`` short-circuiting, we synthesise an
                # ``Ok(nil)`` so the IIFE always returns a non-nil ``*Result``.
                self._emit("return Result_Ok(nil)")
        finally:
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
        self.indent -= 1
        self._emit("}()")
        if do_return_target is not None:
            ret_flag, ret_var = do_return_target
            self._emit(f"if {ret_flag} {{")
            self.indent += 1
            self._emit_return_value(ret_var or None)
            self.indent -= 1
            self._emit("}")
        self._emit_loop_jump_dispatch(jump)

        self._emit(f"if {rdo}.Error != nil {{")
        self.indent += 1
        # Handler scope: ``err_name`` lives only here, plus any locals
        # the handler introduces.
        self._push_scope()
        body_start, vars_at_start = self._scope_unused_tracking_start()
        try:
            error_class = self._do_catch_error_class()
            if err_name == "_":
                # ``catch _ { ... }``: the error is not bound (Go rejects
                # ``_ := ...``, which declares no new variable).
                pass
            elif error_class:
                # ``err`` is an ``Error`` (lamerrors): an ``Error`` (or a
                # subclass, viewed as its ``Error``) as is, any other value
                # wrapped with an empty kind and its text as the message.
                self._need_import("fmt")
                go_err = self._go_class_name(error_class)
                self._emit(
                    f"{err_name} := func(v interface{{}}) *{go_err} {{ "
                    f"if e, ok := v.(*{go_err}); ok {{ return e }}; "
                    f"if e, ok := v.(interface{{ _lamIs_{go_err}() *{go_err} }}); ok {{ return e._lamIs_{go_err}() }}; "
                    f'return New{go_err}("", fmt.Sprint(v), v) }}({rdo}.Error)'
                )
                self._var_go_types[err_name] = f"*{go_err}"
                self._var_types[err_name] = error_class
            else:
                self._emit(f"{err_name} := {rdo}.Error")
            if err_name != "_":
                self._declare_var(err_name)
            if isinstance(handler_suite, Tree) and handler_suite.data == "suite":
                for stmt in handler_suite.children:
                    if isinstance(stmt, Tree):
                        self._visit(stmt)
        finally:
            self._pop_scope_with_unused_local_silencers(body_start, vars_at_start)
        self.indent -= 1
        self._emit("}")
        from compiler.semantic import SemanticChecker
        if SemanticChecker._stmt_definitely_terminates(node):
            # Body and handler both return / raise: control never gets
            # here, and Go needs a terminating statement to see it.
            self._emit('panic("unreachable: do/catch")')

    # ─── With ──────────────────────────────────────────────────

    def _visit_with_stmt(self, node: Tree):
        """``with a as x, b { body }`` is ``try { body } finally { close }``
        (Python's semantics without ``__exit__``): each resource is closed
        (``.Close()``, Lam's ``close``) when the block ends — normally, by
        ``return`` (the function still returns the value), by ``break`` /
        ``continue`` of an enclosing loop, by an exception or a ``?`` —
        before the code after the block, in reverse order."""
        self._emit_try(node.children[1], None, None, None, with_items=node.children[0])

    def _emit_with_items(self, with_items) -> None:
        """Open the resources of a ``with`` (inside its IIFE), each followed
        by the ``defer`` closing it; an ``as`` name has the resource's type
        and lives in the block."""
        if not (isinstance(with_items, Tree) and with_items.data == "with_items"):
            return
        for item in with_items.children:
            if not (isinstance(item, Tree) and item.data == "with_item"):
                continue
            expr = item.children[0]
            as_name = item.children[1] if len(item.children) > 1 and item.children[1] is not None else None
            expr_str = self._expr_to_go(expr)
            if as_name:
                name_str = self._go_binding_name(self._get_name(as_name))
                # ``with file as f``: ``f`` has the resource's type.
                resource_class = self._infer_receiver_class(expr)
                if resource_class:
                    self._var_types[name_str] = resource_class
                else:
                    self._var_types.pop(name_str, None)
                resource_type = self._expr_go_type_hint(expr)
                if resource_type:
                    self._var_go_types[name_str] = resource_type
                else:
                    self._var_go_types.pop(name_str, None)
            else:
                self._with_counter = getattr(self, "_with_counter", 0) + 1
                name_str = f"__lamWith{self._with_counter}"
            self._declare_var(name_str)
            self._emit(f"{name_str} := {expr_str}")
            self._emit(f"defer {name_str}.Close()")

    # ─── Decorated ─────────────────────────────────────────────

    def _visit_decorated(self, node: Tree):
        for child in node.children:
            if isinstance(child, Tree) and child.data in ("funcdef", "classdef"):
                self._visit(child)

    # ─── Go! block (preprocessed) ──────────────────────────────

    def _visit_go_block(self, node: Tree):
        pass
