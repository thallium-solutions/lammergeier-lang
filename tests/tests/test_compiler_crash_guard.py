#!/usr/bin/env python3
"""The compiler crash guard (``compiler/ice.py``, docs/SAFETY.md): whatever the
input, ``lamc`` ends with a Lam diagnostic or an ``internal compiler error``
report and a non-zero status, never a raw Python traceback; a Go build failure
of generated code (not of a ``go!`` block) is an internal compiler error with
the Lam location of the Go error."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"


def lamc_bytes(data: bytes, extra_env: dict | None = None, name: str = "main.lam"):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.write_bytes(data)
        env = dict(os.environ)
        env.pop("LAMC_TRACEBACK", None)
        env.setdefault("LAMC_WORKERS", "1")
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(COMPILER), "build", str(path), "-o", str(Path(tmp) / "main")],
            capture_output=True, text=True, timeout=300, env=env, cwd=tmp,
        )


def lamc(text: str, **kw):
    return lamc_bytes(text.encode("utf-8"), **kw)


def no_traceback(proc) -> None:
    out = proc.stderr + proc.stdout
    assert "Traceback (most recent call last)" not in out, out[-3000:]


def test_invalid_utf8() -> None:
    proc = lamc_bytes(b"func main() {\n    print(\"\xfd\xfe\")\n}\n")
    no_traceback(proc)
    assert proc.returncode == 1, proc.returncode
    assert "is not valid UTF-8 (byte 0xfd at offset" in proc.stderr, proc.stderr
    print("PASS: invalid UTF-8 is a clean error")


def test_random_bytes() -> None:
    import random
    rng = random.Random(7)
    for _ in range(5):
        proc = lamc_bytes(bytes(rng.randrange(256) for _ in range(200)))
        no_traceback(proc)
        assert proc.returncode != 0
    print("PASS: random bytes give clean errors")


def test_deep_block_nesting() -> None:
    n = 400
    src = "func main() {\n    x: int = 0\n"
    for i in range(n):
        src += "    " * (i + 1) + f"if x < {i + 10} {{\n"
    src += "x += 1\n" + "}\n" * n + "    print(x)\n}\n"
    proc = lamc(src)
    no_traceback(proc)
    assert proc.returncode == 1, proc.returncode
    assert "nests too deeply for lamc" in proc.stderr, proc.stderr[-2000:]
    print("PASS: nesting beyond the compiler's depth is a clean error")


def test_deep_expression_nesting() -> None:
    for src in (
        "func main() {\n    x: int = " + "-" * 3000 + "1\n    print(x)\n}\n",
        "func main() {\n    x: list[int] = " + "[" * 1000 + "]" * 1000 + "\n}\n",
    ):
        proc = lamc(src)
        no_traceback(proc)
        assert proc.returncode != 0
    print("PASS: deep expressions give clean errors")


def test_truncations_and_encodings() -> None:
    base = textwrap.dedent("""\
        class Box {
            func init(self, v: int) {
                self.v: int = v
            }
        }

        func main() {
            b: Box = Box(3)
            print(f"v={b.v}")
        }
        """)
    for cut in range(0, len(base), 7):
        proc = lamc(base[:cut])
        no_traceback(proc)
    for data in (
        b"\xef\xbb\xbf" + base.encode(),
        base.replace("\n", "\r\n").encode(),
        base.replace("\n", "\r").encode(),
        base[:40].encode() + b"\x00" + base[40:].encode(),
        "func main() {\n    héllo: int = 1\n    print(héllo)\n}\n".encode(),
        ('func main() {\n    s: str = "' + "a" * 200000 + '"\n    print(len(s))\n}\n').encode(),
    ):
        no_traceback(lamc_bytes(data))
    crlf = lamc_bytes(base.replace("\n", "\r\n").encode())
    assert crlf.returncode == 0, crlf.stderr
    print("PASS: truncated files, BOM, CRLF, CR, NUL, unicode and huge literals never crash")


INJECT = """\
import sys
sys.path.insert(0, {root!r})
from compiler import transpiler as t
original = t.GoTranspiler.transpile
def broken(self, tree):
    {body}
