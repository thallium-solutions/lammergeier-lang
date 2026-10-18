#!/usr/bin/env python3
"""Runtime benchmarks: how fast is the code the compiler generates?

Each ``cases/**/bench_<name>.lam`` program times its own workload (so
process start, Go runtime init and imports are excluded) and prints::

    BENCH <name> <ops> <elapsed_ns>

The runner compiles every program once, runs it ``--runs`` times and reports
the median (and best) time per operation. When a hand-written Go program
``bench_<name>.go`` sits next to it, the same is done for the baseline and
the ``lam/go`` ratio shows the overhead of the generated code.

Usage::

    python3 benchmarks/runtime/run_runtime_bench.py
    python3 benchmarks/runtime/run_runtime_bench.py -f json --runs 9
    python3 benchmarks/runtime/run_runtime_bench.py --json out/runtime.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field, asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
CASES = HERE / "cases"
LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]
BENCH_LINE = re.compile(r"^BENCH\s+(\S+)\s+(\d+)\s+(\d+)\s*$", re.M)


@dataclass
class Sample:
    name: str
    ops: int
    runs_ns: list[int] = field(default_factory=list)

    @property
    def median_ns(self) -> float:
        return statistics.median(self.runs_ns)

    @property
    def best_ns(self) -> int:
        return min(self.runs_ns)

    @property
    def spread_pct(self) -> float:
        """Median absolute deviation as a percentage of the median."""
        med = self.median_ns
        if med == 0 or len(self.runs_ns) < 2:
            return 0.0
        mad = statistics.median(abs(x - med) for x in self.runs_ns)
        return 100.0 * mad / med


@dataclass
class Result:
    group: str
    name: str
    ops: int
    lam: Sample
    go: Sample | None = None
    error: str = ""


def build_lam(source: Path, out: Path) -> str:
    proc = subprocess.run([*LAMC, str(source), "-o", str(out)], capture_output=True, text=True, cwd=ROOT)
    return "" if proc.returncode == 0 else (proc.stderr.strip().splitlines() or ["build failed"])[-1]


def build_go(source: Path, out: Path) -> str:
    proc = subprocess.run(["go", "build", "-o", str(out), str(source)], capture_output=True, text=True,
                          cwd=out.parent, env={**os.environ, "GO111MODULE": "off"})
    return "" if proc.returncode == 0 else (proc.stderr.strip().splitlines() or ["go build failed"])[-1]


def run(binary: Path, runs: int, timeout: int) -> tuple[Sample | None, str]:
    sample: Sample | None = None
    for _ in range(runs):
        proc = subprocess.run([str(binary)], capture_output=True, text=True, timeout=timeout)
        m = BENCH_LINE.search(proc.stdout)
        if proc.returncode != 0 or not m:
            return None, (proc.stderr.strip() or "no BENCH line in the output")[-300:]
        if sample is None:
            sample = Sample(m.group(1), int(m.group(2)))
        sample.runs_ns.append(int(m.group(3)))
    return sample, ""


def fmt_ns(ns: float) -> str:
    if ns >= 1e6:
        return f"{ns / 1e6:.2f} ms"
    if ns >= 1e3:
        return f"{ns / 1e3:.2f} us"
    return f"{ns:.1f} ns"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-f", "--filter", default="", help="only benchmarks whose path contains this text")
    ap.add_argument("--runs", type=int, default=5, help="runs per program (default 5)")
    ap.add_argument("--timeout", type=int, default=120, help="seconds per run")
    ap.add_argument("--no-baseline", action="store_true", help="skip the hand-written Go baselines")
    ap.add_argument("--json", help="write the results to this file")
    args = ap.parse_args()

    cases = sorted(p for p in CASES.rglob("bench_*.lam") if args.filter.lower() in str(p.relative_to(CASES)).lower())
    if not cases:
        print("no benchmarks match", file=sys.stderr)
        return 1
    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="lam-runtime-bench-") as tmp:
        tmpdir = Path(tmp)
        for case in cases:
            group = case.parent.name
            lam_bin = tmpdir / case.stem
            error = build_lam(case, lam_bin)
            lam, run_error = (None, error) if error else run(lam_bin, args.runs, args.timeout)
            if lam is None:
                results.append(Result(group, case.stem[6:], 0, Sample(case.stem[6:], 0), None, run_error or error))
                print(f"  FAIL {case.relative_to(CASES)}: {run_error or error}", file=sys.stderr)
                continue
            result = Result(group, lam.name, lam.ops, lam)
            baseline = case.with_suffix(".go")
            if baseline.exists() and not args.no_baseline:
                go_bin = tmpdir / (case.stem + "_go")
                if not build_go(baseline, go_bin):
                    result.go, _ = run(go_bin, args.runs, args.timeout)
            results.append(result)
            print(f"  ran {case.relative_to(CASES)}", file=sys.stderr)

    print()
    header = f"{'benchmark':<26}{'ops':>10}{'median':>12}{'per op':>12}{'best':>12}{'spread':>8}{'go per op':>12}{'lam/go':>8}"
    print(header)
    print("-" * len(header))
    for group in sorted({r.group for r in results}):
        print(f"[{group}]")
        for r in (x for x in results if x.group == group):
            if r.error:
                print(f"{r.name:<26}  FAILED: {r.error[:80]}")
                continue
            per_op = r.lam.median_ns / r.ops
            go_cols = f"{'':>12}{'':>8}"
            if r.go is not None:
                go_per_op = r.go.median_ns / r.go.ops
                go_cols = f"{fmt_ns(go_per_op):>12}{r.lam.median_ns / r.go.median_ns:>7.2f}x"
            print(f"{r.name:<26}{r.ops:>10}{fmt_ns(r.lam.median_ns):>12}{fmt_ns(per_op):>12}{fmt_ns(r.lam.best_ns):>12}{r.lam.spread_pct:>7.1f}%{go_cols}")
    ratios = [r.lam.median_ns / r.go.median_ns for r in results if r.go is not None and not r.error]
    if ratios:
        geo = statistics.geometric_mean(ratios)
        print(f"\ngeometric mean lam/go over {len(ratios)} baselines: {geo:.2f}x")

    if args.json:
        doc = {
            "schema_version": 2,
            "kind": "runtime",
            "host": {"python": platform.python_version(), "machine": platform.machine(), "system": platform.system()},
            "runs": args.runs,
            "results": [
                {"group": r.group, "name": r.name, "ops": r.ops, "error": r.error,
                 "lam_ns": r.lam.runs_ns, "lam_median_ns": r.lam.median_ns if r.lam.runs_ns else None,
                 "go_ns": r.go.runs_ns if r.go else None,
                 "go_median_ns": r.go.median_ns if r.go else None}
                for r in results
            ],
        }
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(doc, indent=2))
    return 1 if any(r.error for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
