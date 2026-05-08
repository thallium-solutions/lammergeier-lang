#!/usr/bin/env python3
"""Tests for the shared compiler diagnostic model."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.diagnostics import (  # noqa: E402
    Diagnostic,
    DiagnosticSeverity,
    SourceSpan,
    diagnostic_to_lsp,
    semantic_code,
    severity_to_lsp,
)
from compiler.semantic import SemanticError  # noqa: E402


def test_severity_to_lsp() -> None:
    assert severity_to_lsp(DiagnosticSeverity.ERROR) == 1
    assert severity_to_lsp(DiagnosticSeverity.WARNING) == 2
    assert severity_to_lsp(DiagnosticSeverity.INFO) == 3
    assert severity_to_lsp(DiagnosticSeverity.HINT) == 4
    print("PASS: diagnostic severity maps to LSP severity")


def test_diagnostic_to_lsp_is_zero_indexed() -> None:
    diag = Diagnostic(
        code="LAM9999",
        severity=DiagnosticSeverity.ERROR,
        message="boom",
        span=SourceSpan(file=None, line=3, col=5),
    )
    out = diagnostic_to_lsp(diag, default_width=4)
    assert out["code"] == "LAM9999"
    assert out["severity"] == 1
    assert out["range"]["start"] == {"line": 2, "character": 4}
    assert out["range"]["end"] == {"line": 2, "character": 8}
    print("PASS: LSP conversion is zero-indexed and width-aware")


def test_semantic_error_to_diagnostic() -> None:
    err = SemanticError(7, 9, "undefined name `x`", "undefined")
    diag = err.to_diagnostic()
    assert diag.code == semantic_code("undefined")
    assert diag.severity is DiagnosticSeverity.ERROR
    assert diag.span.line == 7
    assert diag.span.col == 9
    print("PASS: semantic errors convert to shared diagnostics")


def test_semantic_warning_to_diagnostic() -> None:
    err = SemanticError(2, 1, "unused import `Strings`", "unused", severity="warning")
    diag = err.to_diagnostic()
    assert diag.code == semantic_code("unused")
    assert diag.severity is DiagnosticSeverity.WARNING
    print("PASS: semantic warnings convert to shared diagnostics")


def main() -> int:
    tests = [
        test_severity_to_lsp,
        test_diagnostic_to_lsp_is_zero_indexed,
        test_semantic_error_to_diagnostic,
        test_semantic_warning_to_diagnostic,
    ]
    for test in tests:
        test()
    print(f"\nDiagnostic results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())

