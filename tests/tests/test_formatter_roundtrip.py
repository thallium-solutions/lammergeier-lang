#!/usr/bin/env python3
"""``lamc fmt`` round trips over real code.

1. Every ``.lam`` file of the test suites, the standard library, the
   examples and (read-only, in memory) the sibling ``lam-*`` library
   checkouts formats to code that still parses and is a fixed point of the
   formatter (idempotence). Files that do not parse as written (syntax
   error fixtures) are skipped.
2. For every core test case the generated Go of the formatted source equals
   the generated Go of the original (``//line`` directives aside, since the
   formatter may move code to other lines): formatting never changes what a
   program does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.formatter import FormatError, format_lam_source  # noqa: E402

COMPILER = ROOT / "compiler" / "lammergeier.py"
CASES = ROOT / "tests" / "tests" / "cases"


def _corpus() -> list[Path]:
    files = sorted((ROOT / "tests").rglob("*.lam"))
    files += sorted((ROOT / "lib").glob("*.lam"))
    files += sorted((ROOT / "examples").rglob("*.lam"))
    for sibling in sorted(ROOT.parent.glob("lam-*")):
        if sibling.is_dir():
            files += sorted(p for p in sibling.rglob("*.lam") if "extlibs" not in p.parts and ".git" not in p.parts)
    return files


def _check_file(path_str: str) -> tuple[str, str]:
    """``(status, detail)``: ``ok``, ``skip`` (does not parse as written)
    or ``fail``."""
    path = Path(path_str)
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "skip", ""
    try:
        first = format_lam_source(source).text
    except FormatError:
        return "skip", ""
    except Exception as e:  # noqa: BLE001 - report any crash as a failure
        return "fail", f"{path}: formatter crashed: {e!r}"
    try:
        second = format_lam_source(first).text
    except Exception as e:  # noqa: BLE001
        return "fail", f"{path}: formatted output does not reformat: {e!r}"
    if second != first:
        return "fail", f"{path}: formatting is not idempotent"
    return "ok", ""


def test_formatter_is_idempotent_on_the_corpus() -> None:
    files = _corpus()
    with ProcessPoolExecutor() as pool:
        results = list(pool.map(_check_file, [str(f) for f in files], chunksize=8))
    failures = [detail for status, detail in results if status == "fail"]
    checked = sum(1 for status, _ in results if status == "ok")
    assert checked > 600, checked
    assert not failures, "\n".join(failures[:40])
    print(f"PASS: fmt is idempotent on {checked} files ({len(files) - checked} skipped as unparsable fixtures)")


def _emit_go(path: Path) -> str:
    proc = subprocess.run(
        [sys.executable, str(COMPILER), str(path), "--emit-go", "--no-cache"],
        capture_output=True, text=True, timeout=300, cwd=str(path.parent),
    )
    return proc.stdout + ("\nEXIT %d\n" % proc.returncode if proc.returncode else "")


def _normalized(go: str, root: Path) -> str:
    lines = [line for line in go.splitlines() if not line.lstrip().startswith("//line ")]
    return "\n".join(lines).replace(str(root), "<ROOT>")


def _compare_case(args: tuple[str, str, str, str]) -> str:
    rel, orig_root, fmt_root, _ = args
    original = _normalized(_emit_go(Path(orig_root) / rel), Path(orig_root))
    formatted = _normalized(_emit_go(Path(fmt_root) / rel), Path(fmt_root))
    if original != formatted:
        return rel
    return ""


def test_formatting_preserves_generated_go() -> None:
    with tempfile.TemporaryDirectory(prefix="lam_fmt_rt_") as tmp:
        orig_root = Path(tmp) / "a"
        fmt_root = Path(tmp) / "b"
        shutil.copytree(CASES, orig_root, ignore=shutil.ignore_patterns("*.go"))
        shutil.copytree(CASES, fmt_root, ignore=shutil.ignore_patterns("*.go"))
        rels: list[str] = []
        changed = 0
        for path in sorted(fmt_root.rglob("*.lam")):
            source = path.read_text(encoding="utf-8")
            try:
                formatted = format_lam_source(source).text
            except FormatError:
                continue
            if formatted != source:
                path.write_text(formatted, encoding="utf-8")
                changed += 1
                rels.append(str(path.relative_to(fmt_root)))
        jobs = [(rel, str(orig_root), str(fmt_root), "") for rel in rels]
        with ProcessPoolExecutor(max_workers=os.cpu_count()) as pool:
            mismatches = [rel for rel in pool.map(_compare_case, jobs) if rel]
    assert not mismatches, "generated Go changed by formatting:\n" + "\n".join(mismatches[:40])
    print(f"PASS: formatting {changed} core test files leaves their generated Go unchanged")


if __name__ == "__main__":
    test_formatter_is_idempotent_on_the_corpus()
    test_formatting_preserves_generated_go()
