# Installing Lammergeier

> **Audience:** developers who want to put `lamc` (the compiler),
> `lammergeier-lsp` (the language server), and the editor
> extension on their machine. For installing third-party
> **libraries** (`lamc install lamwebp`), see
> [`docs/package_manager.md`](#/docs/package_manager).

This is the canonical reference for the toolchain installer
shipped at the repo root as `install.sh`. Everything here is
implemented today and exercised by CI on every change.

---

## 1. TL;DR

```bash
git clone https://github.com/thallium-solutions/lammergeier-lang.git
cd lammergeier-lang
./install.sh                            # auto: system-wide if writable, else ~/.local/bin
lamc --help                             # verify
```

Add the editor extension at the same time:

```bash
./install.sh --with-editor all          # VS Code, Cursor, Windsurf — whichever it can detect
```

Three things land on disk:

1. A `lamc` symlink on `PATH` pointing at the in-repo launcher.
2. A `lammergeier-lsp` symlink on `PATH` pointing at the LSP launcher.
3. The Python `lark` parser (the only runtime Python dependency)
   installed via `pip` — globally if you ran with `--system`, or
   `--user` otherwise.

The compiler keeps reading `lib/`, `compiler/`, and
`lammergeier.lark` straight out of the checkout. **Upgrade by
`git pull`** — there is no separate "update" step.

---

## 2. Prerequisites

| Tool | Minimum | Why |
|------|---------|-----|
| **Python** | 3.10 | The compiler is written in Python and uses pattern-matching, `typing.Optional`, structural-pattern-match, and `tomllib`-equivalent helpers. |
| **Go** | 1.21 | The transpiler emits Go source and shells out to `go build`. Earlier Go versions miss generics features the stdlib uses. |
| **`lark`** | latest | The parser. Installed automatically by `install.sh` unless you pass `--no-pip`. |

The installer surface-checks both Python (hard fail if absent /
too old) and Go (warning, not an error — you can install Lam
without Go but you won't be able to build). All three checks run
in dry-run mode too.

---

## 3. The `install.sh` flags

Run `./install.sh --help` for the inline cheatsheet. The full
matrix:

### 3.1 Choosing where the symlinks land

| Flag | Behaviour |
|------|-----------|
| *(none)* | **Auto.** Tries `/usr/local/bin` if it's writable, otherwise drops to `$HOME/.local/bin`. |
| `--user` | Force `$HOME/.local/bin`. Never elevates with sudo. |
| `--system` | Force `/usr/local/bin`. May prompt for sudo. |
| `--prefix DIR` | Symlinks land in `DIR/bin`. Useful for Homebrew-style staging or NixOS-on-the-side checkouts. |

If the chosen directory isn't on your `PATH`, the installer
prints the exact line you need to append to `~/.bashrc` /
`~/.zshrc` and exits zero — the symlinks are still placed, you
just need to source them.

### 3.2 Editor extension

| Flag | Behaviour |
|------|-----------|
| *(none)* | Skip the editor extension. |
| `--with-editor` | Install into every detected editor (`vscode`, `cursor`, `windsurf`). |
| `--with-editor vscode` | Only the VS Code extensions directory. |
| `--with-editor cursor` | Only Cursor. |
| `--with-editor windsurf` | Only Windsurf. |
| `--no-editor` | Belt-and-braces: never install the extension, even if a future default flips on. |

Under the hood this delegates to `vs-code-extension/install.sh`,
which symlinks the extension into the right per-editor
directory. Re-running the toolchain installer is idempotent.

### 3.3 Python interpreter / pip

| Flag | Behaviour |
|------|-----------|
| `--no-pip` | Skip the `lark` install step. Useful if you manage Python deps with `pipx`, `poetry`, `uv`, or a project-local virtualenv. |
| `--python PY` | Use `PY` instead of `python3`. Pass an absolute path to a particular interpreter (e.g. `--python /opt/homebrew/bin/python3.12`). The version check still runs. |

### 3.4 Other

| Flag | Behaviour |
|------|-----------|
| `-n` / `--dry-run` | Print every action that would run; touch nothing. Pair with `--with-editor` to preview the editor wiring before you commit. |
| `-h` / `--help` | Print the inline help (the comment block at the top of the script) and exit. |

---

## 4. What `lamc` actually does

After install, `lamc` is the single entry point for everything:

| Verb | Purpose |
|------|---------|
| `lamc <file.lam>` | Compile a Lam program to a native binary. Output path defaults to the source path minus `.lam` (so `lamc src/foo.lam` writes `src/foo`). |
| `lamc <file.lam> --run` | Compile and immediately execute. |
| `lamc <file.lam> --emit-go` | Print the generated Go source and stop before `go build`. |
| `lamc <file.lam> --emit-ast` | Print the parsed Lark AST and stop before transpilation. |
| `lamc version` / `lamc --version` | Print the compiler version used by `compatibility.lamc` manifest ranges. |
| `lamc doctor` / `lamc --doctor` | Report compiler, Python, Go, `lark`, stdlib, cache, PATH, Go environment, dependency, manifest, and editor-extension diagnostics. Supports `--json` and `--strict`. |
| `lamc init` | Scaffold a fresh project (manifest + entry-point + `.gitignore`). |
| `lamc install <spec>` | Install a third-party library — see [`docs/package_manager.md`](#/docs/package_manager). |
| `lamc uninstall <name>` | Remove an installed library + its lockfile pin. |
| `lamc tidy [--check]` | Sync `lamlib.toml` `[dependencies]` with the project's actual imports + refresh the lockfile. |
| `lamc verify` | Re-hash every installed extlib against `lamlib.lock.toml` (supply-chain integrity). |
| `lamc list` / `tree` / `why <name>` | Lockfile introspection — flat list, indented tree, single-pin chain. |
| `lamc publish [<dir>]` | Pack a library tree and POST it to a registry. |
| `lamc lib run <script>` | Run a command declared in the nearest `lamlib.toml` `[scripts]` table. |
| `lamc migrate make / up / down / status` | Knex-style SQL migrations. |
| `lamc fmt <file-or-dir>` | Parser-validated formatter. Directories are walked recursively for `.lam` files; use `--check` for CI and `--stdout` for one file. |

Run `lamc --help` for the full surface; every verb has its own
`--help` page.

Use `lamc version` when you need the concrete compiler version for a
library's `compatibility.lamc` range. Use `lamc doctor` after installation
or after moving a checkout to confirm that `lamc` can see the expected
Python interpreter, Go toolchain, `lark` parser, stdlib directory, compiler
cache, language server launcher, package manifest, Python requirements, and
editor extension installs. Use `lamc doctor --json` for scripted inspection
and `lamc doctor --strict` when CI should fail on missing required pieces.

### 4.1 Compile-time flags worth knowing

| Flag | Behaviour |
|------|-----------|
| `-o PATH` | Override the output binary path. |
| `--go-ldflags FLAGS` | Forward `FLAGS` straight to `go build` (`-X main.version=...` etc.). |
| `--extlibs DIR` | Add an extra search root for third-party libraries (repeatable; first wins). |
| `LAMC_CACHE_DIR=/path` | Override the on-disk transpilation cache (defaults to `~/.cache/lammergeier`). |

The full set is in [`SYNTAX.md`](#/docs/syntax) and the `lamc --help`
output.

---

## 5. Editor support

### 5.1 The LSP server

`bin/lammergeier-lsp` is a self-contained Language Server
Protocol 3.17 implementation that speaks JSON-RPC 2.0 over
stdio. Capabilities:

- **Diagnostics** — parse and semantic errors, including undefined names and
  wrong types, publish as red squiggles on every `didChange`. Diagnostics remain
  visible in `# expect-error` fixtures by default.
- **Hover** — function and class signatures.
- **Completion** — top-level functions / classes plus method
  completion after `Foo.` for static methods.
- **Goto-definition** — jump from a usage to the declaration
  line.
- **Document symbols** — outline tree with classes nesting their
  methods.

The LSP keeps useful answers even mid-edit: when the LALR parser
rejects the current buffer, it falls back to a regex-based
symbol extractor so completion and outline still respond.

Set `LAMMERGEIER_LSP_LOG=/tmp/lam-lsp.log` for a verbose
JSON-RPC trace. The server itself never writes to stderr, so the
LSP framing stays clean.

### 5.2 The VS Code / Cursor / Windsurf extension

Three editors share the same VSIX-shaped tree under
`vs-code-extension/lammergeier-lang/`. Features:

- Syntax highlighting (including embedded Go inside `go! { ... }`).
- LSP client wired to `bin/lammergeier-lsp` out of the box.
- Configurable via:
  * `lammergeier.lsp.path` — path to `bin/lammergeier-lsp`
    (PATH-resolved if relative; supports `~` and
    `${workspaceFolder}`).
  * `lammergeier.lsp.enabled` — master switch.
  * `lammergeier.lsp.logFile` — capture the JSON-RPC trace.
  * `lammergeier.lsp.suppressExpectedDiagnostics` — opt into hiding diagnostics
    matched by `# expect-error` / `# expect-warning` fixture directives.
  * `lammergeier.trace.server` — `off` / `messages` / `verbose`.
- A **Lammergeier: Restart Language Server** command after you
  upgrade the compiler.

The toolchain installer wires this up automatically with
`--with-editor`. You can also run the extension installer
directly:

```bash
./vs-code-extension/install.sh           # all detected editors
./vs-code-extension/install.sh vscode    # just VS Code
```

---

## 6. Upgrading

`install.sh` symlinks instead of copying. The compiler reads
`lib/`, `compiler/`, and `lammergeier.lark` out of the checkout
on every invocation, so:

```bash
cd /path/to/lammergeier-lang
git pull
# That's it. No re-install needed.
```

If you re-run `./install.sh` it will replace existing symlinks
with fresh ones (idempotent) — the only reason to do that is
when you change `--user` ↔ `--system` or move the checkout.

---

## 7. Uninstalling

Symlink-based, so removing the toolchain is two `rm` calls plus
an optional pip uninstall:

```bash
# 1. Drop the launchers.
rm "$(command -v lamc)"
rm "$(command -v lammergeier-lsp)"

# 2. (Optional) drop the lark dependency.
python3 -m pip uninstall -y lark

# 3. (Optional) drop the user-global extlibs cache.
rm -rf ~/.lammergeier
```

If you used `--with-editor`, the extension lives under each
editor's per-user extensions directory:

| Editor | Path |
|--------|------|
| VS Code | `~/.vscode/extensions/lammergeier-lang/` |
| Cursor | `~/.cursor/extensions/lammergeier-lang/` |
| Windsurf | `~/.windsurf/extensions/lammergeier-lang/` |

`rm -rf` whichever ones you'd like to remove.

---

## 8. Common situations

### 8.1 "I don't have root"

```bash
./install.sh --user                     # symlinks into ~/.local/bin
```

`pip install lark` falls back to `--user` automatically when
running unprivileged.

### 8.2 "I'm packaging Lammergeier for Homebrew / Nix / Debian"

```bash
./install.sh --prefix /opt/lammergeier --no-pip --no-editor
```

The package recipe handles `lark` (`python3-lark` on Debian,
`python311Packages.lark` on Nix, etc.) and the editor extension
through whatever channel makes sense for that ecosystem.
`--prefix` makes the installer 100 % path-relocatable.

### 8.3 "I'm in a fresh container / CI image"

```bash
apt-get install -y python3 python3-pip golang
git clone https://github.com/thallium-solutions/lammergeier-lang.git
cd lammergeier-lang
./install.sh --system --no-editor
lamc --help
```

The full test suite plus `--system` install runs in under a
minute on a 2-vCPU runner.

### 8.4 "The shell can't find `lamc` after install"

The installer prints the exact line you need to add to your
shell rc when the chosen prefix isn't on `PATH`. Re-run it with
`-n` to see the line again without re-doing the install:

```bash
./install.sh -n
```

### 8.5 "Different Python on `PATH` than I want to use"

Use `--python` and check it sticks:

```bash
./install.sh --python /opt/homebrew/bin/python3.12
head -1 "$(command -v lamc)"            # the shebang of lamc points at /usr/bin/env python3
```

`lamc` itself uses `#!/usr/bin/env python3`, so if you need a
specific interpreter set `PYTHON=/path/to/python3` in your
environment before invoking `lamc`, or shell-alias it.

---

## 9. Verifying the install

```bash
# 1. Compiler responds.
lamc --help | head -1

# 2. LSP launches and exits cleanly with empty stdin.
echo '' | lammergeier-lsp >/dev/null

# 3. End-to-end round-trip.
cat > /tmp/hello.lam <<'LAM'
func main() {
    print("hello, lammergeier")
}
LAM
lamc /tmp/hello.lam --run
# → hello, lammergeier
```

If all three succeed, the toolchain is healthy.

---

## 10. Where to next

- **First Lam program**: [`SYNTAX.md`](#/docs/syntax) — the complete
  language surface in one file.
- **Standard library**: [`stdlib.md`](#/docs/stdlib) — every `lam*`
  module, every class, every method.
- **Adding third-party deps**: [`package_manager.md`](#/docs/package_manager) —
  `lamc install`, the lockfile, conflict detection, transitive
  resolution, the SemVer / API-diff gate.
- **Authoring a third-party library**:
  [`third_party_libraries.md`](#/docs/third_party) — the
  on-disk shape, `lamlib.toml` manifest, scoped names, the
  registry protocol.
