#!/usr/bin/env python3
"""Differential fuzz of the generator state machine lowering.

Random generator bodies (deterministic seed) combine nested ``for`` loops
(over lists, ranges with steps, strings, ``enumerate``, a dict, a generator
call), ``while`` loops, loop ``else``, ``if`` / ``else``, ``match``, ``try`` /
``finally``, ``try`` / ``catch`` around a ``raise``, ``yield from``,
``break`` / ``continue``, locals declared in loop bodies, ``go!`` blocks
reading locals, and
``yield``s at random positions whose values use the variables in scope
(f-string slots, with and without a format spec, concatenations). Each body
is compiled twice: as is (a state machine, ``compiler/visitors/generators.py``)
and with a ``defer`` added, which keeps the coroutine lowering; both must
build and give the same values.

``LAM_GEN_FUZZ_SEED`` / ``LAM_GEN_FUZZ_CASES`` change the seed and the
number of bodies (the default run is bounded for the unit suite).
"""

from __future__ import annotations

import os
import random
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"

SEED = int(os.environ.get("LAM_GEN_FUZZ_SEED", "4931"))
CASES = int(os.environ.get("LAM_GEN_FUZZ_CASES", "48"))
PER_PROGRAM = 24


class _Body:
    """Builds one random generator body."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.counter = 0
        self.lines: list[str] = []

    def fresh(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    def emit(self, depth: int, text: str) -> None:
        self.lines.append("    " * depth + text)

    # ─── Expressions ───────────────────────────────────────────

    def str_value(self, scope: list) -> str:
        rng = self.rng
        names = [name for name, _ in scope]
        if not names or rng.random() < 0.1:
            return f'"c{rng.randint(0, 9)}"'
        picks = rng.sample(names, min(len(names), rng.randint(1, 3)))
        kind = rng.random()
        if kind < 0.55:
            slots = []
            for name in picks:
                slots.append("{" + name + (":" if rng.random() < 0.3 else "") + "}")
            return 'f"' + "/".join(slots) + '"'
        if kind < 0.8:
            name, typ = rng.choice(scope)
            return f'"{name}=" + ' + (name if typ == "str" else f"str({name})")
        ints = [name for name, typ in scope if typ == "int"]
        if ints:
            return f'f"{{{rng.choice(ints)} * 2 + 1}}"'
        return f'f"{{{picks[0]}}}"'

    def int_value(self, scope: list) -> str:
        ints = [name for name, typ in scope if typ == "int"]
        strs = [name for name, typ in scope if typ == "str"]
        rng = self.rng
        choices = [str(rng.randint(0, 3))]
        if ints:
            choices.append(f"{rng.choice(ints)} + {rng.randint(0, 2)}")
        if strs:
            choices.append(f"len({rng.choice(strs)})")
        return rng.choice(choices)

    def condition(self, scope: list) -> str:
        ints = [name for name, typ in scope if typ == "int"]
        strs = [name for name, typ in scope if typ == "str"]
        rng = self.rng
        if ints and (not strs or rng.random() < 0.6):
            return f"{rng.choice(ints)} % {rng.randint(2, 3)} == {rng.randint(0, 1)}"
        if strs:
            return f"len({rng.choice(strs)}) > {rng.randint(0, 2)}"
        return "True"

    # ─── Statements ────────────────────────────────────────────

    def block(self, depth: int, scope: list, nesting: int, in_loop: bool) -> None:
        count = self.rng.randint(1, 4)
        scope = list(scope)
        for _ in range(count):
            self.statement(depth, scope, nesting, in_loop)
        if not any(line.strip().startswith("yield") for line in self.lines[-count:]):
            self.emit(depth, f"yield {self.str_value(scope)}")

    def statement(self, depth: int, scope: list, nesting: int, in_loop: bool) -> None:
        rng = self.rng
        roll = rng.random()
        if roll < 0.32 or nesting >= 3:
            self.emit(depth, f"yield {self.str_value(scope)}")
        elif roll < 0.42:
            name = self.fresh("l")
            self.emit(depth, f"{name}: int = {self.int_value(scope)}")
            scope.append((name, "int"))
        elif roll < 0.48:
            ints = [n for n, t in scope if t == "int"]
            if ints:
                self.emit(depth, "go! {")
                self.emit(depth + 1, f"acc += {rng.choice(ints)}")
                self.emit(depth, "}")
            else:
                self.emit(depth, f"yield {self.str_value(scope)}")
        elif roll < 0.72:
            self.loop(depth, scope, nesting)
        elif roll < 0.82:
            self.emit(depth, f"if {self.condition(scope)} {{")
            self.block(depth + 1, scope, nesting, in_loop)
            if rng.random() < 0.6:
                self.emit(depth, "} else {")
                self.block(depth + 1, scope, nesting, in_loop)
            self.emit(depth, "}")
        elif roll < 0.86:
            self.emit(depth, "try {")
            self.block(depth + 1, scope, nesting, in_loop)
            if rng.random() < 0.5:
                self.emit(depth, "} finally {")
                self.emit(depth + 1, f"acc += {rng.randint(1, 5)}")
            else:
                self.emit(depth + 1, f"if {self.condition(scope)} {{")
                self.emit(depth + 2, f'raise ValueError("e{rng.randint(0, 9)}")')
                self.emit(depth + 1, "}")
                self.emit(depth, "} catch ValueError as err {")
                self.emit(depth + 1, f"yield str(err)")
                self.emit(depth + 1, f"yield {self.str_value(scope)}")
            self.emit(depth, "}")
        elif roll < 0.9:
            ints = [n for n, t in scope if t == "int"]
            subject = rng.choice(ints) if ints else "acc"
            self.emit(depth, f"match {subject} % 3 {{")
            self.emit(depth + 1, "case 0 {")
            self.block(depth + 2, scope, nesting + 1, in_loop)
            self.emit(depth + 1, "}")
            self.emit(depth + 1, "case _ {")
            self.block(depth + 2, scope, nesting + 1, in_loop)
            self.emit(depth + 1, "}")
            self.emit(depth, "}")
        elif roll < 0.93:
            if rng.random() < 0.5:
                self.emit(depth, f"yield from inner({self.int_value(scope)})")
            else:
                name = self.fresh("z")
                self.emit(depth, f"for {name} in inner({self.int_value(scope)}) {{")
                self.block(depth + 1, scope + [(name, "str")], nesting + 1, True)
                self.emit(depth, "}")
        elif in_loop:
            self.emit(depth, f"if {self.condition(scope)} {{")
            if rng.random() < 0.7:
                self.emit(depth + 1, f"yield {self.str_value(scope)}")
            self.emit(depth + 1, rng.choice(["break", "continue"]))
            self.emit(depth, "}")
        else:
            self.emit(depth, f"yield {self.str_value(scope)}")

    def loop(self, depth: int, scope: list, nesting: int) -> None:
        rng = self.rng
        kind = rng.choice(["list", "range", "str", "enumerate", "dict", "while"])
        inner = list(scope)
        if kind == "list":
            name = self.fresh("v")
            self.emit(depth, f"for {name} in xs {{")
            inner.append((name, "int"))
        elif kind == "range":
            name = self.fresh("r")
            start, step = rng.randint(0, 2), rng.choice([1, 2, -1])
            stop = start + 3 if step > 0 else start - 3
            self.emit(depth, f"for {name} in range({start}, {stop}, {step}) {{")
            inner.append((name, "int"))
        elif kind == "str":
            strs = [n for n, t in scope if t == "str"]
            source = rng.choice(strs) if strs else '"ab"'
            name = self.fresh("ch")
            self.emit(depth, f"for {name} in {source} {{")
            inner.append((name, "str"))
        elif kind == "enumerate":
            index, word = self.fresh("i"), self.fresh("w")
            self.emit(depth, f"for {index}, {word} in enumerate(ws) {{")
            inner += [(index, "int"), (word, "str")]
        elif kind == "dict":
            key, value = self.fresh("k"), self.fresh("d")
            self.emit(depth, f'for {key}, {value} in {{"key": {rng.randint(1, 9)}}} {{')
            inner += [(key, "str"), (value, "int")]
        else:
            counter = self.fresh("n")
            self.emit(depth, f"{counter}: int = 0")
            self.emit(depth, f"while {counter} < {rng.randint(1, 3)} {{")
            self.emit(depth + 1, f"{counter} += 1")
            scope.append((counter, "int"))
            inner.append((counter, "int"))
        self.block(depth + 1, inner, nesting + 1, True)
        if rng.random() < 0.2:
            self.emit(depth, "} else {")
            self.block(depth + 1, scope, nesting + 1, False)
        self.emit(depth, "}")


def _program(rng: random.Random, first: int, count: int) -> str:
    out = [
        "func nothing() {",
        "}",
        "",
        "func inner(n: int) -> generator[str] {",
        "    for i in range(n % 3) {",
        '        yield f"in{i}"',
        "    }",
        "}",
        "",
        "func collect(g: generator[str]) -> list[str] {",
        "    return [v for v in g]",
        "}",
        "",
    ]
    for index in range(first, first + count):
        body = _Body(rng)
        body.block(1, [("acc", "int")], 0, False)
        for suffix, prologue in (("", []), ("Co", ["    defer nothing()"])):
            out.append(f"func g{index}{suffix}(xs: list[int], ws: list[str]) -> generator[str] {{")
            out.extend(prologue)
            out.append("    acc: int = 0")
            out.extend(body.lines)
            out.append('    yield f"acc {acc}"')
            out.append("}")
            out.append("")
    out.append("func main() {")
    out.append("    xs: list[int] = [1, 2]")
    out.append('    ws: list[str] = ["ab", "c"]')
    for index in range(first, first + count):
        out.append(f"    a{index}: list[str] = collect(g{index}(xs, ws))")
        out.append(f"    b{index}: list[str] = collect(g{index}Co(xs, ws))")
        out.append(f"    if a{index} == b{index} {{")
        out.append(f'        print(f"ok {index} {{len(a{index})}}")')
        out.append("    } else {")
        out.append(f'        print(f"MISMATCH {index}")')
        out.append(f"        print(a{index})")
        out.append(f"        print(b{index})")
        out.append("    }")
    out.append("}")
    return "\n".join(out) + "\n"


def test_state_machine_matches_coroutine_lowering() -> None:
    rng = random.Random(SEED)
    with tempfile.TemporaryDirectory(prefix="lam_gen_fuzz_") as tmp:
        for first in range(0, CASES, PER_PROGRAM):
            count = min(PER_PROGRAM, CASES - first)
            source = _program(rng, first, count)
            lam_file = Path(tmp) / f"gen_fuzz_{first}.lam"
            binary = Path(tmp) / f"gen_fuzz_{first}"
            lam_file.write_text(source, encoding="utf-8")
            build = subprocess.run(
                [sys.executable, str(COMPILER), str(lam_file), "-o", str(binary)],
                cwd=ROOT, capture_output=True, text=True, timeout=300,
            )
            keep = ROOT / "tests" / "tests" / f"_failed_gen_fuzz_{SEED}_{first}.lam"
            if build.returncode != 0:
                keep.write_text(source, encoding="utf-8")
            assert build.returncode == 0, f"build failed (source kept in {keep}):\n{build.stderr[-4000:]}"
            run = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
            if run.returncode != 0 or "MISMATCH" in run.stdout:
                keep.write_text(source, encoding="utf-8")
            assert run.returncode == 0, f"run failed (source kept in {keep}):\n{run.stderr[-4000:]}"
            assert "MISMATCH" not in run.stdout, f"outputs differ (source kept in {keep}):\n{run.stdout[-4000:]}"
            oks = [line for line in run.stdout.splitlines() if line.startswith("ok ")]
            assert len(oks) == count, run.stdout
    print(f"PASS: {CASES} random generator bodies (seed {SEED}): state machine == coroutine lowering")


if __name__ == "__main__":
    test_state_machine_matches_coroutine_lowering()
