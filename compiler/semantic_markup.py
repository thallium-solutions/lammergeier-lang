"""Semantic checks of the native ``html`` / ``xml`` types.

Markup literals reach the checker as ``__markup__(template, slots...)``
calls (see ``compiler/markup.py``). This mixin gives them their kind,
checks where their slots land and what they hold, and types the casts,
operators and DOM-style methods of markup values.
"""

from __future__ import annotations

import re

from lark import Token, Tree

from compiler.markup import (
    ATTRIBUTE, INVALID, MARKER, MARKUP_METHODS, RAW,
    decode_lam_string_literal, markup_method_return, slot_contexts, xml_template_error,
)
from compiler.typesys import DictType, GenericType, ListType, NamedType, Type, UnionType, parse_type, render_type

MARKUP_KINDS = ("html", "xml")
_KIND_RE = re.compile(r"\b(html|xml)\b")


class MarkupSemanticMixin:
    # ── Literals ────────────────────────────────────────────────────────

    def _is_markup_marker(self, node) -> bool:
        if not isinstance(node, Tree) or node.data != "funccall" or not node.children:
            return False
        callee = node.children[0]
        return (
            isinstance(callee, Tree) and callee.data == "var" and bool(callee.children)
            and self._name_text(callee.children[0]) == MARKER
        )

    @staticmethod
    def _markup_args(node: Tree) -> list:
        if len(node.children) < 2 or not isinstance(node.children[1], Tree):
            return []
        args = node.children[1]
        if args.data != "arguments":
            return [args]
        return [child for child in args.children if isinstance(child, Tree) or isinstance(child, Token)]

    def _markup_template(self, node: Tree) -> str:
        args = self._markup_args(node)
        if not args:
            return ""
        token = next(iter(args[0].scan_values(lambda v: isinstance(v, Token)))) if isinstance(args[0], Tree) else args[0]
        return decode_lam_string_literal(str(token))

    def _markup_kind(self, node: Tree) -> str:
        kind = getattr(node, "_lam_markup_kind", None)
        if kind:
            return kind
        kind = self._markup_context_kind(node) or "html"
        node._lam_markup_kind = kind
        return kind

    @staticmethod
    def _markup_kind_of_type(type_) -> str | None:
        """``html``/``xml`` when a (possibly wrapped) type is markup:
        ``xml``, ``list[xml]``, ``optional[xml]``, ``Result[xml]``..."""
        if type_ is None:
            return None
        text = type_ if isinstance(type_, str) else render_type(type_)
        found = _KIND_RE.findall(text)
        return found[0] if found and len(set(found)) == 1 else None

    def _markup_context_kind(self, node: Tree) -> str | None:
        """The kind a literal takes from where it is written: a typed
        variable, a return, a parameter, the other operand of ``+``, the
        receiver of a DOM method, an enclosing literal."""
        child = node
        parent = self._parents.get(id(node))
        while parent is not None:
            d = parent.data
            if d == "test" and len(parent.children) == 3 and child is not parent.children[1]:
                pass
            elif d in {"list", "set", "tuple", "testlist_tuple"}:
                pass
            elif d in {"list_comprehension", "comprehension"} and parent.children and child is parent.children[0]:
                pass
            elif d == "arith_expr":
                for other in parent.children:
                    if isinstance(other, Tree) and other is not child:
                        kind = self._markup_operand_kind(other)
                        if kind:
                            return kind
            elif d == "arguments" or d == "argvalue":
                if d == "argvalue":
                    child, parent = parent, self._parents.get(id(parent))
                    if parent is None:
                        return None
                    if parent.data != "arguments":
                        continue
                call = self._parents.get(id(parent))
                if call is None or call.data != "funccall":
                    return None
                if self._is_markup_marker(call):
                    return self._markup_kind(call)
                kind = self._markup_call_arg_kind(call, parent, child)
                if kind != "pass":
                    return kind
                child, parent = call, self._parents.get(id(call))
                continue
            elif d == "annassign":
                for part in parent.children:
                    if isinstance(part, Tree) and part.data in {"type_expr", "type_name", "type_generic", "type_union", "type_optional"} and part is not child:
                        return self._markup_kind_of_type(self._type_text_of(part))
                return None
            elif d == "typed_paramvalue":
                # ``func f(doc: xml = <>...</>)``
                param = parent.children[0] if parent.children else None
                if isinstance(param, Tree) and param is not child:
                    for part in param.children:
                        if isinstance(part, Tree) and part.data == "type_expr":
                            return self._markup_kind_of_type(self._type_text_of(part))
                return None
            elif d in {"assign", "augassign"}:
                target = parent.children[0] if parent.children else None
                if target is child:
                    return None
                return self._markup_kind_of_type(self._expr_simple_type(target)) if isinstance(target, Tree) else None
            elif d == "return_stmt":
                funcdef = self._markup_enclosing_funcdef(parent)
                return self._markup_kind_of_type(self._funcdef_return_type_text(funcdef)) if funcdef is not None else None
            elif d == "lambdef" or d == "lambda":
                return None
            else:
                return None
            child, parent = parent, self._parents.get(id(parent))
        return None

    def _type_text_of(self, node) -> str:
        return " ".join(str(v) for v in node.scan_values(lambda v: isinstance(v, Token)))

    def _markup_enclosing_funcdef(self, node):
        cur = self._parents.get(id(node))
        while cur is not None:
            if cur.data == "funcdef":
                return cur
            if cur.data in {"lambdef", "lambda"}:
                return None
            cur = self._parents.get(id(cur))
        return None

    def _markup_operand_kind(self, other) -> str | None:
        if self._is_markup_marker(other):
            explicit = getattr(other, "_lam_markup_kind", None)
            return explicit
        return self._markup_kind_of_type(self._expr_simple_type(other))

    def _markup_call_arg_kind(self, call: Tree, args: Tree, arg) -> str | None:
        """Kind expected by the parameter ``arg`` binds to; ``"pass"`` to
        look further out (``Result.Ok(...)``)."""
        callee = call.children[0]
        if isinstance(callee, Tree) and callee.data == "var" and callee.children:
            name = self._name_text(callee.children[0])
            if name in MARKUP_KINDS:
                return name
        if isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) >= 2:
            method = self._name_text(callee.children[1])
            receiver_kind = self._markup_kind_of_receiver(callee.children[0])
            if receiver_kind and method in MARKUP_METHODS:
                return receiver_kind
            receiver = callee.children[0]
            if (
                isinstance(receiver, Tree) and receiver.data == "var" and receiver.children
                and self._name_text(receiver.children[0]) in {"Result", "LAMMERGEIER"}
                and method in {"Ok", "Err"}
            ):
                return "pass"
        try:
            _target, sigs = self._call_target_sigs(call)
        except Exception:
            return None
        kinds: set[str] = set()
        for sig in sigs or []:
            for _param, expected_name, arg_node in self._call_arg_bindings(call, sig):
                if arg_node is arg:
                    kind = self._markup_kind_of_type(expected_name)
                    if kind:
                        kinds.add(kind)
        if len(kinds) == 1:
            return kinds.pop()
        return None

    def _markup_kind_of_receiver(self, receiver) -> str | None:
        if self._is_markup_marker(receiver):
            return self._markup_kind(receiver)
        type_ = self._expr_simple_type(receiver)
        if isinstance(type_, NamedType) and type_.name in MARKUP_KINDS:
            return type_.name
        return None

    def _check_markup_literal(self, node: Tree) -> None:
        kind = self._markup_kind(node)
        template = self._markup_template(node)
        slots = self._markup_args(node)[1:]
        contexts = slot_contexts(template, kind == "xml")
        for index, arg in enumerate(slots):
            context = contexts.get(index, "text")
            target = arg if isinstance(arg, Tree) else node
            if context == INVALID:
                self._error(target, "markup", "`${...}` can only be used in element content and attribute values")
                continue
            if not isinstance(arg, Tree):
                continue
            value_type = self._markup_slot_type(arg, kind)
            if context in {ATTRIBUTE, RAW}:
                inner = self._markup_kind_of_type(value_type) if value_type is not None and self._markup_is_markup_type(value_type) else None
                if inner:
                    where = "an attribute value" if context == ATTRIBUTE else "<script>/<style> content"
                    self._error(target, "markup", f"{where} cannot be `{inner}`; insert text (str, numbers, bool or None)")
                continue
            issue = self._markup_value_issue(value_type, kind)
            if issue:
                self._error(target, "markup", issue)
        if kind == "xml":
            problem = xml_template_error(template)
            if problem:
                self._error(node, "markup", f"malformed XML literal: {problem}")

    def _markup_slot_type(self, arg, kind: str):
        if self._is_markup_marker(arg):
            return NamedType(self._markup_kind(arg))
        return self._expr_simple_type(arg)

    @staticmethod
    def _markup_is_markup_type(type_) -> bool:
        if isinstance(type_, NamedType):
            return type_.name in MARKUP_KINDS
        if isinstance(type_, ListType):
            return MarkupSemanticMixin._markup_is_markup_type(type_.item)
        if isinstance(type_, GenericType) and type_.base in {"optional", "Option"} and type_.args:
            return MarkupSemanticMixin._markup_is_markup_type(type_.args[0])
        return False

    def _markup_value_issue(self, type_, kind: str) -> str | None:
        """Why a value of ``type_`` cannot be inserted into ``kind`` markup
        content (or converted with ``kind(value)``), or None."""
        if type_ is None:
            return None
        if isinstance(type_, NamedType):
            name = type_.name
            if name in MARKUP_KINDS:
                if name != kind:
                    return f"cannot insert `{name}` into an {kind} literal"
                return None
            if name in {
                "str", "string", "int", "int8", "int16", "int32", "int64", "uint", "uint8", "uint16",
                "uint32", "uint64", "float", "float32", "float64", "byte", "rune", "bool", "None",
                "json", "any", "object", "bytes",
            }:
                return None
            shapes = self._class_method_shapes.get(name)
            if shapes is not None:
                dunder = f"__{kind}__"
                if dunder in shapes:
                    return None
                return f"value of type `{name}` cannot be inserted into {kind}; implement `func {dunder}(self) -> {kind}`"
            return None
        if isinstance(type_, ListType):
            return self._markup_value_issue(type_.item, kind)
        if isinstance(type_, GenericType) and type_.base in {"optional", "Option"} and type_.args:
            return self._markup_value_issue(type_.args[0], kind)
        if isinstance(type_, UnionType):
            for option in type_.options:
                issue = self._markup_value_issue(option, kind)
                if issue:
                    return issue
        if isinstance(type_, DictType):
            return f"a `{render_type(type_)}` cannot be inserted into {kind}"
        return None

    # ── Casts, protocol, methods ───────────────────────────────────────

    def _check_markup_cast(self, name: str, call: Tree, pos_count: int, has_starargs: bool) -> bool:
        """``html(x)`` / ``xml(x)``; True when handled."""
        if name not in MARKUP_KINDS:
            return False
        if has_starargs or pos_count != 1:
            self._error(call, "call", f"cast `{name}` expects exactly 1 argument, got {pos_count}")
            return True
        args = self._call_positional_args(call)
        if args and isinstance(args[0], Tree):
            issue = self._markup_value_issue(self._markup_slot_type(args[0], name), name)
            if issue:
                self._error(args[0], "markup", issue)
        return True

    def _check_markup_method_signature(self, node: Tree) -> None:
        name = self._funcdef_name(node)
        if not self._current_class_stack or name not in {"__html__", "__xml__"}:
            return
        kind = name.strip("_")
        class_name = self._current_class_stack[-1]
        if self._funcdef_is_static(node):
            self._error(node, "markup", f"`{class_name}.{name}` must be an instance method")
            return
        params = self._param_names(self._funcdef_params(node))
        if params != ["self"]:
            self._error(
                node, "markup",
                f"`{class_name}.{name}` must declare only `self`, got {len(params)} parameter{'s' if len(params) != 1 else ''}",
            )
        return_type = self._funcdef_return_type_text(node)
        if return_type != kind:
            self._error(node, "markup", f"`{class_name}.{name}` must return `{kind}`, got `{return_type or 'None'}`")

    def _check_markup_receiver_method(self, call: Tree, callee: Tree, receiver_type, method: str) -> bool:
        """DOM-style method on an html/xml receiver; True when handled."""
        if not (isinstance(receiver_type, NamedType) and receiver_type.name in MARKUP_KINDS):
            return False
        kind = receiver_type.name
        shape = MARKUP_METHODS.get(method)
        if shape is None:
            self._error(callee, "member", f"`{kind}` has no method `{method}`")
            return True
        params, _ret = shape
        args = self._call_positional_args(call)
        if len(args) != len(params):
            plural = "argument" if len(params) == 1 else "arguments"
            self._error(callee, "call", f"`{kind}.{method}()` takes {len(params)} {plural}, got {len(args)}")
            return True
        for position, (param, arg) in enumerate(zip(params, args), start=1):
            if not isinstance(arg, Tree):
                continue
            actual = self._markup_slot_type(arg, kind)
            if param == "str":
                if actual is not None and not (self._is_any_type(actual) or self._is_named_type(actual, "str")):
                    self._error(arg, "type", f"argument {position} of `{kind}.{method}()` must be `str`, got `{render_type(actual)}`")
            else:
                issue = self._markup_value_issue(actual, kind)
                if issue:
                    self._error(arg, "markup", issue)
        return True

    def _markup_method_result_type(self, node: Tree):
        """Type of ``value.method(...)`` on an html/xml receiver."""
        callee = node.children[0] if node.children else None
        if not (isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) >= 2):
            return None
        method = self._name_text(callee.children[1])
        if method not in MARKUP_METHODS:
            return None
        kind = self._markup_kind_of_receiver(callee.children[0])
        if not kind:
            return None
        return parse_type(markup_method_return(method, kind))

    # ── Operators ──────────────────────────────────────────────────────

    def _markup_binary_result(self, op: str, left, right):
        """``html + html|str``, ``str + html`` (and xml): the markup type;
        None when the operands are not markup."""
        if op != "+":
            return None
        names = [t.name if isinstance(t, NamedType) else "" for t in (left, right)]
        kinds = [n for n in names if n in MARKUP_KINDS]
        if not kinds:
            return None
        if len(set(kinds)) == 1 and all(n in MARKUP_KINDS or n in {"str", "string"} for n in names):
            return NamedType(kinds[0])
        return False
