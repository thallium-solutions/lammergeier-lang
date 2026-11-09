#!/usr/bin/env python3
"""``lamc build --workers N`` and the build caches do not change the output.

The generated Go of a multi-module program (packages with re-exports,
relative and aliased imports, a class name two modules define, an interface,
a generic class, defaults, overloads, top-level statements, the stdlib) and of
the largest benchmark corpus program is byte-identical for ``--workers 1``
without any cache (the reference), ``--workers 4`` on an empty cache,
``--workers 4`` / ``--workers 1`` on the primed cache, under the ``spawn``
start method, and for two builds sharing one cache at the same time. After
editing a module (its body, then its signatures) the cached build equals a
build without cache of the edited program. Syntax errors and semantic errors
in imported modules are reported with the same text whatever the worker
count (the first one in module order).

Pools are forced on for these small programs with ``LAMC_POOL_MIN_TASKS=1``
and ``LAMC_POOL_TASKS_PER_WORKER=1``.

Run with ``python3 tests/tests/test_parallel_build.py``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]
LAUNCHER = [sys.executable, str(ROOT / "lamc")]

MODULES = {
    "geometry/__init__.lam": """from .shapes import Point, Shape, describe
from .grid import Grid
""",
    "geometry/shapes.lam": """interface Shape {
    func area(self) -> float
}

class Point {
    func init(self, x: float, y: float) {
        self.x: float = x
        self.y: float = y
    }

    func dist2(self, other: Point) -> float {
        dx: float = self.x - other.x
        dy: float = self.y - other.y
        return dx * dx + dy * dy
    }
}

func describe(s: Shape, label: str = "shape") -> str {
    return f"{label} {s.area()}"
}
""",
    "geometry/grid.lam": """from .shapes import Point

class Grid {
    func init(self, size: int) {
        self.size: int = size
        self.points: list[Point] = []
    }

    func add(self, p: Point) -> int {
        self.points.append(p)
        return len(self.points)
    }

    func add(self, x: float, y: float) -> int {
        return self.add(Point(x, y))
    }
}
""",
    "store/box.lam": """class Box[T] {
    func init(self, value: T) {
        self.value: T = value
    }

    func get(self) -> T {
        return self.value
    }
}

func boxed(n: int) -> Box[int] {
    return Box[int](n)
}
""",
    "store/config.lam": """const Limit: int = 3

class Config {
    func init(self, name: str) {
        self.name: str = name
    }

    func label(self) -> str {
        return "store:" + self.name
    }
}

print("store config loaded")
""",
    "report/config.lam": """class Config {
    func init(self, title: str, width: int = 10) {
        self.title: str = title
        self.width: int = width
    }

    func label(self) -> str {
        return "report:" + self.title.toUpper()
    }
}
""",
    "report/render.lam": """from lamstrings import Strings
from lamerrors import Result, Error
import report.config as rc
from geometry import Shape, describe

class Square {
    func init(self, side: float) {
        self.side: float = side
    }

    func area(self) -> float {
        return self.side * self.side
    }
}

func render(title: str, items: list[str]) -> Result[str] {
    if len(items) == 0 {
        return Result.Err(Error("EmptyReport", "nothing to render"))
    }
    cfg: rc.Config = rc.Config(title)
    lines: list[str] = [cfg.label()]
    for item in items {
        lines.append(Strings.padLeft(item, cfg.width))
    }
    lines.append(describe(Square(2.0)))
    return Result.Ok("\\n".join(lines))
}
""",
    "main.lam": """from geometry import Point, Grid
from store.box import Box, boxed
from store.config import Config, Limit
from report.render import render
from lamerrors import Result

