#!/usr/bin/env python3
"""Crash-tests for the third-party dependency resolver.

Four high-leverage scenarios the user wants documented behaviour
for, exercised end-to-end through the install CLI + the reference
registry server (just like ``test_install_cli.py``):

1. **Transitive Lam deps.** A lib that pulls in another lib via
   ``[dependencies]`` gets the dep auto-installed; the consumer
   project never has to ask for the dep by name.

2. **Two libs, incompatible Lam-dep versions.** ``lib_a`` wants
   ``lamhttp ^1`` and ``lib_b`` wants ``lamhttp ^2``. The installer
   refuses both and surfaces a :class:`DependencyConflict`.

3. **Project vs lib Lam-dep overlap.** The project's own
   ``lamlib.toml`` already pins a dep at one major; a lib comes
   along requiring a different major. The installer reports the
   project-vs-lib conflict (vs silently downgrading the lib).

4. **Go-module variants of 1–3.** ``[go-deps]`` is the parallel
   surface for Go modules; conflict shape is "different majors of
   the same path", which Go itself can't deduplicate.

Each test stands up a fresh registry on an ephemeral port and
publishes the libraries it needs before exercising the install
flow — so all four scenarios run hermetically without polluting
the outer environment.

Run with::

    /usr/bin/python3 tests/tests/test_dependency_crash.py
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Redirect the install cache to a per-session tmpdir so test runs
# don't pollute the developer's real ``~/.lammergeier/cache``. The
# subprocesses spawned by ``LAMC + ["install", ...]`` inherit this
# environment automatically.
_TEST_CACHE = tempfile.mkdtemp(prefix="lamccache-")
os.environ["LAMC_CACHE"] = _TEST_CACHE
atexit.register(lambda: shutil.rmtree(_TEST_CACHE, ignore_errors=True))

LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]
SERVER = ROOT / "tools" / "registry" / "server.py"


# ── Registry fixture (mirrors test_install_cli.py) ──────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def registry():
    """Boot the reference registry on an ephemeral port and yield
    its base URL. Identical to the install-CLI test fixture."""
    with tempfile.TemporaryDirectory(prefix="lamcreg-") as tmp:
        port = _free_port()
        env = dict(os.environ)
        env["LAMC_REGISTRY_DATA"] = tmp
        env["LAMC_REGISTRY_PORT"] = str(port)
        env["LAMC_REGISTRY_HOST"] = "127.0.0.1"
        env["PYTHONPATH"] = (
            f"{os.environ.get('PYTHONPATH', '')}{os.pathsep}{ROOT}".strip(os.pathsep)
        )
        proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        url = f"http://127.0.0.1:{port}"
        try:
            for _ in range(60):
                try:
                    urllib.request.urlopen(url + "/health", timeout=0.2)
                    break
                except Exception:
                    time.sleep(0.1)
            else:
                proc.kill()
                raise AssertionError(f"registry never came up on {url}")
            yield url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ── Library factory ─────────────────────────────────────────

def _make_lib(parent: Path, name: str, version: str,
              *,
              body: str = 'func tag() -> str { return "v1" }',
              deps: dict[str, str] | None = None,
              go_deps: dict[str, str] | None = None) -> Path:
    """Materialise a library tree. ``deps`` is a flat ``{name:
    spec}`` map written into ``[dependencies]``; ``go_deps`` is the
    same shape for ``[go-deps]``. Returns the library directory."""
    safe = name.replace("/", "__").lstrip("@")
    d = parent / f"{safe}-{version}"
    d.mkdir(parents=True, exist_ok=True)
    lines = [
        "[library]",
        f'name    = "{name}"',
        f'version = "{version}"',
        "",
    ]
    if deps:
        lines.append("[dependencies]")
        for k, v in deps.items():
            key = k if k.replace("_", "").replace("-", "").isalnum() else f'"{k}"'
            lines.append(f'{key} = "{v}"')
        lines.append("")
    if go_deps:
        lines.append("[go-deps]")
        for k, v in go_deps.items():
            lines.append(f'"{k}" = "{v}"')
        lines.append("")
    (d / "lamlib.toml").write_text("\n".join(lines), encoding="utf-8")
    (d / "__init__.lam").write_text(body + "\n", encoding="utf-8")
    return d


def _publish(reg_url: str, lib_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        LAMC + ["publish", str(lib_dir), "--registry", reg_url, "-q"],
        capture_output=True, text=True)


def _install(reg_url: str, ext_dir: Path, *specs,
             extra=()) -> subprocess.CompletedProcess:
    # ``--global`` mirrors the helper in test_install_cli.py: the
    # raw install primitive, no lockfile in cwd, no project manifest
    # read. Project-mode scenarios call subprocess directly with
    # ``cwd=<tmp>`` and rely on the new default.
    cmd = LAMC + ["install",
                  "--registry", reg_url,
                  "--global",
                  "--extlibs-dir", str(ext_dir),
                  *extra,
                  *specs]
    return subprocess.run(cmd, capture_output=True, text=True)


# ── Scenario 1: transitive Lam deps install automatically ──

def test_transitive_lam_dep_auto_installs() -> None:
    """``lib_a`` pulls in ``lib_leaf`` via ``[dependencies]``.
    Installing ``lib_a`` materialises both directories on disk —
    the consumer never has to ``lamc install lib_leaf``
    separately."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        leaf = _make_lib(tmp_p, "lamleaf", "1.0.0",
                         body='func leaf_tag() -> str { return "leaf" }')
        a = _make_lib(tmp_p, "lamaaa", "1.0.0",
                      body='func a_tag() -> str { return "a" }',
                      deps={"lamleaf": "^1.0"})
        ext = tmp_p / "ext"; ext.mkdir()

        assert _publish(url, leaf).returncode == 0
        assert _publish(url, a).returncode == 0

        ins = _install(url, ext, "lamaaa")
        assert ins.returncode == 0, ins.stderr
        assert (ext / "lamaaa" / "__init__.lam").exists()
        assert (ext / "lamleaf" / "__init__.lam").exists(), \
            f"transitive dep wasn't installed; ext tree: " \
            f"{[p.name for p in ext.iterdir()]}"
    print("PASS: transitive Lam dep auto-installs")


