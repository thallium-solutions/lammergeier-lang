#!/usr/bin/env python3
"""Tests for source-map primitives."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.source_map import SourceMap, SourcePoint  # noqa: E402


def test_identity_map() -> None:
    smap = SourceMap.identity(3)
    assert smap.generated_to_original(1, 1) == SourcePoint(1, 1)
    assert smap.generated_to_original(3, 5) == SourcePoint(3, 5)
    print("PASS: identity source map preserves positions")


def test_deleted_lines_map() -> None:
    smap = SourceMap.delete_lines(4, {2})
    assert smap.generated_to_original(1, 3) == SourcePoint(1, 3)
    assert smap.generated_to_original(2, 3) == SourcePoint(3, 3)
    assert smap.generated_to_original(3, 3) == SourcePoint(4, 3)
    print("PASS: deleted-line source map skips removed lines")


def test_eof_maps_to_last_original_line() -> None:
    smap = SourceMap.from_line_mapping([2, 4])
    assert smap.generated_to_original(99, 7) == SourcePoint(4, 7)
    print("PASS: out-of-range generated positions map to original EOF")


def test_preprocessor_rewrites_keep_line_numbers() -> None:
    """Brace-less blocks, multi-line triple-quoted strings and dictionary
    destructuring are rewritten without changing the line of any later
    statement, so parser and semantic positions are the user's lines."""
    from compiler.lammergeier import preprocess_for_parse

    source = (
        "func main() {\n"
        "    x = 1\n"
        "    if x > 0 print(\"a\")\n"
        "    else print(\"b\")\n"
        "    for i in range(2) print(i)\n"
        "    while x < 0 x += 1\n"
        "    s = \"\"\"line1\n"
        "line2\n"
        "line3\"\"\" + \"!\"\n"
        "    user = {\"name\": \"a\", \"age\": 3}\n"
        "    {name, age} = user\n"
        "    MARKER = 1\n"
        "}\n"
    )
    out = preprocess_for_parse(source).source.split("\n")
    marker_line = next(i for i, line in enumerate(out, 1) if "MARKER" in line)
    assert marker_line == 12, (marker_line, out)
    assert len(out) == len(source.split("\n")), out
    print("PASS: preprocessor rewrites keep later line numbers")


def main() -> int:
    tests = [
        test_identity_map,
        test_deleted_lines_map,
        test_eof_maps_to_last_original_line,
        test_preprocessor_rewrites_keep_line_numbers,
    ]
    for test in tests:
        test()
    print(f"\nSource-map results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())

