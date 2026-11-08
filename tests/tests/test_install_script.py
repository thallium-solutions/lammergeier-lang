#!/usr/bin/env python3
"""Tests for the repository installer ``install.sh``.

Every run installs into a temporary prefix with ``--no-pip --no-editor``
and a ``PATH`` whose ``sudo`` is a stub that records the call and fails,
so the tests never elevate nor touch the user's installation.
"""

from __future__ import annotations

import os
import site
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
INSTALL = ROOT / "install.sh"


def _stub_path(tmp: Path) -> tuple[dict, Path]:
    """Environment whose ``sudo`` fails (logging its arguments) and whose
    ``python3`` is the interpreter running the tests."""
    stubs = tmp / "stubs"
    stubs.mkdir()
    log = tmp / "sudo.log"
    sudo = stubs / "sudo"
    sudo.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\necho "sudo: a terminal is required" >&2\nexit 1\n',
                    encoding="utf-8")
    sudo.chmod(0o755)
    (stubs / "python3").symlink_to(sys.executable)
    env = dict(os.environ)
    env["PATH"] = f"{stubs}{os.pathsep}{env.get('PATH', '')}"
    # A fake HOME for --user; keep the real user site-packages (lark).
    env["PYTHONUSERBASE"] = env.get("PYTHONUSERBASE") or site.getuserbase()
    env["HOME"] = str(tmp / "home")
    (tmp / "home").mkdir()
    return env, log


def _install(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(INSTALL), "--no-pip", "--no-editor", *args],
                          cwd=str(ROOT), env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=120)


def _lamc_version(env: dict, bin_dir: Path) -> None:
    lamc = bin_dir / "lamc"
    assert lamc.is_symlink(), f"{lamc} is not a symlink"
    assert os.path.realpath(lamc) == str((ROOT / "lamc").resolve())
    proc = subprocess.run([str(lamc), "version"], env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip(), "lamc version printed nothing"
    assert (bin_dir / "lammergeier-lsp").is_symlink()


def test_prefix_with_missing_parents() -> None:
    """Regression: ``--prefix DIR`` with a missing DIR checked whether DIR
    (the parent of DIR/bin) was writable, which a missing directory never
    is, and fell back to ``sudo mkdir`` — failing without a terminal."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        env, sudo_log = _stub_path(tmp_p)
        prefix = tmp_p / "missing" / "deeper"
        proc = _install(env, "--prefix", str(prefix))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not sudo_log.exists(), f"sudo was called: {sudo_log.read_text()}"
        _lamc_version(env, prefix / "bin")

        # Re-running over the existing install replaces the links.
        again = _install(env, f"--prefix={prefix}")
        assert again.returncode == 0, again.stdout + again.stderr
        assert "replacing existing" in again.stdout, again.stdout
        assert not sudo_log.exists()
        _lamc_version(env, prefix / "bin")
    print("PASS: install.sh --prefix creates missing parents without sudo")


def test_user_with_missing_local_dir() -> None:
    """``--user`` with no ``~/.local`` yet creates ``~/.local/bin`` itself."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        env, sudo_log = _stub_path(tmp_p)
        proc = _install(env, "--user")
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not sudo_log.exists(), f"sudo was called: {sudo_log.read_text()}"
        _lamc_version(env, tmp_p / "home" / ".local" / "bin")
    print("PASS: install.sh --user creates ~/.local/bin without sudo")


def test_dry_run_touches_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        env, sudo_log = _stub_path(tmp_p)
        prefix = tmp_p / "missing" / "deeper"
        proc = _install(env, "--dry-run", "--prefix", str(prefix))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert f"[dry-run] mkdir -p '{prefix / 'bin'}'" in proc.stdout, proc.stdout
        assert "[dry-run] sudo" not in proc.stdout, proc.stdout
        assert f"ln -s '{ROOT / 'lamc'}' '{prefix / 'bin' / 'lamc'}'" in proc.stdout, proc.stdout
        assert not (tmp_p / "missing").exists(), "--dry-run must not create directories"
        assert not sudo_log.exists()

        system = _install(env, "--dry-run", "--system")
        assert system.returncode == 0, system.stdout + system.stderr
        assert "bin prefix: /usr/local/bin" in system.stdout, system.stdout
        assert "[dry-run]" in system.stdout and "/usr/local/bin/lamc" in system.stdout, system.stdout
        assert not sudo_log.exists(), "--dry-run must not run sudo"
    print("PASS: install.sh --dry-run prints the plan and touches nothing")


def test_unwritable_ancestor_uses_sudo() -> None:
    """sudo stays the fallback when the nearest existing ancestor of the
    prefix is not writable."""
    if os.geteuid() == 0:
        print("SKIP: install.sh unwritable ancestor (running as root)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        tmp_p = Path(tmp)
        env, sudo_log = _stub_path(tmp_p)
        locked = tmp_p / "locked"
        locked.mkdir()
        locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
        try:
            proc = _install(env, "--prefix", str(locked / "missing" / "deeper"))
        finally:
            locked.chmod(0o755)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert sudo_log.exists(), "the installer must fall back to sudo"
        assert f"mkdir -p {locked / 'missing' / 'deeper' / 'bin'}" in sudo_log.read_text(), sudo_log.read_text()
        assert not (locked / "missing").exists()
    print("PASS: install.sh falls back to sudo for an unwritable ancestor")


def main() -> int:
    tests = [
        test_prefix_with_missing_parents,
        test_user_with_missing_local_dir,
        test_dry_run_touches_nothing,
        test_unwritable_ancestor_uses_sudo,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL: {test.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"ERROR: {test.__name__}: {exc!r}")
    print()
    print("=" * 60)
    if failures:
        print(f"install.sh: {failures} of {len(tests)} tests failed")
        return 1
    print(f"install.sh: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
