#!/usr/bin/env bash
# ============================================================
# Lammergeier Lang — website/build.sh
#
# Copies the authoritative Markdown docs into website/content/
# so the single-page app can fetch them over HTTP without reaching
# outside the deploy root.
#
# Also rewrites a handful of relative links so they continue to
# resolve once the files live at a flat path:
#   - sibling docs   (``stdlib.md``)         → ``#/docs/stdlib``
#   - lib links      (``../lib/foo.lam``)    → absolute GitHub URL
#
# Run this once before you deploy:
#
#   ./website/build.sh
#
# The script is idempotent — re-running it overwrites stale copies.
# ============================================================

set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
WEB="${ROOT}/website"
DOCS="${ROOT}/docs"
CONTENT="${WEB}/content"

mkdir -p "${CONTENT}"

copy_doc() {
    local src="$1"    # absolute source path
    local dst="$2"    # absolute destination path
    if [[ ! -f "${src}" ]]; then
        echo "skip: ${src} (missing)" >&2
        return
    fi
    cp "${src}" "${dst}"
    echo "copied: $(basename "${dst}")"
}

# ── Copy source docs verbatim ───────────────────────────────
copy_doc "${DOCS}/SYNTAX.md"                "${CONTENT}/SYNTAX.md"
copy_doc "${DOCS}/TRANSPILATION.md"         "${CONTENT}/TRANSPILATION.md"
copy_doc "${DOCS}/stdlib.md"                "${CONTENT}/stdlib.md"
copy_doc "${DOCS}/server_plugins.md"        "${CONTENT}/server_plugins.md"
copy_doc "${DOCS}/third_party_libraries.md" "${CONTENT}/third_party_libraries.md"
copy_doc "${DOCS}/installation.md"          "${CONTENT}/installation.md"
copy_doc "${DOCS}/package_manager.md"       "${CONTENT}/package_manager.md"

# README / CONTRIBUTING live at the repo root.
copy_doc "${ROOT}/README.md"         "${CONTENT}/README.md"
copy_doc "${ROOT}/CONTRIBUTING.md"   "${CONTENT}/CONTRIBUTING.md"

# ── Rewrite cross-links so they resolve in-site ────────────
# Every pair below is a literal match → replacement. The order
# matters: longer, more specific matches first so prefixes don't
# shadow them.
#
# In-site doc links go through the SPA hash router; cross-repo
# file links (``lib/`` or ``compiler/``) become absolute GitHub
# URLs pointing at the main branch.
GITHUB_BASE="https://github.com/thallium-solutions/lammergeier-lang/blob/main"

