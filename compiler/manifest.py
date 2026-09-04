"""``lamlib.toml`` manifest parser — minimal, dependency-free.

Lam ships one Python runtime dependency (``lark``) and we want to
keep it that way, so this module parses the small TOML subset
``lamlib.toml`` actually uses instead of pulling in ``tomli`` /
``tomllib`` (``tomllib`` is 3.11+; the project supports 3.10+).

Supported TOML surface
----------------------
- ``# comment`` lines (and end-of-line ``# ...`` suffixes outside
  strings).
- Sections: ``[library]``, ``[compatibility]``, ``[dependencies]``,
  ``[scripts]``. Unknown sections are preserved under ``.extras``.
- String values: ``key = "value"`` (double-quoted only, ``\\"`` and
  ``\\\\`` escapes supported; single-quoted literal strings also
  accepted to match the TOML spec).
- Integer / float / boolean scalars.
- Arrays of strings: ``authors = ["a", "b"]``.
- Inline tables (one level deep): ``dep = { path = "../x" }``.
- Nested table headers (``[dependencies.lamhttp]``) — required
  for the ``lamc = "^0.4"`` form vs ``lamc = { path = ... }``.

Out of scope (would raise ``ManifestError`` on use)
---------------------------------------------------
- Arrays of tables (``[[x]]``) — libraries don't need them.
- Multiline basic / literal strings (``\"\"\"...\"\"\"``).
- Date / time values.
- Dotted keys inside a section body (``a.b.c = 1``).

If you hit any of these we'll error clearly and the user can file a
bug; we'd rather extend the parser on demand than ship a sprawling
re-implementation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ── Errors ───────────────────────────────────────────────────

class ManifestError(Exception):
    """A ``lamlib.toml`` is malformed or violates a validation rule.

    Carries a 1-based line number (``lineno``) when the problem is
    localisable, so callers can produce ``file:line: message`` output
    in the same style as the rest of the compiler.
    """

    def __init__(self, message: str, lineno: Optional[int] = None):
        super().__init__(message)
        self.lineno = lineno


# ── Low-level TOML lexer ─────────────────────────────────────

# Structural regexes used by ``_parse_toml`` below. Kept module-level
# so the compiled patterns survive across calls.
_RE_SECTION = re.compile(r"^\[([^\[\]]+)\]\s*(?:#.*)?$")
# Keys are either bare (``[A-Za-z0-9_@/\-]+``) or double-quoted —
# quoted form lets manifests mention scoped names like
# ``"@bob/lamutil" = "1.x"`` even when the bare regex would also
# match, plus it's required for any key with characters outside
# the bare set.
_RE_KEY = re.compile(
    r'^(?:"([^"\\]*(?:\\.[^"\\]*)*)"|([A-Za-z0-9_@/\-]+))\s*=\s*(.*?)\s*$')
_RE_BLANK = re.compile(r"^\s*(?:#.*)?$")


def _key_value(m: "re.Match") -> Tuple[str, str]:
    """Pull the (key, value) pair from a ``_RE_KEY`` match. Quoted
    keys are unescaped; bare keys pass through verbatim."""
    quoted, bare, value = m.group(1), m.group(2), m.group(3)
    if quoted is not None:
        # Same escapes as basic strings — just unescape ``\\`` and ``\"``.
        key = (quoted.replace('\\\\', '\\').replace('\\"', '"'))
    else:
        key = bare
    return key, value


def _strip_line_comment(s: str) -> str:
    """Return ``s`` with any trailing ``# comment`` stripped.

    Naive but correct for the TOML surface we accept: we walk the
    string tracking whether we're inside a quoted value. Escape
    handling matches TOML's basic string rules (``\\\\`` and ``\\\"``
    only — we don't bother with unicode escapes because the manifest
    values we care about are plain ascii).
    """
    out: list[str] = []
    i, n = 0, len(s)
    quote: Optional[str] = None
    while i < n:
        c = s[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(s[i + 1]); i += 2; continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ('"', "'"):
            quote = c
            out.append(c); i += 1
            continue
        if c == "#":
            break
        out.append(c)
        i += 1
    return "".join(out).rstrip()


def _parse_scalar(raw: str, lineno: int) -> Any:
    """Parse a RHS value into a Python object.

    Accepts strings (basic / literal), integers, floats, booleans,
    arrays of scalars, and single-level inline tables. Anything else
    raises ``ManifestError``.
    """
    raw = raw.strip()
    if not raw:
        raise ManifestError("empty value", lineno)

    # Strings ──────────────────────────────────────────────────
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return _unescape_basic(raw[1:-1], lineno)
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        # Literal string — no escape processing per TOML spec.
        return raw[1:-1]

    # Booleans
    if raw == "true":  return True
    if raw == "false": return False

    # Arrays
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise ManifestError("unterminated array", lineno)
        return _parse_array(raw, lineno)

    # Inline tables
    if raw.startswith("{"):
        if not raw.endswith("}"):
            raise ManifestError("unterminated inline table", lineno)
        return _parse_inline_table(raw, lineno)

    # Numbers
    if re.fullmatch(r"[+-]?\d+", raw):
        return int(raw)
    if re.fullmatch(r"[+-]?\d+\.\d+", raw):
        return float(raw)

    raise ManifestError(f"cannot parse value: {raw!r}", lineno)


def _unescape_basic(s: str, lineno: int) -> str:
    """Translate ``\\\\`` / ``\\\"`` / ``\\n`` / ``\\t`` in a basic TOML
    string. Reject the rest so we don't silently mis-parse an exotic
    escape the caller expected to round-trip."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            if i + 1 >= n:
                raise ManifestError("trailing backslash in string", lineno)
            nxt = s[i + 1]
            mapped = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r"}.get(nxt)
            if mapped is None:
                raise ManifestError(f"unsupported escape \\{nxt}", lineno)
            out.append(mapped); i += 2; continue
        out.append(c); i += 1
    return "".join(out)


