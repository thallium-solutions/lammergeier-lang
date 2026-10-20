#!/usr/bin/env python3
"""An exception raised in an async task that is never awaited is printed to
stderr when ``main`` returns ("Task exception was never retrieved: ..."),
instead of disappearing; an awaited one is re-raised at the ``await`` and not
reported again. The exit status is unchanged (like Python's asyncio)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"

SOURCE = """\
from lamtime import Time

async func load(id: int) -> str {
    if id < 0 {
        raise ValueError(f"bad id {id}")
    }
    return f"item {id}"
}

func main() {
    lost = load(-1)
    seen = load(-2)
    fine = load(3)
    try {
        print(await seen)
    } catch ValueError as e {
        print(f"caught {e}")
    }
    print(await fine)
    Time.sleepMs(100)
    _ = lost
}
"""


def test_unawaited_task_exception_is_reported() -> None:
    with tempfile.TemporaryDirectory(prefix="lam_async_") as tmp:
        src = Path(tmp) / "main.lam"
        src.write_text(SOURCE, encoding="utf-8")
        binary = Path(tmp) / "app"
        build = subprocess.run(
            [sys.executable, str(COMPILER), str(src), "-o", str(binary)],
            capture_output=True, text=True, timeout=300,
        )
        assert build.returncode == 0, build.stderr
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.returncode
    assert run.stdout.splitlines() == ["caught ValueError: bad id -2", "item 3"], run.stdout
    assert run.stderr.strip() == "Task exception was never retrieved: ValueError: bad id -1", run.stderr
    print("PASS: an unawaited async task exception is reported on stderr")


if __name__ == "__main__":
    test_unawaited_task_exception_is_reported()
