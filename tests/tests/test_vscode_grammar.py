#!/usr/bin/env python3
from __future__ import annotations

import json
import plistlib
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
SYNTAXES = ROOT / "vs-code-extension" / "lammergeier-lang" / "syntaxes"
GRAMMAR = SYNTAXES / "lammergeier.tmLanguage.json"
PLIST_GRAMMAR = SYNTAXES / "Lammergeier.tmLanguage"
LANGUAGE_CONFIGURATION = (
    ROOT / "vs-code-extension" / "lammergeier-lang" / "language-configuration.json"
)


def assert_search(pattern: str, text: str) -> re.Match[str]:
    match = re.search(pattern, text, re.MULTILINE)
    assert match is not None, f"{pattern!r} did not match {text!r}"
    return match


def assert_no_search(pattern: str, text: str) -> None:
    assert re.search(pattern, text, re.MULTILINE) is None, (
        f"{pattern!r} unexpectedly matched {text!r}"
    )


def assert_any_fullmatch(patterns: list[dict[str, Any]], text: str) -> None:
    assert any(
        "match" in pattern and re.fullmatch(pattern["match"], text)
        for pattern in patterns
    ), f"no grammar pattern fully matched {text!r}"


def test_structure(grammar: dict[str, Any]) -> None:
    assert grammar["name"] == "Lammergeier"
    assert grammar["scopeName"] == "source.lammergeier"
    repository = grammar["repository"]
    for top_level in grammar["patterns"]:
        include = top_level["include"]
        assert include.startswith("#") and include[1:] in repository

    namespace = repository["lammergeier-namespace"]["patterns"][0]
    assert_search(namespace["match"], "LAMMERGEIER.Result.Ok")
    assert "invalid.illegal" not in json.dumps(repository["lammergeier-namespace"])

    keywords = " ".join(
        pattern["match"] for pattern in repository["keywords"]["patterns"]
    )
    for keyword in ("do", "throw", "defer", "await", "nonlocal", "lambda"):
        assert keyword in keywords
    assert "match" not in keywords and "case" not in keywords
    modifiers = repository["storage-modifiers"]["patterns"][0]["match"]
    assert "const" in modifiers and "await" not in modifiers
    assert "json" in repository["types"]["patterns"][0]["match"]


def test_go_blocks(repository: dict[str, Any]) -> None:
    inline, braced, indented, inline_expression = repository["go-block"]["patterns"]
    for pattern in (inline, braced, indented, inline_expression):
        assert {"include": "#lammergeier-namespace"} in pattern["patterns"]
        assert {"include": "source.go"} in pattern["patterns"]

    inline_line = 'go! { result = map[string]any{"ok": true} }'
    begin = assert_search(inline["begin"], inline_line)
    inline_tail = inline_line[begin.end():]
    inline_ends = list(re.finditer(inline["end"], inline_tail))
    assert len(inline_ends) == 1 and inline_ends[0].end() == len(inline_tail)

    braced_begin = assert_search(braced["begin"], "    go! {")
    braced_end = braced["end"].replace(r"\1", re.escape(braced_begin.group(1)))
    assert_no_search(braced_end, "        }")
    assert_search(braced_end, "    }")
    assert_no_search(braced_end, "    return lamValue;")

    indented_begin = assert_search(indented["begin"], "    go!:")
    indented_end = indented["end"].replace(
        r"\1", re.escape(indented_begin.group(1))
    )
    assert_no_search(indented_end, "        rawGo()")
    assert_search(indented_end, "    return lamValue;")
    assert_search(indented_end, "nextLamStatement();")

    assert_search(inline_expression["begin"], "value = go!(native.Call())")
    assert inline["begin"].endswith("$)")
    assert braced["end"].startswith(r"^\1")


def test_imports_and_decorators(repository: dict[str, Any]) -> None:
    from_import, plain_import = repository["imports"]["patterns"]
    scoped = assert_search(
        from_import["match"], "from @alice/lamwebp import Encoder"
    )
    assert scoped.group(3) == "@alice/lamwebp"
    nested = assert_search(
        from_import["match"], "from @lam/vulture/ws-plugin import WebSocket"
    )
    assert nested.group(3) == "@lam/vulture/ws-plugin"
    dotted = assert_search(
        from_import["match"], "from ...vendor.images.webp import Encoder"
    )
    assert dotted.group(3) == "...vendor.images.webp"
    imported = assert_search(plain_import["match"], "import vendor.images as images")
    assert imported.group(3) == "vendor.images"
    assert from_import["captures"]["3"]["name"].startswith(
        "entity.name.namespace"
    )

    decorator = repository["decorators"]["patterns"][0]
    dotted_decorator = assert_search(
        decorator["match"], '@web.routes.get("/users")'
    )
    assert dotted_decorator.group(3) == "web.routes.get"
    assert_search(decorator["match"], "    @memoize")
    assert_search(decorator["match"], "@private func helper() {")
    assert_no_search(decorator["match"], "from @alice/lamwebp import Encoder")
    assert_no_search(decorator["match"], "value = @matrix")


