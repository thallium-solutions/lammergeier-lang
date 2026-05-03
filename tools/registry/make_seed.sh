#!/usr/bin/env bash
# Regenerate the seed tarballs served on first boot by the local
# registry Docker image. Walks ``tools/registry/seed/src/*/`` and
# produces one ``.tar.gz`` per library directory into the parent
# ``seed/`` directory. The script is idempotent — re-running it
# overwrites the existing archives.
#
# The directories under ``src/`` use an ``<alias>-<version>``
# naming convention where ``<alias>`` is the scoped-name-safe
# alias (``@acme/lamcolor`` → ``@acme__lamcolor``). We strip that
# alias back when writing the tarball filename so the server's
# own flattening (see ``tools/registry/server.py``) matches and
# the index entry the server builds is consistent with what ``lamc
# install`` expects.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
src_dir="$here/seed/src"
out_dir="$here/seed"

if [[ ! -d "$src_dir" ]]; then
    echo "no src directory at $src_dir" >&2
    exit 1
fi

shopt -s nullglob
built=0
for d in "$src_dir"/*; do
    [[ -d "$d" ]] || continue
    base="$(basename "$d")"
    # Drop the trailing ``-<version>`` suffix to obtain the alias,
    # then recover the human tarball name by prepending ``<alias>-``.
    tar_name="${base}.tar.gz"
    tar_path="$out_dir/$tar_name"
    tar -czf "$tar_path" -C "$src_dir" "$base"
    echo "  built $tar_name"
    built=$((built + 1))
done
if (( built == 0 )); then
    echo "no library directories found under $src_dir" >&2
    exit 1
fi
echo "done: $built tarball(s) in $out_dir"
