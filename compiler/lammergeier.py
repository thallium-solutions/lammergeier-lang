#!/usr/bin/env python3
"""
lamc — Lammergeier Lang Compiler.

Pipeline:
  .lam source → preprocess go! blocks → Lark parse → AST → Go transpile → go build → binary

Usage:
    lamc source.lam                      # compile next to source (same dir)
    lamc source.lam -o mybinary          # compile to ./mybinary (CWD-relative)
    lamc source.lam --emit-go            # print generated Go, don't compile
    lamc source.lam --emit-ast           # print the Lark AST, don't compile
    lamc source.lam --run                # compile and run immediately
    lamc source.lam --go-ldflags='-s -w' # pass flags to go build
"""

from __future__ import annotations
import argparse
import os
import subprocess
import sys
import tempfile
import shutil
from dataclasses import dataclass
from pathlib import Path
try:
    import lark as _lark
    from lark import Lark, Tree
    from lark.exceptions import UnexpectedInput
    _LARK_IMPORT_ERROR: ImportError | None = None
except ImportError as _err:
    _lark = None
    Lark = None
    class Tree:
        pass
    UnexpectedInput = Exception
    _LARK_IMPORT_ERROR = _err

# Allow running from project root or compiler/
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    from compiler.transpiler import GoTranspiler, preprocess_go_blocks
except ImportError as _err:
    if _LARK_IMPORT_ERROR is None or getattr(_err, "name", None) != "lark":
        raise
    GoTranspiler = None
    def preprocess_go_blocks(source):
        return source, []
from compiler.preprocessor import (
    apply_lammergeier_aliases,
    expand_dict_destructure,
    find_unknown_lammergeier_aliases,
    LAMMERGEIER_ALIASES,
)
from compiler import cache as _lamcache
from compiler.modules import WorkspaceIndex
from compiler.source_map import SourceMap
try:
    from compiler.syntax_errors import SyntaxDiagnosticError, render_syntax_error
except ImportError as _err:
    if _LARK_IMPORT_ERROR is None or getattr(_err, "name", None) != "lark":
        raise
    SyntaxDiagnosticError = Exception
    def render_syntax_error(exc, source, source_path):
        return str(exc)
import re as _re


def auto_semicolons(source: str) -> str:
    """Insert semicolons at end of lines that need them (Go-style).

    A semicolon is inserted after a line's code portion if the line
    ends with an expression/statement token (not a block opener/closer etc.)

    To support multi-line literals (dicts, sets, lists, tuples split across
    several lines) we track the brace context on a stack:

      - "block"   — opened by `{` that follows a code prefix like `func()`,
                    `if cond`, `else`, etc. A lone `}` on a line that closes
                    a block MUST NOT get a trailing semicolon.
      - "literal" — opened by `{` that follows `=`, `,`, `(`, `[`, `:`, or
                    an operator. A lone `}` closing a literal needs a
                    trailing semicolon because the enclosing statement ends
                    on that line.

    Parentheses / brackets (`(`, `[`) also suppress the trailing semicolon
    while they are open so that arguments, list literals, and dict literals
    can span multiple lines without prematurely terminating the statement.
    """
    lines = source.split('\n')
    result = []
    in_triple = None  # Track multiline string delimiters: ''' or """

    # Brace / bracket tracking across lines. Each stack entry is
    # ``(kind, saved_paren_depth)`` where ``kind`` is one of:
    #   * ``"block"``        — function body, ``if``/``for``/etc. The
    #     closing ``}`` does NOT emit a trailing ``;``.
    #   * ``"literal"``      — dict/set literal inside an expression.
    #     Closing ``}`` emits a ``;``.
    #   * ``"lambda_block"`` — multi-line lambda body. Closing ``}``
    #     emits a ``;`` (the lambda is part of an expression), and
    #     ``paren_depth`` is saved on entry / restored on exit so the
    #     auto-semicolon logic for statements inside the body
    #     behaves as if we were at top level.
    brace_stack: list[tuple[str, int]] = []
    paren_depth = 0               # ( and [ nesting

    # Pre-compute the index of the next "code" line for every line,
    # so we can decide if a continuation token like a leading `.` or
    # binary operator on the next line should suppress the current
    # line's auto-inserted semicolon.
    def _is_code_line(s: str) -> bool:
        st = s.strip()
        return bool(st) and not st.startswith('#')

    next_code_idx: list[int] = [-1] * len(lines)
    nxt = -1
    for j in range(len(lines) - 1, -1, -1):
        next_code_idx[j] = nxt
        if _is_code_line(lines[j]):
            nxt = j

    for idx, line in enumerate(lines):
        stripped = line.rstrip()

        # If inside a multiline string, don't modify
        if in_triple:
            if in_triple in stripped:
                in_triple = None
                # Fall through to normal ; processing for closing line
            else:
                result.append(stripped)
                continue
        else:
            if not stripped.lstrip().startswith('#') and stripped:
                triple_state = _check_triple_quotes(stripped)
                if triple_state:
                    in_triple = triple_state
                    result.append(stripped)
                    continue

        if not stripped or stripped.lstrip().startswith('#'):
            result.append(stripped)
            continue

        code = _strip_inline_comment(stripped).rstrip()
        comment_part = stripped[len(code):]

        if not code or in_triple:
            result.append(stripped)
            continue

        # ── Inline-block semicolons. JS allows ``func foo() { return x }``
        #    on one line; the grammar still needs ``return x;`` before
        #    the closing ``}``. We rewrite the line so it becomes
        #    ``func foo() { return x; }`` *before* the line-level
        #    auto-semicolon pass below. Empty blocks (``{}``) are left
        #    alone because ``;`` after ``{`` would land in
        #    ``small_stmt`` territory.
        code = _inline_block_semicolons(code).rstrip()
        # Reconstruct the line for any path that decides not to add a
        # trailing ``;`` — without this the early-exit branches would
        # output the original (un-rewritten) line.
        rewritten = code + comment_part

        # Walk the line, updating the brace stack and paren depth, and
        # capture the sequence of popped brace contexts. That list lets us
        # tell whether a lone `}` on this line closed a literal (need `;`)
        # or a block (no `;`).
        popped_kinds, paren_depth = _walk_line(code, brace_stack, paren_depth)

        code_stripped = code.lstrip()
        last = code[-1]

        # Inside an unclosed (..) or [..] — no semicolon, statement continues.
        if paren_depth > 0:
            result.append(rewritten)
            continue

        # ``i++`` / ``i--`` are complete statements — the trailing
        # ``+`` / ``-`` must not be treated as a line-continuation
        # (which would swallow the semicolon and glue the next line
        # onto the increment). Checked before the generic
        # binary-operator suppressor below.
        if len(code) >= 2 and code[-2:] in ("++", "--"):
            result.append(code + ';' + comment_part)
            continue

        # Don't add ; after these endings — block / literal openers,
        # explicit line-continuation, and binary-operator endings
        # (e.g. ``a +\n    b``).
        if last in ('{', ',', '(', '[', '\\', ';', '.', '+', '-', '*',
                    '/', '%', '<', '>', '=', '&', '|', '^'):
            result.append(rewritten)
            continue

        # ``a ??\n    b`` — null-coalesce split across lines. We need
        # to keep the trailing ``?`` from being treated as the
        # propagate-operator (which IS a complete expression suffix).
        if last == '?' and len(code) >= 2 and code[-2] == '?':
            result.append(rewritten)
            continue

        # Peek ahead — if the next code line starts with a continuation
        # operator (``.method()``, ``+ x``, ``&& y`` …) the current line
        # is *not* a complete statement, so don't terminate it. This
        # turns the JS-style fluent chain
        #
        #     xs
        #         .map(f)
        #         .filter(g)
        #
        # into a single ``xs.map(f).filter(g);`` line for the parser
        # while preserving the source layout.
        nxt_idx = next_code_idx[idx]
        if nxt_idx != -1:
            nxt_code = _strip_inline_comment(lines[nxt_idx]).lstrip()
            if nxt_code:
                # Two-char continuations first, then single-char.
                two = nxt_code[:2]
                one = nxt_code[:1]
                # Allman-style block openers: when the next line
                # starts with ``{`` we're still inside the header of
                # a func / class / if / for / while / try / …, so
                # don't terminate the current line. (Literal ``{``
                # assignments are impossible here — those would have
                # ended the previous line with ``=``, ``(`` or ``,``
                # and hit the earlier continuation check.)
                allman_brace = one == '{'
                if (allman_brace
                        or two in ('?.', '&&', '||', '==', '!=', '<=', '>=',
                                   '+=', '-=', '*=', '/=', '%=', '**', '//',
                                   '<<', '>>')
                        or one in ('.', '+', '-', '*', '/', '%', '<',
                                   '>', '&', '|', '^', '?', ':')):
                    # Don't override block-closer logic below — a lone
                    # `}` line still needs to handle its literal/block
                    # distinction. For everything else, skip the
                    # semicolon and let the next line continue.
                    if not (last == '}' and code_stripped == '}'):
                        result.append(rewritten)
                        continue

        # A lone `}` on its own line: check whether the corresponding `{`
        # was a literal / lambda body (statement-terminating) or a
        # plain block opener.
        if last == '}' and code_stripped == '}':
            if popped_kinds and popped_kinds[-1] in ("literal", "lambda_block"):
                result.append(code + ';' + comment_part)
            else:
                result.append(rewritten)
            continue

        # Single-line block-ending lines (eg ``func foo() { return 1 }``
        # or ``if a { foo() } else { bar() }``): the inline-block pass
        # already inserted ``;`` before each closing block ``}``.
        # Adding another ``;`` after the line would land between
        # top-level definitions and confuse the parser. Skip it
        # unless a literal ``}`` closes the line (that's still an
        # expression and needs the terminator).
        if last == '}' and popped_kinds and popped_kinds[-1] == "block":
            result.append(rewritten)
            continue

        result.append(code + ';' + comment_part)

    return '\n'.join(result)


_BRACE_LITERAL_PRECEDERS = set("=,([:+-*/%<>!&|^~?")

# Lam keywords that introduce an expression context — a ``{`` that
# directly follows one of these is opening a dict / set literal, not
# a block. Without this list, ``return {"a": 1}`` would have its
# ``{`` classified as a block opener and the closing ``}`` would be
# treated as ending the function body.
_BRACE_LITERAL_KEYWORDS = frozenset({
    "return", "yield", "raise", "throw", "await", "in", "and", "or",
    "not", "is",
})


def _classify_brace_open(prev_meaningful: str, prefix: str) -> str:
    """Return ``"literal"`` or ``"block"`` for an opening ``{``.

    ``prev_meaningful`` is the most recent non-space char (already
    tracked by the line-walking helpers). ``prefix`` is the source
    text emitted up to the ``{`` — we use it to recognise an
    expression-starting keyword (``return``, ``yield`` …) so that
    ``return {"a": 1}`` is treated as an expression, not a block.

    A ``{`` opening a multi-line lambda body
    (``lambda (x: int) -> int { ... }``) is treated as a literal:
    even though the body is a true statement suite, the lambda
    itself is part of an expression so the matching ``}`` ends the
    enclosing statement and needs an auto-semicolon.
    """
    if prev_meaningful in _BRACE_LITERAL_PRECEDERS:
        return "literal"
    # Walk backwards over the prefix to extract the preceding word.
    j = len(prefix) - 1
    while j >= 0 and prefix[j].isspace():
        j -= 1
    end = j + 1
    while j >= 0 and (prefix[j].isalnum() or prefix[j] == "_"):
        j -= 1
    last_word = prefix[j + 1:end]
    if last_word in _BRACE_LITERAL_KEYWORDS:
        return "literal"
    if _prefix_opens_lambda_block(prefix):
        return "lambda_block"
    return "block"


