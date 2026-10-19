#!/usr/bin/env python3
"""isinstance narrowing: Go lowering.

Where :mod:`compiler.narrowing` says ``isinstance(x, T)`` holds, the
transpiler binds a *shadow* of the local at the start of the region, in a
Go block scope of its own, and types the name as ``T`` while the region
is lowered (the same registries a ``match`` binding uses)::

    if <test> {
        x, _ := <x viewed as T>
        _ = x
        ...
    }

The view is a hierarchy downcast through the most-derived object
(``_lamDyn()`` / ``_lamIs_<T>()``, so overrides keep dispatching), a type
assertion for ``any`` and interfaces (``lamJSONRaw(x)`` first for
``json``), a dereference for an ``optional`` value type, or nothing at all
when the Go type does not change (``optional[C]`` to ``C``). The right
operand of ``and`` / ``or`` and the branches of a ternary bind the shadow
inside a closure; the statements after an early-exit guard are wrapped in
``{ ... }``. A local the region assigns is not narrowed (the shadow would
take the assignment), which the semantic checker mirrors.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import List, Optional, Sequence

from lark import Tree

from compiler import narrowing
from compiler.constants import TYPE_MAP
from compiler.typesys import DictType, GenericType, ListType, NamedType, Type, UnionType, render_type

_BUILTIN_TARGETS = {
    "int": NamedType("int"), "float": NamedType("float"), "str": NamedType("str"),
    "bool": NamedType("bool"),
}
_INT_NAMES = {"int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16",
              "uint32", "uint64", "byte", "rune"}

_SEQ_VIEW = (
    "func(v interface{{}}) []interface{{}} {{ if s, ok := v.([]interface{{}}); ok {{ return s }}; "
    "rv := reflect.ValueOf(v); if rv.Kind() != reflect.Slice && rv.Kind() != reflect.Array {{ return nil }}; "
    "out := make([]interface{{}}, rv.Len()); for i := range out {{ out[i] = rv.Index(i).Interface() }}; "
    "return out }}({x})"
)
_MAP_VIEW = (
    "func(v interface{{}}) map[string]interface{{}} {{ if m, ok := v.(map[string]interface{{}}); ok {{ return m }}; "
    "rv := reflect.ValueOf(v); if rv.Kind() != reflect.Map {{ return nil }}; "
    "out := make(map[string]interface{{}}, rv.Len()); iter := rv.MapRange(); "
    "for iter.Next() {{ out[fmt.Sprint(iter.Key().Interface())] = iter.Value().Interface() }}; "
    "return out }}({x})"
)


class NarrowingLowerMixin:
    """Lowering side of isinstance narrowing (see the module docstring)."""

    # ─── Types (mirrors ``compiler.semantic_narrowing``) ───────

    def _nr_class_known(self, name: str) -> bool:
        return name in self._class_names and name not in self._interfaces

    def _nr_kind(self, t: Optional[Type]) -> str:
        if t is None or isinstance(t, UnionType):
            return "dynamic"
        if isinstance(t, ListType):
            return "list"
        if isinstance(t, DictType):
            return "dict"
        if isinstance(t, GenericType):
            if t.base in {"optional", "Option"}:
                return "optional"
            if self._nr_class_known(t.base):
                return "class"
            return "other"
        if isinstance(t, NamedType):
            name = t.name
            if name in {"any", "object", "list", "tuple", "dict"} or name in self._generic_names:
                return "dynamic"
            if name == "json":
                return "json"
            if name in _INT_NAMES:
                return "int"
            if name in {"float", "float32", "float64"}:
                return "float"
            if name in {"str", "string"}:
                return "str"
            if name == "bool":
                return "bool"
            if name in self._interfaces:
                return "interface"
            if self._nr_class_known(name):
                return "class"
        return "other"

    def _nr_class_of(self, t: Optional[Type]) -> str:
        if isinstance(t, NamedType) and self._nr_class_known(t.name):
            return t.name
        if isinstance(t, GenericType) and t.base in {"optional", "Option"} and t.args:
            return self._nr_class_of(t.args[0])
        if isinstance(t, GenericType) and self._nr_class_known(t.base):
            return t.base
        return ""

    def _nr_strict_subclass(self, child: str, base: str) -> bool:
        return child != base and self._hier_is_subclass(child, base)

    def _nr_generic(self, name: str) -> bool:
        return bool(self._generic_classes.get(name) or self._class_type_param_names_of(name))

    def _nr_narrowed_type(self, current: Optional[Type], target) -> Optional[Type]:
        if target is narrowing.NOT_NONE:
            if isinstance(current, GenericType) and current.base in {"optional", "Option"} and current.args:
                return current.args[0]
            return None
        results = [self._nr_narrow_one(current, alt) for alt in narrowing.type_alternatives(target)]
        if not results or any(r is None for r in results):
            return None
        rendered = {render_type(r) for r in results}
        if len(rendered) == 1:
            return results[0]
        names = [r.name for r in results if isinstance(r, NamedType)]
        if len(names) != len(results) or not all(self._nr_class_known(n) for n in names):
            return None
        for candidate in [names[0]] + self._hier_ancestors(names[0]):
            if all(n == candidate or self._hier_is_subclass(n, candidate) for n in names):
                if current is not None and render_type(current) == candidate:
                    return None
                current_class = self._nr_class_of(current)
                if current_class and candidate != current_class and not self._hier_is_subclass(candidate, current_class):
                    return None
                return NamedType(candidate)
        return None

    def _nr_narrow_one(self, current: Optional[Type], alt) -> Optional[Type]:
        name = narrowing.type_target_name(alt)
        if not name or name == "None":
            return None
        kind = self._nr_kind(current)
        if kind == "optional":
            inner = current.args[0] if current.args else None
            if inner is None:
                return None
            inner_name = render_type(inner)
            if name == inner_name or (name in _BUILTIN_TARGETS and render_type(_BUILTIN_TARGETS[name]) == inner_name):
                return inner
            inner_class = self._nr_class_of(inner)
            if inner_class and self._nr_class_known(name) and not self._nr_generic(name) \
                    and self._nr_strict_subclass(name, inner_class):
                return NamedType(name)
            return None
        is_user_type = self._nr_class_known(name) or name in self._interfaces
        if not is_user_type and name in {"int", "float", "str", "bool", "list", "dict"}:
            if kind == "dynamic":
                if name == "list":
                    return ListType(NamedType("any"))
                if name == "dict":
                    return DictType(NamedType("str"), NamedType("any"))
                return _BUILTIN_TARGETS[name]
            if kind == "json" and name in _BUILTIN_TARGETS:
                return _BUILTIN_TARGETS[name]
            return None
        if name in self._interfaces:
            if kind == "dynamic" or (kind == "interface" and isinstance(current, NamedType) and current.name != name):
                return NamedType(name)
            return None
        if self._nr_class_known(name):
            if self._nr_generic(name):
                return None
            if kind in {"dynamic", "interface"}:
                return NamedType(name)
            if kind == "class":
                subject_class = self._nr_class_of(current)
                if subject_class and self._nr_strict_subclass(name, subject_class):
                    return NamedType(name)
            return None
        return None

    # ─── Plans ─────────────────────────────────────────────────

    def _nr_module_variables(self) -> set:
        return getattr(self, "_nr_module_vars", None) or set()

    def _nr_plan(self, facts: Sequence, region: Sequence) -> list:
        """``[(name, go_name, narrowed, bind_lines)]`` for the facts that
        narrow a local over ``region`` (the same decisions as the checker)."""
        plan: list = []
        if not facts or getattr(self, "_nr_module_code", False):
            return plan
        blocked = None
        module_vars = self._nr_module_variables()
        types: dict = {}
        for name, target in facts:
            if name in module_vars:
                continue
            go_name = self._go_binding_name(name)
            if go_name not in self.declared_vars:
                continue
            if blocked is None:
                blocked = narrowing.blocked_names(region, self.go_blocks)
            if name in blocked:
                continue
            current = types.get(name)
            if current is None:
                current = self._pm_subject_type(Tree("var", [Tree("name", [name])]))
            narrowed = self._nr_narrowed_type(current, target)
            if narrowed is None or (current is not None and render_type(narrowed) == render_type(current)):
                continue
            if isinstance(narrowed, (ListType, DictType)) and name in narrowing.mutated_names(region):
                continue
            lines = self._nr_bind_lines(go_name, current, narrowed)
            if lines is None:
                continue
            types[name] = narrowed
            plan.append((name, go_name, narrowed, lines))
        return plan

    def _nr_bind_lines(self, go_name: str, current: Optional[Type], narrowed: Type) -> Optional[List[str]]:
        """Go statements rebinding ``go_name`` as its narrowed view."""
        kind = self._nr_kind(current)
        current_go = self._pm_go_type(current) if current is not None else "interface{}"
        narrowed_go = self._pm_go_type(narrowed)
        x = go_name
        if kind == "optional":
            if narrowed_go == current_go:
                return []  # ``optional[C]`` -> ``C``: same Go pointer
            if current_go == "*" + narrowed_go:
                return [f"{x} := *{x}", f"_ = {x}"]
            cls = narrowed.name if isinstance(narrowed, NamedType) else ""
            if cls and self._hier_dynamic_target(cls):
                inner = self._nr_class_of(current)
                return [f"{x}, _ := {self._hier_downcast_go(x, inner, cls)}", f"_ = {x}"]
            return None
        if isinstance(narrowed, NamedType) and self._nr_class_known(narrowed.name):
            cls = narrowed.name
            static = self._nr_class_of(current) if kind == "class" else ""
            if self._hier_dynamic_target(cls):
                return [f"{x}, _ := {self._hier_downcast_go(x, static, cls)}", f"_ = {x}"]
            if kind == "class":
                return None
            return [f"{x}, _ := interface{{}}({x}).(*{self._go_class_name(cls)})", f"_ = {x}"]
        if isinstance(narrowed, NamedType) and narrowed.name in self._interfaces:
            return [f"{x}, _ := interface{{}}({x}).({self._go_class_name(narrowed.name)})", f"_ = {x}"]
        if isinstance(narrowed, NamedType) and narrowed.name in _BUILTIN_TARGETS:
            go = TYPE_MAP.get(narrowed.name, narrowed_go)
            source = f"lamJSONRaw({x})" if kind == "json" else f"interface{{}}({x})"
            return [f"{x}, _ := {source}.({go})", f"_ = {x}"]
        if isinstance(narrowed, ListType):
            self._need_import("reflect")
            return [f"{x} := {_SEQ_VIEW.format(x=x)}", f"_ = {x}"]
        if isinstance(narrowed, DictType):
            self._need_import("reflect")
            self._need_import("fmt")
            return [f"{x} := {_MAP_VIEW.format(x=x)}", f"_ = {x}"]
        return None

    # ─── Emission ──────────────────────────────────────────────

    def _nr_emit_binds(self, plan: list) -> None:
        for _name, _go, _narrowed, lines in plan:
            for line in lines:
                self._emit(line)

    @staticmethod
    def _nr_inline_binds(plan: list) -> str:
        """The bind statements as a ``stmt; stmt; `` prefix (closures)."""
        return "".join(f"{line}; " for _n, _g, _t, lines in plan for line in lines)

    @contextmanager
    def _nr_registered(self, plan: list):
        """Type the narrowed names while the region is lowered."""
        if not plan:
            yield
            return
        saved = self._pm_register_bindings([(name, go, narrowed) for name, go, narrowed, _l in plan])
        try:
            yield
        finally:
            self._pm_restore_bindings(saved)

    def _nr_emit_region(self, facts: Sequence, region: Sequence, emit_body) -> None:
        """Bind the shadows of ``facts`` and run ``emit_body`` with them."""
        plan = self._nr_plan(facts, region)
        self._nr_emit_binds(plan)
        with self._nr_registered(plan):
            emit_body()

    def _nr_closure_expr(self, plan: list, go_type: str, lower) -> str:
        """``func() T { <binds>; return <expr> }()`` when ``plan`` binds
        something, else the plain lowering. ``lower`` lowers the operand
        with the narrowed types registered; statements it emits (``?``
        prologues) move inside the closure."""
        if not plan:
            return lower()
        outer = self.output_lines
        self.output_lines = []
        try:
            with self._nr_registered(plan):
                expr = lower()
            prologue = [line.strip() for line in self.output_lines if line.strip()]
        finally:
            self.output_lines = outer
        body = self._nr_inline_binds(plan) + "".join(f"{line}; " for line in prologue)
        return f"func() {go_type} {{ {body}return {expr} }}()"

    # ─── Hooks ─────────────────────────────────────────────────

    def _nr_comprehension_plan(self, comp_if, region: list, for_clauses: list) -> list:
        """``[s.side for s in shapes if isinstance(s, Square)]``: the plan
        binding the filter's facts before the result is built (the loop
        variables are locals of the comprehension closure)."""
        if not isinstance(comp_if, Tree) or any(narrowing.contains_propagate(n) for n in region):
            return []
        loop_names: set = set()
        for fc in for_clauses:
            if fc.children:
                loop_names |= narrowing.target_names(fc.children[0])
        added = {self._go_binding_name(n) for n in loop_names} - self.declared_vars
        self.declared_vars |= added
        try:
            return self._nr_plan(narrowing.condition_facts(comp_if, True), region + [comp_if])
        finally:
            self.declared_vars -= added

    def _nr_binary_to_go(self, node: Tree) -> Optional[str]:
        """``a and b`` / ``a or b`` whose later operands are narrowed by the
        earlier ones (``isinstance(x, T) and x.f > 0``); ``None`` when no
        operand is narrowed."""
        operands = [c for c in node.children if isinstance(c, Tree)]
        plans = [self._nr_plan(narrowing.operand_facts(node, i), [c]) if i else [] for i, c in enumerate(operands)]
        if not any(plans):
            return None
        op = "&&" if node.data == "and_test" else "||"
        parts = []
        for child, plan in zip(operands, plans):
            part = self._nr_closure_expr(plan, "bool", lambda child=child: self._expr_to_go(child))
            if not plan and node.data == "and_test" and child.data in ("or_test", "test", "null_coalesce"):
                part = f"({part})"
            parts.append(part)
        return f" {op} ".join(parts)

    def _nr_ternary_plans(self, node: Tree):
        true_facts, false_facts = narrowing.ternary_facts(node)
        return (self._nr_plan(true_facts, [node.children[0]]),
                self._nr_plan(false_facts, [node.children[2]]))

    def _nr_ternary_to_go(self, node: Tree, go_type: str, lower_branch) -> Optional[str]:
        """``a if isinstance(x, T) else b`` with a narrowed branch:
        ``func() T { if cond { <binds>; return a }; <binds>; return b }()``."""
        true_plan, false_plan = self._nr_ternary_plans(node)
        if not true_plan and not false_plan:
            return None
        cond = self._expr_to_go(node.children[1])
        parts = []
        for branch, plan in ((node.children[0], true_plan), (node.children[2], false_plan)):
            outer = self.output_lines
            self.output_lines = []
            try:
                with self._nr_registered(plan):
                    value = lower_branch(branch)
                prologue = [line.strip() for line in self.output_lines if line.strip()]
            finally:
                self.output_lines = outer
            parts.append(self._nr_inline_binds(plan) + "".join(f"{line}; " for line in prologue) + f"return {value}")
        return f"func() {go_type} {{ if {cond} {{ {parts[0]} }}; {parts[1]} }}()"

    def _nr_ternary_narrows(self, node) -> bool:
        if not (isinstance(node, Tree) and node.data == "test" and len(node.children) == 3):
            return False
        true_plan, false_plan = self._nr_ternary_plans(node)
        return bool(true_plan or false_plan)
