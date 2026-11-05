"""Markup literals: ``<>...</>`` with ``${expr}`` slots.

The parser never sees markup. Before parsing, every literal in Lam code is
rewritten into a call to the reserved marker::

    <><p class="x">Hi ${name}</p></>
    ->  __markup__("<p class=\\"x\\">Hi \\ue0000\\ue001</p>", (name))

The first argument is the literal's static text with each slot replaced by
``U+E000 <index> U+E001``; the following arguments are the slot
expressions (Lam code, whose own literals are rewritten the same way). The
semantic checker and the transpiler give the call its kind (``html`` by
default, ``xml`` from the context), check the slots and lower it to the
runtime in ``lib/lammarkup.lam``. A rewritten literal keeps the source's
lines: the slot arguments are moved to the line and column of their
``${`` (line breaks and spaces inside the call's parentheses), so
diagnostics in a slot, a nested literal or the code after a multi-line
literal point at the source.
"""

from __future__ import annotations

import bisect
import re
import xml.dom.minidom
import xml.parsers.expat
from dataclasses import dataclass

MARKER = "__markup__"
SLOT_OPEN = "\ue000"
SLOT_CLOSE = "\ue001"
_SLOT_RE = re.compile("\ue000(\\d+)\ue001")

#: Slot contexts reported by :func:`slot_contexts`.
TEXT = "text"
ATTRIBUTE = "attribute"
RAW = "raw"  # <script> / <style> content (html only)
INVALID = "invalid"

_RAW_TEXT = {"script", "style"}
_ESCAPABLE_RAW = {"textarea", "title"}


@dataclass(frozen=True)
class MarkupDiagnostic:
    line: int
    column: int
    message: str


class _Unterminated(Exception):
    def __init__(self, offset: int, message: str):
        super().__init__(message)
        self.offset = offset
        self.message = message


def _lam_string_literal(text: str) -> str:
    out = ['"']
    for ch in text:
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _skip_string(src: str, i: int) -> int:
    """``src[i]`` opens a Lam string (``"``, ``'``, triple-quoted, with an
    optional ``f``/``r``/``b``/``u`` prefix already consumed); returns the
    index after it (or ``len(src)`` when unterminated)."""
    quote = src[i]
    triple = src.startswith(quote * 3, i)
    if triple:
        end = i + 3
        while end < len(src):
            if src[end] == "\\":
                end += 2
                continue
            if src.startswith(quote * 3, end):
                return end + 3
            end += 1
        return len(src)
    end = i + 1
    while end < len(src):
        ch = src[end]
        if ch == "\\":
            end += 2
            continue
        if ch == quote:
            return end + 1
        if ch == "\n":
            return end
        end += 1
    return len(src)


def _skip_go_block(src: str, i: int) -> int:
    """``src[i:]`` starts with ``go!``: skip a ``go! { ... }`` / ``go!(...)``
    region (Go strings, runes and comments respected)."""
    j = i + 3
    while j < len(src) and src[j] in " \t":
        j += 1
    if j >= len(src) or src[j] not in "{(":
        return i + 3
    open_ch = src[j]
    close_ch = "}" if open_ch == "{" else ")"
    depth = 0
    k = j
    while k < len(src):
        ch = src[k]
        if ch in "\"'`":
            if ch == "`":
                end = src.find("`", k + 1)
                k = len(src) if end < 0 else end + 1
                continue
            k = _skip_string(src, k)
            continue
        if src.startswith("//", k):
            end = src.find("\n", k)
            k = len(src) if end < 0 else end
            continue
        if src.startswith("/*", k):
            end = src.find("*/", k + 2)
            k = len(src) if end < 0 else end + 2
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return k + 1
        k += 1
    return len(src)


def _string_prefix_start(src: str, i: int) -> bool:
    """``src[i]`` is a quote: is it a string start (not part of a name)?"""
    j = i - 1
    while j >= 0 and src[j] in "fFrRbBuU":
        j -= 1
    return j < 0 or not (src[j].isalnum() or src[j] == "_")


