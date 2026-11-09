"""Per-phase compile timings (``lamc build --timings`` / ``LAMC_TIMINGS=1``).

The pipeline in :func:`compiler.lammergeier.compile_lam` calls a handful of
module-level entry points. While timings are on, those entry points are
wrapped so their wall time accumulates per phase; nothing changes when they
are off. Phases:

``preprocess``  semicolon insertion, go! extraction, aliases (all modules)
``parse``       Lark parsing (all modules)
``semantic``    the semantic checker
``transpile``   Lam AST to Go (main program and libraries not in the cache)
``go_mod``      ``go mod init`` / ``go mod tidy``
``go_build``    ``go build`` of the generated program
``cache_load``  reading cached front-end results (preprocessed source + tree)
``workers``     waiting for the build's worker processes (``--workers``)
``total``       the whole ``compile_lam`` call

``preprocess`` / ``parse`` / ``semantic`` / ``transpile`` count the work done
in the ``lamc`` process; the same work done by worker processes is reported
separately under ``workers_ms`` (summed over the workers: it overlaps).
"""
from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager


_ACTIVE: "PhaseTimer | None" = None


def record(phase: str, seconds: float) -> None:
    """Add main-process time to ``phase`` when timings are on."""
    if _ACTIVE is not None:
        _ACTIVE.add(phase, seconds)


def record_worker(phase: str, seconds: float) -> None:
    """Add time a worker process spent in ``phase``."""
    if _ACTIVE is not None:
        _ACTIVE.add_worker(phase, seconds)


def active() -> bool:
    return _ACTIVE is not None


class PhaseTimer:
    def __init__(self) -> None:
        self.phases: dict[str, float] = {}
        self.counts: dict[str, int] = {}
        self.worker_phases: dict[str, float] = {}
        self.worker_counts: dict[str, int] = {}

    def add(self, phase: str, seconds: float) -> None:
        self.phases[phase] = self.phases.get(phase, 0.0) + seconds
        self.counts[phase] = self.counts.get(phase, 0) + 1

    def add_worker(self, phase: str, seconds: float) -> None:
        self.worker_phases[phase] = self.worker_phases.get(phase, 0.0) + seconds
        self.worker_counts[phase] = self.worker_counts.get(phase, 0) + 1

    def wrap(self, phase: str, fn):
        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.add(phase, time.perf_counter() - start)
        return timed

    def report(self) -> dict:
        out = {
            "phases_ms": {k: round(v * 1000, 3) for k, v in self.phases.items()},
            "calls": dict(self.counts),
        }
        if self.worker_phases:
            out["workers_ms"] = {k: round(v * 1000, 3) for k, v in self.worker_phases.items()}
            out["worker_calls"] = dict(self.worker_counts)
        return out


@contextmanager
def phase_timings(enabled: bool, driver=None):
    """Times the compile pipeline phases; prints one JSON line
    ``LAMC_TIMINGS {...}`` on stderr at the end. ``driver`` is the module
    running ``compile_lam`` (``lamc`` executes the compiler file as
    ``__main__``, a different module object than ``compiler.lammergeier``)."""
    if not enabled:
        yield None
        return
    if driver is None:
        from compiler import lammergeier as driver
    L = driver
    from compiler import semantic as S
    from compiler.transpiler import GoTranspiler

    timer = PhaseTimer()
    saved = {
        "preprocess": L.preprocess_for_parse,
        "create_parser": L.create_parser,
        "check_source": S.check_source,
        "transpile": GoTranspiler.transpile,
        "run": L.subprocess.run,
    }

    class _TimedParser:
        def __init__(self, inner) -> None:
            self._inner = inner

        def parse(self, *args, **kwargs):
            start = time.perf_counter()
            try:
                return self._inner.parse(*args, **kwargs)
            finally:
                timer.add("parse", time.perf_counter() - start)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    def timed_run(cmd, *args, **kwargs):
        phase = "subprocess"
        if isinstance(cmd, (list, tuple)) and len(cmd) >= 2 and cmd[0] == "go":
            phase = "go_build" if cmd[1] == "build" else "go_mod" if cmd[1] == "mod" else "go_other"
        start = time.perf_counter()
        try:
            return saved["run"](cmd, *args, **kwargs)
        finally:
            timer.add(phase, time.perf_counter() - start)

    L.preprocess_for_parse = timer.wrap("preprocess", saved["preprocess"])
    L.create_parser = lambda *a, **k: _TimedParser(saved["create_parser"](*a, **k))
    # Every module of the program (the compiled file and its imports).
    S.check_source = timer.wrap("semantic", saved["check_source"])
    GoTranspiler.transpile = timer.wrap("transpile", saved["transpile"])
    L.subprocess.run = timed_run
    global _ACTIVE
    previous = _ACTIVE
    _ACTIVE = timer
    start = time.perf_counter()
    try:
        yield timer
    finally:
        timer.add("total", time.perf_counter() - start)
        _ACTIVE = previous
        L.preprocess_for_parse = saved["preprocess"]
        L.create_parser = saved["create_parser"]
        S.check_source = saved["check_source"]
        GoTranspiler.transpile = saved["transpile"]
        L.subprocess.run = saved["run"]
        print("LAMC_TIMINGS " + json.dumps(timer.report(), sort_keys=True), file=sys.stderr)
