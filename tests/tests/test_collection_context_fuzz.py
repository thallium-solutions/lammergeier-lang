#!/usr/bin/env python3
"""Seeded fuzz coverage for typed collection lowering contexts."""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"


def _run_lam(source: str, tmp_path: Path, case_name: str) -> tuple[str, str]:
    lam_file = tmp_path / f"{case_name}.lam"
    binary = tmp_path / f"{case_name}.bin"
    lam_file.write_text(source, encoding="utf-8")

    emitted = subprocess.run(
        [sys.executable, str(COMPILER), str(lam_file), "--emit-go"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert emitted.returncode == 0, emitted.stderr
    assert "[]interface{}" not in emitted.stdout
    assert "map[interface{}]" not in emitted.stdout

    compiled = subprocess.run(
        [sys.executable, str(COMPILER), str(lam_file), "-o", str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    assert binary.is_file()

    run = subprocess.run(
        [str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert run.returncode == 0, run.stderr
    return emitted.stdout, run.stdout.strip()


def _list(values: list[int]) -> str:
    return "[" + ", ".join(str(value) for value in values) + "]"


def _set(values: list[int]) -> str:
    return "{" + ", ".join(str(value) for value in values) + "}"


def test_seeded_collection_contexts(tmp_path: Path) -> None:
    rng = random.Random(20260802)

    for case_idx in range(10):
        nums = [rng.randint(1, 30) for _ in range(5)]
        stack = [rng.randint(1, 30) for _ in range(4)]
        rows = [[rng.randint(1, 9), rng.randint(1, 9)] for _ in range(3)]
        replacement = [[rng.randint(10, 30), rng.randint(10, 30)] for _ in range(2)]
        row_lo = rng.randint(0, 1)
        row_hi = row_lo + 1
        pick_idx = rng.randint(0, len(nums) - 1)
        ids = [rng.randint(1, 12) for _ in range(6)]
        more_ids = [rng.randint(13, 30) for _ in range(4)]

        sorted_nums = sorted(nums)
        stack_after = stack[:-1]
        rows_after = rows[:row_lo] + replacement + rows[row_hi:]
        unique_ids = set(ids)
        unique_more_ids = set(more_ids)
        expected = [
            str(sorted_nums[0]),
            str(sorted_nums[-1]),
            str(stack[-1]),
            str(len(stack_after)),
            str(rows_after[row_lo][1]),
            str(len(rows_after)),
            str(nums[pick_idx]),
            "v" + str(nums[0]),
            str(len(unique_ids)),
            "true",
            str(len(unique_more_ids)),
            "true",
            "3",
            "2",
            f"case_{case_idx}_ok",
        ]

        source = f"""
func tag(n: int) -> str {{
    return "v" + str(n)
}}

func setSize(ids: set[int]) -> int {{
    return len(ids)
}}

func main() {{
    nums: list[int] = {_list(nums)}
    sortedNums: list[int] = sorted(nums)
    print(sortedNums[0])
    print(sortedNums[{len(sorted_nums) - 1}])

    stack: list[int] = {_list(stack)}
    top: int = stack.pop()
    print(top)
    print(len(stack))

    rows: list[list[int]] = [{", ".join(_list(row) for row in rows)}]
    rows[{row_lo}:{row_hi}] = [{", ".join(_list(row) for row in replacement)}]
    print(rows[{row_lo}][1])
    print(len(rows))

    labels: list[str] = nums.map(lambda n: str(n))
    print(labels[{pick_idx}])

    tagged: list[str] = nums.map(tag)
    print(tagged[0])

    ids: set[int] = {_set(ids)}
    print(len(ids))
    print({ids[0]} in ids)

    ids = {_set(more_ids)}
    print(len(ids))
    print({more_ids[-1]} in ids)

    squares: set[int] = {{i * i for i in range(3)}}
    print(len(squares))
    print(setSize({{8, 9}}))

    print("case_{case_idx}_ok")
}}
"""
        _go, stdout = _run_lam(source, tmp_path, f"collection_context_{case_idx}")
        assert stdout.splitlines() == expected


def test_missing_required_arg_stays_lam_error_without_semantic(tmp_path: Path) -> None:
    lam_file = tmp_path / "missing_arg.lam"
    lam_file.write_text(
        """
func greet(name: str) -> str {
    return name
}

func main() {
    print(greet())
}
""",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            sys.executable,
            str(COMPILER),
            str(lam_file),
            "--emit-go",
            "--no-semantic-check",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0
    assert "missing required argument `name` in call to `greet`" in proc.stderr
