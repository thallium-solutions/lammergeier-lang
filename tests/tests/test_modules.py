#!/usr/bin/env python3
"""Tests for workspace module facts and import resolution."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.modules import WorkspaceIndex, module_facts_from_source  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_module_facts_exports_and_imports() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "main.lam"
        source = """
from helper import Thing as LocalThing, build
import other as o

const SCALE = 2
moduleName: str = "widgets"

interface Renderable {
}
class Widget {
}

func make() -> Widget {
    return Widget()
}
""".lstrip()
        facts = module_facts_from_source(path, source)
        assert set(facts.exports) == {
            "LocalThing", "build", "o", "SCALE", "moduleName", "Renderable", "Widget", "make",
        }, facts.exports
        assert facts.exports["Widget"].kind == "class"
        assert facts.exports["Renderable"].kind == "interface"
        assert facts.exports["moduleName"].kind == "variable"
        assert facts.exports["LocalThing"].kind == "import"
        assert len(facts.imports) == 3, facts.imports
        assert facts.imports[0].module == "helper"
        assert facts.imports[0].imported == "Thing"
        assert facts.imports[0].alias == "LocalThing"
        assert facts.imports[2].module == "other"
        assert facts.imports[2].imported is None
    print("PASS: module facts collect exports and imports")


def test_workspace_resolves_local_sibling() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        helper = root / "helper.lam"
        _write(main, "from helper import double\n")
        _write(helper, "func double(x: int) -> int { return x * 2 }\n")
        index = WorkspaceIndex(root)
        assert index.resolve_module(main, "helper") == helper.resolve()
        symbol = index.resolve_import(main, "helper", "double")
        assert symbol is not None
        assert symbol.name == "double"
    print("PASS: workspace resolves local sibling modules")


def test_workspace_resolves_package_init() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        init_file = root / "packlib" / "__init__.lam"
        _write(main, "from packlib import label\n")
        _write(init_file, "func label() -> str { return \"ok\" }\n")
        index = WorkspaceIndex(root)
        assert index.resolve_module(main, "packlib") == init_file.resolve()
        assert index.resolve_import(main, "packlib", "label") is not None
    print("PASS: workspace resolves package __init__.lam")


def test_stdlib_wins_over_local_shadow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        local = root / "lamstrings.lam"
        _write(main, "from lamstrings import Strings\n")
        _write(local, "func fake() {}\n")
        index = WorkspaceIndex(root)
        resolved = index.resolve_module(main, "lamstrings")
        assert resolved is not None
        assert resolved != local.resolve()
        assert resolved.name == "lamstrings.lam"
        assert resolved.parent == (ROOT / "lib").resolve()
    print("PASS: stdlib modules win over local shadowing")


def test_missing_module_returns_none() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        _write(main, "from nope import Thing\n")
        index = WorkspaceIndex(root)
        assert index.resolve_module(main, "nope") is None
        assert index.resolve_import(main, "nope", "Thing") is None
    print("PASS: missing modules resolve to None")


def main() -> int:
    tests = [
        test_module_facts_exports_and_imports,
        test_workspace_resolves_local_sibling,
        test_workspace_resolves_package_init,
        test_stdlib_wins_over_local_shadow,
        test_missing_module_returns_none,
    ]
    for test in tests:
        test()
    print(f"\nModule-index results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