class _Scanner:
    """Rewrites markup literals into marker calls that keep every slot at
    its line and column (see :func:`preprocess_markup_literals`)."""

    def __init__(self, src: str):
        self.src = src
        # (start, end) of every outermost literal, for the formatter.
        self.spans: list[tuple[int, int]] = []
        self._line_starts = [0] + [m.end() for m in re.finditer("\n", src)]

    def _position(self, offset: int) -> tuple[int, int]:
        """(0-based line, 0-based column) of ``offset`` in the source."""
        line = bisect.bisect_right(self._line_starts, offset) - 1
        return line, offset - self._line_starts[line]

    # ── Lam code ────────────────────────────────────────────────────────

    def code(self, start: int, stop_at_brace: bool, column: int = 0) -> tuple[str, int]:
        """Rewrites Lam code from ``start``, whose output begins at
        ``column``; with ``stop_at_brace`` stops at the ``}`` closing a
        ``${`` slot. Returns (rewritten, index after). Line breaks are kept
        one for one, so the output has the source's lines."""
        src = self.src
        out: list[str] = []
        depth = 0
        i = start

        def emit(text: str) -> None:
            nonlocal column
            out.append(text)
            newline = text.rfind("\n")
            column = len(text) - newline - 1 if newline >= 0 else column + len(text)

        while i < len(src):
            ch = src[i]
            if ch in "\"'" and _string_prefix_start(src, i):
                end = _skip_string(src, i)
                emit(src[i:end])
                i = end
                continue
            if ch == "#":
                if src.startswith("#-", i):
                    end = src.find("-#", i + 2)
                    end = len(src) if end < 0 else end + 2
                else:
                    end = src.find("\n", i)
                    end = len(src) if end < 0 else end
                if stop_at_brace:
                    # A comment cannot stay inside the marker call's
                    # arguments; its line breaks do.
                    emit("\n" * src.count("\n", i, end) or " ")
                else:
                    emit(src[i:end])
                i = end
                continue
            if src.startswith("go!", i) and (i == 0 or not (src[i - 1].isalnum() or src[i - 1] == "_")):
                end = _skip_go_block(src, i)
                emit(src[i:end])
                i = end
                continue
            if src.startswith("<>", i):
                call, end = self.literal(i, column)
                emit(call)
                if not stop_at_brace:
                    self.spans.append((i, end))
                i = end
                continue
            if stop_at_brace:
                if ch in "{([":
                    depth += 1
                elif ch in ")]":
                    depth -= 1
                elif ch == "}":
                    if depth == 0:
                        return "".join(out), i + 1
                    depth -= 1
            emit(ch)
            i += 1
        if stop_at_brace:
            raise _Unterminated(start, "unterminated `${...}` in markup literal")
        return "".join(out), i

    # ── Markup ──────────────────────────────────────────────────────────

    def literal(self, start: int, column: int = 0) -> tuple[str, int]:
        """``src[start:]`` starts with ``<>`` and its call begins at output
        ``column``. Returns (marker call, index after ``</>``).

        The call is ``__markup__("template"`` on the literal's line, then
        each slot as ``,(code)`` with the ``,`` where the slot's ``$`` was
        (line breaks and spaces inside the call's parentheses move it
        there; on the literal's first line the template may push it to the
        right), then ``)`` where the literal's last ``>`` was: the slot
        expressions, and the code after the literal, keep their lines and
        (but for that first line) their columns."""
        src = self.src
        i = start + 2
        static: list[str] = []
        slots: list[tuple[int, str]] = []
        while True:
            if i >= len(src):
                raise _Unterminated(start, "unterminated markup literal: `<>` has no matching `</>`")
            if src.startswith("</>", i):
                end = i + 3
                break
            ch = src[i]
            if ch == "\\" and src.startswith("\\$", i):
                static.append("$")
                i += 2
                continue
            if src.startswith("${", i):
                code, after = self.code(i + 2, stop_at_brace=True, column=self._position(i + 2)[1])
                static.append(f"{SLOT_OPEN}{len(slots)}{SLOT_CLOSE}")
                slots.append((i, code))
                i = after
                continue
            static.append(ch)
            i += 1
        template = _trim_literal_edges("".join(static))
        out: list[str] = [f"{MARKER}({_lam_string_literal(template)}"]
        line = self._position(start)[0]
        column += len(out[0])

        def move_to(offset: int) -> None:
            nonlocal line, column
            target_line, target_column = self._position(offset)
            if target_line > line:
                out.append("\n" * (target_line - line))
                line, column = target_line, 0
            if column < target_column:
                out.append(" " * (target_column - column))
                column = target_column

        def emit(text: str) -> None:
            nonlocal line, column
            out.append(text)
            breaks = text.count("\n")
            if breaks:
                line += breaks
                column = len(text) - text.rfind("\n") - 1
            else:
                column += len(text)

        for dollar, code in slots:
            move_to(dollar)
            if not code.strip():
                code = "None" + "\n" * code.count("\n")
            emit(f",({code})")
        move_to(end - 1)
        emit(")")
        return "".join(out), end


