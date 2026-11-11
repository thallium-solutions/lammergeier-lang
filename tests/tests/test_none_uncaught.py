#!/usr/bin/env python3
"""An uncaught ``None`` error ends the program like any uncaught exception
(after 4.11): exit status 2 and ``panic: AttributeError: None has no attribute
'name'`` (``TypeError: None is not a str`` for a dynamic ``None`` converted
to a value type) on stderr, with the Lam line of the access in the trace;
nothing after it runs. A ``do`` / ``catch`` around it does not handle it
(it handles the errors ``?`` propagates)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"

MEMBER = """\
class User {
    name: str = "ada"

    func hello(self) -> str {
        return "hello"
    }
}

func find(found: bool) -> User {
    if found {
        return User()
    }
    return None
}

func main() {
    print(find(true).name)
    u: User = find(false)
    print(u.hello())
    print("unreachable")
}
"""

CONVERSION = """\
func nothing() -> any {
    return None
}

func main() {
    v: any = nothing()
    s: str = v
    print(s)
    print("unreachable")
}
"""


IN_DO = """\
from lamerrors import Result

class User {
    name: str = "ada"
}

func find() -> User {
    return None
}

func main() {
    do {
        print(find().name)
    } catch err {
        print(f"do catch: {err}")
    }
    print("unreachable")
}
"""


def run(source: str) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "main.lam"
        path.write_text(source, encoding="utf-8")
        binary = Path(tmp) / "main"
        build = subprocess.run(
            [sys.executable, str(COMPILER), str(path), "-o", str(binary)],
            capture_output=True, text=True, timeout=300,
        )
        assert build.returncode == 0, build.stderr
        return subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)


def test_uncaught_attribute_error() -> None:
    proc = run(MEMBER)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stdout == "ada\n", proc.stdout
    assert "panic: AttributeError: None has no attribute 'hello'" in proc.stderr, proc.stderr
    assert "main.lam:19" in proc.stderr, proc.stderr
    print("PASS: an uncaught AttributeError exits with status 2 and its message")


def test_uncaught_type_error() -> None:
    proc = run(CONVERSION)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stdout == "", proc.stdout
    assert "panic: TypeError: None is not a str" in proc.stderr, proc.stderr
    print("PASS: an uncaught TypeError of a None conversion exits with status 2 and its message")


def test_uncaught_inside_do() -> None:
    proc = run(IN_DO)
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert proc.stdout == "", proc.stdout
    assert "panic: AttributeError: None has no attribute 'name'" in proc.stderr, proc.stderr
    print("PASS: a None exception inside a do body is not handled by its catch")


def main() -> int:
    tests = [test_uncaught_attribute_error, test_uncaught_type_error, test_uncaught_inside_do]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL: {test.__name__}: {exc}")
    print(f"None uncaught results: {len(tests) - failed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
