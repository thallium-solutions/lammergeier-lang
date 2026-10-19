#!/usr/bin/env python3
"""Indexes provably non-negative (they skip the negative-index adjustment).

``xs[i]`` follows Python: a negative ``i`` counts from the end. The
transpiler adds a branch-free adjustment to a list / string / tuple index
unless its index can never be negative. The transpiler asks
:func:`index_nonneg` with the lower bounds this pre-pass computed for the
locals of the enclosing function (stored on the ``funcdef`` as
``_lam_index_bounds``). A lower bound is known for

* integer literals, ``len(...)`` and ``abs(...)`` (0), ``min`` / ``max``;
* ``a + b``, ``a - <constant>``, and ``*``, ``//``, ``%`` of non-negative
  values, a ternary;
* locals whose every assignment in the function has a bound: loop
  variables of ``range(n)`` (0), ``range(a, ...)`` (bound of ``a``),
  ``range(a, b, -k)`` (above ``b``), the index of ``enumerate(...)``,
  ``i = 0`` ... ``i += 1``, ``j = len(xs) // 2``, ... (the greatest
  fixpoint of ``bound(x) = min over its assignments``, widened to unknown
  when it keeps decreasing; parameters, destructuring targets and other
  bindings are unknown).

So ``for i in range(1, len(xs)) { xs[i - 1] }`` needs no adjustment.

Module globals are never trusted (a call may change them), so module-level
code only gets the literal / ``len`` rules. Integer overflow is ignored.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Set, Tuple

from lark import Token, Tree

from compiler import narrowing

_Check = Callable[[Dict[str, float]], Optional[float]]


def _name(node) -> str:
    return narrowing._name(node)


def _int_literal(node) -> Optional[int]:
    if isinstance(node, Tree) and node.data == "number" and node.children:
        text = str(node.children[0]).replace("_", "")
        try:
            return int(text, 0)
        except ValueError:
            return None
    if isinstance(node, Tree) and node.data == "factor" and len(node.children) == 2 \
            and str(node.children[0]) in {"-", "+"}:
        value = _int_literal(node.children[1])
        if value is None:
            return None
        return -value if str(node.children[0]) == "-" else value
    return None


_INF = float("inf")
Bounds = Dict[str, float]


class _Analysis:
    def __init__(self, shadowed_builtins: Set[str]):
        self.shadowed = shadowed_builtins

    # ─── Expressions ───────────────────────────────────────────

    def lower_bound(self, node, known: Bounds) -> Optional[float]:
        """A lower bound of the integer ``node`` (``None``: unbounded)."""
        if not isinstance(node, Tree):
            return None
        literal = _int_literal(node)
        if literal is not None:
            return literal
        d = node.data
        if d == "var":
            return known.get(_name(node))
        if d == "funccall" and node.children:
            callee = node.children[0]
            fname = _name(callee) if isinstance(callee, Tree) and callee.data == "var" else ""
            if not fname or fname in self.shadowed:
                return None
            args = narrowing._call_args(node)
            if fname == "len":
                return 0
            if fname == "abs":
                return 0 if len(args) == 1 else None
            bounds = [self.lower_bound(a, known) for a in args]
            if fname == "min":
                return min(bounds) if bounds and all(b is not None for b in bounds) else None
            if fname == "max":
                present = [b for b in bounds if b is not None]
                return max(present) if present else None
            return None
        if d == "arith_expr":
            children = list(node.children)
            result = self.lower_bound(children[0], known)
            for op, operand in zip(children[1::2], children[2::2]):
                if result is None:
                    return None
                op = str(op)
                if op == "+":
                    right = self.lower_bound(operand, known)
                    result = None if right is None else result + right
                elif op == "-":
                    constant = _int_literal(operand)
                    result = None if constant is None else result - constant
                else:
                    return None
            return result
        if d == "term":
            children = list(node.children)
            result = self.lower_bound(children[0], known)
            for op, operand in zip(children[1::2], children[2::2]):
                right = self.lower_bound(operand, known)
                if result is None or right is None or result < 0 or right < 0:
                    return None
                op = str(op)
                if op == "*":
                    result = result * right
                elif op in {"//", "%"}:
                    result = 0
                else:
                    return None
            return result
        if d == "test" and len(node.children) == 3:
            a = self.lower_bound(node.children[0], known)
            b = self.lower_bound(node.children[2], known)
            return None if a is None or b is None else min(a, b)
        if d == "factor" and len(node.children) == 2 and str(node.children[0]) == "+":
            return self.lower_bound(node.children[1], known)
        return None

    def nonneg(self, node, known: Bounds) -> bool:
        bound = self.lower_bound(node, known)
        return bound is not None and bound >= 0

    def range_bound(self, iterable, known: Bounds) -> Optional[float]:
        """Lower bound of the variable of ``for x in range(...)``."""
        if not (isinstance(iterable, Tree) and iterable.data == "funccall" and iterable.children):
            return None
        if _name(iterable.children[0]) != "range" or "range" in self.shadowed:
            return None
        args = narrowing._call_args(iterable)
        if any(isinstance(a, Tree) and a.data == "argvalue" for a in args):
            return None
        if len(args) == 1:
            return 0
        if len(args) == 2:
            return self.lower_bound(args[0], known)
        if len(args) == 3:
            step = _int_literal(args[2])
            if step is None or step == 0:
                return None
            if step > 0:
                return self.lower_bound(args[0], known)
            stop = self.lower_bound(args[1], known)
            return None if stop is None else stop + 1  # values stay above ``stop``
        return None

    def enumerate_bound(self, iterable, known: Bounds) -> Optional[float]:
        if not (isinstance(iterable, Tree) and iterable.data == "funccall" and iterable.children):
            return None
        if _name(iterable.children[0]) != "enumerate" or "enumerate" in self.shadowed:
            return None
        args = narrowing._call_args(iterable)
        if len(args) == 1:
            return 0
        if len(args) == 2:
            start = args[1]
            if isinstance(start, Tree) and start.data == "argvalue" and len(start.children) == 2:
                start = start.children[1]
            return self.lower_bound(start, known)
        return None

    # ─── Bindings of a function ────────────────────────────────

    def bindings(self, root: Tree) -> Tuple[List[Tuple[str, _Check]], Set[str], List[Tree], Set[str]]:
        """``[(name, check)]`` for bindings that may be bounded, the names
        bound in a way that never is, and (collected on the same walk) the
        function definitions under ``root`` and the ``nonlocal`` names."""
        checks: List[Tuple[str, _Check]] = []
        bad: Set[str] = set()
        funcdefs: List[Tree] = []
        nonlocals: Set[str] = set()

        def value_check(value) -> _Check:
            return lambda known, value=value: self.lower_bound(value, known)

        def for_target(target, iterable) -> None:
            names = narrowing.target_names(target)
            single = target
            if isinstance(single, Tree) and single.data in {"untyped_for_target", "typed_for_target", "exprlist"}:
                inner = [c for c in single.children if isinstance(c, Tree) and c.data != "type_expr"]
                if len(inner) == 1 and inner[0].data == "exprlist":
                    inner = [c for c in inner[0].children if isinstance(c, Tree)]
                if len(inner) == 1:
                    single = inner[0]
                elif len(inner) >= 2 and isinstance(inner[0], Tree) and inner[0].data == "var":
                    first = _name(inner[0])
                    checks.append((first, lambda known, it=iterable: self.enumerate_bound(it, known)))
                    bad.update(names - {first})
                    return
            if isinstance(single, Tree) and single.data == "var":
                checks.append((_name(single), lambda known, it=iterable: self.range_bound(it, known)))
                return
            bad.update(names)

        stack = [root]
        while stack:
            node = stack.pop()
            if not isinstance(node, Tree):
                continue
            d = node.data
            if d == "assign":
                children = [c for c in node.children if c is not None]
                value = children[-1]
                for target in children[:-1]:
                    if isinstance(target, Tree) and target.data == "var":
                        checks.append((_name(target), value_check(value)))
                    else:
                        bad.update(narrowing.target_names(target))
            elif d == "annassign":
                children = [c for c in node.children if isinstance(c, Tree)]
                target = children[0] if children else None
                value = children[2] if len(children) > 2 else None
                if isinstance(target, Tree) and target.data == "var":
                    checks.append((_name(target), value_check(value) if value is not None else (lambda known: 0)))
                else:
                    bad.update(narrowing.target_names(target))
            elif d == "augassign":
                target = node.children[0]
                op = str(node.children[1].children[0]) if isinstance(node.children[1], Tree) and node.children[1].children \
                    else str(node.children[1])
                value = node.children[2] if len(node.children) > 2 else None
                if isinstance(target, Tree) and target.data == "var":
                    name = _name(target)
                    binary = {"+=": "+", "-=": "-", "*=": "*", "//=": "//", "%=": "%"}.get(op)
                    if binary is not None and value is not None:
                        # ``x += e`` stores ``x + e``.
                        kind = "arith_expr" if binary in {"+", "-"} else "term"
                        expr = Tree(kind, [Tree("var", [Tree("name", [Token("NAME", name)])]),
                                           Token("OP", binary), value])
                        checks.append((name, value_check(expr)))
                    else:
                        bad.add(name)
            elif d == "inc_stmt":
                pass  # ``i++`` only raises the value
            elif d in {"for_stmt", "comp_for"} and len(node.children) >= 2:
                for_target(node.children[0], node.children[1])
            elif d in narrowing.BINDING_NODES:
                # Every other binding (parameters, ``i--``, destructuring,
                # ``with`` / ``catch`` / pattern names, ...) is unknown.
                names = narrowing.direct_bindings(node)
                bad |= names
                if d == "funcdef":
                    funcdefs.append(node)
                elif d == "nonlocal_stmt":
                    nonlocals |= names
            stack.extend([c for c in node.children if isinstance(c, Tree)])
        return checks, bad, funcdefs, nonlocals

    def known_bounds(self, root: Tree, excluded: Set[str], scan=None) -> Bounds:
        """Lower bounds of the locals of the function ``root``: the greatest
        fixpoint of ``bound(x) = min(bound of each value assigned to x)``,
        widened to unknown for a bound that keeps decreasing (``i -= 1``)."""
        checks, bad = (scan or self.bindings(root))[:2]
        by_name: Dict[str, List[_Check]] = {}
        for name, check in checks:
            by_name.setdefault(name, []).append(check)
        candidates = set(by_name) - excluded - bad
        known: Bounds = {name: _INF for name in candidates}
        for _attempt in range(8):
            changed: Set[str] = set()
            for _round in range(16):
                changed = set()
                for name in list(known):
                    values = [check(known) for check in by_name[name]]
                    if any(v is None for v in values):
                        del known[name]
                        changed.add(name)
                        continue
                    new = min(values)
                    if new < known[name]:
                        known[name] = new
                        changed.add(name)
                if not changed:
                    break
            # Widen a bound that keeps decreasing (``i -= 1``) to unknown,
            # and drop names only ever assigned from themselves (no
            # initial value bounds them); then settle the rest again.
            dropped = changed | {name for name, bound in known.items() if bound == _INF}
            if not dropped:
                return known
            for name in dropped:
                known.pop(name, None)
        return {}


def index_nonneg(node, known: Dict[str, float], shadowed_builtins: Set[str] = frozenset()) -> bool:
    """Whether the index expression ``node`` is provably non-negative given
    the lower bounds ``known`` of the enclosing function's locals."""
    return _Analysis(set(shadowed_builtins)).nonneg(node, known)


def mark_nonnegative_locals(tree: Tree, shadowed_builtins: Set[str] = frozenset()) -> Set[str]:
    """Store on every function and method (``_lam_index_bounds``) the lower
    bounds of the locals of its top-level function (nested functions and
    lambdas share their enclosing function's bounds). Returns the names some
    nested function rebinds with ``nonlocal`` (found on the same walk; the
    isinstance narrowing never narrows them)."""
    nonlocals: Set[str] = set()
    if not isinstance(tree, Tree):
        return nonlocals
    analysis = _Analysis(set(shadowed_builtins))
    module_vars = narrowing.module_variable_names(tree)
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in node.children:
            if not isinstance(child, Tree):
                continue
            if child.data == "funcdef":
                scan = analysis.bindings(child)
                bounds = dict(analysis.known_bounds(child, module_vars, scan))
                for funcdef in scan[2]:
                    funcdef._lam_index_bounds = bounds
                nonlocals |= scan[3]
                continue
            stack.append(child)
    return nonlocals
