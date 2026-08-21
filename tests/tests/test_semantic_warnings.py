#!/usr/bin/env python3
"""Tests for Phase-2 warn-don't-error diagnostics.

Covers:

* unused-import warnings (top-level ``from X import Y`` where ``Y``
  is never referenced);
* unused-parameter warnings, with the leading-``_`` Pythonic
  opt-out;
* the build still proceeds when only warnings fired;
* the transpiler's ``_ = name`` epilogue suppresses Go's
  ``declared and not used`` error for function-scope locals so
  warn-not-error semantics actually hold at the Go layer;
* unused manifest-dependency warnings — the project-level
  analog that compares ``[dependencies]`` keys against the
  union of imports across every ``.lam`` under the project
  root.

These all run via the ``lamc`` CLI subprocess so we exercise the
real wiring (semantic check → render_warnings → stderr) instead of
the in-process API.

Run with::

    python3 tests/tests/test_semantic_warnings.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]


# ── Helpers ──────────────────────────────────────────────────

def _run(*args, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    """Invoke ``lamc`` with the given args. Captures stdout + stderr.

    ``cwd`` lets manifest-discovery tests pin themselves to a
    project root rather than picking up the lammergeier-lang repo's
    own ``lamlib.toml`` (if any).
    """
    return subprocess.run(
        LAMC + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── Phase 2.1 — unused import ───────────────────────────────

def test_unused_import_emits_warning() -> None:
    """``from lamX import Foo`` where ``Foo`` is never referenced
    must emit a ``warning: unused import`` diagnostic. The build
    itself still proceeds because warnings don't abort transpile.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
from lamstrings import Strings;

