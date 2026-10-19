#!/usr/bin/env python3
"""isinstance narrowing in the semantic checker.

Where :mod:`compiler.narrowing` says a fact ``isinstance(x, T)`` holds, the
checker retypes the local ``x`` (its entry in the scope's ``var_types``)
for the duration of the region and restores it afterwards. A local that
cannot be narrowed there (assigned in the region, a module global, read
by a ``go!`` block) keeps its type, and a member error on it says why; so
does a member error on a field tested with isinstance (fields are never
narrowed).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import List, Optional, Sequence

from lark import Tree

from compiler import narrowing
from compiler.typesys import DictType, GenericType, ListType, NamedType, Type, parse_type, render_type

_BUILTIN_TARGETS = {
    "int": NamedType("int"), "float": NamedType("float"), "str": NamedType("str"),
    "bool": NamedType("bool"),
}
_JSON_SCALARS = {"int", "float", "str", "bool"}


class NarrowingCheckMixin:
    """Checker side of isinstance narrowing (see the module docstring)."""

    # ─── Types ─────────────────────────────────────────────────

    def _narrow_class_known(self, name: str) -> bool:
        return name in self._class_method_shapes or name in self._class_members

    def _narrowed_type(self, current: Optional[Type], target) -> Optional[Type]:
        """The type of a ``current``-typed local where ``target`` holds, or
        ``None`` when the fact narrows nothing."""
        if target is narrowing.NOT_NONE:
            if isinstance(current, GenericType) and current.base in {"optional", "Option"} and current.args:
                return current.args[0]
            return None
        results = [self._narrow_one(current, alt) for alt in narrowing.type_alternatives(target)]
        if not results or any(r is None for r in results):
            return None
        rendered = {render_type(r) for r in results}
        if len(rendered) == 1:
            return results[0]
        # ``isinstance(s, (Square, BigSquare))``: the nearest common class.
        names = [r.name for r in results if isinstance(r, NamedType)]
        if len(names) != len(results) or not all(self._narrow_class_known(n) for n in names):
            return None
        for candidate in [names[0]] + self._all_base_names(names[0]):
            if all(n == candidate or candidate in self._all_base_names(n) for n in names):
                narrowed = NamedType(candidate)
                if current is not None and render_type(current) == candidate:
                    return None
                current_class = self._receiver_class_from_type(current)
                if current_class and candidate not in [current_class] and current_class not in self._all_base_names(candidate):
                    return None
                return narrowed
        return None

    def _narrow_one(self, current: Optional[Type], alt) -> Optional[Type]:
        name = narrowing.type_target_name(alt)
        if not name or name == "None":
            return None
        if isinstance(current, GenericType) and current.base in {"optional", "Option"} and current.args:
            inner = current.args[0]
            inner_name = render_type(inner)
            if name == inner_name or (name in _BUILTIN_TARGETS and render_type(_BUILTIN_TARGETS[name]) == inner_name):
                return inner
            inner_class = self._receiver_class_from_type(inner)
            if (
                inner_class and self._narrow_class_known(name) and not self._class_type_params.get(name)
                and inner_class in self._all_base_names(name)
            ):
                return NamedType(name)
            return None
        kind = self._pattern_subject_kind(current)
        is_user_type = self._narrow_class_known(name) or name in self._interface_methods
        if not is_user_type and name in {"int", "float", "str", "bool", "list", "dict"}:
            if kind == "dynamic":
                if name == "list":
                    return ListType(NamedType("any"))
                if name == "dict":
                    return DictType(NamedType("str"), NamedType("any"))
                return _BUILTIN_TARGETS[name]
            if kind == "json" and name in _JSON_SCALARS:
                return _BUILTIN_TARGETS[name]
            return None
        if name in self._interface_methods:
            if kind == "dynamic" or (kind == "interface" and isinstance(current, NamedType) and current.name != name):
                return NamedType(name)
            return None
        if self._narrow_class_known(name):
            if self._class_type_params.get(name):
                return None  # generic classes: no type arguments to narrow to
            if kind in {"dynamic", "interface"}:
                return NamedType(name)
            if kind == "class":
                subject_class = self._receiver_class_from_type(current)
                if subject_class and subject_class != name and subject_class in self._all_base_names(name):
                    return NamedType(name)
            return None
        return None

    # ─── Regions ───────────────────────────────────────────────

    def _narrow_scope_ok(self, name: str) -> Optional[str]:
        """``None`` when ``name`` is a narrowable local, else the reason."""
        if name in getattr(self, "_narrow_module_vars", ()):
            return "module globals are not narrowed"
        if name in getattr(self, "_narrow_nonlocal_names", ()):
            return "a nested function rebinds it (`nonlocal`)"
        scope = self._resolving_scope(name)
        if scope is None:
            return "it is not a local variable"
        index = self._scopes.index(scope)
        if scope.kind in {"module", "class", "constant"} or not any(
            s.kind == "function" for s in self._scopes[: index + 1]
        ):
            return "module globals are not narrowed"
        return None

    def _narrow_apply(self, facts: Sequence, region: Sequence) -> list:
        """Retype the locals ``facts`` narrow over ``region``; return the
        undo log for :meth:`_narrow_restore`."""
        undo: list = []
        if not facts:
            return undo
        blocked = None
        notes = self.__dict__.setdefault("_narrow_notes", {})
        for name, target in facts:
            target_text = "not None" if target is narrowing.NOT_NONE else (
                narrowing.type_target_name(target) if len(narrowing.type_alternatives(target)) == 1
                else "(" + ", ".join(narrowing.type_target_name(t) for t in narrowing.type_alternatives(target)) + ")"
            )
            reason = self._narrow_scope_ok(name)
            if reason is None:
                if blocked is None:
                    blocked = narrowing.blocked_names(region, getattr(self, "_go_blocks", None))
                if name in blocked:
                    reason = "it is assigned in the narrowed region" if name in narrowing.assigned_names(region) \
                        else "a `go!` block reads it"
            if reason is not None:
                undo.append(("note", name, notes.get(name)))
                notes[name] = f"`{name}` is not narrowed to `{target_text}` here: {reason}"
                continue
            text = self._var_type(name)
            try:
                current = parse_type(text) if text else None
            except Exception:
                current = None
            narrowed = self._narrowed_type(current, target)
            if narrowed is None or (current is not None and render_type(narrowed) == render_type(current)):
                continue
            if isinstance(narrowed, (ListType, DictType)) and name in narrowing.mutated_names(region):
                # The narrowed view of a collection held in ``any`` may be a
                # converted copy: in-place changes would be lost.
                undo.append(("note", name, notes.get(name)))
                notes[name] = f"`{name}` is not narrowed to `{target_text}` here: it is changed in place in the narrowed region"
                continue
            scope = next((s for s in reversed(self._scopes) if name in s.var_types), None) or self._resolving_scope(name)
            undo.append(("type", scope, name, name in scope.var_types, scope.var_types.get(name)))
            scope.var_types[name] = render_type(narrowed)
        if undo:
            self.__dict__["_expr_memo"] = {}
        return undo

    def _narrow_restore(self, undo: list) -> None:
        if not undo:
            return
        notes = self.__dict__.setdefault("_narrow_notes", {})
        for entry in reversed(undo):
            if entry[0] == "note":
                _kind, name, previous = entry
                if previous is None:
                    notes.pop(name, None)
                else:
                    notes[name] = previous
                continue
            _kind, scope, name, had, old = entry
            if had:
                scope.var_types[name] = old
            else:
                scope.var_types.pop(name, None)
        self.__dict__["_expr_memo"] = {}

    @contextmanager
    def _narrowed(self, facts: Sequence, region: Sequence, attr_cond=None, attr_truth: bool = True):
        undo = self._narrow_apply(facts, region)
        attr_undo = self._narrow_attr_notes_apply(attr_cond, attr_truth)
        try:
            yield
        finally:
            self._narrow_attr_notes_restore(attr_undo)
            self._narrow_restore(undo)

    # ─── Field subjects (hints only) ───────────────────────────

    def _narrow_attr_notes_apply(self, cond, truth: bool) -> list:
        if cond is None:
            return []
        notes = self.__dict__.setdefault("_narrow_notes", {})
        undo = []
        for dotted in narrowing.attribute_subjects(cond, truth):
            undo.append((dotted, notes.get(dotted)))
            notes[dotted] = f"attributes are not narrowed by `isinstance`; bind `{dotted}` to a local first"
        return undo

    def _narrow_attr_notes_restore(self, undo: list) -> None:
        notes = self.__dict__.setdefault("_narrow_notes", {})
        for dotted, previous in reversed(undo):
            if previous is None:
                notes.pop(dotted, None)
            else:
                notes[dotted] = previous

    def _narrow_tested_field_class(self, obj) -> str:
        """Declared class of a field tested with isinstance in the
        enclosing condition (``h.shape`` in ``if isinstance(h.shape, T)``),
        so a member only the tested class has is reported with the hint
        instead of failing in the Go build."""
        dotted = narrowing._dotted(obj)
        if not dotted or not self._narrow_note_for(dotted).startswith("attributes"):
            return ""
        receiver, field = obj.children[0], self._name_text(obj.children[1])
        owner = ""
        if isinstance(receiver, Tree) and receiver.data == "var" and self._name_text(receiver.children[0]) == "self":
            owner = self._current_class_stack[-1] if self._current_class_stack else ""
        else:
            owner = self._receiver_class_from_type(self._expr_simple_type(receiver))
        if not owner:
            return ""
        for cls in [owner] + self._all_base_names(owner):
            text = self._class_field_types.get(cls, {}).get(field)
            if text:
                try:
                    return self._receiver_class_from_type(parse_type(text))
                except Exception:
                    return ""
        return ""

    def _narrow_note_for(self, receiver_display: str) -> str:
        return self.__dict__.get("_narrow_notes", {}).get(receiver_display, "")

    # ─── Statement and expression hooks ────────────────────────

    def _narrow_visit_binary(self, node: Tree) -> None:
        """``a and b`` / ``a or b``: each operand sees the facts of the
        operands before it."""
        operands = [c for c in node.children if isinstance(c, Tree)]
        truth = node.data == "and_test"
        for index, child in enumerate(operands):
            facts = narrowing.operand_facts(node, index)
            attr = Tree(node.data, operands[:index]) if index else None
            notes_undo = self._narrow_propagate_notes(operands[:index], truth) \
                if index and narrowing.contains_propagate(child) else []
            with self._narrowed(facts, [child], attr, truth):
                self._visit_expr_subtree(child)
            self._narrow_attr_notes_restore(notes_undo)

    def _narrow_propagate_notes(self, before: list, truth: bool) -> list:
        """Explain why an operand holding ``?`` sees no narrowing."""
        notes = self.__dict__.setdefault("_narrow_notes", {})
        undo = []
        for operand in before:
            for name, target in narrowing.condition_facts(operand, truth):
                if target is narrowing.NOT_NONE:
                    continue
                undo.append((name, notes.get(name)))
                notes[name] = (
                    f"`{name}` is not narrowed to `{narrowing.type_target_name(target)}` in an operand "
                    "using `?`; test it in an enclosing `if` instead"
                )
        return undo

    def _narrow_visit_ternary(self, node: Tree) -> None:
        true_expr, cond, false_expr = node.children
        self._visit_expr_subtree(cond)
        true_facts, false_facts = narrowing.ternary_facts(node)
        with self._narrowed(true_facts, [true_expr], cond, True):
            self._visit_expr_subtree(true_expr)
        with self._narrowed(false_facts, [false_expr], cond, False):
            self._visit_expr_subtree(false_expr)

    def _narrow_walk_guarded(self, stmts: List, index: int, undo_stack: list) -> None:
        """After ``stmts[index]`` (an ``if``), narrow the rest of the block
        with the facts of its fall-through path."""
        stmt = stmts[index]
        if not (isinstance(stmt, Tree) and stmt.data == "if_stmt"):
            return
        facts = narrowing.guard_facts(stmt)
        if facts:
            undo_stack.append(self._narrow_apply(facts, stmts[index + 1:]))
