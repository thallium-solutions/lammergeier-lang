#!/usr/bin/env python3
"""Preprocessor: handles go! blocks and inline go! expressions before parsing."""

from typing import Dict, Tuple
import re


# ─── LAMMERGEIER.* namespace ──────────────────────────────────────
#
# The transpiler emits a small handful of helper symbols whose names
# (``Result_Ok``, ``Result_Err``, ``NewError`` …) leak the compiler's
# Go-side calling convention into the stdlib. Library authors used to
# call those names directly from inside ``go!`` blocks, which meant
# every rename of an internal helper would ripple across every
# library file.
#
# ``LAMMERGEIER.<name>`` is the supported, stable alias for those
# helpers. It looks like a normal namespaced call from Lam, but the
# preprocessor lowers it to the underlying Go symbol before the
# parser ever sees it. Renaming an internal helper now only needs
# updating this table.
#
# The preprocessor rewrites the alias *everywhere in the source*
# (inside and outside ``go!`` blocks) so that a future lifting of
# these helpers into pure-Lam wrappers — e.g. exposing
# ``LAMMERGEIER.Result.Ok`` as a real static method on a Lam class
# — would not require touching call sites.

# Map from the public Lam-facing alias to the actual Go-side
# identifier the transpiler emits. Order doesn't matter; longest
# patterns are matched first to avoid prefix collisions.
LAMMERGEIER_ALIASES: Dict[str, str] = {
    "LAMMERGEIER.Result.Ok":  "Result_Ok",
    "LAMMERGEIER.Result.Err": "Result_Err",
    "LAMMERGEIER.Error":      "NewError",
    # ``LAMMERGEIER.None`` / ``LAMMERGEIER.nil`` are convenience
    # aliases for the Go nil literal, useful inside ``go!`` blocks
    # that want to be explicit about emitting a Go ``nil`` rather
    # than a Lam ``None`` (which lowers the same way today, but
    # could diverge later).
    "LAMMERGEIER.None":       "nil",
    "LAMMERGEIER.nil":        "nil",
}

# Compile a single alternation pattern, longest-first. The trailing
# negative lookahead stops the regex from matching ``LAMMERGEIER.X``
# when ``.X`` is itself a longer alias prefix — without it the
# substitution order would matter.
_LAMMERGEIER_RE = re.compile(
    r"\bLAMMERGEIER\.(?:" + "|".join(
        re.escape(k.split(".", 1)[1])
        for k in sorted(LAMMERGEIER_ALIASES, key=len, reverse=True)
    ) + r")\b"
)


def apply_lammergeier_aliases(source: str) -> str:
    """Rewrite every ``LAMMERGEIER.<name>`` reference in ``source``
    to the underlying Go symbol from :data:`LAMMERGEIER_ALIASES`.

    This is a textual pass — it intentionally does *not* parse the
    source first. The aliases are designed so that the underlying
    rewrite produces identical code in any context where the alias
    was syntactically valid (calls, identifiers, arguments).
    """
    if "LAMMERGEIER." not in source:
        return source

    def _sub(m: "re.Match[str]") -> str:
        return LAMMERGEIER_ALIASES.get(m.group(0), m.group(0))

    return _LAMMERGEIER_RE.sub(_sub, source)


# Greedy capture of *any* ``LAMMERGEIER.<dotted>`` reference, used by
# :func:`find_unknown_lammergeier_aliases` to surface typos
# (``LAMMERGEIER.Result.OK`` vs ``Ok``) before the resulting
# unrewritten source reaches Go and produces a less helpful error.
_LAMMERGEIER_ANY_RE = re.compile(r"\bLAMMERGEIER\.([A-Za-z_][A-Za-z_0-9.]*)\b")


