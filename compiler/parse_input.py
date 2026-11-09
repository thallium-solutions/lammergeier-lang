"""The result of the parse preprocessing (``preprocess_for_parse`` in
``compiler/lammergeier.py``).

Defined in a module of its own: the build caches it pickled
(``compiler/frontend.py``), and a class defined in ``lammergeier.py`` would
be recorded as ``__main__.ParsePreprocessResult`` when that file runs as a
script, which another ``lamc`` process could not load.
"""
from __future__ import annotations

from dataclasses import dataclass

from compiler.source_map import SourceMap


@dataclass(frozen=True)
class ParsePreprocessResult:
    source: str
    go_blocks: dict[str, str]
    source_map: SourceMap
    diagnostics: tuple[tuple[int, int, str], ...] = ()
    # Errors that stop parsing (an unterminated markup literal), reported
    # as syntax errors: (line, column, message).
    syntax_errors: tuple[tuple[int, int, str], ...] = ()
