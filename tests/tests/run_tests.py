#!/usr/bin/env python3
"""
Automated test suite for the Lammergeier Lang compiler.

For each .lam file in tests/cases/, it:
  1. Parses the expected output from a header comment  # expect: ...
  2. Compiles the file with lammergeier
  3. Runs the resulting binary
  4. Compares stdout to expected output
  5. Reports PASS / FAIL

Usage:
    python3 tests/run_tests.py             # run all tests
    python3 tests/run_tests.py --verbose   # verbose output
    python3 tests/run_tests.py --filter hello  # run only matching tests
"""

from __future__ import annotations
import os
import sys
import subprocess
import tempfile
import re
import argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

def _find_project_root(start: Path) -> Path:
    """Walk upward until we find the `compiler/lammergeier.py` file."""
    for p in (start, *start.parents):
        if (p / "compiler" / "lammergeier.py").is_file():
            return p
    return start.parent.parent


PROJECT_ROOT = _find_project_root(Path(__file__).resolve())
COMPILER = PROJECT_ROOT / "compiler" / "lammergeier.py"
PYTHON = sys.executable


def find_test_files(directory: Path, filt: str | list[str] | None = None) -> list[Path]:
    """Find all .lam files in the given directory tree (those matching one
    of the filters, when given)."""
    files = sorted(directory.rglob("*.lam"))
    if filt:
        filters = [filt] if isinstance(filt, str) else filt
        # Matches the path below cases/ too (``core rosetta`` = one folder).
        files = [f for f in files
                 if any(x.lower() in str(f.relative_to(directory)).lower() for x in filters)]
    return files


# ``--race``: the tests are built with the Go race detector (``lamc
# --go-race``) and a reported data race fails the test. Read from the
# environment so the worker processes see it.
RACE_ENV = "LAM_TEST_RACE"
DATA_RACE = "WARNING: DATA RACE"


def extract_expected(source: str) -> str | None:
    """
    Extract expected output from source comments.

    Two supported forms:

    1. Repeated single-line directives::

           # expect: line1
           # expect: line2

    2. Block directive opened with ``# expect:`` (no payload) and
       closed by ``# end-expect`` or the first non-comment line::

           # expect:
           #   line1
           #   line2
           # end-expect

       In form 2, every comment immediately following the opener
       contributes a line until either an ``# end-expect`` marker or
       a non-comment line is seen. This avoids the previous behaviour
       where regular indented comments anywhere in the file were
       wrongly absorbed as continuation lines.
    """
    lines = source.split("\n")
    expected_lines: list[str] = []
    in_block = False
    for line in lines:
        m = re.match(r'^\s*#\s*expect:\s*(.*)$', line)
        if m:
            text = m.group(1)
            if text:
                # Repeated single-line directive — terminates any
                # currently-open block (you don't usually mix the two,
                # but be tolerant).
                expected_lines.append(text)
                in_block = False
            else:
                # Block directive opener — empty payload.
                in_block = True
            continue

        if not in_block:
            continue

        # We're inside an open block. End on the first non-comment
        # line or an explicit ``# end-expect`` sentinel.
        stripped = line.lstrip()
        if not stripped.startswith("#"):
            in_block = False
            continue
        if re.match(r'^\s*#\s*end-expect\s*$', line):
            in_block = False
            continue

        # Strip the leading ``#`` and one (optional) leading space, but
        # preserve any further indentation as part of the expected
        # output.
        body = stripped[1:]
        if body.startswith(" "):
            body = body[1:]
        expected_lines.append(body)

    return "\n".join(expected_lines) if expected_lines else None


def _failure_summary(message: str, limit: int = 12) -> list[str]:
    """First lines of a failure, skipping semantic *warning* blocks so the
    actual error (Go build output, panic, mismatch) is not cut off."""
    lines = message.split("\n")
    kept: list[str] = []
    skipping = False
    for line in lines:
        text = line.strip()
        if text.startswith("warning: semantic check noticed") or "warning[" in text:
            skipping = True
            continue
        if skipping and (text.startswith(">>>") or text[:1].isdigit() or not text):
            continue
        skipping = False
        kept.append(line)
    return (kept or lines)[:limit]


SKIPPED = "SKIPPED"


def required_env(source: str) -> list[str]:
    """Names listed in ``# requires-env: NAME [NAME...]`` header lines: the
    test needs an external service and is skipped (not failed) unless every
    variable is set. Such tests never fall back to a default address."""
    names: list[str] = []
    for line in source.splitlines():
        m = re.match(r'^\s*#\s*requires-env:\s*(.+)$', line)
        if m:
            names.extend(m.group(1).split())
    return names