def _split_top_level(body: str, sep: str) -> List[str]:
    """Split ``body`` on ``sep`` while honouring quotes and nesting.

    Used by both the array and inline-table sub-parsers — both need
    "split the commas that aren't inside a string or deeper bracket".
    """
    out: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: Optional[str] = None
    for c in body:
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            continue
        if c in ('"', "'"):
            quote = c; buf.append(c); continue
        if c in "[{":
            depth += 1; buf.append(c); continue
        if c in "]}":
            depth -= 1; buf.append(c); continue
        if c == sep and depth == 0:
            out.append("".join(buf).strip()); buf = []; continue
        buf.append(c)
    tail = "".join(buf).strip()
    if tail:
        out.append(tail)
    return out


def _parse_array(raw: str, lineno: int) -> list:
    body = raw[1:-1].strip()
    if not body:
        return []
    return [_parse_scalar(item, lineno) for item in _split_top_level(body, ",")]


def _parse_inline_table(raw: str, lineno: int) -> dict:
    body = raw[1:-1].strip()
    if not body:
        return {}
    out: dict = {}
    for pair in _split_top_level(body, ","):
        m = _RE_KEY.match(pair)
        if not m:
            raise ManifestError(f"malformed inline table entry: {pair!r}", lineno)
        key, val_raw = _key_value(m)
        out[key] = _parse_scalar(val_raw, lineno)
    return out


def _split_table_path(header: str, lineno: int) -> List[str]:
    """Split a table header like ``go_pins."github.com/foo/bar"`` on
    its dotted segments while keeping quoted segments intact.

    Without this, a Go module path inside a section header would
    be mis-parsed (the dots inside ``github.com`` look like extra
    segment separators). Quoted segments are unescaped in place
    using the same rules as basic strings."""
    out: list[str] = []
    buf: list[str] = []
    in_quote: Optional[str] = None
    i, n = 0, len(header)
    while i < n:
        c = header[i]
        if in_quote:
            if c == "\\" and i + 1 < n:
                buf.append(header[i + 1]); i += 2; continue
            if c == in_quote:
                in_quote = None; i += 1; continue
            buf.append(c); i += 1; continue
        if c in ('"', "'"):
            in_quote = c; i += 1; continue
        if c == ".":
            out.append("".join(buf).strip()); buf = []; i += 1; continue
        buf.append(c); i += 1
    if in_quote is not None:
        raise ManifestError(
            f"unterminated quoted segment in table header [{header}]",
            lineno)
    tail = "".join(buf).strip()
    out.append(tail)
    return out


