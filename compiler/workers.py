"""Process pools of one build (``lamc build --workers N``).

The independent per-module work of a build (parse preprocessing and parsing,
the semantic check of imported modules, the transpile of library modules)
can run in worker processes. Results are always consumed in module order,
so the output does not depend on the number of workers or on which worker
finishes first; ``--workers 1`` keeps everything in the ``lamc`` process.

The worker count defaults to ``LAMC_WORKERS`` when set, else to the CPUs
this process may run on, capped at :data:`DEFAULT_MAX_WORKERS`: past that,
starting and feeding more processes costs more than the remaining work they
take (the compile of one program has a few hundred modules at most, and the
modules are not equally large).

Pools use the platform's default ``multiprocessing`` start method (``fork``
on Linux up to Python 3.13, ``forkserver`` from 3.14, ``spawn`` on macOS and
Windows); ``LAMC_MP_START`` overrides it. A process with other threads
running does not ``fork`` (it uses ``spawn``). Whatever a stage's workers
need is passed to the pool initializer, which costs nothing under ``fork``
and is pickled once per worker otherwise.
"""
from __future__ import annotations

import multiprocessing
import os
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from typing import Any, Callable, Iterable, Sequence

DEFAULT_MAX_WORKERS = 16


def available_cpus() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def default_workers() -> int:
    return max(1, min(available_cpus(), DEFAULT_MAX_WORKERS))


def resolve_workers(requested: int | None) -> int:
    """The worker count of a build: ``requested`` (``--workers``), else
    ``LAMC_WORKERS``, else :func:`default_workers`. ``0`` means the default."""
    if requested is None:
        env = os.environ.get("LAMC_WORKERS", "").strip()
        if env:
            try:
                requested = int(env)
            except ValueError:
                requested = None
    if not requested or requested < 0:
        return default_workers()
    return requested


def start_method() -> str:
    method = os.environ.get("LAMC_MP_START", "").strip()
    if method:
        return method
    method = multiprocessing.get_start_method(allow_none=True) or multiprocessing.get_context().get_start_method()
    if method == "fork" and threading.active_count() > 1:
        return "spawn"
    return method


def min_tasks_for_pool() -> int:
    """Fewer pending tasks than this run in-process: a worker that has to
    import the compiler first (``spawn``) costs more than a few modules."""
    env = os.environ.get("LAMC_POOL_MIN_TASKS", "").strip()
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    return 2 if start_method() == "fork" else 8


def pool_size(workers: int, tasks: int, per_worker: int = 1) -> int:
    """Processes worth starting for ``tasks`` tasks: at most ``workers``,
    and at least ``per_worker`` tasks each (a worker's start-up, and the
    caches it fills before its tasks get fast, cost about that many tasks
    done here). ``0`` or ``1``: run them in this process."""
    if workers <= 1 or tasks < min_tasks_for_pool():
        return 0
    env = os.environ.get("LAMC_POOL_TASKS_PER_WORKER", "").strip()
    if env:  # tests: pools for small programs too
        try:
            per_worker = int(env)
        except ValueError:
            pass
    return min(workers, tasks // max(1, per_worker))


def make_pool(workers: int, tasks: int, initializer: Callable | None = None,
              initargs: tuple = (), per_worker: int = 1) -> ProcessPoolExecutor | None:
    """A process pool for ``tasks`` pending tasks, or ``None`` when they
    should run in-process (one worker, or too few tasks)."""
    count = pool_size(workers, tasks, per_worker)
    if count < 2:
        return None
    context = multiprocessing.get_context(start_method())
    return ProcessPoolExecutor(
        max_workers=count, mp_context=context, initializer=initializer, initargs=initargs,
    )


def run_ordered(pool: ProcessPoolExecutor | None, fn: Callable, items: Sequence[Any],
                local: Callable | None = None) -> Iterable[tuple[Any, BaseException | None, Any]]:
    """Run ``fn(item)`` for every item and yield ``(item, error, result)`` in
    the order of ``items`` (``error`` is the exception the call raised).
    Without a pool, ``local(item)`` (default ``fn``) runs in this process,
    one item at a time, as it is consumed."""
    if pool is None:
        call = local or fn
        for item in items:
            try:
                yield item, None, call(item)
            except Exception as exc:  # noqa: BLE001 - handed to the caller
                yield item, exc, None
        return
    from concurrent.futures import BrokenExecutor

    call = local or fn
    try:
        futures = [pool.submit(fn, item) for item in items]
    except BrokenExecutor:
        futures = [None] * len(items)
    for item, future in zip(items, futures):
        try:
            if future is None:
                raise BrokenExecutor("worker pool unavailable")
            yield item, None, future.result()
        except BrokenExecutor:
            # A worker died (killed, out of memory ...): do it here.
            try:
                yield item, None, call(item)
            except Exception as exc:  # noqa: BLE001 - handed to the caller
                yield item, exc, None
        except Exception as exc:  # noqa: BLE001 - handed to the caller
            yield item, exc, None


class StageClock:
    """Main-process time spent waiting on a pool, for ``--timings``."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.start = time.perf_counter()

    def stop(self) -> None:
        from compiler import timings

        timings.record(self.phase, time.perf_counter() - self.start)
