#!/usr/bin/env python3
"""Exhaustive operator-grouping check against Python's own evaluation.

Lam's grammar drops parentheses from the tree (``(b + c)`` is just the
``arith_expr`` node), so the Go emitter must put them back wherever Go's
precedence or left-to-right associativity would otherwise regroup the
operands. Go's precedence table also differs from Python's (``<<`` / ``&``
bind like ``*`` in Go, but looser than ``+`` in Python), so the mapping is
not the identity.

This test generates, for every pair of binary operators of each operand
type, the shapes ``x op1 (y op2 z)``, ``(x op1 y) op2 z`` and
``x op1 y op2 z`` plus unary operators, ternaries, comparisons and
boolean operators applied to groups, compiles them as one Lam program,
runs it, and compares each printed value with Python's ``eval`` of the
exact same source text (Lam's operators follow Python's semantics,
including ``/``, ``//`` and ``%`` since 3.0).
"""

from __future__ import annotations

import ast
import math
import os
import subprocess
import sys
import tempfile
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"

INT_VARS = {"a": 13, "b": 5, "c": 3, "d": -7, "e": 2}
FLOAT_VARS = {"fx": 7.5, "fy": 2.25, "fz": -1.5}
BOOL_VARS = {"p": True, "q": False, "r": True}
STR_VARS = {"s": "ab", "t": "cd", "u": "ef"}

INT_OPS = ["+", "-", "*", "/", "//", "%", "**", "<<", ">>", "&", "|", "^"]
FLOAT_OPS = ["+", "-", "*", "/", "//", "%", "**"]
CMP_OPS = ["<", "<=", ">", ">=", "==", "!="]
BOOL_OPS = ["and", "or"]
BIT_OPS = {"<<", ">>", "&", "|", "^"}


def _arith_type(op: str, left: str, right: str) -> str | None:
    """Lam's static type of ``left op right`` (``None`` when ill-typed)."""
    if left not in {"int", "float"} or right not in {"int", "float"}:
        return None
    if op in BIT_OPS:
        return "int" if left == right == "int" else None
    if op in {"/", "**"}:
        return "float"
    return "int" if left == right == "int" else "float"


def _int_cases() -> list[tuple[str, str]]:
    cases = []
    triples = [("a", "b", "c"), ("d", "c", "e"), ("a", "e", "d")]
    for op1, op2 in product(INT_OPS, INT_OPS):
        for x, y, z in triples:
            cases.append((f"{x} {op1} ({y} {op2} {z})", "num"))
            cases.append((f"({x} {op1} {y}) {op2} {z}", "num"))
            cases.append((f"{x} {op1} {y} {op2} {z}", "num"))
        # Literal operands take other lowering paths (constant folding,
        # power-of-two shifts and masks).
        cases.append((f"100 {op1} (7 {op2} 3)", "num"))
        cases.append((f"a {op1} (b {op2} 4)", "num"))
        cases.append((f"d {op1} (2 {op2} c)", "num"))
        cases.append((f"(a {op1} 8) {op2} c", "num"))
    for op in INT_OPS:
        cases.append((f"-(b {op} c)", "num"))
        cases.append((f"~(b {op} c)", "num"))
        cases.append((f"+(d {op} e)", "num"))
        cases.append((f"a - -(b {op} c)", "num"))
        cases.append((f"a * -(b {op} c)", "num"))
        cases.append((f"-(-(a {op} e))", "num"))
        cases.append((f"a - (b if p else c) {op} e", "num"))
        cases.append((f"(a if q else b) {op} (c - e)", "num"))
        cases.append((f"a {op} (b - c if p else e)", "num"))
        cases.append((f"-(a if p else b) {op} c", "num"))
    return cases


def _float_cases() -> list[tuple[str, str]]:
    cases = []
    for op1, op2 in product(FLOAT_OPS, FLOAT_OPS):
        cases.append((f"fx {op1} (fy {op2} fz)", "num"))
        cases.append((f"(fx {op1} fy) {op2} fz", "num"))
        cases.append((f"fx {op1} fy {op2} fz", "num"))
        # Mixed int / float operands widen to float.
        cases.append((f"fx {op1} (b {op2} c)", "num"))
        cases.append((f"a {op1} (fy {op2} c)", "num"))
    for op in FLOAT_OPS:
        cases.append((f"-(fx {op} fy)", "num"))
        cases.append((f"fz - -(fx {op} fy)", "num"))
    return cases


