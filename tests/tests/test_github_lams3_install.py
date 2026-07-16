#!/usr/bin/env python3
"""Guarded GitHub integration test for the external lams3 repository.

This test is skipped by default because it talks to GitHub. Enable it with:

    LAMC_LIVE_GITHUB_LAMS3=1 python3 tests/tests/test_github_lams3_install.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]
LAMS3_GIT_URL = "https://github.com/thallium-solutions/lams3.git"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run(args: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        LAMC + args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_github_lams3_git_dependency_roundtrip() -> None:
    if os.environ.get("LAMC_LIVE_GITHUB_LAMS3") != "1":
        print("SKIP: set LAMC_LIVE_GITHUB_LAMS3=1 to install lams3 from GitHub")
        return
    if shutil.which("git") is None:
        raise AssertionError("git not found on PATH")

    with tempfile.TemporaryDirectory(prefix="lamc-github-lams3-") as tmp:
        tmp_path = Path(tmp)
        project = tmp_path / "project"
        cache = tmp_path / "cache"
        project.mkdir()

        env = dict(os.environ)
        env["LAMC_CACHE"] = str(cache)
        env.pop("LAMC_EXTLIBS", None)

        _write(
            project / "lamlib.toml",
            textwrap.dedent(
                """
                [library]
                name = "github_lams3_smoke"
                version = "0.1.0"
                """
            ).lstrip(),
        )

        installed = _run(["install", LAMS3_GIT_URL], project, env)
        assert installed.returncode == 0, installed.stderr

        manifest = (project / "lamlib.toml").read_text(encoding="utf-8")
        assert f'lams3 = {{ git = "{LAMS3_GIT_URL}" }}' in manifest, manifest
        lock = (project / "lamlib.lock.toml").read_text(encoding="utf-8")
        assert 'source = "git"' in lock, lock
        assert f'url = "{LAMS3_GIT_URL}"' in lock, lock
        assert "ref =" in lock, lock
        assert (project / "extlibs" / "lams3" / "lamlib.toml").exists()

        # Prove the lockfile can replay the GitHub source from the local git
        # cache without contacting the network again.
        shutil.rmtree(project / "extlibs")
        frozen = _run(["install", "--frozen", "--offline"], project, env)
        assert frozen.returncode == 0, frozen.stderr
        assert (project / "extlibs" / "lams3" / "lamlib.toml").exists()

        _write(
            project / "main.lam",
            textwrap.dedent(
                """
                from lams3 import S3

                func main() {
                    print(S3.publicUrl("test-bucket.storage-ts.com", "folder/file name.txt"))
                }
                """
            ).lstrip(),
        )

        # --emit-go still resolves and transpiles the external lams3 module, but
        # avoids fetching Go module deps just to link a smoke binary.
        emitted = _run(["build", "--emit-go", str(project / "main.lam")], project, env)
        assert emitted.returncode == 0, emitted.stderr
        assert "test-bucket.storage-ts.com" in emitted.stdout, emitted.stdout
        assert "S3_publicUrl" in emitted.stdout, emitted.stdout
    print("PASS: GitHub lams3 installs, writes back, replays offline, and transpiles")


def main() -> int:
    tests = [test_github_lams3_git_dependency_roundtrip]
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
        print(f"GitHub lams3: {failures} of {len(tests)} tests failed")
        return 1
    print(f"GitHub lams3: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
