#!/usr/bin/env python3
"""``lamc build --timings`` prints one ``LAMC_TIMINGS`` JSON line with the
compile phases (and nothing changes without the flag)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "hello.lam"
        src.write_text('func main() {\n    print("hi")\n}\n')
        out = Path(tmp) / "hello"
        proc = subprocess.run([*LAMC, str(src), "-o", str(out), "--timings", "--no-cache"], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        m = re.search(r"^LAMC_TIMINGS (\{.*\})$", proc.stderr, re.M)
        assert m, proc.stderr
        phases = json.loads(m.group(1))["phases_ms"]
        for phase in ("preprocess", "parse", "semantic", "transpile", "go_build", "total"):
            assert phase in phases, phases
        assert phases["total"] >= phases["go_build"], phases
        plain = subprocess.run([*LAMC, str(src), "-o", str(out)], capture_output=True, text=True)
        assert "LAMC_TIMINGS" not in plain.stderr, plain.stderr
        front = subprocess.run([*LAMC, str(src), "--transpile-only", "--timings"], capture_output=True, text=True)
        assert "go_build" not in front.stderr, front.stderr
    print("timings: 3 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