def _cmp_cases() -> list[tuple[str, str]]:
    cases = []
    for cmp, op in product(CMP_OPS, INT_OPS):
        cases.append((f"a {cmp} (b {op} c)", "bool"))
        cases.append((f"(a {op} b) {cmp} c", "bool"))
        cases.append((f"a {op} b {cmp} c", "bool"))
        cases.append((f"d {cmp} -(b {op} c)", "bool"))
    for cmp1, cmp2 in product(CMP_OPS, CMP_OPS):
        cases.append((f"(a {cmp1} b) == (c {cmp2} e)", "bool"))
        cases.append((f"(a {cmp1} b) != (c {cmp2} e)", "bool"))
        cases.append((f"p == (a {cmp1} b)", "bool"))
        cases.append((f"a {cmp1} b {cmp2} c", "bool"))
        cases.append((f"not a {cmp1} b", "bool"))
        cases.append((f"not (a {cmp1} b) == (c {cmp2} e)", "bool"))
    for cmp in CMP_OPS:
        cases.append((f"s {cmp} (t + u)", "bool"))
        cases.append((f"(s + t) {cmp} u", "bool"))
        cases.append((f"fx {cmp} (fy - fz)", "bool"))
        cases.append((f"fx - fy {cmp} fz", "bool"))
    return cases


def _bool_cases() -> list[tuple[str, str]]:
    cases = []
    for op1, op2 in product(BOOL_OPS, BOOL_OPS):
        for x, y, z in [("p", "q", "r"), ("q", "r", "p"), ("q", "q", "r"), ("r", "q", "q")]:
            cases.append((f"{x} {op1} ({y} {op2} {z})", "bool"))
            cases.append((f"({x} {op1} {y}) {op2} {z}", "bool"))
            cases.append((f"{x} {op1} {y} {op2} {z}", "bool"))
            cases.append((f"not ({x} {op1} {y}) {op2} {z}", "bool"))
            cases.append((f"{x} == ({y} {op1} {z})", "bool"))
            cases.append((f"({x} {op1} {y}) != {z}", "bool"))
            cases.append((f"{x} {op1} (a < b) {op2} {z}", "bool"))
    for op in BOOL_OPS:
        cases.append((f"not (p {op} q)", "bool"))
        cases.append((f"not p {op} q", "bool"))
        cases.append((f"(p if q else r) {op} q", "bool"))
        cases.append((f"p {op} (q if r else p)", "bool"))
    return cases


def _str_cases() -> list[tuple[str, str]]:
    return [
        ("s + (t + u)", "str"),
        ("(s + t) + u", "str"),
        ("s + t * e", "str"),
        ("(s + t) * e", "str"),
        ("s * (e + 1)", "str"),
        ("s * (c - e)", "str"),
        ("s + (t if p else u)", "str"),
        ("(s if q else t) + u", "str"),
        ("s + (t if q else u) + s", "str"),
    ]


_AST_OPS = {
    ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//",
    ast.Mod: "%", ast.Pow: "**", ast.LShift: "<<", ast.RShift: ">>",
    ast.BitAnd: "&", ast.BitOr: "|", ast.BitXor: "^",
}


def lam_type(node, env_types: dict) -> str | None:
    """Lam's static type of a generated expression (``None`` when Lam
    rejects it), following Python's grouping of the same text."""
    if isinstance(node, ast.Expression):
        return lam_type(node.body, env_types)
    if isinstance(node, ast.Name):
        return env_types.get(node.id)
    if isinstance(node, ast.Constant):
        return {int: "int", float: "float", str: "str"}.get(type(node.value))
    if isinstance(node, ast.BinOp):
        left, right = lam_type(node.left, env_types), lam_type(node.right, env_types)
        op = _AST_OPS[type(node.op)]
        if left == "str" or right == "str":
            if op == "+" and left == right == "str":
                return "str"
            if op == "*" and {left, right} == {"str", "int"}:
                return "str"
            return None
        return _arith_type(op, left, right)
    if isinstance(node, ast.UnaryOp):
        inner = lam_type(node.operand, env_types)
        if isinstance(node.op, ast.Not):
            return "bool" if inner == "bool" else None
        if isinstance(node.op, ast.Invert):
            return "int" if inner == "int" else None
        return inner if inner in {"int", "float"} else None
    if isinstance(node, ast.BoolOp):
        types = [lam_type(v, env_types) for v in node.values]
        return "bool" if all(t == "bool" for t in types) else None
    if isinstance(node, ast.Compare):
        types = [lam_type(v, env_types) for v in [node.left, *node.comparators]]
        if any(t is None for t in types):
            return None
        numeric = all(t in {"int", "float"} for t in types)
        if not (numeric or all(t == types[0] for t in types)):
            return None
        if types[0] == "bool" and any(not isinstance(o, (ast.Eq, ast.NotEq)) for o in node.ops):
            return None
        return "bool"
    if isinstance(node, ast.IfExp):
        if lam_type(node.test, env_types) != "bool":
            return None
        body, orelse = lam_type(node.body, env_types), lam_type(node.orelse, env_types)
        return body if body == orelse else None
    return None


