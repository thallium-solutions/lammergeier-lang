# Deferred implementation ideas

These are useful follow-ups that are not concrete enough for the main roadmap
yet, but are worth keeping visible.

## Release hygiene follow-ups

- **Automated release script:** add a non-publishing `scripts/release_check.sh` that runs the documented checklist commands.
- **Changelog validator:** check that `CHANGELOG.md` contains an entry for the version being tagged.
- **Tag guard:** verify the intended tag does not already exist locally or remotely.
- **Fresh checkout smoke test:** automate the post-release clone/install/hello-world verification in a temporary directory.
