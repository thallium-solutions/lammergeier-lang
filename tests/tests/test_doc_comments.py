#!/usr/bin/env python3
"""Documentation-comment delimiter and source-corpus regression tests."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from lark.exceptions import UnexpectedInput

ROOT = Path(__file__).resolve().parent.parent.parent
WORKSPACE = ROOT.parent
sys.path.insert(0, str(ROOT))

from compiler.formatter import format_lam_source  # noqa: E402
from compiler.lammergeier import create_parser, preprocess_for_parse  # noqa: E402


SKIP_DIRS = {
    ".git",
    ".lamcache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "vendor",
}
SPACED_TERMINATOR = re.compile(r"-[ \t]+#")


@dataclass(frozen=True)
class DocComment:
    start: int
    end: int
    body: str


@dataclass(frozen=True)
class ScanIssue:
    offset: int
    message: str


def _quoted_end(source: str, start: int, quote: str) -> int:
    delimiter = quote * 3 if source.startswith(quote * 3, start) else quote
    pos = start + len(delimiter)
    while pos < len(source):
        if source.startswith(delimiter, pos):
            escapes = 0
            scan = pos - 1
            while scan >= start and source[scan] == "\\":
                escapes += 1
                scan -= 1
            if delimiter == "`" or escapes % 2 == 0:
                return pos + len(delimiter)
        if len(delimiter) == 1 and delimiter != "`" and source[pos] == "\n":
            return pos
        pos += 1
    return len(source)


def _balanced_go_end(source: str, open_pos: int) -> int:
    opener = source[open_pos]
    closer = "}" if opener == "{" else ")"
    depth = 1
    pos = open_pos + 1
    while pos < len(source):
        if source.startswith("//", pos):
            newline = source.find("\n", pos + 2)
            pos = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", pos):
            close = source.find("*/", pos + 2)
            pos = len(source) if close < 0 else close + 2
            continue
        char = source[pos]
        if char in {'"', "'", "`"}:
            pos = _quoted_end(source, pos, char)
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return pos + 1
        pos += 1
    return len(source)


def _legacy_go_end(source: str, marker_pos: int) -> int:
    line_start = source.rfind("\n", 0, marker_pos) + 1
    base_indent = len(source[line_start:marker_pos])
    cursor = source.find("\n", marker_pos)
    if cursor < 0:
        return len(source)
    cursor += 1
    while cursor < len(source):
        line_end = source.find("\n", cursor)
        line_end = len(source) if line_end < 0 else line_end
        row = source[cursor:line_end]
        if row.strip():
            indent = len(row) - len(row.lstrip(" \t"))
            if indent <= base_indent:
                return cursor
        cursor = line_end + 1
    return len(source)


def _go_region_end(source: str, pos: int) -> int | None:
    if not source.startswith("go!", pos):
        return None
    if pos and (source[pos - 1].isalnum() or source[pos - 1] == "_"):
        return None
    cursor = pos + 3
    while cursor < len(source) and source[cursor] in " \t":
        cursor += 1
    if cursor < len(source) and source[cursor] in "{(":
        return _balanced_go_end(source, cursor)
    if cursor < len(source) and source[cursor] == ":":
        return _legacy_go_end(source, pos)
    return None


def scan_doc_comments(source: str) -> tuple[list[DocComment], list[ScanIssue]]:
    """Lexically scan Lam doc comments without inspecting strings or Go bodies."""
    comments: list[DocComment] = []
    issues: list[ScanIssue] = []
    pos = 0
    while pos < len(source):
        go_end = _go_region_end(source, pos)
        if go_end is not None:
            pos = go_end
            continue

        char = source[pos]
        if char in {'"', "'"}:
            pos = _quoted_end(source, pos, char)
            continue

        if source.startswith("#-", pos):
            body_start = pos + 2
            exact = source.find("-#", body_start)
            spaced = SPACED_TERMINATOR.search(source, body_start)
            if spaced is not None and (exact < 0 or spaced.start() < exact):
                issues.append(ScanIssue(spaced.start(), "spaced documentation-comment terminator"))
                pos = spaced.end()
                continue
            if exact < 0:
                issues.append(ScanIssue(pos, "unterminated documentation comment"))
                break
            nested = source.find("#-", body_start, exact)
            if nested >= 0:
                issues.append(ScanIssue(nested, "nested documentation-comment opener"))
            comments.append(DocComment(pos, exact + 2, source[body_start:exact]))
            pos = exact + 2
            continue

        if char == "#":
            newline = source.find("\n", pos + 1)
            pos = len(source) if newline < 0 else newline + 1
            continue

        spaced = SPACED_TERMINATOR.match(source, pos)
        if spaced is not None:
            issues.append(ScanIssue(pos, "spaced terminator without a documentation-comment opener"))
            pos = spaced.end()
            continue
        if source.startswith("-#", pos):
            issues.append(ScanIssue(pos, "documentation-comment terminator without an opener"))
            pos += 2
            continue
        pos += 1
    return comments, issues


def _workspace_lam_files() -> list[Path]:
    repositories = [ROOT]
    if WORKSPACE.is_dir():
        repositories.extend(
            child
            for child in sorted(WORKSPACE.iterdir())
            if child.is_dir() and child != ROOT and (child / ".git").exists()
        )

    files: set[Path] = set()
    for repository in repositories:
        for path in repository.rglob("*.lam"):
            if not any(part in SKIP_DIRS for part in path.parts):
                files.add(path.resolve())
    return sorted(files)


def _location(source: str, offset: int) -> tuple[int, int]:
    line = source.count("\n", 0, offset) + 1
    line_start = source.rfind("\n", 0, offset) + 1
    return line, offset - line_start + 1


def _parse(source: str) -> None:
    prepared = preprocess_for_parse(source).source
    if not prepared.endswith("\n"):
        prepared += "\n"
    create_parser().parse(prepared)


def test_scanner_ignores_non_doc_contexts() -> None:
    source = '''# An ordinary comment may mention #- and -  # without opening a doc block.
go! {
    raw := `#- not Lam documentation
-  #`
    _ = raw
}

func main() {
    marker: str = "#- still a string -  #"
    print(marker)
}

#- Real docs keep `code()`, https://example.com/a#fragment, parentheses (ok),
and a literal # hash character. -#
func documented() {
    pass
}
'''
    comments, issues = scan_doc_comments(source)
    assert not issues, issues
    assert len(comments) == 1, comments
    assert "https://example.com/a#fragment" in comments[0].body
    _parse(source)
    print("PASS: lexical scan ignores ordinary comments, strings, and go! bodies")


def test_strict_parser_rejects_bad_terminators() -> None:
    malformed_sources = {
        "spaced": "#- malformed -  #\nfunc main() { pass }\n",
        "spaced multiline": "#- malformed\n-  #\nfunc main() { pass }\n",
        "unterminated": "#- unterminated\nfunc main() { pass }\n",
    }
    with tempfile.TemporaryDirectory() as tmp:
        for index, (label, source) in enumerate(malformed_sources.items()):
            try:
                _parse(source)
            except UnexpectedInput:
                pass
            else:
                raise AssertionError(f"{label} documentation comment unexpectedly parsed")

            path = Path(tmp) / f"malformed_{index}.lam"
            path.write_text(source, encoding="utf-8")
            proc = subprocess.run(
                [sys.executable, str(ROOT / "compiler" / "lammergeier.py"),
                 str(path), "--emit-go"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            diagnostic = (proc.stderr or "") + (proc.stdout or "")
            assert proc.returncode != 0, f"{label} compiled successfully"
            assert "syntax check failed" in diagnostic, diagnostic
    print("PASS: normal compilation rejects bad documentation terminators as syntax errors")


def test_formatter_repairs_only_spaced_doc_terminators() -> None:
    source = '''# Ordinary -  # text stays ordinary.
func main() {
    value: str = "string -  # text"
    print(value)
}

#- Repair this comment while preserving `code()`, https://example.com, and #. -  #
func documented() {
    pass
}
'''
    result = format_lam_source(source)
    assert "#- Repair this comment while preserving `code()`, https://example.com, and #. -#" in result.text
    assert "# Ordinary -  # text stays ordinary." in result.text
    assert '"string -  # text"' in result.text
    _parse(result.text)
    print("PASS: formatter canonicalizes only documentation-comment terminators")


def test_complete_workspace_corpus() -> None:
    parser = create_parser()
    files = _workspace_lam_files()
    assert files, "no Lam source files discovered"

    failures: list[str] = []
    parsed_bodies = 0
    parsed_files = 0
    for path in files:
        source = path.read_text(encoding="utf-8")
        comments, issues = scan_doc_comments(source)
        for issue in issues:
            line, column = _location(source, issue.offset)
            failures.append(f"{path}:{line}:{column}: {issue.message}")

        for comment in comments:
            if re.search(r"[`():/#?&.,\[\]{}*]", comment.body):
                probe = f"#-{comment.body}-#\nfunc docProbe() {{ pass }}\n"
                try:
                    prepared = preprocess_for_parse(probe).source
                    parser.parse(prepared if prepared.endswith("\n") else prepared + "\n")
                    parsed_bodies += 1
                except Exception as exc:  # pragma: no cover - failure detail
                    line, column = _location(source, comment.start)
                    failures.append(
                        f"{path}:{line}:{column}: comment body failed to parse: {exc}"
                    )

        syntax_cases = ROOT / "tests" / "syntax" / "cases"
        if path.is_relative_to(syntax_cases):
            continue
        try:
            prepared = preprocess_for_parse(source).source
            parser.parse(prepared if prepared.endswith("\n") else prepared + "\n")
            parsed_files += 1
        except Exception as exc:  # pragma: no cover - failure detail
            failures.append(f"{path}: complete source failed to parse: {exc}")

    if failures:
        preview = "\n".join(f"  - {failure}" for failure in failures[:50])
        remainder = "" if len(failures) <= 50 else f"\n  ... {len(failures) - 50} more"
        raise AssertionError(f"documentation/source corpus failures:\n{preview}{remainder}")
    assert parsed_bodies > 0, "no punctuation-rich documentation bodies were parsed"
    assert parsed_files > 0, "no complete Lam files were parsed"
    print(
        f"PASS: validated {len(files)} Lam files, parsed {parsed_files} complete sources "
        f"and {parsed_bodies} punctuation-rich documentation bodies"
    )


def main() -> int:
    tests = [
        test_scanner_ignores_non_doc_contexts,
        test_strict_parser_rejects_bad_terminators,
        test_formatter_repairs_only_spaced_doc_terminators,
        test_complete_workspace_corpus,
    ]
    for test in tests:
        test()
    print(f"\ndoc-comment results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