def find_unknown_lammergeier_aliases(
    source: str,
    extra_valid: "set[str] | None" = None,
) -> list:
    """Locate every ``LAMMERGEIER.<dotted>`` reference whose alias
    is unknown.

    A reference is "known" when its tail (the part after the literal
    ``LAMMERGEIER.``) matches:

      * an entry in :data:`LAMMERGEIER_ALIASES` (compiler-emitted
        helper, eg ``Result.Ok``); or
      * any element of ``extra_valid`` (typically user-defined
        functions, classes, or ``ClassName.staticMethod`` static
        method paths). The caller populates this set after parsing
        so user names participate in the typo guard the same way
        compiler helpers do.

    Returns a list of ``(line, column, full_alias)`` tuples (1-indexed
    line, 1-indexed column to match the rest of the compiler's
    diagnostics). The lookup is purely textual and ignores comments
    via a quick line-by-line filter — false positives here would only
    fire on a stale spelling of an alias that was never valid, which
    is exactly the case we want to flag.
    """
    out: list = []
    if "LAMMERGEIER." not in source:
        return out
    valid = {k.split(".", 1)[1] for k in LAMMERGEIER_ALIASES}
    if extra_valid:
        valid |= set(extra_valid)
    for lineno, line in enumerate(source.splitlines(), start=1):
        # Skip ``#`` line comments — they're not active code.
        code_part = line.split("#", 1)[0] if "#" in line else line
        for m in _LAMMERGEIER_ANY_RE.finditer(code_part):
            alias_tail = m.group(1)
            if alias_tail not in valid:
                out.append((lineno, m.start() + 1, f"LAMMERGEIER.{alias_tail}"))
    return out


_DESTRUCTURE_LHS_RE = re.compile(
    # Statement-start ``{ a, b: alias, c }`` followed by ``=`` and an
    # expression. Captures the inner key list and the RHS expression
    # (up to a newline / semicolon — multiline RHSs aren't supported
    # here; if you need one, hoist it into a local first).
    r"^([ \t]*)\{[ \t]*([a-zA-Z_][\w \t,:]*?)[ \t]*\}[ \t]*=[ \t]*(.+?)[ \t]*(?=\n|;|$)",
    re.MULTILINE,
)


_DESTRUCTURE_TEMP_PREFIX = "_lamDestruct"


def expand_dict_destructure(source: str) -> str:
    """Rewrite ``{ a, b: alias } = expr`` into a sequence of plain
    assignments before the parser runs.

    The expansion order is:

        _lamDestructN = expr
        a = _lamDestructN["a"]
        alias = _lamDestructN["b"]

    Only triggers when the ``{...}`` sits at *statement start* (after
    optional indentation) and the contents are a comma-separated list
    of plain identifiers, with optional ``key: alias`` rename. Set
    literals used as expressions are unaffected because they're never
    at the LHS of an ``=``.
    """
    counter = 0

    def _replace(m: re.Match) -> str:
        nonlocal counter
        indent = m.group(1)
        keys_raw = m.group(2)
        rhs = m.group(3).strip()
        # Parse the key list. Each entry is either ``name`` (the key
        # is also the local name) or ``key: alias`` (rename on bind).
        entries = []
        for part in keys_raw.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" in part:
                key, alias = part.split(":", 1)
                key = key.strip()
                alias = alias.strip()
            else:
                key = alias = part
            if not key.isidentifier() or not alias.isidentifier():
                return m.group(0)  # bail — keep original text
            entries.append((key, alias))
        if not entries:
            return m.group(0)
        tmp = f"{_DESTRUCTURE_TEMP_PREFIX}{counter}"
        counter += 1
        lines = [f"{indent}{tmp} = {rhs}"]
        for key, alias in entries:
            lines.append(f'{indent}{alias} = {tmp}["{key}"]')
        return "\n".join(lines)

    return _DESTRUCTURE_LHS_RE.sub(_replace, source)


