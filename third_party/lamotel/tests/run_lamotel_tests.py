#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


LIB_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = LIB_ROOT.parents[1]
COMPILER = PROJECT_ROOT / "compiler" / "lammergeier.py"
PYTHON = sys.executable


def _expectations(source: str) -> list[str]:
    out: list[str] = []
    for line in source.splitlines():
        m = re.match(r'^\s*#\s*expect:\s*(.*)$', line)
        if m:
            out.append(m.group(1).strip())
    return out


def _run_case(path: Path) -> tuple[bool, str]:
    source = path.read_text(encoding="utf-8")
    expected = _expectations(source)
    with tempfile.TemporaryDirectory(prefix="lamotel_test_") as tmp:
        binary = Path(tmp) / "test_binary"
        compile_proc = subprocess.run(
            [PYTHON, str(COMPILER), str(path), "--extlibs", str(LIB_ROOT.parent), "-o", str(binary)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if compile_proc.returncode != 0:
            return False, "COMPILE FAIL:\n" + compile_proc.stderr
        run_proc = subprocess.run(
            [str(binary)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
            env=os.environ.copy(),
        )
        actual = run_proc.stdout.rstrip("\n")
        if run_proc.returncode != 0:
            return False, f"RUN FAIL ({run_proc.returncode}):\n{run_proc.stderr}"
        if expected:
            expected_text = "\n".join(expected).strip()
            if actual != expected_text:
                return False, f"OUTPUT MISMATCH:\n  expected: {expected_text!r}\n  actual:   {actual!r}"
        return True, "ok"


def main() -> None:
    ap = argparse.ArgumentParser(description="lamotel package tests")
    ap.add_argument("--live", action="store_true", help="include live OTLP export tests")
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args()

    cases = sorted((LIB_ROOT / "tests").glob("offline_*.lam"))
    if args.live:
        cases.append(LIB_ROOT / "tests" / "live_export.lam")

    passed = 0
    failures: list[tuple[Path, str]] = []
    print(f"Running {len(cases)} lamotel test(s)...\n")
    for case in cases:
        ok, msg = _run_case(case)
        rel = case.relative_to(LIB_ROOT)
        if ok:
            passed += 1
            print(f"  PASS  {rel}")
        else:
            print(f"  FAIL  {rel}")
            failures.append((case, msg))
            if args.verbose:
                for line in msg.splitlines():
                    print(f"        {line}")

    print(f"\nLamotel results: {passed} passed, {len(failures)} failed, {len(cases)} total")
    if failures and not args.verbose:
        for case, msg in failures:
            print(f"  FAIL {case.relative_to(LIB_ROOT)}")
            for line in msg.splitlines()[:8]:
                print(f"    {line}")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
