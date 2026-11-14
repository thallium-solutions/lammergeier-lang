#!/usr/bin/env python3
"""Process-environment contracts of the stdlib: the zone database compiled
into programs that use time zones, and OS signal delivery.

The zone checks run the built program in a private mount namespace where
``/usr/share/zoneinfo`` and ``$GOROOT/lib/time`` are empty (what a minimal
container sees); they are skipped when unprivileged namespaces are not
available. The signal checks send real signals to a running program.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"


def _build(source: str, td: Path, name: str = "app") -> Path:
    lam_file = td / f"{name}.lam"
    lam_file.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    binary = td / name
    built = subprocess.run(
        [sys.executable, str(COMPILER), str(lam_file), "-o", str(binary)],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    assert built.returncode == 0, built.stderr + built.stdout
    return binary


def _emit_go(source: str, td: Path) -> str:
    lam_file = td / "emit.lam"
    lam_file.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(COMPILER), str(lam_file), "--emit-go"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    return result.stdout


def _goroot() -> str:
    return subprocess.run(["go", "env", "GOROOT"], capture_output=True, text=True).stdout.strip()


def _without_system_zones(binary: Path) -> subprocess.CompletedProcess | None:
    """Run ``binary`` with no zone files on the system (None: unsupported)."""
    unshare = shutil.which("unshare")
    if not unshare:
        return None
    hidden = [p for p in ("/usr/share/zoneinfo", os.path.join(_goroot(), "lib", "time")) if os.path.isdir(p)]
    mounts = " && ".join(f"mount -t tmpfs none '{p}'" for p in hidden)
    probe = subprocess.run([unshare, "-rm", "sh", "-c", f"{mounts} && true"], capture_output=True, text=True)
    if probe.returncode != 0:
        return None
    return subprocess.run(
        [unshare, "-rm", "sh", "-c", f"{mounts} && exec '{binary}'"],
        capture_output=True, text=True, timeout=30, env={**os.environ, "ZONEINFO": "/nonexistent"},
    )


ZONES_PROGRAM = """
from lamdatetime import DateTime
from lamerrors import Result

func show(zone: str) -> str {
    when: Result[DateTime] = DateTime.fromUnix(1783000000, zone)
    if not when.ok() {
        return f"{zone} unavailable"
    }
    return when.unwrap().format("2006-01-02 15:04 -07:00")
}

