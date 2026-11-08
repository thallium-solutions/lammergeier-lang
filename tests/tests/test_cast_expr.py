#!/usr/bin/env python3
"""Grammar and formatter contracts of the inline cast ``expr as T``.

``as`` binds tighter than every binary operator and looser than unary
operators, ``**``, calls, indexing and member access (like Kotlin), so
``row["n"] as int + 1`` is ``(row["n"] as int) + 1`` and ``a + b as int``
is ``a + (b as int)``. The other ``as`` forms (``import x as y``, ``from m
import a as b``, ``catch E as e``, ``with r as f``, ``case P as name``)
keep their meaning.
"""

from __future__ import annotations

import sys
from pathlib import Path

from lark import Token, Tree

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.formatter import format_lam_source  # noqa: E402
from compiler.lammergeier import create_parser  # noqa: E402
from compiler.typesys import parse_type, render_type  # noqa: E402


def _expr(source: str):
    tree = create_parser().parse(f"__probe = {source};\n")
    assign = next(tree.find_data("assign"))
    return assign.children[1]


def _render(node) -> str:
    """The expression with every operator node fully parenthesised."""
    if isinstance(node, Token):
        return str(node)
    data = node.data
    kids = node.children
    if data == "var":
        return _render(kids[0])
    if data in {"name", "number", "string"}:
        return str(kids[0])
    if data == "cast_expr":
        return f"({_render(kids[0])} as {render_type(parse_type(kids[1]))})"
    if data in {"arith_expr", "term", "shift_expr"}:
        return "(" + " ".join(_render(k) for k in kids) + ")"
    if data in {"or_expr", "and_expr", "xor_expr"}:
        op = {"or_expr": " | ", "and_expr": " & ", "xor_expr": " ^ "}[data]
        return "(" + op.join(_render(k) for k in kids) + ")"
    if data == "comparison":
        parts = []
        for kid in kids:
            if isinstance(kid, Tree) and kid.data == "comp_op":
                parts.append(" ".join(str(t) for t in kid.children))
            else:
                parts.append(_render(kid))
        return "(" + " ".join(parts) + ")"
    if data == "factor":
        return f"({kids[0]}{_render(kids[1])})"
    if data == "power":
        return f"({_render(kids[0])} ** {_render(kids[1])})"
    if data == "not_test":
        return f"(not {_render(kids[0])})"
    if data in {"and_test", "or_test"}:
        op = " and " if data == "and_test" else " or "
        return "(" + op.join(_render(k) for k in kids) + ")"
    if data == "null_coalesce":
        return "(" + " ?? ".join(_render(k) for k in kids) + ")"
    if data == "test":
        return f"({_render(kids[0])} if {_render(kids[1])} else {_render(kids[2])})"
    if data == "getattr":
        return f"{_render(kids[0])}.{_render(kids[1])}"
    if data == "getitem":
        return f"{_render(kids[0])}[{_render(kids[1])}]"
    if data == "funccall":
        args = kids[1].children if len(kids) > 1 and kids[1] is not None else []
        return f"{_render(kids[0])}(" + ", ".join(_render(a) for a in args) + ")"
    raise AssertionError(f"unexpected node {data}")


PRECEDENCE = {
    'row["n"] as int + 1': '((row["n"] as int) + 1)',
    "a + b as int": "(a + (b as int))",
    "a * b as float": "(a * (b as float))",
    "2 * v as int + 1": "((2 * (v as int)) + 1)",
    "-x as float": "((-x) as float)",
    "-x as float * 2": "(((-x) as float) * 2)",
    "x ** 2 as int": "((x ** 2) as int)",
    "v as int < 3": "((v as int) < 3)",
    "v as int == 3 and w as bool": "(((v as int) == 3) and (w as bool))",
    "not v as bool": "(not (v as bool))",
    "v as int | 1": "((v as int) | 1)",
    "v as (int | str) | 1": "((v as int | str) | 1)",
    "v as any as int": "((v as any) as int)",
    "v as list[str]": "(v as list[str])",
    "v as dict[str, list[int]]": "(v as dict[str, list[int]])",
    "v as optional[User]": "(v as optional[User])",
    "v as pkg.User": "(v as pkg.User)",
    "f(x) as User": "(f(x) as User)",
    "a.b[0] as int": "(a.b[0] as int)",
    "v as int if c else 0": "((v as int) if c else 0)",
    "v ?? w as int": "(v ?? (w as int))",
    "v << w as int": "(v << (w as int))",
}


def test_cast_precedence() -> None:
    for source, expected in PRECEDENCE.items():
        actual = _render(_expr(source))
        assert actual == expected, f"{source!r}: {actual!r} != {expected!r}"
    member = _expr('(lookup("u") as User).name')
    assert member.data == "getattr" and member.children[0].data == "cast_expr", member
    print(f"PASS: `as` precedence ({len(PRECEDENCE)} shapes)")


def test_other_as_forms_keep_their_meaning() -> None:
    parser = create_parser()
    source = (
        "import lamstrings as ls;\n"
        "from lamjson import Json as J;\n"
        "try { a = 1; } catch TypeError as e { b = 2; } catch pkg.Err as f { c = 3; } catch { d = 4; }\n"
        "with open(\"x\") as fh { e = 5; }\n"
        "with lock { g = 6; }\n"
        "match v { case [1, *_] as xs { h = 7; } }\n"
    )
    tree = parser.parse(source)
    assert not list(tree.find_data("cast_expr")), tree.pretty()
    clauses = list(tree.find_data("catch_clause"))
    names = [
        [str(k.children[0]) for k in c.children if isinstance(k, Tree) and k.data == "name"]
        for c in clauses
    ]
    assert names == [["e"], ["f"], []], names
    with_items = list(tree.find_data("with_item"))
    assert [len(item.children) for item in with_items] == [2, 1], with_items
    assert next(tree.find_data("import_as_name")).children[1].children[0] == "J"
    print("PASS: import / from / catch / with / case `as` forms are not casts")


def test_formatter_spaces_cast() -> None:
    messy = (
        "func main() {\n"
        "    row: dict[str, any] = {}\n"
        "    n: int = row[\"n\"]   as    int+1\n"
        "    xs: list[str] = row[\"xs\"]as list[str]\n"
        "    print(n, xs, (row[\"u\"]  as  any)as int)\n"
        "    alias = row[\"k\"] as(int | str)\n"
        "    print(alias)\n"
        "}\n"
    )
    expected = (
        "func main() {\n"
        "    row: dict[str, any] = {}\n"
        "    n: int = row[\"n\"] as int + 1\n"
        "    xs: list[str] = row[\"xs\"] as list[str]\n"
        "    print(n, xs, (row[\"u\"] as any) as int)\n"
        "    alias = row[\"k\"] as (int | str)\n"
        "    print(alias)\n"
        "}\n"
    )
    result = format_lam_source(messy)
    assert result.text == expected, result.text
    assert not format_lam_source(expected).changed
    print("PASS: formatter writes one space around `as`")


def main() -> int:
    test_cast_precedence()
    test_other_as_forms_keep_their_meaning()
    test_formatter_spaces_cast()
    print("\ncast expression results: 3 passed, 0 failed, 3 total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
