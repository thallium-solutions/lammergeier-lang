#!/usr/bin/env python3
"""Strict mode: the warnings of a program and of its project modules fail the
build (``lamc --strict``, ``LAMC_STRICT=1``, ``[build] strict = true`` in
``lamlib.toml``); the stdlib and installed packages (``extlibs``) are not
covered, and a module checked clean without strict mode is checked again.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAMC = ROOT / "lamc"
PYTHON = os.environ.get("PYTHON", sys.executable)


def lamc(project: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {key: value for key, value in os.environ.items() if key != "LAMC_STRICT"}
    full_env.update(env or {})
    return subprocess.run(
        [PYTHON, str(LAMC), "main.lam", "-o", str(project / "app.bin"), *args],
        cwd=project, capture_output=True, text=True, timeout=300, env=full_env,
    )


def make_project(td: Path, *, manifest_strict: bool = False, module_warning: bool = True) -> Path:
    project = td / "proj"
    (project / "app").mkdir(parents=True)
    (project / "extlibs" / "dep").mkdir(parents=True)
    build = "\n[build]\nstrict = true\n" if manifest_strict else ""
    (project / "lamlib.toml").write_text(f'[library]\nname = "strictdemo"\nversion = "0.1.0"\n{build}')
    param = "unused: int" if module_warning else "_unused: int"
    (project / "app" / "util.lam").write_text(f"func helper({param}) -> int {{\n    return 1\n}}\n")
    # An installed package with a warning: never covered by strict mode.
    (project / "extlibs" / "dep" / "__init__.lam").write_text("func depHelper(spare: int) -> int {\n    return 2\n}\n")
    (project / "main.lam").write_text(
        "from app.util import helper\nfrom dep import depHelper\n\nfunc main() {\n    print(helper(2) + depHelper(3))\n}\n"
    )
    return project


def test_default_build_only_warns() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = make_project(Path(td))
        proc = lamc(project)
        assert proc.returncode == 0, proc.stderr
        print("PASS: without strict mode warnings do not fail the build")


def test_strict_flag_fails_on_project_module_warning() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = make_project(Path(td))
        # A non-strict build first: it remembers util.lam as checked clean.
        assert lamc(project).returncode == 0
        proc = lamc(project, "--strict")
        assert proc.returncode == 1, proc.stderr
        assert "unused parameter `unused`" in proc.stderr, proc.stderr
        assert "error: strict mode: 1 warning in" in proc.stderr and "util.lam" in proc.stderr, proc.stderr
        assert "spare" not in proc.stderr, "extlibs warnings must not be covered"
        print("PASS: --strict fails on a project module's warning (also after a cached clean check)")


def test_strict_environment_and_main_file() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = make_project(Path(td), module_warning=False)
        (project / "main.lam").write_text(
            "from app.util import helper\n\nfunc main() {\n    x: int = helper(1)\n}\n"
        )
        assert lamc(project).returncode == 0
        proc = lamc(project, env={"LAMC_STRICT": "1"})
        assert proc.returncode == 1, proc.stderr
        assert "unused local `x`" in proc.stderr and "error: strict mode: 1 warning in main.lam" in proc.stderr, proc.stderr
        print("PASS: LAMC_STRICT=1 fails on a warning of the compiled file")


def test_manifest_strict_and_clean_project() -> None:
    with tempfile.TemporaryDirectory() as td:
        project = make_project(Path(td), manifest_strict=True)
        proc = lamc(project)
        assert proc.returncode == 1, proc.stderr
        assert "error: strict mode" in proc.stderr, proc.stderr
    with tempfile.TemporaryDirectory() as td:
        project = make_project(Path(td), manifest_strict=True, module_warning=False)
        proc = lamc(project)
        assert proc.returncode == 0, proc.stderr
        print("PASS: [build] strict = true fails on warnings; a clean project builds")


def main() -> int:
    tests = [
        test_default_build_only_warns,
        test_strict_flag_fails_on_project_module_warning,
        test_strict_environment_and_main_file,
        test_manifest_strict_and_clean_project,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL: {test.__name__}: {exc}")
    print(f"\nstrict mode: {len(tests) - failed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
