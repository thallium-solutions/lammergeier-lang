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


def test_parser_accepts_nested_scoped_import() -> None:
    """Official package families can add path segments after the package
    root without giving up the scoped namespace."""
    from compiler.lammergeier import auto_semicolons, create_parser
    from lark import Tree
    parser = create_parser()
    pre = auto_semicolons(
        "from @lam/vulture/std-plugin import requestLog\n"
    )
    tree = parser.parse(pre + "\n")
    scoped = [
        str(node.children[0])
        for node in tree.iter_subtrees_topdown()
        if isinstance(node, Tree) and node.data == "scoped_name"
    ]
    assert scoped == ["@lam/vulture/std-plugin"], scoped
    print("PASS: parser accepts nested scoped package paths")


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


def test_nested_scoped_lib_resolves_and_runs() -> None:
    """Nested package paths resolve beneath the scoped package root."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ext = d / "extlibs"
        _write_lib(ext, "@lam/vulture/std-plugin",
                   "func pluginTag() -> str { return 'std-plugin' }\n")
        main = _write_main(d, (
            "from @lam/vulture/std-plugin import pluginTag\n"
            "func main() { print(pluginTag()) }\n"
        ))
        proc = _compile(main, ext)
        assert proc.returncode == 0, \
            f"compile failed: {proc.stdout}\n{proc.stderr}"
        assert proc.stdout.strip() == "std-plugin", proc.stdout
    print("PASS: nested scoped library resolves + runs")


def test_nested_scoped_reexport_keeps_leaf_context() -> None:
    """A package re-export must transpile its relative leaf as that leaf.

    This covers defaulted parameters referenced by raw Go, an underscore-private
    helper class in the same leaf, and compact grouped Go imports.
    """
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ext = d / "extlibs"
        init = _write_lib(ext, "@lam/vulture/std-plugin",
                          "from .impl import configure\n")
        (ext / "dependency.lam").write_text(
            "class Dependency {\n"
            "    func init(self, value: int, increment: int = 5) {\n"
            "        self.value: int = value\n"
            "        self.increment: int = increment\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        (init.parent / "impl.lam").write_text(
            "from dependency import Dependency\n"
            "go! { import ( \"fmt\" ) }\n"
            "class _Hooks {\n"
            "    func init(self, offset: int) { self.offset: int = offset }\n"
            "}\n"
            "func configure(value: int = 7) {\n"
            "    #- A canonical doc terminator must not consume this function body.\n"
            "    -#\n"
            "    hooks: _Hooks = _Hooks(value)\n"
            "    dependency: Dependency = Dependency(value)\n"
            "    go! { fmt.Println(value + hooks.Offset + dependency.Increment) }\n"
            "}\n",
            encoding="utf-8",
        )
        main = _write_main(d, (
            "from @lam/vulture/std-plugin import configure\n"
            "func main() { configure() }\n"
        ))
        proc = _compile(main, ext)
        assert proc.returncode == 0, f"compile failed: {proc.stdout}\n{proc.stderr}"
        assert proc.stdout.strip() == "19", proc.stdout
    print("PASS: nested scoped re-export preserves leaf transpilation context")


def test_distinct_packages_can_reexport_same_relative_leaf_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ext = d / "extlibs"
        alpha = _write_lib(ext, "@alice/alpha", "from .impl import alphaValue\n")
        beta = _write_lib(ext, "@bob/beta", "from .impl import betaValue\n")
        (alpha.parent / "impl.lam").write_text(
            "func alphaValue() -> str { return \"alpha\" }\n", encoding="utf-8")
        (beta.parent / "impl.lam").write_text(
            "func betaValue() -> str { return \"beta\" }\n", encoding="utf-8")
        main = _write_main(d, (
            "from @alice/alpha import alphaValue\n"
            "from @bob/beta import betaValue\n"
            "func main() { print(alphaValue() + \"-\" + betaValue()) }\n"
        ))
        proc = _compile(main, ext)
        assert proc.returncode == 0, f"compile failed: {proc.stdout}\n{proc.stderr}"
        assert proc.stdout.strip() == "alpha-beta", proc.stdout
    print("PASS: package-relative leaf names are resolved by physical module")


def test_imported_library_calls_external_instance_overload() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        ext = d / "extlibs"
        ext.mkdir()
        (ext / "client.lam").write_text(
            "class Client {\n"
            "    func send(self, value: str) -> str { return \"str:\" + value }\n"
            "    func send(self, value: int) -> str { return \"int:\" + str(value) }\n"
            "}\n",
            encoding="utf-8",
        )
        _write_lib(ext, "@alice/wrapper", (
            "from client import Client\n"
            "class Wrapper {\n"
            "    func init(self) { self.client: Client = Client() }\n"
            "    func emit(self) -> str { return self.client.send(7) }\n"
            "}\n"
        ))
        main = _write_main(d, (
            "from @alice/wrapper import Wrapper\n"
            "func main() { print(Wrapper().emit()) }\n"
        ))
        proc = _compile(main, ext)
        assert proc.returncode == 0, f"compile failed: {proc.stdout}\n{proc.stderr}"
        assert proc.stdout.strip() == "int:7", proc.stdout
    print("PASS: imported libraries resolve external instance overloads")


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
        test_parser_accepts_nested_scoped_import,
        test_scoped_lib_resolves_and_runs,
        test_nested_scoped_lib_resolves_and_runs,
        test_nested_scoped_reexport_keeps_leaf_context,
        test_distinct_packages_can_reexport_same_relative_leaf_name,
        test_imported_library_calls_external_instance_overload,
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
