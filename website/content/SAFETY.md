# Safety

What Lammergeier guarantees about the programs it accepts, what happens when
something goes wrong at run time, the deliberate limits of those guarantees,
and how the compiler is checked against them.

## The contract

1. **A program the checker accepts builds.** If `lamc` reports no error, the
   generated Go compiles. A Go build failure of generated code (not of a
   `go!` block) is a compiler bug: `lamc` reports it as an
   `internal compiler error: the generated Go did not build`, at the Lam line
   the Go error maps to, with a link to report it.
2. **The compiler never crashes on input.** Whatever the source (truncated,
   random bytes, invalid UTF-8, a BOM, CRLF or CR line ends, NUL bytes, very
   deep nesting, huge literals), `lamc` ends with a Lam diagnostic or an
   internal compiler error and a non-zero exit status, never a Python
   traceback. `LAMC_TRACEBACK=1` adds the Python traceback to an internal
   compiler error (for reports).
3. **Run-time faults are Lam exceptions.** A Go runtime fault (an index out
   of range, a division by zero, a failed conversion...) is raised as the
   Lam exception below, with the Lam line of the fault in its message:
   `try` / `catch` catches it like any exception (by its class or a base
   class: `LookupError` catches `IndexError`, `ArithmeticError` catches
   `ZeroDivisionError` and `OverflowError`). It reaches the `await` of an
   async task and the loop over a generator that raised it.
4. **An uncaught exception ends the program with a Lam report**: `panic:
   <Exception>: <message> (at file.lam:LINE)` then the Lam frames (`    at
   main.lam:7`), on stderr, exit status 2. `LAM_TRACEBACK=go` keeps Go's
   own panic report (goroutines, Go frames).

The checker also rejects, at compile time, what Go would reject in the
generated code: constant expressions that overflow (`x: int =
99999999999999999999`, `1 << 64`, `10 ** 30` as an `int`), negative constant
shift counts, duplicate constant keys in a dict literal, a program without
`func main()`, and literals of the wrong kind (`x: list[int] = [[]]`).

## Run-time exceptions

| Fault | Lam exception | Message |
|---|---|---|
| `xs[i]`, `s[i]`, slices out of range, `pop()` of an empty list | `IndexError` (a `LookupError`) | `index out of range [5] with length 3 (at main.lam:7)` |
| `/`, `//` or `%` by zero (ints and floats) | `ZeroDivisionError` (an `ArithmeticError`) | `division by zero`, `integer division or modulo by zero` |
| `int ** int` with a negative exponent | `ValueError` | `negative exponent in an int power (use a float base: 2.0 ** -1)` |
| `int(x)` of a NaN float | `ValueError` | `cannot convert float NaN to integer` |
| `int(x)` of an infinite float or one outside the int range | `OverflowError` (an `ArithmeticError`) | `float -1e+30 does not fit in an int` |
| `x as int` (or an annotated assignment from `any`) of another type | `TypeError` | `a str is not an int` |
| a member of `None` | `AttributeError` | `None has no attribute 'name'` |
| `None` converted to a value type | `TypeError` | `None is not a str` |
| writing to a `None` dict | `AttributeError` | `assignment to an entry of a None dict` |
| a negative shift count computed at run time | `ValueError` | `negative shift count` |
| a negative size for a list or a channel | `ValueError` | Go's message |
| an unhashable or uncomparable dict key | `TypeError` | Go's message |
| any other Go runtime error | `RuntimeError` | Go's message |

## Deliberate semantics (not faults)

These behave as documented and raise nothing:

- **Integers are 64-bit and wrap on overflow** (`int` is Go's `int64`, as in
  Go and Java): `9223372036854775807 + 1` is `-9223372036854775808`, and
  `3 ** 41` wraps too. Only constant expressions are checked (they overflow
  at compile time).
- **A missing key of a typed dict reads as the value type's zero**:
  `d["missing"]` on a `dict[str, int]` is `0` (`""` for strings, `false` for
  booleans, `None` for references). Test with `k in d` or use `d.get(k,
  default)` when the difference matters.
