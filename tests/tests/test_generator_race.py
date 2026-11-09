#!/usr/bin/env python3
"""Generators are state machines resumed by ``Next`` under the generator's
mutex (``GENERATOR_RUNTIME_GO``): the cases sharing one generator value
between concurrent consumers (async tasks), closing it while they iterate
it (``close()``), and the ones stopping generators when they are collected
(cleanups run on another goroutine, some reading a receiver held weakly),
run under the Go race detector (``--go-race``) must print their expected
output and report no data race. Skipped when the race detector is not
available (it needs cgo and a C compiler)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"
CASES = ROOT / "tests" / "tests" / "cases" / "generators"
NAMES = [
    "test_generator_shared_consumers.lam",
    "test_generator_self_cycle.lam",
    "test_generator_no_goroutine_leak.lam",
    "test_generator_close.lam",
    "test_generator_close_concurrent.lam",
    "test_generator_held_cycle.lam",
    "test_generator_held_alive.lam",
]

sys.path.insert(0, str(ROOT / "tests" / "tests"))
from run_tests import extract_expected  # noqa: E402


def race_available() -> bool:
    if shutil.which("go") is None:
        return False
    cgo = subprocess.run(["go", "env", "CGO_ENABLED"], capture_output=True, text=True).stdout.strip()
    cc = subprocess.run(["go", "env", "CC"], capture_output=True, text=True).stdout.strip() or "gcc"
    return cgo == "1" and shutil.which(cc.split()[0]) is not None


def test_generator_cases_are_race_free() -> None:
    with tempfile.TemporaryDirectory(prefix="lam_generator_race_") as tmp:
        for name in NAMES:
            source = CASES / name
            binary = Path(tmp) / source.stem
            build = subprocess.run(
                [sys.executable, str(COMPILER), str(source), "--go-race", "-o", str(binary)],
                capture_output=True, text=True, timeout=600,
            )
            assert build.returncode == 0, build.stderr
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=120,
                                 env={**os.environ, "GORACE": "halt_on_error=1"})
            assert "DATA RACE" not in run.stderr, run.stderr[-3000:]
            assert run.returncode == 0, run.stderr[-3000:]
            expected = extract_expected(source.read_text(encoding="utf-8"))
            assert run.stdout.rstrip("\n") == (expected or "").strip(), run.stdout
            print(f"PASS: {name} under the race detector")


if __name__ == "__main__":
    if not race_available():
        print("SKIP: the Go race detector needs cgo and a C compiler")
        sys.exit(0)
    test_generator_cases_are_race_free()
