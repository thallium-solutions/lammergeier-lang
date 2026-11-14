"""Constant expressions the Go compiler would reject.

Go evaluates constant expressions (literals combined with ``+ - * << >> & |
^`` and unary ``- + ~``) exactly at compile time and rejects a result that
does not fit its type, a negative shift count and duplicate constant keys of
a map literal. Lam lowers those operators to the same Go operators, so a
program the checker accepted used to fail in ``go build``; this pass reports
them as Lam errors instead (docs/SAFETY.md). Values computed at run time are
not affected: 64-bit ints wrap, as documented.
"""

from __future__ import annotations

import ast
from typing import Callable

from lark import Token, Tree

INT_MIN, INT_MAX = -(1 << 63), (1 << 63) - 1
FLOAT_MAX = 1.7976931348623157e308

_NOT_CONST = object()


class _Huge:
    """An integer literal too long for Python's int-to-str limit (4300
    digits by default): far beyond what Go's constants hold, so it overflows
    whatever it is combined with."""

    def __init__(self, digits: int):
        self.digits = digits


def _number(text: str):
    t = text.replace("_", "").lower()
    try:
        if t.startswith(("0x", "0o", "0b")):
            return int(t, 0)
        if any(c in t for c in ".e") and not t.startswith("0x"):
            return float(t)
        return int(t, 10)
    except (ValueError, OverflowError):
        if t.isdigit():
            return _Huge(len(t))
        return _NOT_CONST


class _Folder:
    def __init__(self, report: Callable[[object, str], None]):
        self.report = report

    def fold(self, node):
        """The exact value of a Go constant expression, or ``_NOT_CONST``."""
        if not isinstance(node, Tree):
            return _NOT_CONST
        d = node.data
        if d == "number" and node.children:
            return _number(str(node.children[0]))
        if d == "factor" and len(node.children) == 2:
            v = self.fold(node.children[1])
            if v is _NOT_CONST or isinstance(v, _Huge):
                return v
            op = str(node.children[0])
            if op == "-":
                return -v
            if op == "+":
                return v
            if op == "~" and isinstance(v, int):
                return ~v
            return _NOT_CONST
        if d in ("arith_expr", "term", "shift_expr"):
            operands = [c for c in node.children if isinstance(c, Tree)]
            ops = [str(c) for c in node.children if isinstance(c, Token)]
            if len(ops) != len(operands) - 1 or (d == "term" and set(ops) - {"*"}):
                return _NOT_CONST
            values = [self.fold(c) for c in operands]
            if any(v is _NOT_CONST for v in values):
                return _NOT_CONST
            huge = next((v for v in values if isinstance(v, _Huge)), None)
            if huge is not None:
                return huge
            acc = values[0]
            for op, v in zip(ops, values[1:]):
                if op in ("<<", ">>"):
                    if not (isinstance(acc, int) and isinstance(v, int)) or v < 0 or v > 4096:
                        return _NOT_CONST
                    acc = acc << v if op == "<<" else acc >> v
                elif op == "+":
                    acc = acc + v
                elif op == "-":
                    acc = acc - v
                elif op == "*":
                    acc = acc * v
                if isinstance(acc, int) and abs(acc) > (1 << 8192):
                    return _NOT_CONST
            return acc
        if d in ("and_expr", "or_expr", "xor_expr"):
            values = [self.fold(c) for c in node.children if isinstance(c, Tree)]
            huge = next((v for v in values if isinstance(v, _Huge)), None)
            if huge is not None:
                return huge
            if not values or any(not isinstance(v, int) or isinstance(v, bool) for v in values):
                return _NOT_CONST
            acc = values[0]
            for v in values[1:]:
                acc = acc & v if d == "and_expr" else (acc | v if d == "or_expr" else acc ^ v)
            return acc
        return _NOT_CONST

    def check_expr(self, node, context: str = "int") -> None:
        """Report the constant subexpressions of ``node`` that Go rejects."""
        stack = [(node, context)]
        while stack:
            cur, ctx = stack.pop()
            if not isinstance(cur, Tree):
                continue
            if cur.data in ("funcdef", "classdef", "suite"):
                # Nested statements are visited by ``check_tree``.
                if cur is not node:
                    continue
            value = self.fold(cur)
            if value is not _NOT_CONST:
                self._check_value(cur, value, ctx)
                continue
            if cur.data == "shift_expr":
                operands = [c for c in cur.children if isinstance(c, Tree)]
                ops = [str(c) for c in cur.children if isinstance(c, Token)]
                for op, right in zip(ops, operands[1:]):
                    count = self.fold(right)
                    if isinstance(count, int) and count < 0:
                        self.report(cur, f"negative shift count `{count}` in `{op}`")
            if cur.data == "power":
                self._check_power(cur)
            if cur.data == "dict":
                self._check_dict_keys(cur)
            if cur.data == "getitem":
                self._check_literal_string_index(cur)
            stack.extend((c, "int") for c in cur.children if isinstance(c, Tree))

    def _check_value(self, node, value, ctx: str) -> None:
        if isinstance(value, _Huge):
            self.report(node, f"integer constant with {value.digits} digits overflows `{ctx}`")
            return
        if isinstance(value, float):
            if value != value or abs(value) > FLOAT_MAX:
                self.report(node, "float constant overflows `float` (64-bit IEEE 754: at most about 1.8e308)")
            return
        if ctx == "float":
            if abs(value) > FLOAT_MAX:
                self.report(node, "integer constant overflows `float`")
            return
        if ctx == "byte":
            if not 0 <= value <= 255:
                self.report(node, f"constant {value} overflows `byte` (0..255)")
            return
        if not INT_MIN <= value <= INT_MAX:
            shown = str(value) if len(str(value)) <= 40 else f"{str(value)[:20]}...({len(str(abs(value)))} digits)"
            self.report(
                node,
                f"integer constant {shown} overflows `int` (64-bit: {INT_MIN}..{INT_MAX}); "
                "write a float literal (`1e30`) for a float value",
            )

    def _check_power(self, node) -> None:
        children = [c for c in node.children if c is not None]
        if len(children) != 2:
            return
        base, exp = self.fold(children[0]), self.fold(children[1])
        if not (isinstance(base, int) and isinstance(exp, int)) or isinstance(base, bool) or isinstance(exp, bool):
            return
        if 63 < exp <= 100000:
            if base in (0, 1, -1):
                return
            self.report(node, f"integer constant {base} ** {exp} overflows `int` (64-bit)")
        elif 0 <= exp <= 63 and not INT_MIN <= base ** exp <= INT_MAX:
            self.report(node, f"integer constant {base} ** {exp} overflows `int` (64-bit)")
        elif exp < 0:
            self.report(node, f"negative exponent in the int power {base} ** {exp}: use a float base ({base}.0 ** {exp})")

    def _check_literal_string_index(self, node) -> None:
        """``"abc"[9]``: a constant index out of the range of a string
        literal (``str`` indexes characters) is a compile error, like an
        out-of-range constant list index."""
        if len(node.children) < 2:
            return
        receiver, index = node.children[0], node.children[1]
        if not (isinstance(receiver, Tree) and receiver.data == "string" and receiver.children):
            return
        raw = str(receiver.children[0])
        if raw.startswith(("'" * 3, '"' * 3)):
            return
        try:
            text = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            return
        if not isinstance(text, str):
            return
        value = self.fold(index)
        if not isinstance(value, int) or isinstance(value, bool):
            return
        size = len(text)
        if not -size <= value < size:
            self.report(node, f"index {value} is out of range for the {size}-character string {raw}")

    def _check_dict_keys(self, node) -> None:
        seen: dict = {}
        for child in node.children:
            if not (isinstance(child, Tree) and child.data == "key_value" and child.children):
                continue
            key = child.children[0]
            value = _NOT_CONST
            if isinstance(key, Tree) and key.data == "string" and key.children:
                raw = str(key.children[0])
                if not raw.startswith(("'''", '"""')):
                    try:
                        value = ("str", ast.literal_eval(raw))
                    except (ValueError, SyntaxError):
                        value = _NOT_CONST
            elif isinstance(key, Tree) and key.data in ("const_true", "const_false"):
                value = ("bool", key.data == "const_true")
            else:
                folded = self.fold(key)
                if isinstance(folded, int):
                    value = ("int", folded)
            if value is _NOT_CONST:
                continue
            if value in seen:
                shown = repr(value[1]).replace("'", '"') if value[0] == "str" else str(value[1]).lower()
                self.report(key, f"duplicate key {shown} in dict literal (first given on line {seen[value]})")
            else:
                seen[value] = getattr(getattr(key, "meta", None), "line", 0) or 0


