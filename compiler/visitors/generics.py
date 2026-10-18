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
        typed, literals, callables = [], [], []
        for declared, node in pairs:
            if not declared or not mentions_names(declared, names):
                continue
            if isinstance(node, Tree) and node.data == "lambdef":
                callables.append((declared, node))
            elif self._func_ref_go_type(node, ["interface{}"]) and declared.startswith("func("):
                callables.append((declared, node))
            elif self._is_untyped_literal(node):
                literals.append((declared, node))
            else:
                typed.append((declared, node))
        for declared, node in typed:
            actual = self._expr_go_type_hint(node) or self._infer_type_from_value(node)
            if actual:
                unify_go_types(declared, actual, names, subst)
        for declared, node in literals:
            actual = self._infer_type_from_value(node)
            if actual and actual != "interface{}":
                unify_go_types(declared, actual, names, subst)
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
        return subst

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
        subst = self._method_call_subst(obj_class, method, receiver_node, args_node)
        subst = self._complete_generic_subst(subst, names)
        with self._scoped(_call_type_subst=subst):
            call_args = self._planned_call_args(key, args_node)
        receiver = raw_obj
        if owner != obj_class:
            for ancestor, path, _go_type in self._hier_upcast_paths(obj_class):
                if ancestor == owner:
                    receiver = self._hier_receiver(raw_obj) + path[1:]
                    break
        go_name = self._generic_method_go_name(owner, method)
        return f"{go_name}({', '.join([receiver] + call_args)})"

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
