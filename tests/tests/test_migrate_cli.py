#!/usr/bin/env python3
"""``lamc migrate`` end to end: make, up, status, down on SQLite.

The runner compiles a small Lam program per invocation; it used to be written
against the pre-``Result`` lamdb/lammigrate API and failed the semantic check
(``cannot assign `Result[Db]` to `Db` variable `db```), so ``up``/``down``/
``status`` never ran. A failing database now ends with exit code 1 and the
error, not a panic.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LAMC = [sys.executable, str(ROOT / "lamc")]


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*LAMC, "migrate", *args], cwd=cwd, capture_output=True, text=True, timeout=600, check=False,
    )


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        migrations = work / "migrations"
        made = run("make", "create widgets", "--dir", str(migrations), cwd=work)
        files = sorted(migrations.glob("*_create_widgets.sql"))
        if made.returncode != 0 or len(files) != 1:
            failures.append(f"make: {made.returncode} {made.stdout}{made.stderr}")
        else:
            files[0].write_text(
                "-- +lam up\nCREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY, label TEXT);\n"
                "-- +lam down\nDROP TABLE IF EXISTS widgets;\n",
                encoding="utf-8",
            )
        db = str(work / "app.db")
        common = ["--dir", str(migrations), "--driver", "sqlite", "--dsn", db]
        up = run("up", *common, cwd=work)
        if up.returncode != 0 or "applied-count: 1" not in up.stdout:
            failures.append(f"up: {up.returncode}\n{up.stdout}\n{up.stderr}")
        again = run("up", *common, cwd=work)
        if again.returncode != 0 or "applied-count: 0" not in again.stdout:
            failures.append(f"up again: {again.returncode}\n{again.stdout}\n{again.stderr}")
        status = run("status", *common, cwd=work)
        if status.returncode != 0 or "ran: 1" not in status.stdout or "pending: 0" not in status.stdout:
            failures.append(f"status: {status.returncode}\n{status.stdout}\n{status.stderr}")
        down = run("down", *common, cwd=work)
        if down.returncode != 0 or "rolled-back-count: 1" not in down.stdout:
            failures.append(f"down: {down.returncode}\n{down.stdout}\n{down.stderr}")
        bad = run("up", "--dir", str(migrations), "--driver", "postgres", "--dsn",
                  "postgres://nobody:x@127.0.0.1:1/none?sslmode=disable&connect_timeout=2", cwd=work)
        if bad.returncode != 1 or "error:" not in bad.stdout + bad.stderr or "panic" in bad.stderr:
            failures.append(f"unreachable database: {bad.returncode}\n{bad.stdout}\n{bad.stderr}")
    if failures:
        print("FAIL: lamc migrate\n" + "\n".join(failures))
        return 1
    print("PASS: lamc migrate make/up/status/down")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
