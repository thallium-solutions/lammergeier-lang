#!/usr/bin/env python3
"""Integration tests for the library-transpile cache.

The cache is off the hot path for individual ``.lam`` tests (each one
runs its own subprocess that uses the default cache dir), so we
exercise it here with a dedicated cache directory and verify:

- A fresh build on an empty cache writes at least one entry.
- A second build on the same source produces byte-identical Go output.
- ``--no-cache`` bypasses the cache entirely (no new files written).
- A content change in the library busts the key and emits a second
  entry without touching the first.
- ``--clear-cache`` removes every entry under the configured dir.
- The ``LAMC_CACHE_DIR`` env override is honoured end-to-end.

Run with::

    python3 tests/tests/test_cache_integration.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]


def _count(p: Path) -> int:
    if not p.is_dir():
        return 0
    return sum(1 for _ in p.rglob("*.json"))


def _run(args, cache_dir: Path, check: bool = True) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "LAMC_CACHE_DIR": str(cache_dir),
        "PATH": os.environ.get("PATH", ""),
    }
    return subprocess.run(
        LAMC + list(args),
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _write_project(root: Path) -> tuple[Path, Path]:
    """Create a tiny project with one library and a main file that imports it."""
    lib = root / "lib" / "widget.lam"
    lib.parent.mkdir(parents=True, exist_ok=True)
    lib.write_text(
        "func widget_label(id: int) -> str {\n"
        '    return f"widget-{id}"\n'
        "}\n",
        encoding="utf-8",
    )
    main = root / "main.lam"
    main.write_text(
        "from widget import widget_label\n\n"
        "func main() {\n"
        "    print(widget_label(42))\n"
        "}\n",
        encoding="utf-8",
    )
    return main, lib


# ─── Cases ────────────────────────────────────────────────────


def case_populate_and_hit() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        cache = Path(td) / "cache"
        main, _lib = _write_project(proj)

        assert _count(cache) == 0
        out1 = _run([str(main), "--emit-go"], cache).stdout
        assert _count(cache) == 1, "cold build should write one cache entry"

        out2 = _run([str(main), "--emit-go"], cache).stdout
        assert out1 == out2, "warm build must produce identical Go output"
        assert _count(cache) == 1, "warm build should not create new entries"


def case_no_cache_flag() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        cache = Path(td) / "cache"
        main, _lib = _write_project(proj)

        _run([str(main), "--no-cache", "--emit-go"], cache)
        assert _count(cache) == 0, "--no-cache must not write cache entries"


def case_content_invalidates() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        cache = Path(td) / "cache"
        main, lib = _write_project(proj)

        _run([str(main), "--emit-go"], cache)
        assert _count(cache) == 1

        # Tweak the lib's body — the cache key is content-addressed so a
        # single-byte change should add a new entry and leave the old one.
        lib.write_text(
            lib.read_text(encoding="utf-8").replace(
                'f"widget-{id}"', 'f"gadget-{id}"'
            ),
            encoding="utf-8",
        )
        _run([str(main), "--emit-go"], cache)
        assert _count(cache) == 2, (
            "content change should add a new cache entry, not replace"
        )


def case_clear_cache() -> None:
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        cache = Path(td) / "cache"
        main, _lib = _write_project(proj)

        _run([str(main), "--emit-go"], cache)
        assert _count(cache) == 1

        _run([str(main), "--clear-cache"], cache)
        assert _count(cache) == 0, "--clear-cache should remove every entry"


def case_warm_output_matches_no_cache() -> None:
    """The whole point of the cache: it produces the same Go."""
    with tempfile.TemporaryDirectory() as td:
        proj = Path(td) / "proj"
        cache = Path(td) / "cache"
        main, _lib = _write_project(proj)

        warm = _run([str(main), "--emit-go"], cache).stdout  # cold → warm
        bypass = _run([str(main), "--no-cache", "--emit-go"], cache).stdout
        assert warm == bypass, "cache output must match the uncached build"


# ─── Driver ───────────────────────────────────────────────────


CASES = [
    case_populate_and_hit,
    case_no_cache_flag,
    case_content_invalidates,
    case_clear_cache,
    case_warm_output_matches_no_cache,
]


def main() -> int:
    failed = 0
    for case in CASES:
        try:
            case()
        except AssertionError as e:
            print(f"  FAIL {case.__name__}: {e}", file=sys.stderr)
            failed += 1
        except subprocess.CalledProcessError as e:
            print(
                f"  ERR  {case.__name__}: command failed\n    stdout={e.stdout}\n    stderr={e.stderr}",
                file=sys.stderr,
            )
            failed += 1
        else:
            print(f"  PASS {case.__name__}")
    total = len(CASES)
    passed = total - failed
    print(f"\nCache integration: {passed} passed, {failed} failed, {total} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
