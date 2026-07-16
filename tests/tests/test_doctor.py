#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler import lammergeier as lamc
from compiler.version import LAMC_VERSION

LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]


def test_version_command_reports_semver() -> None:
    for args in (["version"], ["--version"]):
        proc = subprocess.run(
            LAMC + args,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == LAMC_VERSION
        assert re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", proc.stdout.strip())


def test_doctor_command_reports_required_fields() -> None:
    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ)
        env["LAMC_CACHE_DIR"] = str(Path(td) / "cache")
        proc = subprocess.run(
            LAMC + ["doctor"],
            capture_output=True,
            text=True,
            env=env,
        )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    for field in (
        "Lammergeier doctor",
        "lamc:",
        "python:",
        "go:",
        "lark:",
        "project root:",
        "manifest:",
        "compiler root:",
        "stdlib path:",
        "cache path:",
        "cache library:",
        "requirements:",
        "lammergeier-lsp:",
    ):
        assert field in out, out


def test_doctor_flag_alias_works() -> None:
    proc = subprocess.run(
        LAMC + ["--doctor"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "Lammergeier doctor" in proc.stdout


def test_doctor_missing_tools_are_clear() -> None:
    old_which = lamc.shutil.which
    old_lark = lamc._lark
    old_lark_error = lamc._LARK_IMPORT_ERROR

    def fake_which(name: str) -> str | None:
        if name in {"go", "lammergeier-lsp"}:
            return None
        return old_which(name)

    lamc.shutil.which = fake_which
    lamc._lark = None
    lamc._LARK_IMPORT_ERROR = ImportError("No module named 'lark'")
    try:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = lamc._cmd_doctor([])
        out = stdout.getvalue()
    finally:
        lamc.shutil.which = old_which
        lamc._lark = old_lark
        lamc._LARK_IMPORT_ERROR = old_lark_error

    assert rc == 0
    assert "go: missing (go not found on PATH)" in out, out
    assert "lark: missing (No module named 'lark')" in out, out
    assert "lammergeier-lsp:" in out and "not on PATH" in out, out


def test_doctor_json_reports_required_sections() -> None:
    proc = subprocess.run(
        LAMC + ["doctor", "--json"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    for key in (
        "lamc", "python", "go", "lark", "lsp", "paths", "cache",
        "go_env", "dependencies", "extensions", "strict",
    ):
        assert key in payload, payload.keys()
    assert payload["lamc"]["version"] == LAMC_VERSION
    assert payload["strict"]["enabled"] is False


def test_doctor_strict_json_fails_when_required_tools_missing() -> None:
    old_which = lamc.shutil.which
    old_lark = lamc._lark
    old_lark_error = lamc._LARK_IMPORT_ERROR

    def fake_which(name: str) -> str | None:
        if name in {"go", "lammergeier-lsp"}:
            return None
        return old_which(name)

    lamc.shutil.which = fake_which
    lamc._lark = None
    lamc._LARK_IMPORT_ERROR = ImportError("No module named 'lark'")
    try:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = lamc._cmd_doctor(["--strict", "--json"])
        payload = json.loads(stdout.getvalue())
    finally:
        lamc.shutil.which = old_which
        lamc._lark = old_lark
        lamc._LARK_IMPORT_ERROR = old_lark_error

    assert rc == 1
    assert payload["strict"]["enabled"] is True
    assert "go" in payload["strict"]["failures"]
    assert "lark" in payload["strict"]["failures"]


def test_doctor_project_discovery_reports_nearest_manifest() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        nested = root / "a" / "b"
        nested.mkdir(parents=True)
        manifest = root / "lamlib.toml"
        manifest.write_text(
            '[library]\nname = "doctor_proj"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        old_cwd = Path.cwd()
        try:
            os.chdir(nested)
            report = lamc._doctor_report()
        finally:
            os.chdir(old_cwd)

    assert report["paths"]["project_root"] == str(root.resolve()), report["paths"]
    assert report["paths"]["manifest"] == str(manifest.resolve()), report["paths"]


def main() -> int:
    tests = [
        test_version_command_reports_semver,
        test_doctor_command_reports_required_fields,
        test_doctor_flag_alias_works,
        test_doctor_missing_tools_are_clear,
        test_doctor_json_reports_required_sections,
        test_doctor_strict_json_fails_when_required_tools_missing,
        test_doctor_project_discovery_reports_nearest_manifest,
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
        print(f"doctor: {failures} of {len(tests)} tests failed")
        return 1
    print(f"doctor: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
