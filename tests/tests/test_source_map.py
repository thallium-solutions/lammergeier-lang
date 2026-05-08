#!/usr/bin/env python3
"""Tests for source-map primitives."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.source_map import SourceMap, SourcePoint  # noqa: E402


def test_identity_map() -> None:
    smap = SourceMap.identity(3)
    assert smap.generated_to_original(1, 1) == SourcePoint(1, 1)
    assert smap.generated_to_original(3, 5) == SourcePoint(3, 5)
    print("PASS: identity source map preserves positions")


def test_deleted_lines_map() -> None:
    smap = SourceMap.delete_lines(4, {2})
    assert smap.generated_to_original(1, 3) == SourcePoint(1, 3)
    assert smap.generated_to_original(2, 3) == SourcePoint(3, 3)
    assert smap.generated_to_original(3, 3) == SourcePoint(4, 3)
    print("PASS: deleted-line source map skips removed lines")


def test_eof_maps_to_last_original_line() -> None:
    smap = SourceMap.from_line_mapping([2, 4])
    assert smap.generated_to_original(99, 7) == SourcePoint(4, 7)
    print("PASS: out-of-range generated positions map to original EOF")


def main() -> int:
    tests = [
        test_identity_map,
        test_deleted_lines_map,
        test_eof_maps_to_last_original_line,
    ]
    for test in tests:
        test()
    print(f"\nSource-map results: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())

