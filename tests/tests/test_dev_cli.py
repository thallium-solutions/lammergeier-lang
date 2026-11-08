#!/usr/bin/env python3
"""Tests for ``lamc dev``: rebuild and restart a Lam program on changes."""

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
LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]

PROGRAM = """
from lamos import Os
from lamtime import Time

func main() {
    args: list[str] = Os.args()
    rest: str = " ".join(args[1:])
    dev: str = Os.getenv("LAMC_DEV").unwrapOr("")
    _ = Os.appendFile("starts.log", "start VERSION " + rest + " dev=" + dev + "\\n")
    if "crash" in args {
        exit(3)
    }
    while true {
        Time.sleepMs(50)
    }
}
"""


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _program(version: str) -> str:
    return textwrap.dedent(PROGRAM).replace("VERSION", version).lstrip()


def _starts(project: Path) -> list[str]:
    log = project / "starts.log"
    if not log.exists():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def _wait(predicate, timeout: float = 120.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def _start_dev(project: Path, *args: str) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("LAMC_EXTLIBS", None)
    log = open(project / "dev.out", "w", encoding="utf-8")
    return subprocess.Popen(
        LAMC + ["dev", "main.lam", "--poll", "100", "--delay", "150", *args],
        cwd=str(project),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )


def _stop(proc: subprocess.Popen) -> int:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            return proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise AssertionError("lamc dev did not stop on SIGINT")
    return proc.returncode


def _output(project: Path) -> str:
    return (project / "dev.out").read_text(encoding="utf-8")


def _children_alive(project: Path) -> bool:
    proc = subprocess.run(["pgrep", "-f", str(project)], capture_output=True, text=True)
    return bool(proc.stdout.strip())


def test_rebuild_restart_and_last_good_build() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "app"
        _write(project / "lamlib.toml", '[library]\nname = "devapp"\nversion = "0.1.0"\n')
        _write(project / "main.lam", _program("v1"))
        _write(project / "data" / "settings.json", "{}\n")
        binary = project / ".lamc-dev" / "devapp"
        dev = _start_dev(project, "--watch", "data", "-o", str(binary), "--", "serve", "--port=1")
        try:
            _wait(lambda: len(_starts(project)) == 1, what="first start")
            assert binary.exists(), "-o sets the binary path"
            assert _children_alive(project), "the program runs from the -o binary"
            assert _starts(project)[0] == "start v1 serve --port=1 dev=1", _starts(project)

            time.sleep(0.3)
            _write(project / "main.lam", _program("v2"))
            _wait(lambda: len(_starts(project)) == 2, what="rebuild after a .lam change")
            assert _starts(project)[1].startswith("start v2 serve"), _starts(project)

            time.sleep(0.3)
            _write(project / "main.lam", _program("v3").replace("func main() {", "func main( {"))
            _wait(lambda: "build failed" in _output(project), what="the build failure report")
            time.sleep(1.0)
            assert len(_starts(project)) == 2, "a failed build must not restart the program"
            assert dev.poll() is None

            _write(project / "main.lam", _program("v3"))
            _wait(lambda: len(_starts(project)) == 3, what="rebuild after the fix")
            assert _starts(project)[2].startswith("start v3 "), _starts(project)
            builds = _output(project).count("[lamc dev] building")

            time.sleep(0.3)
            _write(project / "data" / "settings.json", '{"changed": true}\n')
            _wait(lambda: len(_starts(project)) == 4, what="restart after a watched file change")
            assert _starts(project)[3].startswith("start v3 "), _starts(project)
            assert _output(project).count("[lamc dev] building") == builds, "non-source changes restart without rebuilding"
        finally:
            code = _stop(dev)
        assert code == 0, _output(project)
        time.sleep(0.5)
        assert not _children_alive(project), "the program must stop with lamc dev"
    print("PASS: lamc dev rebuilds, keeps the last good build and restarts on watched files")


def test_crash_waits_for_changes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "app"
        _write(project / "main.lam", _program("v1"))
        dev = _start_dev(project, "--", "crash")
        try:
            _wait(lambda: "exited with code 3" in _output(project), what="the crash report")
            time.sleep(1.0)
            assert len(_starts(project)) == 1, "a crashed program is not restarted in a loop"
            _write(project / "main.lam", _program("v2"))
            _wait(lambda: len(_starts(project)) == 2, what="rebuild after the crash")
        finally:
            _stop(dev)
    print("PASS: lamc dev waits for changes after a crash")


def test_watcher_rules() -> None:
    sys.path.insert(0, str(ROOT))
    from compiler import dev_cli

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel in ("main.lam", "lamlib.toml", "app/x.lam", "app/notes.txt", "locales/it.json",
                    "node_modules/pkg/y.lam", "tests/t.lam", "extra/a.bin", ".git/HEAD"):
            _write(root / rel, rel)
        watcher = dev_cli.Watcher(root, dev_cli._extensions(["json, .sql"]), [root / "extra"], ["tests"])
        seen = sorted(os.path.relpath(p, root) for p in watcher.snapshot())
        assert seen == ["app/x.lam", "extra/a.bin", "lamlib.toml", "locales/it.json", "main.lam"], seen

        before = watcher.snapshot()
        time.sleep(0.01)
        _write(root / "locales" / "it.json", "{}")
        (root / "app" / "x.lam").unlink()
        changes = dev_cli.changed_paths(before, watcher.snapshot())
        assert [os.path.relpath(p, root) for p in changes] == ["app/x.lam", "locales/it.json"], changes
        assert dev_cli.changed_paths(before, before) == []
    assert dev_cli.needs_build(["/p/a.lam"]) and dev_cli.needs_build(["/p/lamlib.lock.toml"])
    assert not dev_cli.needs_build(["/p/locales/it.json", "/p/m/001.sql"])
    assert dev_cli._project_root(Path("/nonexistent/dir/main.lam")) == Path("/nonexistent/dir")
    print("PASS: lamc dev watch rules (extensions, ignores, extra paths, build triggers)")


def _bump_mtime(path: Path, seconds: float = 5.0) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + int(seconds * 1e9)))


