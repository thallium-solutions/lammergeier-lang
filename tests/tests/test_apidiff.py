#!/usr/bin/env python3
"""Tests for ``compiler/apidiff.py`` — breaking-change detector.

Each test case feeds a synthetic ``old`` + ``new`` source pair into
:func:`compiler.apidiff.compare` and asserts the diff classifies
each delta with the right severity. The detector is what gates the
``lamc install`` SemVer check, so a regression here lets a publisher
ship a "patch" release that secretly removes a public method —
exactly the failure mode the system is supposed to prevent.

Run with::

    python3 tests/tests/test_apidiff.py
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler.apidiff import (
    compare,
    expected_bump,
    extract_surface,
    worst_severity,
)


def _src(text: str) -> str:
    return textwrap.dedent(text).lstrip()


def _surfaces(old_src: str, new_src: str):
    return extract_surface(_src(old_src)), extract_surface(_src(new_src))


def _kinds(changes) -> list[tuple[str, str, str]]:
    """Tuples of ``(severity, kind, path)`` so tests can match on
    structure without coupling to exact message wording."""
    return [(c.severity, c.kind, c.path) for c in changes]


# ── Surface extraction ───────────────────────────────────────

def test_extract_top_level_funcs() -> None:
    """Top-level ``func`` decls land in ``surface.funcs`` with their
    parameter list intact (and the receiver dropped if it sneaks
    into a free function — defensive)."""
    s = extract_surface(_src("""
        func hello(name: str) -> str { return name }
        func goodbye() { return }
    """))
    assert "hello" in s.funcs
    assert "goodbye" in s.funcs
    sig = s.funcs["hello"]
    assert sig.params == [("name", "str")], sig.params
    assert sig.return_type == "str"
    print("PASS: extract top-level funcs")


def test_extract_class_with_methods_and_fields() -> None:
    """Class body scan picks up methods, bare-annotation fields,
    and the receiver gets stripped from each method's params."""
    s = extract_surface(_src("""
        class Encoder {
            quality: int
            level: int = 6
            static shared: str = "yes"
            private static secret: str = "no"
            func encode(self, data: str) -> str { return data }
            func _private(self) { }
            private func internal(self) { }
        }
    """))
    assert "Encoder" in s.classes
    cls = s.classes["Encoder"]
    assert cls.fields == {
        "quality": "int",
        "level": "int",
        "shared": "str",
    }, cls.fields
    assert "encode" in cls.methods, list(cls.methods)
    # Underscore + private both excluded
    assert "_private" not in cls.methods
    assert "internal" not in cls.methods
    sig = cls.methods["encode"]
    assert sig.params == [("data", "str")], sig.params
    print("PASS: extract class shape")


def test_extract_skips_underscore_funcs() -> None:
    """``_helper`` is private by convention and must never appear in
    the public surface."""
    s = extract_surface(_src("""
        func _helper() { }
        func public_one() { }
    """))
    assert "_helper" not in s.funcs
    assert "public_one" in s.funcs
    print("PASS: extract skips underscore funcs")


def test_extract_interface_methods() -> None:
    """``interface`` blocks are treated like ABCs — only methods
    matter. Interface methods can be body-less (``-> str`` with
    no ``{ ... }``) so the surface scanner has to accept that
    shape inside an ``interface`` block."""
    s = extract_surface(_src("""
        interface Greeter {
            func greet(self, name: str) -> str { }
        }
    """))
    assert "Greeter" in s.classes
    assert "greet" in s.classes["Greeter"].methods
    print("PASS: extract interface methods")


# ── compare() — breaking ────────────────────────────────────

def test_breaking_removed_function() -> None:
    """Dropping a public function is breaking, full stop."""
    old, new = _surfaces(
        "func a() { }\nfunc b() { }",
        "func a() { }",
    )
    changes = compare(old, new)
    assert ("breaking", "removed_func", "b") in _kinds(changes), \
        _kinds(changes)
    assert worst_severity(changes) == "breaking"
    print("PASS: removed function is breaking")


