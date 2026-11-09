"""Size cap of the compiler cache: least-recently-used entries go first.

Everything the compiler caches lives under :func:`compiler.cache.cache_dir`
(``LAMC_CACHE_DIR``, default ``~/.cache/lammergeier/libs``): library
entries, the front-end and harvest entries, clean-check markers, the
persisted parser and the per-program Go work directories. Content-addressed
entries are never invalidated in place — an edit writes new ones — so
without a cap the cache only grows.

- **Cap**: ``LAMC_CACHE_MAX_SIZE`` (``500MB``, ``5GB``, ``2GiB``, bytes;
  ``0`` = unlimited), default 2 GB; ``lamc cache prune --max-size``.
  Sizes are disk usage (allocated blocks).
- **Use**: an entry's modification time. Writing an entry sets it and
  every cache hit sets it again (``compiler.cache.touch``), so it is the
  time of the last build that used the entry; a Go work directory is
  marked when a build takes it.
- **Eviction**: over the cap, entries are removed oldest-use first until
  the cache is down to 80% of the cap (so the next builds do not prune
  again right away).
- **When**: after a build that used the cache, at most once per
  :data:`PRUNE_INTERVAL` seconds per cache root (the ``.lamc-prune-stamp``
  file; ``LAMC_CACHE_PRUNE_INTERVAL`` overrides), and on ``lamc cache
  prune``. One process prunes at a time (``.lamc-prune.lock``); the others
  skip.
- **Safety**: only entries of the compiler's own kinds and file-name shapes
  are ever removed (an unknown file, or a cache root pointed at another
  directory, is left alone), symbolic links are never followed, temporary
  files younger than :data:`TEMP_GRACE` seconds (a build is writing them)
  and Go work directories another build holds locked are skipped. A build
  that loses an entry to a concurrent prune just computes it again.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_MAX_SIZE = 2_000_000_000
PRUNE_INTERVAL = 600
TEMP_GRACE = 600
LOW_WATER = 0.8
STAMP = ".lamc-prune-stamp"
LOCK = ".lamc-prune.lock"

_UNITS = {
    "": 1, "b": 1,
    "k": 10 ** 3, "kb": 10 ** 3, "kib": 2 ** 10,
    "m": 10 ** 6, "mb": 10 ** 6, "mib": 2 ** 20,
    "g": 10 ** 9, "gb": 10 ** 9, "gib": 2 ** 30,
    "t": 10 ** 12, "tb": 10 ** 12, "tib": 2 ** 40,
}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")
_HEX_RE = re.compile(r"^[0-9a-f]+$")
_NAMESPACE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_TEMP_RE = re.compile(r"^\.lamc-[A-Za-z0-9_-]+\.(?:tmp|json)$")


def parse_size(text: str) -> int:
    """Bytes for ``500MB``, ``5GB``, ``2GiB``, ``1.5G``, ``1234``; ``0`` and
    ``unlimited`` are 0 (no cap). Raises ``ValueError``."""
    if text.strip().lower() in ("unlimited", "none", "off"):
        return 0
    match = _SIZE_RE.match(text)
    if not match or match.group(2).lower() not in _UNITS:
        raise ValueError(f"invalid size {text!r} (examples: 500MB, 5GB, 2GiB, 0 for unlimited)")
    return int(float(match.group(1)) * _UNITS[match.group(2).lower()])


def format_size(size: int) -> str:
    for unit, factor in (("GB", 10 ** 9), ("MB", 10 ** 6), ("kB", 10 ** 3)):
        if size >= factor:
            return f"{size / factor:.1f} {unit}"
    return f"{size} B"


def max_size_from_env() -> int:
    text = os.environ.get("LAMC_CACHE_MAX_SIZE", "").strip()
    if not text:
        return DEFAULT_MAX_SIZE
    try:
        return parse_size(text)
    except ValueError as exc:
        print(f"warning: LAMC_CACHE_MAX_SIZE: {exc}; using {format_size(DEFAULT_MAX_SIZE)}", file=sys.stderr)
        return DEFAULT_MAX_SIZE


def _interval_from_env() -> float:
    text = os.environ.get("LAMC_CACHE_PRUNE_INTERVAL", "").strip()
    if text:
        try:
            return max(0.0, float(text))
        except ValueError:
            pass
    return float(PRUNE_INTERVAL)


@dataclass
class Item:
    kind: str
    path: Path
    size: int
    used: float
    is_dir: bool = False
    temp: bool = False


@dataclass
class PruneResult:
    total_before: int = 0
    total_after: int = 0
    removed: int = 0
    removed_bytes: int = 0
    skipped: int = 0
    newest_removed: float | None = None
    oldest_kept: float | None = None
    by_kind: dict = field(default_factory=dict)


def _usage(st: os.stat_result) -> int:
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if blocks is not None else st.st_size


def _dir_usage(path: Path) -> tuple[int, float]:
    size = 0
    try:
        newest = os.lstat(path).st_mtime
    except OSError:
        return 0, 0.0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames + dirnames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            size += _usage(st)
    return size, newest


def _entries(directory: Path):
    try:
        with os.scandir(directory) as it:
            return [e for e in it]
    except OSError:
        return []


def _file_item(kind: str, entry) -> Item | None:
    try:
        if not entry.is_file(follow_symlinks=False):
            return None
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return None
    temp = bool(_TEMP_RE.match(entry.name))
    return Item("temp" if temp else kind, Path(entry.path), _usage(st), st.st_mtime, temp=temp)


def _shard_files(kind: str, directory: Path, suffix: str, out: list) -> None:
    """``<directory>/<2 hex>/<hex><suffix>`` entries (and their temp files)."""
    for shard in _entries(directory):
        if len(shard.name) != 2 or not _HEX_RE.match(shard.name):
            continue
        try:
            if not shard.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        for entry in _entries(Path(shard.path)):
            name = entry.name
            own = name.endswith(suffix) and _HEX_RE.match(name[: -len(suffix)])
            if own or _TEMP_RE.match(name):
                item = _file_item(kind, entry)
                if item is not None:
                    out.append(item)


def scan(root: Path) -> list[Item]:
    """Every entry of the compiler cache under ``root``."""
    root = Path(root)
    items: list[Item] = []
    if not root.is_dir() or root.is_symlink():
        return items
    _shard_files("library", root, ".json", items)
    for sub in _entries(root):
        try:
            if not sub.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        name, path = sub.name, Path(sub.path)
        if name == "frontend":
            _shard_files("frontend", path, ".lamfe", items)
        elif name == "checks":
            _shard_files("checks", path, ".check", items)
        elif name == "parsers":
            for entry in _entries(path):
                stem = entry.name[:-4] if entry.name.endswith(".bin") else ""
                if (stem and _HEX_RE.match(stem)) or _TEMP_RE.match(entry.name):
                    item = _file_item("parsers", entry)
                    if item is not None:
                        items.append(item)
        elif name == "gobuild":
            for entry in _entries(path):
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                except OSError:
                    continue
                if is_dir and _HEX_RE.match(entry.name):
                    size, used = _dir_usage(Path(entry.path))
                    items.append(Item("gobuild", Path(entry.path), size, used, is_dir=True))
        elif len(name) == 2 and _HEX_RE.match(name):
            continue  # library shards, above
        elif _NAMESPACE_RE.match(name):
            # Pickled per-module results (``cache.save_blob``): harvest ...
            _shard_files(name, path, ".lamc", items)
    return items


def info(root: Path) -> dict[str, tuple[int, int]]:
    """``{kind: (entries, bytes)}``."""
    out: dict[str, tuple[int, int]] = {}
    for item in scan(root):
        count, size = out.get(item.kind, (0, 0))
        out[item.kind] = (count + 1, size + item.size)
    return out


def _remove_work_dir(item: Item) -> bool:
    """Remove a Go work directory unless a build holds its lock."""
    try:
        import fcntl
    except ImportError:
        return False
    lock = item.path.parent / f"{item.path.name}.lock"
    try:
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        # Held while deleting; the lock file itself stays (a build that
        # opened it is then serialised with this removal).
        shutil.rmtree(item.path, ignore_errors=True)
        return not item.path.exists()
    finally:
        os.close(fd)


def prune(root: Path, max_size: int, *, now: float | None = None, temp_grace: float = TEMP_GRACE) -> PruneResult:
    """Bring the cache under ``root`` below ``max_size`` bytes (0: no cap),
    least recently used entries first; stale temporary files always go."""
    now = time.time() if now is None else now
    result = PruneResult()
    items = scan(Path(root))
    result.total_before = sum(item.size for item in items)
    total = result.total_before
    entries: list[Item] = []
    for item in items:
        if item.temp:
            if now - item.used < temp_grace:
                result.skipped += 1  # being written by a build
                continue
            try:
                os.unlink(item.path)
                result.removed += 1
                result.removed_bytes += item.size
                total -= item.size
            except OSError:
                pass
            continue
        entries.append(item)
    if max_size > 0 and total > max_size:
        target = int(max_size * LOW_WATER)
        entries.sort(key=lambda item: item.used)
        kept: list[Item] = []
        for item in entries:
            if total <= target:
                kept.append(item)
                continue
            if item.is_dir:
                removed = _remove_work_dir(item)
            else:
                try:
                    os.unlink(item.path)
                    removed = True
                except FileNotFoundError:
                    removed = True
                except OSError:
                    removed = False
            if not removed:
                result.skipped += 1  # in use (a locked Go work directory)
                continue
            result.removed += 1
            result.removed_bytes += item.size
            result.by_kind[item.kind] = result.by_kind.get(item.kind, 0) + 1
            total -= item.size
            result.newest_removed = item.used if result.newest_removed is None else max(result.newest_removed, item.used)
        result.oldest_kept = min((item.used for item in kept), default=None)
    result.total_after = total
    return result


def maybe_prune(root: Path | None = None, *, max_size: int | None = None,
                interval: float | None = None) -> PruneResult | None:
    """Prune ``root`` (default: the compiler cache) when the last prune is
    older than ``interval`` seconds and no other process is pruning;
    ``None`` when it did not run."""
    if root is None:
        from compiler import cache as _cache
        root = _cache.cache_dir()
    root = Path(root)
    if not root.is_dir():
        return None
    interval = _interval_from_env() if interval is None else interval
    stamp = root / STAMP
    now = time.time()
    try:
        if now - stamp.stat().st_mtime < interval:
            return None
    except OSError:
        pass
    try:
        import fcntl
    except ImportError:
        fcntl = None
    try:
        fd = os.open(str(root / LOCK), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return None  # another process is pruning
        try:
            stamp.touch()
            os.utime(stamp, (now, now))
        except OSError:
            return None
        cap = max_size_from_env() if max_size is None else max_size
        return prune(root, cap, now=now)
    finally:
        os.close(fd)


def after_build() -> None:
    """The opportunistic prune at the end of a build (never fails it)."""
    try:
        start = time.perf_counter()
        result = maybe_prune()
        if result is not None:
            from compiler import timings
            timings.record("cache_prune", time.perf_counter() - start)
    except Exception:  # noqa: BLE001 - pruning is best-effort
        pass
