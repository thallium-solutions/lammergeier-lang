#!/usr/bin/env bash
# ============================================================
# Lammergeier Lang — website/publish.sh
#
# Publishes the website to bunny.net: replaces the content of the storage
# zone with the site files, then purges the pull zone (CDN) cache.
#
#   ./website/build.sh              # refresh content/ from docs/ first
#   ./website/publish.sh            # delete, upload, purge
#   ./website/publish.sh --dry-run  # show what would happen
#
# Published: assets/, content/, index.html, robots.txt, sitemap.xml.
# Everything else in the storage zone is deleted first.
#
# The bunny.net account API key is read from BUNNY_API_KEY or from
# website/.bunny-token (git-ignored, mode 600). It never appears on a
# command line: curl reads it from a private temporary header file.
#
# Requires bash, curl, python3 (JSON parsing) and sha256sum.
# ============================================================

set -euo pipefail

STORAGE_ZONE="lammergeier-lang-website-production"
PULL_ZONE="production-lammergeier-lang"
API="https://api.bunny.net"
PUBLISHED_DIRS=(assets content)
PUBLISHED_FILES=(index.html robots.txt sitemap.xml)

WEB="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "${WEB}/.." && pwd)"
TOKEN_FILE="${WEB}/.bunny-token"

DRY_RUN=0
for arg in "$@"; do
    case "${arg}" in
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "error: unknown argument ${arg}" >&2; exit 2 ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }

for tool in curl python3 sha256sum; do
    command -v "${tool}" >/dev/null || die "${tool} is required"
done

# ── API key ─────────────────────────────────────────────────
KEY="${BUNNY_API_KEY:-}"
if [[ -z "${KEY}" ]]; then
    [[ -f "${TOKEN_FILE}" ]] || die "no API key: set BUNNY_API_KEY or create ${TOKEN_FILE}"
    if git -C "${ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
        git -C "${ROOT}" check-ignore -q "${TOKEN_FILE}" || die "${TOKEN_FILE} is not git-ignored; refusing to use it"
        if git -C "${ROOT}" ls-files --error-unmatch "${TOKEN_FILE}" >/dev/null 2>&1; then
            die "${TOKEN_FILE} is tracked by git; remove it from the index and rotate the key"
        fi
    fi
    if [[ "$(stat -c %a "${TOKEN_FILE}")" != "600" && "$(stat -c %a "${TOKEN_FILE}")" != "400" ]]; then
        chmod 600 "${TOKEN_FILE}"
        echo "note: ${TOKEN_FILE} was readable by others; permissions set to 600" >&2
    fi
    KEY="$(tr -d '[:space:]' < "${TOKEN_FILE}")"
fi
[[ -n "${KEY}" ]] || die "the API key is empty"

TMP="$(mktemp -d)"
chmod 700 "${TMP}"
trap 'rm -rf "${TMP}"' EXIT
printf 'AccessKey: %s\n' "${KEY}" > "${TMP}/account.hdr"
unset KEY

# ── Files to publish ────────────────────────────────────────
cd "${WEB}"
for dir in "${PUBLISHED_DIRS[@]}"; do
    [[ -d "${dir}" ]] && [[ -n "$(find "${dir}" -type f -print -quit)" ]] || die "website/${dir}/ is missing or empty"
done
for file in "${PUBLISHED_FILES[@]}"; do
    [[ -f "${file}" ]] || die "website/${file} is missing"
done
# content/ must be current: build.sh copies these sources.
stale=()
for pair in SYNTAX.md:docs/SYNTAX.md TRANSPILATION.md:docs/TRANSPILATION.md stdlib.md:docs/stdlib.md \
            libraries.md:docs/libraries.md server_plugins.md:docs/server_plugins.md \
            third_party_libraries.md:docs/third_party_libraries.md installation.md:docs/installation.md \
            package_manager.md:docs/package_manager.md README.md:README.md CONTRIBUTING.md:CONTRIBUTING.md; do
    copy="content/${pair%%:*}"
    source="${ROOT}/${pair#*:}"
    if [[ -f "${source}" && ( ! -f "${copy}" || "${source}" -nt "${copy}" ) ]]; then
        stale+=("${copy}")
    fi