# ── Scenario 2: incompatible Lam-dep versions across libs ──

def test_conflicting_lam_dep_versions_refused() -> None:
    """Two libs requesting incompatible majors of the same dep
    must surface a structured conflict — not silently install one
    and override the other."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        leaf_v1 = _make_lib(tmp_p, "lamshared", "1.5.0",
                            body='func n() -> int { return 1 }')
        leaf_v2 = _make_lib(tmp_p, "lamshared", "2.0.0",
                            body='func n() -> int { return 2 }')
        a = _make_lib(tmp_p, "lamone", "1.0.0",
                      body='func a() -> str { return "a" }',
                      deps={"lamshared": "^1.0"})
        b = _make_lib(tmp_p, "lamtwo", "1.0.0",
                      body='func b() -> str { return "b" }',
                      deps={"lamshared": "^2.0"})
        ext = tmp_p / "ext"; ext.mkdir()

        for lib in (leaf_v1, leaf_v2, a, b):
            assert _publish(url, lib).returncode == 0, lib

        # ``lamone`` first — succeeds, drags in lamshared@1.x.
        ok = _install(url, ext, "lamone")
        assert ok.returncode == 0, ok.stderr
        installed_shared = (ext / "lamshared" / "lamlib.toml").read_text()
        assert '1.5.0' in installed_shared, installed_shared

        # ``lamtwo`` next — wants lamshared@^2, the gate must fail.
        bad = _install(url, ext, "lamtwo")
        assert bad.returncode != 0, \
            f"expected conflict on lamshared major, got rc=0 with: {bad.stdout}"
        combined = (bad.stdout + bad.stderr).lower()
        assert "conflict" in combined or "lamshared" in combined, combined
        # Disk must NOT have been mutated by the failed install.
        assert (ext / "lamshared" / "lamlib.toml").read_text() == installed_shared
        assert not (ext / "lamtwo").exists(), \
            "failed install left a partial lamtwo on disk"
    print("PASS: conflicting Lam-dep versions refused")


# ── Scenario 3: project vs lib overlap ────────────────────

def test_project_vs_lib_dep_overlap_resolved() -> None:
    """When the project's own ``lamlib.toml`` agrees on a major
    with a transitively-installed lib, the install works and the
    lockfile records exactly one pin per dep — no double-install
    of the shared dep."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        shared_v1 = _make_lib(tmp_p, "lamshared", "1.5.0",
                              body='func n() -> int { return 1 }')
        a = _make_lib(tmp_p, "lamone", "1.0.0",
                      body='func a() -> str { return "a" }',
                      deps={"lamshared": "^1.0"})
        for lib in (shared_v1, a):
            assert _publish(url, lib).returncode == 0

        # Project layout — its own manifest pins lamshared in the
        # same major lib_a wants, plus a direct dep on lib_a.
        proj = tmp_p / "proj"
        proj.mkdir()
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lamone     = "^1.0"
            lamshared  = "^1.0"
        """).lstrip(), encoding="utf-8")

        ok = subprocess.run(
            LAMC + ["install", "--registry", url, "lamone"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr
        # Both deps land under <proj>/extlibs/.
        assert (proj / "extlibs" / "lamone").exists()
        assert (proj / "extlibs" / "lamshared").exists()
        # And the lockfile records one pin per — no duplicates.
        lock = (proj / "lamlib.lock.toml").read_text()
        assert lock.count("[pins.lamshared]") == 1, lock
        assert lock.count("[pins.lamone]") == 1, lock
    print("PASS: project vs lib Lam-dep overlap resolved")


def test_project_vs_lib_dep_conflict_refused() -> None:
    """Same shape as the success case, but the project pins a
    *different* major than the lib needs. The project's intent
    must win and the install must refuse without trampling the
    project's pins."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        shared_v1 = _make_lib(tmp_p, "lamshared", "1.5.0",
                              body='func n() -> int { return 1 }')
        shared_v2 = _make_lib(tmp_p, "lamshared", "2.0.0",
                              body='func n() -> int { return 2 }')
        # Lib needs ^2 — incompatible with the project's ^1 pin.
        a = _make_lib(tmp_p, "lamone", "1.0.0",
                      body='func a() -> str { return "a" }',
                      deps={"lamshared": "^2.0"})
        for lib in (shared_v1, shared_v2, a):
            assert _publish(url, lib).returncode == 0

        proj = tmp_p / "proj"
        proj.mkdir()
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lamshared = "^1.0"
        """).lstrip(), encoding="utf-8")

        bad = subprocess.run(
            LAMC + ["install", "--registry", url, "lamone"],
            cwd=str(proj), capture_output=True, text=True)
        assert bad.returncode != 0, \
            f"expected conflict, got rc=0:\n{bad.stdout}\n{bad.stderr}"
        combined = (bad.stdout + bad.stderr).lower()
        assert "conflict" in combined, combined
        # Project's manifest must be untouched and no half-install.
        proj_mf = (proj / "lamlib.toml").read_text()
        assert "^1.0" in proj_mf
        assert not (proj / "extlibs" / "lamone").exists()
    print("PASS: project vs lib Lam-dep conflict refused")


# ── Scenario 4: Go-module variants ─────────────────────────

def test_go_dep_aggregated_into_lockfile() -> None:
    """A lib that declares ``[go-deps]`` causes those modules to
    show up in ``lamlib.lock.toml`` under the resolved
    ``[go_pins.*]`` block — that's the bridge the compile step
    later reads."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        a = _make_lib(tmp_p, "lamgouser", "1.0.0",
                      body='func a() -> str { return "a" }',
                      go_deps={"github.com/foo/bar": "v1.2.3"})
        assert _publish(url, a).returncode == 0

        proj = tmp_p / "proj"; proj.mkdir()
        ok = subprocess.run(
            LAMC + ["install", "--registry", url, "lamgouser"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr
        lock = (proj / "lamlib.lock.toml").read_text()
        assert "go_pins" in lock, lock
        assert "github.com/foo/bar" in lock
        assert "v1.2.3" in lock
    print("PASS: go-dep aggregated into lockfile")


def test_go_dep_compatible_majors_pick_highest() -> None:
    """Two libs declare the SAME Go module at compatible majors
    (``v1.2.3`` and ``v1.5.0``). Both libs install fine and the
    lockfile records the higher version (Go's MVS rule)."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        a = _make_lib(tmp_p, "lamgoa", "1.0.0",
                      body='func a() -> str { return "a" }',
                      go_deps={"github.com/foo/bar": "v1.2.3"})
        b = _make_lib(tmp_p, "lamgob", "1.0.0",
                      body='func b() -> str { return "b" }',
                      go_deps={"github.com/foo/bar": "v1.5.0"})
        for lib in (a, b):
            assert _publish(url, lib).returncode == 0

        proj = tmp_p / "proj"; proj.mkdir()
        # First install: pulls in v1.2.3.
        ok1 = subprocess.run(
            LAMC + ["install", "--registry", url, "lamgoa"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok1.returncode == 0, ok1.stderr
        # Second: same module, higher version. MVS upgrades.
        ok2 = subprocess.run(
            LAMC + ["install", "--registry", url, "lamgob"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok2.returncode == 0, ok2.stderr
        lock = (proj / "lamlib.lock.toml").read_text()
        assert "v1.5.0" in lock, lock
    print("PASS: go-dep compatible majors pick highest")


def test_go_dep_incompatible_majors_refused() -> None:
    """``v1`` and ``v2`` of the same Go path are different
    packages as far as Go is concerned. After the first lib lands
    on disk, the second install must see the existing lib's
    ``[go-deps]`` and refuse the incompatible major outright."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        a = _make_lib(tmp_p, "lamgoa", "1.0.0",
                      body='func a() -> str { return "a" }',
                      go_deps={"github.com/foo/bar": "v1.2.3"})
        b = _make_lib(tmp_p, "lamgob", "1.0.0",
                      body='func b() -> str { return "b" }',
                      go_deps={"github.com/foo/bar": "v2.0.0"})
        for lib in (a, b):
            assert _publish(url, lib).returncode == 0

        proj = tmp_p / "proj"; proj.mkdir()
        ok1 = subprocess.run(
            LAMC + ["install", "--registry", url, "lamgoa"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok1.returncode == 0, ok1.stderr

        # Second install must fail explicitly: the on-disk lamgoa
        # still demands v1, lamgob demands v2 — different majors of
        # the same Go module cannot coexist.
        bad = subprocess.run(
            LAMC + ["install", "--registry", url, "lamgob"],
            cwd=str(proj), capture_output=True, text=True)
        assert bad.returncode != 0, \
            f"expected conflict, got rc=0:\n{bad.stdout}\n{bad.stderr}"
        combined = (bad.stdout + bad.stderr).lower()
        assert "conflict" in combined and "github.com/foo/bar" in combined, combined

        # Disk state must be untouched by the failed install.
        assert (proj / "extlibs" / "lamgoa").exists()
        assert not (proj / "extlibs" / "lamgob").exists()
        # Lockfile must NOT carry both majors simultaneously.
        lock = (proj / "lamlib.lock.toml").read_text()
        assert not ("v1.2.3" in lock and "v2.0.0" in lock), \
            f"both majors leaked into lockfile:\n{lock}"
    print("PASS: go-dep incompatible majors refused")


def test_go_pins_materialise_in_synthesised_go_mod() -> None:
    """End-to-end check: after a successful install with a Go pin,
    the compiler's ``_collect_go_pins`` reads it back from the
    lockfile + manifest and ``_inject_go_requires`` writes it into
    a synthesised ``go.mod`` exactly as Go's own build expects."""
    from compiler.lammergeier import (
        _collect_go_pins, _inject_go_requires)

    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        a = _make_lib(tmp_p, "lamgouser", "1.0.0",
                      body='func a() -> str { return "a" }',
                      go_deps={"github.com/foo/bar": "v1.2.3",
                               "gopkg.in/yaml.v2":   "v2.4.0"})
        assert _publish(url, a).returncode == 0

        proj = tmp_p / "proj"; proj.mkdir()
        ok = subprocess.run(
            LAMC + ["install", "--registry", url, "lamgouser"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr

        # Step 1 — lockfile lookup.
        pins = _collect_go_pins(proj, stdlib_modules=set())
        assert pins == {
            "github.com/foo/bar": "v1.2.3",
            "gopkg.in/yaml.v2":   "v2.4.0",
        }, pins

        # Step 2 — go.mod synthesis. Spin up a tempdir, write a
        # blank go.mod skeleton (the equivalent of ``go mod init``
        # output), then inject. Verify each pin landed verbatim.
        with tempfile.TemporaryDirectory() as gotmp:
            gomod = Path(gotmp) / "go.mod"
            gomod.write_text("module myapp\n\ngo 1.21\n", encoding="utf-8")
            _inject_go_requires(gomod, pins)
            text = gomod.read_text()
            assert "require (" in text, text
            assert "github.com/foo/bar v1.2.3" in text, text
            assert "gopkg.in/yaml.v2 v2.4.0" in text, text
            # Sorted output keeps git diffs stable.
            i_bar = text.index("github.com/foo/bar")
            i_yaml = text.index("gopkg.in/yaml.v2")
            assert i_bar < i_yaml, "go.mod requires must be alphabetically ordered"
    print("PASS: go pins materialise in synthesised go.mod")


def test_go_dep_project_vs_lib_overlap() -> None:
    """The project's own ``[go-deps]`` add to the merge. A lib
    that pins a different major of the same module conflicts
    against the project's pin — the project wins."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        a = _make_lib(tmp_p, "lamgoa", "1.0.0",
                      body='func a() -> str { return "a" }',
                      go_deps={"github.com/foo/bar": "v2.0.0"})
        assert _publish(url, a).returncode == 0

        proj = tmp_p / "proj"
        proj.mkdir()
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [go-deps]
            "github.com/foo/bar" = "v1.2.3"
        """).lstrip(), encoding="utf-8")

        bad = subprocess.run(
            LAMC + ["install", "--registry", url, "lamgoa"],
            cwd=str(proj), capture_output=True, text=True)
        assert bad.returncode != 0, \
            f"expected go major conflict, got rc=0:\n{bad.stdout}\n{bad.stderr}"
        combined = (bad.stdout + bad.stderr).lower()
        assert "conflict" in combined, combined
        # The project's pin must remain untouched.
        assert "v1.2.3" in (proj / "lamlib.toml").read_text()
    print("PASS: project vs lib Go-dep overlap refused")


# ── Driver ──────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_transitive_lam_dep_auto_installs,
        test_conflicting_lam_dep_versions_refused,
        test_project_vs_lib_dep_overlap_resolved,
        test_project_vs_lib_dep_conflict_refused,
        test_go_dep_aggregated_into_lockfile,
        test_go_dep_compatible_majors_pick_highest,
        test_go_dep_incompatible_majors_refused,
        test_go_pins_materialise_in_synthesised_go_mod,
        test_go_dep_project_vs_lib_overlap,
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
        print(f"Dependency crash: {failures} of {len(tests)} tests failed")
        return 1
    print(f"Dependency crash: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