rewrite_file() {
    local f="$1"

    # Sibling doc links (relative ``stdlib.md`` refs, ``SYNTAX.md`` etc.).
    sed -i \
        -e 's|](SYNTAX\.md)|](#/docs/syntax)|g' \
        -e 's|](SYNTAX\.md#|](#/docs/syntax?h=|g' \
        -e 's|](TRANSPILATION\.md)|](#/docs/transpilation)|g' \
        -e 's|](TRANSPILATION\.md#|](#/docs/transpilation?h=|g' \
        -e 's|](stdlib\.md)|](#/docs/stdlib)|g' \
        -e 's|](stdlib\.md#|](#/docs/stdlib?h=|g' \
        -e 's|](server_plugins\.md)|](#/docs/server_plugins)|g' \
        -e 's|](server_plugins\.md#|](#/docs/server_plugins?h=|g' \
        -e 's|](third_party_libraries\.md)|](#/docs/third_party)|g' \
        -e 's|](third_party_libraries\.md#|](#/docs/third_party?h=|g' \
        -e 's|](installation\.md)|](#/docs/installation)|g' \
        -e 's|](installation\.md#|](#/docs/installation?h=|g' \
        -e 's|](package_manager\.md)|](#/docs/package_manager)|g' \
        -e 's|](package_manager\.md#|](#/docs/package_manager?h=|g' \
        -e 's|](CONTRIBUTING\.md)|](#/docs/contributing)|g' \
        -e 's|](README\.md)|](#/docs/readme)|g' \
        -e 's|](docs/SYNTAX\.md)|](#/docs/syntax)|g' \
        -e 's|](docs/SYNTAX\.md#|](#/docs/syntax?h=|g' \
        -e 's|](docs/TRANSPILATION\.md)|](#/docs/transpilation)|g' \
        -e 's|](docs/TRANSPILATION\.md#|](#/docs/transpilation?h=|g' \
        -e 's|](docs/stdlib\.md)|](#/docs/stdlib)|g' \
        -e 's|](docs/stdlib\.md#|](#/docs/stdlib?h=|g' \
        -e 's|](docs/server_plugins\.md)|](#/docs/server_plugins)|g' \
        -e 's|](docs/server_plugins\.md#|](#/docs/server_plugins?h=|g' \
        -e 's|](docs/third_party_libraries\.md)|](#/docs/third_party)|g' \
        -e 's|](docs/third_party_libraries\.md#|](#/docs/third_party?h=|g' \
        -e 's|](docs/installation\.md)|](#/docs/installation)|g' \
        -e 's|](docs/installation\.md#|](#/docs/installation?h=|g' \
        -e 's|](docs/package_manager\.md)|](#/docs/package_manager)|g' \
        -e 's|](docs/package_manager\.md#|](#/docs/package_manager?h=|g' \
        "${f}"

    # The README top nav uses raw HTML links, not Markdown links.
    sed -i \
        -e 's|href="docs/SYNTAX\.md"|href="#/docs/syntax"|g' \
        -e 's|href="docs/stdlib\.md"|href="#/docs/stdlib"|g' \
        -e 's|href="docs/installation\.md"|href="#/docs/installation"|g' \
        -e 's|href="docs/package_manager\.md"|href="#/docs/package_manager"|g' \
        -e 's|href="docs/TRANSPILATION\.md"|href="#/docs/transpilation"|g' \
        -e 's|href="CONTRIBUTING\.md"|href="#/docs/contributing"|g' \
        "${f}"

    # Out-of-docs links that point into the repo tree get rerouted
    # to GitHub so they stay clickable from the deployed site.
    # Uses a different delimiter (``#``) to avoid tangling with
    # pipe-heavy regexes above.
    sed -i -E \
        -e "s#\]\((\.\./)+(lib/[^)\"]+)\)#](${GITHUB_BASE}/\2)#g" \
        -e "s#\]\((\.\./)+(compiler/[^)\"]+)\)#](${GITHUB_BASE}/\2)#g" \
        -e "s#\]\((\.\./)+(tests/[^)\"]+)\)#](${GITHUB_BASE}/\2)#g" \
        -e "s#\]\((\.\./)+(bin/[^)\"]+)\)#](${GITHUB_BASE}/\2)#g" \
        -e "s#\]\((\.\./)+(tools/[^)\"]+)\)#](${GITHUB_BASE}/\2)#g" \
        -e "s#\]\(\.\./(vs-code-extension/[^)\"]+)\)#](${GITHUB_BASE}/\1)#g" \
        -e "s#\]\(\.\./lib\)#](${GITHUB_BASE}/lib)#g" \
        "${f}"

    # README links use bare (non-prefixed) paths — e.g. ``lib/foo.lam``
    # or ``tests/tests/run_tests.py``. Map those to GitHub too.
    sed -i -E \
        -e "s#\]\((lib/[^)\"]+\.lam)\)#](${GITHUB_BASE}/\1)#g" \
        -e "s#\]\((tests/[^)\"]+)\)#](${GITHUB_BASE}/\1)#g" \
        -e "s#\]\((compiler/[^)\"]+)\)#](${GITHUB_BASE}/\1)#g" \
        -e "s#\]\((bin/[^)\"]+)\)#](${GITHUB_BASE}/\1)#g" \
        -e "s#\]\((tools/[^)\"]+)\)#](${GITHUB_BASE}/\1)#g" \
        -e "s#\]\((vs-code-extension/[^)\"]+)\)#](${GITHUB_BASE}/\1)#g" \
        -e "s#\]\((website/README\.md)\)#](${GITHUB_BASE}/\1)#g" \
        -e "s#\]\((website)\)#](${GITHUB_BASE}/\1)#g" \
        -e "s#\]\((lib)\)#](${GITHUB_BASE}/\1)#g" \
        "${f}"

    # ``images/...`` references (README) — pick them up from our
    # local ``assets/images/`` copy so the site stays self-contained.
    sed -i -E \
        -e 's#(src=")images/([^"]+)#\1assets/images/\2#g' \
        -e 's#\]\(images/([^)]+)\)#](assets/images/\1)#g' \
        "${f}"
}

for f in "${CONTENT}"/*.md; do
    rewrite_file "${f}"
done

echo
echo "✓ website/content/ populated from source docs."
echo "  Point a static server at ./website/ to preview:"
echo "    python3 -m http.server --directory website 8765"
