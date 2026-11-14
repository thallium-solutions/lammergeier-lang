#!/usr/bin/env python3
"""Docs as tests: every Lam code block of ``docs/*.md`` and ``README.md`` builds.

A fenced block is Lam when its info string starts with ``lammergeier`` or
``lam``. The words after the language name are markers (CONTRIBUTING.md,
"Documentation examples are tests"):

- no marker: the block is built with the working-tree compiler. A block with
  a top-level ``func main`` is a complete program and is built as is. Any
  other block is completed into a program: its top-level declarations
  (``from``/``import``, ``func``, ``class``, ``interface``, decorators,
  ``const``, ``go!``) stay at the top level and its other top-level lines
  become the body of a ``func main()`` (an empty one when there are none).
- ``# Output:`` in the block: the comment lines that follow it (``#`` and one
  space stripped, up to the first line that is not a comment) are the exact
  standard output of the program, which is then run and compared. A ``text``
  block right after the Lam block whose first line runs it (``$ lamc
  main.lam --run``) gives the expected output the same way.
- ``fragment``: not checked (an excerpt that needs surrounding code, a
  placeholder such as ``...``, an external package or a service).
- ``error``: must be rejected by ``lamc`` with a Lam diagnostic (non-zero
  exit status, no internal compiler error, no Python traceback); each
  ``# error: <message>`` comment must appear in the diagnostics (a trailing
  ``...`` elides the rest).

Usage::

    python3 tests/tests/test_doc_examples.py                 # every block
    python3 tests/tests/test_doc_examples.py --filter SYNTAX.md:1040
    python3 tests/tests/test_doc_examples.py --list          # classification only
    python3 tests/tests/test_doc_examples.py --show stdlib.md:410

``LAM_DOC_EXAMPLES_JOBS`` (or ``--jobs``) sets the number of parallel builds.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"
DOC_FILES = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]

LANGS = ("lammergeier", "lam")
MARKERS = ("fragment", "error")
FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^`]*)$")
DECLARATION = re.compile(
    r"^(from\s|import\s|(private\s+)?(static\s+)?(async\s+)?func\s|class\s|interface\s|@|const\s|go!)"
)
MAIN = re.compile(r"^(async\s+)?func\s+main\s*\(", re.MULTILINE)
RUN_TIMEOUT = 30
BUILD_TIMEOUT = 600


@dataclass
class Block:
    path: Path
    line: int          # 1-based line of the opening fence
    markers: tuple[str, ...]
    body: str
    shown_output: str | None = None  # the output block that follows it, if any
    result: str = ""
    detail: str = ""
    seconds: float = 0.0
    ran: bool = False
    source: str = field(default="", repr=False)

    @property
    def name(self) -> str:
        try:
            return f"{self.path.relative_to(ROOT)}:{self.line}"
        except ValueError:
            return f"{self.path}:{self.line}"

    @property
    def kind(self) -> str:
        if "fragment" in self.markers:
            return "fragment"
        if "error" in self.markers:
            return "error"
        return "program" if MAIN.search(self.body) else "wrapped"


def extract_blocks(path: Path) -> list[Block]:
    """The Lam fenced blocks of a Markdown file (indented ones too)."""
    lines = path.read_text(encoding="utf-8").split("\n")
    blocks: list[Block] = []
    i = 0
    while i < len(lines):
        m = FENCE_OPEN.match(lines[i])
        if not m:
            i += 1
            continue
        indent, fence, info = m.group("indent"), m.group("fence"), m.group("info").split()
        close = re.compile(r"^[ \t]*" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}[ \t]*$")
        j = i + 1
        while j < len(lines) and not close.match(lines[j]):
            j += 1
        if info and info[0] in LANGS:
            unknown = [w for w in info[1:] if w not in MARKERS]
            if unknown:
                raise SystemExit(f"{path}:{i + 1}: unknown doc-example marker {unknown}")
            body = "\n".join(l[len(indent):] if l.startswith(indent) else l.lstrip() for l in lines[i + 1:j])
            blocks.append(Block(path, i + 1, tuple(info[1:]), body + "\n", shown_output(lines, j + 1)))
        i = j + 1
    return blocks


def shown_output(lines: list[str], k: int) -> str | None:
    """The output of a ``text`` (or unlabelled) block right after a Lam block
    whose first line runs it (``$ lamc main.lam --run``), or None."""
    while k < len(lines) and not lines[k].strip():
        k += 1
    m = FENCE_OPEN.match(lines[k]) if k < len(lines) else None
    if not m or m.group("info").strip() not in ("", "text", "console") or k + 1 >= len(lines):
        return None
    command = lines[k + 1].strip()
    if not (command.startswith("$ ") and "--run" in command.split()):
        return None
    out = []
    for line in lines[k + 2:]:
        if FENCE_OPEN.match(line) and line.strip().strip(m.group("fence")[0]) == "":
            break
        out.append(line[len(m.group("indent")):])
    return "\n".join(out).rstrip("\n")


def expected_output(block: Block) -> str | None:
    """The comment lines after ``# Output:`` in the block, else the output
    block shown after it, else None."""
    lines = block.body.split("\n")
    for k, line in enumerate(lines):
        if line.strip() == "# Output:":
            out = []
            for follow in lines[k + 1:]:
                text = follow.strip()
                if not text.startswith("#"):
                    break
                out.append(text[2:] if text.startswith("# ") else text[1:])
            return "\n".join(out).rstrip("\n")
    return block.shown_output


def expected_errors(body: str) -> list[str]:
    """The messages of the ``# error: <message>`` comments of an ``error``
    block (a trailing ``...`` elides the rest of the message)."""
    found = []
    for m in re.finditer(r"(?:#|//)\s*error:\s*(.+)$", body, re.MULTILINE):
        text = m.group(1).strip()
        if text.endswith("..."):
            text = text[:-3].rstrip(" :")
        if text:
            found.append(text)
    return found


def complete_program(body: str) -> str:
    """A buildable program from a block: declarations at the top level, the
    other top-level lines in ``func main()``."""
    if MAIN.search(body):
        return body
    decls: list[str] = []
    stmts: list[str] = []
    target = decls
    in_comment = False  # inside a ``#- ... -#`` block comment
    for line in body.split("\n"):
        if in_comment:
            in_comment = "-#" not in line
        elif line.lstrip().startswith("#-") and "-#" not in line.lstrip()[2:]:
            in_comment = True
        elif line and not line[0].isspace() and line[0] not in "})]#":
            target = decls if DECLARATION.match(line) else stmts
        target.append(line)
    # Not re-indented: braces delimit the blocks, and a triple-quoted string
    # must keep its lines as written.
    main_body = "\n".join(stmts).rstrip()
    return "\n".join(decls).rstrip() + "\n\nfunc main() {\n" + (main_body + "\n" if main_body.strip() else "") + "}\n"


def check(block: Block, tmp_root: Path) -> Block:
    start = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="doc_", dir=tmp_root))
    source = work / "main.lam"
    block.source = complete_program(block.body)
    source.write_text(block.source, encoding="utf-8")
    binary = work / "main"
    env = {**os.environ, "LAMC_WORKERS": "1"}
    env.pop("LAMC_TRACEBACK", None)
    try:
        build = subprocess.run(
            [sys.executable, str(COMPILER), "build", str(source), "-o", str(binary)],
            capture_output=True, text=True, timeout=BUILD_TIMEOUT, cwd=work, env=env,
        )
    except subprocess.TimeoutExpired:
        block.result, block.detail = "FAIL", "build timed out"
        return block
    out = build.stdout + build.stderr
    block.seconds = time.monotonic() - start
    if block.kind == "error":
        if build.returncode == 0:
            block.result, block.detail = "FAIL", "marked `error` but lamc accepted it"
        elif "Traceback (most recent call last)" in out or "internal compiler error" in out:
            block.result, block.detail = "FAIL", "marked `error` but lamc crashed:\n" + out[-3000:]
        else:
            missing = [m for m in expected_errors(block.body) if m not in out]
            if missing:
                block.result = "FAIL"
                block.detail = f"diagnostic not reported: {missing}\n{out[-3000:]}"
            else:
                block.result = "PASS"
        return block
    if build.returncode != 0:
        block.result, block.detail = "FAIL", "build failed:\n" + out[-4000:]
        return block
    expected = expected_output(block)
    block.result = "PASS"
    if expected is not None:
        block.ran = True
        try:
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=RUN_TIMEOUT, cwd=work,
                                 stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            block.result, block.detail = "FAIL", "run timed out"
            return block
        actual = run.stdout.rstrip("\n")
        if run.returncode != 0:
            block.result, block.detail = "FAIL", f"exit status {run.returncode}:\n{run.stderr[-3000:]}"
        elif actual != expected:
            block.result = "FAIL"
            block.detail = f"output differs\n--- expected\n{expected}\n--- actual\n{actual}"
    block.seconds = time.monotonic() - start
    return block


def self_test() -> None:
    """The extraction and completion rules on fixed inputs."""
    with tempfile.TemporaryDirectory() as tmp:
        md = Path(tmp) / "x.md"
        md.write_text(
            "```lammergeier\nprint(1)\n```\n\n```text\n$ lamc main.lam --run\n1\n```\n\n"
            "- item\n\n  ```lam fragment\n  x\n  ```\n\n```go\nfunc main() {}\n```\n\n"
            "```lammergeier error\nx: int = \"s\"   # error: cannot assign ...\n```\n",
            encoding="utf-8",
        )
        blocks = extract_blocks(md)
    assert [(b.line, b.kind) for b in blocks] == [(1, "wrapped"), (12, "fragment"), (20, "error")], blocks
    assert blocks[0].shown_output == "1" and blocks[1].body == "x\n", blocks
    assert blocks[1].shown_output is None and blocks[2].shown_output is None, blocks
    assert expected_errors(blocks[2].body) == ["cannot assign"], blocks[2].body
    body = (
        'from lamfmt import Fmt\n\n#- a\nnote -#\nx: int = 1\nfunc f() -> int {\n    return 2\n}\n'
        's: str = """a\n  b"""\nprint(x, f(), s)\n# Output:\n# 1 2 a\n#   b\n#\n'
    )
    program = complete_program(body)
    assert program.startswith("from lamfmt import Fmt\n\n#- a\nnote -#\n"), program
    assert "func f() -> int {\n    return 2\n}\n\nfunc main() {\n" in program, program
    in_main = program.split("func main()")[1]
    assert "\nx: int = 1\n" in in_main and 's: str = """a\n  b"""' in in_main, program
    assert expected_output(Block(Path("x"), 1, (), body)) == "1 2 a\n  b"
    assert complete_program("func main() {\n}\n") == "func main() {\n}\n"


def collect(filters: list[str]) -> list[Block]:
    blocks = [b for path in DOC_FILES for b in extract_blocks(path)]
    if filters:
        blocks = [b for b in blocks if any(f in b.name for f in filters)]
    return blocks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--filter", action="append", default=[], help="substring of `file:line` (repeatable)")
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("LAM_DOC_EXAMPLES_JOBS", "0")) or
                        min(16, os.cpu_count() or 4))
    parser.add_argument("--list", action="store_true", help="print the classification and exit")
    parser.add_argument("--show", help="print the program built for a block (`file:line`) and exit")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    self_test()
    blocks = collect(args.filter)
    if args.show:
        for b in collect([args.show]):
            print(f"// {b.name} ({b.kind})\n{complete_program(b.body)}")
        return 0
    if args.list:
        for b in blocks:
            out = " output" if expected_output(b) is not None else ""
            print(f"{b.kind:9}{out:7} {b.name}")
        return 0

    checked = [b for b in blocks if b.kind != "fragment"]
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="lam_doc_examples_") as tmp:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            results = list(pool.map(lambda b: check(b, Path(tmp)), checked))
    elapsed = time.monotonic() - start

    failures = [b for b in results if b.result != "PASS"]
    for b in results:
        if b.result != "PASS":
            print(f"FAIL: {b.name} ({b.kind}): {b.detail}")
            if args.verbose:
                print("    program:\n" + "\n".join("    | " + l for l in b.source.split("\n")))
        elif args.verbose:
            print(f"PASS: {b.name} ({b.kind}{', ran' if b.ran else ''}, {b.seconds:.1f}s)")
    kinds = {k: sum(1 for b in blocks if b.kind == k) for k in ("program", "wrapped", "error", "fragment")}
    ran = sum(1 for b in results if b.ran)
    print(
        f"doc examples: {len(blocks)} Lam blocks — {kinds['program']} programs, {kinds['wrapped']} completed "
        f"into programs, {kinds['error']} expected errors, {kinds['fragment']} fragments skipped; "
        f"{len(results)} built, {ran} run and compared with their output, {len(failures)} failed "
        f"({elapsed:.0f}s)"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
