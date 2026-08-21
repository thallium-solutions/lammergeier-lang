#!/usr/bin/env python3
"""Regression tests for Lam-source mapping of generated Go errors."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"


def _compile(path: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(COMPILER), str(path), "-o", str(output)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_go_build_error_maps_to_main_lam_line() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "bad_go.lam"
        source.write_text(
            "func main() {\n"
            "    go! { var typed string = 1 }\n"
            "}\n",
            encoding="utf-8",
        )
        result = _compile(source, root / "bad")
    stderr = result.stderr
    assert result.returncode != 0
    assert "error: Go build failed for" in stderr
    assert "line 2: cannot use 1" in stderr
    assert ">>>    2 |     go! { var typed string = 1 }" in stderr
    print("PASS: Go build errors map to the main Lam source line")


def test_go_build_error_maps_to_imported_lam_line() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "helper.lam").write_text(
            "func trigger() {\n"
            "    go! { var typed string = 1 }\n"
            "}\n",
            encoding="utf-8",
        )
        main = root / "main.lam"
        main.write_text(
            "from helper import trigger\n\n"
            "func main() {\n"
            "    trigger()\n"
            "}\n",
            encoding="utf-8",
        )
        result = _compile(main, root / "main")
    stderr = result.stderr
    assert result.returncode != 0
    assert "helper.lam:2: cannot use 1" in stderr
    assert ">>>    2 |     go! { var typed string = 1 }" in stderr
    print("PASS: Go build errors map to imported Lam source lines")


def main() -> int:
    tests = [
        test_go_build_error_maps_to_main_lam_line,
        test_go_build_error_maps_to_imported_lam_line,
    ]
    for test in tests:
        test()
    print(f"\nGo error mapping: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
