<p align="center">
  <img src="https://raw.githubusercontent.com/thallium-solutions/lammergeier-lang/main/vs-code-extension/lammergeier-lang/icon.png" alt="Lammergeier Lang logo" width="160">
</p>

<h1 align="center">Lammergeier Lang for VS Code</h1>

<p align="center">
  Syntax highlighting and rich language support for <strong>Lammergeier</strong> (<code>.lam</code>) — a typed Python-like language that compiles to Go.
</p>

<p align="center">
  Semantic coloring · Accurate folding · Diagnostics · Completion · Navigation · Refactoring · Formatting
</p>

## Features

- **Syntax highlighting** for all Lammergeier constructs, including f-string interpolation, with dedicated scopes for stdlib modules (`lamarray`, `lamxml`, `lamyaml`, `lamcrypto`, `lamcontext`, `lamtemplate`, `lamretry`, …) and built-in classes (`Result`, `Server`, `HttpClient`, `Crypto`, `Context`, `Xml`, `Template`, `TokenBucket`, …)
- **Semantic coloring** distinguishes classes, interfaces, functions, methods, parameters, properties, types, decorators, imports, and `LAMMERGEIER.*` namespace paths
- **Embedded Go highlighting** inside `go! { ... }` blocks (when the built-in Go grammar is available)
- **Accurate folding** for nested `{ }` blocks, raw `go!` blocks, multiline strings, consecutive comments, and multiline comments (`#- ... -#`)
- **Editing basics** — bracket matching and auto-closing for `{}`, `[]`, and `()` plus comment toggling with `#` (line) / `#- ... -#` (block)
- **Language Server** integration — compiler-parity parse/preprocess/semantic diagnostics (including `json(value)` / `__json__`, method overloads, dict patterns, captures, `nonlocal`, and `LAMMERGEIER.*`), overload-aware signature help, hover, completion, local and imported go-to-definition, references, rename, formatting, and document/workspace symbols
- Settings to point at a custom LSP launcher and capture trace output

## Language Highlights

| Construct   | Example                                 |
|-------------|-----------------------------------------|
| Functions   | `func add(x: int, y: int) -> int { }`   |
| Classes     | `class Point { func init(self) { } }`   |
| Interfaces  | `interface Shape { func area(self) -> float; }` |
| Modifiers   | `private static async func helper() { }`|
| Go blocks   | `go! { fmt.Println("raw Go") }`         |
| Inline Go   | `go!(someGoExpr)`                       |
| Decorators  | `@decorator`                            |
| Variadic    | `func sum(*args: int) -> int { }`       |
| Match       | `match value { case 1 { ... } }`        |

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
`~/.cursor/extensions/`,`~/.devin/extensions/`) with the correct
`<publisher>.<name>-<version>` folder name. Reload the editor
window afterwards.

You can target a single editor:

```bash
./vs-code-extension/install.sh vscode
./vs-code-extension/install.sh windsurf
./vs-code-extension/install.sh cursor
./vs-code-extension/install.sh devin
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
        ~/.vscode/extensions/lammergeier.lammergeier-lang-1.4.0
```

#### Windsurf

Windsurf uses `~/.windsurf/extensions/` and requires the `-universal`
target-platform suffix on the folder name:

```bash
# From the project root:
ln -sfn "$(pwd)/vs-code-extension/lammergeier-lang" \
        ~/.windsurf/extensions/lammergeier.lammergeier-lang-1.4.0-universal
```

#### Devin

Devin uses `~/.devin/extensions/` and requires the `-universal`
target-platform suffix on the folder name:

```bash
# From the project root:
ln -sfn "$(pwd)/vs-code-extension/lammergeier-lang" \
        ~/.devin/extensions/lammergeier.lammergeier-lang-1.4.0-universal
```

#### Cursor (same mechanism)

```bash
ln -sfn "$(pwd)/vs-code-extension/lammergeier-lang" \
        ~/.cursor/extensions/lammergeier.lammergeier-lang-1.4.0
```

After creating the symlink, reload the editor window (command palette →
`Developer: Reload Window`) and open any `.lam` file. Highlighting should
activate automatically.

> **Note:** The folder name inside the extensions directory **must**
> follow the `<publisher>.<name>-<version>` convention
> (`lammergeier.lammergeier-lang-1.4.0`). Using a bare `lammergeier-lang`
> folder name is the most common reason the extension silently fails to
> load on both VS Code and Windsurf.

## Language Server

The extension activates a Language Server Protocol client when you open
a `.lam` file, providing:

- **Diagnostics** — parse errors and semantic checks (undefined names,
  wrong types, duplicate members, misplaced flow) surface live as red squiggles,
  including inside `# expect-error` and `# expect-warning` test fixtures. Set
  `lammergeier.lsp.suppressExpectedDiagnostics` to `true` to hide matched
  fixture diagnostics explicitly.
- **Semantic Tokens** — contextual roles supplement the TextMate grammar so declarations, types, parameters, members, decorators, imports, and calls remain distinct.
- **Folding Ranges** — nested Lam blocks, `go!` blocks, multiline strings, and comment groups collapse without braces inside strings or comments confusing the editor.
- **Hover** — types and short docs for built-ins, modules, and your
  own functions/classes. **Cross-file**: hovering on a name imported
  via `from lam<x> import …` shows the signature pulled from the
  bundled stdlib file, prefixed with the originating module name.
- **Completion** — keywords, types, builtins, locals, parameters,
  class fields, inherited members, `self`, `base`, named inheritance
  aliases, and identifiers harvested from the current document,
  **plus stdlib symbols you've imported from `lib/lam*.lam`**.
  Typing after `from ` suggests stdlib, workspace, and scoped package modules;
  typing inside `from lam<x> import |` suggests public exports; typing
  `instance.|`, `instance?.|`, `self.|`, `base.|`, or `MyClass.|` lists the
  available members. Importable symbols from stdlib and workspace modules are
  offered as read-only suggestions like `from lamenv import Dotenv`;
  the extension never inserts imports automatically.
- **Signature Help** — function, method, and constructor calls show
  argument help. Instance methods hide implicit `self`, and constructors
  are displayed with the preferred `init(...)` sugar instead of
  `__init__(...)`.
- **Go to Definition** — `F12` jumps to local variables, parameters, functions,
  classes, methods, fields, inherited members, imported module paths, and
  imported stdlib/user-module symbols.
- **References** — `Shift+F12` finds usages in open Lam documents and follows
  imported aliases back to the defining module.
- **Rename** — `F2` validates and renames known local symbols in the current
  document; imported and cross-file renames are rejected rather than applied
  partially.
- **Formatting** — Format Document uses the same parser-validated formatter as
  `lamc fmt`.
- **Workspace Symbols** — workspace search can find Lam functions,
  classes, methods, fields, and exported module symbols.
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
| `lammergeier.lsp.suppressExpectedDiagnostics` | `false` | Hide errors/warnings matched by fixture directives. |
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
rm ~/.vscode/extensions/lammergeier.lammergeier-lang-1.4.0
rm ~/.windsurf/extensions/lammergeier.lammergeier-lang-1.4.0-universal
rm ~/.devin/extensions/lammergeier.lammergeier-lang-1.4.0-universal
rm ~/.cursor/extensions/lammergeier.lammergeier-lang-1.4.0
```
