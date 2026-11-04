#!/usr/bin/env python3
"""Fuzz contract of ``Html.sanitize`` (lib/lamhtml.lam).

Random markup assembled from XSS building blocks (dangerous elements in
mixed case, event handlers, ``javascript:`` URLs hidden behind entities,
tabs and control characters, broken tags, comments, CDATA, raw-text
elements, odd quoting) goes through one compiled Lam program with the
``basic`` policy and a ``rich`` policy that also allows styles and data
images. Each output is then read back with Python's own HTML tokenizer
(``html.parser``, independent of the Lam parser, decoding entities as a
browser does) and must hold only allowed elements and attributes, no
comments / declarations / processing instructions, no event handlers, and
URL attributes whose scheme (read as browsers read it) is allowed.
Sanitizing an output again must not change it.
"""

from __future__ import annotations

import json
import random
import re
import subprocess
import sys
import tempfile
import textwrap
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"
CASES = 1500
SEED = 4242

PROGRAM = """
from lamhtml import Html, HtmlPolicy
from lamjson import Json
from lamos import Os

func main() {
    doc: json = Json.decode(Os.readStdin().unwrap()).unwrap()
    basic: HtmlPolicy = HtmlPolicy.basic()
    rich: HtmlPolicy = HtmlPolicy.rich().allowStyles(["color", "text-align"]).allowDataImages()
    rows: list[list[str]] = []
    for i in range(len(doc)) {
        text: str = doc[i]
        b: str = str(Html.sanitize(text, basic))
        r: str = str(Html.sanitize(text, rich))
        rows.append([b, str(Html.sanitize(b, basic)), r, str(Html.sanitize(r, rich))])
    }
    print(Json.encode(Json.fromValue(rows).unwrap()).unwrap())
}
"""

BASIC_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "cite", "code", "del", "em", "i", "ins", "kbd", "li",
    "mark", "ol", "p", "pre", "q", "s", "small", "span", "strong", "sub", "sup", "u", "ul",
}
RICH_TAGS = BASIC_TAGS | {
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "div", "section", "article", "aside", "header",
    "footer", "img", "figure", "figcaption", "table", "caption", "thead", "tbody", "tfoot", "tr",
    "th", "td", "dl", "dt", "dd", "details", "summary",
}
BASIC_ATTRS = {"a": {"href", "title"}, "abbr": {"title"}, "blockquote": {"cite"}, "q": {"cite"}}
RICH_ATTRS = {
    "*": {"class", "title", "lang", "dir", "style"},
    "a": {"href", "title", "target", "rel"},
    "abbr": {"title"}, "blockquote": {"cite"}, "q": {"cite"},
    "img": {"src", "alt", "width", "height"},
    "th": {"colspan", "rowspan", "scope"}, "td": {"colspan", "rowspan"},
    "details": {"open"},
}
URL_ATTRS = {"href", "src", "cite", "action", "formaction", "poster", "background", "xlink:href"}
SCHEMES = {"http", "https", "mailto"}
DATA_IMAGE = re.compile(r"^data:image/(png|gif|jpeg|jpg|webp|avif);base64,[A-Za-z0-9+/=]*$", re.I)

