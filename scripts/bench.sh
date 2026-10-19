#!/bin/sh
# Benchmarks (not tests): see benchmarks/README.md.
#   sh scripts/bench.sh compiler [options]   how fast lamc compiles
#   sh scripts/bench.sh runtime  [options]   how fast the generated code runs
# The output is also written to benchmarks/results/<suite>-<UTC time>-<version>-<commit>.txt
# (commit it with the change it measures); BENCH_SAVE=0 only prints.
set -eu

ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
suite=${1:-}
[ $# -gt 0 ] && shift

cd "$ROOT"
case "$suite" in
    compiler) runner=benchmarks/compiler/run_compiler_bench.py ;;
    runtime) runner=benchmarks/runtime/run_runtime_bench.py ;;
    *)
        echo "usage: sh scripts/bench.sh compiler|runtime [options]" >&2
        exit 2
        ;;
esac

if [ "${BENCH_SAVE:-1}" = "0" ]; then
    exec "$PYTHON" "$runner" "$@"
fi

version=$(sed -n 's/^LAMC_VERSION = "\(.*\)"/\1/p' compiler/version.py)
commit=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)
# "+dirty": uncommitted changes in what is measured (compiler, stdlib, runtime, benchmarks).
dirty=$(git status --porcelain --untracked-files=no -- compiler lib benchmarks 2>/dev/null | grep -q . && echo "+dirty" || true)
stamp=$(date -u +%Y%m%d-%H%M%S)
mkdir -p benchmarks/results
out="benchmarks/results/$suite-$stamp-$version-$commit$dirty.txt"
status_file=$(mktemp)
{
    echo "# lamc $version ($commit$dirty), $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "# command: sh scripts/bench.sh $suite $*"
    echo "# host: $(uname -srm); cpu: $(sed -n 's/^model name[[:space:]]*: //p' /proc/cpuinfo 2>/dev/null | head -1); load: $(cut -d' ' -f1-3 /proc/loadavg 2>/dev/null)"
    echo "# go: $(go version 2>/dev/null | cut -d' ' -f3)"
    echo
    { "$PYTHON" "$runner" "$@"; echo $? > "$status_file"; } 2>&1
} | tee "$out"
status=$(cat "$status_file")
rm -f "$status_file"
echo "results: $out" >&2
exit "$status"
