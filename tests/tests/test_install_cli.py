#!/usr/bin/env python3
"""Tests for ``lamc install`` / ``uninstall`` / ``publish``.

Spins up the reference registry server (``tools/registry/server.py``)
on a free port for each test session, points the install CLI at it
via ``--registry``, and exercises the full workflow:

    publish → list → install → use → uninstall

Includes coverage for:
* lockfile generation in default project mode + the v1 schema
  (``[meta] schema = 1``, ``requested_by`` arrays, ``tree_sha256``
  for git/path sources) plus silent v0 → v1 upgrade-on-write;
* the content-addressed cache populating on first install and
  satisfying a clean-extlibs reinstall on its own;
* ``--global`` opting out of project mode (no manifest read, no
  lockfile write);
* bare ``lamc install`` reading ``[dependencies]`` from
  ``lamlib.toml`` and ``lamc install <spec>`` writing it back;
* ``--frozen`` (lockfile-driven install, refuses drift, refuses
  positional specs) and ``--frozen --offline`` (cache-only);
* SemVer gate refusing a "patch" release that secretly drops a
  public function (and ``--allow-breaking`` overriding it);
* path-based install (no registry).

Docker is NOT required — the registry runs in-process. The
docker-compose file under ``tools/registry/docker-compose.yml``
is the same code path; running these tests is sufficient to
validate the container path too.

Run with::

    python3 tests/tests/test_install_cli.py
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

# Redirect the install cache to a per-session tmpdir so test runs
# don't pollute the developer's real ``~/.lammergeier/cache``. The
# subprocesses spawned by ``LAMC + ["install", ...]`` inherit this
# environment automatically.
_TEST_CACHE = tempfile.mkdtemp(prefix="lamccache-")
os.environ["LAMC_CACHE"] = _TEST_CACHE
atexit.register(lambda: shutil.rmtree(_TEST_CACHE, ignore_errors=True))


# ── Registry fixture ────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def registry(seed_dir: Path | None = None):
    """Run the reference registry on an ephemeral port. Yields the
    base URL the install CLI should target. The data directory is
    deleted when the context exits so tests never see each other's
    state."""
    with tempfile.TemporaryDirectory(prefix="lamcreg-") as tmp:
        port = _free_port()
        env = dict(os.environ)
        env["LAMC_REGISTRY_DATA"] = tmp
        env["LAMC_REGISTRY_PORT"] = str(port)
        env["LAMC_REGISTRY_HOST"] = "127.0.0.1"
        if seed_dir is not None:
            env["LAMC_REGISTRY_SEED"] = str(seed_dir)
        # Server imports compiler.manifest; PYTHONPATH must include
        # the project root so the in-tree manifest module resolves.
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
                raise AssertionError(
                    f"registry never came up on {url}")
            yield url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ── Library factory ─────────────────────────────────────────

def _make_lib(parent: Path, name: str, version: str,
              body: str = "func tag() -> str { return \"v1\" }") -> Path:
    """Materialise a library tree under ``parent/<safe>-<version>/``
    with a ``lamlib.toml`` and an ``__init__.lam``. Returns the
    library directory path."""
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


def _make_git_lib(parent: Path, name: str, version: str,
                  tag: str | None = None,
                  body: str = "func tag() -> str { return \"git\" }") -> Path:
    repo = _make_lib(parent, name, version, body=body)
    subprocess.run(["git", "init", "--quiet"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"],
                   cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "Lam Tests"],
                   cwd=str(repo), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "initial"],
                   cwd=str(repo), check=True)
    if tag:
        subprocess.run(["git", "tag", tag], cwd=str(repo), check=True)
    return repo


def _publish(reg_url: str, lib_dir: Path) -> subprocess.CompletedProcess:
    """Invoke ``lamc publish`` against the test registry. Returns the
    completed process so tests can inspect rc / stderr / stdout."""
    return subprocess.run(
        LAMC + ["publish", str(lib_dir), "--registry", reg_url, "-q"],
        capture_output=True, text=True)


def _install(reg_url: str, ext_dir: Path, *specs,
             extra=()) -> subprocess.CompletedProcess:
    # ``--global`` keeps these tests focused on the install primitive
    # — no project lamlib.lock.toml gets written into the test's
    # cwd, no project lamlib.toml gets read. Tests that exercise
    # project mode (lockfile + manifest behaviour) call subprocess
    # directly, set ``cwd=<tmp>``, and let the new default kick in.
    cmd = LAMC + ["install",
                  "--registry", reg_url,
                  "--global",
                  "--extlibs-dir", str(ext_dir),
                  *extra,
                  *specs]
    return subprocess.run(cmd, capture_output=True, text=True)


def _uninstall(ext_dir: Path, *names) -> subprocess.CompletedProcess:
    return subprocess.run(
        LAMC + ["uninstall", "--global",
                "--extlibs-dir", str(ext_dir), *names],
        capture_output=True, text=True)


# ── Tests ───────────────────────────────────────────────────

def test_publish_then_install_roundtrip() -> None:
    """End-to-end happy path: a library published with
    ``lamc publish`` is listed by the registry, installed by
    ``lamc install`` into a clean extlibs dir, and the tree on
    disk matches what the publisher shipped."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        lib  = _make_lib(tmp_p, "lamtest_one", "1.0.0")
        ext  = tmp_p / "ext"; ext.mkdir()

        pub = _publish(url, lib)
        assert pub.returncode == 0, pub.stderr

        # Index should now include the version
        idx = json.loads(urllib.request.urlopen(
            url + "/api/v1/libraries/lamtest_one").read())
        assert any(v["version"] == "1.0.0" for v in idx["versions"]), idx

        ins = _install(url, ext, "lamtest_one")
        assert ins.returncode == 0, ins.stderr

        installed_init = ext / "lamtest_one" / "__init__.lam"
        installed_mf   = ext / "lamtest_one" / "lamlib.toml"
        assert installed_init.exists()
        assert installed_mf.exists()
        assert "tag()" in installed_init.read_text()
    print("PASS: publish → install round-trip")