func main() {
    g: Grid = Grid(Limit)
    _ = g.add(Point(1.0, 2.0))
    n: int = g.add(3.0, 4.0)
    b: Box[int] = boxed(n)
    c: Config = Config("main")
    out: Result[str] = render(c.label(), [f"{b.get()}", "two"])
    if out.ok() {
        print(out.unwrap())
    }
    print(Point(0.0, 0.0).dist2(Point(3.0, 4.0)))
}
""",
}

FORCE_POOLS = {"LAMC_POOL_MIN_TASKS": "1", "LAMC_POOL_TASKS_PER_WORKER": "1"}


def _write_program(root: Path, modules: dict[str, str]) -> Path:
    for rel, text in modules.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root / "main.lam"


def _emit(cmd: list[str], main: Path, cache: Path, workers: int, *flags: str,
          env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "LAMC_CACHE_DIR": str(cache), **FORCE_POOLS, **(env_extra or {})}
    env.pop("LAMC_WORKERS", None)
    return subprocess.run(
        [*cmd, "build", str(main), "--emit-go", "--workers", str(workers), *flags],
        cwd=str(main.parent), env=env, capture_output=True, text=True, timeout=600,
    )


def _go(proc: subprocess.CompletedProcess, what: str) -> str:
    assert proc.returncode == 0, f"{what}: lamc failed\n{proc.stderr[-3000:]}"
    assert "package main" in proc.stdout, what
    return proc.stdout


def _reference(cmd, main: Path, tmp: Path) -> str:
    return _go(_emit(cmd, main, tmp / "ref-cache", 1, "--no-cache"), "reference (--workers 1 --no-cache)")


def _check_modes(cmd, main: Path, tmp: Path, label: str) -> None:
    reference = _reference(cmd, main, tmp)
    cache = tmp / f"cache-{label}"
    modes = [
        ("--workers 4, empty cache", cache, 4, {}),
        ("--workers 4, primed cache", cache, 4, {}),
        ("--workers 1, primed cache", cache, 1, {}),
        ("--workers 4, spawn, empty cache", tmp / f"cache-{label}-spawn", 4, {"LAMC_MP_START": "spawn"}),
        ("--workers 3, spawn, primed cache", tmp / f"cache-{label}-spawn", 3, {"LAMC_MP_START": "spawn"}),
    ]
    for what, mode_cache, workers, env_extra in modes:
        out = _go(_emit(cmd, main, mode_cache, workers, env_extra=env_extra), f"{label}: {what}")
        assert out == reference, f"{label}: {what}: generated Go differs from the --workers 1 --no-cache build"


def case_multi_module_program() -> None:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        main = _write_program(tmp / "app", MODULES)
        _check_modes(SCRIPT, main, tmp, "multi-module")
        _check_modes(LAUNCHER, main, tmp, "multi-module (lamc launcher)")


def case_corpus_program() -> None:
    source = ROOT / "benchmarks" / "compiler" / "corpus" / "resilient_service_pipeline.lam"
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        main = tmp / "app" / source.name
        main.parent.mkdir(parents=True)
        shutil.copy(source, main)
        _check_modes(SCRIPT, main, tmp, "resilient_service_pipeline")


def case_script_and_launcher_share_a_cache() -> None:
    # ``compiler/lammergeier.py`` runs as ``__main__`` (the test runners) or
    # is imported by the ``lamc`` launcher: each reads the other's entries.
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        main = _write_program(tmp / "app", MODULES)
        reference = _reference(SCRIPT, main, tmp)
        for order in ((SCRIPT, LAUNCHER, SCRIPT), (LAUNCHER, SCRIPT, LAUNCHER)):
            cache = tmp / f"shared-{len(list(tmp.iterdir()))}"
            for cmd in order:
                for workers in (1, 4):
                    out = _go(_emit(cmd, main, cache, workers), f"{cmd[-1]} --workers {workers}")
                    assert out == reference, f"{cmd[-1]} on a cache shared with the other driver"


def case_concurrent_builds_share_a_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        main = _write_program(tmp / "app", MODULES)
        reference = _reference(SCRIPT, main, tmp)
        cache = tmp / "shared"
        env = {**os.environ, "LAMC_CACHE_DIR": str(cache), **FORCE_POOLS}
        procs = [
            subprocess.Popen(
                [*SCRIPT, "build", str(main), "--emit-go", "--workers", str(workers)],
                cwd=str(main.parent), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for workers in (1, 4, 4)
        ]
        for proc in procs:
            out, err = proc.communicate(timeout=600)
            assert proc.returncode == 0, err[-3000:]
            assert out == reference, "a build racing others on one cache differs"
        assert not list(cache.rglob("*.tmp")), "temporary cache files left behind"


def case_edits_invalidate_exactly() -> None:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        main = _write_program(tmp / "app", MODULES)
        cache = tmp / "cache"
        _go(_emit(SCRIPT, main, cache, 4), "priming build")
        edits = [
            # A body: only that module's results change.
            ("geometry/shapes.lam", 'return f"{label} {s.area()}"', 'return f"{label}: {s.area()}"'),
            # A signature and a new function: every library's inputs change.
            ("store/box.lam", "func boxed(n: int) -> Box[int] {",
             "func unboxed(b: Box[int]) -> int {\n    return b.get()\n}\n\nfunc boxed(n: int, extra: int = 0) -> Box[int] {"),
            # A class renamed in one module only (the namespace plans change).
            ("report/config.lam", "class Config {", "class Config {\n    static func make() -> str {\n        return \"x\"\n    }\n"),
        ]
        for rel, old, new in edits:
            path = main.parent / rel
            text = path.read_text(encoding="utf-8")
            assert old in text, (rel, old)
            path.write_text(text.replace(old, new, 1), encoding="utf-8")
            fresh = _reference(SCRIPT, main, tmp / f"fresh-{rel.replace('/', '_')}")
            for workers in (4, 1):
                cached = _go(_emit(SCRIPT, main, cache, workers), f"after editing {rel} (--workers {workers})")
                assert cached == fresh, f"after editing {rel}: the cached build (--workers {workers}) differs from a fresh one"


def case_errors_are_the_same() -> None:
    broken = dict(MODULES)
    broken["store/config.lam"] = MODULES["store/config.lam"].replace("func label(self) -> str {", "func label(self) -> str {{")
    broken["report/config.lam"] = MODULES["report/config.lam"].replace("self.width: int = width", "self.width: int = = width")
    semantic = dict(MODULES)
    semantic["store/config.lam"] = MODULES["store/config.lam"].replace('return "store:" + self.name', "return 1 + undefinedName")
    semantic["geometry/grid.lam"] = MODULES["geometry/grid.lam"].replace("return len(self.points)", "return missingToo")
    for label, modules in (("syntax errors", broken), ("semantic errors", semantic)):
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            main = _write_program(tmp / "app", modules)
            results = []
            for workers, cache in ((1, tmp / "c1"), (4, tmp / "c4"), (4, tmp / "c4")):
                proc = _emit(SCRIPT, main, cache, workers)
                assert proc.returncode != 0, f"{label}: the build must fail"
                results.append(proc.stderr)
            assert results[0] == results[1] == results[2], (
                f"{label}: reports differ between --workers 1 and 4:\n--- 1\n{results[0]}\n--- 4\n{results[1]}"
            )
            assert "error" in results[0].lower(), results[0]


def case_worker_count_flag() -> None:
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        main = _write_program(tmp / "app", MODULES)
        bad = subprocess.run([*SCRIPT, "build", str(main), "--emit-go", "--workers", "-2"],
                             capture_output=True, text=True, timeout=120)
        assert bad.returncode == 2 and "worker count" in bad.stderr, bad.stderr
        reference = _reference(SCRIPT, main, tmp)
        # 0 is the default; LAMC_WORKERS sets the default.
        for workers, env_extra in ((0, {}), (0, {"LAMC_WORKERS": "2"})):
            out = _go(_emit(SCRIPT, main, tmp / "c", workers, env_extra=env_extra), f"--workers {workers} {env_extra}")
            assert out == reference, (workers, env_extra)
        sys.path.insert(0, str(ROOT))
        from compiler import workers as _workers
        saved = os.environ.pop("LAMC_WORKERS", None)
        try:
            assert _workers.resolve_workers(None) == _workers.default_workers()
            assert 1 <= _workers.default_workers() <= _workers.DEFAULT_MAX_WORKERS
            assert _workers.resolve_workers(1) == 1 and _workers.resolve_workers(5) == 5
            os.environ["LAMC_WORKERS"] = "3"
            assert _workers.resolve_workers(None) == 3
            assert _workers.resolve_workers(1) == 1, "the flag wins over LAMC_WORKERS"
        finally:
            os.environ.pop("LAMC_WORKERS", None)
            if saved is not None:
                os.environ["LAMC_WORKERS"] = saved


def main() -> int:
    cases = [
        case_worker_count_flag,
        case_multi_module_program,
        case_corpus_program,
        case_script_and_launcher_share_a_cache,
        case_concurrent_builds_share_a_cache,
        case_edits_invalidate_exactly,
        case_errors_are_the_same,
    ]
    for case in cases:
        case()
        print(f"  ok  {case.__name__}")
    print(f"parallel build: {len(cases)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
