from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class FormatResult:
    text: str
    changed: bool


_BINARY_OPS = (
    "**=", "//=", "<<=", ">>=", "==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "%=",
    "&=", "|=", "^=", "->", "=>", "??", "&&", "||", "<<", ">>", "**", "//",
    "=", "+", "-", "*", "/", "%", "<", ">", "&", "|", "^",
)

class FormatError(Exception):
    pass


def format_lam_source(source: str) -> FormatResult:
    from compiler.lammergeier import _canonicalize_doc_comment_terminators
    from compiler.fstrings import protect_fstring_quotes, restore_fstring_quotes

    reparable = _canonicalize_doc_comment_terminators(source)
    _parse_or_raise(reparable)
    # Markup literals are kept exactly as written (their text is HTML/XML,
    # not Lam code): each becomes a placeholder while the lines are laid out.
    protected, literals = _protect_markup_literals(reparable)
    # f-string slots may hold the delimiter quote (``f"{d["k"]}"``): hide it
    # so the line scanners below see one string literal.
    formatted = restore_fstring_quotes(_format_lines(protect_fstring_quotes(protected)))
    for index, literal in enumerate(literals):
        formatted = formatted.replace(_markup_placeholder(index), literal, 1)
    _parse_or_raise(formatted)
    return FormatResult(text=formatted, changed=formatted != source)


def _markup_placeholder(index: int) -> str:
    return f"__lamFormatMarkup{index}__"


def _protect_markup_literals(source: str) -> tuple[str, list[str]]:
    from compiler.markup import markup_literal_spans

    spans = markup_literal_spans(source)
    if not spans:
        return source, []
    out: list[str] = []
    literals: list[str] = []
    cursor = 0
    for start, end in spans:
        out.append(source[cursor:start])
        out.append(_markup_placeholder(len(literals)))
        literals.append(source[start:end])
        cursor = end
    out.append(source[cursor:])
    return "".join(out), literals


def _parse_or_raise(source: str) -> None:
    from compiler.lammergeier import create_parser, preprocess_for_parse

    text = preprocess_for_parse(source).source
    if not text.endswith("\n"):
        text += "\n"
    try:
        create_parser().parse(text)
    except Exception as e:
        raise FormatError(str(e)) from e


def _format_lines(source: str) -> str:
    lines = _expand_compact_layout(source.splitlines())
    out: list[str] = []
    # One entry per open bracket (``(``, ``[``, ``{``): the index of the line
    # that opened it. A line is indented once per *line* holding open
    # brackets, so ``take(\n    {...},\n)`` and ``print(f(\n    1\n))``
    # indent their continuation lines one level.
    open_lines: list[int] = []
    # The bracket characters still open at the start of the line, so a
    # continuation line knows it is inside a call (``reverse=true,``).
    open_chars: list[str] = []
    prev_code: int | None = None  # the last code line laid out
    prev_dot_extra: int | None = None  # its extra level when it continued a chain
    i = 0

    def indent_level(text: str) -> int:
        closers = _leading_closing_brackets(text)
        if closers and closers <= len(open_lines):
            # A line starting with closing brackets aligns with the line
            # that opened the outermost of them, even when that line has
            # other brackets still open (``}, 0)`` closing the body of a
            # lambda opened by ``reduce(lambda ... {``, ``], [``).
            first = open_lines[-closers]
            return len({line for line in open_lines[:-closers] if line != first})
        return len(set(open_lines))

    def track(text: str, line_id: int) -> None:
        for ch in _bracket_chars(text):
            if ch in "([{":
                open_lines.append(line_id)
                open_chars.append(ch)
            elif open_lines:
                open_lines.pop()
                open_chars.pop()

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            i += 1
            continue

        if stripped.startswith("#-"):
            block, next_i = _collect_doc_comment(lines, i)
            out.extend(block)
            i = next_i
            continue

        if _starts_go_block(stripped):
            block, next_i = _collect_go_block(lines, i)
            out.extend(_format_go_block(block, indent_level(stripped)))
            i = next_i
            continue

        if "\n" in stripped:
            # Multi-line string statement (``_expand_compact_layout``): only
            # the first line is re-indented, the string text is kept.
            masked = _mask_triple_strings(stripped)
            first, rest = stripped.split("\n", 1)
            out.append("    " * indent_level(masked) + first.strip() + "\n" + rest.rstrip())
            track(masked, i)
            i += 1
            continue

        line_depth = indent_level(stripped)
        dot_extra = None
        if stripped.startswith(".") and not stripped.startswith("..") and prev_code is not None:
            # ``xs\n.map(f)\n.filter(g)``: a method chain continues the
            # statement one level in (unless the line before the chain
            # left a bracket open, which already indents it).
            dot_extra = prev_dot_extra if prev_dot_extra is not None else int(prev_code not in open_lines)
            line_depth += dot_extra
        formatted = _format_code_line(stripped, open_chars[-1] if open_chars else "")
        if formatted == "}" and out and out[-1] == "":
            out.pop()
        if formatted in {"else {", "elif {", "finally {", "catch {"} and out and out[-1].strip() == "}":
            out[-1] = out[-1] + " " + formatted[:-2].strip()
            out[-1] = out[-1] + " {"
            track(stripped, i)
            i += 1
            continue
        out.append("    " * line_depth + formatted)
        track(stripped, i)
        prev_code = i
        prev_dot_extra = dot_extra
        i += 1
    return "\n".join(out).rstrip() + "\n"