def test_scoped_publish_and_install() -> None:
    """Scoped names round-trip through the registry's URL handling
    + the install CLI's spec parser. The on-disk layout uses the
    scope as a real directory (``extlibs/@alice/lamx``)."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        lib = _make_lib(tmp_p, "@alice/lamx", "0.3.0")
        ext = tmp_p / "ext"; ext.mkdir()

        pub = _publish(url, lib)
        assert pub.returncode == 0, pub.stderr

        ins = _install(url, ext, "@alice/lamx")
        assert ins.returncode == 0, ins.stderr
        assert (ext / "@alice" / "lamx" / "__init__.lam").exists()
    print("PASS: scoped publish + install")


def test_install_pinned_version() -> None:
    """``lamc install name@1.0.0`` selects the specific version
    even when a newer one exists."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        v1 = _make_lib(tmp_p, "lampinned", "1.0.0",
                       'func tag() -> str { return "old" }')
        v2 = _make_lib(tmp_p, "lampinned", "1.1.0",
                       'func tag() -> str { return "new" }')
        ext = tmp_p / "ext"; ext.mkdir()

        assert _publish(url, v1).returncode == 0
        assert _publish(url, v2).returncode == 0

        ins = _install(url, ext, "lampinned@1.0.0")
        assert ins.returncode == 0, ins.stderr
        body = (ext / "lampinned" / "__init__.lam").read_text()
        assert "old" in body, body
    print("PASS: install pinned version")


