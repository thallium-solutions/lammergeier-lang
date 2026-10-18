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
``total``       the whole ``compile_lam`` call
"""
from __future__ import annotations

import json
import sys
import time
from contextlib import contextmanager


class PhaseTimer:
    def __init__(self) -> None:
        self.phases: dict[str, float] = {}
        self.counts: dict[str, int] = {}

    def add(self, phase: str, seconds: float) -> None:
        self.phases[phase] = self.phases.get(phase, 0.0) + seconds
        self.counts[phase] = self.counts.get(phase, 0) + 1

    def wrap(self, phase: str, fn):
        def timed(*args, **kwargs):
            start = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                self.add(phase, time.perf_counter() - start)
        return timed

    def report(self) -> dict:
        return {
            "phases_ms": {k: round(v * 1000, 3) for k, v in self.phases.items()},
            "calls": dict(self.counts),
        }


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
    S.check_source = timer.wrap("semantic", saved["check_source"])
    GoTranspiler.transpile = timer.wrap("transpile", saved["transpile"])
    L.subprocess.run = timed_run
    start = time.perf_counter()
    try:
        yield timer
    finally:
        timer.add("total", time.perf_counter() - start)
        L.preprocess_for_parse = saved["preprocess"]
        L.create_parser = saved["create_parser"]
        S.check_source = saved["check_source"]
        GoTranspiler.transpile = saved["transpile"]
        L.subprocess.run = saved["run"]
        print("LAMC_TIMINGS " + json.dumps(timer.report(), sort_keys=True), file=sys.stderr)
