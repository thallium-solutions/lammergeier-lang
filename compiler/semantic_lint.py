#!/usr/bin/env python3
"""Developer-experience lint checks of the semantic checker (warnings;
errors under strict mode, ``lamc --strict``).

* ``x = x``: an assignment of a value to itself;
* an ``elif`` repeating an earlier condition of the chain (its branch never
  runs);
* a comparison of an expression with itself (``n == n``);
* a ``for`` loop variable overwriting a parameter of the function;
* a private function or method that nothing in its module uses;
* SQL built with an f-string or ``+`` from values that are not constants,
  passed to lamdb (``query``, ``exec``, ``first``, ``whereRaw``...): an
  injection risk, values belong in ``?`` parameters;
* a command built from values for ``Exec.run`` / ``Exec.runSilent``, which
  split it on spaces (a value with spaces becomes several arguments).
"""

from __future__ import annotations

import re

from lark import Token, Tree

from compiler.fstrings import fstring_body, split_fstring

# lamdb methods whose first argument is SQL text.
_SQL_METHODS = frozenset({
    "exec", "query", "first", "scalar", "queryJson", "firstJson", "scalarJson",
    "whereRaw", "selectRaw", "orderByRaw", "groupByRaw", "havingRaw", "joinRaw",
})
_SQL_RECEIVERS = frozenset({"Db", "Tx", "DbConn", "QueryBuilder", "Where"})
# ``Where`` methods taking SQL text, and the position of that argument.
_WHERE_SQL_ARGS = {"add": 0, "addIf": 1, "addIn": 0}
_SHELL_METHODS = frozenset({"run", "runSilent"})
# A column or table name cannot be a ``?`` parameter: the warning says how
# to choose one at run time.
_SQL_NAMES_HINT = (
    "; a column or table name chosen at run time: index a `const` table "
    "(`Columns[key]`)"
)

# Expressions without calls, indexes or assignments: evaluating them twice
# gives the same value.
_PURE_NODES = frozenset({"var", "number", "string", "const_true", "const_false", "const_none"})


def _pure(expr) -> bool:
    if not isinstance(expr, Tree):
        return False
    if expr.data in _PURE_NODES:
        return True
    if expr.data == "getattr" and expr.children:
        return _pure(expr.children[0])
    if expr.data in {"arith_expr", "term", "factor", "comparison", "not_test", "and_test", "or_test"}:
        operands = [c for c in expr.children if isinstance(c, Tree) and c.data != "comp_op"]
        return bool(operands) and all(_pure(c) for c in operands)
    return False


