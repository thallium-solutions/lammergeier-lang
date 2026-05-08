"""Source position mapping helpers.

The first implementation is intentionally line-oriented. It is enough to
give preprocessing a stable API now while later work tightens mappings for
multi-line rewrites and token-level edits.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SourcePoint:
    line: int
    col: int


@dataclass(frozen=True)
class SourceMap:
    """Map generated parser positions back to original source positions.

    ``line_map`` is 1-indexed by generated line number. The value is the
    original line number. Columns currently pass through unchanged.
    """

    line_map: tuple[int, ...]

    @classmethod
    def identity(cls, line_count: int) -> "SourceMap":
        return cls(tuple(range(1, max(0, line_count) + 1)))

    @classmethod
    def from_line_mapping(cls, line_map: list[int] | tuple[int, ...]) -> "SourceMap":
        return cls(tuple(max(1, int(line)) for line in line_map))

    @classmethod
    def delete_lines(cls, total_lines: int, deleted_lines: set[int]) -> "SourceMap":
        """Return a map for a transform that deleted whole lines.

        ``deleted_lines`` uses original 1-indexed line numbers.
        """
        return cls(tuple(
            line for line in range(1, max(0, total_lines) + 1)
            if line not in deleted_lines
        ))

    def generated_to_original(self, line: int, col: int) -> SourcePoint:
        safe_line = max(1, int(line))
        safe_col = max(1, int(col))
        if not self.line_map:
            return SourcePoint(safe_line, safe_col)
        if safe_line <= len(self.line_map):
            return SourcePoint(self.line_map[safe_line - 1], safe_col)
        # Positions beyond the mapped text are best treated as EOF on
        # the last original line.
        return SourcePoint(self.line_map[-1], safe_col)

