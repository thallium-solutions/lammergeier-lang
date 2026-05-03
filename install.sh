#!/usr/bin/env bash
# Lammergeier Lang — unified installer.
#
# Installs:
#   1. the `lamc` compiler launcher (symlinked onto PATH)
#   2. the `lammergeier-lsp` language-server launcher (same target dir)
#   3. the Python `lark` parser dependency
#   4. (optional) the VS Code / Cursor / Windsurf editor extension
#
# Prefix selection:
#   --user            Install symlinks into $HOME/.local/bin  (no sudo)
#   --system          Install symlinks into /usr/local/bin    (may sudo)
#   --prefix DIR      Install symlinks into DIR/bin
#   (default)         Auto-pick: try /usr/local/bin if writable, else
#                     $HOME/.local/bin.
#
# Extension:
#   --with-editor [vscode|windsurf|cursor|all]
#                     Also install the editor extension. Accepts any
#                     value accepted by vs-code-extension/install.sh.
#                     Defaults to "all" when the flag is bare.
#   --no-editor       Skip the editor extension even if auto-detected.
#
# Other:
#   --no-pip          Skip installing `lark` via pip (assume it's
#                     already on the system's Python).
#   --python PY       Use PY as the Python interpreter (default
#                     ``python3``). The compiler needs Python 3.10+.
#   -n, --dry-run     Print the commands that would run, but don't
#                     touch the filesystem.
#   -h, --help        Show this help and exit.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

MODE="auto"
PREFIX=""
PYTHON="${PYTHON:-python3}"
DO_PIP=1
EDITOR_TARGET=""
NO_EDITOR=0
DRY_RUN=0

usage() {
    sed -n '1,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit 0
}

log()  { printf '[install] %s\n' "$*"; }
warn() { printf '[install] WARNING: %s\n' "$*" >&2; }
die()  { printf '[install] ERROR: %s\n' "$*" >&2; exit 1; }

run() {
    if (( DRY_RUN )); then
        printf '[dry-run] %s\n' "$*"
    else
        eval "$@"
    fi
}

while (( $# )); do
    case "$1" in
        --user)           MODE="user"; shift ;;
        --system)         MODE="system"; shift ;;
        --prefix)         MODE="prefix"; PREFIX="$2"; shift 2 ;;
        --prefix=*)       MODE="prefix"; PREFIX="${1#*=}"; shift ;;
        --with-editor)
            # Optional argument: next token if it doesn't start with '-'.
            if [[ $# -ge 2 && "$2" != -* ]]; then
                EDITOR_TARGET="$2"; shift 2
            else
                EDITOR_TARGET="all"; shift
            fi
            ;;
        --with-editor=*)  EDITOR_TARGET="${1#*=}"; shift ;;
        --no-editor)      NO_EDITOR=1; shift ;;
        --no-pip)         DO_PIP=0; shift ;;
        --python)         PYTHON="$2"; shift 2 ;;
        --python=*)       PYTHON="${1#*=}"; shift ;;
        -n|--dry-run)     DRY_RUN=1; shift ;;
        -h|--help)        usage ;;
        *) die "unknown argument: $1 (try --help)" ;;
    esac
done

# ─── Prerequisites ──────────────────────────────────────────
command -v "$PYTHON" >/dev/null 2>&1 || die "$PYTHON not found on PATH"
PY_VER="$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
case "$PY_VER" in
    3.1[0-9]|3.[2-9]*|[4-9].*) : ;;
    *) die "Python 3.10+ required (found $PY_VER)" ;;
esac
log "using Python $PY_VER ($PYTHON)"

if ! command -v go >/dev/null 2>&1; then
    warn "Go toolchain not found. Install Go 1.21+ (https://go.dev/dl/) before running lamc."
else
    GO_VER="$(go version | awk '{print $3}' | sed 's/^go//')"
    log "found Go $GO_VER"
fi

