Contribution on Github is "disabled" for now. Since the project has just been released in beta, I still have to sort some things out.
I would love to hear any kind of suggestion anyway.

The following will be the way to go once we'll accept contributions:

# Contributing to Lammergeier

Welcome. Lammergeier is a typed, Python-flavoured language that
compiles to Go by transpiling each `.lam` file into a `.go` file
and handing the result to the Go toolchain. The project evolves
fast and accepts contributions from both humans and AI pair-
programmers, but the quality bar is the same for both.

This guide is the single source of truth for **how to work on the
codebase**. It covers the architecture, the development loop,
testing discipline, naming conventions, the stdlib authoring
playbook, and the pull-request checklist.

If you only read one thing, read the [**Development Workflow
Cheat-Sheet**](#development-workflow-cheat-sheet).

---

## Table of Contents

- [The Golden Rules](#the-golden-rules)
- [Repository Layout](#repository-layout)
- [Compiler Pipeline](#compiler-pipeline)
- [Development Workflow Cheat-Sheet](#development-workflow-cheat-sheet)
- [Full Development Workflow](#full-development-workflow)
- [Testing Discipline](#testing-discipline)
- [Running Tests](#running-tests)
- [Benchmarking](#benchmarking)
- [Writing Stdlib Modules](#writing-stdlib-modules)
- [Naming Conventions](#naming-conventions)
- [Grammar & Transpiler Changes](#grammar--transpiler-changes)
- [Error Messages](#error-messages)
- [Documentation Obligations](#documentation-obligations)
- [AI-Assisted Contributions](#ai-assisted-contributions)
- [Anti-Drift Rules](#anti-drift-rules)
- [Pull Request Checklist](#pull-request-checklist)

---

## The Golden Rules

1. **Correctness before cleverness.** A clear implementation that
   passes its tests beats a clever one that mostly works.
2. **If it's user-visible, it's documented.** `docs/SYNTAX.md` /
   `docs/stdlib.md` / `README.md` are part of the implementation.
3. **Every feature ships with tests.** New features: at least ten
   tests from trivial to composition. Bug fixes: a regression test
   that used to fail.
4. **Do not weaken tests to make a change compile.** Tests are
   contracts. If a test needs updating, say so explicitly in the
   PR description with a one-line justification.
5. **Keep the language and stdlib consistent.** Match existing
   patterns before inventing new ones. A stdlib method named
   `sortBy` forces another `sortOn` to look wrong.
6. **Real-world example or it doesn't exist.** Feature work ships
   with at least one realistic example program (even a short one)
   that would ring a user's "I'd actually write that" bell.
7. **Prefer the minimal diff.** Single-line upstream fixes beat
   cascading downstream workarounds.

---

## Repository Layout

```
lammergeier-lang/
├── compiler/                 ← the transpiler (Python)
│   ├── lammergeier.py        ← CLI entrypoint + compile pipeline
│   ├── preprocessor.py       ← go!-block extraction, LAMMERGEIER.* aliases
│   ├── semantic.py           ← pre-emission AST checks (undefined
│   │                           names, duplicates, unreachable…)
│   ├── transpiler.py         ← Lark-tree → Go-source driver
│   ├── visitors/             ← per-node emission helpers
│   │   ├── definitions.py    ← func / class / interface / import
│   │   ├── expressions.py    ← all expr-level lowerings
│   │   ├── statements.py     ← control flow, loops, match, try
│   │   └── helpers.py        ← shared utilities (type maps, etc.)
│   ├── cache.py              ← on-disk library transpile cache
│   ├── constants.py          ← centralised builtin / exception sets
│   └── lsp.py                ← language-server (stdio JSON-RPC)
│
├── lib/                      ← stdlib source (.lam files)
│   ├── lamjson.lam   lamxml.lam   lamyaml.lam  …
│   └── lamhttp.lam   lamserver*.lam              …
│
├── lammergeier.lark          ← Lark grammar (LALR)
│
├── tests/
│   ├── tests/                ← end-to-end "compile-and-run" tests
│   │   ├── run_tests.py
│   │   └── cases/            ← .lam cases with `# expect:` lines
│   ├── semantic/             ← semantic-checker diagnostics
│   │   ├── run_semantic_tests.py
│   │   └── cases/            ← `# expect-error:` / `# expect-pass`
│   ├── benchmarks/           ← compile + run + size measurements
│   │   ├── run_benchmarks.py
│   │   └── cases/language|stdlib/
│   ├── rosetta_tests/        ← larger realistic programs
│   └── transpilation/        ← Go-output snapshot comparisons
│
├── docs/                     ← language + stdlib docs
│   ├── SYNTAX.md             ←   language surface reference
│   ├── TRANSPILATION.md      ←   Lam → Go lowering notes
│   ├── stdlib.md             ←   stdlib reference
│   └── (more topic docs)
│
├── vs-code-extension/        ← VS Code / Windsurf / Cursor extension
│
├── README.md                 ← user-facing intro
├── CONTRIBUTING.md           ← this file
└── TODO.md                   ← planning / roadmap
```

---

## Compiler Pipeline

Every build goes through the same sequence. Knowing where your
change belongs is usually the hardest part of a PR.

```
                ┌─────────────────────────────────────────┐
 .lam source →  │ 1. Preprocessor                         │
                │    • LAMMERGEIER.* alias rewrite        │
                │    • go!-block extraction               │
                │    • doc-comment strip                  │
                │    • multiline-string collapse          │
                │    • auto-semicolons, empty-block fill  │
                └──────────────┬──────────────────────────┘
                               ▼
                ┌─────────────────────────────────────────┐
                │ 2. Lark parse (LALR)                    │
                │    • Parser pickled to disk (cached)    │
                └──────────────┬──────────────────────────┘
                               ▼
                ┌─────────────────────────────────────────┐
                │ 3. Semantic check (AST walk)            │
                │    • undefined names + did-you-mean     │
                │    • duplicate decls / shadowing        │
                │    • misplaced flow / unreachable code  │
                │    • Go-reserved identifier collision   │
                │    • const reassignment                 │
                └──────────────┬──────────────────────────┘
                               ▼
                ┌─────────────────────────────────────────┐
                │ 4. Transpile (tree → Go source)         │
                │    • definitions → statements → exprs   │
                │    • go! blocks spliced back verbatim   │
                │    • imports from lib/ sorted & deduped │
                │    • per-library cache keyed on sha256  │
                └──────────────┬──────────────────────────┘
                               ▼
                ┌─────────────────────────────────────────┐
                │ 5. `go build` inside tmpdir             │
                │    • `go.mod` synthesized on the fly    │
                │    • `go mod tidy` never pollutes repo  │
                └─────────────────────────────────────────┘
                               ▼
                          Binary output
```

| Stage | File(s) | Add a feature here when… |
|-------|---------|--------------------------|
| Preprocessor | `compiler/preprocessor.py`, `compiler/lammergeier.py` | You want a purely-textual transform (new shorthand, namespace alias, embedded block). |
| Grammar | `lammergeier.lark` | You're adding **new syntax** — any novel keyword, operator, or statement shape. |
| Semantic check | `compiler/semantic.py`, `tests/semantic/cases/` | You're catching a mistake that today would fail at `go build` with a confusing line number. |
| Transpilation | `compiler/visitors/*.py`, `compiler/transpiler.py` | You're changing how a Lam construct lowers to Go, or adding a new node. |
| Runtime / stdlib | `lib/*.lam` | You're exposing a new API to users; no compiler change needed. |
| Language server | `compiler/lsp.py` | You're changing diagnostics, hover, completion, or symbol extraction. |

---

## Development Workflow Cheat-Sheet

```bash
# 1. Make your change (grammar / semantic / transpiler / stdlib).

# 2. Iterate on the affected layer, running just those tests:
python3 tests/semantic/run_semantic_tests.py -f <keyword>
python3 tests/tests/run_tests.py -f <keyword>

# 3. Run the *full* suites before opening a PR:
python3 tests/tests/run_tests.py             # compile+run end-to-end
python3 tests/semantic/run_semantic_tests.py # semantic diagnostics

# 4. If the change might affect performance:
python3 tests/benchmarks/run_benchmarks.py --runs 5

# 5. Update docs touched by the change:
#    - docs/SYNTAX.md (language surface)
#    - docs/stdlib.md (new / changed stdlib API)
#    - README.md      (only for headline features)
#    - lib/<module>.lam top-of-file docstring

# 6. Open the PR using the Pull Request Checklist at the bottom.
```

---

## Full Development Workflow

### 1. Frame the change

Before writing code, write a one-paragraph description of **what**
and **why**. A typical description answers:

- What user-visible behaviour am I adding / changing / removing?
- What is the expected syntax / API?
- What are the non-goals?
- What existing tests or docs need updating?

A bug fix description additionally states:

- What is the current incorrect behaviour?
- What is the expected behaviour?
- A minimal reproducer (ideally already failing as a test).

### 2. Implement in the right layer

Use the table in [Compiler Pipeline](#compiler-pipeline) to pick
the correct file. **Resist the urge to add workarounds in a later
layer** — a malformed preprocessor output should be fixed in the
preprocessor, not papered over in the transpiler.

Keep diffs tight. If a change grows past ~300 lines of diff in a
single commit, split it.

### 3. Write or update tests

See [Testing Discipline](#testing-discipline). Every new feature
must ship with at least ten test cases. Every bug fix ships with
a regression test that would have caught the bug.

### 4. Run the affected suite while iterating

Short feedback loops save hours:

```bash
python3 tests/semantic/run_semantic_tests.py -f my_change
python3 tests/tests/run_tests.py -f my_change --verbose
```

### 5. Run the full suites before PR

Nothing ships without:

```bash
python3 tests/tests/run_tests.py
python3 tests/semantic/run_semantic_tests.py
```

If the change is performance-sensitive, also run benchmarks and
attach the before/after numbers to the PR description.

### 6. Update documentation

Documentation is part of the change, not an afterthought:

- `docs/SYNTAX.md` — any new/changed Lam surface.
- `docs/stdlib.md` — any new/changed stdlib module or method.
- Module docstring at the top of `lib/<module>.lam` — always.
- `README.md` — only when adding a headline feature users should
  know about.
- `docs/TRANSPILATION.md` — when you change how a construct lowers.

### 7. Final consistency pass

Walk the diff one more time and ask:

- Is my naming consistent with the existing stdlib (camelCase)?
- Does the diff accidentally touch unrelated files?
- Did I leave `print()` calls, commented-out code, or TODOs?
- Does the PR description match what the diff actually does?

---

## Testing Discipline

Lammergeier ships with **five** test families. Know them; use the
right one.

| Suite | Location | Purpose |
|-------|----------|---------|
| End-to-end | `tests/tests/` | Compile + run with `# expect:` stdout assertions. The primary regression shield. |
| Semantic | `tests/semantic/` | Compile-time diagnostics (`# expect-error:`, `# expect-pass`). |
| Transpilation | `tests/transpilation/` | Snapshot-compare the emitted Go (for lowering changes). |
| Rosetta | `tests/rosetta_tests/` | Realistic end-to-end programs proving day-to-day composition. |
| Benchmarks | `tests/benchmarks/` | Compile + run + binary-size stats. Not a pass/fail suite. |

### End-to-end tests

Shape:

```lammergeier
# expect: hello alice
# expect: count=3

func main() {
    greet("alice")
    print("count=3")
}
```

Every `# expect:` line must appear in the program's stdout in
**order**. Use this suite for anything that exercises the whole
pipeline (the overwhelming majority of features).

### Semantic tests

Shape:

```lammergeier
# expect-error: undefined name `mystery`
# expect-error: did you mean `mystery_val`?

func main() {
    print(mystery)
}
```

Or:

```lammergeier
# expect-pass
# This construct is legal; make sure the semantic check doesn't
# reject it.
```

Use this suite to lock in:

- Error-message wording (we assert substrings, so small refactors
  to the message don't need to touch every test).
- Rejection of mistakes that today fail at `go build`.
- Acceptance of legal constructs that look similar to rejected ones.

### Writing a good test set for a new feature

At least **ten** cases, ordered from simplest to most composed:

1. Minimal valid usage.
2. Expected usage in a realistic shape.
3. Syntax variations (whitespace, braces, trailing commas, single-line).
4. Composition with existing features (`if`, loops, classes, generics).
5. Boundary cases (empty, zero, single-element).
6. Realistic example (a small snippet a user would actually write).
7. Invalid syntax → expected parser error.
8. Invalid semantics → expected semantic diagnostic.
9. Regression case for any bug discovered during implementation.
10. Nested / composed stress case.

### What NOT to do

- **Don't delete or weaken an existing test** to make your change
  pass. If a test needs updating, explain why in the PR.
- **Don't rely on wall-clock time** except in the benchmark suite.
- **Don't assume a specific stderr format** beyond the substrings
  the semantic runner checks.
- **Don't leave `print(...)` debug output** in test cases unless
  it's an `# expect:` line.

---

## Running Tests

```bash
# Full end-to-end pipeline (277+ cases today)
python3 tests/tests/run_tests.py

# Filter by substring (case-insensitive)
python3 tests/tests/run_tests.py -f xml

# Verbose output for the failures
python3 tests/tests/run_tests.py -v

# Semantic diagnostics suite
python3 tests/semantic/run_semantic_tests.py

# One specific semantic case
python3 tests/semantic/run_semantic_tests.py -f shadow

# Benchmarks
python3 tests/benchmarks/run_benchmarks.py
python3 tests/benchmarks/run_benchmarks.py --warm
python3 tests/benchmarks/run_benchmarks.py --json out/bench.json
```

The tests assume `python3 -m compiler.lammergeier <file>` works
from the repo root. If your Python environment doesn't have Lark
on the default path, export `PYTHONPATH` to include the install
location of `lark-parser`.

---

## Benchmarking

Benchmarks live in `tests/benchmarks/` and are split by **what they
stress**:

- `cases/language/` — core language primitives (loops, lists,
  dicts, strings, method dispatch, f-strings).
- `cases/stdlib/` — stdlib hot paths (JSON, regex, sort, hash,
  string split).

### When to run benchmarks

- Before + after any change to `compiler/transpiler.py`,
  `compiler/visitors/*.py`, or a hot-path stdlib module
  (`lamjson`, `lamre`, `lamhttp`, `lamstrings`, …).
- When optimising an f-string lowering, string-builder fast path,
  map-iteration change — anything that would measurably move a
  microbenchmark.

### Reading the numbers

Three columns matter:

- `compile(ms)` — `lamc --no-cache` wall time. Moves when the
  compiler pipeline changes.
- `run-best(ms)` — fastest of N runs. Moves when lowering or the
  stdlib changes.
- `size(KiB)` — binary size. Moves when imports or lowering
  change, usually in small increments.

`σ(ms)` above ~10% of the mean is a signal that the workload is
too noisy and probably needs a bigger N (the innermost loop
counter inside the benchmark).

### Adding a benchmark

See `tests/benchmarks/README.md` for the full checklist. The short
version:

1. Drop `bench_<feature>.lam` under the right group directory.
2. Size the workload so one run takes 10 – 200 ms.
3. Assert the result so Go can't dead-code-eliminate the loop.
4. Comment **what it stresses** and **which change would move it**.

---

## Writing Stdlib Modules

Stdlib modules live in `lib/` as `lam*.lam` files. They're just
regular Lam files that happen to be auto-importable via
`from lam<name> import ...`.

### Mandatory layout

Every stdlib file starts with:

```lammergeier
# Lammergeier standard library: <one-line description>.
#
# <2–4 paragraphs explaining:
#   - what the module does
#   - which Go packages it wraps
#   - the shape of the public API
#   - any non-obvious design choice>
#
# Import with: ``from lam<name> import <ClassA>, <ClassB>``.

from lamerrors import Result, Error   # if you return Result

go! {
    import (
        "encoding/<stdlib>"
        // third-party deps go here
    )
}

class <ClassName> {
    #- <doc comment for the class> -#
    …
}
```

### API conventions

- **Classes are nouns.** `Xml`, `HttpClient`, `TokenBucket`.
- **Methods are verbs.** `encode`, `tryDecode`, `setHeader`, `wait`.
- **`try*` variants return `Result`.** The non-`try` flavour must
  either panic on error or swallow to a sentinel (`None`, `""`,
  `-1` — document which). Rule of thumb: if the error is a user-
  input validity question, expose a `try*` variant.
- **Static-only helpers go on a class named after the capability**
  (`Json.encode(...)`, `Xml.parse(...)`). Instance classes expose
  both state and behaviour (`HttpClient`, `JwtKeySet`).
- **`Result.Ok` / `Result.Err` / `Error` are spelt
  `LAMMERGEIER.Result.Ok`, `.Err`, `.Error`** inside stdlib `go!`
  blocks. The preprocessor lowers the alias to the Go-side symbol;
  see `docs/SYNTAX.md`. **Don't reach for the raw `Result_Ok` /
  `NewError` names.**

### Writing the Go-side

A method usually looks like this:

```lammergeier
class Xml {
    static func encode(value: any, root: str = "root") -> str {
        #- Marshal a Lam-shaped value to XML wrapped in ``<root>``. -#
        out: str = ""
        rootName: str = root
        go! {
            var sb strings.Builder
            lamXmlEncodeValue(rootName, value, &sb)
            out = sb.String()
        }
        return out
    }
}
```

Rules that save you debugging time:

- **Capture Lam locals into Go-visible names.** Assign to a local
  at Lam level (`out: str = ""`) and mutate it inside the `go!`
  block. Returning directly from a `go!` block works but loses
  Lam-level typechecking of the return value.
- **Don't use `break` / `continue` inside a `go!` block.** They
  don't cross the block boundary the way you'd expect; structure
  the Go loop with a sentinel variable instead.
- **Never emit `go!` text that depends on Lam-side braces.** The
  extraction uses naïve brace matching. Strings with `{` or `}`
  need to avoid appearing in source at the top of a line or the
  extractor gets confused.
- **Void inner closures: use `if/else`, not bare `return`.** The
  auto-padding pass adds a default-value return based on the
  *enclosing Lam method*, which is wrong inside a void-returning
  Go closure. Structure branches instead.

### Tests for a new module

Add `tests/tests/cases/stdlib/test_stdlib_<module>.lam`. Cover:

1. Happy-path encode / decode / round-trip.
2. Every public class's primary methods.
3. Error paths via `try*` variants.
4. Composition with another stdlib module (often `Result`).
5. At least one "user-shaped" example (parse a realistic payload,
   not just `{"a": 1}`).

### Documentation

- Top-of-file comment with description + example (already covered
  by the mandatory layout).
- Entry in `docs/stdlib.md` under **Quick reference** and a short
  code block in the **New modules** section for headline additions.
- No `README.md` entry unless the addition is headline-worthy.

---

## Naming Conventions

| Target | Convention | Example |
|--------|------------|---------|
| Stdlib classes | `PascalCase` | `HttpClient`, `XmlNode`, `TokenBucket` |
| Stdlib methods / fields | `camelCase` | `setHeader`, `tryDecode`, `retryAfter` |
| User-code Lam identifiers | `camelCase` (recommended) | `userName`, `computeScore` |
| Lam parameters | `camelCase`, no underscore prefix | `func f(pageSize: int, pageToken: str)` |
| Go-side helper funcs inside `go!` blocks | `lowerCamelCase` with `lam` prefix for internals | `lamXmlEncodeValue`, `lamHttpDoVerb` |
| Grammar rules | `snake_case` | `funcdef`, `typed_parameters` |
| Python compiler internals | `snake_case` | `_check_go_reserved`, `apply_lammergeier_aliases` |
| Test files | `snake_case` + `test_` prefix | `test_stdlib_xml.lam`, `bench_fib.lam` |
| `LAMMERGEIER.*` aliases | Matches Python `LAMMERGEIER_ALIASES` keys exactly | `LAMMERGEIER.Result.Ok` |

**Do not introduce new casing conventions.** If in doubt, open a
draft PR and ask — rolling back a rename is expensive.

---

## Grammar & Transpiler Changes

Grammar changes are the highest-risk edits. Before touching
`lammergeier.lark`:

1. Write the **target Lam syntax** you want, ideally as a test case
   (it'll fail to parse first — that's OK).
2. Add the minimum new rule. Prefer extending an existing rule
   over inventing a parallel one.
3. Rebuild the parser cache once: `python3 -m compiler.lammergeier
   --clear-cache`.
4. Run parser-only smoke checks against a few existing files:

   ```bash
   PYTHONPATH=... python3 -c "
   from compiler.lammergeier import create_parser
   p = create_parser()
   for f in ['lib/lamjson.lam', 'lib/lamhttp.lam']:
       p.parse(open(f).read())
   print('ok')
   "
   ```

5. Add a visitor in the right `compiler/visitors/*.py` file. The
   visitor name must match the new tree node's `data` string.
6. Run `tests/tests/run_tests.py` — LALR ambiguities usually
   surface as a test that *used to* parse now failing.

### Transpiler changes

- Emission uses string concatenation into a `StringIO`-like
  buffer. Keep lines short and prefer small helpers over big
  switch statements.
- When your emission could produce wrong Go, emit `//line
  <path>:<n>` directives so the Go compiler's errors still point
  at `.lam` lines.
- Anything you emit that isn't trivially a Go expression should be
  checked in `tests/transpilation/` with a snapshot comparison.

---

## Error Messages

Good error messages are what turn a transpiler into a language
people enjoy using. The existing error families set the bar:

- **Three-line source snippet** around the offending line, with a
  `>>>` marker on the reported line.
- **Backticked symbols** in the message body so editors can
  underline them.
- **Did-you-mean suggestion** when there's a close match in scope.
- **No Python traceback** unless the compiler genuinely crashed —
  user mistakes must not surface as uncaught exceptions.

See `compiler/semantic.py`'s `SemanticError.format` and
`find_unknown_lammergeier_aliases` in `compiler/preprocessor.py`
for reference formatters to copy.

### When adding a diagnostic

1. Decide whether it belongs in the semantic checker
   (`compiler/semantic.py`) or the preprocessor
   (`compiler/preprocessor.py`). Semantic = AST-shape problem;
   preprocessor = purely textual problem.
2. Add a new `kind` string (e.g. `"shadow"`, `"unreachable"`) so
   future filters / IDE categorisations can group diagnostics.
3. Write the positive case **and** the negative case in
   `tests/semantic/cases/`.
4. Check that the LSP surfaces it — `compiler/lsp.py` already
   forwards semantic-checker errors, so usually no code change is
   needed, but confirm with a hand-run of the LSP.

---

## Documentation Obligations

A change is incomplete if one of these is out of sync. Reviewers
will bounce PRs that don't match.

| You changed… | Then you update… |
|--------------|------------------|
| Lam syntax | `docs/SYNTAX.md` |
| Compiler pipeline layer | `docs/TRANSPILATION.md` (when lowering changed) |
| Stdlib module | Top-of-file docstring + `docs/stdlib.md` |
| User-facing feature | `README.md` (only for headline changes) |
| Benchmark harness | `tests/benchmarks/README.md` |
| Test format | The README of the suite you touched |
| VS Code grammar | `vs-code-extension/.../package.json` bump + README note |

---

## AI-Assisted Contributions

AI pair-programmers are welcome. They must follow the same
workflow as a human contributor — with two extra rules:

1. **Every AI-drafted diff must be read, reasoned about, and
   tested before commit.** Treat AI output as a senior-engineer
   colleague's *draft*, not as the final artefact.
2. **No "looks right" merges.** If you can't explain every line
   of the diff, you're not ready to land it. Roll it back, ask the
   assistant for a narrower edit, or switch to a manual
   implementation.

Good AI prompts for this codebase tend to:

- Quote the exact file + line range being modified.
- Quote the expected shape of a test.
- Specify "minimal-diff" explicitly.
- Ask for reasoning about edge cases (empty input, unicode,
  error paths) before the edit.

---

## Anti-Drift Rules

These are the mistakes that make a language feel patchy. Don't:

- Invent syntax without documenting it the same PR.
- Add behaviour without at least one positive + one negative test.
- Introduce stdlib names that break the camelCase / PascalCase
  rules above.
- Leave documentation behind the implementation.
- Make architectural changes (new pipeline stage, new cache,
  parallel variant of an existing file) without a one-paragraph
  justification in the PR.
- Ship "it works but the error message is bad" — the error
  message IS part of the feature.

When uncertain, prefer:

- Simpler syntax over cleverer syntax.
- A smaller change that's correct over a bigger one that's fast.
- Clearer documentation over more documentation.
- Stronger tests over more tests.

---

## Pull Request Checklist

Before opening the PR:

- [ ] The change has a one-paragraph framing (what / why / non-goals).
- [ ] The implementation is in the correct pipeline stage.
- [ ] Grammar changes (if any) come with parser smoke-tests.
- [ ] Semantic / transpiler / runtime changes each come with tests.
- [ ] New features ship with ≥10 tests from simple to composed.
- [ ] Bug fixes ship with a regression test that previously failed.
- [ ] Stdlib additions follow the camelCase + `LAMMERGEIER.*` rules.
- [ ] `python3 tests/tests/run_tests.py` is green.
- [ ] `python3 tests/semantic/run_semantic_tests.py` is green.
- [ ] For performance-sensitive changes, benchmarks run clean and
      before/after numbers are in the PR description.
- [ ] `docs/SYNTAX.md` / `docs/stdlib.md` / module docstrings updated.
- [ ] `README.md` updated only if the change is headline-worthy.
- [ ] No unrelated files touched.
- [ ] No commented-out code or stray `print(...)` debugging.
- [ ] Error messages have a three-line source snippet + backticked
      symbols + (when applicable) a did-you-mean suggestion.

---

## Suggested PR description template

```
## Summary
<one-paragraph what + why>

## Implementation notes
<which pipeline stage(s) changed and why>

## Tests
<which suites were updated, which cases were added>

## Docs
<docs/SYNTAX.md / docs/stdlib.md / README.md sections touched>

## Performance
<benchmark before/after, only for perf-sensitive changes>

## Non-goals
<what this PR deliberately does NOT do>
```

---

## Final Note

Lammergeier grows intentionally. Every contribution should improve
not just the codebase but the clarity, reliability, and usability
of the language itself. That's the only rule you really need to
remember.

Thanks for helping build it well.
