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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple, Union

#: Stand-ins for a delimiter quote inside an f-string slot (private use
#: characters), one per quote kind so the text can be restored as is.
PLACEHOLDERS = {'"': "\ue000", "'": "\ue001"}

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
                parts.append("".join(text))
                text = []
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
        parts.append("".join(text))
    return parts


def restore_fstring_quotes(text: str) -> str:
    """Undo :func:`protect_fstring_quotes`."""
    for quote, placeholder in PLACEHOLDERS.items():
        if placeholder in text:
            text = text.replace(placeholder, quote)
    return text


def decode_placeholders(text: str, q: str = "") -> str:
    return restore_fstring_quotes(text)
