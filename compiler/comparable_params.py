#!/usr/bin/env python3
"""Type parameters that must be Go ``comparable``.

A Lam set lowers to ``map[T]bool`` and a dict to ``map[K]V``, and Go only
accepts a type parameter as a map key when its constraint is
``comparable``: ``func count[T](s: set[T])`` emitted as
``func Count[T any](s map[T]bool)`` failed in ``go build`` with ``invalid
map key type T (missing comparable constraint)``.

``comparable_type_params(tree)`` finds, for every generic function, method
and class of a module, the type parameters written as a set element or a
dict key anywhere in its signature or body (``set[T]``, ``dict[K, V]``,
``dict[str, set[T]]``), then propagates the requirement through the
module's own generic definitions, until nothing changes:

* an explicit instantiation (``Bag[T]`` in an annotation,
  ``Bag[T]()`` / ``distinct[T](xs)`` in an expression) of a definition
  whose parameter at that position needs ``comparable``;
* an inferred call (``distinct(xs)`` / ``Bag(xs)``) whose argument is a
  name declared with a type mentioning the caller's type parameter where
  the callee's parameter type has the comparable one (``xs: list[T]``
  against ``ys: list[U]``).

The result maps ``id(type_params node)`` to the names needing
``comparable``; ``HelpersMixin._type_params_to_go`` reads it when it
lowers the clause. A type parameter used only elsewhere keeps its
constraint (``any`` by default).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from lark import Token, Tree

# Go constraints of ``HelpersMixin._BUILTIN_CONSTRAINTS`` whose type sets
# hold only comparable types: kept as they are for a map key.
COMPARABLE_GO_CONSTRAINTS = frozenset({
    "comparable", "int", "int64", "float64", "string", "bool",
    "float64 | int", "~int | ~int64 | ~float64 | ~string",
})


def comparable_constraint(constraint: str) -> str:
    """The Go constraint of a type parameter used as a map key."""
    constraint = (constraint or "any").strip()
    if constraint in COMPARABLE_GO_CONSTRAINTS:
        return constraint
    if constraint == "any":
        return "comparable"
    return f"interface{{ comparable; {constraint} }}"


def _name(node) -> str:
    if isinstance(node, Token):
        return str(node)
    if isinstance(node, Tree):
        if node.data in {"name", "var"} and node.children:
            return _name(node.children[0])
        if node.data == "dotted_name":
            return ".".join(_name(c) for c in node.children if c is not None)
    return ""


def _core(type_node):
    """``type_expr`` / ``type_union`` wrappers of a single type removed."""
    while (
        isinstance(type_node, Tree)
        and type_node.data in {"type_expr", "type_union"}
        and len([c for c in type_node.children if c is not None]) == 1
    ):
        type_node = next(c for c in type_node.children if c is not None)
    return type_node


def _bare_type_name(type_node) -> str:
    """``T`` for a type written as one plain name, else ``""``."""
    core = _core(type_node)
    if isinstance(core, Tree) and core.data == "type_name" and core.children:
        dotted = core.children[0]
        if isinstance(dotted, Tree) and dotted.data == "dotted_name" and len(dotted.children) == 1:
            return _name(dotted)
    return ""


class _Def:
    """A generic function, method or class (or any function, for its
    locals)."""

    def __init__(self, node: Tree, kind: str, name: str, tp_node, parent: Optional["_Def"]):
        self.node = node
        self.kind = kind
        self.name = name
        self.tp_node = tp_node
        self.parent = parent
        self.names: List[str] = []
        if isinstance(tp_node, Tree):
            for child in tp_node.children:
                if isinstance(child, Tree) and child.data == "type_param" and child.children:
                    self.names.append(_name(child.children[0]))
        # Positional typed parameters (name, type_expr); a class takes its
        # ``init``'s.
        self.params: List[tuple] = []
        # Declared types of parameters and annotated locals (name -> type_expr).
        self.env: Dict[str, Tree] = {}

    def lookup(self, var: str):
        scope: Optional[_Def] = self
        while scope is not None:
            if var in scope.env:
                return scope.env[var]
            scope = scope.parent
        return None


def _type_params_node(node: Tree):
    return next((c for c in node.children if isinstance(c, Tree) and c.data == "type_params"), None)


def _typed_params(params_node) -> List[tuple]:
    out: List[tuple] = []
    if not isinstance(params_node, Tree):
        return out
    for child in params_node.children:
        param = child
        if isinstance(child, Tree) and child.data == "typed_paramvalue" and child.children:
            param = child.children[0]
        if isinstance(param, Tree) and param.data == "typed_param" and len(param.children) >= 2:
            type_node = next((c for c in param.children[1:] if isinstance(c, Tree) and c.data == "type_expr"), None)
            if type_node is not None:
                out.append((_name(param.children[0]), type_node))
    return out


class _Analysis:
    def __init__(self) -> None:
        self.defs: List[_Def] = []
        self.by_name: Dict[str, List[_Def]] = {}
        self.need: Dict[int, Set[str]] = {}
        # (owner def, type-parameter name) marked directly.
        self.direct: List[tuple] = []
        # (callee name, position, owner def, type-parameter name).
        self.positional: List[tuple] = []
        # (callee name, [argument nodes], caller def).
        self.inferred: List[tuple] = []
        # Names of the module's generic definitions (``_generic_names``).
        self.generic_names: Set[str] = set()

    # ── Scope resolution ────────────────────────────────────────

    @staticmethod
    def _resolve(scope: Optional[_Def], name: str) -> Optional[_Def]:
        while scope is not None:
            if name in scope.names:
                return scope
            scope = scope.parent
        return None

    # ── Collection walk ─────────────────────────────────────────

    def walk(self, node, scope: Optional[_Def]) -> None:
        if not isinstance(node, Tree):
            return
        if node.data == "funcdef":
            self._walk_funcdef(node, scope)
            return
        if node.data == "classdef":
            self._walk_classdef(node, scope)
            return
        if node.data == "type_generic":
            self._record_type_generic(node, scope)
        elif node.data == "annassign":
            self._record_annassign(node, scope)
        elif node.data == "funccall":
            self._record_call(node, scope)
        elif node.data == "getitem":
            self._record_explicit_instantiation(node, scope)
        for child in node.children:
            self.walk(child, scope)

    def _walk_funcdef(self, node: Tree, scope: Optional[_Def]) -> None:
        name_node = next((c for c in node.children if isinstance(c, Tree) and c.data == "name"), None)
        params_node = next((c for c in node.children if isinstance(c, Tree) and c.data == "typed_parameters"), None)
        fdef = _Def(node, "func", _name(name_node), _type_params_node(node), scope)
        fdef.params = _typed_params(params_node)
        fdef.env.update(dict(fdef.params))
        self.defs.append(fdef)
        if fdef.names and not (scope is not None and scope.kind == "class"):
            self.by_name.setdefault(fdef.name, []).append(fdef)
        if scope is not None and scope.kind == "class" and fdef.name in {"init", "__init__"} and not scope.params:
            scope.params = fdef.params
        for child in node.children:
            if isinstance(child, Tree) and child.data != "type_params":
                self.walk(child, fdef)

    def _walk_classdef(self, node: Tree, scope: Optional[_Def]) -> None:
        name = _name(node.children[0]) if node.children else ""
        cdef = _Def(node, "class", name, _type_params_node(node), scope)
        self.defs.append(cdef)
        if cdef.names:
            self.by_name.setdefault(name, []).append(cdef)
        for child in node.children[1:]:
            if isinstance(child, Tree) and child.data != "type_params":
                self.walk(child, cdef)

    def _record_type_generic(self, node: Tree, scope: Optional[_Def]) -> None:
        if not node.children:
            return
        base = _name(node.children[0])
        args = [c for c in node.children[1:] if isinstance(c, Tree) and c.data == "type_expr"]
        if base in {"set", "dict"} and args:
            key = _bare_type_name(args[0])
            owner = self._resolve(scope, key) if key else None
            if owner is not None:
                self.direct.append((owner, key))
            return
        for position, arg in enumerate(args):
            key = _bare_type_name(arg)
            owner = self._resolve(scope, key) if key else None
            if owner is not None:
                self.positional.append((base, position, owner, key))

    def _record_annassign(self, node: Tree, scope: Optional[_Def]) -> None:
        if scope is None:
            return
        target = next((c for c in node.children if isinstance(c, Tree)), None)
        type_node = next((c for c in node.children if isinstance(c, Tree) and c.data == "type_expr"), None)
        if isinstance(target, Tree) and target.data == "var" and type_node is not None:
            scope.env.setdefault(_name(target), type_node)

    def _record_explicit_instantiation(self, node: Tree, scope: Optional[_Def]) -> None:
        """``Bag[T]()`` / ``distinct[T](xs)``: type arguments written as an
        index of a generic definition's name."""
        if len(node.children) != 2:
            return
        base, index = node.children
        if not (isinstance(base, Tree) and base.data == "var"):
            return
        name = _name(base)
        if name not in self.generic_names:
            return
        items = index.children if isinstance(index, Tree) and index.data == "subscript_tuple" else [index]
        for position, item in enumerate(items):
            key = _name(item) if isinstance(item, Tree) and item.data == "var" else ""
            owner = self._resolve(scope, key) if key else None
            if owner is not None:
                self.positional.append((name, position, owner, key))

    def _record_call(self, node: Tree, scope: Optional[_Def]) -> None:
        if scope is None or not node.children:
            return
        callee = node.children[0]
        if not (isinstance(callee, Tree) and callee.data == "var"):
            return
        args_node = node.children[1] if len(node.children) > 1 else None
        if isinstance(args_node, Tree) and args_node.data == "arguments":
            raw = list(args_node.children)
        elif args_node is None:
            raw = []
        else:
            raw = [args_node]
        positional = []
        for arg in raw:
            if isinstance(arg, Tree) and (
                (arg.data == "argvalue" and len(arg.children) == 2)
                or arg.data in {"stararg", "starargs", "kwargs"}
            ):
                break
            positional.append(arg)
        self.inferred.append((_name(callee), positional, scope))

    # ── Propagation ─────────────────────────────────────────────

    def _mark(self, owner: _Def, name: str) -> bool:
        names = self.need.setdefault(id(owner.tp_node), set())
        if name in names:
            return False
        names.add(name)
        return True

    def _needs(self, callee: _Def, name: str) -> bool:
        return name in self.need.get(id(callee.tp_node), set())

    def _unify(self, pattern, actual, callee: _Def, caller: _Def, out: List[tuple]) -> None:
        """Bindings ``(callee type parameter, caller def, caller type
        parameter)`` of ``pattern`` (the callee's parameter type) against
        ``actual`` (the argument's declared type)."""
        pattern, actual = _core(pattern), _core(actual)
        bare = _bare_type_name(pattern)
        if bare and bare in callee.names:
            key = _bare_type_name(actual)
            owner = self._resolve(caller, key) if key else None
            if owner is not None:
                out.append((bare, owner, key))
            return
        if not (isinstance(pattern, Tree) and isinstance(actual, Tree)) or pattern.data != actual.data:
            return
        left = [c for c in pattern.children if isinstance(c, Tree)]
        right = [c for c in actual.children if isinstance(c, Tree)]
        if len(left) != len(right):
            return
        for p_child, a_child in zip(left, right):
            self._unify(p_child, a_child, callee, caller, out)

    def propagate(self) -> None:
        for owner, name in self.direct:
            self._mark(owner, name)
        changed = True
        while changed:
            changed = False
            for callee_name, position, owner, name in self.positional:
                for callee in self.by_name.get(callee_name, []):
                    if position < len(callee.names) and self._needs(callee, callee.names[position]):
                        changed |= self._mark(owner, name)
            for callee_name, args, caller in self.inferred:
                for callee in self.by_name.get(callee_name, []):
                    if not self.need.get(id(callee.tp_node)):
                        continue
                    for (_param, pattern), arg in zip(callee.params, args):
                        if not (isinstance(arg, Tree) and arg.data == "var"):
                            continue
                        actual = caller.lookup(_name(arg))
                        if actual is None:
                            continue
                        bindings: List[tuple] = []
                        self._unify(pattern, actual, callee, caller, bindings)
                        for callee_tp, owner, name in bindings:
                            if self._needs(callee, callee_tp):
                                changed |= self._mark(owner, name)


def _generic_names(tree) -> Set[str]:
    """Names of the module's generic functions and classes (known before
    the walk, so an explicit instantiation of a later definition is
    recorded)."""
    out: Set[str] = set()
    stack = [tree]
    while stack:
        node = stack.pop()
        if not isinstance(node, Tree):
            continue
        if node.data in {"funcdef", "classdef"} and _type_params_node(node) is not None:
            if node.data == "classdef":
                out.add(_name(node.children[0]) if node.children else "")
            else:
                name_node = next((c for c in node.children if isinstance(c, Tree) and c.data == "name"), None)
                out.add(_name(name_node))
        stack.extend(c for c in node.children if isinstance(c, Tree))
    return out


def comparable_type_params(tree) -> Dict[int, Set[str]]:
    """``id(type_params node)`` -> the type parameters of that definition
    that must be Go ``comparable`` (see module docstring)."""
    if not isinstance(tree, Tree):
        return {}
    analysis = _Analysis()
    analysis.generic_names = _generic_names(tree)
    if not analysis.generic_names:
        return {}
    analysis.walk(tree, None)
    analysis.propagate()
    return {key: names for key, names in analysis.need.items() if names}
