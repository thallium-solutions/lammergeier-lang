#!/usr/bin/env python3
"""Tests for the ``@scope/name`` import syntax + resolver path.

Three layers get exercised here:

1. **Grammar.** The Lark parser must recognise
   ``from @alice/lamwebp import Foo`` as a valid import statement
   and yield a ``scoped_name`` AST node so downstream tools can
   tell scoped from plain imports.
2. **Resolver.** ``compile_tpy`` must locate the library at
   ``<extlibs>/@scope/name/__init__.lam`` (or
   ``<extlibs>/@scope/name.lam``) and bundle its Go source into
   the same binary.
3. **Static analysis.** ``compiler.semantic`` must NOT crash when
   it walks an ``import_from`` whose left-hand side is a
   ``scoped_name`` rather than a ``dotted_name``.

Run with::

    python3 tests/tests/test_scoped_imports.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]


# ── Grammar-level checks ────────────────────────────────────

def test_parser_accepts_scoped_import() -> None:
    """The parser yields a ``scoped_name`` node, never silently
    splits ``@`` and ``alice`` into two tokens."""
    from compiler.lammergeier import auto_semicolons, create_parser
    from lark import Tree
    parser = create_parser()
    pre = auto_semicolons(
        "from @alice/lamwebp import Foo\nfunc main() { print(0) }\n")
    tree = parser.parse(pre + "\n")

    # Walk and confirm exactly one scoped_name child sits under an
    # ``import_from``. ``dotted_name`` must NOT appear in this tree.
    found_scoped = []
    found_dotted = []
    def walk(n):
        if not isinstance(n, Tree):
            return
        if n.data == "scoped_name":
            found_scoped.append(str(n.children[0]))
        if n.data == "dotted_name":
            for kid in n.children:
                if isinstance(kid, Tree):
                    found_dotted.append(str(kid.children[0]))
        for c in n.children:
            walk(c)
    walk(tree)
    assert found_scoped == ["@alice/lamwebp"], found_scoped
    assert "alice" not in found_dotted, found_dotted
    print("PASS: parser accepts scoped import")


def test_parser_accepts_case_preserving_scope() -> None:
    """Scoped package tokens preserve camelCase/PascalCase in both
    the scope and package segment."""
    from compiler.lammergeier import auto_semicolons, create_parser
    from lark import Tree
    parser = create_parser()
    pre = auto_semicolons("from @AliceTeam/LamWebP import Decoder\n")
    tree = parser.parse(pre + "\n")
    scoped = [
        str(node.children[0])
        for node in tree.iter_subtrees_topdown()
        if isinstance(node, Tree) and node.data == "scoped_name"
    ]
    assert scoped == ["@AliceTeam/LamWebP"], scoped
    print("PASS: parser preserves scoped package casing")


# ── Resolution + compile end-to-end ─────────────────────────

def _write_lib(extlibs: Path, scoped_name: str, body: str) -> Path:
    """Materialise a ``@scope/name`` library on disk under
    ``extlibs/<scoped_name>/__init__.lam``."""
    lib_dir = extlibs / scoped_name
    lib_dir.mkdir(parents=True, exist_ok=True)
    init = lib_dir / "__init__.lam"
    init.write_text(body, encoding="utf-8")
    return init


def _write_main(d: Path, body: str) -> Path:
    p = d / "main.lam"
    p.write_text(body, encoding="utf-8")
    return p


def _compile(main: Path, extlibs: Path, *, run: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("LAMC_EXTLIBS", None)
    args = LAMC + ["build", "--extlibs", str(extlibs)]
    if run:
        args.append("--run")
    args.append(str(main))
    return subprocess.run(args, capture_output=True, text=True)


def test_scoped_lib_resolves_and_runs() -> None:
    """End-to-end: a ``@scope/name`` library is found under
    ``extlibs/@scope/name/__init__.lam`` and its function is
    callable from the main program."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ext = d / "extlibs"
        _write_lib(ext, "@alice/lamhello",
                   "func greet() -> str { return 'hi-from-scoped' }\n")
        main = _write_main(d, (
            "from @alice/lamhello import greet\n"
            "func main() { print(greet()) }\n"
        ))
        proc = _compile(main, ext)
        assert proc.returncode == 0, \
            f"compile failed: {proc.stdout}\n{proc.stderr}"
        assert proc.stdout.strip() == "hi-from-scoped", \
            f"got: {proc.stdout!r}"
    print("PASS: scoped library resolves + runs")