def test_contextual_keywords_and_generics(repository: dict[str, Any]) -> None:
    match_keyword, case_keyword = repository["contextual-keywords"]["patterns"]
    assert_search(match_keyword["match"], "match subject {")
    assert_search(case_keyword["match"], '    case "ready" if enabled {')
    for legal_identifier in (
        "match = callback;",
        "func use(match: any) {",
        "case = handler;",
        "case(value);",
        "obj.case;",
    ):
        assert_no_search(match_keyword["match"], legal_identifier)
        assert_no_search(case_keyword["match"], legal_identifier)

    function = repository["function-definition"]["patterns"][0]
    generic_function = assert_search(
        function["match"],
        "private static async func choose[T, U: comparable](left: T, right: U) (",
    )
    assert generic_function.group(3) == "choose"
    assert generic_function.group(4).strip() == "[T, U: comparable]"

    class_definition = repository["class-definition"]["patterns"][0]
    generic_class = assert_search(class_definition["match"], "class Cache[K, V] {")
    assert generic_class.group(2) == "Cache"
    assert generic_class.group(3).strip() == "[K, V]"


def test_strings(repository: dict[str, Any]) -> None:
    string_patterns = repository["strings"]["patterns"]
    prefixes = ("", "u", "b", "r", "ur", "br", "fr", "ru", "rb", "rf", "f")
    delimiters = ('"', "'", '"""', "'''")
    for prefix in prefixes:
        for delimiter in delimiters:
            opener = prefix + delimiter
            assert any(
                re.match(pattern["begin"], opener)
                for pattern in string_patterns
            ), f"no string begin rule matched {opener!r}"
            upper_opener = prefix.upper() + delimiter
            assert any(
                re.match(pattern["begin"], upper_opener)
                for pattern in string_patterns
            ), f"no string begin rule matched {upper_opener!r}"

    interpolated = [
        pattern
        for pattern in string_patterns
        if "interpolated" in pattern["name"]
    ]
    assert len(interpolated) == 4
    for opener in ('f"', "f'", 'f"""', "f'''"):
        pattern = next(
            pattern for pattern in interpolated if re.match(pattern["begin"], opener)
        )
        assert {"include": "#fstring-interpolation"} in pattern["patterns"]

    double_quoted = next(
        pattern
        for pattern in string_patterns
        if pattern["name"] == "string.quoted.double.lammergeier"
    )
    assert_search(double_quoted["end"], '\\\\"')
    assert_no_search(double_quoted["end"], '\\"')

    escape = repository["string-escape"]["patterns"][0]["match"]
    for escaped in (
        r"\n",
        r"\t",
        r"\\",
        r'\"',
        r"\'",
        r"\123",
        r"\x7f",
        r"\u03bb",
        r"\U0001f985",
        r"\N{LATIN SMALL LETTER A}",
    ):
        assert re.fullmatch(escape, escaped), f"escape not recognized: {escaped!r}"


def test_operators(repository: dict[str, Any]) -> None:
    patterns = repository["operators"]["patterns"]
    by_name = {pattern["name"]: pattern["match"] for pattern in patterns}
    samples = {
        "keyword.operator.arrow.lammergeier": ("->",),
        "keyword.operator.null-coalescing.lammergeier": ("??",),
        "keyword.operator.safe-navigation.lammergeier": ("?.",),
        "keyword.operator.comparison.lammergeier": (
            "==",
            "!=",
            "<=",
            ">=",
            "<",
            ">",
        ),
        "keyword.operator.assignment.lammergeier": (
            "=",
            ":=",
            "+=",
            "-=",
            "*=",
            "@=",
            "/=",
            "%=",
            "&=",
            "|=",
            "^=",
            "<<=",
            ">>=",
            "**=",
            "//=",
        ),
        "keyword.operator.increment-decrement.lammergeier": ("++", "--"),
        "keyword.operator.arithmetic.lammergeier": (
            "+",
            "-",
            "*",
            "@",
            "/",
            "%",
            "**",
            "//",
        ),
    }
    for scope, operators in samples.items():
        for operator in operators:
            assert re.fullmatch(by_name[scope], operator), (scope, operator)

    propagation = by_name["keyword.operator.propagation.lammergeier"]
    propagated = assert_search(propagation, "result?")
    assert propagated.group(0) == "?"
    assert_no_search(propagation, "result?? fallback")
    assert_no_search(propagation, "result?.value")

    names = [pattern["name"] for pattern in patterns]
    assert names.index("keyword.operator.comparison.lammergeier") < names.index(
        "keyword.operator.assignment.lammergeier"
    )
    assert names.index("keyword.operator.assignment.lammergeier") < names.index(
        "keyword.operator.arithmetic.lammergeier"
    )


