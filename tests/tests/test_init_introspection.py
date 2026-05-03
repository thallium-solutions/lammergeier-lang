#!/usr/bin/env python3
"""Tests for Phase-4 ``lamc init`` and the lockfile introspection
verbs (``list``, ``tree``, ``why``).

``init`` scaffolds a fresh project with the requested manifest
shape; the introspection verbs read the lockfile (and use its
``requested_by`` field for the tree / why renderers).

Both layers run via the ``lamc`` CLI subprocess so we exercise
the same wiring users see, including stderr / exit code
semantics.

Run with::

    python3 tests/tests/test_init_introspection.py
"""

from __future__ import annotations

import atexit
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

LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]
SERVER = ROOT / "tools" / "registry" / "server.py"


_TEST_CACHE = tempfile.mkdtemp(prefix="lamc-test-cache-init-")
os.environ["LAMC_CACHE"] = _TEST_CACHE
atexit.register(lambda: shutil.rmtree(_TEST_CACHE, ignore_errors=True))


# ── Registry harness (matches test_install_cli.py) ──────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def registry():
    with tempfile.TemporaryDirectory(prefix="lamcreg-") as tmp:
        port = _free_port()
        env = dict(os.environ)
        env["LAMC_REGISTRY_DATA"] = tmp
        env["LAMC_REGISTRY_PORT"] = str(port)
        env["LAMC_REGISTRY_HOST"] = "127.0.0.1"
        env["PYTHONPATH"] = (
            f"{os.environ.get('PYTHONPATH', '')}{os.pathsep}{ROOT}"
            .strip(os.pathsep)
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


def _make_lib(parent: Path, name: str, version: str,
              body: str = 'func tag() -> str { return "v1" }',
              deps: dict | None = None) -> Path:
    safe = name.replace("/", "__").lstrip("@")
    d = parent / f"{safe}-{version}"
    d.mkdir(parents=True, exist_ok=True)
    deps_block = ""
    if deps:
        lines = ["[dependencies]"]
        for k, v in deps.items():
            lines.append(f'{k} = "{v}"')
        deps_block = "\n" + "\n".join(lines) + "\n"
    (d / "lamlib.toml").write_text(textwrap.dedent(f"""
        [library]
        name    = "{name}"
        version = "{version}"
    """).lstrip() + deps_block, encoding="utf-8")
    (d / "__init__.lam").write_text(body + "\n", encoding="utf-8")
    return d


def _publish(reg_url: str, lib_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        LAMC + ["publish", str(lib_dir), "--registry", reg_url, "-q"],
        capture_output=True, text=True)


def _run(*args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        LAMC + list(args),
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


# ── lamc init ───────────────────────────────────────────────

def test_init_default_scaffold_runs() -> None:
    """The zero-flag form of ``lamc init`` writes a manifest, a
    ``main.lam``, and a ``.gitignore``, and the resulting
    ``main.lam`` compiles + runs cleanly."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        r = _run("init", "-q", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert (proj / "lamlib.toml").exists()
        assert (proj / "main.lam").exists()
        assert (proj / ".gitignore").exists()

        run = _run(str(proj / "main.lam"), "--run", cwd=proj)
        assert run.returncode == 0, (run.returncode, run.stderr)
        assert "hello, lammergeier!" in run.stdout, run.stdout
    print("PASS: init default scaffold builds + runs")


def test_init_custom_name_version_scope() -> None:
    """``--name``/``--version``/``--scope`` flags flow into the
    manifest. The scope must start with ``@``; a missing leading
    ``@`` is rejected at the CLI before any file is written."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        r = _run("init", "--name", "widget", "--version", "0.5.1",
                 "--scope", "@alice", "-q", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)
        text = (proj / "lamlib.toml").read_text()
        assert 'name    = "@alice/widget"' in text, text
        assert 'version = "0.5.1"' in text, text
    print("PASS: init honours --name / --version / --scope")


def test_init_lib_writes_named_module() -> None:
    """``--lib`` produces ``<name>.lam`` instead of ``main.lam``,
    and the file declares a ``tag()`` helper so consumers have a
    cheap smoke-test entry point."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        r = _run("init", "--name", "lamutil", "--lib", "-q", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)
        assert (proj / "lamutil.lam").exists()
        assert not (proj / "main.lam").exists()
        body = (proj / "lamutil.lam").read_text()
        assert "func tag()" in body, body
    print("PASS: init --lib writes <name>.lam, no main.lam")


def test_init_refuses_overwrite_without_force() -> None:
    """Non-destructive by default: a pre-existing ``lamlib.toml``
    blocks the scaffold unless ``--force`` is set."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        (proj / "lamlib.toml").write_text("# pre-existing\n", encoding="utf-8")

        r = _run("init", "--name", "demo", cwd=proj)
        assert r.returncode == 1, (r.returncode, r.stderr, r.stdout)
        assert "refusing to overwrite" in r.stderr, r.stderr
        # Sentinel should still be there.
        assert "pre-existing" in (proj / "lamlib.toml").read_text()

        r = _run("init", "--name", "demo", "--force", "-q", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)
        # Now overwritten.
        assert "pre-existing" not in (proj / "lamlib.toml").read_text()
    print("PASS: init refuses to overwrite without --force")


def test_init_rejects_invalid_name_and_version() -> None:
    """Validation: bad module names and SemVer strings are rejected
    before any file is written."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        r = _run("init", "--name", "1bad-name", cwd=proj)
        assert r.returncode == 2, (r.returncode, r.stderr)
        assert "not a legal module name" in r.stderr, r.stderr

        r = _run("init", "--name", "fine", "--version", "not-semver",
                 cwd=proj)
        assert r.returncode == 2, (r.returncode, r.stderr)
        assert "not a valid SemVer" in r.stderr, r.stderr

        r = _run("init", "--name", "fine", "--scope", "missing-at",
                 cwd=proj)
        assert r.returncode == 2, (r.returncode, r.stderr)
        assert "must look like '@alice'" in r.stderr, r.stderr

        # No files should have been written by any of the failed
        # invocations.
        assert not (proj / "lamlib.toml").exists()
    print("PASS: init validates --name / --version / --scope")


# ── lamc list / tree / why ──────────────────────────────────

def test_list_tree_why_render_correctly() -> None:
    """Build a small dep graph (root → A → B) via the registry, run
    a real install, then exercise list / tree / why against the
    resulting lockfile."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        # B is a leaf; A depends on B; project depends on A.
        _publish(url, _make_lib(tmp_p, "lamintro_b", "1.0.0"))
        _publish(url, _make_lib(tmp_p, "lamintro_a", "1.0.0",
                                deps={"lamintro_b": "^1.0"}))
        proj = tmp_p / "proj"
        proj.mkdir()
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lamintro_a = "^1.0"
        """).lstrip(), encoding="utf-8")
        (proj / "main.lam").write_text(textwrap.dedent("""
            from lamintro_a import tag;

            func main() {
                print(tag());
            }
        """).lstrip(), encoding="utf-8")
        r = _run("install", "--registry", url, cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)

        # ── list: both pins, sorted, with source.
        r = _run("list", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr, r.stdout)
        lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
        assert any(ln.startswith("lamintro_a@1.0.0") for ln in lines), lines
        assert any(ln.startswith("lamintro_b@1.0.0") for ln in lines), lines

        # ── tree: A under root, B under A.
        r = _run("tree", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr, r.stdout)
        out = r.stdout
        assert "myapp@0.1.0" in out, out
        assert "lamintro_a@1.0.0" in out, out
        assert "lamintro_b@1.0.0" in out, out
        # B should appear *after* A (i.e. as its child, deeper indent).
        a_idx = out.index("lamintro_a@1.0.0")
        b_idx = out.index("lamintro_b@1.0.0")
        assert b_idx > a_idx, (a_idx, b_idx, out)

        # ── why: the chain B ← A ← root.
        r = _run("why", "lamintro_b", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr, r.stdout)
        out = r.stdout
        assert "lamintro_b@1.0.0" in out, out
        assert "requested by lamintro_a@1.0.0" in out, out
        assert "requested by root" in out, out
    print("PASS: list / tree / why render the dependency graph correctly")


def test_why_rejects_unknown_pin() -> None:
    """``lamc why <unknown>`` fails fast with a helpful error
    rather than walking an empty chain."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamintro_c", "1.0.0"))
        proj = tmp_p / "proj"
        proj.mkdir()
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lamintro_c = "^1.0"
        """).lstrip(), encoding="utf-8")
        (proj / "main.lam").write_text(textwrap.dedent("""
            from lamintro_c import tag;
            func main() { print(tag()); }
        """).lstrip(), encoding="utf-8")
        r = _run("install", "--registry", url, cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)

        r = _run("why", "nope", cwd=proj)
        assert r.returncode == 1, (r.returncode, r.stderr, r.stdout)
        assert "not in the lockfile" in r.stderr, r.stderr
    print("PASS: why rejects an unknown pin with a clear error")


def test_introspection_fails_without_lockfile() -> None:
    """All three introspection verbs need both a manifest and a
    lockfile. With no lockfile, they exit 2 (setup error) so CI
    can tell ``not set up`` apart from a real graph problem."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"
        """).lstrip(), encoding="utf-8")

        for verb_args in [("list",), ("tree",), ("why", "anything")]:
            r = _run(*verb_args, cwd=proj)
            assert r.returncode == 2, (
                verb_args, r.returncode, r.stderr, r.stdout)
            assert "lamlib.lock.toml" in r.stderr, (verb_args, r.stderr)
    print("PASS: list / tree / why exit 2 (setup error) without a lockfile")


# ── Driver ──────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_init_default_scaffold_runs,
        test_init_custom_name_version_scope,
        test_init_lib_writes_named_module,
        test_init_refuses_overwrite_without_force,
        test_init_rejects_invalid_name_and_version,
        test_list_tree_why_render_correctly,
        test_why_rejects_unknown_pin,
        test_introspection_fails_without_lockfile,
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
        print(f"Init / introspection: {failures} of {len(tests)} tests failed")
        return 1
    print(f"Init / introspection: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
