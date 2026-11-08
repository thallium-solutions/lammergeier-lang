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

Change detection polls modification times and sizes, so it also works on
bind mounts inside containers, where file-system events are not delivered.
A file whose modification time or size changed is then hashed and compared
with the content seen before: touching a file, or rewriting it with the same
bytes (``lamc install --frozen`` re-copying ``extlibs/``), is not a change.

Every good build is stored in a persistent cache (``BuildCache``); at start
the build whose inputs are unchanged is run without building
(``--rebuild`` bypasses it).
"""

from __future__ import annotations

import argparse
import contextlib
import fnmatch
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

COMPILER_DIR = Path(__file__).resolve().parent
LAMC = [sys.executable, str(COMPILER_DIR / "lammergeier.py")]
DEFAULT_IGNORES = (".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", ".lamc-dev")
BUILD_FILES = ("lamlib.toml", "lamlib.lock.toml")
PREFIX = "[lamc dev]"
# Environment that changes what `lamc build` / `go build` produce.
BUILD_ENV = ("LAMC_EXTLIBS", "GOFLAGS", "GOOS", "GOARCH", "GOAMD64", "GOARM", "GOARM64", "GO386",
             "GOEXPERIMENT", "GOTOOLCHAIN", "CGO_ENABLED", "CC", "CXX", "CGO_CFLAGS",
             "CGO_CPPFLAGS", "CGO_CXXFLAGS", "CGO_LDFLAGS")
CACHE_KEEP = 3              # cached builds kept per project entry point
CACHE_FORMAT = 1
STALE_SECONDS = 600         # an older temporary file is a crashed writer's

# path -> content digest of the file when the snapshot was taken
Snapshot = Dict[str, str]


def _content_digest(path: Path) -> Optional[str]:
    """BLAKE2b digest of a file's bytes, or None when it can't be read."""
    digest = hashlib.blake2b(digest_size=16)
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


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
            "source's directory). Type `rs` + Enter to rebuild by hand. Good builds "
            "are cached in $LAMC_CACHE/dev (default ~/.lammergeier/cache/dev): an "
            "unchanged program starts its last good build without building."
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
    ap.add_argument("--rebuild", action="store_true",
                    help="Build at start even when a cached build of the same inputs "
                         "exists (the new build is cached)")
    ap.add_argument("--no-cache", action="store_true",
                    help="Passed to `lamc build`; also implies --rebuild")
    ap.add_argument("--no-semantic-check", action="store_true",
                    help="Passed to `lamc build`")
    return ap


# ── Build cache ──────────────────────────────────────────────────

def _cache_root() -> Path:
    """``$LAMC_CACHE``, else ``~/.lammergeier/cache`` (as the package cache
    of ``compiler/install_cli.py``)."""
    override = os.environ.get("LAMC_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".lammergeier" / "cache"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _go_version() -> str:
    try:
        proc = subprocess.run(["go", "version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"
    return (proc.stdout or proc.stderr).strip()


def _dir_digest(directory: Optional[Path], pattern: str) -> str:
    """Digest of the names and contents of ``directory/pattern``."""
    if directory is None or not directory.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(directory.glob(pattern)):
        digest.update(path.name.encode("utf-8") + b"\0" + (_content_digest(path) or "-").encode("ascii"))
    return digest.hexdigest()


def compiler_fingerprint() -> str:
    """The compiler version, its location and a digest of its sources (every
    ``compiler/`` Python file and the grammar): another checkout or an edited
    one never reuses a build. The stdlib ``.lam`` modules are build inputs
    recorded per build, so only the ones a program imports invalidate its
    builds."""
    from compiler import cache as lamcache
    from compiler.version import LAMC_VERSION

    return ":".join((LAMC_VERSION, str(COMPILER_DIR), lamcache.compiler_version(),
                     _dir_digest(COMPILER_DIR.parent / "stdlib", "*.go")))


def build_config_key(args: argparse.Namespace) -> str:
    """Everything but the sources that decides the binary: the compiler, the
    Go toolchain, the build flags and the build environment."""
    config = {
        "format": CACHE_FORMAT,
        "compiler": compiler_fingerprint(),
        "go": _go_version(),
        "env": {name: os.environ.get(name) for name in BUILD_ENV},
        "extlibs": [str(Path(p).resolve()) for p in (args.extlibs or [])],
        "stdlib": str(Path(args.stdlib).resolve()) if args.stdlib else None,
        "stdlib_go": _dir_digest(Path(args.stdlib), "*.go") if args.stdlib else "",
        "go_tags": args.go_tags,
        "go_ldflags": args.go_ldflags,
        "go_race": bool(args.go_race),
        "no_semantic_check": bool(args.no_semantic_check),
    }
    return _sha256(json.dumps(config, sort_keys=True))


def tree_digest(snapshot: Snapshot) -> str:
    """Digest of the build files (``.lam``, ``lamlib*.toml``) of a watcher
    snapshot: any edited, new or deleted source under the watched roots
    changes it."""
    digest = hashlib.sha256()
    for path in sorted(p for p in snapshot if needs_build([p])):
        digest.update(f"{path}\0{snapshot[path]}\n".encode("utf-8"))
    return digest.hexdigest()


class BuildCache:
    """The last good builds of one entry point of one project:
    ``$LAMC_CACHE/dev/<project>/<entry>/`` holding ``<id>.bin`` and its
    ``<id>.json`` (inputs, size, SHA-256), ``<id>`` a digest of every input.

    Writes are a temporary file renamed into place, the binary before its
    metadata, so a reader sees a complete entry or none, whatever the number
    of `lamc dev` processes sharing the directory or a crash. At most
    ``CACHE_KEEP`` entries stay, the least recently used go first."""

    def __init__(self, directory: Path, config: str):
        self.dir = directory
        self.config = config
        self.dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def for_entry(cls, root: Path, source: Path, config: str) -> "BuildCache":
        try:
            entry = source.relative_to(root).as_posix()
        except ValueError:
            entry = source.as_posix()
        name = f"{source.stem}-{_sha256(entry)[:10]}"
        return cls(_cache_root() / "dev" / _sha256(str(root))[:16] / name, config)

    @contextlib.contextmanager
    def start_lock(self, stopping: threading.Event, on_wait) -> Iterator[bool]:
        """Exclusive lock of the entry for the start (lookup, build, store):
        a second `lamc dev` of the same entry waits for the first one's build
        and then starts it. Yields False when stopped while waiting; without
        file locks (no ``fcntl``, a file system refusing them) it does not
        lock."""
        try:
            import fcntl
            fd = os.open(str(self.dir / ".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        except (ImportError, OSError):
            yield True
            return
        try:
            waited = False
            acquired = True
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    pass
                except OSError:
                    break
                if not waited:
                    waited = True
                    on_wait()
                if stopping.wait(0.2):
                    acquired = False
                    break
            yield acquired
        finally:
            os.close(fd)

    def _entries(self) -> List[Tuple[Path, Optional[dict]]]:
        """Metadata files, most recently used first (None: unreadable)."""
        entries = []
        for meta_path in self.dir.glob("*.json"):
            try:
                mtime = meta_path.stat().st_mtime
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if not isinstance(meta, dict):
                    meta = None
            except FileNotFoundError:
                continue
            except (OSError, ValueError):
                mtime, meta = 0.0, None
            entries.append((mtime, meta_path, meta))
        entries.sort(key=lambda item: item[0], reverse=True)
        return [(path, meta) for _, path, meta in entries]

    def _remove(self, meta_path: Path) -> None:
        for path in (meta_path, meta_path.with_suffix(".bin")):
            try:
                path.unlink()
            except OSError:
                pass

    def lookup(self, snapshot: Snapshot) -> Optional[dict]:
        """The newest entry built from the current inputs: same configuration,
        same build files under the watched roots, every other recorded input
        unchanged and every recorded missing module path still missing."""
        tree = tree_digest(snapshot)
        for meta_path, meta in self._entries():
            if meta is None:
                self._remove(meta_path)
                continue
            try:
                if (meta.get("format") != CACHE_FORMAT or meta.get("config") != self.config
                        or meta.get("tree") != tree):
                    continue
                if any(os.path.lexists(path) for path in meta["absent"]):
                    continue
                if any((snapshot.get(path) or _content_digest(Path(path))) != digest
                       for path, digest in meta["inputs"].items()):
                    continue
                binary = meta_path.with_suffix(".bin")
                if binary.stat().st_size != meta["size"]:
                    meta["damaged"] = True
            except (KeyError, TypeError, AttributeError):
                self._remove(meta_path)
                continue
            except OSError:
                meta["damaged"] = True
            meta["path"] = str(meta_path)
            return meta
        return None

    def restore(self, meta: dict, dest: Path) -> bool:
        """Copy the entry's binary to ``dest``, checking its size and SHA-256;
        a damaged entry is removed and False returned."""
        meta_path = Path(meta["path"])
        ok = False
        if not meta.get("damaged"):
            digest = hashlib.sha256()
            size = 0
            try:
                with open(meta_path.with_suffix(".bin"), "rb") as src, open(dest, "wb") as out:
                    for chunk in iter(lambda: src.read(1 << 20), b""):
                        digest.update(chunk)
                        size += len(chunk)
                        out.write(chunk)
                ok = size == meta["size"] and digest.hexdigest() == meta["sha256"]
            except OSError:
                ok = False
        if not ok:
            with contextlib.suppress(OSError):
                dest.unlink()
            self._remove(meta_path)
            return False
        os.chmod(dest, 0o755)
        with contextlib.suppress(OSError):
            os.utime(meta_path)                 # most recently used
        return True

    def _write_atomic(self, path: Path, write) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(self.dir))
        try:
            with os.fdopen(fd, "wb") as out:
                write(out)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def store(self, binary: Path, snapshot: Snapshot, inputs: Dict[str, str], absent: List[str]) -> None:
        tree = tree_digest(snapshot)
        key = _sha256(json.dumps([self.config, tree, sorted(inputs.items()), sorted(absent)]))[:32]
        bin_path = self.dir / f"{key}.bin"
        digest = hashlib.sha256()
        size = 0

        def copy(out) -> None:
            nonlocal size
            with open(binary, "rb") as src:
                for chunk in iter(lambda: src.read(1 << 20), b""):
                    digest.update(chunk)
                    size += len(chunk)
                    out.write(chunk)
            os.fchmod(out.fileno(), 0o755)

        self._write_atomic(bin_path, copy)
        meta = {
            "format": CACHE_FORMAT,
            "config": self.config,
            "tree": tree,
            "inputs": inputs,
            "absent": sorted(absent),
            "size": size,
            "sha256": digest.hexdigest(),
            "created": time.time(),
        }
        self._write_atomic(bin_path.with_suffix(".json"),
                           lambda out: out.write(json.dumps(meta, indent=1).encode("utf-8")))
        self.prune()

    def prune(self) -> None:
        """Keep the ``CACHE_KEEP`` most recently used entries; remove the
        binaries without metadata and the temporary files of crashed writers
        (only old ones: another process may be writing them)."""
        for meta_path, _meta in self._entries()[CACHE_KEEP:]:
            self._remove(meta_path)
        now = time.time()
        for path in self.dir.iterdir():
            stale = path.name.startswith(".tmp-") or (
                path.suffix == ".bin" and not path.with_suffix(".json").exists())
            if not stale:
                continue
            with contextlib.suppress(OSError):
                if now - path.stat().st_mtime > STALE_SECONDS:
                    path.unlink()


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
    """Polls the files under the watched roots.

    The (mtime_ns, size) pair is the cheap first check; only a file whose
    pair changed since the previous snapshot is read and hashed, so a
    snapshot records content digests while costing one ``stat`` per
    unchanged file."""

    def __init__(self, root: Path, exts: Tuple[str, ...], paths: List[Path], ignores: List[str]):
        self.root = root
        self.exts = exts
        self.paths = paths
        self.ignores = list(DEFAULT_IGNORES) + ignores
        self._digests: Dict[str, Tuple[Tuple[int, int], str]] = {}

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

    def _stat(self, path: Path, out: Snapshot) -> None:
        key = str(path)
        if key in out:
            return
        try:
            st = path.stat()
        except OSError:
            return
        sig = (st.st_mtime_ns, st.st_size)
        cached = self._digests.get(key)
        if cached is not None and cached[0] == sig:
            out[key] = cached[1]
            return
        digest = _content_digest(path)
        if digest is None:
            return
        self._digests[key] = (sig, digest)
        out[key] = digest

    def snapshot(self) -> Snapshot:
        out: Snapshot = {}
        self._walk(self.root, lambda p: p.suffix in self.exts or p.name in BUILD_FILES, out)
        for path in self.paths:
            if path.exists():
                self._walk(path, lambda p: True, out)
        for key in [k for k in self._digests if k not in out]:
            del self._digests[key]
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
        self.tmpdir = tempfile.mkdtemp(prefix="lamc-dev-")
        if args.output:
            self.binary = Path(args.output).resolve()
        else:
            self.binary = Path(self.tmpdir) / self.source.stem
        self.next_binary = self.binary.with_name(self.binary.name + ".next")
        self.inputs_file = Path(self.tmpdir) / "build-inputs.json"
        self.cache: Optional[BuildCache] = None
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
        if self.cache is not None:
            cmd += ["--record-inputs", str(self.inputs_file)]
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

    def build(self, note: str = "") -> bool:
        self.log(f"building {self.source.name}" + (f" ({note})" if note else ""))
        started = time.monotonic()
        before = self.watcher.snapshot() if self.cache is not None else {}
        with contextlib.suppress(OSError):
            self.inputs_file.unlink()
        proc = subprocess.run(self.build_command())
        if proc.returncode != 0 or not self.next_binary.exists():
            keep = "keeping the previous build running" if self.running() else "waiting for changes"
            self.log(f"build failed; {keep}", always=True)
            return False
        os.replace(self.next_binary, self.binary)
        self.log(f"built in {time.monotonic() - started:.1f}s")
        if self.cache is not None:
            self.store(before)
        return True

    def store(self, before: Snapshot) -> None:
        """Cache the build just made from the sources of ``before`` (the
        snapshot taken when it started). Not cached when a source changed
        during the build: the watcher rebuilds it."""
        assert self.cache is not None
        try:
            record = json.loads(self.inputs_file.read_text(encoding="utf-8"))
            files, absent = list(record["files"]), list(record["absent"])
        except (OSError, ValueError, KeyError, TypeError):
            return
        after = self.watcher.snapshot()
        if tree_digest(before) != tree_digest(after):
            return
        inputs: Dict[str, str] = {}
        for path in files:
            digest = before.get(path) or _content_digest(Path(path))
            if digest is None or (path in after and after[path] != digest):
                return
            inputs[path] = digest
        try:
            self.cache.store(self.binary, before, inputs, absent)
        except OSError as exc:
            self.log(f"could not cache the build: {exc}")

    def open_cache(self) -> None:
        try:
            self.cache = BuildCache.for_entry(self.root, self.source, build_config_key(self.args))
        except OSError as exc:
            self.log(f"build cache unavailable ({exc}); building without it")

    def first_start(self) -> None:
        """Start the cached build of the current inputs, else build and start."""
        self.open_cache()
        if self.cache is None:
            self.restart(rebuild=True)
            return
        bypass = self.args.rebuild or self.args.no_cache
        name = self.source.name
        on_wait = lambda: self.log(f"another lamc dev is building {name}; waiting for it", always=True)
        with self.cache.start_lock(self.stopping, on_wait) as locked:
            if not locked:
                return
            if bypass:
                note = ("--rebuild" if self.args.rebuild else "--no-cache") + ": not using a cached build"
            else:
                note = "no cached build; the first build can take a minute or two"
            if not bypass:
                # a fresh snapshot: the sources may have changed while waiting
                meta = self.cache.lookup(self.watcher.snapshot())
                if meta is not None:
                    self.binary.parent.mkdir(parents=True, exist_ok=True)
                    if self.cache.restore(meta, self.next_binary):
                        os.replace(self.next_binary, self.binary)
                        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(meta.get("created", 0)))
                        self.start(f"cached build of {when}")
                        return
                    self.log("cached build is damaged; rebuilding", always=True)
                    note = "the first build can take a minute or two"
            built = self.build(note)
        if built:
            self.stop()
            self.start()

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, note: str = "") -> None:
        env = dict(os.environ)
        env["LAMC_DEV"] = "1"
        self.log("starting " + " ".join([self.source.stem] + self.program_args)
                 + (f" ({note})" if note else ""), always=True)
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
            self.first_start()
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
                # A file rewritten in steps (deleted, then re-created by
                # an install) may end up with the content it started with.
                changes = changed_paths(state, current)
                state = current
                if not changes:
                    continue
                shown = ", ".join(os.path.relpath(p, self.root) for p in changes[:3])
                if len(changes) > 3:
                    shown += f" (+{len(changes) - 3})"
                self.log(f"changed: {shown}")
                self.restart(rebuild=needs_build(changes))
        finally:
            self.stop()
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