_LAMBDA_BLOCK_HEAD_RE = _re.compile(
    r"\blambda\b"
    # Optional parameter list: either a paren-typed group (with one
    # level of inner nesting allowed for tuple types like ``list[int]``
    # or function types) or a bare comma-list of names.
    r"(?:"
    r"\s*\((?:[^()]|\([^)]*\))*\)"
    r"|\s+\w+(?:\s*,\s*\w+)*"
    r")?"
    # Optional return-type annotation: ``-> Type`` where ``Type`` is
    # any non-whitespace, non-brace text (handles ``int``, ``list[int]``,
    # ``dict[str, any]`` even though they contain commas — we keep
    # eating until whitespace or ``{``).
    r"(?:\s*->\s*[^\s{][^{]*?)?"
    r"\s*$"
)


def _prefix_opens_lambda_block(prefix: str) -> bool:
    """True when ``prefix`` is a lambda signature ending right before
    its block body's ``{``.

    The signature shape we accept matches the grammar:
      lambda                        -> nullary
      lambda <name|name,...>        -> bare-name params
      lambda (...)                  -> typed paren group
      lambda (...) -> Type          -> typed paren group + return anno
      lambda <bare> -> Type         -> bare params + return anno

    False positives are harmless — they just turn a regular block
    ``}`` into a literal ``}``, which still emits a valid
    (extra-``;``) program.
    """
    return bool(_LAMBDA_BLOCK_HEAD_RE.search(prefix))


def _collapse_runaway_semicolons(source: str) -> str:
    """Collapse runs of ``;`` (with optional whitespace) into one.

    Users should be free to terminate every statement explicitly if
    they want (``a: int = 1;``) without introducing parse errors
    through double / triple semicolons. The Lam grammar's
    ``simple_stmts: small_stmt (";" small_stmt)* [";"]`` allows at
    most one trailing ``;`` per statement list, so we normalise
    ``;;`` / ``;\\n;`` / ``; ;`` down to ``;``.

    String literals are respected so a ``";;"`` inside a string
    stays intact. Triple-quoted strings survive because
    :func:`_collapse_multiline_strings` already converted them to
    single-line form before this pass runs.
    """
    out: list[str] = []
    in_str: str | None = None
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if in_str is None:
            if ch in ('"', "'"):
                in_str = ch
                out.append(ch)
                i += 1
                continue
            # Skip line comments — don't touch ``;`` inside them.
            if ch == '#':
                end = source.find('\n', i)
                if end == -1:
                    out.append(source[i:])
                    return "".join(out)
                out.append(source[i:end])
                i = end
                continue
            if ch == ';':
                # Only collapse when ANOTHER ``;`` follows (possibly
                # separated by spaces / tabs / newlines). A bare
                # ``; `` on its own is valid Lam and must stay.
                j = i + 1
                while j < n and source[j] in ' \t\r\n':
                    j += 1
                if j < n and source[j] == ';':
                    # Consume the whole run, emit a single ``;``.
                    out.append(';')
                    i = j + 1
                    while i < n and source[i] in ' \t;':
                        # Keep advancing past extra ``;`` + trailing
                        # horizontal whitespace, but stop at newlines
                        # so line numbers survive.
                        i += 1
                    continue
                out.append(';')
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        else:
            out.append(ch)
            if ch == in_str and (i == 0 or source[i - 1] != '\\'):
                in_str = None
            i += 1
            continue
    return "".join(out)


def _fill_empty_blocks(source: str) -> str:
    """Insert ``pass;`` into block bodies that contain only whitespace.

    JS lets you write empty function / loop bodies as ``func foo() {}`` or
    ``while x {}``, but the Lam grammar's ``suite: "{" stmt+ "}"`` rule
    requires at least one statement. This pass detects every block-kind
    ``{`` whose matching ``}`` has only whitespace / newlines / comments
    in between and inserts ``pass;`` before the closing brace.

    Literal braces (dict / set literals — preceded by ``=``, ``,``, ``:``,
    operators, etc.) are left alone, so ``{}`` inside expressions still
    means "empty literal".

    Strings are respected so ``{`` / ``}`` inside string literals
    aren't classified as braces.
    """
    out: list[str] = []
    in_str: str | None = None
    # Stack of (open_offset_in_out, kind, has_content). When the
    # closing ``}`` arrives we look at ``has_content`` to decide
    # whether to inject ``pass;``.
    stack: list[list] = []
    prev_meaningful = ""
    i = 0
    while i < len(source):
        ch = source[i]
        if in_str is None:
            if ch in ('"', "'"):
                triple = source[i:i + 3]
                if triple in ('"""', "'''"):
                    in_str = triple
                    out.append(triple)
                    i += 3
                    if stack:
                        stack[-1][2] = True
                    prev_meaningful = '"'
                    continue
                in_str = ch
                out.append(ch)
                i += 1
                if stack:
                    stack[-1][2] = True
                prev_meaningful = '"'
                continue

            # Skip past line comments — they don't count as content.
            if ch == '#':
                # Read up to newline (or end-of-string).
                end = source.find('\n', i)
                if end == -1:
                    out.append(source[i:])
                    return "".join(out)
                out.append(source[i:end])
                i = end
                continue
        else:
            if len(in_str) == 3 and source[i:i + 3] == in_str:
                in_str = None
                out.append(source[i:i + 3])
                i += 3
                continue
            if len(in_str) == 1 and ch == in_str and (i == 0 or source[i - 1] != '\\'):
                in_str = None
            out.append(ch)
            i += 1
            continue

        if ch == '{':
            kind = _classify_brace_open(prev_meaningful, "".join(out))
            out.append(ch)
            # Record offset of the position right after this `{`.
            stack.append([len("".join(out)), kind, False])
        elif ch == '}':
            if stack:
                _, kind, has_content = stack.pop()
                if kind == "block" and not has_content:
                    # Strip trailing whitespace from the buffer so the
                    # injected ``pass;`` lands on the same line as the
                    # opening ``{`` (the auto-semicolon pass has
                    # already run, so we don't need to add another).
                    trail = ""
                    while out and out[-1] in " \t\r\n":
                        trail = out.pop() + trail
                    out.append(" pass;")
                    if trail:
                        out.append(trail)
            out.append(ch)
        else:
            out.append(ch)
            # Track whether the current block has any content beyond
            # whitespace / semicolons / comments.
            if not ch.isspace() and ch != ';':
                if stack:
                    stack[-1][2] = True

        if not ch.isspace():
            prev_meaningful = ch
        i += 1

    return "".join(out)


def _inline_block_semicolons(code: str) -> str:
    """Insert ``;`` before every ``}`` that closes a block whose body
    contains a statement on the same line.

    Examples (input → output)::

        func foo() { return 1 }            → func foo() { return 1; }
        if x { print(x); print(y) }        → if x { print(x); print(y); }
        m = {"a": 1, "b": 2}               → m = {"a": 1, "b": 2}      (literal — untouched)
        func bar() {}                      → func bar() {}              (empty — untouched)
        if a { foo() } else { bar() }      → if a { foo(); } else { bar(); }

    Strings are respected so ``{`` / ``}`` inside string literals are
    ignored. Brace classification (block vs literal) reuses the same
    rule as ``_walk_line``: a ``{`` whose preceding meaningful char is
    in ``_BRACE_LITERAL_PRECEDERS`` is a literal opener.
    """
    out: list[str] = []
    in_str: str | None = None
    # Stack of (kind, last_meaningful_char_inside_block) per opened
    # brace. We only insert ``;`` when the closing ``}`` belongs to a
    # block AND the most-recent meaningful char inside is non-empty
    # and isn't already a separator.
    stack: list[tuple[str, str]] = []
    prev_meaningful = ""
    i = 0
    while i < len(code):
        ch = code[i]
        if in_str is None:
            if ch in ('"', "'"):
                triple = code[i:i + 3]
                if triple in ('"""', "'''"):
                    in_str = triple
                    out.append(triple)
                    i += 3
                    if stack:
                        stack[-1] = (stack[-1][0], '"')
                    prev_meaningful = '"'
                    continue
                in_str = ch
                out.append(ch)
                i += 1
                if stack:
                    stack[-1] = (stack[-1][0], '"')
                prev_meaningful = '"'
                continue
        else:
            if len(in_str) == 3 and code[i:i + 3] == in_str:
                in_str = None
                out.append(code[i:i + 3])
                i += 3
                continue
            if len(in_str) == 1 and ch == in_str and (i == 0 or code[i - 1] != '\\'):
                in_str = None
            out.append(ch)
            i += 1
            continue

        if ch == '{':
            kind = _classify_brace_open(prev_meaningful, "".join(out))
            stack.append((kind, ""))
            out.append(ch)
        elif ch == '}':
            kind = ""
            inner = ""
            if stack:
                kind, inner = stack.pop()
            # Insert a ``;`` before this ``}`` if it closes a block
            # whose body has a statement that didn't already end in
            # one of ``; { } ,``.
            if kind in ("block", "lambda_block") and inner not in ("", ";", "{", "}", ",", ":"):
                # Trim trailing whitespace from the buffer first.
                # ``out`` may end with spaces between the last
                # meaningful char and the closing ``}``.
                trail = ""
                while out and out[-1] in " \t":
                    trail = out.pop() + trail
                out.append(";")
                if trail:
                    out.append(trail)
            out.append(ch)
        else:
            out.append(ch)

        if not ch.isspace():
            prev_meaningful = ch
            if stack and ch != '{':
                stack[-1] = (stack[-1][0], ch)
        i += 1

    return "".join(out)


def _walk_line(
    code: str,
    brace_stack: list[tuple[str, int]],
    paren_depth: int,
) -> tuple[list[str], int]:
    """Walk `code` left-to-right, updating the shared brace/paren state.

    Returns the list of brace contexts popped by `}` on this line (in
    order, as bare strings) and the new paren depth after the walk.
    ``brace_stack`` is mutated in place; each entry is
    ``(kind, saved_paren_depth)``. String literals are respected so
    that `{`, `}`, `(`, `[` inside strings are ignored.

    A `{` is classified as *literal* when the previous non-space
    character in the line (or the beginning of the line when the stack
    is empty of same-line entries) is in ``_BRACE_LITERAL_PRECEDERS``
    (operators, list/tuple openers, `=` / `:` / `,`). When the
    preceding text is a complete lambda signature the brace opens a
    ``lambda_block`` instead. Everything else (a keyword, an
    identifier, `)`, a prior `}`) is treated as a *block* opener.
    """
    popped: list[str] = []
    in_str: str | None = None
    prev_meaningful: str = ""
    i = 0
    while i < len(code):
        ch = code[i]
        if in_str is None:
            if ch in ('"', "'"):
                triple = code[i:i + 3]
                if triple in ('"""', "'''"):
                    in_str = triple
                    i += 3
                    continue
                in_str = ch
                i += 1
                if ch != " ":
                    prev_meaningful = ch
                continue
        else:
            if len(in_str) == 3 and code[i:i + 3] == in_str:
                in_str = None
                i += 3
                continue
            if len(in_str) == 1 and ch == in_str and (i == 0 or code[i - 1] != '\\'):
                in_str = None
            i += 1
            continue

        if ch == '{':
            kind = _classify_brace_open(prev_meaningful, code[:i])
            if kind == "lambda_block":
                # Save and reset paren_depth so statements inside the
                # lambda body get auto-semicoloned even when the
                # lambda call itself is mid-expression
                # (``foo(lambda(){ stmt; stmt })``).
                brace_stack.append((kind, paren_depth))
                paren_depth = 0
            else:
                brace_stack.append((kind, paren_depth))
        elif ch == '}':
            if brace_stack:
                kind, saved = brace_stack.pop()
                popped.append(kind)
                if kind == "lambda_block":
                    paren_depth = saved
        elif ch == '(' or ch == '[':
            paren_depth += 1
        elif ch == ')' or ch == ']':
            if paren_depth > 0:
                paren_depth -= 1

        if not ch.isspace():
            prev_meaningful = ch
        i += 1

    return popped, paren_depth


