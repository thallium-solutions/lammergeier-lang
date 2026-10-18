#!/usr/bin/env python3
"""Expression visitor methods for the transpiler."""

from __future__ import annotations
from lark import Tree, Token
from typing import List, Optional
import re
from compiler.constants import (
    PYTHON_EXCEPTIONS, LOWER_PKGS, GO_BUILTINS,
    OP_TO_DUNDER, CMP_TO_DUNDER,
)
from compiler.typesys import DictType
from compiler.visitors.generics import mentions_names, substitute_go_type


class ExpressionVisitorMixin:
    """Handles expression-to-Go conversion, function calls, f-strings, comprehensions."""

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
        boxed = self._optional_value_to_go(node, go_type)
        if boxed:
            return boxed
        if go_type == "LamJSON":
            raw = self._json_value_to_go(node)
            return raw if self._expr_go_type_hint(node) == "LamJSON" else f"lamJSONMust({raw})"
        if (
            isinstance(node, Tree)
            and node.data == "var"
            and node.children
            and go_type
            and go_type != "interface{}"
            and (go_type in {"string", "bool", "int", "float64"} or go_type.startswith(("[]", "map[", "func(")))
            and self._var_go_types.get(self._get_name(node.children[0])) == "interface{}"
        ):
            # ``items: list[any] = raw`` where ``raw: any``: a checked
            # type assertion (it panics with the dynamic type if wrong).
            # ``interface{}(...)`` keeps it valid when the name tracking is
            # stale and the variable is already concrete.
            return f"interface{{}}({self._expr_to_go(node)}).({go_type})"
        if (
            isinstance(node, Tree)
            and node.data == "getitem"
            and go_type
            and go_type != "interface{}"
            and (go_type in {"string", "bool", "int", "float64"} or go_type.startswith("[]") or go_type.startswith("map[") or go_type.startswith("*"))
            and (self._expr_go_type_hint(node.children[0]) or "") in {"[]interface{}", "map[string]interface{}"}
            and not (isinstance(node.children[1], Tree) and node.children[1].data == "slice")
        ):
            # ``name: str = row[1]`` from a ``list[any]`` / ``dict[str, any]``:
            # a checked conversion of the element.
            return f"interface{{}}({self._expr_to_go(node)}).({go_type})"
        if isinstance(node, Tree):
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
        if (
            go_type not in {"", "LamJSON", "interface{}"}
            and self._expr_go_type_hint(node) == "LamJSON"
        ):
            return f"lamJSONRaw({raw}).({go_type})"
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
                f"for i, v := range src {{ out[i] = v.({elem}) }}; return out }}({raw})"
            )
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
                    return f"{raw}.({go_type})"
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
                        return f"{raw}.({go_type})"
                if node.data == "var":
                    var_name = self._get_name(node.children[0])
                    if self._var_go_types.get(var_name) == "interface{}":
                        return f"{raw}.({go_type})"
        if go_type not in {"", "interface{}"} and isinstance(node, Tree) and node.data == "getitem":
            class_name = self._infer_expr_class(node.children[0])
            if self._method_return_types.get(f"{class_name}.__getitem__") in {"any", "object"}:
                return f"{raw}.({go_type})"
        return raw

    _OPTIONAL_VALUE_GO_TYPES = frozenset({
        "int", "int8", "int16", "int32", "int64", "uint", "float64", "float32",
        "string", "bool", "byte", "rune",
    })

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

    def _optional_value_to_go(self, node, go_type: str) -> str:
        """Box a plain value into an ``optional[int]``-style ``*int`` target.

        ``optional[T]`` of a value type lowers to ``*T``; a ``T`` value
        (literal, arithmetic, call returning ``T``) assigned or returned
        there becomes a pointer to a fresh copy. ``None`` stays ``nil``.
        """
        if not go_type.startswith("*") or go_type[1:] not in self._OPTIONAL_VALUE_GO_TYPES:
            return ""
        if not isinstance(node, Tree) or node.data == "const_none":
            return ""
        if node.data == "var" and node.children and self._get_name(node.children[0]) in {"None", "nil"}:
            return ""
        inner = go_type[1:]
        hint = self._expr_go_type_hint(node) or ""
        if hint == go_type:
            return ""
        if not hint and node.data == "funccall" and isinstance(node.children[0], Tree) and node.children[0].data == "var":
            callee = self._get_name(node.children[0].children[0])
            if callee == "len" and callee not in self._user_functions:
                hint = "int"
            else:
                hint = self._func_return_go_types.get(callee, "")
        if not hint:
            inferred = self._infer_type_from_value(node)
            hint = inferred if inferred != "interface{}" else ""
        literal = node.data in {"number", "string", "const_true", "const_false", "factor", "arith_expr", "term"}
        if hint != inner and not (literal and hint in {"", "int", "float64"}):
            return ""
        value = self._typed_value_to_go(node, inner)
        return f"func() {go_type} {{ __lamV := {inner}({value}); return &__lamV }}()"

    def _json_value_to_go(self, node) -> str:
        if isinstance(node, Tree) and node.data == "dict":
            entries = []
            for child in node.children:
                if isinstance(child, Tree) and child.data == "key_value" and len(child.children) >= 2:
                    key = self._typed_value_to_go(child.children[0], "string")
                    value = self._json_value_to_go(child.children[1])
                    entries.append(f"{key}: {value}")
            return f"map[string]interface{{}}{{{', '.join(entries)}}}"
        if isinstance(node, Tree) and node.data == "list":
            values = [self._json_value_to_go(child) for child in node.children if child is not None]
            return f"[]interface{{}}{{{', '.join(values)}}}"
        if isinstance(node, Tree) and node.data in {"list_comprehension", "tuple_comprehension"}:
            return self._list_comp_to_go(node, elem_type="[]interface{}")
        if isinstance(node, Tree) and node.data == "dict_comprehension":
            return self._dict_comp_to_go(node, map_type="map[string]interface{}")
        return self._expr_to_go(node)

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
        "dedent": "string", "format": "string",
    }

    def _expr_go_type_hint_plain_field(self, node) -> str:
        """Go type of the field read by ``obj?.field`` (as if ``obj.field``)."""
        if not (isinstance(node, Tree) and node.data == "getattr_safe" and len(node.children) >= 2):
            return ""
        return self._expr_go_type_hint(Tree("getattr", list(node.children))) or ""

    def _safe_getattr_result_go_type(self, node) -> str:
        """Go type of ``obj?.field`` on a pointer (``optional``/class)
        receiver: the field type when it is nilable, else a pointer to it
        (``optional[str]`` -> ``*string``). ``""`` when unknown."""
        obj_type = self._expr_go_type_hint(node.children[0]) or ""
        if not obj_type.startswith("*"):
            return ""
        field_type = self._expr_go_type_hint_plain_field(node)
        if not field_type or field_type == "interface{}":
            return ""
        if field_type in self._OPTIONAL_VALUE_GO_TYPES:
            return "*" + field_type
        if field_type.startswith(("*", "[]", "map[", "func(")):
            return field_type
        return ""

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
        if node.data == "lambdef":
            return self._lambda_go_func_type(node)
        if node.data == "null_coalesce":
            operands = [c for c in node.children if isinstance(c, Tree)]
            if len(operands) == 2 and self._is_none_literal(operands[1]):
                return self._expr_go_type_hint(operands[0])
        if node.data == "getattr_safe" and len(node.children) >= 2:
            if self._expr_go_type_hint(node.children[0]) == "LamJSON":
                return "LamJSON"
            return self._safe_getattr_result_go_type(node)
        if node.data == "test" and len(node.children) == 3:
            return self._ternary_common_go_type(node)
        if node.data in ("arith_expr", "term") and len(node.children) >= 3:
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
            if kinds and all(k == "int" for k in kinds) and ops <= {"+", "-", "*", "//", "%"}:
                return "int"
            if kinds and all(k in {"int", "float64"} for k in kinds) and "float64" in kinds and ops <= {"+", "-", "*", "/"}:
                return "float64"
            if kinds and all(k == "string" for k in kinds) and ops == {"+"}:
                return "string"
            return ""
        if node.data == "var" and node.children:
            name = self._get_name(node.children[0])
            if name == "_":
                return ""
            if name == "self" and self.current_class:
                return f"*{self._go_class_name(self.current_class)}"
            return self._var_go_types.get(name, "")
        if node.data == "funccall" and node.children:
            callee = node.children[0]
            if isinstance(callee, Tree) and callee.data == "var" and callee.children:
                name = self._get_name(callee.children[0])
                if name == "json":
                    return "LamJSON"
                if name in {"int", "float", "bool", "str"} and name not in self._function_go_names:
                    return {"int": "int", "float": "float64", "bool": "bool", "str": "string"}[name]
                if name == "len" and name not in self._user_functions:
                    return "int"
                if name in self._async_functions and name not in self._var_go_types:
                    return self._async_handle_go_type(self._func_return_go_types.get(name, ""))
                if name in self._result_payload_go_types:
                    return "*Result"
                ret = self._func_return_go_types.get(name, "")
                if (
                    ret.startswith("*")
                    and len(self._overload_variants.get(name, ())) <= 1
                    and name not in self._class_names
                    and not ret.startswith("*Result")
                ):
                    # ``lookup(id) ?? fallback`` where ``lookup`` returns
                    # ``optional[User]`` / a class pointer.
                    return ret
                if ret.startswith("func(") and name not in self._class_names:
                    return ret
                if (
                    (ret in {"string", "int", "float64", "bool"} or ret.startswith(("[]", "map[")))
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
            if isinstance(callee, Tree) and callee.data in {"getattr", "getattr_safe"} and len(callee.children) >= 2:
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
                    raw_return = self._method_return_types.get(f"{class_name}.{method}", "")
                    scalar = {"str": "string", "int": "int", "float": "float64", "bool": "bool", "bytes": "[]byte", "json": "LamJSON"}
                    if raw_return in scalar:
                        return scalar[raw_return]
                    container = self._method_return_types.get(f"go:{class_name}.{method}", "")
                    if container:
                        return container
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
        if node.data in {"getattr", "getattr_safe"} and len(node.children) >= 2:
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
            elif isinstance(obj, Tree) and obj.data in {"funccall", "getattr", "getattr_safe"}:
                # ``current().box`` / ``holder.inner.box``: the receiver's class
                # from call return types and field types.
                class_name = self._infer_receiver_class(obj)
            if class_name:
                for field_name, field_type in self.class_fields.get(class_name, []):
                    if field_name == attr or self._go_field_name(class_name, field_name) == attr:
                        return field_type
                field_type = self._external_field_types.get(class_name, {}).get(attr)
                if field_type:
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
                return self._go_user_function_name(name)
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

        if d == "funccall":
            reused = self.__dict__.get("_reuse_lowering", {}).get(id(node))
            if reused is not None and reused[0] is node:
                return reused[1]
            call = self._funccall_to_go(node)
            payload = self._unwrap_payload_go_type(node)
            if payload and call.endswith(")"):
                return f"{call}.({payload})"
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
                    # the generated package-level function.
                    return f"{self._go_class_name(raw_obj)}_{attr}"
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
            obj = self._expr_to_go(obj_node)
            if obj in LOWER_PKGS:
                return f"{obj}.{attr}"
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

        if d == "getattr_safe":
            # ``obj?.field`` — guard the access with a nil-check and
            # return ``nil`` when the receiver is ``nil``. The result
            # type is ``interface{}`` because we can't statically
            # guarantee the field's concrete type in every context; a
            # caller that needs a typed value can assign to an
            # annotated variable (the assignment site will unbox).
            obj_node = node.children[0]
            obj = self._expr_to_go(obj_node)
            attr = self._get_name(node.children[1])
            if self._expr_go_type_hint(obj_node) == "LamJSON":
                return f'lamJSONGet({obj}, "{attr}")'
            class_name = self._infer_receiver_class(obj_node)
            field_names = {name for name, _type in self.class_fields.get(class_name, [])}
            field_names.update(self._external_field_names.get(class_name, set()))
            if class_name and attr in field_names:
                field = self._go_field_name(class_name, attr)
            elif class_name and attr in self._instance_methods.get(class_name, set()):
                field = self._go_method_name(class_name, attr)
            elif self._is_private_method_name(attr):
                field = self._go_private_name(attr)
            else:
                field = self._go_public_name(attr)
            obj_type = self._expr_go_type_hint(obj_node) or ""
            result_type = self._safe_getattr_result_go_type(node)
            if obj_type.startswith("*") and result_type:
                # Typed ``optional`` chain: a nil receiver yields ``None``;
                # a value-typed field is boxed (``optional[str]`` is
                # ``*string``) so ``??`` and further ``?.`` stay typed.
                if result_type == "*" + (self._expr_go_type_hint_plain_field(node) or ""):
                    value = f"__lamV := __lamO.{field}; return &__lamV"
                else:
                    value = f"return __lamO.{field}"
                return (
                    f"func() {result_type} {{ if __lamO := {obj}; __lamO != nil {{ "
                    f"{value} }}; return nil }}()"
                )
            return (
                f"func() interface{{}} {{ if {obj} != nil {{ "
                f"return {obj}.{field} }}; return nil }}()"
            )

        if d == "propagate":
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
            with self._scoped(_propagate_cast_hint=""):
                inner_str = self._expr_to_go(node.children[0])
            self._q_temp_counter += 1
            tmp = f"__q{self._q_temp_counter}"
            self._emit(f"{tmp} := {inner_str}")
            if self._q_propagate_ok and self._guard_q_var:
                # Inside a match guard's condition closure: record the error
                # and select the case, whose body propagates it.
                self._emit(f"if !{tmp}.Ok() {{ {self._guard_q_var} = {tmp}; return true }}")
            elif self._q_propagate_ok:
                if self._in_async_func and self._async_chan_name:
                    self._emit(
                        f"if !{tmp}.Ok() {{ "
                        f"{self._async_chan_name} <- func() *Result {{ return {tmp} }}; return "
                        f"}}"
                    )
                else:
                    self._emit(f"if !{tmp}.Ok() {{ return {tmp} }}")
            else:
                self._emit(
                    f"if !{tmp}.Ok() {{ "
                    f"panic({tmp}.Error) "
                    f"}}"
                )
            self._declare_var(tmp)
            cast = self._propagate_cast_hint or self._propagated_payload_go_type(node.children[0])
            if cast and cast != "interface{}":
                return f"{tmp}.Value.({cast})"
            return f"{tmp}.Value"

        if d == "null_coalesce":
            # ``a ?? b ?? c`` — return the first non-nil operand. We
            # fold right-to-left, each layer wrapping the previous
            # result, so evaluation still stops at the first non-nil
            # value (Go's early ``return`` inside the IIFE).
            #
            # The left operand is boxed as ``interface{}`` before the
            # comparison so Go doesn't complain about untyped-nil or
            # primitive-to-nil comparisons (``"s" != nil`` and
            # ``nil != nil`` are both compile errors otherwise).
            nodes = [c for c in node.children if isinstance(c, Tree)]
            if len(nodes) == 2 and self._expr_go_type_hint(nodes[0]) == "LamJSON":
                left = self._expr_to_go(nodes[0])
                expected = (getattr(self, "_expected_expr_go_type", "") or "").strip()
                if expected and expected not in {"LamJSON", "interface{}"}:
                    left_value = f"lamJSONRaw({left}).({expected})"
                    fallback = self._typed_value_to_go(nodes[1], expected)
                    return f"func() {expected} {{ if !lamJSONIsNull({left}) {{ return {left_value} }}; return {fallback} }}()"
                fallback = self._expr_to_go(nodes[1])
                return f"func() interface{{}} {{ if !lamJSONIsNull({left}) {{ return {left} }}; return {fallback} }}()"
            if len(nodes) == 2 and self._is_none_literal(nodes[1]):
                # ``x ?? None`` is ``x``.
                return self._expr_to_go(nodes[0])
            if (
                len(nodes) == 2
                and nodes[0].data == "getitem"
                and not (isinstance(nodes[0].children[1], Tree) and nodes[0].children[1].data == "slice")
            ):
                map_type = self._expr_go_type_hint(nodes[0].children[0]) or ""
                key_type, value_type = self._split_go_map_type(map_type)
                if key_type and value_type and not self._is_go_set_type(map_type):
                    # ``d[key] ?? fallback`` on a map: a missing key (or a
                    # ``None`` value) selects the fallback.
                    fallback_type = self._expr_go_type_hint(nodes[1]) or self._infer_type_from_value(nodes[1]) or ""
                    result_type = value_type
                    if value_type != "interface{}" and fallback_type not in {"", "interface{}", value_type}:
                        result_type = "interface{}"
                    receiver = self._expr_to_go(nodes[0].children[0])
                    key = self._typed_value_to_go(nodes[0].children[1], key_type)
                    fallback = self._typed_value_to_go(nodes[1], result_type)
                    present = "ok" if value_type != "interface{}" and not value_type.startswith(("*", "[]", "map[")) else "ok && __lamV != nil"
                    return (
                        f"func() {result_type} {{ if __lamV, ok := {receiver}[{key}]; {present} {{ return __lamV }}; "
                        f"return {fallback} }}()"
                    )
            left_hint = self._expr_go_type_hint(nodes[0]) if len(nodes) == 2 else ""
            if left_hint.startswith("*") and "[" not in left_hint:
                # ``opt ?? fallback`` on an ``optional[T]`` (a Go pointer):
                # a nil pointer selects the fallback (boxing it in
                # ``interface{}`` first would make a typed nil non-nil);
                # an ``optional[int]`` yields the pointed-to ``int``.
                left = self._expr_to_go(nodes[0])
                inner = left_hint[1:]
                fallback_type = self._expr_go_type_hint(nodes[1]) or self._infer_type_from_value(nodes[1]) or ""
                mixed = (
                    inner in self._OPTIONAL_VALUE_GO_TYPES
                    and fallback_type not in {"", "interface{}", inner}
                    and not (inner == "float64" and fallback_type == "int")
                )
                if inner in self._OPTIONAL_VALUE_GO_TYPES and not mixed:
                    fallback = self._typed_value_to_go(nodes[1], inner)
                    return f"func() {inner} {{ if __lamL := {left}; __lamL != nil {{ return *__lamL }}; return {fallback} }}()"
                if inner.split("[", 1)[0] in {self._go_class_name(c) for c in self._class_names}:
                    fallback = self._typed_value_to_go(nodes[1], left_hint)
                    return f"func() {left_hint} {{ if __lamL := {left}; __lamL != nil {{ return __lamL }}; return {fallback} }}()"
            operands = [c for c in node.children if c is not None]
            parts = [self._expr_to_go(c) for c in operands]
            if not parts:
                return "nil"
            if len(parts) == 1:
                return parts[0]
            expr = parts[-1]
            for operand, left in reversed(list(zip(operands[:-1], parts[:-1]))):
                hint = self._expr_go_type_hint(operand) or ""
                if hint.startswith("*"):
                    # A typed nil pointer boxed in ``interface{}`` is not
                    # ``nil``: test the pointer itself.
                    value = "*__lamP" if hint[1:] in self._OPTIONAL_VALUE_GO_TYPES else "__lamP"
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
            obj = self._expr_to_go(node.children[0])
            negative = self._negative_int_constant(inner)
            receiver_type = self._expr_go_type_hint(node.children[0]) or ""
            if negative is not None and not receiver_type.startswith("map[") and receiver_type not in {"interface{}"}:
                # ``xs[-1]``: Python semantics, counted from the end (an
                # index still out of range raises ``IndexError``).
                if re.fullmatch(r"[A-Za-z_][\w.]*", obj):
                    return f"{obj}[len({obj})-{negative}]"
                if receiver_type == "string" or receiver_type.startswith("[]"):
                    elem = "byte" if receiver_type == "string" else self._slice_elem_go_type(receiver_type)
                    return f"func(_s {receiver_type}) {elem} {{ return _s[len(_s)-{negative}] }}({obj})"
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
            parts = [self._expr_to_go(c) for c in node.children if c is not None]
            wrapped = [f"({p})" if any(op in p for op in "+-*/%") else p for p in parts]
            inner = " & ".join(wrapped)
            return f"({inner})" if len(parts) > 1 else inner

        if d == "or_expr":
            union = self._set_chain_to_go(node, "|")
            if union:
                return union
            parts = [self._expr_to_go(c) for c in node.children if c is not None]
            wrapped = [f"({p})" if any(op in p for op in "+-*/%") else p for p in parts]
            inner = " | ".join(wrapped)
            return f"({inner})" if len(parts) > 1 else inner

        if d == "xor_expr":
            parts = [self._expr_to_go(c) for c in node.children if c is not None]
            wrapped = [f"({p})" if any(op in p for op in "+-*/%") else p for p in parts]
            inner = " ^ ".join(wrapped)
            return f"({inner})" if len(parts) > 1 else inner

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
                operand = self._expr_to_go(operand_node)
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
                base_is_int = isinstance(node.children[0], Tree) and node.children[0].data == "number" and "." not in str(node.children[0].children[0])
                exp_is_int = isinstance(node.children[1], Tree) and node.children[1].data == "number" and "." not in str(node.children[1].children[0])
                if base_is_int and exp_is_int:
                    try:
                        exp_val = int(exp)
                        if exp_val > 63:
                            self._need_import("math/big")
                            return f'new(big.Int).Exp(big.NewInt({base}), big.NewInt({exp}), nil)'
                    except ValueError:
                        pass
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
                parts.append(part)
            return f" {op} ".join(parts)

        if d == "tuple":
            elems = [self._expr_to_go(c) for c in node.children if c is not None]
            return ", ".join(elems)

        if d == "list":
            elems = [self._expr_to_go(c) for c in node.children if c is not None]
            return f"[]interface{{}}{{{', '.join(elems)}}}"

        if d == "list_comprehension":
            return self._list_comp_to_go(node)

        if d == "dict":
            return self._dict_to_go(node)

        if d == "dict_comprehension":
            return self._dict_comp_to_go(node)

        if d == "set":
            # Build the set inside an IIFE so duplicate literals
            # don't trip Go's "duplicate key in map literal" check
            # — sets are de-duplicating by definition.
            elems = [self._expr_to_go(c) for c in node.children if c is not None]
            if not elems:
                return "map[interface{}]bool{}"
            lines = ["func() map[interface{}]bool {",
                     "\t_s := map[interface{}]bool{}"]
            for e in elems:
                lines.append(f"\t_s[{e}] = true")
            lines.append("\treturn _s")
            lines.append("}()")
            return self._pin_multiline_to(node, "\n".join(lines))

        if d == "set_comprehension":
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
                common = self._ternary_common_go_type(node)
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

        if d == "yield_expr" or d == "yield_from":
            if self._in_generator and self._generator_chan:
                if node.children and node.children[0] is not None:
                    value_node = node.children[0]
                    if isinstance(value_node, Tree) and value_node.data == "tuple":
                        # ``yield ("a", 1)``: one tuple value per receive.
                        val = self._typed_value_to_go(value_node, "[]interface{}")
                    elif self._generator_elem_type:
                        # ``yield [1, 2]`` in a ``-> generator[list[int]]``.
                        val = self._typed_value_to_go(value_node, self._generator_elem_type)
                    else:
                        val = self._expr_to_go(value_node)
                    return f"{self._generator_chan} <- {val}"
                return f"{self._generator_chan} <- nil"
            return "/* yield not supported */"

        if d == "await_call":
            # The handle yields a thunk returning the result or re-raising
            # the async body's exception (``_emit_async_callable``).
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
        else:
            call_args = self._planned_call_args(call_key, args_node, args, kwargs)
        if type_args is not None:
            return f"{go_name}[{self._args_to_go(type_args)}]({self._args_to_go(call_args)})"
        return f"{go_name}({self._args_to_go(call_args)})"

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
        go_cls = self._go_class_name(class_name)
        method_key = f"{class_name}.{method_name}"
        suffix = ""
        call_key = method_key
        if len(self._overload_variants.get(method_key, [])) > 1:
            call_sig = self._infer_call_arg_sig(args_node)
            variant = self._overload_variant_for_sig(method_key, call_sig)
            suffix = self._overload_suffix_for_sig(method_key, call_sig)
            if variant is not None:
                call_key = str(variant.get("meta_key") or method_key)
        go_name = f"{go_cls}_{method_name}{suffix}"
        if method_name in self._private_methods.get(class_name, set()):
            go_name = self._go_private_name(go_name)
        call_args = self._planned_call_args_inferred(call_key, args_node)
        explicit = self._static_generic_fallback_type_args(class_name, call_key, args_node)
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
        return obj_class

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
            if args is None:
                args, kwargs = self._collect_call_args(args_node)
            return self._nested_generic_call_to_go(base_name, args, type_args)
        if base_name in self._class_names and base_name in self._generic_classes:
            type_args = self._type_arg_list(func.children[1])
            go_cls = self._go_class_name(base_name)
            init_key = f"{base_name}.init"
            call_args = self._planned_call_args(init_key, args_node)
            return f"New{go_cls}[{self._args_to_go(type_args)}]({self._args_to_go(call_args)})"
        if base_name in self._user_functions:
            type_args = self._type_arg_list(func.children[1])
            return self._user_function_call_to_go(
                base_name, args or [], kwargs or {}, args_node, type_args=type_args,
            )
        return None

    def _funccall_to_go(self, node: Tree) -> str:
        # A call nested in another call's callee (``make(inc).get()``) must
        # lower its own arguments outside the outer callee context, so bare
        # function references are mangled.
        if self._in_funccall_head:
            with self._scoped(_in_funccall_head=False):
                return self._funccall_to_go_inner(node)
        return self._funccall_to_go_inner(node)

    def _funccall_to_go_inner(self, node: Tree) -> str:
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

        # ``obj?.method(args)`` — safe-navigation followed by a call.
        # We don't want the ``getattr_safe`` IIFE (which returns
        # ``interface{}``) as a callee, so rewrite the whole thing as
        # a nil-guarded call at this layer.
        if isinstance(func, Tree) and func.data == "getattr_safe":
            args, kwargs = plain_call_args()
            obj = self._expr_to_go(func.children[0])
            attr = self._get_name(func.children[1])
            if self._is_private_method_name(attr):
                method = self._go_private_name(attr)
            else:
                method = self._go_public_name(attr)
            args_str = ", ".join(args)
            return (
                f"func() interface{{}} {{ if {obj} != nil {{ "
                f"return {obj}.{method}({args_str}) }}; return nil }}()"
            )

        if isinstance(func, Tree) and func.data == "getitem":
            # ``handlers[0](x)`` / ``table["inc"](x)``: call the function
            # value stored in a collection element.
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
            raw_obj = self._expr_to_go(func.children[0])
            raw_method = self._get_name(func.children[1])
            _alias, parent_base, parent_receiver = self._parent_alias_call_info(func)
            if parent_base and raw_method:
                method_key = f"{parent_base}.init" if raw_method in {"init", "__init__"} else f"{parent_base}.{raw_method}"
                call_args = self._planned_call_args(method_key, args_node)
                if raw_method in {"init", "__init__"}:
                    go_base = self._go_class_name(parent_base)
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
                        [f"{parent_receiver} = New{go_base}{type_args}({', '.join(call_args)})"] + binds + defaults
                    )
            if raw_obj == "LAMMERGEIER" and raw_method:
                if raw_method in self._user_functions:
                    return self._user_function_call_to_go(raw_method, [], {}, args_node)
                if raw_method in self._class_names:
                    call_args = self._planned_call_args(f"{raw_method}.init", args_node)
                    return f"New{self._go_class_name(raw_method)}({', '.join(call_args)})"
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
            raw_obj is not None and isinstance(receiver_node, Tree) and receiver_node.data == "funccall"
        )
        if reused:
            # ``a.f().g()``: the receiver call was lowered for ``raw_obj``;
            # lowering it again for the callee doubled the work per link.
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
                args = [self._optional_display_go(a, r) for a, r in zip(args, raw_args)]
            return f'fmt.Println({", ".join(args)})'

        if func_str == "len":
            raw_args = self._get_raw_call_args(args_node)
            if raw_args and self._expr_go_type_hint(raw_args[0]) == "LamJSON":
                return f"lamJSONLen({args[0]})"
            return f"len({args[0]})" if args else "0"

        if func_str == "str":
            self._need_import("fmt")
            return f'fmt.Sprintf("%v", {args[0]})' if args else '""'

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
                arg_type = self._expr_go_type_hint(raw_args[0]) or self._infer_type_from_value(raw_args[0]) or ""
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
                target_expr = f"append({slice_type}{{}}, {source}...)"
                return (
                    f"func() {slice_type} {{ tmp := {target_expr}; {prologue}"
                    f"sort.SliceStable(tmp, func(i, j int) bool {{ return {less_expr} }}); return tmp }}()"
                )

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
            return (
                "func() []interface{} { "
                "v := reflect.ValueOf(" + src + "); "
                "if !v.IsValid() { return []interface{}{} }; "
                "out := make([]interface{}, v.Len()); "
                "for i := 0; i < v.Len(); i++ { "
                "out[i] = []interface{}{i, v.Index(i).Interface()} "
                "}; return out }()"
            )

        if func_str == "isinstance":
            raw = self._get_raw_call_args(args_node)
            if len(raw) >= 2:
                obj_go = self._expr_to_go(raw[0])
                if self._isinstance_name(raw[1]) == "None":
                    static = self._expr_go_type_hint(raw[0]) or self._infer_type_from_value(raw[0]) or ""
                    if static in self._OPTIONAL_VALUE_GO_TYPES:
                        # A value-typed operand is never ``None``.
                        return "false"
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
        if func_str.startswith("math."):
            self._need_import("math")
            method = func_str[5:]
            go_method = method[0].upper() + method[1:]
            return f"math.{go_method}({', '.join(args)})"

        if func_str.startswith("os."):
            self._need_import("os")
            return f"{func_str}({', '.join(args)})"

        if func_str.startswith("strings."):
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
                if raw_method == "append" and args:
                    typed_args = [
                        self._typed_value_to_go(arg_node, _elem_go) if _elem_go else arg
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
                        return (
                            f"func(i int) {result_type} {{ if i < 0 {{ i += len({raw_obj}) }}; item := {raw_obj}[i]; "
                            f"{raw_obj} = append({raw_obj}[:i], {raw_obj}[i+1:]...); return item }}({args[0]})"
                        )
                    return f"func() {result_type} {{ last := {raw_obj}[len({raw_obj})-1]; {raw_obj} = {raw_obj}[:len({raw_obj})-1]; return last }}()"
                if raw_method == "extend" and args:
                    typed_arg = (
                        self._typed_value_to_go(raw_args_nodes[0], f"[]{_elem_go}")
                        if _elem_go and raw_args_nodes else args[0]
                    )
                    return f"{raw_obj} = append({raw_obj}, {typed_arg}...)"
                if raw_method == "insert" and len(args) >= 2:
                    item_arg = (
                        self._typed_value_to_go(raw_args_nodes[1], _elem_go)
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
                    return self._go_user_function_name(fn_expr)
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
                    if key_type in self._ORDERED_GO_TYPES:
                        key_less = "_k(_r[i]) < _k(_r[j])"
                    elif key_type == "bool":
                        key_less = "!_k(_r[i]) && _k(_r[j])"
                    else:
                        self._need_import("fmt")
                        key_less = 'fmt.Sprintf("%v", _k(_r[i])) < fmt.Sprintf("%v", _k(_r[j]))'
                    return (
                        f"func() {_slice_type} {{ _r := {target_expr}; _k := {key_fn}; "
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
                if is_user_func or result_elem_type == "interface{}":
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
                # ``go! LAMMERGEIER.foo()`` both work.
                return self._resolve_user_lammergeier(inline_raw)
            return f"/* go inline {block_id} not found */"

        # ── User-defined function ──
        if func_str in self._nested_generic_functions:
            return self._nested_generic_call_to_go(func_str, args)

        if func_str in self._user_functions:
            return self._user_function_call_to_go(func_str, args, kwargs, args_node)

        # ── Local variable (lambda, etc.) ──
        if func_str in self.declared_vars:
            return f"{func_str}({', '.join(args)})"

        # ── Constructor ──
        if func_str in self._class_names:
            go_cls = self._go_class_name(func_str)
            init_key = f"{func_str}.init"
            call_args = self._planned_call_args(init_key, args_node)
            return f"New{go_cls}({', '.join(call_args)})"
        if func_str and func_str[0].isupper() and "." not in func_str:
            return f"New{func_str}({', '.join(args)})"

        # ── Default ──
        go_name = self._go_public_name(func_str)
        return f"{go_name}({', '.join(args)})"

    # ─── f-string ──────────────────────────────────────────────

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
        # Cache parses to avoid re-running Lark for repeated slots
        # (a typical log line has the same identifier multiple times).
        cache = self.__dict__.setdefault("_fstring_expr_cache", {})
        if expr in cache:
            cached = cache[expr]
            if cached is None:
                return None
            return self._optional_display_go(self._expr_to_go(cached), cached)
        try:
            from compiler.lammergeier import create_parser as _cp
        except Exception:
            return None
        try:
            parser = _cp()
            wrapped = f"_lamFstrTmp = ({expr});\n"
            tree = parser.parse(wrapped)
        except Exception:
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
        try:
            return self._optional_display_go(self._expr_to_go(rhs), rhs)
        except Exception:
            cache[expr] = None
            return None

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
                        go_cls = self._go_class_name(obj_part)
                        go_name = f"{go_cls}_{method_name}"
                        return f"{go_name}({inner_args_go})"
                go_method = self._go_public_name(method_name)
                return f"{obj_go}.{go_method}({inner_args_go})"

        dot_match = re.match(r'^(\w+)\.(\w+)$', expr)
        if dot_match:
            obj_name = dot_match.group(1)
            attr_name = dot_match.group(2)
            if obj_name in LOWER_PKGS:
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
            return f"{go_name}({call_args})"

        return expr

    def _fstring_to_go(self, node: Tree) -> str:
        self._need_import("fmt")
        raw = str(node.children[0])
        # Triple-quoted ``f"""..."""`` must be tested *before* the
        # single-quoted forms — every triple-quoted literal also
        # starts with ``f"``/``f'`` so the shorter prefix would win
        # and leave two quote characters dangling on each end, which
        # then leak into the emitted Go format string.
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
        if return_anno is not None and return_anno.children:
            explicit_return = self._type_expr_to_go(return_anno.children[0])

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
        try:
            if is_block_body:
                with self._scoped(_current_return_type=resolved_return,
                                  **self._callable_boundary_context()):
                    body_src = self._lambda_block_body_to_go(body_node)
            elif void_body:
                # ``func()`` context: the body is evaluated for its effect.
                body_src = self._expr_to_go(body_node)
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
            return f"func({params_str}){ret_part} {{\n{body_src}\n}}"
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

    def _optional_display_go(self, value_go: str, node) -> str:
        """An ``optional[str]``-style value (a Go ``*string``) shown by
        ``print`` / f-strings: the pointed-to value, or ``<nil>``."""
        hint = self._expr_go_type_hint(node) if isinstance(node, Tree) else ""
        if hint and hint.startswith("*") and hint[1:] in self._OPTIONAL_VALUE_GO_TYPES:
            return f"func() interface{{}} {{ if __lamP := {value_go}; __lamP != nil {{ return *__lamP }}; return nil }}()"
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
        if value.data not in {
            "list", "dict", "set", "list_comprehension", "dict_comprehension",
            "set_comprehension", "test",
        }:
            return ""
        return f"{self._go_class_name('Result')}_Ok({self._typed_value_to_go(value, payload)})"

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
        if name == "int" and source in {"float64", "float32"} and re.fullmatch(r"[-+0-9.eE()\s*/]+", value):
            # ``int(3.7)`` truncates (3); Go rejects converting a float
            # constant with a fractional part, so convert at run time.
            return f"func(v float64) int {{ return int(v) }}({value})"
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
                f"case float64: return int(n); case float32: return int(n); "
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

    def _ternary_common_go_type(self, node) -> str:
        """Common Go type of ``a if c else b`` branches; ``""`` when unknown.

        Equal known types are used as-is; ``int`` with ``float64`` widens to
        ``float64``. Unknown or ``interface{}`` branches keep the dynamic
        fallback.
        """
        def branch_type(branch) -> str:
            go_type = self._expr_go_type_hint(branch) or self._infer_type_from_value(branch) or ""
            return "" if go_type == "interface{}" else go_type

        left = branch_type(node.children[0])
        right = branch_type(node.children[2])
        if not left or not right:
            return ""
        if left == right:
            return left
        if {left, right} == {"int", "float64"}:
            return "float64"
        return ""

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

    def _propagated_payload_go_type(self, operand) -> str:
        if not isinstance(operand, Tree) or operand.data != "funccall" or not operand.children:
            return ""
        callee = operand.children[0]
        if not isinstance(callee, Tree):
            return ""
        if callee.data == "var" and callee.children:
            return self._payload_go_type_for_call(self._get_name(callee.children[0]), operand)
        if callee.data not in {"getattr", "getattr_safe"} or len(callee.children) < 2:
            return ""
        receiver = callee.children[0]
        method = self._get_name(callee.children[1])
        if not isinstance(receiver, Tree):
            return ""
        if receiver.data != "var" or not receiver.children:
            class_name = self._infer_receiver_class(receiver)
            return self._payload_go_type_for_call(f"{class_name}.{method}", operand) if class_name else ""
        receiver_name = self._get_name(receiver.children[0])
        class_name = receiver_name if receiver_name in self._class_names else self._var_types.get(receiver_name, "")
        if not class_name and receiver_name == "self" and self.current_class:
            # ``self.items()?`` inside a method of the class.
            class_name = self.current_class
        key = f"{class_name}.{method}" if class_name else f"{receiver_name}.{method}"
        return self._payload_go_type_for_call(key, operand)

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
                receiver = callee.children[0] if isinstance(callee, Tree) and callee.data in {"getattr", "getattr_safe"} else None
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
        if obj_node.data == "var":
            name = self._get_name(obj_node.children[0])
            known = self._var_types.get(name, "")
            if known:
                return known
            # Loop variables over ``list[Shape]`` and similar only record
            # their Go type.
            return self._hier_class_from_go_type(self._var_go_types.get(name, ""))
        if obj_node.data == "getitem":
            # ``shapes[0].area()`` over a ``list[Shape]`` / ``dict[str, Shape]``.
            return self._hier_class_from_go_type(self._expr_go_type_hint(obj_node))
        if obj_node.data in {"getattr", "getattr_safe"}:
            go_type = self._expr_go_type_hint(obj_node)
            class_name = go_type.removeprefix("*").split("[", 1)[0]
            if class_name in self._class_names:
                return class_name
        if obj_node.data == "propagate" and obj_node.children:
            go_type = self._propagated_payload_go_type(obj_node.children[0])
            class_name = go_type.removeprefix("*").split("[", 1)[0]
            return class_name if class_name in self._class_names else ""
        if obj_node.data == "funccall":
            inner_func = obj_node.children[0]
            if isinstance(inner_func, Tree):
                if inner_func.data == "getattr":
                    inner_obj = inner_func.children[0]
                    inner_method = self._get_name(inner_func.children[1])
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
                        return returned if returned in self._class_names or returned in self._interfaces else ""
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

    def _generator_call_elem_go_type(self, node) -> str:
        if isinstance(node, Tree) and node.data == "funccall" and node.children:
            callee = node.children[0]
            if isinstance(callee, Tree) and callee.data == "var" and callee.children:
                return self._generator_elem_go_types.get(self._get_name(callee.children[0]), "")
        return ""

    def _comprehension_tuple_raw_targets(self, target) -> list:
        if not isinstance(target, Tree):
            return []
        if target.data in {"typed_for_target", "untyped_for_target"} and target.children:
            return self._comprehension_tuple_raw_targets(target.children[0])
        if target.data in {"tuple", "exprlist"}:
            return [child for child in target.children if child is not None]
        return []

    def _append_comprehension_loop(self, lines: List[str], clause: Tree, depth: int) -> None:
        var_expr = clause.children[0]
        iter_expr = clause.children[1]
        var_str = self._expr_to_go(var_expr)
        targets = self._comprehension_tuple_targets(var_expr)
        range_info = self._check_range_call(iter_expr) if isinstance(iter_expr, Tree) and iter_expr.data == "funccall" else None
        tabs = "\t" * (depth + 1)
        if range_info:
            start, end, step = range_info
            if var_str == "_":
                # Go has no ``_`` loop counter; the value is unused anyway.
                var_str = f"_lamCompIndex{depth}"
            lines.append(f"{tabs}for {var_str} := {start}; {var_str} < {end}; {var_str} += {step} {{")
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
            raw_targets = self._comprehension_tuple_raw_targets(var_expr)
            nested = raw_targets[1] if len(raw_targets) == 2 else None
            if isinstance(nested, Tree) and nested.data in {"tuple", "exprlist"}:
                # ``for i, (name, n) in enumerate(pairs)``: unpack the item.
                inner = [self._expr_to_go(c) for c in nested.children if c is not None]
                item = f"_lamCompItem{depth}"
                lines.append(f"{tabs}for {idx}, {item} := range {enumerate_src} {{")
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
            if idx != "_":
                self._var_go_types[idx] = "int"
            if val != "_" and src_type.startswith("[]"):
                self._var_go_types[val] = src_type[2:]
            elif val != "_" and src_type == "string":
                lines[-1] = f"{tabs}for {idx}, {val}Rune := range []rune({enumerate_src}) {{"
                lines.append(f"{bind_tabs}{val} := string({val}Rune)")
                self._var_go_types[val] = "string"
            silence(targets)
            return
        iter_str = self._expr_to_go(iter_expr)
        if self._is_generator_call(iter_expr):
            # A generator is a channel: one value per receive.
            if targets:
                pair_name = f"_lamCompPair{depth}"
                lines.append(f"{tabs}for {pair_name} := range {iter_str} {{")
                for index, target in enumerate(targets):
                    if target != "_":
                        lines.append(f"{bind_tabs}{target} := {pair_name}.([]interface{{}})[{index}]")
                silence(targets)
            else:
                name = var_str if var_str != "_" else f"_lamCompItem{depth}"
                elem = self._generator_call_elem_go_type(iter_expr)
                if elem and name == var_str:
                    # ``for b in batches()`` with ``-> generator[list[int]]``.
                    lines.append(f"{tabs}for {name}Raw := range {iter_str} {{")
                    lines.append(f"{bind_tabs}{name} := {name}Raw.({elem})")
                    self._var_go_types[name] = elem
                else:
                    lines.append(f"{tabs}for {name} := range {iter_str} {{")
                silence([name])
            return
        if isinstance(iter_expr, Tree) and iter_expr.data == "list" and not targets:
            # ``[i for i in [1, 2, 3] if i > 1]``: range over the typed slice
            # so the variable has the elements' type.
            literal_type = self._infer_type_from_value(iter_expr)
            if literal_type.startswith("[]") and literal_type != "[]interface{}":
                iter_str = self._typed_value_to_go(iter_expr, literal_type)
                self._var_go_types[var_str] = literal_type[2:]
        if len(targets) == 2 and self._range_single_target_uses_key(iter_expr):
            lines.append(f"{tabs}for {targets[0]}, {targets[1]} := range {iter_str} {{")
            key_type, value_type = self._split_go_map_type(self._expr_go_type_hint(iter_expr) or self._infer_type_from_value(iter_expr) or "")
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
        if self._range_single_target_uses_key(iter_expr):
            lines.append(f"{tabs}for {var_str} := range {iter_str} {{")
        elif self._iterates_string(iter_expr):
            # Lam yields one-character strings, Go ranges over runes.
            lines.append(f"{tabs}for _, {var_str}Rune := range {iter_str} {{")
            lines.append(f"{chr(9) * (depth + 2)}{var_str} := string({var_str}Rune)")
            self._var_go_types[var_str] = "string"
        else:
            lines.append(f"{tabs}for _, {var_str} := range {iter_str} {{")

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
            self._need_import("slices")
            return f"slices.{name.capitalize()}({args[0]})"
        if types and all(t in ints for t in types) and len(set(types)) == 1:
            return f"{name}({', '.join(args)})"
        if types and all(t in ints | {"float64", "float32"} for t in types):
            return f"{name}({', '.join(f'float64({a})' for a in args)})"
        if types and all(t == "string" for t in types):
            return f"{name}({', '.join(args)})"
        if len(args) >= 2:
            # Unknown operand types: compare as floats (previous behaviour).
            return f"{name}({', '.join(self._unknown_conversion_to_go('float', a) if t in {'', 'interface{}'} else f'float64({a})' for a, t in zip(args, types))})"
        return ""

    def _iterates_string(self, iter_expr) -> bool:
        return (self._expr_go_type_hint(iter_expr) or self._infer_type_from_value(iter_expr)) == "string"

    def _list_comp_to_go(self, node: Tree, elem_type: str = None) -> str:
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
        result_str = (
            self._typed_value_to_go(result_expr, result_type)
            if result_type else self._expr_to_go(result_expr)
        )

        inner_tabs = "\t" * (depth + 1)
        if comp_if and isinstance(comp_if, Tree):
            cond_str = self._expr_to_go(comp_if)
            lines.append(f"{inner_tabs}if {cond_str} {{")
            lines.append(f"{inner_tabs}\t_result = append(_result, {result_str})")
            lines.append(f"{inner_tabs}}}")
        else:
            lines.append(f"{inner_tabs}_result = append(_result, {result_str})")

        for i in range(depth):
            tabs = "\t" * (depth - i)
            lines.append(f"{tabs}}}")

        lines.append(f"\treturn _result")
        lines.append(f"}}()")

        return self._pin_multiline_to(node, "\n".join(lines))

    # ─── Set comprehension ─────────────────────────────────────

    def _set_comp_to_go(self, node: Tree, map_type: str = None) -> str:
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
        result_str = (
            self._typed_value_to_go(result_expr, key_type)
            if key_type else self._expr_to_go(result_expr)
        )

        inner_tabs = "\t" * (depth + 1)
        if comp_if and isinstance(comp_if, Tree):
            cond_str = self._expr_to_go(comp_if)
            lines.append(f"{inner_tabs}if {cond_str} {{")
            lines.append(f"{inner_tabs}\t_result[{result_str}] = true")
            lines.append(f"{inner_tabs}}}")
        else:
            lines.append(f"{inner_tabs}_result[{result_str}] = true")

        for i in range(depth):
            tabs = "\t" * (depth - i)
            lines.append(f"{tabs}}}")

        lines.append(f"\treturn _result")
        lines.append(f"}}()")

        return self._pin_multiline_to(node, "\n".join(lines))

    # ─── Dict comprehension ───────────────────────────────────

    def _dict_comp_to_go(self, node: Tree, map_type: str = None) -> str:
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
        key_expr = (
            self._typed_value_to_go(kv.children[0], key_type)
            if key_type else self._expr_to_go(kv.children[0])
        )
        val_expr = (
            self._typed_value_to_go(kv.children[1], value_type)
            if value_type else self._expr_to_go(kv.children[1])
        )

        inner_tabs = "\t" * (depth + 1)
        if comp_if and isinstance(comp_if, Tree):
            cond_str = self._expr_to_go(comp_if)
            lines.append(f"{inner_tabs}if {cond_str} {{")
            lines.append(f"{inner_tabs}\t_r[{key_expr}] = {val_expr}")
            lines.append(f"{inner_tabs}}}")
        else:
            lines.append(f"{inner_tabs}_r[{key_expr}] = {val_expr}")

        for index in range(depth):
            tabs = "\t" * (depth - index)
            lines.append(f"{tabs}}}")
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
            dunder = CMP_TO_DUNDER.get(comp_ops[0], "")
            method = self._get_dunder_method(cls, dunder) if dunder else ""
            if method:
                left = self._expr_to_go(exprs[0])
                right = self._expr_to_go(exprs[1])
                return f"{left}.{method}({right})"

        return ""

    def _binop_to_go(self, node: Tree) -> str:
        operands = [c for c in node.children if isinstance(c, Tree)]
        ops = {str(c) for c in node.children if isinstance(c, Token)}
        promote: set = set()
        if ops <= {"+", "-", "*", "/"} and len(operands) >= 2:
            # ``1.5 * a`` with ``a: int``: Go needs the int converted.
            kinds = [self._numeric_operand_kind(c) for c in operands]
            if "float64" in kinds and "int" in kinds:
                promote = {id(c) for c, k in zip(operands, kinds) if k == "int"}
        parts = []
        for child in node.children:
            if isinstance(child, Token):
                op = str(child)
                parts.append("/" if op == "//" else op)
            elif isinstance(child, Tree):
                s = self._expr_to_go(child)
                if id(child) in promote:
                    s = f"float64({s})"
                elif child.data in ("arith_expr", "term") and node.data != child.data:
                    s = f"({s})"
                parts.append(s)
        return " ".join(parts)

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
        left_go = self._comparison_operand_to_go(left)
        right_go = self._comparison_operand_to_go(right)
        left_json = self._expr_go_type_hint(left) == "LamJSON"
        right_json = self._expr_go_type_hint(right) == "LamJSON"
        if (left_json or right_json) and op in {"==", "!=", "is", "is not"}:
            left_value = left_go if left_json else f"lamJSONMust({left_go})"
            right_value = right_go if right_json else f"lamJSONMust({right_go})"
            equal = f"lamJSONEqual({left_value}, {right_value})"
            return f"!({equal})" if op in {"!=", "is not"} else equal
        if op in {"==", "!=", "is", "is not"}:
            # Identity of a subclass instance and a base-typed value:
            # compare both as the base (the subclass's embedded parent).
            left_type = self._hier_expr_go_type(left)
            right_type = self._hier_expr_go_type(right)
            left_go = self._hier_upcast_to_go(left_go, left_type, right_type) or left_go
            right_go = self._hier_upcast_to_go(right_go, right_type, left_type) or right_go
        if op == "is not":
            return f"{left_go} != {right_go}"
        if op == "is":
            return f"{left_go} == {right_go}"
        return f"{left_go} {op} {right_go}"

    def _comparison_operand_to_go(self, node) -> str:
        """An operand that is itself a comparison or boolean expression was
        parenthesized in Lam; Go needs the parentheses too."""
        go = self._expr_to_go(node)
        if isinstance(node, Tree) and node.data in {"comparison", "and_test", "or_test"}:
            return f"({go})"
        return go

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

        if container_type.startswith("map["):
            key_type, _value_type = self._split_go_map_type(container_type)
            item_go = self._typed_value_to_go(item_node, key_type or "interface{}")
            return (
                f"func() bool {{ _, _ok := {container_go}[{item_go}]; "
                f"return _ok }}()"
            )

        if container_type.startswith("[]"):
            self._need_import("reflect")
            item_go = self._typed_value_to_go(item_node, self._slice_elem_go_type(container_type) or "interface{}")
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
        iterable_type = self._expr_go_type_hint(iterable_node) or self._infer_type_from_value(iterable_node)
        return (iterable_type or "").strip().startswith("map[")

    def _dict_to_go(self, node: Tree) -> str:
        entries = []
        has_non_string_key = False
        for child in node.children:
            if isinstance(child, Tree) and child.data == "key_value":
                k = self._expr_to_go(child.children[0])
                v = self._expr_to_go(child.children[1])
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
