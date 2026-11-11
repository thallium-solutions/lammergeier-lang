"""Semantic rules of ``None`` (no optional type, no safe navigation).

Lam has no optional type: ``optional[T]`` / ``Option[T]`` and the safe
navigation operator ``?.`` were removed after 4.11. What can be ``None`` is
fixed by the type:

* references: class instances, interface values, ``html``, ``xml``,
  ``json`` (null), ``any`` and ``Result`` payloads;
* never the value types ``str``, ``int``, ``float``, ``bool``, ``bytes``,
  lists, dicts, sets and tuples (a literal ``None`` there is a compile
  error; a dynamic one raises ``TypeError: None is not a str`` where it is
  converted).

A member of ``None`` raises ``AttributeError: None has no attribute 'x'``
at run time. This mixin reports the removed spellings with their migration
(``optional[Shop]`` -> ``Shop``; ``optional[str]`` -> ``Result[str]``, a
default or ``any``; ``obj?.field`` -> a ``None`` test or ``?`` on a
``Result``).
"""

from __future__ import annotations

from lark import Token, Tree

from compiler.constants import lam_simple_expr_text
from compiler.safe_nav import rewrite_result_safe_nav
from compiler.typesys import GenericType, ListType, DictType, NamedType, Type, UnionType, parse_type, render_type

# The removed optional spellings.
REMOVED_OPTIONAL_NAMES = frozenset({"optional", "Option"})

# Builtin types whose values can be ``None`` (besides classes and
# interfaces).
NONE_HOLDING_BUILTINS = frozenset({"html", "xml", "json", "any", "object", "Error", "error"})

# Value types: never ``None``.
VALUE_TYPE_NAMES = frozenset({
    "str", "string", "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64",
    "float", "float32", "float64", "bool", "bytes", "byte", "rune",
    "list", "dict", "set", "tuple",
})

# Subtrees rewritten when they are visited, with their own scope (see
# ``compiler/safe_nav.py``).
_SAFE_NAV_STOP = frozenset({"suite", "funcdef", "classdef", "lambdef", "go_block", "go_inline"})

SAFE_NAV_REMOVED = (
    "`?.` was removed: test `{recv} != None` first (`None.{member}` throws), or use `?` on a `Result`"
)


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def removed_optional_message(written: str, payload_text: str, holds_none: bool, interface: bool = False) -> str:
    """The migration error of ``optional[payload]`` (``written`` is the
    spelling found, ``optional[Shop]``)."""
    if holds_none:
        if payload_text == "json":
            what = "a `json` value"
        elif payload_text in {"html", "xml", "any", "object"}:
            what = f"an `{payload_text}` value"
        elif interface:
            what = "an interface value"
        elif payload_text.startswith("Result"):
            what = "a `Result` payload"
        else:
            what = "a class reference"
        return (
            f"`{written}` was removed: write `{payload_text}` ({what} can be `None`; "
            f"test `x != None` before using it)"
        )
    return (
        f"`{written}` was removed: {_article(payload_text)} `{payload_text}` cannot be `None`; return a "
        f"`Result[{payload_text}]` (an error when there is no value), use a default value, or `any`"
    )


