#!/usr/bin/env python3
"""Tests for the stdlib Go-module pin table and its merge into
``_collect_go_pins``.

The pin table is a single source of truth for the Go-module
versions the Lam stdlib was validated against. These tests guard
against two regressions:

1. Accidental drift in the table (e.g. malformed version strings,
   duplicated module paths).
2. Silent breakage of the three-layer precedence
   (stdlib < manifest < lockfile) that ``_collect_go_pins``
   promises callers.

Run with::

    python3 tests/tests/test_go_pins.py
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler import lammergeier as lamc
from compiler.lammergeier import _collect_go_pins
from compiler.manifest import is_valid_go_module_path
from compiler.stdlib_go_deps import STDLIB_GO_PINS, STDLIB_GO_PIN_MODULES


# ── Helpers ──────────────────────────────────────────────────

def _write(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip("\n"), encoding="utf-8")


# ── Table sanity ─────────────────────────────────────────────

def test_stdlib_pins_nonempty() -> None:
    assert STDLIB_GO_PINS, "stdlib pin table should not be empty"


def test_stdlib_pins_module_paths_valid() -> None:
    # Every key must be a legal Go module path — otherwise ``go mod``
    # will reject the synthesized ``require`` block and fall back to
    # MVS-latest, defeating the whole point of pinning.
    for path in STDLIB_GO_PINS:
        assert is_valid_go_module_path(path), f"bad module path: {path}"


def test_stdlib_pins_versions_look_semver() -> None:
    # We allow ``vMAJOR.MINOR.PATCH`` and the pseudo-version form
    # (``v0.0.0-YYYYMMDDhhmmss-<commit>``) that ``go get`` emits for
    # untagged commits. Reject anything else early so a typo in the
    # table can't slip into a release.
    for path, ver in STDLIB_GO_PINS.items():
        assert ver.startswith("v"), f"{path}: version must start with 'v'"
        rest = ver[1:]
        head = rest.split("-", 1)[0]  # strip pseudo-version suffix
        parts = head.split(".")
        assert len(parts) == 3, f"{path}: version not vX.Y.Z — got {ver!r}"
        for comp in parts:
            assert comp.isdigit() or comp[0].isdigit(), \
                f"{path}: non-numeric component in {ver!r}"


def test_stdlib_pin_owners_cover_every_pin() -> None:
    assert set(STDLIB_GO_PIN_MODULES) == set(STDLIB_GO_PINS)
    for path, modules in STDLIB_GO_PIN_MODULES.items():
        assert modules, f"{path}: no owning stdlib modules declared"
        for module in modules:
            assert module.startswith("lam"), f"{path}: bad stdlib module {module!r}"


# ── Precedence layering ──────────────────────────────────────

def test_standalone_script_gets_stdlib_pins() -> None:
    # A single-file build with no ``lamlib.toml`` anywhere in the
    # parent chain should still receive every stdlib pin — that's
    # how we keep one-off scripts reproducible.
    with tempfile.TemporaryDirectory() as td:
        pins = _collect_go_pins(Path(td))
    for path, ver in STDLIB_GO_PINS.items():
        assert pins.get(path) == ver, \
            f"stdlib pin missing for {path}: got {pins.get(path)!r}"


def test_project_manifest_overrides_stdlib_pin() -> None:
    # If the user asks for a newer cron in their manifest, the
    # merged table must carry the user's version, not ours.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "lamlib.toml", """
            [library]
            name = "demo"
            version = "0.1.0"
            lammergeier = ">=0.1.0"

            [go-deps]
            "github.com/robfig/cron/v3" = "v3.99.0"
        """)
        pins = _collect_go_pins(root)
    assert pins["github.com/robfig/cron/v3"] == "v3.99.0", pins.get(
        "github.com/robfig/cron/v3"
    )
    # Unrelated stdlib pins are still present.
    assert "gopkg.in/yaml.v3" in pins


def test_lockfile_overrides_manifest_and_stdlib() -> None:
    # Lockfile is the canonical post-resolution view and must win
    # over both the raw manifest and the stdlib defaults.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "lamlib.toml", """
            [library]
            name = "demo"
            version = "0.1.0"
            lammergeier = ">=0.1.0"

            [go-deps]
            "github.com/robfig/cron/v3" = "v3.99.0"
        """)
        _write(root / "lamlib.lock.toml", """
            [go_pins."github.com/robfig/cron/v3"]
            path = "github.com/robfig/cron/v3"
            version = "v3.100.0"
        """)
        pins = _collect_go_pins(root)
    assert pins["github.com/robfig/cron/v3"] == "v3.100.0", pins.get(
        "github.com/robfig/cron/v3"
    )


def test_pins_walk_up_from_subdir() -> None:
    # ``_collect_go_pins`` walks a handful of parent directories —
    # confirm a manifest two levels up is picked up for a source
    # file buried inside ``src/app``.
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "src" / "app").mkdir(parents=True)
        _write(root / "lamlib.toml", """
            [library]
            name = "demo"
            version = "0.1.0"
            lammergeier = ">=0.1.0"

            [go-deps]
            "gopkg.in/yaml.v3" = "v3.42.0"
        """)
        pins = _collect_go_pins(root / "src" / "app")
    assert pins["gopkg.in/yaml.v3"] == "v3.42.0"


def test_stdlib_pins_filter_to_imported_modules() -> None:
    with tempfile.TemporaryDirectory() as td:
        light = _collect_go_pins(Path(td), stdlib_modules={"lamstrings"})
        data = _collect_go_pins(Path(td), stdlib_modules={"lamdata"})
    assert light == {}, light
    assert data == {
        "github.com/go-gota/gota": STDLIB_GO_PINS["github.com/go-gota/gota"],
    }, data


def test_filtered_stdlib_pins_keep_project_pins() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write(root / "lamlib.toml", """
            [library]
            name = "demo"
            version = "0.1.0"
            lammergeier = ">=0.1.0"

            [go-deps]
            "github.com/robfig/cron/v3" = "v3.99.0"
        """)
        pins = _collect_go_pins(root, stdlib_modules={"lamstrings"})
    assert pins == {"github.com/robfig/cron/v3": "v3.99.0"}, pins


def test_lamstrings_build_go_mod_stays_light() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        source = root / "main.lam"
        output = root / "app"
        build_dir = root / "build"
        _write(source, """
            from lamstrings import Strings

            func main() {
                print(Strings.toUpper("ok"))
            }
        """)

        original_mkdtemp = lamc.tempfile.mkdtemp

        def fake_mkdtemp(prefix: str = "", *args, **kwargs) -> str:
            build_dir.mkdir()
            return str(build_dir)

        lamc.tempfile.mkdtemp = fake_mkdtemp
        try:
            lamc.compile_lam(
                str(source),
                str(output),
                keep_go=True,
                use_cache=False,
            )
        finally:
            lamc.tempfile.mkdtemp = original_mkdtemp

        go_mod = (build_dir / "go.mod").read_text(encoding="utf-8")
    heavy_modules = {
        "github.com/go-gota/gota",
        "github.com/redis/go-redis/v9",
        "modernc.org/sqlite",
        "google.golang.org/protobuf",
    }
    for module in heavy_modules:
        assert module not in go_mod, go_mod


# ── Entrypoint ───────────────────────────────────────────────

def main() -> int:
    tests = [
        test_stdlib_pins_nonempty,
        test_stdlib_pins_module_paths_valid,
        test_stdlib_pins_versions_look_semver,
        test_stdlib_pin_owners_cover_every_pin,
        test_standalone_script_gets_stdlib_pins,
        test_project_manifest_overrides_stdlib_pin,
        test_lockfile_overrides_manifest_and_stdlib,
        test_pins_walk_up_from_subdir,
        test_stdlib_pins_filter_to_imported_modules,
        test_filtered_stdlib_pins_keep_project_pins,
        test_lamstrings_build_go_mod_stays_light,
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
        print(f"Go-pins: {failures} of {len(tests)} tests failed")
        return 1
    print(f"Go-pins: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
