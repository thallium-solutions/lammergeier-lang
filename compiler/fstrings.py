#!/usr/bin/env python3
"""f-string slots: nested quotes and brackets.

A slot of a single-line f-string may hold any expression, including
strings delimited by the f-string's own quote and commas, brackets or
``#`` inside them (``f"{d["k"]}"``, ``f"{",".join(xs)}"``). The grammar
lexes an f-string as one token ending at the first unescaped delimiter,
and several preprocessing passes scan quotes naively, so before parsing
:func:`protect_fstring_quotes` replaces every delimiter quote *inside a
slot* with a :data:`PLACEHOLDERS` character (one for one, so lines and
columns do not move). :func:`split_fstring` decodes it back and splits the
literal body into text and slots, honouring nested strings and brackets:
a slot ends at the ``}`` closing it at bracket depth 0 outside strings,
and its format spec starts at the first ``:`` at depth 0 outside strings.
The escaped-delimiter form (``f"{d[\\"k\\"]}"``) keeps working.
:func:`parse_slot` parses a slot expression (with the statement
preprocessing when a lambda in it has a block body) and
:func:`fstring_spec_to_go` maps a format spec to a Go ``fmt`` verb.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

#: Stand-ins for a delimiter quote inside an f-string slot (private use
#: characters), one per quote kind so the text can be restored as is.
PLACEHOLDERS = {'"': "\ue000", "'": "\ue001"}

#: Follows the ``\\n`` escape of each line break of a multi-line f-string
#: collapsed onto one line before parsing (``_collapse_multiline_strings``),
#: so the slots' source lines can be found again. :func:`split_fstring`
#: drops it from the text and the slots; it never reaches the output.
SOURCE_NEWLINE = "\ue00a"

_PREFIX_LETTERS = set("rRbBuUfF")
_OPEN = "([{"
_CLOSE = ")]}"


@dataclass(frozen=True)
class Slot:
    expr: str          # the expression, quotes decoded
    spec: str          # text after the top-level ``:`` ("" when none)
    has_spec: bool     # a top-level ``:`` was present
    start: int         # offset of the expression in the body
    end: int           # offset of the closing ``}`` in the body


Part = Union[str, Slot]


# ─── Protection (before parsing) ──────────────────────────────


def _is_word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _string_prefix(src: str, i: int) -> str:
    """Letters prefixing the quote at ``i`` (``f``, ``rb``, ...)."""
    j = i
    while j > 0 and i - j < 2 and src[j - 1] in _PREFIX_LETTERS:
        j -= 1
    if j > 0 and _is_word(src[j - 1]):
        return ""  # part of an identifier (``elif"`` cannot happen; ``xf"``)
    return src[j:i]


def _skip_simple_string(src: str, i: int, q: str) -> int:
    """Index after the string whose quote ``q`` is at ``i`` (same line)."""
    j = i + 1
    while j < len(src):
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == q:
            return j + 1
        if ch == "\n":
            return j
        j += 1
    return j


def _skip_triple_string(src: str, i: int, q3: str) -> int:
    j = i + 3
    while j < len(src):
        if src[j] == "\\":
            j += 2
            continue
        if src.startswith(q3, j):
            return j + 3
        j += 1
    return j


def _protect_fstring(src: str, i: int, q: str) -> Optional[Tuple[str, int]]:
    """``(protected text, end)`` for the single-line f-string whose opening
    quote ``q`` is at ``i``; ``None`` when it is malformed."""
    out = [q]
    j = i + 1
    n = len(src)
    PLACEHOLDER = PLACEHOLDERS[q]
    while j < n:
        ch = src[j]
        if ch == "\n":
            return None
        if ch == "\\":
            out.append(src[j:j + 2])
            j += 2
            continue
        if ch == q:
            out.append(q)
            return "".join(out), j + 1
        if ch == "{" and src.startswith("{{", j):
            out.append("{{")
            j += 2
            continue
        if ch != "{":
            out.append(ch)
            j += 1
            continue
        # A slot: copy to its closing brace, protecting delimiter quotes.
        out.append("{")
        j += 1
        depth = 0
        while True:
            if j >= n or src[j] == "\n":
                return None
            ch = src[j]
            if ch == "\\" and j + 1 < n and src[j + 1] == q:
                # ``\"...\"``: a nested string in the escaped form.
                end = src.find("\\" + q, j + 2)
                if end < 0 or "\n" in src[j:end]:
                    return None
                out.append(src[j:end + 2])
                j = end + 2
                continue
            if ch in "\"'":
                k = j + 1
                piece = [PLACEHOLDER if ch == q else ch]
                while True:
                    if k >= n or src[k] == "\n":
                        return None
                    c = src[k]
                    if c == "\\" and k + 1 < n:
                        piece.append(src[k:k + 2])
                        k += 2
                        continue
                    if c == ch:
                        piece.append(PLACEHOLDER if ch == q else ch)
                        k += 1
                        break
                    piece.append(PLACEHOLDER if c == q else c)
                    k += 1
                out.append("".join(piece))
                j = k
                continue
            if ch in _OPEN:
                depth += 1
            elif ch in _CLOSE:
                if ch == "}" and depth == 0:
                    out.append("}")
                    j += 1
                    break
                depth -= 1
            out.append(ch)
            j += 1
    return None


def protect_fstring_quotes(src: str) -> str:
    """Replace delimiter quotes inside single-line f-string slots with
    :data:`PLACEHOLDERS` characters (see the module docstring)."""
    if "{" not in src or ("f\"" not in src and "f'" not in src and "F\"" not in src and "F'" not in src):
        return src
    out: List[str] = []
    i = 0
    n = len(src)
    last = 0
    while i < n:
        ch = src[i]
        if ch == "#":
            nl = src.find("\n", i)
            i = n if nl < 0 else nl
            continue
        if ch not in "\"'":
            i += 1
            continue
        if src.startswith(ch * 3, i):
            i = _skip_triple_string(src, i, ch * 3)
            continue
        prefix = _string_prefix(src, i)
        if "f" not in prefix.lower():
            i = _skip_simple_string(src, i, ch)
            continue
        protected = _protect_fstring(src, i, ch)
        if protected is None:
            i = _skip_simple_string(src, i, ch)
            continue
        text, end = protected
        if text != src[i:end]:
            out.append(src[last:i])
            out.append(text)
            last = end
        i = end
    if not out:
        return src
    out.append(src[last:])
    return "".join(out)


# ─── Splitting (after parsing) ────────────────────────────────


def fstring_body(raw: str) -> Optional[Tuple[str, str, int]]:
    """``(body, quote, prefix length)`` of an f-string token's text."""
    lowered = raw[:4].lower()
    if lowered.startswith(('f"""', "f'''")):
        return raw[4:-3], raw[1], 4
    if lowered.startswith(('f"', "f'")):
        return raw[2:-1], raw[1], 2
    return None