func main() {
    for zone in ["Europe/Rome", "America/New_York", "Asia/Kolkata", "Australia/Lord_Howe"] {
        print(show(zone))
    }
    winter: DateTime = DateTime.fromUnix(1767225600, "Europe/Rome").unwrap()
    print(winter.format("MST"), winter.offsetSeconds())
    moved: DateTime = winter.inLocation("Asia/Tokyo").unwrap()
    print(moved.format("2006-01-02 15:04 MST"))
}
"""

ZONES_EXPECTED = [
    "2026-07-02 15:46 +02:00",
    "2026-07-02 09:46 -04:00",
    "2026-07-02 19:16 +05:30",
    "2026-07-03 00:16 +10:30",
    "CET 3600",
    "2026-01-01 09:00 JST",
]


def test_generated_go_embeds_zone_database_only_for_zone_apis() -> None:
    with tempfile.TemporaryDirectory() as td:
        zoned = _emit_go(ZONES_PROGRAM, Path(td))
        assert '_ "time/tzdata"' in zoned, "lamdatetime programs must embed time/tzdata"
        cron = _emit_go(
            """
            from lamcron import Cron
            func main() {
                c: Cron = Cron.new()
                print(c.count())
            }
            """,
            Path(td),
        )
        assert '_ "time/tzdata"' in cron, "lamcron programs (CRON_TZ=...) must embed time/tzdata"
        rows = _emit_go(
            """
            from lamdb import Row
            func main() {
                print(Row.unix({"at": "2026-11-03"}, "at"))
            }
            """,
            Path(td),
        )
        assert '_ "time/tzdata"' in rows, "lamdb (Row.time returns a DateTime) embeds time/tzdata"
        plain = _emit_go(
            """
            from lamtime import Time
            func main() {
                print(Time.nowUnix() > 0)
            }
            """,
            Path(td),
        )
        assert "time/tzdata" not in plain, "programs without zone APIs stay small"


def test_datetime_zones_without_system_tzdata() -> None:
    with tempfile.TemporaryDirectory() as td:
        binary = _build(ZONES_PROGRAM, Path(td))
        normal = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30)
        assert normal.stdout.split("\n")[:6] == ZONES_EXPECTED, normal.stdout + normal.stderr
        isolated = _without_system_zones(binary)
        if isolated is None:
            print("  skip: unprivileged mount namespaces unavailable")
            return
        assert isolated.returncode == 0, isolated.stderr
        assert isolated.stdout.split("\n")[:6] == ZONES_EXPECTED, isolated.stdout + isolated.stderr


def test_cron_zone_without_system_tzdata() -> None:
    with tempfile.TemporaryDirectory() as td:
        binary = _build(
            """
            from lamcron import Cron
            from lamerrors import Result

            func noop() {
                pass
            }

            func main() {
                c: Cron = Cron.new()
                rome: Result[int] = c.schedule("CRON_TZ=Europe/Rome 0 9 * * *", noop)
                print("rome", rome.ok())
                bad: Result[int] = c.schedule("CRON_TZ=Mars/Olympus 0 9 * * *", noop)
                print("mars", bad.ok(), c.count())
            }
            """,
            Path(td),
        )
        isolated = _without_system_zones(binary)
        if isolated is None:
            print("  skip: unprivileged mount namespaces unavailable")
            return
        assert isolated.returncode == 0, isolated.stderr
        assert isolated.stdout.split("\n")[:2] == ["rome true", "mars false 1"], isolated.stdout + isolated.stderr


def _start(binary: Path, *args: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [str(binary), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline().strip()
    assert line == "ready", line + (proc.stderr.read() if proc.stderr else "")
    return proc


def _finish(proc: subprocess.Popen) -> tuple[int, list[str], str]:
    out, err = proc.communicate(timeout=20)
    return proc.returncode, [l for l in out.split("\n") if l], err


WAIT_PROGRAM = """
from lamos import Os

func main() {
    args: list[str] = Os.args()
    names: list[str] = args[1:]
    print("ready")
    received: str = Os.waitForSignal(names) if len(names) > 0 else Os.waitForSignal()
    print(f"got {received}")
}
"""


def test_wait_for_signal_returns_the_signal_name() -> None:
    with tempfile.TemporaryDirectory() as td:
        binary = _build(WAIT_PROGRAM, Path(td))
        for args, sent, expected in [
            ((), signal.SIGTERM, "got SIGTERM"),
            ((), signal.SIGINT, "got SIGINT"),
            (("SIGHUP",), signal.SIGHUP, "got SIGHUP"),
            (("hup", "quit"), signal.SIGQUIT, "got SIGQUIT"),
        ]:
            proc = _start(binary, *args)
            time.sleep(0.5)  # "ready" is printed just before waitForSignal registers
            proc.send_signal(sent)
            code, lines, err = _finish(proc)
            assert code == 0 and lines == [expected], (args, code, lines, err)


def test_wait_for_signal_rejects_unknown_names() -> None:
    with tempfile.TemporaryDirectory() as td:
        binary = _build(
            """
            from lamos import Os

            func main() {
                try {
                    print(Os.waitForSignal(["SIGTERM", "SIGNOPE"]))
                } catch ValueError as e {
                    print(f"error {e}")
                }
                try {
                    print(Os.waitForSignal([]))
                } catch ValueError as e {
                    print(f"error {e}")
                }
            }
            """,
            Path(td),
        )
        run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
        assert run.returncode == 0, run.stderr
        assert run.stdout.split("\n")[:2] == [
            'error ValueError: unknown signal "SIGNOPE" (supported: SIGINT, SIGTERM, SIGHUP, SIGQUIT)',
            "error ValueError: no signal names given",
        ], run.stdout


CONTEXT_PROGRAM = """
from lamcontext import Context
from lamtime import Time

