"""Internal compiler errors: a crash inside ``lamc`` becomes a clean report.

Whatever the input (a fuzzed or truncated source, random bytes, very deep
nesting, a huge literal), the compiler must end with a Lam diagnostic or an
``internal compiler error`` report and a non-zero exit status, never a raw
Python traceback. ``guard`` wraps the ``lamc`` entry point:

* a ``RecursionError`` is the compiler's nesting limit, reported as a Lam
  error at the deepest source line being processed;
* a ``UnicodeDecodeError`` while reading a source file is a Lam error naming
  the file and the offending byte;
* any other exception is an ``internal compiler error`` with the Lam
  location being processed (found in the crashing frames), the phase and a
  "please report" hint. ``LAMC_TRACEBACK=1`` prints the Python traceback too.

``generated_go_failed`` formats a Go build failure of generated (not raw
``go!``) code the same way: the checker accepted the program, so a Go error
is a compiler bug, not a user mistake.
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

REPORT_URL = "https://github.com/thallium-solutions/lammergeier-lang/issues"

# Exit status of an internal compiler error (``EX_SOFTWARE``).
ICE_EXIT = 70


def _lam_location(tb) -> tuple[str, int]:
    """The innermost Lam source file and line the crashing frames were
    processing: a lark ``Tree`` / ``Token`` local gives the line, a
    ``.lam`` path local the file."""
    path, line = "", 0
    frames = []
    while tb is not None:
        frames.append(tb.tb_frame)
        tb = tb.tb_next
    for frame in reversed(frames):
        try:
            values = list(frame.f_locals.values())
        except Exception:  # noqa: BLE001 - best effort
            continue
        for value in values:
            if not line:
                line = _node_line(value)
            if not path:
                path = _lam_path(value)
        if line and path:
            break
    return path, line


def _node_line(value) -> int:
    meta = getattr(value, "meta", None)
    if meta is not None and getattr(meta, "empty", True) is False:
        return int(getattr(meta, "line", 0) or 0)
    if type(value).__name__ == "Token":
        return int(getattr(value, "line", 0) or 0)
    return 0


def _lam_path(value) -> str:
    if isinstance(value, (str, Path)):
        text = str(value)
        if text.endswith(".lam") and "\n" not in text and len(text) < 4096:
            return text
    return ""


def _source_line(path: str, line: int) -> str:
    if not path or line <= 0:
        return ""
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    text = lines[line - 1] if line <= len(lines) else ""
    return text if len(text) <= 160 else text[:157] + "..."


def _where(path: str, line: int) -> str:
    if path and line:
        return f"{path}:{line}"
    return path or "<input>"


def report(exc: BaseException, argv_source: str = "") -> int:
    """Print the clean report of ``exc``; return the exit status."""
    tb = exc.__traceback__
    path, line = _lam_location(tb)
    path = path or argv_source
    if isinstance(exc, RecursionError):
        print(f"error: {_where(path, line)}: the program nests too deeply for lamc", file=sys.stderr)
        snippet = _source_line(path, line)
        if snippet:
            print(f"    >>> {line:4d} | {snippet}", file=sys.stderr)
        print("  help: split the expression or block (extract a function or a local variable); "
              "lamc processes nesting recursively and stops at a fixed depth", file=sys.stderr)
        _maybe_traceback(exc)
        return 1
    if isinstance(exc, UnicodeDecodeError):
        bad = exc.object[exc.start:exc.start + 1] if isinstance(exc.object, (bytes, bytearray)) else b""
        print(f"error: {path or '<input>'}: the file is not valid UTF-8 "
              f"(byte 0x{bad.hex() or '??'} at offset {exc.start})", file=sys.stderr)
        print("  help: Lam sources are UTF-8 text; re-save the file as UTF-8", file=sys.stderr)
        _maybe_traceback(exc)
        return 1
    if isinstance(exc, MemoryError):
        print(f"error: {_where(path, line)}: lamc ran out of memory", file=sys.stderr)
        return 1
    kind = type(exc).__name__
    message = str(exc).splitlines()[0] if str(exc) else ""
    print(f"internal compiler error: {kind}{': ' + message if message else ''}", file=sys.stderr)
    print(f"  --> {_where(path, line)}", file=sys.stderr)
    snippet = _source_line(path, line)
    if snippet:
        print(f"    >>> {line:4d} | {snippet}", file=sys.stderr)
    crash_site = _crash_site(tb)
    if crash_site:
        print(f"  in {crash_site}", file=sys.stderr)
    print("  this is a bug in lamc, not in your program: please report it at "
          f"{REPORT_URL} with the source (set LAMC_TRACEBACK=1 for the compiler traceback)", file=sys.stderr)
    _maybe_traceback(exc)
    return ICE_EXIT


def _crash_site(tb) -> str:
    last = None
    for frame, lineno in traceback.walk_tb(tb):
        if "compiler" in frame.f_code.co_filename:
            last = (frame.f_code.co_filename, lineno, frame.f_code.co_name)
    if last is None:
        return ""
    return f"{Path(last[0]).name}:{last[1]} ({last[2]})"


def _maybe_traceback(exc: BaseException) -> None:
    if os.environ.get("LAMC_TRACEBACK"):
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)


def guard(entry, argv_source: str = ""):
    """Run ``entry()``; a crash becomes a clean report and exit status."""
    try:
        return entry()
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("lamc: interrupted", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(1)
    except BaseException as exc:  # noqa: BLE001 - the crash guard
        try:
            status = report(exc, argv_source)
        except BaseException:  # noqa: BLE001 - never crash while reporting a crash
            print(f"internal compiler error: {type(exc).__name__}", file=sys.stderr)
            status = ICE_EXIT
        sys.exit(status)


def generated_go_failed(source_path: str) -> None:
    """Header of a Go build failure of generated code (a compiler bug)."""
    print(f"internal compiler error: the generated Go did not build ({source_path})", file=sys.stderr)


def generated_go_hint() -> None:
    print("  the checker accepted this program, so this is a bug in lamc: please report it at "
          f"{REPORT_URL} with the source (`lamc --emit-go` prints the generated Go)", file=sys.stderr)