def _bracket_chars(line: str) -> str:
    """The brackets of ``line`` outside strings and comments, in order."""
    code, _comment = _split_inline_comment(line)
    quote = ""
    found: list[str] = []
    i = 0
    while i < len(code):
        if quote:
            if code[i] == "\\" and len(quote) == 1:
                i += 2
                continue
            if code.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
            i += 1
            continue
        if code.startswith("'''", i) or code.startswith('"""', i):
            quote = code[i:i + 3]
            i += 3
            continue
        if code[i] in {'"', "'"}:
            quote = code[i]
            i += 1
            continue
        if code[i] in "()[]{}":
            found.append(code[i])
        i += 1
    return "".join(found)


def _leading_closing_brackets(line: str) -> int:
    count = 0
    for ch in line:
        if ch in ")]}":
            count += 1
        elif ch.isspace():
            continue
        else:
            break
    return count


_LAYOUT_LITERAL_PRECEDERS = set("=,([:+-*/%<>!&|^~?")
_LAYOUT_LITERAL_KEYWORDS = frozenset({
    "return", "yield", "raise", "throw", "await", "in", "and", "or",
    "not", "is", "case",
})
_LAYOUT_LAMBDA_HEAD_RE = re.compile(
    r"\blambda\b(?:\s*\((?:[^()]|\([^)]*\))*\)|\s+\w+(?:\s*,\s*\w+)*)?"
    r"(?:\s*->\s*[^\s{][^{]*?)?\s*$"
)


def _open_triple_quote(line: str, state: str = "") -> str:
    """The triple quote (``\"\"\"`` / ``'''``) still open after ``line``,
    starting inside one when ``state`` is set; ``""`` otherwise."""
    i = 0
    quote = state
    while i < len(line):
        if quote:
            if len(quote) == 1 and line[i] == "\\":
                i += 2
                continue
            if line.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
            i += 1
            continue
        if line[i] == "#":
            break
        if line.startswith('"""', i) or line.startswith("'''", i):
            quote = line[i:i + 3]
            i += 3
            continue
        if line[i] in "\"'":
            quote = line[i]
        i += 1
    return quote if len(quote) == 3 else ""


def _mask_triple_strings(text: str) -> str:
    """``text`` with the content of triple-quoted strings removed."""
    return re.sub(r'("""|\'\'\')(.*?)\1', r"\1\1", text, flags=re.S)


def _expand_compact_layout(lines: list[str]) -> list[str]:
    expanded: list[str] = []
    layout_stack: list[str] = []
    i = 0
    while i < len(lines):
        if _open_triple_quote(lines[i]):
            # A statement holding a multi-line string stays verbatim (one
            # element with embedded newlines): its text is part of a value.
            group = [lines[i]]
            state = _open_triple_quote(lines[i])
            i += 1
            while i < len(lines) and state:
                group.append(lines[i])
                state = _open_triple_quote(lines[i], state)
                i += 1
            expanded.append("\n".join(group))
            continue
        stripped = lines[i].strip()
        if stripped.startswith("#-"):
            block, next_i = _collect_doc_comment(lines, i)
            expanded.extend(block)
            i = next_i
            continue
        if stripped and _starts_go_block(stripped):
            block, next_i = _collect_go_block(lines, i)
            expanded.extend(block)
            i = next_i
            continue
        # ``go!([]byte{})``: braces of inline raw Go are not Lam layout.
        protected, inline_go = _protect_inline_go(lines[i])
        expanded.extend(_restore_inline_go(part, inline_go) for part in _split_compact_line(protected, layout_stack))
        i += 1
    return expanded


