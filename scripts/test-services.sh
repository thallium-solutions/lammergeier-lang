#!/bin/sh
set -eu

ROOT=$(CDPATH= cd "$(dirname "$0")/.." && pwd)

STARTED=""

usage() {
    cat <<'EOF'
Usage:
  sh scripts/test-services.sh

Starts the Docker services required by the full Lammergeier test suite:
Postgres, Redis without auth, Redis with auth, memcached, and the memcached
auth fixture. Containers are started with docker run --rm and are stopped when
this script exits.
EOF
}

case "${1:-}" in
    -h|--help)
        usage
        exit 0
        ;;
    "")
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "error: docker is not installed or not on PATH" >&2
        exit 1
    fi
}

container_exists() {
    docker container inspect "$1" >/dev/null 2>&1
}

start_container() {
    name=$1
    shift

    if container_exists "$name"; then
        echo "[test-services] $name already exists; leaving it running"
        return 0
    fi

    echo "[test-services] starting $name"
    docker run --rm -d --name "$name" "$@" >/dev/null
    STARTED="$STARTED $name"
}

cleanup() {
    if [ -z "$STARTED" ]; then
        return
    fi
    echo
    echo "[test-services] stopping containers:$STARTED"
    # shellcheck disable=SC2086
    docker stop $STARTED >/dev/null 2>&1 || true
    STARTED=""
}

require_docker
trap cleanup EXIT INT TERM

start_container postgres-test \
    -e POSTGRES_USER=postgres \
    -e POSTGRES_PASSWORD=Password \
    -e POSTGRES_DB=postgres \
    -p 5432:5432 \
    postgres:16.9-alpine3.22

start_container redis-test \
    -p 6380:6379 \
    redis:latest

start_container redis-test-auth \
    -p 6379:6379 \
    redis:latest \
    redis-server --requirepass Password

start_container memcached-test \
    -p 11211:11211 \
    memcached:latest

start_container memcached-test-auth \
    -p 11212:11211 \
    -e MEMCACHED_USERNAME=memuser \
    -e MEMCACHED_PASSWORD=mempass123 \
    memcached:1.6

cat <<EOF

[test-services] services are starting.
[test-services] In another terminal, run:

  cd "$ROOT"
  sh scripts/test.sh

[test-services] Press Ctrl-C here to stop containers started by this script.
EOF

while :; do
    sleep 3600 &
    wait $!
done
