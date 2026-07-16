# Release checklist

Use this checklist for manual Lammergeier releases while the project does not have automated CI.

## 1. Confirm the release commit

- **Review the tree:** `git status --short`
- **Require a clean tree:** continue only when all intended changes are committed and there are no unexpected untracked files.
- **Review recent history:** `git log --oneline -n 10`

## 2. Install dependencies

- **Python dependency:** `python3 -m pip install -r requirements.txt`
- **Go toolchain:** `go version`
- **Node tooling for the editor extension:** from `vs-code-extension/lammergeier-lang`, run `npm install` if `node_modules` is missing or stale.

## 3. Run the regression suite

Confirm the local toolchain first:

```bash
lamc doctor --strict
```

```bash
PYTHON=python3 sh scripts/test.sh
```

The release should not proceed unless every suite passes.

## 4. Run benchmarks

```bash
PYTHON=python3 sh scripts/bench.sh
```

Record any notable regressions in the release notes before continuing.

## 5. Smoke-test the installer

Use a temporary prefix so the smoke test does not replace the user's active installation:

```bash
tmpdir=$(mktemp -d)
./install.sh --prefix "$tmpdir/lammergeier" --no-editor
"$tmpdir/lammergeier/bin/lamc" tests/rosetta_tests/hello_world.lam --run
"$tmpdir/lammergeier/bin/lammergeier-lsp" --help >/dev/null || true
rm -rf "$tmpdir"
```

Also run the safe command-level smoke test:

```bash
sh scripts/smoke.sh
```

## 6. Smoke-test the VS Code extension

From `vs-code-extension/lammergeier-lang`:

```bash
npm install
npm run build
```

Then install the extension into one local editor and verify it manually:

```bash
./vs-code-extension/install.sh vscode
```

- **Reload the editor:** run `Developer: Reload Window`.
- **Open a `.lam` file:** verify syntax highlighting activates.
- **Check LSP startup:** verify hover, completion, and diagnostics work on a small Lam file.
- **Check settings:** if `lammergeier-lsp` is not on `PATH`, set `lammergeier.lsp.path` to the built launcher path.

## 7. Update the changelog

- **Edit `CHANGELOG.md`:** add the release date, notable additions, fixes, tests, and breaking changes.
- **Verify docs links:** check that changed documentation paths still exist.
- **Review final diff:** `git diff --stat` and `git diff --check`.

## 8. Tag the release

After the release commit is created and verified:

```bash
git tag -a vX.Y.Z -m "Lammergeier vX.Y.Z"
git push origin main
git push origin vX.Y.Z
```

Replace `vX.Y.Z` with the release version.

## 9. Post-release check

- **Fresh checkout:** clone or update a separate checkout at the pushed tag.
- **Install from tag:** run `./install.sh --user --no-editor` or the intended installation mode.
- **Compile hello world:** `lamc tests/rosetta_tests/hello_world.lam --run`.
- **Confirm editor extension:** open a `.lam` file in the target editor and confirm the extension activates.