def _update_go_block_depth(line: str, depth: int) -> int:
    """Advance the multi-line ``go! { ... }`` brace counter by one
    physical line of Go source.

    Walks the line character by character but *skips* spans that
    can legitimately contain a stray ``{`` or ``}`` without
    changing the surrounding block's depth:

    - ``"..."`` double-quoted strings (with ``\\"`` escapes)
    - ```...``` raw strings (no escapes)
    - ``'...'`` rune literals (with ``\\'`` escapes)
    - ``// ...`` line comments
    - ``/* ... */`` block comments (single-line only — the
      multi-line case would need state across calls and is vanishingly
      rare inside the small ``go!`` blocks users write)

    Returns the new depth after this line. A caller still breaks out
    of the collection loop on the first line that brings the depth
    down to 0.
    """
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == '/' and i + 1 < n and line[i+1] == '/':
            break  # line comment — nothing past here matters
        if ch == '/' and i + 1 < n and line[i+1] == '*':
            end = line.find('*/', i + 2)
            if end < 0:
                return depth  # unterminated block comment — stop
            i = end + 2
            continue
        if ch == '"':
            i += 1
            while i < n:
                if line[i] == '\\' and i + 1 < n:
                    i += 2
                    continue
                if line[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == '`':
            i += 1
            while i < n and line[i] != '`':
                i += 1
            if i < n:
                i += 1
            continue
        if ch == '\'':
            i += 1
            while i < n:
                if line[i] == '\\' and i + 1 < n:
                    i += 2
                    continue
                if line[i] == '\'':
                    i += 1
                    break
                i += 1
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return depth
        i += 1
    return depth


def preprocess_go_blocks(source: str) -> Tuple[str, Dict[str, str]]:
    """
    Find go! { ... } blocks and replace them with __go_block__("id") calls.

    A go! block looks like:
        go! {
            raw Go code line 1
            raw Go code line 2
        }

    Also supports legacy go!: indentation syntax for backwards compatibility.

    Inline go!(expr) is replaced with __go_inline__("id") and the expression
    is stored in the go_blocks dict.

    Returns (modified_source, {id: raw_go_code})
    """
    lines = source.split("\n")
    result_lines = []
    go_blocks: Dict[str, str] = {}
    block_id = 0
    i = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip()

        # Match inline go!(expr) — replace all occurrences on the line
        if 'go!(' in stripped:
            # Find and replace go!(expr) patterns (handling nested parens)
            new_line = stripped
            while 'go!(' in new_line:
                start = new_line.index('go!(')
                # Find matching closing paren
                depth = 0
                j = start + 3  # position of '('
                for j in range(start + 3, len(new_line)):
                    if new_line[j] == '(':
                        depth += 1
                    elif new_line[j] == ')':
                        depth -= 1
                        if depth == 0:
                            break
                expr = new_line[start + 4:j]
                bid = str(block_id)
                go_blocks[bid] = expr
                new_line = new_line[:start] + f'__go_inline__("{bid}")' + new_line[j+1:]
                block_id += 1
            result_lines.append(new_line)
            i += 1
            continue

        # Match go! { (brace syntax)
        match_brace = re.match(r'^(\s*)go!\s*\{', stripped)
        if match_brace:
            indent = match_brace.group(1)
            # Check if closing brace is on the same line
            after_open = stripped[match_brace.end():]
            if '}' in after_open:
                # Single-line go! { code }
                content = after_open[:after_open.rindex('}')].strip()
                bid = str(block_id)
                go_blocks[bid] = content
                result_lines.append(f'{indent}__go_block__("{bid}")')
                block_id += 1
                i += 1
                continue

            # Multi-line: collect until closing }
            raw_lines = []
            brace_depth = 1
            i += 1
            while i < len(lines) and brace_depth > 0:
                l = lines[i]
                # Count braces *outside* Go string / rune / comment
                # contexts. A naïve per-character loop mis-counts
                # things like ``js[j] != '}'`` inside a Go rune
                # literal, which then closes the go-block
                # prematurely and corrupts every subsequent line.
                brace_depth = _update_go_block_depth(l, brace_depth)
                if brace_depth == 0:
                    # Don't include the closing brace line content (before })
                    before_close = l[:l.rindex('}')].rstrip()
                    if before_close.strip():
                        raw_lines.append(before_close.strip())
                    i += 1
                    break
                else:
                    # Strip common indent (indent + 4 spaces or tab)
                    content_line = l
                    prefix = indent + "    "
                    if content_line.startswith(prefix):
                        content_line = content_line[len(prefix):]
                    elif content_line.strip() == "":
                        content_line = ""
                    else:
                        content_line = content_line.strip()
                    raw_lines.append(content_line)
                    i += 1

            bid = str(block_id)
            go_blocks[bid] = "\n".join(raw_lines)
            result_lines.append(f'{indent}__go_block__("{bid}")')
            block_id += 1
            continue

        # Legacy: Match go!: at any indent level (indent-based)
        match_legacy = re.match(r'^(\s*)go!\s*:', stripped)
        if match_legacy:
            indent = match_legacy.group(1)
            block_indent_len = len(indent)
            raw_lines = []
            i += 1
            # Collect indented lines
            while i < len(lines):
                l = lines[i]
                if l.strip() == "":
                    raw_lines.append("")
                    i += 1
                    continue
                # Check if indented deeper than the go!: line
                cur_indent = len(l) - len(l.lstrip())
                if cur_indent > block_indent_len:
                    # Strip the extra indent relative to block
                    raw_lines.append(l[block_indent_len + 4:] if len(l) > block_indent_len + 4 else l.strip())
                    i += 1
                else:
                    break
            bid = str(block_id)
            go_blocks[bid] = "\n".join(raw_lines)
            result_lines.append(f'{indent}__go_block__("{bid}")')
            block_id += 1
        else:
            result_lines.append(line)
            i += 1

    return "\n".join(result_lines), go_blocks