_DESTRUCTURE_LINE_RE = re.compile(r"^\s*\{[ \t]*[A-Za-z_][^{}\n]*\}\s*=(?!=)")


def _split_compact_line(line: str, layout_stack: list[str]) -> list[str]:
    code, comment = _split_inline_comment(line)
    if not code.strip():
        return [line]
    if _DESTRUCTURE_LINE_RE.match(code):
        # ``{name, age} = user``: dictionary destructuring, not a block.
        return [line]
    out: list[str] = []
    buf: list[str] = []
    quote = ""
    prev_meaningful = _previous_layout_char(layout_stack)
    i = 0
    while i < len(code):
        ch = code[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and len(quote) == 1 and i + 1 < len(code):
                i += 1
                buf.append(code[i])
            elif code.startswith(quote, i):
                if len(quote) == 3:
                    buf.extend(code[i + 1:i + 3])
                    i += 2
                quote = ""
            i += 1
            continue
        if code.startswith("'''", i) or code.startswith('"""', i):
            quote = code[i:i + 3]
            buf.append(quote)
            i += 3
            prev_meaningful = '"'
            continue
        if ch in {'"', "'"}:
            quote = ch
            buf.append(ch)
            i += 1
            prev_meaningful = ch
            continue
        if ch in "([":
            layout_stack.append(ch)
        elif ch in ")]" and layout_stack and layout_stack[-1] in "([":
            layout_stack.pop()
        if ch == "{":
            kind = _layout_brace_kind(prev_meaningful, "".join(buf))
            layout_stack.append(kind)
            buf.append(ch)
            prev_meaningful = ch
            if kind in {"block", "lambda_block"}:
                _flush_layout_segment(out, buf)
            i += 1
            continue
        if ch == "}":
            while layout_stack and layout_stack[-1] in "([":
                layout_stack.pop()  # unbalanced ``(`` / ``[`` inside braces
            kind = layout_stack.pop() if layout_stack else "block"
            if kind in {"block", "lambda_block"} and "".join(buf).strip():
                _flush_layout_segment(out, buf)
            buf.append(ch)
            prev_meaningful = ch
            if kind in {"block", "lambda_block"}:
                # Keep ``else``/``catch`` on the same virtual line as
                # the closing brace so the normal line formatter can emit
                # Go-like ``} else {``, and what follows a lambda body in
                # its expression (``}), {"k": 1})``, ``}, 0)``).
                rest = code[i + 1:].lstrip()
                if not _starts_following_clause(rest) and not (
                    kind == "lambda_block" and rest[:1] in tuple(")]},.")
                ):
                    _flush_layout_segment(out, buf)
            i += 1
            continue
        if ch == ";" and not _inside_layout_expression(layout_stack):
            _flush_layout_segment(out, buf)
            prev_meaningful = ch
            i += 1
            continue
        buf.append(ch)
        if not ch.isspace():
            prev_meaningful = ch
        i += 1
    if comment:
        if "".join(buf).strip():
            buf.append("  " + comment.strip())
        else:
            buf.append(comment.strip())
    _flush_layout_segment(out, buf)
    return out or [""]


def _flush_layout_segment(out: list[str], buf: list[str]) -> None:
    segment = "".join(buf).strip()
    if segment:
        out.append(segment)
    buf.clear()


def _layout_brace_kind(prev_meaningful: str, prefix: str) -> str:
    if prev_meaningful in _LAYOUT_LITERAL_PRECEDERS:
        return "literal"
    last_word = _last_layout_word(prefix)
    if last_word in _LAYOUT_LITERAL_KEYWORDS:
        return "literal"
    if last_word == "else" and prefix.rstrip()[:-len("else")].strip() not in {"", "}"}:
        # ``x if c else {}``: the ``else`` of a conditional expression (a
        # statement ``else`` starts its line or follows a block's ``}``).
        return "literal"
    if _LAYOUT_LAMBDA_HEAD_RE.search(prefix):
        return "lambda_block"
    return "block"


def _last_layout_word(prefix: str) -> str:
    j = len(prefix) - 1
    while j >= 0 and prefix[j].isspace():
        j -= 1
    end = j + 1
    while j >= 0 and (prefix[j].isalnum() or prefix[j] == "_"):
        j -= 1
    return prefix[j + 1:end]


def _previous_layout_char(layout_stack: list[str]) -> str:
    """The meaningful character a line continues after: ``{`` inside a
    block, ``(`` inside brackets or a literal (a ``{`` starting the line
    there is a dict / set literal), nothing at statement level."""
    if not layout_stack:
        return ""
    return "{" if layout_stack[-1] in {"block", "lambda_block"} else "("


def _inside_layout_expression(layout_stack: list[str]) -> bool:
    return bool(layout_stack and layout_stack[-1] in {"literal", "(", "["})


def _starts_following_clause(text: str) -> bool:
    return any(text.startswith(keyword) for keyword in ("else", "elif", "catch", "finally"))


def _format_code_line(line: str, enclosing: str = "") -> str:
    code, comment = _split_inline_comment(line)
    protected, inline_go = _protect_inline_go(code.strip())
    code = _format_code_fragment(protected, enclosing)
    code = _restore_inline_go(code, inline_go)
    if comment:
        if code:
            return f"{code}  {comment.strip()}"
        return comment.strip()
    return code


def _format_code_fragment(code: str, enclosing: str = "") -> str:
    """``enclosing`` is the innermost bracket left open by the lines
    before (``(`` for the argument lines of a multi-line call)."""
    out: list[str] = []
    i = 0
    quote = ""
    # Open brackets of this line: ``(index in out, char)``; a ``:`` directly
    # inside ``[...]`` is a slice (``s[:3]``, ``x[0:7]``), not an annotation,
    # unless the ``[`` opens type parameters (``func f[T: ordered]``,
    # recorded as ``"<"``).
    brackets: list[tuple[int, str]] = []
    keyword_eq_end = -1  # ``len(out)`` right after a keyword argument's ``=``
    while i < len(code):
        if quote:
            out.append(code[i])
            if code[i] == "\\" and i + 1 < len(code):
                i += 1
                out.append(code[i])
            elif code.startswith(quote, i):
                if len(quote) == 3:
                    out.extend(code[i + 1:i + 3])
                    i += 2
                quote = ""
            i += 1
            continue
        if code.startswith("'''", i) or code.startswith('\"\"\"', i):
            quote = code[i:i + 3]
            out.append(quote)
            i += 3
            continue
        if code[i] in {'"', "'"}:
            quote = code[i]
            out.append(code[i])
            i += 1
            continue
        postfix = code[i:i + 2]
        if (
            postfix in {"++", "--"}
            and (i + 2 == len(code) or code[i + 2] in " \t;)]}")
            and out and (str(out[-1])[-1:].isalnum() or str(out[-1])[-1:] in "_)]")
        ):
            # ``i++`` / ``xs[0]--``: postfix increment, not two operators.
            _rstrip_spaces(out)
            out.append(postfix)
            i += 2
            continue
        op = _operator_at(code, i)
        if op == "=" and _is_keyword_argument(out, brackets[-1][1] if brackets else enclosing):
            # ``sorted(xs, reverse=true)`` / ``case Point(x=0)``.
            _rstrip_spaces(out)
            out.append("=")
            keyword_eq_end = len(out)
            i += 1
            while i < len(code) and code[i].isspace():
                i += 1
            continue
        if op:
            _append_space(out)
            out.append(op)
            _append_space(out)
            i += len(op)
            continue
        ch = code[i]
        if ch == ":":
            if brackets and brackets[-1][1] == "[" and not re.search(
                r"\blambda\b", "".join(out[brackets[-1][0]:]),
            ):
                _rstrip_spaces(out)
                out.append(":")
                i += 1
                while i < len(code) and code[i].isspace():
                    i += 1
                continue
            _rstrip_spaces(out)
            out.append(": ")
            i += 1
            while i < len(code) and code[i].isspace():
                i += 1
            continue
        if ch == ",":
            _rstrip_spaces(out)
            out.append(", ")
            i += 1
            while i < len(code) and code[i].isspace():
                i += 1
            continue
        if ch in "([{":
            prefix = "".join(out).rstrip()
            words = prefix.rsplit(None, 2)
            # ``func match(...)`` / ``obj.match(...)``: a name, not a keyword.
            declared_name = len(words) >= 2 and words[-2] in {"func", "class", "interface"}
            if declared_name or not _space_before_opener_is_meaningful(out):
                _rstrip_spaces(out)
            prefix = "".join(out).rstrip()
            if ch == "(" and not declared_name and _keyword_before(out) in _SPACED_PAREN_KEYWORDS:
                # ``if (a and b) or c`` / ``not (x)`` / ``lambda (x: int)``:
                # exactly one space between the keyword and ``(``.
                _rstrip_spaces(out)
                out.append(" ")
            if (
                ch == "{" and out and not str(out[-1]).endswith(" ") and "".join(out).rstrip()[-1:] not in "([{"
                and len(out) != keyword_eq_end
            ):
                # ``if x {`` / ``= {``, but ``take({...})`` / ``[{...}]`` /
                # ``f(attrs={...})``.
                out.append(" ")
            out.append(ch)
            # ``func top[T: ordered]`` / ``class Registry[K: comparable, V]``.
            brackets.append((len(out), "<" if ch == "[" and declared_name else ch))
            i += 1
            while i < len(code) and code[i].isspace():
                i += 1
            continue
        if ch in ")]}":
            _rstrip_spaces(out)
            out.append(ch)
            if brackets:
                brackets.pop()
            i += 1
            continue
        if ch.isspace():
            _append_space(out)
            i += 1
            continue
        if ch == "a" and out and _is_word_at(code, i, "as"):
            # ``row["n"]as int`` / ``(v as any)as int``: one space around
            # the cast (and binding) keyword.
            _rstrip_spaces(out)
            out.append(" as ")
            i += 2
            while i < len(code) and code[i].isspace():
                i += 1
            continue
        if ch in "+-" and _is_unary_sign(code, i):
            # ``x = - 1`` / ``n ?? - 1``: a sign sticks to its operand.
            out.append(ch)
            i += 1
            while i < len(code) and code[i].isspace():
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _is_word_at(code: str, index: int, word: str) -> bool:
    """``word`` starts at ``index`` as a whole identifier."""
    if not code.startswith(word, index):
        return False
    before = code[index - 1] if index > 0 else ""
    after = code[index + len(word)] if index + len(word) < len(code) else ""
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


def _is_keyword_argument(out: list[str], bracket: str) -> bool:
    """The ``=`` about to be written follows ``(name`` / ``, name`` (or a
    bare ``name`` starting a continuation line) directly inside
    parentheses: a keyword argument, written ``name=value``. A typed
    default (``n: int = 3``) and an assignment are not."""
    if bracket != "(":
        return False
    text = "".join(out).rstrip()
    match = re.search(r"(?:^|[(,])\s*[A-Za-z_]\w*$", text)
    return match is not None


def _operator_at(code: str, index: int) -> str:
    for op in _BINARY_OPS:
        if code.startswith(op, index):
            if op in {"/", "-"} and _is_scoped_name_punctuation(code, index):
                return ""
            if op == "*" and _is_unary_or_variadic_star(code, index):
                return ""
            if op == "*" and index + 1 < len(code) and code[index + 1] == "*":
                continue
            if op == "-" and index + 1 < len(code) and code[index + 1] == ">":
                continue
            if op in {"+", "-"} and index > 0 and (
                code[index - 1] in "([{=,:+-*/%<>!&|^" or code[max(0, index - 2):index] == "??"
            ):
                return ""
            if op in {"+", "-"} and _is_unary_sign(code, index):
                return ""
            return op
    return ""


def _is_unary_or_variadic_star(code: str, index: int) -> bool:
    prev = _previous_nonspace(code, index)
    nxt = code[index + 1] if index + 1 < len(code) else ""
    if nxt == "*":
        return True
    if prev == "" or prev in "([{=,:":
        return bool(nxt and (nxt.isalpha() or nxt == "_"))
    return False


_UNARY_AFTER_WORDS = {"return", "if", "elif", "while", "in", "and", "or", "not", "yield", "else", "case"}


def _is_unary_sign(code: str, index: int) -> bool:
    """``i - -1`` / ``return -1``: a sign after an operator or keyword
    (separated by spaces) is unary."""
    if index + 1 >= len(code) or not code[index + 1:].strip():
        return False
    prev = _previous_nonspace(code, index)
    if not prev:
        # A line starting with ``- b`` continues a binary expression of the
        # line before; ``-1`` starts with a negative number.
        return not code[index + 1].isspace()
    if prev in "([{=,:+-*/%<>!&|^":
        return True
    if prev == "?" and _ends_with_coalesce(code[:index]):
        # ``n ?? -1`` (a postfix ``?`` is not an operator: ``r? - 1``).
        return True
    if prev.isalnum() or prev == "_":
        j = index - 1
        while j >= 0 and code[j].isspace():
            j -= 1
        end = j + 1
        while j >= 0 and (code[j].isalnum() or code[j] == "_"):
            j -= 1
        return code[j + 1:end] in _UNARY_AFTER_WORDS
    return False


def _ends_with_coalesce(text: str) -> bool:
    """``text`` (spaces aside) ends with the ``??`` operator, not with the
    postfix ``?`` of a propagation (``r?``)."""
    return text.rstrip().endswith("??")


def _previous_nonspace(code: str, index: int) -> str:
    i = index - 1
    while i >= 0 and code[i].isspace():
        i -= 1
    if i < 0:
        return ""
    return code[i]


def _is_scoped_name_punctuation(code: str, index: int) -> bool:
    left = index - 1
    while left >= 0 and (code[left].isalnum() or code[left] in "@_-/"):
        left -= 1
    right = index + 1
    while right < len(code) and (code[right].isalnum() or code[right] in "_-"):
        right += 1
    return code[left + 1:index].startswith("@") and right > index + 1


def _split_inline_comment(line: str) -> tuple[str, str]:
    quote = ""
    i = 0
    while i < len(line):
        if quote:
            if line[i] == "\\" and len(quote) == 1:
                i += 2
                continue
            if line.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
            i += 1
            continue
        if line.startswith("'''", i) or line.startswith('\"\"\"', i):
            quote = line[i:i + 3]
            i += 3
            continue
        if line[i] in {'"', "'"}:
            quote = line[i]
            i += 1
            continue
        if line[i] == "#":
            return line[:i].rstrip(), line[i:]
        i += 1
    return line.rstrip(), ""


def _brace_delta(line: str) -> int:
    code, _comment = _split_inline_comment(line)
    quote = ""
    delta = 0
    i = 0
    while i < len(code):
        if quote:
            if code[i] == "\\" and len(quote) == 1:
                i += 2
                continue
            if code.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
            i += 1
            continue
        if code.startswith("'''", i) or code.startswith('\"\"\"', i):
            quote = code[i:i + 3]
            i += 3
            continue
        if code[i] in {'"', "'"}:
            quote = code[i]
            i += 1
            continue
        if code[i] == "{":
            delta += 1
        elif code[i] == "}":
            delta -= 1
        i += 1
    return delta


def _collect_doc_comment(lines: list[str], start: int) -> tuple[list[str], int]:
    block: list[str] = []
    i = start
    while i < len(lines):
        block.append(lines[i])
        i += 1
        if "-#" in lines[i - 1]:
            break
    return block, i


def _starts_go_block(line: str) -> bool:
    return bool(re.match(r"^go!\s*\{", line)) or line == "go!"


def _collect_go_block(lines: list[str], start: int) -> tuple[list[str], int]:
    block: list[str] = []
    depth = 0
    i = start
    while i < len(lines):
        raw = lines[i].rstrip()
        block.append(raw)
        depth += _brace_delta(raw)
        i += 1
        if depth <= 0:
            break
    return block, i


def _format_go_block(block: list[str], line_depth: int) -> list[str]:
    """A ``go! { ... }`` block: raw Go is left byte for byte as written
    (comments, spacing and indentation included); only the ``go! {`` line
    (and a closing ``}`` alone on its line) follow the Lam indentation."""
    indent = "    " * line_depth
    if not block:
        return []
    out = [indent + block[0].strip()]
    if len(block) == 1:
        return out
    out.extend(block[1:-1])
    last = block[-1]
    out.append(indent + "}" if last.strip() == "}" else last)
    return out


def _protect_inline_go(code: str) -> tuple[str, list[str]]:
    values: list[str] = []
    out: list[str] = []
    i = 0
    quote = ""
    while i < len(code):
        if quote:
            out.append(code[i])
            if code[i] == "\\" and len(quote) == 1 and i + 1 < len(code):
                i += 1
                out.append(code[i])
            elif code.startswith(quote, i):
                out.extend(code[i + 1:i + len(quote)])
                i += len(quote) - 1
                quote = ""
            i += 1
            continue
        if code.startswith("'''", i) or code.startswith('"""', i):
            quote = code[i:i + 3]
            out.append(quote)
            i += 3
            continue
        if code[i] in {'"', "'"}:
            quote = code[i]
            out.append(code[i])
            i += 1
            continue
        if code.startswith("go!(", i):
            end = _find_matching_paren(code, i + 3)
            if end is not None:
                placeholder = f"__LAM_INLINE_GO_{len(values)}__"
                values.append(code[i:end + 1])
                out.append(placeholder)
                i = end + 1
                continue
        out.append(code[i])
        i += 1
    return "".join(out), values


def _restore_inline_go(code: str, values: list[str]) -> str:
    for i, value in enumerate(values):
        code = code.replace(f"__LAM_INLINE_GO_{i}__", value)
    return code


def _find_matching_paren(code: str, open_index: int) -> int | None:
    depth = 0
    quote = ""
    i = open_index
    while i < len(code):
        if quote:
            if code[i] == "\\" and len(quote) == 1:
                i += 2
                continue
            if code.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
            i += 1
            continue
        if code.startswith("'''", i) or code.startswith('"""', i):
            quote = code[i:i + 3]
            i += 3
            continue
        if code[i] in {'"', "'"}:
            quote = code[i]
            i += 1
            continue
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _append_space(out: list[str]) -> None:
    if out and not str(out[-1]).endswith(" "):
        out.append(" ")


def _rstrip_spaces(out: list[str]) -> None:
    while out and str(out[-1]).endswith(" "):
        out[-1] = str(out[-1]).rstrip()
        if out[-1]:
            break
        out.pop()


def _keyword_before(out: list[str]) -> str:
    """The keyword ending ``out`` (``if``, ``not``, ``lambda``; not a
    member such as ``obj.match``), or ``""``."""
    text = "".join(out).rstrip()
    match = re.search(r"(?:^|[^\w.])([A-Za-z_]\w*)$", text)
    return match.group(1) if match else ""


def _space_before_opener_is_meaningful(out: list[str]) -> bool:
    text = "".join(out)
    i = len(text) - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    if i < 0:
        return False
    if text[i].isalnum() or text[i] == "_":
        # ``match [a, b]`` / ``case [x, *rest]`` / ``return (x)``: a keyword
        # before the opener is not a call or subscript.
        end = i + 1
        while i >= 0 and (text[i].isalnum() or text[i] == "_"):
            i -= 1
        return text[i + 1:end] in _OPENER_KEYWORDS and (i < 0 or text[i] != ".")
    # ``xs ?? []`` / ``n ?? (a + b)``: an operand after ``??`` (but
    # ``f()?[0]`` indexes the propagated value).
    return text[i] in "=,:+-*/%<>!&|^" or _ends_with_coalesce(text[:i + 1])


# Keywords written with one space before a ``(`` that follows them.
_SPACED_PAREN_KEYWORDS = frozenset({
    "if", "elif", "while", "for", "catch", "with", "match", "case", "in", "return", "as",
    "yield", "and", "or", "not", "assert", "raise", "throw", "await", "is", "else", "lambda",
})

_OPENER_KEYWORDS = frozenset({
    "match", "case", "in", "return", "yield", "if", "elif", "while", "and", "or",
    "not", "else", "assert", "raise", "throw", "await", "is",
})
