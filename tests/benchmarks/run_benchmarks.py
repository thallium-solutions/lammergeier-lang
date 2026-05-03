#!/usr/bin/env python3
"""Lammergeier benchmark harness.

Runs a curated set of ``.lam`` programs (under
``tests/benchmarks/cases/``) and reports four numbers per case:

* **compile** — wall time of ``lamc --no-cache <file>`` (Lam → binary).
  Uses ``--no-cache`` to measure *cold* performance so iteration
  effects don't vanish behind the disk cache.
* **run**     — best-of-``N`` wall time of executing the compiled
  binary (``N`` defaults to 3).
* **binary**  — size of the compiled Go binary in KiB.
* **lines**   — source LOC, useful for normalisation when comparing
  compile throughput across benchmarks.

Benchmarks are grouped by directory — typically ``language/`` for
core language microbenchmarks and ``stdlib/`` for stdlib-targeted
cases. The runner prints one table per group plus an overall
summary, and writes the raw stats as JSON to a path of the user's
choosing (``--json``) so CI pipelines can track regressions.

Usage::

    python3 tests/benchmarks/run_benchmarks.py
    python3 tests/benchmarks/run_benchmarks.py -f fib     # filter by name
    python3 tests/benchmarks/run_benchmarks.py --runs 5   # more samples
    python3 tests/benchmarks/run_benchmarks.py --json out.json

Exit code is always 0 when every case *builds and runs*; a crash
in any case fails the whole run, which is deliberate — a benchmark
that stops compiling is a regression worth surfacing loudly.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional


def _find_project_root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / "compiler" / "lammergeier.py").is_file():
            return p
    return start.parent.parent


PROJECT_ROOT = _find_project_root(Path(__file__).resolve())
COMPILER = PROJECT_ROOT / "compiler" / "lammergeier.py"
PYTHON = sys.executable


# ─── Result record ──────────────────────────────────────────────


@dataclass
class BenchResult:
    """Single benchmark's measurements.

    ``name`` is the relative path from ``cases/`` without the ``.lam``
    suffix so identical benchmarks across runs are directly comparable.
    """
    group: str
    name: str
    compile_ms: float
    run_ms: float         # best-of-N
    run_ms_mean: float
    run_ms_stdev: float
    binary_kib: float
    lines: int

    def to_row(self) -> tuple:
        return (
            self.name,
            f"{self.compile_ms:8.1f}",
            f"{self.run_ms:8.2f}",
            f"{self.run_ms_mean:8.2f}",
            f"{self.run_ms_stdev:6.2f}",
            f"{self.binary_kib:8.1f}",
            f"{self.lines:6d}",
        )


# ─── Single benchmark runner ────────────────────────────────────


def _time_compile(lam_file: Path, out_dir: Path) -> tuple[float, Path]:
    """Compile ``lam_file`` with ``--no-cache`` into ``out_dir``.

    Returns ``(wall_ms, binary_path)``. Raises :class:`RuntimeError`
    with the captured stderr if compilation fails — benchmarking a
    broken build would report meaningless timings.
    """
    binary = out_dir / lam_file.stem
    start = time.perf_counter()
    proc = subprocess.run(
        [PYTHON, str(COMPILER), str(lam_file),
         "-o", str(binary), "--no-cache"],
        capture_output=True, text=True, timeout=120,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    if proc.returncode != 0:
        raise RuntimeError(
            f"compilation failed for {lam_file.name}:\n"
            f"STDERR:\n{proc.stderr[:2000]}"
        )
    if not binary.is_file():
        raise RuntimeError(
            f"compiler exited 0 but produced no binary at {binary}"
        )
    return elapsed_ms, binary


def _time_run(binary: Path, runs: int) -> tuple[float, float, float]:
    """Execute ``binary`` ``runs`` times, returning
    ``(best_ms, mean_ms, stdev_ms)``. The binary's stdout/stderr is
    captured and discarded to avoid terminal I/O dominating tiny
    runtimes.
    """
    samples: List[float] = []
    for _ in range(runs):
        start = time.perf_counter()
        proc = subprocess.run(
            [str(binary)],
            capture_output=True, timeout=60,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        if proc.returncode != 0:
            raise RuntimeError(
                f"binary exited non-zero ({proc.returncode}) — "
                f"stderr head:\n{proc.stderr.decode('utf-8', 'replace')[:400]}"
            )
        samples.append(elapsed_ms)
    best = min(samples)
    mean = statistics.fmean(samples)
    stdev = statistics.stdev(samples) if len(samples) > 1 else 0.0
    return best, mean, stdev


def _line_count(lam_file: Path) -> int:
    """Count non-blank, non-pure-comment lines — a rough proxy for
    "effective LOC" that ignores doc-only files."""
    total = 0
    for line in lam_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        total += 1
    return total


def run_one(
    lam_file: Path,
    group: str,
    *,
    runs: int,
    tmpdir: Path,
) -> BenchResult:
    """Build, measure, and return one ``BenchResult``. Each call
    gets its own tmp output path so a leftover binary from a
    previous invocation can't pollute the reading.
    """
    local_out = tmpdir / lam_file.stem
    local_out.mkdir(parents=True, exist_ok=True)
    compile_ms, binary = _time_compile(lam_file, local_out)
    try:
        best_ms, mean_ms, stdev_ms = _time_run(binary, runs)
        size_kib = binary.stat().st_size / 1024.0
    finally:
        # Clean up the binary after measurement so repeated runs
        # don't fill tmpfs. The containing dir is wiped by the
        # outer finally in :func:`main`.
        try:
            binary.unlink()
        except OSError:
            pass
    rel_name = lam_file.stem
    return BenchResult(
        group=group,
        name=rel_name,
        compile_ms=compile_ms,
        run_ms=best_ms,
        run_ms_mean=mean_ms,
        run_ms_stdev=stdev_ms,
        binary_kib=size_kib,
        lines=_line_count(lam_file),
    )


# ─── Pretty-printing ────────────────────────────────────────────


_HEADERS = ("benchmark", "compile(ms)", "run-best(ms)", "run-mean(ms)",
            "σ(ms)", "size(KiB)", "LOC")


def _print_table(title: str, rows: List[BenchResult]) -> None:
    """Render a single group's rows as a fixed-width table."""
    if not rows:
        return
    print(f"\n── {title} ({len(rows)} benchmarks) ──────────────────────────────────")
    # Dynamic widths so the benchmark-name column doesn't truncate.
    name_w = max(len(_HEADERS[0]), max(len(r.name) for r in rows))
    hdr = (f"  {_HEADERS[0]:<{name_w}}  "
           f"{_HEADERS[1]:>10}  {_HEADERS[2]:>12}  "
           f"{_HEADERS[3]:>12}  {_HEADERS[4]:>8}  "
           f"{_HEADERS[5]:>10}  {_HEADERS[6]:>6}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        cells = r.to_row()
        print(f"  {r.name:<{name_w}}  "
              f"{cells[1]:>10}  {cells[2]:>12}  "
              f"{cells[3]:>12}  {cells[4]:>8}  "
              f"{cells[5]:>10}  {cells[6]:>6}")


def _print_summary(rows: List[BenchResult]) -> None:
    """Aggregate totals — useful for "is the compiler faster than
    last week?" spot checks without loading the JSON output."""
    if not rows:
        return
    total_compile = sum(r.compile_ms for r in rows)
    total_run = sum(r.run_ms for r in rows)
    total_lines = sum(r.lines for r in rows)
    total_size = sum(r.binary_kib for r in rows)
    throughput = total_lines / (total_compile / 1000.0) if total_compile else 0.0
    print("\n── summary ───────────────────────────────────────────────────────────────")
    print(f"  benchmarks:          {len(rows)}")
    print(f"  total compile time:  {total_compile:10.1f} ms")
    print(f"  total run time:      {total_run:10.1f} ms (best-of-N per case)")
    print(f"  aggregate LOC:       {total_lines:10d}")
    print(f"  aggregate binary:    {total_size:10.1f} KiB")
    print(f"  compile throughput:  {throughput:10.1f} lines/sec")


# ─── CLI entrypoint ─────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Lammergeier benchmark runner — measures compile + run "
                    "time and binary size for each .lam case.",
    )
    ap.add_argument(
        "--filter", "-f", default=None,
        help="Substring matched against the benchmark name (case-insensitive).",
    )
    ap.add_argument(
        "--runs", "-n", type=int, default=3,
        help="Run each compiled binary this many times; best/mean/stdev "
             "are reported (default: 3).",
    )
    ap.add_argument(
        "--json", dest="json_out", default=None,
        help="Write raw measurements as JSON to this path. Useful "
             "for CI regression tracking.",
    )
    ap.add_argument(
        "--warm", action="store_true",
        help="Don't pass --no-cache to the compiler; measures cached "
             "compile performance instead of cold.",
    )
    args = ap.parse_args()

    cases_dir = Path(__file__).resolve().parent / "cases"
    if not cases_dir.is_dir():
        print(f"error: no benchmark cases directory at {cases_dir}",
              file=sys.stderr)
        sys.exit(1)

    all_files = sorted(cases_dir.rglob("*.lam"))
    if args.filter:
        needle = args.filter.lower()
        all_files = [f for f in all_files if needle in f.name.lower()]
    if not all_files:
        print("no matching benchmark cases", file=sys.stderr)
        sys.exit(1)

    # ``--warm`` toggles --no-cache in the compile command; we
    # monkey-patch the helper instead of plumbing a flag through
    # every call site because the behaviour is harness-wide.
    if args.warm:
        global _time_compile
        original = _time_compile

        def _time_compile_warm(lam_file: Path, out_dir: Path):
            binary = out_dir / lam_file.stem
            start = time.perf_counter()
            proc = subprocess.run(
                [PYTHON, str(COMPILER), str(lam_file), "-o", str(binary)],
                capture_output=True, text=True, timeout=120,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            if proc.returncode != 0:
                raise RuntimeError(
                    f"compilation failed for {lam_file.name}:\n"
                    f"STDERR:\n{proc.stderr[:2000]}"
                )
            return elapsed_ms, binary

        _time_compile = _time_compile_warm  # type: ignore[assignment]

    print(f"Running {len(all_files)} benchmarks "
          f"({args.runs} run(s) each, {'warm' if args.warm else 'cold'} compile)...",
          flush=True)

    # All builds share a throwaway tmpdir — keeps the user's repo
    # free of benchmark artefacts and means a failure mid-run
    # doesn't leave ``.go`` files behind.
    tmpdir_root = Path(tempfile.mkdtemp(prefix="lam-bench-"))
    results: List[BenchResult] = []
    failures: List[tuple[str, str]] = []
    try:
        for lam_file in all_files:
            group = lam_file.parent.name if lam_file.parent != cases_dir else "top"
            try:
                r = run_one(lam_file, group, runs=args.runs, tmpdir=tmpdir_root)
                results.append(r)
                print(f"  ✓ {group}/{r.name}  "
                      f"compile={r.compile_ms:7.1f}ms  "
                      f"run={r.run_ms:6.2f}ms  "
                      f"size={r.binary_kib:6.1f}KiB")
            except Exception as exc:
                failures.append((str(lam_file), str(exc)))
                print(f"  ✗ {group}/{lam_file.stem}  FAILED", file=sys.stderr)
                print(f"    {exc}", file=sys.stderr)
    finally:
        shutil.rmtree(tmpdir_root, ignore_errors=True)

    # Group rows by directory so language/ and stdlib/ each get
    # their own table.
    by_group: Dict[str, List[BenchResult]] = {}
    for r in results:
        by_group.setdefault(r.group, []).append(r)
    for group in sorted(by_group):
        _print_table(group, by_group[group])
    _print_summary(results)

    if args.json_out:
        path = Path(args.json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = {
            "schema_version": 1,
            "python": sys.version.split()[0],
            "project_root": str(PROJECT_ROOT),
            "runs_per_bench": args.runs,
            "warm": args.warm,
            "results": [asdict(r) for r in results],
            "failures": failures,
        }
        path.write_text(json.dumps(payload, indent=2))
        print(f"\nJSON stats written to {path}")

    if failures:
        print(f"\n{len(failures)} benchmark(s) failed — see stderr above.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
