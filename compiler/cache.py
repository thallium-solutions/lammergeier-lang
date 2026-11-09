"""On-disk compiler cache: transpiled library modules, parse trees, harvested
module metadata, clean semantic checks and the Go work directories.

Transpiling ``.lam`` libraries (parse, AST walk, emit Go, strip ``main``,
collect metadata) is the most expensive phase of a cold build. When the
library file, the metadata of the program it is compiled against and the
compiler itself are unchanged, the Go source the transpiler would emit is
byte-for-byte identical, so re-running the pipeline is wasted work.

This module provides the content-addressed store under :func:`cache_dir`:

- **Library entries** (``<xx>/<key>.json``): the key is
  ``sha256(compiler_version || lib_content || extra)``. The
  ``compiler_version`` digest rolls in every ``.py`` file under
  ``compiler/`` plus ``lammergeier.lark``, so any change to the compiler
  or grammar transparently invalidates every cached library; ``extra``
  digests the program context (``compiler/lib_transpile.py``). The value
  is the emitted Go source and the module's metadata tables as deltas
  (``compiler/lib_results.py``).
- **Front-end entries** (``frontend/``, ``compiler/frontend.py``): the
  preprocessed source and parse tree of a module.
- **Pickled per-module results** (``<namespace>/<key>.lamc``:
  :func:`load_blob` / :func:`save_blob`), e.g. the metadata harvest
  (``compiler/harvest.py``).
- **Semantic-check markers** (``checks/``) and the persisted Lark parser
  (``parsers/``); the Go work directories (``gobuild/``,
  ``compiler/gobuild.py``).

The cache lives under ``$XDG_CACHE_HOME/lammergeier/libs/`` (or
``~/.cache/lammergeier/libs/`` when XDG is unset; ``LAMC_CACHE_DIR``
overrides it). A corrupt or unreadable entry is treated as a miss — the
pipeline re-runs and overwrites the bad entry. Entries are written
atomically (a unique temporary file, then a rename), so a crashed build
cannot leave a half-written entry behind and concurrent ``lamc``
processes only ever read whole entries.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional


# ─── Paths ────────────────────────────────────────────────────


def cache_dir() -> Path:
    """Return the directory under which library cache files are stored.

    Honours ``LAMC_CACHE_DIR`` (absolute override used by tests) and
    ``XDG_CACHE_HOME``. Falls back to ``~/.cache/lammergeier/libs``.
    """
    override = os.environ.get("LAMC_CACHE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".cache"
    return base / "lammergeier" / "libs"


# ─── Compiler-version digest ──────────────────────────────────


_COMPILER_VERSION_CACHE: Optional[str] = None


def _compiler_version_files() -> List[Path]:
    """Return every file whose contents should salt the cache key.

    Any edit to these files should invalidate cached outputs — they're
    the complete surface that can change the emitted Go. We include all
    ``.py`` under ``compiler/`` and the Lark grammar.
    """
    here = Path(__file__).resolve().parent
    root = here.parent
    files: List[Path] = []
    for p in sorted(here.rglob("*.py")):
        # Skip __pycache__ and the cache module itself (its contents
        # don't affect emitted Go, so there's no need to bust the cache
        # when this file changes).
        if "__pycache__" in p.parts:
            continue
        if p.name == "cache.py":
            continue
        files.append(p)
    lark = root / "lammergeier.lark"
    if lark.exists():
        files.append(lark)
    return files


def compiler_version() -> str:
    """Return a stable sha256 digest of the compiler's source files.

    Computed once per process and memoised. Any change to a compiler
    file (or the grammar) rolls the digest, which in turn busts every
    cached library entry.
    """
    global _COMPILER_VERSION_CACHE
    if _COMPILER_VERSION_CACHE is not None:
        return _COMPILER_VERSION_CACHE
    h = hashlib.sha256()
    for p in _compiler_version_files():
        try:
            h.update(p.name.encode("utf-8"))
            h.update(b"\x00")
            h.update(p.read_bytes())
            h.update(b"\x00")
        except OSError:
            # A source file we expected is missing; include a marker so
            # the digest still changes if the set of present files
            # shifts between runs.
            h.update(b"MISSING:")
            h.update(p.name.encode("utf-8"))
            h.update(b"\x00")
    _COMPILER_VERSION_CACHE = h.hexdigest()
    return _COMPILER_VERSION_CACHE


# ─── Key + path ───────────────────────────────────────────────


def cache_key(lib_content: bytes, extra: bytes = b"") -> str:
    """Compute the cache key for a library file's raw bytes.

    ``extra`` is mixed in as an additional discriminator so callers can
    bucket otherwise-identical libraries by structural context that the
    transpile output depends on but isn't part of the source itself —
    e.g. the union of class names + static members discovered across
    *all* libraries in the build, which the per-library transpiler
    needs to emit correct cross-module dispatch.
    """
    h = hashlib.sha256()
    h.update(compiler_version().encode("ascii"))
    h.update(b"\x00")
    h.update(lib_content)
    if extra:
        h.update(b"\x00")
        h.update(extra)
    return h.hexdigest()


def touch(path: Path) -> bool:
    """Mark a cache entry used now (the size cap evicts the entries used
    least recently, ``compiler/cache_prune.py``). ``False``: no such entry."""
    try:
        os.utime(path)
        return True
    except OSError:
        return False


def _entry_path(key: str) -> Path:
    """Return the path for a given cache key.

    Keys are sharded by their first two hex characters so a large cache
    stays tolerable to list/browse even on filesystems that slow down
    with thousands of files per directory.
    """
    return cache_dir() / key[:2] / f"{key[2:]}.json"


# ─── Load / store ─────────────────────────────────────────────


def load(lib_content: bytes, extra: bytes = b"") -> Optional[Dict[str, Any]]:
    """Return the cached entry for ``lib_content`` or ``None`` on miss.

    A malformed cache file (bad JSON, missing fields) is treated as a
    miss and will be overwritten by the next ``save`` call. ``extra``
    is forwarded to ``cache_key`` so callers can scope an entry by
    additional context (see ``cache_key``).
    """
    key = cache_key(lib_content, extra)
    path = _entry_path(key)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            entry = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(entry, dict):
        return None
    touch(path)
    return entry


def save(lib_content: bytes, entry: Dict[str, Any], extra: bytes = b"") -> None:
    """Write ``entry`` under the key derived from ``lib_content``.

    Uses a tmp-file-then-rename dance so an interrupted save leaves the
    previous entry (if any) intact instead of a half-written file.
    Errors are swallowed — an unusable cache is not a build failure.
    ``extra`` is forwarded to ``cache_key`` so callers can scope an
    entry by additional context (see ``cache_key``).
    """
    key = cache_key(lib_content, extra)
    path = _entry_path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".lamc-cache-", suffix=".json", dir=str(path.parent)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(entry, f)
        os.replace(tmp_name, path)
    except OSError:
        return


# ─── Pickled per-module results ───────────────────────────────


def _blob_path(namespace: str, key: str) -> Path:
    return cache_dir() / namespace / key[:2] / f"{key[2:]}.lamc"


def load_blob(namespace: str, key: str) -> Optional[bytes]:
    """The bytes stored under ``key`` in ``namespace`` (e.g. ``harvest``),
    else ``None``. ``key`` must already digest every input of the value."""
    path = _blob_path(namespace, key)
    try:
        data = path.read_bytes()
    except OSError:
        return None
    touch(path)
    return data


def save_blob(namespace: str, key: str, data: bytes) -> None:
    """Store ``data`` atomically (tmp file + rename: concurrent ``lamc``
    processes see the old entry or the new one, never half of one)."""
    path = _blob_path(namespace, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".lamc-blob-", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, path)
    except OSError:
        return


# ─── Semantic-check markers ───────────────────────────────────


def _check_marker_path(key: str) -> Path:
    return cache_dir() / "checks" / key[:2] / f"{key[2:]}.check"


def has_clean_check(module_content: bytes, extra: bytes) -> bool:
    """Whether an imported module with ``module_content`` passed the semantic
    check with no diagnostics, against the imports ``extra`` digests (see
    ``lammergeier._imported_check_cache_extra``). An empty marker file per
    clean check; a module with diagnostics is never recorded. A hit marks
    the marker used."""
    return touch(_check_marker_path(cache_key(module_content, extra)))


def mark_clean_check(module_content: bytes, extra: bytes) -> None:
    path = _check_marker_path(cache_key(module_content, extra))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        return


# ─── Maintenance ──────────────────────────────────────────────


def clear() -> int:
    """Delete every cached entry. Returns the number of files removed.

    Wipes both the per-library JSON entries and the persisted Lark
    parser blobs (``parsers/<digest>.bin``) so a single
    ``--clear-cache`` reaches every cached artefact.
    """
    d = cache_dir()
    if not d.is_dir():
        return 0
    removed = 0
    # Go work directories of the builds (compiler/gobuild.py).
    gobuild = d / "gobuild"
    if gobuild.is_dir():
        import shutil
        for entry in gobuild.iterdir():
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
            elif entry.suffix == ".lock":
                try:
                    entry.unlink()
                except OSError:
                    pass
    for pattern in ("*.json", "*.bin", "*.check", "*.lamfe", "*.lamc"):
        for entry in d.rglob(pattern):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    # Best-effort cleanup of the now-empty shard directories.
    for sub in d.iterdir():
        if sub.is_dir():
            try:
                # rmdir recursively for the parsers/ subdir which
                # may contain only empties at this point.
                for inner in sub.iterdir():
                    if inner.is_dir():
                        try:
                            inner.rmdir()
                        except OSError:
                            pass
                sub.rmdir()
            except OSError:
                pass
    return removed
