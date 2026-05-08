#!/bin/sh
set -eu

ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}

cd "$ROOT"
exec "$PYTHON" tests/benchmarks/run_benchmarks.py "$@"