class LintChecksMixin:
    """Mixed into :class:`compiler.semantic.SemanticChecker`."""

    # ── x = x ────────────────────────────────────────────────

    def _lint_assignment(self, node: Tree) -> None:
        assign = node
        if node.data == "assign_stmt":
            assign = next((c for c in node.children if isinstance(c, Tree)), None)
        if not (isinstance(assign, Tree) and assign.data == "assign" and len(assign.children) == 2):
            return
        target, value = assign.children
        if isinstance(target, Tree) and isinstance(value, Tree) and target.data in ("var", "getattr") and target == value:
            from compiler.constants import lam_simple_expr_text
            shown = lam_simple_expr_text(target) or "the value"
            self._warning(node, "unused", f"assigning `{shown}` to itself has no effect")

    # ── if / elif ────────────────────────────────────────────

    def _lint_if(self, node: Tree) -> None:
        """An ``elif`` whose condition repeats an earlier one of the chain."""
        conditions: list = []
        first = node.children[0] if node.children else None
        if isinstance(first, Tree) and first.data != "suite":
            conditions.append(first)
        for child in node.children:
            if isinstance(child, Tree) and child.data == "elifs":
                for branch in child.children:
                    if isinstance(branch, Tree) and branch.children and isinstance(branch.children[0], Tree):
                        conditions.append(branch.children[0])
        seen: list = []
        for condition in conditions:
            if not _pure(condition):
                seen.append(None)
                continue
            earlier = next((c for c in seen if c is not None and c == condition), None)
            if earlier is not None:
                line = getattr(getattr(earlier, "meta", None), "line", None)
                where = f" on line {line}" if line else ""
                self._warning(
                    condition, "flow",
                    f"this `elif` repeats the condition{where}: its branch never runs",
                )
            seen.append(condition)

    # ── comparisons ──────────────────────────────────────────

    def _lint_comparison(self, node: Tree) -> None:
        operands = [c for c in node.children if isinstance(c, Tree) and c.data != "comp_op"]
        ops = [c for c in node.children if isinstance(c, Tree) and c.data == "comp_op"]
        if len(operands) != 2 or len(ops) != 1:
            return
        left, right = operands
        if not (_pure(left) and left == right) or left.data in ("number", "string", "const_true", "const_false", "const_none"):
            return
        op = " ".join(str(t) for t in ops[0].children if isinstance(t, Token))
        kind = self._expr_simple_type(left)
        if getattr(kind, "name", "") == "float":
            # ``x != x`` is the NaN test.
            return
        outcome = {"==": "true", "<=": "true", ">=": "true", "is": "true", "!=": "false", "<": "false", ">": "false", "is not": "false"}.get(op)
        if outcome is None:
            return
        from compiler.constants import lam_simple_expr_text
        shown = lam_simple_expr_text(left) or "a value"
        self._warning(node, "flow", f"comparing `{shown}` with itself is always {outcome}")

    # ── for loops ────────────────────────────────────────────

    def _lint_for(self, node: Tree) -> None:
        """A loop variable that overwrites a parameter of the function."""
        stack = self.__dict__.get("_funcdef_stack") or []
        if not stack or not node.children or not isinstance(node.children[0], Tree):
            return
        target = node.children[0]
        if target.data not in ("untyped_for_target", "typed_for_target", "for_target"):
            return
        params = set(self._param_names(self._funcdef_params(stack[-1]))) - {"self", "cls"}
        for sub in target.iter_subtrees():
            if sub.data == "var" and sub.children:
                name = self._name_text(sub.children[0])
                if name in params:
                    self._warning(
                        sub, "shadow",
                        f"the loop variable `{name}` overwrites the parameter `{name}`: use another name",
                    )

    # ── unused private functions and methods ─────────────────

    def _lint_unused_private(self, tree: Tree) -> None:
        used_names: set[str] = set()
        used_members: set[str] = set()
        definitions: list[tuple[str, str, Tree]] = []
        for sub in tree.iter_subtrees():
            if sub.data == "var" and sub.children:
                used_names.add(self._name_text(sub.children[0]))
            elif sub.data == "getattr" and len(sub.children) >= 2:
                used_members.add(self._name_text(sub.children[1]))
            elif sub.data == "fstring" and sub.children:
                # ``f"{Report.isoCol('at')}"``: slots are not parsed into the
                # tree; their words count as uses.
                words = set(re.findall(r"[A-Za-z_]\w*", str(sub.children[0])))
                used_names |= words
                used_members |= words
        go_text = " ".join(str(v) for v in (getattr(self, "_go_blocks", None) or {}).values())
        go_words = set(re.findall(r"[A-Za-z_]\w*", go_text))
        for stmt in tree.children:
            if not isinstance(stmt, Tree):
                continue
            if stmt.data == "funcdef" and self._funcdef_is_private(stmt):
                definitions.append(("function", self._funcdef_name(stmt), stmt))
            elif stmt.data == "classdef":
                suite = self._suite_node(stmt)
                for member in self._suite_stmts(suite) if suite is not None else ():
                    if isinstance(member, Tree) and member.data == "funcdef" and self._funcdef_is_private(member):
                        definitions.append(("method", self._funcdef_name(member), member))
        for kind, name, definition in definitions:
            if not name or name.startswith("__"):
                continue
            used = name in used_members if kind == "method" else name in used_names
            go_name = name[0].upper() + name[1:]
            if used or name in go_words or go_name in go_words:
                continue
            self._warning(definition, "unused", f"private {kind} `{name}` is never used in its module")

    # ── SQL and shell commands built from values ─────────────

    def _lint_call(self, node: Tree) -> None:
        if not node.children or not isinstance(node.children[0], Tree) or node.children[0].data != "getattr":
            return
        callee = node.children[0]
        if len(callee.children) < 2:
            return
        method = self._name_text(callee.children[1])
        receiver = callee.children[0]
        position = 0
        if method in _WHERE_SQL_ARGS and method not in _SQL_METHODS:
            if getattr(self._expr_simple_type(receiver), "name", "") != "Where" and not (
                isinstance(receiver, Tree) and receiver.data == "funccall" and receiver.children
                and isinstance(receiver.children[0], Tree) and receiver.children[0].data == "var"
                and self._name_text(receiver.children[0].children[0]) == "Where"
            ) and not self._lint_is_query_chain(receiver):
                return
            position = _WHERE_SQL_ARGS[method]
        elif method not in _SQL_METHODS and method not in _SHELL_METHODS:
            return
        args = node.children[1] if len(node.children) > 1 and isinstance(node.children[1], Tree) else None
        values = [c for c in args.children if isinstance(c, Tree)] if args is not None else []
        first = values[position] if position < len(values) else None
        if isinstance(first, Tree) and first.data == "argvalue" and first.children:
            first = first.children[0]
        if not isinstance(first, Tree) or not (
            self._lint_built_from_values(first) or self._lint_local_built_from_values(first)
        ):
            return
        if method in _WHERE_SQL_ARGS and method not in _SQL_METHODS:
            self._warning(
                first, "security",
                f"SQL built from values is an injection risk (they are not escaped): pass them as `?` parameters "
                f"(`.{method}({'condition, ' if method == 'addIf' else ''}\"name = ?\", [name])`){_SQL_NAMES_HINT}",
            )
            return
        if method in _SHELL_METHODS:
            if isinstance(receiver, Tree) and receiver.data == "var" and self._name_text(receiver.children[0]) == "Exec":
                self._warning(
                    first, "security",
                    f"`Exec.{method}` splits the command on spaces: a value with spaces (or a leading `-`) becomes "
                    "other arguments; build it with `Exec.command(name, [args])`",
                )
            return
        kind = self._expr_simple_type(receiver)
        if getattr(kind, "name", "") not in _SQL_RECEIVERS and not self._lint_is_query_chain(receiver):
            return
        self._warning(
            first, "security",
            "SQL built from values is an injection risk (they are not escaped): pass them as `?` parameters "
            f"(`.{method}(\"... WHERE id = ?\", [id])`){_SQL_NAMES_HINT}",
        )

    def _lint_is_query_chain(self, receiver) -> bool:
        """``db.table("t").whereEq(...)``: a lamdb builder chain."""
        while isinstance(receiver, Tree) and receiver.data == "funccall" and receiver.children:
            inner = receiver.children[0]
            if not (isinstance(inner, Tree) and inner.data == "getattr" and inner.children):
                return False
            receiver = inner.children[0]
            kind = self._expr_simple_type(receiver)
            if getattr(kind, "name", "") in _SQL_RECEIVERS:
                return True
        return False

    def _lint_built_from_values(self, expr, depth: int = 0, checking: frozenset | set = frozenset()) -> bool:
        """An f-string with a slot, or a ``+`` concatenation, holding a value
        that is not a constant (literals, names assigned only literals and
        numbers are constants)."""
        if expr.data == "fstring" and expr.children:
            split = fstring_body(str(expr.children[0]))
            if split is None:
                return False
            body, quote, _prefix = split
            parts = split_fstring(body, quote) or []
            for part in parts:
                if isinstance(part, str):
                    continue
                slot = self._fstring_slot_tree(part.expr) if part.expr.strip() else None
                if slot is None or not self._lint_constant_value(slot, depth + 1, checking):
                    return True
            return False
        if expr.data == "arith_expr":
            operands = [c for c in expr.children if isinstance(c, Tree)]
            return any(
                self._lint_built_from_values(c, depth + 1, checking) if c.data in ("fstring", "arith_expr") else not self._lint_constant_value(c, depth + 1, checking)
                for c in operands
            )
        return False

    def _lint_local_built_from_values(self, expr) -> bool:
        """``sql = f"... {v}"`` then ``conn.exec(sql)``: a local assigned SQL
        built from values in the function."""
        if not (isinstance(expr, Tree) and expr.data == "var" and expr.children):
            return False
        stack = self.__dict__.get("_funcdef_stack") or []
        if not stack:
            return False
        name = self._name_text(expr.children[0])
        return any(
            isinstance(v, Tree) and v.data in ("fstring", "arith_expr") and self._lint_built_from_values(v, 1, {name})
            for v in self._assigned_values(stack[-1], name)
        )

    def _lint_constant_value(self, expr, depth: int = 0, checking: frozenset | set = frozenset()) -> bool:
        """A value that cannot carry user input into SQL: a literal, a
        number, a ``const`` (also an imported one: ``UnixParamSql``,
        ``db.Columns``), an f-string or ``+`` of those, a call whose arguments
        are all such values (``SlotSql.epoch("t.at")``: SQL written by the
        program), the SQL of a lamdb builder (``where.sql()``), or a local
        assigned only such values."""
        if not isinstance(expr, Tree) or depth > 12:
            return False
        if expr.data in ("fstring", "arith_expr"):
            return not self._lint_built_from_values(expr, depth + 1, checking)
        if expr.data in ("funccall", "getattr") and getattr(self._expr_simple_type(expr), "name", "") in ("int", "float", "bool"):
            # ``Input.timeSql(input.has(key), input.local)``: a number or a
            # boolean, whatever the call.
            return True
        if expr.data == "funccall" and expr.children:
            callee = expr.children[0]
            args = expr.children[1] if len(expr.children) > 1 and isinstance(expr.children[1], Tree) else None
            values = []
            for arg in (args.children if args is not None else ()):
                if isinstance(arg, Tree):
                    values.append(arg.children[0] if arg.data == "argvalue" and arg.children else arg)
            if isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) >= 2:
                method = self._name_text(callee.children[1])
                receiver = self._expr_simple_type(callee.children[0])
                if method == "sql" and getattr(receiver, "name", "") in _SQL_RECEIVERS:
                    return True
                if method == "join" and len(values) == 1:
                    # ``", ".join(parts)``: constant separator, parts checked.
                    return self._lint_constant_value(callee.children[0], depth + 1) and self._lint_constant_value(values[0], depth + 1)
                if not self._lint_constant_value(callee.children[0], depth + 1) and not self._lint_static_receiver(callee.children[0]):
                    return False
            return all(self._lint_constant_value(v, depth + 1) for v in values)
        if expr.data == "var" and expr.children:
            name = self._name_text(expr.children[0])
            if name in checking:
                # ``where = where + " AND x = ?"``: the name's own value, being
                # proven constant from its other assignments.
                return True
            stack = self.__dict__.get("_funcdef_stack") or []
            if stack and self._lint_is_local(stack[-1], name):
                # A local, also one of a block the check is past (``column``
                # assigned in a loop body, read through an appended f-string).
                values = self._assigned_values(stack[-1], name)
                inner = set(checking) | {name}
                if values and all(self._lint_constant_value(v, depth + 1, inner) for v in values) and self._lint_appends_constant(stack[-1], name, depth):
                    return True
                # ``for key in Keys``: an element of a constant collection.
                iterables = self._loop_iterables(stack[-1], name)
                if iterables and all(self._lint_constant_value(v, depth + 1, inner) for v in iterables):
                    return True
        if expr.data in ("list", "tuple", "set"):
            return all(self._lint_constant_value(c, depth + 1, checking) for c in expr.children if isinstance(c, Tree))
        if expr.data == "dict":
            return all(
                isinstance(c, Tree) and c.data == "key_value" and len(c.children) == 2
                and self._lint_constant_value(c.children[1], depth + 1, checking)
                for c in expr.children
            )
        if expr.data in ("list_comprehension", "set_comprehension") and expr.children:
            # ``["?" for _ in values]``: the elements are written by the
            # program (the loop variables of constant collections count).
            comp = expr.children[0]
            if isinstance(comp, Tree) and comp.data == "comprehension" and comp.children:
                return self._lint_constant_value(comp.children[0], depth + 1, checking)
        if expr.data == "getitem" and expr.children:
            # ``Sorts[listing.sort]``: whatever the key, the value is one of a
            # constant table's.
            return self._lint_constant_value(expr.children[0], depth + 1)
        kind = self._expr_simple_type(expr)
        if getattr(kind, "name", "") in ("int", "float", "bool"):
            return True
        if isinstance(expr, Tree) and expr.data == "var" and expr.children:
            name = self._name_text(expr.children[0])
            if self._is_const(name) or (
                name in self._imported_const_names and not any(name in sc.names for sc in self._scopes[1:])
            ):
                return True
        if isinstance(expr, Tree) and expr.data == "getattr" and len(expr.children) >= 2:
            head = expr.children[0]
            if isinstance(head, Tree) and head.data == "var" and head.children:
                if f"{self._name_text(head.children[0])}.{self._name_text(expr.children[1])}" in self._imported_const_names:
                    return True
        return self._markup_value_trusted(expr, "sql")

    def _lint_is_local(self, funcdef, name: str) -> bool:
        """``name`` is a local of the function: in scope here, or bound in a
        block of it the check is no longer in."""
        if any(name in sc.names for sc in self._scopes[1:]):
            return True
        return bool(self._assigned_values(funcdef, name) or self._loop_iterables(funcdef, name))

    def _lint_static_receiver(self, receiver) -> bool:
        """``SlotSql`` in ``SlotSql.epoch(...)``: a class name (a static call)."""
        if isinstance(receiver, Tree) and receiver.data == "var" and receiver.children:
            name = self._name_text(receiver.children[0])
            return name in self._class_method_shapes and not any(name in sc.names for sc in self._scopes[1:])
        return False

    def _lint_appends_constant(self, funcdef, name: str, depth: int) -> bool:
        """Every value the function writes into ``name`` (``name.append(x)``,
        ``name += x``, ``name[k] = x``...) is a constant (a list of SQL
        fragments built by the program)."""
        return all(
            not isinstance(value, Tree) or self._lint_constant_value(value, depth + 1, {name})
            for value in self._written_values(funcdef, name)
        )
