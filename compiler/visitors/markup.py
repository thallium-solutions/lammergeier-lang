"""Go lowering of the native ``html`` / ``xml`` types.

Markup literals arrive as ``__markup__(template, slots...)`` (see
``compiler/markup.py``); the semantic checker records each literal's kind
on its node (``_lam_markup_kind``). Values are ``LamHTML`` / ``LamXML``
(``lib/lammarkup.lam``).
"""

from __future__ import annotations

from lark import Token, Tree

from compiler.markup import MARKER, MARKUP_GO_TYPES, MARKUP_METHODS, markup_method_go_name, markup_method_return_go

_GO_KINDS = {"LamHTML": "html", "LamXML": "xml"}


class MarkupTranspilerMixin:
    def _is_markup_marker_node(self, node) -> bool:
        if not isinstance(node, Tree) or node.data != "funccall" or not node.children:
            return False
        callee = node.children[0]
        return (
            isinstance(callee, Tree) and callee.data == "var" and bool(callee.children)
            and self._get_name(callee.children[0]) == MARKER
        )

    def _markup_cast_kind(self, node) -> str:
        """``html`` / ``xml`` for a ``html(x)`` / ``xml(x)`` cast."""
        if not isinstance(node, Tree) or node.data != "funccall" or not node.children:
            return ""
        callee = node.children[0]
        if not (isinstance(callee, Tree) and callee.data == "var" and callee.children):
            return ""
        name = self._get_name(callee.children[0])
        if name in MARKUP_GO_TYPES and name not in self._user_functions and name not in self._class_names:
            return name
        return ""

    def _markup_node_kind(self, node, forced: str = "") -> str:
        if forced:
            return forced
        kind = getattr(node, "_lam_markup_kind", None)
        if kind:
            return kind
        stack = self.__dict__.setdefault("_markup_kind_stack", [])
        return stack[-1] if stack else "html"

    def _markup_call_to_go(self, node, forced_kind: str = "") -> str | None:
        """A markup literal or an ``html(x)`` / ``xml(x)`` cast; None for
        other calls."""
        if self._is_markup_marker_node(node):
            kind = self._markup_node_kind(node, forced_kind)
            args = self._get_raw_call_args(node.children[1] if len(node.children) > 1 else None)
            template = args[0] if args else None
            token = None
            if isinstance(template, Tree):
                token = next(iter(template.scan_values(lambda v: isinstance(v, Token))), None)
            elif isinstance(template, Token):
                token = template
            go_template = str(token) if token is not None else '""'
            stack = self.__dict__.setdefault("_markup_kind_stack", [])
            stack.append(kind)
            try:
                slots = [self._expr_to_go(arg) for arg in args[1:]]
            finally:
                stack.pop()
            values = f"[]interface{{}}{{{', '.join(slots)}}}" if slots else "nil"
            func = "lamHTMLRender" if kind == "html" else "lamXMLRender"
            return f"{func}({go_template}, {values})"
        kind = self._markup_cast_kind(node)
        if kind:
            args = self._get_raw_call_args(node.children[1] if len(node.children) > 1 else None)
            if not args:
                return f"Lam{kind.upper()}{{}}"
            arg = args[0]
            if self._is_markup_marker_node(arg):
                return self._markup_call_to_go(arg, kind)
            stack = self.__dict__.setdefault("_markup_kind_stack", [])
            stack.append(kind)
            try:
                value = self._expr_to_go(arg)
            finally:
                stack.pop()
            return f"lam{kind.upper()}From({value})"
        return None

    def _markup_go_kind(self, node) -> str:
        """``html`` / ``xml`` when ``node`` is a markup value, else ''."""
        if self._is_markup_marker_node(node):
            return self._markup_node_kind(node)
        return _GO_KINDS.get(self._expr_go_type_hint(node) or "", "")

    def _markup_method_to_go(self, func: Tree, args_node, receiver_go: str) -> str | None:
        """``value.select(...)`` and the other DOM-style methods."""
        method = self._get_name(func.children[1])
        if method not in MARKUP_METHODS:
            return None
        kind = self._markup_go_kind(func.children[0])
        if not kind:
            return None
        params, _ret = MARKUP_METHODS[method]
        raw_args = self._get_raw_call_args(args_node)
        lowered = []
        for param, arg in zip(params, raw_args):
            if param == "content" and self._is_markup_marker_node(arg):
                lowered.append(self._markup_call_to_go(arg, kind))
            else:
                lowered.append(self._expr_to_go(arg))
        return f"{receiver_go}.{markup_method_go_name(method)}({', '.join(lowered)})"

    def _markup_method_result_go_type(self, node) -> str:
        if not (isinstance(node, Tree) and node.data == "funccall" and node.children):
            return ""
        func = node.children[0]
        if not (isinstance(func, Tree) and func.data == "getattr" and len(func.children) >= 2):
            return ""
        method = self._get_name(func.children[1])
        if method not in MARKUP_METHODS:
            return ""
        kind = self._markup_go_kind(func.children[0])
        if not kind:
            return ""
        return markup_method_return_go(method, kind)

    def _markup_expr_go_type(self, node) -> str:
        """Go type hint of markup expressions (literals, casts, DOM-style
        method results, ``+``)."""
        if not isinstance(node, Tree):
            return ""
        if self._is_markup_marker_node(node):
            return MARKUP_GO_TYPES[self._markup_node_kind(node)]
        kind = self._markup_cast_kind(node)
        if kind:
            return MARKUP_GO_TYPES[kind]
        result = self._markup_method_result_go_type(node)
        if result:
            return result
        if node.data == "arith_expr" and {str(c) for c in node.children[1::2]} == {"+"}:
            kinds = {self._markup_go_kind(c) for c in node.children[0::2] if isinstance(c, Tree)} - {""}
            if len(kinds) == 1:
                return MARKUP_GO_TYPES[kinds.pop()]
        return ""

    def _markup_concat_to_go(self, node) -> str | None:
        """``a + b + ...`` where an operand is markup: ``lamHTMLConcat``."""
        if node.data != "arith_expr" or {str(c) for c in node.children[1::2]} != {"+"}:
            return None
        operands = [c for c in node.children[0::2]]
        kinds = {self._markup_go_kind(c) for c in operands if isinstance(c, Tree)} - {""}
        if len(kinds) != 1:
            return None
        kind = kinds.pop()
        func = "lamHTMLConcat" if kind == "html" else "lamXMLConcat"
        out = self._markup_operand_to_go(operands[0], kind)
        for operand in operands[1:]:
            out = f"{func}({out}, {self._markup_operand_to_go(operand, kind)})"
        return out

    def _markup_operand_to_go(self, operand, kind: str) -> str:
        if self._is_markup_marker_node(operand):
            return self._markup_call_to_go(operand, getattr(operand, "_lam_markup_kind", None) or kind)
        return self._expr_to_go(operand)

    def _markup_augassign_to_go(self, target_go: str, target_node, op: str, value_node) -> str | None:
        """``page += part`` on an html/xml target."""
        if op != "+=":
            return None
        kind = self._markup_go_kind(target_node)
        if not kind:
            return None
        func = "lamHTMLConcat" if kind == "html" else "lamXMLConcat"
        return f"{target_go} = {func}({target_go}, {self._markup_operand_to_go(value_node, kind)})"

    def _markup_comparison_to_go(self, left_node, op: str, right_node) -> str | None:
        if op not in {"==", "!="}:
            return None
        if not (self._markup_go_kind(left_node) or self._markup_go_kind(right_node)):
            return None
        left = self._markup_operand_to_go(left_node, self._markup_go_kind(left_node) or self._markup_go_kind(right_node))
        right = self._markup_operand_to_go(right_node, self._markup_go_kind(right_node) or self._markup_go_kind(left_node))
        test = f"lamMEqual({left}, {right})"
        return test if op == "==" else f"!{test}"