- **Floats are IEEE 754 doubles**: a zero divisor still raises
  `ZeroDivisionError` (never an infinity), but overflow gives `+Inf`
  (`1e308 * 10.0`) and `float("nan")` is NaN; converting those to `int`
  raises.

## Limits

- **`go!` blocks are the unsafe escape hatch.** Raw Go is not checked by Lam:
  a type error in it is the user's Go build error (reported as such, not as
  an internal compiler error), and a panic it raises is a Go panic (a Go
  runtime error is still turned into the Lam exception above when it
  propagates through Lam code). Keep `go!` to SDK and client construction and
  operations the standard library does not offer.
- **A stack overflow cannot be caught.** Unbounded recursion ends the program
  (exit status 2, Go's `stack overflow` report); Go treats it as fatal.
- **Concurrent writes to one dict from several async tasks are fatal** (Go's
  `concurrent map writes`, not recoverable). Share data between tasks
  through their results (`await`), a `Mutex` (`lamconcurrency`) or a
  `Channel`.
- **Running out of memory is fatal**, as in Go.

## How the contract is checked

- `tests/fuzz/lamfuzz.py`, a type-aware program fuzzer. Each case is
  deterministic by `(seed, index)`, and failures are shrunk and written to
  `--out`.
  - `--mode gen` generates well-typed programs with a Python *twin* that
    computes the expected output; the program must build and print exactly
    that.
  - `--mode mutate` derives near-valid programs: `lamc` must accept them (and
    then Go must build) or reject them with a Lam diagnostic.
  - `--mode bytes` feeds hostile inputs: a clean diagnostic or a build, never
    a traceback.

  ```bash
  sh scripts/fuzz.sh all 500                       # every mode, 500 cases (JOBS=, FUZZ_OUT=)
  python3 tests/fuzz/lamfuzz.py --mode gen --seed 1 --count 500 --jobs 8 --out /tmp/lamfuzz
  python3 tests/fuzz/lamfuzz.py --show 1042        # the Lam program of a seed and its Python twin
  ```

  The constructs whose Lam semantics differ from Python, and how the twin
  models them, are described at the top of `lamfuzz.py`.
- `tests/tests/test_compiler_crash_guard.py`: hostile inputs, internal
  compiler errors, Go build failures of generated code and of `go!` blocks.
- `tests/tests/test_runtime_panics.py` and `tests/tests/cases/safety/`: the
  exceptions above, uncaught reports, async tasks, generators.
- `tests/tests/test_doc_examples.py` (in the `unit` suite): every Lam example
  of `docs/` and `README.md` builds, and the examples that show their output
  (`# Output:`) print exactly that (CONTRIBUTING.md, "Documentation examples
  are tests").
- The race detector: `sh scripts/race.sh` (or `sh scripts/test.sh race`)
  builds a subset of the core cases with `lamc --go-race` and runs them (async
  tasks, generators, channels and locks, markup sharing, run-time faults, the
  stdlib modules with shared state, the HTTP client and server, `lamdb` on
  SQLite); a reported data race fails the test. `sh scripts/race.sh NAME`
  runs instead the core cases whose path contains `NAME` (`python3
  tests/tests/run_tests.py --race` all of them). It needs cgo and a C
  compiler.
- Canary builds: `sh scripts/canaries.sh` (or `sh scripts/test.sh canaries
  [project]`) builds, without running them, the downstream projects with the
  working-tree compiler: Luxyt (`cmd/*.lam`), managed-file-transfer
  (`main.lam`) and the test programs of every official `lam-*` package
  (against a copy of the local package checkouts). It is opt-in (minutes, and
  the checkouts are outside this repository: `LAM_CANARY_LUXYT`,
  `LAM_CANARY_MFT`, `LAM_CANARY_PACKAGES`; an absent one is skipped), and it
  checks that no build changes a project's `git status`.
- Every bug the fuzzer finds gets a minimal regression test (a semantic case
  or a `tests/tests/cases/safety/` program) before it is fixed.