def _parse_toml(text: str) -> Dict[str, Any]:
    """Parse the supported TOML subset into a nested dict.

    Table headers ``[a.b.c]`` create nested dicts. Each subsequent
    ``key = value`` line attaches to the most recent header (or the
    root table when no header has been seen yet).
    """
    root: Dict[str, Any] = {}
    current: Dict[str, Any] = root

    for i, raw_line in enumerate(text.splitlines(), start=1):
        line = _strip_line_comment(raw_line)
        if _RE_BLANK.match(line):
            continue

        m = _RE_SECTION.match(line)
        if m:
            path = _split_table_path(m.group(1), i)
            if any(not p for p in path):
                raise ManifestError(f"empty table segment in {line!r}", i)
            node = root
            for segment in path:
                node = node.setdefault(segment, {})
                if not isinstance(node, dict):
                    raise ManifestError(
                        f"table header {line!r} overwrites scalar", i)
            current = node
            continue

        m = _RE_KEY.match(line)
        if m:
            key, val_raw = _key_value(m)
            current[key] = _parse_scalar(val_raw, i)
            continue

        raise ManifestError(f"unexpected line: {line!r}", i)

    return root


# ── Manifest dataclass & validation ─────────────────────────

# Legal module-name characters: case-preserving Lam identifiers plus
# the ``@scope/name`` npm-style shape. Matches the grammar-side
# SCOPED_NAME regex so manifest validation and import resolution
# agree on what a legal library name looks like.
_MODULE_NAME_RE = re.compile(r"^(@[A-Za-z0-9_][A-Za-z0-9_\-]*\/[A-Za-z_][A-Za-z0-9_\-]*|[A-Za-z_][A-Za-z0-9_]*)$")

# SemVer MAJOR.MINOR.PATCH with an optional ``-prerelease`` /
# ``+build``. Deliberately stricter than PEP 440 — published Lam
# libraries speak SemVer, full stop.
_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z\-.]+))?(?:\+(?P<build>[0-9A-Za-z\-.]+))?$"
)

# Go-module path: at least one ``segment/segment`` shape, where each
# segment is alphanumeric / dot / dash / underscore. Mirrors the Go
# tooling's own ``module.CheckPath`` predicate but kept loose enough
# to accept gopkg.in / k8s.io / vanity-domain layouts. A single bare
# segment (no slash) is rejected because Go's import system would
# treat it as the local std-lib package, not a module.
_GO_MODULE_PATH_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-]*"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._\-]*)+"
    r"(?:/v[2-9][0-9]*)?$"
)

# Go-module version: either ``vMAJOR.MINOR.PATCH[-pre][+build]``
# (the published-tag form) or a pseudo-version produced by
# ``go mod`` itself — ``v0.0.0-20250101010101-deadbeefcafe``.
_GO_VERSION_RE = re.compile(
    r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z\-.]+))?(?:\+(?P<build>[0-9A-Za-z\-.]+))?$"
)


