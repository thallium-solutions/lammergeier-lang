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


def test_workspace_resolves_nested_submodule() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        codec = root / "lamwebp" / "codec.lam"
        _write(main, "from lamwebp.codec import Decoder\n")
        _write(codec, "class Decoder {}\n")
        index = WorkspaceIndex(root)
        assert index.resolve_module(main, "lamwebp.codec") == codec.resolve()
        assert index.resolve_import(main, "lamwebp.codec", "Decoder") is not None
    print("PASS: workspace resolves nested submodule files")


def test_workspace_resolves_deep_package_submodule() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        init_file = root / "lamwebp" / "io" / "files" / "__init__.lam"
        _write(main, "from lamwebp.io.files import readWebp\n")
        _write(init_file, "func readWebp(path: str) -> str { return path }\n")
        index = WorkspaceIndex(root)
        assert index.resolve_module(main, "lamwebp.io.files") == init_file.resolve()
        assert index.resolve_import(main, "lamwebp.io.files", "readWebp") is not None
    print("PASS: workspace resolves deeply nested package submodules")


def test_workspace_preserves_mixed_case_module_paths() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        module = root / "PascalPackage" / "camelModule.lam"
        _write(main, "from PascalPackage.camelModule import MixedCaseType\n")
        _write(module, "class MixedCaseType {}\n")
        index = WorkspaceIndex(root, stdlib_dir=root / "missing-stdlib")
        assert index.resolve_module(main, "PascalPackage.camelModule") == module.resolve()
        assert index.resolve_import(main, "PascalPackage.camelModule", "MixedCaseType") is not None
        assert "PascalPackage.camelModule" in index.available_modules(main)
    print("PASS: workspace preserves mixed-case module paths")


def test_nested_submodule_wins_over_legacy_dotted_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        nested = root / "lamwebp" / "codec.lam"
        legacy = root / "lamwebp.codec.lam"
        _write(main, "from lamwebp.codec import Decoder\n")
        _write(nested, "class Decoder {}\n")
        _write(legacy, "class LegacyDecoder {}\n")
        index = WorkspaceIndex(root)
        assert index.resolve_module(main, "lamwebp.codec") == nested.resolve()
    print("PASS: nested submodule path wins over legacy dotted file")


def test_legacy_dotted_file_remains_resolvable() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        legacy = root / "lamwebp.codec.lam"
        _write(main, "from lamwebp.codec import Decoder\n")
        _write(legacy, "class Decoder {}\n")
        index = WorkspaceIndex(root)
        assert index.resolve_module(main, "lamwebp.codec") == legacy.resolve()
    print("PASS: legacy dotted module files remain resolvable")


def test_module_search_paths_include_nested_candidates() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        _write(main, "from lamwebp.io.files import readWebp\n")
        index = WorkspaceIndex(root, stdlib_dir=root / "missing-stdlib")
        searched = index.module_search_paths(main, "lamwebp.io.files")
        assert searched[:3] == [
            root / "lamwebp" / "io" / "files.lam",
            root / "lamwebp" / "io" / "files" / "__init__.lam",
            root / "lamwebp.io.files.lam",
        ], searched
    print("PASS: module diagnostics expose nested search candidates")


def test_available_modules_use_dotted_names() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        main = root / "main.lam"
        _write(main, "func main() {}\n")
        _write(root / "lamwebp" / "codec.lam", "class Decoder {}\n")
        _write(root / "lamwebp" / "io" / "files" / "__init__.lam", "func readWebp() {}\n")
        index = WorkspaceIndex(root, stdlib_dir=root / "missing-stdlib")
        names = index.available_modules(main)
        assert "lamwebp.codec" in names
        assert "lamwebp.io.files" in names
        assert "codec" not in names
        assert "files" not in names
    print("PASS: module discovery reports qualified dotted names")


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
        test_workspace_resolves_nested_submodule,
        test_workspace_resolves_deep_package_submodule,
        test_workspace_preserves_mixed_case_module_paths,
        test_nested_submodule_wins_over_legacy_dotted_file,
        test_legacy_dotted_file_remains_resolvable,
        test_module_search_paths_include_nested_candidates,
        test_available_modules_use_dotted_names,
        test_stdlib_wins_over_local_shadow,
        test_missing_module_returns_none,
    ]
    for test in tests:
        test()
    print(f"\nModule-index results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
