"""Breaking-change detector for Lammergeier libraries.

Given two source trees (``old`` and ``new`` versions of the same
library) we extract the **public API surface** from each and report
every change that is likely to break a downstream consumer. The
install command uses the result to refuse to call a release "patch"
when it actually removes a public symbol — SemVer discipline we
enforce by code instead of trusting the publisher.

Surface extraction
------------------

A regex pass over the ``.lam`` files — not the full Lark grammar —
is deliberate: we don't care about method bodies here and running
the whole transpiler per install call would bloat the startup
budget. The declaration shapes we scan are unambiguous at the
top-level.

Public surface = ``func``, ``class``, ``interface`` declarations
NOT starting with ``_`` and NOT tagged ``private``. Class body
scans pick up methods and bare-annotation fields (``name: type``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple


# ── Types ────────────────────────────────────────────────────

Severity = str  # "breaking" | "feature" | "patch"


@dataclass
class FuncSig:
    """Minimal signature view. ``params`` preserves order so a
    reorder of required parameters counts as breaking; ``defaults``
    flags which indices have a default value, so adding a NEW
    required parameter is breaking while adding one with a default
    is a feature."""
    params:      List[Tuple[str, str]] = field(default_factory=list)
    defaults:    List[bool] = field(default_factory=list)
    rest:        bool = False
    kwargs:      bool = False
    return_type: str = ""


@dataclass
class ClassShape:
    fields:  Dict[str, str] = field(default_factory=dict)
    methods: Dict[str, FuncSig] = field(default_factory=dict)
    bases:   List[str] = field(default_factory=list)


@dataclass
class ApiSurface:
    funcs:   Dict[str, FuncSig] = field(default_factory=dict)
    classes: Dict[str, ClassShape] = field(default_factory=dict)


@dataclass
class Change:
    """One API-surface delta. ``path`` is a dotted symbol
    (``lib.Class.method`` / ``lib.func``) for easy grouping."""
    severity: Severity
    kind:     str
    path:     str
    message:  str

    def __str__(self) -> str:
        tag = {"breaking": "BREAK",
               "feature":  "FEAT ",
               "patch":    "PATCH"}[self.severity]
        return f"{tag} {self.path}: {self.message}"


# ── Declaration regexes ──────────────────────────────────────

_RE_FUNC = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?:(?P<private>private)\s+)?"
    r"(?:static\s+)?(?:async\s+)?"
    r"func\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\[[^\]]*\])?"
    r"\s*\(\s*(?P<params>[^)]*)\)"
    r"(?:\s*->\s*(?P<ret>[^:\{#]+?))?"
    # Body starts after ``{`` or ``:`` — we don't care about what
    # comes next on the same line (single-line methods like
    # ``func x() -> int { return 1 }`` are legal and common).
    r"\s*[{:]"
)

_RE_CLASS = re.compile(
    r"^(?P<indent>[ \t]*)class\s+(?P<name>[A-Za-z_]\w*)"
    r"(?:\[[^\]]*\])?"
    r"(?:\s*\(\s*(?P<bases>[^)]*)\))?"
    r"\s*[{:]"
)

_RE_INTERFACE = re.compile(
    r"^(?P<indent>[ \t]*)interface\s+(?P<name>[A-Za-z_]\w*)"
)

_RE_FIELD = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?P<private>private)\s+)?(?:static\s+)?"
    r"(?P<name>[A-Za-z_]\w*)\s*:\s*"
    r"(?P<ty>[^=#]+?)\s*(?:=.*)?$"
)


def _is_public(name: str, flagged_private: bool = False) -> bool:
    """Underscore-prefix + ``private`` keyword both mean non-public."""
    return not (flagged_private or name.startswith("_"))


def _parse_params(raw: str):
    """Break a ``typed_parameters`` string into
    ``(params, defaults, *args, **kwargs)``. Drops the receiver
    (``self`` / ``self: Foo``) at index 0 so callers only see the
    externally observable parameters."""
    if not raw.strip():
        return [], [], False, False

    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    for ch in raw:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf).strip()); buf = []
        else:
            buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)

    params, defaults = [], []
    rest = kwargs = False
    for idx, p in enumerate(parts):
        if p.startswith("**"):
            kwargs = True; continue
        if p.startswith("*"):
            rest = True; continue
        if idx == 0 and (p == "self" or p.startswith("self:")
                         or p.startswith("self ")):
            continue
        has_default = False
        if "=" in p:
            p, _ = p.split("=", 1); has_default = True
        if ":" in p:
            name, ty = p.split(":", 1)
            params.append((name.strip(), ty.strip()))
        else:
            params.append((p.strip(), "any"))
        defaults.append(has_default)
    return params, defaults, rest, kwargs


def _skip_block(lines: List[str], header_idx: int) -> int:
    """Return the index of the first line NOT part of the block
    whose header sits at ``lines[header_idx]``. Handles both
    brace-based (``{ ... }``) and colon-indented shapes."""
    header = lines[header_idx]
    depth = header.count("{") - header.count("}")
    if depth > 0:
        j = header_idx + 1
        while j < len(lines) and depth > 0:
            depth += lines[j].count("{") - lines[j].count("}")
            j += 1
        return j
    base_indent = len(header) - len(header.lstrip())
    j = header_idx + 1
    while j < len(lines):
        ln = lines[j]
        if not ln.strip():
            j += 1; continue
        ind = len(ln) - len(ln.lstrip())
        if ind <= base_indent:
            break
        j += 1
    return j


def extract_surface(src: str) -> ApiSurface:
    """Extract the public API surface from a Lam source string."""
    surface = ApiSurface()
    lines = src.splitlines()

    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.rstrip()
        if not stripped.strip() or stripped.lstrip().startswith("#"):
            i += 1; continue

        m = _RE_FUNC.match(stripped)
        if m and len(m.group("indent")) == 0:
            fn_name = m.group("name")
            if _is_public(fn_name, bool(m.group("private"))):
                params, defaults, rest, kwargs = _parse_params(m.group("params"))
                surface.funcs[fn_name] = FuncSig(
                    params=params, defaults=defaults,
                    rest=rest, kwargs=kwargs,
                    return_type=(m.group("ret") or "").strip(),
                )
            i += 1; continue

        m = _RE_CLASS.match(stripped)
        if m and len(m.group("indent")) == 0:
            cls_name = m.group("name")
            body_end = _skip_block(lines, i)
            if _is_public(cls_name):
                shape = ClassShape(bases=[
                    b.strip() for b in (m.group("bases") or "").split(",")
                    if b.strip()
                ])
                for j in range(i + 1, body_end):
                    body_line = lines[j]
                    if not body_line.strip() or body_line.lstrip().startswith("#"):
                        continue
                    fm = _RE_FUNC.match(body_line)
                    if fm and len(fm.group("indent")) > 0:
                        m_name = fm.group("name")
                        if _is_public(m_name, bool(fm.group("private"))):
                            p, d, r, kw = _parse_params(fm.group("params"))
                            shape.methods[m_name] = FuncSig(
                                params=p, defaults=d, rest=r, kwargs=kw,
                                return_type=(fm.group("ret") or "").strip(),
                            )
                        continue
                    fld = _RE_FIELD.match(body_line)
                    if fld and len(fld.group("indent")) > 0:
                        fld_name = fld.group("name")
                        if _is_public(fld_name, bool(fld.group("private"))):
                            shape.fields[fld_name] = fld.group("ty").strip()
                surface.classes[cls_name] = shape
            i = body_end; continue

        m = _RE_INTERFACE.match(stripped)
        if m and len(m.group("indent")) == 0:
            name = m.group("name")
            body_end = _skip_block(lines, i)
            if _is_public(name):
                shape = ClassShape()
                for j in range(i + 1, body_end):
                    body_line = lines[j]
                    fm = _RE_FUNC.match(body_line)
                    if fm and len(fm.group("indent")) > 0:
                        p, d, r, kw = _parse_params(fm.group("params"))
                        shape.methods[fm.group("name")] = FuncSig(
                            params=p, defaults=d, rest=r, kwargs=kw,
                            return_type=(fm.group("ret") or "").strip(),
                        )
                surface.classes[name] = shape
            i = body_end; continue

        i += 1

    return surface


def surface_from_path(path: Path) -> ApiSurface:
    """Aggregate the public API of every ``.lam`` / ``.tpy`` file
    under ``path``. Hidden dirs + ``tests`` / ``benchmarks`` /
    ``extlibs`` / ``build`` are skipped because they're not
    published surface."""
    surface = ApiSurface()
    base = Path(path)
    if base.is_file():
        _merge(surface, extract_surface(base.read_text(encoding="utf-8")))
        return surface

    skip_dirs = {"tests", "benchmarks", "extlibs", ".git", "build"}
    for p in sorted(base.rglob("*")):
        if p.is_dir():
            continue
        if any(part in skip_dirs or part.startswith(".") for part in p.parts):
            continue
        if p.suffix not in (".lam", ".tpy"):
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        _merge(surface, extract_surface(text))
    return surface


def _merge(dst: ApiSurface, src: ApiSurface) -> None:
    for k, v in src.funcs.items():
        dst.funcs[k] = v
    for k, v in src.classes.items():
        dst.classes[k] = v


# ── Diff ─────────────────────────────────────────────────────

def compare(old: ApiSurface, new: ApiSurface) -> List[Change]:
    """Return every ``Change`` required to explain the ``old → new``
    delta. Classes first, then free funcs; within each, breaking
    before feature."""
    out: List[Change] = []

    for name, old_cls in old.classes.items():
        if name not in new.classes:
            out.append(Change(
                "breaking", "removed_class", name,
                f"class `{name}` removed"))
            continue
        out.extend(_diff_class(name, old_cls, new.classes[name]))
    for name in new.classes.keys() - old.classes.keys():
        out.append(Change(
            "feature", "added_class", name,
            f"new class `{name}`"))

    for name, old_fn in old.funcs.items():
        if name not in new.funcs:
            out.append(Change(
                "breaking", "removed_func", name,
                f"function `{name}` removed"))
            continue
        out.extend(_diff_func(name, old_fn, new.funcs[name]))
    for name in new.funcs.keys() - old.funcs.keys():
        out.append(Change(
            "feature", "added_func", name,
            f"new function `{name}`"))
    return out


def _diff_class(cls: str, old: ClassShape, new: ClassShape) -> List[Change]:
    out: List[Change] = []
    if old.bases != new.bases:
        out.append(Change(
            "breaking", "changed_bases", cls,
            f"base list changed: {old.bases} → {new.bases}"))
    for f, ty in old.fields.items():
        if f not in new.fields:
            out.append(Change(
                "breaking", "removed_field", f"{cls}.{f}",
                f"field `{f}` removed"))
        elif new.fields[f] != ty:
            out.append(Change(
                "breaking", "changed_field", f"{cls}.{f}",
                f"field type changed: {ty} → {new.fields[f]}"))
    for f in new.fields.keys() - old.fields.keys():
        out.append(Change(
            "feature", "added_field", f"{cls}.{f}",
            f"new field `{f}`"))
    for m, sig in old.methods.items():
        if m not in new.methods:
            out.append(Change(
                "breaking", "removed_method", f"{cls}.{m}",
                f"method `{m}` removed"))
            continue
        out.extend(_diff_func(f"{cls}.{m}", sig, new.methods[m]))
    for m in new.methods.keys() - old.methods.keys():
        out.append(Change(
            "feature", "added_method", f"{cls}.{m}",
            f"new method `{m}`"))
    return out


def _diff_func(path: str, old: FuncSig, new: FuncSig) -> List[Change]:
    out: List[Change] = []
    if old.return_type != new.return_type:
        out.append(Change(
            "breaking", "changed_return", path,
            f"return type: {old.return_type or 'void'} "
            f"→ {new.return_type or 'void'}"))

    old_req = [p for p, d in zip(old.params, old.defaults) if not d]
    new_req = [p for p, d in zip(new.params, new.defaults) if not d]

    if len(new_req) > len(old_req):
        extras = [p for p in new_req if p not in old_req]
        out.append(Change(
            "breaking", "param_added_required", path,
            f"new required parameter(s): "
            f"{', '.join(n for n, _ in extras) or '<renamed>'}"))
    elif len(new_req) < len(old_req):
        dropped = [p for p in old_req if p not in new_req]
        out.append(Change(
            "breaking", "param_removed", path,
            f"required parameter(s) removed: "
            f"{', '.join(n for n, _ in dropped) or '<renamed>'}"))
    elif old_req != new_req:
        out.append(Change(
            "breaking", "param_changed", path,
            f"required parameter signature changed: "
            f"{old_req} → {new_req}"))

    for i, (op, oty) in enumerate(old.params):
        if i >= len(new.params):
            break
        np, nty = new.params[i]
        if op == np and oty != nty:
            out.append(Change(
                "breaking", "param_type_changed", f"{path}({op})",
                f"parameter `{op}` type: {oty} → {nty}"))

    if len(new.params) > len(old.params):
        added = new.params[len(old.params):]
        new_defaults = new.defaults[len(old.params):]
        if all(new_defaults):
            out.append(Change(
                "feature", "param_added_optional", path,
                f"new optional parameter(s): "
                f"{', '.join(n for n, _ in added)}"))

    if old.rest != new.rest:
        out.append(Change(
            "breaking" if old.rest and not new.rest else "feature",
            "rest_param_changed", path,
            f"*args flag: {old.rest} → {new.rest}"))
    if old.kwargs != new.kwargs:
        out.append(Change(
            "breaking" if old.kwargs and not new.kwargs else "feature",
            "kwargs_param_changed", path,
            f"**kwargs flag: {old.kwargs} → {new.kwargs}"))
    return out


# ── SemVer severity helpers ──────────────────────────────────

def worst_severity(changes: List[Change]) -> Severity:
    """Collapse a changelog down to the single worst severity
    present — ``"breaking"`` > ``"feature"`` > ``"patch"``."""
    ranks = {"breaking": 2, "feature": 1, "patch": 0}
    best = "patch"
    for c in changes:
        if ranks[c.severity] > ranks[best]:
            best = c.severity
    return best


def expected_bump(old_ver: str, new_ver: str) -> Severity:
    """Map the numeric delta of two SemVer strings onto one of
    ``"breaking"`` / ``"feature"`` / ``"patch"``. Helper for the
    ``lamc install`` gate that rejects mis-labelled releases."""
    def tup(v: str):
        parts = v.split("-", 1)[0].split(".")
        while len(parts) < 3:
            parts.append("0")
        try:
            return tuple(int(p) for p in parts[:3])
        except ValueError:
            return (0, 0, 0)
    o, n = tup(old_ver), tup(new_ver)
    if n[0] > o[0]:
        return "breaking"
    if n[1] > o[1]:
        return "feature"
    return "patch"
