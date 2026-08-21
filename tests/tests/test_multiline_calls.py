#!/usr/bin/env python3

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.lammergeier import auto_semicolons, create_parser, preprocess_for_parse  # noqa: E402


SOURCE = """func add(a: int, b: int) -> int {
    return a + b
}

func main() {
    value: int = add(
        1,
        2,
    )
    print(value)
}
"""


def test_auto_semicolons_preserves_multiline_call_arguments() -> None:
    preprocessed = auto_semicolons(SOURCE)
    assert "value: int = add(\n" in preprocessed
    assert "        1,\n" in preprocessed
    assert "        2,\n" in preprocessed
    assert "    );" in preprocessed
    print("PASS: auto-semicolons preserves multi-line call arguments")


def test_parser_accepts_multiline_call_with_trailing_comma() -> None:
    parsed = create_parser().parse(preprocess_for_parse(SOURCE).source)
    calls: list[str] = []

    def walk(node) -> None:
        if getattr(node, "data", None) == "funccall":
            calls.append("funccall")
        for child in getattr(node, "children", []):
            walk(child)

    walk(parsed)
    assert calls, parsed.pretty()
    print("PASS: parser accepts multi-line calls with trailing comma")


def test_parser_accepts_statement_before_closing_block_brace() -> None:
    source = """func main(){
    print("ok")}
"""
    parsed = create_parser().parse(preprocess_for_parse(source).source + "\n")
    assert "funccall" in parsed.pretty()

    literal_source = """func main(){ values: dict[str, int] = {"ok": 1} }
"""
    parsed_literal = create_parser().parse(preprocess_for_parse(literal_source).source + "\n")
    assert "dict" in parsed_literal.pretty()
    print("PASS: parser accepts compact statements before closing block braces")


def test_auto_semicolons_preserves_nested_literal_continuation_closes() -> None:
    source = """func main() {
    payload: list[dict[str, dict[str, list[dict[str, list[int]]]]]] = [
        {"primary": {"left": [{"values": [2, 13], "weights": [23, 30]}]}},
        {"backup": {"right": [{"values": [49, 51], "weights": [1, 2]}]}},
    ]
    print(payload[0]["primary"]["left"][0]["values"][0])
}
"""
    preprocessed = preprocess_for_parse(source).source
    assert "}]};}," not in preprocessed
    assert "}]}}," in preprocessed
    parsed = create_parser().parse(preprocessed + "\n")
    assert "dict" in parsed.pretty()
    print("PASS: auto-semicolons preserves nested literal continuation closes")


def main() -> int:
    tests = [
        test_auto_semicolons_preserves_multiline_call_arguments,
        test_parser_accepts_multiline_call_with_trailing_comma,
        test_parser_accepts_statement_before_closing_block_brace,
        test_auto_semicolons_preserves_nested_literal_continuation_closes,
    ]
    for test in tests:
        test()
    print(f"\nmultiline call parser results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