func main() {
    print("hi");
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused import `Strings`" in r.stderr, r.stderr
        # The Go emission still happened — warnings don't abort.
        assert "package main" in r.stdout, r.stdout
    print("PASS: unused import emits a warning, build proceeds")


def test_used_import_does_not_warn() -> None:
    """The negative case: when the import is actually used, no
    warning fires."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
from lamstrings import Strings;

func main() {
    print(Strings.toUpper("hi"));
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused import" not in r.stderr, r.stderr
    print("PASS: used import does not warn")


def test_import_used_in_generic_return_type_does_not_warn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        _write(proj / "core.lam", """
class MonsterProjection {
    func init(self) {
    }
}
""".lstrip())
        _write(proj / "main.lam", """
from core import MonsterProjection;

func projectedMonsters() -> list[MonsterProjection] {
    return [];
}
""".lstrip())
        r = _run(str(proj / "main.lam"), "--emit-go", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused import `MonsterProjection`" not in r.stderr, r.stderr
    print("PASS: imported generic return type does not warn")


def test_import_used_in_interface_return_type_does_not_warn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        _write(proj / "core.lam", """
class Rect {
    func init(self) {
    }
}
""".lstrip())
        _write(proj / "main.lam", """
from core import Rect;

interface Drawable {
    func project(self) -> Rect;
}
""".lstrip())
        r = _run(str(proj / "main.lam"), "--emit-go", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused import `Rect`" not in r.stderr, r.stderr
    print("PASS: imported interface return type does not warn")


def test_missing_import_export_suggests_close_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        _write(proj / "libfoo.lam", """
class S3Client {
}
""".lstrip())
        _write(proj / "main.lam", """
from libfoo import S3Clinet;

func main() {
    print("hi");
}
""".lstrip())
        r = _run(str(proj / "main.lam"), "--emit-go", cwd=proj)
        assert r.returncode != 0, (r.returncode, r.stderr)
        assert "module `libfoo` does not export `S3Clinet`" in r.stderr, r.stderr
        assert "did you mean `S3Client`" in r.stderr, r.stderr
    print("PASS: missing import export suggests close name")


# ── Phase 2.2 — unused parameter ────────────────────────────

def test_unused_parameter_emits_warning() -> None:
    """A parameter the body never references emits an unused-
    parameter warning. The ``self`` and ``cls`` receivers are
    deliberately exempt so methods don't get noisy."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
func greet(name, age) {
    print(name);
}

func main() {
    greet("alice", 30);
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused parameter `age`" in r.stderr, r.stderr
        assert "unused parameter `name`" not in r.stderr, r.stderr
    print("PASS: unused parameter emits a warning")


def test_underscore_param_silences_warning() -> None:
    """The Pythonic ``_unused`` opt-out: any parameter whose name
    starts with ``_`` is treated as deliberately unused."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
func greet(name, _age) {
    print(name);
}

func main() {
    greet("alice", 30);
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused parameter" not in r.stderr, r.stderr
    print("PASS: leading-_ param silences the warning")


def test_self_does_not_warn() -> None:
    """Class methods get an implicit ``self`` even when they don't
    touch it — that's a language convention, not a code smell, and
    must never emit a warning."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
class Counter {
    func bump() {
        print("bumped");
    }
}

func main() {
    c = Counter();
    c.bump();
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused parameter `self`" not in r.stderr, r.stderr
    print("PASS: implicit `self` never warns")


# ── Phase 2.4 — Go ``declared and not used`` is silenced ─────

def test_unused_local_does_not_break_go_build() -> None:
    """The interesting interaction: Lam emits a warning for the
    unused local *and* the transpiler emits ``_ = name`` so Go's
    ``declared and not used`` error doesn't reject the build.
    Without this epilogue, warn-don't-error would be a lie at the
    Go layer.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
func compute() {
    x = 42;
    y = 99;
    print(y);
}

func main() {
    compute();
}
""".lstrip())
        # ``--emit-go`` skips ``go build`` so we test the silencer
        # text directly. Then ``--run`` exercises the full pipeline
        # to prove Go actually accepts the output.
        emit = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert emit.returncode == 0, (emit.returncode, emit.stderr)
        assert "_ = x" in emit.stdout, emit.stdout
        assert "_ = y" not in emit.stdout, emit.stdout

        run = _run(str(src), "--run", cwd=Path(tmp))
        assert run.returncode == 0, (run.returncode, run.stderr)
        assert "99" in run.stdout, run.stdout
    print("PASS: unused local silenced for go build, semantic warning still surfaces")


def test_unused_block_local_does_not_break_go_build() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
func main() {
    if True {
        inside: int = 42;
    }
    if True {
        _ignored: int = 99;
    }
    print("ok");
}
""".lstrip())
        emit = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert emit.returncode == 0, (emit.returncode, emit.stderr)
        assert "unused local `inside`" in emit.stderr, emit.stderr
        assert "unused local `_ignored`" not in emit.stderr, emit.stderr
        assert "_ = inside" in emit.stdout, emit.stdout
        assert "_ = _ignored" in emit.stdout, emit.stdout

        run = _run(str(src), "--run", cwd=Path(tmp))
        assert run.returncode == 0, (run.returncode, run.stderr)
        assert "ok" in run.stdout, run.stdout
    print("PASS: unused block local warns and is silenced for go build")


def test_go_block_references_silence_unused_parameters() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
go! { import "fmt" }

func show(x: int) {
    go! {
        fmt.Println(x)
    }
}

func main() {
    show(7);
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused parameter `x`" not in r.stderr, r.stderr
    print("PASS: go! references count as parameter usage")


def test_unused_warning_line_numbers_survive_multiline_go_blocks() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
go! {
    import "fmt"
}

func later(unused: int) {
    print("ok");
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "line 5: warning[unused]: unused parameter `unused`" in r.stderr, r.stderr
        assert ">>>    5 | func later(unused: int) {" in r.stderr, r.stderr
    print("PASS: multiline go! blocks preserve diagnostic line numbers")


def test_outer_scope_local_used_inside_loop_block_does_not_warn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
func main() {
    running: bool = true;
    while running {
        if running {
            running = false;
        }
    }
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused local `running`" not in r.stderr, r.stderr
    print("PASS: outer-scope locals used inside loop blocks do not warn")


def test_attribute_assignment_receiver_counts_as_parameter_use() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
class Monster {
    func init(self) {
        self.alive: bool = true;
    }
}

class ShooterGame {
    func killMonster(self, monster: Monster) {
        monster.alive = false;
    }
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused parameter `monster`" not in r.stderr, r.stderr
    print("PASS: attribute assignment receiver counts as parameter use")


def test_subscript_assignment_container_counts_as_local_use() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
func main() {
    xs: list[int] = [1];
    xs[0] = 2;
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "unused local `xs`" not in r.stderr, r.stderr
    print("PASS: subscript assignment container counts as local use")


# ── Phase 2 CLI — errors still abort, warnings don't ────────

def test_genuine_error_still_aborts_build() -> None:
    """Sanity check: warning-friendly diagnostics didn't break the
    existing error path. An undefined-name reference must still
    abort with a non-zero exit code.
    """
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "main.lam"
        _write(src, """
func main() {
    print(undefined_thing);
}
""".lstrip())
        r = _run(str(src), "--emit-go", cwd=Path(tmp))
        assert r.returncode != 0, (r.returncode, r.stderr)
        assert "undefined name `undefined_thing`" in r.stderr, r.stderr
    print("PASS: real semantic errors still abort the build")


# ── Phase 2.5 — unused manifest dep warning ─────────────────

def test_unused_manifest_dep_warns() -> None:
    """A ``[dependencies]`` entry that no ``.lam`` in the project
    actually imports must emit a project-level warning — the
    manifest-vs-imports analog of the per-file unused-import check.
    """
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        _write(proj / "lamlib.toml", """
[library]
name = "myproj"
version = "0.1.0"

[dependencies]
lamwebp = "^1.0"
lamhttp = "^1.0"
""".lstrip())
        _write(proj / "main.lam", """
from lamhttp import HttpServer;

func main() {
    print(HttpServer);
}
""".lstrip())
        r = _run(str(proj / "main.lam"), "--emit-go", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "lamwebp" in r.stderr, r.stderr
        assert "declared in lamlib.toml" in r.stderr, r.stderr
        # Used dep doesn't trigger the warning.
        assert "- lamhttp:" not in r.stderr, r.stderr
    print("PASS: unused manifest dependency emits a warning")


def test_multifile_project_does_not_false_positive() -> None:
    """A dep that the *main* file doesn't import directly but a
    sibling ``.lam`` under the project root does is still a
    *used* dep — the project-level scan must aggregate imports
    across the whole tree."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        _write(proj / "lamlib.toml", """
[library]
name = "myproj"
version = "0.1.0"

[dependencies]
lamhttp = "^1.0"
""".lstrip())
        # Main file uses nothing from the manifest.
        _write(proj / "main.lam", """
func main() {
    print("hi");
}
""".lstrip())
        # Sibling file under ``lib/`` is the actual user of lamhttp.
        _write(proj / "lib" / "api.lam", """
from lamhttp import HttpServer;

class Api {
    func handle() {
        s = HttpServer();
        print(s);
    }
}
""".lstrip())
        r = _run(str(proj / "main.lam"), "--emit-go", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "lamhttp" not in r.stderr or "unused import" in r.stderr, r.stderr
        # The "manifest dependency not imported" line must not name lamhttp.
        assert "- lamhttp:" not in r.stderr, r.stderr
    print("PASS: multi-file project does not false-positive on root-level miss")


def test_extlibs_imports_do_not_count_as_user_usage() -> None:
    """Imports inside ``extlibs/`` (third-party installed libraries)
    must NOT count towards the user's manifest. Otherwise installing
    a library would silently silence the warning that prompted the
    install in the first place.
    """
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        _write(proj / "lamlib.toml", """
[library]
name = "myproj"
version = "0.1.0"

[dependencies]
lamhttp = "^1.0"
""".lstrip())
        _write(proj / "main.lam", """
func main() {
    print("hi");
}
""".lstrip())
        # An installed library "uses" lamhttp internally — but that
        # shouldn't count towards the user's manifest declaration.
        _write(proj / "extlibs" / "lamfoo" / "lamlib.toml", """
[library]
name = "lamfoo"
version = "1.0.0"
""".lstrip())
        _write(proj / "extlibs" / "lamfoo" / "lamfoo.lam", """
from lamhttp import Server;
""".lstrip())
        r = _run(str(proj / "main.lam"), "--emit-go", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert "- lamhttp:" in r.stderr, r.stderr
    print("PASS: extlibs/ imports don't satisfy the user's manifest")


# ── Driver ──────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_unused_import_emits_warning,
        test_used_import_does_not_warn,
        test_import_used_in_generic_return_type_does_not_warn,
        test_import_used_in_interface_return_type_does_not_warn,
        test_missing_import_export_suggests_close_name,
        test_unused_parameter_emits_warning,
        test_underscore_param_silences_warning,
        test_self_does_not_warn,
        test_unused_local_does_not_break_go_build,
        test_unused_block_local_does_not_break_go_build,
        test_go_block_references_silence_unused_parameters,
        test_unused_warning_line_numbers_survive_multiline_go_blocks,
        test_outer_scope_local_used_inside_loop_block_does_not_warn,
        test_attribute_assignment_receiver_counts_as_parameter_use,
        test_subscript_assignment_container_counts_as_local_use,
        test_genuine_error_still_aborts_build,
        test_unused_manifest_dep_warns,
        test_multifile_project_does_not_false_positive,
        test_extlibs_imports_do_not_count_as_user_usage,
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
        print(f"Semantic warnings: {failures} of {len(tests)} tests failed")
        return 1
    print(f"Semantic warnings: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
