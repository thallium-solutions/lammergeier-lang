#!/bin/sh
# Fuzzing campaigns of the Lam compiler (tests/fuzz/lamfuzz.py, docs/SAFETY.md).
#
#   sh scripts/fuzz.sh                 # every mode, 300 cases from seed 1
#   sh scripts/fuzz.sh gen 2000 5000   # mode, count, first seed
#   JOBS=16 sh scripts/fuzz.sh all 5000
#
# Failing cases (shrunk) are written to ${FUZZ_OUT:-fuzz-failures/}: turn each
# into a regression test (a semantic case or tests/tests/cases/safety/) before
# fixing it.
set -eu

ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"
MODE="${1:-all}"
COUNT="${2:-300}"
SEED="${3:-1}"
JOBS="${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)}"

exec "$PYTHON" "$ROOT/tests/fuzz/lamfuzz.py" --mode "$MODE" --count "$COUNT" --seed "$SEED" \
    --jobs "$JOBS" --out "${FUZZ_OUT:-$ROOT/fuzz-failures}"
