# Lammergeier Agent Conventions

This document captures project conventions for AI assistants working on `lammergeier-lang`.

## TODO.md workflow

- When the user asks to continue work from `TODO.md`, handle tasks **point-by-point** from the current TODO position.
- Follow the instructions written in `TODO.md` for each point.
- **Stage modifications with `git` after each completed point** before starting the next point.

## Package manager state

- `lamlib.toml` is the human-edited manifest.
- `lamlib.lock.toml` is the committed resolution.
- Direct git URL installs are pinned in `lamlib.lock.toml` but are **not** auto-written back into `[dependencies]`.
- Persistent forks/branches should use a normal `[dependencies]` entry plus a project-level `[replace]` entry.
- `library.license` is optional in the manifest parser but strongly recommended for public releases.

## Documentation and website mirroring

- Keep `website/content/` in sync with `docs/` content.
- Avoid unrelated website route-link edits unless the user explicitly requests them.

## Third-party packages

- `third_party/` contains vendored-style packages (`lams3`, `lamstripe`, `lamotel`).
- Keep stdlib boundaries clear: use Lam stdlib APIs (`lamenv`, `lamhttp`, etc.) where possible; limit raw `go!` blocks to SDK/client construction and unavoidable operations.
- Never commit real S3 credentials or echo them into command logs/files. Live tests are guarded and should not run without explicit credentials.

## Testing

- Follow the test-first workflow in `CONTRIBUTING.md` and never weaken a valid test to hide an implementation defect.
- Every feature set requires representative unit/semantic cases plus a cross-feature integration or stress fixture that exercises both the software and the Lam language constructs composing it; external packages must test through the installed-package boundary.
- Run syntax diagnostics tests with `/usr/bin/python3 tests/syntax/run_syntax_tests.py --verbose` because the project-local `.venv` Python currently lacks `lark`, while `/usr/bin/python3` has `lark 1.3.1`.
- Benchmarks: run them through `sh scripts/bench.sh compiler|runtime`, which also writes the output to `benchmarks/results/`; commit that file (never run a benchmark without keeping its output).
- Common verification commands: `git diff --check` and `PYTHON=/usr/bin/python3 sh scripts/test.sh <suites>`.
