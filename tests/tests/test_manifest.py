#!/usr/bin/env python3
"""Tests for ``compiler/manifest.py`` — ``lamlib.toml`` parser.

The manifest parser is hand-rolled because we don't want a runtime
dependency on a TOML library, so the test surface is intentionally
broad: every grammar shape (key/value, table, array, escape) and
every documented validation rule (SemVer, name, required keys) gets
its own test.

Run with::

    python3 tests/tests/test_manifest.py
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.manifest import (
    Manifest,
    ManifestError,
    go_major,
    go_version_tuple,
    is_valid_go_module_path,
    is_valid_go_version,
    is_valid_module_name,
    is_valid_semver,
    parse_constraint,
    satisfies,
)


# ── Helpers ──────────────────────────────────────────────────

def _from_text(text: str) -> Manifest:
    return Manifest.from_text(textwrap.dedent(text).lstrip())


# ── Round-trip parsing ──────────────────────────────────────

def test_minimal_manifest_parses() -> None:
    """The smallest legal manifest — ``[library]`` with name + version
    — must round-trip without errors and expose both fields."""
    mf = _from_text("""
        [library]
        name = "lamhello"
        version = "1.0.0"
    """)
    assert mf.name == "lamhello", mf.name
    assert mf.version == "1.0.0", mf.version
    assert mf.dependencies == {}, mf.dependencies
    assert mf.is_scoped is False
    print("PASS: minimal manifest parses")


def test_full_manifest_fields() -> None:
    """All optional fields populate; arrays + scoped names + comments
    survive the parser without surprises."""
    mf = _from_text("""
        # This is a comment and should be ignored.
        [library]
        name        = "@alice/lamwebp"
        version     = "1.2.3-rc.1"
        description = "WebP encoder bindings"
        license     = "Apache-2.0"
        authors     = ["Alice <a@x.io>", "Bob <b@y.io>"]
        homepage    = "https://example.com/lamwebp"
        repository  = "https://github.com/alice/lamwebp"

        [dependencies]
        lamhttp     = "1.x"
        "@bob/lamutil" = ">=0.4 <1.0"

        [compatibility]
        lamc = "*"

        [tags]
        keywords = ["image", "encoder"]
    """)
    assert mf.name == "@alice/lamwebp", mf.name
    assert mf.is_scoped is True
    assert mf.scope == "alice"
    assert mf.bare_name == "lamwebp"
    assert mf.version == "1.2.3-rc.1"
    assert mf.description == "WebP encoder bindings"
    assert mf.license == "Apache-2.0"
    assert mf.authors == ["Alice <a@x.io>", "Bob <b@y.io>"], mf.authors
    assert mf.homepage == "https://example.com/lamwebp"
    assert mf.repository == "https://github.com/alice/lamwebp"
    # ``dependencies`` is ``Dict[str, DepSpec]`` — compare the
    # range strings rather than relying on dataclass equality.
    assert {k: v.range for k, v in mf.dependencies.items()} == {
        "lamhttp":      "1.x",
        "@bob/lamutil": ">=0.4 <1.0",
    }, mf.dependencies
    print("PASS: full manifest fields")


def test_string_escapes() -> None:
    """Standard JSON-style escapes inside ``"..."`` strings (``\\n``
    et al.) decode to the intended characters; literal ``'...'``
    strings preserve their content verbatim."""
    mf = _from_text("""
        [library]
        name = "lamhello"
        version = "1.0.0"
        description = "line1\\nline2\\t\\\"end"
        homepage = 'https://x.io/raw\\nstays'
    """)
    assert mf.description == 'line1\nline2\t"end', repr(mf.description)
    assert mf.homepage == 'https://x.io/raw\\nstays', repr(mf.homepage)
    print("PASS: string escapes")


def test_load_from_file() -> None:
    """``Manifest.load`` records ``source_path`` so the install gate
    can locate the on-disk source tree later."""
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "lamlib.toml"
        p.write_text(
            '[library]\nname = "lamfile"\nversion = "0.1.0"\n',
            encoding="utf-8")
        mf = Manifest.load(p)
        assert mf.source_path == p, mf.source_path
        assert mf.name == "lamfile"
    print("PASS: Manifest.load preserves source_path")


# ── Validation ──────────────────────────────────────────────

def test_missing_required_keys_fail() -> None:
    """A manifest without ``name`` or ``version`` must raise
    ``ManifestError`` — these are the two pieces of metadata
    every other tool relies on."""
    for body in [
        '[library]\nversion = "1.0.0"\n',           # no name
        '[library]\nname = "x"\n',                  # no version
        'name = "x"\nversion = "1.0.0"\n',          # no [library] section
    ]:
        try:
            Manifest.from_text(body)
        except ManifestError:
            continue
        raise AssertionError(f"expected ManifestError for {body!r}")
    print("PASS: missing required keys raise ManifestError")


def test_bad_version_strings_rejected() -> None:
    """Versions must be SemVer-shaped (``MAJOR.MINOR.PATCH`` with an
    optional ``-PRE``). Garbage strings are rejected with a clear
    error so ``lamc publish`` can't ship ``versions=["latest"]``
    style nonsense."""
    for bad in ["1.0", "abc", "1.0.0.0", "1.0-rc1"]:
        try:
            _from_text(f'[library]\nname = "x"\nversion = "{bad}"\n')
        except ManifestError:
            continue
        raise AssertionError(f"expected rejection of version={bad!r}")
    print("PASS: bad versions rejected")


def test_module_name_case_styles_accepted() -> None:
    """Library, dependency, and replacement names preserve the user's
    chosen ASCII casing instead of forcing snake_case."""
    for name in ["snake_case", "camelCase", "PascalCase", "SCREAMING_SNAKE_CASE",
                 "@MyTeam/camelCase", "@my_team/Pascal-Lib"]:
        mf = _from_text(f'[library]\nname = "{name}"\nversion = "1.0.0"\n')
        assert mf.name == name
    mf = _from_text("""
        [library]
        name = "MyLibrary"
        version = "1.0.0"

        [dependencies]
        camelCaseDep = "^1.0"
        "@MyTeam/PascalDep" = "^2.0"

        [replace]
        PascalReplacement = { path = "../replacement" }
    """)
    assert set(mf.dependencies) == {"camelCaseDep", "@MyTeam/PascalDep"}
    assert set(mf.replace) == {"PascalReplacement"}
    print("PASS: module names preserve snake/camel/Pascal/uppercase styles")


def test_bad_module_names_rejected() -> None:
    """Names remain path-safe Lam identifiers: dots, spaces, traversal,
    plain-name hyphens, missing scoped segments, and leading digits are out."""
    for bad in ["1library", "lam.webp", "lam-webp", "has space", "../escape",
                "@/lamwebp", "@alice/", "/lamwebp", "@Team/1Library"]:
        try:
            _from_text(f'[library]\nname = "{bad}"\nversion = "1.0.0"\n')
        except ManifestError:
            continue
        raise AssertionError(f"expected rejection of name={bad!r}")
    print("PASS: unsafe module names rejected")


def test_unknown_top_level_section_warns_only() -> None:
    """Unknown sections (e.g. a future ``[whatever]`` table) must
    not break the parser — they're attached to the manifest as
    ``extras`` so newer compilers can co-exist with older
    manifests."""
    mf = _from_text("""
        [library]
        name = "lamx"
        version = "1.0.0"

        [made_up_section]
        anything = true
    """)
    assert "made_up_section" in mf.extras
    assert mf.extras["made_up_section"]["anything"] is True
    print("PASS: unknown sections survive as ``extras``")


# ── SemVer helpers ───────────────────────────────────────────

def test_satisfies_basic() -> None:
    """The constraint helpers cover the three shapes documented in
    ``third_party_libraries.md``: exact, range, and ``MAJOR.x``."""
    assert satisfies("1.2.3", "1.x")
    assert not satisfies("2.0.0", "1.x")
    assert satisfies("1.2.3", ">=1.0 <2.0")
    assert not satisfies("0.9.0", ">=1.0 <2.0")
    assert satisfies("1.0.0", "1.0.0")
    assert satisfies("1.2.0", "*")
    assert satisfies("1.2.0", "")     # empty == any
    print("PASS: satisfies() basic cases")


def test_is_valid_helpers() -> None:
    """The ``is_valid_*`` helpers are also exposed for the install
    CLI's spec parser, so verify them directly here."""
    assert is_valid_module_name("lamwebp")
    assert is_valid_module_name("LamWebP")
    assert is_valid_module_name("camelCase")
    assert is_valid_module_name("SCREAMING_SNAKE_CASE")
    assert is_valid_module_name("@Alice/LamWebP")
    assert not is_valid_module_name("1Lamwebp")
    assert not is_valid_module_name("@/lamwebp")
    assert is_valid_semver("1.0.0")
    assert is_valid_semver("1.0.0-rc.1")
    assert not is_valid_semver("1.0")
    assert not is_valid_semver("v1.0.0")
    print("PASS: is_valid_module_name + is_valid_semver")