def test_numbers_constants_and_builtins(repository: dict[str, Any]) -> None:
    number_patterns = repository["numbers"]["patterns"]
    for number in (
        "0",
        "1_000_000",
        "0x_FF_0a",
        "0o7_5_5",
        "0b_1010_0011",
        ".5",
        "1.",
        "1_000.25_0",
        "6.02e23",
        "1_0e-3_0",
        "2j",
        ".5J",
        "1.e+2j",
    ):
        assert_any_fullmatch(number_patterns, number)
    for invalid in ("1_", "1__0", "0x", "0b_", ".", "1e", "1e_2"):
        assert not any(
            re.fullmatch(pattern["match"], invalid) for pattern in number_patterns
        ), f"invalid number unexpectedly matched: {invalid!r}"

    constants = repository["constants"]["patterns"][0]["match"]
    for constant in ("True", "False", "None", "true", "false", "nil", "null"):
        assert re.fullmatch(constants, constant)

    builtins = repository["builtins"]["patterns"][0]["match"]
    assert_search(builtins, "type(value)")
    assert_search(builtins, "print (value)")
    assert_search(builtins, "panic(error)")
    assert_no_search(builtins, "println(value)")
    assert_no_search(builtins, "type = factory;")
    assert_no_search(builtins, "value = type;")


def test_stdlib_modules(repository: dict[str, Any]) -> None:
    module_pattern = repository["stdlib-modules"]["patterns"][0]["match"]
    assert module_pattern.startswith(r"\b(") and module_pattern.endswith(r")\b")
    grammar_modules = set(module_pattern[len(r"\b(") : -len(r")\b")].split("|"))
    library_modules = {path.stem for path in (ROOT / "lib").glob("*.lam")}
    assert grammar_modules == library_modules, (
        f"missing={sorted(library_modules - grammar_modules)}, "
        f"stale={sorted(grammar_modules - library_modules)}"
    )
    for module in library_modules:
        assert re.fullmatch(module_pattern, module)


def test_folding_configuration() -> None:
    configuration = json.loads(LANGUAGE_CONFIGURATION.read_text(encoding="utf-8"))
    markers = configuration["folding"]["markers"]
    for start in ("#- block comment", "    # region parser"):
        assert_search(markers["start"], start)
    for end in ("-#", "    # endregion"):
        assert_search(markers["end"], end)
    assert_no_search(markers["start"], "# ordinary comment")
    assert_no_search(markers["end"], "# region")


def first_difference(expected: Any, actual: Any, path: str = "grammar") -> str:
    if type(expected) is not type(actual):
        return f"{path}: type {type(expected).__name__} != {type(actual).__name__}"
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return f"{path}: keys {sorted(expected)} != {sorted(actual)}"
        for key in expected:
            difference = first_difference(expected[key], actual[key], f"{path}.{key}")
            if difference:
                return difference
        return ""
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}: length {len(expected)} != {len(actual)}"
        for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
            difference = first_difference(
                expected_item, actual_item, f"{path}[{index}]"
            )
            if difference:
                return difference
        return ""
    if expected != actual:
        return f"{path}: {expected!r} != {actual!r}"
    return ""


def test_plist_parity(grammar: dict[str, Any]) -> None:
    with PLIST_GRAMMAR.open("rb") as stream:
        plist_grammar = plistlib.load(stream)
    difference = first_difference(grammar, plist_grammar)
    assert not difference, (
        "Lammergeier.tmLanguage must be an exact plist equivalent of "
        f"lammergeier.tmLanguage.json: {difference}"
    )


def main() -> int:
    grammar = json.loads(GRAMMAR.read_text(encoding="utf-8"))
    repository = grammar["repository"]
    test_structure(grammar)
    test_go_blocks(repository)
    test_imports_and_decorators(repository)
    test_contextual_keywords_and_generics(repository)
    test_strings(repository)
    test_operators(repository)
    test_numbers_constants_and_builtins(repository)
    test_stdlib_modules(repository)
    test_folding_configuration()
    test_plist_parity(grammar)
    print("PASS: VS Code and Linguist grammars cover Lammergeier syntax and corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
