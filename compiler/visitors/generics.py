#!/usr/bin/env python3
"""Type-argument inference for calls to generic callables.

A call to a generic function or method (``mapList(xs, lambda x: x * 2)``,
``it.map(lambda x: x * x)`` with ``it: Iter[int]``) needs its type
parameters resolved at the call site for two reasons:

* a lambda argument lowers to a Go function literal, which must spell its
  parameter and result types (``func(x int) int``): Go infers type
  arguments *from* such a literal but never types the literal itself;
* the call's result type feeds the next link of a chain
  (``Iter.range_(0, 9).filter(...).map(...).toList()``) and the
  contextual typing of later lambdas.

The inference works on the Go spelling of the declared parameter types
(``func(T) U``, ``[]T``, ``*Iter[T]``), like Go's own: the receiver's type
arguments bind the class parameters, then typed arguments, then untyped
literals, and last lambdas and function references, whose parameters are
typed from what is already known and whose result binds the rest
(``func(T) U`` with ``T = int`` and the body ``x * x`` gives ``U = int``).

Methods with their own type parameters (``func map[U](self, ...)``) lower
to package-level generic functions ``Class_Method[T, U](s, ...)`` because
Go methods cannot declare type parameters; ``_generic_method_call_to_go``
rewrites their call sites.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from lark import Token, Tree


_IDENT_RE = re.compile(r"[A-Za-z_][\w.]*")

# Builtins usable as function values where a ``func(...)`` is expected
# (``xs.map(len)``): Go builtins and conversions are not values, so they
# are wrapped in a function literal. Name -> Go result type (``""`` keeps
# the parameter type).
BUILTIN_FUNC_REFS = {
    "len": "int",
    "str": "string",
    "int": "int",
    "float": "float64",
    "bool": "bool",
    "abs": "",
}


def parse_go_type(text: str):
    """Parse a Go type spelling into a small tree:

    ``("name", "int")``, ``("ptr", t)``, ``("slice", t)``, ``("map", k, v)``,
    ``("chan", t)``, ``("func", [params], ret_or_None)``,
    ``("generic", base, [args])``; ``None`` when the text is not modelled.
    """
    text = (text or "").strip()
    try:
        node, rest = _parse(text, 0)
    except (IndexError, ValueError):
        return None
    if node is None or text[rest:].strip():
        return None
    return node


def _skip(text: str, i: int) -> int:
    while i < len(text) and text[i] == " ":
        i += 1
    return i


def _parse(text: str, i: int):
    i = _skip(text, i)
    if text.startswith("interface{}", i):
        return ("name", "interface{}"), i + len("interface{}")
    if text.startswith("struct{}", i):
        return ("name", "struct{}"), i + len("struct{}")
    if text.startswith("*", i):
        inner, j = _parse(text, i + 1)
        return ("ptr", inner), j
    if text.startswith("[]", i):
        inner, j = _parse(text, i + 2)
        return ("slice", inner), j
    if text.startswith("...", i):
        inner, j = _parse(text, i + 3)
        return ("slice", inner), j
    if text.startswith("map[", i):
        key, j = _parse(text, i + 4)
        j = _skip(text, j)
        if text[j] != "]":
            raise ValueError(text)
        value, k = _parse(text, j + 1)
        return ("map", key, value), k
    if text.startswith("chan ", i):
        inner, j = _parse(text, i + 5)
        return ("chan", inner), j
    if text.startswith("func(", i):
        j = i + 5
        params = []
        j = _skip(text, j)
        if text[j] == ")":
            j += 1
        else:
            while True:
                param, j = _parse(text, j)
                params.append(param)
                j = _skip(text, j)
                if text[j] == ",":
                    j += 1
                    continue
                if text[j] == ")":
                    j += 1
                    break
                raise ValueError(text)
        k = _skip(text, j)
        if k < len(text) and text[k] not in ",)]" and not text.startswith("{", k):
            if text[k] == "(":
                raise ValueError(text)  # multiple results: not modelled
            ret, k = _parse(text, k)
            return ("func", params, ret), k
        return ("func", params, None), j
    match = _IDENT_RE.match(text, i)
    if not match:
        raise ValueError(text)
    name = match.group(0)
    j = match.end()
    if j < len(text) and text[j] == "[":
        args = []
        j += 1
        while True:
            arg, j = _parse(text, j)
            args.append(arg)
            j = _skip(text, j)
            if text[j] == ",":
                j += 1
                continue
            if text[j] == "]":
                j += 1
                break
            raise ValueError(text)
        return ("generic", name, args), j
    return ("name", name), j


def render_go_type(node) -> str:
    kind = node[0]
    if kind == "name":
        return node[1]
    if kind == "ptr":
        return "*" + render_go_type(node[1])
    if kind == "slice":
        return "[]" + render_go_type(node[1])
    if kind == "map":
        return f"map[{render_go_type(node[1])}]{render_go_type(node[2])}"
    if kind == "chan":
        return "chan " + render_go_type(node[1])
    if kind == "func":
        params = ", ".join(render_go_type(p) for p in node[1])
        return f"func({params}) {render_go_type(node[2])}" if node[2] is not None else f"func({params})"
    if kind == "generic":
        return f"{node[1]}[{', '.join(render_go_type(a) for a in node[2])}]"
    return ""


def unify_go_types(pattern: str, actual: str, names, out: Dict[str, str], bind_any: bool = True) -> None:
    """Bind the type parameters ``names`` appearing in ``pattern`` from the
    matching parts of ``actual`` (first binding wins). ``bind_any=False``
    leaves a parameter unbound when the actual part is ``interface{}``
    (an unknown lambda result, not a deliberate ``any``)."""
    p = parse_go_type(pattern)
    a = parse_go_type(actual)
    if p is None or a is None:
        return
    _unify(p, a, set(names), out, bind_any)


def _unify(p, a, names, out, bind_any) -> None:
    if p[0] == "name" and p[1] in names:
        rendered = render_go_type(a)
        if not rendered or (rendered == "interface{}" and not bind_any):
            return
        out.setdefault(p[1], rendered)
        return
    if p[0] != a[0]:
        return
    if p[0] in {"ptr", "slice", "chan"}:
        _unify(p[1], a[1], names, out, bind_any)
    elif p[0] == "map":
        _unify(p[1], a[1], names, out, bind_any)
        _unify(p[2], a[2], names, out, bind_any)
    elif p[0] == "generic":
        if p[1] == a[1] and len(p[2]) == len(a[2]):
            for pa, aa in zip(p[2], a[2]):
                _unify(pa, aa, names, out, bind_any)
    elif p[0] == "func":
        if len(p[1]) == len(a[1]):
            for pa, aa in zip(p[1], a[1]):
                _unify(pa, aa, names, out, bind_any)
        if p[2] is not None and a[2] is not None:
            _unify(p[2], a[2], names, out, bind_any)


def render_lam_type(type_) -> str:
    from compiler.typesys import render_type
    return render_type(type_)


def _unify_lam_types(pattern, actual, names, out: Dict[str, object]) -> None:
    """Bind the type parameters ``names`` of the Lam type ``pattern``
    against ``actual`` (``list[tuple[K, V]]`` / ``list[tuple[str, int]]``,
    ``func(Conn) -> Result[T]`` / ``func(Conn) -> Result[int]``)."""
    from compiler.typesys import DictType, FuncType, GenericType, ListType, NamedType, UnionType
    if isinstance(pattern, NamedType) and pattern.name in names:
        if not (isinstance(actual, NamedType) and actual.name in {"any", "object"}):
            out.setdefault(pattern.name, actual)
        return
    if isinstance(pattern, FuncType) and isinstance(actual, FuncType):
        if len(pattern.params) == len(actual.params):
            for p, a in zip(pattern.params, actual.params):
                _unify_lam_types(p, a, names, out)
        _unify_lam_types(pattern.ret, actual.ret, names, out)
    elif isinstance(pattern, UnionType):
        # ``T | None`` against ``int | None`` (or ``int``).
        def _some(type_):
            options = type_.options if isinstance(type_, UnionType) else (type_,)
            return [o for o in options if not (isinstance(o, NamedType) and o.name == "None")]
        p_opts, a_opts = _some(pattern), _some(actual)
        if len(p_opts) == len(a_opts):
            for p, a in zip(p_opts, a_opts):
                _unify_lam_types(p, a, names, out)
    elif isinstance(pattern, ListType) and isinstance(actual, ListType):
        _unify_lam_types(pattern.item, actual.item, names, out)
    elif isinstance(pattern, DictType) and isinstance(actual, DictType):
        _unify_lam_types(pattern.key, actual.key, names, out)
        _unify_lam_types(pattern.value, actual.value, names, out)
    elif (
        isinstance(pattern, GenericType) and isinstance(actual, GenericType)
        and pattern.base == actual.base and len(pattern.args) == len(actual.args)
    ):
        for p, a in zip(pattern.args, actual.args):
            _unify_lam_types(p, a, names, out)


def substitute_go_type(text: str, subst: Dict[str, str]) -> str:
    """Replace whole-word type parameter names in a Go type spelling."""
    if not subst or not text:
        return text
    pattern = re.compile(r"(?<![\w.])(" + "|".join(re.escape(k) for k in sorted(subst, key=len, reverse=True)) + r")(?![\w])")
    return pattern.sub(lambda m: subst[m.group(1)], text)


def mentions_names(text: str, names) -> bool:
    if not names or not text:
        return False
    return any(re.search(r"(?<![\w.])" + re.escape(n) + r"(?![\w])", text) for n in names)


class GenericCallMixin:
    """Call-site type-argument inference (see module docstring)."""

    # ─── Arguments of a call ─────────────────────────────────────

    def _generic_call_arg_pairs(self, func_key: str, args_node) -> List[Tuple[str, object]]:
        """``[(declared Go param type, argument node)]`` for the positional
        and keyword arguments of a call to ``func_key``."""
        param_types = self._func_param_go_types.get(func_key, [])
        param_names = self._func_param_names.get(func_key, [])
        pairs: List[Tuple[str, object]] = []
        if not isinstance(args_node, Tree) or args_node.data != "arguments":
            return pairs
        index = 0
        for child in args_node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "argvalue" and len(child.children) == 2:
                name_node = child.children[0]
                name = self._get_name(name_node.children[0]) if (
                    isinstance(name_node, Tree) and name_node.data == "var" and name_node.children
                ) else ""
                if name and name in param_names:
                    pos = param_names.index(name)
                    if pos < len(param_types):
                        pairs.append((param_types[pos], child.children[1]))
                continue
            if child.data in {"starargs", "stararg", "kwargs"}:
                continue
            node = child.children[0] if child.data == "argvalue" and child.children else child
            pairs.append((self._param_go_type_at(param_types, index), node))
            index += 1
        return pairs

    @staticmethod
    def _is_untyped_literal(node) -> bool:
        """A literal whose Go type is only a default (``0``, ``"x"``,
        ``1.5``, ``true``): like Go, typed arguments bind first."""
        if not isinstance(node, Tree):
            return False
        if node.data in {"number", "string", "const_true", "const_false", "const_none"}:
            return True
        if node.data == "factor" and node.children and isinstance(node.children[-1], Tree):
            return node.children[-1].data == "number"
        return False

    def _func_ref_go_type(self, node, expected_params: Optional[List[str]] = None) -> str:
        """Go ``func(...)`` type of a function passed by name: a user
        function (``map(shout)``) or a wrapped builtin (``map(len)``)."""
        if not (isinstance(node, Tree) and node.data == "var" and node.children):
            return ""
        name = self._get_name(node.children[0])
        if name in self.declared_vars or name in self._var_go_types:
            return self._var_go_types.get(name, "") if self._var_go_types.get(name, "").startswith("func(") else ""
        if name in BUILTIN_FUNC_REFS and name not in self._user_functions and expected_params and len(expected_params) == 1:
            result = BUILTIN_FUNC_REFS[name] or expected_params[0]
            return f"func({expected_params[0]}) {result}"
        if name not in self._user_functions or len(self._overload_variants.get(name, ())) > 1:
            return ""
        if name in self._func_generic_params:
            return ""
        params = self._func_param_go_types.get(name)
        if params is None:
            params = []
        if len(params) != self._func_param_counts.get(name, len(params)):
            return ""
        ret = self._func_return_go_types.get(name, "")
        if ret.startswith("("):
            return ""
        joined = ", ".join(params)
        return f"func({joined}) {ret}" if ret else f"func({joined})"

    def _generic_call_subst(self, func_key: str, args_node, generic_names,
                            base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Bind the type parameters ``generic_names`` of a call to
        ``func_key``: ``base`` (the receiver's type arguments) first, then
        typed arguments, untyped literals, and lambdas / function
        references (typed from what is already bound)."""
        subst: Dict[str, str] = dict(base or {})
        names = [n for n in generic_names if n not in subst]
        if not names:
            return subst
        pairs = self._generic_call_arg_pairs(func_key, args_node)
        typed, literals, callables, empties = [], [], [], []
        for declared, node in pairs:
            if not declared or not mentions_names(declared, names):
                continue
            if isinstance(node, Tree) and node.data in {"list", "dict"} and not node.children:
                # ``Box([])``: an empty literal gives no element type; the
                # expected type of the call binds ``T`` first.
                empties.append((declared, node))
            elif isinstance(node, Tree) and node.data == "lambdef":
                callables.append((declared, node))
            elif self._func_ref_go_type(node, ["interface{}"]) and declared.startswith("func("):
                callables.append((declared, node))
            elif self._is_untyped_literal(node):
                literals.append((declared, node))
            else:
                typed.append((declared, node))
        for declared, node in typed:
            # ``pick([orders[0]], 0)``: a list literal is typed by its
            # elements' expression types too (indexes, fields, calls).
            literal = self._list_literal_index_type(node) if isinstance(node, Tree) and node.data == "list" else ""
            actual = literal or self._expr_go_type_hint(node) or self._infer_type_from_value(node)
            if actual:
                unify_go_types(declared, actual, names, subst)
        for declared, node in literals:
            actual = self._infer_type_from_value(node)
            if actual and actual != "interface{}":
                unify_go_types(declared, actual, names, subst)
        if empties and any(n not in subst for n in names) and self._expected_call_lam_type() is not None:
            # ``c: Box[int] = Box([])``: the expected type binds ``T``.
            self._lam_generic_call_subst(func_key, args_node, [n for n in names if n not in subst], subst)
        for declared, node in empties:
            # Otherwise the empty literal's ``[]interface{}`` makes it ``any``.
            if any(n not in subst for n in names):
                actual = self._expr_go_type_hint(node) or self._infer_type_from_value(node)
                if actual:
                    unify_go_types(declared, actual, [n for n in names if n not in subst], subst)
        for declared, node in callables:
            pattern = substitute_go_type(declared, subst)
            parsed = parse_go_type(pattern)
            if parsed is None or parsed[0] != "func":
                continue
            param_types = [render_go_type(p) for p in parsed[1]]
            if any(mentions_names(t, names) for t in param_types):
                continue
            if isinstance(node, Tree) and node.data == "lambdef":
                result = self._lambda_result_go_type(node, param_types)
                if result and result != "interface{}":
                    actual = f"func({', '.join(param_types)}) {result}"
                    unify_go_types(pattern, actual, names, subst, bind_any=False)
            else:
                actual = self._func_ref_go_type(node, param_types)
                if actual:
                    unify_go_types(pattern, actual, names, subst)
        if any(n not in subst for n in names):
            self._lam_generic_call_subst(func_key, args_node, [n for n in names if n not in subst], subst)
        return subst

    # ─── Type parameters Go cannot see ───────────────────────────
    #
    # A tuple lowers to ``[]interface{}``, so ``K`` / ``V`` of
    # ``pairs: list[tuple[K, V]]`` are absent from the Go parameter types:
    # the Go-level unification above cannot bind them, and Go itself
    # reports ``cannot infer K``. They are bound from the Lam types of the
    # parameters and arguments, and the call spells them explicitly
    # (``_uninferable_type_args``).

    @staticmethod
    def _parsed_lam_type(node):
        from compiler.typesys import parse_type
        try:
            return parse_type(node)
        except Exception:
            return None

    def _arg_lam_type(self, node):
        """The Lam type of a call argument: an annotated / inferred name or
        a list / tuple literal of typed elements; ``None`` otherwise."""
        from compiler.typesys import GenericType, ListType, parse_type
        if isinstance(node, Tree) and node.data == "argvalue" and len(node.children) == 1:
            node = node.children[0]
        if not isinstance(node, Tree):
            return None
        if node.data == "var" and node.children:
            name = self._get_name(node.children[0])
            nested_key = self._nested_func_keys.get(name)
            if isinstance(nested_key, str):
                # A nested ``func`` passed by name (in scope: its key ends
                # with the enclosing body).
                return self._func_ref_lam_type(nested_key)
            local = getattr(self, "_var_lam_types", {}).get(name)
            if local is not None or name in self.declared_vars or name in self._var_go_types:
                return local
            return self._func_ref_lam_type(name)
        if node.data == "lambdef":
            return self._lambda_lam_type(node)
        if node.data == "getattr" and len(node.children) == 2:
            # ``Tools.countRows`` passed by name: a static method.
            owner = node.children[0]
            if isinstance(owner, Tree) and owner.data == "var" and owner.children:
                class_name = self._get_name(owner.children[0])
                method = self._get_name(node.children[1])
                if (
                    class_name not in self.declared_vars
                    and method in self._static_methods.get(class_name, set())
                ):
                    return self._func_ref_lam_type(f"{class_name}.{method}")
        if node.data == "tuple":
            items = [self._arg_lam_type(c) or self._literal_elem_lam_type(c) for c in node.children if c is not None]
            return GenericType("tuple", tuple(items)) if items and all(items) else None
        if node.data == "list" and node.children:
            items = [self._arg_lam_type(c) or self._literal_elem_lam_type(c) for c in node.children if c is not None]
            if items and all(items) and all(item == items[0] for item in items):
                return ListType(items[0])
            return None
        go_type = self._expr_go_type_hint(node) or ""
        if go_type and "interface{}" not in go_type:
            try:
                return parse_type(self._go_type_lam_text(go_type))
            except Exception:
                return None
        return None

    def _func_ref_lam_type(self, key: str):
        """``func(Conn) -> Result[int]`` for a function (or ``Class.method``
        static method) passed by name; ``None`` when unknown or overloaded."""
        from compiler.typesys import FuncType, NamedType
        if key not in self._user_functions and key not in self._external_user_functions and "." not in key:
            return None
        if len(self._overload_variants.get(key, ())) > 1:
            return None
        params = self._func_param_lam_types.get(key)
        if params is None:
            if self._func_param_counts.get(key, -1) != 0:
                return None
            params = []
        if any(p is None for p in params) or len(params) != self._func_param_counts.get(key, len(params)):
            return None
        ret = self._func_return_lam_types.get(key, NamedType("None"))
        if ret is None:
            return None
        return FuncType(tuple(params), ret)

    def _lambda_lam_type(self, node):
        """The Lam type of a lambda from its annotations
        (``lambda (tx: Conn) -> Result[int]: ...`` is
        ``func(Conn) -> Result[int]``); unannotated parts are ``any``, which
        binds nothing."""
        from compiler.typesys import FuncType, NamedType
        params = []
        ret = None
        params_node = None
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data in ("paren_lambda_params", "inline_lambda_params"):
                params_node = child
                for param in child.children:
                    if isinstance(param, Tree) and param.data == "typed_lambda_param" and len(param.children) > 1 \
                            and param.children[1] is not None:
                        params.append(self._parsed_lam_type(param.children[1]) or NamedType("any"))
                    elif param is not None:
                        params.append(NamedType("any"))
            elif child.data == "lambda_return_anno" and child.children:
                ret = self._parsed_lam_type(child.children[0])
        if ret is None:
            go_ret = ""
            raw_params = self._collect_lambda_params(params_node)
            if all(declared for _name, declared in raw_params):
                go_ret = self._lambda_result_go_type(node, [declared for _name, declared in raw_params])
            ret = NamedType("any")
            if go_ret and "interface{}" not in go_ret:
                try:
                    from compiler.typesys import parse_type
                    ret = parse_type(self._go_type_lam_text(go_ret))
                except Exception:
                    pass
            if isinstance(ret, NamedType) and ret.name in {"any", "Result"}:
                # ``lambda tx: addItem(tx, "d")``: the declared result of
                # the function the body calls (``Result[int]``), which the
                # erased Go ``*Result`` does not carry.
                called = self._lambda_body_call_lam_type(node)
                if called is not None:
                    ret = called
        return FuncType(tuple(params), ret)

    def _lambda_body_call_lam_type(self, node):
        """The declared Lam result of the non-generic, non-overloaded
        function (top-level, nested or ``Class.static``) an expression-bodied
        lambda calls; ``None`` otherwise."""
        body = None
        for child in node.children:
            if isinstance(child, Tree) and child.data in (
                "paren_lambda_params", "inline_lambda_params", "lambda_return_anno",
            ):
                continue
            if child is not None:
                body = child
        if not isinstance(body, Tree) or body.data != "funccall" or not body.children:
            return None
        callee = body.children[0]
        key = ""
        if isinstance(callee, Tree) and callee.data == "var" and callee.children:
            name = self._get_name(callee.children[0])
            nested_key = self._nested_func_keys.get(name)
            if isinstance(nested_key, str):
                key = nested_key
            elif name not in self.declared_vars and name not in self._var_go_types:
                key = name
        elif isinstance(callee, Tree) and callee.data == "getattr" and len(callee.children) == 2:
            owner = callee.children[0]
            if isinstance(owner, Tree) and owner.data == "var" and owner.children:
                owner_name = self._get_name(owner.children[0])
                method = self._get_name(callee.children[1])
                if owner_name not in self.declared_vars and method in self._static_methods.get(owner_name, set()):
                    key = f"{owner_name}.{method}"
                elif owner_name in self._var_types:
                    # ``accounts.count(tx)``: a method of the receiver's
                    # class or of the first base declaring it.
                    pending = [self._var_types[owner_name]]
                    seen: set = set()
                    while pending and not key:
                        class_name = pending.pop(0)
                        if class_name in seen or class_name in self._generic_classes:
                            continue
                        seen.add(class_name)
                        if f"{class_name}.{method}" in self._func_return_lam_types:
                            key = f"{class_name}.{method}"
                        pending.extend(self.class_bases.get(class_name, []))
        if not key or key in self._func_generic_order or key in self._generic_instance_methods:
            return None
        ref = self._func_ref_lam_type(key)
        return ref.ret if ref is not None else None

    def _expected_call_lam_type(self):
        """The Lam type the value of the call being lowered must have
        (``r: Result[int] = make()``), published by ``_expr_to_go`` for
        the call that is the whole value of an annotated assignment, a
        ``return``, an argument or a field assignment; ``None``
        elsewhere."""
        return getattr(self, "_call_result_lam", None)

    # ─── Expected types of calls ─────────────────────────────────
    #
    # A type parameter only the result mentions (``make[T]() ->
    # Result[T]``) is bound by the type the call's value must have. The
    # annotated assignment publishes it through ``_expected_value_lam``;
    # the other positions record it per call node in
    # ``_expected_call_lams`` (``_expr_to_go`` reads both).

    def _note_expected_call_lam(self, value, lam_type) -> None:
        """Record that the call ``value`` (or the operand of ``value?``,
        then a ``Result[lam_type]``) must have the Lam type ``lam_type``."""
        from compiler.typesys import GenericType, NamedType
        if lam_type is None or isinstance(value, Token) or not isinstance(value, Tree):
            return
        if isinstance(lam_type, NamedType) and lam_type.name in {"any", "object", "json"}:
            return
        if value.data == "argvalue" and len(value.children) == 1:
            value = value.children[0]
        if isinstance(value, Tree) and value.data == "propagate" and value.children:
            value = value.children[0]
            lam_type = GenericType("Result", (lam_type,))
        if isinstance(value, Tree) and value.data == "funccall":
            self._expected_call_lams[id(value)] = lam_type
        elif isinstance(value, Tree) and value.data in {"list", "set", "dict"}:
            # ``xs: list[Box[int]] = [Box([])]``: the items of a literal
            # with an expected collection type expect its item type.
            self._note_expected_item_lams(value, lam_type)

    def _note_expected_item_lams(self, literal, lam_type) -> None:
        from compiler.typesys import DictType, GenericType, ListType
        if literal.data == "list" and isinstance(lam_type, ListType):
            items = [c for c in literal.children if isinstance(c, Tree)]
            item_type = lam_type.item
        elif literal.data == "set" and isinstance(lam_type, GenericType) and lam_type.base == "set" and lam_type.args:
            items = [c for c in literal.children if isinstance(c, Tree)]
            item_type = lam_type.args[0]
        elif literal.data == "dict" and isinstance(lam_type, DictType):
            items = [
                c.children[1] for c in literal.children
                if isinstance(c, Tree) and c.data == "key_value" and len(c.children) >= 2
            ]
            item_type = lam_type.value
        else:
            return
        for item in items:
            self._note_expected_call_lam(item, item_type)

    def _note_expected_go_item(self, value, go_type: str, collection=None, extend: bool = False) -> None:
        """``xs.append(Box([]))`` on a ``list[Box[int]]``: the item expects
        the item type of the collection ``collection`` (an expression: its
        recorded Lam type, else the Lam type of the Go item type
        ``go_type``)."""
        from compiler.typesys import DictType, GenericType, ListType, parse_type
        lam_type = None
        if isinstance(collection, Tree) and collection.data == "var" and collection.children:
            known = self._var_lam_types.get(self._get_name(collection.children[0]))
            if isinstance(known, ListType):
                lam_type = known if extend else known.item
            elif isinstance(known, DictType):
                lam_type = known.value
            elif isinstance(known, GenericType) and known.base == "set" and known.args:
                lam_type = known.args[0]
        elif isinstance(collection, Tree) and collection.data == "getattr":
            known = self._field_expected_lam(collection)
            if isinstance(known, ListType):
                lam_type = known if extend else known.item
            elif isinstance(known, DictType):
                lam_type = known.value
        if lam_type is None:
            if not go_type or "interface{}" in go_type:
                return
            try:
                lam_type = parse_type(self._go_type_lam_text(go_type))
            except Exception:
                return
        self._note_expected_call_lam(value, lam_type)

    def _collect_expected_return_calls(self, tree) -> None:
        """Record the declared result of the enclosing function or lambda
        for every call that is the value of a ``return`` (and for the body
        of an expression lambda): ``return make()`` in a ``-> Result[int]``
        function binds ``make``'s ``T``."""
        from compiler.typesys import GenericType
        stack = [(tree, None)]
        while stack:
            node, ret = stack.pop()
            for child in node.children:
                if not isinstance(child, Tree):
                    continue
                child_ret = ret
                if child.data == "funcdef":
                    return_node = self._parse_funcdef(child)[5]
                    child_ret = None
                    if isinstance(return_node, Tree) and return_node.data == "single_return_type":
                        child_ret = self._parsed_lam_type(return_node)
                        if isinstance(child_ret, GenericType) and child_ret.base in {"generator", "Generator", "Iterator", "iterator"}:
                            child_ret = None
                elif child.data == "lambdef":
                    child_ret = None
                    for part in child.children:
                        if isinstance(part, Tree) and part.data == "lambda_return_anno" and part.children:
                            child_ret = self._parsed_lam_type(part.children[0])
                    body = child.children[-1] if child.children else None
                    if isinstance(body, Tree) and body.data != "suite":
                        self._note_expected_call_lam(body, child_ret)
                elif child.data == "return_stmt":
                    if child.children and child.children[0] is not None:
                        self._note_expected_call_lam(child.children[0], ret)
                stack.append((child, child_ret))

    def _note_arg_expected_lams(self, func_key: str, args_node) -> None:
        """The declared Lam parameter types of ``func_key`` for the calls
        passed to it (``take(make())``, ``take(r=make())``); a parameter
        type mentioning a type parameter of the callee the call does not
        bind gives none."""
        lam_params = self._func_param_lam_types.get(func_key) or []
        if not lam_params or not isinstance(args_node, Tree) or args_node.data != "arguments":
            return
        param_names = self._func_param_names.get(func_key, [])
        owner = func_key.rsplit(".", 1)[0] if "." in func_key else ""
        open_names = set(self._func_generic_params.get(func_key, set()) or ())
        if owner:
            open_names.update(self._class_type_param_names_of(owner) or ())
        index = 0
        for child in args_node.children:
            if not isinstance(child, Tree) or child.data in {"starargs", "stararg", "kwargs"}:
                continue
            if child.data == "argvalue" and len(child.children) == 2:
                name_node = child.children[0]
                name = self._get_name(name_node.children[0]) if (
                    isinstance(name_node, Tree) and name_node.data == "var" and name_node.children
                ) else ""
                pos = param_names.index(name) if name in param_names else -1
                node = child.children[1]
            else:
                pos = index
                index += 1
                node = child.children[0] if child.data == "argvalue" and child.children else child
            declared = lam_params[pos] if 0 <= pos < len(lam_params) else None
            if declared is None or not isinstance(node, Tree) or node.data not in {
                "funccall", "propagate", "list", "set", "dict",
            }:
                continue
            declared = self._bound_lam_type(declared, open_names)
            if declared is not None:
                self._note_expected_call_lam(node, declared)

    def _bound_lam_type(self, lam_type, open_names):
        """``lam_type`` with the type parameters in ``open_names`` replaced
        by the current call's type arguments (``_call_type_subst``);
        ``None`` when one stays unbound."""
        from compiler.typesys import parse_type
        if not open_names:
            return lam_type
        text = render_lam_type(lam_type)
        mentioned = [n for n in open_names if mentions_names(text, [n])]
        if not mentioned:
            return lam_type
        subst = {}
        for name in mentioned:
            go_type = (self._call_type_subst or {}).get(name, "")
            if not go_type or go_type == name or "interface{}" in go_type:
                return None
            subst[name] = self._go_type_lam_text(go_type)
        try:
            return parse_type(substitute_go_type(text, subst))
        except Exception:
            return None

    def _field_expected_lam(self, target_node):
        """The declared Lam type of the field ``target_node``
        (``self.items``, ``store.items``) is assigned to; ``None`` when
        unknown or when it mentions the type parameters of a generic
        class seen from outside."""
        if not (isinstance(target_node, Tree) and target_node.data == "getattr" and len(target_node.children) == 2):
            return None
        obj_node = target_node.children[0]
        field = self._get_name(target_node.children[1])
        own = (
            isinstance(obj_node, Tree) and obj_node.data == "var" and obj_node.children
            and self._get_name(obj_node.children[0]) == "self" and bool(self.current_class)
        )
        class_name = self.current_class if own else self._infer_expr_class(obj_node)
        if not class_name or not field:
            return None
        for owner in [class_name, *self._hier_ancestors(class_name)]:
            lam_type = self._field_lam_types.get(f"{owner}.{field}") or self._external_field_lam_types.get(f"{owner}.{field}")
            if lam_type is None:
                continue
            params = self._class_type_param_names_of(owner) or []
            if params and not (own and owner == class_name) and self._mentions_lam_names(lam_type, set(params)):
                # ``q.items = none()`` with ``q: Reg[str]``: the receiver's
                # type arguments.
                receiver = self._receiver_type_subst(owner, obj_node, class_name)
                if not receiver:
                    return None
                with self._scoped(_call_type_subst=receiver):
                    return self._bound_lam_type(lam_type, set(params))
            return lam_type
        return None

    def _literal_elem_lam_type(self, node):
        from compiler.typesys import parse_type
        go_type = (self._infer_type_from_value(node) or "").strip()
        if not go_type or "interface{}" in go_type:
            return None
        try:
            return parse_type(self._go_type_lam_text(go_type))
        except Exception:
            return None

    def _lam_generic_call_subst(self, func_key: str, args_node, names, subst: Dict[str, str]) -> None:
        lam_params = self._func_param_lam_types.get(func_key) or []
        bound: Dict[str, object] = {}
        if lam_params and isinstance(args_node, Tree) and args_node.data == "arguments":
            self._bind_lam_args(func_key, args_node, lam_params, names, bound)
        expected = self._expected_call_lam_type()
        returned = self._func_return_lam_types.get(func_key)
        if returned is None and expected is not None and func_key.endswith(".init"):
            # ``c: Box[int] = Box([])``: a generic constructor yields
            # ``Box[T]``.
            from compiler.typesys import GenericType, NamedType
            class_name = func_key[: -len(".init")]
            params = self._class_type_param_names_of(class_name) or []
            if params:
                returned = GenericType(class_name, tuple(NamedType(p) for p in params))
        if expected is not None and returned is not None and any(n not in bound for n in names):
            # ``r: Result[int] = make()`` with ``make[T]() -> Result[T]``:
            # nothing else determines ``T``.
            _unify_lam_types(returned, expected, set(names), bound)
        for name, lam_type in bound.items():
            if isinstance(lam_type, Tree) or lam_type is None:
                continue
            if self._mentions_lam_names(lam_type, set(names) - set(self._generic_names)):
                # Bound to another unresolved parameter of the callee.
                continue
            type_node = self._lam_type_text_node(render_lam_type(lam_type))
            go_type = self._type_expr_to_go(type_node) if type_node is not None else ""
            if go_type and go_type != "interface{}":
                subst.setdefault(name, go_type)

    @staticmethod
    def _mentions_lam_names(type_, names) -> bool:
        from compiler.typesys import render_type
        return bool(names) and mentions_names(render_type(type_), names)

    def _bind_lam_args(self, func_key: str, args_node, lam_params, names, bound: Dict[str, object]) -> None:
        param_names = self._func_param_names.get(func_key, [])
        index = 0
        for child in args_node.children:
            if not isinstance(child, Tree) or child.data in {"starargs", "stararg", "kwargs"}:
                continue
            if child.data == "argvalue" and len(child.children) == 2:
                name_node = child.children[0]
                name = self._get_name(name_node.children[0]) if (
                    isinstance(name_node, Tree) and name_node.data == "var" and name_node.children
                ) else ""
                pos = param_names.index(name) if name in param_names else -1
                node = child.children[1]
            else:
                pos = index
                index += 1
                node = child.children[0] if child.data == "argvalue" and child.children else child
            declared = lam_params[pos] if 0 <= pos < len(lam_params) else None
            if declared is None:
                continue
            actual = self._arg_lam_type(node)
            if actual is not None:
                _unify_lam_types(declared, actual, set(names), bound)

    def _uninferable_type_args(self, call_key: str, args_node, order: Optional[List[str]] = None,
                               base: Optional[Dict[str, str]] = None) -> str:
        """``[string, int]`` for a call Go cannot infer the type arguments
        of (a type parameter appears in no Go parameter type), when this
        call binds all of them; ``""`` otherwise. ``order`` defaults to the
        function's type parameters, ``base`` holds the receiver's."""
        order = order if order is not None else (self._func_generic_order.get(call_key) or [])
        if not order:
            return ""
        signature = " ".join(self._func_param_go_types.get(call_key, []))
        if all(base and name in base or mentions_names(signature, [name]) for name in order):
            return ""
        subst = self._generic_call_subst(call_key, args_node, order, base)
        if not all(subst.get(name) for name in order):
            return ""
        return "[" + ", ".join(subst[name] for name in order) + "]"

    def _complete_generic_subst(self, subst: Dict[str, str], generic_names) -> Dict[str, str]:
        """Unresolved type parameters fall back to ``interface{}`` (Go then
        infers the same), except those in scope at the call site (a
        generic method calling another one with its own ``T``)."""
        out = dict(subst)
        for name in generic_names:
            if name not in out and name not in self._generic_names:
                out[name] = "interface{}"
        return out

    # ─── Methods of generic classes / generic methods ───────────

    def _generic_method_owner(self, class_name: str, method: str) -> str:
        """The class declaring ``method`` among ``class_name`` and its
        ancestors (``""`` when unknown)."""
        if f"{class_name}.{method}" in self._func_param_go_types or f"{class_name}.{method}" in self._generic_method_returns:
            return class_name
        for ancestor in self._hier_ancestors(class_name):
            key = f"{ancestor}.{method}"
            if key in self._func_param_go_types or key in self._generic_method_returns or key in self._generic_instance_methods:
                return ancestor
        return class_name

    def _receiver_type_subst(self, owner: str, receiver_node, class_name: str) -> Dict[str, str]:
        """``{T: int}`` for a method of ``owner`` called on a receiver typed
        ``*Owner[int]`` (or on a subclass extending ``Owner[int]``)."""
        params = self._class_type_param_names_of(owner)
        if not params:
            return {}
        if (
            isinstance(receiver_node, Tree) and receiver_node.data == "var" and receiver_node.children
            and self._get_name(receiver_node.children[0]) == "self"
            and owner == class_name == self.current_class
        ):
            # ``self.each(...)`` inside ``class Seq[T]``: the class's own,
            # in-scope ``T``.
            return {p: p for p in params}
        if owner != class_name:
            mapping = self._ancestor_type_args(class_name, owner)
            if mapping:
                return dict(zip(params, mapping))
            return {}
        go_type = self._expr_go_type_hint(receiver_node) if receiver_node is not None else ""
        parsed = parse_go_type(go_type)
        if parsed is not None and parsed[0] == "ptr" and parsed[1][0] == "generic":
            args = [render_go_type(a) for a in parsed[1][2]]
            if len(args) == len(params) and not any(a == p for a, p in zip(args, params)):
                return dict(zip(params, args))
        return {}

    def _ancestor_type_args(self, class_name: str, ancestor: str) -> List[str]:
        """Go type arguments ``class_name`` gives the generic ``ancestor``
        (``class IntSeq(Seq[int])`` -> ``["int"]``)."""
        for base in self._hier_bases(class_name):
            args = self._base_type_args(class_name, base)
            if base == ancestor:
                return self._split_type_arg_list(args)
            deeper = self._ancestor_type_args(base, ancestor)
            if deeper:
                own = self._split_type_arg_list(args)
                base_params = self._class_type_param_names_of(base)
                if own and len(own) == len(base_params):
                    mapping = dict(zip(base_params, own))
                    deeper = [substitute_go_type(d, mapping) for d in deeper]
                return deeper
        return []

    @staticmethod
    def _split_type_arg_list(args: str) -> List[str]:
        args = (args or "").strip()
        if not args.startswith("[") or not args.endswith("]"):
            return []
        parsed = parse_go_type("X" + args)
        if parsed is None or parsed[0] != "generic":
            return []
        return [render_go_type(a) for a in parsed[2]]

    def _method_call_subst(self, class_name: str, method: str, receiver_node, args_node,
                           static: bool = False) -> Dict[str, str]:
        """Complete type-parameter binding of ``recv.method(args)`` (or the
        static ``Class.method(args)``)."""
        owner = self._generic_method_owner(class_name, method)
        key = f"{owner}.{method}"
        names = list(self._func_generic_params.get(key, set()))
        if not names:
            return {}
        base = {} if static else self._receiver_type_subst(owner, receiver_node, class_name)
        subst = self._generic_call_subst(key, args_node, sorted(names), base)
        return subst

    def _generic_method_result_go_type(self, class_name: str, method: str, receiver_node, args_node,
                                       static: bool = False) -> str:
        """Go result type of a method of a generic class (or a generic
        method) at this call site: ``*Iter[int]`` for ``it.map(...)``."""
        owner = self._generic_method_owner(class_name, method)
        declared = self._generic_method_returns.get(f"{owner}.{method}", "")
        if not declared:
            return ""
        names = self._func_generic_params.get(f"{owner}.{method}", set())
        subst = self._method_call_subst(class_name, method, receiver_node, args_node, static=static)
        result = substitute_go_type(declared, subst)
        if mentions_names(result, [n for n in names if n not in self._generic_names]):
            return ""
        return result

    def _generic_method_call_to_go(self, obj_class: str, method: str, raw_obj: str,
                                   receiver_node, args_node) -> Optional[str]:
        """``it.map(f)`` where ``map`` declares its own type parameters:
        ``Iter_Map(it, f)`` (the receiver upcast to the declaring class)."""
        owner = self._generic_method_owner(obj_class, method)
        key = f"{owner}.{method}"
        if key not in self._generic_instance_methods:
            return None
        names = sorted(self._func_generic_params.get(key, set()))
        bound = self._method_call_subst(obj_class, method, receiver_node, args_node)
        subst = self._complete_generic_subst(bound, names)
        with self._scoped(_call_type_subst=subst):
            call_args = self._planned_call_args(key, args_node)
        receiver = raw_obj
        if owner != obj_class:
            for ancestor, path, _go_type in self._hier_upcast_paths(obj_class):
                if ancestor == owner:
                    receiver = self._hier_receiver(raw_obj) + path[1:]
                    break
        go_name = self._generic_method_go_name(owner, method)
        explicit = self._uninferable_type_args(
            key, args_node, self._method_generic_order.get(key, []),
            {k: v for k, v in bound.items() if k not in self._generic_instance_methods.get(key, [])},
        )
        return f"{go_name}{explicit}({', '.join([receiver] + call_args)})"

    def _builtin_func_ref_to_go(self, node, go_type: str) -> str:
        """``xs.map(len)`` / ``it.map(str)``: a builtin passed where a
        one-parameter ``func(P) R`` is expected becomes a function literal
        (Go builtins and conversions are not values)."""
        if not (isinstance(node, Tree) and node.data == "var" and node.children):
            return ""
        name = self._get_name(node.children[0])
        if (
            name not in BUILTIN_FUNC_REFS
            or name in self._user_functions
            or name in self.declared_vars
            or name in self._var_go_types
        ):
            return ""
        parsed = parse_go_type(go_type)
        if parsed is None or parsed[0] != "func" or len(parsed[1]) != 1:
            return ""
        param = render_go_type(parsed[1][0])
        arg = "_lamArg"
        call = Tree("funccall", [
            Tree("var", [Token("NAME", name)]),
            Tree("arguments", [Tree("var", [Token("NAME", arg)])]),
        ])
        saved = dict(self._var_go_types)
        saved_declared = set(self.declared_vars)
        try:
            self._var_go_types[arg] = param
            self.declared_vars.add(arg)
            result = render_go_type(parsed[2]) if parsed[2] is not None else (
                BUILTIN_FUNC_REFS[name] or param
            )
            body = self._typed_value_to_go(call, result)
        finally:
            self._var_go_types = saved
            self.declared_vars = saved_declared
        return f"func({arg} {param}) {result} {{ return {body} }}"

    # ─── Builtin list combinators ──────────────────────────────

    def _list_callback_result_go_type(self, callback, param_types: List[str]) -> str:
        """Go result type of a list-method callback (lambda, function name or
        builtin) called with ``param_types``; ``""`` when unknown."""
        if isinstance(callback, Tree) and callback.data == "argvalue" and len(callback.children) == 1:
            callback = callback.children[0]
        if isinstance(callback, Tree) and callback.data == "lambdef":
            result = self._lambda_result_go_type(callback, param_types)
            return "" if result == "interface{}" else result
        ref = self._func_ref_go_type(callback, param_types)
        parsed = parse_go_type(ref)
        if parsed is not None and parsed[0] == "func" and parsed[2] is not None:
            return render_go_type(parsed[2])
        return ""

    def _list_reduce_acc_go_type(self, elem: str, raw_args: list) -> str:
        """Accumulator type of ``xs.reduce(fn, initial)``: the initial
        value's type when given (``words.reduce(f, 0)`` is an ``int``)."""
        if len(raw_args) >= 2:
            initial = raw_args[1]
            hint = self._expr_go_type_hint(initial) or self._infer_type_from_value(initial)
            if hint and hint != "interface{}":
                return hint
        return elem

    def _list_method_result_go_type(self, receiver_go: str, method: str, args_node) -> str:
        """Go type of ``xs.map(f)`` / ``filter`` / ``sort`` / ``reduce`` /
        ``any`` / ``all`` on a ``[]X`` receiver, for chained calls."""
        if not receiver_go.startswith("[]"):
            return ""
        elem = receiver_go[2:]
        raw_args = self._get_raw_call_args(args_node)
        if method in {"filter", "sort"}:
            return receiver_go
        if method in {"any", "all"}:
            return "bool"
        if method == "reduce":
            return self._list_reduce_acc_go_type(elem, raw_args)
        if method == "map" and raw_args:
            result = self._list_callback_result_go_type(raw_args[0], [elem])
            return f"[]{result}" if result else ""
        return ""