def _check_triple_quotes(line: str) -> str | None:
    """If line opens a triple-quoted string that doesn't close, return the delimiter."""
    count_dq = line.count('"""')
    count_sq = line.count("'''")
    # Odd count means unclosed
    if count_dq % 2 == 1:
        return '"""'
    if count_sq % 2 == 1:
        return "'''"
    return None


def _strip_inline_comment(line: str) -> str:
    """Remove inline comment from a line, respecting strings."""
    in_str = None
    i = 0
    while i < len(line):
        c = line[i]
        if in_str is None:
            if c == '#':
                return line[:i]
            if c in ('"', "'"):
                triple = line[i:i+3]
                if triple in ('"""', "'''"):
                    in_str = triple
                    i += 3
                    continue
                in_str = c
        else:
            if len(in_str) == 3 and line[i:i+3] == in_str:
                in_str = None
                i += 3
                continue
            elif len(in_str) == 1 and c == in_str and (i == 0 or line[i-1] != '\\'):
                in_str = None
        i += 1
    return line


@dataclass(frozen=True)
class ParsePreprocessResult:
    source: str
    go_blocks: dict[str, str]
    source_map: SourceMap


def preprocess_for_parse(source: str) -> ParsePreprocessResult:
    """Run the full Lammergeier parse-preprocessing pipeline.

    CLI and LSP callers must use this function before feeding source to
    Lark. The source map is currently line-identity over the original
    input; later phases can make individual transforms map-aware without
    changing the call sites.
    """
    rewritten = apply_lammergeier_aliases(source)
    preprocessed, go_blocks = preprocess_go_blocks(rewritten)
    preprocessed = _strip_doc_comments_preserve_lines(preprocessed)
    preprocessed = _collapse_multiline_strings(preprocessed)
    preprocessed = expand_dict_destructure(preprocessed)
    preprocessed = _expand_single_statement_blocks(preprocessed)
    preprocessed = auto_semicolons(preprocessed)
    preprocessed = _collapse_runaway_semicolons(preprocessed)
    preprocessed = _fill_empty_blocks(preprocessed)
    return ParsePreprocessResult(
        source=preprocessed,
        go_blocks=go_blocks,
        source_map=SourceMap.identity(len(source.splitlines()) or 1),
    )


def _strip_doc_comments_preserve_lines(source: str) -> str:
    """Strip ``#- ... -#`` comments without shifting later line numbers."""
    return _re.sub(
        r'#-[\s\S]*?-#',
        lambda m: "\n" * m.group(0).count("\n"),
        source,
    )


_PARSER_CACHE: "Lark | None" = None


def create_parser() -> Lark:
    """Build (or load) the Lark parser for the Lammergeier grammar.

    Cold-start a fresh ``Lark(...)`` for every build was ~150 – 300 ms
    of pure grammar-compile work — which dwarfs the per-file transpile
    cost on the small libraries that make up the stdlib. To avoid
    re-paying that cost on every invocation we:

    1. Memoise the instance for the lifetime of the process — repeat
       calls inside the same build (one per library, one for the main
       file) return the same object. Lark parsers are stateless after
       construction, so this is safe across threads.
    2. Persist a serialised form of the parser to the on-disk cache,
       keyed on a digest of (grammar text, Lark major version,
       Python major.minor). A change to any of those rolls the key
       and forces a rebuild. The serialised parser typically loads
       in <10 ms even on cold disk.

    The persistent cache lives next to the library cache so a single
    ``lamc --clear-cache`` wipes both.
    """
    global _PARSER_CACHE
    if _LARK_IMPORT_ERROR is not None or Lark is None:
        print(
            f"[lamc] missing Python dependency: lark ({_LARK_IMPORT_ERROR})",
            file=sys.stderr,
        )
        print("[lamc] install it with: python3 -m pip install lark",
              file=sys.stderr)
        sys.exit(1)
    if _PARSER_CACHE is not None:
        return _PARSER_CACHE

    grammar_path = PROJECT_ROOT / "lammergeier.lark"
    grammar_bytes = grammar_path.read_bytes()

    # Cache key salts on the grammar bytes plus the Lark + Python
    # versions, since serialised parsers aren't portable across them.
    import hashlib as _hashlib
    salt = (
        f"lark={_lark.__version__};"
        f"py={sys.version_info.major}.{sys.version_info.minor};"
    ).encode("ascii")
    digest = _hashlib.sha256(salt + grammar_bytes).hexdigest()

    cache_path = _lamcache.cache_dir() / "parsers" / f"{digest}.bin"
    if cache_path.is_file():
        try:
            with cache_path.open("rb") as f:
                _PARSER_CACHE = Lark.load(f)
                return _PARSER_CACHE
        except Exception:
            # Stale or unreadable cache → fall through and rebuild.
            try:
                cache_path.unlink()
            except OSError:
                pass

    # Cache miss / fallback: build from source and persist.
    parser = Lark(
        grammar_bytes.decode("utf-8"),
        parser="lalr",
        start="file_input",
        propagate_positions=True,
    )
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write so a crash mid-save can't leave a half-written
        # blob that would later fail Lark.load() and trip the
        # exception path above on every subsequent build.
        tmp_path = cache_path.with_suffix(".tmp")
        with tmp_path.open("wb") as f:
            parser.save(f)
        os.replace(tmp_path, cache_path)
    except Exception:
        # Disk caching is best-effort — never fail the build because
        # the parser cache couldn't be written.
        pass

    _PARSER_CACHE = parser
    return parser


def _collapse_multiline_strings(source: str) -> str:
    """Replace multiline triple-quoted strings with single-line equivalents."""
    result = []
    i = 0
    while i < len(source):
        # Check for triple-quote delimiters
        matched = False
        for delim in ('f"""', "f'''", '"""', "'''"):
            if source[i:i+len(delim)] == delim:
                quote = delim[-3:]  # """ or '''
                prefix = delim[:-3]  # f or empty
                end = source.find(quote, i + len(delim))
                if end == -1:
                    break  # unclosed
                content = source[i+len(delim):end]
                # Check if actually multiline
                if '\n' not in content:
                    # Single line triple-quoted — leave as-is but use single quotes
                    result.append(source[i:end+3])
                else:
                    # Replace newlines with \n escape sequence
                    sq = quote[0]
                    content = content.replace('\r\n', '\n').replace('\r', '\n')
                    content = content.replace('\\', '\\\\')
                    content = content.replace('\n', '\\n')
                    content = content.replace(sq, '\\' + sq)
                    result.append(f'{prefix}{sq}{content}{sq}')
                i = end + 3
                matched = True
                break
        if not matched:
            result.append(source[i])
            i += 1
    return ''.join(result)


_BLOCK_KEYWORDS = {'if', 'elif', 'else', 'for', 'while'}

def _expand_single_statement_blocks(source: str) -> str:
    """Expand single-statement blocks into braced blocks.
    
    Detects lines like:
        if x > 0 print("yes")
        for i in range(10) print(i)
        else print("no")
    
    And transforms them to:
        if x > 0 { print("yes") }
        for i in range(10) { print(i) }
        else { print("no") }
    
    Only applies when the line doesn't already end with '{' or '}'.
    """
    lines = source.split('\n')
    result = []
    
    for line in lines:
        stripped = line.rstrip()
        if not stripped or stripped.lstrip().startswith('#'):
            result.append(stripped)
            continue
        
        code = stripped.lstrip()
        indent = stripped[:len(stripped) - len(code)]
        
        # Check if this is a block keyword line that doesn't use braces
        first_word = code.split()[0] if code.split() else ""
        
        if first_word not in _BLOCK_KEYWORDS:
            result.append(stripped)
            continue
        
        # Skip if line already ends with { or } or : (legacy go!) 
        if code.endswith('{') or code.endswith('}') or code.endswith(':'):
            result.append(stripped)
            continue
        
        # Now determine where the condition ends and the statement begins
        # For 'else': everything after 'else' is the statement
        # For 'elif'/'if': condition is up to the part where a statement keyword starts
        # For 'for': condition is "for VAR in EXPR", then statement
        # For 'while': condition is "while EXPR", then statement
        
        if first_word == 'else':
            rest = code[4:].strip()
            if rest and rest != '{':
                result.append(f'{indent}else {{')
                result.append(f'{indent}    {rest}')
                result.append(f'{indent}}}')
                continue
        
        elif first_word == 'if' or first_word == 'elif':
            # Find where the condition ends — heuristic: look for a known
            # statement keyword (print, return, break, continue, pass, var assignment, etc.)
            # after the condition. We scan for balanced parens/brackets.
            cond_and_body = code[len(first_word):].strip()
            stmt_start = _find_statement_start(cond_and_body)
            if stmt_start is not None:
                cond = cond_and_body[:stmt_start].strip()
                body = cond_and_body[stmt_start:].strip()
                result.append(f'{indent}{first_word} {cond} {{')
                result.append(f'{indent}    {body}')
                result.append(f'{indent}}}')
                continue
        
        elif first_word == 'while':
            cond_and_body = code[5:].strip()
            stmt_start = _find_statement_start(cond_and_body)
            if stmt_start is not None:
                cond = cond_and_body[:stmt_start].strip()
                body = cond_and_body[stmt_start:].strip()
                result.append(f'{indent}while {cond} {{')
                result.append(f'{indent}    {body}')
                result.append(f'{indent}}}')
                continue
        
        elif first_word == 'for':
            # "for VAR in EXPR STMT" — find "in" first, then find statement start after expr
            in_idx = code.find(' in ')
            if in_idx > 0:
                after_in = code[in_idx + 4:].strip()
                stmt_start = _find_statement_start(after_in)
                if stmt_start is not None:
                    expr = after_in[:stmt_start].strip()
                    body = after_in[stmt_start:].strip()
                    var_part = code[3:in_idx].strip()
                    result.append(f'{indent}for {var_part} in {expr} {{')
                    result.append(f'{indent}    {body}')
                    result.append(f'{indent}}}')
                    continue
        
        result.append(stripped)
    
    return '\n'.join(result)


_STMT_STARTERS = {'print', 'return', 'break', 'continue', 'pass', 'yield', 'del', 'assert', 'raise'}

def _find_statement_start(text: str) -> int | None:
    """Find where a statement begins in text (after a condition).
    
    Scans the text for known statement-starting keywords/patterns,
    skipping over balanced parens/brackets/strings.
    """
    i = 0
    depth = 0  # paren/bracket depth
    in_str = None
    
    while i < len(text):
        c = text[i]
        
        # Track string literals
        if in_str:
            if c == in_str and (i == 0 or text[i-1] != '\\'):
                in_str = None
            i += 1
            continue
        if c in ('"', "'"):
            in_str = c
            i += 1
            continue
        
        # Track paren/bracket depth
        if c in ('(', '['):
            depth += 1
        elif c in (')', ']'):
            depth -= 1
        
        # Only look for statement starts at depth 0
        if depth == 0 and c == ' ':
            rest = text[i+1:]
            for kw in _STMT_STARTERS:
                if rest.startswith(kw) and (len(rest) == len(kw) or rest[len(kw)] in ' ('):
                    return i + 1
            # Also check for assignment: identifier followed by = or :
            # or function call: identifier(
            word_match = _re.match(r'([a-zA-Z_]\w*)\s*[\(=:]', rest)
            if word_match and word_match.group(1) not in ('in', 'and', 'or', 'not', 'is', 'true', 'false'):
                return i + 1
        
        i += 1
    
    return None


