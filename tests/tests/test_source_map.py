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


def _semantic_positions(source: str) -> dict[str, tuple[int, int]]:
    from compiler.lammergeier import create_parser, preprocess_for_parse
    from compiler.semantic import check_source

    prepared = preprocess_for_parse(source)
    tree = create_parser().parse(prepared.source + "\n")
    errors = check_source(tree, go_blocks=prepared.go_blocks)
    return {e.message.split(";")[0]: (e.line, e.col) for e in errors if not e.is_warning}


def test_markup_slots_keep_line_and_column() -> None:
    """A markup literal is rewritten into a marker call whose slot
    arguments sit at the line and column of their ``${``: errors in a slot,
    in a slot of a literal nested in a slot, and in the code after a
    multi-line literal point at the source (they used to point at the
    literal's first line)."""
    from compiler.lammergeier import preprocess_for_parse

    source = (
        "func wrap(body: html, cls: str = \"\") -> html {\n"           # 1
        "    return <><div class=\"${cls}\">${body}</div></>\n"        # 2
        "}\n"                                                          # 3
        "func row(label: html, n: int) -> html {\n"                    # 4
        "    return wrap(<>\n"                                         # 5
        "        <td>${n + \"a\"}</td>\n"                              # 6
        "        <td>${wrap(<>\n"                                      # 7
        "            <b title=\"${label}\">${missingName}</b>\n"       # 8
        "        </>, cls = \"x\")}</td>\n"                            # 9
        "    </>, cls = otherMissing)\n"                               # 10
        "}\n"                                                          # 11
        "func main() {\n"                                              # 12
        "    print(str(row(<>x</>, 1)))\n"                             # 13
        "}\n"
    )
    out = preprocess_for_parse(source).source.split("\n")
    assert len(out) == len(source.split("\n")), out
    positions = _semantic_positions(source)
    assert positions["cannot add `int` and `str`"] == (6, 15), positions
    assert positions["an attribute value cannot be `html`"] == (8, 25), positions
    assert positions["undefined name `missingName`"] == (8, 35), positions
    assert positions["undefined name `otherMissing`"] == (10, 16), positions
    print("PASS: markup slots keep their line and column")


def test_multiline_fstring_slots_keep_line_and_column() -> None:
    """A multi-line f-string is collapsed onto one line before parsing; its
    slots are still reported at their own line and column."""
    source = (
        "func main() {\n"                       # 1
        "    n: int = 1\n"                      # 2
        "    a: str = f\"\"\"first {n}\n"          # 3
        "second {n + \"x\"}\n"                  # 4
        "  third {missingName}\"\"\"\n"          # 5
        "    print(a)\n"                        # 6
        "}\n"
    )
    positions = _semantic_positions(source)
    assert positions["cannot add `int` and `str`"] == (4, 9), positions
    assert positions["undefined name `missingName` in f-string"][0] == 5, positions
    assert positions["undefined name `missingName` in f-string"][1] == 10, positions
    print("PASS: multi-line f-string slots keep their line and column")


def main() -> int:
    tests = [
        test_identity_map,
        test_deleted_lines_map,
        test_eof_maps_to_last_original_line,
        test_preprocessor_rewrites_keep_line_numbers,
        test_markup_slots_keep_line_and_column,
        test_multiline_fstring_slots_keep_line_and_column,
    ]
    for test in tests:
        test()
    print(f"\nSource-map results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())

