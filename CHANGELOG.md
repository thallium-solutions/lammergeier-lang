# Changelog

All notable changes to Lammergeier Lang are documented here.

This repository does not currently use version tags, so entries are grouped by
commit date from the Git history and summarized against the current README and
documentation.

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

### Tests

- Added focused syntax, semantic, and import-resolution diagnostic test suites
  covering negative and positive/fallback cases for the new Lam-side error
  messages.

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