def test_watcher_compares_content() -> None:
    """Only content changes count: touching a file or rewriting it with the
    same bytes (`lamc install --frozen` re-copying extlibs/) is not a change,
    and only files whose mtime/size changed are hashed."""
    sys.path.insert(0, str(ROOT))
    from compiler import dev_cli

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for i in range(20):
            _write(root / "src" / f"m{i}.lam", f"// module {i}\n")
        _write(root / "lamlib.toml", '[library]\nname = "x"\n')
        _write(root / "extlibs" / "@lam" / "pkg" / "lib.lam", "// pkg\n")
        watcher = dev_cli.Watcher(root, dev_cli._extensions([]), [root / "extlibs"], [])
        base = watcher.snapshot()

        hashed: list[str] = []
        original = dev_cli._content_digest

        def counting(path: Path):
            hashed.append(os.path.relpath(path, root))
            return original(path)

        dev_cli._content_digest = counting
        try:
            assert dev_cli.changed_paths(base, watcher.snapshot()) == []
            assert hashed == [], f"unchanged stat must not rehash: {hashed}"

            # touch: new mtime, same bytes
            _bump_mtime(root / "src" / "m3.lam")
            touched = watcher.snapshot()
            assert dev_cli.changed_paths(base, touched) == [], "a touch is not a change"
            assert hashed == ["src/m3.lam"], f"only the touched file is hashed: {hashed}"

            # identical rewrite, including delete + recreate as an install does
            hashed.clear()
            pkg = root / "extlibs" / "@lam" / "pkg"
            shutil.rmtree(root / "extlibs")
            _write(pkg / "lib.lam", "// pkg\n")
            _bump_mtime(pkg / "lib.lam")
            (root / "lamlib.toml").write_text('[library]\nname = "x"\n', encoding="utf-8")
            _bump_mtime(root / "lamlib.toml")
            rewritten = watcher.snapshot()
            assert dev_cli.changed_paths(base, rewritten) == [], "identical rewrites are not changes"
            assert sorted(hashed) == ["extlibs/@lam/pkg/lib.lam", "lamlib.toml"], hashed

            # a real change with the same size and a new mtime is a change
            _write(root / "src" / "m4.lam", "// module 9\n")
            _bump_mtime(root / "src" / "m4.lam")
            changed = watcher.snapshot()
            assert [os.path.relpath(p, root) for p in dev_cli.changed_paths(base, changed)] == ["src/m4.lam"]

            # deletion and creation stay changes
            (root / "src" / "m5.lam").unlink()
            _write(root / "src" / "new.lam", "// new\n")
            after = watcher.snapshot()
            assert [os.path.relpath(p, root) for p in dev_cli.changed_paths(changed, after)] == [
                "src/m5.lam", "src/new.lam"]
        finally:
            dev_cli._content_digest = original
    print("PASS: lamc dev compares content and hashes only files whose stat changed")