@dataclass
class Manifest:
    """In-memory view of a ``lamlib.toml``.

    Only the fields Lam actually consumes are typed out; the raw
    parsed tree is preserved in ``.extras`` for forward-compat with
    newer registries that add extra keys the current compiler
    doesn't yet understand.
    """

    name:         str
    version:      str
    description:  str = ""
    license:      str = ""
    authors:      List[str] = field(default_factory=list)
    # ``homepage`` / ``repository`` are documentation-only metadata
    # the registry surfaces in its package listings; the compiler
    # doesn't read them, but ``lamc publish`` ships them.
    homepage:     str = ""
    repository:   str = ""

    lamc_range:   Optional[str] = None           # ``compatibility.lamc``
    dependencies: Dict[str, "DepSpec"] = field(default_factory=dict)
    # Go-module requirements declared by the library. Keys are
    # full Go import paths (``github.com/foo/bar``) and values are
    # version strings the library has been tested against. The
    # installer collects these across the whole transitive set so it
    # can warn / refuse when two libraries pull in incompatible
    # majors of the same module — and so the compile-time
    # ``go.mod`` writer can pin them deterministically instead of
    # trusting whatever ``go mod tidy`` happens to land on first.
    go_deps:      Dict[str, str] = field(default_factory=dict)
    scripts:      Dict[str, str] = field(default_factory=dict)

    # ``[replace]`` overrides — only honoured when this manifest is
    # the *project root*; library-level replaces are intentionally
    # ignored at install time so a transitive lib can't sneak in a
    # rewrite of an unrelated dep behind the user's back. Modelled
    # on Go's ``go.mod`` ``replace`` directive.
    replace:      Dict[str, "ReplaceSpec"] = field(default_factory=dict)

    source_path:  Optional[Path] = None          # path of the .toml
    extras:       Dict[str, Any] = field(default_factory=dict)

    # ── Factory / IO ─────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        """Read + parse ``path`` (usually ``<lib>/lamlib.toml``)."""
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise ManifestError(f"cannot read {path}: {e}")
        m = cls.from_text(text)
        m.source_path = Path(path).resolve()
        return m

    @classmethod
    def from_text(cls, text: str) -> "Manifest":
        tree = _parse_toml(text)
        lib = tree.get("library")
        if not isinstance(lib, dict):
            raise ManifestError("missing [library] section")

        # Required fields
        name = _require_str(lib, "name", "library.name")
        if not _MODULE_NAME_RE.match(name):
            raise ManifestError(
                f"library.name {name!r} is not a legal module name "
                "(expected an ASCII identifier or @scope/name)")
        version = _require_str(lib, "version", "library.version")
        if not _SEMVER_RE.match(version):
            raise ManifestError(
                f"library.version {version!r} is not a valid SemVer "
                "(MAJOR.MINOR.PATCH[-prerelease])")
        licence = lib.get("license", "")
        if not isinstance(licence, str):
            raise ManifestError("library.license must be a string")

        # Optional fields
        description = lib.get("description", "")
        authors = lib.get("authors", []) or []
        if not isinstance(authors, list) or not all(isinstance(a, str) for a in authors):
            raise ManifestError("library.authors must be a list of strings")
        homepage = lib.get("homepage", "")
        repository = lib.get("repository", "")
        if not isinstance(homepage, str):
            raise ManifestError("library.homepage must be a string")
        if not isinstance(repository, str):
            raise ManifestError("library.repository must be a string")

        # Compatibility
        compat = tree.get("compatibility", {})
        lamc_range = compat.get("lamc") if isinstance(compat, dict) else None
        if lamc_range is not None and not isinstance(lamc_range, str):
            raise ManifestError("compatibility.lamc must be a string range")

        # Dependencies — either ``name = "^1.0"`` or ``name = {path="…"}``
        deps_raw = tree.get("dependencies", {}) or {}
        if not isinstance(deps_raw, dict):
            raise ManifestError("[dependencies] must be a table")
        deps: Dict[str, DepSpec] = {}
        for k, v in deps_raw.items():
            if not _MODULE_NAME_RE.match(k):
                raise ManifestError(
                    f"dependencies.{k} is not a legal module name")
            deps[k] = DepSpec.parse(k, v)

        # Go-module dependencies — flat ``"path" = "version"`` table.
        # Both ``[go-deps]`` and ``[go.dependencies]`` are accepted to
        # match the two natural spellings new contributors reach for.
        go_deps_raw = (tree.get("go-deps")
                       or (tree.get("go", {}) or {}).get("dependencies")
                       or {})
        if not isinstance(go_deps_raw, dict):
            raise ManifestError("[go-deps] must be a table")
        go_deps: Dict[str, str] = {}
        for k, v in go_deps_raw.items():
            if not _GO_MODULE_PATH_RE.match(k):
                raise ManifestError(
                    f"go-deps.{k!r} is not a legal Go module path "
                    "(expected ``host/path/segment`` shape)")
            if not isinstance(v, str) or not v.strip():
                raise ManifestError(
                    f"go-deps.{k} must be a non-empty version string")
            if not _GO_VERSION_RE.match(v.strip()):
                raise ManifestError(
                    f"go-deps.{k} version {v!r} is not a valid Go "
                    "module version (expected ``v<MAJOR>.<MINOR>.<PATCH>`` "
                    "or a pseudo-version)")
            go_deps[k] = v.strip()

        # Scripts
        scripts_raw = tree.get("scripts", {}) or {}
        if not isinstance(scripts_raw, dict):
            raise ManifestError("[scripts] must be a table")
        scripts: Dict[str, str] = {}
        for k, v in scripts_raw.items():
            if not isinstance(v, str):
                raise ManifestError(f"scripts.{k} must be a string command")
            scripts[k] = v

        # ``[replace]`` — project-only override map. The resolver
        # consults this *before* normal dep lookup so a library can
        # be redirected to a local checkout or a forked git URL
        # without modifying ``[dependencies]`` itself. Modeled on
        # Go's ``replace`` directive in ``go.mod``.
        replace_raw = tree.get("replace", {}) or {}
        if not isinstance(replace_raw, dict):
            raise ManifestError("[replace] must be a table")
        replace: Dict[str, "ReplaceSpec"] = {}
        for k, v in replace_raw.items():
            if not _MODULE_NAME_RE.match(k):
                raise ManifestError(
                    f"replace.{k} is not a legal module name")
            replace[k] = ReplaceSpec.parse(k, v)

        # ``[workspace]`` — reserved for a future multi-package
        # layout (Cargo / npm workspaces). Forbidden in user
        # manifests for now so manifests written today don't lock
        # us into an accidental schema when the feature lands.
        if "workspace" in tree:
            raise ManifestError(
                "[workspace] is reserved for a future Lammergeier "
                "feature and is not yet supported in user manifests. "
                "Please remove the [workspace] block.")

        # Stash the whole tree so callers can peek at unknown keys.
        extras = {k: v for k, v in tree.items()
                  if k not in {"library", "compatibility",
                               "dependencies", "scripts",
                               "go-deps", "go", "replace",
                               "workspace"}}

        return cls(
            name=name,
            version=version,
            description=description,
            homepage=homepage,
            repository=repository,
            license=licence,
            authors=list(authors),
            lamc_range=lamc_range,
            dependencies=deps,
            go_deps=go_deps,
            scripts=scripts,
            replace=replace,
            extras=extras,
        )

    # ── Convenience ─────────────────────────────────────────

    @property
    def is_scoped(self) -> bool:
        """``True`` iff this library uses a ``@scope/name`` identifier."""
        return self.name.startswith("@")

    @property
    def scope(self) -> str:
        """The ``@scope`` half of a scoped name (``"alice"`` for
        ``"@alice/lamwebp"``), or ``""`` for a plain name."""
        if not self.is_scoped:
            return ""
        return self.name[1:].split("/", 1)[0]

    @property
    def bare_name(self) -> str:
        """The portion after ``/`` for scoped names; the full name
        otherwise. Used by tooling that wants the symbol-friendly
        identifier without the scope prefix."""
        if self.is_scoped:
            return self.name.split("/", 1)[1]
        return self.name

    def semver_parts(self) -> Tuple[int, int, int, str, str]:
        """Return ``(major, minor, patch, prerelease, build)`` tuple."""
        m = _SEMVER_RE.match(self.version)
        assert m, "validated by from_text"
        return (int(m["major"]), int(m["minor"]), int(m["patch"]),
                m["pre"] or "", m["build"] or "")