def test_parse_constraint_round_trip() -> None:
    """``parse_constraint`` is the canonical normaliser; bad
    constraints raise so the install CLI can surface useful
    diagnostics."""
    for ok in ["", "*", "1.x", "1.0.0", ">=1.0 <2.0"]:
        parse_constraint(ok)
    for bad in ["1.x.y", ">=garbage", "<<1.0"]:
        try:
            parse_constraint(bad)
        except ManifestError:
            continue
        raise AssertionError(f"expected rejection of {bad!r}")
    print("PASS: parse_constraint validates")


# ── Go-module deps ───────────────────────────────────────────

def test_go_deps_table_parses() -> None:
    """Both ``[go-deps]`` and ``[go.dependencies]`` populate the
    same dict on the manifest, since publishers reach for either
    spelling."""
    mf = _from_text("""
        [library]
        name = "lamx"
        version = "1.0.0"

        [go-deps]
        "github.com/foo/bar" = "v1.2.3"
        "gopkg.in/yaml.v2"   = "v2.4.0"
    """)
    assert mf.go_deps == {
        "github.com/foo/bar": "v1.2.3",
        "gopkg.in/yaml.v2":   "v2.4.0",
    }, mf.go_deps

    # The alternative spelling resolves to the same field.
    mf2 = _from_text("""
        [library]
        name = "lamx"
        version = "1.0.0"

        [go.dependencies]
        "github.com/foo/bar" = "v1.2.3"
    """)
    assert mf2.go_deps == {"github.com/foo/bar": "v1.2.3"}, mf2.go_deps
    print("PASS: [go-deps] / [go.dependencies] parse")


