from __future__ import annotations

import re
import subprocess
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
    _parse_or_raise(source)
    formatted = _format_lines(source)
    _parse_or_raise(formatted)
    return FormatResult(text=formatted, changed=formatted != source)


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
    depth = 0
    i = 0
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            i += 1
            continue

        if _starts_go_block(stripped):
            block, next_i = _collect_go_block(lines, i)
            leading_closers = _leading_closing_braces(stripped)
            line_depth = max(0, depth - leading_closers)
            out.extend(_format_go_block(block, line_depth))
            i = next_i
            continue

        leading_closers = _leading_closing_braces(stripped)
        line_depth = max(0, depth - leading_closers)
        formatted = _format_code_line(stripped)
        if formatted == "}" and out and out[-1] == "":
            out.pop()
        if formatted in {"else {", "elif {", "finally {", "catch {"} and out and out[-1].strip() == "}":
            out[-1] = out[-1] + " " + formatted[:-2].strip()
            out[-1] = out[-1] + " {"
            depth = max(0, depth + _brace_delta(stripped))
            i += 1
            continue
        out.append("    " * line_depth + formatted)
        depth = max(0, depth + _brace_delta(stripped))
        i += 1
    return "\n".join(out).rstrip() + "\n"


_LAYOUT_LITERAL_PRECEDERS = set("=,([:+-*/%<>!&|^~?")
_LAYOUT_LITERAL_KEYWORDS = frozenset({
    "return", "yield", "raise", "throw", "await", "in", "and", "or",
    "not", "is",
})
_LAYOUT_LAMBDA_HEAD_RE = re.compile(
    r"\blambda\b(?:\s*\((?:[^()]|\([^)]*\))*\)|\s+\w+(?:\s*,\s*\w+)*)?"
    r"(?:\s*->\s*[^\s{][^{]*?)?\s*$"
)


def _expand_compact_layout(lines: list[str]) -> list[str]:
    expanded: list[str] = []
    layout_stack: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped and _starts_go_block(stripped):
            block, next_i = _collect_go_block(lines, i)
            expanded.extend(block)
            i = next_i
            continue
        expanded.extend(_split_compact_line(lines[i], layout_stack))
        i += 1
    return expanded


def _split_compact_line(line: str, layout_stack: list[str]) -> list[str]:
    code, comment = _split_inline_comment(line)
    if not code.strip():
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
            kind = layout_stack.pop() if layout_stack else "block"
            if kind in {"block", "lambda_block"} and "".join(buf).strip():
                _flush_layout_segment(out, buf)
            buf.append(ch)
            prev_meaningful = ch
            if kind in {"block", "lambda_block"}:
                # Keep ``else``/``catch`` on the same virtual line as
                # the closing brace so the normal line formatter can emit
                # Go-like ``} else {``.
                rest = code[i + 1:].lstrip()
                if not _starts_following_clause(rest):
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
    return "{" if layout_stack and layout_stack[-1] in {"block", "lambda_block"} else ""


def _inside_layout_expression(layout_stack: list[str]) -> bool:
    return bool(layout_stack and layout_stack[-1] == "literal")


def _starts_following_clause(text: str) -> bool:
    return any(text.startswith(keyword) for keyword in ("else", "elif", "catch", "finally"))


def _format_code_line(line: str) -> str:
    code, comment = _split_inline_comment(line)
    protected, inline_go = _protect_inline_go(code.strip())
    code = _format_code_fragment(protected)
    code = _restore_inline_go(code, inline_go)
    if comment:
        if code:
            return f"{code}  {comment.strip()}"
        return comment.strip()
    return code