@dataclass
class DepSpec:
    """One entry in ``[dependencies]``.

    Either a version-range spec, a local ``path`` override, or a
    direct ``git`` source. Exactly one of ``range`` / ``path`` /
    ``git`` is populated.
    """

    name:  str
    range: Optional[str] = None
    path:  Optional[str] = None
    git:   Optional[str] = None
    ref:   Optional[str] = None

    @classmethod
    def parse(cls, name: str, raw: Any) -> "DepSpec":
        if isinstance(raw, str):
            return cls(name=name, range=raw)
        if isinstance(raw, dict):
            source_keys = [k for k in ("path", "git", "version") if k in raw]
            if len(source_keys) > 1:
                raise ManifestError(
                    f"dependencies.{name}: cannot mix 'path', 'git', and 'version'")
            if "path" in raw:
                p = raw["path"]
                if not isinstance(p, str):
                    raise ManifestError(
                        f"dependencies.{name}.path must be a string")
                return cls(name=name, path=p)
            if "git" in raw:
                g = raw["git"]
                if not isinstance(g, str) or not g:
                    raise ManifestError(
                        f"dependencies.{name}.git must be a non-empty string")
                ref = raw.get("ref")
                if ref is not None and not isinstance(ref, str):
                    raise ManifestError(
                        f"dependencies.{name}.ref must be a string")
                return cls(name=name, git=g, ref=ref or None)
            if "version" in raw:
                v = raw["version"]
                if not isinstance(v, str):
                    raise ManifestError(
                        f"dependencies.{name}.version must be a string")
                return cls(name=name, range=v)
            raise ManifestError(
                f"dependencies.{name} must have 'version', 'path', or 'git'")
        raise ManifestError(f"dependencies.{name} has unsupported shape")


