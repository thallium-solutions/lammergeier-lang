#!/usr/bin/env python3
"""Unit tests for the link-time top-level Go symbol clash check."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.link_symbols import (  # noqa: E402
    GoTopLevelSymbol,
    find_symbol_clashes,
    render_symbol_clashes,
    scan_top_level_symbols,
)


def _names(go_source: str) -> list[tuple[str, str]]:
    return [(sym.keyword, sym.name) for sym in scan_top_level_symbols(go_source, "m", "/m.lam")]


def test_simple_declarations() -> None:
    src = "package main\n\nfunc Tag() string {\n\treturn \"x\"\n}\n\ntype Box struct {\n\tV int\n}\n\nvar Version string = \"1\"\nconst limit = 3\n"
    assert _names(src) == [("func", "Tag"), ("type", "Box"), ("var", "Version"), ("const", "limit")]
    print("PASS: simple top-level declarations")


def test_methods_are_not_top_level() -> None:
    src = "func (s *Box) Tag() string { return \"\" }\nfunc (Box) Size() int { return 0 }\nfunc NewBox() *Box { return &Box{} }\n"
    assert _names(src) == [("func", "NewBox")]
    print("PASS: methods are skipped")


def test_generic_and_multiline_signatures() -> None:
    src = "func Map[T any, U any](\n\txs []T,\n\tf func(T) U,\n) []U {\n\treturn nil\n}\ntype Pair[K comparable, V any] struct {\n\tKey K\n}\n"
    assert _names(src) == [("func", "Map"), ("type", "Pair")]
    print("PASS: generic and multi-line signatures")


def test_grouped_declarations() -> None:
    src = (
        "var (\n\tshared = 1\n\tother, third = 2, 3\n\tm = map[string]int{\n\t\t\"a\": 1,\n\t}\n)\n"
        "const (\n\tA = iota\n\tB\n)\n"
        "type (\n\tID string\n\tPoint struct {\n\t\tX int\n\t}\n)\n"
    )
    assert _names(src) == [
        ("var", "shared"), ("var", "other"), ("var", "third"), ("var", "m"),
        ("const", "A"), ("const", "B"),
        ("type", "ID"), ("type", "Point"),
    ]
    print("PASS: grouped var/const/type declarations")


def test_strings_and_comments_are_ignored() -> None:
    src = (
        "// func Commented() {}\n"
        "/* type Hidden struct{} */\n"
        "var text = \"func Fake() {}\"\n"
        "var raw = `\nfunc AlsoFake() {}\n`\n"
        "var r = '{'\n"
        "func Real() {}\n"
    )
    assert _names(src) == [("var", "text"), ("var", "raw"), ("var", "r"), ("func", "Real")]
    print("PASS: strings, runes and comments are ignored")


def test_line_directives_attribute_lam_lines() -> None:
    src = "package main\n//line /src/lib.lam:18\nfunc Tag() string {\n//line /src/lib.lam:19\n\treturn \"a\"\n}\n\nvar After = 1\n"
    symbols = scan_top_level_symbols(src, "lib", "/fallback.lam")
    assert [(s.name, s.file, s.line) for s in symbols] == [
        ("Tag", "/src/lib.lam", 18),
        ("After", "/src/lib.lam", 22),
    ]
    print("PASS: //line directives map declarations to Lam lines")


def test_init_and_blank_are_ignored() -> None:
    src = "func init() {}\nfunc init() {}\nvar _ = 1\nvar _, kept = 1, 2\n"
    assert _names(src) == [("var", "kept")]
    print("PASS: init functions and blank identifiers are ignored")


def test_clashes_only_across_origins() -> None:
    units = [
        ("alpha", "func Tag() {}\nfunc Only() {}\n", "/alpha.lam"),
        ("beta", "func Tag() {}\nvar shared = 1\n", "/beta.lam"),
        ("gamma", "var shared = 2\n", "/gamma.lam"),
        ("alpha", "func Same() {}\n", "/alpha2.lam"),
        ("alpha", "func Same() {}\n", "/alpha3.lam"),
    ]
    clashes = find_symbol_clashes(units)
    assert [(group[0].name, [s.origin for s in group]) for group in clashes] == [
        ("Tag", ["alpha", "beta"]),
        ("shared", ["beta", "gamma"]),
    ]
    print("PASS: clashes are reported across modules only")


def test_three_way_clash_is_one_group() -> None:
    units = [(name, "type Schema struct{}\n", f"/{name}.lam") for name in ("lamschema", "lammigrate", "app")]
    clashes = find_symbol_clashes(units)
    assert len(clashes) == 1
    assert [s.origin for s in clashes[0]] == ["lamschema", "lammigrate", "app"]
    print("PASS: a three-way clash is reported as one group")


def test_render_has_snippets_and_help() -> None:
    with tempfile.TemporaryDirectory() as td:
        alpha = Path(td) / "alpha.lam"
        beta = Path(td) / "beta.lam"
        alpha.write_text("# alpha\nfunc tag() -> str {\n    return \"a\"\n}\n", encoding="utf-8")
        beta.write_text("go! {\n    func lamxNoise() {}\n\n    func Tag() string {\n        return \"b\"\n    }\n}\n", encoding="utf-8")
        clashes = [[
            GoTopLevelSymbol("Tag", "func", "alpha", str(alpha), 2),
            GoTopLevelSymbol("Tag", "func", "beta", str(beta), 1),
        ]]
        text = render_symbol_clashes(clashes, "main.lam")
    assert "error: top-level symbol clash while linking main.lam" in text
    assert "Go `func Tag` is declared by modules `alpha` and `beta`" in text
    assert f"{alpha}:2" in text
    assert ">>>    2 | func tag() -> str {" in text
    assert f"{beta}:4" in text, "go! declarations are located by name inside the block"
    assert ">>>    4 |     func Tag() string {" in text
    assert "help: rename one of the declarations" in text
    print("PASS: rendered diagnostic has snippets, backticks and help")


def main() -> int:
    tests = [
        test_simple_declarations,
        test_methods_are_not_top_level,
        test_generic_and_multiline_signatures,
        test_grouped_declarations,
        test_strings_and_comments_are_ignored,
        test_line_directives_attribute_lam_lines,
        test_init_and_blank_are_ignored,
        test_clashes_only_across_origins,
        test_three_way_clash_is_one_group,
        test_render_has_snippets_and_help,
    ]
    for test in tests:
        test()
    print(f"\nLink symbols: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