def _annotation_name(type_node) -> str:
    if isinstance(type_node, Tree):
        names = [str(t) for t in type_node.scan_values(lambda v: isinstance(v, Token))]
        if len(names) == 1:
            return names[0]
    return ""


def check_tree(tree: Tree, report: Callable[[object, str], None]) -> None:
    """Report every constant expression of ``tree`` Go would reject."""
    folder = _Folder(report)
    for node in tree.iter_subtrees_topdown():
        if node.data == "annassign":
            children = node.children
            type_node = children[3] if len(children) > 3 else None
            value = children[4] if len(children) > 4 else None
            if isinstance(value, Tree):
                ctx = _annotation_name(type_node)
                folder.check_expr(value, ctx if ctx in ("float", "byte") else "int")
        elif node.data == "const_stmt":
            parts = [c for c in node.children]
            value = parts[-1] if parts else None
            ctx = _annotation_name(parts[1]) if len(parts) >= 3 else ""
            if isinstance(value, Tree):
                folder.check_expr(value, ctx if ctx in ("float", "byte") else "int")
        elif node.data in ("expr_stmt", "return_stmt", "assign", "augassign", "if_stmt", "while_stmt",
                           "for_stmt", "match_stmt", "raise_stmt", "assert_stmt", "yield_expr", "del_stmt"):
            for child in node.children:
                if isinstance(child, Tree) and child.data not in ("suite", "elif_clause", "else_clause"):
                    folder.check_expr(child)
