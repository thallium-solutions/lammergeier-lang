<p align="center">
  <img src="assets/images/logo.png" alt="Lammergeier Lang" width="240">
</p>

<h1 align="center">Lammergeier Lang</h1>

<p align="center">
  <em>A typed, Python-flavoured programming language that compiles to Go.</em>
</p>

<p align="center">
  <a href="#/docs/syntax">Syntax</a> ·
  <a href="#/docs/stdlib">Stdlib</a> ·
  <a href="#/docs/installation">Install</a> ·
  <a href="#/docs/package_manager">Package manager</a> ·
  <a href="#/docs/transpilation">Transpilation</a> ·
  <a href="#/docs/contributing">Contributing</a>
</p>

---

## Why Lammergeier

Lammergeier aims for the readability of Python with the performance and
tooling of Go. You write `.lam` source files using type annotations,
classes, comprehensions, pattern matching, generators, async/await,
f-strings, and more; the compiler turns them into idiomatic Go code and
then hands that Go off to `go build`.

```lammergeier
from lammath import Math

class Circle {
    func __init__(self, radius: float) {
        self.radius: float = radius
    }

    func area(self) -> float {
        return Math.pi() * self.radius * self.radius
    }
}

func main() {
    shapes: list[Circle] = [Circle(1.0), Circle(2.5), Circle(5.0)]
    for c in shapes {
        print(f"area: {c.area()}")
    }
}
```

```
$ ./lamc circle.lam --run
area: 3.141592653589793
area: 19.634954084936208
area: 78.53981633974483
```

## Features at a glance

- **Typed & inferred** — mandatory type annotations for fields and
  parameters, inference for locals.
- **OOP done right** — classes, constructors, `self`, inheritance, static
  and private members, operator overloading, interfaces.
- **Python-style niceties** — f-strings, list/dict comprehensions,
  unpacking, `for … else` / `while … else`, `match`/`case`,
  generators with `yield`.
- **Go-powered runtime** — compiles to portable native binaries via the
  Go toolchain; no VM, no GC surprises.
- **Raw Go escape hatch** — drop into plain Go with `go! { ... }` or
  `go!(expr)` when you need stdlib access the language does not wrap.
- **OOP standard library** — static classes like `Math`, `Strings`,
  `Json`, `Path`, `Random`, `Http`, `Db`, `Hash`, `Stats`, `Compress`,
  `Net`, `Cli`, `Test`, plus data structures (`Stack`, `Queue`, `Set`,
  `Deque`, `Heap`, `PriorityHeap`) and concurrency primitives (`Channel`,
  `Mutex`, `RWMutex`, `WaitGroup`, `Atomic`).
- **Numerics & web stack** — `Array` / `Matrix` backed by **gonum**
  (BLAS-accelerated matmul, LU solve, inverse, det, trace, SVD-flavoured
  helpers), pandas-style `DataFrame` / `Series` / `DataFrameGroups`
  backed by **go-gota** (CSV/JSON I/O, `selectCols` / `filter*` /
  `sort` / `innerJoin` / `groupBy` / `aggregate`, full Series
  statistics), a Fastify-style `Server` with route params, lifecycle
  hooks and a plug-in mechanism, a lazy `Iter` combinator suite, an
  `LruCache` + `TtlCache` pair, and RFC 4122 v4/v7 `Uuid` generation.
- **Editor support** — built-in **LSP server** (`bin/lammergeier-lsp`)
  with diagnostics, hover, completion, goto-definition and document
  outline, plus a VS Code / Cursor / Windsurf extension.
- **Batteries for real scripts** — HTTP client *and* blocking server,
  TCP sockets, DNS, gzip/zlib, HMAC + constant-time comparison,
  cryptographically-secure random tokens, a CLI flag parser, and a
  `Test` framework with `describe` / `assert*` / `summary`.

