#!/usr/bin/env python3
"""Tests for the shared isinstance-narrowing analysis and the
non-negative index analysis (pure parse-tree passes)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from lark import Tree  # noqa: E402

from compiler import narrowing  # noqa: E402
from compiler.index_bounds import index_nonneg, mark_nonnegative_locals  # noqa: E402
from compiler.lammergeier import create_parser, preprocess_for_parse  # noqa: E402


def _parse(source: str) -> Tree:
    return create_parser().parse(preprocess_for_parse(source).source)


def _find(tree: Tree, data: str) -> list:
    return [node for node in tree.iter_subtrees_topdown() if node.data == data]


def _fact_names(facts) -> list:
    return [(name, "None" if target is narrowing.NOT_NONE else narrowing.type_target_name(target))
            for name, target in facts]


def test_condition_facts() -> None:
    tree = _parse(
        "func f(x: any, y: any) {\n"
        "    if isinstance(x, A) and not isinstance(y, None) { pass }\n"
        "    if not isinstance(x, A) or isinstance(y, B) { pass }\n"
        "    if isinstance(self.f, A) or isinstance(x, B) { pass }\n"
        "}\n"
    )
    first, second, third = _find(tree, "if_stmt")
    assert _fact_names(narrowing.condition_facts(first.children[0], True)) == [("x", "A"), ("y", "None")]
    assert narrowing.condition_facts(first.children[0], False) == []
    # A false ``or`` means every operand is false.
    assert _fact_names(narrowing.condition_facts(second.children[0], False)) == [("x", "A")]
    assert narrowing.condition_facts(second.children[0], True) == []
    # Fields are never subjects; a true ``or`` establishes nothing.
    assert narrowing.condition_facts(third.children[0], True) == []
    print("PASS: condition facts follow not / and / or")


def test_guard_facts_need_exiting_branches() -> None:
    tree = _parse(
        "func f(x: any) -> int {\n"
        "    for v in [1] {\n"
        "        if not isinstance(x, A) { continue }\n"
        "        if not isinstance(x, B) { print(1) }\n"
        "        if isinstance(x, C) { return 1 } elif not isinstance(x, D) { raise ValueError(\"x\") }\n"
        "    }\n"
        "    return 0\n"
        "}\n"
    )
    exits, no_exit, chain = _find(tree, "if_stmt")
    assert _fact_names(narrowing.guard_facts(exits)) == [("x", "A")]
    assert narrowing.guard_facts(no_exit) == []
    assert _fact_names(narrowing.guard_facts(chain)) == [("x", "D")]
    print("PASS: guards narrow only when every branch exits")


def test_blocked_names() -> None:
    tree = _parse(
        "func f(x: any, y: any, z: any) {\n"
        "    if isinstance(x, A) {\n"
        "        x = 1\n"
        "        g = lambda: y\n"
        "        z.append(3)\n"
        "        for q in [1] { pass }\n"
        "    }\n"
        "}\n"
    )
    suite = _find(tree, "if_stmt")[0].children[1]
    assigned = narrowing.assigned_names([suite])
    assert {"x", "g", "q"} <= assigned and "y" not in assigned and "z" not in assigned
    assert "z" in narrowing.mutated_names([suite])
    print("PASS: assigned / mutated names of a region")


def test_module_variables_skip_function_bodies() -> None:
    tree = _parse(
        "count: int = 0\n"
        "for i in range(3) { total = i }\n"
        "func f(local: int) -> int {\n"
        "    inner = local\n"
        "    return inner\n"
        "}\n"
    )
    names = narrowing.module_variable_names(tree)
    assert {"count", "i", "total"} <= names
    assert "inner" not in names and "local" not in names
    print("PASS: module variables exclude function locals")


def _bounds(source: str) -> dict:
    tree = _parse(source)
    mark_nonnegative_locals(tree)
    funcs = _find(tree, "funcdef")
    return funcs[0]._lam_index_bounds


def _known(source: str) -> set:
    return {name for name, bound in _bounds(source).items() if bound >= 0}


def test_nonneg_locals_fixpoint() -> None:
    known = _known(
        "func f(p: int, xs: list[int]) {\n"
        "    a: int = 0\n"
        "    a += 1\n"
        "    b = len(xs) // 2\n"
        "    c = a + b * 3\n"
        "    d = 0\n"
        "    d = d - 1\n"
        "    e = p\n"
        "    for i in range(len(xs)) { pass }\n"
        "    for j in range(p, 3) { pass }\n"
        "    for k in range(len(xs) - 1, -1, -1) { pass }\n"
        "    for m in range(3, -2, -1) { pass }\n"
        "    for n, _ in enumerate(xs) { pass }\n"
        "    w = 0\n"
        "    while w < 3 { w++ }\n"
        "    v = 5\n"
        "    v--\n"
        "    u = 0\n"
        "    u, t = 1, 2\n"
        "    r = s = 0\n"
        "    s = -1\n"
        "    loop1 = 0\n"
        "    loop2 = loop1\n"
        "    loop1 = loop2 + 1\n"
        "}\n"
    )
    assert {"a", "b", "c", "i", "k", "n", "w", "r", "loop1", "loop2"} <= known, known
    for name in ("p", "d", "e", "j", "m", "v", "u", "t", "s", "xs"):
        assert name not in known, name
    print("PASS: non-negative locals (greatest fixpoint over assignments)")


def test_lower_bounds() -> None:
    bounds = _bounds(
        "func f(xs: list[int]) {\n"
        "    for i in range(1, len(xs)) { pass }\n"
        "    for j in range(5, 0, -1) { pass }\n"
        "    k = 2\n"
        "    k += 3\n"
        "    m = 3\n"
        "    m -= 1\n"
        "    while m > 0 { m -= 1 }\n"
        "}\n"
    )
    assert bounds.get("i") == 1 and bounds.get("j") == 1 and bounds.get("k") == 2, bounds
    assert "m" not in bounds, bounds  # keeps decreasing: unknown
    print("PASS: lower bounds (range starts, widening of decrements)")


def test_globals_and_shadowed_builtins_are_not_trusted() -> None:
    source = (
        "pos = 0\n"
        "func f(xs: list[int]) -> int {\n"
        "    pos = 1\n"
        "    for i in range(3) { pass }\n"
        "    return xs[pos]\n"
        "}\n"
    )
    assert "pos" not in _known(source) and "i" in _known(source)
    tree = _parse("func f() { for i in range(3) { pass } }\n")
    mark_nonnegative_locals(tree, {"range"})
    assert "i" not in _find(tree, "funcdef")[0]._lam_index_bounds
    print("PASS: globals and shadowed builtins are not trusted")


def test_index_expressions() -> None:
    tree = _parse(
        "func f(xs: list[int], i: int) {\n"
        "    print(xs[0], xs[len(xs) - 1], xs[i + 1], xs[min(i, 2)], xs[max(i, 0)], xs[abs(i)], xs[i % 3])\n"
        "    print(xs[i // 2], xs[i % i], xs[len(xs) // 2], xs[-3 % len(xs)], xs[len(xs) % i])\n"
        "}\n"
    )
    indexes = [node.children[1] for node in _find(tree, "getitem")]
    results = [index_nonneg(node, {"k": 0}) for node in indexes]
    # Python's ``%`` takes the sign of the divisor: ``i % 3`` is never
    # negative, ``i // 2`` and ``len(xs) % i`` may be.
    assert results == [True, False, False, False, True, True, True, False, False, True, True, False], results
    assert index_nonneg(indexes[2], {"i": 0}) and index_nonneg(indexes[6], {"i": 0})
    assert index_nonneg(indexes[7], {"i": 0}) and index_nonneg(indexes[8], {"i": 0})
    assert index_nonneg(indexes[2], {"i": -1}) and not index_nonneg(indexes[2], {"i": -2})
    print("PASS: non-negative index expressions")


if __name__ == "__main__":
    test_condition_facts()
    test_guard_facts_need_exiting_branches()
    test_blocked_names()
    test_module_variables_skip_function_bodies()
    test_nonneg_locals_fixpoint()
    test_lower_bounds()
    test_globals_and_shadowed_builtins_are_not_trusted()
    test_index_expressions()
