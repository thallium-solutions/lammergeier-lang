#!/usr/bin/env python3
"""The element-name tables of lib/lammarkup.lam agree: the renderers ask
``lamHTMLIsVoid`` / ``lamHTMLIsRawText`` (switches, for speed) what the
parser and the sanitizer ask the ``lamHTMLVoid`` / ``lamHTMLRawText`` maps,
so both spellings must list the same names."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SOURCE = (ROOT / "lib" / "lammarkup.lam").read_text(encoding="utf-8")


def _map_keys(name: str) -> set[str]:
    match = re.search(rf"var {name} = map\[string\]bool\{{(.*?)\}}", SOURCE, re.S)
    assert match, name
    return set(re.findall(r'"([^"]+)": true', match.group(1)))


def _function_names(name: str) -> set[str]:
    match = re.search(rf"func {name}\(name string\) bool \{{(.*?)\n    \}}", SOURCE, re.S)
    if match is None:
        match = re.search(rf"func {name}\(name string\) bool \{{(.*?)\}}", SOURCE)
    assert match, name
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_switches_match_the_maps() -> None:
    assert _function_names("lamHTMLIsVoid") == _map_keys("lamHTMLVoid")
    assert _function_names("lamHTMLIsRawText") == _map_keys("lamHTMLRawText")
    print("PASS: the markup renderers' element switches match the maps")


if __name__ == "__main__":
    test_switches_match_the_maps()
    sys.exit(0)
