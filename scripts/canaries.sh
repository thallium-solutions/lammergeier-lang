#!/bin/sh
# Canary builds: the downstream projects (Luxyt, managed-file-transfer and the
# official lam-* packages) build with the working-tree compiler
# (tests/tests/test_canaries.py, docs/SAFETY.md). Nothing runs, no service is
# needed; binaries go to a temporary directory and the projects are left as
# they were (their `git status` is checked).
#
#   sh scripts/canaries.sh                         # every project found
#   sh scripts/canaries.sh --only luxyt -v         # one project, every build
#   LAM_CANARY_LUXYT=~/src/luxyt/server sh scripts/canaries.sh
#
# Checkouts: LAM_CANARY_LUXYT, LAM_CANARY_MFT, LAM_CANARY_PACKAGES (the
# directory of the lam-* packages); an absent one is skipped with a note.
set -eu

ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"

exec "$PYTHON" "$ROOT/tests/tests/test_canaries.py" "$@"