def _format_code_fragment(code: str) -> str:
    out: list[str] = []
    i = 0
    quote = ""
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
        op = _operator_at(code, i)
        if op:
            _append_space(out)
            out.append(op)
            _append_space(out)
            i += len(op)
            continue
        ch = code[i]
        if ch == ":":
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
            if not _space_before_opener_is_meaningful(out):
                _rstrip_spaces(out)
            prefix = "".join(out).rstrip()
            if ch == "(" and prefix.rsplit(" ", 1)[-1] in {"if", "elif", "while", "for", "catch", "with", "match"}:
                out.append(" ")
            if ch == "{" and out and not str(out[-1]).endswith(" "):
                out.append(" ")
            out.append(ch)
            i += 1
            while i < len(code) and code[i].isspace():
                i += 1
            continue
        if ch in ")]}":
            _rstrip_spaces(out)
            out.append(ch)
            i += 1
            continue
        if ch.isspace():
            _append_space(out)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


def _operator_at(code: str, index: int) -> str:
    for op in _BINARY_OPS:
        if code.startswith(op, index):
            if op == "/" and _is_scoped_name_slash(code, index):
                return ""
            if op == "*" and _is_unary_or_variadic_star(code, index):
                return ""
            if op == "*" and index + 1 < len(code) and code[index + 1] == "*":
                continue
            if op == "-" and index + 1 < len(code) and code[index + 1] == ">":
                continue
            if op in {"+", "-"} and index > 0 and code[index - 1] in "([{=,:+-*/%<>!&|^":
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


def _previous_nonspace(code: str, index: int) -> str:
    i = index - 1
    while i >= 0 and code[i].isspace():
        i -= 1
    if i < 0:
        return ""
    return code[i]


def _is_scoped_name_slash(code: str, index: int) -> bool:
    left = index - 1
    while left >= 0 and (code[left].isalnum() or code[left] in "@_-"):
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


def _leading_closing_braces(line: str) -> int:
    count = 0
    for ch in line:
        if ch == "}":
            count += 1
        elif ch.isspace():
            continue
        else:
            break
    return count


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
    indent = "    " * line_depth
    text = "\n".join(line.strip() for line in block)
    open_idx = text.find("{")
    close_idx = text.rfind("}")
    if open_idx < 0 or close_idx < open_idx:
        return [indent + line.strip() for line in block]

    inner = text[open_idx + 1:close_idx].strip("\n")
    formatted_inner = _format_go_inner(inner)
    if not formatted_inner:
        return [indent + "go! {", indent + "}"]

    out = [indent + "go! {"]
    out.extend(indent + "    " + line if line else "" for line in formatted_inner)
    out.append(indent + "}")
    return out


def _format_go_inner(inner: str) -> list[str]:
    stripped = inner.strip()
    if not stripped:
        return []
    decl = _gofmt_declarations(stripped)
    if decl is not None:
        return decl
    stmt = _gofmt_statements(stripped)
    if stmt is not None:
        return stmt
    return [line.rstrip().expandtabs(4) for line in stripped.splitlines()]


def _gofmt_declarations(src: str) -> list[str] | None:
    formatted = _run_gofmt("package p\n\n" + src.rstrip() + "\n")
    if formatted is None:
        return None
    lines = formatted.splitlines()
    if not lines or lines[0] != "package p":
        return None
    body = lines[1:]
    while body and body[0] == "":
        body.pop(0)
    return [line.rstrip().expandtabs(4) for line in body]


def _gofmt_statements(src: str) -> list[str] | None:
    formatted = _run_gofmt("package p\n\nfunc _() {\n" + src.rstrip() + "\n}\n")
    if formatted is None:
        return None
    lines = formatted.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line == "func _() {")
    except StopIteration:
        return None
    body = lines[start + 1:]
    if body and body[-1] == "}":
        body = body[:-1]
    out: list[str] = []
    for line in body:
        if line.startswith("\t"):
            line = line[1:]
        out.append(line.rstrip().expandtabs(4))
    return out


def _run_gofmt(src: str) -> str | None:
    try:
        proc = subprocess.run(
            ["gofmt"],
            input=src,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


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


def _space_before_opener_is_meaningful(out: list[str]) -> bool:
    text = "".join(out)
    i = len(text) - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    if i < 0:
        return False
    return text[i] in "=,:+-*/%<>!&|^"
