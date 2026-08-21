#!/usr/bin/env python3
"""Tests for Go dependency-resolution diagnostics in the compiler driver."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler import lammergeier as lamc  # noqa: E402


def test_go_mod_tidy_failure_reports_module_resolution_error() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "main.lam"
        output = root / "app"
        source.write_text(
            'func main() {\n    print("ok")\n}\n',
            encoding="utf-8",
        )

        calls: list[list[str]] = []
        original_run = lamc.subprocess.run

        def fake_run(args, *pos, **kwargs):
            argv = list(args)
            calls.append(argv)
            if argv[:3] == ["go", "mod", "tidy"]:
                return subprocess.CompletedProcess(
                    argv,
                    1,
                    stdout="",
                    stderr="go: example.com/missing@v0.0.1: module lookup disabled",
                )
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        stderr = io.StringIO()
        lamc.subprocess.run = fake_run
        try:
            with contextlib.redirect_stderr(stderr):
                try:
                    lamc.compile_lam(str(source), str(output), use_cache=False)
                except SystemExit as exc:
                    assert exc.code == 1
                else:
                    raise AssertionError("compile_lam should exit after go mod tidy fails")
        finally:
            lamc.subprocess.run = original_run

    err = stderr.getvalue()
    assert "error: Go module resolution failed for" in err
    assert "module lookup disabled" in err
    assert ["go", "build", "-buildvcs=false", "-o", str(output.resolve()), "."] not in calls
    print("PASS: go mod tidy failures are reported before go build")


def main() -> int:
    tests = [test_go_mod_tidy_failure_reports_module_resolution_error]
    for test in tests:
        test()
    print(f"\nGo dependency diagnostics: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
