"""Link-time check for top-level Go symbol clashes between Lam modules.

Every Lam module of a program — the main file, the stdlib modules, installed
extlibs and project-local libraries — is emitted into the same Go package
(``package main``). Two modules that both declare, say, ``func tag()`` or
``class Schema`` therefore produce two identical top-level Go symbols, and
``go build`` fails with ``X redeclared in this block``. That Go error points
at generated line mappings, cascades into unrelated follow-up errors, and
for raw ``go!`` declarations names the wrong source line.

This module scans the *emitted* Go of every module (so overload suffixes,
``__lamN`` renames and raw ``go!`` declarations are all taken into account
exactly as Go will see them), groups top-level names declared by more than
one module, and renders a Lam-level diagnostic that names every module and
source location involved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_DECL_KEYWORDS = frozenset({"func", "type", "var", "const"})
_LINE_DIRECTIVE = re.compile(r"^//line (.+):(\d+)\s*$")
_IDENT_START = re.compile(r"[A-Za-z_]")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class GoTopLevelSymbol:
    """One package-level Go declaration attributed to a Lam module."""

    name: str
    keyword: str
    origin: str
    file: str
    line: int


def mask_go_literals(go_source: str) -> str:
    """Blank out comments, strings and runes, keeping offsets and newlines.

    ``//line`` directives are comments too; they are read separately from
    the original text, so masking them is harmless.
    """
    out = list(go_source)
    i = 0
    n = len(go_source)
    while i < n:
        ch = go_source[i]
        nxt = go_source[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            j = go_source.find("\n", i)
            j = n if j < 0 else j
        elif ch == "/" and nxt == "*":
            j = go_source.find("*/", i + 2)
            j = n if j < 0 else j + 2
        elif ch == "`":
            j = go_source.find("`", i + 1)
            j = n if j < 0 else j + 1
        elif ch in "\"'":
            j = i + 1
            while j < n and go_source[j] != ch and go_source[j] != "\n":
                j += 2 if go_source[j] == "\\" else 1
            j = min(j + 1, n)
        else:
            i += 1
            continue
        for k in range(i, j):
            if out[k] != "\n":
                out[k] = " "
        i = j
    return "".join(out)


def _line_locations(go_source: str, default_file: str) -> list[tuple[str, int]]:
    """Map each 0-based Go line to the Lam ``(file, line)`` it came from."""
    locations: list[tuple[str, int]] = []
    current_file = default_file
    current_line = 0
    for raw in go_source.split("\n"):
        directive = _LINE_DIRECTIVE.match(raw.strip())
        if directive:
            current_file = directive.group(1)
            current_line = int(directive.group(2))
            locations.append((current_file, 0))
            continue
        locations.append((current_file, current_line))
        if current_line:
            current_line += 1
    return locations


def scan_top_level_symbols(go_source: str, origin: str, default_file: str) -> list[GoTopLevelSymbol]:
    """Return the package-level declarations of one generated Go file.

    Methods, ``func init()`` (Go allows several) and blank identifiers are
    skipped because they cannot clash.
    """
    code = mask_go_literals(go_source)
    locations = _line_locations(go_source, default_file)
    symbols: list[GoTopLevelSymbol] = []
    depth = 0
    group_keyword = ""
    group_depth = 0
    at_line_start = True
    line_no = 0
    i = 0
    n = len(code)

    def emit(name: str, keyword: str, line_index: int) -> None:
        if name == "_" or (keyword == "func" and name == "init"):
            return
        file, line = locations[line_index] if line_index < len(locations) else (default_file, 0)
        symbols.append(GoTopLevelSymbol(name, keyword, origin, file, line))

    def skip_space(pos: int) -> int:
        while pos < n and code[pos] in " \t\r":
            pos += 1
        return pos

    def read_name_list(pos: int) -> tuple[list[str], int]:
        names: list[str] = []
        while True:
            pos = skip_space(pos)
            match = _IDENT.match(code, pos)
            if not match:
                return names, pos
            names.append(match.group(0))
            pos = skip_space(match.end())
            if pos < n and code[pos] == ",":
                pos += 1
                continue
            return names, pos

    while i < n:
        ch = code[i]
        if ch == "\n":
            line_no += 1
            at_line_start = True
            i += 1
            continue
        if ch in " \t\r":
            i += 1
            continue
        if group_keyword and depth == group_depth and at_line_start and _IDENT_START.match(ch):
            names, end = read_name_list(i)
            for name in names:
                emit(name, group_keyword, line_no)
            at_line_start = False
            i = max(end, i + 1)
            continue
        at_line_start = False
        if ch in "([{":
            depth += 1
            i += 1
            continue
        if ch in ")]}":
            depth -= 1
            if group_keyword and depth < group_depth:
                group_keyword = ""
            i += 1
            continue
        if depth == 0 and _IDENT_START.match(ch):
            match = _IDENT.match(code, i)
            word = match.group(0)
            i = match.end()
            if word not in _DECL_KEYWORDS:
                continue
            pos = skip_space(i)
            if pos < n and code[pos] == "(":
                if word == "func":
                    i = pos
                    continue
                group_keyword = word
                group_depth = 1
                depth = 1
                i = pos + 1
                continue
            if word == "func":
                match = _IDENT.match(code, pos)
                if match:
                    emit(match.group(0), word, line_no)
                    i = match.end()
                continue
            names, end = read_name_list(pos)
            if word == "type":
                names = names[:1]
            for name in names:
                emit(name, word, line_no)
            i = max(end, i)
            continue
        i += 1
    return symbols


def find_symbol_clashes(units: Iterable[tuple[str, str, str]]) -> list[list[GoTopLevelSymbol]]:
    """Group top-level names declared by more than one module.

    ``units`` yields ``(origin, go_source, default_file)`` triples. Several
    units may share an origin; duplicates inside one origin are left to the
    Go compiler because they are not cross-module clashes.
    """
    by_name: dict[str, list[GoTopLevelSymbol]] = {}
    order: list[str] = []
    for origin, go_source, default_file in units:
        for symbol in scan_top_level_symbols(go_source, origin, default_file):
            if symbol.name not in by_name:
                by_name[symbol.name] = []
                order.append(symbol.name)
            by_name[symbol.name].append(symbol)
    clashes: list[list[GoTopLevelSymbol]] = []
    for name in order:
        group: list[GoTopLevelSymbol] = []
        origins: list[str] = []
        for symbol in by_name[name]:
            if symbol.origin not in origins:
                origins.append(symbol.origin)
                group.append(symbol)
        if len(group) > 1:
            clashes.append(group)
    return clashes


def _read_lines(path: str) -> list[str]:
    try:
        return Path(path).read_text(encoding="utf-8").split("\n")
    except OSError:
        return []


def _locate(symbol: GoTopLevelSymbol, lines: list[str]) -> int:
    """Refine a location whose ``//line`` directive points at a ``go!`` block start."""
    if not lines:
        return symbol.line
    word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(symbol.name)}(?![A-Za-z0-9_])")
    if 1 <= symbol.line <= len(lines) and word.search(lines[symbol.line - 1]):
        return symbol.line
    start = max(symbol.line - 1, 0)
    for index in range(start, len(lines)):
        text = lines[index]
        if word.search(text) and re.match(rf"\s*(?:(?:func|type|var|const)\s+)?{re.escape(symbol.name)}\b", text):
            return index + 1
    return symbol.line


