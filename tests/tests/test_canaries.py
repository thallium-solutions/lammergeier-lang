#!/usr/bin/env python3
"""Canary builds: the downstream projects build with the working-tree compiler.

Builds (never runs, needs no service) the programs of the projects that use
Lam, with ``compiler/lammergeier.py`` of this checkout:

- Luxyt (``LAM_CANARY_LUXYT``, default ``<code>/luxyt/server``): every
  ``cmd/*.lam``, from that directory (its ``lamlib.toml`` resolves ``lib/``
  and ``extlibs/``);
- managed-file-transfer (``LAM_CANARY_MFT``, default
  ``<code>/managed-file-transfer``): ``main.lam`` with ``--extlibs extlibs
  --extlibs lib``;
- every official package ``lam-*`` of ``LAM_CANARY_PACKAGES`` (default the
  directory of the compiler's main checkout): every test program of its
  ``tests/`` (a ``.lam`` file with ``func main``), against an ``extlibs``
  tree holding a copy of every package (so the packages resolve each other
  from the local checkouts, as their own runners stage them).

``<code>`` is the directory above the one holding the compiler's main
checkout (``git rev-parse --git-common-dir``, so a worktree finds it too).
A project whose checkout is absent is skipped with a note. Binaries go to a
temporary directory; the ``git status`` of every project is compared before
and after (a project must not be modified by its build).

Usage::

    python3 tests/tests/test_canaries.py                 # every project
    python3 tests/tests/test_canaries.py --only luxyt --only lam-jwt -v
    sh scripts/canaries.sh                               # the same
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"
BUILD_TIMEOUT = 1200
MAIN = re.compile(r"^(async\s+)?func\s+main\s*\(", re.MULTILINE)
NAME = re.compile(r'^\s*name\s*=\s*"([^"]+)"', re.MULTILINE)


def main_checkout() -> Path:
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                cwd=ROOT, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        common = ""
    return Path(common).parent if common else ROOT


@dataclass
class Target:
    label: str
    source: Path
    cwd: Path
    extra: list[str] = field(default_factory=list)
    ok: bool = False
    detail: str = ""
    seconds: float = 0.0


@dataclass
class Project:
    name: str
    repo: Path
    targets: list[Target]
    note: str = ""
    seconds: float = 0.0
    status_before: str = ""
    status_after: str = ""


def git_status(repo: Path) -> str:
    """The working-tree state: changed, untracked and ignored paths, and
    the content of the changes (a build must leave all of them alone)."""
    status = subprocess.run(["git", "status", "--porcelain", "--ignored", "-uall"], cwd=repo,
                            capture_output=True, text=True, timeout=120).stdout
    diff = subprocess.run(["git", "diff", "HEAD"], cwd=repo, capture_output=True, text=True, timeout=120).stdout
    return status + "\n--- diff\n" + diff


def is_program(path: Path) -> bool:
    try:
        return bool(MAIN.search(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return False


def package_dirs(base: Path) -> list[tuple[str, Path]]:
    """``(package name, checkout)`` of every ``lam-*`` official package."""
    found = []
    for d in sorted(base.glob("lam-*")):
        manifest = d / "lamlib.toml"
        if not manifest.is_file():
            continue  # e.g. a compiler worktree (lam-next)
        m = NAME.search(manifest.read_text(encoding="utf-8"))
        if m and m.group(1).startswith("@lam/"):
            found.append((m.group(1), d))
    return found


# Fixture packages a package's own test runner writes into its extlibs tree
# (``(runner, function)``, called with the staged tree): the canary calls the
# same function, so the test programs that import them build.
FIXTURES = {
    "lam-ssh": ("tests/run_tests.py", "write_composition_fixture"),
}


def stage_extlibs(packages: list[tuple[str, Path]], dest: Path) -> None:
    for name, src in packages:
        shutil.copytree(src, dest / name, ignore=shutil.ignore_patterns(
            ".git", "build", "extlibs", "*.lamc-cache", "__pycache__", "*.pyc", "node_modules"))
        fixture = FIXTURES.get(src.name)
        if fixture and (src / fixture[0]).is_file():
            runner = src / fixture[0]
            # Executed from its text: importing it would write a __pycache__
            # into the package checkout.
            namespace: dict = {"__name__": "lam_canary_fixture", "__file__": str(runner)}
            exec(compile(runner.read_text(encoding="utf-8"), str(runner), "exec"), namespace)
            if fixture[1] in namespace:
                namespace[fixture[1]](dest)


def projects(code: Path, packages_base: Path, extlibs: Path) -> list[Project]:
    out: list[Project] = []
    luxyt = Path(os.environ.get("LAM_CANARY_LUXYT", code / "luxyt" / "server"))
    if (luxyt / "lamlib.toml").is_file():
        cmds = sorted((luxyt / "cmd").glob("*.lam"))
        out.append(Project("luxyt", luxyt, [Target(f"cmd/{p.name}", p, luxyt) for p in cmds]))
    else:
        out.append(Project("luxyt", luxyt, [], note=f"skipped: no checkout at {luxyt}"))
    mft = Path(os.environ.get("LAM_CANARY_MFT", code / "managed-file-transfer"))
    if (mft / "main.lam").is_file():
        out.append(Project("managed-file-transfer", mft, [
            Target("main.lam", mft / "main.lam", mft, ["--extlibs", "extlibs", "--extlibs", "lib"])]))
    else:
        out.append(Project("managed-file-transfer", mft, [], note=f"skipped: no checkout at {mft}"))
    pkgs = package_dirs(packages_base)
    if not pkgs:
        out.append(Project("lam-* packages", packages_base, [], note=f"skipped: no lam-* package in {packages_base}"))
    for name, d in pkgs:
        tests = sorted(p for p in (d / "tests").rglob("*.lam") if is_program(p)) if (d / "tests").is_dir() else []
        targets = []
        notes = []
        for p in tests:
            extra = ["--extlibs", str(extlibs)]
            if d.name == "lam-keyring" and "interop" in p.relative_to(d).parts:
                # Its runner's --mft: built against managed-file-transfer's
                # extlibs/ and lib/ (mft.security).
                if not (mft / "lib" / "mft" / "security.lam").is_file():
                    notes.append(f"{p.relative_to(d)} skipped: no {mft}/lib/mft/security.lam (its runner's --mft)")
                    continue
                extra += ["--extlibs", str(mft / "extlibs"), "--extlibs", str(mft / "lib")]
            targets.append(Target(str(p.relative_to(d)), p, extlibs.parent, extra))
        out.append(Project(d.name, d, targets, note="; ".join(notes) or ("" if tests else "no test programs")))
    return out


def build(target: Target, outdir: Path, workers: str) -> Target:
    start = time.monotonic()
    binary = outdir / re.sub(r"[^A-Za-z0-9_.-]", "_", target.label)
    env = {**os.environ, "LAMC_WORKERS": workers}
    try:
        proc = subprocess.run(
            [sys.executable, str(COMPILER), "build", str(target.source), "-o", str(binary), *target.extra],
            cwd=target.cwd, capture_output=True, text=True, timeout=BUILD_TIMEOUT, env=env,
        )
        target.ok = proc.returncode == 0 and binary.is_file()
        if not target.ok:
            target.detail = (proc.stdout + proc.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        target.detail = f"timed out after {BUILD_TIMEOUT}s"
    target.seconds = time.monotonic() - start
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", action="append", default=[], help="a project name (repeatable)")
    parser.add_argument("--jobs", type=int, default=int(os.environ.get("LAM_CANARY_JOBS", "0")) or
                        max(1, min(8, (os.cpu_count() or 4) // 2)))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # progress shows in a log

    code = main_checkout().parent.parent
    packages_base = Path(os.environ.get("LAM_CANARY_PACKAGES", main_checkout().parent))
    failures = 0
    total_start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="lam_canaries_") as tmp:
        extlibs = Path(tmp) / "stage" / "extlibs"
        extlibs.mkdir(parents=True)
        stage_extlibs(package_dirs(packages_base), extlibs)
        selected = [p for p in projects(code, packages_base, extlibs) if not args.only or p.name in args.only]
        for project in selected:
            if not project.targets:
                print(f"SKIP: {project.name}: {project.note}")
                continue
            project.status_before = git_status(project.repo)
            outdir = Path(tmp) / "bin" / project.name
            outdir.mkdir(parents=True)
            start = time.monotonic()
            jobs = 1 if len(project.targets) == 1 else args.jobs
            workers = "0" if jobs == 1 else "2"
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                list(pool.map(lambda t: build(t, outdir, workers), project.targets))
            project.seconds = time.monotonic() - start
            project.status_after = git_status(project.repo)
            bad = [t for t in project.targets if not t.ok]
            for t in project.targets:
                if not t.ok:
                    print(f"FAIL: {project.name}: {t.label} ({t.seconds:.0f}s)\n{t.detail}")
                elif args.verbose:
                    print(f"  ok: {project.name}: {t.label} ({t.seconds:.1f}s)")
            changed = project.status_before != project.status_after
            if changed:
                print(f"FAIL: {project.name}: the build changed the checkout {project.repo} (git status differs)")
            failures += len(bad) + int(changed)
            verdict = "PASS" if not bad and not changed else "FAIL"
            print(f"{verdict}: {project.name}: {len(project.targets) - len(bad)}/{len(project.targets)} built "
                  f"in {project.seconds:.0f}s" + (f" ({project.note})" if project.note else ""))
    print(f"canaries: {failures} failure(s) in {time.monotonic() - total_start:.0f}s")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