TAGS = [
    "script", "SCRIPT", "ScRiPt", "img", "IMG", "a", "A", "svg", "math", "style", "iframe", "p", "b",
    "div", "object", "embed", "form", "input", "textarea", "title", "noscript", "xmp", "plaintext",
    "template", "details", "table", "td", "select", "option", "meta", "link", "base", "body", "html",
    "custom-x", "span", "h2", "frameset", "applet", "image", "video", "audio", "source",
]
ATTRS = [
    "onerror", "onload", "ONCLICK", "onmouseover", "OnFocus", "href", "HREF", "src", "style",
    "srcset", "action", "formaction", "xlink:href", "srcdoc", "target", "rel", "class", "id",
    "data-x", "open", "background", "poster", "cite", "title", "alt", "colspan",
]
URLS = [
    "javascript:alert(1)", "JaVaScRiPt:alert(1)", "java\tscript:alert(1)", "java&#x09;script:alert(1)",
    "&#106;avascript:alert(1)", "&#x6A;avascript:alert(1)", " \x01javascript:alert(1)", "vbscript:x",
    "data:text/html,<script>alert(1)</script>", "data:image/svg+xml;base64,PHN2Zz4=",
    "data:image/png;base64,iVBORw0KGgo=", "https://ok.example/", "/rel", "#frag", "//evil.example/x",
    "javascript&colon;alert(1)", "jav&NewLine;ascript:alert(1)", "\x00javascript:alert(1)",
    "mailto:a@b", "x:y", "http://a b", "&#0000106avascript:alert(1)", "j\na\nv\na\ns\nc\nr\ni\np\nt:1",
    "&Tab;javascript:alert(1)", "DATA:image/png;base64,AAAA", "  https://ok.example/?a=1&b=2",
]
STYLES = [
    "color:red", "background:url(javascript:alert(1))", "width:expression(alert(1))", "color:\\72 ed",
    "color: red; position: fixed", "x:y;color:blue", "text-align:center", "color:red/*x*/",
]
TARGETS = ["_blank", "_self", "other", "_top", " _BLANK "]
FRAGMENTS = [
    "<!--", "-->", "<![CDATA[", "]]>", "</", "<", ">", "&lt;", "&#60;", "\"", "'", "=", "/", "text",
    "\u2028", "<!DOCTYPE html>", "<?xml version='1.0'?>", "&amp;", "&", "</script>", "</style>",
    "<!-- <script>alert(1)</script> -->", "<scr<script>ipt>", "\x00", "`", "<a/", "<<", "é",
]


def _value(rng: random.Random, name: str) -> str:
    lower = name.lower()
    if lower in {"href", "src", "action", "formaction", "xlink:href", "background", "poster", "cite"}:
        return rng.choice(URLS)
    if lower == "srcset":
        return ", ".join(f"{rng.choice(URLS)} {rng.randint(1, 3)}x" for _ in range(rng.randint(1, 3)))
    if lower == "style":
        return rng.choice(STYLES)
    if lower == "target":
        return rng.choice(TARGETS)
    return rng.choice(["alert(1)", "x", "\"><script>alert(1)</script>", "1", "javascript:alert(1)", ""])


def _attr(rng: random.Random) -> str:
    name = rng.choice(ATTRS)
    value = _value(rng, name)
    quote = rng.choice(["\"", "'", "", "`", "\""])
    if quote == "" and rng.random() < 0.2:
        return f" {name}"
    if quote == "":
        value = value.replace(" ", "")
    return f" {name}={quote}{value}{quote}"


def _markup(rng: random.Random) -> str:
    out = []
    for _ in range(rng.randint(1, 12)):
        roll = rng.random()
        if roll < 0.45:
            tag = rng.choice(TAGS)
            attrs = "".join(_attr(rng) for _ in range(rng.randint(0, 3)))
            close = rng.choice([">", ">", "/>", " >", ""])
            out.append(f"<{tag}{attrs}{close}")
        elif roll < 0.6:
            out.append(f"</{rng.choice(TAGS)}>")
        elif roll < 0.85:
            out.append(rng.choice(FRAGMENTS))
        else:
            out.append(rng.choice(["hello", "a < b", "x & y", "1 > 0", " "]))
    return "".join(out)


