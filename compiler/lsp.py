#!/usr/bin/env python3
"""Lammergeier Language Server (LSP) — minimal but useful editor backend.

Speaks JSON-RPC 2.0 over stdin/stdout per the Language Server Protocol
v3.17, supporting:

* ``initialize`` / ``initialized`` / ``shutdown`` / ``exit`` lifecycle
* ``textDocument/didOpen``, ``didChange`` (full sync), ``didClose``
* ``textDocument/publishDiagnostics`` (pushed on change)
* ``textDocument/hover``      — type / signature for the symbol under the cursor
* ``textDocument/completion`` — top-level functions, classes, methods of receiver
* ``textDocument/definition`` — jump to top-level definitions
* ``textDocument/documentSymbol`` — outline tree for the editor

The server piggy-backs on the existing Lark grammar and the static
metadata gathered by ``GoTranspiler`` (class names, function defaults,
static methods, etc.). Diagnostics come straight from ``lark.UnexpectedInput``
errors with line/column attached.

Run from a terminal as a sanity check:

    python -m compiler.lsp

Editors should launch it as a child process and pipe LSP messages.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from lark import Lark, Tree, Token
from lark.exceptions import UnexpectedInput

# ── Make the rest of the compiler importable when run as `python -m compiler.lsp`
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from compiler.diagnostics import (  # noqa: E402
    Diagnostic,
    DiagnosticSeverity,
    SourceSpan,
    diagnostic_to_lsp,
)
from compiler.ast_builder import build_module  # noqa: E402
from compiler.ast_nodes import ClassDecl, FuncDecl, InterfaceDecl, Module, Param, VarDecl  # noqa: E402
from compiler.formatter import FormatError, format_lam_source  # noqa: E402
from compiler.modules import WorkspaceIndex  # noqa: E402
from compiler.syntax_errors import make_syntax_diagnostic  # noqa: E402
from compiler.lammergeier import preprocess_for_parse  # noqa: E402

# Optional: log to a file when LAMMERGEIER_LSP_LOG is set. The LSP
# stdio channel must stay clean of stray prints, so we never log to
# stderr by default.
_log_path = os.environ.get("LAMMERGEIER_LSP_LOG")
logger = logging.getLogger("lammergeier-lsp")
if _log_path:
    logging.basicConfig(filename=_log_path, level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
else:
    logger.addHandler(logging.NullHandler())


# ────────────────────────────────────────────────────────────
# JSON-RPC framing
# ────────────────────────────────────────────────────────────

_HEADER_RE = re.compile(rb"Content-Length:\s*(\d+)\r\n", re.IGNORECASE)


def _read_message(stream) -> Optional[Dict[str, Any]]:
    """Read one LSP message from ``stream``. Returns None on EOF."""
    headers = b""
    while True:
        ch = stream.read(1)
        if not ch:
            return None
        headers += ch
        if headers.endswith(b"\r\n\r\n"):
            break
    m = _HEADER_RE.search(headers)
    if not m:
        return None
    length = int(m.group(1))
    body = b""
    while len(body) < length:
        chunk = stream.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        logger.exception("malformed JSON-RPC body")
        return None


def _write_message(stream, msg: Dict[str, Any]) -> None:
    body = json.dumps(msg, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stream.write(header + body)
    stream.flush()


def _uri_to_path(uri: str) -> Optional[Path]:
    try:
        parsed = urlparse(uri)
        if parsed.scheme != "file":
            return None
        return Path(unquote(parsed.path)).resolve()
    except Exception:
        return None


# ────────────────────────────────────────────────────────────
# Document analysis
# ────────────────────────────────────────────────────────────

_PARSER: Optional[Lark] = None
_PARSER_LOCK = threading.Lock()

# ── stdlib symbol index ─────────────────────────────────────
# Lazy-built map ``module_name -> StdlibModule`` for the curated
# stdlib under ``lib/*.lam``. The entries cache parsed symbols
# alongside the file's mtime so we re-read on disk changes (handy
# during stdlib development) without re-parsing every keystroke.

@dataclass
class StdlibModule:
    """Cached metadata for one ``lib/<name>.lam`` file.

    ``symbols_by_name`` is keyed by the public name a user would
    import — top-level functions, classes, and the methods of those
    classes (so ``Dotenv.parse`` resolves correctly). Methods are
    *also* indexed under their bare name so dotted completion against
    a class instance can find them when the parent class is the
    imported alias.
    """
    path: Path
    mtime: float
    symbols: List[Symbol]
    # Top-level public names — what ``from <module> import X`` can
    # see. Maps name -> Symbol.
    public: Dict[str, Symbol]
    # Per-class method buckets. Lookup as
    # ``methods[ClassName] -> {method_name: Symbol}``.
    methods: Dict[str, Dict[str, Symbol]]


_STDLIB_INDEX: Dict[str, StdlibModule] = {}
_STDLIB_INDEX_LOCK = threading.Lock()
_STDLIB_DIR = PROJECT_ROOT / "lib"


def _index_stdlib_module(path: Path) -> Optional[StdlibModule]:
    """Parse ``path`` once and bucket its symbols.

    Failures (a stdlib file that for any reason doesn't parse) are
    swallowed — the LSP must keep working even if a freshly-edited
    library file is briefly malformed. The caller falls back to no
    cross-file resolution for that module until it parses cleanly
    again.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    parser = _get_parser()
    pre = _preprocess_for_parse(text)
    if not pre.endswith("\n"):
        pre += "\n"
    try:
        tree = parser.parse(pre)
    except Exception:
        logger.exception("stdlib parse failed: %s", path)
        return None
    symbols = _collect_symbols(tree)
    public: Dict[str, Symbol] = {}
    methods: Dict[str, Dict[str, Symbol]] = {}
    for sym in symbols:
        if sym.kind == "class":
            public[sym.name] = sym
            methods.setdefault(sym.name, {})
        elif sym.kind == "function" and not sym.name.startswith("_"):
            public[sym.name] = sym
        elif sym.kind in ("method", "static_method") and sym.parent:
            methods.setdefault(sym.parent, {})[sym.name] = sym
    return StdlibModule(
        path=path,
        mtime=path.stat().st_mtime,
        symbols=symbols,
        public=public,
        methods=methods,
    )


def _stdlib_module(name: str) -> Optional[StdlibModule]:
    """Return the cached index for ``name`` (e.g. ``"lamenv"``).

    Re-indexes on stale mtime, so editing the stdlib in another
    window shows up immediately on the next hover / completion.
    """
    path = _STDLIB_DIR / f"{name}.lam"
    if not path.exists():
        return None
    with _STDLIB_INDEX_LOCK:
        cached = _STDLIB_INDEX.get(name)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return cached
        if cached and cached.mtime == mtime:
            return cached
        fresh = _index_stdlib_module(path)
        if fresh is not None:
            _STDLIB_INDEX[name] = fresh
        return fresh or cached


def _stdlib_module_uri(module: str) -> Optional[str]:
    path = _STDLIB_DIR / f"{module}.lam"
    if not path.exists():
        return None
    return path.resolve().as_uri()


def _get_parser() -> Lark:
    """Cache one Lark instance; building it is the expensive part."""
    global _PARSER
    with _PARSER_LOCK:
        if _PARSER is None:
            grammar_path = PROJECT_ROOT / "lammergeier.lark"
            with open(grammar_path) as f:
                grammar = f.read()
            _PARSER = Lark(
                grammar,
                parser="lalr",
                start="file_input",
                propagate_positions=True,
            )
        return _PARSER


@dataclass
class Symbol:
    """A function, class, method, or top-level variable."""
    name: str
    kind: str   # "function" | "class" | "method" | "variable" | "static_method"
    line: int   # 0-indexed
    col: int    # 0-indexed
    end_line: int = 0
    end_col: int = 0
    detail: str = ""
    parent: str = ""   # class name for methods


@dataclass
class Document:
    uri: str
    text: str
    version: int = 0
    tree: Optional[Tree] = None
    symbols: List[Symbol] = field(default_factory=list)
    parse_error: Optional[Diagnostic] = None
    semantic_diagnostics: List[Diagnostic] = field(default_factory=list)
    # Imported names from ``from lam<x> import a, b``. Maps the local
    # alias the user writes in this document to the originating
    # ``(module, exported_name)`` tuple in the stdlib.
    imports: Dict[str, Tuple[str, str]] = field(default_factory=dict)


@dataclass
class ImportResolution:
    module: str
    path: Path
    symbol: Symbol
    stdlib: Optional[StdlibModule] = None


class LspRequestError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _preprocess_for_parse(source: str) -> str:
    """Return the compiler's shared parse-preprocessed source."""
    return preprocess_for_parse(source).source


_FUNC_RE = re.compile(
    r"^(?P<indent>\s*)(?P<mods>(?:static\s+|private\s+)?)func\s+"
    r"(?P<name>\w+)\s*\((?P<params>[^)]*)\)\s*"
    r"(?:->\s*(?P<ret>[^\{]+?))?\s*\{?",
    re.MULTILINE,
)
_CLASS_RE = re.compile(r"^\s*class\s+(?P<name>\w+)", re.MULTILINE)


def _regex_symbols(source: str) -> List[Symbol]:
    """Best-effort symbol extraction for documents the LALR parser
    rejects.

    Walks the raw source line-by-line, tracking class scope by simple
    brace counting. Picks up classes, top-level functions, and class
    methods (including static / private modifiers). The detail strings
    are slightly less polished than the AST-driven path because we
    don't normalise param types, but they're plenty for hover and
    completion while the user is mid-edit.
    """
    out: List[Symbol] = []
    lines = source.splitlines()
    class_stack: List[Tuple[str, int]] = []  # (name, brace_depth_when_entered)
    depth = 0

    for i, raw in enumerate(lines):
        # Determine the class context for this line.
        cls_match = _CLASS_RE.match(raw)
        if cls_match:
            cname = cls_match.group("name")
            col = raw.index("class")
            out.append(Symbol(
                name=cname, kind="class",
                line=i, col=col, end_line=i, end_col=col + len(cname),
                detail=f"class {cname}",
            ))
            class_stack.append((cname, depth))

        fn = _FUNC_RE.match(raw)
        if fn:
            name = fn.group("name")
            params = (fn.group("params") or "").strip()
            ret = (fn.group("ret") or "").strip()
            mods = (fn.group("mods") or "").strip()
            parent = class_stack[-1][0] if class_stack else ""
            kind = "method" if parent else "function"
            if "static" in mods:
                kind = "static_method"
            detail = f"{mods + ' ' if mods else ''}func {name}({params})"
            if ret:
                detail += f" -> {ret}"
            col = raw.find("func")
            out.append(Symbol(
                name=name, kind=kind,
                line=i, col=col,
                end_line=i, end_col=col + len(name) + 5,
                detail=detail.strip(), parent=parent,
            ))

        # Update brace depth and pop classes when their block closes.
        depth += raw.count("{") - raw.count("}")
        while class_stack and depth <= class_stack[-1][1]:
            class_stack.pop()
    return out


def _collect_imports(tree: Tree) -> Dict[str, Tuple[str, str]]:
    """Walk an ``import_from`` ladder and pull out local-name →
    (module, exported-name) mappings.

    Only ``from <module> import a, b as alias, c`` shapes contribute;
    plain ``import foo`` lines do not bind a directly hoverable symbol.
    """
    out: Dict[str, Tuple[str, str]] = {}

    def module_name(node: Tree) -> str:
        # ``import_from`` uses a ``dotted_name`` for the module side.
        # We flatten it into a single name (``a.b`` → ``a.b``).
        return ".".join(
            str(c.children[0]) if isinstance(c, Tree) and c.children else str(c)
            for c in node.children
        )

    def visit_import_from(node: Tree) -> None:
        module = ""
        names_node: Optional[Tree] = None
        for child in node.children:
            if isinstance(child, Tree):
                if child.data == "dotted_name" and not module:
                    module = module_name(child)
                elif child.data == "import_as_names":
                    names_node = child
                elif child.data == "import_as_name":
                    # Single-name shape: ``from x import y``.
                    if names_node is None:
                        names_node = Tree("import_as_names", [child])
        if not module or names_node is None:
            return
        for entry in names_node.children:
            if not isinstance(entry, Tree) or entry.data != "import_as_name":
                continue
            kids = [c for c in entry.children if isinstance(c, Tree)]
            if not kids:
                continue
            original = _name_token(kids[0])
            if not original:
                continue
            local = _name_token(kids[1]) if len(kids) > 1 else original
            out[local] = (module, original)

    def walk(node) -> None:
        if not isinstance(node, Tree):
            return
        if node.data == "import_from":
            visit_import_from(node)
        for c in node.children:
            walk(c)

    walk(tree)
    return out


def analyze(doc: Document) -> None:
    """Parse the document, populate ``tree``, ``symbols``, ``parse_error``,
    and ``semantic_diagnostics``.

    Three-tier strategy:

    1. Run the full Lark parse. On success we get precise positional
       symbols out of the AST. We then run the semantic checker over
       that AST so the user sees undefined-name / duplicate-member /
       misplaced-flow errors live in their editor.
    2. On parse failure we fall back to a regex-based symbol extractor
       so editor features (hover / completion / outline) keep working
       while the user is mid-edit. The parse error is still emitted as
       a diagnostic so the user sees it in their gutter.
    """
    doc.tree = None
    doc.parse_error = None
    doc.semantic_diagnostics = []
    doc.imports = {}

    parser = _get_parser()
    pre = _preprocess_for_parse(doc.text)
    if not pre.endswith("\n"):
        pre += "\n"

    try:
        tree = parser.parse(pre)
        doc.tree = tree
        doc.symbols = _collect_symbols(tree)
        doc.imports = _collect_imports(tree)
        # Semantic check — best-effort. A bug in the checker should
        # never blank the editor, so any exception is logged and
        # swallowed.
        try:
            from compiler.semantic import SemanticChecker
            checker = SemanticChecker()
            errors = checker.check(tree)
            for err in errors:
                doc.semantic_diagnostics.append(err.to_diagnostic())
        except Exception:
            logger.exception("semantic check failed")
        return
    except UnexpectedInput as e:
        doc.parse_error = make_syntax_diagnostic(e, doc.text, "<buffer>").to_diagnostic()
    except Exception as exc:
        doc.parse_error = Diagnostic(
            code="LAM0000",
            severity=DiagnosticSeverity.ERROR,
            message=f"internal parser error: {exc}",
            span=SourceSpan(file=None, line=1, col=1),
        )
        logger.exception("parse error")

    # Fallback: regex over the original source so positions stay
    # meaningful even for users editing past a syntax error.
    doc.symbols = _regex_symbols(doc.text)
    # Imports are resolvable from the raw text even when the parser
    # gave up — a missing closing brace 200 lines down shouldn't
    # blank out cross-file hover for an import line near the top.
    doc.imports = _regex_imports(doc.text)


_IMPORT_FROM_RE = re.compile(
    r"^\s*from\s+(?P<module>[A-Za-z_][\w.]*)\s+import\s+(?P<names>[^\n#]+)",
    re.MULTILINE,
)


def _regex_imports(source: str) -> Dict[str, Tuple[str, str]]:
    """Best-effort import extractor for documents the LALR parser
    rejects — see ``_regex_symbols`` for the parallel rationale.

    Recognises the same shape the AST walker handles
    (``from <module> import a, b as alias``), including parenthesised
    multi-line lists, and skips trailing comments.
    """
    out: Dict[str, Tuple[str, str]] = {}
    # Normalise parenthesised lists onto one line so the simple
    # regex above catches them too.
    flat = re.sub(r"\(\s*([^)]+?)\s*\)", lambda m: m.group(1).replace("\n", " "),
                   source)
    for m in _IMPORT_FROM_RE.finditer(flat):
        module = m.group("module")
        names = m.group("names").strip().rstrip(",")
        if names == "*":
            # ``import *`` — pull every public name in.
            sm = _stdlib_module(module)
            if sm:
                for n, _sym in sm.public.items():
                    out[n] = (module, n)
            continue
        for raw in names.split(","):
            raw = raw.strip()
            if not raw:
                continue
            parts = raw.split()
            original = parts[0]
            local = parts[2] if len(parts) >= 3 and parts[1] == "as" else original
            out[local] = (module, original)
    return out


def _node_pos(node) -> Tuple[int, int]:
    """Best-effort 0-indexed (line, col) for a Lark Tree or Token."""
    if isinstance(node, Token):
        return (max(0, (node.line or 1) - 1), max(0, (node.column or 1) - 1))
    if isinstance(node, Tree):
        meta = getattr(node, "meta", None)
        if meta and not meta.empty:
            return (max(0, (meta.line or 1) - 1), max(0, (meta.column or 1) - 1))
    return (0, 0)


def _node_end(node) -> Tuple[int, int]:
    if isinstance(node, Token):
        end_line = getattr(node, "end_line", node.line) or 1
        end_col = getattr(node, "end_column", node.column) or 1
        return (max(0, end_line - 1), max(0, end_col - 1))
    if isinstance(node, Tree):
        meta = getattr(node, "meta", None)
        if meta and not meta.empty:
            end_line = getattr(meta, "end_line", None) or meta.line
            end_col = getattr(meta, "end_column", None) or meta.column
            return (max(0, (end_line or 1) - 1), max(0, (end_col or 1) - 1))
    return (0, 0)


def _name_token(node) -> Optional[str]:
    if isinstance(node, Token):
        return str(node)
    if isinstance(node, Tree):
        if node.data == "name" and node.children:
            return str(node.children[0])
        # walk down through one wrapping layer
        for c in node.children:
            n = _name_token(c)
            if n:
                return n
    return None


def _params_signature(params_node) -> str:
    """Render a function's parameter list as a one-liner.

    Walks ``parameters`` / ``typed_parameters`` and unwraps each item
    out of the various wrapper rules the grammar uses
    (``typed_paramvalue``, ``paramvalue``, etc.).
    """
    if not isinstance(params_node, Tree):
        return ""
    parts: List[str] = []
    for child in params_node.children:
        if not isinstance(child, Tree):
            continue
        # Unwrap one level of ``*paramvalue`` / ``typed_paramvalue``.
        target = child
        if child.data in ("typed_paramvalue", "paramvalue"):
            for c in child.children:
                if isinstance(c, Tree):
                    target = c
                    break
        if target.data in ("typed_param", "typed_lambda_param", "typed_default_param"):
            name = _name_token(target.children[0]) or "_"
            type_str = _type_to_str(target.children[1]) if len(target.children) > 1 else "any"
            if target.data == "typed_default_param" and len(target.children) > 2:
                parts.append(f"{name}: {type_str} = ...")
            else:
                parts.append(f"{name}: {type_str}")
        elif target.data in ("param", "name"):
            name = _name_token(target)
            if name:
                parts.append(name)
        elif target.data == "default_param":
            name = _name_token(target.children[0])
            if name:
                parts.append(f"{name} = ...")
        elif target.data == "vararg":
            name = _name_token(target) or "args"
            parts.append(f"*{name}")
        elif target.data == "kwarg":
            name = _name_token(target) or "kwargs"
            parts.append(f"**{name}")
    return ", ".join(parts)


def _innermost_open_paren(source: str) -> Optional[int]:
    stack: List[int] = []
    quote = ""
    escape = False
    for idx, ch in enumerate(source):
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = ""
            continue
        if ch in {'"', "'"}:
            quote = ch
        elif ch == "(":
            stack.append(idx)
        elif ch == ")" and stack:
            stack.pop()
    return stack[-1] if stack else None


def _active_parameter_index(source: str) -> int:
    depth = 0
    active = 0
    quote = ""
    escape = False
    for ch in source:
        if quote:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                quote = ""
            continue
        if ch in {'"', "'"}:
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
        elif ch == "," and depth == 0:
            active += 1
    return active


def _parse_signature_detail(detail: str) -> Optional[Tuple[str, List[str]]]:
    stripped = detail.strip()
    match = re.search(r"\bfunc\s+\w+\s*\((?P<params>.*)\)(?:\s*->\s*[^{]+)?(?:\{|$)", stripped)
    if not match:
        return None
    label = stripped[:match.end()].rstrip()
    if label.endswith("{"):
        label = label[:-1].rstrip()
    params_text = match.group("params").strip()
    params = _split_signature_params(params_text) if params_text else []
    return label, params


def _split_signature_params(params: str) -> List[str]:
    out: List[str] = []
    start = 0
    depth = 0
    for idx, ch in enumerate(params):
        if ch in "([{":
            depth += 1
        elif ch in ")]}" and depth > 0:
            depth -= 1
        elif ch == "," and depth == 0:
            item = params[start:idx].strip()
            if item:
                out.append(item)
            start = idx + 1
    item = params[start:].strip()
    if item:
        out.append(item)
    return out


def _identifier_occurrences(source: str, name: str) -> List[Tuple[int, int, int]]:
    pattern = re.compile(rf"\b{re.escape(name)}\b")
    out: List[Tuple[int, int, int]] = []
    for line_no, line in enumerate(source.splitlines()):
        for match in pattern.finditer(line):
            out.append((line_no, match.start(), match.end()))
    return out


def _type_to_str(node) -> str:
    """Render a ``type_expr`` subtree as Lam-flavoured source text.

    The grammar nests types fairly deeply
    (``type_expr → type_union → type_name → dotted_name → name``);
    this function unwraps each layer so we get a one-liner like
    ``list[int]`` or ``dict[str, any]``.
    """
    if isinstance(node, Token):
        return str(node)
    if isinstance(node, Tree):
        if node.data == "name":
            return str(node.children[0])
        if node.data == "dotted_name":
            return ".".join(_type_to_str(c) for c in node.children if c is not None)
        if node.data == "type_subscript" and node.children:
            base = _type_to_str(node.children[0])
            inner = ", ".join(_type_to_str(c) for c in node.children[1:])
            return f"{base}[{inner}]" if inner else base
        if node.data == "type_func":
            params = ", ".join(_type_to_str(c) for c in (node.children[:-1] or []))
            ret = _type_to_str(node.children[-1]) if node.children else ""
            return f"func({params}) -> {ret}"
        # Generic unwrap for type_expr / type_union / type_name etc.
        for c in node.children:
            s = _type_to_str(c)
            if s:
                return s
    return ""


def _collect_symbols(tree: Tree) -> List[Symbol]:
    """Collect symbols from the canonical declaration AST.

    The Lark walker remains as a fallback while the AST grows; successful
    parse paths should use this AST-backed surface.
    """
    try:
        return _symbols_from_ast(build_module(tree))
    except Exception:
        logger.exception("AST symbol extraction failed; falling back to Lark symbols")
        return _collect_symbols_from_lark(tree)


def _symbols_from_ast(module: Module) -> List[Symbol]:
    out: List[Symbol] = []
    for decl in module.body:
        if isinstance(decl, FuncDecl):
            out.append(_func_symbol(decl))
        elif isinstance(decl, ClassDecl):
            out.append(_class_symbol(decl))
            for field in decl.fields:
                out.append(_var_symbol(field, parent=decl.name))
            for method in decl.methods:
                out.append(_func_symbol(method))
        elif isinstance(decl, InterfaceDecl):
            if decl.span is not None:
                line, col, end_line, end_col = _span_parts(decl.span)
                out.append(Symbol(
                    name=decl.name,
                    kind="class",
                    line=line,
                    col=col,
                    end_line=end_line,
                    end_col=end_col,
                    detail=f"interface {decl.name}",
                ))
        elif isinstance(decl, VarDecl):
            out.append(_var_symbol(decl))
    return out


def _func_symbol(decl: FuncDecl) -> Symbol:
    line, col, end_line, end_col = _span_parts(decl.span)
    prefix = "static " if decl.is_static else ("private " if decl.is_private else "")
    detail = f"{prefix}func {decl.name}({_param_list(decl.params)})"
    if decl.return_type is not None and decl.return_type.name:
        detail += f" -> {decl.return_type.name}"
    kind = "static_method" if decl.is_static else ("method" if decl.parent else "function")
    return Symbol(
        name=decl.name,
        kind=kind,
        line=line,
        col=col,
        end_line=end_line,
        end_col=end_col,
        detail=detail,
        parent=decl.parent or "",
    )


def _class_symbol(decl: ClassDecl) -> Symbol:
    span = decl.span
    if span is None:
        return Symbol(name=decl.name, kind="class", line=0, col=0, detail=f"class {decl.name}")
    line, col, end_line, end_col = _span_parts(span)
    return Symbol(
        name=decl.name,
        kind="class",
        line=line,
        col=col,
        end_line=end_line,
        end_col=end_col,
        detail=f"class {decl.name}",
    )


def _var_symbol(decl: VarDecl, *, parent: str = "") -> Symbol:
    line, col, end_line, end_col = _span_parts(decl.span)
    type_name = decl.type_ref.name if decl.type_ref is not None else ""
    if decl.is_const:
        detail = f"const {decl.name}: {type_name}" if type_name else f"const {decl.name}"
    else:
        detail = f"{decl.name}: {type_name}" if type_name else decl.name
    return Symbol(
        name=decl.name,
        kind="variable",
        line=line,
        col=col,
        end_line=end_line,
        end_col=end_col,
        detail=detail,
        parent=parent,
    )


def _param_list(params: List[Param]) -> str:
    parts: list[str] = []
    for param in params:
        prefix = param.variadic or ""
        if param.type_ref is not None and param.type_ref.name:
            item = f"{prefix}{param.name}: {param.type_ref.name}"
        else:
            item = f"{prefix}{param.name}"
        if param.has_default:
            item += " = ..."
        parts.append(item)
    return ", ".join(parts)


def _span_parts(span: SourceSpan) -> tuple[int, int, int, int]:
    line = max(0, span.line - 1)
    col = max(0, span.col - 1)
    end_line = max(0, (span.end_line or span.line) - 1)
    end_col = max(0, (span.end_col or span.col) - 1)
    return line, col, end_line, end_col


def _collect_symbols_from_lark(tree: Tree) -> List[Symbol]:
    """Walk the AST and pull out every top-level def + class methods."""
    out: List[Symbol] = []

    def visit_funcdef(node: Tree, parent_class: str = "") -> None:
        # Possible shapes: (decorator?)* "func"|"static"|"private" name params (-> ret)? suite
        is_static = False
        is_private = False
        name_node = None
        params_node = None
        return_node = None

        for child in node.children:
            if isinstance(child, Token):
                tok = str(child)
                if tok == "static":
                    is_static = True
                elif tok == "private":
                    is_private = True
                continue
            if isinstance(child, Tree):
                if child.data == "name" and name_node is None:
                    name_node = child
                elif child.data in ("parameters", "typed_parameters"):
                    params_node = child
                elif child.data in ("return_type", "single_return_type"):
                    return_node = child

        name = _name_token(name_node) or "<anon>"
        line, col = _node_pos(node)
        end_line, end_col = _node_end(node)

        sig = _params_signature(params_node) if params_node else ""
        ret_type = ""
        if return_node and return_node.children:
            # Skip the wrapping single_return_type / return_type and look
            # for the first type_expr-ish child.
            for c in return_node.children:
                s = _type_to_str(c)
                if s:
                    ret_type = s
                    break
        prefix = "static " if is_static else ("private " if is_private else "")
        detail = f"{prefix}func {name}({sig})"
        if ret_type:
            detail += f" -> {ret_type}"

        kind = "method" if parent_class else "function"
        if is_static:
            kind = "static_method"

        out.append(Symbol(
            name=name, kind=kind,
            line=line, col=col,
            end_line=end_line, end_col=end_col,
            detail=detail, parent=parent_class,
        ))

    def visit_classdef(node: Tree) -> None:
        name = "<class>"
        suite = None
        for child in node.children:
            if isinstance(child, Tree):
                if child.data == "name" and name == "<class>":
                    name = _name_token(child) or "<class>"
                elif child.data == "suite":
                    suite = child
        line, col = _node_pos(node)
        end_line, end_col = _node_end(node)
        out.append(Symbol(
            name=name, kind="class",
            line=line, col=col,
            end_line=end_line, end_col=end_col,
            detail=f"class {name}",
        ))
        if suite:
            for stmt in suite.children:
                if isinstance(stmt, Tree) and stmt.data == "funcdef":
                    visit_funcdef(stmt, parent_class=name)
                # Class field — ``self.x: T = ...`` inside ``__init__``
                # already gets picked up via the function's scope walk
                # below; here we only want top-level assignments to
                # static class fields.
                elif isinstance(stmt, Tree) and stmt.data in (
                        "annassign", "assign_stmt", "const_stmt", "simple_stmt"):
                    for tgt, detail in _assign_bindings(stmt):
                        line2, col2 = _node_pos(stmt)
                        out.append(Symbol(
                            name=tgt, kind="variable",
                            line=line2, col=col2,
                            end_line=line2,
                            end_col=col2 + len(tgt),
                            detail=detail or f"{tgt}",
                            parent=name,
                        ))

    def visit_top_assign(node: Tree) -> None:
        """Module-level ``x: int = 10`` / ``const PI = 3.14`` — publish
        each LHS name as a ``variable`` symbol so completion and hover
        can find it."""
        for tgt, detail in _assign_bindings(node):
            line, col = _node_pos(node)
            out.append(Symbol(
                name=tgt, kind="variable",
                line=line, col=col,
                end_line=line, end_col=col + len(tgt),
                detail=detail or f"{tgt}",
            ))

    def walk(node) -> None:
        if not isinstance(node, Tree):
            return
        if node.data == "funcdef":
            visit_funcdef(node)
            return
        if node.data == "classdef":
            visit_classdef(node)
            return
        # Module-level assignments land in ``file_input`` / ``simple_stmt``
        # / ``suite`` wrappers. Only pick them up at the top level —
        # inner-scope locals are resolved by ``_scope_locals_at``.
        if node.data in ("annassign", "const_stmt"):
            visit_top_assign(node)
            return
        if node.data == "assign_stmt":
            # Only treat as "top-level variable" if we're walking the
            # outer file — the walker below recurses from ``file_input``
            # so the first ``assign_stmt`` we hit is at module scope.
            visit_top_assign(node)
            return
        for c in node.children:
            walk(c)

    walk(tree)
    return out


def _assign_bindings(node: Tree) -> List[Tuple[str, str]]:
    """Return ``[(name, detail), …]`` for one assignment-like node.

    Handles:
      * ``annassign`` — ``x: int = value``
      * ``assign_stmt`` — ``x = value`` / ``a, b = …``
      * ``const_stmt`` — ``const x: T = value`` / ``const x = value``
      * ``simple_stmt`` wrappers for the above

    ``detail`` is a short "``x: int``" string for the hover / signature
    column; empty when no type annotation is present.
    """
    if not isinstance(node, Tree):
        return []

    # Peel through ``simple_stmt`` and any single-child wrappers.
    inner: List[Tree] = []
    if node.data == "simple_stmt":
        for c in node.children:
            if isinstance(c, Tree):
                inner.extend(_assign_bindings(c))
        return inner

    results: List[Tuple[str, str]] = []

    if node.data == "annassign":
        # children: name, type_expr, (optional) rhs
        kids = [c for c in node.children if isinstance(c, Tree)]
        if not kids:
            return results
        name = _name_token(kids[0])
        type_str = _type_to_str(kids[1]) if len(kids) > 1 else ""
        if name:
            detail = f"{name}: {type_str}" if type_str else name
            results.append((name, detail))

    elif node.data == "const_stmt":
        # ``const x: T = value`` or ``const x = value``.
        kids = [c for c in node.children if isinstance(c, Tree)]
        if not kids:
            return results
        name = _name_token(kids[0])
        type_str = ""
        if len(kids) > 1 and kids[1].data not in ("test", "expr"):
            type_str = _type_to_str(kids[1])
        if name:
            detail = f"const {name}: {type_str}" if type_str else f"const {name}"
            results.append((name, detail))

    elif node.data == "assign_stmt":
        # ``x = rhs`` — LHS is the first child. Handle ``a, b = …``
        # by walking the name-tuple shape.
        if not node.children:
            return results
        lhs = node.children[0]
        if isinstance(lhs, Tree):
            if lhs.data == "name":
                n = _name_token(lhs)
                if n:
                    results.append((n, n))
            elif lhs.data in ("testlist_star_expr", "tuple_target", "exprlist"):
                for sub in lhs.children:
                    n = _name_token(sub)
                    if n and n != "_":
                        results.append((n, n))
            else:
                n = _name_token(lhs)
                if n and n != "_":
                    results.append((n, n))

    return results


def _scope_locals_at(tree: Tree, line: int) -> List[Symbol]:
    """Return the parameter list + local assignments of the ``funcdef``
    enclosing ``line`` (0-indexed).

    The walker finds the deepest ``funcdef`` whose source range contains
    the cursor and emits one ``variable``-kind ``Symbol`` per parameter
    and per assignment target. Falls back to an empty list if the
    cursor is at module scope. Best-effort: when positions are missing
    on the AST we skip silently instead of flooding completion with
    false hits.
    """
    locals_out: List[Symbol] = []

    def contains(node: Tree) -> bool:
        meta = getattr(node, "meta", None)
        if not meta or meta.empty:
            return False
        start = (meta.line or 1) - 1
        end = (getattr(meta, "end_line", None) or start + 1) - 1
        return start <= line <= end

    def find_enclosing(node, best=None):
        if not isinstance(node, Tree):
            return best
        if node.data == "funcdef" and contains(node):
            best = node
        for c in node.children:
            best = find_enclosing(c, best)
        return best

    func = find_enclosing(tree)
    if func is None:
        return []

    def collect_params(params_node: Tree) -> None:
        for child in params_node.children:
            if isinstance(child, Tree):
                target = child
                if child.data in ("typed_paramvalue", "paramvalue"):
                    for c in child.children:
                        if isinstance(c, Tree):
                            target = c
                            break
                if target.data in (
                        "typed_param", "typed_lambda_param",
                        "typed_default_param", "default_param",
                        "param", "name", "vararg", "kwarg"):
                    name = _name_token(target)
                    type_str = ""
                    if target.data in ("typed_param",
                                         "typed_default_param",
                                         "typed_lambda_param") \
                            and len(target.children) > 1:
                        type_str = _type_to_str(target.children[1])
                    if name:
                        detail = f"{name}: {type_str}" if type_str else f"param {name}"
                        locals_out.append(Symbol(
                            name=name, kind="variable",
                            line=0, col=0, detail=detail,
                        ))
            elif isinstance(child, Token):
                tok = str(child)
                if tok.isidentifier():
                    locals_out.append(Symbol(
                        name=tok, kind="variable",
                        line=0, col=0, detail=f"param {tok}",
                    ))

    def walk_body(node) -> None:
        if not isinstance(node, Tree):
            return
        # Don't descend into nested funcdef/classdef — their locals
        # aren't visible in the outer scope.
        if node.data == "funcdef" and node is not func:
            return
        if node.data == "classdef":
            return
        if node.data in ("annassign", "assign_stmt", "const_stmt"):
            for tgt, detail in _assign_bindings(node):
                locals_out.append(Symbol(
                    name=tgt, kind="variable",
                    line=0, col=0, detail=detail or tgt,
                ))
            return
        if node.data == "for_stmt":
            # The loop variable is the first ``name``/``exprlist`` child.
            if node.children and isinstance(node.children[0], Tree):
                n = _name_token(node.children[0])
                if n and n != "_":
                    locals_out.append(Symbol(
                        name=n, kind="variable",
                        line=0, col=0, detail=f"for-target {n}",
                    ))
        for c in node.children:
            walk_body(c)

    for child in func.children:
        if isinstance(child, Tree) and child.data in ("parameters", "typed_parameters"):
            collect_params(child)

    walk_body(func)

    # De-duplicate by name — inner shadows outer, but we only want
    # one entry per identifier in the completion list.
    seen: set = set()
    uniq: List[Symbol] = []
    for s in locals_out:
        if s.name in seen:
            continue
        seen.add(s.name)
        uniq.append(s)
    return uniq


def _enclosing_func_line_range(tree: Tree, line: int) -> Optional[Tuple[int, int]]:
    def contains(node: Tree) -> bool:
        meta = getattr(node, "meta", None)
        if not meta or meta.empty:
            return False
        start = (meta.line or 1) - 1
        end = (getattr(meta, "end_line", None) or start + 1) - 1
        return start <= line <= end

    def find_enclosing(node, best=None):
        if not isinstance(node, Tree):
            return best
        if node.data == "funcdef" and contains(node):
            best = node
        for c in node.children:
            best = find_enclosing(c, best)
        return best

    func = find_enclosing(tree)
    if func is None:
        return None
    meta = getattr(func, "meta", None)
    if not meta or meta.empty:
        return None
    start = (meta.line or 1) - 1
    end = (getattr(meta, "end_line", None) or start + 1) - 1
    return start, end


# ────────────────────────────────────────────────────────────
# LSP server core
# ────────────────────────────────────────────────────────────

class LspServer:
    def __init__(self, stdin, stdout):
        self.stdin = stdin
        self.stdout = stdout
        self.docs: Dict[str, Document] = {}
        self.workspace_root = PROJECT_ROOT
        self.workspace = WorkspaceIndex(self.workspace_root)
        self.shutdown_requested = False
        self.handlers: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "initialize": self.on_initialize,
            "initialized": self.on_initialized,
            "shutdown": self.on_shutdown,
            "exit": self.on_exit,
            "textDocument/didOpen": self.on_did_open,
            "textDocument/didChange": self.on_did_change,
            "textDocument/didClose": self.on_did_close,
            "textDocument/hover": self.on_hover,
            "textDocument/signatureHelp": self.on_signature_help,
            "textDocument/completion": self.on_completion,
            "textDocument/definition": self.on_definition,
            "textDocument/references": self.on_references,
            "textDocument/prepareRename": self.on_prepare_rename,
            "textDocument/rename": self.on_rename,
            "textDocument/formatting": self.on_formatting,
            "textDocument/documentSymbol": self.on_document_symbol,
        }

    # ── transport ───────────────────────────────────────────
    def serve(self) -> None:
        logger.info("lammergeier-lsp started")
        while True:
            msg = _read_message(self.stdin)
            if msg is None:
                logger.info("eof")
                return
            self._dispatch(msg)
            if self.shutdown_requested and msg.get("method") == "exit":
                return

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        msg_id = msg.get("id")
        try:
            if method in self.handlers:
                result = self.handlers[method](msg)
                if msg_id is not None and result is not None:
                    self._respond(msg_id, result)
                elif msg_id is not None:
                    self._respond(msg_id, None)
            elif msg_id is not None:
                # Unknown request — return MethodNotFound rather than silently dropping.
                self._respond_error(msg_id, -32601, f"method not found: {method}")
        except LspRequestError as e:
            if msg_id is not None:
                self._respond_error(msg_id, e.code, e.message)
        except Exception as e:  # pragma: no cover — defensive net
            logger.exception("handler crashed")
            if msg_id is not None:
                self._respond_error(msg_id, -32603, f"internal error: {e}")

    def _respond(self, msg_id: Any, result: Any) -> None:
        _write_message(self.stdout, {"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _respond_error(self, msg_id: Any, code: int, message: str) -> None:
        _write_message(self.stdout, {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": code, "message": message},
        })

    def _notify(self, method: str, params: Any) -> None:
        _write_message(self.stdout, {"jsonrpc": "2.0", "method": method, "params": params})

    def _index_doc(self, doc: Document) -> None:
        path = _uri_to_path(doc.uri)
        if path is None:
            return
        try:
            self.workspace.update_file(path, doc.text)
        except Exception:
            logger.exception("workspace index update failed")

    # ── lifecycle ───────────────────────────────────────────
    def on_initialize(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        params = msg.get("params") or {}
        root_uri = params.get("rootUri") or ""
        root_path = _uri_to_path(root_uri)
        if root_path is not None:
            self.workspace_root = root_path
            self.workspace = WorkspaceIndex(self.workspace_root)
        return {
            "capabilities": {
                "textDocumentSync": 1,  # full document sync
                "hoverProvider": True,
                "signatureHelpProvider": {"triggerCharacters": ["(", ","]},
                "completionProvider": {"triggerCharacters": [".", "@"]},
                "definitionProvider": True,
                "referencesProvider": True,
                "renameProvider": {"prepareProvider": True},
                "documentFormattingProvider": True,
                "documentSymbolProvider": True,
            },
            "serverInfo": {"name": "lammergeier-lsp", "version": "0.1.0"},
        }

    def on_initialized(self, msg: Dict[str, Any]) -> None:
        return None

    def on_shutdown(self, msg: Dict[str, Any]) -> None:
        self.shutdown_requested = True
        return None

    def on_exit(self, msg: Dict[str, Any]) -> None:
        return None

    # ── document sync ───────────────────────────────────────
    def on_did_open(self, msg: Dict[str, Any]) -> None:
        params = msg.get("params") or {}
        td = params.get("textDocument") or {}
        uri = td.get("uri", "")
        text = td.get("text", "")
        version = td.get("version", 0)
        doc = Document(uri=uri, text=text, version=version)
        analyze(doc)
        self.docs[uri] = doc
        self._index_doc(doc)
        self._publish_diagnostics(doc)

    def on_did_change(self, msg: Dict[str, Any]) -> None:
        params = msg.get("params") or {}
        td = params.get("textDocument") or {}
        uri = td.get("uri", "")
        version = td.get("version", 0)
        changes = params.get("contentChanges") or []
        if not changes:
            return
        # Full sync — take the last change's full text.
        text = changes[-1].get("text", "")
        doc = self.docs.get(uri) or Document(uri=uri, text=text)
        doc.text = text
        doc.version = version
        analyze(doc)
        self.docs[uri] = doc
        self._index_doc(doc)
        self._publish_diagnostics(doc)

    def on_did_close(self, msg: Dict[str, Any]) -> None:
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri", "")
        self.docs.pop(uri, None)
        path = _uri_to_path(uri)
        if path is not None:
            self.workspace.remove_file(path)
        # Clear diagnostics on close.
        self._notify("textDocument/publishDiagnostics", {
            "uri": uri, "diagnostics": [],
        })

    # ── diagnostics ─────────────────────────────────────────
    def _publish_diagnostics(self, doc: Document) -> None:
        diags: List[Dict[str, Any]] = []
        if doc.parse_error is not None:
            diags.append(diagnostic_to_lsp(doc.parse_error))
        # Semantic diagnostics (undefined names, duplicate members,
        # misplaced flow). Each entry is widened to a one-token range
        # so editors can underline a recognisable region; computing
        # the *exact* identifier extent would require re-walking the
        # AST and is more cost than it's worth at this point.
        for diag in doc.semantic_diagnostics:
            # Try to extract the offending identifier from the message
            # so the underline width matches the symbol the user
            # actually misspelt. Falls back to a single character.
            import re as _re
            m = _re.search(r"`([^`]+)`", diag.message)
            width = max(1, len(m.group(1))) if m else 1
            diags.append(diagnostic_to_lsp(diag, default_width=width))
        self._notify("textDocument/publishDiagnostics", {
            "uri": doc.uri, "diagnostics": diags,
        })

    # ── hover ───────────────────────────────────────────────
    def on_hover(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        line = pos.get("line", 0)
        char = pos.get("character", 0)
        word = self._word_at(uri, line, char)
        if not word:
            return None
        doc = self.docs.get(uri)
        # Dotted-member hover: ``Foo.bar`` — show the method signature
        # whether ``bar`` is local or comes from an imported class.
        receiver = self._receiver_at(uri, line, char)
        if doc and receiver:
            method = self._lookup_method(doc, receiver, word)
            if method:
                origin = self._origin_module_for(doc, receiver)
                detail = method.detail or method.name
                if origin:
                    detail = f"# from {origin}\n{detail}"
                return {
                    "contents": {
                        "kind": "markdown",
                        "value": f"```lam\n{detail}\n```",
                    }
                }
        sym = self._lookup_symbol(uri, word)
        if not sym:
            return None
        body = sym.detail or sym.name
        # Annotate cross-file resolutions so the user knows which lib
        # file owns the symbol.
        if doc and doc.imports.get(word):
            module, _ = doc.imports[word]
            body = f"# from {module}\n{body}"
        return {
            "contents": {
                "kind": "markdown",
                "value": f"```lam\n{body}\n```",
            }
        }

    def on_signature_help(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        line = pos.get("line", 0)
        char = pos.get("character", 0)
        doc = self.docs.get(uri)
        if not doc:
            return None
        call = self._call_context_at(doc, line, char)
        if call is None:
            return None
        callee, active = call
        sym = self._lookup_callable(doc, uri, callee)
        if sym is None:
            return None
        signature = self._signature_help_for_symbol(sym, active)
        if signature is None:
            return None
        return signature

    # ── completion ──────────────────────────────────────────
    def on_completion(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        line = pos.get("line", 0)
        char = pos.get("character", 0)
        doc = self.docs.get(uri)
        if not doc:
            return {"isIncomplete": False, "items": []}
        # Look at the line up to the cursor.
        line_text = ""
        try:
            line_text = doc.text.splitlines()[line][:char]
        except IndexError:
            pass
        items: List[Dict[str, Any]] = []

        # ``from <module> import |`` — suggest exported names from
        # the stdlib module the user is targeting.
        from_match = re.match(
            r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.*)$", line_text)
        if from_match:
            module = from_match.group(1)
            sm = _stdlib_module(module)
            already = {n.strip().split()[0]
                        for n in from_match.group(2).split(",")
                        if n.strip()}
            if sm:
                # Comma-separated suggestions; offer everything that
                # hasn't already appeared on this line.
                for name, sym in sm.public.items():
                    if name in already:
                        continue
                    items.append(self._completion_item(sym))
                return {"isIncomplete": False, "items": items}
            path = _uri_to_path(uri)
            if path is not None:
                mod_path = self.workspace.resolve_module(path, module)
                if mod_path is not None:
                    facts = self.workspace.facts_by_path.get(mod_path) or self.workspace.update_file(mod_path)
                    for name, export in facts.exports.items():
                        if name in already:
                            continue
                        items.append(self._completion_item(self._symbol_from_export(export)))
                    return {"isIncomplete": False, "items": items}

        # Member completion — `Foo.<here>`. Looks at:
        #   1. Methods of a class declared in this document.
        #   2. Methods of an *imported* class (e.g. ``Dotenv.parse``).
        m = re.search(r"(\w+)\.\s*$", line_text)
        if m:
            target = m.group(1)
            seen = set()
            for sym in doc.symbols:
                if sym.parent == target and sym.name not in seen:
                    seen.add(sym.name)
                    items.append(self._completion_item(sym))
            # Cross-file: if ``target`` is an imported class, show its
            # methods too.
            resolved = self._resolve_import(doc, target)
            if resolved is not None:
                if resolved.stdlib is not None:
                    bucket = resolved.stdlib.methods.get(resolved.symbol.name, {})
                    for name, msym in bucket.items():
                        if name in seen:
                            continue
                        seen.add(name)
                        items.append(self._completion_item(msym))
            return {"isIncomplete": False, "items": items}

        # Identifier completion (the cursor is on a bare name or at a
        # whitespace/punctuation boundary). We collect, in this order:
        #   1. Locals + parameters of the enclosing function (if any).
        #   2. Top-level functions, classes, and module-scope variables
        #      declared in this document.
        #   3. Names imported from stdlib modules.
        #   4. A curated keyword set as low-priority fallback items.
        seen = set()

        # 1) Scope-aware locals — parameters and in-function assignments
        #    of the function containing the cursor. Gives real
        #    IntelliSense for variables the user has actually bound.
        if doc.tree is not None:
            for sym in _scope_locals_at(doc.tree, line):
                if sym.name in seen:
                    continue
                seen.add(sym.name)
                items.append(self._completion_item(sym))

        # 2) Top-level symbols in this document: functions, classes,
        #    and module-level variables.
        for sym in doc.symbols:
            if sym.parent:
                # Methods / class fields — surface only via dot
                # completion on the owning class.
                continue
            if sym.kind not in ("function", "class", "variable"):
                continue
            if sym.name in seen:
                continue
            seen.add(sym.name)
            items.append(self._completion_item(sym))

        # 3) Imported names.
        for local_name in doc.imports:
            if local_name in seen:
                continue
            resolved = self._resolve_import(doc, local_name)
            if resolved is None:
                continue
            seen.add(local_name)
            sym = resolved.symbol
            # Re-label the item with the local alias (so ``import X as Y``
            # offers ``Y``) while keeping the upstream signature.
            items.append({
                "label": local_name,
                "kind": 7 if sym.kind == "class" else 3,
                "detail": sym.detail,
            })

        # 4) Keywords for convenience.
        for kw in ("if", "elif", "else", "for", "while", "func", "class",
                   "return", "import", "from", "static", "private", "go!",
                   "true", "false", "None", "print", "len", "range"):
            if kw in seen:
                continue
            items.append({"label": kw, "kind": 14})  # 14 == Keyword
        return {"isIncomplete": False, "items": items}

    @staticmethod
    def _completion_item(sym: Symbol) -> Dict[str, Any]:
        # LSP CompletionItemKind: Function=3, Class=7, Method=2, Variable=6
        kind_map = {
            "function": 3, "class": 7, "method": 2,
            "static_method": 2, "variable": 6,
        }
        return {
            "label": sym.name,
            "kind": kind_map.get(sym.kind, 1),
            "detail": sym.detail,
        }

    # ── go-to-definition ────────────────────────────────────
    def on_definition(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        line = pos.get("line", 0)
        char = pos.get("character", 0)
        word = self._word_at(uri, line, char)
        if not word:
            return None
        doc = self.docs.get(uri)
        # Dotted-member jump: ``Foo.bar`` where ``Foo`` is local or
        # imported. Mint a Location pointing at the method declaration.
        receiver = self._receiver_at(uri, line, char)
        if doc and receiver:
            method = self._lookup_method(doc, receiver, word)
            if method:
                target_uri = self._resolve_uri_for(doc, receiver, uri)
                return {
                    "uri": target_uri,
                    "range": {
                        "start": {"line": method.line, "character": method.col},
                        "end":   {"line": method.end_line or method.line,
                                   "character": method.end_col or (method.col + len(method.name))},
                    },
                }
        sym = self._lookup_symbol(uri, word)
        if not sym:
            return None
        # Imported symbols live in a stdlib file — return that file's
        # URI so the editor opens it.
        target_uri = uri
        if doc:
            resolved = self._resolve_import(doc, word)
            if resolved is not None:
                target_uri = resolved.path.as_uri()
        return {
            "uri": target_uri,
            "range": {
                "start": {"line": sym.line, "character": sym.col},
                "end":   {"line": sym.end_line or sym.line,
                           "character": sym.end_col or (sym.col + len(sym.name))},
            },
        }

    def on_references(self, msg: Dict[str, Any]) -> List[Dict[str, Any]]:
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        context = params.get("context") or {}
        include_declaration = bool(context.get("includeDeclaration", True))
        line = pos.get("line", 0)
        char = pos.get("character", 0)
        word = self._word_at(uri, line, char)
        doc = self.docs.get(uri)
        if not word or doc is None:
            return []
        target_uri, target_name, declaration = self._reference_target(doc, uri, word)
        locations: List[Dict[str, Any]] = []
        seen: set[Tuple[str, int, int]] = set()
        for search_uri, search_doc in self._reference_docs(target_uri).items():
            names = self._reference_names_for_doc(search_doc, search_uri, target_uri, target_name)
            for name in sorted(names):
                for start_line, start_col, end_col in _identifier_occurrences(search_doc.text, name):
                    key = (search_uri, start_line, start_col)
                    if key in seen:
                        continue
                    if not include_declaration and declaration == key:
                        continue
                    seen.add(key)
                    locations.append({
                        "uri": search_uri,
                        "range": {
                            "start": {"line": start_line, "character": start_col},
                            "end": {"line": start_line, "character": end_col},
                        },
                    })
        locations.sort(key=lambda loc: (
            loc["uri"],
            loc["range"]["start"]["line"],
            loc["range"]["start"]["character"],
        ))
        return locations

    def on_prepare_rename(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        line = pos.get("line", 0)
        char = pos.get("character", 0)
        word = self._word_at(uri, line, char)
        doc = self.docs.get(uri)
        if not word or doc is None:
            return None
        self._ensure_rename_allowed(doc, uri, word, line)
        word_range = self._word_range_at(uri, line, char)
        if word_range is None:
            return None
        start_col, end_col = word_range
        return {
            "range": {
                "start": {"line": line, "character": start_col},
                "end": {"line": line, "character": end_col},
            },
            "placeholder": word,
        }

    def on_rename(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri", "")
        pos = params.get("position") or {}
        new_name = params.get("newName", "")
        line = pos.get("line", 0)
        char = pos.get("character", 0)
        word = self._word_at(uri, line, char)
        doc = self.docs.get(uri)
        if not word or doc is None:
            raise LspRequestError(-32602, "rename target is not a Lam identifier")
        if not re.fullmatch(r"[A-Za-z_]\w*", new_name):
            raise LspRequestError(-32602, f"invalid Lam identifier for rename: {new_name!r}")
        self._ensure_rename_allowed(doc, uri, word, line)
        edits: List[Dict[str, Any]] = []
        scope_range = None
        if self._lookup_symbol(uri, word) is None and doc.tree is not None:
            scope_range = _enclosing_func_line_range(doc.tree, line)
        for start_line, start_col, end_col in _identifier_occurrences(doc.text, word):
            if scope_range is not None and not (scope_range[0] <= start_line <= scope_range[1]):
                continue
            edits.append({
                "range": {
                    "start": {"line": start_line, "character": start_col},
                    "end": {"line": start_line, "character": end_col},
                },
                "newText": new_name,
            })
        return {"changes": {uri: edits}}

    def on_formatting(self, msg: Dict[str, Any]) -> List[Dict[str, Any]]:
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri", "")
        doc = self.docs.get(uri)
        if doc is None:
            return []
        try:
            result = format_lam_source(doc.text)
        except FormatError as e:
            raise LspRequestError(-32602, f"cannot format document: {e}") from e
        if not result.changed:
            return []
        lines = doc.text.splitlines()
        end_line = len(lines)
        end_char = 0
        if lines:
            end_line = len(lines) - 1
            end_char = len(lines[-1])
            if doc.text.endswith("\n"):
                end_line += 1
                end_char = 0
        return [{
            "range": {
                "start": {"line": 0, "character": 0},
                "end": {"line": end_line, "character": end_char},
            },
            "newText": result.text,
        }]

    # ── document symbol (outline) ───────────────────────────
    def on_document_symbol(self, msg: Dict[str, Any]) -> List[Dict[str, Any]]:
        params = msg.get("params") or {}
        uri = (params.get("textDocument") or {}).get("uri", "")
        doc = self.docs.get(uri)
        if not doc:
            return []
        # SymbolKind: Function=12, Class=5, Method=6, Variable=13, Constant=14
        kind_map = {
            "function": 12, "class": 5, "method": 6,
            "static_method": 6, "variable": 13,
        }
        # First pass: build per-class buckets so we can nest methods.
        classes: Dict[str, Dict[str, Any]] = {}
        top_level: List[Dict[str, Any]] = []

        for sym in doc.symbols:
            entry = {
                "name": sym.name,
                "kind": kind_map.get(sym.kind, 1),
                "detail": sym.detail,
                "range": {
                    "start": {"line": sym.line, "character": sym.col},
                    "end":   {"line": sym.end_line or sym.line,
                              "character": sym.end_col or (sym.col + len(sym.name))},
                },
                "selectionRange": {
                    "start": {"line": sym.line, "character": sym.col},
                    "end":   {"line": sym.line, "character": sym.col + len(sym.name)},
                },
                "children": [],
            }
            if sym.kind == "class":
                classes[sym.name] = entry
                top_level.append(entry)
            elif sym.parent and sym.parent in classes:
                classes[sym.parent]["children"].append(entry)
            else:
                top_level.append(entry)
        return top_level

    # ── helpers ─────────────────────────────────────────────
    def _word_at(self, uri: str, line: int, character: int) -> Optional[str]:
        doc = self.docs.get(uri)
        if not doc:
            return None
        try:
            row = doc.text.splitlines()[line]
        except IndexError:
            return None
        if character > len(row):
            character = len(row)
        # Expand to identifier (\w+).
        start = character
        while start > 0 and (row[start - 1].isalnum() or row[start - 1] == "_"):
            start -= 1
        end = character
        while end < len(row) and (row[end].isalnum() or row[end] == "_"):
            end += 1
        if start == end:
            return None
        return row[start:end]

    def _word_range_at(self, uri: str, line: int, character: int) -> Optional[Tuple[int, int]]:
        doc = self.docs.get(uri)
        if not doc:
            return None
        try:
            row = doc.text.splitlines()[line]
        except IndexError:
            return None
        if character > len(row):
            character = len(row)
        start = character
        while start > 0 and (row[start - 1].isalnum() or row[start - 1] == "_"):
            start -= 1
        end = character
        while end < len(row) and (row[end].isalnum() or row[end] == "_"):
            end += 1
        if start == end:
            return None
        return start, end

    def _lookup_symbol(self, uri: str, name: str) -> Optional[Symbol]:
        doc = self.docs.get(uri)
        if not doc:
            return None
        for sym in doc.symbols:
            if sym.name == name:
                return sym
        # Cross-file resolution for imported names.
        resolved = self._resolve_import(doc, name)
        if resolved is not None:
            return resolved.symbol
        return None

    def _lookup_callable(self, doc: Document, uri: str, callee: str) -> Optional[Symbol]:
        if "." in callee:
            receiver, method = callee.rsplit(".", 1)
            return self._lookup_method(doc, receiver, method)
        sym = self._lookup_symbol(uri, callee)
        if sym and sym.kind in {"function", "method", "static_method"}:
            return sym
        return None

    @staticmethod
    def _call_context_at(doc: Document, line: int, character: int) -> Optional[Tuple[str, int]]:
        lines = doc.text.splitlines()
        if line >= len(lines):
            return None
        character = min(character, len(lines[line]))
        prefix = "\n".join([*lines[:line], lines[line][:character]])
        open_index = _innermost_open_paren(prefix)
        if open_index is None:
            return None
        callee_end = open_index
        callee_start = callee_end
        while callee_start > 0 and prefix[callee_start - 1].isspace():
            callee_start -= 1
            callee_end -= 1
        while callee_start > 0 and (
            prefix[callee_start - 1].isalnum()
            or prefix[callee_start - 1] in {"_", "."}
        ):
            callee_start -= 1
        callee = prefix[callee_start:callee_end]
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", callee or ""):
            return None
        active = _active_parameter_index(prefix[open_index + 1:])
        return callee, active

    @staticmethod
    def _signature_help_for_symbol(sym: Symbol, active_parameter: int) -> Optional[Dict[str, Any]]:
        parsed = _parse_signature_detail(sym.detail)
        if parsed is None:
            return None
        label, parameters = parsed
        if parameters:
            active_parameter = min(active_parameter, len(parameters) - 1)
        else:
            active_parameter = 0
        return {
            "signatures": [{
                "label": label,
                "parameters": [{"label": param} for param in parameters],
            }],
            "activeSignature": 0,
            "activeParameter": active_parameter,
        }

    def _reference_target(
        self,
        doc: Document,
        uri: str,
        word: str,
    ) -> Tuple[str, str, Optional[Tuple[str, int, int]]]:
        resolved = self._resolve_import(doc, word)
        if resolved is not None:
            return (
                resolved.path.as_uri(),
                resolved.symbol.name,
                (resolved.path.as_uri(), resolved.symbol.line, resolved.symbol.col),
            )
        sym = self._lookup_symbol(uri, word)
        if sym is not None:
            return uri, sym.name, (uri, sym.line, sym.col)
        return uri, word, None

    def _reference_docs(self, target_uri: str) -> Dict[str, Document]:
        out: Dict[str, Document] = {}
        for uri, doc in self.docs.items():
            out[uri] = doc
        target_doc = out.get(target_uri)
        if target_doc is None:
            path = _uri_to_path(target_uri)
            if path is not None:
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    text = ""
                if text:
                    target_doc = Document(uri=target_uri, text=text)
                    analyze(target_doc)
                    out[target_uri] = target_doc
        return out

    def _reference_names_for_doc(
        self,
        doc: Document,
        uri: str,
        target_uri: str,
        target_name: str,
    ) -> set[str]:
        names: set[str] = set()
        if uri == target_uri:
            names.add(target_name)
        path = _uri_to_path(target_uri)
        for local, (module, exported) in doc.imports.items():
            if exported != target_name:
                continue
            current_path = _uri_to_path(doc.uri)
            if current_path is None:
                continue
            resolved_path = self.workspace.resolve_module(current_path, module)
            if resolved_path is None and _stdlib_module(module):
                resolved_path = (_STDLIB_DIR / f"{module}.lam").resolve()
            if path is not None and resolved_path == path:
                names.add(local)
        return names

    def _ensure_rename_allowed(self, doc: Document, uri: str, word: str, line: int) -> None:
        if word in doc.imports:
            module, _ = doc.imports[word]
            raise LspRequestError(
                -32602,
                f"rename of imported symbol `{word}` from `{module}` is not supported yet",
            )
        for other_uri, other_doc in self.docs.items():
            if other_uri == uri:
                continue
            for _local, (_module, exported) in other_doc.imports.items():
                if exported == word:
                    raise LspRequestError(
                        -32602,
                        f"rename of cross-file symbol `{word}` is not supported yet",
                    )
        if self._lookup_symbol(uri, word) is not None:
            return
        if doc.tree is not None and any(sym.name == word for sym in _scope_locals_at(doc.tree, line)):
            return
        raise LspRequestError(-32602, f"rename target `{word}` is not a known local symbol")

    def _receiver_at(self, uri: str, line: int, character: int) -> Optional[str]:
        """If the cursor sits on the right-hand side of ``foo.bar``,
        return ``"foo"``. Otherwise return ``None``.

        Operates purely on the source text — cheap, robust, doesn't
        need the parser to have succeeded.
        """
        doc = self.docs.get(uri)
        if not doc:
            return None
        try:
            row = doc.text.splitlines()[line]
        except IndexError:
            return None
        if character > len(row):
            character = len(row)
        # Walk back over the identifier under the cursor first.
        start = character
        while start > 0 and (row[start - 1].isalnum() or row[start - 1] == "_"):
            start -= 1
        if start == 0 or row[start - 1] != ".":
            return None
        # Now skip the dot and walk back over the receiver name.
        recv_end = start - 1
        recv_start = recv_end
        while recv_start > 0 and (row[recv_start - 1].isalnum() or row[recv_start - 1] == "_"):
            recv_start -= 1
        if recv_start == recv_end:
            return None
        return row[recv_start:recv_end]

    def _lookup_method(self, doc: Document, receiver: str,
                        method: str) -> Optional[Symbol]:
        """Find ``method`` on ``receiver`` (a class). Searches local
        symbols first, then methods on the imported class the alias
        refers to.
        """
        for sym in doc.symbols:
            if sym.parent == receiver and sym.name == method:
                return sym
        resolved = self._resolve_import(doc, receiver)
        if resolved is not None:
            if resolved.stdlib is not None:
                return resolved.stdlib.methods.get(resolved.symbol.name, {}).get(method)
        return None

    def _origin_module_for(self, doc: Document, receiver: str) -> Optional[str]:
        if receiver in doc.imports:
            return doc.imports[receiver][0]
        return None

    def _resolve_uri_for(self, doc: Document, receiver: str,
                          fallback_uri: str) -> str:
        """URI to return for a go-to-definition on ``receiver.X``.

        Local class → current document. Imported class → the lib file.
        """
        if receiver in doc.imports:
            resolved = self._resolve_import(doc, receiver)
            if resolved is not None:
                return resolved.path.as_uri()
        return fallback_uri

    def _resolve_import(self, doc: Document, name: str
                        ) -> Optional[ImportResolution]:
        """Look ``name`` up in this document's imports.

        Returns ``(module_index, symbol)`` so the caller can mint a
        ``Location`` that points into the lib file or rewrite the
        hover detail to mention which module the symbol came from.
        """
        binding = doc.imports.get(name)
        if not binding:
            return None
        module, exported = binding
        sm = _stdlib_module(module)
        if sm:
            sym = sm.public.get(exported)
            if sym:
                return ImportResolution(
                    module=module,
                    path=sm.path.resolve(),
                    symbol=sym,
                    stdlib=sm,
                )

        path = _uri_to_path(doc.uri)
        if path is None:
            return None
        export = self.workspace.resolve_import(path, module, exported)
        if export is None:
            return None
        return ImportResolution(
            module=module,
            path=export.path.resolve(),
            symbol=self._symbol_from_export(export),
        )

    @staticmethod
    def _symbol_from_export(export) -> Symbol:
        return Symbol(
            name=export.name,
            kind=export.kind if export.kind != "const" else "variable",
            line=max(0, export.line - 1),
            col=max(0, export.col - 1),
            end_line=max(0, export.line - 1),
            end_col=max(0, export.col - 1 + len(export.name)),
            detail=export.detail or export.name,
        )


# ────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────

def main() -> int:
    # Detach stdin/stdout from any text-mode buffering — LSP framing
    # depends on byte-accurate I/O.
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    server = LspServer(stdin, stdout)
    try:
        server.serve()
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
