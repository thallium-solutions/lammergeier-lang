# Changelog

All notable changes to Lammergeier Lang are documented here.

This repository does not currently use version tags, so entries are grouped by
commit date from the Git history and summarized against the current README and
documentation.

## 2026-07-11

### Added

- Expanded the Lammergeier LSP with richer IntelliSense for instance members,
  inherited fields/methods, `self`, `base`, named inheritance aliases, and
  annotated local variables. Go-to-definition now resolves those members too,
  and semantic/syntax test fixtures with `# expect-error` or
  `# expect-warning` suppress matching expected diagnostics while surfacing
  expectation mismatches clearly.
- Added read-only LSP import suggestions for stdlib and workspace exports,
  module-name completion after `from`, workspace symbol search, and richer
  signature help for methods and constructors. Suggested imports do not edit
  source automatically, method signatures hide implicit `self`, and
  constructors are displayed with `init(...)` sugar.
- Added `lamc version` / `lamc --version` to print the compiler version used
  when evaluating library `compatibility.lamc` ranges.
- Added `lamc lib run <script>` for running commands declared in a project's
  `lamlib.toml` `[scripts]` table, with `--list`, `--cwd`, `--dry-run`, and
  `--quiet` support.
- Added git dependency write-back: explicit git installs now update
  `lamlib.toml` with `{ git = "...", ref = "..." }`, bare `lamc install`
  consumes that form, and lockfiles retain both the requested ref and the
  resolved commit.
- Added a guarded GitHub integration test for
  `https://github.com/thallium-solutions/lams3.git` that verifies git install
  write-back, frozen/offline replay from cache, and external lams3 import
  transpilation when `LAMC_LIVE_GITHUB_LAMS3=1` is set.
- Added richer `lamc doctor` diagnostics, including `--json` output,
  `--strict` CI gating, project-manifest discovery, cache stats, PATH details,
  selected Go environment values, Python requirement checks, and editor
  extension inspection.
- Added Lam-source semantic errors when `?` or `do/catch` are used without the
  required `from lamerrors import Result` support import, avoiding generated-Go
  `undefined: Result` failures.
- Added semantic call-shape/type diagnostics for directly imported functions
  from resolved Lam modules, including `from helper import f` and
  `import helper; helper.f(...)`, so missing arguments, bad keywords, and
  simple argument type mismatches are reported before Go emission.
- Added semantic metadata for directly and module-qualified imported Lam
  classes, so constructor calls, static method calls, instance-method misuse,
  and simple constructor type mismatches are validated at the Lam source
  location.
- Added conservative inferred receiver types for plain assignments from known
  constructors, static factory methods, and imported functions, so misspelled
  members on unannotated imported Lam values are reported before Go emission.
- Added semantic expression diagnostics for common binary operator mismatches,
  list/string/dict index type mistakes, and non-integer slice bounds.
- Added dropped-return warnings for non-void imported functions and imported
  static methods, including module-qualified calls.
- Added semantic name diagnostics for top-level functions, classes, and
  interfaces that lower to the same Go symbol after export-name casing.
- Added `go!` boundary diagnostics for `self` and the receiver alias `s` used
  outside instance methods.
- Added branch-sensitive definite-assignment diagnostics for local reads before
  assignment, `if`/`else` joins, self-referential first assignments, and
  conservative loop boundaries.
- Added branch-sensitive `match` capture scopes so pattern bindings are only
  visible in their own case arm and do not leak after the match.
- Added conservative `try`/`catch`/`else` definite-assignment joins so values
  assigned only on some continuing exception paths are reported before Go
  emission.
- Added destructuring arity diagnostics for tuple literals and local functions
  annotated with `tuple[...]` return types.
- Added alias-specific diagnostics for `from lamerrors import Result as ...`
  used with `?` or `do/catch`, which require the unaliased `Result` helper
  import today.
- Added comparison and membership type diagnostics for incompatible ordered
  comparisons, equality checks, `in` item types, and invalid `in` containers.
- Added unary and boolean operator type diagnostics, including `not`, `and`,
  `or`, unary `+`/`-`, and bitwise `~` operands.
