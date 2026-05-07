#!/usr/bin/env python3
"""Semantic-checker tests.

Each ``.lam`` case under ``tests/semantic/cases/`` declares one of:

  - ``# expect-error: <substring>`` — the compile must fail with the
    semantic-check banner and the stderr output must contain every
    listed substring (multiple ``# expect-error`` lines accumulate).
  - ``# expect-warning: <substring>`` — the compile must succeed past
    semantic checking and stderr/stdout must contain every listed
    warning substring.
  - ``# expect-pass`` — the compile must reach (or pass) the
    semantic-check stage; we run with ``--emit-go`` so we don't pay
    for the Go build but we still exercise the full pipeline.

Usage:
    python3 tests/semantic/run_semantic_tests.py
    python3 tests/semantic/run_semantic_tests.py -v
    python3 tests/semantic/run_semantic_tests.py -f undef
"""

from __future__ import annotations

import argparse
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


def _expectations(source: str) -> tuple[list[str], list[str], bool]:
    """Return (error_substrings, warning_substrings, expect_pass)."""
    errors: list[str] = []
    warnings: list[str] = []
    expect_pass = False
    for line in source.split("\n"):
        m = re.match(r'^\s*#\s*expect-error:\s*(.*)$', line)
        if m:
            errors.append(m.group(1).strip())
            continue
        m = re.match(r'^\s*#\s*expect-warning:\s*(.*)$', line)
        if m:
            warnings.append(m.group(1).strip())
            continue
        if re.match(r'^\s*#\s*expect-pass\s*$', line):
            expect_pass = True
    return errors, warnings, expect_pass


def _run_case(lam_file: Path) -> tuple[bool, str]:
    source = lam_file.read_text(encoding="utf-8")
    expected_errors, expected_warnings, expect_pass = _expectations(source)
    if not expected_errors and not expected_warnings and not expect_pass:
        return False, "no `# expect-error`, `# expect-warning`, or `# expect-pass` declared"

    proc = subprocess.run(
        [PYTHON, str(COMPILER), str(lam_file), "--emit-go"],
        capture_output=True, text=True, timeout=30,
    )
    combined = (proc.stderr or "") + "\n" + (proc.stdout or "")

    if expect_pass:
        if proc.returncode != 0 and "semantic check failed" in combined:
            return False, f"unexpected semantic failure:\n{proc.stderr}"
        # Anything else (Go build issues, etc.) we treat as success
        # for this test family; we only care that the semantic pass
        # didn't reject the input.
        return True, "ok"

    if expected_warnings and not expected_errors:
        if proc.returncode != 0 and "semantic check failed" in combined:
            return False, f"unexpected semantic failure:\n{proc.stderr}"
        missing = [s for s in expected_warnings if s not in combined]
        if missing:
            details = "\n".join(f"    - {m!r}" for m in missing)
            return False, f"MISSING WARNING SUBSTRINGS:\n{details}\n\nGOT:\n{combined[:600]}"
        return True, "ok"

    # Negative case: must fail with a semantic error.
    if proc.returncode == 0:
        return False, "expected semantic-check failure but compile succeeded"
    if "semantic check failed" not in combined:
        return False, f"compiler failed but not at the semantic stage:\n{proc.stderr[:400]}"
    missing = [s for s in expected_errors if s not in combined]
    missing.extend(s for s in expected_warnings if s not in combined)
    if missing:
        details = "\n".join(f"    - {m!r}" for m in missing)
        return False, f"MISSING ERROR SUBSTRINGS:\n{details}\n\nGOT:\n{combined[:600]}"
    return True, "ok"


def main() -> None:
    ap = argparse.ArgumentParser(description="Lammergeier semantic-check tests")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--filter", "-f", default=None)
    args = ap.parse_args()

    cases_dir = Path(__file__).resolve().parent / "cases"
    files = sorted(cases_dir.rglob("*.lam"))
    if args.filter:
        files = [f for f in files if args.filter.lower() in f.name.lower()]
    if not files:
        print("No .lam cases found under", cases_dir)
        sys.exit(1)

    print(f"Running {len(files)} semantic tests...\n")

    passed, failed = 0, 0
    errors: list[tuple[str, str]] = []
    for f in files:
        rel = str(f.relative_to(PROJECT_ROOT))
        ok, message = _run_case(f)
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
    print(f"Semantic results: {passed} passed, {failed} failed, {passed + failed} total")
    if errors and not args.verbose:
        print("\nFailed tests:")
        for rel, msg in errors:
            print(f"  ❌ {rel}")
            for line in msg.split("\n")[:6]:
                print(f"     {line}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
