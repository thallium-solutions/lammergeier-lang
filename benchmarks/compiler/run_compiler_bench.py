#!/usr/bin/env python3
"""Compiler benchmarks: how fast is ``lamc``?

Compiles a corpus with ``lamc build --timings`` and reports the median time
of every phase (preprocess, parse, semantic, transpile, go mod, go build)
plus front-end throughput in lines per second. The corpus is:

* generated programs of three sizes (``gen_program.py``: ~350, ~3,500 and
  ~14,000 lines), the same on every machine;
* the hand-picked programs in ``corpus/``;
* any project given with ``--project main.lam`` (with ``--extlibs`` for
  its libraries), e.g. a real application.

Modes: ``cold`` (``--no-cache``: libraries transpiled again) and ``warm``
(after a priming build, the library cache is used). ``--front-end`` stops
after transpiling (no ``go build``), which isolates the Python compiler.

Usage::

    python3 benchmarks/compiler/run_compiler_bench.py
    python3 benchmarks/compiler/run_compiler_bench.py --front-end --runs 5
    python3 benchmarks/compiler/run_compiler_bench.py --project ../app/main.lam \\
        --extlibs ../app/extlibs --extlibs ../app/lib --json out/compiler.json
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]
SIZES = {"small": 5, "medium": 50, "large": 200}
PHASES = ["preprocess", "parse", "semantic", "transpile", "go_mod", "go_build", "total"]
COLUMNS = ["preprocess", "parse", "semantic", "transpile", "go_mod", "go_build", "other", "total"]
FRONT_END = ["preprocess", "parse", "semantic", "transpile"]


def effective_lines(path: Path) -> int:
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            count += 1
    return count


def compile_once(source: Path, out: Path, extlibs: list[str], cold: bool, front_end: bool) -> dict:
    cmd = [*LAMC, str(source), "-o", str(out), "--timings"]
    if cold:
        cmd.append("--no-cache")
    if front_end:
        cmd.append("--transpile-only")
    for d in extlibs:
        cmd += ["--extlibs", d]
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=source.parent)
    match = re.search(r"^LAMC_TIMINGS (\{.*\})$", proc.stderr, re.M)
    if proc.returncode != 0 or not match:
        tail = [l for l in proc.stderr.splitlines() if l.strip() and not l.startswith("LAMC_TIMINGS")][-3:]
        raise RuntimeError(" | ".join(tail) or "compilation failed")
    return json.loads(match.group(1))["phases_ms"]


def bench(name: str, source: Path, extlibs: list[str], runs: int, cold: bool, front_end: bool, tmp: Path) -> dict:
    out = tmp / f"{name}.bin"
    if not cold:
        compile_once(source, out, extlibs, False, front_end)  # prime the cache
    samples = [compile_once(source, out, extlibs, cold, front_end) for _ in range(runs)]
    medians = {p: statistics.median(s.get(p, 0.0) for s in samples) for p in PHASES}
    # Time not attributed to a phase (module resolution, namespacing, I/O).
    medians["other"] = max(0.0, medians["total"] - sum(medians[p] for p in PHASES if p != "total"))
    lines = effective_lines(source)
    front = sum(medians[p] for p in FRONT_END)
    return {"name": name, "lines": lines, "phases_ms": medians, "front_end_ms": front,
            "lines_per_s": (lines / (front / 1000.0)) if front > 0 else 0.0}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=3, help="builds per program (default 3)")
    ap.add_argument("--mode", choices=["cold", "warm", "both"], default="both")
    ap.add_argument("--front-end", action="store_true", help="stop after transpiling (no go build)")
    ap.add_argument("--project", action="append", default=[], help="extra program to compile (repeatable)")
    ap.add_argument("--extlibs", action="append", default=[], help="extlibs directory for --project programs")
    ap.add_argument("-f", "--filter", default="", help="only programs whose name contains this text")
    ap.add_argument("--json", help="write the results to this file")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory(prefix="lam-compiler-bench-") as tmp_name:
        tmp = Path(tmp_name)
        corpus: list[tuple[str, Path, list[str]]] = []
        sys.path.insert(0, str(HERE))
        from gen_program import generate
        for label, units in SIZES.items():
            path = tmp / f"generated_{label}.lam"
            path.write_text(generate(units))
            corpus.append((f"generated-{label}", path, []))
        for path in sorted((HERE / "corpus").glob("*.lam")):
            corpus.append((path.stem, path, []))
        for project in args.project:
            p = Path(project).resolve()
            corpus.append((p.parent.name + "/" + p.name, p, [str(Path(d).resolve()) for d in args.extlibs]))
        corpus = [c for c in corpus if args.filter.lower() in c[0].lower()]

        modes = ["cold", "warm"] if args.mode == "both" else [args.mode]
        results: list[dict] = []
        for mode in modes:
            for name, path, extlibs in corpus:
                try:
                    r = bench(name, path, extlibs, args.runs, mode == "cold", args.front_end, tmp)
                except RuntimeError as exc:
                    r = {"name": name, "error": str(exc)}
                r["mode"] = mode
                results.append(r)
                print(f"  {mode:<5} {name}", file=sys.stderr)

    shown = [p for p in COLUMNS if not (args.front_end and p in ("go_mod", "go_build"))]
    header = f"{'program':<28}{'mode':<6}{'lines':>7}" + "".join(f"{p:>11}" for p in shown) + f"{'lines/s':>10}"
    print()
    print("median milliseconds per phase (front-end = preprocess + parse + semantic + transpile)")
    print(header)
    print("-" * len(header))
    for r in results:
        if "error" in r:
            print(f"{r['name']:<28}{r['mode']:<6}  FAILED: {r['error'][:90]}")
            continue
        cells = "".join(f"{r['phases_ms'][p]:>11.1f}" for p in shown)
        print(f"{r['name']:<28}{r['mode']:<6}{r['lines']:>7}{cells}{r['lines_per_s']:>10.0f}")
    if args.json:
        doc = {"schema_version": 1, "kind": "compiler", "front_end_only": args.front_end, "runs": args.runs,
               "host": {"python": platform.python_version(), "machine": platform.machine(), "system": platform.system()},
               "results": results}
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(doc, indent=2))
    return 1 if any("error" in r for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
