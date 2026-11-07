#!/usr/bin/env python3

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.formatter import format_lam_source  # noqa: E402


MESSY_SOURCE = """func main(){
value:int=1
items:list[int]=[]
lookup:dict[str,int]={}
if value>0{
print("x:y", value)# inline
}
}
"""

FORMATTED_SOURCE = """func main() {
    value: int = 1
    items: list[int] = []
    lookup: dict[str, int] = {}
    if value > 0 {
        print("x:y", value)  # inline
    }
}
"""

MESSY_GO_SOURCE = """go! { import (
"strings"
"fmt"
) }

func call(*items:str)->str{
raw:any=go!(strings.NewReader("x"))
_ = raw
go! {
fmt.Println(fmt.Sprintf("value:%d",42))
if true {
fmt.Println("ok")
}
}
return ",".join(items)
}
"""

# Raw Go in ``go!`` blocks is left byte for byte as written (gofmt used to
# reformat it, and its doc-comment rewriting turned ````x```` into
# typographic quotes); only the Lam code and the ``go! {`` line move.
FORMATTED_GO_SOURCE = """go! { import (
"strings"
"fmt"
) }

func call(*items: str) -> str {
    raw: any = go!(strings.NewReader("x"))
    _ = raw
    go! {
fmt.Println(fmt.Sprintf("value:%d",42))
if true {
fmt.Println("ok")
}
    }
    return ",".join(items)
}
"""

MESSY_COMPACT_SOURCE = """from appinfo import banner



func main(){
    values:list[int]=[1,2,3]; lookup:dict[str,int]={"a":1}
    if len(values)>0{print(banner("orders","0.1.0"))}}
"""

FORMATTED_COMPACT_SOURCE = """from appinfo import banner

func main() {
    values: list[int] = [1, 2, 3]
    lookup: dict[str, int] = {"a": 1}
    if len(values) > 0 {
        print(banner("orders", "0.1.0"))
    }
}
"""


def test_formatter_snapshot() -> None:
    result = format_lam_source(MESSY_SOURCE)
    assert result.changed
    assert result.text == FORMATTED_SOURCE, result.text
    print("PASS: formatter produces expected snapshot")


def test_formatter_is_idempotent() -> None:
    first = format_lam_source(MESSY_SOURCE).text
    second = format_lam_source(first)
    assert second.text == first
    assert not second.changed
    print("PASS: formatter is idempotent")


def test_formatter_keeps_raw_go_and_inline_go() -> None:
    result = format_lam_source(MESSY_GO_SOURCE)
    assert result.changed
    assert result.text == FORMATTED_GO_SOURCE, result.text
    second = format_lam_source(result.text)
    assert second.text == result.text
    assert not second.changed
    print("PASS: formatter keeps raw go! code and inline go verbatim")


def test_formatter_expands_compact_blocks_and_collapses_blank_lines() -> None:
    result = format_lam_source(MESSY_COMPACT_SOURCE)
    assert result.changed
    assert result.text == FORMATTED_COMPACT_SOURCE, result.text
    second = format_lam_source(result.text)
    assert second.text == result.text
    assert not second.changed
    print("PASS: formatter expands compact blocks and collapses blank lines")


def test_lamc_fmt_stdout_and_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.lam"
        path.write_text(MESSY_SOURCE, encoding="utf-8")
        stdout = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(path), "--stdout"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert stdout.returncode == 0, stdout.stderr
        assert stdout.stdout == FORMATTED_SOURCE
        check_bad = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(path), "--check"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert check_bad.returncode == 1
        write = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(path)],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert write.returncode == 0, write.stderr
        assert path.read_text(encoding="utf-8") == FORMATTED_SOURCE
        check_good = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(path), "--check"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert check_good.returncode == 0, check_good.stderr
    print("PASS: lamc fmt supports stdout, check, and file write modes")


