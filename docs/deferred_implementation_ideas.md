# Deferred implementation ideas

These are useful follow-ups that were considered during the TODO 9.2 `lamc doctor` work but were not implemented in that pass.

## `lamc doctor` follow-ups

- **Machine-readable output:** add `lamc doctor --json` for scripts and release tooling.
- **Strict mode:** add `lamc doctor --strict` that exits non-zero when Go, `lark`, the stdlib, or `lammergeier-lsp` are unavailable.
- **Version constraints:** compare Python, Go, `lark`, Node, npm, and VS Code extension versions against documented minimums.
- **Project discovery:** detect the nearest `lamlib.toml` from the current directory and report the active package root separately from the compiler checkout root.
- **Cache diagnostics:** report parser/library cache size, package-install cache size, and stale cache entries.
- **Extension diagnostics:** inspect VS Code, Cursor, and Windsurf extension manifests to report whether the Lammergeier extension is installed and registered.
- **PATH diagnostics:** explain which directories were searched when `lamc`, `go`, or `lammergeier-lsp` are missing.
- **Go environment details:** include `GOMODCACHE`, `GOCACHE`, `GOOS`, `GOARCH`, and selected `go env` values.
- **Dependency health:** optionally check whether `requirements.txt` is satisfied without installing anything.
- **Release integration:** call `lamc doctor` from `docs/release_checklist.md` once strict or JSON mode exists.

## Release hygiene follow-ups

- **Automated release script:** add a non-publishing `scripts/release_check.sh` that runs the documented checklist commands.
- **Changelog validator:** check that `CHANGELOG.md` contains an entry for the version being tagged.
- **Tag guard:** verify the intended tag does not already exist locally or remotely.
- **Fresh checkout smoke test:** automate the post-release clone/install/hello-world verification in a temporary directory.