done
(( ${#stale[@]} == 0 )) || die "older than their sources: ${stale[*]} (run ./website/build.sh first)"

mapfile -t UPLOADS < <( { find "${PUBLISHED_DIRS[@]}" -type f ! -name '.*'; printf '%s\n' "${PUBLISHED_FILES[@]}"; } | LC_ALL=C sort )

# ── bunny.net helpers ───────────────────────────────────────
# api METHOD PATH: account API (JSON on stdout).
api() {
    curl -fsS --retry 3 --retry-delay 2 -X "$1" -H @"${TMP}/account.hdr" -H "Accept: application/json" "${API}$2"
}

# Exact-name lookup: the API's ``search`` is a substring match over every
# zone of the account.
zone_field() { # JSON on stdin; name, field
    python3 -c '
import json, sys
data = json.load(sys.stdin)
items = data.get("Items", []) if isinstance(data, dict) else data
found = [z for z in items if z.get("Name") == sys.argv[1]]
if len(found) != 1:
    sys.exit(f"expected exactly one zone named {sys.argv[1]!r}, found {len(found)}")
print(found[0].get(sys.argv[2], ""))
' "$1" "$2"
}

echo "→ resolving zones"
zones="$(api GET "/storagezone?page=1&perPage=1000&search=${STORAGE_ZONE}")"
STORAGE_ID="$(zone_field "${STORAGE_ZONE}" Id <<< "${zones}")"
zone="$(api GET "/storagezone/${STORAGE_ID}")"
STORAGE_HOST="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("StorageHostname") or "storage.bunnycdn.com")' <<< "${zone}")"
python3 -c 'import json,sys; print("AccessKey: " + json.load(sys.stdin)["Password"])' <<< "${zone}" > "${TMP}/storage.hdr"
unset zone
pulls="$(api GET "/pullzone?page=1&perPage=1000&search=${PULL_ZONE}")"
PULL_ID="$(zone_field "${PULL_ZONE}" Id <<< "${pulls}")"
PULL_STORAGE="$(zone_field "${PULL_ZONE}" StorageZoneId <<< "${pulls}")"
# Safety: never delete a bucket the CDN zone does not serve.
[[ "${PULL_STORAGE}" == "${STORAGE_ID}" ]] || die "pull zone ${PULL_ZONE} serves storage zone ${PULL_STORAGE}, not ${STORAGE_ZONE} (${STORAGE_ID})"
echo "  storage zone ${STORAGE_ZONE} (${STORAGE_ID}, ${STORAGE_HOST}) behind pull zone ${PULL_ZONE} (${PULL_ID})"

STORAGE="https://${STORAGE_HOST}/${STORAGE_ZONE}"

# ── 1. Delete the current content ──────────────────────────
echo "→ deleting the current files"
existing="$(curl -fsS --retry 3 -H @"${TMP}/storage.hdr" -H "Accept: application/json" "${STORAGE}/")"
mapfile -t ENTRIES < <(python3 -c '
import json, sys
for e in json.load(sys.stdin):
    print(e["ObjectName"] + ("/" if e["IsDirectory"] else ""))
' <<< "${existing}")
for entry in "${ENTRIES[@]}"; do
    [[ -n "${entry}" ]] || continue
    if (( DRY_RUN )); then
        echo "  would delete ${entry}"
        continue
    fi
    # A trailing slash deletes a folder with everything in it.
    curl -fsS --retry 3 -o /dev/null -X DELETE -H @"${TMP}/storage.hdr" "${STORAGE}/${entry}"
    echo "  deleted ${entry}"
done

# ── 2. Upload ──────────────────────────────────────────────
echo "→ uploading ${#UPLOADS[@]} files"
for file in "${UPLOADS[@]}"; do
    if (( DRY_RUN )); then
        echo "  would upload ${file}"
        continue
    fi
    # bunny.net verifies the SHA-256 and refuses a corrupted upload.
    checksum="$(sha256sum "${file}" | cut -d' ' -f1 | tr '[:lower:]' '[:upper:]')"
    curl -fsS --retry 3 -o /dev/null -X PUT -H @"${TMP}/storage.hdr" \
        -H "Checksum: ${checksum}" -H "Content-Type: application/octet-stream" \
        --data-binary @"${file}" "${STORAGE}/${file}"
    echo "  uploaded ${file}"
done

# ── 3. Purge the CDN cache ─────────────────────────────────
if (( DRY_RUN )); then
    echo "→ would purge the cache of ${PULL_ZONE}"
    echo "dry run: nothing changed"
    exit 0
fi
echo "→ purging the cache of ${PULL_ZONE}"
api POST "/pullzone/${PULL_ID}/purgeCache" > /dev/null

echo
echo "✓ website published: ${#UPLOADS[@]} files, cache purged"
