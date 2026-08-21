# Third-party Lammergeier libraries — format & spec

> **Audience:** library authors, package-registry operators, and
> anyone curious about how Lammergeier resolves and validates
> third-party code. **For the day-to-day install workflow**
> (`lamc install`, the lockfile, conflict detection, the SemVer
> gate) see [`docs/package_manager.md`](#/docs/package_manager).
> **For installing the compiler itself**, see
> [`docs/installation.md`](#/docs/installation).

> **Status:** implemented end-to-end. The compiler resolver, the
> `lamlib.toml` manifest parser, scoped imports, the install /
> uninstall / publish CLI, the SemVer / API-diff gate, transitive
> dependency resolution with cross-library + cross-project
> conflict detection (Lammergeier deps + Go module deps), and the
> reference registry server (Docker + docker-compose) all ship in
> the current build. Coverage: `tests/tests/test_manifest.py` (14
> cases), `test_apidiff.py` (13), `test_scoped_imports.py` (6),
> `test_install_cli.py` (7), `test_dependency_crash.py` (9 — the
> "two libs, same dep, incompatible versions" battery).

This document covers how third-party Lammergeier libraries are
**laid out on disk**, **resolved by the compiler**, and how the
**registry protocol** is shaped. Read it if you want to ship a
library, run a registry, or understand the resolver. Read
[`docs/package_manager.md`](#/docs/package_manager) instead if you
just want to install one.

The high-level workflow a first-time user sees:

```bash
# pull a library down
lamc install lamwebp@1.2.0                        # registry
lamc install https://github.com/alice/lamwebp.git # git
lamc install ./local-checkout                     # path

# use it
echo 'from lamwebp import Encoder' >> app.lam
lamc app.lam
```

…with Lam-specific project state instead of Go / JS ceremony:
`lamlib.toml` records the intended Lam dependencies, Go module
pins, and replacements; `lamlib.lock.toml` records the exact
resolved sources and hashes; `extlibs/` holds the installed Lam
source tree. Users do not hand-edit a generated `go.mod`, maintain
a `package.json`, or juggle global package installs.

---

## Official third-party libraries

These packages are maintained as official Lammergeier third-party libraries.
Some are still mirrored under `third_party/` while they move to standalone
repositories.

| Library | Repository / docs | Description |
|---------|-------------------|-------------|
| `lams3` | [thallium-solutions/lams3](https://github.com/thallium-solutions/lams3) | S3-compatible object storage client for AWS S3, Cloudflare R2, MinIO, Wasabi, Backblaze B2, and custom gateways. Includes Result-first APIs, file/buffer/stream upload, metadata, public URL helpers, and presigned URLs. |
| `lamstripe` | [`third_party/lamstripe/README.md`](../third_party/lamstripe/README.md) | Opinionated Stripe REST API helper with configuration, customers, payment intents, checkout sessions, validation, and test helpers. |
| `lamotel` | [`third_party/lamotel/README.md`](../third_party/lamotel/README.md) | Lightweight OpenTelemetry helper for traces, spans, resource attributes, OTLP export payloads, and integration tests. |

---

## 1. Resolution order *(implemented)*

`lamc` (and `lamc build`) resolves every `from <module> import …`
statement by walking three layers, in order:

| # | Layer | Path(s) | Purpose |
|---|-------|---------|---------|
| 1 | **stdlib** | `<compiler>/lib/` | Modules shipped by the compiler itself (`lamhttp`, `lamdb`, `lamredis`, …). Always wins so users can't accidentally shadow a stdlib module by dropping a same-named file in their project. |
| 2 | **extlibs** | see below | Third-party libraries, in one of four flavours. |
| 3 | **project** | `<source-dir>/`, `<source-dir>/lib/` | The user's own source files and any project-local helpers. Lowest priority so it never overrides the stdlib or an installed dep. |

The extlibs layer itself is searched in this order:

1. `--extlibs DIR` CLI flag (repeatable; first wins).
2. `LAMC_EXTLIBS` env var — colon-separated list (on Windows
   semicolon-separated, matching `os.pathsep`).
3. `<source-dir>/extlibs/` — the project-local install root,
   where `lamc install` lands by default. Analogous to Go's
   `vendor/`; commit it (or don't, depending on whether you want
   reproducible-without-network builds).
4. `~/.lammergeier/extlibs/` — user-global install directory,
   used only when `lamc install --global` is in play.

Any path that doesn't exist is silently skipped; duplicate entries
are de-duplicated so the same directory never gets walked twice.

### Example: project-local first, then global

```bash
# Repo layout
myapp/
├── lamlib.toml       # source of truth
├── lamlib.lock.toml  # auto-generated, commit it
├── main.lam
├── extlibs/          # project-local install (default)
│   └── lamwebp.lam
└── lib/              # project-local helpers
    └── helpers.lam

# Anywhere in the repo:
lamc main.lam
# ↳ stdlib checks first ──  miss
# ↳ extlibs/ auto-discovered  ──  hit: lamwebp.lam
# ↳ helpers.lam resolvable via the project layer
```

### Example: one-shot override

```bash
lamc --extlibs ./experimental-libs app.lam
```

`--extlibs` is repeatable, so you can stack paths:

```bash
lamc --extlibs ./a --extlibs ./b --extlibs ./c app.lam
```

First match wins within the extlibs layer, then the walk continues
to the project layer.

---

## 2. Library anatomy *(implemented)*

A third-party library is a directory that follows one of two
shapes:

**Single-file library**

```
lamwebp.lam                 # one file, module = filename without ext
```

**Multi-file library (package)**

```
lamwebp/
├── __init__.lam
├── codec.lam
└── io/
    └── files.lam
```

The compiler imports `lamwebp` as `<dir>/lamwebp/__init__.lam`, while
`from lamwebp.codec import Decoder` and
`from lamwebp.io.files import readWebp` resolve the corresponding nested
files directly. A nested directory may also use its own `__init__.lam`.
Only imported modules and their transitive imports are bundled.

Package roots may re-export a submodule symbol with a normal import:

```lammergeier
# lamwebp/__init__.lam
from lamwebp.codec import Decoder
```

Consumers can then choose either `from lamwebp import Decoder` or the
explicit submodule path. Canonical nested paths win over legacy files
whose names contain literal dots, but those dotted filenames remain a
compatibility fallback.

The accepted file extension is `.lam`. (The earlier `.tpy`
transitional extension has been retired; rename any such files in
flight before compiling against the current toolchain.)

### Conventions

- Module names use `snake_case` (e.g. `lamwebp`, `json_schema`,
  `pdf_forms`), not `lowerCamel` or `PascalCase`.
- Prefix community libraries with `lam` (e.g. `lamwebp`,
  `lamkafka`) to mirror the stdlib naming so they feel native.
  This is a convention, not a hard rule.
- Every library should expose one `README.md` at its root (see
  §4).

---

## 2a. Designing a library people will actually adopt

The mechanical format is small, but a useful Lam library should
feel native to the language. Aim for a public surface that is
typed, boring to import, easy to test, and honest about the Go
code it wraps.

### Shape the public API first

Prefer one obvious import:

```lammergeier
from lamwebp import Encoder, DecodeOptions
```

For small utility libraries, expose top-level functions:

```lammergeier
func encode(data: bytes, quality: int = 90) -> bytes {
    # ...
}
```

For stateful libraries, expose a class with a compact constructor
and methods that return either a value or `Result`:

```lammergeier
from lamerrors import Result, Error

class Client {
    baseUrl: str
    timeoutMs: int

    func __init__(baseUrl: str, timeoutMs: int = 5000) {
        self.baseUrl = baseUrl
        self.timeoutMs = timeoutMs
    }

    func get(path: str) -> Result {
        if path == "" {
            return Result.Err(Error("invalid_path", "path cannot be empty"))
        }
        # ...
        return Result.Ok({"status": 200})
    }
}
```

Use exceptions for programmer mistakes and `Result` for expected
runtime failures such as missing files, refused connections,
invalid user input, parse errors, and authentication failures.
That makes your library compose with `?`, `do / catch`, and the
stdlib's `lamerrors` patterns.

### Keep types explicit at the boundary

Every exported function and method should annotate parameters and
return types. Avoid `any` in public APIs unless the value is truly
open-ended, such as a JSON payload, a plugin decorator bag, or a
logging context map. Internal helpers can be more flexible; the
public boundary is where users learn what is stable.

Good:

```lammergeier
func thumbnail(input: bytes, width: int, height: int) -> Result
```

Too vague for most users:

```lammergeier
func thumbnail(input: any, options: any) -> any
```

If options are growing, create a small class or dict schema and
document every key. If the library wraps a Go API with many knobs,
start with a narrow Lam-facing API and add escape hatches only
where real users need them.

### Keep stdlib boundaries clear

Third-party libraries should compose the stdlib rather than mirror it.
Prefer packages for vendor APIs, optional integrations, opinionated
frameworks, heavyweight Go dependency trees, or domains that evolve on
their own release cadence. Good examples are `lams3` for
S3-compatible object stores, `lamstripe` for Stripe's API, and
`lamotel` for OpenTelemetry export.

`third_party/lams3` is the reference shape for a Go-SDK-backed
storage library: it exposes ergonomic bool/string helpers for quick
scripts, direct constructors plus env loading, `try*` siblings that
return `Result` for production flows, and live tests that operate
inside a dedicated bucket prefix so they do not collide with existing
objects.

If a feature is generic and lightweight — string handling, JSON, HTTP
helpers, filesystem access, environment variables, time, basic crypto,
or data structures — depend on the existing `lam...` stdlib module
instead of copying it into your package. This keeps third-party APIs
small, makes tests read like normal user code, and avoids surprising
behavioural forks.

### Wrap Go dependencies deliberately

When your implementation uses `go! { ... }`, list every external
Go module under `[go-deps]`:

```toml
[go-deps]
"github.com/chai2010/webp" = "v1.1.1"
```

Do not rely on `go mod tidy` discovering an unpinned latest
version during the consumer's build. The package manager merges
Go pins from every installed library, writes the result into the
synthesised `go.mod`, and refuses incompatible majors before the
build starts.

Keep the surrounding Lam wrapper signature precise even when the whole
body is `go!`. The semantic checker exports that declaration metadata to
consumers, so constructors, static methods, aliases, module-qualified
calls, and inferred instance calls all receive normal argument diagnostics.
Only genuinely raw Go or intentionally dynamic `any` values are opaque.

For Go standard-library packages (`net/http`, `crypto/hmac`,
`strings`, etc.), do not add a `[go-deps]` entry; those are part
of the target Go toolchain.

### Document the happy path and the failure path

A good library README has three runnable examples:

1. The smallest successful call.
2. The same call with options or configuration.
3. The error-handling path with `Result`, `?`, or `do / catch`.

Example:

```lammergeier
from lamwebp import Encoder

func main() {
    enc: Encoder = Encoder(quality = 82)
    do {
        out: bytes = enc.encodeFile("in.png")?
        writeFile("out.webp", out)
    } catch err {
        print(f"encode failed: {err}")
    }
}
```

Users should be able to paste the first example into `main.lam`
after `lamc install <name>` and see it compile.

### Test as a consumer

Library-local tests should compile through the same import path
that users will use:

```lammergeier
from lamtest import Test
from lamwebp import Encoder

func main() {
    enc: Encoder = Encoder()
    Test.assertTrue(enc.supports("webp"), "webp supported")
}
```

Before publishing, install the library into a temporary app by
path and compile that app:

```bash
mkdir /tmp/lamwebp-consumer && cd /tmp/lamwebp-consumer
lamc init --name lamwebp_consumer
lamc install /path/to/lamwebp
lamc main.lam --run
```

This catches packaging mistakes that unit tests miss: manifest
name mismatches, missing files in the tarball, undeclared
`[go-deps]`, bad scoped imports, and public symbols that were
never exported from the package root.

### Version for the API your users see

Patch releases are for body-only fixes. Minor releases add public
functions, methods, fields, optional parameters, or new
capabilities. Major releases remove or rename public API, change
types, or add required parameters. `lamc publish` warns when the
API diff disagrees with your version bump, and `lamc install`
refuses lying upgrades unless the consumer passes
`--allow-breaking`.

When in doubt, bump minor rather than patch. It communicates that
the public surface changed without forcing consumers through a
major migration.

---

## 3. Manifest format *(implemented)*

Each library ships a `lamlib.toml` at its root. The compiler
parses it through `compiler.manifest.Manifest` (a hand-rolled
zero-dependency TOML subset — no PyPI install required to read a
manifest). Minimal example:

```toml
[library]
name        = "lamwebp"
version     = "1.2.0"
description = "WebP encode/decode for Lammergeier"
license     = "MIT"
authors     = ["Alice Example <alice@example.com>"]
homepage    = "https://example.com/lamwebp"
repository  = "https://github.com/alice/lamwebp"

[compatibility]
# Range of ``lamc`` versions the library has been tested against.
# Caret matches SemVer. Compile warns when an installed extlib's
# range does not include the running compiler, but does not block
# the build.
lamc = "^0.4"

[dependencies]
# Other Lammergeier libraries this library pulls in transitively.
# Keys are module names, values are version specifiers. Scoped
# names need quoting because the bare-key character set excludes
# the leading ``@`` for grammatical reasons.
lamhttp        = "^1.0"
"@bob/lamutil" = ">=0.4 <1.0"
# Local path overrides for development checkouts:
lamother = { path = "../lamother" }
# Direct git dependencies for forks or libraries outside a registry:
lamfork = { git = "https://github.com/alice/lamfork.git", ref = "v1.2.0" }

[go-deps]
# Go modules the library's transpiled output imports from a
# ``go { }`` inline block. Keys are the canonical module paths
# Go's own ``go.mod`` would carry; values are ``v``-prefixed
# SemVer tags (pseudo-versions like
# ``v0.0.0-20250101010101-deadbeefcafe`` are accepted too). These
# pins are folded into a single merged set at install time and
# written into the synthesised ``go.mod`` at compile time, so
# ``go mod tidy`` honours your pick instead of silently upgrading
# to the latest tag. ``[go.dependencies]`` is an accepted alias.
"github.com/foo/bar" = "v1.2.3"
"gopkg.in/yaml.v2"   = "v2.4.0"

[scripts]
# Optional project/library commands. Run with ``lamc lib run <name>``.
test   = "lamc test/test_all.lam"
format = "lamc fmt src/"
```

`lamc fmt <directory>` walks the directory recursively and formats every
`.lam` file it finds, so a single script can cover a whole library source tree.
Use `lamc fmt <directory> --check` when you want CI to fail on formatter drift.

### Validation rules

| Field | Required | Notes |
|-------|----------|-------|
| `library.name` | ✅ | Must match the module name that consumers `import`. Ascii, `snake_case`. |
| `library.version` | ✅ | SemVer string. `lamc install` refuses to replace an installed library with a version < the one on disk without `--force`. |
| `library.license` | optional | SPDX identifier (`MIT`, `Apache-2.0`, `BSD-3-Clause`, …). Strongly recommended before public publishing. |
| `compatibility.lamc` | optional | Version range. `lamc version` prints the compiler version. During compile, installed extlibs whose range does not include that version emit a warning and continue; the install path does not hard-enforce it. |
| `dependencies` | optional | Resolved transitively by `lamc install`; every reachable `[dependencies]` key lands under `extlibs/`. Conflicting constraints (two libs + project all pinning the same name at incompatible majors) surface as a `DependencyConflict` **before any on-disk mutation**. |
| `go-deps` | optional | Go modules the transpiled output imports. Path must be a multi-segment Go module path (single-segment names are rejected); version must be a ``v``-prefixed SemVer or Go pseudo-version. Incompatible majors across libs + project are a hard error (Go treats each major as a different package). |
| `[scripts]` | optional | Free-form shell commands runnable with `lamc lib run <script>`. The command discovers the nearest `lamlib.toml`, runs from that manifest's directory, and preserves the script exit code. |

Libraries without a `lamlib.toml` are still loadable — the
compiler's search path doesn't require one. The manifest becomes
mandatory **only** when publishing through a registry or when
`lamc install` is asked to fetch the library (the installer needs
the manifest to learn the canonical name + version).

---

## 4. Published layout

A library tarball / git checkout should look like:

```
lamwebp-1.2.0/
├── lamlib.toml         # manifest (§3)
├── README.md           # human-readable docs (API + examples)
├── CHANGELOG.md        # optional but strongly recommended
├── LICENSE             # full license text
├── lamwebp.lam         # or a lamwebp/ package directory
└── tests/              # lamtest-based test suite
    └── test_roundtrip.lam
```

### Authoring checklist

For a publishable library, create a directory whose root contains:

1. `lamlib.toml` with at least `[library].name` and
   `[library].version`; include `license`, `description`,
   `authors`, `repository`, and `compatibility.lamc` before
   publishing publicly.
2. The importable module source, either as `<name>.lam` for a
   single-file library or `<name>/__init__.lam` for a package-style
   library. The manifest name must match what consumers import:
   `name = "lamwebp"` means `from lamwebp import Encoder`.
3. `README.md` with install instructions and examples.
4. `LICENSE` and, ideally, `CHANGELOG.md`.
5. Tests under `tests/` that compile against the public API.

Example minimal library:

```
lamgreet/
├── lamlib.toml
├── README.md
├── LICENSE
├── lamgreet.lam
└── tests/
    └── test_greet.lam
```

```toml
[library]
name        = "lamgreet"
version     = "1.0.0"
license     = "MIT"
description = "Small greeting helpers"

[compatibility]
lamc = "^0.4"

[dependencies]
lamstrings = "^1.0"
```

```lammergeier
# lamgreet.lam
from lamstrings import Strings

func greet(name: str) -> str {
    return "hello " + Strings.toUpper(name)
}
```

Publish it with:

```bash
lamc publish ./lamgreet --registry https://libraries.example.com
```

During publish, `lamc` validates `lamlib.toml`, packs the directory
as a source tarball, skips build/cache/editor artefacts, warns on
SemVer/API surface drift, and uploads to the registry. The registry
stores source only; consumers compile the library on their machine.

`README.md` should include:

1. A one-sentence elevator pitch.
2. Installation command: `lamc install lamwebp`.
3. **Compat matrix**: minimum compiler version.
4. A runnable ≤10-line usage example.
5. A link to the API reference section within `README.md` itself
   (keeping docs next to the code — same convention as the
   stdlib section of `docs/stdlib.md`).

### Dependencies in a library manifest

For libraries, `[dependencies]` is the right place to declare other
Lam libraries your library needs. Consumers do **not** install those
one by one: when they install your library, `lamc install` walks the
full dependency graph and installs every reachable registry
dependency under `extlibs/`.

```toml
[dependencies]
lamstrings = "^1.0"
"@acme/lamcolor" = ">=0.2 <1.0"
```

`path = "../other-lib"` entries are useful while authoring local
checkouts, but they are only followed for the root library being
installed. Published libraries should use registry version
constraints for dependencies so consumers do not inherit
publisher-local filesystem paths.

For application projects, `lamlib.toml` *is* the source of truth.
With no positional arguments, `lamc install` reads the project's
`[dependencies]` table and materialises everything declared there
(plus the lockfile-pinned transitive closure). Adding a new
top-level dep is a one-liner that updates the manifest, the
lockfile, and the on-disk install in one shot:

```bash
lamc install lamstrings@^1.0 @acme/lamcolor@0.2.0
```

Transitive dependencies declared by those libraries are automatic.
For a teammate cloning the repo afterwards, the canonical setup
command is just:

```bash
lamc install                 # reads lamlib.toml + lamlib.lock.toml
```

---

## 5. Scoped names — `@scope/name` *(implemented)*

Libraries can opt into a two-level identifier the same way npm
packages do:

```toml
[library]
name    = "@alice/lamwebp"
version = "1.2.0"
```

```lammergeier
from @alice/lamwebp import Encoder
```

Grammar, resolver and registry all treat the full `@alice/lamwebp`
string as the canonical key. The on-disk install layout uses the
scope as a real directory:

```
~/.lammergeier/extlibs/
└── @alice/
    └── lamwebp/
        ├── lamlib.toml
        └── __init__.lam
```

This lets the same `lamwebp` short name coexist under different
scopes (`@alice/lamwebp` and `@bob/lamwebp` are independent
libraries). Plain (unscoped) names continue to work — they just
live at the root of the extlibs tree.

---

## 6. Publishing *(implemented)*

Two distribution channels are supported:

### 6.1 Plain git

Any git repository that follows the layout in §4 is already a
library. Consumers pin by commit SHA, tag, or branch:

```bash
lamc install https://github.com/alice/lamwebp.git@v1.2.0
lamc install git@github.com:alice/lamwebp.git@main
```

Nothing magical — the install command uses the shared git cache
under `$LAMC_CACHE` (default `~/.lammergeier/cache/git/`), checks
out the requested ref into a temporary working tree, validates
`lamlib.toml`, copies the source into `./extlibs/<name>/` by
default (`~/.lammergeier/extlibs/<name>/` only with `--global`),
records the resolved commit SHA plus source-tree hash in
`lamlib.lock.toml`, and writes a git-form entry into `[dependencies]`
when the install was an explicit command:

```toml
[dependencies]
lamwebp = { git = "https://github.com/alice/lamwebp.git", ref = "v1.2.0" }
```

### 6.2 Registry

A small index site mirrors the pattern of crates.io / npm but is
intentionally minimalist:

- Immutable tarballs keyed by `<alias>-<version>.tar.gz` (where
  `<alias>` is the scope-flattened safe name — `@acme/lamcolor`
  becomes `@acme__lamcolor` for the file path).
- `lamc publish` packs the source tree, validates the manifest
  client-side, and POSTs the tarball to `POST /api/v1/publish`.
- Versions are never deleted, only **yanked** (still
  downloadable, flagged in the index).
- Three endpoints, no auth in the reference implementation
  (production deployments slot in their own auth middleware):
  * `GET /api/v1/libraries/<name>` → JSON `{name, versions: […]}`
  * `GET /api/v1/libraries/<name>/<file>.tar.gz` → tarball stream
  * `POST /api/v1/publish` (multipart `file=<tarball>`)

The reference implementation lives under
[`tools/registry/`](https://github.com/thallium-solutions/lammergeier-lang/blob/main/tools/registry/) and ships as both a
standalone Python script (`server.py`, no PyPI deps) and a
`Dockerfile` + `docker-compose.yml` for local-dev clusters:

```bash
# Build + run the registry locally.
docker compose -f tools/registry/docker-compose.yml up

# Install through it.
lamc install --registry http://localhost:8765 lamwebp
```

A tiny seed catalogue (`lamgreet@1.0.0`, `@acme/lamcolor@0.2.0`)
autoloads on first boot so end-to-end tests and demos work
without an empty registry.

---

## 7. Installation CLI

The full reference for the package-manager verbs (`init`,
`install`, `uninstall`, `tidy`, `verify`, `list`, `tree`,
`why`, `publish`) lives in
**[`docs/package_manager.md`](#/docs/package_manager)**. That doc covers:

- Spec syntax (`name`, `name@version`, `name@<range>`, scoped
  names, git URLs, local paths).
- Every flag: `--registry`, `--force`, `--allow-breaking`,
  `--extlibs-dir`, `-q`, `--global` (opt-out of project mode),
  `--frozen` (lockfile-driven install), `--offline` (no network,
  cache-only — implies `--frozen`).
- `lamc init` — scaffold a fresh project with `--name`,
  `--version`, `--scope`, `--license`, `--bin` / `--lib`,
  `--force`.
- `lamc tidy [--check]` — sync the manifest to the project's
  actual import graph (drop unused, add missing, refresh
  lockfile). `--check` for CI.
- `lamc verify` — re-hash every installed extlib against the
  lockfile's `tree_sha256` for supply-chain integrity.
- `lamc list` / `tree` / `why <name>` — read-only lockfile
  introspection backed by the `requested_by` array on each pin.
- The `[replace]` directive — project-only override map that
  redirects a declared dep to a local path or git URL without
  touching `[dependencies]`. Modeled on Go's `go.mod replace`.
- The lockfile (`lamlib.lock.toml`) schema v1 with `[meta]`,
  `requested_by`, and `tree_sha256` fields, plus `[pins.*]` for
  Lam libraries and `[go_pins.*]` for Go modules.
- Transitive resolution, the cross-library / cross-project
  conflict detector, and the Go-module MVS pick.
- The `LAMC_REGISTRY`, `LAMC_TOKEN`, `LAMC_EXTLIBS`, `LAMC_CACHE`
  environment variables.
- A worked example with conflicts and the cookbook + troubleshooting
  table.

What you need to know **here** to ship a library cleanly is:

- The `lamc install` resolver consults the `[dependencies]` and
  `[go-deps]` tables of every library it pulls in. Be precise
  with your version constraints; "any version" (`*`) is legal
  but signals an unmaintained dep.
- The install respects an immutable-versions invariant — once
  you publish `lamwebp@1.2.0`, you can never republish that
  exact tarball under the same version.

---

## 8. SemVer / API-diff gate

The installer refuses upgrades whose SemVer bump lies about the
actual change. The gate is implemented in `compiler/apidiff.py`
and runs both at install time (hard refusal) and at publish time
(soft warning).

| SemVer bump | Allowed change severity |
|-------------|-------------------------|
| `1.0.0 → 1.0.1` (patch) | only patches |
| `1.0.0 → 1.1.0` (feature) | features + patches |
| `1.0.0 → 2.0.0` (breaking) | anything |

Severity classification (a dependency-free AST scan):

- **breaking** — removed function / method / field (including public
  `static` variables), changed
  return type, changed field type, or a new *required*
  parameter added to an existing function.
- **feature** — new function / method / field, or a new
  *optional* parameter (one with a default value).
- **patch** — anything else (including pure-body changes
  invisible to a surface scan).

Underscore-prefixed and `private`-marked members are excluded
from the diff — they're considered internal implementation
details.

The full install-side workflow (with the `--allow-breaking`
escape hatch) is documented in
[`docs/package_manager.md`](#/docs/package_manager?h=6-the-semver--api-diff-gate).

> **Note.** The gate is a *protection*, not a *guarantee*: it
> only sees the static surface, so a body-only behavioural break
> (same signature, different semantics) won't be caught here.
> The lockfile + immutable-versions rule of §6.2 is the
> orthogonal defence against that.

---

## 9. Roadmap

| Milestone | Status |
|-----------|--------|
| Resolver layering (stdlib / extlibs / project) | ✅ |
| `--extlibs` flag + `LAMC_EXTLIBS` env var | ✅ |
| Auto-discovery of `./extlibs/` and `~/.lammergeier/extlibs/` | ✅ |
| `lamlib.toml` parser in the compiler | ✅ |
| `@scope/name` library identifiers | ✅ |
| `lamc init` scaffold (`--name`, `--version`, `--scope`, `--bin` / `--lib`, `--force`) | ✅ |
| `lamc install <name>@<ver>` (registry / git / path) | ✅ |
| Bare `lamc install` reads `[dependencies]` + `--frozen` / `--offline` flags | ✅ |
| Lockfile schema v1 (`[meta]`, `requested_by` arrays, `tree_sha256` for every source) | ✅ |
| Content-addressed cache at `$LAMC_CACHE` / `~/.lammergeier/cache/` | ✅ |
| Dependency resolution / `lamlib.lock.toml` | ✅ (transitive; every reachable `[dependencies]` dep is installed under `extlibs/`, every reachable `[go-deps]` module ends up in `[go_pins.*]`) |
| Cross-lib + project conflict detection | ✅ (demand set folds project manifest + every installed lib's constraints; incompatible majors raise `DependencyConflict` before touching disk) |
| `[go-deps]` — Go modules pinned per library | ✅ (merged via MVS, written into the synthesised `go.mod` at compile time so `go mod tidy` honours the pick) |
| `[replace]` directive — project-only redirect to path / git URL | ✅ (applies transitively, lockfile records the replacement's source) |
| `[workspace]` reserved for future multi-package layouts | ✅ (parser rejects user manifests that declare it) |
| `lamc tidy` / `tidy --check` — sync manifest to the project's import graph | ✅ |
| `lamc verify` — re-hash extlibs against `tree_sha256` for integrity | ✅ |
| `lamc list` / `tree` / `why` — lockfile introspection | ✅ |
| Reference registry + `lamc publish` | ✅ (Docker image under `tools/registry/`) |
| SemVer / API-diff gate (`lamc install --allow-breaking` to override) | ✅ |
| Unused-import / unused-parameter / unused-manifest-dep warnings | ✅ (Go-style warn-don't-error semantics) |
| `lamc lib run <script>` | ✅ (runs `[scripts]` entries from the nearest `lamlib.toml`; supports `--list`, `--cwd`, `--dry-run`, and `--quiet`) |
| Submodule imports (`from lamwebp.codec import …`) | ✅ (nested files/packages, root re-exports, transitive bundling, diagnostics, and LSP completion) |

Resolved design questions:

- **Namespacing:** scoped names (`@scope/name`) shipped, plain
  names continue to work.
- **Binary-only distribution:** rejected — libraries always ship
  source so the host's Go toolchain reproduces the binary
  deterministically across OS / arch boundaries.
- **SemVer enforcement:** auto-detected via AST diff; install is
  hard-refused on lying releases (with an `--allow-breaking`
  escape hatch) and a soft-warned at publish time.
