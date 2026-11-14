#!/bin/sh
# Runtime tests under the Go race detector (docs/SAFETY.md "How the contract
# is checked"): a representative subset of the core cases (async tasks,
# generators, channels and locks, markup sharing, run-time faults, and the
# stdlib modules with shared state: context, cache, rate limit, retry, regexp
# cache, signals, atomics, the HTTP client and server, lamdb on SQLite) is
# built with `lamc --go-race` (tests/tests/run_tests.py --race) and run; a
# reported data race fails its test.
#
#   sh scripts/race.sh                  # the subset below
#   sh scripts/race.sh generator_close  # instead, the core cases matching a filter
#
# Needs cgo and a C compiler (the race detector's runtime); skipped otherwise.
set -eu

ROOT="$(cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="${PYTHON:-/usr/bin/python3}"

if [ "$(go env CGO_ENABLED 2>/dev/null)" != "1" ] || ! command -v "$(go env CC 2>/dev/null | cut -d' ' -f1 || echo gcc)" >/dev/null 2>&1; then
    echo "SKIP: the Go race detector needs cgo and a C compiler"
    exit 0
fi

# Paths below tests/tests/cases/ (any file whose path contains one of them).
SUBSET="
concurrency/
generators/
markup/
safety/
misc/test_go_sync
classes/test_private_async
errors/test_do_catch_return
integration/test_int_crawler
integration/test_int_shop_cross_feature
stress/test_stress_runtime_orchestration
stdlib/test_stdlib_atomic
stdlib/test_stdlib_cache
stdlib/test_stdlib_context
stdlib/test_stdlib_ratelimit
stdlib/test_stdlib_retry
stdlib/test_stdlib_re_cache
stdlib/test_stdlib_os_signals
stdlib/test_stdlib_queue
stdlib/test_stdlib_lamcron
stdlib/test_stdlib_uuid
stdlib/test_stdlib_html_policy_reuse
stdlib/test_stdlib_http_client
stdlib/test_stdlib_http_multipart
stdlib/test_stdlib_http_server
stdlib/test_stdlib_markup_bodies
stdlib/test_db.lam
stdlib/test_stdlib_db
"

ARGS=""
if [ -n "${1:-}" ]; then
    ARGS="--filter $1"
else
    for path in $SUBSET; do
        ARGS="$ARGS --filter $path"
    done
fi

cd "$ROOT"
# shellcheck disable=SC2086
exec "$PYTHON" tests/tests/run_tests.py --race --dir tests/tests/cases --jobs "${JOBS:-8}" $ARGS
