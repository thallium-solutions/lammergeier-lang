#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
GRAMMAR = ROOT / "vs-code-extension" / "lammergeier-lang" / "syntaxes" / "lammergeier.tmLanguage.json"


def main() -> int:
    grammar = json.loads(GRAMMAR.read_text(encoding="utf-8"))
    repository = grammar["repository"]
    namespace = repository["lammergeier-namespace"]["patterns"][0]
    assert "LAMMERGEIER" in namespace["match"]
    assert "invalid.illegal" not in json.dumps(repository["lammergeier-namespace"])
    go_patterns = repository["go-block"]["patterns"][0]["patterns"]
    assert {"include": "#lammergeier-namespace"} in go_patterns
    keywords = " ".join(pattern["match"] for pattern in repository["keywords"]["patterns"])
    for keyword in ("do", "throw", "defer", "await", "nonlocal", "lambda"):
        assert keyword in keywords
    modifiers = repository["storage-modifiers"]["patterns"][0]["match"]
    assert "const" in modifiers and "await" not in modifiers
    print("PASS: VS Code grammar scopes LAMMERGEIER and contextual keywords")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
