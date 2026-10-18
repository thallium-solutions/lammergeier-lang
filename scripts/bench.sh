#!/bin/sh
# Benchmarks (not tests): see benchmarks/README.md.
#   sh scripts/bench.sh compiler [options]   how fast lamc compiles
#   sh scripts/bench.sh runtime  [options]   how fast the generated code runs
set -eu

ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
suite=${1:-}
[ $# -gt 0 ] && shift

cd "$ROOT"
case "$suite" in
    compiler) exec "$PYTHON" benchmarks/compiler/run_compiler_bench.py "$@" ;;
    runtime) exec "$PYTHON" benchmarks/runtime/run_runtime_bench.py "$@" ;;
    *)
        echo "usage: sh scripts/bench.sh compiler|runtime [options]" >&2
        exit 2
        ;;
esac
