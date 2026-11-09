"""The Go work directory of a build.

``go build`` caches compiled packages by content *and directory*: a fresh
temporary directory per build made every build recompile the generated
``main`` package (all modules of the program are one Go package) and link
again, even when nothing changed. A program now builds in a directory of
its own under the compiler cache (``<cache_dir>/gobuild/<digest>``, the
digest of the source path and Go module name), so an unchanged program is a
Go cache hit and a changed one reuses everything else Go cached.

The generated files are written only when their content changed, files of
modules no longer in the program are removed, and ``go mod init`` / ``go mod
tidy`` run again only when what they depend on changed (the Go module name,
the Go version, the pinned requirements and the import declarations of the
generated files); a build that fails after reusing ``go.mod`` runs them
again and retries.

A directory is locked (``flock``) while a build uses it; a second ``lamc``
building the same program at the same time uses a temporary directory, as
does ``--no-cache``.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path

GOMOD_STAMP = ".lamc-gomod"
_IMPORT_LINE_RE = re.compile(r'^\s*(?:import\s+)?(?:[\w.]+\s+)?"[^"\n]+"\s*$')


class WorkDir:
    def __init__(self, path: Path, temporary: bool, lock_fd: int | None = None) -> None:
        self.path = path
        self.temporary = temporary
        self._lock_fd = lock_fd

    def release(self, keep: bool = False) -> None:
        if self._lock_fd is not None:
            try:
                os.close(self._lock_fd)
            except OSError:
                pass
            self._lock_fd = None
        if self.temporary and not keep:
            shutil.rmtree(self.path, ignore_errors=True)


def _temporary() -> WorkDir:
    return WorkDir(Path(tempfile.mkdtemp(prefix="lammergeier_")), temporary=True)


def work_dir(source_file: Path, go_module: str, use_cache: bool) -> WorkDir:
    """The Go work directory for building ``source_file``."""
    if not use_cache:
        return _temporary()
    try:
        import fcntl
    except ImportError:  # no flock (Windows): no shared directory
        return _temporary()
    from compiler import cache as _cache

    digest = hashlib.sha256(f"{source_file}\0{go_module}".encode("utf-8")).hexdigest()[:24]
    root = _cache.cache_dir() / "gobuild"
    path = root / digest
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(root / f"{digest}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return _temporary()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return _temporary()
    try:
        # A concurrent prune may have removed it before the lock was taken.
        path.mkdir(parents=True, exist_ok=True)
        os.utime(path)  # used now (the cache size cap evicts old ones first)
    except OSError:
        os.close(fd)
        return _temporary()
    return WorkDir(path, temporary=False, lock_fd=fd)


def sync_files(directory: Path, files: dict[str, str]) -> None:
    """Make the ``*.go`` files of ``directory`` exactly ``files`` (name ->
    content), writing only the ones that changed."""
    for existing in directory.glob("*.go"):
        if existing.name not in files:
            try:
                existing.unlink()
            except OSError:
                pass
    for name, content in files.items():
        target = directory / name
        data = content.encode("utf-8")
        try:
            if target.is_file() and target.stat().st_size == len(data) and target.read_bytes() == data:
                continue
        except OSError:
            pass
        tmp = directory / f".{name}.tmp"
        tmp.write_bytes(data)
        os.replace(tmp, target)


def import_declarations(go_source: str) -> list[str]:
    """The import declarations of a generated Go file (what ``go mod tidy``
    reads from it)."""
    out: list[str] = []
    in_block = False
    for line in go_source.splitlines():
        stripped = line.strip()
        if in_block:
            if stripped.startswith(")"):
                in_block = False
            elif stripped and not stripped.startswith("//"):
                out.append(stripped)
            continue
        if stripped.startswith("import ("):
            in_block = True
        elif stripped.startswith("import ") and _IMPORT_LINE_RE.match(stripped):
            out.append(stripped)
    return out


def gomod_stamp(go_module: str, go_version: str, pins: dict[str, str], files: dict[str, str]) -> str:
    h = hashlib.sha256(b"lam-gomod-v1\0")
    h.update(go_module.encode("utf-8") + b"\0" + go_version.encode("utf-8") + b"\0")
    for path in sorted(pins):
        h.update(f"{path}@{pins[path]}\0".encode("utf-8"))
    imports = set()
    for name in sorted(files):
        if name.endswith(".go"):
            imports.update(import_declarations(files[name]))
    for line in sorted(imports):
        h.update(line.encode("utf-8") + b"\n")
    return h.hexdigest()


def gomod_is_current(directory: Path, stamp: str) -> bool:
    try:
        return (
            (directory / "go.mod").is_file()
            and (directory / GOMOD_STAMP).read_text(encoding="utf-8").strip() == stamp
        )
    except OSError:
        return False


def write_gomod_stamp(directory: Path, stamp: str | None) -> None:
    target = directory / GOMOD_STAMP
    try:
        if stamp is None:
            target.unlink(missing_ok=True)
        else:
            target.write_text(stamp + "\n", encoding="utf-8")
    except OSError:
        pass


def reset_gomod(directory: Path) -> None:
    for name in ("go.mod", "go.sum", GOMOD_STAMP):
        try:
            (directory / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


_MODULE_ERROR_MARKERS = (
    "no required module provides package",
    "missing go.sum entry",
    "updates to go.mod needed",
    "cannot find module providing package",
    "go.mod",
    "go.sum",
)


def looks_like_module_error(stderr: str) -> bool:
    """Whether a failed ``go build`` complains about modules (a reused
    ``go.mod`` / ``go.sum`` that no longer fits the sources)."""
    return any(marker in (stderr or "") for marker in _MODULE_ERROR_MARKERS)