@dataclass
class ReplaceSpec:
    """One entry in ``[replace]``.

    Either a local ``path`` override or a ``git`` URL with an
    optional ``ref``. Exactly one of the two source fields is
    populated. Keeping the schema flat and Go-shaped (``path`` /
    ``git`` / ``ref``) means the eventual ``go mod`` interop is
    trivial and the user's mental model carries over.
    """

    name: str
    path: Optional[str] = None
    git:  Optional[str] = None
    ref:  Optional[str] = None

    @classmethod
    def parse(cls, name: str, raw: Any) -> "ReplaceSpec":
        if not isinstance(raw, dict):
            raise ManifestError(
                f"replace.{name} must be an inline table "
                f"(``{{ path = \"…\" }}`` or ``{{ git = \"…\", ref = \"…\" }}``)")
        if "path" in raw and ("git" in raw or "ref" in raw):
            raise ManifestError(
                f"replace.{name}: cannot mix ``path`` with ``git`` / ``ref``")
        if "path" in raw:
            p = raw["path"]
            if not isinstance(p, str) or not p:
                raise ManifestError(
                    f"replace.{name}.path must be a non-empty string")
            return cls(name=name, path=p)
        if "git" in raw:
            g = raw["git"]
            if not isinstance(g, str) or not g:
                raise ManifestError(
                    f"replace.{name}.git must be a non-empty string")
            ref = raw.get("ref")
            if ref is not None and not isinstance(ref, str):
                raise ManifestError(
                    f"replace.{name}.ref must be a string")
            return cls(name=name, git=g, ref=ref or None)
        raise ManifestError(
            f"replace.{name} must declare either ``path`` or ``git``")

    def to_install_spec(self, base_dir: Optional[Path] = None) -> str:
        """Render this replacement as a string the install resolver
        (``_resolve_plan`` / ``install_one``) can consume directly.

        ``base_dir`` is the directory the project's ``lamlib.toml``
        lives in; relative ``path =`` overrides resolve against it
        so a teammate cloning the repo gets the same install set
        regardless of where they ran ``lamc install`` from.
        """
        if self.path:
            p = Path(self.path)
            if not p.is_absolute() and base_dir is not None:
                p = (base_dir / p).resolve()
            return str(p)
        if self.git:
            return f"{self.git}@{self.ref}" if self.ref else self.git
        raise ManifestError(
            f"replace.{self.name} has no usable source")