def _scan_slot(body: str, i: int, q: str) -> Optional[Tuple[str, str, bool, int]]:
    """Scan the slot whose ``{`` is at ``i - 1``; ``(expr, spec, has_spec,
    index of the closing brace)``. Nested strings may use either quote, the
    delimiter written plainly (after :func:`protect_fstring_quotes`, or in a
    triple-quoted f-string) or escaped (``\\"``)."""
    n = len(body)
    depth = 0
    j = i
    expr: List[str] = []
    colon = -1
    while j < n:
        ch = body[j]
        if ch == "\\" and j + 1 < n and body[j + 1] == q:
            # Escaped-delimiter nested string: ``\"k\"`` is ``"k"``.
            end = body.find("\\" + q, j + 2)
            if end < 0:
                return None
            expr.append(q + body[j + 2:end] + q)
            j = end + 2
            continue
        if ch in "\"'":
            k = j + 1
            piece = [ch]
            while k < n and body[k] != ch:
                if body[k] == "\\" and k + 1 < n:
                    piece.append(body[k:k + 2])
                    k += 2
                    continue
                piece.append(body[k])
                k += 1
            if k >= n:
                return None
            piece.append(ch)
            expr.append("".join(piece))
            j = k + 1
            continue
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            if depth == 0:
                if ch != "}":
                    return None
                text = "".join(expr)
                if colon >= 0:
                    return text[:colon], text[colon + 1:], True, j
                return text, "", False, j
            depth -= 1
        elif ch == ":" and depth == 0 and colon < 0:
            # ``a := b`` and ``!=`` are not specs; ``::`` neither.
            nxt = body[j + 1] if j + 1 < n else ""
            if nxt != "=":
                colon = len("".join(expr))
        expr.append(ch)
        j += 1
    return None


def split_fstring(body: str, q: str) -> Optional[List[Part]]:
    """Literal text and :class:`Slot` parts of an f-string body (``{{`` /
    ``}}`` decoded); ``None`` when a slot does not close."""
    body = restore_fstring_quotes(body)
    parts: List[Part] = []
    text: List[str] = []
    i = 0
    n = len(body)
    while i < n:
        ch = body[i]
        if ch == "{":
            if body.startswith("{{", i):
                text.append("{")
                i += 2
                continue
            scanned = _scan_slot(body, i + 1, q)
            if scanned is None:
                return None
            expr, spec, has_spec, end = scanned
            if text:
                parts.append("".join(text).replace(SOURCE_NEWLINE, ""))
                text = []
            # A line break inside a slot of a multi-line f-string is white
            # space of the expression.
            expr = expr.replace("\\n" + SOURCE_NEWLINE, " ").replace(SOURCE_NEWLINE, "")
            spec = spec.replace(SOURCE_NEWLINE, "")
            parts.append(Slot(expr, spec, has_spec, i + 1, end))
            i = end + 1
            continue
        if ch == "}" and body.startswith("}}", i):
            text.append("}")
            i += 2
            continue
        text.append(ch)
        i += 1
    if text:
        parts.append("".join(text).replace(SOURCE_NEWLINE, ""))
    return parts


