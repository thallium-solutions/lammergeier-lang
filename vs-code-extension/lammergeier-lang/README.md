# Lammergeier Lang — VS Code / Windsurf Extension

Syntax highlighting and language support for **Lammergeier** (`.lam`) — a typed Python-like language that compiles to Go.

## Features

- **Syntax highlighting** for all Lammergeier constructs, with dedicated scopes for stdlib modules (`lamarray`, `lamserver`, `lamxml`, `lamyaml`, `lamjwt`, `lamtemplate`, `lamretry`, `lamratelimit`, …) and built-in classes (`Result`, `Server`, `HttpClient`, `Xml`, `Template`, `JwtKeySet`, `TokenBucket`, …)
- **`LAMMERGEIER.*` namespace** — known aliases (`LAMMERGEIER.Result.Ok`, `.Result.Err`, `.Error`, `.None`, `.nil`) highlight as constants; mistyped aliases highlight as illegal so typos surface immediately in the editor
- **Embedded Go highlighting** inside `go! { ... }` blocks (when the built-in Go grammar is available)
- **Multiline comments** (`#- ... -#`) folded and toggled correctly via `Ctrl-/`
- **Bracket matching** and auto-closing for `{}`, `[]`, `()`
- **Comment toggling** with `#` (line) / `#- ... -#` (block)
- **Code folding** on `{ }` blocks
- **F-string interpolation** highlighting
- **Language Server** integration — parse + semantic diagnostics (undefined names, duplicate class members, misplaced `return` / `break` / `continue`), hover, completion, goto-definition, document symbols
- Settings to point at a custom LSP launcher and capture trace output

## Language Highlights

| Construct   | Example                                 |
|-------------|-----------------------------------------|
| Functions   | `func add(x: int, y: int) -> int { }`   |
| Classes     | `class Point(x: int, y: int) { }`       |
| Interfaces  | `interface Shape { }`                   |
| Modifiers   | `private static async func helper() { }`|
| Go blocks   | `go! { fmt.Println("raw Go") }`         |
| Inline Go   | `go!(someGoExpr)`                       |
| Decorators  | `@decorator`                            |
| Variadic    | `func sum(*args: int) -> int { }`       |
| Match       | `match value { case 1: ... }`           |

## Installation

### Quick install (recommended)

From the project root run:

```bash
# 1. Build the LSP-client TypeScript bundle once.
(cd vs-code-extension/lammergeier-lang && npm install && npm run build)

# 2. Symlink into every detected editor.
./vs-code-extension/install.sh
```

The install script symlinks the extension into every detected editor
(`~/.vscode/extensions/`, `~/.windsurf/extensions/`,
`~/.cursor/extensions/`) with the correct
`<publisher>.<name>-<version>` folder name. Reload the editor
window afterwards.

You can target a single editor:

```bash
./vs-code-extension/install.sh vscode
./vs-code-extension/install.sh windsurf
./vs-code-extension/install.sh cursor
```

If you skip step 1, the highlighting still works; only the LSP client
stays inert until `out/extension.js` exists.

### Manual install

If you prefer to symlink by hand, **point the link at the inner
`vs-code-extension/lammergeier-lang/` directory** (not the outer
`vs-code-extension/` wrapper — that is the #1 cause of the extension
silently failing to load).

#### VS Code

```bash
# From the project root:
ln -sfn "$(pwd)/vs-code-extension/lammergeier-lang" \
        ~/.vscode/extensions/lammergeier.lammergeier-lang-0.3.0
```

#### Windsurf

Windsurf uses `~/.windsurf/extensions/` and requires the `-universal`
target-platform suffix on the folder name:

```bash
# From the project root:
ln -sfn "$(pwd)/vs-code-extension/lammergeier-lang" \
        ~/.windsurf/extensions/lammergeier.lammergeier-lang-0.3.0-universal
```

#### Cursor (same mechanism)

```bash
ln -sfn "$(pwd)/vs-code-extension/lammergeier-lang" \
        ~/.cursor/extensions/lammergeier.lammergeier-lang-0.3.0
```

After creating the symlink, reload the editor window (command palette →
`Developer: Reload Window`) and open any `.lam` file. Highlighting should
activate automatically.

> **Note:** The folder name inside the extensions directory **must**
> follow the `<publisher>.<name>-<version>` convention
> (`lammergeier.lammergeier-lang-0.3.0`). Using a bare `lammergeier-lang`
> folder name is the most common reason the extension silently fails to
> load on both VS Code and Windsurf.

## Language Server

The extension activates a Language Server Protocol client when you open
a `.lam` file, providing:

- **Diagnostics** — parse errors and semantic checks (undefined names,
  duplicate members, misplaced flow) surface live as red squiggles.
- **Hover** — types and short docs for built-ins, modules, and your
  own functions/classes. **Cross-file**: hovering on a name imported
  via `from lam<x> import …` shows the signature pulled from the
  bundled stdlib file, prefixed with the originating module name.
- **Completion** — keywords, types, builtins, identifiers harvested
  from the current document, **plus stdlib symbols you've imported
  from `lib/lam*.lam`**. Typing inside `from lam<x> import |`
  suggests the module's public exports; typing `MyClass.|` lists
  methods of an imported class as well as a local one.
- **Go to Definition** — `F12` jumps to the function/class
  declaration in the current document, or into the matching
  `lib/lam<x>.lam` stdlib file when the symbol comes from an
  import.
- **Document Symbols** — outline view + breadcrumbs.

The client spawns the `lammergeier-lsp` launcher
(`bin/lammergeier-lsp` in this repo) over stdio. Add `bin/` to your
`PATH`, or set `lammergeier.lsp.path` to the absolute path of the
launcher. The setting accepts `~` and `${workspaceFolder}` substitution.

### Settings

| Setting                         | Default              | Purpose                                            |
|---------------------------------|----------------------|----------------------------------------------------|
| `lammergeier.lsp.enabled`       | `true`               | Master switch for the LSP client.                  |
| `lammergeier.lsp.path`          | `"lammergeier-lsp"`  | Path to the launcher (PATH-resolved if relative).  |
| `lammergeier.lsp.args`          | `[]`                 | Extra args forwarded to the launcher.              |
| `lammergeier.lsp.logFile`       | `""`                 | If set, exports `LAMMERGEIER_LSP_LOG=<path>`.      |
| `lammergeier.trace.server`      | `"off"`              | `off` / `messages` / `verbose` JSON-RPC tracing.   |

Use the **Lammergeier: Restart Language Server** command from the
palette after changing the launcher path or upgrading the compiler.

### Development

```bash
cd vs-code-extension/lammergeier-lang
npm install              # one-time
npm run watch            # rebuilds out/extension.js on change
# Then: F5 in VS Code to launch an Extension Development Host.
```

## Uninstall

```bash
rm ~/.vscode/extensions/lammergeier.lammergeier-lang-0.3.0
rm ~/.windsurf/extensions/lammergeier.lammergeier-lang-0.3.0-universal
rm ~/.cursor/extensions/lammergeier.lammergeier-lang-0.3.0
```