# ── Small helpers ────────────────────────────────────────────

def _require_str(d: dict, key: str, label: str) -> str:
    if key not in d:
        raise ManifestError(f"missing required field: {label}")
    v = d[key]
    if not isinstance(v, str):
        raise ManifestError(f"{label} must be a string")
    return v


# ── SemVer range matching ────────────────────────────────────

# Subset we honour on the install path. Expressive enough to cover
# the patterns ``lamlib.toml`` files will actually use:
#   "1.2.3"          exact
#   "^1.2.3"         >=1.2.3, <2.0.0 (major pinned)
#   "~1.2.3"         >=1.2.3, <1.3.0 (minor pinned)
#   ">=1.2.3"        tail-open
#   ">=1.2.3, <2.0"  conjunction
#   "*"              any

_OP_RE = re.compile(r"^(>=|<=|>|<|==|=|\^|~)?\s*([0-9A-Za-z\-.+*]+)$")


def _ver_tuple(v: str) -> Tuple[int, int, int]:
    """Coerce a (possibly partial) SemVer like ``1.2`` → ``(1, 2, 0)``.

    Partial versions appear in ranges like ``>=1.2`` — we treat the
    missing components as zero, matching the npm / Cargo conventions.
    Returns ``(0, 0, 0)`` for the wildcard ``*``.
    """
    if v == "*":
        return (0, 0, 0)
    parts = v.split("-", 1)[0].split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        return (0, 0, 0)


# ``MAJOR.x``, ``MAJOR.MINOR.x`` — npm-style major / minor wildcards.
_X_RE = re.compile(r"^(0|[1-9]\d*)(?:\.(0|[1-9]\d*))?\.x$")


def _normalise_clauses(spec: str) -> List[str]:
    """Split ``spec`` into a list of single-operator constraints.

    Supports the three documented separators — comma (``">=1, <2"``),
    space (``">=1 <2"``), and a single ``MAJOR.x`` shorthand which
    expands into ``[">=MAJOR.0.0", "<MAJOR+1.0.0"]``. The output is
    always a list of clauses each match-able by ``_OP_RE``."""
    spec = spec.strip()
    if not spec or spec in ("*", "any"):
        return []

    m = _X_RE.match(spec)
    if m:
        major = int(m.group(1))
        if m.group(2) is not None:
            minor = int(m.group(2))
            return [f">={major}.{minor}.0", f"<{major}.{minor + 1}.0"]
        return [f">={major}.0.0", f"<{major + 1}.0.0"]

    # Comma OR whitespace separates clauses. We split on commas
    # first, then split each chunk on whitespace BUT only between
    # clauses (a single ``>=1.0`` chunk has internal whitespace).
    chunks: list[str] = []
    for piece in spec.split(","):
        piece = piece.strip()
        if not piece:
            continue
        # Re-glue ``>=`` / ``<=`` / ``==`` to their version literal
        # if a stray space was inserted between them.
        # ``>= 1.0`` → ``>=1.0``.
        piece = re.sub(r"(>=|<=|==|=|<|>|\^|~)\s+", r"\1", piece)
        # If the chunk still contains whitespace, every word inside
        # is an independent clause (``">=1.0 <2.0"`` shape).
        for sub in piece.split():
            if sub:
                chunks.append(sub)
    return chunks


def satisfies(version: str, spec: str) -> bool:
    """Return ``True`` iff ``version`` (concrete SemVer) matches
    the range ``spec``. Unknown spec syntax falls back to exact
    match so at worst we err on the side of rejecting.
    """
    clauses = _normalise_clauses(spec)
    if not clauses:
        return True
    vt = _ver_tuple(version)
    for clause in clauses:
        m = _OP_RE.match(clause)
        if not m:
            # Bare "1.2.3" — treat as exact equality.
            if _ver_tuple(clause) != vt:
                return False
            continue
        op, ref = m.group(1) or "=", m.group(2)
        rt = _ver_tuple(ref)
        if op in ("=", "=="):
            if vt != rt:
                return False
        elif op == ">":
            if not (vt > rt):
                return False
        elif op == ">=":
            if not (vt >= rt):
                return False
        elif op == "<":
            if not (vt < rt):
                return False
        elif op == "<=":
            if not (vt <= rt):
                return False
        elif op == "^":
            # Same major, >= ref.
            if vt < rt or vt[0] != rt[0]:
                return False
        elif op == "~":
            # Same major+minor, >= ref.
            if vt < rt or vt[0] != rt[0] or vt[1] != rt[1]:
                return False
        else:
            return False
    return True


