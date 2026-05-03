#!/usr/bin/env python3
"""Tests for Phase-3 ``lamc tidy`` and ``lamc verify`` verbs.

``tidy`` reconciles ``lamlib.toml`` ``[dependencies]`` with the
project's actual ``import`` graph (drop unused, add missing,
refresh lockfile). ``verify`` re-hashes installed extlibs against
the lockfile so users can catch on-disk tampering or partial
installs.

Both verbs run via the ``lamc`` CLI subprocess so we exercise the
same wiring users see, including stderr / exit code semantics.
The registry harness mirrors test_install_cli.py (spawns
``tools/registry/server.py`` on a free port for each context).

Run with::

    python3 tests/tests/test_tidy_verify.py
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

LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]
SERVER = ROOT / "tools" / "registry" / "server.py"


# ── Per-test cache isolation ────────────────────────────────
#
# Match test_install_cli.py's pattern: pin LAMC_CACHE to a tmpdir
# scoped to this test process so the developer's real cache stays
# untouched.

_TEST_CACHE = tempfile.mkdtemp(prefix="lamc-test-cache-tidy-")
os.environ["LAMC_CACHE"] = _TEST_CACHE
atexit.register(lambda: shutil.rmtree(_TEST_CACHE, ignore_errors=True))


# ── Registry fixture (spawn-as-subprocess style) ────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def registry():
    """Run the reference registry on an ephemeral port. Yields the
    base URL the install CLI should target."""
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
              body: str = 'func tag() -> str { return "v1" }') -> Path:
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


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


# ── tidy: in-sync project ───────────────────────────────────

def test_tidy_noop_when_in_sync() -> None:
    """A manifest whose ``[dependencies]`` exactly mirrors the
    project's imports must report a no-op and exit 0."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamtidy_a", "1.0.0"))
        proj = tmp_p / "proj"
        proj.mkdir()

        _write(proj / "lamlib.toml", textwrap.dedent("""
            [library]
            name    = "myproj"
            version = "0.1.0"

            [dependencies]
            lamtidy_a = "^1.0"
        """).lstrip())
        _write(proj / "main.lam", textwrap.dedent("""
            from lamtidy_a import tag;

            func main() {
                print(tag());
            }
        """).lstrip())

        # Install once so extlibs/ is populated.
        r = _run("install", "--registry", url, cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)

        r = _run("tidy", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr, r.stdout)
        assert "already in sync" in r.stdout, r.stdout
    print("PASS: tidy is a no-op on an in-sync manifest")


# ── tidy: drops unused deps ─────────────────────────────────

def test_tidy_removes_unused_dep() -> None:
    """A declared dep that no source file imports gets removed
    from the manifest."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamtidy_b", "1.0.0"))
        proj = tmp_p / "proj"
        proj.mkdir()

        _write(proj / "lamlib.toml", textwrap.dedent("""
            [library]
            name    = "myproj"
            version = "0.1.0"

            [dependencies]
            lamtidy_b = "^1.0"
        """).lstrip())
        _write(proj / "main.lam", textwrap.dedent("""
            func main() {
                print("hello");
            }
        """).lstrip())

        r = _run("install", "--registry", url, cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)

        r = _run("tidy", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr, r.stdout)
        assert "would remove" in r.stdout, r.stdout
        assert "lamtidy_b" in r.stdout, r.stdout

        new_text = (proj / "lamlib.toml").read_text(encoding="utf-8")
        assert "lamtidy_b" not in new_text, new_text
    print("PASS: tidy removes a declared-but-unused dep")


# ── tidy: --check exits non-zero on diff ────────────────────

def test_tidy_check_exits_nonzero_on_diff() -> None:
    """``tidy --check`` reports the plan and exits non-zero
    without mutating the file."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamtidy_c", "1.0.0"))
        proj = tmp_p / "proj"
        proj.mkdir()

        _write(proj / "lamlib.toml", textwrap.dedent("""
            [library]
            name    = "myproj"
            version = "0.1.0"

            [dependencies]
            lamtidy_c = "^1.0"
        """).lstrip())
        _write(proj / "main.lam", textwrap.dedent("""
            func main() {
                print("hello");
            }
        """).lstrip())

        r = _run("install", "--registry", url, cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)

        r = _run("tidy", "--check", cwd=proj)
        assert r.returncode == 1, (r.returncode, r.stderr, r.stdout)
        assert "would remove" in r.stdout, r.stdout
        # Manifest not modified.
        text = (proj / "lamlib.toml").read_text(encoding="utf-8")
        assert "lamtidy_c" in text, text
    print("PASS: tidy --check exits non-zero on diff, leaves manifest alone")


# ── tidy: refuses to add a not-installed import ─────────────