class NoneSemanticMixin:
    # ── ``optional[T]`` / ``Option[T]`` ─────────────────────────────────

    def _module_type_param_names(self, tree: Tree) -> set[str]:
        names: set[str] = set()
        for sub in tree.iter_subtrees():
            if sub.data == "type_param" and sub.children:
                names.add(self._name_text(sub.children[0]))
        return names

    def _type_holds_none(self, type_: Type, type_params: set[str]) -> bool:
        """A value of ``type_`` can be ``None`` (a class, an interface,
        ``html`` / ``xml`` / ``json`` / ``any``, a ``Result``)."""
        if isinstance(type_, NamedType):
            name = type_.name
            if name in NONE_HOLDING_BUILTINS or name == "Result":
                return True
            if name in VALUE_TYPE_NAMES or name in type_params or name == "None":
                return False
            return True  # a class or an interface (also an unknown name)
        if isinstance(type_, GenericType):
            if type_.base in REMOVED_OPTIONAL_NAMES:
                return bool(type_.args) and self._type_holds_none(type_.args[0], type_params)
            return type_.base not in VALUE_TYPE_NAMES and type_.base not in {"chan", "generator", "iterator"}
        if isinstance(type_, UnionType):
            return False
        return False  # lists, dicts, functions

    def _is_value_type(self, type_: Type | None) -> bool:
        """A value of ``type_`` is never ``None``: ``str``, numbers,
        ``bool``, ``bytes``, lists, dicts, sets and tuples."""
        if isinstance(type_, NamedType):
            return type_.name in VALUE_TYPE_NAMES and type_.name not in {"chan", "generator", "iterator"}
        if isinstance(type_, (ListType, DictType)):
            return True
        if isinstance(type_, GenericType):
            return type_.base in {"set", "tuple", "list", "dict"}
        return False

    def _check_removed_optionals(self, tree: Tree) -> None:
        """``optional[T]`` / ``Option[T]`` anywhere (annotations, generic
        arguments, casts, ``Result[optional[T]]``): the migration error."""
        type_params = self._module_type_param_names(tree)
        interfaces = set(getattr(self, "_interface_methods", {}) or ())
        for sub in tree.iter_subtrees():
            if sub.data == "interfacedef" and sub.children and isinstance(sub.children[0], Tree):
                interfaces.add(self._name_text(sub.children[0]))
        # Innermost first: each reported node is then replaced by what it
        # migrates to (``optional[Shop]`` by ``Shop``, ``optional[str]`` by
        # ``any``), so the rest of the check sees no optional.
        reports: list = []
        for sub in reversed(list(tree.iter_subtrees_topdown())):
            if sub.data == "type_generic" and sub.children:
                base = self._type_node_base_name(sub)
                if base not in REMOVED_OPTIONAL_NAMES:
                    continue
                payload_nodes = [c for c in sub.children[1:] if isinstance(c, Tree) and c.data == "type_expr"]
                try:
                    args = [parse_type(c) for c in payload_nodes]
                except Exception:
                    continue
                payload = args[0] if args else NamedType("any")
                written = f"{base}[{', '.join(render_type(a) for a in args)}]"
                holds_none = self._type_holds_none(payload, type_params)
                reports.append((self._loc(sub), removed_optional_message(
                    written, render_type(payload), holds_none,
                    isinstance(payload, NamedType) and payload.name in interfaces,
                )))
                self._replace_removed_optional(sub, holds_none)
            elif sub.data == "type_name" and sub.children and self._type_node_base_name(sub) in REMOVED_OPTIONAL_NAMES:
                base = self._type_node_base_name(sub)
                reports.append((self._loc(sub), (
                    f"`{base}` was removed: a class reference (and `html`, `xml`, `json`, `any`) can be `None`, "
                    "a value type cannot (use a `Result`, a default value, or `any`)"
                )))
                self._replace_removed_optional(sub, False)
        if reports:
            from compiler.semantic import SemanticError
            for (line, col), message in sorted(reports, key=lambda r: r[0]):
                self.errors.append(SemanticError(line, col, message, "type"))

    @staticmethod
    def _replace_removed_optional(node: Tree, holds_none: bool) -> None:
        """``node`` (``optional[X]``) becomes ``X`` (a reference payload) or
        ``any`` (a value payload: no ``None`` would fit it)."""
        payload = next(
            (c for c in node.children[1:] if isinstance(c, Tree) and c.data == "type_expr" and c.children),
            None,
        ) if node.data == "type_generic" else None
        if holds_none and payload is not None and isinstance(payload.children[0], Tree):
            inner = payload.children[0]
            node.data, node.children = inner.data, list(inner.children)
            return
        node.data = "type_name"
        node.children = [Tree("dotted_name", [Tree("name", [Token("NAME", "any")])])]

    @staticmethod
    def _type_node_base_name(node: Tree) -> str:
        head = node.children[0] if node.children else None
        if isinstance(head, Token):
            return str(head)
        if isinstance(head, Tree):
            parts = []
            for child in head.children:
                if isinstance(child, Token):
                    parts.append(str(child))
                elif isinstance(child, Tree) and child.children:
                    parts.append(str(child.children[0]))
            return ".".join(parts)
        return ""

    # ── ``obj?.member`` ────────────────────────────────────────────────

    def _rewrite_safe_nav(self, node) -> None:
        """``f()?.m()`` on a ``Result`` is propagation then member access
        (``rewrite_result_safe_nav``); any other ``?.`` is the removed safe
        navigation: an error, then read as a plain member access so the
        rest of the statement is checked without follow-up noise."""
        if not isinstance(node, Tree):
            return
        rewrite_result_safe_nav(node, self._is_result_receiver)
        order: list = []
        stack = [node]
        while stack:
            cur = stack.pop()
            if not isinstance(cur, Tree):
                continue
            order.append(cur)
            for child in cur.children:
                if isinstance(child, Tree) and child.data not in _SAFE_NAV_STOP:
                    stack.append(child)
        # Receivers first, so ``u?.friend?.name`` names ``u.friend``.
        for cur in reversed(order):
            if cur.data == "getattr_safe" and len(cur.children) >= 2:
                recv = lam_simple_expr_text(cur.children[0])
                if not recv or len(recv) > 40:
                    recv = "obj"
                member = self._name_text(cur.children[1]) or "field"
                self._error(cur, "syntax", SAFE_NAV_REMOVED.format(recv=recv, member=member))
                cur.data = "getattr"
