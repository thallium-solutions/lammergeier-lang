#!/usr/bin/env python3
"""Tests for ``lamc lib run`` manifest scripts."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_manifest(root: Path) -> None:
    _write(
        root / "lamlib.toml",
        textwrap.dedent(
            """
            [library]
            name = "demo_lib"
            version = "0.1.0"

            [scripts]
            make_file = "printf root-ok > ran.txt"
            say = "printf hello"
            fail = "exit 7"
            """
        ).strip()
        + "\n",
    )


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("LAMC_EXTLIBS", None)
    return subprocess.run(
        LAMC + args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def test_run_executes_from_manifest_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "project"
        nested = project / "tools" / "scripts"
        nested.mkdir(parents=True)
        _write_manifest(project)

        proc = _run(["lib", "run", "make_file", "-q"], cwd=nested)
        assert proc.returncode == 0, proc.stderr
        assert (project / "ran.txt").read_text(encoding="utf-8") == "root-ok"
        assert not (nested / "ran.txt").exists()
    print("PASS: lib run executes scripts from the manifest root")


def test_list_and_dry_run() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_manifest(project)

        listed = _run(["lib", "run", "--list"], cwd=project)
        assert listed.returncode == 0, listed.stderr
        assert "make_file\tprintf root-ok > ran.txt" in listed.stdout
        assert "say\tprintf hello" in listed.stdout

        dry = _run(["lib", "run", "make_file", "--dry-run"], cwd=project)
        assert dry.returncode == 0, dry.stderr
        assert dry.stdout.strip() == "printf root-ok > ran.txt"
        assert not (project / "ran.txt").exists()
    print("PASS: lib run lists scripts and supports dry-run")


def test_cwd_option_controls_manifest_discovery() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        outside = Path(tmp) / "outside"
        project = Path(tmp) / "project"
        nested = project / "nested"
        outside.mkdir()
        nested.mkdir(parents=True)
        _write_manifest(project)

        proc = _run(
            ["lib", "run", "make_file", "--cwd", str(nested), "-q"],
            cwd=outside,
        )
        assert proc.returncode == 0, proc.stderr
        assert (project / "ran.txt").exists()
        assert not (outside / "ran.txt").exists()
    print("PASS: lib run --cwd controls manifest discovery")


def test_missing_script_reports_available_scripts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_manifest(project)

        proc = _run(["lib", "run", "missing"], cwd=project)
        assert proc.returncode == 2
        assert "script 'missing' is not declared" in proc.stderr
        assert "available scripts: fail, make_file, say" in proc.stderr
    print("PASS: lib run reports missing scripts clearly")


def test_script_exit_code_is_preserved() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write_manifest(project)

        proc = _run(["lib", "run", "fail", "-q"], cwd=project)
        assert proc.returncode == 7, proc.stderr
    print("PASS: lib run preserves script exit codes")


def main() -> int:
    tests = [
        test_run_executes_from_manifest_root,
        test_list_and_dry_run,
        test_cwd_option_controls_manifest_discovery,
        test_missing_script_reports_available_scripts,
        test_script_exit_code_is_preserved,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL: {test.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"ERROR: {test.__name__}: {exc!r}")
    print()
    print("=" * 60)
    if failures:
        print(f"lib run: {failures} of {len(tests)} tests failed")
        return 1
    print(f"lib run: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