def test_lockfile_written_in_default_project_mode() -> None:
    """With no flag, ``lamc install`` writes ``lamlib.lock.toml``
    next to the project + materialises into ``./extlibs/``. This is
    the new default — ``--global`` opts out of both."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        lib = _make_lib(tmp_p, "lamlocked", "0.1.0")
        proj = tmp_p / "proj"; proj.mkdir()

        assert _publish(url, lib).returncode == 0

        proc = subprocess.run(
            LAMC + ["install", "--registry", url, "lamlocked"],
            cwd=str(proj), capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

        lock = (proj / "lamlib.lock.toml").read_text()
        assert "lamlocked" in lock, lock
        assert '0.1.0' in lock
        # Project mode installs under <cwd>/extlibs/
        assert (proj / "extlibs" / "lamlocked" / "__init__.lam").exists()
    print("PASS: lockfile written in default project mode")


def test_uninstall_removes_tree_and_lock_pin() -> None:
    """``lamc uninstall`` deletes the on-disk tree and prunes the
    matching lockfile entry. Project mode is the default, so no
    flag is needed."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamremove", "1.0.0"))
        proj = tmp_p / "proj"; proj.mkdir()

        ok = subprocess.run(
            LAMC + ["install", "--registry", url, "lamremove"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr
        assert (proj / "extlibs" / "lamremove").exists()

        rm = subprocess.run(
            LAMC + ["uninstall", "lamremove"],
            cwd=str(proj), capture_output=True, text=True)
        assert rm.returncode == 0, rm.stderr
        assert not (proj / "extlibs" / "lamremove").exists()
        assert "lamremove" not in (proj / "lamlib.lock.toml").read_text()
    print("PASS: uninstall removes tree + lock pin")


def test_semver_gate_blocks_silent_break() -> None:
    """A 0.1.0 → 0.1.1 (patch) release that drops a public function
    is the textbook case the SemVer gate exists to catch. The
    second install must fail and stderr must mention drift /
    breaking."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        v1 = _make_lib(tmp_p, "lamguard", "0.1.0",
                       'func a() { } \nfunc b() { }')
        # NOTE: ``b()`` is missing in v2 — that's a breaking change
        # but the publisher is calling it a "patch" bump.
        v2 = _make_lib(tmp_p, "lamguard", "0.1.1",
                       'func a() { }')
        ext = tmp_p / "ext"; ext.mkdir()

        assert _publish(url, v1).returncode == 0
        assert _publish(url, v2).returncode == 0

        # First install: succeeds (v1, no prior install).
        first = _install(url, ext, "lamguard@0.1.0")
        assert first.returncode == 0, first.stderr

        # Second install (upgrade to v2): the gate refuses.
        second = _install(url, ext, "lamguard@0.1.1")
        assert second.returncode != 0, \
            f"expected gate to refuse upgrade: {second.stdout}\n{second.stderr}"
        combined = (second.stdout + second.stderr).lower()
        assert "breaking" in combined or "drift" in combined, combined

        # ``--allow-breaking`` overrides the gate.
        forced = _install(url, ext, "lamguard@0.1.1",
                          extra=("--allow-breaking",))
        assert forced.returncode == 0, forced.stderr
    print("PASS: SemVer gate refuses silent breaking change")


def test_path_install_works_offline() -> None:
    """An absolute path spec installs without any registry roundtrip
    — useful for development and for the seeded scenarios in CI."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        lib = _make_lib(tmp_p, "lamlocal", "0.1.0")
        ext = tmp_p / "ext"; ext.mkdir()

        proc = subprocess.run(
            LAMC + ["install", "--extlibs-dir", str(ext), str(lib)],
            capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert (ext / "lamlocal" / "__init__.lam").exists()
    print("PASS: path-based install works offline")


def test_lockfile_v1_schema_fields() -> None:
    """The lockfile carries the v1 markers we rely on:

    - ``[meta] schema = 1`` at the top.
    - ``requested_by = [...]`` as a TOML array per pin.
    - ``tree_sha256 = "<hex>"`` for path-source pins (the registry
      branch already covers integrity via ``sha256``)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        lib = _make_lib(tmp_p, "lamschema", "0.1.0")
        proj = tmp_p / "proj"; proj.mkdir()

        proc = subprocess.run(
            LAMC + ["install", str(lib)],
            cwd=str(proj), capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

        lock_text = (proj / "lamlib.lock.toml").read_text()

        assert "[meta]" in lock_text, lock_text
        assert "schema = 1" in lock_text, lock_text

        # Direct pin labelled "root" (the resolver's
        # ``<requested>`` sentinel collapsed by the writer).
        assert 'requested_by = ["root"]' in lock_text, lock_text

        # Path source records a content hash so we can detect drift
        # without hitting the network.
        assert "tree_sha256 =" in lock_text, lock_text

        # The v1 layout puts [meta] before any [pins.*] block — the
        # writer ordering is part of the contract, not an accident.
        assert lock_text.index("[meta]") < lock_text.index("[pins."), \
            f"expected [meta] before [pins.*]:\n{lock_text}"
    print("PASS: lockfile v1 carries [meta], requested_by, tree_sha256")


def test_bare_install_reads_project_manifest() -> None:
    """``lamc install`` with no positional args installs every
    entry in the project's ``[dependencies]`` table. The manifest
    is the source of truth; the command is the cheap "I just
    cloned this repo, get me set up" verb teammates run."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lambare_a", "1.0.0"))
        _publish(url, _make_lib(tmp_p, "lambare_b", "2.0.0"))
        proj = tmp_p / "proj"; proj.mkdir()

        # Pre-existing manifest with two registry deps.
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lambare_a = "^1.0"
            lambare_b = "^2.0"
        """).lstrip(), encoding="utf-8")

        proc = subprocess.run(
            LAMC + ["install", "--registry", url],
            cwd=str(proj), capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

        # Both deps land in the project extlibs.
        assert (proj / "extlibs" / "lambare_a" / "__init__.lam").exists()
        assert (proj / "extlibs" / "lambare_b" / "__init__.lam").exists()
        # Lockfile pins both.
        lock = (proj / "lamlib.lock.toml").read_text()
        assert "lambare_a" in lock and "lambare_b" in lock, lock
        # Manifest is unchanged (bare install reads, never writes).
        mf_text = (proj / "lamlib.toml").read_text()
        assert mf_text.count("lambare_a") == 1, mf_text
        assert mf_text.count("lambare_b") == 1, mf_text
    print("PASS: bare lamc install reads project manifest")


def test_install_spec_writes_manifest_dependency() -> None:
    """``lamc install foo`` (with an explicit spec) appends ``foo``
    to ``[dependencies]`` after the install succeeds. The version
    is caret-pinned to the resolved version \u2014 the most permissive
    SemVer range. A second install of a different version updates
    the entry in place."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lammfwrite", "1.0.0"))
        _publish(url, _make_lib(tmp_p, "lammfwrite", "1.1.0"))
        proj = tmp_p / "proj"; proj.mkdir()

        # Project starts with a [library] header and nothing else.
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"
        """).lstrip(), encoding="utf-8")

        # First install: appends [dependencies] + the entry.
        ok = subprocess.run(
            LAMC + ["install", "--registry", url, "lammfwrite@1.0.0"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr
        mf1 = (proj / "lamlib.toml").read_text()
        assert "[dependencies]" in mf1, mf1
        assert 'lammfwrite = "^1.0.0"' in mf1, mf1

        # Second install: updates the existing entry, doesn't
        # duplicate it.
        ok2 = subprocess.run(
            LAMC + ["install", "--registry", url, "lammfwrite@1.1.0",
                    "--allow-breaking"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok2.returncode == 0, ok2.stderr
        mf2 = (proj / "lamlib.toml").read_text()
        assert mf2.count("lammfwrite") == 1, mf2
        assert 'lammfwrite = "^1.1.0"' in mf2, mf2
    print("PASS: spec install writes / updates manifest dependency")


def test_git_install_writes_manifest_dependency() -> None:
    """A direct git install should persist the git source in
    ``[dependencies]`` so a teammate's bare ``lamc install`` can
    reproduce the same dependency without copying the original CLI
    command from history."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        repo = _make_git_lib(tmp_p, "lamgitwrite", "0.1.0", tag="v0.1.0")
        proj = tmp_p / "proj"; proj.mkdir()
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"
        """).lstrip(), encoding="utf-8")

        url = repo.as_uri()
        ok = subprocess.run(
            LAMC + ["install", f"{url}@v0.1.0"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr
        mf = (proj / "lamlib.toml").read_text()
        assert f'lamgitwrite = {{ git = "{url}", ref = "v0.1.0" }}' in mf, mf
        lock = (proj / "lamlib.lock.toml").read_text()
        assert 'source = "git"' in lock, lock
        assert f'url = "{url}"' in lock, lock
        assert 'requested_ref = "v0.1.0"' in lock, lock
    print("PASS: git install writes manifest dependency")


def test_bare_install_reads_git_dependency() -> None:
    """The persisted git dependency form is consumed by bare
    ``lamc install`` and materialises the git library into extlibs."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        repo = _make_git_lib(tmp_p, "lambaregit", "0.2.0", tag="v0.2.0")
        proj = tmp_p / "proj"; proj.mkdir()
        url = repo.as_uri()
        (proj / "lamlib.toml").write_text(textwrap.dedent(f"""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lambaregit = {{ git = "{url}", ref = "v0.2.0" }}
        """).lstrip(), encoding="utf-8")

        ok = subprocess.run(
            LAMC + ["install"],
            cwd=str(proj), capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr
        assert (proj / "extlibs" / "lambaregit" / "__init__.lam").exists()
        lock = (proj / "lamlib.lock.toml").read_text()
        assert 'requested_ref = "v0.2.0"' in lock, lock
    print("PASS: bare install reads git dependencies")


def test_frozen_succeeds_when_manifest_and_lockfile_agree() -> None:
    """``--frozen`` validates the manifest \u2194 lockfile pair and then
    materialises every pin from the lockfile (skipping the resolver).
    With both files in agreement, it's a clean cache-warmed install."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamfrozen", "1.0.0"))
        proj = tmp_p / "proj"; proj.mkdir()

        # Manifest declaring lamfrozen ^1.0.
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lamfrozen = "^1.0"
        """).lstrip(), encoding="utf-8")

        # First install (no --frozen): populates lockfile + extlibs.
        ok = subprocess.run(
            LAMC + ["install", "--registry", url],
            cwd=str(proj), capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr
        assert (proj / "lamlib.lock.toml").exists()

        # Wipe extlibs, leave lockfile + manifest. --frozen should
        # rebuild from the lockfile without re-resolving.
        shutil.rmtree(proj / "extlibs")
        rebuild = subprocess.run(
            LAMC + ["install", "--registry", url, "--frozen"],
            cwd=str(proj), capture_output=True, text=True)
        assert rebuild.returncode == 0, rebuild.stderr
        assert (proj / "extlibs" / "lamfrozen" / "__init__.lam").exists()
    print("PASS: --frozen rebuilds extlibs from lockfile")


def test_frozen_refuses_on_manifest_lockfile_drift() -> None:
    """Editing ``lamlib.toml`` to a stricter range without re-running
    ``lamc install`` leaves the lockfile pinning a now-incompatible
    version. ``--frozen`` must refuse with a clear drift error
    rather than silently re-resolving."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamdrift", "1.0.0"))
        _publish(url, _make_lib(tmp_p, "lamdrift", "2.0.0"))
        proj = tmp_p / "proj"; proj.mkdir()

        # Initial: manifest = ^1.0, install pins 1.0.0.
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lamdrift = "^1.0"
        """).lstrip(), encoding="utf-8")
        ok = subprocess.run(
            LAMC + ["install", "--registry", url],
            cwd=str(proj), capture_output=True, text=True)
        assert ok.returncode == 0, ok.stderr

        # Edit the manifest to require ^2.0 without re-installing.
        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lamdrift = "^2.0"
        """).lstrip(), encoding="utf-8")

        bad = subprocess.run(
            LAMC + ["install", "--registry", url, "--frozen"],
            cwd=str(proj), capture_output=True, text=True)
        assert bad.returncode != 0, \
            f"expected --frozen to refuse drift, got rc=0:\n{bad.stdout}\n{bad.stderr}"
        combined = (bad.stdout + bad.stderr).lower()
        assert "drift" in combined, combined
        assert "lamdrift" in combined, combined
    print("PASS: --frozen refuses manifest \u2194 lockfile drift")


def test_frozen_offline_works_after_cache_warmup() -> None:
    """The Docker / CI use case: a populated cache + lockfile is
    enough to succeed without any network access. Combine
    ``--frozen --offline`` and the install reads only from the
    on-disk cache."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamcold", "1.2.0"))
        proj = tmp_p / "proj"; proj.mkdir()

        (proj / "lamlib.toml").write_text(textwrap.dedent("""
            [library]
            name    = "myapp"
            version = "0.1.0"

            [dependencies]
            lamcold = "^1.0"
        """).lstrip(), encoding="utf-8")

        # Warm the cache + lockfile.
        warm = subprocess.run(
            LAMC + ["install", "--registry", url],
            cwd=str(proj), capture_output=True, text=True)
        assert warm.returncode == 0, warm.stderr
        shutil.rmtree(proj / "extlibs")

        # Now stop the registry by passing an unroutable URL: with
        # --frozen --offline, the registry url is irrelevant
        # (we only consult the cache). The lockfile already records
        # the original tarball URL, but Registry.download short-
        # circuits on a verified cache hit before any network call.
        cold = subprocess.run(
            LAMC + ["install",
                    "--registry", "http://127.0.0.1:1",  # closed port
                    "--frozen", "--offline"],
            cwd=str(proj), capture_output=True, text=True)
        assert cold.returncode == 0, \
            f"--frozen --offline should succeed from cache:\n{cold.stdout}\n{cold.stderr}"
        assert (proj / "extlibs" / "lamcold" / "__init__.lam").exists()
    print("PASS: --frozen --offline succeeds from cache")