def test_git_dependency_form_parses() -> None:
    """Direct git dependencies use the same flat inline-table shape
    as ``[replace]``: ``{ git = "...", ref = "..." }``."""
    mf = _from_text("""
        [library]
        name = "lamx"
        version = "1.0.0"

        [dependencies]
        lamgit = { git = "https://github.com/acme/lamgit.git", ref = "v1.2.0" }
    """)
    dep = mf.dependencies["lamgit"]
    assert dep.git == "https://github.com/acme/lamgit.git", dep
    assert dep.ref == "v1.2.0", dep
    assert dep.range is None and dep.path is None, dep
    print("PASS: git dependency form parses")


def test_go_deps_validate_paths_and_versions() -> None:
    """Bad Go module paths (bare segment, ``UPPER`` chars at start)
    and bad versions (missing ``v`` prefix, non-SemVer body) are
    rejected up-front so we catch typos before the registry roundtrip."""
    bad_path = """
        [library]
        name = "lamx"
        version = "1.0.0"

        [go-deps]
        "single_segment" = "v1.0.0"
    """
    try:
        _from_text(bad_path)
    except ManifestError:
        pass
    else:
        raise AssertionError("expected rejection of single-segment go-dep path")

    bad_ver = """
        [library]
        name = "lamx"
        version = "1.0.0"

        [go-deps]
        "github.com/foo/bar" = "1.2.3"
    """
    try:
        _from_text(bad_ver)
    except ManifestError:
        pass
    else:
        raise AssertionError("expected rejection of unprefixed go version")
    print("PASS: go-deps validation rejects malformed entries")


def test_go_version_helpers() -> None:
    """The Go-side helpers feed the installer's MVS pick + conflict
    detector. Verify they sort sensibly and surface the major."""
    assert is_valid_go_module_path("github.com/foo/bar")
    assert is_valid_go_module_path("github.com/foo/bar/v3")
    assert not is_valid_go_module_path("foo")
    assert not is_valid_go_module_path("/leading-slash")

    assert is_valid_go_version("v1.0.0")
    assert is_valid_go_version("v0.0.0-20250101010101-deadbeefcafe")
    assert not is_valid_go_version("1.0.0")  # missing v
    assert not is_valid_go_version("vlatest")

    # Version sort: tagged release > prerelease of the same triple.
    assert go_version_tuple("v1.2.3") > go_version_tuple("v1.2.3-rc1")
    assert go_version_tuple("v2.0.0") > go_version_tuple("v1.99.99")
    assert go_major("v1.2.3") == 1
    assert go_major("v2.0.0") == 2
    assert go_major("not-a-version") == 0
    print("PASS: go version + path helpers")


# ── Driver ───────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_minimal_manifest_parses,
        test_full_manifest_fields,
        test_string_escapes,
        test_load_from_file,
        test_missing_required_keys_fail,
        test_bad_version_strings_rejected,
        test_module_name_case_styles_accepted,
        test_bad_module_names_rejected,
        test_unknown_top_level_section_warns_only,
        test_satisfies_basic,
        test_is_valid_helpers,
        test_parse_constraint_round_trip,
        test_go_deps_table_parses,
        test_git_dependency_form_parses,
        test_go_deps_validate_paths_and_versions,
        test_go_version_helpers,
    ]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL: {t.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"ERROR: {t.__name__}: {exc!r}")
    print()
    print("=" * 60)
    if failures:
        print(f"Manifest: {failures} of {len(tests)} tests failed")
        return 1
    print(f"Manifest: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