- Added ternary expression type diagnostics for non-boolean conditions,
  incompatible branch values, and annotated assignment/return inference.
- Added comprehension filter and annotated container literal diagnostics,
  including list elements, dict keys/values, set elements, and const literals.
- Added built-in cast diagnostics for invalid cast arity, keyword arguments,
  and non-numeric arguments to numeric/bool casts.
- Added explicit generic-call diagnostics for type-argument arity and concrete
  type-parameter substitutions in function and class constructor calls.
- Allowed Go-only keywords such as `select` in Lam declarations that lower to
  safe Go names, including public functions, classes, class fields, static
  methods, and public instance methods.
- Added semantic diagnostics for parsed-but-unsupported decorators, including a
  specific `@private` hint to use the supported `private func` spelling.
- Added inheritance diagnostics for incompatible method overrides,
  instance/static override mismatches, duplicate direct bases, field/method
  conflicts, and ambiguous direct promoted members from multiple inheritance.
- Added interface conformance diagnostics for method parameter type mismatches.
- Extended class/interface diagnostics to compare nested generic method
  parameter and return types, including imported interfaces/classes.
- Extended inheritance diagnostics through transitive and imported base
  hierarchies.
- Added a non-fatal warning for classes with multiple distinct base classes,
  since multiple inheritance is parsed but not fully supported yet.
- Added module-boundary visibility diagnostics for importing private functions,
  calling module-qualified private functions, and calling private methods on
  imported classes.
- Added tuple/destructuring diagnostics for annotated element type mismatches,
  annotation arity mismatches, duplicate destructuring targets, `-> (T, U)`
  multi-return arity, and imported tuple-returning functions/static/instance
  methods.
- Improved destructuring type inference so each tuple target receives its own
  element type instead of the whole tuple type.
- Added specialized dropped `Result`/`Option` warnings that tell callers to
  handle the value or assign it to `_`.
- Added conservative branch-join inference for local variable types across
  `if`/`else`, `match`, and `try`/`catch`, so misspelled members after branches
  are reported when every continuing path agrees on the receiver class.
- Extended Go-reserved identifier diagnostics to import aliases, function and
  lambda parameters, loop/comprehension targets, catch bindings, and
  destructuring targets.
- Added warnings for user-declared names with compiler-reserved prefixes such
  as `__lam*` and `__q*`.
- Added standalone `third_party/lams3/CHANGELOG.md`, `LICENSE`, and `NOTICE`
  files so lams3 can be distributed from its external repository.
- Added explicit inheritance parent aliases: single unnamed parents expose
  `base`, named bases use `class Child(alias: Parent)`, and
  `base.init(...)` / `alias.__init__(...)` initialize the embedded parent via
  the parent constructor.

### Changed

- Updated lams3 package metadata to advertise Apache-2.0 licensing.
- Compiles now warn, without blocking, when an installed extlib's
  `compatibility.lamc` range does not include the running compiler version.
- Refactored `lamc fmt` / LSP document formatting to preserve inline `go!(...)`,
  format `go! { ... }` blocks with `gofmt` when available, and keep Lam source
  in the repository's K&R/four-space style more reliably.
- Updated roadmap and diagnostics documentation to reflect that unused local
  warnings and their Go silencing are implemented for function and block
  scopes.
- Corrected inheritance transpilation documentation to remove unsupported
  `super()` lowering and describe current parent embedding, constructor, and
  inherited-method behavior.
- Multiple inheritance is now supported through Go embedding with named parent
  aliases, ambiguity diagnostics for unqualified inherited members, and
  inheritance-cycle errors.

## 2026-07-10

### Added

- Expanded `third_party/lams3` with Result-returning S3 operations, including
  text/bytes object I/O, buffer upload/download, stream upload, file
  upload/download, object stat/head metadata, copy, move, key-only listing,
  bulk delete, config validation, and presigned GET/PUT URL helpers.
- Added lams3 offline coverage for `Result`/`do-catch` config handling and
  direct configuration without environment variables, plus presigned URL
  generation and a live R2/S3 roundtrip that uses a dedicated
  `lams3-tests/live-roundtrip/` prefix.

