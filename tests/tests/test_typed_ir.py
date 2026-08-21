#!/usr/bin/env python3
"""Tests for the incremental typed IR builder."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.lammergeier import create_parser, preprocess_for_parse  # noqa: E402
from compiler.typed_ir import TypedClass, TypedFunction, TypedVariable, build_typed_module  # noqa: E402
from compiler.typesys import FuncType, ListType, NamedType, render_type  # noqa: E402


SOURCE = """func add(x: int, y: int = 1) -> int {
    total: int = x
    label: str = "sum"
    values: list[int] = [1, 2, 3]
    inferred = add(total, y)
    return inferred
}

class Box {
    value: int = 0

    func get(self) -> int {
        current: int = self.value
        return current
    }
}
"""


def _typed_module(source: str = SOURCE):
    parsed = create_parser().parse(preprocess_for_parse(source).source)
    return build_typed_module(parsed)


def test_typed_ir_collects_function_signature_and_locals() -> None:
    module = _typed_module()
    func = module.body[0]
    assert isinstance(func, TypedFunction)
    assert func.name == "add"
    assert func.signature == FuncType((NamedType("int"), NamedType("int")), NamedType("int"))
    assert module.function_signatures["add"] == func.signature

    locals_by_name = {local.name: local for local in func.locals}
    assert set(locals_by_name) == {"total", "label", "values", "inferred"}, locals_by_name
    assert locals_by_name["total"].type == NamedType("int")
    assert locals_by_name["label"].type == NamedType("str")
    assert locals_by_name["values"].type == ListType(NamedType("int"))
    assert locals_by_name["values"].initializer is not None
    assert locals_by_name["values"].initializer.expected_type == ListType(NamedType("int"))
    assert locals_by_name["inferred"].type == NamedType("int")
    assert locals_by_name["inferred"].initializer is not None
    assert locals_by_name["inferred"].initializer.name == "add"
    assert locals_by_name["inferred"].initializer.expected_type is None
    assert render_type(locals_by_name["values"].type) == "list[int]"
    print("PASS: typed IR collects resolved function signatures and local variable types")


def test_typed_ir_records_expected_initializer_contexts() -> None:
    module = _typed_module("""func f() {
    nums: list[int] = [1, 2]
    names: dict[str, str] = {"a": "A"}
    nums = [3, 4]
    const ports: list[int] = [5432, 6380]
}
""")
    func = module.body[0]
    assert isinstance(func, TypedFunction)
    locals_by_name = {local.name: local for local in func.locals}
    assert locals_by_name["nums"].initializer is not None
    assert locals_by_name["nums"].initializer.expected_type == ListType(NamedType("int"))
    assert locals_by_name["names"].initializer is not None
    assert render_type(locals_by_name["names"].initializer.expected_type) == "dict[str, str]"
    assert locals_by_name["ports"].initializer is not None
    assert locals_by_name["ports"].initializer.expected_type == ListType(NamedType("int"))
    print("PASS: typed IR records expected types for contextual initializers")


def test_typed_ir_records_expected_call_argument_contexts() -> None:
    module = _typed_module("""func sumValues(values: list[int]) -> int {
    return 0
}

func f() {
    total = sumValues([1, 2])
}
""")
    func = module.body[1]
    assert isinstance(func, TypedFunction)
    local = func.locals[0]
    assert local.name == "total"
    assert local.initializer is not None
    call = local.initializer
    assert call.name == "sumValues"
    assert call.args[0].expected_type == ListType(NamedType("int"))
    print("PASS: typed IR records expected types for known call arguments")


def test_typed_ir_collects_class_methods_and_fields() -> None:
    module = _typed_module()
    cls = module.body[1]
    assert isinstance(cls, TypedClass)
    assert cls.name == "Box"
    assert len(cls.fields) == 1
    field = cls.fields[0]
    assert isinstance(field, TypedVariable)
    assert field.name == "value"
    assert field.type == NamedType("int")
    assert len(cls.methods) == 1
    method = cls.methods[0]
    assert method.name == "get"
    assert method.parent == "Box"
    assert method.return_type == NamedType("int")
    assert module.function_signatures["Box.get"] == method.signature
    method_locals = {local.name: local for local in method.locals}
    assert method_locals["current"].type == NamedType("int")
    print("PASS: typed IR collects class fields and method signatures")


def main() -> int:
    tests = [
        test_typed_ir_collects_function_signature_and_locals,
        test_typed_ir_records_expected_initializer_contexts,
        test_typed_ir_records_expected_call_argument_contexts,
        test_typed_ir_collects_class_methods_and_fields,
    ]
    for test in tests:
        test()
    print(f"\ntyped IR results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