# ── Public validation helpers ───────────────────────────────

def is_valid_module_name(name: str) -> bool:
    """Cheap predicate the install CLI uses when parsing a spec.

    Mirrors :data:`_MODULE_NAME_RE` so the manifest validator and
    every other tool agree on the grammar."""
    return bool(name) and bool(_MODULE_NAME_RE.match(name))


def is_valid_semver(version: str) -> bool:
    """Cheap SemVer predicate (no exception path)."""
    return bool(version) and bool(_SEMVER_RE.match(version))


def parse_constraint(spec: str) -> List[str]:
    """Validate + canonicalise a version constraint string.

    Returns the list of normalised clauses (e.g. ``"1.x"`` →
    ``[">=1.0.0", "<2.0.0"]``). Raises :class:`ManifestError` for
    constraints we can't parse so callers can surface a useful
    error rather than silently coercing to "match everything"."""
    spec = (spec or "").strip()
    if not spec or spec in ("*", "any"):
        return []
    clauses = _normalise_clauses(spec)
    if not clauses:
        raise ManifestError(f"unparseable constraint: {spec!r}")
    for c in clauses:
        m = _OP_RE.match(c)
        if not m:
            raise ManifestError(f"unparseable constraint clause: {c!r}")
        op, ref = m.group(1) or "=", m.group(2)
        # The reference must look like a (possibly partial) SemVer
        # — letters that aren't part of a prerelease tag are
        # rejected so we catch typos like ``>=garbage``.
        if not re.match(r"^[0-9]+(\.[0-9]+){0,2}(?:-[0-9A-Za-z\-.]+)?$",
                        ref):
            raise ManifestError(f"unparseable version reference: {ref!r}")
    return clauses


# ── Go-module helpers ───────────────────────────────────────

def is_valid_go_module_path(path: str) -> bool:
    """Public predicate the installer uses when validating
    ``[go-deps]`` keys outside the manifest parser proper."""
    return bool(path) and bool(_GO_MODULE_PATH_RE.match(path))


def is_valid_go_version(version: str) -> bool:
    """Cheap Go-version predicate (no exception path)."""
    return bool(version) and bool(_GO_VERSION_RE.match(version))


def go_version_tuple(version: str) -> Tuple[int, int, int, int, str]:
    """Sortable view of a Go-module version.

    Returns ``(major, minor, patch, is_release, prerelease)`` where
    ``is_release`` is ``1`` for a tagged release and ``0`` for a
    prerelease — that boolean carries the SemVer precedence rule
    that *any* prerelease sorts before its corresponding release
    (``v1.2.3-rc1 < v1.2.3``). Unrecognised inputs collapse to
    ``(0, 0, 0, 0, "")`` so callers can still compare them without
    crashing."""
    m = _GO_VERSION_RE.match(version or "")
    if not m:
        return (0, 0, 0, 0, "")
    pre = m["pre"] or ""
    is_release = 1 if not pre else 0
    return (int(m["major"]), int(m["minor"]), int(m["patch"]),
            is_release, pre)


def go_major(version: str) -> int:
    """Return the SemVer major number embedded in a Go version
    string. Used to spot ``v1`` vs ``v2`` conflicts — Go modules
    treat each major as a distinct importable package, so two
    libraries that pin different majors fundamentally cannot be
    deduplicated."""
    m = _GO_VERSION_RE.match(version or "")
    if not m:
        return 0
    return int(m["major"])