### Changed

- Updated lams3 AWS SDK for Go v2 pins, including
  `github.com/aws/aws-sdk-go-v2/service/s3 v1.105.0`.
- Simplified lams3 environment configuration to use `S3_BUCKET` instead of
  separate public/private bucket variables.
- Updated lams3 documentation to cover direct constructors, Result-first APIs,
  file/buffer/stream workflows, stat metadata, copy/move, bulk delete, and
  presigned URL usage.

## 2026-05-09

### Added

- Expanded `third_party/lams3` offline coverage for environment configuration,
  readiness checks, static and client-level public URL generation, URL encoding
  variants, and `S3Object` defaults.
- Added a more complete `third_party/lams3` README covering configuration,
  usage, API reference, offline tests, and optional live S3-compatible
  round-trip testing.

### Changed

- Updated the `lams3` package test runner to discover every `offline_*.lam`
  regression case automatically.

## 2026-05-07

### Added

- Lam-facing syntax diagnostics that render parser failures with source
  snippets, expected Lam constructs, and targeted repair hints.
- Import-resolution diagnostics for direct missing modules, missing imported
  symbols, typo suggestions, searched paths, and package `__init__.lam`
  resolution.
- Semantic diagnostics for local call-shape errors, including missing required
  arguments, too many positional arguments, unknown or duplicate keyword
  arguments, and positional/keyword duplicates.
- Semantic return-flow diagnostics for functions that omit required return
  values or return values from `-> None` functions.
- Conservative known-class member diagnostics for unknown `self.member`,
  `Class.member`, static field, and static method references, with suggestions
  when a close member exists.
- Import binding diagnostics for duplicate aliases, aliases that shadow
  builtins, and imports that conflict with existing functions, classes,
  interfaces, or constants.
- Structural interface conformance diagnostics for known local classes at
  typed assignments and function-call boundaries, covering missing methods,
  obvious method arity mismatches, and return annotation mismatches.
- Constructor call-shape diagnostics for known local classes, validating
  `init` / `__init__` arguments and zero-argument classes before Go emission.
- Syntax repair hints for C-style logical operators, pointing `!`, `&&`, and
  `||` users toward Lammergeier's `not`, `and`, and `or` forms.
- Syntax repair hints for common wrong function declaration keywords such as
  `function` and `fn`, pointing users to Lammergeier's `func`.
- Semantic diagnostics for misplaced `self`, class-qualified instance method
  calls, and static methods called through known instances.
- Deterministic undefined-name suggestions that prefer Lam keywords, builtins,
  and stdlib modules before falling back to in-scope variable/function names.
- Semantic warnings for likely receiver declaration mistakes, including methods
  that use `self` without declaring it, constructors missing `self`, and static
  methods that still accept `self`.
- Semantic diagnostics for unknown members on known typed instances, close
  type-annotation typos, and obvious literal return-type mismatches.
- Clearer semantic diagnostic rendering with explicit `error[kind]` /
  `warning[kind]` tags and modest ANSI color in interactive terminals.
- Conservative non-void return-path diagnostics for functions with missing
  paths through simple `if` / `match` / `try` control flow.
- Import diagnostics now warn on unused top-level imports and suggest close
  exported symbol names when a direct `from module import Name` typo can be
  resolved from the imported Lam module.
- Match diagnostics now warn on duplicate unguarded literal `case` patterns
  and cases made unreachable by an earlier wildcard `_`.
- LSP diagnostics now publish semantic warnings as editor warnings instead of
  suppressing advisory diagnostics.
- LSP preprocessing now mirrors the compiler's dict-destructuring rewrite, so
  valid `{key, other: alias} = expr` syntax no longer appears as a parse error
  in editors.

### Tests

- Added focused syntax, semantic, and import-resolution diagnostic test suites
  covering negative and positive/fallback cases for the new Lam-side error
  messages.
- Extended semantic diagnostic tests with warning expectations so advisory
  messages are verified separately from hard errors.
- Added coverage for missing return paths, unused import warnings, and import
  export typo suggestions, plus full normal runtime regression validation.
