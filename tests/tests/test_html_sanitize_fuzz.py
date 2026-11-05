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

A third policy, ``rich`` with ``allowFrames(HtmlPolicy.videoHosts())``, runs
on the same inputs plus frame-focused ones (``iframe`` / ``frame`` in mixed
case with look-alike hosts, userinfo, ports, downgrades, encoded schemes,
``srcdoc``, ``sandbox`` / ``allow`` / ``referrerpolicy`` values, nesting in
``svg`` / ``math`` / other frames): every ``iframe`` left must load an
``https`` URL of a listed host or a subdomain of one, carry only the fixed
attributes with the forced ``sandbox``, safe ``allow`` features and
referrer policy, and have no content.
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
    frames: HtmlPolicy = HtmlPolicy.rich().allowFrames(HtmlPolicy.videoHosts())
    rows: list[list[str]] = []
    for i in range(len(doc)) {
        text: str = doc[i]
        b: str = str(Html.sanitize(text, basic))
        r: str = str(Html.sanitize(text, rich))
        f: str = str(Html.sanitize(text, frames))
        rows.append([b, str(Html.sanitize(b, basic)), r, str(Html.sanitize(r, rich)), f, str(Html.sanitize(f, frames))])
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
FRAME_TAGS = RICH_TAGS | {"iframe"}
FRAME_ATTRS = dict(RICH_ATTRS)
FRAME_ATTRS["*"] = {"class", "title", "lang", "dir"}
FRAME_ATTRS["iframe"] = {
    "src", "width", "height", "title", "allow", "allowfullscreen", "loading", "referrerpolicy", "sandbox",
}
FRAME_SANDBOX = "allow-scripts allow-same-origin allow-presentation allow-popups"
FRAME_SRC = re.compile(
    r"^https://(?:[a-z0-9-]+\.)*(?:www\.youtube\.com|www\.youtube-nocookie\.com|player\.vimeo\.com)(?:[/?#]|$)"
)
FRAME_FEATURES = {
    "accelerometer", "autoplay", "clipboard-write", "encrypted-media", "gyroscope", "picture-in-picture",
    "fullscreen",
}
FRAME_REFERRERS = {"no-referrer", "origin", "same-origin", "strict-origin", "strict-origin-when-cross-origin"}
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
FRAME_ELEMENTS = ["iframe", "IFRAME", "IfRaMe", "frame", "svg", "math", "p", "object", "embed"]
FRAME_ATTR_NAMES = [
    "src", "SRC", "srcdoc", "sandbox", "allow", "allowfullscreen", "referrerpolicy", "loading", "name",
    "width", "height", "title", "onload", "style", "csp", "credentialless",
]
FRAME_URLS = [
    "https://www.youtube.com/embed/x", "HTTPS://WWW.YOUTUBE.COM:443/embed/y", "https://player.vimeo.com/video/1",
    "https://www.youtube-nocookie.com/embed/z?a=1&b=2", "https://sub.player.vimeo.com/v",
    "https://www.youtube.com.evil.com/x", "https://evil.com/?www.youtube.com", "https://evil.com#www.youtube.com",
    "https://www.youtube.com@evil.com/", "https://u:p@www.youtube.com/", "https://youtube.com/embed/x",
    "https://evilwww.youtube.com/", "http://www.youtube.com/embed/x", "//www.youtube.com/embed/x",
    "/embed/x", "ht\ttps://www.youtube.com/embed/x", "ht&#9;tps://www.youtube.com/", "https://www.youtube.com:8080/",
    "https://www.y\u043eutube.com/", "https://www.youtube.com%2eevil.com/", "https://evil.com\\@www.youtube.com/",
    "https:///evil.com/", "https://[::1]/", "https://127.0.0.1/", "javascript:alert(1)//www.youtube.com",
    "&#106;avascript:alert(1)", "data:text/html,<script>alert(1)</script>", " https://www.youtube.com/embed/x ",
    "https://www.youtube.com./x", "https://www.youtube.com:0443/embed/x", "https://www.youtube.com:99999/",
]
FRAME_VALUES = {
    "sandbox": ["allow-top-navigation allow-forms", "", "allow-scripts", "allow-popups-to-escape-sandbox"],
    "allow": ["autoplay; camera; microphone", "fullscreen *; payment", "geolocation", "AUTOPLAY; autoplay"],
    "referrerpolicy": ["unsafe-url", "no-referrer", "ORIGIN", "no-referrer-when-downgrade", "x"],
    "loading": ["lazy", "EAGER", "auto"],
    "width": ["560", "100%", "1e9", "-1", "\"><script>"],
    "height": ["315", "0", "abc"],
}
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


def _frame_attr(rng: random.Random) -> str:
    name = rng.choice(FRAME_ATTR_NAMES)
    lower = name.lower()
    if lower == "src":
        value = rng.choice(FRAME_URLS)
    elif lower in FRAME_VALUES:
        value = rng.choice(FRAME_VALUES[lower])
    elif lower == "allowfullscreen" and rng.random() < 0.5:
        return f" {name}"
    else:
        value = rng.choice(["<script>alert(1)</script>", "alert(1)", "x", "javascript:alert(1)"])
    quote = rng.choice(["\"", "'", "\""])
    return f" {name}={quote}{value}{quote}"


def _frame_markup(rng: random.Random) -> str:
    out = []
    for _ in range(rng.randint(1, 6)):
        roll = rng.random()
        if roll < 0.55:
            tag = rng.choice(FRAME_ELEMENTS)
            attrs = "".join(_frame_attr(rng) for _ in range(rng.randint(0, 5)))
            if rng.random() < 0.5:
                attrs = f" src=\"{rng.choice(FRAME_URLS)}\"" + attrs
            out.append(f"<{tag}{attrs}{rng.choice(['>', '>', '/>', ''])}")
        elif roll < 0.8:
            out.append(f"</{rng.choice(FRAME_ELEMENTS)}>")
        else:
            out.append(rng.choice(FRAGMENTS + ["fallback", "<b>x</b>"]))
    return "".join(out)


class _Checker(HTMLParser):
    def __init__(self, tags: set, attrs: dict, rich: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.tags, self.attrs, self.rich = tags, attrs, rich
        self.problems: list[str] = []
        self.in_frame = False

    def _check_frame(self, attrs: list) -> None:
        found = dict(attrs)
        if len(found) != len(attrs):
            self.problems.append("duplicated iframe attribute")
        if found.get("sandbox") != FRAME_SANDBOX:
            self.problems.append(f"iframe sandbox={found.get('sandbox')!r}")
        if not FRAME_SRC.match(found.get("src") or ""):
            self.problems.append(f"iframe src={found.get('src')!r}")
        if found.get("referrerpolicy") not in FRAME_REFERRERS:
            self.problems.append(f"iframe referrerpolicy={found.get('referrerpolicy')!r}")
        if "allow" in found and any(f.strip() not in FRAME_FEATURES for f in (found["allow"] or "").split(";")):
            self.problems.append(f"iframe allow={found['allow']!r}")
        for size in ("width", "height"):
            if size in found and not re.match(r"^[0-9]{1,5}%?$", found[size] or ""):
                self.problems.append(f"iframe {size}={found[size]!r}")

    def _check(self, tag: str, attrs: list) -> None:
        if self.in_frame:
            self.problems.append(f"<{tag}> inside an iframe")
        if tag == "iframe" and tag in self.tags:
            self._check_frame(attrs)
            self.in_frame = True
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

    def handle_data(self, data):
        if self.in_frame:
            self.problems.append("content inside an iframe")

    def handle_endtag(self, tag):
        if tag == "iframe":
            self.in_frame = False
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
    frame_rng = random.Random(SEED + 1)
    inputs += [_frame_markup(frame_rng) for _ in range(CASES)]
    outputs = _run(inputs)
    assert len(outputs) == len(inputs)
    failures = []
    kept_frames = 0
    for source, (basic, basic_again, rich, rich_again, framed, framed_again) in zip(inputs, outputs):
        kept_frames += framed.count("<iframe")
        for label, out, again, tags, attrs, is_rich in (
            ("basic", basic, basic_again, BASIC_TAGS, BASIC_ATTRS, False),
            ("rich", rich, rich_again, RICH_TAGS, RICH_ATTRS, True),
            ("frames", framed, framed_again, FRAME_TAGS, FRAME_ATTRS, False),
        ):
            problems = _problems(out, tags, attrs, is_rich)
            if out != again:
                problems.append("not idempotent")
            if problems:
                failures.append(f"{label} {source!r} -> {out!r}: {', '.join(problems)}")
    assert not failures, f"{len(failures)} unsafe outputs, first: " + "\n".join(failures[:5])
    # The frame inputs must exercise the kept path too, not only refusals.
    assert kept_frames >= 50, f"only {kept_frames} frames kept"


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
    print(f"\nhtml sanitize fuzz: {len(tests) - failed} passed, {failed} failed, {len(tests)} total ({2 * CASES} inputs)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
