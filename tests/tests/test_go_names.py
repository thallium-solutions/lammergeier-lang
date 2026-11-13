#!/usr/bin/env python3
"""Unit tests for the Go names of generated declarations.

* ``namespacing.static_renames`` / ``constructor_renames`` /
  ``type_go_names``: a static member ``<Class>_<member>`` or a constructor
  ``New<Class>`` that is another definition's Go name is renamed, the same
  way for every module and whatever the order the modules are listed in;
* ``constants.COMPILER_RUNTIME_HELPERS`` lists every package-level ``lam*``
  Go declaration the compiler emits (the semantic checker rejects Lam names
  that would redeclare one).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from compiler import namespacing  # noqa: E402
from compiler.constants import COMPILER_RUNTIME_HELPER_PREFIXES, COMPILER_RUNTIME_HELPERS  # noqa: E402


def _module(path: str, name: str, symbols: dict, statics: dict | None = None) -> namespacing.ModuleInfo:
    return namespacing.ModuleInfo(
        path=Path(path), name=name, tree=None, symbols=dict(symbols), statics=dict(statics or {}),
    )


def _default_go(name: str, kind: str) -> str:
    if kind == "func":
        return name[:1].upper() + name[1:]
    if kind == "private func":
        return name[:1].lower() + name[1:]
    return name


def _parse(source: str):
    from compiler.lammergeier import create_parser, preprocess_for_parse

    return create_parser().parse(preprocess_for_parse(source).source)


def test_type_go_names_case_only() -> None:
    modules = {Path("/m.lam"): _module("/m.lam", "main", {"site": "class", "Site": "class", "Shape": "interface"})}
    names = namespacing.type_go_names(modules, {})
    assert names[(Path("/m.lam"), "Site")] == "Site"
    assert names[(Path("/m.lam"), "site")] == "Site__lam2"
    assert names[(Path("/m.lam"), "Shape")] == "Shape"
    print("PASS: case-only class names get __lam2 like the transpiler")


def test_constructor_renames_case_only_classes() -> None:
    modules = {Path("/m.lam"): _module("/m.lam", "main", {"site": "class", "Site": "class", "NewSite": "class"})}
    ctors, owners = namespacing.constructor_renames(modules, {}, _default_go)
    # ``site`` is ``Site__lam2`` (constructor ``NewSite__lam2``): no clash.
    assert ctors == {"Site": "NewSite__ctor"}, ctors
    assert owners == {"NewSite": {(Path("/m.lam"), "NewSite")}}
    print("PASS: only the constructor of Site is renamed next to class NewSite")


def test_constructor_renames_never_share_a_name() -> None:
    modules = {
        Path("/a.lam"): _module("/a.lam", "a", {"Site": "class", "NewSite__ctor": "class"}),
        Path("/b.lam"): _module("/b.lam", "b", {"NewSite": "class"}),
    }
    ctors, _owners = namespacing.constructor_renames(modules, {}, _default_go)
    assert ctors == {"Site": "NewSite__ctor2"}, ctors
    print("PASS: a renamed constructor skips taken names")


def test_static_renames_class_function_and_value() -> None:
    renamed: dict = {}
    modules = {
        Path("/m.lam"): _module(
            "/m.lam", "main",
            {"Site": "class", "Site_make": "class", "site_list": "func", "Site_count": "const", "Box": "class"},
            {"Site": {"make": "func", "list": "func", "count": "var", "ok": "func"}, "Box": {"of": "func"}},
        ),
    }
    statics, owners = namespacing.static_renames(modules, renamed, _default_go, {})
    assert statics == {"Site.make": "Site_make__static", "Site.list": "Site_list__static"}, statics
    # The const gives way instead.
    assert renamed == {(Path("/m.lam"), "Site_count"): "Site_count__main"}, renamed
    assert owners["Site_make"] == {(Path("/m.lam"), "Site_make")}
    assert owners["Site_list"] == {(Path("/m.lam"), "site_list")}
    print("PASS: statics give way to classes and functions, consts to statics")


def test_static_renames_static_versus_static_is_order_independent() -> None:
    a = _module("/a.lam", "a", {"A": "class"}, {"A": {"b_c": "func"}})
    b = _module("/b.lam", "b", {"A_b": "class"}, {"A_b": {"c": "func"}})
    first = namespacing.static_renames({a.path: a, b.path: b}, {}, _default_go, {})
    second = namespacing.static_renames({b.path: b, a.path: a}, {}, _default_go, {})
    assert first == second, (first, second)
    assert first[0] == {"A_b.c": "A_b_c__static"}, first
    assert first[1] == {"A_b_c": {(Path("/a.lam"), "A")}}
    print("PASS: between two statics the first module/class keeps the name")


def test_static_renames_private_and_renamed_classes() -> None:
    # ``private static func hidden`` is ``site_hidden``, like a private
    # function ``site_hidden``; the class renamed by namespacing
    # (``Site__lib``) names its statics after the new name.
    renamed = {(Path("/lib.lam"), "Site"): "Site__lib"}
    modules = {
        Path("/lib.lam"): _module("/lib.lam", "lib", {"Site": "class"}, {"Site": {"hidden": "private func", "make": "func"}}),
        Path("/m.lam"): _module("/m.lam", "main", {"site_hidden": "private func", "Site__lib_make": "func"}),
    }
    statics, _owners = namespacing.static_renames(modules, renamed, _default_go, {})
    assert statics == {"Site__lib.make": "Site__lib_make__static"}, statics
    modules[Path("/m.lam")].symbols["site__lib_hidden"] = "private func"
    statics, _owners = namespacing.static_renames(modules, renamed, _default_go, {})
    assert statics == {"Site__lib.hidden": "Site__lib_hidden__static", "Site__lib.make": "Site__lib_make__static"}, statics
    print("PASS: private statics and namespaced classes")


def test_build_plans_static_names_and_go_renames() -> None:
    lib = _module("/lib.lam", "lib", {"Registry": "class"}, {"Registry": {"make": "func", "count": "var"}})
    main = _module("/m.lam", "main", {"Registry_make": "class", "Registry_count": "const"})
    main.bindings = [namespacing.ImportBinding("lib", "Registry", "Registry", False)]
    lib.locally_bound = set()
    main.locally_bound = set()
    lib.member_accesses = {}
    main.member_accesses = {}
    plans = namespacing.build_plans(
        {lib.path: lib, main.path: main}, main.path,
        lambda module, _from: Path("/lib.lam") if module == "lib" else None,
        lambda name, private: _default_go(name, "private func" if private else "func"),
    )
    for plan in plans.values():
        assert plan.static_go_names == {"Registry.make": "Registry_make__static"}, plan.static_go_names
        assert "st:Registry.make>Registry_make__static" in plan.cache_key()
    # Raw ``go!`` code of ``lib`` (which cannot see the class
    # ``Registry_make``) keeps calling ``Registry_make(...)``.
    assert plans[lib.path].go_renames.get("Registry_make") == "Registry_make__static"
    assert "Registry_make" not in plans[main.path].go_renames
    assert plans[main.path].const_go_names == {"Registry_count": "Registry_count__main"}
    print("PASS: plans carry the static renames program-wide")


def test_class_static_members_from_tree() -> None:
    tree = _parse(
        "class Site {\n"
        "    static count: int = 0\n"
        "    private static secret: str = \"s\"\n"
        "    name: str = \"\"\n"
        "    static func make() -> Site {\n        return Site()\n    }\n"
        "    private static func hidden() -> int {\n        return 1\n    }\n"
        "    func url(self) -> str {\n        return self.name\n    }\n"
        "}\n"
        "class Plain {\n    func init(self) {\n        self.x: int = 1\n    }\n}\n"
        "func main() {\n}\n"
    )
    assert namespacing.class_static_members(tree) == {
        "Site": {"count": "var", "secret": "private var", "make": "func", "hidden": "private func"},
    }, namespacing.class_static_members(tree)
    print("PASS: class_static_members reads static methods and fields")


_GO_DECL = re.compile(r"(?:^|[\s\"'(\\]|\\t|\\n)(?:func|type|var|const)\s+(lam[A-Z]\w*)")
_CONST_BLOCK = re.compile(r"\\t(lam[A-Z]\w*)\s+=\s+\d")


def test_runtime_helpers_list_is_complete() -> None:
    found: set[str] = set()
    for path in sorted((ROOT / "compiler").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        found.update(_GO_DECL.findall(text))
        found.update(_CONST_BLOCK.findall(text))
    missing = sorted(
        name for name in found
        if name not in COMPILER_RUNTIME_HELPERS
        and not any(name.startswith(prefix) for prefix in COMPILER_RUNTIME_HELPER_PREFIXES)
    )
    assert not missing, f"add to COMPILER_RUNTIME_HELPERS: {missing}"
    assert {"lamNN", "lamGenRun", "lamGenYield", "lamAs"} <= COMPILER_RUNTIME_HELPERS
    print(f"PASS: {len(found)} generated lam* declarations are reserved")


def main() -> int:
    tests = [
        test_type_go_names_case_only,
        test_constructor_renames_case_only_classes,
        test_constructor_renames_never_share_a_name,
        test_static_renames_class_function_and_value,
        test_static_renames_static_versus_static_is_order_independent,
        test_static_renames_private_and_renamed_classes,
        test_build_plans_static_names_and_go_renames,
        test_class_static_members_from_tree,
        test_runtime_helpers_list_is_complete,
    ]
    for test in tests:
        test()
    print(f"\nGo names: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
