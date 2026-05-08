from __future__ import annotations

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
    lines = source.splitlines()
    out: list[str] = []
    depth = 0
    in_go_block = False
    go_depth = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped:
            out.append("")
            continue
        if in_go_block:
            out.append(raw.rstrip())
            go_depth += _brace_delta(raw)
            if go_depth <= 0:
                in_go_block = False
            continue
        leading_closers = _leading_closing_braces(stripped)
        line_depth = max(0, depth - leading_closers)
        formatted = _format_code_line(stripped)
        out.append("    " * line_depth + formatted)
        if _starts_go_block(stripped):
            in_go_block = True
            go_depth = _brace_delta(stripped)
            if go_depth <= 0:
                in_go_block = False
        depth = max(0, depth + _brace_delta(stripped))
    return "\n".join(out).rstrip() + "\n"


def _format_code_line(line: str) -> str:
    code, comment = _split_inline_comment(line)
    code = _format_code_fragment(code.strip())
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
            if op == "*" and index + 1 < len(code) and code[index + 1] == "*":
                continue
            if op == "-" and index + 1 < len(code) and code[index + 1] == ">":
                continue
            if op in {"+", "-"} and index > 0 and code[index - 1] in "([{=,:+-*/%<>!&|^":
                return ""
            return op
    return ""


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
    return line.startswith("go! ") or line.startswith("go!{") or line == "go!"


def _append_space(out: list[str]) -> None:
    if out and not str(out[-1]).endswith(" "):
        out.append(" ")


def _rstrip_spaces(out: list[str]) -> None:
    while out and str(out[-1]).endswith(" "):
        out[-1] = str(out[-1]).rstrip()
        if out[-1]:
            break
        out.pop()
