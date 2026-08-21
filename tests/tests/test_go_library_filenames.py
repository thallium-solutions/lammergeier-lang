#!/usr/bin/env python3
"""Regression tests for bundled library Go filenames."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"
sys.path.insert(0, str(ROOT))

from compiler.lammergeier import _library_go_filename  # noqa: E402


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body.lstrip("\n"), encoding="utf-8")


def _build_and_run(project: Path) -> str:
    source = project / "src" / "main.lam"
    binary = project / "app"
    built = subprocess.run(
        [sys.executable, str(COMPILER), str(source), "-o", str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert built.returncode == 0, built.stderr + built.stdout
    run = subprocess.run(
        [str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert run.returncode == 0, run.stderr + run.stdout
    return run.stdout.strip()


def _main_source() -> str:
    return """
from test import banner

func main() {
    print(banner("orders", "0.1.0"))
}
"""


def _library_source() -> str:
    return """
func banner(name: str, version: str) -> str {
    return f"{name} v{version}"
}
"""


def test_library_filename_never_uses_go_test_suffix() -> None:
    assert _library_go_filename("test") == "lib_test_lam.go"
    assert _library_go_filename("unit_test") == "lib_unit_test_lam.go"
    assert _library_go_filename("lamwebp.codec") == "lib_lamwebp__codec_lam.go"
    assert not _library_go_filename("test").endswith("_test.go")
    assert not _library_go_filename("unit_test").endswith("_test.go")
    print("PASS: bundled library filenames avoid Go's *_test.go suffix")


def test_flat_module_named_test_builds() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write(project / "src" / "main.lam", _main_source())
        _write(project / "src" / "test.lam", _library_source())
        assert _build_and_run(project) == "orders v0.1.0"
    print("PASS: flat module named test builds into the binary")


def test_package_module_named_test_builds() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = Path(td)
        _write(project / "src" / "main.lam", _main_source())
        _write(project / "src" / "test" / "__init__.lam", _library_source())
        assert _build_and_run(project) == "orders v0.1.0"
    print("PASS: package module named test builds into the binary")


def main() -> int:
    tests = [
        test_library_filename_never_uses_go_test_suffix,
        test_flat_module_named_test_builds,
        test_package_module_named_test_builds,
    ]
    for test in tests:
        test()
    print(f"\nGo library filenames: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
