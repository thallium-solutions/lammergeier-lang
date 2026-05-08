#!/usr/bin/env python3
"""Tests for the canonical Lammergeier type model."""

from __future__ import annotations

import sys
from pathlib import Path

from lark import Tree

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.lammergeier import create_parser, preprocess_for_parse  # noqa: E402
from compiler.typesys import (  # noqa: E402
    DictType,
    FuncType,
    GenericType,
    ListType,
    NamedType,
    UnionType,
    is_assignable,
    parse_type,
    render_type,
    type_to_go,
)


SOURCE = """func accepts(
    xs: list[int],
    table: dict[str, list[int]],
    cb: func(int, str) -> bool,
) -> dict[str, list[int]] {
    return {}
}
"""


def test_parse_text_builtin_and_collection_types() -> None:
    assert parse_type("int") == NamedType("int")
    assert type_to_go(parse_type("int")) == "int"

    list_type = parse_type("list[int]")
    assert list_type == ListType(NamedType("int"))
    assert render_type(list_type) == "list[int]"
    assert type_to_go(list_type) == "[]int"

    dict_type = parse_type("dict[str, list[int]]")
    assert dict_type == DictType(NamedType("str"), ListType(NamedType("int")))
    assert render_type(dict_type) == "dict[str, list[int]]"
    assert type_to_go(dict_type) == "map[string][]int"
    print("PASS: text parser handles builtins and collection types")


def test_parse_text_function_and_union_types() -> None:
    func_type = parse_type("func(int, str) -> bool")
    assert func_type == FuncType((NamedType("int"), NamedType("str")), NamedType("bool"))
    assert render_type(func_type) == "func(int, str) -> bool"
    assert type_to_go(func_type) == "func(int, string) bool"

    void_func = parse_type("func()")
    assert void_func == FuncType((), NamedType("None"))
    assert render_type(void_func) == "func()"
    assert type_to_go(void_func) == "func()"

    union_type = parse_type("int | str")
    assert union_type == UnionType((NamedType("int"), NamedType("str")))
    assert render_type(union_type) == "int | str"
    assert type_to_go(union_type) == "interface{}"
    print("PASS: text parser handles function and union types")


def test_go_lowering_preserves_current_generic_rules() -> None:
    assert type_to_go(parse_type("optional[int]")) == "*int"
    assert type_to_go(parse_type("chan[str]")) == "chan string"
    assert type_to_go(parse_type("tuple[int, str]")) == "[]interface{}"
    assert type_to_go(parse_type("Box[int]")) == "interface{}"
    assert type_to_go(parse_type("Box[int]"), generic_classes={"Box"}) == "*Box[int]"
    assert type_to_go(NamedType("T"), generic_names={"T"}) == "T"
    assert type_to_go(NamedType("Reader"), interfaces={"Reader"}) == "Reader"
    assert isinstance(parse_type("Box[int]"), GenericType)
    print("PASS: Go lowering preserves existing generic type rules")


def test_assignability_covers_simple_type_rules() -> None:
    assert is_assignable(parse_type("int"), parse_type("int"))
    assert is_assignable(parse_type("float"), parse_type("int"))
    assert is_assignable(parse_type("str | int"), parse_type("str"))
    assert is_assignable(parse_type("any"), parse_type("dict[str, int]"))
    assert not is_assignable(parse_type("int"), parse_type("str"))
    assert not is_assignable(parse_type("list[int]"), parse_type("list[str]"))
    assert is_assignable(parse_type("dict[str, float]"), parse_type("dict[str, int]"))
    print("PASS: assignability covers simple type rules")


def test_parse_lark_type_nodes() -> None:
    tree = create_parser().parse(preprocess_for_parse(SOURCE).source)
    type_nodes = _type_expr_nodes(tree)
    rendered = [render_type(parse_type(node)) for node in type_nodes]
    assert "list[int]" in rendered, rendered
    assert "dict[str, list[int]]" in rendered, rendered
    assert "func(int, str) -> bool" in rendered, rendered

    dict_node = next(node for node in type_nodes if render_type(parse_type(node)) == "dict[str, list[int]]")
    dict_type = parse_type(dict_node)
    assert dict_type == DictType(NamedType("str"), ListType(NamedType("int")))
    assert type_to_go(dict_type) == "map[string][]int"
    print("PASS: parser type subtrees convert to canonical types")


def _type_expr_nodes(node) -> list[Tree]:
    out: list[Tree] = []
    if isinstance(node, Tree):
        if node.data == "type_expr":
            out.append(node)
        for child in node.children:
            out.extend(_type_expr_nodes(child))
    return out


def main() -> int:
    tests = [
        test_parse_text_builtin_and_collection_types,
        test_parse_text_function_and_union_types,
        test_go_lowering_preserves_current_generic_rules,
        test_assignability_covers_simple_type_rules,
        test_parse_lark_type_nodes,
    ]
    for test in tests:
        test()
    print(f"\ntypesys results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
