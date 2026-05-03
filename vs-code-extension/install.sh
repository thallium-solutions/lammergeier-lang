#!/usr/bin/env bash
# Install the Lammergeier Lang VS Code / Windsurf / Cursor extension
# by symlinking it into the editor's extensions directory AND
# registering it in the editor's ``extensions.json`` manifest so the
# editor actually loads it.
#
# Usage:
#   ./install.sh           # install into every editor detected
#   ./install.sh vscode    # install only into VS Code
#   ./install.sh windsurf  # install only into Windsurf
#   ./install.sh cursor    # install only into Cursor
#
# Why register in ``extensions.json``?
#   Windsurf (VS Code fork) still auto-scans ``$HOME/.windsurf/extensions/``
#   for new folders, but **vanilla VS Code has stopped doing that** — it
#   only loads what's listed in ``extensions.json``. Dropping a folder
#   into the extensions dir without a manifest entry silently does
#   nothing, which is the exact bug users hit when syntax highlighting
#   and the LSP refuse to activate.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXT_DIR="$HERE/lammergeier-lang"

if [[ ! -f "$EXT_DIR/package.json" ]]; then
    echo "error: extension package.json not found at $EXT_DIR/package.json" >&2
    exit 1
fi

# Pull publisher / name / version / engine from package.json. The
# ``engine`` goes into the manifest metadata so VS Code doesn't
# immediately flag the extension as incompatible.
read PUBLISHER NAME VERSION ENGINE <<<"$(python3 -c '
import json, pathlib
m = json.loads(pathlib.Path("'"$EXT_DIR/package.json"'").read_text())
print(m["publisher"], m["name"], m["version"], m.get("engines", {}).get("vscode", "*"))
')"

ID="${PUBLISHER}.${NAME}"
FOLDER_BASE="${ID}-${VERSION}"

build_client_if_possible() {
    if ! command -v npm >/dev/null 2>&1; then
        echo "[warn] npm not found; syntax highlighting will work, but the LSP client needs out/extension.js"
        return
    fi
    if [[ ! -d "$EXT_DIR/node_modules" ]]; then
        echo "[info] installing extension dependencies"
        (cd "$EXT_DIR" && npm install)
    fi
    echo "[info] building extension client"
    (cd "$EXT_DIR" && npm run build)
}

# ─── extensions.json registration ──────────────────────────────
# VS Code stores installed extensions in a JSON array keyed by
# identifier. We rebuild the array with a freshly-constructed entry
# for our extension, preserving everything else. The Python helper
# keeps the edit atomic: write to a temp file, then rename, so a
# crash midway can't corrupt the manifest.
register_in_manifest() {
    local manifest="$1"
    local rel_location="$2"

    mkdir -p "$(dirname "$manifest")"

    python3 - "$manifest" "$ID" "$VERSION" "$rel_location" "$(dirname "$manifest")" "$ENGINE" <<'PY'
import json
import os
import pathlib
import sys
import time

manifest_path = pathlib.Path(sys.argv[1])
ext_id        = sys.argv[2]
version       = sys.argv[3]
rel_location  = sys.argv[4]
ext_root      = pathlib.Path(sys.argv[5])
engine        = sys.argv[6]

abs_path = str((ext_root / rel_location).resolve())

# Load existing manifest (array of entries). Missing / empty / corrupt
# files start fresh — VS Code regenerates them on launch anyway.
try:
    data = json.loads(manifest_path.read_text())
    if not isinstance(data, list):
        data = []
except (FileNotFoundError, json.JSONDecodeError):
    data = []

# Drop every prior entry for our extension (any version) so the user
# doesn't accumulate stale 0.1.0 / 0.2.0 / … siblings over time.
data = [e for e in data if e.get("identifier", {}).get("id") != ext_id]

entry = {
    "identifier": {"id": ext_id},
    "version": version,
    "location": {
        "$mid": 1,
        "path": abs_path,
        "scheme": "file",
    },
    "relativeLocation": rel_location,
    "metadata": {
        "installedTimestamp": int(time.time() * 1000),
        "source": "resource",
        "targetPlatform": "undefined",
        "size": 0,
        "pinned": True,
        "engine": engine,
    },
}
data.append(entry)

tmp = manifest_path.with_suffix(".json.tmp")
tmp.write_text(json.dumps(data, indent=0))
os.replace(tmp, manifest_path)
PY
}

# ─── obsolete stale folders ────────────────────────────────────
cleanup_old_folders() {
    local base_dir="$1"
    local suffix="$2"

    shopt -s nullglob
    for existing in "$base_dir/${ID}-"*; do
        if [[ -z "$suffix" || "$existing" == *"$suffix" ]]; then
            rm -rf "$existing" 2>/dev/null || true
        fi
    done
    # Legacy folders from earlier installers that used a bare name.
    rm -rf "$base_dir/lammergeier-lang" 2>/dev/null || true
    shopt -u nullglob
}

install_into() {
    local editor_name="$1"
    local base_dir="$2"
    local suffix="$3"  # "" for vscode / cursor, "-universal" for windsurf
    local rel_folder="${FOLDER_BASE}${suffix}"
    local target="$base_dir/${rel_folder}"

    if [[ ! -d "$base_dir" ]]; then
        echo "[skip] $editor_name: $base_dir does not exist"
        return
    fi

    cleanup_old_folders "$base_dir" "$suffix"

    ln -sfn "$EXT_DIR" "$target"
    register_in_manifest "$base_dir/extensions.json" "$rel_folder"

    echo "[ok]   $editor_name: $target -> $EXT_DIR (registered in extensions.json)"
}

targets="${1:-all}"

build_client_if_possible

if [[ "$targets" == "all" || "$targets" == "vscode" ]]; then
    install_into "VS Code"  "$HOME/.vscode/extensions"             ""
fi
if [[ "$targets" == "all" || "$targets" == "windsurf" ]]; then
    install_into "Windsurf" "$HOME/.windsurf/extensions"           "-universal"
fi
if [[ "$targets" == "all" || "$targets" == "cursor" ]]; then
    install_into "Cursor"   "$HOME/.cursor/extensions"             ""
fi

echo
echo "Reload your editor window (Command Palette → 'Developer: Reload Window')"
echo "and open any .lam file to verify highlighting + LSP activation."