def _trim_literal_edges(text: str) -> str:
    """Whitespace that contains a newline right after ``<>`` / before
    ``</>`` is layout, not content."""
    stripped = text.lstrip()
    if len(stripped) != len(text) and "\n" in text[: len(text) - len(stripped)]:
        text = stripped
    stripped = text.rstrip()
    if len(stripped) != len(text) and "\n" in text[len(stripped):]:
        text = stripped
    return text


def preprocess_markup_literals(source: str) -> tuple[str, list[MarkupDiagnostic]]:
    """Rewrites every ``<>...</>`` literal of ``source`` into a marker call.
    An unterminated literal is left untouched (the parser then reports it)
    and described in the returned diagnostics."""
    if "<>" not in source:
        return source, []
    scanner = _Scanner(source)
    try:
        rewritten, _ = scanner.code(0, stop_at_brace=False)
    except _Unterminated as exc:
        line = source.count("\n", 0, exc.offset) + 1
        column = exc.offset - (source.rfind("\n", 0, exc.offset) + 1) + 1
        return source, [MarkupDiagnostic(line, column, exc.message)]
    return rewritten, []


def markup_literal_spans(source: str) -> list[tuple[int, int]]:
    """(start, end) offsets of the outermost markup literals of ``source``
    (empty when a literal is unterminated)."""
    if "<>" not in source:
        return []
    scanner = _Scanner(source)
    try:
        scanner.code(0, stop_at_brace=False)
    except _Unterminated:
        return []
    return scanner.spans


# ── Templates (used by the semantic checker) ─────────────────────────────


def template_slots(template: str) -> list[int]:
    return [int(m.group(1)) for m in _SLOT_RE.finditer(template)]


def slot_contexts(template: str, xml_mode: bool) -> dict[int, str]:
    """Where each slot of ``template`` lands: element content, an attribute
    value, raw ``<script>``/``<style>`` text, or a place where a value
    cannot go (tag or attribute names, comments, doctype, instructions)."""
    contexts: dict[int, str] = {}
    i = 0
    n = len(template)

    def mark(segment: str, context: str) -> None:
        for m in _SLOT_RE.finditer(segment):
            contexts[int(m.group(1))] = context

    while i < n:
        lt = template.find("<", i)
        if lt < 0:
            mark(template[i:], TEXT)
            break
        mark(template[i:lt], TEXT)
        i = lt
        rest = template[i:]
        if rest.startswith("<!--"):
            end = template.find("-->", i + 4)
            end = n if end < 0 else end + 3
            mark(template[i:end], INVALID)
            i = end
            continue
        if rest.startswith("<![CDATA[") and xml_mode:
            end = template.find("]]>", i)
            end = n if end < 0 else end + 3
            mark(template[i:end], TEXT)
            i = end
            continue
        if rest.startswith("<!") or rest.startswith("<?"):
            end = template.find(">", i)
            end = n if end < 0 else end + 1
            mark(template[i:end], INVALID)
            i = end
            continue
        if rest.startswith("</"):
            end = template.find(">", i)
            end = n if end < 0 else end + 1
            mark(template[i:end], INVALID)
            i = end
            continue
        if len(rest) > 1 and (rest[1].isalpha() or rest[1] == SLOT_OPEN):
            # Start tag: name, then attributes.
            j = i + 1
            while j < n and template[j] not in " \t\r\n/>":
                j += 1
            name = template[i + 1:j]
            mark(name, INVALID)
            while j < n:
                while j < n and template[j] in " \t\r\n":
                    j += 1
                if j >= n:
                    break
                if template[j] == ">":
                    j += 1
                    break
                if template.startswith("/>", j):
                    j += 2
                    break
                if template[j] == "/":
                    j += 1
                    continue
                k = j
                while k < n and template[k] not in " \t\r\n=>" and not template.startswith("/>", k):
                    k += 1
                mark(template[j:k], INVALID)
                j = k
                while j < n and template[j] in " \t\r\n":
                    j += 1
                if j < n and template[j] == "=":
                    j += 1
                    while j < n and template[j] in " \t\r\n":
                        j += 1
                    if j < n and template[j] in "\"'":
                        q = template[j]
                        end = template.find(q, j + 1)
                        end = n if end < 0 else end
                        mark(template[j + 1:end], ATTRIBUTE)
                        j = end + 1
                    else:
                        k = j
                        while k < n and template[k] not in " \t\r\n>":
                            k += 1
                        mark(template[j:k], ATTRIBUTE)
                        j = k
            i = j
            lname = name.lower()
            if not xml_mode and (lname in _RAW_TEXT or lname in _ESCAPABLE_RAW) and not template[:j].rstrip().endswith("/>"):
                end = template.lower().find("</" + lname, i)
                end = n if end < 0 else end
                mark(template[i:end], RAW if lname in _RAW_TEXT else TEXT)
                i = end
            continue
        mark(template[i:i + 1], TEXT)
        i += 1
    return contexts


