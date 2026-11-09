#!/usr/bin/env python3
"""Automatic pruning of the compiler cache (``compiler/cache_prune.py``).

- Sizes: ``LAMC_CACHE_MAX_SIZE`` / ``--max-size`` parse ``500MB``, ``5GB``,
  ``2GiB``, plain bytes; ``0`` is unlimited; unset is the 2 GB default.
- Pruning removes the least recently used entries first until the cache is
  under the cap, keeps the most recently used ones, and only ever touches
  entries of the compiler's own kinds under the cache root: unknown files,
  symlinked trees, recent temporary files (a build is writing them) and Go
  work directories another build holds locked are left alone; stale
  temporary files are removed.
- A hit marks an entry used (its modification time), so a build keeps what
  it reads.
- After a build, pruning runs at most once per interval per cache root (a
  stamp file).
- ``lamc cache info`` / ``lamc cache prune`` / ``lamc cache clear``.
- A build after a prune produces the same Go (entries are recomputed).

Run with ``python3 tests/tests/test_cache_prune.py``.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compiler import cache_prune as P  # noqa: E402

SCRIPT = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]
HEX62 = "ab" * 31
DAY = 86400


def _file(path: Path, size: int, age: float, now: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (now - age, now - age))
    return path


def _key(i: int, width: int = 62) -> str:
    return f"{i:0{width}x}"


def _populate(root: Path, now: float) -> dict[str, Path]:
    """One entry of every kind, ages from 10 days (oldest) to 1 day."""
    paths = {
        "library": _file(root / "0a" / f"{_key(1)}.json", 40_000, 10 * DAY, now),
        "frontend": _file(root / "frontend" / "0b" / f"{_key(2)}.lamfe", 40_000, 9 * DAY, now),
        "harvest": _file(root / "harvest" / "0c" / f"{_key(3)}.lamc", 40_000, 8 * DAY, now),
        "checks": _file(root / "checks" / "0d" / f"{_key(4)}.check", 0, 7 * DAY, now),
        "parsers": _file(root / "parsers" / f"{_key(5, 64)}.bin", 40_000, 6 * DAY, now),
    }
    work = root / "gobuild" / _key(6, 24)
    _file(work / "main.go", 40_000, 5 * DAY, now)
    _file(work / "go.mod", 100, 5 * DAY, now)
    os.utime(work, (now - 5 * DAY, now - 5 * DAY))
    (root / "gobuild" / f"{_key(6, 24)}.lock").touch()
    paths["gobuild"] = work
    paths["library-new"] = _file(root / "1a" / f"{_key(7)}.json", 40_000, 1 * DAY, now)
    return paths


def case_sizes() -> None:
    assert P.parse_size("0") == 0
    assert P.parse_size("500MB") == 500_000_000
    assert P.parse_size("5GB") == 5_000_000_000
    assert P.parse_size("5 gb") == 5_000_000_000
    assert P.parse_size("2GiB") == 2 * 1024 ** 3
    assert P.parse_size("1.5G") == 1_500_000_000
    assert P.parse_size("64K") == 64_000
    assert P.parse_size("1234") == 1234
    assert P.parse_size("unlimited") == 0
    for bad in ("", "lots", "-1GB", "5XB"):
        try:
            P.parse_size(bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} must be rejected")
    saved = os.environ.pop("LAMC_CACHE_MAX_SIZE", None)
    try:
        assert P.max_size_from_env() == P.DEFAULT_MAX_SIZE == 2_000_000_000
        os.environ["LAMC_CACHE_MAX_SIZE"] = "0"
        assert P.max_size_from_env() == 0
        os.environ["LAMC_CACHE_MAX_SIZE"] = "750MB"
        assert P.max_size_from_env() == 750_000_000
        os.environ["LAMC_CACHE_MAX_SIZE"] = "nonsense"
        assert P.max_size_from_env() == P.DEFAULT_MAX_SIZE, "an invalid value falls back to the default"
    finally:
        os.environ.pop("LAMC_CACHE_MAX_SIZE", None)
        if saved is not None:
            os.environ["LAMC_CACHE_MAX_SIZE"] = saved


def case_lru_under_the_cap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "cache"
        now = time.time()
        paths = _populate(root, now)
        total = sum(item.size for item in P.scan(root))
        # Unlimited and a generous cap delete nothing.
        for cap in (0, total * 2):
            result = P.prune(root, cap, now=now)
            assert result.removed == 0 and all(p.exists() for p in paths.values()), cap
        # A cap of about three entries keeps the three most recently used.
        cap = 130_000
        result = P.prune(root, cap, now=now)
        assert result.removed > 0
        remaining = sum(item.size for item in P.scan(root))
        assert remaining <= cap, (remaining, cap)
        kept = [name for name, p in paths.items() if p.exists()]
        assert set(kept) <= {"library-new", "gobuild", "parsers", "checks"}, kept
        assert paths["library-new"].exists() and paths["gobuild"].exists(), kept
        for gone in ("library", "frontend", "harvest"):
            assert not paths[gone].exists(), gone
        # Ages order the evictions: everything removed is older than everything kept.
        assert result.oldest_kept is None or result.newest_removed <= result.oldest_kept
        assert (root / "gobuild" / f"{_key(6, 24)}.lock").exists(), "lock files stay"


def case_only_own_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "cache"
        outside = Path(tmp) / "outside"
        now = time.time()
        _populate(root, now)
        precious = [
            _file(root / "notes.txt", 50_000, 30 * DAY, now),
            _file(root / "0a" / "README.json", 50_000, 30 * DAY, now),
            _file(root / "frontend" / "zz" / "data.bin", 50_000, 30 * DAY, now),
            _file(outside / "0a" / f"{_key(9)}.json", 50_000, 30 * DAY, now),
            _file(outside / "gobuild" / _key(8, 24) / "main.go", 50_000, 30 * DAY, now),
        ]
        (root / "linked").symlink_to(outside, target_is_directory=True)
        (root / "gobuild" / _key(8, 24)).symlink_to(outside / "gobuild" / _key(8, 24), target_is_directory=True)
        _file(root / "frontend" / "0e" / f"{_key(10)}.lamfe", 10, 30 * DAY, now)
        P.prune(root, 1, now=now)
        for path in precious:
            assert path.exists(), f"{path} is not a cache entry: never deleted"
        assert not list(root.rglob("*.lamfe")), "own entries are pruned"


def case_skips_work_in_progress() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "cache"
        now = time.time()
        paths = _populate(root, now)
        fresh_tmp = _file(root / "0a" / ".lamc-cache-abc123.json", 30_000, 30, now)
        fresh_blob = _file(root / "harvest" / "0c" / ".lamc-blob-xyz.tmp", 30_000, 60, now)
        stale_tmp = _file(root / "frontend" / "0b" / ".lamc-fe-old.tmp", 30_000, DAY, now)
        lock_path = root / "gobuild" / f"{_key(6, 24)}.lock"
        fd = os.open(str(lock_path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)  # a build is using this work directory
            result = P.prune(root, 1, now=now)
            assert paths["gobuild"].exists(), "a locked Go work directory is skipped"
            assert result.skipped >= 3, result
        finally:
            os.close(fd)
        assert fresh_tmp.exists() and fresh_blob.exists(), "temporary files a build is writing stay"
        assert not stale_tmp.exists(), "a stale temporary file is removed"
        P.prune(root, 1, now=now)
        assert not paths["gobuild"].exists(), "unlocked, the work directory goes"


def case_hits_mark_entries_used() -> None:
    from compiler import cache as C

    with tempfile.TemporaryDirectory() as tmp:
        saved = os.environ.get("LAMC_CACHE_DIR")
        os.environ["LAMC_CACHE_DIR"] = str(Path(tmp) / "cache")
        try:
            C.save(b"lib", {"version": 1}, extra=b"x")
            C.save_blob("harvest", HEX62 + "00", b"data")
            C.mark_clean_check(b"mod", b"extra")
            old = time.time() - 30 * DAY
            files = [p for p in Path(tmp, "cache").rglob("*") if p.is_file()]
            assert len(files) == 3, files
            for p in files:
                os.utime(p, (old, old))
            assert C.load(b"lib", extra=b"x") == {"version": 1}
            assert C.load_blob("harvest", HEX62 + "00") == b"data"
            assert C.has_clean_check(b"mod", b"extra")
            for p in files:
                assert p.stat().st_mtime > old + DAY, f"{p} was read but not marked used"
            assert not C.has_clean_check(b"other", b"extra")
        finally:
            if saved is None:
                os.environ.pop("LAMC_CACHE_DIR", None)
            else:
                os.environ["LAMC_CACHE_DIR"] = saved


def case_stamp_throttles() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "cache"
        now = time.time()
        _populate(root, now)
        assert P.maybe_prune(root, max_size=1, interval=3600) is not None, "first run prunes"
        _populate(root, now)
        assert P.maybe_prune(root, max_size=1, interval=3600) is None, "throttled by the stamp"
        assert (root / "0a" / f"{_key(1)}.json").exists()
        assert P.maybe_prune(root, max_size=1, interval=0) is not None, "interval elapsed"
        assert not (root / "0a" / f"{_key(1)}.json").exists()
        assert P.maybe_prune(Path(tmp) / "missing", max_size=1, interval=0) is None, "no cache: nothing to do"
        assert not (Path(tmp) / "missing").exists(), "pruning creates nothing"


def _lamc(args: list[str], cache: Path, **env_extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "LAMC_CACHE_DIR": str(cache), **env_extra}
    return subprocess.run([*SCRIPT, *args], capture_output=True, text=True, timeout=600, env=env)


def case_builds_after_pruning() -> None:
    import test_parallel_build as T

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        main = T._write_program(tmp / "app", T.MODULES)
        cache = tmp / "cache"
        first = _lamc(["build", str(main), "--emit-go"], cache, LAMC_CACHE_PRUNE_INTERVAL="0")
        assert first.returncode == 0, first.stderr
        info = _lamc(["cache", "info"], cache)
        assert info.returncode == 0, info.stderr
        for kind in ("library", "frontend", "harvest", "checks", "total", "max size"):
            assert kind in info.stdout, info.stdout
        # A cap below one entry: the next build's automatic prune empties the cache.
        pruned = _lamc(["build", str(main), "--emit-go"], cache,
                       LAMC_CACHE_PRUNE_INTERVAL="0", LAMC_CACHE_MAX_SIZE="1")
        assert pruned.returncode == 0 and pruned.stdout == first.stdout, "same Go with the cache in use"
        assert not list(cache.rglob("*.lamfe")) and not list(cache.rglob("*.json")), "the cap was enforced"
        again = _lamc(["build", str(main), "--emit-go"], cache)
        assert again.returncode == 0 and again.stdout == first.stdout, "entries are recomputed: same Go"
        assert list(cache.rglob("*.lamfe")), "and stored again"
        # ``lamc cache prune --max-size`` and ``lamc cache clear``.
        manual = _lamc(["cache", "prune", "--max-size", "1"], cache)
        assert manual.returncode == 0 and "removed" in manual.stdout, manual.stdout + manual.stderr
        assert not list(cache.rglob("*.lamfe"))
        bad = _lamc(["cache", "prune", "--max-size", "lots"], cache)
        assert bad.returncode == 2, bad.stderr
        _lamc(["build", str(main), "--emit-go"], cache)
        clear = _lamc(["cache", "clear"], cache)
        assert clear.returncode == 0 and not list(cache.rglob("*.lamfe")), clear.stderr
        legacy = _lamc(["build", str(main), "--clear-cache"], cache)
        assert legacy.returncode == 0, legacy.stderr
        # --no-cache builds leave the cache alone (no prune either).
        _lamc(["build", str(main), "--emit-go"], cache)
        count = len(list(cache.rglob("*.lamfe")))
        nocache = _lamc(["build", str(main), "--emit-go", "--no-cache"], cache,
                        LAMC_CACHE_PRUNE_INTERVAL="0", LAMC_CACHE_MAX_SIZE="1")
        assert nocache.returncode == 0 and len(list(cache.rglob("*.lamfe"))) == count


def main() -> int:
    cases = [
        case_sizes,
        case_lru_under_the_cap,
        case_only_own_entries,
        case_skips_work_in_progress,
        case_hits_mark_entries_used,
        case_stamp_throttles,
        case_builds_after_pruning,
    ]
    for case in cases:
        case()
        print(f"  ok  {case.__name__}")
    print(f"cache prune: {len(cases)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
