#!/usr/bin/env python3
"""Deterministic random-program coverage for broad Lam -> Go lowering."""

from __future__ import annotations

import random
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"


def _list(values: list[int]) -> str:
    return "[" + ", ".join(str(value) for value in values) + "]"


def _set(values: list[int]) -> str:
    return "{" + ", ".join(str(value) for value in values) + "}"


def _dict_int(values: dict[str, int]) -> str:
    entries = ", ".join(f'"{key}": {value}' for key, value in values.items())
    return "{" + entries + "}"


def _dict_list(values: dict[str, list[int]]) -> str:
    entries = ", ".join(f'"{key}": {_list(value)}' for key, value in values.items())
    return "{" + entries + "}"


def _run_lam(source: str, expected: list[str], tmp_path: Path, case_idx: int) -> None:
    lam_file = tmp_path / f"random_program_{case_idx}.lam"
    binary = tmp_path / f"random_program_{case_idx}.bin"
    lam_file.write_text(source, encoding="utf-8")

    emitted = subprocess.run(
        [sys.executable, str(COMPILER), str(lam_file), "--emit-go"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _assert_lam_command_ok(
        emitted,
        source,
        tmp_path,
        f"random_program_{case_idx}_emit",
        ["--emit-go"],
    )
    _assert_go_invariants(emitted.stdout)

    compiled = subprocess.run(
        [sys.executable, str(COMPILER), str(lam_file), "-o", str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _assert_lam_command_ok(
        compiled,
        source,
        tmp_path,
        f"random_program_{case_idx}_build",
        ["-o", str(binary)],
    )

    run = subprocess.run(
        [str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip().splitlines() == expected


def _compile_lam(source: str, tmp_path: Path, case_name: str) -> str:
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
    _assert_lam_command_ok(
        emitted,
        source,
        tmp_path,
        f"{case_name}_emit",
        ["--emit-go"],
    )
    _assert_go_invariants(emitted.stdout)

    compiled = subprocess.run(
        [sys.executable, str(COMPILER), str(lam_file), "-o", str(binary)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    _assert_lam_command_ok(
        compiled,
        source,
        tmp_path,
        f"{case_name}_build",
        ["-o", str(binary)],
    )
    assert binary.is_file()
    return emitted.stdout


def _compile_emitted_go(go_source: str, tmp_path: Path, case_name: str) -> None:
    go_dir = tmp_path / f"{case_name}_go"
    go_dir.mkdir()
    (go_dir / "go.mod").write_text(
        f"module lammergeier_fuzz_{case_name}\n\ngo 1.22\n",
        encoding="utf-8",
    )
    go_file = go_dir / "main.go"
    go_file.write_text(go_source, encoding="utf-8")

    formatted = subprocess.run(
        ["gofmt", "-w", str(go_file)],
        cwd=go_dir,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert formatted.returncode == 0, (
        formatted.stderr
        + "\nGenerated Go source:\n"
        + _numbered_source(go_source)
    )

    built = subprocess.run(
        ["go", "test", "./..."],
        cwd=go_dir,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert built.returncode == 0, (
        built.stderr
        + built.stdout
        + "\nGenerated Go source:\n"
        + _numbered_source(go_source)
    )


def _assert_go_invariants(go_source: str) -> None:
    forbidden = [
        "[]interface{}",
        "map[interface{}]",
        "/* in */",
        "/* not in */",
        "/* unknown */",
        "/* unsupported */",
    ]
    for needle in forbidden:
        assert needle not in go_source, needle
    assert "for _, value := range ids" not in go_source
    assert "for _, id := range ids" not in go_source
    assert not re.search(r"var\s+\w+\s+int\s+=\s+math\.Pow", go_source)
    assert not re.search(r"return\s+math\.Pow", go_source)


def _assert_lam_command_ok(
    result: subprocess.CompletedProcess[str],
    source: str,
    tmp_path: Path,
    case_name: str,
    compiler_args: list[str],
) -> None:
    if result.returncode == 0:
        return
    reduced = _shrink_lam_failure(source, tmp_path, case_name, compiler_args, result)
    raise AssertionError(
        result.stderr
        + result.stdout
        + "\nReduced failing Lam source:\n"
        + _numbered_source(reduced)
    )


def _shrink_lam_failure(
    source: str,
    tmp_path: Path,
    case_name: str,
    compiler_args: list[str],
    original: subprocess.CompletedProcess[str],
) -> str:
    """Best-effort line shrinker for unexpected fuzz failures.

    The shrinker runs only after a fuzz case has already failed. It keeps a
    candidate only when the compiler still fails with the same diagnostic
    signal, so a useful Go-build regression is not replaced by an unrelated
    syntax error from an over-shrunk program.
    """
    signal = _failure_signal(original)
    if not signal:
        return source
    current = source
    changed = True
    while changed:
        changed = False
        lines = current.splitlines()
        for idx in range(len(lines)):
            candidate_lines = lines[:idx] + lines[idx + 1:]
            candidate = "\n".join(candidate_lines).strip() + "\n"
            if "func main" not in candidate:
                continue
            lam_file = tmp_path / f"{case_name}_shrink.lam"
            lam_file.write_text(candidate, encoding="utf-8")
            probe = subprocess.run(
                [sys.executable, str(COMPILER), str(lam_file), *compiler_args],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if probe.returncode != 0 and signal in (probe.stderr + probe.stdout):
                current = candidate
                changed = True
                break
    return current


def _failure_signal(result: subprocess.CompletedProcess[str]) -> str:
    for line in (result.stderr + result.stdout).splitlines():
        clean = line.strip()
        if clean.startswith("error:") or "Go build failed" in clean:
            return clean
    return ""


def _numbered_source(source: str) -> str:
    return "\n".join(f"{idx:4d} | {line}" for idx, line in enumerate(source.splitlines(), 1))


@pytest.mark.parametrize("case_idx", range(20))
def test_random_programs_compile_and_run(case_idx: int, tmp_path: Path) -> None:
    rng = random.Random(2026080200 + case_idx)
    nums = [rng.randint(1, 9) for _ in range(5)]
    more = [rng.randint(2, 12) for _ in range(4)]
    ids = [rng.randint(1, 7) for _ in range(6)]
    row_a = [rng.randint(1, 6), rng.randint(7, 12)]
    row_b = [rng.randint(13, 18), rng.randint(19, 24)]
    nested_left = [rng.randint(1, 5), rng.randint(6, 10)]
    nested_right = [rng.randint(11, 15), rng.randint(16, 20)]
    meta = {"lo": min(nums), "hi": max(nums)}
    offset = rng.randint(1, 5)
    power_base = rng.randint(2, 4)
    power_exp = rng.randint(2, 4)

    nums_sum = sum(nums)
    more_sum = sum(more)
    unique_ids = set(ids)
    matrix = [row_a, row_b]
    matrix_total = sum(sum(row) for row in matrix)
    nested_total = sum(nested_left) + sum(nested_right)
    table_total = nested_total + nums[0] + nums[1] + more[0] + more[1]
    expected_total = matrix_total + table_total
    expected_power = power_base ** power_exp
    expected = [
        str(nums_sum),
        str(more_sum),
        str(expected_total),
        "true",
        "true",
        "true",
        "true",
        str(len(unique_ids)),
        str(nums[0] * nums[0]),
        str(nums[0] + nums[1]),
        str(more[0] + more[1]),
        "true",
        str(expected_power),
        str(expected_power),
        "v" + str(nums[0] + offset),
        f"random_{case_idx}_ok",
    ]

    nested_literal = (
        "["
        f"{_dict_list({'left': nested_left, 'right': nested_right})}, "
        f'{{"left": [nums[0], nums[1]], "right": [more[0], more[1]]}}'
        "]"
    )

    selected_ids = [ids[0], ids[1]]
    fallback_ids = [more[0], more[1]]

    source = f"""
func sumValues(values: list[int]) -> int {{
    total: int = 0
    for value in values {{
        total += value
    }}
    return total
}}

func countSet(ids: set[int]) -> int {{
    return len(ids)
}}

func nestedTotal(rows: list[dict[str, list[int]]]) -> int {{
    total: int = 0
    for row in rows {{
        total += sumValues(row["left"])
        total += sumValues(row["right"])
    }}
    return total
}}

func withDefault(values: list[int] = {_list(more)}) -> int {{
    return sumValues(values)
}}

class Bucket {{
    func init(self, rows: list[list[int]], meta: dict[str, int]) {{
        self.rows: list[list[int]] = rows
        self.meta: dict[str, int] = meta
    }}

    func score(self) -> int {{
        total: int = 0
        for row in self.rows {{
            total += sumValues(row)
        }}
        return total + self.meta["lo"] + self.meta["hi"]
    }}
}}

func main() {{
    nums: list[int] = {_list(nums)}
    more: list[int] = {_list(more)}
    matrix: list[list[int]] = [{_list(row_a)}, {_list(row_b)}]
    nested: list[dict[str, list[int]]] = {nested_literal}
    table: dict[str, list[dict[str, list[int]]]] = {{"items": nested}}
    ids: set[int] = {_set(ids)}
    groups: dict[str, set[int]] = {{"ids": {_set(ids)}, "more": {_set(more)}}}

    bucket: Bucket = Bucket(matrix, {_dict_int(meta)})
    labels: list[str] = nums.map(lambda n: "v" + str(n + {offset}))
    picked: list[int] = [n for n in nums if n >= {min(nums)}]
    squares: dict[int, int] = {{n: n * n for n in nums}}
    selected: list[int] = [nums[0], nums[1]] if True else [more[0], more[1]]
    selectedSet: set[int] = ({_set(selected_ids)}) if True else ({_set(fallback_ids)})
    selectedDict: dict[str, list[int]] = ({{"picked": [nums[0], nums[1]]}}) if False else ({{"picked": [more[0], more[1]]}})

    print(sumValues(nums))
    print(withDefault())
    print(bucket.score() + nestedTotal(table["items"]) - {meta["lo"] + meta["hi"]})
    print(nums[0] in picked)
    print("{nums[0] + offset}" in labels[0])
    print("ids" in groups)
    print({ids[0]} in groups["ids"])
    print(countSet({{value for value in ids}}))
    print(squares[nums[0]])
    print(sumValues(selected))
    print(sumValues(selectedDict["picked"]))
    print({ids[0]} in selectedSet)

    powValue: int = {power_base} ** {power_exp}
    print(powValue)
    ternaryPow: int = {power_base} ** {power_exp} if True else 2 ** 2
    print(ternaryPow)
    print(labels[0])

    if {power_base} < powValue < {expected_power + 10} {{
        print("random_{case_idx}_ok")
    }} else {{
        print("random_{case_idx}_bad")
    }}
}}
"""
    _run_lam(source, expected, tmp_path, case_idx)


@pytest.mark.parametrize("case_idx", range(20))
def test_random_deep_programs_compile(case_idx: int, tmp_path: Path) -> None:
    rng = random.Random(2026080300 + case_idx)
    rows = [
        [rng.randint(1, 9), rng.randint(10, 19)],
        [rng.randint(20, 29), rng.randint(30, 39)],
    ]
    alt_rows = [
        [rng.randint(40, 49), rng.randint(50, 59)],
        [rng.randint(60, 69), rng.randint(70, 79)],
    ]
    ids = [rng.randint(1, 12) for _ in range(6)]
    names = [f"n{rng.randint(1, 99)}", f"n{rng.randint(100, 199)}"]
    pick = rng.choice([True, False])
    pick_text = "True" if pick else "False"
    nested_map = (
        "{"
        f'"primary": {{"rows": [{_list(rows[0])}, {_list(rows[1])}]}}, '
        f'"backup": {{"rows": [{_list(alt_rows[0])}, {_list(alt_rows[1])}]}}'
        "}"
    )
    tags = "{" + ", ".join(f'"{name}"' for name in names) + "}"

    source = f"""
func totalRows(rows: list[list[int]]) -> int {{
    total: int = 0
    for row in rows {{
        for value in row {{
            total += value
        }}
    }}
    return total
}}

func hasTag(tags: set[str], tag: str) -> bool {{
    return tag in tags
}}

func main() {{
    nested: dict[str, dict[str, list[list[int]]]] = {nested_map}
    selected: list[list[int]] = nested["primary"]["rows"] if {pick_text} else nested["backup"]["rows"]
    replaced: list[list[int]] = [[1, 2], [3, 4]]
    replaced[0:2] = selected

    ids: set[int] = {_set(ids)}
    copied: set[int] = {{id for id in ids}}
    tagSet: set[str] = {tags}
    tagChoice: set[str] = ({tags}) if True else ({tags})
    aliases: dict[str, set[str]] = {{"tags": tagChoice}}
    labels: list[str] = [tag for tag in aliases["tags"]]
    sizes: dict[str, int] = {{"rows": totalRows(replaced), "ids": len(copied)}}

    powValue: int = 3 ** 3 if True else 2 ** 2
    ok: bool = hasTag(aliases["tags"], "{names[0]}") and ("rows" in sizes) and (1 < powValue < 40)

    print(totalRows(replaced))
    print(sizes["ids"])
    print(ok)
    print(len(labels))
}}
"""
    _compile_lam(source, tmp_path, f"random_deep_program_{case_idx}")


@pytest.mark.parametrize("case_idx", range(20))
def test_emitted_go_random_programs_compile(case_idx: int, tmp_path: Path) -> None:
    rng = random.Random(2026080400 + case_idx)
    a = [rng.randint(1, 9), rng.randint(10, 19)]
    b = [rng.randint(20, 29), rng.randint(30, 39)]
    c = [rng.randint(40, 49), rng.randint(50, 59)]
    ids = [rng.randint(1, 12) for _ in range(5)]
    labels = [f'"k{rng.randint(1, 99)}"', f'"k{rng.randint(100, 199)}"']
    threshold = rng.randint(20, 60)
    use_primary = "True" if rng.choice([True, False]) else "False"

    source = f"""
func sumValues(values: list[int]) -> int {{
    total: int = 0
    for value in values {{
        total += value
    }}
    return total
}}

func consumePayload(payload: list[dict[str, dict[str, list[dict[str, list[int]]]]]]) -> int {{
    total: int = 0
    for item in payload {{
        for name, group in item {{
            _ = name
            for bucketName, rows in group {{
                _ = bucketName
                for row in rows {{
                    total += sumValues(row["values"])
                    total += sumValues(row["weights"])
                }}
            }}
        }}
    }}
    return total
}}

class Mixer {{
    static func choose(flag: bool, left: list[int], right: list[int]) -> list[int] {{
        return left if flag else right
    }}

    func init(self, base: list[int], aliases: dict[str, set[str]]) {{
        self.base: list[int] = base
        self.aliases: dict[str, set[str]] = aliases
    }}

    func score(self, extra: list[int] = [{a[0]}, {b[0]}]) -> int {{
        tags: set[str] = self.aliases["tags"]
        return sumValues(self.base) + sumValues(extra) + len(tags)
    }}
}}

func main() {{
    payload: list[dict[str, dict[str, list[dict[str, list[int]]]]]] = [
        {{"primary": {{"left": [{{"values": {_list(a)}, "weights": {_list(b)}}}]}}}},
        {{"backup": {{"right": [{{"values": {_list(c)}, "weights": [1, 2]}}]}}}},
    ]
    anonymousScore: int = consumePayload([
        {{"inline": {{"rows": [{{"values": [1, 2, 3], "weights": [4, 5]}}]}}}},
    ])
    selected: list[int] = Mixer.choose({use_primary}, [payload[0]["primary"]["left"][0]["values"][0], payload[0]["primary"]["left"][0]["values"][1]], {_list(c)})
    selected[0:1] = [{a[0]}]

    ids: set[int] = {_set(ids)}
    copied: set[int] = {{id for id in ids}}
    aliases: dict[str, set[str]] = {{"tags": {{{", ".join(labels)}}}}}
    mixer: Mixer = Mixer(selected, aliases)
    mapped: list[int] = selected.map(lambda n: n + len(copied))
    chosen: dict[str, list[int]] = ({{"items": mapped}}) if mixer.score() > {threshold} else ({{"items": [1, 2]}})

    print(consumePayload(payload))
    print(anonymousScore)
    print(sumValues(chosen["items"]))
    print({ids[0]} in copied)
}}
"""
    go_source = _compile_lam(source, tmp_path, f"emitted_go_random_{case_idx}")
    _compile_emitted_go(go_source, tmp_path, f"emitted_go_random_{case_idx}")