t.GoTranspiler.transpile = broken
from compiler import lammergeier
sys.argv = ["lamc", "build", {path!r}, "-o", {out!r}]
lammergeier.main()
"""


def run_injected(body: str, extra_env: dict | None = None):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "main.lam"
        path.write_text("func main() {\n    x: int = 2\n    print(x)\n}\n", encoding="utf-8")
        script = INJECT.format(root=str(ROOT), body=body, path=str(path), out=str(Path(tmp) / "main"))
        env = dict(os.environ)
        env.pop("LAMC_TRACEBACK", None)
        env["LAMC_WORKERS"] = "1"
        env.update(extra_env or {})
        return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                              timeout=300, env=env, cwd=tmp)


def test_internal_exception_is_reported() -> None:
    proc = run_injected("for stmt in tree.children:\n        raise KeyError('boom')\n    return original(self, tree)")
    no_traceback(proc)
    assert proc.returncode == 70, (proc.returncode, proc.stderr)
    assert "internal compiler error: KeyError: 'boom'" in proc.stderr, proc.stderr
    assert "--> " in proc.stderr and "main.lam" in proc.stderr, proc.stderr
    assert "please report it" in proc.stderr, proc.stderr
    with_tb = run_injected("raise KeyError('boom')", {"LAMC_TRACEBACK": "1"})
    assert "Traceback (most recent call last)" in with_tb.stderr, with_tb.stderr
    print("PASS: a compiler exception is an internal compiler error (LAMC_TRACEBACK=1 shows the traceback)")


def test_generated_go_failure_is_internal_error() -> None:
    body = (
        "go = original(self, tree)\n"
        "    return go.replace('\\tdefer lamUncaught()\\n', '\\tdefer lamUncaught()\\n//line ' + self._source_file + ':2\\n\\tvar _ int = \"x\"\\n', 1)"
    )
    proc = run_injected(body)
    no_traceback(proc)
    assert proc.returncode == 1, (proc.returncode, proc.stderr)
    assert "internal compiler error: the generated Go did not build" in proc.stderr, proc.stderr
    assert "line 2: cannot use \"x\"" in proc.stderr, proc.stderr
    assert ">>>    2 |     x: int = 2" in proc.stderr, proc.stderr
    assert "please report it" in proc.stderr, proc.stderr
    print("PASS: a Go build failure of generated code is an internal compiler error at the Lam line")


def test_go_block_failure_stays_user_error() -> None:
    proc = lamc("func main() {\n    go! { var typed string = 1 }\n}\n")
    no_traceback(proc)
    assert proc.returncode == 1
    assert "error: Go build failed for" in proc.stderr, proc.stderr
    assert "internal compiler error" not in proc.stderr, proc.stderr
    print("PASS: a Go error inside go! stays the user's Go build error")


def test_program_without_main() -> None:
    """Found by the fuzzer: a file without ``func main()`` failed in go build
    ("function main is undeclared in the main package")."""
    proc = lamc("# only declarations\nfunc helper() -> int {\n    return 1\n}\n")
    no_traceback(proc)
    assert proc.returncode == 1, proc.returncode
    assert "error[main]: the program has no `func main()`" in proc.stderr, proc.stderr
    assert "Go build failed" not in proc.stderr and "internal compiler error" not in proc.stderr, proc.stderr
    proc = lamc("")
    no_traceback(proc)
    assert "the program has no `func main()`" in proc.stderr, proc.stderr
    print("PASS: a program without main is a Lam error")


def main() -> int:
    tests = [
        test_invalid_utf8,
        test_random_bytes,
        test_deep_block_nesting,
        test_deep_expression_nesting,
        test_truncations_and_encodings,
        test_internal_exception_is_reported,
        test_generated_go_failure_is_internal_error,
        test_go_block_failure_stays_user_error,
        test_program_without_main,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failed += 1
            print(f"FAIL: {test.__name__}: {str(exc)[:3000]}")
    print(f"Crash guard results: {len(tests) - failed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
