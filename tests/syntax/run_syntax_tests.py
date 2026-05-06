#!/usr/bin/env python3
"""Syntax-diagnostic tests.

Each ``.lam`` case under ``tests/syntax/cases/`` must fail during the
parser stage and declare one or more ``# expect-error: <substring>``
lines. The runner checks that Lam-facing syntax diagnostics stay
stable: banner, source location, expected constructs, and repair hints.
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


def _expectations(source: str) -> list[str]:
    out: list[str] = []
    for line in source.splitlines():
        m = re.match(r'^\s*#\s*expect-error:\s*(.*)$', line)
        if m:
            out.append(m.group(1).strip())
    return out


def _run_case(path: Path) -> tuple[bool, str]:
    source = path.read_text(encoding="utf-8")
    expected = _expectations(source)
    if not expected:
        return False, "no `# expect-error` directives declared"

    proc = subprocess.run(
        [PYTHON, str(COMPILER), str(path), "--emit-go"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = (proc.stderr or "") + "\n" + (proc.stdout or "")

    if proc.returncode == 0:
        return False, "expected parser failure but compile succeeded"
    if "syntax check failed" not in combined:
        return False, f"compiler failed outside the syntax stage:\n{combined[:800]}"

    missing = [needle for needle in expected if needle not in combined]
    if missing:
        details = "\n".join(f"    - {m!r}" for m in missing)
        return False, f"MISSING ERROR SUBSTRINGS:\n{details}\n\nGOT:\n{combined[:1000]}"
    return True, "ok"


def main() -> None:
    ap = argparse.ArgumentParser(description="Lammergeier syntax-diagnostic tests")
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

    print(f"Running {len(files)} syntax tests...\n")
    passed = 0
    failures: list[tuple[str, str]] = []
    for path in files:
        rel = str(path.relative_to(PROJECT_ROOT))
        ok, msg = _run_case(path)
        if ok:
            passed += 1
            print(f"  PASS  {rel}")
        else:
            print(f"  FAIL  {rel}")
            failures.append((rel, msg))
            if args.verbose:
                for line in msg.splitlines():
                    print(f"        {line}")

    print(f"\n{'=' * 60}")
    print(f"Syntax results: {passed} passed, {len(failures)} failed, {len(files)} total")
    if failures and not args.verbose:
        print("\nFailed tests:")
        for rel, msg in failures:
            print(f"  FAIL  {rel}")
            for line in msg.splitlines()[:8]:
                print(f"    {line}")
    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