- Added coverage for duplicate `match` cases, wildcard-before-case warnings,
  and guarded-case fallbacks.
- Added LSP coverage for semantic warning severities and verified the VS Code
  extension TypeScript build.
- Added LSP regression coverage for valid dict destructuring syntax.
- Added standalone `lams3` third-party library tests for offline URL helpers
  and optional live S3-compatible upload/read/list/delete round-trips.

## 2026-05-05

### Changed

- Renamed primitive string method aliases to match `lamstrings.Strings` static
  method names exactly. For example, `"hi".toUpper()` now mirrors
  `Strings.toUpper("hi")`; Python-style aliases such as `.upper()`, `.strip()`,
  `.lstrip()`, and `.find()` were replaced by names such as `.toUpper()`,
  `.trim()`, `.trimLeft()`, and `.index()`.
- Refactored built-in string method dispatch to call the stdlib
  `lamstrings.lam` implementation instead of emitting inline Go `strings.*`
  calls.
- Refined `LAMMERGEIER.*` resolution to use the dynamic dispatcher for
  user-defined symbols while retaining only `None` and `nil` as literal aliases.
- Removed legacy `.tpy` file support.

### Added

- Auto-injection of `lamstrings` when built-in string methods are detected, so
  simple string method calls no longer require an explicit import.
- `Strings.format`, backed by a variadic `fmt.Sprintf` wrapper and used by
  `.format()`.
- Import aliasing with `as`.
- Ignored-return-value handling through assignment to `_`.
- Postfix `++` and `--` operators, including overload support.
- Multiline f-strings.
- Same-arity function overloading by parameter type.
- Compiler warning when the `?` operator is used outside a `Result`-returning
  function.
- Frozen stdlib Go-module pins documentation and a pinned
  `modernc.org/sqlite v1.50.0` dependency.

### Documentation

- Updated `docs/SYNTAX.md` and `docs/TRANSPILATION.md` to describe the
  string-method naming model, `lamstrings` auto-injection, and the current
  Lam-to-Go lowering behavior.

## 2026-05-03

### Added

- Static variables in classes.
- Apache License 2.0 licensing metadata and NOTICE file.

### Fixed

- Documentation references and miscellaneous fixes.

## 2026-05-02

### Added

- Third-party package manager support through `lamc install`, `lamc uninstall`,
  `lamc publish`, `lamc tidy`, `lamc verify`, and project scaffolding helpers.
- SemVer and API-diff enforcement for published Lammergeier libraries.
- Transitive dependency resolution for both Lammergeier libraries and Go
  modules.
- Registry server implementation under `tools/registry/`.
- Vendor-style installs with deterministic lockfiles.
- Content-addressed cache support for tarballs and git repositories.
- Lockfile v1 metadata including `requested_by` arrays and `tree_sha256` for
  git/path sources.
- `--frozen` and `--offline` package-manager modes for reproducible CI and
  container builds.
- `--global` installs for one-off user-global library installs.
- `[replace]` directives for local overrides of transitive dependencies.

### Documentation

- Reorganized the README quick start and package-manager sections.
- Added `docs/installation.md` with installer flags, prerequisites, editor
  wiring, upgrade flow, uninstall steps, and troubleshooting.
- Added package authoring and registry documentation in
  `docs/package_manager.md` and `docs/third_party_libraries.md`.
- Documented accepted-but-unused grammar forms in `docs/SYNTAX.md` and
  `docs/TRANSPILATION.md`.

### Fixed

- Website fixes and documentation cleanup.
- Additional test coverage for package-manager and compiler behavior.

## 2026-05-01

### Added

- LSP server and editor extension refactors for VS Code, Cursor, and Windsurf.
- LSP symbol collection for top-level variable assignments, scope-aware local
  bindings, and type annotation details.
- Blank identifier `_` for discarding values.
- Multiline lambda bodies with explicit return types.
- `lamdata` DataFrame, Series, and DataFrameGroups stdlib module with
  pandas-style operations.

### Changed

- Moved `SYNTAX.md` and `TRANSPILATION.md` into the `docs/` directory and
  updated references across the repository.