def build_cases() -> list[tuple[str, str, object]]:
    env = {**INT_VARS, **FLOAT_VARS, **BOOL_VARS, **STR_VARS}
    env_types = {name: type(value).__name__ for name, value in env.items()}
    seen = set()
    out = []
    for expr, kind in _int_cases() + _float_cases() + _cmp_cases() + _bool_cases() + _str_cases():
        if expr in seen:
            continue
        seen.add(expr)
        if lam_type(ast.parse(expr, mode="eval"), env_types) is None:
            continue
        try:
            value = eval(expr, {}, dict(env))  # noqa: S307 - test-generated text
        except (ArithmeticError, ValueError, TypeError):
            continue
        if isinstance(value, complex):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if abs(value) > 2 ** 52 or not math.isfinite(value):
                continue
            if isinstance(value, float) and value != 0 and abs(value) < 1e-9:
                continue
        out.append((expr, lam_type(ast.parse(expr, mode="eval"), env_types), value))
    return out


def _lam_program(cases, chunk: int = 150) -> str:
    decls = []
    for name, value in INT_VARS.items():
        decls.append(f"    {name}: int = {value}")
    for name, value in FLOAT_VARS.items():
        decls.append(f"    {name}: float = {value}")
    for name, value in BOOL_VARS.items():
        decls.append(f"    {name}: bool = {'true' if value else 'false'}")
    for name, value in STR_VARS.items():
        decls.append(f'    {name}: str = "{value}"')
    decls.extend(f"    _ = {name}" for name in [*INT_VARS, *FLOAT_VARS, *BOOL_VARS, *STR_VARS])
    statements = [[f"    print({expr})"] for expr, _lam_type, _value in cases]
    # The same expressions in typed contexts (a declaration and a typed
    # function argument), which lower through the typed-value path.
    statements += [
        [f"    v{i}: {lam_type_name} = {expr}", f"    print(v{i})"]
        for i, (expr, lam_type_name, _value) in enumerate(cases)
    ]
    statements += [[f"    show_{lam_type_name}({expr})"] for expr, lam_type_name, _value in cases]
    lines = []
    parts = 0
    for start in range(0, len(statements), chunk):
        lines.append(f"func part{parts}() {{")
        lines.extend(decls)
        for group in statements[start:start + chunk]:
            lines.extend(group)
        lines.append("}")
        parts += 1
    for lam_type_name in ("int", "float", "bool", "str"):
        lines.append(f"func show_{lam_type_name}(v: {lam_type_name}) {{")
        lines.append("    print(v)")
        lines.append("}")
    lines.append("func main() {")
    lines.extend(f"    part{i}()" for i in range(parts))
    lines.append("}")
    return "\n".join(lines) + "\n"


def _matches(text: str, value) -> bool:
    if isinstance(value, bool):
        return text == ("true" if value else "false")
    if isinstance(value, str):
        return text == value
    try:
        got = float(text)
    except ValueError:
        return False
    if isinstance(value, int) and text.lstrip("-").isdigit():
        return int(text) == value
    return math.isclose(got, float(value), rel_tol=1e-9, abs_tol=1e-9)


def test_operator_grouping_matches_python() -> None:
    cases = build_cases()
    assert len(cases) > 1000, len(cases)
    with tempfile.TemporaryDirectory(prefix="lam_prec_") as tmp:
        src = Path(tmp) / "prec.lam"
        src.write_text(_lam_program(cases), encoding="utf-8")
        binary = Path(tmp) / "prec"
        build = subprocess.run(
            [sys.executable, str(COMPILER), str(src), "-o", str(binary), "--no-cache"],
            capture_output=True, text=True, timeout=600, cwd=tmp,
        )
        assert build.returncode == 0, build.stderr[-4000:]
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
        assert run.returncode == 0, run.stderr[-4000:]
    got = run.stdout.split("\n")
    mismatches = []
    for context in range(3):
        for i, (expr, _lam_type, value) in enumerate(cases):
            line = context * len(cases) + i
            text = got[line] if line < len(got) else "<missing>"
            if not _matches(text, value):
                where = ["print", "typed declaration", "typed argument"][context]
                mismatches.append(f"  {expr} ({where}): lam {text!r}, python {value!r}")
    assert not mismatches, (
        f"{len(mismatches)} of {len(cases)} expressions differ from Python:\n"
        + "\n".join(mismatches[:60])
    )
    print(f"PASS: {len(cases)} operator groupings match Python's evaluation")


if __name__ == "__main__":
    os.chdir(ROOT)
    test_operator_grouping_matches_python()