def _join_names(names: list[str]) -> str:
    quoted = [f"`{name}`" for name in names]
    if len(quoted) <= 2:
        return " and ".join(quoted)
    return ", ".join(quoted[:-1]) + " and " + quoted[-1]


def render_symbol_clashes(clashes: list[list[GoTopLevelSymbol]], program_path: str) -> str:
    """Render clashes with backticked symbols and three-line source snippets."""
    out = [f"error: top-level symbol clash while linking {program_path}"]
    for group in clashes:
        first = group[0]
        modules = _join_names([symbol.origin for symbol in group])
        out.append(f"  Go `{first.keyword} {first.name}` is declared by modules {modules}")
        for symbol in group:
            lines = _read_lines(symbol.file)
            line = _locate(symbol, lines)
            where = f"{symbol.file}:{line}" if line else symbol.file
            out.append(f"    - `{symbol.origin}` at {where}")
            if line and lines:
                for index in range(max(1, line - 1), min(len(lines), line + 1) + 1):
                    marker = ">>>" if index == line else "   "
                    out.append(f"        {marker} {index:4d} | {lines[index - 1]}")
    out.append(
        "  help: rename one of the declarations. All Lam modules of a program link into one Go "
        "package; same-name functions, constants, variables, classes and interfaces are "
        "namespaced per module automatically, except definitions in the main module and the "
        "compiler-referenced classes (`lamerrors.Result`/`Error`, `lamstrings.Strings`); raw "
        "`go!` declarations and a class constructor next to a same-named function must also be "
        "unique across modules."
    )
    return "\n".join(out)
