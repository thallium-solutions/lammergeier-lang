#!/usr/bin/env python3
"""Tests for Phase-4 ``[replace]`` directive + ``[workspace]``
keyword reservation.

``[replace]`` (4.1) lets the project manifest redirect a
declared dependency to a local path or git URL without touching
``[dependencies]`` itself — the Lam analog of Go's ``go.mod
replace`` directive.

``[workspace]`` (4.4) is reserved for a future multi-package
layout. User manifests that use it today must fail loudly so
nobody accidentally depends on a half-baked schema.

Run with::

    python3 tests/tests/test_replace_workspace.py
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

from compiler.manifest import (  # noqa: E402
    Manifest,
    ManifestError,
    ReplaceSpec,
)

LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]
SERVER = ROOT / "tools" / "registry" / "server.py"


_TEST_CACHE = tempfile.mkdtemp(prefix="lamc-test-cache-replace-")
os.environ["LAMC_CACHE"] = _TEST_CACHE
atexit.register(lambda: shutil.rmtree(_TEST_CACHE, ignore_errors=True))


# ── Registry harness (matches the others) ───────────────────

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
              body: str = 'func tag() -> str { return "registry" }') -> Path:
    safe = name.replace("/", "__").lstrip("@")
    d = parent / f"{safe}-{version}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "lamlib.toml").write_text(textwrap.dedent(f"""
        [library]
        name    = "{name}"
        version = "{version}"
    """).lstrip(), encoding="utf-8")
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


# ── Manifest-level tests (parsing only) ─────────────────────

def test_replace_path_round_trips() -> None:
    """``[replace]`` with a ``path = "../foo"`` form lands on the
    parsed manifest as a :class:`ReplaceSpec` with the right
    fields populated."""
    text = textwrap.dedent("""
        [library]
        name    = "myapp"
        version = "0.1.0"

        [replace]
        lamwebp = { path = "../local-lamwebp" }
    """).lstrip()
    mf = Manifest.from_text(text)
    assert "lamwebp" in mf.replace, mf.replace
    rs = mf.replace["lamwebp"]
    assert rs.path == "../local-lamwebp", rs
    assert rs.git is None and rs.ref is None, rs
    print("PASS: [replace] path form parses as ReplaceSpec")


def test_replace_git_with_ref_round_trips() -> None:
    """Git form: ``{ git = "…", ref = "…" }`` populates both
    fields. The optional ``ref`` is None when omitted."""
    text = textwrap.dedent("""
        [library]
        name    = "myapp"
        version = "0.1.0"

        [replace]
        lamcolor = { git = "https://github.com/myfork/lamcolor.git", ref = "main" }
        lambare  = { git = "https://example.com/x.git" }
    """).lstrip()
    mf = Manifest.from_text(text)
    assert mf.replace["lamcolor"].git == "https://github.com/myfork/lamcolor.git"
    assert mf.replace["lamcolor"].ref == "main"
    assert mf.replace["lambare"].git == "https://example.com/x.git"
    assert mf.replace["lambare"].ref is None
    print("PASS: [replace] git form parses with optional ref")


def test_replace_rejects_mixed_path_and_git() -> None:
    """A replacement that mixes ``path`` with ``git`` / ``ref`` is
    nonsense; the parser must reject it up-front rather than
    silently picking a winner."""
    text = textwrap.dedent("""
        [library]
        name    = "myapp"
        version = "0.1.0"

        [replace]
        lamx = { path = "../x", git = "https://example.com/x.git" }
    """).lstrip()
    try:
        Manifest.from_text(text)
    except ManifestError as e:
        assert "cannot mix" in str(e), e
        print("PASS: [replace] mixing path + git is rejected")
        return
    raise AssertionError("expected ManifestError on mixed [replace] entry")


def test_workspace_keyword_is_reserved() -> None:
    """``[workspace]`` is reserved for future use. A user manifest
    that declares it must error so nobody locks themselves into
    an accidental schema before the feature lands."""
    text = textwrap.dedent("""
        [library]
        name    = "myapp"
        version = "0.1.0"

        [workspace]
        members = ["lib/foo", "lib/bar"]
    """).lstrip()
    try:
        Manifest.from_text(text)
    except ManifestError as e:
        assert "reserved" in str(e).lower(), e
        print("PASS: [workspace] is reserved and rejected today")
        return
    raise AssertionError("expected ManifestError on [workspace] block")


# ── End-to-end resolver tests ───────────────────────────────

def test_replace_path_overrides_registry() -> None:
    """The big one: project declares ``lamfoo = "^1.0"`` *and*
    ``[replace] lamfoo = { path = "..." }``. ``lamc install``
    must materialise the local checkout's tree, not the registry's
    version."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        # Registry version says "registry"; local checkout says
        # "local". The body each lib emits is the smoke-test
        # signal we'll grep in the install output.
        _publish(url, _make_lib(
            tmp_p, "lamrep_a", "1.0.0",
            body='func tag() -> str { return "registry" }'))

        local = tmp_p / "local-lamrep_a"
        local.mkdir()
        (local / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "lamrep_a"
            version = "1.0.0-local"
        """).lstrip(), encoding="utf-8")
        (local / "__init__.lam").write_text(
            'func tag() -> str { return "local" }\n', encoding="utf-8")

        proj = tmp_p / "proj"
        proj.mkdir()
        (proj / "lamlib.toml").write_text(textwrap.dedent(f"""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lamrep_a = "^1.0"

            [replace]
            lamrep_a = {{ path = "{local}" }}
        """).lstrip(), encoding="utf-8")
        (proj / "main.lam").write_text(textwrap.dedent("""
            from lamrep_a import tag;
            func main() { print(tag()); }
        """).lstrip(), encoding="utf-8")

        r = _run("install", "--registry", url, cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)

        # On-disk install carries the LOCAL body, not the registry one.
        installed = (proj / "extlibs" / "lamrep_a" / "__init__.lam"
                     ).read_text()
        assert '"local"' in installed, installed
        assert '"registry"' not in installed, installed

        # And the lockfile records the source as path, not registry.
        lock = (proj / "lamlib.lock.toml").read_text()
        assert 'source = "path"' in lock, lock
    print("PASS: [replace] path redirects a registry dep to a local checkout")


def test_replace_propagates_to_transitive_deps() -> None:
    """If the project replaces a *transitive* dep, every library
    that asks for it gets the replacement. Mirrors Go's ``go.mod
    replace`` semantics."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        # B is the leaf the user wants to override.
        _publish(url, _make_lib(
            tmp_p, "lamrep_b", "1.0.0",
            body='func tag() -> str { return "registry-b" }'))
        # A imports B transitively.
        a_dir = tmp_p / "lamrep_a-1.0.0"
        a_dir.mkdir()
        (a_dir / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "lamrep_a"
            version = "1.0.0"

            [dependencies]
            lamrep_b = "^1.0"
        """).lstrip(), encoding="utf-8")
        (a_dir / "__init__.lam").write_text(
            'from lamrep_b import tag;\n'
            'func tag_a() -> str { return tag(); }\n',
            encoding="utf-8")
        _publish(url, a_dir)

        # Local override of B.
        local_b = tmp_p / "local-lamrep_b"
        local_b.mkdir()
        (local_b / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "lamrep_b"
            version = "1.0.0-local"
        """).lstrip(), encoding="utf-8")
        (local_b / "__init__.lam").write_text(
            'func tag() -> str { return "local-b" }\n',
            encoding="utf-8")

        proj = tmp_p / "proj"
        proj.mkdir()
        (proj / "lamlib.toml").write_text(textwrap.dedent(f"""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lamrep_a = "^1.0"

            [replace]
            lamrep_b = {{ path = "{local_b}" }}
        """).lstrip(), encoding="utf-8")
        (proj / "main.lam").write_text(textwrap.dedent("""
            from lamrep_a import tag_a;
            func main() { print(tag_a()); }
        """).lstrip(), encoding="utf-8")

        r = _run("install", "--registry", url, cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)

        # B on disk is the local override.
        installed_b = (proj / "extlibs" / "lamrep_b" / "__init__.lam"
                       ).read_text()
        assert "local-b" in installed_b, installed_b
        # Lockfile pins B as path, not registry.
        lock = (proj / "lamlib.lock.toml").read_text()
        # B's pin block should be tagged source = "path".
        b_idx = lock.index("[pins.lamrep_b]")
        b_block = lock[b_idx:b_idx + 400]
        assert 'source = "path"' in b_block, b_block
    print("PASS: [replace] applies to transitive deps too")


# ── Driver ──────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_replace_path_round_trips,
        test_replace_git_with_ref_round_trips,
        test_replace_rejects_mixed_path_and_git,
        test_workspace_keyword_is_reserved,
        test_replace_path_overrides_registry,
        test_replace_propagates_to_transitive_deps,
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
        print(f"Replace / workspace: {failures} of {len(tests)} tests failed")
        return 1
    print(f"Replace / workspace: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