def test_breaking_required_param_added() -> None:
    """Adding a parameter without a default value to an existing
    function breaks every caller."""
    old, new = _surfaces(
        "func f(a: int) { }",
        "func f(a: int, b: str) { }",
    )
    changes = compare(old, new)
    kinds = _kinds(changes)
    assert any(k[0] == "breaking" and k[1] == "param_added_required"
               for k in kinds), kinds
    print("PASS: added required param is breaking")


def test_feature_optional_param_added() -> None:
    """Adding a parameter *with* a default doesn't break anyone —
    classified as a feature."""
    old, new = _surfaces(
        "func f(a: int) { }",
        "func f(a: int, b: str = 'x') { }",
    )
    changes = compare(old, new)
    kinds = _kinds(changes)
    assert any(k == ("feature", "param_added_optional", "f")
               for k in kinds), kinds
    assert worst_severity(changes) == "feature"
    print("PASS: added optional param is feature")


def test_breaking_return_type_changed() -> None:
    """A different return type breaks consumers that assigned the
    result to a typed binding."""
    old, new = _surfaces(
        "func f() -> int { return 0 }",
        "func f() -> str { return '' }",
    )
    changes = compare(old, new)
    assert any(k[1] == "changed_return" and k[0] == "breaking"
               for k in _kinds(changes)), _kinds(changes)
    print("PASS: return type change is breaking")


def test_breaking_method_removed() -> None:
    """Removing a method from a public class is breaking even if
    the class itself remains."""
    old, new = _surfaces(
        "class C {\n    func a(self) {}\n    func b(self) {}\n}",
        "class C {\n    func a(self) {}\n}",
    )
    changes = compare(old, new)
    assert ("breaking", "removed_method", "C.b") in _kinds(changes)
    print("PASS: removed method is breaking")


def test_breaking_field_type_changed() -> None:
    """A field's type changing is breaking — callers that assigned
    a value of the old type now produce a type error."""
    old, new = _surfaces(
        "class C {\n    quality: int\n}",
        "class C {\n    quality: str\n}",
    )
    changes = compare(old, new)
    assert any(k[0] == "breaking" and k[1] == "changed_field"
               for k in _kinds(changes))
    print("PASS: field type change is breaking")


def test_feature_added_function() -> None:
    """Adding a brand-new public function is a feature, not a
    break."""
    old, new = _surfaces(
        "func a() { }",
        "func a() { }\nfunc b() { }",
    )
    changes = compare(old, new)
    assert any(k == ("feature", "added_func", "b")
               for k in _kinds(changes))
    assert worst_severity(changes) == "feature"
    print("PASS: added function is feature")


def test_no_change_yields_patch() -> None:
    """When nothing differs, ``worst_severity`` falls back to
    ``patch`` — the install gate wants a strictly comparable rank
    even on the empty-diff case."""
    old, new = _surfaces(
        "func a() { print(1) }",
        "func a() { print(2) }",       # body change is invisible to us
    )
    changes = compare(old, new)
    assert changes == [], changes
    assert worst_severity(changes) == "patch"
    print("PASS: empty diff is patch")


def test_expected_bump_mapping() -> None:
    """SemVer math: major bump ⇒ breaking allowed, minor ⇒ feature
    allowed, patch ⇒ only patch allowed."""
    assert expected_bump("1.0.0", "2.0.0") == "breaking"
    assert expected_bump("1.0.0", "1.1.0") == "feature"
    assert expected_bump("1.0.0", "1.0.1") == "patch"
    assert expected_bump("0.1.0", "0.1.1") == "patch"
    print("PASS: expected_bump mapping")


# ── Driver ───────────────────────────────────────────────────

def main() -> int:
    tests = [
        test_extract_top_level_funcs,
        test_extract_class_with_methods_and_fields,
        test_extract_skips_underscore_funcs,
        test_extract_interface_methods,
        test_breaking_removed_function,
        test_breaking_required_param_added,
        test_feature_optional_param_added,
        test_breaking_return_type_changed,
        test_breaking_method_removed,
        test_breaking_field_type_changed,
        test_feature_added_function,
        test_no_change_yields_patch,
        test_expected_bump_mapping,
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
        print(f"ApiDiff: {failures} of {len(tests)} tests failed")
        return 1
    print(f"ApiDiff: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
