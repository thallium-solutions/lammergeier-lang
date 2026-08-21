# Lammergeier Roadmap

This file collects deferred work that is still referenced across the docs.
It is not a release promise; it is a practical backlog of features whose
syntax, command shape, or operational behavior is already hinted at in the
documentation.

## Package and Library Workflows

### Workspaces

`[workspace]` is reserved today and rejected by the manifest parser. The future
feature would support multi-package repositories without forcing each package
to duplicate dependency and tooling configuration.

Possible manifest shape:

```toml
[workspace]
members = ["apps/api", "apps/worker", "libs/shared"]

[workspace.dependencies]
lamhttp = "^1.4"
lamjson = "^0.8"
```

Implementation notes:

- Define package-root discovery when multiple `lamlib.toml` files are present.
- Decide whether each member keeps its own lockfile or the workspace has one
  shared lockfile.
- Teach `lamc install`, `tidy`, `verify`, `list`, `tree`, and `why` to operate
  on either one package or the workspace.
- Ensure member-local dependencies can override workspace-level defaults
  without surprising conflict resolution.

## Release Hygiene

### `scripts/release_check.sh`

The release checklist is currently manual. A script should run the documented
non-publishing checks in the right order.

Expected command:

```bash
sh scripts/release_check.sh
```

Implementation notes:

- Run `git status --short`, `git diff --check`, tests, benchmarks, installer
  smoke tests, and extension build checks.
- Print each command before running it.
- Exit on the first failing command unless a `--keep-going` flag is provided.

### Changelog validator

Before tagging a release, tooling should verify that `CHANGELOG.md` contains an
entry for the intended version or release date.

Expected command:

```bash
python3 scripts/check_changelog.py v0.5.0
```

Implementation notes:

- Accept either version headings or date headings while the project is not fully
  version-tagged.
- Fail with a clear message when no matching entry is found.

### Tag guard

Release tooling should verify that the intended Git tag does not already exist
locally or remotely.

Expected command:

```bash
python3 scripts/check_tag.py v0.5.0
```

Implementation notes:

- Check `git rev-parse --verify refs/tags/<tag>` locally.
- Check `git ls-remote --tags origin <tag>` remotely.
- Print the existing commit when a collision is found.

### Fresh checkout smoke test

Post-release verification should clone the tagged repository into a temporary
directory, install from that checkout, and compile a hello-world program.

Implementation notes:

- Use a temporary install prefix so the user's active `lamc` is not replaced.
- Run `./install.sh --prefix "$tmpdir/prefix" --no-editor`.
- Run the installed `lamc` against `tests/rosetta_tests/hello_world.lam`.
- Clean up the temporary clone and install prefix on success.