def fstring_source_position(body: str, offset: int, line: int, column: int, prefix: int) -> tuple[int, int]:
    """(line, column) in the source of ``offset`` in the body of an
    f-string token starting at ``line`` / ``column``: line breaks are real
    ones (a token spanning lines) or :data:`SOURCE_NEWLINE` marks (a
    multi-line f-string collapsed before parsing)."""
    before = body[:offset]
    breaks = before.count("\n") + before.count(SOURCE_NEWLINE)
    if not breaks:
        return line, column + prefix + offset
    last = max(before.rfind("\n"), before.rfind(SOURCE_NEWLINE))
    return line + breaks, offset - last


def parse_slot(prefix: str, expr: str):
    """Parse the slot expression ``expr`` wrapped as ``{prefix}{expr});``
    (``prefix`` opens a parenthesised assignment) into a parse tree, or
    ``None``. A slot is parsed as written first; when that fails (a lambda
    with a block body, ``lambda (x: int) -> bool { return x > 0 }``, needs
    the ``;`` before its ``}``) the wrapped text goes through the statement
    preprocessing every source line gets before parsing."""
    from compiler.lammergeier import create_parser, preprocess_for_parse

    parser = create_parser()
    try:
        return parser.parse(f"{prefix}{expr});\n")
    except Exception:
        pass
    if "{" not in expr:
        return None
    try:
        text = preprocess_for_parse(f"{prefix}{expr})\n").source
        return parser.parse(text if text.endswith("\n") else text + "\n")
    except Exception:
        return None


# ─── Format specs ─────────────────────────────────────────────

#: A spec that already is a Go ``fmt`` verb (``.2f``, ``5d``, ``-8s``,
#: ``+d``, ``#x``, ``08.3f``): used as written.
_GO_SPEC_RE = re.compile(r"[+\-# 0]*\d*(?:\.\d+)?[bcdeEfFgGoOqstTUvxX]")

#: Python's format-spec mini-language: ``[[fill]align][sign][z][#][0]
#: [width][grouping][.precision][type]``.
_PY_SPEC_RE = re.compile(
    r"(?:(?P<fill>.)?(?P<align>[<>^=]))?(?P<sign>[+\- ])?(?P<z>z)?(?P<alt>#)?"
    r"(?P<zero>0)?(?P<width>\d+)?(?P<group>[,_])?(?:\.(?P<prec>\d+))?(?P<type>[a-zA-Z%])?",
    re.S,
)

_PY_TYPES_TO_GO = {t: t for t in "bcdeEfFgGosxX"}


def fstring_spec_to_go(spec: str) -> Tuple[Optional[str], str]:
    """``(Go verb, "")`` for the format spec of an f-string slot (the text
    after its top-level ``:``), or ``(None, reason)`` when Go's ``fmt`` has
    no equivalent. A Go verb (``.2f``, ``5d``, ``-8s``) is used as written;
    Python's alignment forms map to Go flags: ``>5`` is ``%5v``, ``<6`` is
    ``%-6v``, ``>8.2f`` is ``%8.2f``, an empty spec is ``%v``. Centering,
    a fill other than a space, ``=`` alignment, digit grouping, ``z``, ``n``
    and ``%`` have no verb."""
    if spec == "":
        return "%v", ""
    if _GO_SPEC_RE.fullmatch(spec):
        return f"%{spec}", ""
    m = _PY_SPEC_RE.fullmatch(spec)
    if m is None:
        return None, "it is neither a Go `fmt` verb nor a Python format spec"
    if m.group("align") == "^":
        width = m.group("width") or "n"
        return None, f"centering has no Go verb; use `.center({width})` on the `str` value"
    if m.group("align") == "=":
        return None, "`=` alignment has no Go verb; use `0` padding (`05d`)"
    if m.group("fill") not in (None, " "):
        return None, "only a space fill is supported (`>6`, `<6`); use `05d` for zero padding"
    if m.group("group"):
        return None, "digit grouping has no Go verb; use `Fmt.grouped(value)` from `lamfmt`"
    if m.group("z"):
        return None, "`z` has no Go verb"
    kind = m.group("type") or "v"
    if kind != "v" and kind not in _PY_TYPES_TO_GO:
        return None, f"type `{kind}` has no Go verb"
    flags = ""
    if m.group("align") == "<":
        flags += "-"
    sign = m.group("sign")
    if sign in ("+", " "):
        flags += sign
    if m.group("alt"):
        flags += "#"
    if m.group("zero") and m.group("align") != "<":
        flags += "0"
    width = m.group("width") or ""
    prec = f".{m.group('prec')}" if m.group("prec") is not None else ""
    return f"%{flags}{width}{prec}{_PY_TYPES_TO_GO.get(kind, kind)}", ""


def restore_fstring_quotes(text: str) -> str:
    """Undo :func:`protect_fstring_quotes`."""
    for quote, placeholder in PLACEHOLDERS.items():
        if placeholder in text:
            text = text.replace(placeholder, quote)
    return text


def decode_placeholders(text: str, q: str = "") -> str:
    return restore_fstring_quotes(text)
