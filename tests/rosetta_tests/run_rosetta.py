#!/usr/bin/env python3
"""
Rosetta Code test runner for lammergeier.

Compiles and runs each .lam file in rosetta_tests/,
comparing stdout to # expect: lines.

Usage:
    python3 rosetta_tests/run_rosetta.py
    python3 rosetta_tests/run_rosetta.py --filter fizzbuzz
"""
from __future__ import annotations
import os, sys, subprocess, tempfile, re, argparse
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

def _find_project_root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / "compiler" / "lammergeier.py").is_file():
            return p
    return start.parent.parent


PROJECT_ROOT = _find_project_root(Path(__file__).resolve())
COMPILER = PROJECT_ROOT / "compiler" / "lammergeier.py"
PYTHON = sys.executable

def find_tests(directory: Path, filt: str | None = None) -> list[Path]:
    files = sorted(directory.rglob("*.lam"))
    if filt:
        files = [f for f in files if filt.lower() in f.name.lower()]
    return files

def extract_expected(source: str) -> str | None:
    lines = source.split("\n")
    expected = []
    for line in lines:
        m = re.match(r'^\s*#\s*expect:\s*(.*)$', line)
        if m:
            text = m.group(1)
            if text:
                expected.append(text)
        elif re.match(r'^\s*#\s{2,}(.+)$', line) and expected:
            m2 = re.match(r'^\s*#\s{2,}(.+)$', line)
            if m2:
                expected.append(m2.group(1))
    return "\n".join(expected) if expected else None

def run_test(tpy_file: Path, verbose: bool = False) -> tuple[bool, str]:
    source = tpy_file.read_text(encoding="utf-8")
    expected = extract_expected(source)
    with tempfile.TemporaryDirectory(prefix="lammergeier_rosetta_") as tmpdir:
        binary = os.path.join(tmpdir, "test_binary")
        result = subprocess.run(
            [PYTHON, str(COMPILER), str(tpy_file), "-o", binary],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return False, f"COMPILE FAIL:\n{result.stderr[:500]}"
        try:
            run_result = subprocess.run(
                [binary], capture_output=True, text=True, timeout=10,
            )
        except subprocess.TimeoutExpired:
            return False, "TIMEOUT"
        actual = run_result.stdout.rstrip("\n")
        if expected is None:
            return True, "NO EXPECT (compiled + ran OK)"
        if actual == expected:
            return True, "OK"
        return False, f"OUTPUT MISMATCH:\nExpected:\n{expected}\nGot:\n{actual}"

def _run_wrapper(path_str):
    f = Path(path_str)
    try:
        ok, msg = run_test(f)
    except Exception as e:
        ok, msg = False, f"EXCEPTION: {e}"
    return f"rosetta_tests/{f.name}", ok, msg

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--filter", default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-j", "--jobs", type=int, default=None)
    args = ap.parse_args()

    test_dir = Path(__file__).resolve().parent
    files = find_tests(test_dir, args.filter)
    workers = args.jobs or os.cpu_count() or 4
    print(f"Running {len(files)} rosetta tests with {workers} workers...\n")

    results = []
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_run_wrapper, str(f)): f for f in files}
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda r: r[0])
    passed = failed = 0
    failures = []
    for label, ok, msg in results:
        if ok:
            print(f"  ✅ PASS  {label}")
            passed += 1
        else:
            print(f"  ❌ FAIL  {label}")
            failed += 1
            failures.append((label, msg))
            if args.verbose:
                for line in msg.split("\n"):
                    print(f"         {line}")
    print(f"\n{'='*60}")
    print(f"Rosetta Results: {passed} passed, {failed} failed, {passed+failed} total")
    if failures:
        print(f"\nFailed tests:")
        for name, msg in failures:
            print(f"  ❌ {name}")
            for line in msg.split("\n")[:5]:
                print(f"     {line}")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