# ── Go-module pin helpers (compile-time) ─────────────────────

def _find_project_manifest(source_dir: Path) -> Optional[Path]:
    """Walk upward from ``source_dir`` looking for the nearest
    ``lamlib.toml``. Returns the path or ``None`` if no manifest sits
    within six levels of the source. The depth bound matches the
    other manifest-discovery helpers in this file and prevents a
    standalone script in ``/tmp`` from accidentally adopting an
    unrelated grandparent's manifest.
    """
    here = source_dir.resolve()
    for _ in range(6):
        cand = here / "lamlib.toml"
        if cand.exists():
            return cand
        parent = here.parent
        if parent == here:
            break
        here = parent
    return None


_USER_IMPORT_RE = _re.compile(
    r"^\s*(?:from\s+(?P<from_mod>@?[\w./@-]+)\s+import\b"
    r"|import\s+(?P<imp_mod>@?[\w./@-]+))",
    _re.MULTILINE,
)


def _scan_project_imports(project_root: Path) -> set[str]:
    """Return the set of *module* names imported by user code under
    ``project_root``.

    Walks every ``.lam`` file in the tree, with ``extlibs/``,
    ``.git/``, ``build/``, and ``__pycache__`` pruned
    so we never count an installed library's own imports against
    the user's manifest. We use a tolerant regex over raw source
    rather than a full parse — the manifest-vs-imports comparison is
    a *warning* surface, not a correctness check, so a file that
    fails to parse mid-edit shouldn't blank the diagnostic.

    Module-name normalisation matches the resolver: ``from
    lamwebp.codec import Decoder`` contributes both ``lamwebp.codec``
    and the unqualified root ``lamwebp`` so a manifest declaring
    ``lamwebp = "^1.0"`` counts as referenced.
    """
    seen: set[str] = set()
    skip_dirs = {"extlibs", ".git", "build", "__pycache__", "node_modules"}
    for path in project_root.rglob("*"):
        if path.is_dir():
            continue
        if path.suffix != ".lam":
            continue
        # Prune anything under a skipped directory at any depth.
        if any(part in skip_dirs for part in path.relative_to(project_root).parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _USER_IMPORT_RE.finditer(text):
            mod = m.group("from_mod") or m.group("imp_mod")
            if not mod:
                continue
            seen.add(mod)
            if "." in mod and not mod.startswith("@"):
                seen.add(mod.split(".", 1)[0])
    return seen


def _emit_unused_manifest_dep_warnings(source_dir: Path) -> None:
    """If a project ``lamlib.toml`` declares a dependency that no
    ``.lam`` file in the project actually imports, print a Phase-2
    warning. This is the project-level analog of the per-file
    "unused import" warning emitted by the semantic checker — it
    nudges users towards a clean manifest without forcing a tidy
    pass. The check is skipped silently when no manifest is found.
    """
    mf_path = _find_project_manifest(source_dir)
    if mf_path is None:
        return
    try:
        from compiler.manifest import Manifest, ManifestError
        mf = Manifest.load(mf_path)
    except (ImportError, ManifestError, OSError):
        return
    if not mf.dependencies:
        return
    used = _scan_project_imports(mf_path.parent)
    unused = sorted(name for name in mf.dependencies if name not in used)
    if not unused:
        return
    plural = "ies" if len(unused) != 1 else "y"
    print(
        f"warning: {len(unused)} declared manifest dependenc{plural} "
        f"not imported anywhere under {mf_path.parent}:",
        file=sys.stderr,
    )
    for name in unused:
        print(
            f"  - {name}: declared in {mf_path.name} but no "
            f"`from {name} import …` / `import {name}` in the project tree",
            file=sys.stderr,
        )
    print(
        f"  (run `lamc uninstall {unused[0]}` to drop it, or import it "
        f"to silence this warning)",
        file=sys.stderr,
    )


def _collect_go_pins(
    source_dir: Path,
    stdlib_modules: set[str] | None = None,
) -> dict[str, str]:
    """Collect ``{go_module_path: version}`` pins for the project.

    Three layers contribute, in increasing precedence:

    1. **Stdlib pins** — the ``STDLIB_GO_PINS`` table shipped with
       the compiler. These are the versions the Lam stdlib was
       tested against; they stop ``go mod tidy`` from silently
       upgrading a stdlib-linked Go module to a new major between
       builds. When callers provide the resolved stdlib import set,
       only pins owned by those modules are seeded so core imports do
       not spread heavy domain dependencies.
    2. **Project manifest** — ``[go-deps]`` / ``[go.dependencies]``
       in the nearest ``lamlib.toml`` walking up from
       ``source_dir``. Users override a stdlib pin here when they
       need a newer version.
    3. **Lockfile** — ``[go_pins.*]`` entries in
       ``lamlib.lock.toml``, the post-resolution view produced by
       ``lamc install``. Wins over everything because it encodes
       the conflict-resolved decisions across the whole transitive
       library graph.

    Returns a dict that's safe to iterate even on a standalone script
    with no ``lamlib.toml``. Passing ``stdlib_modules`` filters the
    bundled stdlib defaults to just the modules actually used by the
    current build; project and lockfile pins are always merged.
    """
    try:
        from compiler.manifest import Manifest, ManifestError, _parse_toml
    except ImportError:
        return {}
    try:
        from compiler.stdlib_go_deps import STDLIB_GO_PINS, STDLIB_GO_PIN_MODULES
    except ImportError:
        STDLIB_GO_PINS = {}
        STDLIB_GO_PIN_MODULES = {}

    here = source_dir.resolve()
    manifest_path = None
    lock_path = None
    # Walk up at most a few levels — projects rarely nest deeper than
    # ``src/foo/bar`` and we don't want to silently pick up an
    # unrelated grandparent's manifest.
    for _ in range(6):
        cand_mf = here / "lamlib.toml"
        cand_lock = here / "lamlib.lock.toml"
        if manifest_path is None and cand_mf.exists():
            manifest_path = cand_mf
        if lock_path is None and cand_lock.exists():
            lock_path = cand_lock
        if manifest_path or lock_path:
            break
        parent = here.parent
        if parent == here:
            break
        here = parent

    # Seed with stdlib defaults. Compiler builds pass the resolved
    # stdlib import graph so a core import like ``lamstrings`` does
    # not drag unrelated Redis/DB/DataFrame pins into ``go.mod``.
    if stdlib_modules is None:
        pins: dict[str, str] = dict(STDLIB_GO_PINS)
    else:
        selected = set(stdlib_modules)
        pins = {
            path: ver
            for path, ver in STDLIB_GO_PINS.items()
            if selected.intersection(STDLIB_GO_PIN_MODULES.get(path, ()))
        }
    if manifest_path is not None:
        try:
            mf = Manifest.load(manifest_path)
        except ManifestError:
            mf = None
        if mf is not None:
            pins.update(mf.go_deps)
    if lock_path is not None:
        try:
            tree = _parse_toml(lock_path.read_text(encoding="utf-8"))
        except (ManifestError, OSError):
            tree = {}
        for entry in (tree.get("go_pins") or {}).values():
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            ver  = entry.get("version")
            if isinstance(path, str) and isinstance(ver, str):
                pins[path] = ver  # lockfile wins
    return pins


def _effective_extlibs_dirs(
    source_dir: Path,
    extlibs_paths: list[str] | None,
) -> list[Path]:
    """Return the Lam library search path for installed third-party libs."""
    extlibs_dirs: list[Path] = []
    seen_extlibs: set[Path] = set()

    def _push_extlibs(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen_extlibs or not resolved.is_dir():
            return
        seen_extlibs.add(resolved)
        extlibs_dirs.append(resolved)

    for p in (extlibs_paths or []):
        _push_extlibs(Path(p))
    env_extlibs = os.environ.get("LAMC_EXTLIBS")
    if env_extlibs:
        for seg in env_extlibs.split(os.pathsep):
            if seg:
                _push_extlibs(Path(seg))
    _push_extlibs(source_dir / "extlibs")
    _push_extlibs(Path.home() / ".lammergeier" / "extlibs")
    return extlibs_dirs


def _inject_go_requires(go_mod_path: Path,
                        pins: dict[str, str]) -> None:
    """Append ``require`` directives to a freshly-initialised
    ``go.mod`` for every entry in ``pins``.

    We don't try to reproduce the full ``go mod`` grammar — we just
    add a ``require ( … )`` block at the bottom; the subsequent
    ``go mod tidy`` run will canonicalise the file shape. The aim
    is purely to seed ``tidy`` with our preferred versions so MVS
    promotes them rather than picking the latest tag at random."""
    if not pins:
        return
    text = go_mod_path.read_text(encoding="utf-8")
    block = ["", "require ("]
    for path in sorted(pins):
        block.append(f"\t{path} {pins[path]}")
    block.append(")")
    block.append("")
    go_mod_path.write_text(text.rstrip() + "\n" + "\n".join(block),
                           encoding="utf-8")


def compile_lam(
    source_path: str,
    output_path: str | None = None,
    emit_go: bool = False,
    emit_ast: bool = False,
    transpile_only: bool = False,
    run: bool = False,
    verbose: bool = False,
    go_module: str = "lamc/app",
    go_extra_flags: list[str] | None = None,
    go_ldflags: str | None = None,
    go_tags: str | None = None,
    go_gcflags: str | None = None,
    go_race: bool = False,
    go_trimpath: bool = False,
    keep_go: bool = False,
    stdlib_path: str | None = None,
    extlibs_paths: list[str] | None = None,
    use_cache: bool = True,
    no_semantic_check: bool = False,
):
    """Full compilation pipeline.

    ``extlibs_paths`` (optional) lists directories to consult for
    third-party Lam libraries, in addition to the stdlib (``lib/``
    under the compiler root) and project-local sources. The effective
    search order is::

        stdlib  →  extlibs  →  project directories

    so users cannot accidentally shadow stdlib modules by dropping a
    same-named file in their project tree. Inside the extlibs layer
    the priorities are:

      1. ``extlibs_paths`` (this argument), left-to-right
      2. ``LAMC_EXTLIBS`` env var, colon-separated
      3. ``<source_dir>/extlibs`` (per-project vendored deps)
      4. ``~/.lammergeier/extlibs`` (user-global install)
    """
    source_file = Path(source_path).resolve()
    if not source_file.exists():
        print(f"error: file not found: {source_path}", file=sys.stderr)
        sys.exit(1)
    source_dir = source_file.parent
    extlibs_dirs = _effective_extlibs_dirs(source_dir, extlibs_paths)
    stdlib_dir = Path(stdlib_path).resolve() if stdlib_path else PROJECT_ROOT / "lib"
    module_index = WorkspaceIndex(
        source_dir,
        stdlib_dir=stdlib_dir,
        extlibs_dirs=extlibs_dirs,
    )

    source = source_file.read_text(encoding="utf-8")

    # ── LAMMERGEIER.* compiler-alias rewrite ──
    # Lower every recognised compiler-emitted alias (``Result.Ok`` →
    # ``Result_Ok``, ``Error`` → ``NewError``, ``None`` / ``nil`` →
    # ``nil``) to its Go-side identifier so go! blocks get the
    # rewritten symbol when their content is captured below.
    #
    # The typo guard runs LATER, after the parser produced the AST
    # and the transpiler has collected user function / class names,
    # so ``LAMMERGEIER.<userFunction>`` references are recognised as
    # valid (and resolve to the Go-mangled identifier at go-block
    # emission time). See the deferred check after
    # ``_collect_function_names`` below.
    source = apply_lammergeier_aliases(source)

    parse_input = preprocess_for_parse(source)
    preprocessed = parse_input.source
    go_blocks = parse_input.go_blocks

    if verbose:
        print(f"[lamc] go! blocks found: {len(go_blocks)}", file=sys.stderr)

    # ── Parse ──
    parser = create_parser()
    try:
        tree = parser.parse(preprocessed)
    except UnexpectedInput as e:
        print(render_syntax_error(e, source, source_path), file=sys.stderr)
        sys.exit(1)

    if emit_ast:
        print(tree.pretty())
        return

    # ── Semantic check ──
    # Catch undefined names, duplicate class members, misplaced flow
    # statements, and lint-class issues (unused imports / unused
    # parameters) before the Go transpile runs. Errors here are
    # reported with Lam-source snippets and abort the build; warnings
    # are printed but the build still proceeds (Go-style "warn don't
    # error" semantics for advisory diagnostics). The check is pure-
    # AST so it adds <50ms even on the largest .lam.
    if not no_semantic_check:
        from compiler import semantic as _semantic
        sem_diags = _semantic.check_source(
            tree,
            module_index=module_index,
            source_path=source_file,
        )
        warning_block = _semantic.render_warnings(sem_diags, source, str(source_path))
        if warning_block:
            print(warning_block, file=sys.stderr)
        if _semantic.has_errors(sem_diags):
            print(_semantic.render_errors(sem_diags, source, str(source_path)),
                  file=sys.stderr)
            sys.exit(1)
        # Project-level: declared-but-unused manifest dependencies.
        # Operates on the regex-scanned import graph of every ``.lam``
        # under the project root, so we don't false-positive on
        # multi-file projects where the main file doesn't import every
        # declared dep itself. No-ops when there's no ``lamlib.toml``.
        _emit_unused_manifest_dep_warnings(source_file.parent)

    # ── Transpile ──
    # Pre-scan: collect imports from the parsed tree to resolve libs first
    _pre_transpiler = GoTranspiler(go_blocks=go_blocks, stdlib_path=stdlib_path, go_module_name=go_module)
    _pre_transpiler._collect_function_names(tree)
    _pre_transpiler._collect_class_fields(tree)


    def _extract_import_sites(node) -> list[tuple[str, Tree]]:
        """Walk ``node`` and return every imported module with its AST node.

        Used both for the user's main file and for transitive
        resolution: a library that does ``from lamerrors import ...``
        triggers a second pass on ``lamerrors`` itself.
        """
        out: list[tuple[str, Tree]] = []

        def _walk(n):
            if not isinstance(n, Tree):
                return
            if n.data == "import_from":
                for child in n.children:
                    if isinstance(child, Tree) and child.data == "dotted_name":
                        parts: list[str] = []
                        for c in child.children:
                            if isinstance(c, Tree):
                                parts.append(str(c.children[0]))
                            else:
                                parts.append(str(c))
                        out.append((".".join(parts), n))
                        break
                    # ``scoped_name`` — ``from @alice/lamwebp import ...``.
                    # One child, a single SCOPED_NAME token.
                    if isinstance(child, Tree) and child.data == "scoped_name":
                        if child.children:
                            out.append((str(child.children[0]), n))
                        break
            elif n.data == "import_name":
                for child in n.children:
                    if isinstance(child, Tree) and child.data == "dotted_as_names":
                        for das in child.children:
                            if isinstance(das, Tree) and das.data == "dotted_as_name":
                                parts = []
                                dn = das.children[0]
                                for c in dn.children:
                                    if isinstance(c, Tree):
                                        parts.append(str(c.children[0]))
                                    else:
                                        parts.append(str(c))
                                out.append((".".join(parts), n))
            for child in n.children:
                if isinstance(child, Tree):
                    _walk(child)

        _walk(node)
        return out

    def _extract_imports(node) -> list[str]:
        return [mod for mod, _ in _extract_import_sites(node)]

    _direct_import_sites = _extract_import_sites(tree)
    _pre_transpiler._lam_imports.extend(mod for mod, _ in _direct_import_sites)

    # ── Auto-inject ``lamstrings`` when the source uses any of
    #    the built-in string-method dispatch names ──
    #
    # The string-method dispatcher in ``compiler/visitors/expressions.py``
    # lowers ``"hello".toUpper()`` to a call into the ``lamstrings``
    # library (``Strings_toUpper(...)``) so the runtime behaviour
    # comes from a single place — the standard library — rather
    # than from inlined ``strings.X`` calls scattered through the
    # transpiler. For that to link, ``lamstrings`` has to be in
    # the import worklist below.
    #
    # The detection is purely textual on the *preprocessed* source
    # (go! blocks already replaced with markers, comments
    # stripped). False positives (e.g. a user class that exposes
    # a method called ``contains``) are harmless: the dispatcher
    # itself checks the receiver type and routes user-instance
    # calls to the user method, so the bundled ``Strings_*``
    # functions just become unused dead code that Go's linker
    # discards. The point of the auto-inject is to avoid forcing
    # users to write ``from lamstrings import Strings`` for the
    # syntactic sugar they expect to "just work".
    _STRING_METHOD_DISPATCH_NAMES = (
        "repeat", "contains", "hasPrefix", "hasSuffix",
        "toUpper", "toLower", "trim", "trimLeft", "trimRight",
        "replace", "split", "join", "count", "index", "lastIndex",
        "title", "equalFold", "fields", "capitalize", "isAlpha",
        "isDigit", "isAlnum", "isSpace", "reverse", "center",
        "zfill", "padLeft", "padRight", "splitLines", "splitN",
        "replaceFirst", "startsWith", "endsWith", "containsAny",
        "isEmpty", "isBlank", "indent", "dedent", "format",
    )
    _lam_string_method_re = _re.compile(
        r"\.(?:" + "|".join(_STRING_METHOD_DISPATCH_NAMES) + r")\s*\("
    )
    if (
        "lamstrings" not in _pre_transpiler._lam_imports
        and _lam_string_method_re.search(preprocessed)
    ):
        _pre_transpiler._lam_imports.append("lamstrings")

    # ── Resolve custom library imports (before main transpile) ──
    lib_sources = {}  # {module_name: go_source}
    lib_class_names = set()
    lib_static_methods = {}  # {class_name: {method_name, ...}}
    lib_static_vars = {}     # {class_name: {var_name: is_private, ...}}

    # Overall resolution order: stdlib first (immutable, so users
    # can't accidentally shadow core modules), extlibs next, then
    # project-local directories as the fallback.
    lib_dirs = [stdlib_dir, *extlibs_dirs, source_dir, source_dir / "lib"]

    def _resolve_lib_path(mod_name: str, from_file: Path = source_file):
        """Locate a Lam module through the shared workspace index.

        Accepts two shapes per directory: a flat ``<mod>.lam`` file,
        or a package directory ``<mod>/__init__.lam``. The flat form
        wins when both exist.
        """
        return module_index.resolve_module(from_file, mod_name)

    def _node_loc(node) -> tuple[int, int]:
        meta = getattr(node, "meta", None)
        if meta is not None and not getattr(meta, "empty", True):
            return int(getattr(meta, "line", 1) or 1), int(getattr(meta, "column", 1) or 1)
        return 1, 1

    def _format_missing_module_error(mod_name: str, node) -> str:
        from difflib import get_close_matches as _get_close_matches

        line, col = _node_loc(node)
        src_lines = source.split("\n")
        src_line = src_lines[line - 1] if 1 <= line <= len(src_lines) else ""
        known_modules = sorted(
            p.stem
            for d in lib_dirs
            if d.exists()
            for p in d.glob("*.lam")
        )
        suggestion = _get_close_matches(mod_name, known_modules, n=1, cutoff=0.72)
        out = [
            f"error: import resolution failed for {source_path}",
            f"  line {line}: module `{mod_name}` could not be found",
        ]
        if src_line:
            out.append(f"    >>> {line:4d} | {src_line}")
            out.append(f"        {' ' * max(0, col - 1)}^")
        if suggestion:
            out.append(f"  help: did you mean `{suggestion[0]}`?")
        out.append(
            f"  help: Lammergeier looked for `{mod_name}.lam` or "
            f"`{mod_name}/__init__.lam`."
        )
        out.append("  searched:")
        for d in lib_dirs:
            out.append(f"    - {d}")
        return "\n".join(out)

    def _from_import_symbols(node) -> list[tuple[str, str, Tree]]:
        if not isinstance(node, Tree) or node.data != "import_from":
            return []
        out: list[tuple[str, str, Tree]] = []
        for child in node.children:
            if isinstance(child, Tree) and child.data == "import_as_names":
                for item in child.children:
                    if not isinstance(item, Tree) or item.data != "import_as_name":
                        continue
                    names = [c for c in item.children
                             if isinstance(c, Tree) and c.data == "name"]
                    if not names:
                        continue
                    requested = str(names[0].children[0]) if names[0].children else ""
                    local = requested
                    if len(names) > 1 and names[1].children:
                        local = str(names[1].children[0])
                    if requested:
                        out.append((requested, local, item))
        return out

    def _parse_lam_module(mod_file: Path):
        try:
            sub_source = mod_file.read_bytes().decode("utf-8")
            module_index.update_file(mod_file, sub_source)
            sub_pre = preprocess_for_parse(sub_source).source
            return create_parser().parse(sub_pre + "\n")
        except Exception:
            return None

    def _module_export_names(mod_file: Path) -> set[str]:
        facts = module_index.facts_by_path.get(mod_file.resolve())
        if facts is None:
            facts = module_index.update_file(mod_file)
        return set(facts.exports)

    def _format_missing_import_symbol_error(
        mod_name: str,
        symbol: str,
        local_name: str,
        node,
        exports: set[str],
    ) -> str:
        from difflib import get_close_matches as _get_close_matches

        line, col = _node_loc(node)
        src_lines = source.split("\n")
        src_line = src_lines[line - 1] if 1 <= line <= len(src_lines) else ""
        suggestion = _get_close_matches(symbol, sorted(exports), n=1, cutoff=0.72)
        alias_note = f" as `{local_name}`" if local_name != symbol else ""
        out = [
            f"error: import resolution failed for {source_path}",
            f"  line {line}: module `{mod_name}` does not export `{symbol}`{alias_note}",
        ]
        if src_line:
            out.append(f"    >>> {line:4d} | {src_line}")
            out.append(f"        {' ' * max(0, col - 1)}^")
        if suggestion:
            out.append(f"  help: did you mean `{suggestion[0]}`?")
        if exports:
            out.append(f"  exported by `{mod_name}`:")
            for name in sorted(exports):
                out.append(f"    - {name}")
        else:
            out.append(f"  help: `{mod_name}` has no exported Lam symbols.")
        return "\n".join(out)

    direct_module_files: dict[str, Path] = {}
    for mod_name, import_node in _direct_import_sites:
        mod_file = direct_module_files.get(mod_name)
        if mod_file is None:
            mod_file = _resolve_lib_path(mod_name, source_file)
            if mod_file is None:
                print(_format_missing_module_error(mod_name, import_node), file=sys.stderr)
                sys.exit(1)
            direct_module_files[mod_name] = mod_file
        if _from_import_symbols(import_node):
            _parse_lam_module(mod_file)

    for mod_name, import_node in _direct_import_sites:
        symbols = _from_import_symbols(import_node)
        if not symbols:
            continue
        exports = _module_export_names(direct_module_files[mod_name])
        for requested, local_name, symbol_node in symbols:
            if requested not in exports:
                print(
                    _format_missing_import_symbol_error(
                        mod_name, requested, local_name, symbol_node, exports,
                    ),
                    file=sys.stderr,
                )
                sys.exit(1)

    # Resolve the user's direct imports, then walk transitively: any
    # library that itself imports another library (e.g. ``lamconv``
    # imports ``lamerrors`` so its ``try*`` methods can return
    # ``Result``) must also be bundled. The worklist keeps going until
    # we reach a fixed point.
    #
    # While we're already parsing each library's tree just to discover
    # its imports, we also harvest its class names + static-method
    # buckets into ``lib_pre_class_names`` / ``lib_pre_static_methods``
    # / ``lib_pre_static_vars``.
    # Those merged sets are injected into every per-library
    # transpiler before transpilation runs so that one library calling
    # another library's static methods (e.g. ``Strings.toUpper`` from
    # a custom helper module that imports ``lamstrings``) resolves
    # correctly. Without this, the per-library transpiler only knows
    # the classes defined in its own file and silently emits the bare
    # ``Strings.ToUpper`` Go expression that won't compile.
    lib_mod_files = {}  # {mod_name: Path}
    lib_pre_class_names: set[str] = set()
    lib_pre_static_methods: dict[str, set[str]] = {}
    lib_pre_static_vars: dict[str, dict[str, bool]] = {}
    seen: set[str] = set()
    worklist: list[tuple[str, Path]] = [(mod, source_file) for mod in _pre_transpiler._lam_imports]
    while worklist:
        mod_name, from_file = worklist.pop()
        if mod_name in seen:
            continue
        seen.add(mod_name)
        mod_file = _resolve_lib_path(mod_name, from_file)
        if mod_file is None:
            continue
        lib_mod_files[mod_name] = mod_file
        # Parse just to discover *its* imports — best-effort, so a
        # library that fails to parse here is silently skipped (the
        # later transpile pass will surface the error with full
        # diagnostics).
        try:
            sub_source = mod_file.read_bytes().decode("utf-8")
            module_index.update_file(mod_file, sub_source)
            sub_pre = preprocess_for_parse(sub_source).source
            sub_parser = create_parser()
            sub_tree = sub_parser.parse(sub_pre + "\n")
        except Exception:
            continue
        for imp in _extract_imports(sub_tree):
            if imp not in seen:
                worklist.append((imp, mod_file))
        # If this library calls any string method that the
        # built-in dispatcher routes through ``Strings_*``,
        # ``lamstrings`` has to be in the import graph too —
        # otherwise the per-library transpile would emit calls
        # to undefined symbols. Skip the inject when the library
        # *is* lamstrings (it can't import itself).
        if (
            mod_name != "lamstrings"
            and "lamstrings" not in seen
            and all(mod != "lamstrings" for mod, _ in worklist)
            and _lam_string_method_re.search(sub_pre)
        ):
            worklist.append(("lamstrings", mod_file))
        # Harvest this library's class names and static members so
        # cross-library static-member access can be lowered correctly.
        try:
            sub_collector = GoTranspiler(
                go_blocks=[], stdlib_path=stdlib_path,
                go_module_name=go_module,
            )
            sub_collector._collect_function_names(sub_tree)
            sub_collector._collect_class_fields(sub_tree)
            lib_pre_class_names.update(sub_collector._class_names)
            for cls, meths in sub_collector._static_methods.items():
                lib_pre_static_methods.setdefault(cls, set()).update(meths)
            for cls, fields in sub_collector._static_vars.items():
                lib_pre_static_vars.setdefault(cls, {}).update(fields)
        except Exception:
            # Pre-scan is best-effort; downstream transpile will
            # surface real errors with full positional diagnostics.
            pass

    # ── Deferred LAMMERGEIER.* typo guard ──
    # Runs *after* the library worklist so the ``extra_valid`` set
    # includes every class / static member reachable through a
    # ``from X import …`` chain. Without that, user code that
    # writes ``LAMMERGEIER.Result.Ok`` inside a ``go!`` block would
    # be flagged even though the emitter's dynamic dispatcher is
    # going to resolve it correctly at go-block emission time.
    extra_valid: set[str] = set()
    extra_valid |= set(_pre_transpiler._user_functions)
    extra_valid |= set(_pre_transpiler._class_names)
    for cls, methods in _pre_transpiler._static_methods.items():
        for m in methods:
            extra_valid.add(f"{cls}.{m}")
    for cls, fields in _pre_transpiler._static_vars.items():
        for name in fields:
            extra_valid.add(f"{cls}.{name}")
    # Transitive library symbols: every class + static member
    # harvested from the imports of the current file (and their
    # transitive imports). The emitter injects these into each
    # library transpiler too, so what the typo guard accepts here
    # matches exactly what the dispatcher will resolve later.
    extra_valid |= set(lib_pre_class_names)
    for cls, methods in lib_pre_static_methods.items():
        for m in methods:
            extra_valid.add(f"{cls}.{m}")
    for cls, fields in lib_pre_static_vars.items():
        for name in fields:
            extra_valid.add(f"{cls}.{name}")
    unknown = find_unknown_lammergeier_aliases(source, extra_valid=extra_valid)
    if unknown:
        print(f"error: unknown LAMMERGEIER.* reference in {source_path}", file=sys.stderr)
        valid = sorted({k for k in LAMMERGEIER_ALIASES})
        src_lines = source.split('\n')
        from difflib import get_close_matches as _gcm
        valid_tails = [k.split(".", 1)[1] for k in LAMMERGEIER_ALIASES] + sorted(extra_valid)
        for lineno, col, full in unknown:
            tail = full.split(".", 1)[1] if "." in full else full
            match = _gcm(tail, valid_tails, n=1, cutoff=0.65)
            suffix = f" — did you mean `LAMMERGEIER.{match[0]}`?" if match else ""
            print(f"  line {lineno}: `{full}` does not resolve to a known "
                  f"symbol{suffix}", file=sys.stderr)
            start = max(0, lineno - 2)
            end = min(len(src_lines), lineno + 1)
            for i in range(start, end):
                marker = ">>>" if i == lineno - 1 else "   "
                print(f"    {marker} {i+1:4d} | {src_lines[i]}", file=sys.stderr)
        print("\n  LAMMERGEIER.* resolves to either of:", file=sys.stderr)
        print("    • a compiler-emitted literal alias (for values "
              "with no Lam-level symbol):", file=sys.stderr)
        for v in valid:
            print(f"        - {v}", file=sys.stderr)
        if extra_valid:
            print("    • a Lam-level symbol in scope "
                  "(function / class / static member):", file=sys.stderr)
            for v in sorted(extra_valid):
                print(f"        - LAMMERGEIER.{v}", file=sys.stderr)
        sys.exit(1)

    # Stable byte digest of the cross-library class/static-member
    # union so the per-library cache can't return a stale entry that
    # was emitted before another library introduced a relevant class.
    # Sorting normalises the order so two builds with the same set
    # produce the same key.
    _cross_lib_extra = ("|".join(
        sorted(lib_pre_class_names)
        + [f"{cls}#{','.join(sorted(meths))}"
           for cls, meths in sorted(lib_pre_static_methods.items())]
        + [f"{cls}${','.join(f'{name}:{int(is_private)}' for name, is_private in sorted(fields.items()))}"
           for cls, fields in sorted(lib_pre_static_vars.items())]
    )).encode("utf-8")

    def _transpile_lib(mod_name, mod_file):
        """Transpile a single library module. Thread-safe (own parser+transpiler).

        Consults the on-disk cache first — a hit skips parsing and the
        whole visitor pipeline and returns the previously-emitted Go
        source plus the re-injectable metadata sets. On a miss, runs
        the full pipeline and writes the result back to the cache.
        """
        lib_source_bytes = mod_file.read_bytes()
        lib_source = lib_source_bytes.decode("utf-8")

        # ── Cache lookup ─────────────────────────────────────
        cache_hit = None
        if use_cache:
            entry = _lamcache.load(lib_source_bytes, extra=_cross_lib_extra)
            if entry is not None:
                cache_hit = _lamcache.deserialise_transpile_result(entry)
        if cache_hit is not None:
            (go_src, cls, stat, svars, defs, counts, var, ufns, pfns,
             mret, pnames) = cache_hit
            return (mod_name, go_src, cls, stat, svars, defs, counts, var,
                    ufns, pfns, mret, pnames)

        # Apply the same parse preprocessing pipeline used for main
        # files, including LAMMERGEIER.* aliases and go! extraction.
        lib_parse_input = preprocess_for_parse(lib_source)
        lib_source = apply_lammergeier_aliases(lib_source)
        lib_preprocessed = lib_parse_input.source
        lib_go_blocks = lib_parse_input.go_blocks
        lib_parser = create_parser()
        try:
            lib_tree = lib_parser.parse(lib_preprocessed + "\n")
        except UnexpectedInput as e:
            rendered = render_syntax_error(e, lib_source, mod_file)
            raise SyntaxDiagnosticError(rendered) from None
        lib_transpiler = GoTranspiler(
            go_blocks=lib_go_blocks,
            stdlib_path=stdlib_path,
            go_module_name=go_module,
            source_file=str(mod_file.resolve()),
        )
        # Inject the union of class names + static members seen across
        # *all* libraries so a custom library that imports another
        # library (e.g. user code importing ``Strings`` from
        # ``lamstrings``) can lower static-method calls correctly even
        # though the called class is defined in a different file.
        lib_transpiler._class_names.update(lib_pre_class_names)
        for cls, meths in lib_pre_static_methods.items():
            lib_transpiler._static_methods.setdefault(cls, set()).update(meths)
        for cls, fields in lib_pre_static_vars.items():
            lib_transpiler._static_vars.setdefault(cls, {}).update(fields)
        lib_go = lib_transpiler.transpile(lib_tree)
        # Remove func main() block from library — but also drop any
        # `//line` directive immediately preceding it so the stripped
        # region doesn't leave a dangling pragma pointing at the
        # removed body.
        lines = lib_go.split("\n")
        filtered = []
        skip_main = False
        for line in lines:
            if line.strip().startswith("func main()"):
                # Drop any //line pragma that was sitting right above
                # ``func main()``; it's now attached to nothing useful.
                if filtered and filtered[-1].lstrip().startswith("//line "):
                    filtered.pop()
                skip_main = True
                continue
            if skip_main:
                continue
            filtered.append(line)
        go_src = "\n".join(filtered)

        # ── Cache store ──────────────────────────────────────
        # ``go! { ... }`` blocks were initially assumed to be
        # uncacheable because their placeholder ids (``__go_block__``
        # identifiers) are build-local. In practice the transpiler
        # inlines each block's raw Go source at emit time, so by the
        # point we get here ``go_src`` is already self-contained and
        # doesn't reference the placeholder table.
        #
        # Default-parameter values are stored as Lark ``Tree`` nodes
        # internally — lower each one to its Go source before handing
        # the dict to the cache so JSON can serialise it. The reload
        # side unwraps strings directly in ``_fill_default_args``.
        if use_cache:
            lowered_defaults: dict = {}
            for fn, pairs in lib_transpiler._func_defaults.items():
                lowered_pairs = []
                for idx, node in pairs:
                    if isinstance(node, str):
                        lowered_pairs.append((idx, node))
                    else:
                        try:
                            lowered_pairs.append((idx, lib_transpiler._expr_to_go(node)))
                        except Exception:
                            # If a default can't be lowered ahead of
                            # call-site context (e.g. it references a
                            # caller-scoped binding) skip caching the
                            # entry rather than crashing the build.
                            lowered_pairs = None
                            break
                if lowered_pairs is not None:
                    lowered_defaults[fn] = lowered_pairs
            _lamcache.save(
                lib_source_bytes,
                _lamcache.serialise_transpile_result(
                    go_src,
                    lib_transpiler._class_names,
                    lib_transpiler._static_methods,
                    lib_transpiler._static_vars,
                    lowered_defaults,
                    lib_transpiler._func_param_counts,
                    lib_transpiler._variadic_functions,
                    lib_transpiler._user_functions,
                    lib_transpiler._private_functions,
                    lib_transpiler._method_return_types,
                    lib_transpiler._func_param_names,
                ),
                extra=_cross_lib_extra,
            )

        return (
            mod_name,
            go_src,
            lib_transpiler._class_names,
            lib_transpiler._static_methods,
            lib_transpiler._static_vars,
            lib_transpiler._func_defaults,
            lib_transpiler._func_param_counts,
            lib_transpiler._variadic_functions,
            lib_transpiler._user_functions,
            lib_transpiler._private_functions,
            lib_transpiler._method_return_types,
            lib_transpiler._func_param_names,
        )

    # Transpile libraries in parallel
    lib_func_defaults: dict = {}
    lib_func_param_counts: dict = {}
    lib_func_param_names: dict = {}
    lib_variadic_functions: set = set()
    lib_user_functions: set = set()
    lib_private_functions: set = set()
    lib_method_return_types: dict = {}
    from concurrent.futures import ThreadPoolExecutor
    if lib_mod_files:
        with ThreadPoolExecutor(max_workers=min(8, len(lib_mod_files))) as pool:
            futures = {pool.submit(_transpile_lib, name, path): name
                       for name, path in lib_mod_files.items()}
            for fut in futures:
                try:
                    (mod_name, go_src, cls_names, static_meths, static_vars,
                     fn_defaults, fn_param_counts, variadic_set,
                     user_fns, priv_fns, method_returns,
                     fn_param_names) = fut.result()
                except SyntaxDiagnosticError as e:
                    print(str(e), file=sys.stderr)
                    sys.exit(1)
                lib_sources[mod_name] = go_src
                lib_class_names.update(cls_names)
                for cls_name, methods in static_meths.items():
                    if cls_name not in lib_static_methods:
                        lib_static_methods[cls_name] = set()
                    lib_static_methods[cls_name].update(methods)
                for cls_name, fields in static_vars.items():
                    lib_static_vars.setdefault(cls_name, {}).update(fields)
                lib_func_defaults.update(fn_defaults)
                lib_func_param_counts.update(fn_param_counts)
                lib_func_param_names.update(fn_param_names)
                lib_variadic_functions.update(variadic_set)
                lib_user_functions.update(user_fns)
                lib_private_functions.update(priv_fns)
                lib_method_return_types.update(method_returns)

    # Now transpile main with lib class/static info injected
    transpiler = GoTranspiler(
        go_blocks=go_blocks,
        stdlib_path=stdlib_path,
        go_module_name=go_module,
        source_file=str(source_file),
    )
    # Inject library class/static info
    transpiler._class_names.update(lib_class_names)
    for cls_name, methods in lib_static_methods.items():
        if cls_name not in transpiler._static_methods:
            transpiler._static_methods[cls_name] = set()
        transpiler._static_methods[cls_name].update(methods)
    for cls_name, fields in lib_static_vars.items():
        transpiler._static_vars.setdefault(cls_name, {}).update(fields)
    # Inject library function defaults so calls in the main file
    # can auto-fill defaults for imported static methods.
    for key, defaults in lib_func_defaults.items():
        transpiler._func_defaults.setdefault(key, defaults)
    for key, count in lib_func_param_counts.items():
        transpiler._func_param_counts.setdefault(key, count)
    for key, names in lib_func_param_names.items():
        transpiler._func_param_names.setdefault(key, names)
    transpiler._variadic_functions.update(lib_variadic_functions)
    # Imported library functions need to be visible to the main
    # transpiler's call-site dispatcher (the ``_user_functions`` /
    # ``_private_functions`` branches handle ``_go_public_name``
    # rewriting *and* default-arg filling). Without this, calls like
    # ``metrics(srv)`` fall through to the bare-emit path which
    # neither rewrites the name nor fills defaults — Go then rejects
    # the call with ``not enough arguments``.
    transpiler._external_user_functions.update(lib_user_functions)
    transpiler._external_private_functions.update(lib_private_functions)
    # Library-declared method return types power chained-call class
    # inference (eg ``db.table(name).where(...)``).
    for key, ret in lib_method_return_types.items():
        transpiler._method_return_types.setdefault(key, ret)
    try:
        go_source = transpiler.transpile(tree)
    except RuntimeError as e:
        # User-facing transpile errors (e.g. unknown keyword argument,
        # name collision) bubble up here. Print them in the same
        # ``error: <message>`` shape the rest of the compiler uses
        # rather than dumping a Python traceback.
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if emit_go:
        print(go_source)
        for mod_name, lib_src in lib_sources.items():
            print(f"\n// === Library: {mod_name} ===")
            print(lib_src)
        return

    if transpile_only:
        # Write Go source to file without compiling
        go_out = str(source_file.with_suffix(".go"))
        if output_path:
            go_out = output_path
        with open(go_out, "w") as f:
            f.write(go_source)
        if verbose:
            print(f"[lamc] Go source written to {go_out}", file=sys.stderr)
        return

    # ── Check Go is installed ──
    if not shutil.which("go"):
        print("error: Go compiler not found in PATH. Install Go from https://go.dev/dl/", file=sys.stderr)
        sys.exit(1)

    # ── Compile ──
    if output_path is None:
        # Default the binary next to the source file, not in the
        # caller's CWD. This keeps invocations like
        # ``lamc tests/tests/cases/foo.lam`` from polluting the
        # project root — the binary lands in ``tests/tests/cases/foo``
        # instead. Pass ``-o <path>`` to override, or ``-o ./foo`` to
        # restore the old CWD-stem behaviour.
        output_path = str(source_file.with_suffix(""))

    output_abs = str(Path(output_path).resolve())

    tmpdir = tempfile.mkdtemp(prefix="lammergeier_")
    try:
        # Write Go source
        go_file = os.path.join(tmpdir, "main.go")
        with open(go_file, "w") as f:
            f.write(go_source)

        # Write library Go sources. ``mod_name`` can be a scoped
        # package (``@alice/lamwebp``) whose ``/`` would otherwise
        # reference a subdirectory that doesn't exist in the build
        # tempdir. Flatten to a filesystem-safe form — strip the
        # leading ``@`` and replace the scope separator with ``__``
        # so ``@alice/lamwebp`` → ``lib_alice__lamwebp.go`` (unique
        # because ``__`` isn't permitted in plain module names).
        for mod_name, lib_src in lib_sources.items():
            safe = mod_name.replace("/", "__")
            if safe.startswith("@"):
                safe = safe[1:]
            lib_file = os.path.join(tmpdir, f"lib_{safe}.go")
            with open(lib_file, "w") as f:
                f.write(lib_src)

        if verbose:
            print(f"[lamc] Go source written to {go_file}", file=sys.stderr)
            print(go_source, file=sys.stderr)

        # Copy stdlib if present
        if stdlib_path and os.path.isdir(stdlib_path):
            for fname in os.listdir(stdlib_path):
                if fname.endswith(".go"):
                    shutil.copy2(os.path.join(stdlib_path, fname),
                                 os.path.join(tmpdir, fname))

        # go mod init
        subprocess.run(
            ["go", "mod", "init", go_module],
            cwd=tmpdir, capture_output=True, text=True,
        )

        # Inject Go-module pins from the project's lamlib.toml +
        # lamlib.lock.toml. The lockfile wins when both are present
        # (it's the resolved, post-conflict-detection view); the raw
        # manifest is only consulted when no lockfile exists yet.
        # Without this step, ``go mod tidy`` would simply pick the
        # newest tag for each unknown import — silently undoing the
        # version conflict resolution the install CLI just did.
        go_pins = _collect_go_pins(source_dir, stdlib_modules=seen)
        if go_pins:
            try:
                _inject_go_requires(Path(tmpdir) / "go.mod", go_pins)
            except OSError as e:
                if verbose:
                    print(f"[lamc] go.mod pin injection skipped: {e}",
                          file=sys.stderr)

        # go mod tidy (download external dependencies if needed)
        subprocess.run(
            ["go", "mod", "tidy"],
            cwd=tmpdir, capture_output=True, text=True,
        )

        # Build command
        build_cmd = ["go", "build"]

        if go_race:
            build_cmd.append("-race")
        if go_trimpath:
            build_cmd.append("-trimpath")
        if go_tags:
            build_cmd.extend(["-tags", go_tags])
        if go_ldflags:
            build_cmd.extend(["-ldflags", go_ldflags])
        if go_gcflags:
            build_cmd.extend(["-gcflags", go_gcflags])
        if go_extra_flags:
            build_cmd.extend(go_extra_flags)

        build_cmd.extend(["-o", output_abs, "."])

        if verbose:
            print(f"[lamc] {' '.join(build_cmd)}", file=sys.stderr)

        result = subprocess.run(
            build_cmd, cwd=tmpdir, capture_output=True, text=True,
        )

        if result.returncode != 0:
            print(f"error: Go build failed for {source_path}", file=sys.stderr)
            import re as _re3
            # Cache: map absolute .lam path → list of source lines. The
            # error may reference the main file or any imported library
            # file, and each needs its own context lookup.
            _src_cache: dict[str, list[str]] = {str(source_file): source.split('\n')}

            def _lam_lines(path: str) -> list[str]:
                if path in _src_cache:
                    return _src_cache[path]
                try:
                    txt = Path(path).read_text(encoding="utf-8")
                except OSError:
                    txt = ""
                _src_cache[path] = txt.split('\n')
                return _src_cache[path]

            for err_line in result.stderr.strip().split('\n'):
                # Try to match .lam file references (from //line directives).
                # The path can include directory components (libraries live
                # alongside main), so capture everything up to ``.lam``.
                m = _re3.search(r'(\S*?\.lam):(\d+):\d*:?\s*(.*)', err_line)
                if m:
                    lam_path = m.group(1)
                    lam_line = int(m.group(2))
                    msg = m.group(3)
                    # Resolve relative paths against the build tmpdir's
                    # neighbouring source directory when possible.
                    abs_path = lam_path
                    if not Path(lam_path).is_absolute():
                        cand = Path(source_file.parent) / Path(lam_path).name
                        if cand.exists():
                            abs_path = str(cand)
                    src_lines = _lam_lines(abs_path)
                    # Show the file name only when it differs from the
                    # main source to keep the usual case compact.
                    prefix = f"  line {lam_line}"
                    if abs_path != str(source_file):
                        prefix = f"  {Path(abs_path).name}:{lam_line}"
                    print(f"{prefix}: {msg}", file=sys.stderr)
                    start = max(0, lam_line - 2)
                    end = min(len(src_lines), lam_line + 1)
                    for i in range(start, end):
                        marker = ">>>" if i == lam_line - 1 else "   "
                        print(f"    {marker} {i+1:4d} | {src_lines[i]}", file=sys.stderr)
                else:
                    # Fallback: try main.go line references
                    m2 = _re3.search(r'main\.go:(\d+):\d+:\s*(.*)', err_line)
                    if m2:
                        go_line = int(m2.group(1))
                        go_msg = m2.group(2)
                        print(f"  Go line {go_line}: {go_msg}", file=sys.stderr)
                    else:
                        print(f"  {err_line}", file=sys.stderr)
            if verbose:
                # Dump annotated Go source with line numbers for debugging
                print("\n  Generated Go source:", file=sys.stderr)
                for i, line in enumerate(go_source.split("\n"), 1):
                    print(f"  {i:4d}  {line}", file=sys.stderr)
            sys.exit(1)

        if verbose:
            print(f"[lamc] compiled → {output_abs}", file=sys.stderr)

        if keep_go:
            dest = str(source_file.with_suffix(".go"))
            shutil.copy2(go_file, dest)
            print(f"[lamc] Go source saved to {dest}", file=sys.stderr)

        # ── Run ──
        if run:
            proc = subprocess.run([output_abs], capture_output=False)
            sys.exit(proc.returncode)

    finally:
        if not keep_go:
            shutil.rmtree(tmpdir, ignore_errors=True)


# Set of known top-level subcommand verbs. Anything in this list
# is routed to its dedicated handler; anything else is treated as
# the legacy ``lamc <source.lam>`` invocation — so existing scripts,
# docs, and tests keep working verbatim.
_LAMC_SUBCOMMANDS = ("build", "fmt", "doctor", "migrate")


def _build_build_parser() -> "argparse.ArgumentParser":
    """Return the argparse parser for the ``build`` (compile) verb.

    Kept in its own helper so the same parser backs both ``lamc
    build <src>`` and the legacy ``lamc <src>`` form without us
    duplicating flag definitions.
    """
    ap = argparse.ArgumentParser(
        prog="lamc build",
        description=(
            "Compile (and optionally run) a Lammergeier source file. "
            "The legacy ``lamc <source.lam>`` form is an alias for "
            "``lamc build <source.lam>`` and accepts the same flags."
        ),
    )
    ap.add_argument("source", help="Path to .lam source file")
    ap.add_argument("-o", "--output", help="Output binary path")
    ap.add_argument("--emit-go", action="store_true",
                    help="Print generated Go source and exit")
    ap.add_argument("--emit-ast", action="store_true",
                    help="Print Lark AST and exit")
    ap.add_argument("--run", action="store_true",
                    help="Compile and run immediately")
    ap.add_argument("--transpile-only", action="store_true",
                    help="Generate .go file without compiling")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Verbose output")
    ap.add_argument("--keep-go", action="store_true",
                    help="Save generated .go file alongside source")
    ap.add_argument("--go-module", default="lamc/app",
                    help="Go module name (default: lamc/app)")
    ap.add_argument("--stdlib", default=None,
                    help="Path to lamc stdlib directory")
    ap.add_argument(
        "--extlibs",
        action="append",
        default=None,
        metavar="DIR",
        help=(
            "Directory of third-party Lam libraries. May be given "
            "multiple times; the ``LAMC_EXTLIBS`` env var accepts a "
            "colon-separated list of the same shape. Search order is "
            "stdlib → extlibs → project."
        ),
    )

    # Go compiler passthrough flags (--go-*)
    ap.add_argument("--go-ldflags",
                    help="Pass -ldflags to go build")
    ap.add_argument("--go-tags",
                    help="Pass -tags to go build")
    ap.add_argument("--go-gcflags",
                    help="Pass -gcflags to go build")
    ap.add_argument("--go-race", action="store_true",
                    help="Enable Go race detector")
    ap.add_argument("--go-trimpath", action="store_true",
                    help="Remove file system paths from binary")
    ap.add_argument("--go-extra", nargs="*", default=[],
                    help="Extra flags to pass to go build")

    ap.add_argument("--no-cache", action="store_true",
                    help="Skip the on-disk library cache for this build")
    ap.add_argument("--clear-cache", action="store_true",
                    help="Delete all cached library entries, then exit")
    ap.add_argument("--no-semantic-check", action="store_true",
                    help="Skip the pre-emission semantic checker")
    return ap


def _cmd_build(argv: list) -> int:
    """Run the compile/transpile pipeline. Returns the process exit
    code the outer dispatcher should propagate.
    """
    args = _build_build_parser().parse_args(argv)

    if args.clear_cache:
        removed = _lamcache.clear()
        print(f"[lamc] removed {removed} cached entries from {_lamcache.cache_dir()}",
              file=sys.stderr)
        return 0

    # Auto-detect stdlib
    stdlib = args.stdlib
    if stdlib is None:
        default_stdlib = PROJECT_ROOT / "stdlib"
        if default_stdlib.is_dir() and any(default_stdlib.glob("*.go")):
            stdlib = str(default_stdlib)

    compile_lam(
        source_path=args.source,
        output_path=args.output,
        emit_go=args.emit_go,
        emit_ast=args.emit_ast,
        transpile_only=args.transpile_only,
        run=args.run,
        verbose=args.verbose,
        go_module=args.go_module,
        go_ldflags=args.go_ldflags,
        go_tags=args.go_tags,
        go_gcflags=args.go_gcflags,
        go_race=args.go_race,
        go_trimpath=args.go_trimpath,
        go_extra_flags=args.go_extra,
        keep_go=args.keep_go,
        stdlib_path=stdlib,
        extlibs_paths=args.extlibs,
        use_cache=not args.no_cache,
        no_semantic_check=args.no_semantic_check,
    )
    return 0


def _build_fmt_parser() -> "argparse.ArgumentParser":
    ap = argparse.ArgumentParser(
        prog="lamc fmt",
        description="Format a Lammergeier source file.",
    )
    ap.add_argument("source", help="Path to .lam source file")
    ap.add_argument("--stdout", action="store_true",
                    help="Print formatted source instead of writing the file")
    ap.add_argument("--check", action="store_true",
                    help="Exit non-zero if the file is not already formatted")
    return ap


def _cmd_fmt(argv: list) -> int:
    from compiler.formatter import FormatError, format_lam_source

    args = _build_fmt_parser().parse_args(argv)
    path = Path(args.source)
    try:
        source = path.read_text(encoding="utf-8")
        result = format_lam_source(source)
    except OSError as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 1
    except FormatError as e:
        print(f"error: cannot format {path}: {e}", file=sys.stderr)
        return 1
    if args.check:
        if result.changed:
            print(f"{path} is not formatted", file=sys.stderr)
            return 1
        return 0
    if args.stdout:
        print(result.text, end="")
        return 0
    if result.changed:
        path.write_text(result.text, encoding="utf-8")
    return 0


def _doctor_go_status() -> str:
    go = shutil.which("go")
    if not go:
        return "missing (go not found on PATH)"
    try:
        proc = subprocess.run(
            [go, "version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError as e:
        return f"error running {go}: {e}"
    except subprocess.TimeoutExpired:
        return f"error running {go}: timed out"
    output = (proc.stdout or proc.stderr).strip()
    if proc.returncode != 0:
        detail = output or f"exit {proc.returncode}"
        return f"error ({detail})"
    return f"{output} ({go})"


def _doctor_lark_status() -> str:
    if _LARK_IMPORT_ERROR is not None or _lark is None:
        return f"missing ({_LARK_IMPORT_ERROR})"
    version = getattr(_lark, "__version__", "unknown")
    location = getattr(_lark, "__file__", "")
    if location:
        return f"{version} ({Path(location).resolve()})"
    return str(version)


def _doctor_lsp_status() -> str:
    found = shutil.which("lammergeier-lsp")
    checkout = PROJECT_ROOT / "bin" / "lammergeier-lsp"
    if found:
        return f"found at {found}"
    if checkout.exists():
        return f"not on PATH (checkout launcher exists at {checkout})"
    return "missing (lammergeier-lsp not found on PATH)"


def _cmd_doctor(argv: list) -> int:
    ap = argparse.ArgumentParser(
        prog="lamc doctor",
        description="Report local Lammergeier toolchain paths and versions.",
    )
    ap.parse_args(argv)

    stdlib_path = PROJECT_ROOT / "lib"
    print("Lammergeier doctor")
    print(f"python: {sys.version.split()[0]} ({sys.executable})")
    print(f"go: {_doctor_go_status()}")
    print(f"lark: {_doctor_lark_status()}")
    print(f"project root: {PROJECT_ROOT}")
    print(f"stdlib path: {stdlib_path}")
    print(f"cache path: {_lamcache.cache_dir()}")
    print(f"lammergeier-lsp: {_doctor_lsp_status()}")
    return 0


_LAMC_TOP_HELP = """\
usage: lamc [<subcommand>] <args...>

Lammergeier Lang → Go compiler

Subcommands:
  build      Compile (and optionally run) a .lam source file.
             Also the default when no subcommand is given, so
             ``lamc foo.lam`` is equivalent to ``lamc build foo.lam``.
  fmt        Format a .lam source file. Use ``--check`` for CI or
             ``--stdout`` to print without writing.
  doctor     Report Python, Go, lark, project, stdlib, cache, and
             language-server availability.
  init       Scaffold a fresh project (``lamlib.toml`` + entry-point
             ``.lam`` + ``.gitignore``). Flags: ``--name``, ``--version``,
             ``--scope``, ``--license``, ``--bin`` / ``--lib``, ``--force``.
  install    Fetch + install a third-party library from a registry,
             git URL, or local path. Writes to ``./extlibs/`` and
             updates ``lamlib.lock.toml`` (project mode) by default;
             pass ``--global`` for the legacy ``~/.lammergeier/extlibs/``
             write. Bare ``lamc install`` reads ``lamlib.toml`` and
             installs everything declared in ``[dependencies]``.
  uninstall  Remove an installed library.
  tidy       Sync ``lamlib.toml`` ``[dependencies]`` with the project's
             actual import graph. Drops unused entries, adds missing
             ones (from already-installed extlibs/), refreshes the
             lockfile. ``--check`` for CI.
  verify     Re-hash every installed extlib and compare against
             ``lamlib.lock.toml``. Catches supply-chain tampering,
             partial installs, and drift after a manual edit.
  list       Print every dep in ``lamlib.lock.toml``.
  tree       Render the dependency tree (uses ``requested_by``).
  why        Explain why a particular pin is in the lockfile.
  publish    Pack a library directory and POST it to a registry.
  migrate    Knex-style SQL migrations (make / up / down / status).

Run ``lamc <subcommand> --help`` for details on any subcommand, or
``lamc build --help`` for the full list of compile flags.
"""


# Back-compat alias. ``compile_tpy`` was the original name when
# Lam source files carried the ``.tpy`` extension during the
# Python-flavoured prototype phase. The implementation has always
# compiled any path the user hands it (extension is not checked),
# and the canonical name is now ``compile_lam``. Keep the old
# symbol exported so third-party tooling that imports it via
# ``from compiler.lammergeier import compile_tpy`` keeps working;
# new code should use ``compile_lam``.
compile_tpy = compile_lam


def main():
    """Top-level ``lamc`` entry point.

    Dispatches on the first positional token:

      * ``migrate``   → hands off to ``compiler.migrate_cli.main``
      * ``install`` / ``uninstall`` / ``publish``
                      → ``compiler.install_cli.main``
      * ``build``     → explicit compile verb (same flags as below)
      * anything else / no args → legacy ``lamc <source.lam>`` shape,
        routed through the same ``build`` parser as a courtesy to
        existing tooling, docs, and CI.

    We emit our own top-level ``--help`` so the subcommand table is
    actually visible — argparse subparsers would make the legacy
    form awkward to preserve cleanly, so we do the routing by hand.
    """
    argv = list(sys.argv[1:])

    # Bare ``lamc`` or explicit top-level help: print the subcommand
    # overview. Subcommand-specific ``--help`` (e.g. ``lamc migrate
    # --help``) is handled by each subparser below.
    if not argv or (argv[0] in ("-h", "--help") and len(argv) == 1):
        print(_LAMC_TOP_HELP)
        return

    if argv[0] == "--doctor":
        sys.exit(_cmd_doctor(argv[1:]))

    verb = argv[0]

    if verb == "migrate":
        from compiler import migrate_cli
        sys.exit(migrate_cli.main(argv[1:]))

    if verb in ("install", "uninstall", "publish",
                "tidy", "verify", "init", "list", "tree", "why"):
        from compiler import install_cli
        sys.exit(install_cli.main(argv))

    if verb == "build":
        sys.exit(_cmd_build(argv[1:]))

    if verb == "fmt":
        sys.exit(_cmd_fmt(argv[1:]))

    if verb == "doctor":
        sys.exit(_cmd_doctor(argv[1:]))

    # Legacy: ``lamc <source.lam> [flags]`` — inject the ``build``
    # verb invisibly so existing scripts keep working.
    sys.exit(_cmd_build(argv))


if __name__ == "__main__":
    main()
