"""Workspace module facts and import resolution.

This module is intentionally side-effect free: it records facts about
Lammergeier files and resolves module paths so the compiler and LSP can
share one import graph.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from compiler.ast_nodes import ClassDecl, FuncDecl, ImportDecl, InterfaceDecl, Module, VarDecl


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ImportEdge:
    module: str
    imported: str | None
    alias: str | None
    line: int
    col: int


@dataclass
class ModuleFacts:
    path: Path
    module_name: str
    exports: dict[str, "ExportSymbol"] = field(default_factory=dict)
    imports: list[ImportEdge] = field(default_factory=list)


@dataclass(frozen=True)
class ExportSymbol:
    name: str
    kind: str
    line: int
    col: int
    path: Path
    detail: str = ""


_FUNC_RE = re.compile(r"^\s*(?:private\s+|static\s+|async\s+)*func\s+([A-Za-z_]\w*)\b")
_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)\b")
_INTERFACE_RE = re.compile(r"^\s*interface\s+([A-Za-z_]\w*)\b")
_CONST_RE = re.compile(r"^\s*const\s+([A-Za-z_]\w*)\b")
_ASSIGN_RE = re.compile(r"^\s*(?:private\s+|static\s+)?([A-Za-z_]\w*)\s*(?::|=)")
_FROM_RE = re.compile(r"^\s*from\s+(@?[A-Za-z0-9_./-]+)\s+import\s+(.+?)(?:#.*)?$")
_IMPORT_RE = re.compile(r"^\s*import\s+(@?[A-Za-z0-9_./-]+)(?:\s+as\s+([A-Za-z_]\w*))?(?:#.*)?$")
_NON_EXPORT_ASSIGN_NAMES = {
    "and", "as", "break", "class", "const", "continue", "else", "false",
    "for", "from", "func", "if", "import", "in", "interface", "not", "or",
    "pass", "return", "static", "true", "while",
}


def module_name_for_path(path: Path) -> str:
    if path.name == "__init__.lam":
        return path.parent.name
    return path.stem


def module_facts_from_source(path: Path, source: str) -> ModuleFacts:
    path = path.resolve()
    facts = ModuleFacts(path=path, module_name=module_name_for_path(path))
    depth = 0

    for lineno, raw in enumerate(source.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            depth += raw.count("{") - raw.count("}")
            continue

        _collect_import_edges(facts, raw, lineno)

        if depth == 0:
            _collect_reexports(facts, raw, lineno)
            for regex, kind in (
                (_FUNC_RE, "function"),
                (_CLASS_RE, "class"),
                (_INTERFACE_RE, "interface"),
                (_CONST_RE, "const"),
                (_ASSIGN_RE, "variable"),
            ):
                match = regex.match(raw)
                if match:
                    name = match.group(1)
                    if name in _NON_EXPORT_ASSIGN_NAMES:
                        break
                    _record_export(facts, name, kind, lineno, match.start(1) + 1, raw)
                    break

        depth += raw.count("{") - raw.count("}")

    return facts


def module_facts_from_path(path: Path) -> ModuleFacts:
    return module_facts_from_source(path, path.read_text(encoding="utf-8"))


def module_facts_from_ast(path: Path, module: Module) -> ModuleFacts:
    path = path.resolve()
    facts = ModuleFacts(path=path, module_name=module_name_for_path(path))
    for decl in module.body:
        if isinstance(decl, FuncDecl):
            _record_export_from_span(facts, decl.name, "function", decl.span, f"func {decl.name}")
        elif isinstance(decl, ClassDecl):
            if decl.span is not None:
                _record_export_from_span(facts, decl.name, "class", decl.span, f"class {decl.name}")
        elif isinstance(decl, InterfaceDecl):
            if decl.span is not None:
                _record_export_from_span(facts, decl.name, "interface", decl.span, f"interface {decl.name}")
        elif isinstance(decl, VarDecl):
            kind = "const" if decl.is_const else "variable"
            _record_export_from_span(facts, decl.name, kind, decl.span, decl.name)
        elif isinstance(decl, ImportDecl):
            for binding in decl.bindings:
                if binding.name and binding.name != "*":
                    _record_export_from_span(facts, binding.name, "import", binding.span, binding.name)
    return facts


def _record_export_from_span(
    facts: ModuleFacts,
    name: str,
    kind: str,
    span,
    detail: str,
) -> None:
    if not name:
        return
    facts.exports[name] = ExportSymbol(
        name=name,
        kind=kind,
        line=span.line,
        col=span.col,
        path=facts.path,
        detail=detail,
    )


def _record_export(
    facts: ModuleFacts,
    name: str,
    kind: str,
    lineno: int,
    col: int,
    raw: str,
) -> None:
    facts.exports[name] = ExportSymbol(
        name=name,
        kind=kind,
        line=lineno,
        col=col,
        path=facts.path,
        detail=raw.strip(),
    )


def _collect_import_edges(facts: ModuleFacts, raw: str, lineno: int) -> None:
    match = _IMPORT_RE.match(raw)
    if match:
        module = match.group(1)
        alias = match.group(2)
        facts.imports.append(ImportEdge(
            module=module,
            imported=None,
            alias=alias,
            line=lineno,
            col=match.start(1) + 1,
        ))
        return

    match = _FROM_RE.match(raw)
    if not match:
        return
    module = match.group(1)
    names = match.group(2).strip()
    if names.startswith("(") and names.endswith(")"):
        names = names[1:-1]
    for piece in names.split(","):
        piece = piece.strip()
        if not piece:
            continue
        parts = piece.split()
        imported = parts[0]
        alias = parts[2] if len(parts) >= 3 and parts[1] == "as" else None
        facts.imports.append(ImportEdge(
            module=module,
            imported=imported,
            alias=alias,
            line=lineno,
            col=raw.find(imported) + 1,
        ))


def _collect_reexports(facts: ModuleFacts, raw: str, lineno: int) -> None:
    match = _IMPORT_RE.match(raw)
    if match:
        module = match.group(1)
        alias = match.group(2)
        local = alias or module.rsplit(".", 1)[-1].rsplit("/", 1)[-1]
        if local:
            _record_export(facts, local, "import", lineno, match.start(1) + 1, raw)
        return

    match = _FROM_RE.match(raw)
    if not match:
        return
    names = match.group(2).strip()
    if names.startswith("(") and names.endswith(")"):
        names = names[1:-1]
    for piece in names.split(","):
        piece = piece.strip()
        if not piece:
            continue
        parts = piece.split()
        imported = parts[0]
        if imported == "*":
            continue
        alias = parts[2] if len(parts) >= 3 and parts[1] == "as" else None
        local = alias or imported
        _record_export(facts, local, "import", lineno, raw.find(imported) + 1, raw)


class WorkspaceIndex:
    def __init__(
        self,
        root: Path,
        *,
        stdlib_dir: Path | None = None,
        extlibs_dirs: list[Path] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.stdlib_dir = (stdlib_dir or PROJECT_ROOT / "lib").resolve()
        self.extlibs_dirs = [p.resolve() for p in (extlibs_dirs or []) if p.exists()]
        self.facts_by_path: dict[Path, ModuleFacts] = {}

    @classmethod
    def for_source(
        cls,
        source_path: Path,
        *,
        extlibs_dirs: list[Path] | None = None,
    ) -> "WorkspaceIndex":
        root = source_path.resolve().parent
        dirs = list(extlibs_dirs or [])
        env_extlibs = os.environ.get("LAMC_EXTLIBS")
        if env_extlibs:
            dirs.extend(Path(seg) for seg in env_extlibs.split(os.pathsep) if seg)
        dirs.append(root / "extlibs")
        dirs.append(Path.home() / ".lammergeier" / "extlibs")
        return cls(root, extlibs_dirs=dirs)

    def update_file(self, path: Path, text: str | None = None) -> ModuleFacts:
        path = path.resolve()
        source = text if text is not None else path.read_text(encoding="utf-8")
        facts = module_facts_from_source(path, source)
        self.facts_by_path[path] = facts
        return facts

    def remove_file(self, path: Path) -> None:
        self.facts_by_path.pop(path.resolve(), None)

    def resolve_module(self, from_path: Path, module: str) -> Path | None:
        from_dir = from_path.resolve().parent
        roots = [self.stdlib_dir, *self.extlibs_dirs, from_dir, self.root, self.root / "lib"]
        seen: set[Path] = set()
        for root in roots:
            root = root.resolve()
            if root in seen or not root.exists():
                continue
            seen.add(root)
            found = self._module_path_under(root, module)
            if found:
                return found
        for path, facts in self.facts_by_path.items():
            if facts.module_name == module and path.parent in {from_dir, self.root, self.root / "lib"}:
                return path
        return None

    def resolve_import(self, from_path: Path, module: str, name: str) -> ExportSymbol | None:
        path = self.resolve_module(from_path, module)
        if path is None:
            return None
        facts = self.facts_by_path.get(path)
        if facts is None:
            facts = self.update_file(path)
        return facts.exports.get(name)

    @staticmethod
    def _module_path_under(root: Path, module: str) -> Path | None:
        rel = Path(module.replace(".", "/"))
        flat = root / f"{module}.lam"
        if flat.exists():
            return flat.resolve()
        nested_flat = root / rel.with_suffix(".lam")
        if nested_flat.exists():
            return nested_flat.resolve()
        init_file = root / rel / "__init__.lam"
        if init_file.exists():
            return init_file.resolve()
        return None
