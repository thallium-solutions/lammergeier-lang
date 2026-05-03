#!/usr/bin/env python3
"""
Transpilation-output tests for the Lammergeier Lang compiler.

For each `.lam` file under `tests/transpilation/cases/`, this runner:

  1. Invokes the compiler with `--emit-go` to get the generated Go source.
  2. Extracts the expectations from header comments of the form
         # expect-go: <substring>
     (or, for a multi-line block:
         # expect-go:
         #   line 1
         #   line 2
     where consecutive lines prefixed with two or more spaces after `#`
     are each treated as an independent substring.)
  3. Asserts that every expected substring appears in the emitted Go.

These tests protect the compiler against regressions in the Lam → Go
mapping described in docs/TRANSPILATION.md. They do not run any Go
binary.

Usage:
    python3 tests/transpilation/run_transpilation_tests.py
    python3 tests/transpilation/run_transpilation_tests.py -v
    python3 tests/transpilation/run_transpilation_tests.py -f hello
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


def _find_project_root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / "compiler" / "lammergeier.py").is_file():
            return p
    return start.parent.parent


PROJECT_ROOT = _find_project_root(Path(__file__).resolve())
COMPILER = PROJECT_ROOT / "compiler" / "lammergeier.py"
PYTHON = sys.executable


def find_cases(directory: Path, filt: str | None = None) -> list[Path]:
    files = sorted(directory.rglob("*.lam"))
    if filt:
        files = [f for f in files if filt.lower() in f.name.lower()]
    return files


def extract_expectations(source: str) -> list[str]:
    """Return every `# expect-go:` substring declared in the source."""
    expectations: list[str] = []
    lines = source.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^\s*#\s*expect-go:\s*(.*)$', line)
        if m:
            text = m.group(1).rstrip()
            if text:
                expectations.append(text)
            else:
                # Multi-line block: swallow subsequent `#   <line>` entries.
                j = i + 1
                while j < len(lines):
                    cont = re.match(r'^\s*#\s{2,}(.+)$', lines[j])
                    if not cont:
                        break
                    expectations.append(cont.group(1).rstrip())
                    j += 1
                i = j
                continue
        i += 1
    return expectations


def emit_go(lam_file: Path) -> tuple[bool, str]:
    result = subprocess.run(
        [PYTHON, str(COMPILER), str(lam_file), "--emit-go"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        return False, result.stderr or "compiler exited with non-zero status"
    return True, result.stdout


def run_case(lam_file: Path) -> tuple[bool, str]:
    source = lam_file.read_text(encoding="utf-8")
    expectations = extract_expectations(source)
    if not expectations:
        return False, "no `# expect-go:` expectations declared"

    ok, output = emit_go(lam_file)
    if not ok:
        return False, f"COMPILER ERROR:\n{output}"

    missing = [e for e in expectations if e not in output]
    if missing:
        lines = "\n".join(f"    - {m!r}" for m in missing)
        return False, f"MISSING EXPECTATIONS:\n{lines}"
    return True, "ok"


def main() -> None:
    ap = argparse.ArgumentParser(description="Lammergeier transpilation tests")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--filter", "-f", default=None)
    args = ap.parse_args()

    cases_dir = Path(__file__).resolve().parent / "cases"
    files = find_cases(cases_dir, args.filter)
    if not files:
        print("No .lam cases found under", cases_dir)
        sys.exit(1)

    print(f"Running {len(files)} transpilation tests...\n")

    passed, failed = 0, 0
    errors: list[tuple[str, str]] = []
    for f in files:
        rel = str(f.relative_to(PROJECT_ROOT))
        ok, message = run_case(f)
        if ok:
            passed += 1
            print(f"  ✅ PASS  {rel}")
        else:
            failed += 1
            print(f"  ❌ FAIL  {rel}")
            errors.append((rel, message))
            if args.verbose:
                for line in message.split("\n"):
                    print(f"         {line}")

    print(f"\n{'='*60}")
    print(f"Transpilation results: {passed} passed, {failed} failed, {passed + failed} total")
    if errors and not args.verbose:
        print("\nFailed tests:")
        for rel, msg in errors:
            print(f"  ❌ {rel}")
            for line in msg.split("\n")[:6]:
                print(f"     {line}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
