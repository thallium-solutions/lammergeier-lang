#!/usr/bin/env python3
"""``lamc build --go-pgo PROFILE``: profile-guided optimisation. A program
writes its own CPU profile (``runtime/pprof`` in a ``go!`` block, the
recipe docs/TRANSPILATION.md gives), is rebuilt with it (``go build
-pgo=<absolute path>``) and still prints the same; a missing profile is
an error before anything is built."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"

PROGRAM = """\
go! {
    import "os"
    import "runtime/pprof"
}

func fib(n: int) -> int {
    if n < 2 {
        return n
    }
    return fib(n - 1) + fib(n - 2)
}

func main() {
    go! {
        if path := os.Getenv("CPU_PROFILE"); path != "" {
            f, err := os.Create(path)
            if err == nil {
                pprof.StartCPUProfile(f)
                defer pprof.StopCPUProfile()
            }
        }
    }
    print(fib(25))
}
"""


def _lamc(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(COMPILER), *args], cwd=cwd,
                          capture_output=True, text=True, timeout=600)


def test_build_with_a_profile() -> None:
    with tempfile.TemporaryDirectory(prefix="lam_pgo_") as tmp:
        root = Path(tmp)
        (root / "main.lam").write_text(PROGRAM, encoding="utf-8")
        built = _lamc("build", "main.lam", "-o", "app", cwd=root)
        assert built.returncode == 0, built.stderr
        run = subprocess.run([str(root / "app")], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "CPU_PROFILE": str(root / "cpu.pprof")})
        assert run.returncode == 0 and run.stdout == "75025\n", run.stdout + run.stderr
        assert (root / "cpu.pprof").stat().st_size > 0
        rebuilt = _lamc("build", "main.lam", "-o", "app-pgo", "--go-pgo", "cpu.pprof", "-v", cwd=root)
        assert rebuilt.returncode == 0, rebuilt.stderr
        assert f"-pgo={root / 'cpu.pprof'}" in rebuilt.stderr, rebuilt.stderr
        again = subprocess.run([str(root / "app-pgo")], capture_output=True, text=True, timeout=60)
        assert again.stdout == "75025\n", again.stdout + again.stderr
        print("PASS: lamc build --go-pgo builds with the profile")


def test_missing_profile_is_an_error() -> None:
    with tempfile.TemporaryDirectory(prefix="lam_pgo_") as tmp:
        root = Path(tmp)
        (root / "main.lam").write_text(PROGRAM, encoding="utf-8")
        built = _lamc("build", "main.lam", "-o", "app", "--go-pgo", "missing.pprof", cwd=root)
        assert built.returncode == 1, built.stderr
        assert "error: --go-pgo profile not found: missing.pprof" in built.stderr, built.stderr
        assert not (root / "app").exists()
        print("PASS: a missing --go-pgo profile is an error")


if __name__ == "__main__":
    test_build_with_a_profile()
    test_missing_profile_is_an_error()