# ─── Bin prefix ─────────────────────────────────────────────
choose_bin_dir() {
    case "$MODE" in
        user)    echo "$HOME/.local/bin" ;;
        system) echo "/usr/local/bin" ;;
        prefix) echo "$PREFIX/bin" ;;
        auto)
            if [[ -w "/usr/local/bin" ]]; then
                echo "/usr/local/bin"
            else
                echo "$HOME/.local/bin"
            fi
            ;;
        *) die "invalid mode: $MODE" ;;
    esac
}

BIN_DIR="$(choose_bin_dir)"
log "bin prefix: $BIN_DIR"

# Decide whether we need sudo to write there.
SUDO=""
if [[ ! -d "$BIN_DIR" ]]; then
    if [[ -w "$(dirname "$BIN_DIR")" ]]; then
        run "mkdir -p '$BIN_DIR'"
    elif command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
        run "$SUDO mkdir -p '$BIN_DIR'"
    else
        die "cannot create $BIN_DIR (no write access and sudo unavailable)"
    fi
elif [[ ! -w "$BIN_DIR" ]]; then
    if command -v sudo >/dev/null 2>&1; then
        SUDO="sudo"
        log "write access to $BIN_DIR requires sudo"
    else
        die "no write access to $BIN_DIR and sudo unavailable"
    fi
fi

# ─── lark (Python parser) ───────────────────────────────────
if (( DO_PIP )); then
    if "$PYTHON" -c 'import lark' >/dev/null 2>&1; then
        log "lark already installed"
    else
        log "installing lark via pip (--user if needed)"
        # Prefer user install when not going system-wide.
        if [[ "$MODE" == "user" || "$MODE" == "auto" && -z "$SUDO" ]]; then
            run "'$PYTHON' -m pip install --user --upgrade lark" \
                || warn "pip install failed — install 'lark' manually before running lamc"
        else
            run "$SUDO '$PYTHON' -m pip install --upgrade lark" \
                || warn "pip install failed — install 'lark' manually before running lamc"
        fi
    fi
else
    log "skipping pip install (--no-pip)"
fi

# ─── Symlink the launchers ──────────────────────────────────
LAMC_SRC="$HERE/lamc"
LSP_SRC="$HERE/bin/lammergeier-lsp"

[[ -x "$LAMC_SRC" ]] || die "missing or non-executable $LAMC_SRC"
[[ -x "$LSP_SRC"  ]] || die "missing or non-executable $LSP_SRC"

install_symlink() {
    local src="$1" name="$2"
    local dst="$BIN_DIR/$name"
    if [[ -e "$dst" || -L "$dst" ]]; then
        log "replacing existing $dst"
        run "$SUDO rm -f '$dst'"
    fi
    run "$SUDO ln -s '$src' '$dst'"
    log "linked $dst -> $src"
}

install_symlink "$LAMC_SRC" "lamc"
install_symlink "$LSP_SRC"  "lammergeier-lsp"

# ─── Editor extension (optional) ────────────────────────────
if (( NO_EDITOR == 0 )) && [[ -n "$EDITOR_TARGET" ]]; then
    EXT_SCRIPT="$HERE/vs-code-extension/install.sh"
    if [[ -x "$EXT_SCRIPT" ]]; then
        log "installing editor extension ($EDITOR_TARGET)"
        run "'$EXT_SCRIPT' '$EDITOR_TARGET'"
    else
        warn "missing $EXT_SCRIPT — skipping editor extension"
    fi
fi

# ─── PATH hint ──────────────────────────────────────────────
case ":$PATH:" in
    *":$BIN_DIR:"*) : ;;
    *) warn "$BIN_DIR is not on \$PATH. Add it, e.g.:
        echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc"
       ;;
esac

log "done. Run 'lamc --help' to verify."
if [[ -z "$EDITOR_TARGET" && $NO_EDITOR -eq 0 ]]; then
    log "tip: re-run with --with-editor to install the VS Code / Cursor / Windsurf extension."
fi
