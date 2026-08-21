#!/usr/bin/env python3
"""Static contracts for typed expression lowering.

These tests are intentionally narrow. They do not try to prove every
``_expr_to_go`` call is wrong or right; instead they guard the known
typed-context entry points so future refactors keep routing values through
``_typed_value_to_go`` before generated Go sees them.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def _function_source(path: Path, name: str) -> str:
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(module):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
    raise AssertionError(f"{name} not found in {path}")


def _assert_contains(path: str, func: str, needles: list[str]) -> None:
    source = _function_source(ROOT / path, func)
    missing = [needle for needle in needles if needle not in source]
    assert not missing, f"{path}:{func} missing {missing}"


def test_typed_value_to_go_is_the_context_boundary() -> None:
    _assert_contains(
        "compiler/visitors/expressions.py",
        "_typed_value_to_go",
        [
            "_typed_list_literal_to_go",
            "_typed_dict_literal_to_go",
            "_typed_set_literal_to_go",
            "_expected_expr_go_type=go_type",
            "_propagate_cast_hint=go_type",
        ],
    )
    print("PASS: typed value lowering remains the contextual boundary")


def test_call_arguments_are_lowered_with_parameter_types() -> None:
    _assert_contains(
        "compiler/visitors/helpers.py",
        "_collect_call_args_for_func",
        ["_transpile_argvalue_with_type", "_param_go_type_at"],
    )
    _assert_contains(
        "compiler/visitors/helpers.py",
        "_apply_call_kwargs",
        ["_typed_value_to_go(entry, target_type)"],
    )
    _assert_contains(
        "compiler/visitors/helpers.py",
        "_transpile_argvalue_with_type",
        ["_typed_value_to_go(inner, go_type)", 'f"[]{go_type}"'],
    )
    _assert_contains(
        "compiler/visitors/expressions.py",
        "_planned_call_args",
        ["_collect_call_args_for_func"],
    )
    print("PASS: call-site lowering keeps parameter type context")


def test_statement_typed_contexts_use_typed_lowering() -> None:
    for func in ["_visit_annassign", "_visit_const_stmt", "_visit_assign", "_visit_return_stmt"]:
        _assert_contains(
            "compiler/visitors/statements.py",
            func,
            ["_typed_value_to_go"],
        )
    print("PASS: statement typed contexts use typed expression lowering")


def main() -> int:
    tests = [
        test_typed_value_to_go_is_the_context_boundary,
        test_call_arguments_are_lowered_with_parameter_types,
        test_statement_typed_contexts_use_typed_lowering,
    ]
    for test in tests:
        test()
    print(f"\nlowering contracts: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