def test_identical_rewrites_do_not_restart() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp) / "app"
        _write(project / "lamlib.toml", '[library]\nname = "devapp"\nversion = "0.1.0"\n')
        _write(project / "main.lam", _program("v1"))
        _write(project / "data" / "settings.json", "{}\n")
        _write(project / "extlibs" / "@lam" / "pkg" / "lib.lam", "// pkg\n")
        dev = _start_dev(project, "--watch", "data", "-o", str(project / ".lamc-dev" / "devapp"))
        try:
            _wait(lambda: len(_starts(project)) == 1, what="first start")
            builds = _output(project).count("[lamc dev] building")
            assert builds == 1, _output(project)

            time.sleep(0.3)
            _bump_mtime(project / "main.lam")                       # touch
            _write(project / "lamlib.toml", '[library]\nname = "devapp"\nversion = "0.1.0"\n')
            _bump_mtime(project / "lamlib.toml")                    # identical rewrite
            _write(project / "data" / "settings.json", "{}\n")      # watched non-source file
            _bump_mtime(project / "data" / "settings.json")
            shutil.rmtree(project / "extlibs")                      # reinstall identical package
            time.sleep(0.05)
            _write(project / "extlibs" / "@lam" / "pkg" / "lib.lam", "// pkg\n")
            time.sleep(1.5)
            assert len(_starts(project)) == 1, f"identical content must not restart: {_starts(project)}"
            assert _output(project).count("[lamc dev] building") == 1, _output(project)
            assert "changed:" not in _output(project), _output(project)

            _write(project / "main.lam", _program("v2"))
            _wait(lambda: len(_starts(project)) == 2, what="rebuild after a real change")
            assert _starts(project)[1].startswith("start v2"), _starts(project)
            assert _output(project).count("[lamc dev] building") == 2, _output(project)

            time.sleep(0.3)
            (project / "data" / "settings.json").unlink()
            _wait(lambda: len(_starts(project)) == 3, what="restart after a watched file deletion")
            assert _output(project).count("[lamc dev] building") == 2, "a deleted non-source file only restarts"
        finally:
            _stop(dev)
    print("PASS: lamc dev ignores touches and identical rewrites, acts on content changes")


def test_build_run_passes_arguments() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = Path(tmp)
        _write(project / "args.lam", 'from lamos import Os\n\nfunc main() {\n    print("|".join(Os.args()[1:]))\n}\n')
        proc = subprocess.run(LAMC + ["build", "args.lam", "-o", str(project / "args"), "--run", "--",
                                      "migrate", "--dry-run", "two words"],
                              cwd=str(project), capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "migrate|--dry-run|two words", proc.stdout
        refused = subprocess.run(LAMC + ["build", "args.lam", "--", "x"], cwd=str(project),
                                 capture_output=True, text=True)
        assert refused.returncode == 2 and "need --run" in refused.stderr, refused.stderr
    print("PASS: lamc build --run passes the arguments after -- to the program")


def test_help_lists_dev() -> None:
    proc = subprocess.run(LAMC + ["--help"], capture_output=True, text=True)
    assert proc.returncode == 0
    assert "  dev " in proc.stdout
    sub = subprocess.run(LAMC + ["dev", "--help"], capture_output=True, text=True)
    assert sub.returncode == 0
    assert "--watch" in sub.stdout and "--ignore" in sub.stdout
    print("PASS: lamc dev is documented in the help")


def main() -> int:
    tests = [
        test_watcher_rules,
        test_watcher_compares_content,
        test_build_run_passes_arguments,
        test_help_lists_dev,
        test_rebuild_restart_and_last_good_build,
        test_crash_waits_for_changes,
        test_identical_rewrites_do_not_restart,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL: {test.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"ERROR: {test.__name__}: {exc!r}")
    print()
    print("=" * 60)
    if failures:
        print(f"lamc dev: {failures} of {len(tests)} tests failed")
        return 1
    print(f"lamc dev: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
