"""Shared compiler diagnostic model.

The CLI, LSP, parser, and semantic checker should exchange diagnostics
through this module instead of inventing phase-local shapes. Positions in
this model are Lammergeier source positions and are always 1-indexed.
Protocol adapters, such as LSP publishing, are responsible for converting
to their own coordinate systems.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class DiagnosticSeverity(Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"
    HINT = "hint"


@dataclass(frozen=True)
class SourceSpan:
    file: Path | None
    line: int
    col: int
    end_line: int | None = None
    end_col: int | None = None


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: DiagnosticSeverity
    message: str
    span: SourceSpan
    hint: str | None = None


def severity_to_lsp(severity: DiagnosticSeverity) -> int:
    """Return LSP's numeric diagnostic severity."""
    if severity is DiagnosticSeverity.ERROR:
        return 1
    if severity is DiagnosticSeverity.WARNING:
        return 2
    if severity is DiagnosticSeverity.INFO:
        return 3
    return 4


def diagnostic_to_lsp(
    diagnostic: Diagnostic,
    *,
    source: str = "lammergeier",
    default_width: int = 1,
) -> dict[str, Any]:
    """Convert a Lam diagnostic to an LSP diagnostic dictionary.

    LSP positions are 0-indexed. When the diagnostic carries no explicit
    end position, use ``default_width`` on the start line so editors have
    a visible underline.
    """
    line = max(1, diagnostic.span.line)
    col = max(1, diagnostic.span.col)
    end_line = diagnostic.span.end_line or line
    end_col = diagnostic.span.end_col or (col + max(1, default_width))

    out: dict[str, Any] = {
        "range": {
            "start": {"line": line - 1, "character": col - 1},
            "end": {"line": max(1, end_line) - 1, "character": max(1, end_col) - 1},
        },
        "severity": severity_to_lsp(diagnostic.severity),
        "source": source,
        "message": diagnostic.message,
        "code": diagnostic.code,
    }
    if diagnostic.hint and diagnostic.span.file:
        out["relatedInformation"] = [{
            "location": {
                "uri": diagnostic.span.file.as_uri(),
                "range": out["range"],
            },
            "message": diagnostic.hint,
        }]
    return out


_SEMANTIC_CODES: dict[str, str] = {
    "undefined": "LAM1001",
    "duplicate": "LAM1002",
    "flow": "LAM1003",
    "shadow": "LAM1004",
    "reserved": "LAM1005",
    "const": "LAM1006",
    "call": "LAM1007",
    "member": "LAM1008",
    "interface": "LAM1009",
    "return": "LAM1010",
    "unreachable": "LAM1011",
    "unused": "LAM1012",
    "match": "LAM1013",
}


def semantic_code(kind: str) -> str:
    return _SEMANTIC_CODES.get(kind, "LAM1999")