def run_test(lam_file: Path, verbose: bool = False) -> tuple[bool, str]:
    """Compile and run a single .lam file, return (success, message)."""
    source = lam_file.read_text(encoding="utf-8")
    missing = [name for name in required_env(source) if not os.environ.get(name)]
    if missing:
        return True, f"{SKIPPED}: set {' '.join(missing)}"
    expected = extract_expected(source)
    race = os.environ.get(RACE_ENV) == "1"

    with tempfile.TemporaryDirectory(prefix="lammergeier_test_") as tmpdir:
        binary = os.path.join(tmpdir, "test_binary")

        # Compile
        result = subprocess.run(
            [PYTHON, str(COMPILER), str(lam_file), "-o", binary] + (["--go-race"] if race else []),
            capture_output=True, text=True, timeout=600 if race else 30,
        )

        if result.returncode != 0:
            return False, f"COMPILE FAIL:\n{result.stderr}"

        if not os.path.isfile(binary):
            return False, "COMPILE FAIL: no binary produced"

        # Run
        try:
            run_result = subprocess.run(
                [binary], capture_output=True, text=True, timeout=120 if race else 10,
                env={**os.environ, "GORACE": "halt_on_error=1"} if race else None,
            )
        except subprocess.TimeoutExpired:
            return False, "RUN FAIL: timeout"

        if race and DATA_RACE in run_result.stderr:
            return False, f"DATA RACE (exit {run_result.returncode}):\n{run_result.stderr[-6000:]}"

        actual = run_result.stdout.rstrip("\n")

        if run_result.returncode != 0 and expected is not None:
            return False, f"RUN FAIL (exit {run_result.returncode}):\n{run_result.stderr}"

        if expected is not None:
            expected_clean = expected.strip()
            if actual == expected_clean:
                return True, "output matches"
            else:
                return False, f"OUTPUT MISMATCH:\n  expected: {repr(expected_clean)}\n  actual:   {repr(actual)}"

        # No expected output specified — just check it compiles and runs
        return True, f"compiled & ran (no expected output to check)"


def _run_test_wrapper(lam_file_str: str) -> tuple[str, bool, str]:
    """Wrapper for parallel execution (needs picklable args)."""
    lam_file = Path(lam_file_str)
    try:
        success, message = run_test(lam_file)
    except Exception as e:
        success = False
        message = f"EXCEPTION: {e}"
    try:
        rel = str(lam_file.relative_to(PROJECT_ROOT))
    except ValueError:
        rel = str(lam_file)
    return rel, success, message


def main():
    ap = argparse.ArgumentParser(description="lammergeier test runner")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--filter", "-f", action="append", default=None,
                    help="Filter test files by name (repeatable: a file matching any of them)")
    ap.add_argument("--dir", "-d", default=None, help="Test directory (default: examples/)")
    ap.add_argument("--jobs", "-j", type=int, default=None,
                    help="Number of parallel workers (default: CPU count)")
    ap.add_argument("--race", action="store_true",
                    help="Build with the Go race detector (lamc --go-race); a data race fails the test")
    args = ap.parse_args()
    if args.race:
        os.environ[RACE_ENV] = "1"

    test_dirs = []
    if args.dir:
        test_dirs.append(Path(args.dir))
    else:
        test_dirs.append(PROJECT_ROOT / "examples" / "basic")
        test_dirs.append(PROJECT_ROOT / "examples" / "advanced")
        # Legacy layout: tests/cases
        legacy_cases = PROJECT_ROOT / "tests" / "cases"
        if legacy_cases.is_dir():
            test_dirs.append(legacy_cases)
        # Current layout: tests/tests/cases
        current_cases = PROJECT_ROOT / "tests" / "tests" / "cases"
        if current_cases.is_dir():
            test_dirs.append(current_cases)

    all_files = []
    for d in test_dirs:
        if d.is_dir():
            all_files.extend(find_test_files(d, args.filter))

    if not all_files:
        print("No test files found.")
        sys.exit(1)

    workers = args.jobs or os.cpu_count() or 4
    print(f"Running {len(all_files)} tests with {workers} workers...\n")
    # The tests already run in parallel, one ``lamc`` per CPU: each keeps its
    # per-module work in-process (``lamc build --workers``) unless asked.
    os.environ.setdefault("LAMC_WORKERS", "1")

    results = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_run_test_wrapper, str(f)): f for f in all_files}
        for future in as_completed(futures):
            results.append(future.result())

    # Sort by file path for stable output
    results.sort(key=lambda r: r[0])

    passed = 0
    failed = 0
    skipped = 0
    errors = []

    for rel, success, message in results:
        if success and message.startswith(SKIPPED):
            skipped += 1
            print(f"  ⏭️  SKIP  {rel} ({message})")
        elif success:
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
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped, {passed + failed + skipped} total")

    if errors:
        print(f"\nFailed tests:")
        for rel, msg in errors:
            print(f"  ❌ {rel}")
            for line in _failure_summary(msg):
                print(f"     {line}")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
