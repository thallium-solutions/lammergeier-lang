# Lammergeier benchmarks

Microbenchmarks that measure how long it takes to **compile** and
**run** representative ``.lam`` programs, plus the resulting binary
size. The suite is grouped by what the benchmark stresses:

- **`cases/language/`** — core language primitives: arithmetic,
  loops, lists, dicts, strings, method dispatch, f-string
  interpolation. These should move only when the transpiler itself
  changes.
- **`cases/stdlib/`** — stdlib hot paths: JSON encode/decode, regex
  matching, string splitting, hashing, sorting. These move when
  a library is retuned (e.g. pre-compiled regex cache,
  `strings.Builder` fast path).

## Running

```bash
# Full suite (cold compile, 3 runs each)
python3 tests/benchmarks/run_benchmarks.py

# Only fib / regex / anything matching a substring
python3 tests/benchmarks/run_benchmarks.py -f fib

# More samples for tighter stdev bars
python3 tests/benchmarks/run_benchmarks.py --runs 10

# Warm compile mode (keeps the library + parser cache)
python3 tests/benchmarks/run_benchmarks.py --warm

# Persist raw numbers for CI diffing
python3 tests/benchmarks/run_benchmarks.py --json out/bench.json
```

Cold compile is the default because it captures the full
preprocess → parse → transpile → `go build` chain; warm mode is
useful when you specifically want to isolate transpiler changes.

## What each benchmark reports

| Column          | Meaning                                            |
|-----------------|----------------------------------------------------|
| `compile(ms)`   | Wall time of `lamc --no-cache <file> -o <bin>`     |
| `run-best(ms)`  | Fastest of N runs of the compiled binary           |
| `run-mean(ms)`  | Mean of the N runs                                 |
| `σ(ms)`         | Sample stdev across the N runs                    |
| `size(KiB)`     | Compiled binary size                                |
| `LOC`           | Effective lines (blank + pure-comment stripped)    |

A final **summary** block aggregates the group totals and prints a
rough "lines/sec" compile throughput figure.

## Adding a new benchmark

1. Drop a `bench_<feature>.lam` file under `cases/language/` or
   `cases/stdlib/`. Name the file so a substring filter (`-f
   json`) picks it up.
2. Size the workload so one run is in the **10 – 200 ms** range on
   a modern laptop — short enough that a full suite finishes in
   under a minute, long enough that noise is under ±10%.
3. Assert your own result (e.g. `if total < 0 { print("unreachable") }`)
   so the Go compiler can't elide the loop as dead code.
4. Comment the benchmark explaining **what it stresses** and **which
   optimisation would move the needle** — this is what turns a
   benchmark into a tracking tool rather than a one-off script.
5. Run `python3 tests/benchmarks/run_benchmarks.py -f <your-name>`
   once to confirm it builds + runs, then again with `--runs 5` to
   confirm the variance is reasonable.

## Using benchmarks for regression tracking

The JSON output has a stable schema (`schema_version: 1`) so it
slots into a simple CI job:

```bash
python3 tests/benchmarks/run_benchmarks.py --json out/bench.json
python3 tools/bench_diff.py baseline.json out/bench.json  # future tool
```

Until `bench_diff.py` exists, the two best-of-3 numbers you care
about are `compile_ms` (changes when the compiler pipeline
changes) and `run_ms` (changes when either the transpiler or the
stdlib changes).