class Worker {
    func init(self, name: str) {
        self.name: str = name
        self.rounds: int = 0
    }

    async func run(self, ctx: Context) -> int {
        while not ctx.wait(20) {
            self.rounds += 1
        }
        return self.rounds
    }
}

func main() {
    root: Context = Context.onSignals(["SIGTERM", "SIGINT"])
    scoped: Context = root.valued("job", "sync")
    a: Worker = Worker("a")
    b: Worker = Worker("b")
    first = a.run(scoped.child())
    second = b.run(scoped.child())
    print("ready")
    total: int = await first
    total += await second
    print(f"stopped {root.reason()} {scoped.reason()} {scoped['job']} {root.expired()}")
    print(f"busy {total > 0 and a.rounds > 0 and b.rounds > 0}")
    # A second signal is no longer handled: the default action ends the process.
    Time.sleepMs(5000)
    print("still running")
}
"""


def test_context_on_signals_cancels_and_restores_default_handling() -> None:
    with tempfile.TemporaryDirectory() as td:
        binary = _build(CONTEXT_PROGRAM, Path(td))
        proc = _start(binary)
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        assert proc.stdout is not None
        first = proc.stdout.readline().strip()
        second = proc.stdout.readline().strip()
        assert first == "stopped SIGTERM SIGTERM sync true", first
        assert second == "busy true", second
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        code, lines, _err = _finish(proc)
        assert code == -signal.SIGTERM, code
        assert lines == [], lines
        assert time.monotonic() - started < 3


def test_context_on_signals_stops_listening_when_cancelled() -> None:
    with tempfile.TemporaryDirectory() as td:
        binary = _build(
            """
            from lamcontext import Context
            from lamtime import Time

            func main() {
                ctx: Context = Context.onSignals(["SIGHUP"])
                print(ctx.expired(), ctx.reason())
                ctx.cancel("done")
                print(ctx.expired(), ctx.reason())
                print("ready")
                Time.sleepMs(5000)
                print("still running")
            }
            """,
            Path(td),
        )
        proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "false None"
        assert proc.stdout.readline().strip() == "true done"
        assert proc.stdout.readline().strip() == "ready"
        time.sleep(0.3)
        proc.send_signal(signal.SIGHUP)
        code, lines, _err = _finish(proc)
        assert code == -signal.SIGHUP, (code, lines)


def test_context_on_signals_cancelled_under_load_restores_default() -> None:
    """A signal that arrives right after ``cancel()``, before the watcher
    stopped listening, still gets the default handling (here: many
    contexts cancelled back to back, then the signal at once)."""
    with tempfile.TemporaryDirectory() as td:
        binary = _build(
            """
            from lamcontext import Context
            from lamtime import Time

            func main() {
                for i in range(200) {
                    ctx: Context = Context.onSignals(["SIGHUP"])
                    ctx.cancel("done")
                }
                last: Context = Context.onSignals(["SIGHUP"])
                last.cancel("done")
                print("ready")
                Time.sleepMs(5000)
                print("still running")
            }
            """,
            Path(td),
        )
        for _ in range(5):
            proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert proc.stdout is not None
            assert proc.stdout.readline().strip() == "ready"
            proc.send_signal(signal.SIGHUP)
            code, lines, _err = _finish(proc)
            assert code == -signal.SIGHUP, (code, lines)


def main() -> int:
    tests = [
        test_generated_go_embeds_zone_database_only_for_zone_apis,
        test_datetime_zones_without_system_tzdata,
        test_cron_zone_without_system_tzdata,
        test_wait_for_signal_returns_the_signal_name,
        test_wait_for_signal_rejects_unknown_names,
        test_context_on_signals_cancels_and_restores_default_handling,
        test_context_on_signals_stops_listening_when_cancelled,
        test_context_on_signals_cancelled_under_load_restores_default,
    ]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\nstdlib process-environment contracts: {len(tests) - failed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