def xml_template_error(template: str) -> str | None:
    """Why the static text of an xml literal is not well-formed XML (slots
    read as text), or None."""
    text = _SLOT_RE.sub("x", template)
    prolog = ""
    body = text
    m = re.match(r"\s*(<\?xml[^?]*\?>)", body)
    if m:
        prolog = m.group(1)
        body = body[m.end():]
    if re.search(r"<\?xml[\s?]", body):
        return "the XML declaration `<?xml ...?>` must come first"
    # Fragments may hold several roots (or none): wrap them, keeping any
    # DOCTYPE outside the wrapper.
    doctype = ""
    dm = re.match(r"\s*(<!DOCTYPE[^>]*>)", body, re.IGNORECASE)
    if dm:
        doctype = dm.group(1)
        body = body[dm.end():]
    declaration = prolog or '<?xml version="1.0"?>'
    document = f"{declaration}{doctype}<lamMarkupRoot>{body}</lamMarkupRoot>"
    try:
        xml.dom.minidom.parseString(document.encode("utf-8"))
    except xml.parsers.expat.ExpatError as exc:
        message = str(exc)
        message = re.sub(r": line \d+, column \d+$", "", message)
        if "lamMarkupRoot" in message or message.startswith("mismatched tag"):
            return "mismatched tag"
        return message
    return None


# ── DOM-style methods of html / xml values ───────────────────────────────
#
# name -> (parameter kinds, return type). Parameter kinds: "str", or
# "content" (markup of the receiver's kind, str or a list of them). Return
# types use "T" for the receiver's own type. The Go method is the name
# with its first letter in upper case (``select`` -> ``Select``).
MARKUP_METHODS: dict[str, tuple[tuple[str, ...], str]] = {
    "select": (("str",), "list[T]"),
    "first": (("str",), "T"),
    "text": ((), "str"),
    "tag": ((), "str"),
    "attr": (("str",), "str"),
    "hasAttr": (("str",), "bool"),
    "attrs": ((), "dict[str, str]"),
    "hasClass": (("str",), "bool"),
    "setAttr": (("str", "str"), "None"),
    "removeAttr": (("str",), "None"),
    "addClass": (("str",), "None"),
    "removeClass": (("str",), "None"),
    "toggleClass": (("str",), "None"),
    "setText": (("str",), "None"),
    "setInner": (("content",), "None"),
    "append": (("content",), "None"),
    "prepend": (("content",), "None"),
    "replaceWith": (("content",), "None"),
    "remove": ((), "None"),
    "children": ((), "list[T]"),
    "nodes": ((), "list[T]"),
    "parent": ((), "T"),
    "clone": ((), "T"),
    "isEmpty": ((), "bool"),
}

MARKUP_GO_TYPES = {"html": "LamHTML", "xml": "LamXML"}


def markup_method_go_name(method: str) -> str:
    return method[:1].upper() + method[1:]


def markup_method_return(method: str, kind: str) -> str:
    return MARKUP_METHODS[method][1].replace("T", kind)


def markup_method_return_go(method: str, kind: str) -> str:
    ret = MARKUP_METHODS[method][1]
    go_kind = MARKUP_GO_TYPES[kind]
    return {
        "list[T]": f"[]{go_kind}", "T": go_kind, "str": "string", "bool": "bool",
        "dict[str, str]": "map[string]string", "None": "",
    }[ret]


def decode_lam_string_literal(text: str) -> str:
    """The value of a Lam string literal written by
    :func:`_lam_string_literal` (``"..."`` with ``\\\\ \\" \\n \\r \\t``)."""
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"', "'": "'"}.get(nxt, "\\" + nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)
