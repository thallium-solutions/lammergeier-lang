#!/usr/bin/env python3
"""Uncaught exceptions and Go runtime faults end a Lam program with a Lam
report, never a raw Go panic (docs/SAFETY.md): ``panic: <Exception>: <message>``
then the Lam frames (``    at main.lam:7``) on stderr, exit status 2.
``LAM_TRACEBACK=go`` keeps Go's own report. A stack overflow is fatal in Go
and cannot be caught: it ends the program with status 2 and Go's
``stack overflow`` report (a documented limit)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"


def run(source: str, env: dict | None = None) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "main.lam"
        path.write_text(source, encoding="utf-8")
        binary = Path(tmp) / "main"
        build = subprocess.run(
            [sys.executable, str(COMPILER), str(path), "-o", str(binary)],
            capture_output=True, text=True, timeout=300,
        )
        assert build.returncode == 0, build.stderr
        full_env = dict(os.environ)
        full_env.pop("LAM_TRACEBACK", None)
        full_env.update(env or {})
        return subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=full_env)


INDEX = """\
func pick(xs: list[int], i: int) -> int {
    return xs[i]
}

func main() {
    print("before")
    print(pick([1, 2, 3], 5))
    print("unreachable")
}
"""


def test_uncaught_index_error() -> None:
    proc = run(INDEX)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stdout == "before\n", proc.stdout
    lines = proc.stderr.splitlines()
    assert lines[0] == "panic: IndexError: index out of range [5] with length 3 (at main.lam:2)", proc.stderr
    assert "    at main.lam:2" in lines and "    at main.lam:7" in lines, proc.stderr
    assert "goroutine" not in proc.stderr, proc.stderr
    print("PASS: an uncaught IndexError prints the Lam exception and frames, exit status 2")


def test_go_traceback_opt_in() -> None:
    proc = run(INDEX, {"LAM_TRACEBACK": "go"})
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert "goroutine 1 [running]" in proc.stderr, proc.stderr
    assert "main.lam:2" in proc.stderr, proc.stderr
    print("PASS: LAM_TRACEBACK=go keeps Go's panic report")


def test_uncaught_user_exception() -> None:
    proc = run("""\
func check(n: int) {
    if n < 0 {
        throw ValueError(f"negative: {n}")
    }
}

func main() {
    check(-4)
}
""")
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stderr.splitlines()[0] == "panic: ValueError: negative: -4", proc.stderr
    assert "    at main.lam:3" in proc.stderr and "    at main.lam:8" in proc.stderr, proc.stderr
    print("PASS: an uncaught thrown exception prints its Lam frames")


def test_uncaught_in_top_level_statement() -> None:
    proc = run("""\
values: list[int] = [1]
print(values[3])

func main() {
    print("unreachable")
}
""")
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stdout == "", proc.stdout
    assert proc.stderr.startswith("panic: IndexError: index out of range [3] with length 1 (at main.lam:2)"), proc.stderr
    print("PASS: a fault in a top-level statement is reported the same way")


def test_uncaught_type_error_of_cast() -> None:
    proc = run("""\
func value() -> any {
    return 2.5
}

func main() {
    n: int = value() as int
    print(n)
}
""")
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stderr.startswith("panic: TypeError: a float is not an int (at main.lam:6)"), proc.stderr
    print("PASS: a failed cast is a TypeError naming Lam types")


def test_uncaught_in_async_task_reaches_await() -> None:
    proc = run("""\
async func load(xs: list[int]) -> int {
    return xs[4]
}

func main() {
    pending = load([1])
    print(await pending)
}
""")
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stderr.startswith("panic: IndexError: index out of range [4] with length 1 (at main.lam:2)"), proc.stderr
    print("PASS: a fault in an async task is re-raised at the await with its Lam line")


def test_stack_overflow_is_fatal() -> None:
    proc = run("""\
func depth(n: int) -> int {
    return depth(n + 1) + 1
}

func main() {
    try {
        print(depth(0))
    } catch e {
        print("not reached: a Go stack overflow cannot be caught")
    }
}
""")
    assert proc.returncode == 2, (proc.returncode, proc.stderr[:500])
    assert "stack overflow" in proc.stderr[:2000], proc.stderr[:500]
    assert "not reached" not in proc.stdout
    print("PASS: unbounded recursion ends the program (documented: not catchable)")


def main() -> int:
    tests = [
        test_uncaught_index_error,
        test_go_traceback_opt_in,
        test_uncaught_user_exception,
        test_uncaught_in_top_level_statement,
        test_uncaught_type_error_of_cast,
        test_uncaught_in_async_task_reaches_await,
        test_stack_overflow_is_fatal,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL: {test.__name__}: {exc}")
    print(f"Runtime panic results: {len(tests) - failed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
