#!/usr/bin/env python3
"""Expression visitor methods for the transpiler."""

from __future__ import annotations
from lark import Tree, Token
from typing import List, Optional
import re
from contextlib import contextmanager
from compiler.constants import (
    PYTHON_EXCEPTIONS, LOWER_PKGS, GO_BUILTINS,
    OP_TO_DUNDER, CMP_TO_DUNDER, generator_go_elem,
)
from compiler.typesys import DictType, GenericType
from compiler.visitors.generics import mentions_names, substitute_go_type
from compiler.index_bounds import index_nonneg, lower_bound
from compiler.fstrings import SOURCE_NEWLINE, fstring_body, fstring_spec_to_go, parse_slot, split_fstring
from compiler import narrowing


_GO_INT_TYPES = frozenset({
    "int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16", "uint32", "uint64",
    "uintptr", "byte", "rune",
})

# Go's binary operator precedence (``go/token``); unary operators bind
# tighter (6) and a primary expression (call, index, parenthesized
# group, literal, name) tighter still (7).
_GO_BINARY_PREC = {
    "||": 1, "&&": 2,
    "==": 3, "!=": 3, "<": 3, "<=": 3, ">": 3, ">=": 3,
    "+": 4, "-": 4, "|": 4, "^": 4,
    "*": 5, "/": 5, "%": 5, "<<": 5, ">>": 5, "&": 5, "&^": 5,
}
_GO_UNARY_PREC = 6
_GO_PRIMARY_PREC = 7
_GO_OPERATOR_CHARS = "|&!<>+-^*/%"
_GO_NUMBER_RE = re.compile(
    r"0[xX][0-9a-fA-F_.]*(?:[pP][+-]?[0-9_]+)?i?|0[oObB][0-7_]+|[0-9_]*\.?[0-9_]*(?:[eE][+-]?[0-9_]+)?i?"
)


def go_expr_precedence(go: str) -> int:
    """Precedence of the loosest operator at the top level of the Go
    expression ``go`` (outside parentheses, brackets, braces and literals):
    1..5 for a binary operator, 6 for a unary one, 7 for a primary
    expression. Parentheses are dropped from the Lam tree, so an emitter
    joining operands with an operator wraps an operand whose precedence is
    lower than the operator's (or equal, on the right of a left-associative
    operator)."""
    text = go.strip()
    lowest = _GO_PRIMARY_PREC
    depth = 0
    i = 0
    n = len(text)
    operand_before = False  # an operand ends right before position i
    starts_unary = False
    while i < n:
        ch = text[i]
        if ch in "\"'`":
            # String / rune literal (``\`` escapes; raw strings have none).
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if (ch != "`" and text[j] == "\\") else 1
            i = j + 1
            operand_before = True
            continue
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            depth -= 1
            i += 1
            operand_before = True
            continue
        if ch.isspace():
            i += 1
            continue
        if depth > 0:
            i += 1
            continue
        if not operand_before and (ch.isdigit() or (ch == "." and text[i + 1:i + 2].isdigit())):
            # A number literal (``1e+21`` holds no operator).
            m = _GO_NUMBER_RE.match(text, i)
            i = m.end() if m and m.end() > i else i + 1
            operand_before = True
            continue
        two = text[i:i + 2]
        if ch in _GO_OPERATOR_CHARS or two == "==":
            op = two if two in _GO_BINARY_PREC or two in {"<-", "&^"} else ch
            if operand_before and op in _GO_BINARY_PREC:
                lowest = min(lowest, _GO_BINARY_PREC[op])
            elif not operand_before and i == 0:
                starts_unary = True
            i += len(op)
            operand_before = False
            continue
        if ch in ",;=":
            # Not a single expression (a list or a statement): leave it
            # as written.
            return _GO_PRIMARY_PREC
        operand_before = True
        i += 1
    if lowest == _GO_PRIMARY_PREC and starts_unary:
        return _GO_UNARY_PREC
    return lowest


def go_operand(go: str, parent_prec: int, right: bool = False, associative: bool = False) -> str:
    """``go`` as an operand of an operator of precedence ``parent_prec``
    (6 for a unary operator), parenthesized when Go would otherwise group
    it differently from the Lam source."""
    prec = go_expr_precedence(go)
    if prec < parent_prec or (prec == parent_prec and (parent_prec == _GO_UNARY_PREC or (right and not associative))):
        return f"({go})"
    return go


class ExpressionVisitorMixin:
    """Handles expression-to-Go conversion, function calls, f-strings, comprehensions."""

    # Nodes whose value type is not the type of a ``?`` operand inside them:
    # the enclosing statement's ``_propagate_cast_hint`` stops there.
    _CAST_HINT_OPAQUE_NODES = frozenset({
        "getattr", "getitem", "funccall", "list", "dict", "set", "tuple",
        "list_comprehension", "dict_comprehension", "set_comprehension", "tuple_comprehension",
        "comparison", "not_test", "and_test", "or_test", "lambdef", "fstring", "string_concat",
        "cast_expr",
    })

    _NUMERIC_GO_TYPES = {
        "int", "int8", "int16", "int32", "int64",
        "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
        "float32", "float64",
    }

    def _parent_alias_base(self, name: str) -> str:
        if not name or not self.current_class or not self._self_replacement:
            return ""
        if name in self.declared_vars:
            return ""
        return self.class_base_aliases.get(self.current_class, {}).get(name, "")

    def _parent_alias_expr(self, name: str) -> str:
        base = self._parent_alias_base(name)
        if not base:
            return ""
        return f"{self._self_replacement}.{self._go_class_name(base)}"

    def _parent_alias_call_info(self, func) -> tuple[str, str, str]:
        if not isinstance(func, Tree) or func.data != "getattr" or len(func.children) < 2:
            return "", "", ""
        obj = func.children[0]
        if not isinstance(obj, Tree) or obj.data != "var" or not obj.children:
            return "", "", ""
        alias = self._get_name(obj.children[0])
        base = self._parent_alias_base(alias)
        if not base:
            return "", "", ""
        receiver = f"{self._self_replacement}.{self._go_class_name(base)}"
        return alias, base, receiver

    # ─── Typed value conversion ────────────────────────────────

    def _repeat_operand_kind(self, node) -> str:
        """``string``/``int`` for literals (tokens or trees) and typed
        expressions, ``""`` otherwise."""
        if isinstance(node, Token):
            if node.type in {"STRING", "LONG_STRING"}:
                return "string"
            if node.type in {"DEC_NUMBER", "HEX_NUMBER", "OCT_NUMBER", "BIN_NUMBER", "NUMBER"} and "." not in str(node):
                return "int"
            return ""
        if isinstance(node, Tree):
            if node.data in {"string", "string_concat", "fstring"}:
                return "string"
            if node.data == "number":
                text = str(node.children[0]) if node.children else ""
                return "int" if text and "." not in text and "e" not in text.lower() else ""
            if node.data == "factor" and len(node.children) == 2 and str(node.children[0]) == "-":
                return "int" if self._repeat_operand_kind(node.children[1]) == "int" else ""
        hint = self._expr_go_type_hint(node) or ""
        return hint if hint in {"string", "int"} else ""

    def _string_repeat_to_go(self, node: Tree) -> str:
        """``"ab" * 3`` / ``3 * "ab"``: string repetition (``""`` for a
        negative count, like Python)."""
        if node.data != "term" or len(node.children) != 3 or str(node.children[1]) != "*":
            return ""
        left, right = node.children[0], node.children[2]
        left_type = self._repeat_operand_kind(left)
        right_type = self._repeat_operand_kind(right)
        if left_type == "string" and right_type == "int":
            text, count = left, right
        elif left_type == "int" and right_type == "string":
            text, count = right, left
        else:
            return ""
        self._need_import("strings")
        return f"strings.Repeat({self._expr_to_go(text)}, max({self._expr_to_go(count)}, 0))"

    def _typed_value_to_go(self, node, go_type: str, type_key: str = "") -> str:
        """Convert an expression with an expected Go type.

        This is the single choke point for contextual lowering. Callers that
        know the target type should route through here so anonymous literals,
        comprehensions, ternaries, and numeric expressions are shaped before
        Go sees them.
        """
        go_type = (go_type or "").strip()
        if (
            isinstance(node, Tree)
            and node.data == "lambdef"
            and go_type.startswith("func(")
        ):
            # Contextual typing: a lambda where a ``func(...)`` value is
            # expected takes its parameter and return types from it.
            split = self._split_go_func_type(go_type)
            if split is not None:
                params, ret = split
                return self._lambda_to_go(node, params, ret, contextual=True)
        if go_type.startswith("func("):
            builtin_ref = self._builtin_func_ref_to_go(node, go_type)
            if builtin_ref:
                return builtin_ref
        shaped_ok = self._typed_result_ok_to_go(node, go_type, type_key)
        if shaped_ok:
            return shaped_ok
        if go_type in self._generic_names and self._is_none_literal(node):
            # ``return None`` in a ``-> T``: ``None`` of a reference type
            # argument, a ``TypeError`` for a value type (``lamNoneOf``).
            return f"lamNoneOf[{go_type}]()"
        if go_type in {"LamHTML", "LamXML"} and self._is_none_literal(node):
            # ``None`` of ``html`` / ``xml``: the zero value (no fragment).
            return f"{go_type}{{}}"
        if go_type in {"LamHTML", "LamXML"} and self._is_markup_marker_node(node):
            return self._markup_call_to_go(node, "html" if go_type == "LamHTML" else "xml")
        if go_type == "LamJSON":
            raw = self._json_value_to_go(node)
            if self._is_json_literal_node(node):
                # A fresh object/array: normalized in place, not copied.
                return f"lamJSONLiteral({raw})"
            return raw if self._expr_go_type_hint(node) == "LamJSON" else f"lamJSONMust({raw})"
        if go_type not in {"", "interface{}", "any"} and self._is_dynamic_any_value(node):
            # An ``any`` value where a concrete type is expected — an element
            # of a ``list[any]`` / ``dict[K, any]``, an ``any`` local,
            # parameter or field, ``r.value``, the result of a callable
            # declared ``-> any`` (``n: int = anyv(3)``): one checked
            # conversion for all of them, the one ``unwrap()`` of an untyped
            # ``Result`` uses (an int widens to ``float``, a subclass
            # instance is viewed as its base, ``None`` fits a class).
            return self._dynamic_to_go(self._expr_to_go(node), go_type)
        if isinstance(node, Tree):
            if go_type in ("interface{}", "any") and node.data in ("list", "dict"):
                # A literal stored in an ``any`` stays untyped (``[]interface{}``,
                # ``map[string]interface{}``), so it converts back to the
                # ``list[any]`` / ``dict[str, any]`` the program reads it as.
                return self._untyped_literal_to_go(node)
            if node.data == "list" and go_type.startswith("[]"):
                return self._typed_list_literal_to_go(node, go_type)
            if node.data == "tuple" and go_type == "[]interface{}":
                elems = [self._expr_to_go(c) for c in node.children if c is not None]
                return f"[]interface{{}}{{{', '.join(elems)}}}"
            if node.data == "list_comprehension" and go_type.startswith("[]"):
                return self._list_comp_to_go(node, elem_type=go_type)
            if node.data == "dict_comprehension" and go_type.startswith("map["):
                return self._dict_comp_to_go(node, map_type=go_type)
            if node.data == "dict" and go_type.startswith("map["):
                return self._typed_dict_literal_to_go(node, go_type)
            if node.data == "set" and self._is_go_set_type(go_type):
                return self._typed_set_literal_to_go(node, go_type)
            if node.data == "set_comprehension" and self._is_go_set_type(go_type):
                return self._set_comp_to_go(node, map_type=go_type)
            if node.data == "test" and len(node.children) == 3 and go_type:
                if self._holds_propagate(node.children[0]) or self._holds_propagate(node.children[2]):
                    with self._scoped(_propagate_cast_hint=go_type if go_type != "interface{}" else ""):
                        hoisted = self._ternary_propagate_to_go(node)
                    if hoisted is not None:
                        return hoisted
                narrowed = self._nr_ternary_to_go(
                    node, go_type, lambda b: self._typed_value_to_go(b, go_type, type_key),
                )
                if narrowed is not None:
                    return narrowed
                true_val = self._typed_value_to_go(node.children[0], go_type, type_key)
                cond = self._expr_to_go(node.children[1])
                false_val = self._typed_value_to_go(node.children[2], go_type, type_key)
                return (
                    f"func() {go_type} {{ if {cond} {{ return {true_val} }}; "
                    f"return {false_val} }}()"
                )
        if go_type:
            context_key = type_key or go_type
            if self._consumes_result_call(node) and not context_key.startswith("*Result"):
                # ``x: T = call()?`` / ``call().unwrap()``: the consumed call
                # is expected to return ``Result[T]``, which selects
                # return-type overloads such as ``Conv.parse``.
                context_key = f"*Result[{go_type}]"
            with self._scoped(
                _expected_expr_go_type=go_type,
                _expected_expr_type_key=context_key,
                _propagate_cast_hint=go_type,
            ):
                raw = self._expr_to_go(node)
        else:
            raw = self._expr_to_go(node)
        if go_type.startswith("*") and isinstance(node, Tree):
            # A subclass instance where its base class is expected: view it
            # as the embedded parent (``sq._lamUp_Shape()``).
            upcast = self._hier_upcast_to_go(raw, self._hier_expr_go_type(node), go_type)
            if upcast:
                return upcast
        elif go_type and isinstance(node, Tree):
            # A base-typed value stored as an interface (or ``any``) keeps
            # the most-derived object, so interface calls reach overrides.
            dynamic = self._hier_dynamic_to_go(raw, self._hier_expr_go_type(node), go_type)
            if dynamic:
                return dynamic
        if go_type in {"byte", "uint8"} and isinstance(node, Tree) and self._numeric_literal_value(node) is None:
            # An ``int`` where a byte is expected (``data: bytes = [x for x
            # in codes]``): a checked conversion, like Python's ``bytes``.
            source = self._expr_go_type_hint(node) or ""
            if not source and node.data in {"arith_expr", "term", "factor"}:
                source = self._numeric_operand_kind(node)
            if source == "int":
                return self._checked_byte_to_go(raw)
        if go_type in {"float64", "float32"} and isinstance(node, Tree) and self._numeric_literal_value(node) is None:
            # Implicit int -> float widening (Python): ``half(n)``,
            # ``return n + 1`` in a ``-> float``, ``xs: list[float] = [n]``.
            with self._scoped(_expected_expr_go_type=go_type, _expected_expr_type_key=type_key or go_type):
                # (return-type overloads pick their variant from the context)
                source = self._expr_go_type_hint(node) or ""
            if not source and node.data in {"arith_expr", "term", "factor"}:
                source = self._numeric_operand_kind(node)
            already_float = raw.startswith(f"{go_type}(") or re.match(
                r"func\([^)]*\) " + re.escape(go_type) + r" \{", raw,
            )
            if source in _GO_INT_TYPES and not already_float:
                return f"{go_type}({raw})"
        if (
            go_type not in {"", "LamJSON", "interface{}"}
            and not (isinstance(node, Tree) and node.data == "null_coalesce")
            and self._expr_go_type_hint(node) == "LamJSON"
        ):
            # (``doc.key ?? fallback`` extracts the expected type itself.)
            if go_type in {"float64", "float32"}:
                # ``x: float = doc.sum`` with ``30``: an integral JSON number
                # is an ``int`` in the json value; widen it.
                return (
                    f"func(v interface{{}}) {go_type} {{ if n, ok := v.(int); ok {{ return {go_type}(n) }}; "
                    f"if f, ok := v.(float64); ok {{ return {go_type}(f) }}; "
                    f"return lamAs[{go_type}](v, {self._none_is_not_text(go_type)}) }}(lamJSONRaw({raw}))"
                )
            # (a JSON null into a value type: ``TypeError: None is not a str``)
            return self._result_payload_assert_go(f"lamJSONRaw({raw})", go_type)
        if (
            go_type.startswith("[]")
            and go_type != "[]interface{}"
            and isinstance(node, Tree)
            and node.data in {"funccall", "var", "getattr", "propagate"}
            and self._expr_go_type_hint(node) == "[]interface{}"
            and not raw.startswith(go_type + "{")
        ):
            # ``evens: list[int] = Iter...toList()`` (a ``list[any]``): a
            # checked conversion of every element.
            elem = self._slice_elem_go_type(go_type)
            return (
                f"func(src []interface{{}}) {go_type} {{ out := make({go_type}, len(src)); "
                f"for i, v := range src {{ out[i] = {self._result_payload_assert_go('v', elem)} }}; return out }}({raw})"
            )
        if (
            go_type.startswith(("[]", "map["))
            and isinstance(node, Tree)
            and node.data not in {"list", "dict", "list_comprehension", "dict_comprehension", "set", "set_comprehension"}
        ):
            # ``xs: list[any] = ys`` with ``ys: list[int]`` (also arguments,
            # returns, ``Result.Ok`` payloads, ``dict[str, int]`` to
            # ``dict[str, any]``, nested lists, int to float elements): a
            # converted copy, like Python's ``list(ys)``.
            # (typed in the context ``raw`` was lowered in: an enclosing
            # statement's expected type must not leak into the hint)
            with self._scoped(_expected_expr_go_type=go_type, _expected_expr_type_key=type_key or go_type):
                source = self._expr_go_type_hint(node) or ""
            if source and source != go_type and not raw.startswith(go_type + "{"):
                widened = self._container_widening_go(raw, source, go_type)
                if widened:
                    return widened
        if (
            go_type in self._NUMERIC_GO_TYPES
            and go_type != "float64"
            and isinstance(node, Tree)
            and self._tree_contains(node, "power")
        ):
            return f"{go_type}({raw})"
        if go_type not in {"", "interface{}"} and isinstance(node, Tree) and node.data == "funccall" and node.children:
            callee = node.children[0]
            if isinstance(callee, Tree) and callee.data == "getattr":
                receiver = callee.children[0]
                method = self._get_name(callee.children[1])
                if (
                    method in {"unwrap", "unwrapOr"}
                    and self._expr_go_type_hint(receiver) == "*Result"
                    and not self._unwrap_payload_go_type(node)
                ):
                    return self._result_payload_assert_go(raw, go_type)
        # ── Class-pointer coercion ─────────────────────────────
        # When annassign targets a Lam class (``go_type`` is
        # ``*<ClassName>``) and the value is either:
        #
        #   * an attribute access on another Lam object
        #     (``r.error`` / ``r.value`` — the attribute's Go type is
        #     ``interface{}``), or
        #   * a bare variable reference whose tracked Go type is
        #     ``interface{}`` (e.g. an ``err`` bound by a ``catch``
        #     clause — we now preserve the live panic value rather
        #     than stringifying it, so typed unboxing of the caught
        #     object is the natural way to reach its fields),
        #
        # wrap the rvalue in a type assertion so the declaration
        # typechecks:
        #
        #     err: Error = r.error
        #   → var err *Error = r.Error.(*Error)
        #
        #     ve:  ValidationError = err   # err bound by catch
        #   → var ve *ValidationError = err.(*ValidationError)
        #
        # ``getattr`` / ``var`` are the only shapes we coerce —
        # function calls and constructors already return typed
        # ``*Class`` values and double-casting would be a silent
        # correctness hazard if the return type ever drifts.
        if go_type.startswith("*") and not raw.endswith(f".({go_type})"):
            class_name = go_type[1:]
            if class_name in self._class_names and isinstance(node, Tree):
                if node.data == "getattr":
                    hint = self._expr_go_type_hint(node)
                    if hint == go_type:
                        return raw
                    if not hint or hint == "interface{}":
                        return f"lamAsNil[{go_type}]({raw})"
                if node.data == "var":
                    var_name = self._get_name(node.children[0])
                    if self._var_go_types.get(var_name) == "interface{}":
                        return f"lamAsNil[{go_type}]({raw})"
        if go_type not in {"", "interface{}"} and isinstance(node, Tree) and node.data == "getitem":
            class_name = self._infer_expr_class(node.children[0])
            if self._method_return_types.get(f"{class_name}.__getitem__") in {"any", "object"}:
                return self._result_payload_assert_go(raw, go_type)
        return raw

    # Go types of Lam value types that never hold ``None`` (scalars).
    _VALUE_GO_TYPES = frozenset({
        "int", "int8", "int16", "int32", "int64", "uint", "float64", "float32",
        "string", "bool", "byte", "rune",
    })

    def _container_widening_go(self, raw: str, source: str, target: str) -> str:
        """``raw`` (Go type ``source``) converted to the wider container
        ``target``: ``[]int`` to ``[]interface{}``, ``map[string]int`` to
        ``map[string]interface{}``, ``[][]int`` to ``[][]interface{}``,
        ``[]int`` to ``[]float64``. The result is a new list / dict (element
        writes do not reach the source). ``""`` when no such widening applies
        (``raw`` may be ``""`` to only test that)."""
        source, target = (source or "").strip(), (target or "").strip()
        if not source or not target or source == target:
            return ""
        if not (source.startswith(("[]", "map[")) and target.startswith(("[]", "map["))):
            return ""
        converted = self._widen_value_go(raw, source, target, 0)
        return converted if converted is not None and converted != raw else ""

    def _widen_value_go(self, expr: str, source: str, target: str, depth: int):
        """Go expression converting ``expr`` from ``source`` to ``target`` by
        implicit widening (to ``interface{}``, int to float, element-wise
        for lists and dicts), or ``None``."""
        if source == target:
            return expr
        if target == "interface{}":
            return expr if source else None
        if target in {"float64", "float32"} and source in _GO_INT_TYPES:
            return f"{target}({expr})"
        if depth > 8:
            return None
        if source.startswith("[]") and target.startswith("[]"):
            src_elem, dst_elem = source[2:], target[2:]
            elem = self._widen_value_go(f"__lamE{depth}", src_elem, dst_elem, depth + 1)
            if elem is None:
                return None
            return (
                f"func(__lamS{depth} {source}) {target} {{ if __lamS{depth} == nil {{ return nil }}; "
                f"__lamO{depth} := make({target}, len(__lamS{depth})); "
                f"for __lamI{depth}, __lamE{depth} := range __lamS{depth} {{ __lamO{depth}[__lamI{depth}] = {elem} }}; "
                f"return __lamO{depth} }}({expr})"
            )
        if source.startswith("map[") and target.startswith("map["):
            src_key, src_value = self._split_go_map_type(source)
            dst_key, dst_value = self._split_go_map_type(target)
            if not src_key or src_key != dst_key or self._is_go_set_type(source) or self._is_go_set_type(target):
                return None
            value = self._widen_value_go(f"__lamE{depth}", src_value, dst_value, depth + 1)
            if value is None:
                return None
            return (
                f"func(__lamS{depth} {source}) {target} {{ if __lamS{depth} == nil {{ return nil }}; "
                f"__lamO{depth} := make({target}, len(__lamS{depth})); "
                f"for __lamK{depth}, __lamE{depth} := range __lamS{depth} {{ __lamO{depth}[__lamK{depth}] = {value} }}; "
                f"return __lamO{depth} }}({expr})"
            )
        return None

    # ─── Native sets (``map[T]bool``) ──────────────────────────

    _SET_METHODS = frozenset({"add", "remove", "discard", "contains"})

    def _is_lam_set_value(self, raw_obj: str, go_type: str) -> bool:
        """``go_type`` is a set's ``map[T]bool`` and the value is not known
        to be a ``dict[K, bool]`` (same Go shape)."""
        if not self._is_go_set_type(go_type):
            return False
        lam_type = getattr(self, "_var_lam_types", {}).get(raw_obj)
        return not isinstance(lam_type, DictType)

    def _set_method_call_to_go(self, raw_obj: str, method: str, inferred_go: str, args_node) -> Optional[str]:
        """``s.add(x)`` / ``remove`` / ``discard`` / ``contains`` on a set."""
        if method not in self._SET_METHODS:
            return None
        obj_cls = self._var_types.get(raw_obj, "")
        if obj_cls and obj_cls in self._class_names:
            return None
        set_go = self._var_go_types.get(raw_obj, "") or inferred_go
        if not self._is_lam_set_value(raw_obj, set_go):
            return None
        raw_args = self._get_raw_call_args(args_node)
        if len(raw_args) != 1:
            return None
        key_go, _ = self._split_go_map_type(set_go)
        lam_set = getattr(self, "_var_lam_types", {}).get(raw_obj)
        if method == "add" and isinstance(lam_set, GenericType) and lam_set.base == "set" and lam_set.args:
            # ``s.add(Box([]))`` on a ``set[Box[int]]``: the item type binds
            # the call's type parameters.
            self._note_expected_call_lam(raw_args[0], lam_set.args[0])
        elif method == "add":
            self._note_expected_go_item(raw_args[0], key_go)
        item = self._typed_value_to_go(raw_args[0], key_go)
        if method == "add":
            return f"{raw_obj}[{item}] = true"
        if method == "discard":
            return f"delete({raw_obj}, {item})"
        if method == "contains":
            return f"{raw_obj}[{item}]"
        self._need_import("fmt")
        return (
            f"func(k {key_go}) {{ if _, ok := {raw_obj}[k]; !ok {{ panic(fmt.Sprintf(\"KeyError: %v\", k)) }}; "
            f"delete({raw_obj}, k) }}({item})"
        )

    def _set_operand_go_type(self, node) -> str:
        hint = self._expr_go_type_hint(node) or ""
        if not hint and isinstance(node, Tree) and node.data in {"set", "set_comprehension"}:
            hint = self._infer_type_from_value(node) or ""
        if not hint and isinstance(node, Tree) and node.data in {"or_expr", "and_expr", "arith_expr"}:
            first = next((c for c in node.children if isinstance(c, Tree)), None)
            hint = self._set_operand_go_type(first) if first is not None else ""
        if isinstance(node, Tree) and node.data == "var" and node.children:
            if not self._is_lam_set_value(self._get_name(node.children[0]), hint):
                return ""
        return hint if self._is_go_set_type(hint) else ""

    def _set_binop_to_go(self, op: str, left_go: str, right_go: str, set_type: str) -> str:
        """Union (``|``), intersection (``&``) or difference (``-``) of two
        sets as a fresh set; the operands are evaluated once."""
        if op == "|":
            body = "for k := range l { out[k] = true }; for k := range r { out[k] = true }"
            alloc = f"make({set_type}, len(l)+len(r))"
        elif op == "&":
            body = "for k := range l { if r[k] { out[k] = true } }"
            alloc = f"{set_type}{{}}"
        else:
            body = "for k := range l { if !r[k] { out[k] = true } }"
            alloc = f"{set_type}{{}}"
        return f"func(l, r {set_type}) {set_type} {{ out := {alloc}; {body}; return out }}({left_go}, {right_go})"

    def _set_chain_to_go(self, node: Tree, op: str) -> str:
        """``a | b | c`` (one operator) over sets, or ``""``."""
        operands = [c for c in node.children if isinstance(c, Tree)]
        if len(operands) < 2:
            return ""
        ops = {str(c) for c in node.children if isinstance(c, Token)}
        if ops and ops != {op}:
            return ""
        set_type = self._set_operand_go_type(operands[0])
        if not set_type:
            return ""
        result = self._typed_value_to_go(operands[0], set_type)
        for operand in operands[1:]:
            result = self._set_binop_to_go(op, result, self._typed_value_to_go(operand, set_type), set_type)
        return result

    def _q_propagate_stmt(self, value: str) -> str:
        """The statement ``?`` uses to return the error ``value``."""
        if self._in_async_func and self._async_chan_name:
            return f"{self._async_chan_name} <- func() *Result {{ return {value} }}; return"
        return f"return {value}"

    _JSON_LITERAL_NODES = frozenset({"dict", "list", "list_comprehension", "tuple_comprehension", "dict_comprehension"})

    def _is_json_literal_node(self, node) -> bool:
        """``node`` builds a new object or array (a literal or a
        comprehension) when lowered by ``_json_value_to_go``."""
        return isinstance(node, Tree) and node.data in self._JSON_LITERAL_NODES

    def _json_member_to_go(self, node) -> str:
        """A member of a json literal; a nested literal is a json value of
        its own (``lamJSONLiteral``), which the outer one then shares."""
        value = self._json_value_to_go(node)
        return f"lamJSONLiteral({value})" if self._is_json_literal_node(node) else value

    def _json_value_to_go(self, node) -> str:
        if isinstance(node, Tree) and node.data == "dict":
            entries = []
            for child in node.children:
                if isinstance(child, Tree) and child.data == "key_value" and len(child.children) >= 2:
                    key = self._typed_value_to_go(child.children[0], "string")
                    value = self._json_member_to_go(child.children[1])
                    entries.append(f"{key}: {value}")
            return f"map[string]interface{{}}{{{', '.join(entries)}}}"
        if isinstance(node, Tree) and node.data == "list":
            values = [self._json_member_to_go(child) for child in node.children if child is not None]
            return f"[]interface{{}}{{{', '.join(values)}}}"
        if isinstance(node, Tree) and node.data in {"list_comprehension", "tuple_comprehension"}:
            return self._list_comp_to_go(node, elem_type="[]interface{}")
        if isinstance(node, Tree) and node.data == "dict_comprehension":
            return self._dict_comp_to_go(node, map_type="map[string]interface{}")
        return self._expr_to_go(node)

    def _checked_byte_to_go(self, value_go: str) -> str:
        """``value_go`` (an ``int``) as a ``byte``; values outside 0..255
        raise ``ValueError`` instead of wrapping around."""
        self._need_import("fmt")
        return (
            "func(_v int) byte { if _v < 0 || _v > 255 { panic(fmt.Sprintf("
            "\"ValueError: byte must be in range(0, 256), got %d\", _v)) }; return byte(_v) }"
            f"({value_go})"
        )

    def _bytes_conversion_to_go(self, raw_arg, arg_go: str) -> str:
        """``bytes()`` / ``bytes(text)`` / ``bytes(codes)`` / ``bytes(data)``."""
        if raw_arg is None:
            return "[]byte{}"
        if isinstance(raw_arg, Tree) and raw_arg.data in {"list", "list_comprehension"}:
            # ``bytes([104, 105])`` / ``bytes([c + 1 for c in codes])``.
            return self._typed_value_to_go(raw_arg, "[]byte")
        source = (self._expr_go_type_hint(raw_arg) or self._infer_type_from_value(raw_arg) or "").strip()
        if source == "string":
            return f"[]byte({arg_go})"
        if source == "[]byte":
            # A copy: the result never aliases the argument.
            return f"append([]byte{{}}, {arg_go}...)"
        if source.startswith("[]") and self._slice_elem_go_type(source) in {"int", "byte", "uint8"}:
            if self._slice_elem_go_type(source) != "int":
                return f"append([]byte{{}}, {arg_go}...)"
            return (
                "func(_src []int) []byte { _out := make([]byte, len(_src)); "
                f"for _i, _v := range _src {{ _out[_i] = {self._checked_byte_to_go('_v')} }}; return _out }}({arg_go})"
            )
        self._need_import("fmt")
        return (
            "func(_src interface{}) []byte { switch _v := _src.(type) { "
            "case string: return []byte(_v); "
            "case []byte: return append([]byte{}, _v...); "
            "case []int: _out := make([]byte, len(_v)); for _i, _n := range _v { "
            f"_out[_i] = {self._checked_byte_to_go('_n')} }}; return _out; "
            "case []interface{}: _out := make([]byte, len(_v)); for _i, _x := range _v { "
            "_n, _ok := _x.(int); if !_ok { panic(fmt.Sprintf(\"TypeError: bytes(...) expects int items, got %T\", _x)) }; "
            f"_out[_i] = {self._checked_byte_to_go('_n')} }}; return _out }}; "
            "panic(fmt.Sprintf(\"TypeError: bytes(...) cannot convert %T\", _src)) }"
            f"({arg_go})"
        )

    def _typed_list_literal_to_go(self, node: Tree, go_type: str) -> str:
        elem_type = self._slice_elem_go_type(go_type)
        elems = [
            self._typed_value_to_go(c, elem_type)
            for c in node.children
            if c is not None
        ]
        return f"{go_type}{{{', '.join(elems)}}}"

    def _typed_dict_literal_to_go(self, node: Tree, go_type: str) -> str:
        key_type, value_type = self._split_go_map_type(go_type)
        entries = []
        for child in node.children:
            if isinstance(child, Tree) and child.data == "key_value":
                k = self._typed_value_to_go(child.children[0], key_type)
                v = self._typed_value_to_go(child.children[1], value_type)
                entries.append(f"{k}: {v}")
        return f"{go_type}{{{', '.join(entries)}}}"

    def _typed_set_literal_to_go(self, node: Tree, go_type: str) -> str:
        key_type, _value_type = self._split_go_map_type(go_type)
        elems = [
            self._typed_value_to_go(c, key_type)
            for c in node.children
            if c is not None
        ]
        if not elems:
            return f"{go_type}{{}}"
        lines = [f"func() {go_type} {{",
                 f"\t_s := {go_type}{{}}"]
        for e in elems:
            lines.append(f"\t_s[{e}] = true")
        lines.append("\treturn _s")
        lines.append("}()")
        return self._pin_multiline_to(node, "\n".join(lines))

    @staticmethod
    def _split_go_map_type(go_type: str) -> tuple[str, str]:
        """Split ``map[K]V`` into ``(K, V)`` without regexing nested types."""
        if not go_type.startswith("map["):
            return "", ""
        depth = 0
        for i, ch in enumerate(go_type[4:], start=4):
            if ch == "[":
                depth += 1
            elif ch == "]":
                if depth == 0:
                    return go_type[4:i], go_type[i + 1:]
                depth -= 1
        return "", ""

    @classmethod
    def _sorted_map_key_go_type(cls, go_type: str) -> str:
        """Key type ``K`` of a set's ``map[K]bool`` / a dict's ``map[K]V``:
        ``sorted()`` of either is the sorted ``[]K`` of its keys."""
        key_type, _value_type = cls._split_go_map_type((go_type or "").strip())
        return key_type

    def _sorted_source_go_type(self, node) -> str:
        """Go type of ``sorted()``'s argument; a set comprehension
        (``{x.toUpper() for x in xs}``, untyped otherwise) is typed by its
        element (``map[string]bool``)."""
        source = self._expr_go_type_hint(node) or self._infer_type_from_value(node) or ""
        if (
            isinstance(node, Tree) and node.data in {"or_expr", "and_expr", "arith_expr"}
            and not source.startswith(("[]", "map["))
        ):
            # ``sorted(a | b)`` / ``sorted(a - b)``: a set operation.
            source = self._set_operand_go_type(node) or source
        if (
            isinstance(node, Tree) and node.data == "set_comprehension"
            and self._sorted_map_key_go_type(source) in {"", "interface{}"}
        ):
            elem = self._list_comp_static_elem_go_type(node)
            if elem:
                return f"map[{elem}]bool"
        return source

    @classmethod
    def _is_go_set_type(cls, go_type: str) -> bool:
        """Return True for the Go map representation used by Lam sets."""
        _key_type, value_type = cls._split_go_map_type((go_type or "").strip())
        return value_type == "bool"

    @staticmethod
    def _slice_elem_go_type(go_type: str) -> str:
        go_type = (go_type or "").strip()
        return go_type[2:] if go_type.startswith("[]") else ""

    @staticmethod
    def _split_go_tuple_type(go_type: str) -> list[str]:
        go_type = (go_type or "").strip()
        if not (go_type.startswith("(") and go_type.endswith(")")):
            return []
        inner = go_type[1:-1]
        parts: list[str] = []
        current = ""
        depth = 0
        for ch in inner:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(current.strip())
                current = ""
            else:
                current += ch
        if current.strip():
            parts.append(current.strip())
        return parts

    _STRING_METHOD_RESULT_GO_TYPES = {
        "split": "[]string", "fields": "[]string", "splitLines": "[]string", "splitN": "[]string",
        "length": "int", "index": "int", "lastIndex": "int", "count": "int",
        "contains": "bool", "hasPrefix": "bool", "hasSuffix": "bool", "startsWith": "bool",
        "endsWith": "bool", "equalFold": "bool", "isAlpha": "bool", "isDigit": "bool",
        "isAlnum": "bool", "isSpace": "bool", "containsAny": "bool", "isEmpty": "bool",
        "isBlank": "bool",
        "repeat": "string", "toUpper": "string", "toLower": "string", "trim": "string",
        "trimLeft": "string", "trimRight": "string", "replace": "string", "title": "string",
        "capitalize": "string", "reverse": "string", "center": "string", "zfill": "string",
        "padLeft": "string", "padRight": "string", "replaceFirst": "string", "indent": "string",
        "dedent": "string", "format": "string", "camelCase": "string", "pascalCase": "string",
        "snakeCase": "string", "kebabCase": "string", "join": "string",
    }

    def _coalesce_fallback_node(self, node, operands: list):
        """The right side of ``operands[0] ?? ...``: the second operand, or
        for a chain ``a ?? b ?? c`` the (cached) sub-chain ``b ?? c``."""
        if len(operands) <= 2:
            return operands[-1]
        cache = self.__dict__.setdefault("_coalesce_rest_cache", {})
        entry = cache.get(id(node))
        if entry is not None and entry[0] is node:
            return entry[1]
        rest = Tree("null_coalesce", list(operands[1:]))
        cache[id(node)] = (node, rest)
        return rest

    def _coalesce_plan(self, left, fallback, count: int) -> tuple[str, str]:
        """How ``left ?? fallback`` lowers and the Go type it produces:
        ``("json", T)`` a JSON value (``T`` from the expected type),
        ``("none", T)`` ``x ?? None``, ``("map", V)`` a map lookup,
        ``("propagate", T)`` a ``Result`` payload (``load()? ?? x``),
        ``("plain", T)`` a value type (never ``None``), ``("markup", T)`` an
        ``html`` / ``xml`` value, ``("class", T)`` a class reference or an
        interface value, or ``("", "")`` untyped."""
        if not isinstance(left, Tree):
            return "", ""
        left_hint = self._expr_go_type_hint(left) or ""
        if count == 2 and left_hint == "LamJSON":
            expected = (getattr(self, "_expected_expr_go_type", "") or "").strip()
            if expected in {"", "LamJSON"}:
                return "json", "LamJSON"
            return "json", "" if expected == "interface{}" else expected
        if count == 2 and self._is_none_literal(fallback):
            return "none", left_hint
        if (
            count == 2
            and left.data == "getitem"
            and not (isinstance(left.children[1], Tree) and left.children[1].data == "slice")
        ):
            map_type = self._expr_go_type_hint(left.children[0]) or ""
            key_type, value_type = self._split_go_map_type(map_type)
            if key_type and value_type and not self._is_go_set_type(map_type):
                fallback_type = self._expr_go_type_hint(fallback) or self._infer_type_from_value(fallback) or ""
                result_type = value_type
                if value_type != "interface{}" and fallback_type not in {"", "interface{}", value_type}:
                    result_type = "interface{}"
                return "map", result_type
        if count == 2 and left.data == "propagate" and left.children:
            # ``load()? ?? "none"``: the ``Result`` payload stays dynamic
            # until ``??`` tests it for ``None`` (``Result.Ok(None)`` built
            # from an ``any``); a non-``None`` payload converts to the
            # declared payload type.
            payload = self._propagated_payload_go_type(left.children[0]) or ""
            if payload in {"", "interface{}"}:
                return "propagate", "interface{}"
            fb = ""
            if isinstance(fallback, Tree):
                fb = self._expr_go_type_hint(fallback) or self._infer_type_from_value(fallback) or ""
            if payload in _GO_INT_TYPES and fb in {"float64", "float32"}:
                return "propagate", fb
            return "propagate", payload
        if count == 2 and left_hint in self._VALUE_GO_TYPES:
            # ``n ?? -1`` with ``n`` a value type: never ``None``, it is
            # ``n`` (the checker warns; an int with a float fallback widens).
            fb = ""
            if isinstance(fallback, Tree):
                fb = self._expr_go_type_hint(fallback) or self._infer_type_from_value(fallback) or ""
            if fb in {"", "interface{}", left_hint}:
                return "plain", left_hint
            if fb in _GO_INT_TYPES and left_hint in {"float64", "float32"}:
                return "plain", left_hint
            if left_hint in _GO_INT_TYPES and fb in {"float64", "float32"}:
                return "plain", fb
        if left_hint in {"LamHTML", "LamXML"}:
            # ``h ?? html"..."``: a ``None`` markup value is the zero value.
            return "markup", left_hint
        if left_hint in self._interface_go_names():
            return "class", left_hint
        if not left_hint.startswith("*") or left_hint == "*Result":
            return "", ""
        inner = left_hint[1:]
        if inner.split("[", 1)[0] in {self._go_class_name(c) for c in self._class_names}:
            return "class", left_hint
        return "", ""

    def _hint_free_names(self, node) -> tuple:
        """Names read below ``node`` (cached per node), the state a call's
        type hint depends on besides the program-wide metadata."""
        cache = self.__dict__.setdefault("_hint_free_names_cache", {})
        entry = cache.get(id(node))
        if entry is not None and entry[0] is node:
            return entry[1]
        names = set()
        stack = [node]
        while stack:
            cur = stack.pop()
            if isinstance(cur, Tree):
                if cur.data == "var" and cur.children:
                    names.add(self._get_name(cur.children[0]))
                stack.extend(cur.children)
        result = tuple(sorted(n for n in names if n))
        cache[id(node)] = (node, result)
        return result

    def _interface_go_names(self) -> frozenset:
        cache = self.__dict__.get("_interface_go_names_cache")
        if cache is None or cache[0] != len(self._interfaces):
            cache = (len(self._interfaces), frozenset(self._go_class_name(i) for i in self._interfaces))
            self._interface_go_names_cache = cache
        return cache[1]

    def _expr_go_type_hint(self, node) -> str:
        """Best-effort Go type for an expression that can be assigned to."""
        if isinstance(node, Tree) and node.data == "funccall":
            # Chained generic calls type their receiver several times per
            # link; memoize on the state the hint reads.
            names = self._hint_free_names(node)
            state = (
                tuple((n, self._var_go_types.get(n), self._var_types.get(n), n in self.declared_vars) for n in names),
                self.current_class, self._self_replacement,
                tuple(sorted(self._generic_names)), tuple(sorted(self._call_type_subst.items())),
                getattr(self, "_expected_expr_go_type", ""),
            )
            memo = self.__dict__.setdefault("_hint_memo", {})
            entry = memo.get(id(node))
            if entry is not None and entry[0] is node and entry[1] == state:
                return entry[2]
            result = self._expr_go_type_hint_uncached(node)
            memo[id(node)] = (node, state, result)
            return result
        return self._expr_go_type_hint_uncached(node)

    def _expr_go_type_hint_uncached(self, node) -> str:
        """Best-effort Go type for an expression that can be assigned to."""
        if not isinstance(node, Tree):
            return ""
        markup_type = self._markup_expr_go_type(node)
        if markup_type:
            return markup_type
        if node.data == "lambdef":
            return self._lambda_go_func_type(node)
        if node.data == "cast_expr" and len(node.children) == 2:
            # ``expr as T`` is a ``T``.
            return self._type_expr_to_go(node.children[1])
        if node.data == "propagate" and node.children:
            # ``parse(s)?``: the lowering asserts a known payload type, so
            # operators around it (``n()? * 2.5``, ``word()? + "!"``,
            # ``-n()?``) see that type. An unknown / ``any`` payload stays
            # unknown (the enclosing context's cast decides).
            payload = self._propagated_payload_go_type(node.children[0])
            return payload if payload != "interface{}" else ""
        if node.data == "null_coalesce":
            # The type the ``??`` lowering produces (``load()? ?? 0`` over a
            # ``Result[int]`` is an ``int``); untyped forms stay unknown.
            operands = [c for c in node.children if c is not None]
            if len(operands) >= 2:
                _kind, result_type = self._coalesce_plan(
                    operands[0], self._coalesce_fallback_node(node, operands), len(operands),
                )
                if result_type:
                    return result_type
        if node.data == "test" and len(node.children) == 3:
            return self._ternary_common_go_type(node)
        if node.data == "list_comprehension":
            elem = self._list_comp_static_elem_go_type(node)
            return f"[]{elem}" if elem else ""
        if node.data in ("string", "fstring", "string_concat"):
            return "string"
        if node.data == "set_comprehension":
            elem = self._list_comp_static_elem_go_type(node)
            return f"map[{elem}]bool" if elem in ("int", "string", "float64", "bool") else ""
        if node.data in ("list", "dict", "set") and node.children:
            # ``{"a": 1}[k]`` / ``[1, 2][0:1]`` / ``for x in {n, 3}``: a
            # literal with uniformly typed items has their type.
            literal = self._infer_type_from_value(node) or ""
            if literal and "interface{}" not in literal:
                return literal
        if (
            node.data == "power" and len(node.children) > 1 and node.children[1] is not None
            and not self._infer_expr_class(node.children[0])
        ):
            # ``int ** int`` is an int (``lamIPow``); otherwise ``a ** b``
            # lowers to ``math.Pow``, a ``float``, so ``a + b ** c`` widens ``a``.
            return "int" if self._int_power_operands(node) else "float64"
        if node.data in ("arith_expr", "term") and len(node.children) >= 3:
            if node.data == "arith_expr" and {str(c) for c in node.children[1::2]} == {"+"}:
                list_operands = list(node.children[0::2])
                concat = self._list_concat_go_type(list_operands) if all(
                    isinstance(c, Tree) for c in list_operands
                ) else ""
                if concat:
                    return concat  # ``xs + ys``: a new list
            # ``size - offset``: ints stay int, a float operand widens; int
            # ``/`` and other mixes stay unknown (previous behaviour).
            kinds = []
            for child in node.children[0::2]:
                if isinstance(child, Token):
                    text = str(child)
                    if child.type in {"STRING", "LONG_STRING"}:
                        kinds.append("string")
                    else:
                        kinds.append("float64" if child.type in {"FLOAT_NUMBER", "DECIMAL"} or "." in text else ("int" if child.type.endswith("NUMBER") else ""))
                else:
                    kind = self._expr_go_type_hint(child) or self._infer_type_from_value(child)
                    kinds.append(kind if kind != "interface{}" else "")
            ops = {str(c) for c in node.children[1::2]}
            if node.data == "term" and "/" in ops and ops <= {"*", "/", "//", "%"} and all(
                k in self._NUMERIC_GO_TYPES or k in self._generic_names or k == "" for k in kinds
            ):
                # Python's ``/`` is a float division.
                return "float64"
            if kinds and all(k == "int" for k in kinds) and ops <= {"+", "-", "*", "//", "%"}:
                return "int"
            if kinds and all(k in {"int", "float64"} for k in kinds) and "float64" in kinds and ops <= {"+", "-", "*", "/", "//", "%"}:
                return "float64"
            if kinds and all(k == "string" for k in kinds) and ops == {"+"}:
                return "string"
            if node.data == "term" and ops == {"*"} and kinds.count("string") == 1 and all(
                k in ("string", "int") for k in kinds
            ):
                # ``("ab" * 2).split("b")``: string repetition is a string.
                return "string"
            return ""
        if node.data == "var" and node.children:
            name = self._get_name(node.children[0])
            if name == "_":
                return ""
            if name == "self" and self.current_class:
                return f"*{self._go_class_name(self.current_class)}"
            found = self._var_go_types.get(name, "")
            if not found and name in self._link_const_go_names:
                # A namespaced global (``Names`` emitted as ``Names__main``).
                found = self._var_go_types.get(self._link_const_go_names[name], "")
            return found
        if node.data == "funccall" and node.children:
            callee = node.children[0]
            if (
                isinstance(callee, Tree) and callee.data in {"getattr", "getitem"}
                and self._func_value_lam_type(callee) is not None
            ):
                # ``self.loader(2)`` / ``fns[0](x)``: a function value's
                # declared result.
                split = self._split_go_func_type(self._expr_go_type_hint(callee) or "")
                if split is not None and split[1]:
                    return split[1]
                declared = self._func_value_call_result_go(node, "result")
                if declared:
                    return declared
            if isinstance(callee, Tree) and callee.data == "var" and callee.children:
                name = self._get_name(callee.children[0])
                if name == "json":
                    return "LamJSON"
                if name in {"int", "float", "bool", "str", "bytes"} and name not in self._function_go_names:
                    return {"int": "int", "float": "float64", "bool": "bool", "str": "string", "bytes": "[]byte"}[name]
                if name == "len" and name not in self._user_functions:
                    return "int"
                if name == "sorted" and name not in self._user_functions and name not in self._var_go_types:
                    # ``for x in sorted(opt)``: a copy of the argument's list type.
                    args_node = node.children[1] if len(node.children) > 1 else None
                    raw_args = self._get_raw_call_args(args_node) if isinstance(args_node, Tree) else []
                    if raw_args:
                        source = self._sorted_source_go_type(raw_args[0])
                        if source.startswith("[]"):
                            return source
                        map_key = self._sorted_map_key_go_type(source)
                        if map_key:
                            # A set's / dict's sorted keys (``[]K``).
                            return f"[]{map_key}"
                if name == "set" and name not in self._user_functions and name not in self._var_go_types:
                    # ``sorted(set(xs))`` / ``len(set(xs))``: ``set(iterable)``
                    # is a ``map[E]bool`` of the iterable's elements.
                    args_node = node.children[1] if len(node.children) > 1 else None
                    raw_args = self._get_raw_call_args(args_node) if isinstance(args_node, Tree) else []
                    if len(raw_args) == 1:
                        source = self._expr_go_type_hint(raw_args[0]) or self._infer_type_from_value(raw_args[0]) or ""
                        if self._is_go_set_type(source):
                            return source
                        elem = self._slice_elem_go_type(source) if source.startswith("[]") else ""
                        if elem:
                            return f"map[{elem}]bool"
                if name == "reversed" and name not in self._user_functions and name not in self._var_go_types:
                    args_node = node.children[1] if len(node.children) > 1 else None
                    raw_args = self._get_raw_call_args(args_node) if isinstance(args_node, Tree) else []
                    if len(raw_args) == 1:
                        source = self._reversed_source_go_type(raw_args[0])
                        if source == "string":
                            return "[]string"
                        return source if source.startswith("[]") else "[]interface{}"
                if name in {"abs", "min", "max"} and name not in self._user_functions and name not in self._var_go_types:
                    # ``max(abs(a), 2)``: ints stay ints (``_numeric_builtin_to_go``).
                    result = self._numeric_builtin_go_type(name, node)
                    if result:
                        return result
                if name in self._async_functions and name not in self._var_go_types:
                    return self._async_handle_go_type(self._func_return_go_types.get(name, ""))
                if name in self._result_payload_go_types:
                    return "*Result"
                ret = self._func_return_go_types.get(name, "")
                generic_params = sorted(self._func_generic_params.get(name, set()) or ())
                if (
                    generic_params
                    and ret
                    and mentions_names(ret, generic_params)
                    and name not in self._var_go_types
                    and name not in self._class_names
                    and len(self._overload_variants.get(name, ())) <= 1
                    and not ret.startswith(("*Result", "("))
                ):
                    # ``first([1, 2])`` with ``first[T](xs: list[T]) -> T``:
                    # the result with this call's type arguments (``int``).
                    args_node = node.children[1] if len(node.children) > 1 else None
                    subst = self._generic_call_subst(name, args_node, generic_params)
                    bound = substitute_go_type(ret, subst)
                    if not mentions_names(bound, [n for n in generic_params if n not in self._generic_names]):
                        return bound
                    return ""
                if (
                    (ret.startswith("*") or ret in self._interface_go_names())
                    and len(self._overload_variants.get(name, ())) <= 1
                    and name not in self._class_names
                    and not ret.startswith("*Result")
                ):
                    # ``lookup(id) ?? fallback`` where ``lookup`` returns a
                    # class pointer (or an interface value).
                    return ret
                if ret.startswith("func(") and name not in self._class_names:
                    return ret
                if (
                    (ret.startswith("chan ") or generator_go_elem(ret) is not None)
                    and name in self._user_functions
                    and len(self._overload_variants.get(name, ())) <= 1
                    and name not in self._var_go_types
                    and name not in self._async_functions
                ):
                    # ``circles()`` with ``-> generator[Circle]``: a ``*lamGenerator[*Circle]``.
                    return ret
                if (
                    (ret in {"string", "int", "float64", "bool", "LamHTML", "LamXML", "LamJSON"} or ret.startswith(("[]", "map[")))
                    and name in self._user_functions
                    and len(self._overload_variants.get(name, ())) <= 1
                    and name not in self._var_go_types
                    and name not in self._async_functions
                ):
                    # ``word() * 2`` with ``word() -> str``.
                    return ret
                var_type = self._var_go_types.get(name, "")
                if var_type.startswith("func(") and name not in self._user_functions:
                    split = self._split_go_func_type(var_type)
                    if split is not None and split[1]:
                        return split[1]
            if isinstance(callee, Tree) and callee.data == "funccall":
                split = self._split_go_func_type(self._expr_go_type_hint(callee) or "")
                if split is not None:
                    return split[1]
            if (
                isinstance(callee, Tree) and callee.data == "getitem" and len(callee.children) >= 2
                and isinstance(callee.children[0], Tree) and callee.children[0].data == "var"
            ):
                # ``Box[str]("hi")``: an explicitly instantiated generic class.
                base_name = self._get_name(callee.children[0].children[0])
                if base_name in self._class_names and base_name in self._generic_classes:
                    type_args = self._type_arg_list(callee.children[1])
                    if type_args and all(type_args):
                        return f"*{self._go_class_name(base_name)}[{', '.join(type_args)}]"
            if isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) >= 2:
                receiver = callee.children[0]
                method = self._get_name(callee.children[1])
                string_result = self._STRING_METHOD_RESULT_GO_TYPES.get(method)
                if string_result and (
                    (isinstance(receiver, Tree) and receiver.data in {"string", "fstring", "string_concat"})
                    or self._expr_go_type_hint(receiver) == "string"
                ):
                    # ``"a,b".split(",").length()``: builtin str methods.
                    return string_result
                if method in {"map", "filter", "sort", "reduce", "any", "all"}:
                    receiver_hint = self._expr_go_type_hint(receiver) or ""
                    if receiver_hint.startswith("[]"):
                        # ``words.map(len).filter(...)``: builtin list methods.
                        list_result = self._list_method_result_go_type(
                            receiver_hint, method, node.children[1] if len(node.children) > 1 else None,
                        )
                        if list_result:
                            return list_result
                if method in {"unwrap", "unwrapOr"}:
                    # ``r.unwrap().name`` with ``r: Result[json]``.
                    payload = self._unwrap_payload_go_type(node)
                    if payload:
                        return payload
                class_name = self._infer_receiver_class(receiver)
                if not class_name and isinstance(receiver, Tree) and receiver.data == "var" and receiver.children:
                    candidate = self._get_name(receiver.children[0])
                    if candidate in self._class_names:
                        class_name = candidate
                    elif candidate == "self" and self.current_class:
                        # ``return self.label()`` in a method: the call's
                        # declared result.
                        class_name = self.current_class
                async_handle = self._async_method_handle_go_type(class_name, method) if class_name else ""
                if async_handle:
                    # ``tasks = [a.run(1), b.run(2)]``: a handle, not the
                    # method's declared result (of a generic class: with
                    # the call's type arguments, ``Box[str]("x").get()``).
                    if f"{class_name}.{method}" in self._generic_method_returns:
                        static = (
                            isinstance(receiver, Tree) and receiver.data == "var" and receiver.children
                            and self._get_name(receiver.children[0]) == class_name
                            and class_name not in self.declared_vars
                        )
                        payload = self._generic_method_result_go_type(
                            class_name, method, receiver,
                            node.children[1] if len(node.children) > 1 else None,
                            static=bool(static),
                        )
                        return self._async_handle_go_type(payload) if payload else ""
                    return async_handle
                if class_name and f"{class_name}.{method}" in self._result_payload_go_types:
                    return "*Result"
                if class_name:
                    # Methods of generic classes and generic methods: the
                    # declared result with the call's type arguments
                    # (``Iter.range_(0, 9).map(...)`` -> ``*Iter[int]``).
                    static = (
                        isinstance(receiver, Tree) and receiver.data == "var" and receiver.children
                        and self._get_name(receiver.children[0]) == class_name
                        and class_name not in self.declared_vars
                    )
                    generic_result = self._generic_method_result_go_type(
                        class_name, method, receiver,
                        node.children[1] if len(node.children) > 1 else None,
                        static=bool(static),
                    )
                    if generic_result:
                        return generic_result
                    # Scalar return types (static and instance methods, local
                    # or imported), so ``Rows.str(r, k).contains(...)`` and
                    # similar chains dispatch on the value's type.
                    owner = class_name
                    if f"{class_name}.{method}" not in self._method_return_types:
                        # ``p.label()`` of a method ``Product`` inherits from
                        # ``Item``: the ancestor's declared result.
                        owner = next(
                            (
                                a for a in self._hier_ancestors(class_name)
                                if f"{a}.{method}" in self._method_return_types and not self._generic_classes.get(a)
                            ),
                            class_name,
                        )
                    raw_return = self._method_return_types.get(f"{owner}.{method}", "")
                    go_return = self._method_return_types.get(f"go:{owner}.{method}", "")
                    scalar = {
                        "str": "string", "int": "int", "float": "float64", "bool": "bool",
                        "bytes": "[]byte", "json": "LamJSON", "html": "LamHTML", "xml": "LamXML",
                    }
                    if raw_return in scalar:
                        return scalar[raw_return]
                    container = self._method_return_types.get(f"go:{owner}.{method}", "")
                    if container:
                        return container
        if node.data == "dict_comprehension":
            # ``{k: v for ...}["x"]``: the map type its lowering gives it.
            return self._dict_comp_static_go_type(node)
        if node.data == "getitem" and len(node.children) >= 2:
            receiver_type = self._expr_go_type_hint(node.children[0])
            index = node.children[1]
            if receiver_type == "LamJSON":
                return "LamJSON"
            if isinstance(index, Tree) and index.data == "slice":
                return receiver_type
            elem_type = self._slice_elem_go_type(receiver_type)
            if elem_type:
                return elem_type
            if receiver_type.startswith("map["):
                _key, value = self._split_go_map_type(receiver_type)
                return value
            return ""
        if node.data == "getattr" and len(node.children) >= 2:
            obj = node.children[0]
            if self._expr_go_type_hint(obj) == "LamJSON":
                return "LamJSON"
            attr = self._get_name(node.children[1])
            class_name = ""
            if isinstance(obj, Tree) and obj.data == "var" and obj.children:
                obj_name = self._get_name(obj.children[0])
                if obj_name == "self" and self.current_class:
                    class_name = self.current_class
                elif obj_name in self._class_names:
                    static_name = self._static_var_go_name(obj_name, attr)
                    static_type = self._var_go_types.get(static_name, "")
                    if static_type:
                        return static_type
                else:
                    class_name = self._var_types.get(obj_name, "")
                    if not class_name:
                        # ``for it in items``: the loop variable's Go type
                        # (``*Item``) names its class.
                        obj_go_type = self._var_go_types.get(obj_name, "")
                        if obj_go_type.startswith("*") and "[" not in obj_go_type:
                            class_name = self._hier_class_from_go_type(obj_go_type) or ""
            elif isinstance(obj, Tree) and obj.data in {"funccall", "getattr", "getitem", "propagate"}:
                # ``current().box`` / ``holder.inner.box`` / ``docs[0].body`` /
                # ``load()?.body``: the receiver's class from call return
                # types, field types and container element types.
                class_name = self._infer_receiver_class(obj) or self._result_error_view_class(obj, attr)
            if class_name:
                # The class, then its ancestors (``self.points`` declared by
                # the base class).
                for owner in [class_name, *self._hier_ancestors(class_name)]:
                    for field_name, field_type in self.class_fields.get(owner, []):
                        if field_name == attr or self._go_field_name(owner, field_name) == attr:
                            return self._generic_field_go_type(owner, field_type, obj, class_name)
                    field_type = self._external_field_types.get(owner, {}).get(attr)
                    if field_type:
                        return self._generic_field_go_type(owner, field_type, obj, class_name)
        return ""

    def _generic_field_go_type(self, owner: str, field_type: str, receiver_node, class_name: str) -> str:
        """``b.v`` with ``v: T`` of ``class Box[T]`` read on a ``*Box[int]``
        (a loop, comprehension or lambda variable, an element): ``int``. A
        type parameter the receiver does not bind is unknown (``""``), never
        a ``T`` outside the class."""
        params = self._class_type_param_names_of(owner)
        if not params or not mentions_names(field_type, params):
            return field_type
        subst = self._receiver_type_subst(owner, receiver_node, class_name)
        if subst:
            return substitute_go_type(field_type, subst)
        if all(p in self._generic_names for p in params if mentions_names(field_type, [p])):
            return field_type
        return ""

    # ─── Main expression dispatcher ───────────────────────────

    def _expr_to_go(self, node) -> str:
        if node is None:
            return ""
        if isinstance(node, Token):
            return str(node)
        if not isinstance(node, Tree):
            return str(node)

        d = node.data

        reuse = self.__dict__.get("_reuse_lowering")
        if reuse:
            # A receiver already evaluated into a temporary (``obj?.m()``
            # calls ``m`` on ``__lamR``; ``a.f().g()`` reuses ``a.f()``).
            reused = reuse.get(id(node))
            if reused is not None and reused[0] is node:
                return reused[1]

        if self._propagate_cast_hint and d in self._CAST_HINT_OPAQUE_NODES:
            # ``a: str = (m.make()?).key``: the target's type is the type of
            # the field / call / element, not of the ``?`` payload inside.
            with self._scoped(_propagate_cast_hint=""):
                return self._expr_to_go(node)

        if d == "var":
            name = self._get_name(node.children[0])
            if name == "self" and self._self_replacement:
                return self._self_replacement
            parent_expr = self._parent_alias_expr(name)
            if parent_expr:
                return parent_expr
            # Bare references to user-defined top-level functions need to
            # resolve to their Go public name so `Http.serve(port, handler)`
            # passes the actual Go function value rather than an undefined
            # lowercase identifier. Guarded so this only fires when we are
            # not in a call position (the call path extracts the raw name
            # directly and wraps it in _funccall_to_go).
            if (
                name
                and name not in self.declared_vars
                and name in self._user_functions
                and not self._in_funccall_head
                and name != "main"
            ):
                return self._unshadowed_function(self._go_user_function_name(name), name)
            if (
                self._in_funccall_head
                and name in self._user_functions
                and name not in self.declared_vars
            ):
                # The call path maps the raw Lam name to the function's Go
                # name; binding-mangling a Go keyword first (``fallthrough``
                # -> ``fallthrough__lam``) would miss that lookup.
                return name
            if name in self._link_const_go_names and name not in self.declared_vars:
                return self._link_const_go_names[name]
            return self._go_binding_name(name)

        if d == "name":
            return self._get_name(node)

        if d == "number":
            return self._number_to_go(node)

        if d == "string":
            raw = str(node.children[0])
            if raw.startswith('"""') or raw.startswith("'''"):
                inner = raw[3:-3]
                inner = inner.replace('`', '` + "`" + `')
                return f"`{inner}`"
            # Normalise Python-style single-quoted strings to Go double-quoted
            # strings so that 'foo' does not produce a rune literal.
            if raw.startswith("'") and raw.endswith("'"):
                inner = raw[1:-1]
                # Unescape single quote, then escape any unescaped double quote.
                inner = inner.replace(r"\'", "'")
                inner = re.sub(r'(?<!\\)"', r'\\"', inner)
                return f'"{inner}"'
            return raw

        if d == "fstring":
            return self._fstring_to_go(node)

        if d == "string_concat":
            parts = [self._expr_to_go(c) for c in node.children if c is not None]
            return " + ".join(parts) if len(parts) > 1 else parts[0] if parts else '""'

        if d == "const_none":
            return "nil"
        if d == "const_true":
            return "true"
        if d == "const_false":
            return "false"
        if d == "ellipsis":
            return "nil"

        if d == "cast_expr" and len(node.children) == 2:
            return self._cast_expr_to_go(node)

        if d == "funccall":
            reused = self.__dict__.get("_reuse_lowering", {}).get(id(node))
            if reused is not None and reused[0] is node:
                return reused[1]
            expected_value = self._expected_value_lam
            registered = self._expected_call_lams.get(id(node)) if self._expected_call_lams else None
            if expected_value is not None or self._call_result_lam is not None or registered is not None:
                # The expected Lam type applies to the assigned (returned,
                # passed) call only, not to the calls in its arguments or
                # receiver.
                if expected_value is not None and expected_value[0] is node:
                    expected = expected_value[1]
                else:
                    expected = registered
                with self._scoped(_call_result_lam=expected, _expected_value_lam=None):
                    call = self._funccall_to_go(node)
            else:
                call = self._funccall_to_go(node)
            payload = self._unwrap_payload_go_type(node)
            if payload and call.endswith(")"):
                return self._result_payload_assert_go(call, payload)
            return call

        if d == "getattr":
            attr = self._get_name(node.children[1])
            obj_node = node.children[0]
            if self._expr_go_type_hint(obj_node) == "LamJSON":
                return f'lamJSONGet({self._expr_to_go(obj_node)}, "{attr}")'
            if isinstance(obj_node, Tree) and obj_node.data == "var":
                raw_obj = self._get_name(obj_node.children[0])
                if attr in self._static_vars.get(raw_obj, {}):
                    return self._static_var_go_name(raw_obj, attr)
                if (
                    raw_obj in self._class_names
                    and attr in self._static_methods.get(raw_obj, set())
                    and raw_obj not in self.declared_vars
                ):
                    # ``apply(K.twice)``: a static method used as a value is
                    # the generated package-level function, spelled like a
                    # call of it (a private one is unexported).
                    go_name = self._go_static_base(raw_obj, attr)
                    if attr in self._private_methods.get(raw_obj, set()):
                        go_name = self._go_private_name(go_name)
                    return go_name
            if isinstance(obj_node, Tree) and obj_node.data == "getattr" and len(obj_node.children) >= 2:
                namespace = obj_node.children[0]
                class_name = self._get_name(obj_node.children[1])
                if (
                    isinstance(namespace, Tree)
                    and namespace.data == "var"
                    and namespace.children
                    and self._get_name(namespace.children[0]) == "LAMMERGEIER"
                    and attr in self._static_vars.get(class_name, {})
                ):
                    return self._static_var_go_name(class_name, attr)
            class_name = self._infer_receiver_class(obj_node)
            if isinstance(obj_node, Tree) and obj_node.data == "var" and obj_node.children:
                if self._get_name(obj_node.children[0]) == "self" and self.current_class:
                    class_name = self.current_class
            error_view = "" if class_name else self._result_error_view_class(obj_node, attr)
            if error_view:
                # ``r.error.kind``: the error viewed as an ``Error`` (like the
                # ``do/catch`` binding: another value is wrapped).
                class_name = error_view
                obj = self._error_view_go(self._expr_to_go(obj_node), error_view)
            else:
                obj = self._expr_to_go(obj_node)
            if obj in LOWER_PKGS and not self._is_lam_local_node(obj_node):
                return f"{obj}.{attr}"
            if not error_view:
                # ``None.name`` raises ``AttributeError`` (``none_checks.py``).
                obj = self._nn_receiver_go(obj, obj_node, attr)
            if class_name:
                field_names = {name for name, _type in self.class_fields.get(class_name, [])}
                field_names.update(self._external_field_names.get(class_name, set()))
                if attr in field_names:
                    return f"{obj}.{self._go_field_name(class_name, attr)}"
                if attr in self._instance_methods.get(class_name, set()):
                    return f"{obj}.{self._go_method_name(class_name, attr)}"
            if self._is_private_method_name(attr):
                return f"{obj}.{self._go_private_name(attr)}"
            return f"{obj}.{self._go_public_name(attr)}"

        if d == "propagate":
            reused = self.__dict__.get("_reuse_lowering", {}).get(id(node))
            if reused is not None and reused[0] is node:
                # ``make()?.value()``: the receiver was lowered (and its
                # guard emitted) once already for the method call.
                return reused[1]
            # ``expr?`` — Result-propagation operator. Lowers to:
            #
            #     __qN := <expr>
            #     if !__qN.Ok() { return __qN }
            #     __qN.Value [.(GO_TYPE)]
            #
            # The typed assertion is added when the enclosing
            # annassign published its target Go type via
            # ``_propagate_cast_hint``; otherwise the result is left
            # as ``interface{}`` and either flows through an untyped
            # context (function arg of type ``any``, ``return``) or
            # the user lifts to a typed local for the cast.
            #
            # When the enclosing function *doesn't* return a
            # ``Result`` (and we aren't inside a ``do { } catch``
            # IIFE) there's no valid ``return`` target for the
            # propagated ``*Result``. The semantic checker already
            # warned the author, but we still need to emit *something*
            # that compiles. Falling back to ``panic(__qN.Error)``
            # turns an ``Err`` into an exception, which is recoverable
            # via a ``try { } catch { }`` upstream and surfaces the
            # underlying error message instead of Go's opaque
            # "too many return values" build failure.
            # The enclosing statement's cast applies to this ``?`` only, not
            # to ``?`` nested in its operand (``outer(inner()?)?``).
            raw_payload = getattr(self, "_propagate_raw_payload", False)
            with self._scoped(_propagate_cast_hint="", _propagate_raw_payload=False):
                inner_str = self._expr_to_go(node.children[0])
            self._q_temp_counter += 1
            tmp = f"__q{self._q_temp_counter}"
            self._emit(f"{tmp} := {inner_str}")
            self._emit_q_guard(tmp)
            self._declare_var(tmp)
            if raw_payload:
                # ``load()? ?? fallback``: ``??`` tests the payload itself.
                return f"{tmp}.Value"
            # The operand's own payload type wins over the enclosing
            # context's hint: the hint is the type of the whole expression
            # (``Result.Ok(parse(s)? * 2)`` publishes ``interface{}``,
            # ``x: float = n()? * 2`` publishes ``float64``), not of the
            # ``?`` operand inside it. The hint only fills in an unknown
            # (or ``any``) payload.
            payload = self._propagated_payload_go_type(node.children[0])
            cast = payload if payload not in {"", "interface{}"} else self._propagate_cast_hint
            if cast and cast != "interface{}":
                return self._result_payload_assert_go(f"{tmp}.Value", cast)
            return f"{tmp}.Value"

        if d == "null_coalesce":
            # ``a ?? b ?? c`` — return the first non-nil operand; evaluation
            # stops at the first non-nil value (Go's early ``return`` inside
            # the IIFE). A chain is ``a ?? (b ?? c)``, so the typed forms
            # below apply link by link (``a(1) ?? a(2) ?? 3`` is an ``int``).
            operands = [c for c in node.children if c is not None]
            if len(operands) < 2:
                return self._expr_to_go(operands[0]) if operands else "nil"
            left_node, fallback_node = operands[0], self._coalesce_fallback_node(node, operands)
            kind, result_type = self._coalesce_plan(left_node, fallback_node, len(operands))
            if kind == "json":
                left = self._expr_to_go(left_node)
                if result_type == "LamJSON":
                    # ``value: json = doc.key ?? None``: a JSON value either way.
                    fallback = self._typed_value_to_go(fallback_node, "LamJSON")
                    return f"func() LamJSON {{ if !lamJSONIsNull({left}) {{ return {left} }}; return {fallback} }}()"
                if result_type:
                    left_value = f"lamJSONRaw({left}).({result_type})"
                    if result_type in {"float64", "float32"}:
                        # ``x: float = doc.n ?? 0`` with ``30``: an integral
                        # JSON number is an ``int`` in the json value; widen it.
                        left_value = (
                            f"func(v interface{{}}) {result_type} {{ if n, ok := v.(int); ok {{ return {result_type}(n) }}; "
                            f"return v.({result_type}) }}(lamJSONRaw({left}))"
                        )
                    fallback = self._typed_value_to_go(fallback_node, result_type)
                    return f"func() {result_type} {{ if !lamJSONIsNull({left}) {{ return {left_value} }}; return {fallback} }}()"
                fallback = self._expr_to_go(fallback_node)
                return f"func() interface{{}} {{ if !lamJSONIsNull({left}) {{ return {left} }}; return {fallback} }}()"
            if kind == "none":
                # ``x ?? None`` is ``x``.
                return self._expr_to_go(left_node)
            if kind == "plain":
                left = self._expr_to_go(left_node)
                left_type = self._expr_go_type_hint(left_node) or ""
                return left if left_type == result_type else f"{result_type}({left})"
            if kind == "map":
                # ``d[key] ?? fallback`` on a map: a missing key (or a
                # ``None`` value) selects the fallback.
                map_type = self._expr_go_type_hint(left_node.children[0]) or ""
                key_type, value_type = self._split_go_map_type(map_type)
                receiver = self._expr_to_go(left_node.children[0])
                key = self._typed_value_to_go(left_node.children[1], key_type)
                fallback = self._typed_value_to_go(fallback_node, result_type)
                present = "ok" if value_type != "interface{}" and not value_type.startswith(("*", "[]", "map[")) else "ok && __lamV != nil"
                return (
                    f"func() {result_type} {{ if __lamV, ok := {receiver}[{key}]; {present} {{ return __lamV }}; "
                    f"return {fallback} }}()"
                )
            if kind == "propagate":
                # ``load()? ?? fallback``: the payload read as ``interface{}``
                # (an ``Err`` returns as for any ``?``); ``None`` selects the
                # fallback, another value converts to the payload type.
                with self._scoped(_propagate_raw_payload=True):
                    left = self._expr_to_go(left_node)
                fallback = self._typed_value_to_go(fallback_node, result_type)
                if result_type == "interface{}":
                    value = "__lamL"
                else:
                    value = self._result_payload_assert_go("__lamL", result_type)
                return f"func() {result_type} {{ if __lamL := {left}; __lamL != nil {{ return {value} }}; return {fallback} }}()"
            if kind == "markup":
                # ``h ?? html"..."``: a ``None`` markup value (the zero
                # ``LamHTML``) selects the fallback.
                left = self._expr_to_go(left_node)
                fallback = self._typed_value_to_go(fallback_node, result_type)
                return f"func() {result_type} {{ if __lamL := {left}; !__lamL.lamIsNone() {{ return __lamL }}; return {fallback} }}()"
            if kind == "class":
                # A class reference or an interface value: the first
                # non-``nil`` one.
                left = self._expr_to_go(left_node)
                fallback = self._typed_value_to_go(fallback_node, result_type)
                return f"func() {result_type} {{ if __lamL := {left}; __lamL != nil {{ return __lamL }}; return {fallback} }}()"
            #
            # Untyped operands: each is boxed as ``interface{}`` before the
            # comparison so Go doesn't complain about untyped-nil or
            # primitive-to-nil comparisons (``"s" != nil`` and
            # ``nil != nil`` are both compile errors otherwise).
            parts = [self._expr_to_go(c) for c in operands]
            expr = parts[-1]
            for operand, left in reversed(list(zip(operands[:-1], parts[:-1]))):
                hint = self._expr_go_type_hint(operand) or ""
                if hint.startswith("*"):
                    # A typed nil pointer boxed in ``interface{}`` is not
                    # ``nil``: test the pointer itself.
                    value = "__lamP"
                    expr = (
                        f"func() interface{{}} {{ if __lamP := {left}; __lamP != nil {{ return {value} }}; "
                        f"return {expr} }}()"
                    )
                    continue
                expr = (
                    f"func() interface{{}} {{ _v := interface{{}}({left}); "
                    f"if _v != nil {{ return _v }}; return {expr} }}()"
                )
            return expr

        if d == "getitem":
            inner = node.children[1]
            if self._expr_go_type_hint(node.children[0]) == "LamJSON":
                return f"lamJSONGet({self._expr_to_go(node.children[0])}, {self._expr_to_go(inner)})"
            class_name = self._infer_expr_class(node.children[0])
            method = self._get_dunder_method(class_name, "__getitem__") if class_name else ""
            if method and not (isinstance(inner, Tree) and inner.data == "slice"):
                method_key = f"{class_name}.__getitem__"
                param_types = self._func_param_go_types.get(method_key, [])
                index_type = param_types[0] if param_types else "interface{}"
                index = self._typed_value_to_go(inner, index_type)
                return f"{self._expr_to_go(node.children[0])}.{method}({index})"
            if isinstance(inner, Tree) and inner.data == "slice":
                obj_go = self._expr_to_go(node.children[0])
                return self._slice_to_go(obj_go, inner, node.children[0])
            literal_type = self._list_literal_index_type(node.children[0])
            if literal_type:
                # ``[obj][0].name``: index the typed slice, so the element
                # keeps its class (not ``[]interface{}{obj}[0]``).
                obj = self._typed_value_to_go(node.children[0], literal_type)
            else:
                obj = self._expr_to_go(node.children[0])
            negative = self._negative_int_constant(inner)
            receiver_type = literal_type or self._expr_go_type_hint(node.children[0]) or ""
            if negative is not None and not receiver_type.startswith("map[") and receiver_type not in {"interface{}"}:
                # ``xs[-1]``: Python semantics, counted from the end (an
                # index still out of range raises ``IndexError``).
                if re.fullmatch(r"[A-Za-z_][\w.]*", obj):
                    return f"{obj}[len({obj})-{negative}]"
                if receiver_type == "string" or receiver_type.startswith("[]"):
                    elem = "byte" if receiver_type == "string" else self._slice_elem_go_type(receiver_type)
                    return f"func(_s {receiver_type}) {elem} {{ return _s[len(_s)-{negative}] }}({obj})"
            if (
                (receiver_type == "string" or receiver_type.startswith("[]"))
                and negative is None
                and not self._nonneg_int_literal(inner)
                and not index_nonneg(inner, self._index_bounds, self._nonneg_shadowed)
                and not self._index_is_last_of(inner, node.children[0])
            ):
                # ``xs[i]`` with a runtime ``i``: Python semantics.
                return self._dynamic_index_to_go(obj, node.children[0], inner, receiver_type)
            idx = self._expr_to_go(inner)
            return f"{obj}[{idx}]"

        if d == "slice":
            # Bare ``slice`` (no enclosing ``getitem``) shouldn't appear
            # in valid programs, but emit something parseable rather
            # than crashing.
            parts = []
            for c in node.children:
                if c is None:
                    parts.append("")
                elif isinstance(c, Tree) and c.data == "sliceop":
                    step_val = ""
                    for sc in c.children:
                        if sc is not None:
                            step_val = self._expr_to_go(sc)
                    parts.append(step_val)
                else:
                    parts.append(self._expr_to_go(c))
            while len(parts) > 2 and parts[-1] == "":
                parts.pop()
            return ":".join(parts)

        if d == "arith_expr" or d == "term":
            concat = self._markup_concat_to_go(node)
            if concat is not None:
                return concat
            overloaded = self._try_operator_overload(node)
            if overloaded:
                return overloaded
            if d == "arith_expr":
                difference = self._set_chain_to_go(node, "-")
                if difference:
                    return difference
            repeated = self._string_repeat_to_go(node)
            if repeated:
                return repeated
            return self._binop_to_go(node)

        if d == "and_expr":
            intersection = self._set_chain_to_go(node, "&")
            if intersection:
                return intersection
            return self._bitwise_chain_to_go(node, "&")

        if d == "or_expr":
            union = self._set_chain_to_go(node, "|")
            if union:
                return union
            return self._bitwise_chain_to_go(node, "|")

        if d == "xor_expr":
            return self._bitwise_chain_to_go(node, "^")

        if d == "shift_expr":
            return self._binop_to_go(node)

        if d == "factor":
            if len(node.children) == 2:
                op = str(node.children[0])
                operand_node = node.children[1]
                if op == "-":
                    cls = self._infer_expr_class(operand_node)
                    method = self._get_dunder_method(cls, "__neg__") if cls else ""
                    if method:
                        return f"{self._expr_to_go(operand_node)}.{method}()"
                if op == "~":
                    cls = self._infer_expr_class(operand_node)
                    method = self._get_dunder_method(cls, "__invert__") if cls else ""
                    if method:
                        return f"{self._expr_to_go(operand_node)}.{method}()"
                    op = "^"
                # ``-(b + c)``: the group keeps its parentheses.
                operand = go_operand(self._expr_to_go(operand_node), _GO_UNARY_PREC)
                return f"({op}{operand})"
            return self._expr_to_go(node.children[0])

        if d == "power":
            base_node = node.children[0]
            if len(node.children) > 1 and node.children[1] is not None:
                cls = self._infer_expr_class(base_node)
                method = self._get_dunder_method(cls, "__pow__") if cls else ""
                if method:
                    left = self._expr_to_go(base_node)
                    right = self._expr_to_go(node.children[1])
                    return f"{left}.{method}({right})"
                base = self._expr_to_go(base_node)
                exp = self._expr_to_go(node.children[1])
                if self._int_power_operands(node):
                    # ``int ** int`` is an int, exact (``lamIPow`` wraps on
                    # overflow like every int operation and raises
                    # ``ValueError`` for a negative exponent); two literals
                    # fold (the checker rejects an overflowing constant,
                    # ``compiler/semantic_consts.py``).
                    try:
                        base_val, exp_val = int(base, 0), int(exp, 0)
                        if 0 <= exp_val <= 4096:
                            value = base_val ** exp_val
                            if -(1 << 63) <= value < (1 << 63):
                                return str(value)
                    except ValueError:
                        pass
                    return f"lamIPow({base}, {exp})"
                self._need_import("math")
                return f"math.Pow(float64({base}), float64({exp}))"
            return base

        if d == "comparison":
            overloaded = self._try_comparison_overload(node)
            if overloaded:
                return overloaded
            return self._comparison_to_go(node)

        if d == "not_test":
            return f"!({self._expr_to_go(node.children[0])})"

        if d in ("or_test", "and_test"):
            hoisted = self._short_circuit_propagate_to_go(node)
            if hoisted is not None:
                return hoisted
            narrowed = self._nr_binary_to_go(node)
            if narrowed is not None:
                return narrowed
            op = "&&" if d == "and_test" else "||"
            parts = []
            for c in node.children:
                if not isinstance(c, Tree):
                    continue
                part = self._expr_to_go(c)
                # ``a and (b or c)``: Go's ``||`` binds looser than ``&&``, so
                # an ``or`` (or a conditional/coalescing expression) inside
                # an ``and`` keeps its parentheses.
                if d == "and_test" and c.data in ("or_test", "test", "null_coalesce"):
                    part = f"({part})"
                else:
                    part = go_operand(part, _GO_BINARY_PREC[op], right=bool(parts), associative=True)
                parts.append(part)
            return f" {op} ".join(parts)

        if d == "tuple":
            elems = [self._expr_to_go(c) for c in node.children if c is not None]
            return ", ".join(elems)

        if d == "list":
            literal = self._infer_type_from_value(node) if node.children else ""
            if literal.startswith("[]") and "interface{}" not in literal and self._uniform_literal_items(node):
                # ``[1, 2][0:1]`` / ``[3, 4] + xs``: a literal whose items all
                # have one type is a list of that type, also where no
                # annotation gives it one.
                return self._typed_value_to_go(node, literal)
            elems = [self._expr_to_go(c) for c in node.children if c is not None]
            return f"[]interface{{}}{{{', '.join(elems)}}}"

        if d == "list_comprehension":
            # ``[w.toUpper() for w in words][1:]``: typed by its element
            # (``[]string``) even outside a typed context.
            elem = self._list_comp_static_elem_go_type(node)
            return self._list_comp_to_go(node, elem_type=f"[]{elem}" if elem else None)

        if d == "dict":
            return self._dict_to_go(node)

        if d == "dict_comprehension":
            # ``{k: v for ...}["x"]``, ``for k, v in {...}``: a typed map when
            # its key and value types are known (``interface{}`` values
            # would not convert to the int the context expects).
            inferred = self._dict_comp_static_go_type(node)
            return self._dict_comp_to_go(node, map_type=inferred or None)

        if d == "set":
            # Build the set inside an IIFE so duplicate literals
            # don't trip Go's "duplicate key in map literal" check
            # — sets are de-duplicating by definition.
            elems = [self._expr_to_go(c) for c in node.children if c is not None]
            if not elems:
                return "map[interface{}]bool{}"
            # ``for x in {n, 3}``: uniformly typed items give a typed set.
            set_type = self._infer_type_from_value(node) or ""
            if not (set_type.startswith("map[") and "interface{}" not in set_type and self._uniform_literal_items(node)):
                set_type = "map[interface{}]bool"
            lines = [f"func() {set_type} {{",
                     f"\t_s := {set_type}{{}}"]
            for e in elems:
                lines.append(f"\t_s[{e}] = true")
            lines.append("\treturn _s")
            lines.append("}()")
            return self._pin_multiline_to(node, "\n".join(lines))

        if d == "set_comprehension":
            # ``for x in {f(y) for y in ys}``: typed by its element, like a
            # list comprehension, also outside a typed context.
            elem = self._list_comp_static_elem_go_type(node)
            if elem in ("int", "string", "float64", "bool"):
                return self._set_comp_to_go(node, f"map[{elem}]bool")
            return self._set_comp_to_go(node)

        if d == "tuple_comprehension":
            return self._list_comp_to_go(node)

        if d == "star_expr":
            return self._expr_to_go(node.children[0]) + "..."

        if d == "kwargs":
            return self._expr_to_go(node.children[0])

        if d == "testlist_tuple":
            return ", ".join(self._expr_to_go(c) for c in node.children if c is not None)

        if d == "test":
            if len(node.children) == 3:
                hoisted = self._ternary_propagate_to_go(node)
                if hoisted is not None:
                    return hoisted
                common = self._ternary_common_go_type(node)
                narrowed = self._nr_ternary_to_go(
                    node, common or "interface{}",
                    (lambda b: self._typed_value_to_go(b, common)) if common else self._expr_to_go,
                )
                if narrowed is not None:
                    return narrowed
                cond = self._expr_to_go(node.children[1])
                if common:
                    true_val = self._typed_value_to_go(node.children[0], common)
                    false_val = self._typed_value_to_go(node.children[2], common)
                    return f"func() {common} {{ if {cond} {{ return {true_val} }}; return {false_val} }}()"
                true_val = self._expr_to_go(node.children[0])
                false_val = self._expr_to_go(node.children[2])
                return f"func() interface{{}} {{ if {cond} {{ return {true_val} }}; return {false_val} }}()"
            return self._expr_to_go(node.children[0])

        if d == "assign_expr":
            name = self._get_name(node.children[0])
            val = self._expr_to_go(node.children[1])
            return f"func() interface{{}} {{ {name} := {val}; return {name} }}()"

        if d == "lambdef":
            return self._lambda_to_go(node)

        if d == "yield_expr":
            # A ``yield`` statement is ``if !__lamYield(v) { return }``
            # (``_visit_yield_stmt``); as a value, the call reports whether
            # the consumer wants more.
            if self._in_generator and self._generator_chan:
                return f"{self._generator_chan}({self._yield_value_go(node)})"
            return "/* yield not supported */"
        if d == "yield_from":
            return "/* yield from is a statement */"

        if d == "await_call":
            # The handle yields a thunk returning the result or re-raising
            # the async body's exception (``_emit_async_callable``).
            if self._expr_go_type_hint(node.children[0]) == "interface{}":
                from compiler.constants import await_any_message, lam_simple_expr_text
                self._report_lam_error(node, "type", await_any_message(lam_simple_expr_text(node.children[0])))
            return f"(<-{self._expr_to_go(node.children[0])})()"

        if d == "type_expr":
            return self._type_expr_to_go(node)

        # Fallback
        parts = [self._expr_to_go(c) for c in node.children if c is not None]
        return " ".join(parts)

    # ─── Function calls ───────────────────────────────────────

    def _args_to_go(self, args: List[str]) -> str:
        return ", ".join(args)

    def _is_private_method_name(self, method_name: str) -> bool:
        return any(method_name in methods for methods in self._private_methods.values())

    def _unshadowed_function(self, go_name: str, func_name: str) -> str:
        """``go_name`` of a package-level function, or a package-level alias
        of it when a type parameter in scope has the same Go name: ``t`` is
        ``T`` in Go, which inside ``func f[T]`` / ``class Box[T]`` names the
        type parameter (``T("x")`` is a conversion, ``T`` as a value "is not
        an expression"). The alias (``var lamFuncRef_T_<module> = T``) is
        declared at package level, where ``T`` is the function. A generic
        function cannot be aliased without instantiation and is left as is
        (the checker reports that case)."""
        if go_name not in self._generic_names:
            return go_name
        if self._func_generic_params.get(func_name) or self._func_generic_order.get(func_name):
            return go_name
        alias = self._shadowed_function_aliases.get(go_name)
        if alias is None:
            module = self._module_init_name.removeprefix("lamModuleInit_")
            alias = f"lamFuncRef_{go_name}_{module}"
            self._shadowed_function_aliases[go_name] = alias
        return alias

    def _go_user_function_name(self, func_name: str) -> str:
        if func_name in self._function_go_names:
            return self._function_go_names[func_name]
        if func_name in self._private_functions:
            return self._go_private_name(func_name)
        if func_name != "main":
            return self._go_public_name(func_name)
        return func_name

    def _is_user_function_reference(self, func_expr: str) -> bool:
        if func_expr in self._user_functions:
            return True
        return any(
            self._go_user_function_name(name) == func_expr
            or self._go_private_name(name) == func_expr
            for name in self._user_functions
        )

    @staticmethod
    def _contains_propagate(node) -> bool:
        if not isinstance(node, Tree):
            return False
        if node.data == "propagate":
            return True
        return any(ExpressionVisitorMixin._contains_propagate(child) for child in node.children)

    def _user_function_call_to_go(
        self, func_name: str, args: List[str], kwargs, args_node,
        type_args: Optional[List[str]] = None,
    ) -> str:
        go_name = self._go_user_function_name(func_name)
        call_key = func_name
        if (len(self._overloaded_functions.get(func_name, set())) > 1
                or len(self._overload_variants.get(func_name, [])) > 1):
            call_sig = self._infer_call_arg_sig(args_node)
            variant = self._overload_variant_for_sig(func_name, call_sig)
            go_name = f"{go_name}{self._overload_suffix_for_sig(func_name, call_sig)}"
            if variant is not None:
                call_key = str(variant.get("meta_key") or func_name)
        if type_args is None and args_node is not None and self._func_generic_params.get(call_key):
            call_args = self._planned_call_args_inferred(call_key, args_node)
        elif type_args is not None and self._func_generic_order.get(call_key):
            # ``firstOf[str]()``: the explicit type arguments type the
            # arguments and the defaults (``xs: list[T] = []`` is ``[]string{}``).
            order = self._func_generic_order[call_key]
            subst = {name: str(arg) for name, arg in zip(order, type_args)}
            with self._scoped(_call_type_subst={**self._call_type_subst, **subst}):
                call_args = self._planned_call_args(call_key, args_node, args, kwargs)
        else:
            call_args = self._planned_call_args(call_key, args_node, args, kwargs)
        if type_args is not None:
            return f"{go_name}[{self._args_to_go(type_args)}]({self._args_to_go(call_args)})"
        go_name = self._unshadowed_function(go_name, call_key)
        explicit = self._uninferable_type_args(call_key, args_node)
        return f"{go_name}{explicit}({self._args_to_go(call_args)})"

    def _planned_call_args(
        self,
        func_key: str,
        args_node,
        fallback_args: Optional[List[str]] = None,
        fallback_kwargs: Optional[dict] = None,
    ) -> List[str]:
        """Lower call args exactly once using metadata for ``func_key``.

        Call sites with known parameter metadata should use this helper so
        contextual typing, keyword reordering, and default filling stay in one
        path. ``fallback_*`` is only used for genuinely untyped calls or for
        already-lowered generic/nested call paths.
        """
        if args_node is not None:
            args, kwargs = self._collect_call_args_for_func(func_key, args_node)
        else:
            args = fallback_args or []
            kwargs = fallback_kwargs or {}
        call_args = self._apply_call_kwargs(func_key, args, kwargs)
        return self._fill_default_args(func_key, call_args)

    def _known_static_method_call_to_go(
        self, class_name: str, method_name: str, args_node,
    ) -> Optional[str]:
        if (
            class_name not in self._class_names
            or method_name not in self._static_methods.get(class_name, set())
        ):
            return None
        method_key = f"{class_name}.{method_name}"
        suffix = ""
        call_key = method_key
        if len(self._overload_variants.get(method_key, [])) > 1:
            call_sig = self._infer_call_arg_sig(args_node)
            variant = self._overload_variant_for_sig(method_key, call_sig)
            suffix = self._overload_suffix_for_sig(method_key, call_sig)
            if variant is not None:
                call_key = str(variant.get("meta_key") or method_key)
        go_name = f"{self._go_static_base(class_name, method_name)}{suffix}"
        if method_name in self._private_methods.get(class_name, set()):
            go_name = self._go_private_name(go_name)
        call_args = self._planned_call_args_inferred(call_key, args_node)
        explicit = self._static_generic_fallback_type_args(class_name, call_key, args_node)
        if not explicit and not self._class_type_param_names_of(class_name):
            # ``Tools.swap(pairs)`` with ``swap[K, V](pairs: list[tuple[K, V]])``.
            explicit = self._uninferable_type_args(
                call_key, args_node, self._method_generic_order.get(method_key, []),
            )
        return f"{go_name}{explicit}({', '.join(call_args)})"

    def _static_generic_fallback_type_args(self, class_name: str, call_key: str, args_node) -> str:
        """``[interface{}]`` for ``Iter.repeat(None, 2)``: a static method of
        a generic class whose type parameter no argument determines (Go
        cannot infer it from ``nil``) gets explicit type arguments."""
        names = self._func_generic_params.get(call_key, set())
        class_params = self._class_type_param_names_of(class_name)
        if not names or not class_params or not names <= set(class_params):
            return ""
        signature = " ".join(self._func_param_go_types.get(call_key, []) + [self._generic_method_returns.get(call_key, "")])
        used = [p for p in class_params if mentions_names(signature, [p])]
        if not used:
            return ""
        subst = self._generic_call_subst(call_key, args_node, used)
        if all(p in subst for p in used):
            return ""
        return "[" + ", ".join(subst.get(p, "interface{}") for p in used) + "]"

    def _planned_call_args_inferred(self, call_key: str, args_node) -> List[str]:
        """``_planned_call_args`` for a generic callable: lambdas and other
        arguments are lowered with the type parameters inferred at this
        call (``Sort.sortWith(users, lambda a, b: ...)`` types ``a``/``b``)."""
        generic_names = sorted(self._func_generic_params.get(call_key, set()))
        if not generic_names or args_node is None:
            return self._planned_call_args(call_key, args_node)
        subst = self._generic_call_subst(call_key, args_node, generic_names)
        subst = {k: v for k, v in subst.items() if k not in self._generic_names or v != k}
        if not subst:
            return self._planned_call_args(call_key, args_node)
        with self._scoped(_call_type_subst=subst):
            return self._planned_call_args(call_key, args_node)

    def _receiver_class_for_call(self, raw_obj: str, func) -> str:
        obj_class = self._var_types.get(raw_obj, "")
        if (
            not obj_class
            and self.current_class
            and self._self_replacement
            and raw_obj == self._self_replacement
        ):
            return self.current_class
        if not obj_class and isinstance(func, Tree) and func.data == "getattr":
            obj_class = self._infer_receiver_class(func.children[0])
        if not obj_class and self.current_class and self._self_replacement:
            for field_name, field_type in self.class_fields.get(self.current_class, []):
                go_field = self._go_field_name(self.current_class, field_name)
                if raw_obj == f"{self._self_replacement}.{go_field}":
                    candidate = field_type.removeprefix("*").split("[", 1)[0]
                    if candidate in self._class_names:
                        return candidate
        if not obj_class and self._interfaces and isinstance(func, Tree) and func.data == "getattr":
            # A receiver typed by an interface (a loop / comprehension
            # variable, a field, a lambda parameter): calls use the
            # interface's declared signature (defaults, keywords).
            hint = self._expr_go_type_hint(func.children[0]) or ""
            for iface in self._interfaces:
                if hint and hint == self._go_class_name(iface):
                    return iface
        return obj_class

    def _inherited_method_param_key(self, class_name: str, method_name: str) -> str:
        """``"Base.method"`` of the nearest non-generic ancestor defining
        ``method_name`` with known parameter types (``""`` when none)."""
        for ancestor in self._hier_ancestors(class_name):
            key = f"{ancestor}.{method_name}"
            if key in self._func_param_go_types:
                if self._class_type_param_names_of(ancestor) or len(self._overload_variants.get(key, [])) > 1:
                    return ""
                return key
        return ""

    def _known_instance_method_call_to_go(
        self, func_str: str, raw_obj: str, method_name: str, func, args_node,
    ) -> Optional[str]:
        obj_class = self._receiver_class_for_call(raw_obj, func)
        if not obj_class:
            return None
        generic_call = self._generic_method_call_to_go(
            obj_class, method_name, raw_obj,
            func.children[0] if isinstance(func, Tree) and func.children else None, args_node,
        )
        if generic_call is not None:
            return generic_call
        method_key = f"{obj_class}.{method_name}"
        suffix = ""
        call_key = method_key
        if len(self._overload_variants.get(method_key, [])) > 1:
            call_sig = self._infer_call_arg_sig(args_node)
            variant = self._overload_variant_for_sig(method_key, call_sig)
            suffix = self._overload_suffix_for_sig(method_key, call_sig)
            if variant is not None:
                call_key = str(variant.get("meta_key") or method_key)
        if call_key not in self._func_param_go_types and len(self._overload_variants.get(method_key, [])) <= 1:
            # A method inherited from a base (of another module): its
            # parameters type the arguments (``s.need(["a"])`` passes a
            # ``[]string``).
            call_key = self._inherited_method_param_key(obj_class, method_name) or call_key
        subst = self._generic_receiver_subst(obj_class, func)
        generic_names = sorted(self._func_generic_params.get(call_key, set()) or self._func_generic_params.get(method_key, set()))
        if generic_names:
            subst = self._complete_generic_subst(
                self._generic_call_subst(call_key, args_node, generic_names, subst), [],
            )
        if subst:
            with self._scoped(_call_type_subst=subst):
                call_args = self._planned_call_args(call_key, args_node)
        else:
            call_args = self._planned_call_args(call_key, args_node)
        if call_key not in self._func_param_go_types and isinstance(func, Tree):
            # ``self.rate(book)`` with a field ``rate: func(Item) -> float``:
            # its arguments convert like a function local's (upcasts,
            # typed literals, int to float).
            field_go_type = self._expr_go_type_hint(func) or ""
            if field_go_type.startswith("func("):
                call_args = self._func_typed_call_args(field_go_type, call_args, {}, args_node)
        if not suffix and self._hier_needs_virtual(obj_class, method_name):
            # A descendant of the receiver's static class overrides the
            # method: dispatch to the most-derived object (see
            # ``_virtual_method_plan``), for ``self`` and any receiver.
            head, dot, go_method = func_str.rpartition(".")
            if dot and head:
                return f"{head}._lamVirt_{go_method}({', '.join(call_args)})"
        return f"{func_str}{suffix}({', '.join(call_args)})"

    def _nested_generic_call_to_go(
        self, func_name: str, args: List[str],
        explicit_type_args: Optional[List[str]] = None,
    ) -> str:
        info = self._nested_generic_functions[func_name]
        hidden = [name for name, _ in info.get("captures", [])]
        type_args = list(info.get("outer_type_names", []))
        if explicit_type_args:
            type_args.extend(explicit_type_args)
        call_args = hidden + args
        if type_args:
            return f"{info['go_name']}[{self._args_to_go(type_args)}]({self._args_to_go(call_args)})"
        return f"{info['go_name']}({self._args_to_go(call_args)})"

    def _generic_getitem_call_to_go(
        self, func, args: Optional[List[str]], kwargs, args_node,
    ) -> Optional[str]:
        if not (
            isinstance(func, Tree)
            and func.data == "getitem"
            and isinstance(func.children[0], Tree)
            and func.children[0].data == "var"
        ):
            return None
        base_name = self._get_name(func.children[0].children[0])
        if base_name in self._nested_generic_functions:
            type_args = self._type_arg_list(func.children[1])
            nested_key = self._nested_func_keys.get(base_name)
            if nested_key and args_node is not None:
                # ``pick[str](["a"], 0)``: the explicit type arguments type
                # the arguments, defaults and keywords.
                own = self._nested_generic_functions[base_name].get("type_param_names") or []
                subst = {name: str(arg) for name, arg in zip(own, type_args) if arg and str(arg) != name}
                with self._scoped(_call_type_subst={**self._call_type_subst, **subst}):
                    args = self._planned_call_args(nested_key, args_node)
            elif args is None:
                args, kwargs = self._collect_call_args(args_node)
            return self._nested_generic_call_to_go(base_name, args, type_args)
        if base_name in self._class_names and base_name in self._generic_classes:
            type_args = self._type_arg_list(func.children[1])
            init_key = f"{base_name}.init"
            # ``Queue[str](lambda items: len(items))``: the class's type
            # parameters take the explicit type arguments in the parameter
            # types (``func([]T) int`` -> ``func([]string) int``).
            # Imported generic classes too (``Box[Row]([Row(s) for s in
            # xs], n)`` with ``class Box[T]`` in another module: the
            # comprehension lowers to ``[]*Row``, not ``[]T``).
            params = self._class_type_param_names_of(base_name)
            subst = {
                name: arg for name, arg in zip(params, type_args)
                if arg and arg != name and name not in self._generic_names
            }
            with self._scoped(_call_type_subst={**self._call_type_subst, **subst}):
                call_args = self._planned_call_args(init_key, args_node)
            return f"{self._go_ctor_name(base_name)}[{self._args_to_go(type_args)}]({self._args_to_go(call_args)})"
        if base_name in self._user_functions:
            type_args = self._type_arg_list(func.children[1])
            return self._user_function_call_to_go(
                base_name, args or [], kwargs or {}, args_node, type_args=type_args,
            )
        return None

    def _check_str_receiver_method(self, func: Tree, args_node) -> None:
        """``label.substring(0, 2)`` on a Go ``string`` would lower to
        ``label.Substring(...)``, which Go rejects. The semantic checker
        reports it for the compiled file; this reports it for imported
        modules (and receivers the checker cannot type) with the same
        message."""
        from compiler.constants import STR_METHODS, lam_simple_expr_text, unknown_str_method_message

        method = self._get_name(func.children[1])
        if not method:
            return
        receiver = func.children[0]
        if not isinstance(receiver, Tree):
            return
        if (
            receiver.data == "getitem" and len(receiver.children) == 2
            and not (isinstance(receiver.children[1], Tree) and receiver.children[1].data == "slice")
            and self._expr_go_type_hint(receiver.children[0]) == "string"
        ):
            # ``name[0].toUpper()``: indexing a ``str`` gives a ``byte``.
            from compiler.constants import str_index_byte_hint
            self._report_lam_error(
                func, "member",
                f"`byte` has no method `{method}`"
                + str_index_byte_hint(
                    lam_simple_expr_text(receiver), method, bool(self._get_raw_call_args(args_node)),
                ),
            )
            return
        if receiver.data not in {"string", "fstring", "string_concat"} and self._expr_go_type_hint(receiver) != "string":
            return
        if method in STR_METHODS:
            from compiler.constants import str_method_arity_message
            arity = str_method_arity_message(method, len(self._get_raw_call_args(args_node)))
            if arity:
                self._report_lam_error(func, "call", arity)
            return
        arg_texts = [lam_simple_expr_text(arg) for arg in self._get_raw_call_args(args_node)]
        self._report_lam_error(
            func, "member",
            unknown_str_method_message(method, lam_simple_expr_text(receiver), arg_texts),
        )

    _COLLECTION_LITERAL_NODES = frozenset({
        "list", "dict", "set", "list_comprehension", "dict_comprehension", "set_comprehension",
    })

    def _func_value_call_args(self, name: str, args: List[str], kwargs, args_node) -> List[str]:
        """Arguments of a call to a function value (a typed lambda, a
        ``func(...)`` parameter or local): an inline list / dict / set
        literal takes the declared parameter type (``size(["a"])`` with
        ``size: func(list[str]) -> int`` passes a ``[]string``)."""
        return self._func_typed_call_args(self._var_go_types.get(name, "") or "", args, kwargs, args_node)

    def _func_typed_call_args(self, go_type: str, args: List[str], kwargs, args_node) -> List[str]:
        """``_func_value_call_args`` for a function value of Go type
        ``go_type`` (a local, a parameter or a ``func(...)`` field)."""
        split = self._split_go_func_type(go_type)
        raw_args = self._get_raw_call_args(args_node)
        if split is None or kwargs or len(raw_args) != len(args) or len(split[0]) != len(args):
            return args
        out = list(args)
        for index, (raw, param_type) in enumerate(zip(raw_args, split[0])):
            if (
                isinstance(raw, Tree) and raw.data in self._COLLECTION_LITERAL_NODES
                and param_type and not param_type.startswith("...") and param_type != "interface{}"
                and not self._contains_propagate(raw)
            ):
                out[index] = self._typed_value_to_go(raw, param_type)
            elif (
                param_type in {"float64", "float32"} and isinstance(raw, Tree)
                and self._numeric_literal_value(raw) is None
                and (self._expr_go_type_hint(raw) or "") in _GO_INT_TYPES
            ):
                # ``g(n)`` with ``g: func(float) -> float`` and ``n: int``.
                out[index] = f"{param_type}({args[index]})"
            elif param_type.startswith("*") and isinstance(raw, Tree):
                # ``rate(book)`` with ``rate: func(Item) -> float`` and
                # ``book: Featured`` (``class Featured(Item)``): the same
                # upcast as an argument of a declared function.
                upcast = self._hier_upcast_to_go(args[index], self._hier_expr_go_type(raw), param_type)
                if upcast:
                    out[index] = upcast
        return out

    def _funccall_to_go(self, node: Tree) -> str:
        # A call nested in another call's callee (``make(inc).get()``) must
        # lower its own arguments outside the outer callee context, so bare
        # function references are mangled.
        if self._in_funccall_head:
            with self._scoped(_in_funccall_head=False):
                return self._funccall_to_go_inner(node)
        return self._funccall_to_go_inner(node)

    def _funccall_to_go_inner(self, node: Tree) -> str:
        markup = self._markup_call_to_go(node)
        if markup is not None:
            return markup
        func = node.children[0]
        args_node = node.children[1] if len(node.children) > 1 else None
        args: Optional[List[str]] = None
        kwargs = None

        def plain_call_args():
            nonlocal args, kwargs
            if args is None or kwargs is None:
                args, kwargs = self._collect_call_args(args_node)
            return args, kwargs

        # ``Pair[int, str](...)`` — generic-constructor call on a user
        # class. The parser produces ``funccall(getitem(Pair, <types>),
        # args)`` and the naive lowering would dump the subscript into
        # the callee expression, losing commas and producing invalid Go.
        # Intercept that shape here and emit ``NewPair[int, string](...)``
        # directly.
        generic_call = self._generic_getitem_call_to_go(func, args, kwargs, args_node)
        if generic_call is not None:
            return generic_call

        if isinstance(func, Tree) and func.data == "getitem":
            # ``handlers[0](x)`` / ``table["inc"](x)``: call the function
            # value stored in a collection element, shaping the arguments
            # with its parameter types (``fns[0]("a", [Item(1)])``).
            split = self._split_go_func_type(self._expr_go_type_hint(func) or "")
            raw_args = self._get_raw_call_args(args_node)
            if split is not None and len(split[0]) == len(raw_args) and all(
                not (isinstance(a, Tree) and a.data in {"argvalue", "starargs", "kwargs"}) for a in raw_args
            ):
                args = [self._transpile_argvalue_with_type(a, t) for a, t in zip(raw_args, split[0])]
            else:
                args, kwargs = plain_call_args()
            return f"{self._expr_to_go(func)}({', '.join(args)})"

        if isinstance(func, Tree) and func.data == "funccall":
            # ``maker()(x)``: call the function value a call returned,
            # shaping the arguments with its parameter types.
            callee_go = self._expr_to_go(func)
            split = self._split_go_func_type(self._expr_go_type_hint(func) or "")
            raw_args = self._get_raw_call_args(args_node)
            if split is not None and len(split[0]) == len(raw_args):
                lowered = [self._transpile_argvalue_with_type(a, t) for a, t in zip(raw_args, split[0])]
            else:
                lowered, _kw = plain_call_args()
            return f"{callee_go}({', '.join(lowered)})"

        raw_method = None
        raw_obj = None
        if isinstance(func, Tree) and func.data == "getattr":
            raw_method = self._get_name(func.children[1])
            raw_obj = self._expr_to_go(func.children[0])
            markup_method = self._markup_method_to_go(func, args_node, raw_obj)
            if markup_method is not None:
                return markup_method
            self._check_str_receiver_method(func, args_node)
            _alias, parent_base, parent_receiver = self._parent_alias_call_info(func)
            if parent_base and raw_method:
                method_key = f"{parent_base}.init" if raw_method in {"init", "__init__"} else f"{parent_base}.{raw_method}"
                call_args = self._planned_call_args(method_key, args_node)
                if raw_method in {"init", "__init__"}:
                    # The fresh parent (and its ancestors) must dispatch
                    # ``self.m()`` back to this object, not to itself.
                    binds = self._virtual_bind_stmts(
                        parent_base, parent_receiver, self._self_replacement or "s",
                    ) if self._self_replacement else []
                    type_args = self._base_type_args(self.current_class, parent_base) if self.current_class else ""
                    defaults = self._hier_parent_field_default_stmts(
                        self.current_class, parent_base, self._self_replacement,
                    ) if self.current_class and self._self_replacement else []
                    return "; ".join(
                        [f"{parent_receiver} = {self._go_ctor_name(parent_base)}{type_args}({', '.join(call_args)})"] + binds + defaults
                    )
            if raw_obj == "LAMMERGEIER" and raw_method:
                if raw_method in self._user_functions:
                    return self._user_function_call_to_go(raw_method, [], {}, args_node)
                if raw_method in self._class_names:
                    call_args = self._planned_call_args(f"{raw_method}.init", args_node)
                    return f"{self._go_ctor_name(raw_method)}({', '.join(call_args)})"
            if raw_obj and raw_obj.startswith("LAMMERGEIER.") and raw_method:
                class_name = raw_obj.split(".", 1)[1]
                static_call = self._known_static_method_call_to_go(class_name, raw_method, args_node)
                if static_call is not None:
                    return static_call

        # Resolve the callee expression without letting bare identifiers
        # be rewritten to their Go public name (that's handled here).
        reuse = self.__dict__.setdefault("_reuse_lowering", {})
        receiver_node = func.children[0] if isinstance(func, Tree) and func.data == "getattr" else None
        reused = (
            raw_obj is not None and isinstance(receiver_node, Tree)
            and receiver_node.data in {"funccall", "propagate"}
        )
        if reused:
            # ``a.f().g()``: the receiver call was lowered for ``raw_obj``;
            # lowering it again for the callee doubled the work per link.
            # ``a()?.g()``: it also emitted the ``?`` guard (and evaluated
            # ``a()``) a second time.
            reuse[id(receiver_node)] = (receiver_node, raw_obj)
        try:
            with self._scoped(_in_funccall_head=True):
                func_str = self._expr_to_go(func)
        finally:
            if reused:
                reuse.pop(id(receiver_node), None)

        if raw_obj and raw_method:
            # Static user-method calls know their parameter types; lower
            # arguments only on that typed path so ``?`` and other
            # emit-time expressions are not duplicated by speculative
            # untyped lowering.
            static_call = self._known_static_method_call_to_go(
                raw_obj, raw_method, args_node,
            )
            if static_call is not None:
                return static_call
            instance_call = self._known_instance_method_call_to_go(
                func_str, raw_obj, raw_method, func, args_node,
            )
            if instance_call is not None:
                return instance_call

        # User functions lower their arguments once, with parameter types
        # (``_planned_call_args``). Lowering them here as well would emit the
        # statements of a ``?`` inside an argument twice.
        if (
            func_str in self._user_functions
            and func_str not in self._nested_generic_functions
            and self._contains_propagate(args_node)
        ):
            return self._user_function_call_to_go(func_str, [], {}, args_node)

        # ── Built-in mappings ──
        args, kwargs = plain_call_args()
        if func_str == "print":
            self._need_import("fmt")
            raw_args = self._get_raw_call_args(args_node)
            if len(raw_args) == len(args):
                args = [self._display_go(a, r) for a, r in zip(args, raw_args)]
            return f'fmt.Println({", ".join(args)})'

        if func_str == "len":
            raw_args = self._get_raw_call_args(args_node)
            if raw_args and self._expr_go_type_hint(raw_args[0]) == "LamJSON":
                return f"lamJSONLen({args[0]})"
            return f"len({args[0]})" if args else "0"

        if func_str == "str":
            raw_args = self._get_raw_call_args(args_node)
            if args and raw_args and self._expr_go_type_hint(raw_args[0]) == "[]byte":
                # ``str(data)``: the UTF-8 text of the bytes.
                return f"string({args[0]})"
            if args and raw_args and self._expr_go_type_hint(raw_args[0]) in self._TEXT_VALUE_GO_TYPES:
                # ``str(page)`` of html / xml / json: the value's own text,
                # rendered once, not copied again by fmt (``lamValueText``,
                # lamjson, also right if the value is an ``interface{}``).
                return f"lamValueText({args[0]})"
            if args and len(raw_args) == 1:
                # ``str(name)`` of a declared str is the string; of a declared
                # int / bool ``strconv``'s text (the same as ``%v``); of a
                # value hinted str without a declared type ``lamSlotText``.
                certain = self._certain_go_type(raw_args[0])
                if certain == "string":
                    return args[0]
                if certain in {"int", "bool"}:
                    self._need_import("strconv")
                    return f"strconv.{'Itoa' if certain == 'int' else 'FormatBool'}({args[0]})"
                if certain not in {"string", "int", "bool"} and self._expr_go_type_hint(raw_args[0]) == "string":
                    return f"lamSlotText({args[0]})"
                args = [self._display_go(args[0], raw_args[0])] + list(args[1:])
            self._need_import("fmt")
            return f'fmt.Sprintf("%v", {args[0]})' if args else '""'

        if func_str == "bytes" and "bytes" not in self._user_functions:
            raw_args = self._get_raw_call_args(args_node)
            return self._bytes_conversion_to_go(raw_args[0] if raw_args else None, args[0] if args else "")

        if func_str == "json":
            raw_args = self._get_raw_call_args(args_node)
            return self._typed_value_to_go(raw_args[0], "LamJSON") if raw_args else "lamJSONMust(nil)"

        if func_str == "repr":
            self._need_import("fmt")
            return f'fmt.Sprintf("%#v", {args[0]})' if args else '""'

        if func_str in ("int", "float", "bool"):
            if not args:
                return {"int": "0", "float": "0.0", "bool": "false"}[func_str]
            raw_args = self._get_raw_call_args(args_node)
            return self._conversion_to_go(func_str, raw_args[0] if raw_args else None, args[0])

        if func_str in ("panic", "recover", "make", "new", "cap", "copy", "delete", "close") and func_str not in self._user_functions:
            return f"{func_str}({', '.join(args)})"

        if func_str in ("string", "byte", "rune", "int8", "int16", "int32", "int64",
                         "uint", "uint8", "uint16", "uint32", "uint64",
                         "float32", "float64"):
            return f"{func_str}({', '.join(args)})" if args else f"{func_str}(0)"

        if func_str in {"min", "max"} and args and self._raw_call_kwargs(args_node).get("key") is not None:
            lowered = self._minmax_key_to_go(func_str, args_node)
            if lowered:
                return lowered
        if func_str in {"abs", "min", "max"} and args:
            lowered = self._numeric_builtin_to_go(func_str, args, self._get_raw_call_args(args_node))
            if lowered:
                return lowered

        if func_str == "set" and "set" not in self._user_functions:
            # ``set()`` (empty, typed by the context) / ``set(iterable)``.
            expected = (getattr(self, "_expected_expr_go_type", "") or "").strip()
            raw_args = self._get_raw_call_args(args_node)
            set_type = expected if self._is_go_set_type(expected) else ""
            if not raw_args:
                return f"{set_type or 'map[interface{}]bool'}{{}}"
            source_type = self._expr_go_type_hint(raw_args[0]) or self._infer_type_from_value(raw_args[0]) or ""
            elem = self._slice_elem_go_type(source_type) if source_type.startswith("[]") else ""
            if self._is_go_set_type(source_type):
                elem = self._split_go_map_type(source_type)[0]
            if not set_type:
                set_type = f"map[{elem or 'interface{}'}]bool"
            if isinstance(raw_args[0], Tree) and raw_args[0].data == "list" and source_type.startswith("[]"):
                # ``set([3, 1, 2])``: the literal typed like its elements.
                source = self._typed_value_to_go(raw_args[0], source_type)
            else:
                source = self._expr_to_go(raw_args[0])
            loop = "for k := range src" if self._is_go_set_type(source_type) else "for _, k := range src"
            return (
                f"func(src {source_type or 'interface{}'}) {set_type} {{ out := {set_type}{{}}; "
                f"{loop} {{ out[k] = true }}; return out }}({source})"
            )

        if func_str == "sorted" and "sorted" not in self._user_functions:
            self._need_import("sort")
            raw_args = self._get_raw_call_args(args_node)
            if raw_args:
                raw_kwargs = self._raw_call_kwargs(args_node)
                expected_type = getattr(self, "_expected_expr_go_type", "") or ""
                arg_type = self._sorted_source_go_type(raw_args[0])
                map_key_type = self._sorted_map_key_go_type(arg_type)
                if map_key_type:
                    # ``sorted(s)`` of a set (``map[T]bool``) / ``sorted(d)``
                    # of a dict (``map[K]V``): a new sorted ``[]T`` of the keys.
                    # The map is the closure's argument: evaluated once.
                    slice_type = f"[]{map_key_type}"
                    elem_type = map_key_type
                    map_param = f"lamSrc {arg_type}"
                    map_arg = self._typed_value_to_go(raw_args[0], arg_type)
                    target_expr = f"make({slice_type}, 0, len(lamSrc))"
                    collect = "for k := range lamSrc { tmp = append(tmp, k) }; "
                else:
                    map_param = map_arg = ""
                    slice_type = expected_type if expected_type.startswith("[]") else arg_type
                    if not slice_type.startswith("[]"):
                        slice_type = "[]interface{}"
                    elem_type = self._slice_elem_go_type(slice_type) or "interface{}"
                    source = (
                        self._typed_value_to_go(raw_args[0], slice_type)
                        if slice_type != "[]interface{}" or isinstance(raw_args[0], Tree) and raw_args[0].data in {"list", "list_comprehension"}
                        else args[0]
                    )
                    if arg_type and arg_type != slice_type and arg_type.startswith("[]") and not (
                        isinstance(raw_args[0], Tree) and raw_args[0].data in {"list", "list_comprehension"}
                    ):
                        source = args[0]
                        slice_type = arg_type
                        elem_type = self._slice_elem_go_type(slice_type) or "interface{}"
                    target_expr = f"append({slice_type}{{}}, {source}...)"
                    collect = ""
                ordered = {
                    "int", "int8", "int16", "int32", "int64",
                    "uint", "uint8", "uint16", "uint32", "uint64",
                    "float32", "float64", "string",
                }
                key_node = raw_kwargs.get("key")
                reverse_node = raw_kwargs.get("reverse")
                left, right = "tmp[i]", "tmp[j]"
                prologue = ""
                key_type = elem_type
                if key_node is not None:
                    # ``sorted(xs, key=lambda s: len(s))``: compare the keys.
                    if self._is_lambda_node(key_node):
                        key_type = self._lambda_result_go_type(key_node, [elem_type]) or "interface{}"
                    else:
                        key_type = self._split_go_func_type(self._expr_go_type_hint(key_node) or "")
                        key_type = key_type[1] if key_type else ""
                        if not key_type and isinstance(key_node, Tree) and key_node.data == "var":
                            key_type = self._func_return_go_types.get(self._get_name(key_node.children[0]), "")
                        key_type = key_type or "interface{}"
                    key_fn = self._typed_value_to_go(key_node, f"func({elem_type}) {key_type}")
                    prologue = f"key := {key_fn}; "
                    left, right = "key(tmp[i])", "key(tmp[j])"
                if key_type in ordered:
                    less_expr = f"{left} < {right}"
                elif key_type == "bool":
                    less_expr = f"!{left} && {right}"
                elif self._orders_dynamically(key_type):
                    # ``list[any]`` / ``json`` / ``T``: by kind at run time.
                    prologue += f"lamCmp := {self._dynamic_compare_go()}; "
                    less_expr = f"lamCmp({left}, {right}) < 0"
                else:
                    self._need_import("fmt")
                    less_expr = f'fmt.Sprintf("%v", {left}) < fmt.Sprintf("%v", {right})'
                if reverse_node is not None:
                    reverse_go = self._typed_value_to_go(reverse_node, "bool")
                    if reverse_go == "true":
                        less_expr = less_expr.replace(left, "\x00L").replace(right, left).replace("\x00L", right)
                    elif reverse_go != "false":
                        swapped = less_expr.replace(left, "\x00L").replace(right, left).replace("\x00L", right)
                        less_expr = f"(({reverse_go}) && {swapped}) || (!({reverse_go}) && {less_expr})"
                return (
                    f"func({map_param}) {slice_type} {{ tmp := {target_expr}; {collect}{prologue}"
                    f"sort.SliceStable(tmp, func(i, j int) bool {{ return {less_expr} }}); return tmp }}({map_arg})"
                )

        if func_str == "reversed" and "reversed" not in self._user_functions and "reversed" not in self._var_go_types:
            raw_args = self._get_raw_call_args(args_node)
            if len(raw_args) == 1:
                return self._reversed_to_go(raw_args[0])

        if func_str == "enumerate":
            # In a for-head ``for i, v in enumerate(xs)`` the for-stmt
            # visitor rewrites this directly into a Go ``range`` loop.
            # A bare call needs to materialise the ``(i, v)`` pairs, so
            # we reflect over the iterable at runtime and emit a slice
            # where each element is a two-element ``[]interface{}``.
            if not args:
                return "[]interface{}{}"
            self._need_import("reflect")
            src = args[0]
            start_node = self._enumerate_start_node(node)
            start = self._typed_value_to_go(start_node, "int") if start_node is not None else "0"
            return (
                "func() []interface{} { "
                "v := reflect.ValueOf(" + src + "); "
                "if !v.IsValid() { return []interface{}{} }; "
                "start := " + start + "; "
                "out := make([]interface{}, v.Len()); "
                "for i := 0; i < v.Len(); i++ { "
                "out[i] = []interface{}{start + i, v.Index(i).Interface()} "
                "}; return out }()"
            )

        if func_str == "isinstance":
            raw = self._get_raw_call_args(args_node)
            if len(raw) >= 2:
                obj_go = self._expr_to_go(raw[0])
                static = self._expr_go_type_hint(raw[0]) or ""
                if self._isinstance_name(raw[1]) == "None":
                    static = static or self._infer_type_from_value(raw[0]) or ""
                    if static in self._VALUE_GO_TYPES:
                        # A value-typed operand is never ``None``.
                        return "false"
                if static == "LamJSON":
                    # ``json``: test the JSON value it holds (``isinstance(j,
                    # int)``, ``dict`` for an object, ``None`` for null).
                    if self._isinstance_name(raw[1]) == "json":
                        return "true"
                    return self._isinstance_test_go(f"lamJSONRaw({obj_go})", raw[1])
                static_class = self._hier_class_from_go_type(self._hier_expr_go_type(raw[0]))
                return self._isinstance_test_go(obj_go, raw[1], static_class)
            return "false"

        if func_str == "type":
            self._need_import("fmt")
            return f'fmt.Sprintf("%T", {args[0]})' if args else '"unknown"'

        if func_str == "input":
            self._need_import("fmt")
            self._need_import("bufio")
            self._need_import("os")
            if args:
                return f'func() string {{ fmt.Print({args[0]}); scanner := bufio.NewScanner(os.Stdin); scanner.Scan(); return scanner.Text() }}()'
            return f'func() string {{ scanner := bufio.NewScanner(os.Stdin); scanner.Scan(); return scanner.Text() }}()'

        if func_str == "exit":
            self._need_import("os")
            return f"os.Exit({args[0] if args else '0'})"

        if func_str == "append":
            if len(args) >= 2:
                return f"append({args[0]}, {', '.join(args[1:])})"

        if func_str == "format":
            self._need_import("fmt")
            return f'fmt.Sprintf({", ".join(args)})'

        # ── math module ──
        if func_str.startswith("math.") and "(" not in func_str:
            self._need_import("math")
            method = func_str[5:]
            go_method = method[0].upper() + method[1:]
            return f"math.{go_method}({', '.join(args)})"

        if func_str.startswith("os.") and "(" not in func_str:
            self._need_import("os")
            return f"{func_str}({', '.join(args)})"

        if func_str.startswith("strings.") and "(" not in func_str:
            self._need_import("strings")
            return f"{func_str}({', '.join(args)})"

        # ── String methods ──
        #
        # Every Lam-side string method (``"hello".toUpper()``,
        # ``s.split(",")``, …) lowers to a call into the
        # ``lamstrings`` standard library rather than inlining a
        # ``strings.X`` Go call here. The dispatch key matches the
        # Lam-facing static-method name on ``lamstrings.Strings``
        # exactly (``toUpper``, ``trim``, ``startsWith``,
        # ``index``, …) so the language surface reads like the
        # stdlib: ``"hi".toUpper()`` and ``Strings.toUpper("hi")``
        # are spelled the same way and run the same code. The
        # Go-mangled emission is ``Strings_<methodName>``.
        # See ``lib/lamstrings.lam`` for the reference
        # implementations; the compiler driver auto-injects
        # ``lamstrings`` into the import worklist whenever any of
        # these dispatch names appears in user source, so callers
        # don't need ``from lamstrings import Strings`` for the
        # syntactic sugar to "just work".
        #
        # The whitespace cutset for argument-less ``.trimLeft()``
        # / ``.trimRight()`` matches what the Go-level
        # ``strings.TrimLeft`` / ``TrimRight`` defaults used to be
        # — a small, ASCII-only set, not the full Unicode
        # whitespace class that ``strings.TrimSpace`` would use.
        # We keep that exact behaviour so existing user code
        # doesn't change semantics under the refactor.
        _ws_arg = '" \\t\\n"'

        def _strings_call(method, *args):
            return f"Strings_{method}({', '.join(args)})"

        string_methods = {
            "repeat":     lambda o, a: _strings_call("repeat", o, a[0]),
            "contains":   lambda o, a: _strings_call("contains", o, a[0]),
            "hasPrefix":  lambda o, a: _strings_call("hasPrefix", o, a[0]),
            "hasSuffix":  lambda o, a: _strings_call("hasSuffix", o, a[0]),
            "toUpper":    lambda o, a: _strings_call("toUpper", o),
            "toLower":    lambda o, a: _strings_call("toLower", o),
            "trim":       lambda o, a: _strings_call("trim", o),
            "length":     lambda o, a: _strings_call("length", o),
            # ``Strings.trimLeft`` / ``trimRight`` need an explicit
            # cutset; default to ASCII whitespace when the Lam call
            # was bare (``s.trimLeft()``).
            "trimLeft":   lambda o, a: _strings_call("trimLeft", o, a[0] if a else _ws_arg),
            "trimRight":  lambda o, a: _strings_call("trimRight", o, a[0] if a else _ws_arg),
            "replace":    lambda o, a: _strings_call("replace", o, *a),
            "split":      lambda o, a: _strings_call("split", o, a[0]),
            # ``"sep".join(parts)`` — the receiver is the
            # separator, the argument is the sequence, but
            # ``Strings.join(parts, sep)`` takes them the other
            # way around. The dispatcher swaps them so callers
            # keep the familiar receiver-as-separator ordering.
            "join":       lambda o, a: _strings_call("join", a[0], o) if a else o,
            "startsWith": lambda o, a: _strings_call("startsWith", o, a[0]),
            "endsWith":   lambda o, a: _strings_call("endsWith", o, a[0]),
            # ``Strings.index`` returns -1 when the substring is
            # absent — same shape as Go's ``strings.Index``.
            "index":      lambda o, a: _strings_call("index", o, a[0]),
            "lastIndex":  lambda o, a: _strings_call("lastIndex", o, a[0]),
            "count":      lambda o, a: _strings_call("count", o, a[0]),
            "title":      lambda o, a: _strings_call("title", o),
            "equalFold":  lambda o, a: _strings_call("equalFold", o, a[0]),
            "fields":     lambda o, a: _strings_call("fields", o),
            "capitalize": lambda o, a: _strings_call("capitalize", o),
            "isAlpha":    lambda o, a: _strings_call("isAlpha", o),
            "isDigit":    lambda o, a: _strings_call("isDigit", o),
            "isAlnum":    lambda o, a: _strings_call("isAlnum", o),
            "isSpace":    lambda o, a: _strings_call("isSpace", o),
            "reverse":    lambda o, a: _strings_call("reverse", o),
            "center":     lambda o, a: _strings_call("center", o, a[0], a[1] if len(a) > 1 else '" "'),
            "zfill":      lambda o, a: _strings_call("zfill", o, a[0]),
            "padLeft":    lambda o, a: _strings_call("padLeft", o, a[0], a[1] if len(a) > 1 else '" "'),
            "padRight":   lambda o, a: _strings_call("padRight", o, a[0], a[1] if len(a) > 1 else '" "'),
            "splitLines": lambda o, a: _strings_call("splitLines", o),
            "splitN":     lambda o, a: _strings_call("splitN", o, a[0], a[1]),
            "replaceFirst": lambda o, a: _strings_call("replaceFirst", o, *a),
            "containsAny":  lambda o, a: _strings_call("containsAny", o, a[0]),
            "isEmpty":    lambda o, a: _strings_call("isEmpty", o),
            "isBlank":    lambda o, a: _strings_call("isBlank", o),
            "indent":     lambda o, a: _strings_call("indent", o, a[0]),
            "dedent":     lambda o, a: _strings_call("dedent", o),
            "camelCase":  lambda o, a: _strings_call("camelCase", o),
            "pascalCase": lambda o, a: _strings_call("pascalCase", o),
            "snakeCase":  lambda o, a: _strings_call("snakeCase", o),
            "kebabCase":  lambda o, a: _strings_call("kebabCase", o),
            # ``Strings.format`` is variadic so the ``args`` are
            # forwarded as-is. Empty ``a`` is fine — Sprintf with
            # no extras just returns the template untouched.
            "format":     lambda o, a: _strings_call("format", o, *a),
        }

        if raw_method and raw_obj is not None:
            raw_obj_node = func.children[0] if isinstance(func, Tree) and func.data == "getattr" else None
            inferred_obj_go = ""
            if isinstance(raw_obj_node, Tree):
                candidate_obj_go = (
                    self._expr_go_type_hint(raw_obj_node)
                    or self._infer_type_from_value(raw_obj_node)
                )
                if candidate_obj_go and candidate_obj_go != "interface{}":
                    inferred_obj_go = candidate_obj_go
                if inferred_obj_go.startswith("[]") and raw_obj_node.data in {
                    "list", "list_comprehension",
                }:
                    raw_obj = self._typed_value_to_go(raw_obj_node, inferred_obj_go)

            set_call = self._set_method_call_to_go(raw_obj, raw_method, inferred_obj_go, args_node)
            if set_call is not None:
                return set_call

            # ``str``-builtins like ``replace``/``count``/``split``/``find``
            # take at least one mandatory argument. If a Lam method-call
            # uses one of those names with zero args (e.g.
            # ``qb.count()`` on a custom class), intercepting it as
            # ``strings.Count(o, " ")`` would silently emit nonsense.
            # Fall through to user-method dispatch in that case.
            string_method_min_args = {
                "repeat": 1,
                "contains": 1,
                "hasPrefix": 1,
                "hasSuffix": 1,
                "replace": 2,
                "split": 1,
                "join": 1,
                "startsWith": 1,
                "endsWith": 1,
                "index": 1,
                "lastIndex": 1,
                "count": 1,
                "equalFold": 1,
                "center": 1,
                "zfill": 1,
                "padLeft": 1,
                "padRight": 1,
                "splitN": 2,
                "replaceFirst": 2,
                "containsAny": 1,
                "indent": 1,
            }

            if (
                raw_method in string_methods
                and len(args) >= string_method_min_args.get(raw_method, 0)
            ):
                # Only treat this as a string-builtin call when we are sure
                # the receiver is actually a string. If the receiver is a
                # known class instance (tracked via a type annotation) we
                # delegate to the regular method-call lowering instead.
                obj_cls = self._var_types.get(raw_obj, "")
                obj_go_type = self._var_go_types.get(raw_obj, "") or inferred_obj_go
                is_user_instance = bool(obj_cls) and obj_cls in self._class_names
                is_known_non_string = obj_go_type and obj_go_type != "string"
                if not is_user_instance and not is_known_non_string:
                    if raw_method == "join" and args:
                        # ``Strings.join(parts: list[str], sep)``: shape an
                        # inline list literal / comprehension as ``[]string``.
                        raw_args = self._get_raw_call_args(args_node)
                        if raw_args and isinstance(raw_args[0], Tree) and raw_args[0].data in {
                            "list", "list_comprehension",
                        }:
                            args = [self._typed_value_to_go(raw_args[0], "[]string"), *args[1:]]
                    return string_methods[raw_method](raw_obj, args)

            # List methods — only applied when the receiver is (or may be)
            # a list. If we know the receiver is a user class instance,
            # dispatch to the user-defined method instead so `obj.pop()`
            # calls the class's `pop` rather than slicing the struct.
            _obj_cls = self._var_types.get(raw_obj, "")
            _obj_go = self._var_go_types.get(raw_obj, "") or inferred_obj_go
            _is_user_inst = bool(_obj_cls) and _obj_cls in self._class_names
            # Inside a class method body, ``self`` (which lowers to the
            # configured replacement, normally ``s``) refers to the
            # current class instance. Treat it as a user instance so
            # ``self.pop()`` dispatches to the method rather than the
            # built-in list-pop transform.
            if (
                not _is_user_inst
                and self.current_class
                and self._self_replacement
                and raw_obj == self._self_replacement
            ):
                _obj_cls = self.current_class
                _is_user_inst = True
            _is_non_list = _obj_go and not _obj_go.startswith("[]")
            if not _is_user_inst and not _is_non_list:
                _elem_go = self._slice_elem_go_type(_obj_go) or self._infer_receiver_elem_type(
                    func.children[0] if isinstance(func, Tree) else None
                )
                raw_args_nodes = self._get_raw_call_args(args_node)
                if raw_method == "length":
                    return f"len({raw_obj})"
                if raw_method in {"append", "insert", "extend"} and _elem_go:
                    # ``xs.append(Box([]))`` on a ``list[Box[int]]``: the
                    # item's generic call expects the list's item type.
                    item_index = 1 if raw_method == "insert" else 0
                    if len(raw_args_nodes) > item_index:
                        self._note_expected_go_item(
                            raw_args_nodes[item_index],
                            f"[]{_elem_go}" if raw_method == "extend" else _elem_go,
                            func.children[0] if isinstance(func, Tree) else None,
                            extend=raw_method == "extend",
                        )
                if raw_method == "append" and args:
                    typed_args = [
                        self._retyped_lowered_arg(arg_node, arg, _elem_go) if _elem_go else arg
                        for arg_node, arg in zip(raw_args_nodes, args)
                    ]
                    if len(args) > len(typed_args):
                        typed_args.extend(args[len(typed_args):])
                    return f"{raw_obj} = append({raw_obj}, {', '.join(typed_args)})"
                if raw_method == "pop":
                    result_type = _elem_go or "interface{}"
                    if args:
                        # ``xs.pop(i)``: remove and return the item at ``i``
                        # (negative counts from the end, like Python).
                        popped = (
                            f"func(i int) {result_type} {{ if i < 0 {{ i += len({raw_obj}) }}; item := {raw_obj}[i]; "
                            f"{raw_obj} = append({raw_obj}[:i], {raw_obj}[i+1:]...); return item }}({args[0]})"
                        )
                    else:
                        popped = f"func() {result_type} {{ last := {raw_obj}[len({raw_obj})-1]; {raw_obj} = {raw_obj}[:len({raw_obj})-1]; return last }}()"
                    return popped
                if raw_method == "extend" and args:
                    typed_arg = (
                        self._retyped_lowered_arg(raw_args_nodes[0], args[0], f"[]{_elem_go}")
                        if _elem_go and raw_args_nodes else args[0]
                    )
                    return f"{raw_obj} = append({raw_obj}, {typed_arg}...)"
                if raw_method == "insert" and len(args) >= 2:
                    item_arg = (
                        self._retyped_lowered_arg(raw_args_nodes[1], args[1], _elem_go)
                        if _elem_go and len(raw_args_nodes) > 1 else args[1]
                    )
                    scratch_type = f"[]{_elem_go}" if _elem_go else "[]interface{}"
                    return f"{raw_obj} = append({raw_obj}[:int({args[0]})], append({scratch_type}{{{item_arg}}}, {raw_obj}[int({args[0]}):]...)...)"

            # Functional methods — same gate as the list methods above.
            # If the receiver is known to be a user class, fall through
            # to the generic method-call dispatcher so that classes can
            # define their own map/filter/reduce/… without colliding
            # with the built-in list combinators.
            _obj_go_type = self._var_go_types.get(raw_obj, "") or inferred_obj_go
            _elem_type = ""
            if _obj_go_type.startswith("[]"):
                _elem_type = _obj_go_type[2:]
            if not _elem_type:
                # Chained receivers like ``nums.filter(...).map(...)``
                # aren't tracked in _var_go_types, so walk the AST to
                # see if we can infer the element type from an earlier
                # link in the chain.
                _elem_type = self._infer_receiver_elem_type(
                    func.children[0] if isinstance(func, Tree) else None
                )
            if not _elem_type:
                _elem_type = "interface{}"
            _slice_type = f"[]{_elem_type}"
            _apply_list_combinators = not _is_user_inst and not _is_non_list

            def _resolve_func_ref(fn_expr):
                if fn_expr in self._user_functions:
                    return self._unshadowed_function(self._go_user_function_name(fn_expr), fn_expr)
                return fn_expr

            def _is_user_func_arg(arg_expr):
                return self._is_user_function_reference(arg_expr)

            # Raw AST nodes for the functional-method arguments — we
            # inspect the first positional arg to detect inline lambdas
            # and transpile them with the receiver's element type as a
            # hint, so the emitted callback has typed params (and often
            # a concrete return type) rather than ``interface{}``.
            raw_args_nodes = self._get_raw_call_args(args_node)
            inline_lambda = (
                raw_args_nodes and self._is_lambda_node(raw_args_nodes[0])
            )

            raw_kwargs_nodes = {}
            if isinstance(args_node, Tree) and args_node.data == "arguments":
                for child in args_node.children:
                    if not (
                        isinstance(child, Tree)
                        and child.data == "argvalue"
                        and len(child.children) == 2
                    ):
                        continue
                    name_node = child.children[0]
                    if (
                        isinstance(name_node, Tree)
                        and name_node.data == "var"
                        and name_node.children
                    ):
                        raw_kwargs_nodes[
                            self._get_name(name_node.children[0])
                        ] = child.children[1]

            if _apply_list_combinators and raw_method == "sort":
                self._need_import("sort")
                sort_args = list(args)
                compare_expr = kwargs.get("compare")
                inplace_expr = kwargs.get("inplace")
                compare_raw = raw_kwargs_nodes.get("compare")
                if sort_args:
                    first = sort_args[0]
                    if first in ("true", "false") and len(sort_args) == 1:
                        inplace_expr = first
                    else:
                        compare_expr = first
                        compare_raw = raw_args_nodes[0] if raw_args_nodes else None
                        if len(sort_args) >= 2:
                            inplace_expr = sort_args[1]
                inplace = inplace_expr == "true"
                target_expr = raw_obj if inplace else f"append({_slice_type}{{}}, {raw_obj}...)"
                key_raw = raw_kwargs_nodes.get("key")
                if key_raw is None and compare_raw is not None and self._sort_callback_arity(compare_raw) == 1:
                    # ``xs.sort(lambda w: len(w))``: a one-parameter
                    # callback is a key function, like Python's ``key=``.
                    key_raw, compare_expr = compare_raw, ""
                if key_raw is not None:
                    key_type = self._list_callback_result_go_type(key_raw, [_elem_type]) or "interface{}"
                    if self._is_lambda_node(key_raw):
                        key_fn = self._lambda_to_go(key_raw, param_types=[_elem_type], return_type=key_type)
                    else:
                        key_fn = (
                            self._builtin_func_ref_to_go(key_raw, f"func({_elem_type}) {key_type}")
                            or _resolve_func_ref(self._expr_to_go(key_raw))
                        )
                    key_cmp = ""
                    if key_type in self._ORDERED_GO_TYPES:
                        key_less = "_k(_r[i]) < _k(_r[j])"
                    elif key_type == "bool":
                        key_less = "!_k(_r[i]) && _k(_r[j])"
                    elif self._orders_dynamically(key_type):
                        key_cmp = f"_c := {self._dynamic_compare_go()}; "
                        key_less = "_c(_k(_r[i]), _k(_r[j])) < 0"
                    else:
                        self._need_import("fmt")
                        key_less = 'fmt.Sprintf("%v", _k(_r[i])) < fmt.Sprintf("%v", _k(_r[j]))'
                    return (
                        f"func() {_slice_type} {{ _r := {target_expr}; _k := {key_fn}; {key_cmp}"
                        f"sort.SliceStable(_r, func(i, j int) bool {{ return {key_less} }}); return _r }}()"
                    )
                if compare_expr:
                    if compare_raw is not None and self._is_lambda_node(compare_raw):
                        fn = self._lambda_to_go(
                            compare_raw,
                            param_types=[_elem_type, _elem_type],
                            return_type="bool",
                        )
                        less_expr = f"({fn})(_r[i], _r[j])"
                    else:
                        fn = _resolve_func_ref(compare_expr)
                        less_expr = f"{fn}(_r[i], _r[j])"
                elif _elem_type in (
                    "int", "int8", "int16", "int32", "int64",
                    "uint", "uint8", "uint16", "uint32", "uint64",
                    "float32", "float64", "string",
                ):
                    less_expr = "_r[i] < _r[j]"
                elif _elem_type == "bool":
                    less_expr = "!_r[i] && _r[j]"
                elif self._orders_dynamically(_elem_type):
                    # ``list[any]`` / ``list[json]`` / ``list[T]``: by kind.
                    return (
                        f"func() {_slice_type} {{ _r := {target_expr}; _c := {self._dynamic_compare_go()}; "
                        f"sort.SliceStable(_r, func(i, j int) bool {{ "
                        f"return _c(_r[i], _r[j]) < 0 }}); return _r }}()"
                    )
                else:
                    self._need_import("fmt")
                    less_expr = (
                        'fmt.Sprintf("%v", _r[i]) < '
                        'fmt.Sprintf("%v", _r[j])'
                    )
                return (
                    f"func() {_slice_type} {{ _r := {target_expr}; "
                    f"sort.Slice(_r, func(i, j int) bool {{ "
                    f"return {less_expr} }}); return _r }}()"
                )

            if _apply_list_combinators and raw_method == "map" and args:
                expected_type = getattr(self, "_expected_expr_go_type", "") or ""
                result_elem_type = (
                    self._slice_elem_go_type(expected_type)
                    if expected_type.startswith("[]") else ""
                )
                if not result_elem_type and inline_lambda:
                    # ``ps.map(lambda p: p.name)``: type the body with the
                    # parameter bound to the receiver's element type.
                    result_elem_type = self._lambda_result_go_type(raw_args_nodes[0], [_elem_type])
                    if result_elem_type == "interface{}":
                        result_elem_type = _elem_type
                if not result_elem_type and raw_args_nodes:
                    result_elem_type = (
                        self._callable_return_elem_go_type(raw_args_nodes[0])
                        or self._list_callback_result_go_type(raw_args_nodes[0], [_elem_type])
                    )
                if not result_elem_type:
                    result_elem_type = _elem_type
                result_slice_type = f"[]{result_elem_type}"
                builtin_fn = self._builtin_func_ref_to_go(
                    raw_args_nodes[0], f"func({_elem_type}) {result_elem_type}",
                ) if raw_args_nodes else ""
                if builtin_fn:
                    # ``words.map(len)``: builtins are not Go values.
                    return (
                        f"func() {result_slice_type} {{ _r := make({result_slice_type}, len({raw_obj})); "
                        f"for _i, _v := range {raw_obj} {{ _r[_i] = ({builtin_fn})(_v) }}; return _r }}()"
                    )
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0],
                        param_types=[_elem_type],
                        return_type=result_elem_type,
                    )
                    return (
                        f"func() {result_slice_type} {{ _r := make({result_slice_type}, len({raw_obj})); "
                        f"for _i, _v := range {raw_obj} {{ _r[_i] = ({fn})(_v) }}; return _r }}()"
                    )
                fn = _resolve_func_ref(args[0])
                is_user_func = _is_user_func_arg(args[0])
                callback_type = (self._expr_go_type_hint(raw_args_nodes[0]) or "") if raw_args_nodes else ""
                callback_name = (
                    self._get_name(raw_args_nodes[0].children[0])
                    if raw_args_nodes and isinstance(raw_args_nodes[0], Tree) and raw_args_nodes[0].data == "var"
                    else ""
                )
                # A nested ``func`` returns its declared type: no assertion.
                typed_callback = (
                    bool(callback_name)
                    and self.__dict__.get("_nested_closure_go_types", {}).get(callback_name) == callback_type
                    and callback_type.startswith("func(") and not callback_type.endswith(")")
                    and not callback_type.endswith("interface{}")
                )
                if is_user_func or typed_callback or result_elem_type == "interface{}":
                    return f"func() {result_slice_type} {{ _r := make({result_slice_type}, len({raw_obj})); for _i, _v := range {raw_obj} {{ _r[_i] = {fn}(_v) }}; return _r }}()"
                else:
                    return f"func() {result_slice_type} {{ _r := make({result_slice_type}, len({raw_obj})); for _i, _v := range {raw_obj} {{ _r[_i] = {fn}(_v).({result_elem_type}) }}; return _r }}()"
            if _apply_list_combinators and raw_method == "filter" and args:
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0], param_types=[_elem_type], return_type="bool"
                    )
                    return (
                        f"func() {_slice_type} {{ _r := {_slice_type}{{}}; "
                        f"for _, _v := range {raw_obj} {{ if ({fn})(_v) {{ _r = append(_r, _v) }} }}; return _r }}()"
                    )
                fn = _resolve_func_ref(args[0])
                is_user_func = _is_user_func_arg(args[0])
                if is_user_func:
                    return f"func() {_slice_type} {{ _r := {_slice_type}{{}}; for _, _v := range {raw_obj} {{ if {fn}(_v) {{ _r = append(_r, _v) }} }}; return _r }}()"
                else:
                    return f"func() {_slice_type} {{ _r := {_slice_type}{{}}; for _, _v := range {raw_obj} {{ if {fn}(_v).(bool) {{ _r = append(_r, _v) }} }}; return _r }}()"
            if _apply_list_combinators and raw_method == "reduce" and len(args) >= 1:
                # ``words.reduce(lambda acc, w: acc + len(w), 0)``: the
                # accumulator takes the initial value's type.
                _acc_type = self._list_reduce_acc_go_type(_elem_type, raw_args_nodes) if len(args) >= 2 else _elem_type
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0],
                        param_types=[_acc_type, _elem_type],
                        return_type=_acc_type,
                    )
                    if len(args) >= 2:
                        init = self._typed_value_to_go(raw_args_nodes[1], _acc_type) if len(raw_args_nodes) >= 2 else args[1]
                        return (
                            f"func() {_acc_type} {{ _acc := {_acc_type}({init}); "
                            f"for _, _v := range {raw_obj} {{ _acc = ({fn})(_acc, _v) }}; return _acc }}()"
                        )
                    return (
                        f"func() {_elem_type} {{ _acc := {raw_obj}[0]; "
                        f"for _, _v := range {raw_obj}[1:] {{ _acc = ({fn})(_acc, _v) }}; return _acc }}()"
                    )
                fn = _resolve_func_ref(args[0])
                if len(args) >= 2:
                    init = args[1]
                    return f"func() {_acc_type} {{ _acc := {_acc_type}({init}); for _, _v := range {raw_obj} {{ _acc = {fn}(_acc, _v) }}; return _acc }}()"
                else:
                    return f"func() {_elem_type} {{ _acc := {raw_obj}[0]; for _, _v := range {raw_obj}[1:] {{ _acc = {fn}(_acc, _v) }}; return _acc }}()"
            if _apply_list_combinators and raw_method == "any" and args:
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0], param_types=[_elem_type], return_type="bool"
                    )
                    return (
                        f"func() bool {{ for _, _v := range {raw_obj} {{ if ({fn})(_v) {{ return true }} }}; return false }}()"
                    )
                fn = _resolve_func_ref(args[0])
                is_user_func = _is_user_func_arg(args[0])
                if is_user_func:
                    return f"func() bool {{ for _, _v := range {raw_obj} {{ if {fn}(_v) {{ return true }} }}; return false }}()"
                else:
                    return f"func() bool {{ for _, _v := range {raw_obj} {{ if {fn}(_v).(bool) {{ return true }} }}; return false }}()"
            if _apply_list_combinators and raw_method == "all" and args:
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0], param_types=[_elem_type], return_type="bool"
                    )
                    return (
                        f"func() bool {{ for _, _v := range {raw_obj} {{ if !({fn})(_v) {{ return false }} }}; return true }}()"
                    )
                fn = _resolve_func_ref(args[0])
                is_user_func = _is_user_func_arg(args[0])
                if is_user_func:
                    return f"func() bool {{ for _, _v := range {raw_obj} {{ if !{fn}(_v) {{ return false }} }}; return true }}()"
                else:
                    return f"func() bool {{ for _, _v := range {raw_obj} {{ if !{fn}(_v).(bool) {{ return false }} }}; return true }}()"
            if _apply_list_combinators and raw_method == "foreach" and args:
                if inline_lambda:
                    fn = self._lambda_to_go(
                        raw_args_nodes[0], param_types=[_elem_type],
                        return_type="", contextual=True,
                    )
                    return (
                        f"func() {{ for _, _v := range {raw_obj} {{ ({fn})(_v) }} }}()"
                    )
                fn = _resolve_func_ref(args[0])
                return f"func() {{ for _, _v := range {raw_obj} {{ {fn}(_v) }} }}()"

            return f"{func_str}({', '.join(args)})"
        elif "." in func_str:
            return f"{func_str}({', '.join(args)})"

        # ── Python exception constructors ──
        if func_str in PYTHON_EXCEPTIONS:
            self._need_import("fmt")
            if args:
                return f'fmt.Sprintf("{func_str}: %v", {", ".join(args)})'
            return f'"{func_str}"'

        # ── go! block marker ──
        if func_str == "__go_block__" and args:
            block_id = args[0].strip('"').strip("'")
            if block_id in self.go_blocks:
                raw = self.go_blocks[block_id]
                # Resolve ``LAMMERGEIER.<userFunc/Class[.staticMethod]>``
                # references to the Go-mangled symbol. Compiler-emitted
                # aliases were already rewritten before parsing; this
                # pass handles user-defined names that need the AST
                # to resolve. Done before the return-rewrite so the
                # zero-value rewriter sees the final shape.
                raw = self._resolve_user_lammergeier(raw)
                # ``options("a")`` -> ``Options("a")`` for module functions
                # named with their Lam spelling (same as ``go!(...)``).
                raw = self._resolve_raw_lam_function_names(raw, block_id)
                raw = self._rename_shadowing_locals_in_raw_go(raw)
                # Rewrite bare ``return`` statements inside this block
                # to include the zero-value of the surrounding Lam
                # function's declared return type. This lets Lam
                # authors write idiomatic Go control-flow inside
                # ``go!`` without having to know Go's
                # ``not enough return values`` rule.
                raw = self._rewrite_go_block_returns(raw)
                # When the surrounding scope is a method body, rewrite
                # any literal ``self.<Field>`` reference inside the
                # ``go!`` block to use the configured Go receiver
                # name (typically ``s``). Lam authors otherwise have
                # to remember to write ``s.X`` manually inside go!
                # blocks even though the surrounding Lam code uses
                # ``self.X`` — this aligns the two. Field names are
                # also title-cased to match Go's exported convention
                # used by the struct emitter.
                if self._self_replacement:
                    import re as _re_self
                    repl = self._self_replacement
                    def _self_to_recv(m):
                        attr = m.group(1)
                        return f"{repl}.{self._go_public_name(attr)}"
                    raw = _re_self.sub(r'\bself\.(\w+)', _self_to_recv, raw)
                lines_raw = raw.strip().split("\n")
                in_import_group = False

                def _record_import_specs(text: str) -> bool:
                    specs = re.findall(
                        r'(?:[A-Za-z_]\w*|[._])?\s*(?:"(?:\\.|[^"\\])*"|`[^`]+`)',
                        text,
                    )
                    for spec in specs:
                        spec = spec.strip()
                        if spec:
                            self.user_go_imports.add(spec)
                    return bool(specs)

                for line in lines_raw:
                    stripped = line.strip()
                    if stripped.startswith("import ("):
                        remainder = stripped[len("import ("):].strip()
                        if remainder.endswith(")"):
                            _record_import_specs(remainder[:-1])
                        else:
                            in_import_group = True
                            _record_import_specs(remainder)
                        continue
                    if in_import_group:
                        if stripped.endswith(")"):
                            _record_import_specs(stripped[:-1])
                            in_import_group = False
                        else:
                            _record_import_specs(stripped)
                        continue
                    if stripped.startswith("import "):
                        if not _record_import_specs(stripped[len("import "):]):
                            imp = stripped[len("import "):].strip().strip('"')
                            if imp:
                                self.user_go_imports.add(imp)
                    else:
                        self._emit(stripped)
                return ""
            return f"/* go block {block_id} not found */"

        # ── Inline go! expression ──
        if func_str == "__go_inline__" and args:
            block_id = args[0].strip().strip('"').strip("'")
            if block_id in self.go_blocks:
                inline_raw = self.go_blocks[block_id].strip()
                # Same user-name resolution as the statement form so
                # ``go! { LAMMERGEIER.foo() }`` and inline
                # ``go! LAMMERGEIER.foo()`` both work, and so do Lam
                # spellings of module functions (``go!(options("a"))``).
                return self._rename_shadowing_locals_in_raw_go(self._resolve_raw_lam_function_names(
                    self._resolve_user_lammergeier(inline_raw), block_id,
                ))
            return f"/* go inline {block_id} not found */"

        # ── User-defined function ──
        nested_key = self._nested_func_keys.get(func_str)
        if func_str in self._nested_generic_functions:
            if nested_key:
                # Defaults and keywords of the nested function; its own type
                # parameters are bound from the arguments, as for a
                # top-level generic (``pick(["a"], 0)`` passes a
                # ``[]string``, not a ``[]T``).
                own = self._nested_generic_functions[func_str].get("type_param_names") or []
                subst = (
                    self._generic_call_subst(nested_key, args_node, own)
                    if own and args_node is not None else {}
                )
                subst = {k: v for k, v in subst.items() if v and v != k}
                with self._scoped(_call_type_subst={**self._call_type_subst, **subst}):
                    args = self._planned_call_args(nested_key, args_node)
            return self._nested_generic_call_to_go(func_str, args)

        if func_str in self._user_functions and not (nested_key and func_str in self.declared_vars):
            return self._user_function_call_to_go(func_str, args, kwargs, args_node)

        # ── Local variable (lambda, etc.) ──
        if func_str in self.declared_vars:
            if nested_key:
                # ``pad(s)`` / ``pad(s, fill="0")`` of a nested
                # ``func pad(s: str, width: int = 5, fill: str = ".")``.
                return f"{func_str}({', '.join(self._planned_call_args(nested_key, args_node))})"
            return f"{func_str}({', '.join(self._func_value_call_args(func_str, args, kwargs, args_node))})"

        # ── Constructor ──
        if func_str in self._class_names:
            init_key = f"{func_str}.init"
            class_params = self._class_type_param_names_of(func_str)
            if class_params:
                # ``Box([Row(x) for x in xs])``: the arguments are lowered
                # with the class's type parameters inferred at this call
                # (a comprehension hinted by ``list[T]`` would be ``[]T``).
                call_args = self._planned_call_args_inferred(init_key, args_node)
                # ``r: Reg[int] = Reg()``: a type parameter no constructor
                # parameter mentions is spelled from the expected type.
                explicit = self._uninferable_type_args(init_key, args_node, list(class_params))
                return f"{self._go_ctor_name(func_str)}{explicit}({', '.join(call_args)})"
            else:
                call_args = self._planned_call_args(init_key, args_node)
            return f"{self._go_ctor_name(func_str)}({', '.join(call_args)})"
        if func_str and func_str[0].isupper() and "." not in func_str:
            return f"New{func_str}({', '.join(args)})"

        # ── Default ──
        go_name = self._go_public_name(func_str)
        return f"{go_name}({', '.join(args)})"

    # ─── f-string ──────────────────────────────────────────────

    def _fstring_slot_tree(self, expr: str):
        """The parsed expression of an f-string slot (cached; ``None`` when
        it does not parse), without lowering it."""
        cache = self.__dict__.setdefault("_fstring_tree_cache", {})
        if expr in cache:
            return cache[expr]
        # A lambda with a block body needs the statement preprocessing
        # (``compiler/fstrings.py``: ``parse_slot``).
        tree = parse_slot("_lamFstrTmp = (", expr)
        if tree is None:
            cache[expr] = None
            return None
        # Find the assign tree's RHS — the parser produces a tiny
        # ``file_input > expr_stmt > assign_stmt > assign`` chain, but
        # the exact shape depends on whether the RHS was a tuple,
        # ternary, etc. Walk for the first ``assign`` and grab the
        # last child (which is the RHS by convention).
        rhs = None
        for sub in tree.iter_subtrees():
            if sub.data == "assign":
                if len(sub.children) >= 2:
                    rhs = sub.children[-1]
                    break
        if rhs is None:
            cache[expr] = None
            return None
        # The RHS came in as ``( ... )`` so the outer node is a
        # ``test`` wrapper around the actual expression. Unwrap when
        # the parens collapse to a single child.
        from lark import Tree as _Tree
        while isinstance(rhs, _Tree) and rhs.data == "test" and len(rhs.children) == 1:
            rhs = rhs.children[0]
        cache[expr] = rhs
        return rhs

    def _lower_fstring_expr_via_ast(self, expr: str) -> "Optional[str]":
        """Parse a single f-string interpolation slot and lower it
        through the normal expression pipeline.

        Wraps the slot in a tiny synthetic statement (``_x = (<expr>);``)
        so the existing parser can handle it without needing a second
        start rule. The wrapper unwraps to find the RHS and returns
        ``self._expr_to_go(rhs)``. Returns ``None`` on any parse
        failure so the caller can fall back to the legacy regex path
        (and on rare expressions the regex is still correct, e.g.
        bare identifiers that were already valid Go).
        """
        cache = self.__dict__.setdefault("_fstring_expr_cache", {})
        if expr in cache:
            cached = cache[expr]
            if cached is None:
                return None
            cached = self._fstring_slot_result_nav(cached)
            with self._scoped(_synthetic_positions=True):
                return self._display_go(self._expr_to_go(cached), cached)
        rhs = self._fstring_slot_tree(expr)
        if rhs is None:
            return None
        cache[expr] = rhs
        try:
            rhs = self._fstring_slot_result_nav(rhs)
            with self._scoped(_synthetic_positions=True):
                return self._display_go(self._expr_to_go(rhs), rhs)
        except Exception:
            cache[expr] = None
            return None

    def _fstring_slot_result_nav(self, slot):
        """``f"{word()?.toUpper()}"``: the slot's ``?.`` on a ``Result`` is
        propagation then member access, like in a statement (the statement
        rewrite does not reach slots, which are parsed here). The cached
        slot tree is shared between scopes, so a copy is rewritten."""
        if not isinstance(slot, Tree) or not any(True for _ in slot.find_data("getattr_safe")):
            return slot
        import copy
        from compiler.safe_nav import rewrite_safe_nav_as_propagation
        slot = copy.deepcopy(slot)
        rewrite_safe_nav_as_propagation(slot)
        return slot

    def _transform_fstring_expr(self, expr: str) -> str:
        expr = expr.strip()
        if not expr:
            return expr

        # ── Full-AST path ─────────────────────────────────────────
        # Try parsing the interpolation slot as a Lam expression and
        # lowering it through the regular ``_expr_to_go`` pipeline.
        # This is the *only* way to handle composite expressions like
        # ``users[0].profile?.email ?? "none"`` correctly — the regex
        # path below can only see the outermost shape and silently
        # drops the rest. Parse failures fall through to the legacy
        # regex path so simple expressions never regress.
        ast_lowered = self._lower_fstring_expr_via_ast(expr)
        if ast_lowered is not None:
            return ast_lowered

        if self._self_replacement:
            def _replace_self_attr(m):
                attr = m.group(1)
                return f"{self._self_replacement}.{self._go_public_name(attr)}"
            expr = re.sub(r'\bself\.(\w+)', _replace_self_attr, expr)

        sm_match = re.match(r'^(.+?)\.(\w+)\((.*)\)$', expr)
        if sm_match:
            obj_part = sm_match.group(1)
            method_name = sm_match.group(2)
            call_args = sm_match.group(3).strip()

            if method_name == "join":
                self._need_import("strings")
                obj_go = self._transform_fstring_expr(obj_part)
                return f"strings.Join({call_args}, {obj_go})"
            else:
                obj_go = self._transform_fstring_expr(obj_part)
                # Recursively rewrite the call args so nested static
                # calls (``Uuid.isValid(Uuid.v7())``) are translated
                # all the way down rather than being left in
                # dotted-method form for the outer transform to fix.
                inner_args_go = (
                    self._transform_fstring_expr(call_args)
                    if call_args else ""
                )
                if obj_part in self._class_names and obj_part in self._static_methods:
                    if method_name in self._static_methods[obj_part]:
                        go_name = self._go_static_base(obj_part, method_name)
                        return f"{go_name}({inner_args_go})"
                go_method = self._go_public_name(method_name)
                return f"{obj_go}.{go_method}({inner_args_go})"

        dot_match = re.match(r'^(\w+)\.(\w+)$', expr)
        if dot_match:
            obj_name = dot_match.group(1)
            attr_name = dot_match.group(2)
            if obj_name in LOWER_PKGS and obj_name not in self.declared_vars and obj_name not in self._var_go_types:
                return f"{obj_name}.{attr_name}"
            if attr_name in self._static_vars.get(obj_name, {}):
                return self._static_var_go_name(obj_name, attr_name)
            return f"{obj_name}.{self._go_public_name(attr_name)}"

        bare_call_match = re.match(r'^(\w+)\((.*)\)$', expr, re.DOTALL)
        if bare_call_match:
            func_name = bare_call_match.group(1)
            call_args = bare_call_match.group(2)
            if func_name in GO_BUILTINS:
                return expr
            go_name = self._go_user_function_name(func_name)
            if func_name in self._func_defaults:
                arg_list = [a.strip() for a in call_args.split(",") if a.strip()] if call_args.strip() else []
                arg_list = self._fill_default_args(func_name, arg_list)
                call_args = ", ".join(arg_list)
            if len(self._overloaded_functions.get(func_name, set())) > 1:
                arg_count = len([a.strip() for a in call_args.split(",") if a.strip()]) if call_args.strip() else 0
                go_name = f"{go_name}_{arg_count}"
            return f"{self._unshadowed_function(go_name, func_name)}({call_args})"

        return expr

    _FSTRING_SPEC_VERBS = 'fdseEgGoxXbcn%'

    def _fstring_expr_parses(self, expr: str) -> bool:
        """The text before a slot's top-level ``:`` is an expression (so the
        rest is its format spec)."""
        expr = expr.strip()
        if not expr:
            return False
        cache = self.__dict__.setdefault("_fstring_parse_ok_cache", {})
        if expr not in cache:
            cache[expr] = parse_slot("_lamFstrTmp = (", expr) is not None
        return cache[expr]

    # Values whose text is their ``String()`` method (``LamHTML`` and
    # ``LamXML`` render, ``LamJSON`` encodes).
    _TEXT_VALUE_GO_TYPES = frozenset({"LamHTML", "LamXML", "LamJSON"})

    # Lam scalar results of declared signatures, by Go type.
    _CERTAIN_LAM_SCALARS = {"str": "string", "int": "int", "bool": "bool"}

    def _certain_class(self, node) -> str:
        """The class (or interface) of a receiver whose type is declared:
        ``self`` in a method, a variable emitted with an explicit class
        type (a parameter, ``u: User = ...``), a field or a call declared
        with one (``AppConfig.current()``). ``""`` otherwise."""
        if not isinstance(node, Tree):
            return ""
        if node.data == "var" and node.children and self._get_name(node.children[0]) == "self":
            return self.current_class or ""
        go_type = self._certain_go_type(node)
        if not go_type or "[" in go_type:
            return ""
        for cls in self._class_names:
            if go_type == f"*{self._go_class_name(cls)}" and not self._generic_classes.get(cls):
                return cls
        for iface in self._interfaces:
            if go_type == self._go_class_name(iface):
                return iface
        return ""

    def _certain_var_go_type(self, name: str) -> str:
        """Go type of the variable ``name`` when it was declared with an
        explicit type (``_typed_decls``) the type tables still agree with."""
        if not name or name == "_":
            return ""
        go_name = self._link_const_go_names.get(name, name) if name not in self.declared_vars else name
        declared = self._typed_decls.get(go_name) or self._typed_decls.get(self._go_binding_name(name))
        if not declared:
            return ""
        if self._var_go_types.get(go_name, self._var_go_types.get(name)) != declared:
            return ""
        return declared

    def _certain_method_go_type(self, cls: str, method: str) -> str:
        """Go result of the method ``cls.method`` (or an ancestor's) declared
        ``-> str`` / ``int`` / ``bool`` (``string`` / ``int`` / ``bool``) or
        ``-> SomeClass`` (``*SomeClass``) in every overload; ``""`` otherwise
        (async, generic, ``Result``, containers,
        unknown)."""
        for owner in [cls, *self._hier_ancestors(cls)]:
            key = f"{owner}.{method}"
            raw = self._method_return_types.get(key, "")
            if not raw:
                continue
            go_type = self._CERTAIN_LAM_SCALARS.get(raw, "")
            if not go_type and raw in self._class_names and not self._generic_classes.get(raw):
                # ``AppConfig.current() -> AppConfig``: a ``*AppConfig``.
                go_type = f"*{self._go_class_name(raw)}"
                if self._method_return_types.get(f"go:{key}", go_type) != go_type:
                    return ""
            elif self._method_return_types.get(f"go:{key}"):
                return ""
            if (
                not go_type
                or self._method_return_types.get(f"async:{key}")
                or key in self._generic_method_returns
                or key in self._result_payload_go_types
            ):
                return ""
            variants = self._overload_variants.get(key, ())
            if any(v.get("return") != go_type for v in variants):
                return ""
            return go_type
        return ""

    def _certain_go_type(self, node) -> str:
        """The Go type of ``node`` when the declarations alone determine it,
        else ``""``. Unlike ``_expr_go_type_hint`` (best effort: the type a
        value probably has), this never guesses: a string literal or
        f-string (``string``); a variable emitted with an explicit type
        (``_typed_decls``: a parameter, an annotated local, a global, a
        const, an imported const; any type); a static field of a scalar
        type; a field of a receiver of certain class (``self``, such a
        variable, field or call; the struct field's type); a call of a
        function declared with a result type, of a static or instance method
        declared ``-> str`` / ``int`` / ``bool`` / ``-> SomeClass``, of
        ``str()`` / ``len()``, of a ``str`` method on a certain ``str``; a
        slice of a certain ``str``; ``+`` of certain ``str`` values; a
        ternary of two certain values of one type. An unannotated local
        (``x = f()``, declared with ``:=``) is never certain: its type comes
        from a hint."""
        if not isinstance(node, Tree):
            return ""
        d = node.data
        if d in {"string", "string_concat", "fstring"}:
            return "string"
        if d == "var" and node.children:
            name = self._get_name(node.children[0])
            if name == "self" or self._parent_alias_expr(name):
                return ""
            return self._certain_var_go_type(name)
        if d == "getattr" and len(node.children) >= 2:
            obj = node.children[0]
            attr = self._get_name(node.children[1])
            if isinstance(obj, Tree) and obj.data == "var" and obj.children:
                obj_name = self._get_name(obj.children[0])
                if (
                    obj_name in self._class_names and obj_name not in self.declared_vars
                    and obj_name not in self._var_go_types and attr in self._static_vars.get(obj_name, {})
                ):
                    # ``Config.prefix``: a static field (``var Config_prefix T``).
                    go_name = self._static_var_go_name(obj_name, attr)
                    go_type = self._typed_decls.get(go_name, "")
                    return go_type if go_type in self._CERTAIN_LAM_SCALARS.values() else ""
            cls = self._certain_class(obj)
            if not cls or cls in self._interfaces:
                return ""
            for owner in [cls, *self._hier_ancestors(cls)]:
                if self._generic_classes.get(owner):
                    return ""
                # The struct field's declared Go type.
                for field_name, field_type in self.class_fields.get(owner, []):
                    if field_name == attr:
                        return field_type
                field_type = self._external_field_types.get(owner, {}).get(attr)
                if field_type:
                    return field_type
            return ""
        if d == "getitem" and len(node.children) >= 2:
            index = node.children[1]
            if isinstance(index, Tree) and index.data == "slice" and self._certain_go_type(node.children[0]) == "string":
                return "string"
            return ""
        if d == "arith_expr" and len(node.children) >= 3:
            if {str(c) for c in node.children[1::2]} == {"+"} and all(
                self._certain_go_type(c) == "string" for c in node.children[0::2]
            ):
                return "string"
            return ""
        if d == "test" and len(node.children) == 3:
            first = self._certain_go_type(node.children[0])
            return first if first and first == self._certain_go_type(node.children[2]) else ""
        if d == "funccall" and node.children:
            return self._certain_call_go_type(node)
        return ""

    def _certain_call_go_type(self, node: Tree) -> str:
        callee = node.children[0]
        args_node = node.children[1] if len(node.children) > 1 else None
        raw_args = self._get_raw_call_args(args_node) if isinstance(args_node, Tree) else []
        positional = all(not (isinstance(a, Tree) and a.data in {"argvalue", "starargs", "kwargs"}) for a in raw_args)
        if isinstance(callee, Tree) and callee.data == "var" and callee.children:
            name = self._get_name(callee.children[0])
            if (
                name in self.declared_vars or name in self._var_go_types
                or self._go_binding_name(name) in self.declared_vars
            ):
                return ""  # a local holding a function value
            if name in {"str", "len"} and name not in self._user_functions and name not in self._function_go_names:
                return ("string" if name == "str" else "int") if len(raw_args) == 1 and positional else ""
            if (
                name in self._user_functions and name not in self._class_names
                and name not in self._async_functions and name not in self._result_payload_go_types
                and len(self._overload_variants.get(name, ())) <= 1
                and name not in getattr(self, "_nested_generic_functions", {})
            ):
                if self._func_generic_params.get(name):
                    return ""
                # The emitted function's declared result.
                return self._func_return_go_types.get(name, "")
            return ""
        if isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) >= 2:
            receiver = callee.children[0]
            method = self._get_name(callee.children[1])
            if self._certain_go_type(receiver) == "string":
                from compiler.constants import STR_METHODS, STR_METHOD_RESULT_TYPES
                if method in STR_METHODS:
                    # ``s.toLower()``, ``sep.join(parts)``: ``lamstrings``.
                    return self._CERTAIN_LAM_SCALARS.get(STR_METHOD_RESULT_TYPES.get(method, "str"), "")
                return ""
            if isinstance(receiver, Tree) and receiver.data == "var" and receiver.children:
                obj_name = self._get_name(receiver.children[0])
                if (
                    obj_name in self._class_names and obj_name not in self.declared_vars
                    and obj_name not in self._var_go_types
                    and method in self._static_methods.get(obj_name, set())
                ):
                    # ``ImageKeys.key(entry, size)``: a static method.
                    return self._certain_method_go_type(obj_name, method)
            cls = self._certain_class(receiver)
            if cls and not any(
                field_name == method
                for owner in [cls, *self._hier_ancestors(cls)]
                for field_name, _type in self.class_fields.get(owner, [])
            ):
                # ``user.displayName()`` (not a field holding a function).
                return self._certain_method_go_type(cls, method)
        return ""

    def _fstring_concat_to_go(self, parts) -> "Optional[str]":
        """An f-string whose slots are all ``str`` / ``int`` / ``bool`` /
        ``html`` / ``xml`` / ``json`` without format specs, as a string
        concatenation (one exact-size allocation instead of fmt's boxing,
        buffer and copy). A slot whose type is certain
        (``_certain_go_type``) is used as is (a ``str``) or converted with
        ``strconv`` (an ``int`` / ``bool``, the text ``%v`` gives); a slot
        hinted ``str`` but not certain (an unannotated local, ...) goes
        through ``lamSlotText`` (the string itself, or fmt's ``%v`` of
        whatever it really holds); html / xml / json through
        ``lamValueText`` (lamjson: the value's own text). Any other slot
        keeps ``fmt.Sprintf`` (``None``)."""
        pieces = []
        for part in parts:
            if isinstance(part, str):
                if part:
                    pieces.append(f'"{part}"')
                continue
            if part.has_spec:
                return None
            slot = self._fstring_slot_tree(part.expr.strip())
            if slot is None:
                return None
            nav = self._fstring_slot_result_nav(slot)
            certain = self._certain_go_type(nav)
            if certain in {"string", "int", "bool"}:
                pieces.append((part, certain))
                continue
            hint = self._expr_go_type_hint(nav)
            if hint in self._TEXT_VALUE_GO_TYPES:
                pieces.append((part, "text"))
            elif hint == "string":
                pieces.append((part, "slot"))
            else:
                return None
        # Every slot qualifies: lower each one once, left to right.
        out = []
        for piece in pieces:
            if isinstance(piece, str):
                out.append(piece)
                continue
            part, kind = piece
            value = self._transform_fstring_expr(part.expr)
            if kind == "string":
                out.append(value)
            elif kind == "int":
                self._need_import("strconv")
                out.append(f"strconv.Itoa({value})")
            elif kind == "bool":
                self._need_import("strconv")
                out.append(f"strconv.FormatBool({value})")
            elif kind == "slot":
                out.append(f"lamSlotText({value})")
            else:
                out.append(f"lamValueText({value})")
        return " + ".join(out) if out else '""'

    def _fstring_to_go(self, node: Tree) -> str:
        raw = str(node.children[0])
        split = fstring_body(raw)
        parts = split_fstring(split[0], split[1]) if split else None
        if parts is not None:
            concat = self._fstring_concat_to_go(parts)
            if concat is not None:
                return concat
        self._need_import("fmt")
        if parts is not None:
            # Slots may hold strings in the f-string's own quote, commas,
            # brackets (``compiler/fstrings.py``).
            fmt_str = ""
            go_args = []
            for part in parts:
                if isinstance(part, str):
                    fmt_str += part.replace("%", "%%")
                    continue
                spec = part.spec
                verb = fstring_spec_to_go(spec)[0] if part.has_spec else "%v"
                if verb and (not part.has_spec or self._fstring_expr_parses(part.expr)):
                    # ``{x:.2f}`` / ``{x:>5}`` / ``{x:}``: the Go verb.
                    fmt_str += verb
                    arg = self._transform_fstring_expr(part.expr)
                    if part.has_spec and verb[-1:] in ("f", "F", "e", "E", "g", "G"):
                        # ``{n:.1f}`` of an int formats it as a float, as in
                        # Python (Go prints ``%!f(int=23)``).
                        slot = self._fstring_slot_tree(part.expr.strip())
                        slot_type = (self._expr_go_type_hint(slot) or self._infer_type_from_value(slot) or "") if slot is not None else ""
                        if slot_type in self._NUMERIC_GO_TYPES and slot_type not in ("float64", "float32"):
                            arg = f"float64({arg})"
                    go_args.append(arg)
                elif part.has_spec and spec and (spec[-1] in self._FSTRING_SPEC_VERBS or spec.startswith('.')):
                    fmt_str += f"%{spec}"
                    go_args.append(self._transform_fstring_expr(part.expr))
                else:
                    expr = part.expr + (":" + spec if part.has_spec else "")
                    fmt_str += "%v"
                    go_args.append(self._transform_fstring_expr(expr))
            if go_args:
                return f'fmt.Sprintf("{fmt_str}", {", ".join(go_args)})'
            return f'"{fmt_str}"'
        # Triple-quoted ``f"""..."""`` must be tested *before* the
        # single-quoted forms — every triple-quoted literal also
        # starts with ``f"``/``f'`` so the shorter prefix would win
        # and leave two quote characters dangling on each end, which
        # then leak into the emitted Go format string.
        raw = raw.replace(SOURCE_NEWLINE, "")
        if raw.startswith('f"""') or raw.startswith("f'''"):
            raw = raw[4:-3]
        elif raw.startswith("f'") or raw.startswith('f"'):
            raw = raw[2:-1]

        fmt_str = ""
        go_args = []
        i = 0
        while i < len(raw):
            if raw[i] == '{':
                if i + 1 < len(raw) and raw[i+1] == '{':
                    fmt_str += "{"
                    i += 2
                    continue
                depth = 0
                j = i
                while j < len(raw):
                    if raw[j] == '{':
                        depth += 1
                    elif raw[j] == '}':
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                expr_str = raw[i+1:j]
                # ``f"{table[\"key\"]}"``: inside an interpolation the
                # escaped delimiter is just a quote of the expression.
                expr_str = expr_str.replace('\\"', '"').replace("\\'", "'")
                if ':' in expr_str:
                    last_colon = expr_str.rfind(':')
                    spec_part = expr_str[last_colon+1:]
                    if spec_part and (spec_part[-1] in 'fdseEgGoxXbcn%' or spec_part.startswith('.')):
                        expr_part = expr_str[:last_colon]
                        spec = spec_part
                        fmt_str += f"%{spec}"
                        go_args.append(self._transform_fstring_expr(expr_part))
                    else:
                        fmt_str += "%v"
                        go_args.append(self._transform_fstring_expr(expr_str))
                else:
                    fmt_str += "%v"
                    go_args.append(self._transform_fstring_expr(expr_str))
                i = j + 1
            elif raw[i] == '}':
                if i + 1 < len(raw) and raw[i+1] == '}':
                    fmt_str += "}"
                    i += 2
                    continue
                fmt_str += raw[i]
                i += 1
            else:
                if raw[i] == '%':
                    fmt_str += "%%"
                else:
                    fmt_str += raw[i]
                i += 1

        if go_args:
            args_str = ", ".join(go_args)
            return f'fmt.Sprintf("{fmt_str}", {args_str})'
        else:
            return f'"{fmt_str}"'

    # ─── Lambda ────────────────────────────────────────────────

    _ORDERED_GO_TYPES = frozenset({
        "int", "int8", "int16", "int32", "int64",
        "uint", "uint8", "uint16", "uint32", "uint64",
        "float32", "float64", "string",
    })

    # Ordering of values whose static type is not an ordered Go type
    # (``any``, ``json``, a type parameter): by kind at
    # run time. Numbers compare numerically (ints exactly, a float with
    # anything numerically, NaN first), strings by bytes, ``false <
    # true``; values of different kinds order as None < bool < number <
    # str < other, other values by their ``%v`` text. ``json`` values are
    # seen through (``LamMatchRaw``), set pointers are dereferenced.
    _DYNAMIC_COMPARE_GO = (
        "func(a, b interface{}) int { "
        "key := func(v interface{}) (int, interface{}) { "
        "if raw, ok := v.(interface{ LamMatchRaw() interface{} }); ok { v = raw.LamMatchRaw() }; "
        "rv := reflect.ValueOf(v); "
        "for rv.IsValid() && (rv.Kind() == reflect.Pointer || rv.Kind() == reflect.Interface) && !rv.IsNil() && rv.Elem().Kind() != reflect.Struct { rv = rv.Elem() }; "
        "if !rv.IsValid() || ((rv.Kind() == reflect.Pointer || rv.Kind() == reflect.Interface) && rv.IsNil()) { return 0, nil }; "
        "switch rv.Kind() { "
        "case reflect.Bool: return 1, rv.Bool(); "
        "case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64: return 2, rv.Int(); "
        "case reflect.Uint, reflect.Uint8, reflect.Uint16, reflect.Uint32, reflect.Uint64, reflect.Uintptr: return 2, rv.Uint(); "
        "case reflect.Float32, reflect.Float64: return 2, rv.Float(); "
        "case reflect.String: return 3, rv.String() }; "
        "return 4, fmt.Sprintf(\"%v\", v) }; "
        "ka, va := key(a); kb, vb := key(b); "
        "if ka != kb { if ka < kb { return -1 }; return 1 }; "
        "switch x := va.(type) { "
        "case bool: if y := vb.(bool); x == y { return 0 } else if !x { return -1 }; return 1; "
        "case string: y := vb.(string); if x < y { return -1 } else if x > y { return 1 }; return 0; "
        "case int64: if y, ok := vb.(int64); ok { if x < y { return -1 } else if x > y { return 1 }; return 0 }; "
        "if y, ok := vb.(uint64); ok { if x < 0 || uint64(x) < y { return -1 } else if uint64(x) > y { return 1 }; return 0 }; "
        "case uint64: if y, ok := vb.(uint64); ok { if x < y { return -1 } else if x > y { return 1 }; return 0 }; "
        "if y, ok := vb.(int64); ok { if y < 0 || x > uint64(y) { return 1 } else if x < uint64(y) { return -1 }; return 0 } }; "
        "num := func(v interface{}) float64 { switch n := v.(type) { case int64: return float64(n); case uint64: return float64(n); case float64: return n }; return 0 }; "
        "x, y := num(va), num(vb); "
        "if x != x || y != y { if x != x && y != y { return 0 } else if x != x { return -1 }; return 1 }; "
        "if x < y { return -1 } else if x > y { return 1 }; return 0 }"
    )

    def _reversed_to_go(self, arg) -> str:
        """``reversed(xs)``: a reversed copy of a list (same slice type); for
        a ``str``, its characters (runes) last first as a ``[]string``, so
        ``"".join(reversed(s))`` and ``for ch in reversed(s)`` work as in
        Python. The argument is evaluated once and never modified."""
        go_type = self._reversed_source_go_type(arg)
        source = self._typed_value_to_go(arg, go_type) if go_type.startswith("[]") else self._expr_to_go(arg)
        if go_type == "string":
            return (
                f"func(src string) []string {{ rs := []rune(src); out := make([]string, len(rs)); "
                f"for i, r := range rs {{ out[len(rs)-1-i] = string(r) }}; return out }}({source})"
            )
        if go_type.startswith("[]"):
            return (
                f"func(src {go_type}) {go_type} {{ out := make({go_type}, len(src)); "
                f"for i, v := range src {{ out[len(src)-1-i] = v }}; return out }}({source})"
            )
        self._need_import("reflect")
        return (
            "func(src interface{}) []interface{} { v := reflect.ValueOf(src); "
            "if v.Kind() == reflect.String { rs := []rune(v.String()); out := make([]interface{}, len(rs)); "
            "for i, r := range rs { out[len(rs)-1-i] = string(r) }; return out }; "
            "n := v.Len(); out := make([]interface{}, n); "
            "for i := 0; i < n; i++ { out[n-1-i] = v.Index(i).Interface() }; return out }"
            f"({source})"
        )

    def _reversed_source_go_type(self, arg) -> str:
        return self._expr_go_type_hint(arg) or self._infer_type_from_value(arg) or ""

    def _dynamic_compare_go(self) -> str:
        """A Go ``func(a, b interface{}) int`` ordering values by kind
        (``_DYNAMIC_COMPARE_GO``)."""
        self._need_import("fmt")
        self._need_import("reflect")
        return self._DYNAMIC_COMPARE_GO

    def _orders_dynamically(self, go_type: str) -> bool:
        """Values of ``go_type`` are ordered by kind at run time: ``any``,
        ``json``, a type parameter or a type the compiler could not
        determine."""
        t = (go_type or "").strip()
        if t in ("", "interface{}", "any", "LamJSON"):
            return True
        return t in (getattr(self, "_generic_names", None) or ())

    def _sort_callback_arity(self, node) -> int:
        """Parameter count of a ``sort`` callback (lambda or function name);
        ``0`` when unknown."""
        if isinstance(node, Tree) and node.data == "lambdef":
            params_node = next(
                (c for c in node.children if isinstance(c, Tree) and c.data in ("paren_lambda_params", "inline_lambda_params")),
                None,
            )
            return len(self._collect_lambda_params(params_node))
        if isinstance(node, Tree) and node.data == "var" and node.children:
            name = self._get_name(node.children[0])
            if name in self._user_functions:
                return self._func_param_counts.get(name, 0)
        return 0

    @staticmethod
    def _split_go_func_type(go_type: str):
        """``func(int, string) bool`` -> ``(["int", "string"], "bool")``;
        ``func()`` -> ``([], "")``. ``None`` for shapes we do not model
        (multiple results)."""
        go_type = go_type.strip()
        if not go_type.startswith("func("):
            return None
        depth = 0
        params: list = []
        cur = ""
        end = -1
        for i in range(5, len(go_type)):
            ch = go_type[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0 and ch == ")":
                    end = i
                    break
                depth -= 1
            if ch == "," and depth == 0:
                params.append(cur.strip())
                cur = ""
                continue
            cur += ch
        if end < 0:
            return None
        if cur.strip():
            params.append(cur.strip())
        ret = go_type[end + 1:].strip()
        if ret.startswith("("):
            return None
        return params, ret

    def _lambda_to_go(self, node: Tree, param_types=None, return_type=None,
                      contextual: bool = False) -> str:
        # A lambda's parameters and locals are narrowable even inside
        # module-level statements.
        # A lambda may run after the region that narrowed a captured
        # reference to non-``None``: its members are checked again.
        with self._scoped(_nr_module_code=False, _nonnil_names=set()):
            return self._lambda_to_go_body(node, param_types, return_type, contextual)

    def _lambda_to_go_body(self, node: Tree, param_types=None, return_type=None,
                           contextual: bool = False) -> str:
        """Transpile a lambda literal to a Go function value.

        ``param_types`` — optional list of Go types used to fill in any
        params that lack an annotation. Supplied by callers that know
        the lambda's context (e.g. ``.map(lambda x: x * 2)`` on a
        ``list[int]`` receiver).

        ``return_type`` — optional Go return type. When absent we use
        ``interface{}`` unless the body and param types let us infer a
        concrete type.

        The grammar admits four shapes — bare-untyped (``lambda x: x``),
        paren-typed (``lambda (x: int, y: float): x + y``), explicit
        return-type (``lambda (x) -> int: x``), and brace-delimited
        block body (``lambda (x: int) { return x * 2 }``). All four
        funnel through here.
        """
        params_node = None
        return_anno = None
        body_node = None
        for child in node.children:
            if isinstance(child, Tree):
                if child.data in ("paren_lambda_params", "inline_lambda_params"):
                    params_node = child
                elif child.data == "lambda_return_anno":
                    return_anno = child
                else:
                    # Anything else is the body. ``suite`` for block
                    # bodies; any expression node otherwise.
                    body_node = child
            elif child is not None:
                body_node = child

        # Collect (name, declared_go_type_or_None) pairs so we can
        # backfill from hints without losing user-supplied annotations.
        raw_params = self._collect_lambda_params(params_node)

        hints = list(param_types) if param_types else []
        params = []
        for i, (name, declared) in enumerate(raw_params):
            if declared:
                go_type = declared
            elif i < len(hints) and hints[i]:
                go_type = hints[i]
            else:
                go_type = "interface{}"
            params.append(f"{self._go_binding_name(name)} {go_type}")

        # An explicit ``-> Type`` annotation on the lambda always wins
        # over the contextual hint and inference.
        explicit_return = None
        return_key = ""
        if return_anno is not None and return_anno.children:
            explicit_return = self._type_expr_to_go(return_anno.children[0])
            # ``-> Result[list[Item]]``: ``Result.Ok([...])`` takes the payload type.
            return_key = self._overload_return_key(return_anno.children[0])

        # Teach the body visitor about the lambda's parameter types so
        # uses of those names compile to the right Go type (no more
        # `x.(int) * 2` shims for pure arithmetic).
        saved_vars = dict(self._var_go_types)
        saved_payloads = dict(self._var_result_payloads)
        saved_declared = set(self.declared_vars)
        saved_classes = dict(self._var_types)
        for (name, _), p in zip(raw_params, params):
            go_ty = p.split(" ", 1)[1]
            self._var_go_types[name] = go_ty
            self.declared_vars.add(name)
            class_name = go_ty[1:].split("[", 1)[0] if go_ty.startswith("*") else ""
            matches = [c for c in self._class_names if class_name and self._go_class_name(c) == class_name]
            if matches:
                self._var_types[name] = matches[0]
            else:
                self._var_types.pop(name, None)
        for child in (params_node.children if isinstance(params_node, Tree) else []):
            if isinstance(child, Tree) and child.data == "typed_lambda_param" and len(child.children) > 1:
                self._note_result_payload(self._get_name(child.children[0]), child.children[1])

        is_block_body = isinstance(body_node, Tree) and body_node.data == "suite"
        if not is_block_body and isinstance(body_node, Tree):
            # ``f()?.size()`` in an expression body (no statement, so not
            # rewritten by ``_visit``): propagation, then member access.
            from compiler.safe_nav import rewrite_safe_nav_as_propagation
            rewrite_safe_nav_as_propagation(body_node)

        # The default return type stays ``interface{}`` for
        # compatibility: existing callers rely on the lambda result
        # being type-asserted to a concrete Go type (e.g. ``.map`` does
        # ``fn(_v).(int)``). Return-type inference only kicks in when
        # an explicit hint has been provided *or* when the caller has
        # also supplied ``param_types``, which signals an inference-
        # friendly context like ``.map``/``.filter``.
        if explicit_return is not None:
            resolved_return = explicit_return
        elif return_type is not None and (return_type or contextual):
            resolved_return = return_type
        elif not is_block_body and self._is_void_call_expr(body_node):
            # ``lambda: print("tick")``: the body has no value, so the
            # lambda is a ``func(...)`` without a result.
            resolved_return = ""
        elif is_block_body:
            # Block bodies always need an explicit return type from the
            # caller or the user. We default to interface{} so callers
            # can still receive a value via type assertion.
            resolved_return = "interface{}"
        elif param_types:
            resolved_return = self._infer_lambda_return_type(body_node, params)
        else:
            resolved_return = "interface{}"

        void_body = resolved_return == ""
        # ``?`` in the body returns the ``Err`` from the lambda when it
        # returns a ``Result`` (and panics otherwise), never from the
        # enclosing function or ``do`` block.
        boundary = dict(self._callable_boundary_context(),
                        _q_propagate_ok=(resolved_return == "*Result"),
                        _guard_q_var="")
        # The statements an expression body needs first (the guard of a
        # ``?``) belong inside the lambda, which then gets a block body.
        prelude: list = []
        # A ``try`` in a block body lowers to an IIFE: as in a function,
        # its ``return`` (and the guard of a ``?``) sets the named result
        # ``retval`` and leaves the IIFE, and the lambda returns it.
        try_result = bool(
            is_block_body and resolved_return
            and self._suite_contains_try(body_node)
        )
        try:
            if is_block_body:
                if try_result:
                    self._declare_try_results(resolved_return)
                with self._scoped(_current_return_type=resolved_return,
                                  _current_return_type_key=return_key or resolved_return,
                                  **{**boundary, "_in_try_func": try_result}):
                    body_src = self._lambda_block_body_to_go(body_node)
            else:
                with self._capture_emits(prelude), self._scoped(
                        _current_return_type=resolved_return,
                        _current_return_type_key=return_key or resolved_return,
                        **boundary):
                    if void_body:
                        # ``func()`` context: the body is evaluated for its effect.
                        body_src = self._expr_to_go(body_node)
                    elif return_key and return_key != resolved_return:
                        with self._scoped(_expected_expr_type_key=return_key):
                            body_src = self._typed_value_to_go(body_node, resolved_return, return_key)
                    else:
                        body_src = self._typed_value_to_go(body_node, resolved_return)
        finally:
            self._var_go_types = saved_vars
            self._var_result_payloads = saved_payloads
            self.declared_vars = saved_declared
            self._var_types = saved_classes

        params_str = ", ".join(params)
        ret_part = f" {resolved_return}" if resolved_return else ""
        if is_block_body:
            if try_result:
                return f"func({params_str}) {self._try_result_sig(resolved_return)} {{\n{body_src}\n\treturn\n}}"
            return f"func({params_str}){ret_part} {{\n{body_src}\n}}"
        if prelude:
            lines = "\n".join("\t" + line for line in prelude)
            if void_body:
                tail = body_src if isinstance(body_node, Tree) and body_node.data in {"funccall", "await_expr"} else f"_ = {body_src}"
            else:
                tail = f"return {body_src}"
            return f"func({params_str}){ret_part} {{\n{lines}\n\t{tail}\n}}"
        if void_body:
            if isinstance(body_node, Tree) and body_node.data in {"funccall", "await_expr"}:
                return f"func({params_str}) {{ {body_src} }}"
            return f"func({params_str}) {{ _ = {body_src} }}"
        return f"func({params_str}){ret_part} {{ return {body_src} }}"

    def _raw_call_kwargs(self, args_node) -> dict:
        """``{name: value_node}`` for the keyword arguments of a call."""
        out: dict = {}
        if not isinstance(args_node, Tree):
            return out
        stack = [args_node]
        while stack:
            cur = stack.pop()
            for child in cur.children:
                if not isinstance(child, Tree):
                    continue
                if child.data == "argvalue" and len(child.children) == 2:
                    name_node = child.children[0]
                    if isinstance(name_node, Tree) and name_node.data == "var" and name_node.children:
                        out[self._get_name(name_node.children[0])] = child.children[1]
                elif child.data in {"arguments", "starargs", "kwargs"}:
                    stack.append(child)
        return out

    @staticmethod
    def _is_none_literal(node) -> bool:
        if isinstance(node, Tree) and node.data == "const_none":
            return True
        return (
            isinstance(node, Tree) and node.data == "var" and bool(node.children)
            and str(node.children[0].children[0] if isinstance(node.children[0], Tree) else node.children[0]) in {"None", "nil"}
        )

    def _display_go(self, value_go: str, node) -> str:
        """A value shown by ``print`` / f-strings: ``bytes`` show their text,
        like ``str(data)``."""
        hint = self._expr_go_type_hint(node) if isinstance(node, Tree) else ""
        if hint == "[]byte":
            return f"string({value_go})"
        return value_go

    def _is_void_call_expr(self, node) -> bool:
        """``print(...)`` or a call to a user function declared without a
        return type: an expression that produces no value."""
        if not (isinstance(node, Tree) and node.data == "funccall" and node.children):
            return False
        callee = node.children[0]
        if not (isinstance(callee, Tree) and callee.data == "var" and callee.children):
            return False
        name = self._get_name(callee.children[0])
        if name in self._var_go_types:
            return False
        if name == "print":
            return name not in self._user_functions
        return (
            name in self._user_functions
            and name not in self._class_names
            and not self._func_return_go_types.get(name)
            and len(self._overload_variants.get(name, ())) <= 1
            and name not in getattr(self, "_nested_generic_functions", set())
        )

    def _lambda_go_func_type(self, node: Tree) -> str:
        """``func(int) string`` for a lambda whose parameters are all typed
        (``lambda (n: int) -> str { ... }``); ``""`` otherwise."""
        params_node = next(
            (c for c in node.children if isinstance(c, Tree) and c.data in ("paren_lambda_params", "inline_lambda_params")),
            None,
        )
        raw_params = self._collect_lambda_params(params_node)
        if any(not declared for _name, declared in raw_params):
            return ""
        param_types = [declared for _name, declared in raw_params]
        anno = next((c for c in node.children if isinstance(c, Tree) and c.data == "lambda_return_anno"), None)
        body = next(
            (c for c in node.children
             if isinstance(c, Tree) and c.data not in ("paren_lambda_params", "inline_lambda_params", "lambda_return_anno")),
            None,
        )
        if anno is not None and anno.children:
            ret = self._type_expr_to_go(anno.children[0]) or ""
        elif isinstance(body, Tree) and body.data == "suite":
            return ""
        elif self._is_void_call_expr(body):
            ret = ""
        else:
            ret = self._lambda_result_go_type(node, param_types)
            if not ret or ret == "interface{}":
                return ""
        joined = ", ".join(param_types)
        return f"func({joined}) {ret}" if ret else f"func({joined})"

    def _lambda_result_go_type(self, node: Tree, param_go_types) -> str:
        """Go type of an expression-bodied lambda's result when its untyped
        parameters take ``param_go_types``; ``interface{}`` if unknown."""
        params_node = None
        body_node = None
        for child in node.children:
            if isinstance(child, Tree) and child.data in ("paren_lambda_params", "inline_lambda_params"):
                params_node = child
            elif isinstance(child, Tree) and child.data == "lambda_return_anno":
                if child.children:
                    return self._type_expr_to_go(child.children[0]) or "interface{}"
            elif child is not None:
                body_node = child
        if not isinstance(body_node, Tree) or body_node.data == "suite":
            return "interface{}"
        raw_params = self._collect_lambda_params(params_node)
        saved_go = dict(self._var_go_types)
        saved_cls = dict(self._var_types)
        typed = []
        try:
            for i, (name, declared) in enumerate(raw_params):
                go_type = declared or (param_go_types[i] if i < len(param_go_types) else "") or "interface{}"
                typed.append(f"{name} {go_type}")
                self._var_go_types[name] = go_type
                class_name = go_type[1:].split("[", 1)[0] if go_type.startswith("*") else ""
                matches = [c for c in self._class_names if self._go_class_name(c) == class_name]
                if matches:
                    self._var_types[name] = matches[0]
                else:
                    self._var_types.pop(name, None)
            hint = self._expr_go_type_hint(body_node)
            if hint and hint != "interface{}":
                return hint
            inferred = self._infer_lambda_return_type(body_node, typed)
            if inferred == "interface{}" and body_node.data in {"list", "dict", "set", "tuple"}:
                # ``lambda n: [n, n * 2]`` is a ``[]int``.
                literal = self._infer_type_from_value(body_node)
                if literal:
                    return literal
            return inferred
        finally:
            self._var_go_types = saved_go
            self._var_types = saved_cls

    def _collect_lambda_params(self, params_node):
        """Extract ``(name, go_type_or_None)`` pairs from a lambda's
        parameter node. Handles both the paren-wrapped (typed) form
        and the bare comma-list (untyped) form."""
        out = []
        if params_node is None:
            return out
        if not isinstance(params_node, Tree):
            return out
        for child in params_node.children:
            if child is None:
                continue
            if isinstance(child, Tree):
                if child.data == "typed_lambda_param":
                    name = self._get_name(child.children[0])
                    declared = None
                    if len(child.children) > 1:
                        declared = self._type_expr_to_go(child.children[1])
                    out.append((name, declared))
                elif child.data == "inline_lambda_param":
                    name = self._get_name(child.children[0])
                    out.append((name, None))
                elif child.data == "name":
                    out.append((self._get_name(child), None))
            elif isinstance(child, Token):
                # Bare ``NAME`` token, possible when the inline-param
                # rule's single ``name`` child collapsed under ``?``.
                out.append((str(child), None))
        return out

    def _lambda_block_body_to_go(self, suite_node) -> str:
        """Render a multi-line lambda body (a ``suite`` of statements)
        as Go source. Reuses the statement visitor so every Lam
        construct that works in a function body — ``if``/``for``/
        ``return``/...— works here too."""
        # Snapshot + isolate the line buffer so we can capture only
        # the suite's emitted lines. The buffer-swap pattern is the
        # same one used by inline ``go!`` blocks.
        saved_lines = self.output_lines
        saved_indent = self.indent
        self.output_lines = []
        self.indent = 1
        try:
            self._visit_suite(suite_node)
            body = "\n".join(self.output_lines)
        finally:
            self.output_lines = saved_lines
            self.indent = saved_indent
        return body

    # Heuristic inference for a lambda's return type when the caller
    # can't supply one. We walk the body for simple patterns: comparisons
    # return bool, arithmetic over homogenous numeric params keeps their
    # type, string concatenation yields string.
    def _infer_lambda_return_type(self, body_node, params) -> str:
        if not isinstance(body_node, Tree):
            return "interface{}"
        param_types = {}
        for p in params:
            name, ty = p.split(" ", 1)
            param_types[name] = ty
        if body_node.data in ("string", "fstring", "string_concat"):
            return "string"
        if body_node.data in ("const_true", "const_false"):
            return "bool"
        if body_node.data == "number":
            for child in body_node.children:
                if isinstance(child, Token) and child.type in {"FLOAT_NUMBER", "IMAG_NUMBER"}:
                    return "float64"
            return "int"
        if body_node.data == "funccall" and body_node.children:
            callee = body_node.children[0]
            if isinstance(callee, Tree) and callee.data == "var" and callee.children:
                name = self._get_name(callee.children[0])
                if name in {"str", "string"}:
                    return "string"
                if name == "int":
                    return "int"
                if name == "float":
                    return "float64"
                if name == "bool":
                    return "bool"
                ret = self._func_return_go_types.get(name, "")
                if ret:
                    return ret
        # Comparison / boolean-producing nodes → bool.
        if body_node.data in (
            "comparison", "and_test", "or_test", "not_test",
            "const_true", "const_false",
        ):
            return "bool"
        # Arithmetic: if every ``var`` leaf resolves to the same numeric
        # type we return that type.
        if body_node.data == "term" and "/" in {str(c) for c in body_node.children[1::2]}:
            return "float64"  # Python's ``/`` is a float division
        if body_node.data in ("arith_expr", "term", "factor"):
            numeric = {"int", "int8", "int16", "int32", "int64",
                       "uint", "uint8", "uint16", "uint32", "uint64",
                       "float32", "float64"}
            seen = set()
            def _walk(n):
                if isinstance(n, Tree):
                    if n.data == "var":
                        name = self._get_name(n.children[0])
                        t = param_types.get(name)
                        if t and t in numeric:
                            seen.add(t)
                    elif n.data == "number":
                        seen.add("int")
                    elif n.data == "float_number":
                        seen.add("float64")
                    for c in n.children:
                        _walk(c)
            _walk(body_node)
            if len(seen) == 1:
                return next(iter(seen))
            if seen and seen.issubset({"int", "float64"}):
                return "float64"
        return "interface{}"

    def _callable_return_elem_go_type(self, node) -> str:
        if not isinstance(node, Tree):
            return ""
        if node.data == "argvalue" and node.children:
            return self._callable_return_elem_go_type(node.children[0])
        if node.data == "var" and node.children:
            return self._func_return_go_types.get(self._get_name(node.children[0]), "")
        return ""

    def _is_lambda_node(self, node) -> bool:
        return isinstance(node, Tree) and node.data == "lambdef"

    def _typed_result_ok_to_go(self, node, go_type: str, type_key: str) -> str:
        """``Result.Ok(arg)`` where the target is ``Result[T]``: shape ``arg``
        as ``T`` so collection literals and comprehensions get concrete Go
        types instead of ``[]interface{}`` / ``map[string]interface{}``.
        """
        if go_type != "*Result" or not type_key.startswith("*Result[") or not type_key.endswith("]"):
            return ""
        payload = type_key[len("*Result["):-1]
        if payload in {"", "interface{}"}:
            return ""
        if not isinstance(node, Tree) or node.data != "funccall" or len(node.children) < 2:
            return ""
        callee = node.children[0]
        if (
            not isinstance(callee, Tree)
            or callee.data != "getattr"
            or len(callee.children) < 2
            or self._get_name(callee.children[1]) != "Ok"
            or not isinstance(callee.children[0], Tree)
            or callee.children[0].data != "var"
            or self._get_name(callee.children[0].children[0]) != "Result"
        ):
            return ""
        args_node = node.children[1]
        if not isinstance(args_node, Tree) or args_node.data != "arguments":
            return ""
        values = [child for child in args_node.children if isinstance(child, Tree)]
        if len(values) != 1:
            return ""
        value = values[0]
        if value.data == "argvalue":
            if len(value.children) != 1 or not isinstance(value.children[0], Tree):
                return ""
            value = value.children[0]
        ok_func = f"{self._go_class_name('Result')}_Ok"
        if value.data not in {
            "list", "dict", "set", "list_comprehension", "dict_comprehension",
            "set_comprehension", "test",
        }:
            converted = self._result_ok_payload_conversion(value, payload)
            return f"{ok_func}({converted})" if converted else ""
        return f"{ok_func}({self._typed_value_to_go(value, payload)})"

    @staticmethod
    def _holds_propagate(node) -> bool:
        return isinstance(node, Tree) and any(True for _ in node.find_data("propagate"))

    def _short_circuit_propagate_to_go(self, node) -> str | None:
        """``a and f()?`` / ``a or f()?``: a ``?`` after the first operand
        lowers to statements (call, early return), which must run only when
        the left side does not decide the result (they used to run first,
        always)."""
        operands = [c for c in node.children if isinstance(c, Tree)]
        if len(operands) < 2 or not any(self._holds_propagate(c) for c in operands[1:]):
            return None
        self._q_temp_counter += 1
        tmp = f"__lamSc{self._q_temp_counter}"
        self._emit(f"{tmp} := bool({self._expr_to_go(operands[0])})")
        self._declare_var(tmp)
        guard = tmp if node.data == "and_test" else f"!{tmp}"
        for operand in operands[1:]:
            self._emit(f"if {guard} {{")
            self.indent += 1
            with self._scoped(_propagate_cast_hint="bool"):
                part = self._expr_to_go(operand)
            self._emit(f"{tmp} = bool({part})")
            self.indent -= 1
            self._emit("}")
        return tmp

    def _ternary_propagate_to_go(self, node) -> str | None:
        """``f()? if c else x``: the branch holding ``?`` runs only when chosen."""
        true_node, cond_node, false_node = node.children
        if not (self._holds_propagate(true_node) or self._holds_propagate(false_node)):
            return None
        common = self._ternary_common_go_type(node) or self._propagate_cast_hint or "interface{}"
        cond = self._expr_to_go(cond_node)
        self._q_temp_counter += 1
        tmp = f"__lamTe{self._q_temp_counter}"
        self._emit(f"var {tmp} {common}")
        self._declare_var(tmp)
        self._emit(f"if {cond} {{")
        self.indent += 1
        with self._scoped(_propagate_cast_hint=common if common != "interface{}" else ""):
            value = self._typed_value_to_go(true_node, common) if common != "interface{}" else self._expr_to_go(true_node)
        self._emit(f"{tmp} = {value}")
        self.indent -= 1
        self._emit("} else {")
        self.indent += 1
        with self._scoped(_propagate_cast_hint=common if common != "interface{}" else ""):
            value = self._typed_value_to_go(false_node, common) if common != "interface{}" else self._expr_to_go(false_node)
        self._emit(f"{tmp} = {value}")
        self.indent -= 1
        self._emit("}")
        return tmp

    def _retyped_lowered_arg(self, node, lowered: str, go_type: str) -> str:
        """``node`` (already lowered to ``lowered``) as a ``go_type`` value.
        An argument holding ``?`` is not lowered again: that would emit its
        call and early return a second time (``xs.append(f()?)`` called
        ``f`` twice)."""
        if isinstance(node, Tree) and any(True for _ in node.find_data("propagate")):
            if lowered.endswith(".Value") and go_type and go_type != "interface{}":
                return self._result_payload_assert_go(lowered, go_type)
            if go_type == "LamJSON":
                # ``rows.append({"doc": Json.decode(text)?})``: the literal
                # was lowered untyped; lamJSONMust normalises it.
                return lowered if self._expr_go_type_hint(node) == "LamJSON" else f"lamJSONMust({lowered})"
            return lowered
        return self._typed_value_to_go(node, go_type)

    def _is_dynamic_any_value(self, node) -> bool:
        """``node`` is statically an ``any`` (Go ``interface{}``) value: an
        ``any`` name, an element of a collection of ``any``, an ``any``
        field or the result of a callable declared ``-> any`` (or a generic
        result bound to ``any``)."""
        if not isinstance(node, Tree) or not node.children:
            return False
        if node.data == "cast_expr" and len(node.children) == 2:
            # ``v as any`` where a concrete type is expected.
            return self._type_expr_to_go(node.children[1]) == "interface{}"
        if node.data == "var":
            return self._var_go_types.get(self._get_name(node.children[0])) == "interface{}"
        if node.data == "getitem":
            if isinstance(node.children[1], Tree) and node.children[1].data == "slice":
                return False
            container = (self._expr_go_type_hint(node.children[0]) or "").strip()
            if container.startswith("[]"):
                return container[2:] == "interface{}"
            if container.startswith("map["):
                return self._split_go_map_type(container)[1] == "interface{}"
            return False
        if node.data == "getattr":
            return (self._expr_go_type_hint(node) or "").strip() == "interface{}"
        if node.data == "null_coalesce":
            # ``n ?? -1`` with ``n: any``: an ``interface{}`` either way.
            operands = [c for c in node.children if c is not None]
            if len(operands) < 2:
                return False
            kind, result_type = self._coalesce_plan(
                operands[0], self._coalesce_fallback_node(node, operands), len(operands),
            )
            return kind != "json" and result_type in {"", "interface{}"}
        if node.data == "funccall":
            if self._consumes_result_call(node):
                return False
            return (
                (self._expr_go_type_hint(node) or "").strip() == "interface{}"
                or self._call_returns_any(node)
            )
        return False

    def _call_returns_any(self, node: Tree) -> bool:
        """A call of a function, nested function, function-typed local
        (lambda), method or static method declared ``-> any``, or of a
        generic function whose result is bound to ``any``."""
        callee = node.children[0]
        if isinstance(callee, Tree) and callee.data == "var" and callee.children:
            name = self._get_name(callee.children[0])
            local = (self._var_go_types.get(name) or "").strip()
            if local.startswith("func("):
                split = self._split_go_func_type(local)
                return split is not None and (split[1] or "").strip() == "interface{}"
            if name in self._class_names or len(self._overload_variants.get(name, ())) > 1:
                return False
            ret = (self._func_return_go_types.get(name) or "").strip()
            generic_params = sorted(self._func_generic_params.get(name, set()) or ())
            if ret and generic_params and mentions_names(ret, generic_params):
                args_node = node.children[1] if len(node.children) > 1 else None
                subst = self._generic_call_subst(name, args_node, generic_params)
                ret = substitute_go_type(ret, subst)
            return ret == "interface{}"
        if isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) >= 2:
            method = self._get_name(callee.children[1])
            receiver = callee.children[0]
            class_name = ""
            if isinstance(receiver, Tree) and receiver.data == "var" and receiver.children:
                candidate = self._get_name(receiver.children[0])
                if candidate in self._class_names and candidate not in self.declared_vars:
                    class_name = candidate
            class_name = class_name or self._infer_receiver_class(receiver)
            if not class_name or self._generic_classes.get(class_name):
                return False
            owner = self._hier_def_class(class_name, method) or class_name
            key = f"{owner}.{method}"
            if len(self._overload_variants.get(key, ())) > 1:
                return False
            return self._method_return_types.get(key, "") in {"any", "object"}
        return False

    _CAST_PRIMARY_NODES = frozenset({
        "var", "getattr", "getitem", "funccall", "number", "string",
        "const_none", "const_true", "const_false", "list", "dict", "set", "tuple", "cast_expr", "propagate",
    })

    def _cast_expr_to_go(self, node: Tree) -> str:
        """``expr as T``: the conversion an annotated assignment to ``T``
        performs (``_typed_value_to_go``), plus the checked downcast of a
        base-class or interface value to a class. The result is a Go
        primary expression, so operators around the cast need no
        parentheses."""
        value, type_node = node.children[0], node.children[1]
        target = self._type_expr_to_go(type_node)
        downcast = self._cast_downcast_to_go(value, target)
        if downcast:
            return downcast
        if (
            target not in {"", "interface{}"}
            and isinstance(value, Tree)
            and value.data not in {"const_none", "list", "dict", "set", "tuple", "propagate"}
            and not self._is_dynamic_any_value(value)
            and (self._expr_go_type_hint(value) or self._infer_type_from_value(value) or "").strip()
            in {"", "interface{}"}
        ):
            # A value the lowering cannot type (a comprehension variable over
            # a ``list[any]``, ...): the semantic checker accepted the cast,
            # so convert it at run time like any other ``any`` value.
            with self._scoped(_propagate_cast_hint=""):
                return self._dynamic_to_go(self._expr_to_go(value), target)
        raw = self._typed_value_to_go(value, target)
        if isinstance(value, Tree) and value.data not in self._CAST_PRIMARY_NODES and not (
            raw.startswith("(") and raw.endswith(")")
        ):
            raw = f"({raw})"
        return raw

    def _cast_downcast_to_go(self, value, target: str) -> str:
        """``base as Square`` / ``priced as Order``: the most-derived object
        behind a base-class (or interface) value viewed as ``target``; a
        value of another class throws ``TypeError``, ``None`` stays nil."""
        if not isinstance(value, Tree) or self._is_dynamic_any_value(value):
            return ""
        source = (self._hier_expr_go_type(value) or "").strip()
        if not source or source == target or source == "interface{}":
            return ""
        target_class = self._hier_class_from_go_type(target) if target.startswith("*") else ""
        target_iface = self._interface_from_go_type(target)
        source_class = self._hier_class_from_go_type(source) if source.startswith("*") else ""
        source_iface = self._interface_from_go_type(source)
        raw = self._expr_to_go(value)
        if source_class and target_class:
            if target_class == source_class or not self._hier_is_subclass(target_class, source_class):
                return ""
            dyn = (
                f"{self._hier_receiver(raw)}._lamDyn()"
                if self._hier_member(source_class) and not self._generic_classes.get(source_class)
                else self._hier_dyn_value_go(raw)
            )
            return self._cast_class_assert_go(dyn, target_class, target)
        if source_iface and target_class:
            return self._cast_class_assert_go(f"interface{{}}({raw})", target_class, target)
        if source_iface and target_iface and source_iface != target_iface:
            return (
                f"func(v interface{{}}) {target} {{ if v == nil {{ return nil }}; "
                f"return v.({target}) }}({raw})"
            )
        return ""

    def _cast_class_assert_go(self, dyn: str, target_class: str, target: str) -> str:
        if self._hier_dynamic_target(target_class):
            go_cls = self._go_class_name(target_class)
            return (
                f"func(v interface{{}}) {target} {{ if v == nil {{ return nil }}; "
                f"return v.({self._hier_is_iface(target_class)})._lamIs_{go_cls}() }}({dyn})"
            )
        return (
            f"func(v interface{{}}) {target} {{ if v == nil {{ return nil }}; "
            f"return v.({target}) }}({dyn})"
        )

    def _dynamic_to_go(self, raw: str, go_type: str) -> str:
        """The checked conversion of the ``interface{}`` value ``raw`` to
        ``go_type`` (see ``_result_payload_assert_go``)."""
        # (every form takes an ``interface{}``: a value whose Go type is
        # already concrete, from stale name tracking, converts too)
        return self._result_payload_assert_go(raw, go_type)

    def _result_payload_assert_go(self, value: str, payload: str) -> str:
        """``value`` (a ``Result`` payload read as ``interface{}`` by
        ``unwrap()`` / ``unwrapOr`` / ``?``, an ``any``) asserted to
        ``payload``. A ``float`` payload also accepts an int and a base-class
        payload a subclass instance (viewed as the base class), so a
        ``Result`` built outside a typed context (``f(Result.Ok(1))`` with a
        ``Result[float]`` parameter) unwraps instead of panicking. ``None``
        is ``None`` of a reference payload (a class, an interface, ``html``,
        ``json``...) and raises ``TypeError: None is not a str`` for a value
        type, which cannot hold it (``lamAs``, ``NONE_RUNTIME_GO``)."""
        if payload == "float64":
            return (
                "func(v interface{}) float64 { if n, ok := v.(int); ok { return float64(n) }; "
                f'return lamAs[float64](v, "a float") }}({value})'
            )
        payload_class = self._hier_class_from_go_type(payload) if payload.startswith("*") else ""
        if (
            payload_class
            and payload == f"*{self._go_class_name(payload_class)}"
            and self._hier_subclassed(payload_class)
            and not self._generic_classes.get(payload_class)
        ):
            up = f"_lamUp_{self._go_class_name(payload_class)}"
            return (
                f"func(v interface{{}}) {payload} {{ if x, ok := v.({payload}); ok || v == nil {{ return x }}; "
                f"return v.(interface{{ {up}() {payload} }}).{up}() }}({value})"
            )
        if self._go_type_holds_none(payload):
            return f"lamAsNil[{payload}]({value})"
        none_is_not = self._none_is_not_text(payload)
        elem = self._slice_elem_go_type(payload)
        if elem and elem != "interface{}" and payload != "[]byte":
            # ``Result.Ok([Item("a")])`` built outside a typed context holds a
            # ``[]interface{}``: convert it element by element.
            return (
                f"func(v interface{{}}) {payload} {{ if x, ok := v.({payload}); ok {{ return x }}; "
                f"if v == nil {{ lamNoneIsNot({none_is_not}) }}; "
                f"src := v.([]interface{{}}); out := make({payload}, len(src)); "
                f"for i, e := range src {{ out[i] = {self._result_payload_assert_go('e', elem)} }}; return out }}({value})"
            )
        key, map_value = self._split_go_map_type(payload)
        if key == "string" and map_value and map_value != "interface{}" and not self._is_go_set_type(payload):
            return (
                f"func(v interface{{}}) {payload} {{ if x, ok := v.({payload}); ok {{ return x }}; "
                f"if v == nil {{ lamNoneIsNot({none_is_not}) }}; "
                f"src := v.(map[string]interface{{}}); out := make({payload}, len(src)); "
                f"for k, e := range src {{ out[k] = {self._result_payload_assert_go('e', map_value)} }}; return out }}({value})"
            )
        return f"lamAs[{payload}]({value}, {none_is_not})"

    def _go_type_holds_none(self, go_type: str) -> bool:
        """A value of the Go type ``go_type`` may be Lam's ``None``: a class
        pointer, an interface, ``html`` / ``xml`` / ``json`` (their zero
        value), ``any``, a function. Value types (scalars, lists, dicts,
        sets) cannot."""
        go_type = (go_type or "").strip()
        if go_type.startswith(("*", "func(", "chan ")) or go_type in {"interface{}", "any", "LamHTML", "LamXML", "LamJSON"}:
            return True
        return go_type in self._interface_go_names()

    def _none_is_not_text(self, go_type: str) -> str:
        """The quoted Go string ``"a str"`` / ``"an int"`` / ``"a
        list[int]"`` of ``TypeError: None is not ...`` for ``go_type``."""
        name = self._go_type_lam_text(go_type) or go_type
        article = "an" if name[:1] in "aeiou" else "a"
        return '"' + f"{article} {name}".replace('"', "'") + '"'

    def _result_ok_payload_conversion(self, value: Tree, payload: str) -> str:
        """The Go for ``value`` stored as a ``payload`` when it fits it only
        through a conversion, else ``""``: ``unwrap()`` asserts the declared
        payload type, so ``Result.Ok(1)`` in a ``Result[float]`` (an ``int``)
        and ``Result.Ok(Circle())`` in a ``Result[Shape]`` (a ``*Circle``)
        panicked. Ints widen to the float type; a subclass instance is viewed
        as its base class, like any other upcast."""
        actual = self._hier_expr_go_type(value) or self._infer_type_from_value(value) or ""
        if payload in {"float64", "float32"} and actual in _GO_INT_TYPES:
            return f"{payload}({self._expr_to_go(value)})"
        if payload.startswith(("[]", "map[")):
            # ``Result.Ok(ys)`` with ``ys: list[str]`` in a
            # ``Result[list[any]]``: store the widened copy ``unwrap()``
            # asserts.
            source = self._expr_go_type_hint(value) or ""
            if self._container_widening_go("", source, payload):
                return self._container_widening_go(self._expr_to_go(value), source, payload)
            return ""
        if payload.startswith("*") and actual.startswith("*"):
            if actual != payload:
                return self._hier_upcast_to_go(self._expr_to_go(value), actual, payload)
            if self._hier_subclassed(self._hier_class_from_go_type(payload)):
                # ``s: Shape = Circle(); Result.Ok(s)``: store the ``*Shape``
                # view, not the most-derived object ``Result_Ok``'s
                # ``interface{}`` parameter would otherwise get.
                return self._expr_to_go(value)
        return ""

    def _conversion_to_go(self, name: str, node, value: str) -> str:
        """Lower ``int(x)`` / ``float(x)`` / ``bool(x)`` by the static type of ``x``.

        numeric -> Go conversion; ``bool`` -> 0/1; ``str`` -> parsed, panicking
        with ``ParseError`` on malformed text; ``json`` -> ``lamJSONTo*``;
        class -> its ``__int__`` / ``__float__`` / ``__bool__``; ``any`` ->
        Go type assertion (the semantic checker warns).
        """
        go_target = {"int": "int", "float": "float64", "bool": "bool"}[name]
        source = ""
        if isinstance(node, Tree):
            declared = self._expr_go_type_hint(node)
            inferred = self._infer_type_from_value(node) or ""
            # ``interface{}`` from value inference means "unknown", not ``any``:
            # only a declared/tracked ``any`` gets the type assertion.
            source = declared or ("" if inferred == "interface{}" else inferred)
            dunder = f"__{name}__"
            class_name = ""
            if source.startswith("*") and source[1:] in self._class_names:
                class_name = source[1:]
            elif not source:
                class_name = self._infer_receiver_class(node)
            if class_name and self._class_defines_method(class_name, dunder):
                return f"{value}.{self._go_method_name(class_name, dunder)}()"
        if source == "LamJSON":
            return {"int": "lamJSONToInt", "float": "lamJSONToFloat", "bool": "lamJSONToBool"}[name] + (
                f"({value}, \"float\")" if name == "float" else f"({value})"
            )
        if source == "string":
            self._need_import("strconv")
            self._need_import("fmt")
            parse = {
                "int": "strconv.Atoi(v)",
                "float": "strconv.ParseFloat(v, 64)",
                "bool": "strconv.ParseBool(v)",
            }[name]
            return (
                f"func(v string) {go_target} {{ out, err := {parse}; "
                f"if err != nil {{ panic(fmt.Sprintf(\"ParseError: cannot parse %q as {name}: %v\", v, err)) }}; "
                f"return out }}({value})"
            )
        if source == "bool":
            if name == "bool":
                return value
            return f"func(v bool) {go_target} {{ if v {{ return 1 }}; return 0 }}({value})"
        if source == "interface{}":
            return self._dynamic_conversion_to_go(name, value)
        if not source:
            # The semantic checker accepted the call, but the transpiler cannot
            # see the argument's type (e.g. an index into a method call result).
            return self._unknown_conversion_to_go(name, value)
        if name == "bool":
            return f"({value} != 0)"
        if name == "int" and source in {"float64", "float32"}:
            # ``int(3.7)`` truncates (3) at run time (Go rejects converting a
            # float constant with a fractional part); NaN, infinities and
            # out-of-range values raise (``lamFloatToInt``) instead of Go's
            # undefined result.
            return f"lamFloatToInt(float64({value}))"
        return f"{go_target}({value})"

    def _unknown_conversion_to_go(self, name: str, value: str) -> str:
        """Same rules as the static lowering, decided at run time: strings
        are parsed, numbers converted, booleans become 0/1."""
        self._need_import("strconv")
        self._need_import("fmt")
        go_target = {"int": "int", "float": "float64", "bool": "bool"}[name]
        if name == "int":
            cases = (
                f"case int: return n; case int8: return int(n); case int16: return int(n); case int32: return int(n); "
                f"case int64: return int(n); case uint: return int(n); case uint8: return int(n); case uint16: return int(n); "
                f"case uint32: return int(n); case uint64: return int(n); case uintptr: return int(n); "
                f"case float64: return lamFloatToInt(n); case float32: return lamFloatToInt(float64(n)); "
                f"case bool: if n {{ return 1 }}; return 0; "
                f"case string: out, err := strconv.Atoi(n); if err != nil {{ panic(fmt.Sprintf(\"ParseError: cannot parse %q as int: %v\", n, err)) }}; return out; "
            )
        elif name == "float":
            cases = (
                f"case float64: return n; case float32: return float64(n); "
                f"case int: return float64(n); case int8: return float64(n); case int16: return float64(n); case int32: return float64(n); "
                f"case int64: return float64(n); case uint: return float64(n); case uint8: return float64(n); case uint16: return float64(n); "
                f"case uint32: return float64(n); case uint64: return float64(n); "
                f"case bool: if n {{ return 1 }}; return 0; "
                f"case string: out, err := strconv.ParseFloat(n, 64); if err != nil {{ panic(fmt.Sprintf(\"ParseError: cannot parse %q as float: %v\", n, err)) }}; return out; "
            )
        else:
            cases = (
                f"case bool: return n; "
                f"case int: return n != 0; case int64: return n != 0; case int32: return n != 0; case uint: return n != 0; "
                f"case uint64: return n != 0; case uint8: return n != 0; case float64: return n != 0; case float32: return n != 0; "
                f"case string: out, err := strconv.ParseBool(n); if err != nil {{ panic(fmt.Sprintf(\"ParseError: cannot parse %q as bool: %v\", n, err)) }}; return out; "
            )
        return (
            f"func(v interface{{}}) {go_target} {{ switch n := v.(type) {{ {cases}}}; "
            f"panic(fmt.Sprintf(\"TypeError: {name}(...) cannot convert %T\", v)) }}({value})"
        )

    _INTEGER_GO_KINDS = (
        "int", "int8", "int16", "int32", "int64",
        "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
    )

    def _dynamic_conversion_to_go(self, name: str, value: str) -> str:
        """``int(any)`` accepts every Go integer kind, ``float(any)`` every
        floating kind and ``bool(any)`` a bool; anything else panics with a
        ``TypeError`` naming the dynamic type."""
        self._need_import("fmt")
        if name == "int":
            cases = "".join(f"case {kind}: return int(n); " for kind in self._INTEGER_GO_KINDS)
            target, expects = "int", "an integer"
        elif name == "float":
            cases = "case float64: return n; case float32: return float64(n); "
            target, expects = "float64", "a floating-point number"
        else:
            cases = "case bool: return n; "
            target, expects = "bool", "a bool"
        return (
            f"func(v interface{{}}) {target} {{ switch n := v.(type) {{ {cases}}}; "
            f"panic(fmt.Sprintf(\"TypeError: {name}(...) expects {expects}, got %T\", v)) }}({value})"
        )

    def _class_defines_method(self, class_name: str, method: str) -> bool:
        """True when ``class_name`` or one of its bases declares ``method``."""
        seen: set[str] = set()
        pending = [class_name]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            if method in self._instance_methods.get(current, set()):
                return True
            pending.extend(self.class_bases.get(current, []))
        return False

    def _async_method_handle_go_type(self, class_name: str, method: str) -> str:
        """``chan func() T`` when ``class_name.method`` (or the method a base
        class declares) is an ``async func`` with a known handle type."""
        pending = [class_name]
        seen: set = set()
        while pending:
            current = pending.pop(0)
            if not current or current in seen:
                continue
            seen.add(current)
            handle = self._method_return_types.get(f"async:{current}.{method}", "")
            if handle:
                return handle
            if self._hier_defines(current, method) or (
                not self._hier_bases(current)
                and (method in self._instance_methods.get(current, set())
                     or method in self._static_methods.get(current, set()))
            ):
                # Declared here (not async): ``_instance_methods`` also
                # lists inherited methods, so only the class's own
                # declarations stop the walk to its bases.
                return ""
            pending.extend(self.class_bases.get(current, []) or self._program_class_bases.get(current, []))
        return ""

    def _list_literal_index_type(self, node) -> str:
        """``[]T`` of a list literal indexed directly (``[a, b][i]``) when its
        elements share one concrete Go type; ``""`` otherwise."""
        if not (isinstance(node, Tree) and node.data == "list" and node.children):
            return ""
        inferred = self._infer_type_from_value(node) or ""
        literal_type = self._list_literal_elem_hint_type(node, inferred)
        if not literal_type.startswith("[]") or literal_type in {"[]interface{}", "[]any"}:
            return ""
        return literal_type

    def _none_ternary_go_type(self, node) -> str:
        """Go type of ``x if c else None`` (either branch ``None``): the other
        branch's type when it holds ``None`` (a class, an interface,
        ``html``...), else ``interface{}`` (a ``str`` cannot be ``None``:
        ``"alert" if c else None`` is an ``any``); ``""`` for another
        ternary or an untyped branch."""
        if not (isinstance(node, Tree) and node.data == "test" and len(node.children) == 3):
            return ""
        true_branch, false_branch = node.children[0], node.children[2]
        nested = [self._none_ternary_go_type(b) for b in (true_branch, false_branch)]
        if "interface{}" in nested:
            # ``"a" if x else ("b" if y else None)``: an ``any`` as a whole.
            return "interface{}"
        if self._is_none_literal(true_branch) or nested[0]:
            other = false_branch
        elif self._is_none_literal(false_branch) or nested[1]:
            other = true_branch
        else:
            return ""
        if self._is_none_literal(other):
            return "interface{}"
        go_type = self._expr_go_type_hint(other) or self._infer_type_from_value(other) or ""
        if not go_type:
            return ""
        return go_type if self._go_type_holds_none(go_type) else "interface{}"

    def _ternary_common_go_type(self, node) -> str:
        """Common Go type of ``a if c else b`` branches; ``""`` when unknown.

        Equal known types are used as-is; ``int`` with ``float64`` widens to
        ``float64``. Unknown or ``interface{}`` branches keep the dynamic
        fallback.
        """
        def branch_type(branch, plan) -> str:
            with self._nr_registered(plan):
                go_type = self._expr_go_type_hint(branch) or self._infer_type_from_value(branch) or ""
            return "" if go_type == "interface{}" else go_type

        # ``x.side if isinstance(x, Square) else 0``: each branch is typed
        # with its narrowing.
        none_type = self._none_ternary_go_type(node)
        if none_type:
            return none_type
        true_plan, false_plan = self._nr_ternary_plans(node)
        left = branch_type(node.children[0], true_plan)
        right = branch_type(node.children[2], false_plan)
        if not left or not right:
            return ""
        if left == right:
            return left
        if {left, right} == {"int", "float64"}:
            return "float64"
        # ``Square() if c else Shape()``: the least common base class.
        return self._hier_common_base_go_type([left, right])

    def _consumes_result_call(self, node) -> bool:
        """True for ``call()?``, ``call().unwrap()`` and ``call().unwrapOr(d)``."""
        if not isinstance(node, Tree):
            return False
        if node.data == "propagate":
            operand = next((child for child in node.children if isinstance(child, Tree)), None)
            return isinstance(operand, Tree) and operand.data == "funccall"
        if node.data != "funccall" or not node.children:
            return False
        callee = node.children[0]
        return (
            isinstance(callee, Tree)
            and callee.data == "getattr"
            and len(callee.children) >= 2
            and self._get_name(callee.children[1]) in {"unwrap", "unwrapOr"}
            and isinstance(callee.children[0], Tree)
            and callee.children[0].data == "funccall"
        )

    def _result_payload_of(self, node) -> str:
        """Statically known payload Go type of a ``Result``-valued expression."""
        if not isinstance(node, Tree):
            return ""
        if node.data == "var" and node.children:
            name = self._get_name(node.children[0])
            if self._var_go_types.get(name) == "*Result":
                return self._var_result_payloads.get(name, "")
            return ""
        if node.data == "funccall":
            return self._propagated_payload_go_type(node)
        return ""

    def _func_value_lam_type(self, callee):
        """The declared Lam ``func(...) -> R`` type of the function value
        ``callee`` names (not a declared function or method): a
        function-typed local or parameter (``fn``), field (``self.loader``,
        ``h.loader``), collection element (``fns[0]``, ``table["k"]``) or
        call result (``maker()``). ``None`` when unknown."""
        from compiler.typesys import FuncType, ListType
        if not isinstance(callee, Tree):
            return None
        found = None
        if callee.data == "var" and callee.children:
            name = self._get_name(callee.children[0])
            if (self._var_go_types.get(name) or "").startswith("func("):
                found = getattr(self, "_var_lam_types", {}).get(name)
        elif callee.data == "getattr" and len(callee.children) >= 2:
            obj = callee.children[0]
            attr = self._get_name(callee.children[1])
            own = (
                isinstance(obj, Tree) and obj.data == "var" and obj.children
                and self._get_name(obj.children[0]) == "self" and bool(self.current_class)
            )
            class_name = self.current_class if own else self._infer_receiver_class(obj)
            if class_name and attr:
                for owner in [class_name, *self._hier_ancestors(class_name)]:
                    key = f"{owner}.{attr}"
                    found = self._field_lam_types.get(key) or self._external_field_lam_types.get(key)
                    if found is not None:
                        break
        elif callee.data == "getitem" and callee.children:
            container = self._func_value_container_lam_type(callee.children[0])
            if isinstance(container, ListType):
                found = container.item
            elif isinstance(container, DictType):
                found = container.value
        elif callee.data == "funccall" and callee.children:
            inner = self._func_value_lam_type(callee.children[0])
            if inner is None and isinstance(callee.children[0], Tree) and callee.children[0].data == "var":
                name = self._get_name(callee.children[0].children[0])
                if name not in self.declared_vars and len(self._overload_variants.get(name, ())) <= 1:
                    returned = self._func_return_lam_types.get(name)
                    found = returned if isinstance(returned, FuncType) else None
            elif isinstance(inner, FuncType):
                found = inner.ret
        return found if isinstance(found, FuncType) else None

    def _func_value_container_lam_type(self, node):
        """Lam type of a collection a function value is read from (``fns``
        in ``fns[0](x)``): a local, parameter or field."""
        if not isinstance(node, Tree):
            return None
        if node.data == "var" and node.children:
            return getattr(self, "_var_lam_types", {}).get(self._get_name(node.children[0]))
        if node.data == "getattr" and len(node.children) >= 2:
            obj = node.children[0]
            attr = self._get_name(node.children[1])
            own = (
                isinstance(obj, Tree) and obj.data == "var" and obj.children
                and self._get_name(obj.children[0]) == "self" and bool(self.current_class)
            )
            class_name = self.current_class if own else self._infer_receiver_class(obj)
            for owner in [class_name, *self._hier_ancestors(class_name)] if class_name else []:
                key = f"{owner}.{attr}"
                found = self._field_lam_types.get(key) or self._external_field_lam_types.get(key)
                if found is not None:
                    return found
        return None

    def _lam_type_go(self, lam_type) -> str:
        """Go type of a Lam type object (``list[int]`` -> ``[]int``), through
        its spelling and the annotation lowering; ``""`` when unknown."""
        from compiler.typesys import render_type
        try:
            node = self._lam_type_text_node(render_type(lam_type))
        except Exception:
            return ""
        if node is None:
            return ""
        try:
            return self._type_expr_to_go(node) or ""
        except Exception:
            return ""

    def _func_value_call_result_go(self, call_node, kind: str) -> str:
        """For a call of a function value (``_func_value_lam_type``): the Go
        type of its declared result (``kind == "result"``), of the payload
        of its ``Result[T]`` (``"payload"``) or of the payload of the
        ``Result[T]`` items of its ``list`` / ``generator`` (``"elem"``)."""
        from compiler.typesys import GenericType as _Generic, ListType
        if not (isinstance(call_node, Tree) and call_node.data == "funccall" and call_node.children):
            return ""
        func_type = self._func_value_lam_type(call_node.children[0])
        if func_type is None:
            return ""
        ret = func_type.ret
        if kind == "result":
            return self._lam_type_go(ret)
        if kind == "elem":
            if isinstance(ret, ListType):
                ret = ret.item
            elif isinstance(ret, _Generic) and ret.base in {"generator", "iterator"} and len(ret.args) == 1:
                ret = ret.args[0]
            else:
                return ""
        if isinstance(ret, _Generic) and ret.base == "Result" and len(ret.args) == 1:
            payload = self._lam_type_go(ret.args[0])
            return "" if payload in {"", "interface{}"} else payload
        return ""

    def _unwrap_payload_go_type(self, call_node) -> str:
        """Payload type asserted by an inline ``x.unwrap()`` / ``x.unwrapOr(d)``.

        Empty when the receiver's payload is unknown or ``any``; the call then
        keeps returning ``interface{}`` (the dynamic fallback).
        """
        if not isinstance(call_node, Tree) or call_node.data != "funccall" or not call_node.children:
            return ""
        callee = call_node.children[0]
        if not isinstance(callee, Tree) or callee.data != "getattr" or len(callee.children) < 2:
            return ""
        if self._get_name(callee.children[1]) not in {"unwrap", "unwrapOr"}:
            return ""
        payload = self._result_payload_of(callee.children[0])
        return "" if payload in {"", "interface{}"} else payload

    def _iterable_result_payload_go_type(self, iterable) -> str:
        """Payload Go type of the ``Result`` elements of ``iterable``: a
        call of a function / method declared ``-> generator[Result[T]]`` /
        ``-> list[Result[T]]`` or a variable annotated so."""
        if isinstance(iterable, Tree) and iterable.data == "var" and iterable.children:
            return self._var_result_payloads.get(f"{self._get_name(iterable.children[0])}[]", "")
        if isinstance(iterable, Tree) and iterable.data == "getattr" and len(iterable.children) >= 2:
            # ``self.cached`` with a field ``cached: list[Result[int]]``.
            receiver = iterable.children[0]
            class_name = self.current_class if (
                isinstance(receiver, Tree) and receiver.data == "var" and receiver.children
                and self._get_name(receiver.children[0]) == "self"
            ) else self._infer_receiver_class(receiver)
            if class_name:
                field = self._get_name(iterable.children[1])
                for owner in [class_name] + list(self._hier_ancestors(class_name)):
                    payload = self._result_payload_go_types.get(f"{owner}.{field}[]", "")
                    if payload:
                        return payload
            return ""
        return self._propagated_payload_go_type(iterable, "[]")

    def _propagated_payload_go_type(self, operand, suffix: str = "") -> str:
        if isinstance(operand, Tree) and operand.data == "var":
            # ``r?`` with ``r: Result[Box]``.
            return self._result_payload_of(operand) if not suffix else ""
        if not isinstance(operand, Tree) or operand.data != "funccall" or not operand.children:
            return ""
        callee = operand.children[0]
        if not isinstance(callee, Tree):
            return ""
        # ``fn(x)?`` / ``self.loader(1).unwrap()`` / ``fns[0]()?``: a call of
        # a function value whose declared type returns ``Result[T]``.
        through_value = self._func_value_call_result_go(operand, "elem" if suffix else "payload")
        if through_value:
            return through_value
        if callee.data == "var" and callee.children:
            return self._payload_go_type_for_call(self._get_name(callee.children[0]) + suffix, operand)
        if callee.data != "getattr" or len(callee.children) < 2:
            return ""
        receiver = callee.children[0]
        method = self._get_name(callee.children[1])
        if not isinstance(receiver, Tree):
            return ""
        if receiver.data != "var" or not receiver.children:
            class_name = self._infer_receiver_class(receiver)
            return self._payload_go_type_for_call(f"{class_name}.{method}{suffix}", operand) if class_name else ""
        receiver_name = self._get_name(receiver.children[0])
        class_name = receiver_name if receiver_name in self._class_names else self._var_types.get(receiver_name, "")
        if not class_name and receiver_name == "self" and self.current_class:
            # ``self.items()?`` inside a method of the class.
            class_name = self.current_class
        key = f"{class_name}.{method}" if class_name else f"{receiver_name}.{method}"
        return self._payload_go_type_for_call(key + suffix, operand)

    def _payload_go_type_for_call(self, key: str, call_node: Tree) -> str:
        """Result payload Go type of the callee ``key`` at ``call_node``.

        Overloads may carry different payload types, so the variant the
        call dispatches to is resolved first; the name-level entry only
        records the first variant seen. An unresolvable overloaded call
        yields ``""`` (no assertion) rather than another variant's type.
        """
        variants = self._overload_variants.get(key) or []
        if len(variants) <= 1:
            payload = self._result_payload_go_types.get(key, "")
            generic_names = self._func_generic_params.get(key, set())
            if payload and mentions_names(payload, generic_names):
                # ``it.first()`` on an ``Iter[int]``: ``Result[T]`` is a
                # ``Result[int]`` here.
                class_name, _dot, method = key.rpartition(".")
                callee = call_node.children[0] if call_node.children else None
                receiver = callee.children[0] if isinstance(callee, Tree) and callee.data == "getattr" else None
                static = (
                    isinstance(receiver, Tree) and receiver.data == "var" and receiver.children
                    and self._get_name(receiver.children[0]) == class_name
                )
                subst = self._method_call_subst(
                    class_name, method, receiver,
                    call_node.children[1] if len(call_node.children) > 1 else None,
                    static=bool(static),
                ) if class_name else self._generic_call_subst(
                    key, call_node.children[1] if len(call_node.children) > 1 else None, sorted(generic_names),
                )
                payload = substitute_go_type(payload, subst)
                if mentions_names(payload, [n for n in generic_names if n not in self._generic_names]):
                    return ""
            return payload
        args_node = next(
            (child for child in call_node.children[1:] if isinstance(child, Tree)),
            None,
        )
        variant = self._overload_variant_for_sig(key, self._infer_call_arg_sig(args_node))
        if variant is None:
            return ""
        return self._result_payload_go_types.get(str(variant.get("meta_key") or ""), "")

    def _result_error_view_class(self, node, attr: str) -> str:
        """``Error`` when ``node`` is ``r.error`` on a ``Result`` and ``attr``
        is a member of lamerrors' ``Error`` (``""`` otherwise)."""
        if not (isinstance(node, Tree) and node.data == "getattr" and len(node.children) >= 2):
            return ""
        if self._get_name(node.children[1]) != "error":
            return ""
        receiver = node.children[0]
        receiver_go = self._expr_go_type_hint(receiver) or ""
        result_go = f"*{self._go_class_name('Result')}" if "Result" in self._class_names else ""
        if not result_go or not (receiver_go == result_go or receiver_go.startswith(result_go + "[")):
            if self._infer_receiver_class(receiver) != "Result":
                return ""
        error_class = self._do_catch_error_class()
        if not error_class:
            return ""
        members = {name for name, _t in self.class_fields.get(error_class, [])}
        members |= self._external_field_names.get(error_class, set())
        members |= self._instance_methods.get(error_class, set())
        return error_class if attr in members else ""

    def _error_view_go(self, value_go: str, error_class: str) -> str:
        """``value_go`` (an ``interface{}``) as a ``*Error``: an ``Error`` or
        a subclass view as is, ``nil`` for no error, another value wrapped
        (``Error("", fmt.Sprint(v), v)``)."""
        self._need_import("fmt")
        go_err = self._go_class_name(error_class)
        return (
            f"func(v interface{{}}) *{go_err} {{ if v == nil {{ return nil }}; "
            f"if e, ok := v.(*{go_err}); ok {{ return e }}; "
            f"if e, ok := v.(interface{{ _lamIs_{go_err}() *{go_err} }}); ok {{ return e._lamIs_{go_err}() }}; "
            f'return {self._go_ctor_name(error_class)}("", fmt.Sprint(v), v) }}({value_go})'
        )

    def _infer_receiver_class(self, obj_node) -> str:
        """Return the Lam class name of the value flowing into a
        method call's receiver expression, or ``""`` if it can't be
        determined.

        Cases handled:
          * ``var`` — direct lookup in ``_var_types``.
          * ``funccall(getattr(<inner>, method))`` — recurse on
            ``inner`` to learn its class, then return
            ``_method_return_types[InnerClass.method]``.
          * ``funccall(var(Class))`` — constructor or static call;
            for constructors return ``Class``, for static methods
            consult ``_method_return_types[Class.method]``.

        This makes fluent builders work: ``db.table(name).where(...)``
        resolves ``where`` against ``QueryBuilder`` (the declared
        return type of ``Db.table``) without needing the
        intermediate value to be assigned to a typed variable.
        """
        if not isinstance(obj_node, Tree):
            return ""
        if obj_node.data == "cast_expr" and len(obj_node.children) == 2:
            # ``(lookup(k) as User).greet()``: the cast's class.
            class_name = self._hier_class_from_go_type(self._type_expr_to_go(obj_node.children[1]))
            return class_name if class_name in self._class_names else ""
        if obj_node.data == "var":
            name = self._get_name(obj_node.children[0])
            known = self._var_types.get(name, "")
            if known:
                return known
            if name == "self" and self.current_class and name not in self._var_go_types:
                return self.current_class
            # Loop variables over ``list[Shape]`` (or ``list[DbConn]``) and
            # similar only record their Go type.
            return self._receiver_type_from_go_type(self._var_go_types.get(name, ""))
        if obj_node.data == "getitem":
            # ``shapes[0].area()`` over a ``list[Shape]`` / ``dict[str, Shape]``.
            literal_type = self._list_literal_index_type(obj_node.children[0]) if obj_node.children else ""
            if literal_type:
                # ``[Square(), Shape()][0].area()``: the literal's item class.
                return self._receiver_type_from_go_type(literal_type[2:])
            return self._receiver_type_from_go_type(self._expr_go_type_hint(obj_node))
        if obj_node.data == "getattr":
            go_type = self._expr_go_type_hint(obj_node)
            class_name = go_type.removeprefix("*").split("[", 1)[0]
            if class_name in self._class_names:
                return class_name
            # ``self.db.table(name).whereRaw(...)`` with a field ``db:
            # DbConn``: the chain resolves against the interface's method
            # signatures and return types (defaults, ``?`` payloads).
            interface_name = self._interface_from_go_type(go_type)
            if interface_name:
                return interface_name
        if obj_node.data == "propagate" and obj_node.children:
            go_type = self._propagated_payload_go_type(obj_node.children[0])
            class_name = go_type.removeprefix("*").split("[", 1)[0]
            return class_name if class_name in self._class_names else ""
        if obj_node.data == "null_coalesce":
            # ``(lookup() ?? User("g")).greet()``: the class of the result.
            go_type = self._expr_go_type_hint(obj_node) or ""
            class_name = go_type.removeprefix("*").split("[", 1)[0]
            return class_name if go_type.startswith("*") and class_name in self._class_names else ""
        if obj_node.data == "funccall":
            inner_func = obj_node.children[0]
            if isinstance(inner_func, Tree):
                if inner_func.data == "getattr":
                    inner_obj = inner_func.children[0]
                    inner_method = self._get_name(inner_func.children[1])
                    if inner_method in {"unwrap", "unwrapOr"}:
                        # ``r.unwrap().area()`` with ``r: Result[Shape]``:
                        # the payload class (so overrides dispatch).
                        payload_class = self._hier_class_from_go_type(self._unwrap_payload_go_type(obj_node))
                        if payload_class:
                            return payload_class
                    # Static call ``Class.method(...)``?
                    # Only class-typed returns identify a receiver class; a
                    # scalar return (``-> str``) must keep its builtin
                    # methods (``Rows.str(r, k).contains(...)``).
                    if isinstance(inner_obj, Tree) and inner_obj.data == "var":
                        receiver_name = self._get_name(inner_obj.children[0])
                        if receiver_name in self._class_names:
                            returned = self._method_return_types.get(
                                f"{receiver_name}.{inner_method}", ""
                            )
                            return returned if returned in self._class_names or returned in self._interfaces else ""
                    # Instance call — recurse for the inner receiver's
                    # class, then look up the method's return type on it.
                    inner_class = self._infer_receiver_class(inner_obj)
                    if inner_class:
                        returned = self._method_return_types.get(
                            f"{inner_class}.{inner_method}", ""
                        )
                        if returned in self._class_names or returned in self._interfaces:
                            return returned
                        if not returned or returned in self._class_type_param_names_of(
                            self._generic_method_owner(inner_class, inner_method)
                        ):
                            # ``self.at(0).name`` of a method inherited from
                            # a generic base (``-> T``, ``T = User``): the
                            # class of the call's Go type.
                            return self._hier_class_from_go_type(self._expr_go_type_hint(obj_node) or "")
                        return ""
                elif inner_func.data == "var":
                    # Bare ``Class()`` constructor.
                    receiver_name = self._get_name(inner_func.children[0])
                    if receiver_name in self._class_names:
                        return receiver_name
                    # ``current().field`` — a function returning a class.
                    returned = self._func_return_go_types.get(receiver_name, "")
                    returned_class = returned.removeprefix("*").split("[", 1)[0]
                    if returned.startswith("*") and returned_class in self._class_names:
                        return returned_class
                    # ``requestDb().query(q)`` with ``requestDb() -> Sql``.
                    returned_interface = self._interface_from_go_type(returned)
                    if returned_interface:
                        return returned_interface
            if (
                isinstance(inner_func, Tree) and inner_func.data == "getitem"
                and isinstance(inner_func.children[0], Tree) and inner_func.children[0].data == "var"
            ):
                # ``Box[str]("hi").map(...)``: a generic class constructor.
                receiver_name = self._get_name(inner_func.children[0].children[0])
                if receiver_name in self._class_names:
                    return receiver_name
            if isinstance(inner_func, Tree) and inner_func.data in {"funccall", "var"}:
                # ``maker()()`` / ``f()`` with ``f: func() -> Shape``.
                return self._hier_class_from_go_type(self._expr_go_type_hint(obj_node))
        return ""

    def _infer_receiver_elem_type(self, obj_node) -> str:
        """Walk a chained method-call receiver AST to figure out the
        element type of the list flowing into the current method call.

        Used when ``_var_go_types`` doesn't know about the immediate
        receiver (e.g. because it's a chained fluent call that
        returns a fresh slice value)."""
        if not isinstance(obj_node, Tree):
            return ""
        if obj_node.data == "var":
            name = self._get_name(obj_node.children[0])
            go_ty = self._var_go_types.get(name, "")
            if go_ty.startswith("[]"):
                return go_ty[2:]
            return ""
        if obj_node.data == "list":
            # Bare list literal — no type info, caller will default.
            return ""
        if obj_node.data == "funccall":
            hint = self._expr_go_type_hint(obj_node) or ""
            if hint.startswith("[]"):
                return hint[2:]
            inner_func = obj_node.children[0]
            if isinstance(inner_func, Tree) and inner_func.data == "getattr":
                inner_obj = inner_func.children[0]
                inner_method = self._get_name(inner_func.children[1])
                inner_elem = self._infer_receiver_elem_type(inner_obj)
                if inner_method in ("filter", "map", "sort"):
                    # These preserve the element type (for map that is
                    # only approximate, but matches the common case).
                    return inner_elem
        return ""

    # ─── Python-style slicing ──────────────────────────────────

    @staticmethod
    def _slice_bound_is_native(node) -> bool:
        """Return True when ``node`` (a slice start/end expression)
        is safe to drop straight into Go's native ``[a:b]`` form.

        The cheap path is invalid when the expression syntactically
        contains a unary-minus (i.e. a negative index in Python's
        sense). Anything else — literal ints, identifiers,
        ``len(x) - 1``, function calls — is a non-negative computation
        from Go's POV, so it lowers cleanly. We walk the subtree
        looking for a ``factor`` node whose first child is the ``-``
        token; that's the AST shape of ``-x``.
        """
        if node is None:
            return True
        def _has_neg(n) -> bool:
            if not isinstance(n, Tree):
                return False
            if n.data == "factor" and n.children:
                first = n.children[0]
                if isinstance(first, Token) and str(first) == "-":
                    return True
            for c in n.children:
                if _has_neg(c):
                    return True
            return False
        return not _has_neg(node)

    def _slice_to_go(self, obj_go: str, slice_node: Tree, obj_node) -> str:
        """Lower a ``getitem(obj, slice(...))`` to Go.

        Cheap path: when start / stop are non-negative integer literals
        (or absent) and there is no step, emit the native ``obj[a:b]``
        form so Go can keep the slice header trick.

        Full Python semantics path: emit a runtime IIFE that resolves
        negative indices against ``len(obj)`` and supports a stride
        (including a negative one for reversed iteration). The IIFE
        uses ``reflect`` so it handles slices, arrays, and strings
        uniformly. Strings round-trip back to a ``string`` rather
        than a ``[]interface{}`` — slicing a string in Lam should
        feel like Python's slicing, not Go's.
        """
        # Parse the slice node into start / end / step subtrees.
        start_node = None
        end_node = None
        step_node = None
        non_op = [c for c in slice_node.children
                  if not (isinstance(c, Tree) and c.data == "sliceop")]
        if len(non_op) >= 1:
            start_node = non_op[0]
        if len(non_op) >= 2:
            end_node = non_op[1]
        for c in slice_node.children:
            if isinstance(c, Tree) and c.data == "sliceop":
                for sc in c.children:
                    if sc is not None:
                        step_node = sc

        cheap = (
            step_node is None
            and self._slice_bound_is_native(start_node)
            and self._slice_bound_is_native(end_node)
        )
        if cheap:
            start_go = self._expr_to_go(start_node) if start_node is not None else ""
            end_go = self._expr_to_go(end_node) if end_node is not None else ""
            obj_type = (self._expr_go_type_hint(obj_node) or "") if obj_node is not None else ""
            if not obj_type and isinstance(obj_node, Tree) and obj_node.data in ("string", "fstring", "string_concat"):
                # ``"k"[2:0]``: Go rejects constant out-of-range bounds of a
                # constant string; the clamping form keeps Python's result.
                obj_type = "string"
            if (start_go or end_go) and (obj_type == "string" or (obj_type.startswith("[]") and "func(" not in obj_type)):
                # Python semantics: out-of-range bounds clamp instead of
                # panicking (``s[i:i + 2]`` near the end, ``xs[5:]`` on a
                # short list). Go inlines the immediately-called literal.
                lo = start_go or "0"
                clamp = (
                    "if _a < 0 { _a += len(_s); if _a < 0 { _a = 0 } }; "
                    "if _b < 0 { _b += len(_s); if _b < 0 { _b = 0 } }; "
                    "if _b > len(_s) { _b = len(_s) }; if _a > _b { _a = _b }; return _s[_a:_b] }"
                )
                if end_go:
                    return f"func(_s {obj_type}, _a int, _b int) {obj_type} {{ {clamp}({obj_go}, {lo}, {end_go})"
                return f"func(_s {obj_type}, _a int) {obj_type} {{ _b := len(_s); {clamp}({obj_go}, {lo})"
            return f"{obj_go}[{start_go}:{end_go}]"

        # The IIFE keeps Python's slice semantics: negative indices
        # resolve against the length, a missing ``end`` defaults to
        # ``len(obj)`` for a forward step or ``-1`` for a reverse step,
        # and a non-1 step (positive or negative) drives the iteration.
        # The bound expressions are assumed to be ``int``-typed; if the
        # user passes an ``any`` value, they should ``int(x)`` it.
        start_go = self._expr_to_go(start_node) if start_node is not None else "0"
        end_go = self._expr_to_go(end_node) if end_node is not None else "0"
        step_go = self._expr_to_go(step_node) if step_node is not None else "1"
        start_provided = "true" if start_node is not None else "false"
        end_provided = "true" if end_node is not None else "false"

        obj_type = (self._expr_go_type_hint(obj_node) or "") if obj_node is not None else ""
        if obj_type == "string" or (obj_type.startswith("[]") and "func(" not in obj_type):
            # Statically typed receiver: the same Python semantics without
            # reflection, keeping the receiver's type (``a[-2:]`` of a
            # ``list[int]`` is a ``list[int]``).
            if obj_type == "string":
                self._need_import("strings")
                init, add, done = "var _sb strings.Builder", "_sb.WriteByte(_s[_i])", "return _sb.String()"
            else:
                init, add, done = f"_r := {obj_type}{{}}", "_r = append(_r, _s[_i])", "return _r"
            body = (
                f"func(_s {obj_type}, _start int, _end int, _step int, _sp bool, _ep bool) {obj_type} {{ "
                "_n := len(_s); if _step == 0 { _step = 1 }; "
                "if _sp && _start < 0 { _start += _n }; if _ep && _end < 0 { _end += _n }; "
                "if _step > 0 { if !_sp || _start < 0 { _start = 0 }; if _start > _n { _start = _n }; "
                "if !_ep || _end > _n { _end = _n }; if _end < 0 { _end = 0 } } else { "
                "if !_sp || _start >= _n { _start = _n - 1 }; if _start < -1 { _start = -1 }; "
                "if !_ep || _end < -1 { _end = -1 }; if _end >= _n { _end = _n - 1 } }; "
                f"{init}; if _step > 0 {{ for _i := _start; _i < _end; _i += _step {{ {add} }} }} else {{ "
                f"for _i := _start; _i > _end; _i += _step {{ {add} }} }}; {done} }}"
                f"({obj_go}, int({start_go}), int({end_go}), int({step_go}), {start_provided}, {end_provided})"
            )
            return body

        self._need_import("reflect")
        self._need_import("strings")
        lines = [
            "func() interface{} {",
            f"\t_o := interface{{}}({obj_go})",
            "\t_v := reflect.ValueOf(_o)",
            "\t_isStr := _v.Kind() == reflect.String",
            "\t_n := 0",
            "\tif _isStr { _n = len(_o.(string)) } else if _v.IsValid() && (_v.Kind() == reflect.Slice || _v.Kind() == reflect.Array) { _n = _v.Len() }",
            f"\t_step := int({step_go})",
            "\tif _step == 0 { _step = 1 }",
            f"\t_startProvided := {start_provided}",
            f"\t_start := int({start_go})",
            f"\t_endProvided := {end_provided}",
            f"\t_end := int({end_go})",
            "\tif _startProvided && _start < 0 { _start += _n }",
            "\tif _endProvided && _end < 0 { _end += _n }",
            "\tif _step > 0 {",
            "\t\tif !_startProvided { _start = 0 }",
            "\t\tif _start < 0 { _start = 0 }",
            "\t\tif _start > _n { _start = _n }",
            "\t\tif !_endProvided { _end = _n }",
            "\t\tif _end < 0 { _end = 0 }",
            "\t\tif _end > _n { _end = _n }",
            "\t} else {",
            "\t\tif !_startProvided { _start = _n - 1 }",
            "\t\tif _start >= _n { _start = _n - 1 }",
            "\t\tif _start < -1 { _start = -1 }",
            "\t\tif !_endProvided { _end = -1 }",
            "\t\tif _end >= _n { _end = _n - 1 }",
            "\t\tif _end < -1 { _end = -1 }",
            "\t}",
            "\tif _isStr {",
            "\t\t_s := _o.(string)",
            "\t\tvar _sb strings.Builder",
            "\t\tif _step > 0 {",
            "\t\t\tfor _i := _start; _i < _end; _i += _step { _sb.WriteByte(_s[_i]) }",
            "\t\t} else {",
            "\t\t\tfor _i := _start; _i > _end; _i += _step { _sb.WriteByte(_s[_i]) }",
            "\t\t}",
            "\t\treturn _sb.String()",
            "\t}",
            "\t_r := []interface{}{}",
            "\tif _step > 0 {",
            "\t\tfor _i := _start; _i < _end; _i += _step { _r = append(_r, _v.Index(_i).Interface()) }",
            "\t} else {",
            "\t\tfor _i := _start; _i > _end; _i += _step { _r = append(_r, _v.Index(_i).Interface()) }",
            "\t}",
            "\treturn _r",
            "}()",
        ]
        return self._pin_multiline_to(slice_node, "\n".join(lines))

    # ─── List comprehension ────────────────────────────────────

    def _comprehension_tuple_targets(self, target) -> List[str]:
        if not isinstance(target, Tree):
            return []
        if target.data in {"typed_for_target", "untyped_for_target"} and target.children:
            return self._comprehension_tuple_targets(target.children[0])
        if target.data in {"tuple", "exprlist"}:
            return [self._expr_to_go(child) for child in target.children if child is not None]
        return []

    def _yield_value_go(self, node) -> str:
        """Go value a ``yield`` (``yield_expr``) hands to the consumer."""
        if not node.children or node.children[0] is None:
            return "nil"
        value_node = node.children[0]
        if isinstance(value_node, Tree) and value_node.data == "tuple":
            # ``yield ("a", 1)``: one tuple value per resume.
            return self._typed_value_to_go(value_node, "[]interface{}")
        if self._generator_elem_type:
            # ``yield [1, 2]`` in a ``-> generator[list[int]]``.
            return self._typed_value_to_go(value_node, self._generator_elem_type)
        return self._expr_to_go(value_node)

    def _generator_call_elem_go_type(self, node) -> str:
        if isinstance(node, Tree) and node.data == "funccall" and node.children:
            callee = node.children[0]
            if isinstance(callee, Tree) and callee.data == "var" and callee.children:
                name = self._get_name(callee.children[0])
                if name in self._nested_generator_elems and name in self.declared_vars:
                    elem = self._nested_generator_elems[name]
                    return "" if elem == "interface{}" else elem
                return self._generator_elem_go_types.get(name, "")
        return ""

    def _list_comp_static_elem_go_type(self, node) -> str:
        """Go element type of ``[expr for x in xs ...]`` from the iterables'
        types (lists, strings, dict items, ``range``, ``enumerate``), without
        emitting anything; ``""`` when unknown or ``interface{}``."""
        comp = node.children[0] if isinstance(node, Tree) and node.children else None
        if not (isinstance(comp, Tree) and comp.data == "comprehension" and len(comp.children) >= 2):
            return ""
        result_expr, comp_fors = comp.children[0], comp.children[1]
        clauses = [c for c in getattr(comp_fors, "children", []) if isinstance(c, Tree) and c.data == "comp_for"]
        if not clauses or not isinstance(result_expr, Tree) or result_expr.data == "star_expr":
            return ""
        # A ``?`` in the comprehension is typed by its payload
        # (``[load(r)?.name for r in rows]`` is a ``[]string``); an unknown
        # payload leaves the element unknown.
        saved = self._save_local_types()

        def bind(name_node, go_type: str) -> bool:
            names = sorted(narrowing.target_names(name_node))
            if not names and isinstance(name_node, Tree) and name_node.data == "var" and self._get_name(name_node.children[0]) == "_":
                return True
            if len(names) != 1 or not go_type or go_type == "interface{}":
                return False
            if names[0] != "_":
                self._var_go_types[names[0]] = go_type
                elem_class = self._hier_class_from_go_type(go_type) if go_type.startswith("*") else ""
                if elem_class:
                    self._var_types[names[0]] = elem_class
                else:
                    self._var_types.pop(names[0], None)
            return True

        try:
            for clause in clauses:
                var_expr, iter_expr = clause.children[0], clause.children[1]
                targets = self._comprehension_tuple_raw_targets(var_expr)
                callee = iter_expr.children[0] if isinstance(iter_expr, Tree) and iter_expr.data == "funccall" else None
                callee_name = (
                    self._get_name(callee.children[0])
                    if isinstance(callee, Tree) and callee.data == "var" and callee.children else ""
                )
                if callee_name in {"range", "enumerate"} and callee_name not in self._user_functions:
                    if callee_name == "range":
                        if targets or not bind(var_expr, "int"):
                            return ""
                        continue
                    raw = self._get_raw_call_args(iter_expr.children[1] if len(iter_expr.children) > 1 else None)
                    src_type = (self._expr_go_type_hint(raw[0]) or self._infer_type_from_value(raw[0]) or "") if raw else ""
                    if len(targets) != 2 or not src_type.startswith("[]"):
                        return ""
                    if not (bind(targets[0], "int") and bind(targets[1], src_type[2:])):
                        return ""
                    continue
                if not targets and (self._is_generator_call(iter_expr) or self._is_chan_iterable(iter_expr)):
                    # ``[g for g in gen(3)][1]``: the generator's element type.
                    elem = self._generator_iter_elem_go_type(iter_expr) or self._generator_call_elem_go_type(iter_expr)
                    if not bind(var_expr, elem):
                        return ""
                    continue
                iter_type = (self._expr_go_type_hint(iter_expr) or self._infer_type_from_value(iter_expr) or "").strip()
                if targets:
                    key_type, value_type = self._split_go_map_type(iter_type)
                    if len(targets) != 2 or not key_type or self._is_go_set_type(iter_type):
                        return ""
                    if not (bind(targets[0], key_type) and bind(targets[1], value_type)):
                        return ""
                    continue
                if iter_type.startswith("[]"):
                    elem = iter_type[2:]
                elif iter_type == "string":
                    elem = "string"
                elif iter_type.startswith("map["):
                    elem = self._split_go_map_type(iter_type)[0]
                else:
                    return ""
                if not bind(var_expr, elem):
                    return ""
            result = (self._expr_go_type_hint(result_expr) or self._infer_type_from_value(result_expr) or "").strip()
        finally:
            self._restore_local_types(saved)
        if not result or result == "interface{}" or result.startswith("("):
            return ""
        return result

    def _int_power_operands(self, node) -> bool:
        """Whether both operands of ``a ** b`` are ints (literals without a
        fraction or exponent, or int-typed expressions)."""
        if not isinstance(node, Tree) or len(node.children) < 2 or node.children[1] is None:
            return False

        def is_int(operand) -> bool:
            if not isinstance(operand, Tree):
                return False
            if operand.data == "number" and operand.children:
                text = str(operand.children[0]).lower().replace("_", "")
                return text.startswith(("0x", "0o", "0b")) or not any(c in text for c in ".e")
            known = self._expr_go_type_hint(operand) or self._infer_type_from_value(operand) or ""
            return known == "int"

        return is_int(node.children[0]) and is_int(node.children[1])

    def _dict_comp_static_go_type(self, node) -> str:
        """Go map type of ``{k: v for x in xs ...}`` from the iterables'
        types (as ``_list_comp_static_elem_go_type`` for the key and the
        value); ``""`` when either is unknown or ``interface{}``."""
        comp = node.children[0] if isinstance(node, Tree) and node.children else None
        if not (isinstance(comp, Tree) and comp.data == "comprehension" and len(comp.children) >= 2):
            return ""
        kv = comp.children[0]
        if not (isinstance(kv, Tree) and kv.data == "key_value" and len(kv.children) >= 2):
            return ""
        rest = list(comp.children[1:])
        parts = []
        for expr in kv.children[:2]:
            probe = Tree("list_comprehension", [Tree("comprehension", [expr] + rest)])
            parts.append(self._list_comp_static_elem_go_type(probe))
        if not all(parts):
            return ""
        return f"map[{parts[0]}]{parts[1]}"

    def _comprehension_tuple_raw_targets(self, target) -> list:
        if not isinstance(target, Tree):
            return []
        if target.data in {"typed_for_target", "untyped_for_target"} and target.children:
            return self._comprehension_tuple_raw_targets(target.children[0])
        if target.data in {"tuple", "exprlist"}:
            return [child for child in target.children if child is not None]
        return []

    def _append_comprehension_loop(self, lines: List[str], clause: Tree, depth: int) -> None:
        self._append_comprehension_loop_inner(lines, clause, depth)
        var_expr = clause.children[0]
        if isinstance(var_expr, Tree) and var_expr.data == "var" and var_expr.children:
            # ``[r.unwrap() * 2 for r in results()]`` over ``Result[int]``
            # elements: the variable keeps the payload.
            name = self._go_binding_name(self._get_name(var_expr.children[0]))
            payload = self._iterable_result_payload_go_type(clause.children[1])
            if payload and payload != "interface{}" and self._var_go_types.get(name) in (None, "", "*Result"):
                self._var_go_types[name] = "*Result"
                self._var_result_payloads[name] = payload

    def _append_comprehension_loop_inner(self, lines: List[str], clause: Tree, depth: int) -> None:
        var_expr = clause.children[0]
        iter_expr = clause.children[1]
        # The loop variables are locals of the comprehension, even when a
        # function has their name.
        for name in narrowing.target_names(var_expr):
            self.declared_vars.add(self._go_binding_name(name))
        var_str = self._expr_to_go(var_expr)
        targets = self._comprehension_tuple_targets(var_expr)
        range_info = self._check_range_call(iter_expr) if isinstance(iter_expr, Tree) and iter_expr.data == "funccall" else None
        tabs = "\t" * (depth + 1)
        if range_info:
            start, end, step = range_info
            if var_str == "_":
                # Go has no ``_`` loop counter; the value is unused anyway.
                var_str = f"_lamCompIndex{depth}"
            else:
                # ``[max(7, i) for i in range(4)]``: the counter is an int
                # (not the type of a same-named local outside).
                self._var_go_types[var_str] = "int"
                self._var_types.pop(var_str, None)
            if self._is_int_literal_go(step) and step.strip("() ") not in ("0", "-0"):
                # A literal step: its sign picks the comparison.
                cmp = ">" if step.strip().lstrip("(").lstrip().startswith("-") else "<"
                lines.append(f"{tabs}for {var_str} := {start}; {var_str} {cmp} {end}; {var_str} += {step} {{")
            else:
                # ``range(a, b, s)`` with ``s`` known at run time: checked
                # (``ValueError`` for 0) and evaluated once, like the bound.
                end_v, step_v = f"_lamCompEnd{depth}", f"_lamCompStep{depth}"
                lines.append(
                    f"{tabs}for {var_str}, {end_v}, {step_v} := {start}, {end}, lamRangeStep({step}); "
                    f"({step_v} > 0 && {var_str} < {end_v}) || ({step_v} < 0 && {var_str} > {end_v}); {var_str} += {step_v} {{"
                )
            return
        bind_tabs = "\t" * (depth + 2)

        def silence(names) -> None:
            # Comprehension targets may be unused (``[k for k, v in d]``).
            for name in names:
                if name and name != "_":
                    lines.append(f"{bind_tabs}_ = {name}")

        enumerate_src = self._check_enumerate_call(iter_expr)
        if enumerate_src is not None and len(targets) == 2:
            # ``[f"{i}:{w}" for i, w in enumerate(words)]``.
            raw_src = self._get_raw_call_args(iter_expr.children[1] if len(iter_expr.children) > 1 else None)
            src_node = raw_src[0] if raw_src else None
            src_type = (self._expr_go_type_hint(src_node) or self._infer_type_from_value(src_node) or "") if src_node is not None else ""
            if isinstance(src_node, Tree) and src_node.data == "list" and src_type.startswith("[]") and src_type != "[]interface{}":
                enumerate_src = self._typed_value_to_go(src_node, src_type)
            idx, val = targets
            # ``enumerate(xs, start)``: bind ``idx`` from the raw position.
            start_go = self._enumerate_start_go(
                iter_expr, lambda name, value: lines.insert(1, f"\t{name} := {value}"),
            ) if idx != "_" else ""
            if start_go:
                user_idx, idx = idx, f"{idx}__lamRaw"
                self._var_go_types[user_idx] = "int"
            raw_targets = self._comprehension_tuple_raw_targets(var_expr)
            nested = raw_targets[1] if len(raw_targets) == 2 else None
            if isinstance(nested, Tree) and nested.data in {"tuple", "exprlist"}:
                # ``for i, (name, n) in enumerate(pairs)``: unpack the item.
                inner = [self._expr_to_go(c) for c in nested.children if c is not None]
                item = f"_lamCompItem{depth}"
                lines.append(f"{tabs}for {idx}, {item} := range {enumerate_src} {{")
                if start_go:
                    lines.append(f"{bind_tabs}{user_idx} := {idx} + {start_go}")
                    idx = user_idx
                if idx != "_":
                    self._var_go_types[idx] = "int"
                src_name = self._get_name(src_node.children[0]) if isinstance(src_node, Tree) and src_node.data == "var" else ""
                tuple_types = self._tuple_iterable_element_types.get(src_name, [])
                for index, name in enumerate(inner):
                    if name == "_":
                        continue
                    cast = tuple_types[index] if index < len(tuple_types) and tuple_types[index] != "interface{}" else ""
                    lines.append(f"{bind_tabs}{name} := {item}[{index}]" + (f".({cast})" if cast else ""))
                    if cast:
                        self._var_go_types[name] = cast
                silence([idx] + inner)
                return
            lines.append(f"{tabs}for {idx}, {val} := range {enumerate_src} {{")
            if val != "_" and src_type.startswith("[]"):
                self._var_go_types[val] = src_type[2:]
            elif val != "_" and src_type == "string":
                lines[-1] = f"{tabs}for {idx}, {val}Rune := range []rune({enumerate_src}) {{"
                lines.append(f"{bind_tabs}{val} := string({val}Rune)")
                self._var_go_types[val] = "string"
            if start_go:
                lines.append(f"{bind_tabs}{user_idx} := {idx} + {start_go}")
                idx = user_idx
            if idx != "_":
                self._var_go_types[idx] = "int"
            silence([idx, val])
            return
        # ``return Result.Ok([f(r) for r in conn.query(q)?])``: the iterable's
        # ``?`` payload is not the enclosing statement's type.
        with self._scoped(_propagate_cast_hint=""):
            iter_str = self._expr_to_go(iter_expr)
        if self._is_generator_call(iter_expr) or self._is_chan_iterable(iter_expr):
            # A generator (or a channel): one value per resume.
            iter_str += self._generator_range_method(iter_expr)
            chan_elem = self._generator_iter_elem_go_type(iter_expr)
            if targets:
                pair_name = f"_lamCompPair{depth}"
                lines.append(f"{tabs}for {pair_name} := range {iter_str} {{")
                # A ``chan []interface{}`` (``generator[tuple[...]]``) yields
                # slices; a dynamic channel yields ``interface{}`` values.
                pair_expr = pair_name if chan_elem.startswith("[]") else f"{pair_name}.([]interface{{}})"
                for index, target in enumerate(targets):
                    if target != "_":
                        lines.append(f"{bind_tabs}{target} := {pair_expr}[{index}]")
                silence(targets)
            else:
                name = var_str if var_str != "_" else f"_lamCompItem{depth}"
                elem = "" if chan_elem else self._generator_call_elem_go_type(iter_expr)
                if chan_elem and name == var_str:
                    # ``[c.title for c in circles()]``: the channel is typed.
                    lines.append(f"{tabs}for {name} := range {iter_str} {{")
                    self._var_go_types[name] = chan_elem
                    if chan_elem.startswith("*"):
                        class_name = self._hier_class_from_go_type(chan_elem) or chan_elem[1:].split("[", 1)[0]
                        if class_name in self._class_names:
                            self._var_types[name] = class_name
                elif elem and name == var_str:
                    # ``for b in batches()`` with ``-> generator[list[int]]``.
                    lines.append(f"{tabs}for {name}Raw := range {iter_str} {{")
                    lines.append(f"{bind_tabs}{name} := {name}Raw.({elem})")
                    self._var_go_types[name] = elem
                else:
                    lines.append(f"{tabs}for {name} := range {iter_str} {{")
                silence([name])
            return
        literal_typed = False
        if isinstance(iter_expr, Tree) and iter_expr.data == "list" and not targets:
            # ``[i for i in [1, 2, 3] if i > 1]``: range over the typed slice
            # so the variable has the elements' type.
            literal_type = self._infer_type_from_value(iter_expr)
            if literal_type.startswith("[]") and literal_type != "[]interface{}":
                iter_str = self._typed_value_to_go(iter_expr, literal_type)
                self._var_go_types[var_str] = literal_type[2:]
                literal_typed = True
        if len(targets) == 2 and self._range_single_target_uses_key(iter_expr):
            map_type = self._expr_go_type_hint(iter_expr) or self._infer_type_from_value(iter_expr) or ""
            dict_literal_type = self._uniform_dict_literal_type(iter_expr)
            if dict_literal_type:
                # ``[f"{k}:{v}" for k, v in {"k": "v"}]``: range over the
                # typed map the targets are typed from.
                iter_str = self._typed_dict_literal_to_go(iter_expr, dict_literal_type)
                map_type = dict_literal_type
            lines.append(f"{tabs}for {targets[0]}, {targets[1]} := range {iter_str} {{")
            key_type, value_type = self._split_go_map_type(map_type)
            if key_type and targets[0] != "_":
                self._var_go_types[targets[0]] = key_type
            if value_type and targets[1] != "_":
                self._var_go_types[targets[1]] = value_type
            silence(targets)
            return
        if targets:
            pair_name = f"_lamCompPair{depth}"
            lines.append(f"{tabs}for _, {pair_name} := range {iter_str} {{")
            iter_name = self._get_name(iter_expr.children[0]) if isinstance(iter_expr, Tree) and iter_expr.data == "var" else ""
            tuple_types = self._tuple_iterable_element_types.get(iter_name, [])
            for index, target in enumerate(targets):
                if target == "_":
                    continue
                cast = f".({tuple_types[index]})" if index < len(tuple_types) and tuple_types[index] != "interface{}" else ""
                lines.append(f"{bind_tabs}{target} := {pair_name}[{index}]{cast}")
                if cast:
                    self._var_go_types[target] = tuple_types[index]
            silence(targets)
            return
        if not targets and var_str != "_":
            # ``[o.name for o in orders]``: the variable has the element type.
            iter_type = self._expr_go_type_hint(iter_expr) or ""
            if iter_type.startswith("[]") and iter_type != "[]interface{}":
                self._var_go_types[var_str] = iter_type[2:]
                elem_class = self._hier_class_from_go_type(iter_type[2:])
                if elem_class:
                    self._var_types[var_str] = elem_class
                else:
                    self._var_types.pop(var_str, None)
            elif not literal_typed:
                # Unknown element type: not the type of a same-named local
                # outside the comprehension.
                self._var_go_types.pop(var_str, None)
                self._var_types.pop(var_str, None)
        if var_str == "_" and not targets:
            # ``[0 for _ in xs]``: Go has no ``_ :=`` binding.
            lines.append(f"{tabs}for range {iter_str} {{")
        elif self._range_single_target_uses_key(iter_expr):
            lines.append(f"{tabs}for {var_str} := range {iter_str} {{")
            silence([var_str])
        elif self._iterates_string(iter_expr):
            # Lam yields one-character strings, Go ranges over runes.
            lines.append(f"{tabs}for _, {var_str}Rune := range {iter_str} {{")
            lines.append(f"{chr(9) * (depth + 2)}{var_str} := string({var_str}Rune)")
            self._var_go_types[var_str] = "string"
            silence([var_str])
        else:
            lines.append(f"{tabs}for _, {var_str} := range {iter_str} {{")
            # ``[0 for x in xs]``: the element is unused.
            silence([var_str])

    @staticmethod
    def _positional_raw_args(args_node) -> list:
        """The positional argument nodes of a call (keywords left out)."""
        if not isinstance(args_node, Tree) or args_node.data != "arguments":
            return []
        out = []
        for child in args_node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "argvalue":
                if len(child.children) == 1 and isinstance(child.children[0], Tree):
                    out.append(child.children[0])
                continue
            out.append(child)
        return out

    def _minmax_key_plan(self, raw_args: list):
        """``(slice_type, source)`` of the values ``min``/``max`` with a
        ``key`` choose from: one list argument (``max(shapes, key=...)``) or
        the arguments themselves (``max(a, b, key=...)``); ``None`` when no
        argument."""
        if not raw_args:
            return None
        if len(raw_args) == 1:
            node = raw_args[0]
            slice_type = (self._expr_go_type_hint(node) or self._infer_type_from_value(node) or "").strip()
            if not slice_type.startswith("[]"):
                slice_type = "[]interface{}"
            return slice_type, node
        types = {
            (self._expr_go_type_hint(n) or self._infer_type_from_value(n) or "").strip()
            for n in raw_args
        }
        elem = types.pop() if len(types) == 1 else "interface{}"
        return f"[]{elem or 'interface{}'}", raw_args

    def _minmax_key_to_go(self, name: str, args_node) -> str:
        """``min(xs, key=fn)`` / ``max(a, b, key=fn)``: the first value with
        the smallest / largest ``fn(value)`` (keys compare like ``sorted``'s);
        an empty list throws ``ValueError``."""
        plan = self._minmax_key_plan(self._positional_raw_args(args_node))
        if plan is None:
            return ""
        slice_type, source_node = plan
        elem = self._slice_elem_go_type(slice_type) or "interface{}"
        if isinstance(source_node, list):
            source = f"{slice_type}{{{', '.join(self._typed_value_to_go(n, elem) for n in source_node)}}}"
        else:
            source = self._typed_value_to_go(source_node, slice_type)
        key_node = self._raw_call_kwargs(args_node)["key"]
        if self._is_lambda_node(key_node):
            key_type = self._lambda_result_go_type(key_node, [elem]) or "interface{}"
        else:
            split = self._split_go_func_type(self._expr_go_type_hint(key_node) or "")
            key_type = split[1] if split else ""
            if not key_type and isinstance(key_node, Tree) and key_node.data == "var":
                key_type = self._func_return_go_types.get(self._get_name(key_node.children[0]), "")
            key_type = key_type or "interface{}"
        key_fn = self._typed_value_to_go(key_node, f"func({elem}) {key_type}")
        ordered = {
            "int", "int8", "int16", "int32", "int64",
            "uint", "uint8", "uint16", "uint32", "uint64",
            "float32", "float64", "string",
        }
        prologue = ""
        if key_type in ordered:
            better = "k < bk" if name == "min" else "k > bk"
        elif key_type == "bool":
            better = "!k && bk" if name == "min" else "k && !bk"
        elif self._orders_dynamically(key_type):
            prologue = f"_c := {self._dynamic_compare_go()}; "
            better = "_c(k, bk) < 0" if name == "min" else "_c(k, bk) > 0"
        else:
            self._need_import("fmt")
            prologue = "_s := func(v interface{}) string { return fmt.Sprintf(\"%v\", v) }; "
            better = "_s(k) < _s(bk)" if name == "min" else "_s(k) > _s(bk)"
        return (
            f"func(xs {slice_type}) {elem} {{ if len(xs) == 0 {{ "
            f"panic(\"ValueError: {name}() of an empty list\") }}; "
            f"key := {key_fn}; {prologue}best := xs[0]; bk := key(best); "
            f"for _, v := range xs[1:] {{ k := key(v); if {better} {{ best, bk = v, k }} }}; "
            f"return best }}({source})"
        )

    def _numeric_builtin_go_type(self, name: str, call: Tree) -> str:
        """Go result type of ``abs`` / ``min`` / ``max`` (matching
        ``_numeric_builtin_to_go``), ``""`` when unknown."""
        args_node = call.children[1] if len(call.children) > 1 else None
        if isinstance(args_node, Tree) and args_node.data == "arguments":
            raw = [c for c in args_node.children if isinstance(c, Tree)]
        elif isinstance(args_node, Tree):
            raw = [args_node]
        else:
            return ""
        if name in {"min", "max"} and self._raw_call_kwargs(args_node).get("key") is not None:
            # ``max(shapes, key=lambda s: s.area)``: one of the values.
            plan = self._minmax_key_plan(self._positional_raw_args(args_node))
            return plan[0][2:] if plan and plan[0] != "[]interface{}" else ""
        if any(c.data in {"argvalue", "starargs", "kwargs", "comprehension"} for c in raw):
            return ""
        types = [self._expr_go_type_hint(c) or self._infer_type_from_value(c) or "" for c in raw]
        ints = {"int", "int64", "int32"}
        if not types:
            return ""
        if name == "abs":
            if types[0] in ints:
                return types[0]
            return "float64" if types[0] in {"float64", "float32"} else ""
        if len(types) == 1 and types[0].startswith("[]"):
            elem = self._slice_elem_go_type(types[0])
            return elem if elem != "interface{}" else ""
        if all(t in ints for t in types) and len(set(types)) == 1:
            return types[0]
        if all(t in ints | {"float64", "float32"} for t in types):
            return "float64"
        if all(t == "string" for t in types):
            return "string"
        if name in {"min", "max"} and len(set(types)) == 1 and types[0] in self._generic_names:
            # ``max(a, b)`` with ``a, b: T`` (``T: ordered``) is a ``T``.
            return types[0]
        return ""

    def _numeric_builtin_to_go(self, name: str, args: List[str], raw: list) -> str:
        """``min``/``max``/``abs`` by argument type: ints stay int (Go's
        builtin ``min``/``max``), any float widens to float64, strings compare
        lexicographically, one slice argument uses ``slices.Min``/``Max``."""
        types = [
            (self._expr_go_type_hint(node) or self._infer_type_from_value(node) or "") if isinstance(node, Tree) else ""
            for node in raw
        ]
        ints = {"int", "int64", "int32"}
        if name == "abs":
            if types and types[0] in ints:
                return f"func(v {types[0]}) {types[0]} {{ if v < 0 {{ return -v }}; return v }}({args[0]})"
            self._need_import("math")
            return f"math.Abs(float64({args[0]}))"
        if len(args) == 1 and types and types[0].startswith("[]"):
            elem = self._slice_elem_go_type(types[0])
            if self._orders_dynamically(elem):
                # ``min(xs)`` of a ``list[any]`` / ``list[json]``: by kind.
                better = "< 0" if name == "min" else "> 0"
                return (
                    f"func(xs {types[0]}) {elem} {{ if len(xs) == 0 {{ "
                    f"panic(\"ValueError: {name}() of an empty list\") }}; "
                    f"_c := {self._dynamic_compare_go()}; best := xs[0]; "
                    f"for _, v := range xs[1:] {{ if _c(v, best) {better} {{ best = v }} }}; return best }}({args[0]})"
                )
            self._need_import("slices")
            source = args[0]
            if isinstance(raw[0], Tree) and raw[0].data == "list":
                # ``min([4, 1, 9])``: the literal typed like its elements
                # (a ``[]interface{}`` is not ``cmp.Ordered``).
                source = self._typed_value_to_go(raw[0], types[0])
            return f"slices.{name.capitalize()}({source})"
        if types and all(t in ints for t in types) and len(set(types)) == 1:
            return f"{name}({', '.join(args)})"
        if types and all(t in ints | {"float64", "float32"} for t in types):
            return f"{name}({', '.join(f'float64({a})' for a in args)})"
        if types and all(t == "string" for t in types):
            return f"{name}({', '.join(args)})"
        if len(args) >= 2 and len(set(types)) == 1 and types[0] in self._generic_names:
            # ``max(a, b)`` with ``a, b: T`` and ``T: ordered``: Go's
            # builtin ``max`` / ``min`` take any ``cmp.Ordered`` type.
            return f"{name}({', '.join(args)})"
        if len(args) >= 2:
            # Unknown operand types: compare as floats (previous behaviour).
            return f"{name}({', '.join(self._unknown_conversion_to_go('float', a) if t in {'', 'interface{}'} else f'float64({a})' for a, t in zip(args, types))})"
        return ""

    def _iterates_string(self, iter_expr) -> bool:
        return (self._expr_go_type_hint(iter_expr) or self._infer_type_from_value(iter_expr)) == "string"

    def _is_lam_local_node(self, node) -> bool:
        """``node`` names a Lam local / parameter in scope (``http`` in
        ``http: HttpRequestOptions``), not a Go package."""
        if not (isinstance(node, Tree) and node.data == "var" and node.children):
            return False
        name = self._get_name(node.children[0])
        return (
            name in self.declared_vars or self._go_binding_name(name) in self.declared_vars
            or name in self._var_go_types
        )

    def _emit_q_guard(self, tmp: str) -> None:
        """The early exit of ``?`` for the ``*Result`` temp ``tmp`` in the
        current lowering context."""
        if self._comp_q_return:
            # Inside a comprehension closure with a ``?``: return the error
            # beside the zero result; the statement prelude propagates it.
            self._emit(f"if !{tmp}.Ok() {{ return {self._comp_q_return}, {tmp} }}")
        elif self._q_propagate_ok and self._guard_q_var:
            # Inside a match guard's condition closure: record the error
            # and select the case, whose body propagates it.
            self._emit(f"if !{tmp}.Ok() {{ {self._guard_q_var} = {tmp}; return true }}")
        elif self._q_propagate_ok and self._sm_emit_q_guard(tmp):
            # In a ``do`` body holding a ``yield``: the ``do`` handler runs.
            return
        elif self._q_propagate_ok:
            if self._in_async_func and self._async_chan_name:
                self._emit(
                    f"if !{tmp}.Ok() {{ "
                    f"{self._async_chan_name} <- func() *Result {{ return {tmp} }}; return "
                    f"}}"
                )
            elif self._in_try_iife and (self._in_try_func or self._do_q_target):
                # Inside a try IIFE: leave it, then the enclosing function
                # (its named result) or ``do`` closure returns the error.
                target = "retval" if self._in_try_func else self._do_q_target
                self._emit(f"if !{tmp}.Ok() {{ {target} = {tmp}; __lamShouldReturn = true; return }}")
            else:
                self._emit(f"if !{tmp}.Ok() {{ return {tmp} }}")
        else:
            self._emit(
                f"if !{tmp}.Ok() {{ "
                f"panic({tmp}.Error) "
                f"}}"
            )

    @contextmanager
    def _capture_emits(self, lines: list):
        """Collect the statements emitted meanwhile (the prelude of a ``?``)
        in ``lines``, unindented, instead of the output."""
        saved_output, saved_indent = self.output_lines, self.indent
        self.output_lines, self.indent = lines, 0
        try:
            yield
        finally:
            self.output_lines, self.indent = saved_output, saved_indent

    def _comp_q_convert(self, zero: str, convert):
        """``convert()`` for an element / filter of a comprehension holding a
        ``?``: ``(prelude_lines, go)``, the ``?`` returning ``zero`` and its
        error from the comprehension closure."""
        prelude: list = []
        with self._capture_emits(prelude), self._scoped(_comp_q_return=zero):
            go = convert()
        return prelude, go

    def _comp_q_finish(self, lines: list, comp_type: str) -> str:
        """Emit the closure ``lines`` of a comprehension holding a ``?`` (it
        returns ``(value, *Result)``) as a statement prelude binding the value,
        and propagate its error like a ``?``; the value's name."""
        self._q_temp_counter += 1
        value = f"__lamComp{self._q_temp_counter}"
        err = f"__lamCompErr{self._q_temp_counter}"
        self._emit(f"{value}, {err} := func() ({comp_type}, *Result) {{")
        for line in lines[1:]:
            self._emit(line)
        self._emit_q_guard(err)
        self._declare_var(value)
        self._declare_var(err)
        return value

    def _list_comp_to_go(self, node: Tree, elem_type: str = None) -> str:
        # Comprehension variables are declared only inside the closure.
        saved = set(self.declared_vars)
        saved_types = self._save_local_types()
        try:
            return self._list_comp_to_go_body(node, elem_type)
        finally:
            self.declared_vars = saved
            self._restore_local_types(saved_types)

    def _list_comp_to_go_body(self, node: Tree, elem_type: str = None) -> str:
        comp = node.children[0]
        slice_type = elem_type if elem_type else "[]interface{}"
        if not isinstance(comp, Tree) or comp.data != "comprehension":
            return f"{slice_type}{{}}"

        result_expr = comp.children[0]
        comp_fors = comp.children[1]
        comp_if = comp.children[2] if len(comp.children) > 2 else None

        for_clauses = []
        if isinstance(comp_fors, Tree) and comp_fors.data == "comp_fors":
            for child in comp_fors.children:
                if isinstance(child, Tree) and child.data == "comp_for":
                    for_clauses.append(child)

        if not for_clauses:
            return f"{slice_type}{{}}"

        lines = [f"func() {slice_type} {{"]
        lines.append(f"\t_result := {slice_type}{{}}")

        depth = 0
        for fc in for_clauses:
            self._append_comprehension_loop(lines, fc, depth)
            depth += 1

        # The loop variables are typed now (``_append_comprehension_loop``).
        result_type = self._slice_elem_go_type(slice_type)
        plan = self._nr_comprehension_plan(comp_if, [result_expr], for_clauses)
        # ``[half(n)? for n in xs]``: the ``?`` runs inside the loop.
        has_q = self._contains_propagate(result_expr) or self._contains_propagate(comp_if)
        result_prelude: list = []
        cond_prelude: list = []
        with self._nr_registered(plan):
            def convert_result():
                return (
                    self._typed_value_to_go(result_expr, result_type)
                    if result_type else self._expr_to_go(result_expr)
                )
            if has_q:
                result_prelude, result_str = self._comp_q_convert("nil", convert_result)
            else:
                result_str = convert_result()

        inner_tabs = "\t" * (depth + 1)
        if comp_if and isinstance(comp_if, Tree):
            if has_q:
                cond_prelude, cond_str = self._comp_q_convert("nil", lambda: self._typed_value_to_go(comp_if, "bool"))
            else:
                cond_str = self._expr_to_go(comp_if)
            lines.extend(f"{inner_tabs}{line}" for line in cond_prelude)
            lines.append(f"{inner_tabs}if {cond_str} {{")
            lines.extend(f"{inner_tabs}\t{line}" for _n, _g, _t, binds in plan for line in binds)
            lines.extend(f"{inner_tabs}\t{line}" for line in result_prelude)
            lines.append(f"{inner_tabs}\t_result = append(_result, {result_str})")
            lines.append(f"{inner_tabs}}}")
        else:
            lines.extend(f"{inner_tabs}{line}" for line in result_prelude)
            lines.append(f"{inner_tabs}_result = append(_result, {result_str})")

        for i in range(depth):
            tabs = "\t" * (depth - i)
            lines.append(f"{tabs}}}")

        if has_q:
            lines.append("\treturn _result, Result_Ok(nil)")
            lines.append("}()")
            return self._comp_q_finish(lines, slice_type)
        lines.append(f"\treturn _result")
        lines.append(f"}}()")

        return self._pin_multiline_to(node, "\n".join(lines))

    # ─── Set comprehension ─────────────────────────────────────

    def _set_comp_to_go(self, node: Tree, map_type: str = None) -> str:
        # Comprehension variables are declared only inside the closure.
        saved = set(self.declared_vars)
        saved_types = self._save_local_types()
        try:
            return self._set_comp_to_go_body(node, map_type)
        finally:
            self.declared_vars = saved
            self._restore_local_types(saved_types)

    def _set_comp_to_go_body(self, node: Tree, map_type: str = None) -> str:
        # ``{f(x) for x in xs if cond}`` lowers to a `map[interface{}]bool`
        # unless an enclosing typed context supplies a concrete set type.
        comp = node.children[0]
        set_type = map_type if map_type else "map[interface{}]bool"
        if not isinstance(comp, Tree) or comp.data != "comprehension":
            return f"{set_type}{{}}"

        result_expr = comp.children[0]
        comp_fors = comp.children[1]
        comp_if = comp.children[2] if len(comp.children) > 2 else None

        for_clauses = []
        if isinstance(comp_fors, Tree) and comp_fors.data == "comp_fors":
            for child in comp_fors.children:
                if isinstance(child, Tree) and child.data == "comp_for":
                    for_clauses.append(child)

        if not for_clauses:
            return f"{set_type}{{}}"

        lines = [f"func() {set_type} {{"]
        lines.append(f"\t_result := {set_type}{{}}")

        depth = 0
        for fc in for_clauses:
            self._append_comprehension_loop(lines, fc, depth)
            depth += 1

        key_type, _value_type = self._split_go_map_type(set_type)
        plan = self._nr_comprehension_plan(comp_if, [result_expr], for_clauses)
        has_q = self._contains_propagate(result_expr) or self._contains_propagate(comp_if)
        result_prelude: list = []
        cond_prelude: list = []
        with self._nr_registered(plan):
            def convert_result():
                return (
                    self._typed_value_to_go(result_expr, key_type)
                    if key_type else self._expr_to_go(result_expr)
                )
            if has_q:
                result_prelude, result_str = self._comp_q_convert("nil", convert_result)
            else:
                result_str = convert_result()

        inner_tabs = "\t" * (depth + 1)
        if comp_if and isinstance(comp_if, Tree):
            if has_q:
                cond_prelude, cond_str = self._comp_q_convert("nil", lambda: self._typed_value_to_go(comp_if, "bool"))
            else:
                cond_str = self._expr_to_go(comp_if)
            lines.extend(f"{inner_tabs}{line}" for line in cond_prelude)
            lines.append(f"{inner_tabs}if {cond_str} {{")
            lines.extend(f"{inner_tabs}\t{line}" for _n, _g, _t, binds in plan for line in binds)
            lines.extend(f"{inner_tabs}\t{line}" for line in result_prelude)
            lines.append(f"{inner_tabs}\t_result[{result_str}] = true")
            lines.append(f"{inner_tabs}}}")
        else:
            lines.extend(f"{inner_tabs}{line}" for line in result_prelude)
            lines.append(f"{inner_tabs}_result[{result_str}] = true")

        for i in range(depth):
            tabs = "\t" * (depth - i)
            lines.append(f"{tabs}}}")

        if has_q:
            lines.append("\treturn _result, Result_Ok(nil)")
            lines.append("}()")
            return self._comp_q_finish(lines, set_type)
        lines.append(f"\treturn _result")
        lines.append(f"}}()")

        return self._pin_multiline_to(node, "\n".join(lines))

    # ─── Dict comprehension ───────────────────────────────────

    def _dict_comp_to_go(self, node: Tree, map_type: str = None) -> str:
        # Comprehension variables are declared only inside the closure.
        saved = set(self.declared_vars)
        saved_types = self._save_local_types()
        try:
            return self._dict_comp_to_go_body(node, map_type)
        finally:
            self.declared_vars = saved
            self._restore_local_types(saved_types)

    def _dict_comp_to_go_body(self, node: Tree, map_type: str = None) -> str:
        comp = node.children[0]
        mtype = map_type if map_type else "map[interface{}]interface{}"
        if not isinstance(comp, Tree) or comp.data != "comprehension":
            return f"{mtype}{{}}"

        kv = comp.children[0]
        comp_fors = comp.children[1]
        comp_if = comp.children[2] if len(comp.children) > 2 else None

        if not (isinstance(kv, Tree) and kv.data == "key_value"):
            return f"{mtype}{{}}"

        for_clauses = [
            child
            for child in comp_fors.children
            if isinstance(child, Tree) and child.data == "comp_for"
        ]
        if not for_clauses:
            return f"{mtype}{{}}"

        lines = [f"func() {mtype} {{", f"\t_r := {mtype}{{}}"]
        depth = 0
        for clause in for_clauses:
            self._append_comprehension_loop(lines, clause, depth)
            depth += 1

        key_type, value_type = self._split_go_map_type(mtype)
        plan = self._nr_comprehension_plan(comp_if, [kv], for_clauses)
        has_q = self._contains_propagate(kv) or self._contains_propagate(comp_if)
        result_prelude: list = []
        cond_prelude: list = []
        with self._nr_registered(plan):
            def convert_entry():
                key_go = (
                    self._typed_value_to_go(kv.children[0], key_type)
                    if key_type else self._expr_to_go(kv.children[0])
                )
                value_go = (
                    self._typed_value_to_go(kv.children[1], value_type)
                    if value_type else self._expr_to_go(kv.children[1])
                )
                return key_go, value_go
            if has_q:
                result_prelude, (key_expr, val_expr) = self._comp_q_convert("nil", convert_entry)
            else:
                key_expr, val_expr = convert_entry()

        inner_tabs = "\t" * (depth + 1)
        if comp_if and isinstance(comp_if, Tree):
            if has_q:
                cond_prelude, cond_str = self._comp_q_convert("nil", lambda: self._typed_value_to_go(comp_if, "bool"))
            else:
                cond_str = self._expr_to_go(comp_if)
            lines.extend(f"{inner_tabs}{line}" for line in cond_prelude)
            lines.append(f"{inner_tabs}if {cond_str} {{")
            lines.extend(f"{inner_tabs}\t{line}" for _n, _g, _t, binds in plan for line in binds)
            lines.extend(f"{inner_tabs}\t{line}" for line in result_prelude)
            lines.append(f"{inner_tabs}\t_r[{key_expr}] = {val_expr}")
            lines.append(f"{inner_tabs}}}")
        else:
            lines.extend(f"{inner_tabs}{line}" for line in result_prelude)
            lines.append(f"{inner_tabs}_r[{key_expr}] = {val_expr}")

        for index in range(depth):
            tabs = "\t" * (depth - index)
            lines.append(f"{tabs}}}")
        if has_q:
            lines.extend(["\treturn _r, Result_Ok(nil)", "}()"])
            return self._comp_q_finish(lines, mtype)
        lines.extend(["\treturn _r", "}()"])
        return self._pin_multiline_to(node, "\n".join(lines))

    # ─── Binary ops / operator overloading ─────────────────────

    def _try_operator_overload(self, node: Tree) -> str:
        operands = []
        operators = []
        for child in node.children:
            if isinstance(child, Token):
                operators.append(str(child))
            elif isinstance(child, Tree):
                operands.append(child)

        if not operators or not operands:
            return ""

        cls = self._infer_expr_class(operands[0])
        if not cls:
            return ""

        result = self._expr_to_go(operands[0])
        all_overloaded = True
        for i, op in enumerate(operators):
            py_op = "//" if op == "//" else op
            dunder = OP_TO_DUNDER.get(py_op, "")
            method = self._get_dunder_method(cls, dunder) if dunder else ""
            if method and i + 1 < len(operands):
                right = self._expr_to_go(operands[i + 1])
                result = f"{result}.{method}({right})"
            else:
                all_overloaded = False
                break

        return result if all_overloaded else ""

    def _try_comparison_overload(self, node: Tree) -> str:
        exprs = []
        comp_ops = []
        for child in node.children:
            if isinstance(child, Tree) and child.data == "comp_op":
                ops = [str(t) for t in child.children]
                comp_ops.append(" ".join(ops))
            elif isinstance(child, Tree):
                exprs.append(child)

        if len(exprs) < 2 or not comp_ops:
            return ""

        cls = self._infer_expr_class(exprs[0])
        if not cls:
            return ""

        if len(comp_ops) == 1 and len(exprs) == 2:
            if comp_ops[0] in {"==", "!="} and (self._is_none_literal(exprs[0]) or self._is_none_literal(exprs[1])):
                # ``value == None`` on a class reference: a nil test, not
                # ``__eq__`` (which would run on a nil receiver).
                return ""
            dunder = CMP_TO_DUNDER.get(comp_ops[0], "")
            method = self._get_dunder_method(cls, dunder) if dunder else ""
            if method:
                left = self._expr_to_go(exprs[0])
                right = self._expr_to_go(exprs[1])
                return f"{left}.{method}({right})"

        return ""

    # ─── Python division (``/``, ``//``, ``%``) ─────────────────
    #
    # ``/`` is a float division, ``//`` floors and ``%`` takes the sign of
    # the divisor (ints and floats), and a zero divisor raises
    # ``ZeroDivisionError``. Go's ``/`` and ``%`` truncate (and a float
    # division by zero is an infinity), so the operation goes through a
    # small closure typed by the operands (Go inlines it) unless Go's
    # operator already agrees: a non-zero constant divisor of ``/``, or a
    # provably non-negative dividend with a provably positive divisor.

    _INT_FLOOR_DIV_GO = (
        "func(a, b {t}) {t} {{ if b == 0 {{ panic(\"ZeroDivisionError: integer division or modulo by zero\") }}; "
        "q := a / b; if a%b != 0 && (a < 0) != (b < 0) {{ q-- }}; return q }}({a}, {b})"
    )
    _INT_MOD_GO = (
        "func(a, b {t}) {t} {{ if b == 0 {{ panic(\"ZeroDivisionError: integer division or modulo by zero\") }}; "
        "r := a % b; if r != 0 && (r < 0) != (b < 0) {{ r += b }}; return r }}({a}, {b})"
    )
    _FLOAT_DIV_GO = (
        "func(a, b float64) float64 {{ if b == 0 {{ panic(\"ZeroDivisionError: division by zero\") }}; "
        "return a / b }}({a}, {b})"
    )
    # CPython's float floor division and modulo (``_float_div_mod``).
    _FLOAT_FLOOR_DIV_GO = (
        "func(a, b float64) float64 {{ if b == 0 {{ panic(\"ZeroDivisionError: float floor division by zero\") }}; "
        "m := math.Mod(a, b); d := (a - m) / b; if m != 0 && (b < 0) != (m < 0) {{ d -= 1 }}; "
        "if d == 0 {{ return math.Copysign(0, a/b) }}; f := math.Floor(d); if d-f > 0.5 {{ f += 1 }}; "
        "return f }}({a}, {b})"
    )
    _FLOAT_MOD_GO = (
        "func(a, b float64) float64 {{ if b == 0 {{ panic(\"ZeroDivisionError: float modulo\") }}; "
        "m := math.Mod(a, b); if m == 0 {{ return math.Copysign(0, b) }}; "
        "if (b < 0) != (m < 0) {{ m += b }}; return m }}({a}, {b})"
    )

    @staticmethod
    def _numeric_literal_value(node):
        """The value of a numeric constant operand (``7``, ``-7``, ``2.5``),
        ``None`` for anything else."""
        sign = 1
        if isinstance(node, Tree) and node.data == "factor" and len(node.children) == 2 \
                and str(node.children[0]) in {"-", "+"}:
            sign = -1 if str(node.children[0]) == "-" else 1
            node = node.children[1]
        if not (isinstance(node, Tree) and node.data == "number" and node.children):
            return None
        text = str(node.children[0]).replace("_", "")
        try:
            return sign * int(text, 0)
        except ValueError:
            pass
        try:
            return sign * float(text)
        except ValueError:
            return None

    def _division_operand_kind(self, node) -> str:
        """``float64`` / ``float32`` / an integer Go type for a typed
        operand, ``intlit`` / ``floatlit`` for a constant, ``param`` for a
        type parameter, ``""`` when unknown."""
        value = self._numeric_literal_value(node)
        if isinstance(value, bool):
            return ""
        if isinstance(value, int):
            return "intlit"
        if isinstance(value, float):
            return "floatlit"
        if isinstance(node, Tree) and node.data == "factor" and len(node.children) == 2 \
                and str(node.children[0]) in {"-", "+"} and not self._infer_expr_class(node.children[1]):
            return self._division_operand_kind(node.children[1])
        hint = self._expr_go_type_hint(node) or self._infer_type_from_value(node) or ""
        if hint in {"float64", "float32"} or hint in _GO_INT_TYPES:
            return hint
        if hint in self._generic_names:
            return "param"
        return ""

    @staticmethod
    def _is_float_kind(kind: str) -> bool:
        return kind in {"float64", "float32", "floatlit"}

    @staticmethod
    def _as_float64(go: str, kind: str) -> str:
        return go if kind in {"float64", "floatlit"} else f"float64({go})"

    def _division_step_to_go(self, left: str, left_kind: str, left_tree, op: str, right_node):
        """``left <op> right`` for ``op`` in ``/``, ``//``, ``%``, ``*``:
        ``(go, kind, native)`` where ``native`` means Go's own operator
        (``left <go op> right``) was used with ``right`` as written."""
        right = self._expr_to_go(right_node)
        if isinstance(right_node, Tree) and right_node.data in {"arith_expr", "term"}:
            right = f"({right})"
        else:
            right = go_operand(right, _GO_BINARY_PREC["*"], right=True)
        right_kind = self._division_operand_kind(right_node)
        right_value = self._numeric_literal_value(right_node)
        if op == "*":
            if self._is_float_kind(left_kind) and right_kind in _GO_INT_TYPES:
                return f"{left} * float64({right})", "float64", False
            if self._is_float_kind(right_kind) and left_kind in _GO_INT_TYPES:
                return f"float64({left}) * {right}", "float64", False
            if self._is_float_kind(left_kind) or self._is_float_kind(right_kind):
                kind = "float64"
            else:
                kind = left_kind if left_kind not in {"", "intlit"} else right_kind
            return f"{left} * {right}", kind, True
        if op == "/":
            a = self._as_float64(left, left_kind)
            b = self._as_float64(right, right_kind)
            if right_value is not None and right_value != 0:
                b = right if right_kind in {"intlit", "floatlit"} else b
                return f"{a} / {b}", "float64", a == left and b == right
            return self._FLOAT_DIV_GO.format(a=a, b=b), "float64", False
        if self._is_float_kind(left_kind) or self._is_float_kind(right_kind):
            self._need_import("math")
            template = self._FLOAT_FLOOR_DIV_GO if op == "//" else self._FLOAT_MOD_GO
            a = self._as_float64(left, left_kind)
            b = self._as_float64(right, right_kind)
            return template.format(a=a, b=b), "float64", False
        go_op = "/" if op == "//" else "%"
        if "param" in {left_kind, right_kind}:
            # A type parameter: Go's operator (its constraint decides).
            return f"{left} {go_op} {right}", "param", True
        int_kind = left_kind if left_kind in _GO_INT_TYPES else (
            right_kind if right_kind in _GO_INT_TYPES else ""
        )
        left_value = self._numeric_literal_value(left_tree) if left_tree is not None else None
        if isinstance(left_value, int) and isinstance(right_value, int) and right_value != 0:
            folded = left_value // right_value if op == "//" else left_value % right_value
            return (f"({folded})" if folded < 0 else str(folded)), "intlit", False
        right_bound = lower_bound(right_node, self._index_bounds, self._nonneg_shadowed)
        if (
            right_bound is not None and right_bound >= 1 and left_tree is not None
            and index_nonneg(left_tree, self._index_bounds, self._nonneg_shadowed)
        ):
            return f"{left} {go_op} {right}", int_kind or "intlit", True
        if isinstance(right_value, int) and right_value > 0 and right_value & (right_value - 1) == 0:
            # A power-of-two constant: two's complement gives Python's floor
            # and modulo directly (``-7 >> 1 == -4``, ``-7 & 1 == 1``); Go's
            # ``>>`` / ``&`` share the precedence of ``/`` / ``%``.
            if op == "//":
                return f"{left} >> {right_value.bit_length() - 1}", int_kind or "int", False
            return f"{left} & {right_value - 1}", int_kind or "int", False
        template = self._INT_FLOOR_DIV_GO if op == "//" else self._INT_MOD_GO
        return template.format(t=int_kind or "int", a=left, b=right), int_kind or "int", False

    def _python_term_to_go(self, node: Tree) -> str:
        """A ``term`` chain with ``/``, ``//`` or ``%``, folded left to right."""
        children = list(node.children)
        first = children[0]
        go = self._expr_to_go(first)
        if isinstance(first, Tree) and first.data in {"arith_expr", "term"}:
            go = f"({go})"
        else:
            go = go_operand(go, _GO_BINARY_PREC["*"])
        kind = self._division_operand_kind(first)
        prefix = [first]
        for op_token, right in zip(children[1::2], children[2::2]):
            left_tree = prefix[0] if len(prefix) == 1 else Tree("term", list(prefix))
            go, kind, _native = self._division_step_to_go(go, kind, left_tree, str(op_token), right)
            prefix.extend([op_token, right])
        return go

    # ─── List concatenation (``xs + ys``) ───────────────────────

    _LIST_LITERAL_NODES = frozenset({"list", "list_comprehension"})

    def _list_operand_go_type(self, node) -> str:
        hint = self._expr_go_type_hint(node) or self._infer_type_from_value(node) or ""
        return hint if hint.startswith("[]") else ""

    def _list_concat_go_type(self, operands) -> str:
        """The ``[]T`` of ``a + b + ...`` when every operand is a list
        (``""`` otherwise): the operands' common type, the expected type
        of the context, or ``[]interface{}`` for mixed element types."""
        types = [self._list_operand_go_type(c) for c in operands]
        literal = [isinstance(c, Tree) and c.data in self._LIST_LITERAL_NODES for c in operands]
        if not operands or not all(t or lit for t, lit in zip(types, literal)):
            return ""
        typed = [t for t, lit in zip(types, literal) if t and not lit]
        expected = (getattr(self, "_expected_expr_go_type", "") or "").strip()
        if typed:
            def fits(literal_type: str) -> bool:
                if literal_type in {"", "[]interface{}", typed[0]}:
                    return True
                elem, want = self._slice_elem_go_type(literal_type), self._slice_elem_go_type(typed[0])
                return elem in self._NUMERIC_GO_TYPES and want in self._NUMERIC_GO_TYPES
            if all(t == typed[0] for t in typed) and all(
                fits(t) for t, lit in zip(types, literal) if lit
            ):
                return typed[0]
            return expected if expected.startswith("[]") else "[]interface{}"
        if expected.startswith("[]"):
            return expected
        known = [t for t in types if t and t != "[]interface{}"]
        if known and all(t == known[0] for t in known):
            return known[0]
        return "[]interface{}"

    def _slice_to_go_type(self, go: str, source_type: str, target_type: str, node) -> str:
        """``go`` (a ``source_type`` slice, or a literal) as a ``target_type``
        slice; ``""`` when the element types cannot be converted."""
        if isinstance(node, Tree) and node.data in self._LIST_LITERAL_NODES:
            return self._typed_value_to_go(node, target_type)
        if source_type == target_type:
            return go
        elem = self._slice_elem_go_type(target_type)
        source_elem = self._slice_elem_go_type(source_type)
        if elem == "interface{}":
            assign = "v"
        elif elem in self._NUMERIC_GO_TYPES and source_elem in self._NUMERIC_GO_TYPES:
            assign = f"{elem}(v)"
        else:
            return ""
        return (
            f"func(s {source_type}) {target_type} {{ r := make({target_type}, len(s)); "
            f"for i, v := range s {{ r[i] = {assign} }}; return r }}({go})"
        )

    def _list_concat_to_go(self, operands) -> str:
        """``xs + ys``: a new slice holding both (neither operand is
        modified), typed by the element type."""
        target = self._list_concat_go_type(operands)
        if not target:
            return ""
        parts = []
        for operand in operands:
            part = self._slice_to_go_type(
                self._expr_to_go(operand), self._list_operand_go_type(operand), target, operand,
            )
            if not part:
                return ""
            parts.append(part)
        result = f"append({target}{{}}, {parts[0]}...)"
        for part in parts[1:]:
            result = f"append({result}, {part}...)"
        return result

    def _binop_to_go(self, node: Tree) -> str:
        operands = [c for c in node.children if isinstance(c, Tree)]
        ops = {str(c) for c in node.children if isinstance(c, Token)}
        if node.data == "term" and ops & {"/", "//", "%"} and ops <= {"*", "/", "//", "%"}:
            return self._python_term_to_go(node)
        if node.data == "arith_expr" and ops == {"+"}:
            concat = self._list_concat_to_go(operands)
            if concat:
                return concat
        promote: set = set()
        if ops <= {"+", "-", "*", "/"} and len(operands) >= 2:
            # ``1.5 * a`` with ``a: int``: Go needs the int converted.
            kinds = [self._numeric_operand_kind(c) for c in operands]
            if "float64" in kinds and "int" in kinds:
                promote = {id(c) for c, k in zip(operands, kinds) if k == "int"}
        # ``(a if c else "x") + ".zip"``: a ternary whose branch types are not
        # both known joins the string concatenation as a ``string`` (an
        # ``interface{}`` closure would not compile with ``+``).
        string_concat = ops == {"+"} and any(
            (self._expr_go_type_hint(c) or self._infer_type_from_value(c)) == "string" for c in operands
        )
        parts = []
        prec = _GO_BINARY_PREC["+" if node.data == "arith_expr" else "*"]
        position = 0
        for child in node.children:
            if isinstance(child, Token):
                op = str(child)
                parts.append("/" if op == "//" else op)
            elif isinstance(child, Tree):
                if (
                    string_concat and child.data == "test" and len(child.children) == 3
                    and not self._ternary_common_go_type(child)
                ):
                    s = self._string_ternary_to_go(child)
                else:
                    s = self._expr_to_go(child)
                if (
                    node.data == "shift_expr" and position == 0 and child.data == "number"
                    and self._numeric_literal_value(node.children[2]) is None
                ):
                    # ``2 << c``: an untyped constant shifted by a variable
                    # takes the type of its context (``float64(2 << c)``
                    # does not build); it is an ``int``.
                    s = f"int({s})"
                if id(child) in promote:
                    s = f"float64({s})"
                elif child.data in ("arith_expr", "term") and node.data != child.data:
                    s = f"({s})"
                else:
                    # ``a - (b + c)``: the group keeps its parentheses.
                    s = go_operand(s, prec, right=position > 0)
                parts.append(s)
                position += 1
        return " ".join(parts)

    def _bitwise_chain_to_go(self, node: Tree, op: str) -> str:
        """``a & b`` / ``a | b`` / ``a ^ b`` (the whole chain parenthesized:
        Python's bit operators bind looser than its arithmetic, Go's do not)."""
        parts = [self._expr_to_go(c) for c in node.children if c is not None]
        prec = _GO_BINARY_PREC[op]
        wrapped = [
            f"({p})" if go_expr_precedence(p) < _GO_PRIMARY_PREC and any(sym in p for sym in "+-*/%")
            else go_operand(p, prec, right=i > 0)
            for i, p in enumerate(parts)
        ]
        inner = f" {op} ".join(wrapped)
        return f"({inner})" if len(parts) > 1 else inner

    def _string_ternary_to_go(self, node: Tree) -> str:
        """``a if c else b`` as a Go ``string`` (it is joined to a ``str``):
        a branch of a known dynamic type is asserted (``.(string)``, a
        ``TypeError`` when it is not a string, like Python's ``+``)."""
        def branch(value_node) -> str:
            known = self._expr_go_type_hint(value_node) or self._infer_type_from_value(value_node) or ""
            value = self._typed_value_to_go(value_node, "string")
            if known == "interface{}" and not value.endswith(".(string)") and not value.startswith("lamAs[string]("):
                value = self._result_payload_assert_go(value, "string")
            return value

        narrowed = self._nr_ternary_to_go(node, "string", branch)
        if narrowed is not None:
            return narrowed
        cond = self._expr_to_go(node.children[1])
        return (
            f"func() string {{ if {cond} {{ return {branch(node.children[0])} }}; "
            f"return {branch(node.children[2])} }}()"
        )

    @staticmethod
    def _nonneg_int_literal(node) -> bool:
        if isinstance(node, Tree) and node.data == "number" and node.children:
            try:
                return int(str(node.children[0]).replace("_", ""), 0) >= 0
            except ValueError:
                return False
        return False

    _PURE_GO_RE = re.compile(r"[A-Za-z_][\w.]*")

    def _index_is_last_of(self, index, receiver) -> bool:
        """``xs[len(xs) - 1]`` on a side-effect-free receiver: the index is
        ``-1`` only for an empty ``xs``, where both readings are out of
        range, so the plain index already has Python's behaviour."""
        if not (
            isinstance(index, Tree) and index.data == "arith_expr" and len(index.children) == 3
            and str(index.children[1]) == "-" and isinstance(index.children[2], Tree)
            and index.children[2].data == "number" and str(index.children[2].children[0]) == "1"
        ):
            return False
        call = index.children[0]
        if not (isinstance(call, Tree) and call.data == "funccall" and len(call.children) >= 2):
            return False
        if self._get_name(call.children[0]) != "len" or "len" in self._nonneg_shadowed:
            return False
        args = call.children[1]
        arg = args.children[0] if isinstance(args, Tree) and args.data == "arguments" and len(args.children) == 1 else args
        return arg == receiver and self._pure_index_receiver(receiver)

    def _pure_index_receiver(self, node) -> bool:
        """A receiver evaluating it twice is free of side effects: names,
        fields and constant / name indexes of those (``grid[r]``)."""
        if not isinstance(node, Tree):
            return False
        if node.data == "var":
            return True
        if node.data == "getattr" and node.children:
            return self._pure_index_receiver(node.children[0])
        if node.data == "getitem" and len(node.children) >= 2:
            index = node.children[1]
            simple_index = isinstance(index, Tree) and (
                index.data in {"var", "number"} or self._negative_int_constant(index) is not None
            )
            return simple_index and self._pure_index_receiver(node.children[0])
        return False

    def _dynamic_index_to_go(self, obj: str, obj_node, index_node, receiver_type: str) -> str:
        """``xs[i]`` where ``i`` may be negative: ``i + len(xs)&(i>>63)``
        adds the length only when ``i < 0`` (an arithmetic shift of a
        negative ``int`` is ``-1``, all bits set), branch-free and inlined;
        out of range either way still panics with Go's index error, which
        ``catch IndexError`` recognises."""
        idx = self._expr_to_go(index_node)
        idx_type = self._expr_go_type_hint(index_node) or self._infer_type_from_value(index_node) or ""
        if idx_type not in _GO_INT_TYPES and idx_type not in {"", "interface{}"}:
            # A string / float key: the receiver is not a sequence after all
            # (stale type tracking); index it as written.
            return f"{obj}[{idx}]"
        idx_int = idx if idx_type == "int" else f"int({idx})"
        if self._PURE_GO_RE.fullmatch(obj) or self._pure_index_receiver(obj_node):
            if idx_type == "int" and self._PURE_GO_RE.fullmatch(idx):
                return f"{obj}[{idx}+len({obj})&({idx}>>63)]"
            return f"{obj}[func(i, n int) int {{ return i + n&(i>>63) }}({idx_int}, len({obj}))]"
        elem = "byte" if receiver_type == "string" else self._slice_elem_go_type(receiver_type)
        if self._index_target and receiver_type != "string":
            return (
                f"(*func(s {receiver_type}, i int) *{elem} {{ return &s[i+len(s)&(i>>63)] }}"
                f"({obj}, {idx_int}))"
            )
        return f"func(s {receiver_type}, i int) {elem} {{ return s[i+len(s)&(i>>63)] }}({obj}, {idx_int})"

    @staticmethod
    def _negative_int_constant(node) -> Optional[int]:
        """``n`` for an index written ``-n`` (an integer literal), else ``None``."""
        if (
            isinstance(node, Tree) and node.data == "factor" and len(node.children) == 2
            and str(node.children[0]) == "-" and isinstance(node.children[1], Tree)
            and node.children[1].data == "number" and node.children[1].children
        ):
            text = str(node.children[1].children[0]).replace("_", "")
            if text.isdigit() and int(text) > 0:
                return int(text)
        return None

    def _numeric_operand_kind(self, node) -> str:
        """``float64`` / ``int`` for a typed (non-constant) arithmetic
        operand, ``float64`` for a float literal, ``""`` otherwise
        (integer literals adapt to the other operand in Go)."""
        if isinstance(node, Tree) and node.data == "number":
            text = str(node.children[0]) if node.children else ""
            return "float64" if "." in text or "e" in text.lower() else ""
        if isinstance(node, Tree) and node.data == "factor" and len(node.children) == 2:
            return self._numeric_operand_kind(node.children[1])
        hint = self._expr_go_type_hint(node) or ""
        return hint if hint in {"int", "float64"} else ""

    def _comparison_to_go(self, node: Tree) -> str:
        operands = [
            child for child in node.children
            if isinstance(child, Tree) and child.data != "comp_op"
        ]
        ops = [
            self._comparison_op_text(child)
            for child in node.children
            if isinstance(child, Tree) and child.data == "comp_op"
        ]
        if len(operands) < 2 or not ops:
            return " ".join(
                self._expr_to_go(child)
                for child in node.children
                if isinstance(child, Tree)
            )

        comparisons = []
        left = operands[0]
        for op, right in zip(ops, operands[1:]):
            comparisons.append(self._single_comparison_to_go(left, op, right))
            left = right
        if len(comparisons) == 1:
            return comparisons[0]
        return " && ".join(f"({comparison})" for comparison in comparisons)

    @staticmethod
    def _comparison_op_text(node: Tree) -> str:
        return " ".join(str(t) for t in node.children if isinstance(t, Token))

    def _single_comparison_to_go(self, left, op: str, right) -> str:
        if op == "in":
            return self._membership_to_go(left, right)
        if op == "not in":
            return f"!({self._membership_to_go(left, right)})"
        markup_equal = self._markup_comparison_to_go(left, op, right)
        if markup_equal is not None:
            return markup_equal
        left_go = self._comparison_operand_to_go(left)
        right_go = self._comparison_operand_to_go(right)
        left_json = self._expr_go_type_hint(left) == "LamJSON"
        right_json = self._expr_go_type_hint(right) == "LamJSON"
        if (left_json or right_json) and op in {"==", "!=", "is", "is not"}:
            left_value = left_go if left_json else f"lamJSONMust({left_go})"
            right_value = right_go if right_json else f"lamJSONMust({right_go})"
            equal = f"lamJSONEqual({left_value}, {right_value})"
            return f"!({equal})" if op in {"!=", "is not"} else equal
        if op in {"==", "!="}:
            equal = self._collection_equality_to_go(left, right)
            if equal:
                return f"!{equal}" if op == "!=" else equal
        if op in {"==", "!=", "is", "is not"}:
            # Identity of a subclass instance and a base-typed value:
            # compare both as the base (the subclass's embedded parent).
            left_type = self._hier_expr_go_type(left)
            right_type = self._hier_expr_go_type(right)
            left_go = self._hier_upcast_to_go(left_go, left_type, right_type) or left_go
            right_go = self._hier_upcast_to_go(right_go, right_type, left_type) or right_go
        if op in {"<", "<=", ">", ">=", "==", "!="}:
            # ``n < x / 2`` (an int and a float): the int widens, like Python.
            kinds = (self._numeric_operand_kind(left), self._numeric_operand_kind(right))
            if kinds == ("int", "float64"):
                left_go = f"float64({left_go})"
            elif kinds == ("float64", "int"):
                right_go = f"float64({right_go})"
        if op == "is not":
            return f"{left_go} != {right_go}"
        if op == "is":
            return f"{left_go} == {right_go}"
        return f"{left_go} {op} {right_go}"

    _COMPARABLE_ELEM_GO_TYPES = frozenset({
        "string", "bool", "int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16",
        "uint32", "uint64", "uintptr", "byte", "rune", "float32", "float64",
    })

    def _collection_equality_to_go(self, left, right) -> str:
        """``xs == ys`` / ``d == {...}`` on lists, dicts or sets (Go only
        compares slices and maps with ``nil``): element-wise equality typed
        by the element type; ``""`` when neither side is a typed collection."""
        if self._is_none_literal(left) or self._is_none_literal(right):
            return ""
        literal_nodes = {"list", "dict", "set", "list_comprehension", "dict_comprehension", "set_comprehension"}

        def collection_type(node) -> str:
            hint = (self._expr_go_type_hint(node) or "").strip()
            if not hint and isinstance(node, Tree) and node.data in literal_nodes:
                hint = (self._infer_type_from_value(node) or "").strip()
            return hint if hint.startswith(("[]", "map[")) else ""

        left_type, right_type = collection_type(left), collection_type(right)
        left_literal = isinstance(left, Tree) and left.data in literal_nodes
        right_literal = isinstance(right, Tree) and right.data in literal_nodes
        if not left_type and not right_type:
            return ""
        # A literal side takes the other side's type (``xs == ["a"]``).
        if left_type and (right_literal or not right_type):
            target = left_type
        elif right_type and (left_literal or not left_type):
            target = right_type
        else:
            target = left_type
        if not (left_type or left_literal) or not (right_type or right_literal):
            return ""
        if left_type and right_type and left_type != right_type and not (left_literal or right_literal):
            # Different element types (``list[int]`` and ``list[float]``):
            # compare the values dynamically.
            self._need_import("reflect")
            return f"reflect.DeepEqual({self._expr_to_go(left)}, {self._expr_to_go(right)})"
        left_go = self._typed_value_to_go(left, target) if left_literal else self._expr_to_go(left)
        right_go = self._typed_value_to_go(right, target) if right_literal else self._expr_to_go(right)
        if target.startswith("[]"):
            elem = self._slice_elem_go_type(target)
            elem_class = self._hier_class_from_go_type(elem) if elem.startswith("*") else ""
            eq_method = self._get_dunder_method(elem_class, "__eq__") if elem_class else ""
            if eq_method:
                self._need_import("slices")
                return f"slices.EqualFunc({left_go}, {right_go}, func(a, b {elem}) bool {{ return a.{eq_method}(b) }})"
            if elem in self._COMPARABLE_ELEM_GO_TYPES or (elem.startswith("*") and "[" not in elem):
                self._need_import("slices")
                return f"slices.Equal({left_go}, {right_go})"
        else:
            _key, value = self._split_go_map_type(target)
            if value in self._COMPARABLE_ELEM_GO_TYPES:
                self._need_import("maps")
                return f"maps.Equal({left_go}, {right_go})"
        self._need_import("reflect")
        return f"reflect.DeepEqual({left_go}, {right_go})"

    def _comparison_operand_to_go(self, node) -> str:
        """An operand that is itself a comparison or boolean expression was
        parenthesized in Lam; Go needs the parentheses too."""
        go = self._expr_to_go(node)
        if isinstance(node, Tree) and node.data in {"comparison", "and_test", "or_test"}:
            return f"({go})"
        return go_operand(go, _GO_BINARY_PREC["=="], right=True)

    def _membership_to_go(self, item_node, container_node) -> str:
        container_go = self._expr_to_go(container_node)
        container_type = self._expr_go_type_hint(container_node) or self._infer_type_from_value(container_node)
        container_type = (container_type or "").strip()
        class_name = self._infer_expr_class(container_node)
        method = self._get_dunder_method(class_name, "__contains__") if class_name else ""
        if method:
            method_key = f"{class_name}.__contains__"
            param_types = self._func_param_go_types.get(method_key, [])
            item_type = param_types[0] if param_types else "interface{}"
            item_go = self._typed_value_to_go(item_node, item_type)
            return f"{container_go}.{method}({item_go})"

        if container_type == "string":
            self._need_import("strings")
            item_go = self._typed_value_to_go(item_node, "string")
            return f"strings.Contains({container_go}, {item_go})"

        if container_type == "[]byte":
            item_type = self._expr_go_type_hint(item_node) or self._infer_type_from_value(item_node) or ""
            if item_type == "[]byte":
                # A sub-sequence (the empty one is in every value).
                self._need_import("strings")
                return f"strings.Contains(string({container_go}), string({self._expr_to_go(item_node)}))"
            # A byte value: an int outside 0..255 is never present.
            self._need_import("slices")
            item_go = self._expr_to_go(item_node)
            return (
                f"func(_b []byte, _v int) bool {{ return _v >= 0 && _v < 256 && "
                f"slices.Contains(_b, byte(_v)) }}({container_go}, int({item_go}))"
            )

        if container_type.startswith("map["):
            key_type, _value_type = self._split_go_map_type(container_type)
            item_go = self._typed_value_to_go(item_node, key_type or "interface{}")
            return (
                f"func() bool {{ _, _ok := {container_go}[{item_go}]; "
                f"return _ok }}()"
            )

        if container_type.startswith("[]"):
            elem_type = self._slice_elem_go_type(container_type) or "interface{}"
            item_go = self._typed_value_to_go(item_node, elem_type)
            if elem_type in self._COMPARABLE_ELEM_GO_TYPES:
                # ``name in tags`` with ``tags: list[str]``: ``==`` on the
                # elements, what DeepEqual does for them, without boxing
                # (also right when the list was lowered as ``[]interface{}``:
                # an interface equals the item when it holds that value).
                return (
                    f"func() bool {{ for _, _v := range {container_go} {{ "
                    f"if _v == {item_go} {{ return true }} }}; "
                    f"return false }}()"
                )
            self._need_import("reflect")
            return (
                f"func() bool {{ for _, _v := range {container_go} {{ "
                f"if reflect.DeepEqual(_v, {item_go}) {{ return true }} }}; "
                f"return false }}()"
            )

        self._need_import("reflect")
        self._need_import("strings")
        item_go = self._expr_to_go(item_node)
        return (
            f"func() bool {{ _item := interface{{}}({item_go}); "
            f"_container := interface{{}}({container_go}); "
            f"if _s, _ok := _container.(string); _ok {{ "
            f"_needle, _needleOk := _item.(string); return _needleOk && strings.Contains(_s, _needle) }}; "
            f"_v := reflect.ValueOf(_container); "
            f"switch _v.Kind() {{ "
            f"case reflect.Map: _k := reflect.ValueOf(_item); "
            f"if _k.IsValid() && _k.Type().AssignableTo(_v.Type().Key()) {{ return _v.MapIndex(_k).IsValid() }}; "
            f"if _k.IsValid() && _k.Type().ConvertibleTo(_v.Type().Key()) {{ return _v.MapIndex(_k.Convert(_v.Type().Key())).IsValid() }}; "
            f"return false; "
            f"case reflect.Slice, reflect.Array: for _i := 0; _i < _v.Len(); _i++ {{ "
            f"if reflect.DeepEqual(_v.Index(_i).Interface(), _item) {{ return true }} }} }}; "
            f"return false }}()"
        )

    def _range_single_target_uses_key(self, iterable_node) -> bool:
        if isinstance(iterable_node, Tree) and iterable_node.data in ("dict", "dict_comprehension"):
            # A dict literal or comprehension is a map whatever its types
            # (``[k for k in {f"k{i}": i for i in xs}]`` iterates the keys).
            return True
        iterable_type = self._expr_go_type_hint(iterable_node) or self._infer_type_from_value(iterable_node)
        return (iterable_type or "").strip().startswith("map[")

    def _uniform_literal_items(self, node: Tree) -> bool:
        """Whether every item (every key and value of a dict) of a literal
        has a statically known Go type, so the literal can be typed by them."""
        items = []
        for child in node.children:
            if isinstance(child, Tree) and child.data == "key_value":
                items.extend(child.children[:2])
            elif child is not None:
                items.append(child)
        for item in items:
            if not isinstance(item, Tree):
                return False
            known = self._expr_go_type_hint(item) or self._infer_type_from_value(item) or ""
            if not known or "interface{}" in known:
                return False
            if any(re.search(rf"\b{re.escape(name)}\b(?!\[)", known) for name in self._generic_classes):
                # ``[Box("a"), Box("b")]`` of a generic class: its Go type
                # needs type arguments the literal does not state.
                return False
        return True

    def _dict_to_go(self, node: Tree) -> str:
        literal = self._infer_type_from_value(node) if node.children else ""
        if literal.startswith("map[") and "interface{}" not in literal and self._uniform_literal_items(node):
            # ``{"a": 1}[k]``: typed by its keys and values (a missing key
            # reads the value type's zero, as for any typed dict).
            return self._typed_value_to_go(node, literal)
        return self._untyped_literal_to_go(node)

    def _untyped_literal_to_go(self, node: Tree) -> str:
        """A list or dict literal with ``interface{}`` items (nested literals
        too): the lowering of a literal stored in an ``any``."""
        if node.data == "list":
            elems = [self._typed_value_to_go(c, "interface{}") for c in node.children if c is not None]
            return f"[]interface{{}}{{{', '.join(elems)}}}"
        entries = []
        has_non_string_key = False
        for child in node.children:
            if isinstance(child, Tree) and child.data == "key_value":
                k = self._expr_to_go(child.children[0])
                v = self._typed_value_to_go(child.children[1], "interface{}")
                if not (k.startswith('"') or k.startswith("'")):
                    has_non_string_key = True
                entries.append(f"{k}: {v}")
        if has_non_string_key:
            return f"map[interface{{}}]interface{{}}{{{', '.join(entries)}}}"
        return f"map[string]interface{{}}{{{', '.join(entries)}}}"

    # ─── isinstance ────────────────────────────────────────────
    #
    # ``isinstance(obj, type)`` compiles to a small Go IIFE containing a
    # two-value type assertion, ``obj == nil`` check, or a ``reflect``
    # kind comparison — whichever matches the supplied type expression.
    # ``isinstance(obj, (T1, T2, ...))`` OR-combines the per-type tests.

    _ISINSTANCE_PRIMITIVES = {
        "int": "int", "int8": "int8", "int16": "int16",
        "int32": "int32", "int64": "int64",
        "uint": "uint", "uint8": "uint8", "uint16": "uint16",
        "uint32": "uint32", "uint64": "uint64",
        "float": "float64", "float32": "float32", "float64": "float64",
        "str": "string", "string": "string",
        "bool": "bool", "byte": "byte", "rune": "rune",
        "bytes": "[]byte",
    }

    def _isinstance_test_go(self, obj_go: str, type_node, static_class: str = "") -> str:
        """Build the Go boolean expression for `isinstance(obj, type_node)`."""
        # Tuple of types: (int, str, SomeClass)
        if isinstance(type_node, Tree) and type_node.data == "tuple":
            parts = []
            for c in type_node.children:
                if c is None:
                    continue
                parts.append(self._isinstance_test_go(obj_go, c, static_class))
            if not parts:
                return "false"
            return "(" + " || ".join(parts) + ")"

        # Parenthesised single type.
        if isinstance(type_node, Tree) and type_node.data == "argvalue":
            inner = type_node.children[0] if type_node.children else None
            if inner is not None:
                return self._isinstance_test_go(obj_go, inner, static_class)

        # Parametric type: list[int], dict[str, int] — treat as the
        # outer container kind (Go does not carry parametric runtime
        # type info for generic collections).
        if isinstance(type_node, Tree) and type_node.data == "getitem":
            return self._isinstance_test_go(obj_go, type_node.children[0], static_class)

        name = self._isinstance_name(type_node)

        if name is None:
            # Fallback: transpile as a type and use a direct assertion.
            go_ty = self._expr_to_go(type_node)
            return (
                f"func() bool {{ _, ok := interface{{}}({obj_go}).({go_ty}); return ok }}()"
            )

        if name == "None":
            if obj_go.strip() == "nil":
                return "true"
            return f"({obj_go} == nil)"
        if name in ("any", "object"):
            return "true"

        prim = self._ISINSTANCE_PRIMITIVES.get(name)
        if prim is not None:
            if prim == "[]byte":
                return (
                    "func() bool { _, ok := interface{}("
                    + obj_go + ").([]byte); return ok }()"
                )
            return (
                f"func() bool {{ _, ok := interface{{}}({obj_go}).({prim}); "
                f"return ok }}()"
            )

        if name == "list":
            self._need_import("reflect")
            return (
                "func() bool { v := reflect.ValueOf(" + obj_go + "); "
                "return v.IsValid() && (v.Kind() == reflect.Slice || v.Kind() == reflect.Array) }()"
            )
        if name in ("dict", "set"):
            self._need_import("reflect")
            return (
                "func() bool { v := reflect.ValueOf(" + obj_go + "); "
                "return v.IsValid() && v.Kind() == reflect.Map }()"
            )
        if name == "tuple":
            self._need_import("reflect")
            return (
                "func() bool { v := reflect.ValueOf(" + obj_go + "); "
                "return v.IsValid() && (v.Kind() == reflect.Slice || v.Kind() == reflect.Array) }()"
            )
        if name == "error":
            return (
                f"func() bool {{ _, ok := interface{{}}({obj_go}).(error); return ok }}()"
            )
        if name in PYTHON_EXCEPTIONS:
            # Exceptions are represented as panics; runtime type is a
            # string, so we can't reliably identify them from a value.
            return "false"
        if name in self._interfaces:
            go_name = self._go_class_name(name)
            return (
                f"func() bool {{ _, ok := interface{{}}({obj_go}).({go_name}); return ok }}()"
            )

        # A class in an inheritance hierarchy: test the dynamic class (a
        # base-typed value may hold a subclass; a subclass is an instance
        # of its bases).
        hier_test = self._hier_isinstance_go(obj_go, static_class, name)
        if hier_test:
            return hier_test

        # User class — represented as a pointer to a struct in Go.
        go_name = self._go_class_name(name)
        return (
            f"func() bool {{ _, ok := interface{{}}({obj_go}).(*{go_name}); return ok }}()"
        )

    def _isinstance_name(self, node) -> Optional[str]:
        """Best-effort: return the dotted type name from an AST node."""
        if node is None:
            return None
        if isinstance(node, Token):
            return str(node)
        if isinstance(node, Tree):
            if node.data == "const_none":
                return "None"
            if node.data in ("var", "type_name"):
                return self._dotted_name_to_str(node.children[0])
            if node.data == "name":
                return self._get_name(node)
            if node.data == "dotted_name":
                return self._dotted_name_to_str(node)
        return None
# benign comment for cache-invalidation test