def test_frozen_rejects_positional_specs() -> None:
    """``--frozen`` reads the lockfile and ignores positional specs;
    combining the two is ambiguous so we refuse rather than picking
    one interpretation."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        proj = tmp_p / "proj"; proj.mkdir()

        bad = subprocess.run(
            LAMC + ["install", "--registry", url, "--frozen", "lamfoo"],
            cwd=str(proj), capture_output=True, text=True)
        assert bad.returncode != 0, \
            f"expected refusal, got rc=0:\n{bad.stdout}\n{bad.stderr}"
        combined = (bad.stdout + bad.stderr).lower()
        assert "positional" in combined or "spec" in combined, combined
    print("PASS: --frozen + positional specs refused")


def test_global_flag_skips_project_state() -> None:
    """``--global`` opts out of project mode: the install lands at
    the chosen extlibs dir, but no ``lamlib.lock.toml`` is written
    next to the cwd, and no project ``lamlib.toml`` is consulted.
    This is the exact behaviour the old ``--vendor=False`` (default)
    flag had — now you have to ask for it explicitly."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamglobal", "1.0.0"))
        proj = tmp_p / "proj"; proj.mkdir()
        ext  = tmp_p / "ext";  ext.mkdir()

        proc = subprocess.run(
            LAMC + ["install", "--registry", url, "--global",
                    "--extlibs-dir", str(ext), "lamglobal"],
            cwd=str(proj), capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

        assert (ext / "lamglobal" / "__init__.lam").exists()
        # Critically: no lockfile in cwd.
        assert not (proj / "lamlib.lock.toml").exists(), \
            "expected --global to skip lockfile generation"
    print("PASS: --global skips project lockfile + manifest read")


def test_cache_populates_and_satisfies_repeat_install() -> None:
    """First install of a registry tarball lands in the cache;
    second install reuses it. We can't directly assert "no network
    call" without monkeypatching, but the cache file appearing at
    the content-addressed path is the necessary precondition for
    ``--offline`` to succeed later."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamcache", "0.1.0"))
        ext = tmp_p / "ext"; ext.mkdir()

        ins1 = _install(url, ext, "lamcache")
        assert ins1.returncode == 0, ins1.stderr

        # Cache should contain a single sha-sharded tarball.
        cache_root = Path(os.environ["LAMC_CACHE"]) / "tarballs"
        assert cache_root.exists(), \
            f"cache dir not created at {cache_root}"
        all_tars = list(cache_root.rglob("*.tar.gz"))
        assert len(all_tars) >= 1, \
            f"expected at least one cached tarball, got {all_tars}"

        # Second install (clean extlibs) reuses the cached entry.
        ext2 = tmp_p / "ext2"; ext2.mkdir()
        ins2 = _install(url, ext2, "lamcache")
        assert ins2.returncode == 0, ins2.stderr
        assert (ext2 / "lamcache" / "__init__.lam").exists()
    print("PASS: cache populates on first install + satisfies second")


def test_lockfile_v0_loads_and_upgrades_on_write() -> None:
    """A pre-existing v0 lockfile (no [meta] block) must load
    cleanly and be silently upgraded to v1 on the next install
    that touches it. No user-facing migration step required."""
    with tempfile.TemporaryDirectory() as tmp, registry() as url:
        tmp_p = Path(tmp)
        _publish(url, _make_lib(tmp_p, "lamupgrade", "0.1.0"))
        proj = tmp_p / "proj"; proj.mkdir()

        # Hand-craft a v0 lockfile shape (no [meta] block, single
        # historical pin, scalar requested_by on a go pin).
        v0 = (
            "# Auto-generated by `lamc install` — do not edit by hand.\n"
            "\n"
            "[pins.lamhistoric]\n"
            'name    = "lamhistoric"\n'
            'version = "0.0.1"\n'
            'source  = "registry"\n'
        )
        (proj / "lamlib.lock.toml").write_text(v0, encoding="utf-8")

        proc = subprocess.run(
            LAMC + ["install", "--registry", url, "lamupgrade"],
            cwd=str(proj), capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr

        upgraded = (proj / "lamlib.lock.toml").read_text()
        # v1 markers now present.
        assert "[meta]" in upgraded, upgraded
        assert "schema = 1" in upgraded, upgraded
        # Old pin survives the upgrade rewrite.
        assert "lamhistoric" in upgraded, upgraded
        # New install was added.
        assert "lamupgrade" in upgraded, upgraded
    print("PASS: v0 lockfile upgrades silently to v1 on next write")


# ── Driver ───────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_publish_then_install_roundtrip,
        test_scoped_publish_and_install,
        test_install_pinned_version,
        test_lockfile_written_in_default_project_mode,
        test_uninstall_removes_tree_and_lock_pin,
        test_semver_gate_blocks_silent_break,
        test_path_install_works_offline,
        test_lockfile_v1_schema_fields,
        test_bare_install_reads_project_manifest,
        test_install_spec_writes_manifest_dependency,
        test_git_install_writes_manifest_dependency,
        test_bare_install_reads_git_dependency,
        test_frozen_succeeds_when_manifest_and_lockfile_agree,
        test_frozen_refuses_on_manifest_lockfile_drift,
        test_frozen_offline_works_after_cache_warmup,
        test_frozen_rejects_positional_specs,
        test_global_flag_skips_project_state,
        test_cache_populates_and_satisfies_repeat_install,
        test_lockfile_v0_loads_and_upgrades_on_write,
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
        print(f"Install CLI: {failures} of {len(tests)} tests failed")
        return 1
    print(f"Install CLI: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
