"""On-disk cache for transpiled library modules.

Transpiling ``.lam`` libraries (parse, AST walk, emit Go, strip ``main``,
collect metadata) is the most expensive phase of a cold build. When the
library file and the compiler itself are unchanged, the Go source the
transpiler would emit is byte-for-byte identical, so re-running the
pipeline is wasted work.

This module provides a small content-addressed cache:

- **Key** is ``sha256(compiler_version || lib_content)``. The
  ``compiler_version`` digest rolls in every ``.py`` file under
  ``compiler/`` plus ``lammergeier.lark``, so any change to the compiler
  or grammar transparently invalidates every cached library.
- **Value** is a JSON blob with the emitted Go source and the handful of
  metadata sets (``_class_names``, ``_static_methods``, ``_static_vars`` …) that the
  caller needs to re-inject into the main transpiler.

The cache lives under ``$XDG_CACHE_HOME/lammergeier/libs/`` (or
``~/.cache/lammergeier/libs/`` when XDG is unset). A corrupt or
unreadable cache file is treated as a miss — the transpile pipeline
re-runs and overwrites the bad entry. Cache files are written
atomically via a ``.tmp`` rename so a crashed build can't leave a
half-written entry behind.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


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


# ─── Serialisation helpers ────────────────────────────────────


def serialise_transpile_result(
    go_src: str,
    class_names: set,
    static_methods: Dict[str, set],
    static_vars: Optional[Dict[str, Dict[str, bool]]],
    func_defaults: Dict[str, List[Tuple[int, Any]]],
    func_param_counts: Dict[str, int],
    variadic_functions: set,
    user_functions: Optional[set] = None,
    private_functions: Optional[set] = None,
    method_return_types: Optional[Dict[str, str]] = None,
    func_param_names: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    """Convert the tuple returned by ``_transpile_lib`` into a plain
    JSON-serialisable dict.

    Sets become sorted lists and ``func_defaults`` tuples become lists;
    ``deserialise_transpile_result`` is the inverse operation.

    ``user_functions`` and ``private_functions`` are needed by the
    caller so cross-library default-arg filling and ``_go_public_name``
    rewriting work for imported functions just like locally-defined
    ones. ``method_return_types`` powers chained-call class inference
    (e.g. ``db.table(name).where(...)``). ``func_param_names`` carries
    ordered parameter names so keyword-arg call sites can reorder
    cross-library calls.
    """
    return {
        # Bumped from 4 → 5 when ``static_vars`` metadata was added.
        # Older entries are silently ignored on load and re-transpiled.
        "version": 5,
        "go_src": go_src,
        "class_names": sorted(class_names),
        "static_methods": {k: sorted(v) for k, v in static_methods.items()},
        "static_vars": {
            cls: {name: bool(is_private) for name, is_private in fields.items()}
            for cls, fields in (static_vars or {}).items()
        },
        "func_defaults": {
            k: [[idx, val] for idx, val in pairs]
            for k, pairs in func_defaults.items()
        },
        "func_param_counts": dict(func_param_counts),
        "variadic_functions": sorted(variadic_functions),
        "user_functions": sorted(user_functions or set()),
        "private_functions": sorted(private_functions or set()),
        "method_return_types": dict(method_return_types or {}),
        "func_param_names": {k: list(v) for k, v in (func_param_names or {}).items()},
    }


def deserialise_transpile_result(
    entry: Dict[str, Any],
) -> Optional[Tuple[str, set, Dict[str, set], Dict[str, Dict[str, bool]], Dict[str, List[Tuple[int, Any]]], Dict[str, int], set, set, set, Dict[str, str], Dict[str, List[str]]]]:
    """Inverse of :func:`serialise_transpile_result`.

    Returns ``None`` if ``entry`` is missing required keys or has the
    wrong schema version; the caller should treat that as a miss.
    """
    if entry.get("version") != 5:
        return None
    try:
        go_src = entry["go_src"]
        class_names = set(entry["class_names"])
        static_methods = {k: set(v) for k, v in entry["static_methods"].items()}
        static_vars = {
            k: {str(name): bool(is_private) for name, is_private in v.items()}
            for k, v in entry.get("static_vars", {}).items()
        }
        func_defaults = {
            k: [(int(idx), val) for idx, val in pairs]
            for k, pairs in entry["func_defaults"].items()
        }
        func_param_counts = dict(entry["func_param_counts"])
        variadic_functions = set(entry["variadic_functions"])
        user_functions = set(entry.get("user_functions", []))
        private_functions = set(entry.get("private_functions", []))
        method_return_types = dict(entry.get("method_return_types", {}))
        func_param_names = {k: list(v) for k, v in entry.get("func_param_names", {}).items()}
    except (KeyError, TypeError, ValueError):
        return None
    return (
        go_src,
        class_names,
        static_methods,
        static_vars,
        func_defaults,
        func_param_counts,
        variadic_functions,
        user_functions,
        private_functions,
        method_return_types,
        func_param_names,
    )


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
    for pattern in ("*.json", "*.bin"):
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
