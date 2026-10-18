#!/bin/sh
set -eu

ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}

cd "$ROOT"
exec "$PYTHON" compiler/lammergeier.py tests/tests/cases/rosetta/test_hello_world.lam --run
