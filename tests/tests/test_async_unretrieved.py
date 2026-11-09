#!/usr/bin/env python3
"""An exception raised in an async task that is never awaited is printed to
stderr when ``main`` returns ("Task exception was never retrieved: ..."),
instead of disappearing; an awaited one is re-raised at the ``await`` and not
reported again. The exit status is unchanged (like Python's asyncio).

Failures do not accumulate: the exception of a task whose handle is
collected without an ``await`` is reported then (a long-running program
sees it without reaching the end of ``main``), and one raised after the
task already published its result is reported at once, never kept."""

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


# Fire-and-forget tasks: 200 failures whose handles are dropped, then a
# task raising from a ``defer`` after its result was published. The program
# reports how many failures the registry still holds once the collector ran
# (``pending``), then marks the end of ``main`` on stderr.
DROPPED_SOURCE = """\
go! {
    import (
        "fmt"
        "os"
        "runtime"
        "time"
    )
}

from lamtime import Time

async func load(id: int) -> str {
    if id < 0 {
        raise ValueError(f"bad id {id}")
    }
    return f"item {id}"
}

func explode() {
    raise RuntimeError("after publish")
}

async func publishThenFail() -> int {
    defer explode()
    return 1
}

func pending() -> int {
    n: int = 0
    go! {
        for i := 0; i < 400; i++ {
            runtime.GC()
            n = 0
            lamAsyncFailures.Range(func(_, _ interface{}) bool { n++; return true })
            if n == 0 {
                break
            }
            time.Sleep(5 * time.Millisecond)
        }
    }
    return n
}

func main() {
    for i in range(200) {
        load(-1 - i)
    }
    late = publishThenFail()
    Time.sleepMs(200)
    print(await late)
    print(await load(7))
    print("pending", pending())
    go! {
        fmt.Fprintln(os.Stderr, "main end")
    }
}
"""


def _build_and_run(source: str):
    with tempfile.TemporaryDirectory(prefix="lam_async_") as tmp:
        src = Path(tmp) / "main.lam"
        src.write_text(source, encoding="utf-8")
        binary = Path(tmp) / "app"
        build = subprocess.run(
            [sys.executable, str(COMPILER), str(src), "-o", str(binary)],
            capture_output=True, text=True, timeout=300,
        )
        assert build.returncode == 0, build.stderr
        return subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)


def test_dropped_and_late_task_exceptions_do_not_accumulate() -> None:
    run = _build_and_run(DROPPED_SOURCE)
    assert run.returncode == 0, (run.returncode, run.stderr[-2000:])
    assert run.stdout.splitlines() == ["1", "item 7", "pending 0"], run.stdout
    lines = run.stderr.strip().splitlines()
    assert lines and lines[-1] == "main end", run.stderr[-2000:]
    reports = lines[:-1]
    expected = {f"Task exception was never retrieved: ValueError: bad id {-1 - i}" for i in range(200)}
    expected.add("Task exception was never retrieved: RuntimeError: after publish")
    assert len(reports) == len(expected) and set(reports) == expected, run.stderr[-2000:]
    print("PASS: dropped and late async task exceptions are reported once, while main runs")


def test_unawaited_task_exception_is_reported() -> None:
    run = _build_and_run(SOURCE)
    assert run.returncode == 0, run.returncode
    assert run.stdout.splitlines() == ["caught ValueError: bad id -2", "item 3"], run.stdout
    assert run.stderr.strip() == "Task exception was never retrieved: ValueError: bad id -1", run.stderr
    print("PASS: an unawaited async task exception is reported on stderr")


if __name__ == "__main__":
    test_unawaited_task_exception_is_reported()
    test_dropped_and_late_task_exceptions_do_not_accumulate()