- Improved cross-library class/static-method dispatch by extending compiler
  cache keys and harvesting pre-class/static-method metadata.
- Documented default output-path behavior: without `-o`, binaries are emitted
  beside the source file with the `.lam` suffix removed.
- Clarified block-scoped `try`/`catch` recovery behavior.
- Updated `.gitignore` to exclude common compiled binaries generated from
  tests and examples.

## 2026-04-30

### Added

- Three-layer library resolution: bundled stdlib, external libraries, then the
  project layer.
- `--extlibs` CLI flag and `LAMC_EXTLIBS` environment variable.
- `lamc migrate` subcommand while preserving legacy `lamc <source>` behavior.
- `lamcron` scheduler and `lamsmtp` email client documentation.
- Set literals and comprehensions.
- Tuple and generator comprehensions.
- Expanded f-string expression support, including safe navigation and
  null-coalescing.
- Augmented assignment operators.
- `assert` statements.
- `global` and `nonlocal` declarations.
- Nested function closures.
- Python-style slicing with negative indices and strides.
- Dictionary destructuring.
- Named and keyword arguments with validation.

### Documentation

- Rewrote `CONTRIBUTING.md` with repository layout, compiler pipeline,
  workflow, testing discipline, stdlib authoring guidance, parser/transpiler
  change notes, documentation expectations, and pull request checklist.
- Added compiler documentation covering build caching, the `LAMMERGEIER.*`
  namespace, syntax flexibility, diagnostics, parser caching, and semantic
  checker integration with the LSP.

## 2026-04-29

### Added

- `HttpClient` with base URL, headers, timeout, and GET/POST/PUT/PATCH/DELETE
  helpers.
- `HttpResponse` wrapper.
- `JwtKeySet` with JWKS loading, `kid`-based verification routing, and
  `kid`-tagged signing methods.
- Inline-block semicolon insertion, empty-block pass filling, and runaway
  semicolon collapsing.
- `lamserver` lifecycle hooks, decorators, content-type parsers, versioned
  routes, pagination helpers, and OpenAPI generation.
- Cross-library default-argument filling and chained-call class inference using
  enhanced library cache metadata.
- Multiline expression continuation with lookahead for leading operators.
- `go!` block return-statement rewriting and `self` reference translation.

## 2026-04-28

### Documentation

- Added the comprehensive standard library reference in `docs/stdlib.md`.
- Added the `lamserver` plugin authoring guide in `docs/server_plugins.md`.
- Expanded the syntax reference module inventory.

## 2026-04-27

### Added

- Advanced `lamserver` features: query and cookie helpers, HTML responses,
  redirects, cookie helpers, error responses, `onError` hooks, static file
  serving, route prefixes, CORS support, 405 responses with `Allow` headers,
  and graceful shutdown.
- Server tests and README documentation for LSP extension setup.

## 2026-04-26

### Added

- Language Server Protocol server with diagnostics, hover, completion,
  goto-definition, and document symbols.
- `Array` and `Matrix` numerics backed by Gonum.
- `Server` web framework.
- `Iter` combinators.
- `LruCache` and `TtlCache`.
- RFC 4122 UUID v4/v7 generation.
- Constants.
- Function types.
- `Result`-based error handling with the `?` propagation operator.
- `do { } catch { }` blocks.

## 2026-04-25

### Added

- Semantic checker for undefined names, duplicate class members, and misplaced
  flow statements.
- `--no-semantic-check` compiler flag.
- Semantic checker test suite.

## 2026-04-24

### Added

- Comprehensive README and project documentation.
- `docs/TRANSPILATION.md` with Lammergeier-to-Go lowering rules.
- Documentation for multiline expressions, `defer`, `isinstance`, safe
  navigation operators, tuple destructuring, and generics.
- Library cache system with source maps.
- Transpilation-output regression test suite.
- Project icon and initial build-artifact ignore rules.

## 2026-04-23

### Added

- Initial Lammergeier Lang repository structure.
- Compiler, grammar, standard library, examples, tests, website, and editor
  support directories.
