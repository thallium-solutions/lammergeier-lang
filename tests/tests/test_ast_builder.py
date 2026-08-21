#!/usr/bin/env python3
"""Tests for the canonical declaration AST builder."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.ast_builder import build_module  # noqa: E402
from compiler.ast_nodes import ClassDecl, FuncDecl, ImportDecl, InterfaceDecl, VarDecl  # noqa: E402
from compiler.lammergeier import create_parser, preprocess_for_parse  # noqa: E402
from compiler.modules import module_facts_from_ast  # noqa: E402


SOURCE = """const SCALE = 2
func add(x: int, y: int = 1) -> int {
    return x + y
}

class Box {
    value: int = 0

    static func make(v: int) -> Box {
        return Box()
    }

    func get(self) -> int {
        return self.value
    }
}
"""

IMPORT_INTERFACE_SOURCE = """from helper import Thing as LocalThing, build
import other as o

interface Renderable {
    func render(label: str) -> str;
}
"""


def _module(source: str = SOURCE, path: Path | None = None):
    parsed = create_parser().parse(preprocess_for_parse(source).source)
    return build_module(parsed, path=path)


def test_decl_ast_collects_top_level_declarations() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.lam"
        module = _module(path=path)
        names = [decl.name for decl in module.body]
        assert names == ["SCALE", "add", "Box"], names
        const = module.body[0]
        assert isinstance(const, VarDecl)
        assert const.is_const
        assert const.span.line == 1
        func = module.body[1]
        assert isinstance(func, FuncDecl)
        assert [p.name for p in func.params] == ["x", "y"]
        assert [p.type_ref.name for p in func.params if p.type_ref] == ["int", "int"]
        assert func.params[1].has_default
        assert func.return_type is not None
        assert func.return_type.name == "int"
        assert func.span.line == 2
    print("PASS: declaration AST collects top-level declarations")


def test_decl_ast_collects_class_members() -> None:
    module = _module()
    cls = module.body[2]
    assert isinstance(cls, ClassDecl)
    assert cls.span is not None
    assert cls.span.line == 6
    assert [field.name for field in cls.fields] == ["value"]
    assert cls.fields[0].type_ref is not None
    assert cls.fields[0].type_ref.name == "int"
    assert [method.name for method in cls.methods] == ["make", "get"]
    assert cls.methods[0].is_static
    assert not cls.methods[1].is_static
    assert cls.methods[0].parent == "Box"
    assert cls.methods[0].return_type is not None
    assert cls.methods[0].return_type.name == "Box"
    print("PASS: declaration AST collects class fields and methods")


def test_module_facts_can_be_built_from_ast() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.lam"
        module = _module(path=path)
        facts = module_facts_from_ast(path, module)
        assert set(facts.exports) == {"SCALE", "add", "Box"}, facts.exports
        assert facts.exports["SCALE"].kind == "const"
        assert facts.exports["add"].kind == "function"
        assert facts.exports["Box"].kind == "class"
        assert facts.exports["add"].line == 2
    print("PASS: module facts can be built from declaration AST")


def test_decl_ast_collects_imports_and_interface_methods() -> None:
    module = _module(IMPORT_INTERFACE_SOURCE)
    imports = [decl for decl in module.body if isinstance(decl, ImportDecl)]
    assert len(imports) == 2, imports
    assert imports[0].module == "helper"
    assert [b.name for b in imports[0].bindings] == ["LocalThing", "build"]
    assert imports[0].bindings[0].imported == "Thing"
    assert imports[0].bindings[0].alias == "LocalThing"
    assert imports[1].module == "other"
    assert imports[1].bindings[0].name == "o"
    iface = module.body[2]
    assert isinstance(iface, InterfaceDecl)
    assert iface.name == "Renderable"
    assert [method.name for method in iface.methods] == ["render"]
    assert iface.methods[0].params[0].type_ref is not None
    assert iface.methods[0].params[0].type_ref.name == "str"
    assert iface.methods[0].return_type is not None
    assert iface.methods[0].return_type.name == "str"
    print("PASS: declaration AST collects imports and interface methods")


def test_decl_ast_preserves_generic_type_refs() -> None:
    module = _module("""func consume(rows: list[dict[str, list[int]]]) -> dict[str, list[int]] {
    return {"empty": []}
}
""")
    func = module.body[0]
    assert isinstance(func, FuncDecl)
    assert func.params[0].type_ref is not None
    assert func.params[0].type_ref.name == "list[dict[str, list[int]]]"
    assert func.return_type is not None
    assert func.return_type.name == "dict[str, list[int]]"
    print("PASS: declaration AST preserves nested generic type refs")


def test_module_facts_include_ast_reexports() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "imports.lam"
        module = _module(IMPORT_INTERFACE_SOURCE, path=path)
        facts = module_facts_from_ast(path, module)
        assert {"LocalThing", "build", "o", "Renderable"} <= set(facts.exports), facts.exports
        assert facts.exports["LocalThing"].kind == "import"
        assert facts.exports["Renderable"].kind == "interface"
    print("PASS: module facts include AST import re-exports")


def main() -> int:
    tests = [
        test_decl_ast_collects_top_level_declarations,
        test_decl_ast_collects_class_members,
        test_module_facts_can_be_built_from_ast,
        test_decl_ast_collects_imports_and_interface_methods,
        test_decl_ast_preserves_generic_type_refs,
        test_module_facts_include_ast_reexports,
    ]
    for test in tests:
        test()
    print(f"\nAST-builder results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