class _Checker(HTMLParser):
    def __init__(self, tags: set, attrs: dict, rich: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.rich = tags, attrs, rich
        self.problems: list[str] = []

    def _check(self, tag: str, attrs: list) -> None:
        if tag not in self.tags:
            self.problems.append(f"element <{tag}>")
            return
        allowed = self.attrs.get(tag, set()) | self.attrs.get("*", set())
        found = dict(attrs)
        for name, value in attrs:
            value = value or ""
            if name.startswith("on") and name != "open":
                self.problems.append(f"event handler {name}")
            if name not in allowed and not (name == "rel" and "target" in found):
                self.problems.append(f"attribute {name} on <{tag}>")
            if name in URL_ATTRS:
                # Browsers strip leading/trailing C0 controls and spaces and
                # remove tabs and line breaks anywhere before the scheme.
                url = re.sub(r"[\t\n\r]", "", value.strip("".join(chr(c) for c in range(0x21))))
                match = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):", url)
                if match:
                    scheme = match.group(1).lower()
                    image = self.rich and tag == "img" and name == "src" and DATA_IMAGE.match(url)
                    if scheme not in SCHEMES and not image:
                        self.problems.append(f"{name}={value!r}")
            if name == "style":
                lower = value.lower()
                if any(bad in lower for bad in ("url(", "expression", "\\", "/*", "javascript")):
                    self.problems.append(f"style={value!r}")
            if name == "target" and value.strip().lower() not in {"", "_self", "_parent", "_top"}:
                rel = (found.get("rel") or "").split()
                if "noopener" not in rel:
                    self.problems.append("target without noopener")

    def handle_starttag(self, tag, attrs):
        self._check(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._check(tag, attrs)

    def handle_endtag(self, tag):
        if tag not in self.tags:
            self.problems.append(f"end tag </{tag}>")

    def handle_comment(self, data):
        self.problems.append("comment")

    def handle_decl(self, decl):
        self.problems.append("declaration")

    def handle_pi(self, data):
        self.problems.append("processing instruction")

    def unknown_decl(self, data):
        self.problems.append("unknown declaration")


def _problems(output: str, tags: set, attrs: dict, rich: bool) -> list[str]:
    checker = _Checker(tags, attrs, rich)
    checker.feed(output)
    checker.close()
    problems = list(checker.problems)
    # Every "<" of the output starts an allowed element or end tag.
    starts = output.count("<")
    tag_starts = re.findall(r"</?([a-z][a-z0-9-]*)[\s>/]", output)
    if starts != len(tag_starts) or any(name not in tags for name in tag_starts):
        problems.append("stray markup")
    return problems


def _run(inputs: list[str]) -> list[list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        source = td / "fuzz.lam"
        source.write_text(textwrap.dedent(PROGRAM).lstrip("\n"), encoding="utf-8")
        binary = td / "fuzz"
        built = subprocess.run(
            [sys.executable, str(COMPILER), str(source), "-o", str(binary)],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
        )
        assert built.returncode == 0, built.stderr + built.stdout
        ran = subprocess.run(
            [str(binary)], input=json.dumps(inputs), capture_output=True, text=True, timeout=120,
        )
        assert ran.returncode == 0, ran.stderr
        return json.loads(ran.stdout)


def test_random_markup_comes_out_clean_and_stable() -> None:
    rng = random.Random(SEED)
    inputs = [_markup(rng) for _ in range(CASES)]
    outputs = _run(inputs)
    assert len(outputs) == len(inputs)
    failures = []
    for source, (basic, basic_again, rich, rich_again) in zip(inputs, outputs):
        for label, out, again, tags, attrs, is_rich in (
            ("basic", basic, basic_again, BASIC_TAGS, BASIC_ATTRS, False),
            ("rich", rich, rich_again, RICH_TAGS, RICH_ATTRS, True),
        ):
            problems = _problems(out, tags, attrs, is_rich)
            if out != again:
                problems.append("not idempotent")
            if problems:
                failures.append(f"{label} {source!r} -> {out!r}: {', '.join(problems)}")
    assert not failures, f"{len(failures)} unsafe outputs, first: " + "\n".join(failures[:5])


def main() -> int:
    tests = [test_random_markup_comes_out_clean_and_stable]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\nhtml sanitize fuzz: {len(tests) - failed} passed, {failed} failed, {len(tests)} total ({CASES} inputs)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
