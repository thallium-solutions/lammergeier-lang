"""``lamc dev``: build a Lam program, run it, and rebuild/restart it when
its files change (the nodemon workflow for compiled Lam programs).

* A ``.lam`` or ``lamlib*.toml`` change rebuilds, then restarts.
* Any other watched change (``--watch`` paths, ``--ext`` extensions)
  restarts without rebuilding.
* A failed build keeps the previous build running; the next change
  retries.
* A program that exits by itself is not restarted until a change (or
  ``rs`` + Enter on an interactive terminal).
* The program runs with ``LAMC_DEV=1`` in its environment, in its own
  process group, stopped with ``--signal`` (SIGTERM) and killed after
  ``--kill-timeout``.

Change detection polls modification times, so it also works on bind
mounts inside containers, where file-system events are not delivered.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LAMC = [sys.executable, str(Path(__file__).resolve().parent / "lammergeier.py")]
DEFAULT_IGNORES = (".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", ".lamc-dev")
BUILD_FILES = ("lamlib.toml", "lamlib.lock.toml")
PREFIX = "[lamc dev]"

Snapshot = Dict[str, Tuple[int, int]]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="lamc dev",
        description=(
            "Build and run a Lam program, then rebuild and restart it when "
            "its sources change. Arguments after `--` are passed to the program."
        ),
        epilog=(
            "Watched by default: every .lam file and lamlib*.toml under the "
            "project root (the directory of the nearest lamlib.toml, else the "
            "source's directory). Type `rs` + Enter to rebuild by hand."
        ),
    )
    ap.add_argument("source", help="Entry-point .lam file")
    ap.add_argument("--watch", action="append", default=[], metavar="PATH",
                    help="Extra file or directory to watch; a change restarts the "
                         "program (and rebuilds when it is a .lam file). Repeatable.")
    ap.add_argument("--ext", action="append", default=[], metavar="EXT",
                    help="Extra extension(s) watched under the project root, e.g. "
                         "`--ext json,sql`. Repeatable.")
    ap.add_argument("--ignore", action="append", default=[], metavar="PATTERN",
                    help="Glob of paths (relative to the project root) or names to "
                         "ignore, e.g. `--ignore 'tests/*'`. Repeatable.")
    ap.add_argument("--root", help="Project root to watch (default: the directory "
                                   "of the nearest lamlib.toml)")
    ap.add_argument("--delay", type=int, default=300, metavar="MS",
                    help="Quiet period after a change before acting (default 300)")
    ap.add_argument("--poll", type=int, default=250, metavar="MS",
                    help="Polling interval (default 250)")
    ap.add_argument("--signal", default="SIGTERM",
                    help="Signal that stops the program (default SIGTERM)")
    ap.add_argument("--kill-timeout", type=int, default=5000, metavar="MS",
                    help="Time to wait after --signal before SIGKILL (default 5000)")
    ap.add_argument("-o", "--output", help="Binary path (default: a temporary directory)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="Only report failures and restarts")
    ap.add_argument("--extlibs", action="append", default=None, metavar="DIR",
                    help="Passed to `lamc build`; also watched. Repeatable.")
    ap.add_argument("--stdlib", help="Passed to `lamc build`")
    ap.add_argument("--go-tags", help="Passed to `lamc build`")
    ap.add_argument("--go-ldflags", help="Passed to `lamc build`")
    ap.add_argument("--go-race", action="store_true", help="Passed to `lamc build`")
    ap.add_argument("--no-cache", action="store_true", help="Passed to `lamc build`")
    ap.add_argument("--no-semantic-check", action="store_true",
                    help="Passed to `lamc build`")
    return ap


def _project_root(source: Path) -> Path:
    here = source.parent
    for _ in range(8):
        if (here / "lamlib.toml").is_file():
            return here
        if here.parent == here:
            break
        here = here.parent
    return source.parent


def _extensions(values: List[str]) -> Tuple[str, ...]:
    exts = {".lam"}
    for value in values:
        for part in value.split(","):
            part = part.strip().lstrip(".")
            if part:
                exts.add("." + part)
    return tuple(sorted(exts))


class Watcher:
    """Polls file modification times under the watched roots."""

    def __init__(self, root: Path, exts: Tuple[str, ...], paths: List[Path], ignores: List[str]):
        self.root = root
        self.exts = exts
        self.paths = paths
        self.ignores = list(DEFAULT_IGNORES) + ignores

    def ignored(self, path: Path) -> bool:
        try:
            rel = path.relative_to(self.root).as_posix()
        except ValueError:
            rel = path.as_posix()
        for pattern in self.ignores:
            if path.name == pattern or fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(path.name, pattern):
                return True
            if any(part == pattern for part in Path(rel).parts):
                return True
        return False

    def _walk(self, base: Path, keep, out: Snapshot) -> None:
        if base.is_file():
            if keep(base):
                self._stat(base, out)
            return
        for dirpath, dirnames, filenames in os.walk(base):
            current = Path(dirpath)
            dirnames[:] = sorted(d for d in dirnames if not self.ignored(current / d))
            for name in filenames:
                path = current / name
                if not self.ignored(path) and keep(path):
                    self._stat(path, out)

    @staticmethod
    def _stat(path: Path, out: Snapshot) -> None:
        try:
            st = path.stat()
        except OSError:
            return
        out[str(path)] = (st.st_mtime_ns, st.st_size)

    def snapshot(self) -> Snapshot:
        out: Snapshot = {}
        self._walk(self.root, lambda p: p.suffix in self.exts or p.name in BUILD_FILES, out)
        for path in self.paths:
            if path.exists():
                self._walk(path, lambda p: True, out)
        return out


def changed_paths(before: Snapshot, after: Snapshot) -> List[str]:
    keys = set(before) | set(after)
    return sorted(k for k in keys if before.get(k) != after.get(k))


def needs_build(paths: List[str]) -> bool:
    return any(p.endswith(".lam") or Path(p).name in BUILD_FILES for p in paths)


class DevRunner:
    def __init__(self, args: argparse.Namespace, program_args: List[str]):
        self.args = args
        self.program_args = program_args
        self.source = Path(args.source).resolve()
        self.root = Path(args.root).resolve() if args.root else _project_root(self.source)
        watched = [Path(p).resolve() for p in args.watch] + [Path(p).resolve() for p in (args.extlibs or [])]
        self.watcher = Watcher(self.root, _extensions(args.ext), watched, args.ignore)
        self.tmpdir: Optional[str] = None
        if args.output:
            self.binary = Path(args.output).resolve()
        else:
            self.tmpdir = tempfile.mkdtemp(prefix="lamc-dev-")
            self.binary = Path(self.tmpdir) / self.source.stem
        self.next_binary = self.binary.with_name(self.binary.name + ".next")
        self.proc: Optional[subprocess.Popen] = None
        self.stopping = threading.Event()
        self.manual = threading.Event()
        self.exit_reported = False
        self.stop_signal = getattr(signal, args.signal.upper() if args.signal.upper().startswith("SIG")
                                   else "SIG" + args.signal.upper())

    def log(self, message: str, always: bool = False) -> None:
        if always or not self.args.quiet:
            print(f"{PREFIX} {message}", file=sys.stderr, flush=True)

    def build_command(self) -> List[str]:
        cmd = LAMC + ["build", str(self.source), "-o", str(self.next_binary)]
        for path in self.args.extlibs or []:
            cmd += ["--extlibs", path]
        for flag in ("stdlib", "go_tags", "go_ldflags"):
            value = getattr(self.args, flag)
            if value:
                cmd += ["--" + flag.replace("_", "-"), value]
        for flag in ("go_race", "no_cache", "no_semantic_check"):
            if getattr(self.args, flag):
                cmd.append("--" + flag.replace("_", "-"))
        return cmd

    def build(self) -> bool:
        self.log(f"building {self.source.name}")
        started = time.monotonic()
        proc = subprocess.run(self.build_command())
        if proc.returncode != 0 or not self.next_binary.exists():
            keep = "keeping the previous build running" if self.running() else "waiting for changes"
            self.log(f"build failed; {keep}", always=True)
            return False
        os.replace(self.next_binary, self.binary)
        self.log(f"built in {time.monotonic() - started:.1f}s")
        return True

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        env = dict(os.environ)
        env["LAMC_DEV"] = "1"
        self.log("starting " + " ".join([self.source.stem] + self.program_args), always=True)
        self.proc = subprocess.Popen([str(self.binary)] + self.program_args, env=env,
                                     start_new_session=True)
        self.exit_reported = False

    def stop(self) -> None:
        if not self.running():
            self.proc = None
            return
        assert self.proc is not None
        try:
            os.killpg(self.proc.pid, self.stop_signal)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=self.args.kill_timeout / 1000)
        except subprocess.TimeoutExpired:
            self.log("program did not stop in time; killing it", always=True)
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
        self.proc = None

    def restart(self, rebuild: bool) -> None:
        if (rebuild or not self.binary.exists()) and not self.build():
            return
        self.stop()
        self.start()

    def watch_exit(self) -> None:
        if self.proc is not None and not self.exit_reported and self.proc.poll() is not None:
            self.exit_reported = True
            code = self.proc.returncode
            self.log(f"program exited with code {code}; waiting for changes", always=True)

    def read_stdin(self) -> None:
        for line in sys.stdin:
            if line.strip() == "rs":
                self.manual.set()

    def handle_signal(self, signum, _frame) -> None:
        self.stopping.set()

    def run(self) -> int:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self.handle_signal)
        if sys.stdin is not None and sys.stdin.isatty():
            threading.Thread(target=self.read_stdin, daemon=True).start()
        self.log(f"watching {self.root}")
        try:
            state = self.watcher.snapshot()
            self.restart(rebuild=True)
            while not self.stopping.wait(self.args.poll / 1000):
                self.watch_exit()
                if self.manual.is_set():
                    self.manual.clear()
                    self.restart(rebuild=True)
                    state = self.watcher.snapshot()
                    continue
                current = self.watcher.snapshot()
                changes = changed_paths(state, current)
                if not changes:
                    continue
                while not self.stopping.wait(self.args.delay / 1000):
                    settled = self.watcher.snapshot()
                    more = changed_paths(current, settled)
                    if not more:
                        break
                    changes = sorted(set(changes) | set(more))
                    current = settled
                if self.stopping.is_set():
                    break
                state = current
                shown = ", ".join(os.path.relpath(p, self.root) for p in changes[:3])
                if len(changes) > 3:
                    shown += f" (+{len(changes) - 3})"
                self.log(f"changed: {shown}")
                self.restart(rebuild=needs_build(changes))
        finally:
            self.stop()
            if self.tmpdir:
                shutil.rmtree(self.tmpdir, ignore_errors=True)
        return 0


def main(argv: List[str]) -> int:
    program_args: List[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, program_args = argv[:split], argv[split + 1:]
    args = build_parser().parse_args(argv)
    if not Path(args.source).is_file():
        print(f"{PREFIX} no such source file: {args.source}", file=sys.stderr)
        return 2
    try:
        runner = DevRunner(args, program_args)
    except AttributeError:
        print(f"{PREFIX} unknown signal: {args.signal}", file=sys.stderr)
        return 2
    return runner.run()