def test_tidy_refuses_when_import_not_installed() -> None:
    """An import that has no extlibs/<name> on disk can't be
    auto-added — tidy doesn't reach the network. Verb refuses."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        _write(proj / "lamlib.toml", textwrap.dedent("""
            [library]
            name    = "myproj"
            version = "0.1.0"
        """).lstrip())
        _write(proj / "main.lam", textwrap.dedent("""
            from lamghost import Ghost;

            func main() {
                print("boo");
            }
        """).lstrip())
        r = _run("tidy", cwd=proj)
        assert r.returncode == 1, (r.returncode, r.stderr, r.stdout)
        assert "lamghost" in r.stdout, r.stdout
        assert "not installed" in r.stdout, r.stdout
        # Manifest left untouched.
        text = (proj / "lamlib.toml").read_text(encoding="utf-8")
        assert "lamghost" not in text, text
    print("PASS: tidy refuses to add a not-yet-installed import")


# ── tidy: stdlib imports don't trip the missing-dep path ─────

def test_tidy_ignores_stdlib_imports() -> None:
    """Stdlib modules ship with the compiler — they should never
    show up as either ``unused`` or ``missing``."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        _write(proj / "lamlib.toml", textwrap.dedent("""
            [library]
            name    = "myproj"
            version = "0.1.0"
        """).lstrip())
        _write(proj / "main.lam", textwrap.dedent("""
            from lamstrings import Strings;

            func main() {
                print(Strings.toUpper("hi"));
            }
        """).lstrip())
        r = _run("tidy", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr, r.stdout)
        assert "already in sync" in r.stdout, r.stdout
        assert "lamstrings" not in r.stdout, r.stdout
    print("PASS: tidy ignores stdlib imports")


# ── verify: clean install matches lockfile ──────────────────

def test_verify_passes_on_clean_install() -> None:
    """An untampered ``./extlibs/`` should match every lockfile
    pin's ``tree_sha256`` exactly."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamver_a", "1.0.0"))
        proj = tmp_p / "proj"
        proj.mkdir()

        _write(proj / "lamlib.toml", textwrap.dedent("""
            [library]
            name    = "myproj"
            version = "0.1.0"

            [dependencies]
            lamver_a = "^1.0"
        """).lstrip())
        _write(proj / "main.lam", textwrap.dedent("""
            from lamver_a import tag;

            func main() {
                print(tag());
            }
        """).lstrip())
        r = _run("install", "--registry", url, cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)

        r = _run("verify", cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr, r.stdout)
        assert "lamver_a" in r.stdout, r.stdout
        assert "all" in r.stdout, r.stdout
    print("PASS: verify reports clean on an untampered install")


# ── verify: catches on-disk tampering ───────────────────────

def test_verify_catches_tampering() -> None:
    """A modified file inside ``extlibs/<name>`` shifts the tree
    hash and verify must surface the drift with a non-zero exit
    code."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamver_b", "1.0.0"))
        proj = tmp_p / "proj"
        proj.mkdir()

        _write(proj / "lamlib.toml", textwrap.dedent("""
            [library]
            name    = "myproj"
            version = "0.1.0"

            [dependencies]
            lamver_b = "^1.0"
        """).lstrip())
        _write(proj / "main.lam", textwrap.dedent("""
            from lamver_b import tag;

            func main() {
                print(tag());
            }
        """).lstrip())
        r = _run("install", "--registry", url, cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)

        # Tamper with the installed tree.
        installed = proj / "extlibs" / "lamver_b"
        srcs = list(installed.rglob("*.lam"))
        assert srcs, f"expected at least one .lam under {installed}"
        srcs[0].write_text(
            srcs[0].read_text() + "\n# tampered\n", encoding="utf-8")

        r = _run("verify", cwd=proj)
        assert r.returncode == 1, (r.returncode, r.stderr, r.stdout)
        assert "drift" in r.stdout + r.stderr, (r.stdout, r.stderr)
        assert "lamver_b" in r.stdout + r.stderr, (r.stdout, r.stderr)
    print("PASS: verify catches on-disk tampering")


# ── verify: missing install ─────────────────────────────────

def test_verify_catches_missing_install() -> None:
    """A pin in the lockfile but no on-disk extlib must fail with
    a clear ``missing on disk`` diagnostic."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamver_c", "1.0.0"))
        proj = tmp_p / "proj"
        proj.mkdir()

        _write(proj / "lamlib.toml", textwrap.dedent("""
            [library]
            name    = "myproj"
            version = "0.1.0"

            [dependencies]
            lamver_c = "^1.0"
        """).lstrip())
        _write(proj / "main.lam", textwrap.dedent("""
            from lamver_c import tag;

            func main() {
                print(tag());
            }
        """).lstrip())
        r = _run("install", "--registry", url, cwd=proj)
        assert r.returncode == 0, (r.returncode, r.stderr)

        # Rip the on-disk install out from under the lockfile.
        shutil.rmtree(proj / "extlibs" / "lamver_c")

        r = _run("verify", cwd=proj)
        assert r.returncode == 1, (r.returncode, r.stderr, r.stdout)
        assert "missing on disk" in r.stdout + r.stderr, (r.stdout, r.stderr)
    print("PASS: verify catches a missing on-disk install")


# ── verify: bails when there's no lockfile ──────────────────

def test_verify_fails_without_lockfile() -> None:
    """``verify`` is meaningless without a lockfile to compare
    against; it must exit with code 2 (setup error)."""
    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        _write(proj / "lamlib.toml", textwrap.dedent("""
            [library]
            name    = "myproj"
            version = "0.1.0"
        """).lstrip())
        r = _run("verify", cwd=proj)
        assert r.returncode == 2, (r.returncode, r.stderr, r.stdout)
        assert "no lamlib.lock.toml" in r.stderr, r.stderr
    print("PASS: verify bails (exit 2) without a lockfile")


# ── Driver ──────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_tidy_noop_when_in_sync,
        test_tidy_removes_unused_dep,
        test_tidy_check_exits_nonzero_on_diff,
        test_tidy_refuses_when_import_not_installed,
        test_tidy_ignores_stdlib_imports,
        test_verify_passes_on_clean_install,
        test_verify_catches_tampering,
        test_verify_catches_missing_install,
        test_verify_fails_without_lockfile,
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
        print(f"Tidy / verify: {failures} of {len(tests)} tests failed")
        return 1
    print(f"Tidy / verify: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