def test_lamc_fmt_directory_recurses_lam_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        first = root / "src" / "main.lam"
        second = root / "src" / "nested" / "helper.lam"
        ignored = root / "src" / "notes.txt"
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_text(MESSY_SOURCE, encoding="utf-8")
        second.write_text('func helper(){print("ok")}\n', encoding="utf-8")
        ignored.write_text("func nope(){bad}\n", encoding="utf-8")

        check_bad = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(root / "src"), "--check"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert check_bad.returncode == 1
        assert "main.lam is not formatted" in check_bad.stderr
        assert "helper.lam is not formatted" in check_bad.stderr

        write = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(root / "src")],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert write.returncode == 0, write.stderr
        assert first.read_text(encoding="utf-8") == FORMATTED_SOURCE
        assert second.read_text(encoding="utf-8") == (
            'func helper() {\n'
            '    print("ok")\n'
            '}\n'
        )
        assert ignored.read_text(encoding="utf-8") == "func nope(){bad}\n"

        check_good = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(root / "src"), "--check"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert check_good.returncode == 0, check_good.stderr
    print("PASS: lamc fmt directory recursively formats .lam files")


def test_formatter_repairs_doc_terminator_and_preserves_text() -> None:
    source = (
        "class Client{\n"
        "    #- HTTP-date, Set-Cookie, transport-level, User-Agent.\n"
        "       Keep https://example.com and application/json verbatim. -  #\n"
        "func ok(self)->bool{return true}\n"
        "}\n"
    )
    result = format_lam_source(source)
    expected_comment = (
        "    #- HTTP-date, Set-Cookie, transport-level, User-Agent.\n"
        "       Keep https://example.com and application/json verbatim. -#"
    )
    assert expected_comment in result.text, result.text
    assert "-  #" not in result.text
    assert format_lam_source(result.text).text == result.text
    print("PASS: formatter repairs documentation terminators and preserves text")


def test_formatter_preserves_nested_scoped_import() -> None:
    source = (
        "from @lam/vulture/std-plugin import requestLog\n"
        "func main() {\n"
        "    print(requestLog)\n"
        "}\n"
    )
    result = format_lam_source(source)
    assert result.text == source
    assert not result.changed
    print("PASS: formatter preserves nested scoped imports")


def test_formatter_keeps_postfix_increment_and_destructuring() -> None:
    source = """class Counter {
    func init(self, n: int) { self.n: int = n }
    func __inc__(self) -> Counter { return Counter(self.n + 1) }
}

func main() {
    i = 0
    i++
    xs: list[int] = [1]
    xs[0]--
    c: Counter = Counter(0)
    c++
    user: dict[str, str] = {"name": "alice", "role": "admin"}
    {name, role: r} = user
    print(i, name, r, xs, c.n, i - -1)
}
"""
    result = format_lam_source(source)
    assert "    i++\n" in result.text, result.text
    assert "    xs[0]--\n" in result.text, result.text
    assert "    c++\n" in result.text, result.text
    assert "    {name, role: r} = user\n" in result.text, result.text
    assert "i - -1" in result.text, result.text
    assert "i + +" not in result.text and "c + +" not in result.text
    assert format_lam_source(result.text).text == result.text
    print("PASS: formatter keeps postfix ++/-- and dictionary destructuring")


def test_formatter_keeps_multiline_strings_and_patterns() -> None:
    source = """func main() {
  text = \"\"\"first
    indented { not a block }
last\"\"\"
  match [4, 5] {
    case [a, b] if a + b == 9 { print(a) }
    case _ { print("no") }
  }
  d: dict[str, any] = {"kind": "c"}
  match d {
    case {"kind": k} { print(k) }
    case _ { pass }
  }
  print(text)
}
"""
    result = format_lam_source(source)
    assert '    text = """first\n    indented { not a block }\nlast"""\n' in result.text, result.text
    assert "    match [4, 5] {" in result.text, result.text
    assert "case [a, b] if a + b == 9 {" in result.text, result.text
    assert '        case {"kind": k} {' in result.text, result.text
    assert format_lam_source(result.text).text == result.text
    print("PASS: formatter keeps multi-line string text, sequence and mapping patterns")