def test_two_scoped_libs_in_same_program() -> None:
    """Two libraries from different scopes must coexist; their
    Go-side files are written under unambiguous flattened names so
    a build with both succeeds."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ext = d / "extlibs"
        _write_lib(ext, "@alice/colors",
                   "func red(s: str) -> str { return '[r]' + s }\n")
        _write_lib(ext, "@bob/colors",
                   "func blue(s: str) -> str { return '[b]' + s }\n")
        main = _write_main(d, (
            "from @alice/colors import red\n"
            "from @bob/colors   import blue\n"
            "func main() {\n"
            "    print(red(\"x\"))\n"
            "    print(blue(\"y\"))\n"
            "}\n"
        ))
        proc = _compile(main, ext)
        assert proc.returncode == 0, \
            f"compile failed: {proc.stdout}\n{proc.stderr}"
        out = proc.stdout.strip().splitlines()
        assert out == ["[r]x", "[b]y"], out
    print("PASS: two scoped libs coexist")


def test_scoped_lib_alongside_plain_lib() -> None:
    """A scoped import and a plain import in the same source must
    both resolve; the resolver should not get confused by the
    leading ``@`` on the scoped path."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ext = d / "extlibs"
        _write_lib(ext, "@alice/lamhi",
                   "func say_hi() -> str { return 'hi' }\n")
        # Plain library: extlibs/lamplain.lam (single-file form)
        plain = ext / "lamplain.lam"
        plain.parent.mkdir(parents=True, exist_ok=True)
        plain.write_text(
            "func say_plain() -> str { return 'plain' }\n",
            encoding="utf-8")
        main = _write_main(d, (
            "from @alice/lamhi import say_hi\n"
            "from lamplain     import say_plain\n"
            "func main() {\n"
            "    print(say_hi())\n"
            "    print(say_plain())\n"
            "}\n"
        ))
        proc = _compile(main, ext)
        assert proc.returncode == 0, \
            f"compile failed: {proc.stdout}\n{proc.stderr}"
        assert proc.stdout.strip() == "hi\nplain", proc.stdout.strip()
    print("PASS: scoped + plain libs coexist")


def test_mixed_case_scoped_and_file_modules_run() -> None:
    """Manifest/import casing is exact and case-preserving for both a
    scoped package directory and a plain PascalCase ``.lam`` filename."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ext = d / "extlibs"
        _write_lib(ext, "@AliceTeam/LamHello",
                   "func scopedTag() -> str { return 'Scoped' }\n")
        plain = ext / "PascalCaseLib.lam"
        plain.write_text(
            "func plainTag() -> str { return 'Plain' }\n",
            encoding="utf-8",
        )
        main = _write_main(d, (
            "from @AliceTeam/LamHello import scopedTag\n"
            "from PascalCaseLib import plainTag\n"
            "func main() { print(scopedTag() + plainTag()) }\n"
        ))
        proc = _compile(main, ext)
        assert proc.returncode == 0, f"compile failed: {proc.stdout}\n{proc.stderr}"
        assert proc.stdout.strip() == "ScopedPlain", proc.stdout
    print("PASS: mixed-case scoped and file modules compile + run")


def test_missing_scoped_lib_fails_when_used() -> None:
    """A scoped import that names a non-existent library must
    produce a build failure as soon as the imported symbol is
    actually called — the resolver can't find it, so the Go pass
    has nothing to bind ``x`` to.

    (We deliberately don't assert on the case where the symbol is
    imported but never used; the existing compiler silently drops
    unused imports for both scoped and plain names — that's an
    orthogonal optimisation, not a regression in scoped-import
    handling.)"""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ext = d / "extlibs"
        ext.mkdir()
        main = _write_main(d, (
            "from @nobody/nope import missing_fn\n"
            "func main() { print(missing_fn()) }\n"
        ))
        proc = _compile(main, ext)
        assert proc.returncode != 0, \
            f"expected failure but got success:\n{proc.stdout}\n{proc.stderr}"
    print("PASS: missing scoped lib fails when its symbol is used")


# ── Driver ───────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_parser_accepts_scoped_import,
        test_parser_accepts_case_preserving_scope,
        test_scoped_lib_resolves_and_runs,
        test_two_scoped_libs_in_same_program,
        test_scoped_lib_alongside_plain_lib,
        test_mixed_case_scoped_and_file_modules_run,
        test_missing_scoped_lib_fails_when_used,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL: {t.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"ERROR: {t.__name__}: {exc!r}")
    print()
    print("=" * 60)
    if failures:
        print(f"Scoped imports: {failures} of {len(tests)} tests failed")
        return 1
    print(f"Scoped imports: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
