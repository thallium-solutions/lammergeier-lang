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
    decode_lam_string_literal, markup_method_return, slot_contexts, slot_owners, xml_template_error,
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
        ``xml``, ``list[xml]``, ``Result[xml]``..."""
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
        owners = slot_owners(template, kind == "xml") if kind == "html" else {}
        for index, arg in enumerate(slots):
            context = contexts.get(index, "text")
            target = arg if isinstance(arg, Tree) else node
            if context == INVALID:
                self._error(target, "markup", "`${...}` can only be used in element content and attribute values")
                continue
            if not isinstance(arg, Tree):
                continue
            value_type = self._markup_slot_type(arg, kind)
            if index in owners:
                self._warn_unsafe_markup_slot(target, arg, value_type, *owners[index])
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

    # Attributes whose value is a URL (a ``javascript:`` URL runs code).
    _URL_ATTRIBUTES = frozenset({
        "href", "src", "action", "formaction", "xlink:href", "poster", "cite", "background", "data",
        "srcset", "ping", "manifest", "codebase", "longdesc", "usemap", "icon", "lowsrc", "dynsrc",
    })

    def _warn_unsafe_markup_slot(self, target, arg, value_type, owner: str, starts: bool) -> None:
        """A text value in a place where HTML escaping does not make it safe:
        the start of a URL attribute, an event handler, a ``style``, the
        content of ``<script>`` / ``<style>``. The warning names the
        sanitizer that silences it (``Html.safeUrl`` / ``jsValue`` /
        ``safeCss``)."""
        if isinstance(value_type, NamedType) and value_type.name in {"int", "float", "bool"}:
            return
        if owner in self._URL_ATTRIBUTES:
            if not starts or self._markup_value_trusted(arg, "safeUrl"):
                return
            message = (
                f"dynamic URL in `{owner}`: a `javascript:` URL in it runs code; "
                f"pass the value through `Html.safeUrl(...)` (lamhtml), which blocks unsafe schemes"
            )
        elif owner.startswith("on") and owner != "on":
            if self._markup_value_trusted(arg, "jsValue"):
                return
            message = (
                f"a value in the event handler `{owner}` is HTML-escaped, not JavaScript-escaped: "
                f"insert it with `Html.jsValue(...)` (a JavaScript literal, no quotes around it) or use a `data-*` attribute"
            )
        elif owner == "<script>":
            if self._markup_value_trusted(arg, "jsValue"):
                return
            message = (
                "a value inside <script> is not JavaScript-escaped: insert it with `Html.jsValue(...)` "
                "(a JavaScript literal, no quotes around it) or pass it in a `data-*` attribute"
            )
        elif owner in ("style", "<style>"):
            if self._markup_value_trusted(arg, "safeCss"):
                return
            where = "a `style` attribute" if owner == "style" else "<style>"
            message = (
                f"a dynamic value in {where} can load URLs or scripts (`url(...)`, `expression(...)`): "
                f"pass it through `Html.safeCss(...)` (lamhtml)"
            )
        else:
            return
        self._warning(target, "security", message)

    def _markup_value_trusted(self, expr, kind: str, depth: int = 0) -> bool:
        """A value that needs no sanitizer of ``kind`` (``safeUrl`` /
        ``jsValue`` / ``safeCss``): a string literal (written by the
        developer), that sanitizer's output (also through a function whose
        every return is), for URLs a static safe start (``f"/p/{id}"``), a
        ternary of trusted values, or a name whose every assignment in the
        function (or the module) is trusted."""
        if not isinstance(expr, Tree) or depth > 4:
            return False
        if expr.data == "string":
            return True
        if expr.data == "fstring" and expr.children and "{" not in str(expr.children[0]).replace("{{", ""):
            return True
        if kind == "safeUrl":
            from compiler.semantic import static_safe_url
            if static_safe_url(expr):
                return True
        if expr.data == "funccall":
            return (self._markup_sanitizer_of(expr) or self._sanitized_call_kind(expr)) == kind
        if expr.data in ("test", "ternary", "conditional") or (expr.data.endswith("_test") and len(expr.children) == 3 and expr.data not in ("and_test", "or_test", "not_test")):
            branches = [c for c in expr.children if isinstance(c, Tree)]
            if len(branches) == 3:
                return self._markup_value_trusted(branches[0], kind, depth + 1) and self._markup_value_trusted(branches[2], kind, depth + 1)
        if expr.data == "var" and expr.children:
            name = self._name_text(expr.children[0])
            stack = self.__dict__.get("_funcdef_stack") or []
            values = self._assigned_values(stack[-1], name) if stack else []
            if not values and not any(name in sc.names for sc in self._scopes[1:]):
                tree = self.__dict__.get("_module_tree")
                values = self._assigned_values(tree, name, top_level=True) if tree is not None else []
            return bool(values) and all(self._markup_value_trusted(v, kind, depth + 1) for v in values)
        return False

    @staticmethod
    def _assigned_values(root, name: str, top_level: bool = False) -> list:
        """The right-hand sides of ``name = v`` / ``name: T = v`` /
        ``const name: T = v`` in ``root`` (outside nested functions and
        classes when ``top_level``); empty when ``name`` is also bound
        otherwise (a parameter, a loop variable)."""
        def walk(node):
            for child in node.children:
                if not isinstance(child, Tree):
                    continue
                if top_level and child.data in ("funcdef", "classdef"):
                    continue
                yield child
                yield from walk(child)

        def head_name(node) -> str:
            if isinstance(node, Tree) and node.data == "var" and node.children:
                node = node.children[0]
            if isinstance(node, Tree) and node.data == "name" and node.children:
                return str(node.children[0])
            return ""

        values: list = []
        for sub in walk(root):
            if sub.data in ("assign", "annassign", "const_stmt"):
                trees = [c for c in sub.children if isinstance(c, Tree)]
                if len(trees) >= 2 and head_name(trees[0]) == name:
                    values.append(trees[-1])
            elif not top_level and sub.data in ("for_stmt", "typed_paramvalue", "lambdef", "with_item", "catch_clause"):
                if any(isinstance(t, Token) and str(t) == name for t in sub.scan_values(lambda v: isinstance(v, Token))) and sub.data != "for_stmt":
                    if sub.data == "typed_paramvalue":
                        return []
                if sub.data == "for_stmt" and sub.children and isinstance(sub.children[0], Tree) and any(
                    str(t) == name for t in sub.children[0].scan_values(lambda v: isinstance(v, Token))
                ):
                    return []
        if not top_level:
            params = next((c for c in root.children if isinstance(c, Tree) and c.data in ("parameters", "typedargslist", "params")), None)
            if params is not None and any(str(t) == name for t in params.scan_values(lambda v: isinstance(v, Token))):
                return []
        return values

    def _sanitized_call_kind(self, arg) -> str:
        """The sanitizer whose output a call gives: a function or method
        (also of an imported module) whose every return is
        ``Html.safeUrl(...)`` (``jsValue``, ``safeCss``) or, for URLs, a
        string with a static safe start (``Ctx.url``: ``f"/{lang}{path}"``)."""
        if not (isinstance(arg, Tree) and arg.data == "funccall" and arg.children):
            return ""
        callee = arg.children[0]
        if isinstance(callee, Tree) and callee.data == "var" and callee.children:
            name = self._name_text(callee.children[0])
            if any(name in scope.names for scope in self._scopes[1:]):
                return ""
            sigs = self._func_sigs.get(name) or []
            kinds = {getattr(sig, "sanitized", "") for sig in sigs}
            return kinds.pop() if len(kinds) == 1 else ""
        if isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) >= 2:
            receiver, method = callee.children[0], self._name_text(callee.children[1])
            cls = ""
            if isinstance(receiver, Tree) and receiver.data == "var" and receiver.children:
                head = self._name_text(receiver.children[0])
                if head in self._class_method_shapes and not any(head in sc.names for sc in self._scopes[1:]):
                    cls = head
            if not cls:
                kind = self._expr_simple_type(receiver)
                cls = kind.name if isinstance(kind, NamedType) else ""
            shape = self._class_method_shape(cls, method) if cls else None
            return getattr(shape, "sanitized", "") if shape is not None else ""
        return ""

    @staticmethod
    def _markup_sanitizer_of(arg) -> str:
        """``safeUrl`` for ``Html.safeUrl(x)`` (``jsValue``, ``safeCss``...),
        ``""`` otherwise."""
        if not (isinstance(arg, Tree) and arg.data == "funccall" and arg.children):
            return ""
        callee = arg.children[0]
        if not (isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) >= 2):
            return ""
        receiver = callee.children[0]
        if not (isinstance(receiver, Tree) and receiver.data == "var" and receiver.children):
            return ""
        if str(receiver.children[0].children[0] if isinstance(receiver.children[0], Tree) else receiver.children[0]) != "Html":
            return ""
        method = callee.children[1]
        name = str(method.children[0]) if isinstance(method, Tree) and method.children else str(method)
        return name if name in {"safeUrl", "jsValue", "safeCss"} else ""

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
