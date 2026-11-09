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


def _start_dev(project: Path, *args: str, lamc: list[str] | None = None, entry: str = "main.lam",
               out: str = "dev.out", env_extra: dict[str, str] | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env.pop("LAMC_EXTLIBS", None)
    # Every test gets its own build cache (next to the project) so runs
    # never see each other's cached builds nor ~/.lammergeier/cache.
    env["LAMC_CACHE"] = str(project.parent / "lamc-cache")
    env.update(env_extra or {})
    log = open(project / out, "w", encoding="utf-8")
    return subprocess.Popen(
        (lamc or LAMC) + ["dev", entry, "--poll", "100", "--delay", "150", *args],
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


def _output(project: Path, out: str = "dev.out") -> str:
    return (project / out).read_text(encoding="utf-8")


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


# ── Cached builds ──────────────────────────────────────────────

FIRST_BUILD = "[lamc dev] building main.lam (no cached build; the first build can take a minute or two)"


def _log_lines(project: Path, log: str) -> list[str]:
    path = project / log
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line]


def _dev_once(project: Path, *args: str, log: str = "starts.log", **kwargs) -> tuple[str, float]:
    """Start `lamc dev`, wait for the program to start once, stop it.
    Returns lamc dev's output and the seconds until the program started."""
    before = len(_log_lines(project, log))
    started = time.monotonic()
    dev = _start_dev(project, *args, **kwargs)
    out = kwargs.get("out", "dev.out")
    try:
        try:
            _wait(lambda: len(_log_lines(project, log)) > before or dev.poll() is not None,
                  what="the program start")
        except AssertionError as exc:
            raise AssertionError(f"{exc}:\n{_output(project, out)}") from None
        elapsed = time.monotonic() - started
        assert dev.poll() is None, _output(project, out)
    finally:
        _stop(dev)
    return _output(project, out), elapsed


def _built(output: str) -> bool:
    return "[lamc dev] building" in output


def _cache_bins(project: Path) -> list[Path]:
    return sorted((project.parent / "lamc-cache").glob("dev/*/*/*.bin"))


def _assert_cached(output: str, elapsed: float) -> None:
    assert not _built(output), f"an unchanged start must not build:\n{output}"
    assert "(cached build of " in output, output
    assert elapsed < 5.0, f"a cached start took {elapsed:.1f}s:\n{output}"


def _project(tmp: str) -> Path:
    project = Path(tmp) / "app"
    _write(project / "lamlib.toml", '[library]\nname = "devapp"\nversion = "0.1.0"\n')
    _write(project / "main.lam", _program("v1"))
    return project


def test_cached_start_reuses_the_last_good_build() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        out, _ = _dev_once(project, "--", "serve")
        assert FIRST_BUILD in out, out
        assert "[lamc dev] starting main serve" in out, out
        assert len(_cache_bins(project)) == 1, _cache_bins(project)
        assert (project.parent / "lamc-cache" / "dev").is_dir(), "the cache lives under $LAMC_CACHE/dev"

        out, elapsed = _dev_once(project, "--", "serve")
        _assert_cached(out, elapsed)
        assert _starts(project)[-1] == "start v1 serve dev=1", _starts(project)

        # -o: the cached build is copied to PATH and run from there.
        binary = project / ".lamc-dev" / "devapp"
        dev = _start_dev(project, "-o", str(binary), "--", "serve")
        try:
            _wait(lambda: len(_starts(project)) == 3, what="the cached -o start")
            _assert_cached(_output(project), 0.0)
            assert binary.exists() and _children_alive(project), "the -o binary runs"
            # the watcher still rebuilds after a cached start
            time.sleep(0.3)
            _write(project / "main.lam", _program("v2"))
            _wait(lambda: len(_starts(project)) == 4, what="the rebuild after a cached start")
            assert _starts(project)[-1].startswith("start v2 "), _starts(project)
            assert _output(project).count("[lamc dev] building main.lam\n") == 1, _output(project)
        finally:
            _stop(dev)

        # the rebuild of the watch loop was stored too
        out, elapsed = _dev_once(project)
        _assert_cached(out, elapsed)
        assert _starts(project)[-1].startswith("start v2 "), _starts(project)

        # entries are keyed by content: the v1 build is still there
        _write(project / "main.lam", _program("v1"))
        out, elapsed = _dev_once(project)
        _assert_cached(out, elapsed)
        assert _starts(project)[-1].startswith("start v1 "), _starts(project)

        # --rebuild bypasses the cache; --no-cache (lamc build's) too
        for flag in ("--rebuild", "--no-cache"):
            out, _ = _dev_once(project, flag)
            assert _built(out) and "(cached build of " not in out, f"{flag}:\n{out}"

        # a changed lockfile or manifest invalidates
        _write(project / "lamlib.lock.toml", "# lockfile\nversion = 1\n")
        out, _ = _dev_once(project)
        assert FIRST_BUILD in out, out
        out, elapsed = _dev_once(project)
        _assert_cached(out, elapsed)
        _write(project / "lamlib.toml", '[library]\nname = "devapp"\nversion = "0.2.0"\n')
        out, _ = _dev_once(project)
        assert _built(out), out

        # a new .lam file under the project root invalidates (it could shadow a module)
        _write(project / "extra.lam", "func unused() -> int {\n    return 1\n}\n")
        out, _ = _dev_once(project)
        assert _built(out), out
    print("PASS: lamc dev starts the cached last good build when nothing changed")


def test_cache_prunes_old_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        for version in ("v1", "v2", "v3", "v4"):
            _write(project / "main.lam", _program(version))
            out, _ = _dev_once(project)
            assert _built(out), out
        bins = _cache_bins(project)
        assert len(bins) == 3, f"only the last 3 builds are kept: {bins}"
        assert len(list(bins[0].parent.glob("*.json"))) == 3, list(bins[0].parent.iterdir())
        leftovers = [p.name for p in bins[0].parent.iterdir()
                     if p.suffix not in (".bin", ".json") and p.name != ".lock"]
        assert leftovers == [], leftovers
        for version in ("v4", "v2"):
            _write(project / "main.lam", _program(version))
            out, elapsed = _dev_once(project)
            _assert_cached(out, elapsed)
        _write(project / "main.lam", _program("v1"))
        out, _ = _dev_once(project)
        assert _built(out), f"the oldest build was pruned:\n{out}"
    print("PASS: lamc dev keeps the last 3 cached builds per entry")


def test_damaged_cache_is_rebuilt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        out, _ = _dev_once(project)
        assert _built(out), out
        [binary] = _cache_bins(project)
        data = binary.read_bytes()
        binary.write_bytes(data[: len(data) // 2])            # truncated
        out, _ = _dev_once(project)
        assert "cached build is damaged" in out and _built(out), out
        assert _starts(project)[-1].startswith("start v1 "), _starts(project)

        [binary] = _cache_bins(project)
        data = bytearray(binary.read_bytes())
        data[len(data) // 2] ^= 0xFF                           # same size, other bytes
        binary.write_bytes(bytes(data))
        out, _ = _dev_once(project)
        assert "cached build is damaged" in out and _built(out), out

        [binary] = _cache_bins(project)
        binary.with_suffix(".json").write_text("{not json", encoding="utf-8")
        out, _ = _dev_once(project)
        assert _built(out), out
        out, elapsed = _dev_once(project)
        _assert_cached(out, elapsed)
        assert len(_cache_bins(project)) == 1, _cache_bins(project)
    print("PASS: lamc dev rebuilds a damaged cached build")


MODULE_PROGRAM = """
from lamos import Os
from lamtime import Time
from greet import word
from helper import tag

func main() {
    _ = Os.appendFile("starts.log", "start " + word() + " " + tag() + "\\n")
    while true {
        Time.sleepMs(50)
    }
}
"""


def _word_module(word: str, name: str = "word") -> str:
    return f'func {name}() -> str {{\n    return "{word}"\n}}\n'


def test_cache_tracks_modules_outside_the_project() -> None:
    """Modules found through LAMC_EXTLIBS (not watched) are recorded by the
    compiler: a changed module, or a new file that would now be picked for
    an import (an extlib shadowing a project module), invalidates."""
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        ext = Path(tmp) / "ext"
        _write(ext / "greet.lam", _word_module("hello"))
        _write(project / "helper.lam", _word_module("project", "tag"))
        _write(project / "main.lam", textwrap.dedent(MODULE_PROGRAM).lstrip())
        env = {"LAMC_EXTLIBS": str(ext)}
        out, _ = _dev_once(project, env_extra=env)
        assert _built(out) and _starts(project)[-1] == "start hello project", (out, _starts(project))
        out, elapsed = _dev_once(project, env_extra=env)
        _assert_cached(out, elapsed)

        _write(ext / "greet.lam", _word_module("ciao"))
        out, _ = _dev_once(project, env_extra=env)
        assert _built(out) and _starts(project)[-1] == "start ciao project", (out, _starts(project))
        out, elapsed = _dev_once(project, env_extra=env)
        _assert_cached(out, elapsed)

        # an unrelated file next to the used extlib does not invalidate
        _write(ext / "unrelated.lam", _word_module("x", "other"))
        out, elapsed = _dev_once(project, env_extra=env)
        _assert_cached(out, elapsed)

        # extlibs resolve before the project: this file now wins `helper`
        _write(ext / "helper.lam", _word_module("shadow", "tag"))
        out, _ = _dev_once(project, env_extra=env)
        assert _built(out) and _starts(project)[-1] == "start ciao shadow", (out, _starts(project))

        # the environment is part of the key
        other = Path(tmp) / "other-ext"
        shutil.copytree(ext, other)
        out, _ = _dev_once(project, env_extra={"LAMC_EXTLIBS": str(other)})
        assert _built(out), out
    print("PASS: lamc dev's cache tracks modules outside the project and shadowing files")


def _copy_compiler(dest: Path) -> list[str]:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("compiler", "lib"):
        shutil.copytree(ROOT / name, dest / name, ignore=ignore)
    for name in ("lammergeier.lark", "lamc"):
        shutil.copy2(ROOT / name, dest / name)
    if (ROOT / "stdlib").is_dir():
        shutil.copytree(ROOT / "stdlib", dest / "stdlib", ignore=ignore)
    return [sys.executable, str(dest / "compiler" / "lammergeier.py")]


def test_compiler_and_stdlib_changes_invalidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        checkout = Path(tmp) / "lamc-checkout"
        lamc = _copy_compiler(checkout)
        out, _ = _dev_once(project, lamc=lamc)
        assert _built(out), out
        out, elapsed = _dev_once(project, lamc=lamc)
        _assert_cached(out, elapsed)

        # a stdlib module the program imports
        with open(checkout / "lib" / "lamtime.lam", "a", encoding="utf-8") as fh:
            fh.write("\n# changed\n")
        out, _ = _dev_once(project, lamc=lamc)
        assert _built(out), f"a changed imported stdlib module invalidates:\n{out}"
        # a stdlib module the program does not import
        with open(checkout / "lib" / "lamcron.lam", "a", encoding="utf-8") as fh:
            fh.write("\n# changed\n")
        out, elapsed = _dev_once(project, lamc=lamc)
        _assert_cached(out, elapsed)

        # the compiler version
        version = checkout / "compiler" / "version.py"
        text = version.read_text(encoding="utf-8")
        version.write_text(_re_sub_version(text), encoding="utf-8")
        out, _ = _dev_once(project, lamc=lamc)
        assert _built(out), f"a new compiler version invalidates:\n{out}"
        out, elapsed = _dev_once(project, lamc=lamc)
        _assert_cached(out, elapsed)

        # the compiler's own sources (same version, changed checkout)
        with open(checkout / "compiler" / "timings.py", "a", encoding="utf-8") as fh:
            fh.write("\n# changed\n")
        out, _ = _dev_once(project, lamc=lamc)
        assert _built(out), f"a changed compiler source invalidates:\n{out}"

        # the repository's compiler does not reuse the copy's builds
        out, _ = _dev_once(project)
        assert _built(out), out
    print("PASS: lamc dev's cache is invalidated by stdlib and compiler changes")


def _re_sub_version(text: str) -> str:
    import re
    return re.sub(r'LAMC_VERSION = "[^"]+"', 'LAMC_VERSION = "99.0.0"', text)


def test_concurrent_entries_do_not_clobber() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        for name in ("a", "b"):
            _write(project / "cmd" / f"{name}.lam",
                   _program(name.upper()).replace("starts.log", f"{name}.log"))

        def both() -> tuple[str, str]:
            devs = {name: _start_dev(project, entry=f"cmd/{name}.lam", out=f"{name}.out")
                    for name in ("a", "b")}
            before = {name: len(_log_lines(project, f"{name}.log")) for name in devs}
            try:
                for name in devs:
                    _wait(lambda: len(_log_lines(project, f"{name}.log")) > before[name],
                          what=f"the start of {name}")
            finally:
                for dev in devs.values():
                    _stop(dev)
            return _output(project, "a.out"), _output(project, "b.out")

        outs = both()
        assert all(_built(out) for out in outs), outs
        outs = both()
        for out in outs:
            _assert_cached(out, 0.0)
        assert _log_lines(project, "a.log")[-1].startswith("start A "), _log_lines(project, "a.log")
        assert _log_lines(project, "b.log")[-1].startswith("start B "), _log_lines(project, "b.log")
        entries = sorted(p.parent.name for p in _cache_bins(project))
        assert len(entries) == 2 and entries[0] != entries[1], entries

        # two processes of the same entry: one builds, the other waits for it
        _write(project / "cmd" / "a.lam", _program("A2").replace("starts.log", "a.log"))
        devs = [_start_dev(project, entry="cmd/a.lam", out=f"same{i}.out") for i in (1, 2)]
        before = len(_log_lines(project, "a.log"))
        try:
            _wait(lambda: len(_log_lines(project, "a.log")) >= before + 2, what="both starts")
        finally:
            for dev in devs:
                _stop(dev)
        outs = [_output(project, f"same{i}.out") for i in (1, 2)]
        assert sum(_built(out) for out in outs) == 1, outs
        assert sum("(cached build of " in out for out in outs) == 1, outs
        assert all(line.startswith("start A2 ") for line in _log_lines(project, "a.log")[-2:])
    print("PASS: concurrent lamc dev runs share the cache safely")


def test_unusable_cache_still_builds() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        project = _project(tmp)
        _write(project.parent / "lamc-cache", "not a directory\n")
        for _ in range(2):
            out, _ = _dev_once(project)
            assert "build cache unavailable" in out and _built(out), out
        assert _starts(project)[-1].startswith("start v1 "), _starts(project)
    print("PASS: lamc dev builds without a usable cache")


def test_cache_key_inputs() -> None:
    sys.path.insert(0, str(ROOT))
    from compiler import dev_cli

    def key(argv: list[str], env: dict[str, str], go: str = "go version go1.25.3 linux/amd64") -> str:
        args = dev_cli.build_parser().parse_args(argv)
        saved = dict(os.environ)
        original = dev_cli._go_version
        try:
            for name in dev_cli.BUILD_ENV:
                os.environ.pop(name, None)
            os.environ.update(env)
            dev_cli._go_version = lambda: go
            return dev_cli.build_config_key(args)
        finally:
            os.environ.clear()
            os.environ.update(saved)
            dev_cli._go_version = original

    base = key(["main.lam"], {})
    assert key(["main.lam"], {}) == base, "the key is stable"
    assert key(["main.lam", "--watch", "data", "--quiet", "--rebuild"], {}) == base, "watch flags do not matter"
    for argv in (["--go-tags", "x"], ["--go-ldflags=-s"], ["--go-race"], ["--no-semantic-check"],
                 ["--extlibs", "/tmp"], ["--stdlib", "/tmp"]):
        assert key(["main.lam", *argv], {}) != base, argv
    for name, value in (("GOFLAGS", "-mod=mod"), ("GOOS", "darwin"), ("GOARCH", "arm64"),
                        ("CGO_ENABLED", "0"), ("LAMC_EXTLIBS", "/x")):
        assert key(["main.lam"], {name: value}) != base, name
    assert key(["main.lam"], {}, go="go version go1.26.0 linux/amd64") != base, "the Go version"
    # The worker count does not change the binary: same key, passed to the build.
    assert key(["main.lam", "--workers", "3"], {}) == base, "--workers does not matter"
    assert key(["main.lam"], {"LAMC_WORKERS": "2"}) == base, "LAMC_WORKERS does not matter"
    runner = dev_cli.DevRunner(dev_cli.build_parser().parse_args(["main.lam", "--workers", "3"]), [])
    try:
        command = runner.build_command()
    finally:
        shutil.rmtree(runner.tmpdir, ignore_errors=True)
    assert command[-2:] == ["--workers", "3"], command
    print("PASS: lamc dev's cache key covers flags, environment and the Go toolchain")


def main() -> int:
    tests = [
        test_watcher_rules,
        test_watcher_compares_content,
        test_cache_key_inputs,
        test_build_run_passes_arguments,
        test_help_lists_dev,
        test_rebuild_restart_and_last_good_build,
        test_crash_waits_for_changes,
        test_identical_rewrites_do_not_restart,
        test_cached_start_reuses_the_last_good_build,
        test_cache_prunes_old_entries,
        test_damaged_cache_is_rebuilt,
        test_cache_tracks_modules_outside_the_project,
        test_compiler_and_stdlib_changes_invalidate,
        test_concurrent_entries_do_not_clobber,
        test_unusable_cache_still_builds,
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