def test_formatter_keeps_go_comments_byte_for_byte() -> None:
    source = (
        "go! {\n"
        "    // maps become ``key[sub]`` and 'quoted' -- see `x`\n"
        "    func demoHelper() string {   return \"x\" }\n"
        "\t// tab-indented comment\n"
        "}\n\n"
        "func main() {\n"
        "    if true {\n"
        "        go! {\n"
        "            //   ``deep``  spacing kept\n"
        "            x := 1;  _ = x\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    result = format_lam_source(source)
    assert result.text == source, result.text
    assert not result.changed
    print("PASS: formatter leaves go! comments and spacing byte for byte")


def test_formatter_indents_multiline_literals_and_calls() -> None:
    source = (
        "func take(d: dict[str, int]) -> int {\n    return len(d)\n}\n\n"
        "func main() {\n"
        "    items: list[int] = [\n"
        "take( {\"a\": 1}),\n"
        "2\n"
        "]\n"
        "    print(take(\n"
        "1,\n"
        "2\n"
        "))\n"
        "    cfg: dict[str, list[int]] = {\n"
        "\"a\": [\n"
        "1,\n"
        "2\n"
        "],\n"
        "\"b\": []\n"
        "}\n"
        "}\n"
    )
    expected = (
        "func take(d: dict[str, int]) -> int {\n    return len(d)\n}\n\n"
        "func main() {\n"
        "    items: list[int] = [\n"
        "        take({\"a\": 1}),\n"
        "        2\n"
        "    ]\n"
        "    print(take(\n"
        "        1,\n"
        "        2\n"
        "    ))\n"
        "    cfg: dict[str, list[int]] = {\n"
        "        \"a\": [\n"
        "            1,\n"
        "            2\n"
        "        ],\n"
        "        \"b\": []\n"
        "    }\n"
        "}\n"
    )
    result = format_lam_source(source)
    assert result.text == expected, result.text
    assert format_lam_source(expected).text == expected
    print("PASS: formatter indents multi-line list / dict literals and call arguments")


def test_formatter_keeps_slices_compact() -> None:
    source = (
        "func main() {\n"
        "    s: str = \"abcdef\"\n"
        "    print(s[: 3], s[0: 7], s[1 :], s[:: 2], s[a+1:b], xs[i][1:])\n"
        "    fs: list[func(int) -> int] = [lambda x: x + 1]\n"
        "    d: dict[str, int] = {\"a\": 1}\n"
        "    print([{\"k\": v} for v in s[1:3]], d[\"a\"])\n"
        "}\n"
    )
    result = format_lam_source(source)
    assert "print(s[:3], s[0:7], s[1:], s[::2], s[a + 1:b], xs[i][1:])" in result.text, result.text
    assert "[lambda x: x + 1]" in result.text, result.text
    assert 'print([{"k": v} for v in s[1:3]], d["a"])' in result.text, result.text
    assert format_lam_source(result.text).text == result.text
    print("PASS: formatter writes slices without spaces around `:`")


def test_formatter_call_arguments_starting_with_literals() -> None:
    """4.2.1: an argument starting with ``{`` / ``[`` / markup gets no space
    after ``(`` (``res.json( {...})`` was written before 4.1.x), a dict
    literal alone on a continuation line stays a literal (it was laid out
    as a block, ``{`` / ``"k": v`` / ``}`` / ``,``), and a lambda with a
    block body keeps ``})`` / ``}, 0)`` after its body."""
    source = (
        "func f(a: any, b: any = None) -> any {\n    return a\n}\n\n"
        "func main() {\n"
        "    xs: list[int] = [1, 2]\n"
        "    f( {\"ok\": true})\n"
        "    f(f( {\"a\": 1}))\n"
        "    f( [1, 2])\n"
        "    f( <><b>x</b></>)\n"
        "    f( {\n"
        "        \"k\": 1\n"
        "    })\n"
        "    injected = f(\n"
        "    \"POST\",\n"
        "    {\"Content-Type\": \"application/json\"},\n"
        "    )\n"
        "    rows = f([\n"
        "{\"id\": 1},\n"
        "{\"id\": 2}\n"
        "])\n"
        "    page = f(\n"
        "<><p>hi</p></>,\n"
        "[3, 4]\n"
        ")\n"
        "    m = xs.map(lambda (x: int) -> int { return x })\n"
        "    n = xs.map(lambda (x: int) -> int {\n"
        "        return x\n"
        "    })\n"
        "    t = xs.reduce(lambda (a: int, b: int) -> int { return a + b }, 0)\n"
        "    pair = f(xs.map(lambda (x: int) -> int { return x }), {\"k\": 1})\n"
        "    print(injected, rows, page, m, n, t, pair)\n"
        "}\n"
    )
    expected = (
        "func f(a: any, b: any = None) -> any {\n    return a\n}\n\n"
        "func main() {\n"
        "    xs: list[int] = [1, 2]\n"
        "    f({\"ok\": true})\n"
        "    f(f({\"a\": 1}))\n"
        "    f([1, 2])\n"
        "    f(<><b>x</b></>)\n"
        "    f({\n"
        "        \"k\": 1\n"
        "    })\n"
        "    injected = f(\n"
        "        \"POST\",\n"
        "        {\"Content-Type\": \"application/json\"},\n"
        "    )\n"
        "    rows = f([\n"
        "        {\"id\": 1},\n"
        "        {\"id\": 2}\n"
        "    ])\n"
        "    page = f(\n"
        "        <><p>hi</p></>,\n"
        "        [3, 4]\n"
        "    )\n"
        "    m = xs.map(lambda (x: int) -> int {\n"
        "        return x\n"
        "    })\n"
        "    n = xs.map(lambda (x: int) -> int {\n"
        "        return x\n"
        "    })\n"
        "    t = xs.reduce(lambda (a: int, b: int) -> int {\n"
        "        return a + b\n"
        "    }, 0)\n"
        "    pair = f(xs.map(lambda (x: int) -> int {\n"
        "        return x\n"
        "    }), {\"k\": 1})\n"
        "    print(injected, rows, page, m, n, t, pair)\n"
        "}\n"
    )
    result = format_lam_source(source)
    assert result.text == expected, result.text
    assert format_lam_source(expected).text == expected
    assert not format_lam_source(expected).changed
    print("PASS: formatter keeps call arguments starting with `{` / `[` / markup and lambda bodies tight")


def test_formatter_indents_method_chains() -> None:
    """4.2.1: a method chain continued on the next lines (``.map(f)``) is
    indented one level under its statement, as written in the docs; it was
    moved back to the statement's level. A chain inside an open bracket is
    indented by the bracket only."""
    source = (
        "func main() {\n"
        "    xs: list[int] = [1, 2]\n"
        "    b: list[int] = xs\n"
        ".map(lambda x: x * 2)\n"
        "            .filter(lambda x: x > 2)\n"
        "    c: list[int] = (xs\n"
        "  .map(lambda x: x + 1)\n"
        "  .filter(lambda x: x > 1))\n"
        "    print(b, c)\n"
        "}\n"
    )
    expected = (
        "func main() {\n"
        "    xs: list[int] = [1, 2]\n"
        "    b: list[int] = xs\n"
        "        .map(lambda x: x * 2)\n"
        "        .filter(lambda x: x > 2)\n"
        "    c: list[int] = (xs\n"
        "        .map(lambda x: x + 1)\n"
        "        .filter(lambda x: x > 1))\n"
        "    print(b, c)\n"
        "}\n"
    )
    result = format_lam_source(source)
    assert result.text == expected, result.text
    assert format_lam_source(expected).text == expected
    print("PASS: formatter indents method chains continued on the next lines")


def test_formatter_keyword_arguments_and_type_parameters() -> None:
    """Keyword arguments (and keyword sub-patterns) are written without
    spaces around ``=`` and type parameters as ``T: ordered``, as in
    ``docs/SYNTAX.md`` (``sorted(words, reverse=true)``, ``func
    f[T: ordered](...)``); the formatter wrote ``reverse = true`` and
    ``[T:ordered]``. Typed parameter defaults keep ``name: str = "x"``,
    assignments and slices are unchanged."""
    source = (
        "class Registry[K : comparable, V] {\n"
        "    func init(self) {\n"
        "        self.items: dict[K, V] = {}\n"
        "    }\n"
        "\n"
        "    func pick[T:ordered](self, a: T, b: T) -> T {\n"
        "        return a if a > b else b\n"
        "    }\n"
        "}\n"
        "\n"
        "class Point {\n"
        "    func init(self, x: int, y: int) {\n"
        "        self.x: int = x\n"
        "        self.y: int = y\n"
        "    }\n"
        "}\n"
        "\n"
        "func top[T:ordered](xs: list[T], n: int=3, label: str  =  \"top\") -> list[T] {\n"
        "    return sorted(xs, reverse = true)[0 : n]\n"
        "}\n"
        "\n"
        "func greet(name: str = \"world\", times: int = 1) -> str {\n"
        "    return name * times\n"
        "}\n"
        "\n"
        "func where(p: Point) -> str {\n"
        "    match p {\n"
        "        case Point(x = 0, y = y) { return f\"y={y}\" }\n"
        "        case _ { return \"other\" }\n"
        "    }\n"
        "}\n"
        "\n"
        "func main() {\n"
        "    words: list[str] = [\"bb\", \"a\"]\n"
        "    a, b = 1, 2\n"
        "    d: dict[str, str] = {}\n"
        "    d[\"k\"] = greet(name = \"x\", times=3)\n"
        "    out = sorted(\n"
        "        words,\n"
        "        key = lambda w: len(greet(name = w)),\n"
        "        reverse = true,\n"
        "    )\n"
        "    words.foreach(lambda (w: str) {\n"
        "        n = len(w)\n"
        "        print(n == a, n <= b, greet(times = n))\n"
        "    })\n"
        "    print(top(words, n = 1), out, Point(x = 0, y = 1).x, where(Point(0, 2)), d)\n"
        "}\n"
    )
    expected = (
        "class Registry[K: comparable, V] {\n"
        "    func init(self) {\n"
        "        self.items: dict[K, V] = {}\n"
        "    }\n"
        "\n"
        "    func pick[T: ordered](self, a: T, b: T) -> T {\n"
        "        return a if a > b else b\n"
        "    }\n"
        "}\n"
        "\n"
        "class Point {\n"
        "    func init(self, x: int, y: int) {\n"
        "        self.x: int = x\n"
        "        self.y: int = y\n"
        "    }\n"
        "}\n"
        "\n"
        "func top[T: ordered](xs: list[T], n: int = 3, label: str = \"top\") -> list[T] {\n"
        "    return sorted(xs, reverse=true)[0:n]\n"
        "}\n"
        "\n"
        "func greet(name: str = \"world\", times: int = 1) -> str {\n"
        "    return name * times\n"
        "}\n"
        "\n"
        "func where(p: Point) -> str {\n"
        "    match p {\n"
        "        case Point(x=0, y=y) {\n"
        "            return f\"y={y}\"\n"
        "        }\n"
        "        case _ {\n"
        "            return \"other\"\n"
        "        }\n"
        "    }\n"
        "}\n"
        "\n"
        "func main() {\n"
        "    words: list[str] = [\"bb\", \"a\"]\n"
        "    a, b = 1, 2\n"
        "    d: dict[str, str] = {}\n"
        "    d[\"k\"] = greet(name=\"x\", times=3)\n"
        "    out = sorted(\n"
        "        words,\n"
        "        key=lambda w: len(greet(name=w)),\n"
        "        reverse=true,\n"
        "    )\n"
        "    words.foreach(lambda (w: str) {\n"
        "        n = len(w)\n"
        "        print(n == a, n <= b, greet(times=n))\n"
        "    })\n"
        "    print(top(words, n=1), out, Point(x=0, y=1).x, where(Point(0, 2)), d)\n"
        "}\n"
    )
    result = format_lam_source(source)
    assert result.text == expected, result.text
    assert format_lam_source(expected).text == expected
    assert not format_lam_source(expected).changed
    print("PASS: formatter writes keyword arguments as `k=v` and type parameters as `T: ordered`")


def test_formatter_inline_braces_keyword_parens_and_lambda_heads() -> None:
    """A ``{}`` / ``{...}`` operand of a conditional expression (``x if c
    else {}``, also in a lambda body) is a literal kept on its line; the
    formatter split it as a block (``else {`` / ``}``). A ``(`` after
    ``if`` / ``elif`` / ``while`` / ``match`` got two spaces (``if  (a
    and b) or (c)``); keywords before ``(`` now get exactly one. A
    parenthesised lambda head is written ``lambda (x: int)`` as in
    docs/SYNTAX.md (it was tightened to ``lambda(x: int)``), and a keyword
    argument's ``{...}`` value follows ``=`` directly (``attrs={...}``,
    it was written ``attrs= {...}``)."""
    source = (
        "from lamjson import Json\n"
        "\n"
        "func opts(attrs: dict[str, str] = {}, xs: list[int] = []) -> int {\n"
        "    return len(attrs) + len(xs)\n"
        "}\n"
        "\n"
        "func f(schema: json, metas: dict[str, dict[str, any]], k: str, a: bool, b: bool) -> json {\n"
        "    slots: json = schema if Json.kind(schema) == \"object\" else {}\n"
        "    meta: dict[str, any] = metas[k] if k in metas else {}\n"
        "    obj: dict[str, str] = {\"a\": \"b\"} if a else {\"type\": \"object\"}\n"
        "    other: dict[str, str] = obj if a else {\n"
        "        \"k\": \"v\"\n"
        "    }\n"
        "    if (a and b) or (b and a) {\n"
        "        print(meta)\n"
        "    } elif(a) {\n"
        "        print(1)\n"
        "    } else {\n"
        "        print(2)\n"
        "    }\n"
        "    if a { print(3) }\n"
        "    else { print(4) }\n"
        "    while  (a and b) {\n"
        "        break\n"
        "    }\n"
        "    x = not  (a or b)\n"
        "    y = a and(b or a)\n"
        "    for(i, j) in [(1, 2)] {\n"
        "        print(i, j)\n"
        "    }\n"
        "    match  (a) {\n"
        "        case _ { print(x, y) }\n"
        "    }\n"
        "    g = lambda(n: int) -> str { return str(n) }\n"
        "    h = lambda  (n: int) -> int: n * 2\n"
        "    w = lambda (n: int): {\"n\": n} if n > 0 else {}\n"
        "    n = opts(attrs = {\"k\": \"v\"}, xs = [1])\n"
        "    m = opts(\n"
        "        attrs = {\"k\": \"v\"},\n"
        "    )\n"
        "    print(g(1), h(2), w(3), other, n, m)\n"
        "    return (slots)\n"
        "}\n"
    )
    expected = (
        "from lamjson import Json\n"
        "\n"
        "func opts(attrs: dict[str, str] = {}, xs: list[int] = []) -> int {\n"
        "    return len(attrs) + len(xs)\n"
        "}\n"
        "\n"
        "func f(schema: json, metas: dict[str, dict[str, any]], k: str, a: bool, b: bool) -> json {\n"
        "    slots: json = schema if Json.kind(schema) == \"object\" else {}\n"
        "    meta: dict[str, any] = metas[k] if k in metas else {}\n"
        "    obj: dict[str, str] = {\"a\": \"b\"} if a else {\"type\": \"object\"}\n"
        "    other: dict[str, str] = obj if a else {\n"
        "        \"k\": \"v\"\n"
        "    }\n"
        "    if (a and b) or (b and a) {\n"
        "        print(meta)\n"
        "    } elif (a) {\n"
        "        print(1)\n"
        "    } else {\n"
        "        print(2)\n"
        "    }\n"
        "    if a {\n"
        "        print(3)\n"
        "    } else {\n"
        "        print(4)\n"
        "    }\n"
        "    while (a and b) {\n"
        "        break\n"
        "    }\n"
        "    x = not (a or b)\n"
        "    y = a and (b or a)\n"
        "    for (i, j) in [(1, 2)] {\n"
        "        print(i, j)\n"
        "    }\n"
        "    match (a) {\n"
        "        case _ {\n"
        "            print(x, y)\n"
        "        }\n"
        "    }\n"
        "    g = lambda (n: int) -> str {\n"
        "        return str(n)\n"
        "    }\n"
        "    h = lambda (n: int) -> int: n * 2\n"
        "    w = lambda (n: int): {\"n\": n} if n > 0 else {}\n"
        "    n = opts(attrs={\"k\": \"v\"}, xs=[1])\n"
        "    m = opts(\n"
        "        attrs={\"k\": \"v\"},\n"
        "    )\n"
        "    print(g(1), h(2), w(3), other, n, m)\n"
        "    return (slots)\n"
        "}\n"
    )
    result = format_lam_source(source)
    assert result.text == expected, result.text
    assert format_lam_source(expected).text == expected
    assert not format_lam_source(expected).changed
    print("PASS: formatter keeps inline `{}` operands, single-spaces `keyword (` and writes `lambda (x: T)`")


def main() -> int:
    tests = [
        test_formatter_snapshot,
        test_formatter_is_idempotent,
        test_formatter_keeps_raw_go_and_inline_go,
        test_formatter_expands_compact_blocks_and_collapses_blank_lines,
        test_lamc_fmt_stdout_and_check,
        test_lamc_fmt_directory_recurses_lam_files,
        test_formatter_repairs_doc_terminator_and_preserves_text,
        test_formatter_preserves_nested_scoped_import,
        test_formatter_keeps_postfix_increment_and_destructuring,
        test_formatter_keeps_multiline_strings_and_patterns,
        test_formatter_keeps_go_comments_byte_for_byte,
        test_formatter_indents_multiline_literals_and_calls,
        test_formatter_keeps_slices_compact,
        test_formatter_call_arguments_starting_with_literals,
        test_formatter_indents_method_chains,
        test_formatter_keyword_arguments_and_type_parameters,
        test_formatter_inline_braces_keyword_parens_and_lambda_heads,
    ]
    for test in tests:
        test()
    print(f"\nformatter results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
