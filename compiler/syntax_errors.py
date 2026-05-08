"""Lam-facing syntax diagnostics.

The parser is Lark, but users write Lammergeier. This module turns
Lark's low-level ``UnexpectedInput`` exceptions into stable compiler
diagnostics with Lam terminology, source context, expected constructs,
and short repair hints.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable

from lark import Token
from lark.exceptions import UnexpectedCharacters, UnexpectedEOF, UnexpectedInput, UnexpectedToken

from compiler.diagnostics import Diagnostic, DiagnosticSeverity, SourceSpan


class SyntaxDiagnosticError(RuntimeError):
    """Raised internally when a parsed Lam file has a syntax error."""


@dataclass(frozen=True)
class SyntaxDiagnostic:
    path: str
    line: int
    column: int
    message: str
    expected: tuple[str, ...]
    hint: str | None
    source_line: str

    def lsp_message(self) -> str:
        parts = [self.message]
        if self.expected:
            parts.append(f"Expected {_join_labels(self.expected)}.")
        if self.hint:
            parts.append(self.hint)
        return " ".join(parts)

    def to_diagnostic(self) -> Diagnostic:
        return Diagnostic(
            code="LAM0001",
            severity=DiagnosticSeverity.ERROR,
            message=self.lsp_message(),
            span=SourceSpan(
                file=Path(self.path) if self.path != "<buffer>" else None,
                line=self.line,
                col=self.column,
            ),
            hint=self.hint,
        )

    def render(self) -> str:
        out: list[str] = [f"error: syntax check failed for {self.path}"]
        out.append(f"  --> {self.path}:{self.line}:{self.column}")
        out.append("")
        out.extend(_render_source_line(self.line, self.column, self.source_line))
        out.append("")
        out.append(f"  {self.message}")
        if self.expected:
            out.append(f"  expected: {_join_labels(self.expected)}")
        if self.hint:
            out.append(f"  help: {self.hint}")
        return "\n".join(out)


_TOKEN_LABELS: dict[str, str] = {
    "AMPERSAND": "`&`",
    "AND": "`and`",
    "AS": "`as`",
    "ASYNC": "`async`",
    "AT": "`@`",
    "ASSERT": "`assert`",
    "AWAIT": "`await`",
    "BIN_NUMBER": "a binary integer literal",
    "BREAK": "`break`",
    "CASE": "`case`",
    "CATCH": "`catch`",
    "CIRCUMFLEX": "`^`",
    "CLASS": "`class`",
    "COLON": "`:`",
    "COMMA": "`,`",
    "CONST": "`const`",
    "CONTINUE": "`continue`",
    "DEC_NUMBER": "an integer literal",
    "DEFER": "`defer`",
    "DEL": "`del`",
    "DOT": "`.`",
    "DO": "`do`",
    "ELIF": "`elif`",
    "ELSE": "`else`",
    "EQUAL": "`=`",
    "FALSE": "`False`",
    "FINALLY": "`finally`",
    "FLOAT_NUMBER": "a float literal",
    "FOR": "`for`",
    "FROM": "`from`",
    "FUNC": "`func`",
    "FSTRING": "an f-string",
    "FSTRING_LONG": "a multiline f-string",
    "GLOBAL": "`global`",
    "HEX_NUMBER": "a hex integer literal",
    "IF": "`if`",
    "IMAG_NUMBER": "an imaginary number literal",
    "IMPORT": "`import`",
    "IN": "`in`",
    "INTERFACE": "`interface`",
    "IS": "`is`",
    "LAMBDA": "`lambda`",
    "LBRACE": "`{`",
    "LONG_STRING": "a multiline string",
    "LPAR": "`(`",
    "LSQB": "`[`",
    "MATCH": "`match`",
    "MINUS": "`-`",
    "MORETHAN": "`>`",
    "NAME": "an identifier",
    "NONLOCAL": "`nonlocal`",
    "NOT": "`not`",
    "NONE": "`None`",
    "OCT_NUMBER": "an octal integer literal",
    "OR": "`or`",
    "PASS": "`pass`",
    "PERCENT": "`%`",
    "PLUS": "`+`",
    "PRIVATE": "`private`",
    "QMARK": "`?`",
    "RAISE": "`raise`",
    "RBRACE": "`}`",
    "RETURN": "`return`",
    "RPAR": "`)`",
    "RSQB": "`]`",
    "SCOPED_NAME": "a scoped package name like `@scope/name`",
    "SEMICOLON": "the end of the statement",
    "SLASH": "`/`",
    "STAR": "`*`",
    "STATIC": "`static`",
    "STATIC_FUNC": "`static func`",
    "STRING": "a string literal",
    "THROW": "`throw`",
    "TRUE": "`True`",
    "TRY": "`try`",
    "VBAR": "`|`",
    "WHILE": "`while`",
    "WITH": "`with`",
    "YIELD": "`yield`",
    "__ANON_0": "`->`",
    "__ANON_1": "`**`",
    "__ANON_2": "`++`",
    "__ANON_3": "`--`",
    "__ANON_4": "`+=`",
    "__ANON_5": "`-=`",
    "__ANON_6": "`*=`",
    "__ANON_7": "`@=`",
    "__ANON_8": "`/=`",
    "__ANON_9": "`%=`",
    "__ANON_10": "`&=`",
    "__ANON_11": "`|=`",
    "__ANON_12": "`^=`",
    "__ANON_13": "`<<=`",
    "__ANON_14": "`>>=`",
    "__ANON_15": "`**=`",
    "__ANON_16": "`//=`",
    "__ANON_17": "`??`",
    "__ANON_18": "`:=`",
    "__ANON_19": "`<<`",
    "__ANON_20": "`>>`",
    "__ANON_21": "`//`",
    "__ANON_22": "`==`",
    "__ANON_23": "`>=`",
    "__ANON_24": "`<=`",
    "__ANON_25": "`!=`",
    "__ANON_26": "`?.`",
    "__ANON_27": "`...`",
}

_EXPR_START_TOKENS = {
    "AWAIT",
    "DEC_NUMBER",
    "FLOAT_NUMBER",
    "FSTRING",
    "FSTRING_LONG",
    "HEX_NUMBER",
    "IMAG_NUMBER",
    "MINUS",
    "NAME",
    "NOT",
    "OCT_NUMBER",
    "STRING",
}

_BLOCK_STARTERS = {"FUNC", "IF", "ELIF", "ELSE", "FOR", "WHILE", "TRY", "CATCH", "FINALLY", "CLASS", "MATCH", "WITH", "DO"}


def render_syntax_error(exc: UnexpectedInput, source: str, path: str | Path) -> str:
    """Return a complete Lam syntax error block for ``exc``."""

    diag = make_syntax_diagnostic(exc, source, path)
    return diag.render()


def make_syntax_diagnostic(exc: UnexpectedInput, source: str, path: str | Path) -> SyntaxDiagnostic:
    line = max(1, int(getattr(exc, "line", 1) or 1))
    column = max(1, int(getattr(exc, "column", 1) or 1))
    src_line = _source_line(source, line)

    expected_names = _expected_names(exc)
    if _is_end_token(exc):
        for closer in ("RBRACE", "RPAR", "RSQB"):
            if closer in expected_names:
                expected = (_TOKEN_LABELS[closer],)
                break
        else:
            expected = tuple(_expected_labels(expected_names))
    else:
        expected = tuple(_expected_labels(expected_names))
    expected = _refine_expected(exc, expected, source, line, column, src_line)
    message = _message_for(exc, src_line)
    hint = _hint_for(exc, expected, source, line, column, src_line)

    return SyntaxDiagnostic(
        path=str(path),
        line=line,
        column=column,
        message=message,
        expected=expected,
        hint=hint,
        source_line=src_line,
    )


def lsp_syntax_message(exc: UnexpectedInput, source: str) -> str:
    """Single-line syntax message for editor diagnostics."""

    return make_syntax_diagnostic(exc, source, "<buffer>").lsp_message()


def _message_for(exc: UnexpectedInput, src_line: str) -> str:
    if isinstance(exc, UnexpectedEOF) or _is_end_token(exc):
        return "Unexpected end of file. A construct was left unfinished."

    if isinstance(exc, UnexpectedCharacters):
        char = getattr(exc, "char", None)
        if char:
            return f"Unexpected character {char!r}."
        return "Unexpected character."

    if isinstance(exc, UnexpectedToken):
        token = getattr(exc, "token", None)
        if isinstance(token, Token):
            if token.type == "SEMICOLON":
                return "Unexpected end of statement."
            value = _token_source_text(token, src_line)
            label = _display_token(token.type, value)
            if value and label != repr(value):
                return f"Unexpected {label}."
        return "Unexpected token."

    return "The parser could not understand this Lam syntax."


def _expected_names(exc: UnexpectedInput) -> set[str]:
    expected = set(getattr(exc, "expected", None) or [])
    if isinstance(exc, UnexpectedCharacters):
        expected.update(getattr(exc, "allowed", None) or [])
    return {str(x) for x in expected if x}


def _expected_labels(expected: Iterable[str]) -> list[str]:
    names = set(expected)
    labels: list[str] = []

    if names & _EXPR_START_TOKENS:
        labels.append("an expression")
        names -= _EXPR_START_TOKENS

    if names & _BLOCK_STARTERS:
        labels.append("a statement")
        names -= _BLOCK_STARTERS

    priority = [
        "LBRACE", "RBRACE", "LPAR", "RPAR", "LSQB", "RSQB",
        "COLON", "COMMA", "SEMICOLON", "EQUAL", "__ANON_0",
        "NAME", "SCOPED_NAME", "STRING", "DEC_NUMBER",
    ]
    for name in priority:
        if name in names:
            labels.append(_TOKEN_LABELS.get(name, f"`{name}`"))
            names.remove(name)

    for name in sorted(names):
        label = _TOKEN_LABELS.get(name)
        if label and label not in labels:
            labels.append(label)
        if len(labels) >= 8:
            break

    return labels[:8]


def _refine_expected(
    exc: UnexpectedInput,
    expected: tuple[str, ...],
    source: str,
    line: int,
    column: int,
    src_line: str,
) -> tuple[str, ...]:
    token_type, token_value = _token_details(exc, src_line)
    stripped = src_line.strip()
    previous = _previous_nonempty_line(source, line).strip()

    if _looks_like_python_def(stripped) or _looks_like_wrong_function_keyword(stripped):
        return ("`func`",)
    if _looks_like_missing_function_name(stripped):
        return ("an identifier",)
    if _looks_like_missing_named_declaration(stripped):
        return ("an identifier",)
    if _looks_like_missing_return_type(src_line, column):
        return ("a return type",)
    if _looks_like_missing_lambda_body(stripped, token_type):
        return ("`:`", "`{`")
    if _looks_like_missing_for_in(stripped, token_type):
        return ("`in`",)
    if _looks_like_incomplete_import(stripped, token_type):
        return ("a module name",)
    if _looks_like_from_missing_import(stripped, token_type):
        return ("`import`",)
    if _looks_like_dict_missing_colon(src_line, column, token_type):
        return ("`:`",)
    if _looks_like_c_style_not(src_line, column):
        return ("`not`", "an expression")
    if _looks_like_c_style_and(src_line, column):
        return ("`and`",)
    if _looks_like_c_style_or(src_line, column):
        return ("`or`",)
    if _looks_like_missing_header_expr(previous):
        return ("an expression",)
    if _looks_like_empty_match(stripped, token_type, token_value):
        return ("`case`",)
    if _looks_like_empty_interface(stripped, token_type, token_value):
        return ("`func`",)
    if _looks_like_try_missing_handler(source, line, token_type):
        return ("`catch`", "`finally`")
    if token_value == "except":
        return ("`catch`",)
    if stripped.startswith("case"):
        return ("a `match` block",)
    if stripped.startswith("@") and token_type == "SEMICOLON":
        return ("`func` or `class`",)
    return expected


def _hint_for(
    exc: UnexpectedInput,
    expected: tuple[str, ...],
    source: str,
    line: int,
    column: int,
    src_line: str,
) -> str | None:
    stripped = src_line.strip()
    expected_names = _expected_names(exc)
    token_type, token_value = _token_details(exc, src_line)
    previous = _previous_nonempty_line(source, line).strip()

    if _looks_like_python_def(stripped):
        return "Lammergeier declares functions with `func`, for example `func main() { ... }`."
    if _looks_like_wrong_function_keyword(stripped):
        keyword = stripped.split()[0]
        return f"Lammergeier declares functions with `func`, not `{keyword}`."
    if _looks_like_missing_function_name(stripped):
        return "Function declarations need a name: write `func name(...) { ... }`."
    if _looks_like_missing_named_declaration(stripped):
        keyword = stripped.split()[0]
        return f"`{keyword}` declarations need a name before the block."
    if _looks_like_missing_return_type(src_line, column):
        return "Add a return type after `->`, or remove `->` if the function returns nothing."
    if _looks_like_missing_lambda_body(stripped, token_type):
        return "Lambda expressions need a body: write `lambda x: expr` or `lambda x { ... }`."
    if _looks_like_missing_for_in(stripped, token_type):
        return "For loops use `for item in iterable { ... }`; add `in` between the target and iterable."
    if _looks_like_incomplete_import(stripped, token_type):
        return "Import statements need a module path after `import`, for example `import math`."
    if _looks_like_from_missing_import(stripped, token_type):
        return "`from` imports use `from module import Name`; add `import` before the imported names."
    if _looks_like_missing_header_expr(previous):
        keyword = previous.split()[0]
        noun = "resource expression" if keyword == "with" else "condition" if keyword in {"if", "while"} else "expression"
        return f"Add a {noun} after `{keyword}` before opening the block."
    if _looks_like_empty_match(stripped, token_type, token_value):
        return "A `match` block must contain at least one `case pattern { ... }` arm."
    if _looks_like_empty_interface(stripped, token_type, token_value):
        return "Interfaces must declare at least one method, for example `func area() -> float;`."
    if _looks_like_try_missing_handler(source, line, token_type):
        return "A `try` block must be followed by at least one `catch` or `finally` block."
    if _looks_like_dict_missing_colon(src_line, column, token_type):
        return "Dictionary entries use `key: value`; add `:` between the key and value."
    if _looks_like_c_style_not(src_line, column):
        return "Lammergeier uses `not expr` for boolean negation, not `!expr`."
    if _looks_like_c_style_and(src_line, column):
        return "Lammergeier uses `and` for logical conjunction, not `&&`."
    if _looks_like_c_style_or(src_line, column):
        return "Lammergeier uses `or` for logical disjunction, not `||`."
    if token_value == "except":
        return "Lammergeier uses `catch`, not `except`, after `try { ... }` blocks."
    if stripped.startswith("case"):
        return "`case` labels are only valid inside a `match value { ... }` block."
    if stripped.startswith("@") and token_type == "SEMICOLON":
        return "Decorators must be followed immediately by a `func` or `class` declaration."
    if stripped.startswith("else"):
        return "`else` must belong to a preceding `if`, `while`, or `try` block."
    if stripped.startswith("elif"):
        return "`elif` must belong to a preceding `if` block."
    if stripped.startswith("catch"):
        return "`catch` must follow a `try { ... }` block, or a `do { ... } catch err { ... }` block."
    if stripped.startswith("finally"):
        return "`finally` must follow a `try { ... }` block."

    if token_type == "AT" and "SCOPED_NAME" in expected_names:
        return "Scoped imports must be lowercase and use `@scope/name`, for example `from @alice/lamwebp import Encoder`."
    if isinstance(exc, UnexpectedCharacters):
        char = getattr(exc, "char", "")
        if char == "!":
            return "Lammergeier uses `not expr` for boolean negation; keep `!=` for not-equal comparisons."
        if char == "&" and "&&" in src_line:
            return "Lammergeier uses `and` for logical conjunction, not `&&`."
        if char == "|" and "||" in src_line:
            return "Lammergeier uses `or` for logical disjunction, not `||`."
        if char == "@":
            return "Decorators are `@name` before `func`/`class`; scoped imports must be lowercase like `from @scope/name import X`."
        if char in {"'", '"'}:
            return "Check that the string literal is closed on the same line, or use triple quotes for multiline text."

    if token_type == "COLON" and "LBRACE" in expected_names:
        return "Lam blocks use braces, not indentation: replace `:` with `{ ... }`."
    if "COMMA" in expected_names and token_type == "NAME":
        return "Add `,` between parameters/items, or close the surrounding list with the matching delimiter."
    if "COLON" in expected_names and token_type in {"NAME", "IN"}:
        return "Type annotations use `name: Type`; add `:` between the name and its type."
    if "RPAR" in expected_names:
        return "Close the parameter list, call, or grouped expression with `)`."
    if "RSQB" in expected_names:
        return "Close the list, index, or generic type expression with `]`."
    if "RBRACE" in expected_names:
        return "Close the block, dict, set, or match body with `}`."
    if token_type == "RBRACE":
        return "Remove this extra `}`, or add the missing opening `{` before the block it should close."
    if token_type == "RPAR":
        return "Remove this extra `)`, or add the missing opening `(` before the expression it should close."
    if token_type == "RSQB":
        return "Remove this extra `]`, or add the missing opening `[` before the expression it should close."
    if "LBRACE" in expected_names:
        return "Lam blocks use braces: write the header followed by `{ ... }`."
    if "SEMICOLON" in expected_names:
        return "End the statement here, or split the expression with an explicit operator/continuation."
    if isinstance(exc, UnexpectedEOF) or _is_end_token(exc):
        return "Check for a missing `}`, `)`, or `]` near the end of the file."

    if expected:
        return "Rewrite this construct using one of the expected Lam forms above."
    return None


def _token_details(exc: UnexpectedInput, src_line: str) -> tuple[str, str]:
    token = getattr(exc, "token", None)
    if isinstance(token, Token):
        return token.type, _token_source_text(token, src_line)
    return "", ""


def _looks_like_python_def(stripped: str) -> bool:
    return bool(re.match(r"^def\s+\w+", stripped))


def _looks_like_wrong_function_keyword(stripped: str) -> bool:
    return bool(re.match(r"^(function|fn)\s+\w+", stripped))


def _looks_like_missing_function_name(stripped: str) -> bool:
    return bool(re.match(r"^(?:private\s+)?(?:static\s+)?(?:async\s+)?func\s*(?:\(|\[|->|\{|$)", stripped))


def _looks_like_missing_return_type(src_line: str, column: int) -> bool:
    return src_line[:max(0, column - 1)].rstrip().endswith("->")


def _looks_like_missing_lambda_body(stripped: str, token_type: str) -> bool:
    return token_type == "SEMICOLON" and "lambda" in stripped


def _looks_like_missing_named_declaration(stripped: str) -> bool:
    return bool(re.match(r"^(class|interface)\s*(?:\{|$)", stripped))


def _looks_like_missing_for_in(stripped: str, token_type: str) -> bool:
    if token_type != "NAME":
        return False
    if " in " in f" {stripped} ":
        return False
    return bool(re.match(r"^for\s+\S+\s+\S+", stripped))


def _looks_like_incomplete_import(stripped: str, token_type: str) -> bool:
    return token_type == "SEMICOLON" and stripped == "import"


def _looks_like_from_missing_import(stripped: str, token_type: str) -> bool:
    return token_type == "NAME" and bool(re.match(r"^from\s+\S+\s+\S+", stripped)) and " import " not in f" {stripped} "


def _looks_like_missing_header_expr(stripped: str) -> bool:
    return bool(re.match(r"^(if|while|match|with)\s*(?:\{|:)\s*$", stripped))


def _looks_like_empty_match(stripped: str, token_type: str, token_value: str) -> bool:
    return token_type == "PASS" and token_value == "pass" and bool(re.match(r"^match\b.*\{\s*$", stripped))


def _looks_like_empty_interface(stripped: str, token_type: str, token_value: str) -> bool:
    return token_type == "PASS" and token_value == "pass" and bool(re.match(r"^interface\b.*\{\s*$", stripped))


def _looks_like_try_missing_handler(source: str, line: int, token_type: str) -> bool:
    if token_type != "RBRACE":
        return False
    lines = [s.strip() for s in source.splitlines()[:max(0, line - 1)] if s.strip()]
    if not lines or lines[-1] != "}":
        return False
    for stripped in reversed(lines[:-1]):
        if stripped.startswith(("catch", "finally")):
            return False
        if re.match(r"^try\b.*\{\s*$", stripped):
            return True
    return False


def _looks_like_dict_missing_colon(src_line: str, column: int, token_type: str) -> bool:
    if token_type not in _EXPR_START_TOKENS and token_type not in {"LPAR", "LSQB", "LBRACE"}:
        return False
    prefix = src_line[:max(0, column - 1)]
    suffix = src_line[max(0, column - 1):]
    if "{" not in prefix or "}" not in suffix:
        return False
    return bool(re.search(r'[{,]\s*(?:"[^"]*"|\'[^\']*\'|[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?)\s+$', prefix))


def _looks_like_c_style_not(src_line: str, column: int) -> bool:
    idx = max(0, column - 1)
    return src_line[idx:idx + 1] == "!" and src_line[idx:idx + 2] != "!="


def _looks_like_c_style_and(src_line: str, column: int) -> bool:
    idx = max(0, column - 1)
    return src_line[idx:idx + 2] == "&&" or (idx > 0 and src_line[idx - 1:idx + 1] == "&&")


def _looks_like_c_style_or(src_line: str, column: int) -> bool:
    idx = max(0, column - 1)
    return src_line[idx:idx + 2] == "||" or (idx > 0 and src_line[idx - 1:idx + 1] == "||")


def _previous_nonempty_line(source: str, line: int) -> str:
    lines = source.splitlines()
    for idx in range(min(line - 2, len(lines) - 1), -1, -1):
        if lines[idx].strip():
            return lines[idx]
    return ""


def _display_token(token_type: str, value: str) -> str:
    if token_type == "NAME":
        return f"identifier `{value}`"
    if token_type in {"STRING", "LONG_STRING", "FSTRING", "FSTRING_LONG"}:
        return "string literal"
    if token_type in {"DEC_NUMBER", "FLOAT_NUMBER", "HEX_NUMBER", "OCT_NUMBER", "BIN_NUMBER", "IMAG_NUMBER"}:
        return f"number literal `{value}`"
    if value:
        return f"`{value}`"
    return _TOKEN_LABELS.get(token_type, f"`{token_type}`")


def _is_end_token(exc: UnexpectedInput) -> bool:
    token = getattr(exc, "token", None)
    return isinstance(token, Token) and token.type == "$END"


def _token_source_text(token: Token, src_line: str) -> str:
    value = str(token)
    col = int(getattr(token, "column", 1) or 1)
    if src_line and 1 <= col <= len(src_line):
        tail = src_line[col - 1:]
        m = re.match(r"[A-Za-z_][A-Za-z0-9_]*", tail)
        if m and value and m.group(0).startswith(value):
            return m.group(0)
    return value


def _source_line(source: str, line: int) -> str:
    lines = source.splitlines()
    if 1 <= line <= len(lines):
        return lines[line - 1]
    return ""


def _render_source_line(line: int, column: int, text: str) -> list[str]:
    gutter = f"{line:>5} | "
    if text:
        caret_col = max(1, min(column, len(text) + 1))
    else:
        caret_col = 1
    pointer = " " * (len(gutter) + caret_col - 1) + "^"
    return [gutter + text, pointer]


def _join_labels(labels: tuple[str, ...] | list[str]) -> str:
    if not labels:
        return "valid Lam syntax"
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} or {labels[1]}"
    return ", ".join(labels[:-1]) + f", or {labels[-1]}"
