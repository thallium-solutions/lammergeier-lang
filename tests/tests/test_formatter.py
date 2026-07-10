#!/usr/bin/env python3

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.formatter import format_lam_source  # noqa: E402


MESSY_SOURCE = """func main(){
value:int=1
items:list[int]=[]
lookup:dict[str,int]={}
if value>0{
print("x:y", value)# inline
}
}
"""

FORMATTED_SOURCE = """func main() {
    value: int = 1
    items: list[int] = []
    lookup: dict[str, int] = {}
    if value > 0 {
        print("x:y", value)  # inline
    }
}
"""

MESSY_GO_SOURCE = """go! { import (
"strings"
"fmt"
) }

func call(*items:str)->str{
raw:any=go!(strings.NewReader("x"))
_ = raw
go! {
fmt.Println(fmt.Sprintf("value:%d",42))
if true {
fmt.Println("ok")
}
}
return ",".join(items)
}
"""

FORMATTED_GO_SOURCE = """go! {
    import (
        "fmt"
        "strings"
    )
}

func call(*items: str) -> str {
    raw: any = go!(strings.NewReader("x"))
    _ = raw
    go! {
        fmt.Println(fmt.Sprintf("value:%d", 42))
        if true {
            fmt.Println("ok")
        }
    }
    return ",".join(items)
}
"""


def test_formatter_snapshot() -> None:
    result = format_lam_source(MESSY_SOURCE)
    assert result.changed
    assert result.text == FORMATTED_SOURCE, result.text
    print("PASS: formatter produces expected snapshot")


def test_formatter_is_idempotent() -> None:
    first = format_lam_source(MESSY_SOURCE).text
    second = format_lam_source(first)
    assert second.text == first
    assert not second.changed
    print("PASS: formatter is idempotent")


def test_formatter_gofmt_and_inline_go() -> None:
    result = format_lam_source(MESSY_GO_SOURCE)
    assert result.changed
    assert result.text == FORMATTED_GO_SOURCE, result.text
    second = format_lam_source(result.text)
    assert second.text == result.text
    assert not second.changed
    print("PASS: formatter runs gofmt and preserves inline go")


def test_lamc_fmt_stdout_and_check() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.lam"
        path.write_text(MESSY_SOURCE, encoding="utf-8")
        stdout = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(path), "--stdout"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert stdout.returncode == 0, stdout.stderr
        assert stdout.stdout == FORMATTED_SOURCE
        check_bad = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(path), "--check"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert check_bad.returncode == 1
        write = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(path)],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert write.returncode == 0, write.stderr
        assert path.read_text(encoding="utf-8") == FORMATTED_SOURCE
        check_good = subprocess.run(
            [sys.executable, str(ROOT / "lamc"), "fmt", str(path), "--check"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        assert check_good.returncode == 0, check_good.stderr
    print("PASS: lamc fmt supports stdout, check, and file write modes")


def main() -> int:
    tests = [
        test_formatter_snapshot,
        test_formatter_is_idempotent,
        test_formatter_gofmt_and_inline_go,
        test_lamc_fmt_stdout_and_check,
    ]
    for test in tests:
        test()
    print(f"\nformatter results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