See [`docs/SYNTAX.md`](#/docs/syntax) for the complete language reference, and
[`docs/TRANSPILATION.md`](#/docs/transpilation) for the authoritative Lam → Go
mapping used by the compiler.

## Requirements

- **Python 3.10+**
- **Go 1.21+**
- **`lark` parser** — `pip install lark`

## Quick start

```bash
git clone https://github.com/thallium-solutions/lammergeier-lang.git
cd lammergeier-lang

# One-shot install: lamc + LSP + (optionally) the editor extension.
./install.sh                            # auto: system-wide if writable, else ~/.local/bin
./install.sh --with-editor all          # also wire up VS Code / Cursor / Windsurf

lamc examples/hello.lam --run           # now on PATH

# Emit the generated Go for inspection:
lamc examples/hello.lam --emit-go

# Produce a binary at a custom path:
lamc examples/hello.lam -o hello
./hello
```

`install.sh` is symlink-based, so the compiler keeps reading `lib/`,
`compiler/`, and `lammergeier.lark` out of the checkout — `git pull`
to upgrade, no re-install needed. Pass `--user`, `--system`,
`--prefix DIR`, or `--dry-run` to control where the symlinks land.

**Full installer reference** (every flag, editor extension wiring,
verifying the install, uninstalling): [`docs/installation.md`](#/docs/installation).

### Compile-time flags worth knowing

| Flag                 | Description                                                  |
|----------------------|--------------------------------------------------------------|
| `--run`              | Compile and immediately execute the program.                 |
| `-o PATH`            | Output path for the compiled binary. Defaults to the source file's path minus `.lam`, so `lamc src/foo.lam` writes `src/foo` (no CWD pollution). |
| `--emit-go`          | Print the generated Go source and exit.                      |
| `--emit-ast`         | Print the parsed Lark AST and exit.                          |
| `--go-ldflags FLAGS` | Forward `FLAGS` to `go build`.                               |

### Package manager

```bash
lamc install                             # read lamlib.toml, install everything
lamc install lamwebp@1.2.0              # add to ./extlibs + lamlib.toml + lockfile
lamc install ./local-checkout           # local path (great for in-dev libs)
lamc install --frozen --offline          # CI / Docker: lockfile is law, no network
lamc install --global lamwebp           # one-off install into ~/.lammergeier/extlibs
lamc uninstall lamwebp
lamc publish ./mylib                     # POST to a registry; auth via $LAMC_TOKEN
```

The installer resolves transitive `[dependencies]`, merges
`[go-deps]` Go-module pins via MVS, and refuses cross-library
version conflicts before touching disk. A SemVer / API-diff
gate (`compiler/apidiff.py`) rejects upgrades whose bump lies
about the actual change (override with `--allow-breaking`).

The reference registry ships under
[`tools/registry/`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/tools/registry/) as a Docker image:

```bash
docker compose -f tools/registry/docker-compose.yml up
lamc install --registry http://localhost:8765 lamgreet
```

**Full package-manager reference** (CLI flags, lockfile schema,
conflict detection, environment variables, cookbook):
[`docs/package_manager.md`](#/docs/package_manager).
**Authoring a library** (manifest format, scoped names, registry
protocol): [`docs/third_party_libraries.md`](#/docs/third_party).

## Project layout

```
lammergeier-lang/
├── lamc                          # CLI launcher
├── lammergeier.lark              # Lark grammar
├── docs/                          # language + stdlib docs (SYNTAX.md,
│                                 #   TRANSPILATION.md, stdlib.md, …)
├── CONTRIBUTING.md               # contribution process
├── compiler/                     # preprocessor, parser, Go transpiler
│   ├── lammergeier.py
│   ├── transpiler.py
│   ├── preprocessor.py
│   └── visitors/                 # expressions, statements, definitions, helpers
├── lib/                          # Lammergeier standard library (.lam files)
├── tests/                        # language, rosetta and transpilation suites
│   ├── tests/                    # focused language tests
│   ├── rosetta_tests/            # larger end-to-end programs
│   └── transpilation/            # Lam → Go output regression tests
├── vs-code-extension/            # VS Code / Windsurf / Cursor extension
├── website/                      # static documentation site (SPA with
│                                 #   client-side search, deploys straight
│                                 #   to GitHub Pages / Netlify / any CDN)
└── images/                       # logo and assets
```

The static site under [`website/`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/website) renders the same docs
with client-side search and lammergeier-themed visuals. Build the
content folder from the authoritative Markdown with
`./website/build.sh` and preview with
`python3 -m http.server --directory website 8765`. See
[`website/README.md`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/website/README.md) for deployment notes.

## Standard library

The stdlib lives in [`lib/`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/lib) and is imported exactly like any user
module:

```lammergeier
from lamstrings import Strings
from lamfmt import Fmt
from lamstack import Stack

func main() {
    s: Stack = Stack()
    s.push(10)
    s.push(20)
    print(Fmt.sprintf("top = %d (size=%d)", s.peek(), s.size()))
    print(Strings.toUpper("lammergeier"))
}
```

- A **comprehensive reference** for every stdlib module — classes,
  methods, examples — lives in [`docs/stdlib.md`](#/docs/stdlib).
- A **plugin-authoring guide** for the `lamserver` HTTP toolkit
  (lifecycle hooks, state via `req.ctx`, route prefixes,
  testing with `Server.inject`, recipes) is in
  [`docs/server_plugins.md`](#/docs/server_plugins).
- A one-line-per-module inventory is in the
  [Standard Library section of `docs/SYNTAX.md`](#/docs/syntax?h=standard-library).

## Running the tests

The language ships with four complementary suites:

```bash
# 200+ focused language + stdlib tests
python3 tests/tests/run_tests.py

# ~50 longer "rosetta" programs that exercise realistic idioms
python3 tests/rosetta_tests/run_rosetta.py

# Transpilation-output tests: assert the Go the compiler emits
python3 tests/transpilation/run_transpilation_tests.py

# Semantic-check tests: assert pre-emission errors fire where expected
python3 tests/semantic/run_semantic_tests.py

# LSP server (JSON-RPC over stdio): smoke-tested with a black-box harness
python3 tests/lsp/test_lsp.py

# Filter any suite by filename substring:
python3 tests/tests/run_tests.py --filter fstring
```

The first two runners compile each `.lam` file with the in-tree `lamc`,
execute the resulting binary, and compare stdout against `# expect:`
lines embedded in the test source. The transpilation runner compares
the output of `lamc --emit-go` against `# expect-go:` substrings — this
is the canary for Lam → Go mapping regressions described in
[`docs/TRANSPILATION.md`](#/docs/transpilation). The semantic runner verifies
that `lamc`'s pre-emission checker catches undefined names, duplicate
class members, and misplaced flow statements via `# expect-error:` /
`# expect-pass` headers.

## Editor support

### VS Code / Windsurf / Cursor extension

A ready-to-install extension with syntax highlighting **and a built-in
LSP client** lives in
[`vs-code-extension/`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/vs-code-extension/lammergeier-lang/README.md).
The shortest path is:

```bash
# 1. Build the LSP-client TS bundle (once).
(cd vs-code-extension/lammergeier-lang && npm install && npm run build)

# 2. Symlink into every editor we can find.
./vs-code-extension/install.sh
```

The extension contributes embedded Go highlighting inside `go! { ... }`
blocks, dedicated scopes for stdlib modules and built-in classes,
proper `#- ... -#` block-comment toggling, and these settings:

- `lammergeier.lsp.path` — path to `bin/lammergeier-lsp` (PATH-resolved
  if relative; supports `~` and `${workspaceFolder}`).
- `lammergeier.lsp.enabled` — master switch.
- `lammergeier.lsp.logFile` — set to capture a verbose JSON-RPC trace.
- `lammergeier.trace.server` — `off` / `messages` / `verbose`.

Use the **Lammergeier: Restart Language Server** command after
upgrading the compiler.

### Language Server (LSP)

`compiler/lsp.py` is a self-contained Language Server that speaks
JSON-RPC 2.0 over stdio per LSP v3.17. Capabilities:

- **Diagnostics** — parse-error squiggles published on every
  `didChange`, with line/column from Lark.
- **Hover** — function and class signatures (`func helper(x: int) -> int`).
- **Completion** — top-level functions / classes plus method completion
  after `Foo.` for static methods.
- **Goto-definition** — jump from a usage to the declaration line.
- **Document symbols** — outline tree with classes nesting their methods.

Launch the server through the wrapper script (which sets `PYTHONPATH`
and forwards stdio):

```bash
./bin/lammergeier-lsp
```

Configure your editor's LSP client to spawn that path with language
id `lammergeier`. Set `LAMMERGEIER_LSP_LOG=/tmp/lam-lsp.log` if you
need a verbose trace — the server itself never writes to stderr so
the LSP framing stays clean.

The LSP keeps useful answers even while the user is mid-edit: when the
LALR parser rejects the current buffer it falls back to a regex-based
symbol extractor so completion and outline still respond.

## Contributing

Contributions — features, bug fixes, stdlib improvements, tests, docs —
are very welcome. Please read [`CONTRIBUTING.md`](#/docs/contributing) first;
it covers the expected workflow, the quality bar we try to hold
every change to, and guidance specific to this language's
parser → transpiler → Go pipeline.

## License

Lammergeier Lang, including the compiler, VS Code extension, language server,
and website code in this repository, is licensed under the Apache License 2.0.

Copyright 2026 Thallium Solutions di Busconi Alessandro.

## Thallium Solutions

Check our website at <a href="https://thallium-solutions.com/en/">https://thallium-solutions.com/en/</a>