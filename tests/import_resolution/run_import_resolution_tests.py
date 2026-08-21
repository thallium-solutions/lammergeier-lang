#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def _find_project_root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / "compiler" / "lammergeier.py").is_file():
            return p
    return start.parent.parent


PROJECT_ROOT = _find_project_root(Path(__file__).resolve())
COMPILER = PROJECT_ROOT / "compiler" / "lammergeier.py"
PYTHON = sys.executable


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run(source: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(COMPILER), str(source), "--emit-go"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _assert_missing_module(source: str, expected: list[str], libs: dict[str, str] | None = None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        if libs:
            for rel, text in libs.items():
                _write(root / rel, text)
        main = root / "main.lam"
        _write(main, source)
        proc = _run(main)
        combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if proc.returncode == 0:
            raise AssertionError("expected missing-module failure but compile succeeded")
        missing = [s for s in expected if s not in combined]
        if missing:
            raise AssertionError(
                "missing expected substrings:\n"
                + "\n".join(f"  - {m!r}" for m in missing)
                + f"\n\nGOT:\n{combined[:1200]}"
            )


def _assert_import_resolution_error(source: str, expected: list[str], libs: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in libs.items():
            _write(root / rel, text)
        main = root / "main.lam"
        _write(main, source)
        proc = _run(main)
        combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if proc.returncode == 0:
            raise AssertionError("expected import-resolution failure but compile succeeded")
        missing = [s for s in expected if s not in combined]
        if missing:
            raise AssertionError(
                "missing expected substrings:\n"
                + "\n".join(f"  - {m!r}" for m in missing)
                + f"\n\nGOT:\n{combined[:1200]}"
            )


def _assert_resolves(source: str, libs: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in libs.items():
            _write(root / rel, text)
        main = root / "main.lam"
        _write(main, source)
        proc = _run(main)
        combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if proc.returncode != 0 or "import resolution failed" in combined:
            raise AssertionError(f"unexpected import-resolution failure:\n{combined}")


def test_plain_import_missing_module() -> None:
    _assert_missing_module(
        "import does_not_exist\n\nfunc main() {\n    pass\n}\n",
        [
            "import resolution failed",
            "module `does_not_exist` could not be found",
            "looked for `does_not_exist.lam` or `does_not_exist/__init__.lam`",
            "searched:",
        ],
    )
    print("PASS: plain import missing module")


def test_from_import_missing_module() -> None:
    _assert_missing_module(
        "from no_such_package import Thing\n\nfunc main() {\n    pass\n}\n",
        [
            "import resolution failed",
            "module `no_such_package` could not be found",
            "from no_such_package import Thing",
        ],
    )
    print("PASS: from import missing module")


def test_missing_module_suggestion() -> None:
    _assert_missing_module(
        "from helper_mathh import add\n\nfunc main() {\n    pass\n}\n",
        [
            "module `helper_mathh` could not be found",
            "help: did you mean `helper_math`?",
        ],
        libs={"helper_math.lam": "func add(x: int, y: int) -> int { return x + y }\n"},
    )
    print("PASS: missing module suggestion")


def test_package_init_resolves() -> None:
    _assert_resolves(
        "from packlib import label\n\nfunc main() {\n    print(label())\n}\n",
        {"packlib/__init__.lam": "func label() -> str { return \"ok\" }\n"},
    )
    print("PASS: package __init__ resolves")


def test_from_import_missing_symbol() -> None:
    _assert_import_resolution_error(
        "from helper_math import subtract\n\nfunc main() {\n    pass\n}\n",
        [
            "import resolution failed",
            "module `helper_math` does not export `subtract`",
            "exported by `helper_math`:",
            "- add",
        ],
        {"helper_math.lam": "func add(x: int, y: int) -> int { return x + y }\n"},
    )
    print("PASS: from import missing symbol")


def test_from_import_missing_symbol_suggestion() -> None:
    _assert_import_resolution_error(
        "from helper_math import adder\n\nfunc main() {\n    pass\n}\n",
        [
            "module `helper_math` does not export `adder`",
            "help: did you mean `add`?",
        ],
        {"helper_math.lam": "func add(x: int, y: int) -> int { return x + y }\n"},
    )
    print("PASS: from import missing symbol suggestion")


def test_from_import_missing_symbol_alias() -> None:
    _assert_import_resolution_error(
        "from helper_math import adder as add_alias\n\nfunc main() {\n    pass\n}\n",
        [
            "module `helper_math` does not export `adder` as `add_alias`",
            "help: did you mean `add`?",
        ],
        {"helper_math.lam": "func add(x: int, y: int) -> int { return x + y }\n"},
    )
    print("PASS: from import missing symbol alias")


def test_from_import_valid_symbols_resolve() -> None:
    _assert_resolves(
        "from shapes import Point, origin, SCALE\n\nfunc main() {\n    print(origin())\n}\n",
        {
            "shapes.lam": (
                "const SCALE = 2\n"
                "class Point {\n"
                "    x: int = 0\n"
                "}\n"
                "func origin() -> Point {\n"
                "    return Point()\n"
                "}\n"
            )
        },
    )
    print("PASS: valid imported symbols resolve")


def test_nested_submodule_resolves() -> None:
    _assert_resolves(
        "from lamwebp.codec import decode\n\nfunc main() {\n    print(decode(4))\n}\n",
        {"lamwebp/codec.lam": "func decode(value: int) -> int { return value * 2 }\n"},
    )
    print("PASS: nested submodule resolves")


def test_deep_nested_submodule_resolves() -> None:
    _assert_resolves(
        "from lamwebp.io.files import extension\n\nfunc main() {\n    print(extension())\n}\n",
        {"lamwebp/io/files.lam": "func extension() -> str { return \".webp\" }\n"},
    )
    print("PASS: deeply nested submodule resolves")


def test_nested_package_submodule_resolves() -> None:
    _assert_resolves(
        "from lamwebp.io.files import readWebp\n\nfunc main() {\n    print(readWebp(\"image.webp\"))\n}\n",
        {"lamwebp/io/files/__init__.lam": "func readWebp(path: str) -> str { return path }\n"},
    )
    print("PASS: nested package submodule resolves")


def test_root_package_and_submodule_resolve_together() -> None:
    _assert_resolves(
        "from lamwebp import version\nfrom lamwebp.codec import decode\n\nfunc main() {\n    print(version() + decode())\n}\n",
        {
            "lamwebp/__init__.lam": "func version() -> str { return \"v1\" }\n",
            "lamwebp/codec.lam": "func decode() -> str { return \"-codec\" }\n",
        },
    )
    print("PASS: root package and submodule resolve together")


def test_package_reexports_submodule_symbol() -> None:
    _assert_resolves(
        "from lamwebp import decode\n\nfunc main() {\n    print(decode())\n}\n",
        {
            "lamwebp/__init__.lam": "from lamwebp.codec import decode\n",
            "lamwebp/codec.lam": "func decode() -> str { return \"ok\" }\n",
        },
    )
    print("PASS: package root reexports submodule symbol")


def test_submodule_alias_resolves() -> None:
    _assert_resolves(
        "from lamwebp.codec import decode as readImage\n\nfunc main() {\n    print(readImage())\n}\n",
        {"lamwebp/codec.lam": "func decode() -> str { return \"ok\" }\n"},
    )
    print("PASS: submodule import alias resolves")


def test_submodule_transitive_import_resolves() -> None:
    _assert_resolves(
        "from lamwebp.codec.decoder import decode\n\nfunc main() {\n    print(decode())\n}\n",
        {
            "lamwebp/codec/decoder.lam": (
                "from lamwebp.io.files import suffix\n"
                "func decode() -> str { return \"image\" + suffix() }\n"
            ),
            "lamwebp/io/files.lam": "func suffix() -> str { return \".webp\" }\n",
        },
    )
    print("PASS: transitive submodule import resolves")


def test_nested_submodule_precedes_legacy_dotted_file() -> None:
    _assert_resolves(
        "from lamwebp.codec import decode\n\nfunc main() {\n    print(decode())\n}\n",
        {
            "lamwebp/codec.lam": "func decode() -> str { return \"nested\" }\n",
            "lamwebp.codec.lam": "func legacy() -> str { return \"legacy\" }\n",
        },
    )
    print("PASS: nested submodule precedes legacy dotted file")


def test_legacy_dotted_module_file_fallback() -> None:
    _assert_resolves(
        "from lamwebp.codec import decode\n\nfunc main() {\n    print(decode())\n}\n",
        {"lamwebp.codec.lam": "func decode() -> str { return \"legacy\" }\n"},
    )
    print("PASS: legacy dotted module file remains a fallback")


def test_missing_nested_submodule_lists_nested_paths() -> None:
    _assert_missing_module(
        "from lamwebp.io.files import readWebp\n\nfunc main() {\n    pass\n}\n",
        [
            "module `lamwebp.io.files` could not be found",
            "lamwebp/io/files.lam",
            "lamwebp/io/files/__init__.lam",
            "lamwebp.io.files.lam",
        ],
    )
    print("PASS: missing nested submodule lists nested search paths")


def main() -> None:
    tests = [
        test_plain_import_missing_module,
        test_from_import_missing_module,
        test_missing_module_suggestion,
        test_package_init_resolves,
        test_from_import_missing_symbol,
        test_from_import_missing_symbol_suggestion,
        test_from_import_missing_symbol_alias,
        test_from_import_valid_symbols_resolve,
        test_nested_submodule_resolves,
        test_deep_nested_submodule_resolves,
        test_nested_package_submodule_resolves,
        test_root_package_and_submodule_resolve_together,
        test_package_reexports_submodule_symbol,
        test_submodule_alias_resolves,
        test_submodule_transitive_import_resolves,
        test_nested_submodule_precedes_legacy_dotted_file,
        test_legacy_dotted_module_file_fallback,
        test_missing_nested_submodule_lists_nested_paths,
    ]
    for test in tests:
        test()
    print(f"\nImport resolution results: {len(tests)} passed, 0 failed, {len(tests)} total")


if __name__ == "__main__":
    main()
