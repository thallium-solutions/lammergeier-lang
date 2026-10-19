#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path


def _find_project_root(start: Path) -> Path:
    for p in (start, *start.parents):
        if (p / "compiler" / "lammergeier.py").is_file():
            return p
    return start.parent.parent


PROJECT_ROOT = _find_project_root(Path(__file__).resolve())
COMPILER = PROJECT_ROOT / "compiler" / "lammergeier.py"
PYTHON = sys.executable


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run(source: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(COMPILER), str(source), "--emit-go"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _assert_missing_module(source: str, expected: list[str], libs: dict[str, str] | None = None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        if libs:
            for rel, text in libs.items():
                _write(root / rel, text)
        main = root / "main.lam"
        _write(main, source)
        proc = _run(main)
        combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if proc.returncode == 0:
            raise AssertionError("expected missing-module failure but compile succeeded")
        missing = [s for s in expected if s not in combined]
        if missing:
            raise AssertionError(
                "missing expected substrings:\n"
                + "\n".join(f"  - {m!r}" for m in missing)
                + f"\n\nGOT:\n{combined[:1200]}"
            )


def _assert_import_resolution_error(source: str, expected: list[str], libs: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in libs.items():
            _write(root / rel, text)
        main = root / "main.lam"
        _write(main, source)
        proc = _run(main)
        combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if proc.returncode == 0:
            raise AssertionError("expected import-resolution failure but compile succeeded")
        missing = [s for s in expected if s not in combined]
        if missing:
            raise AssertionError(
                "missing expected substrings:\n"
                + "\n".join(f"  - {m!r}" for m in missing)
                + f"\n\nGOT:\n{combined[:1200]}"
            )


def _assert_resolves(source: str, libs: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in libs.items():
            _write(root / rel, text)
        main = root / "main.lam"
        _write(main, source)
        proc = _run(main)
        combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if proc.returncode != 0 or "import resolution failed" in combined:
            raise AssertionError(f"unexpected import-resolution failure:\n{combined}")


def _build(source: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, str(COMPILER), str(source), "-o", str(source.parent / "app")],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )


def _assert_link_clash(source: str, libs: dict[str, str], expected: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in libs.items():
            _write(root / rel, text)
        main = root / "main.lam"
        _write(main, source)
        proc = _build(main)
        combined = (proc.stderr or "") + "\n" + (proc.stdout or "")
        if proc.returncode == 0:
            raise AssertionError("expected a symbol-clash failure but the build succeeded")
        expected = [s.replace("{root}", str(root)) for s in expected]
        missing = [s for s in expected if s not in combined]
        if missing:
            raise AssertionError(
                "missing expected substrings:\n"
                + "\n".join(f"  - {m!r}" for m in missing)
                + f"\n\nGOT:\n{combined[:2000]}"
            )
        if "redeclared in this block" in combined:
            raise AssertionError(f"the raw Go redeclaration error leaked through:\n{combined[:2000]}")


def _assert_builds_and_prints(source: str, libs: dict[str, str], expected_stdout: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in libs.items():
            _write(root / rel, text)
        main = root / "main.lam"
        _write(main, source)
        proc = _build(main)
        if proc.returncode != 0:
            raise AssertionError(f"unexpected build failure:\n{proc.stderr}{proc.stdout}")
        run = subprocess.run([str(root / "app")], capture_output=True, text=True, timeout=30)
        if run.stdout.strip() != expected_stdout:
            raise AssertionError(f"unexpected output {run.stdout!r}, want {expected_stdout!r}")


def test_plain_import_missing_module() -> None:
    _assert_missing_module(
        "import does_not_exist\n\nfunc main() {\n    pass\n}\n",
        [
            "import resolution failed",
            "module `does_not_exist` could not be found",
            "looked for `does_not_exist.lam` or `does_not_exist/__init__.lam`",
            "searched:",
        ],
    )
    print("PASS: plain import missing module")


def test_from_import_missing_module() -> None:
    _assert_missing_module(
        "from no_such_package import Thing\n\nfunc main() {\n    pass\n}\n",
        [
            "import resolution failed",
            "module `no_such_package` could not be found",
            "from no_such_package import Thing",
        ],
    )
    print("PASS: from import missing module")


def test_missing_module_suggestion() -> None:
    _assert_missing_module(
        "from helper_mathh import add\n\nfunc main() {\n    pass\n}\n",
        [
            "module `helper_mathh` could not be found",
            "help: did you mean `helper_math`?",
        ],
        libs={"helper_math.lam": "func add(x: int, y: int) -> int { return x + y }\n"},
    )
    print("PASS: missing module suggestion")


def test_package_init_resolves() -> None:
    _assert_resolves(
        "from packlib import label\n\nfunc main() {\n    print(label())\n}\n",
        {"packlib/__init__.lam": "func label() -> str { return \"ok\" }\n"},
    )
    print("PASS: package __init__ resolves")


def test_from_import_missing_symbol() -> None:
    _assert_import_resolution_error(
        "from helper_math import subtract\n\nfunc main() {\n    pass\n}\n",
        [
            "import resolution failed",
            "module `helper_math` does not export `subtract`",
            "exported by `helper_math`:",
            "- add",
        ],
        {"helper_math.lam": "func add(x: int, y: int) -> int { return x + y }\n"},
    )
    print("PASS: from import missing symbol")


def test_from_import_missing_symbol_suggestion() -> None:
    _assert_import_resolution_error(
        "from helper_math import adder\n\nfunc main() {\n    pass\n}\n",
        [
            "module `helper_math` does not export `adder`",
            "help: did you mean `add`?",
        ],
        {"helper_math.lam": "func add(x: int, y: int) -> int { return x + y }\n"},
    )
    print("PASS: from import missing symbol suggestion")


def test_from_import_missing_symbol_alias() -> None:
    _assert_import_resolution_error(
        "from helper_math import adder as add_alias\n\nfunc main() {\n    pass\n}\n",
        [
            "module `helper_math` does not export `adder` as `add_alias`",
            "help: did you mean `add`?",
        ],
        {"helper_math.lam": "func add(x: int, y: int) -> int { return x + y }\n"},
    )
    print("PASS: from import missing symbol alias")


def test_from_import_valid_symbols_resolve() -> None:
    _assert_resolves(
        "from shapes import Point, origin, SCALE\n\nfunc main() {\n    print(origin())\n}\n",
        {
            "shapes.lam": (
                "const SCALE = 2\n"
                "class Point {\n"
                "    x: int = 0\n"
                "}\n"
                "func origin() -> Point {\n"
                "    return Point()\n"
                "}\n"
            )
        },
    )
    print("PASS: valid imported symbols resolve")


def test_nested_submodule_resolves() -> None:
    _assert_resolves(
        "from lamwebp.codec import decode\n\nfunc main() {\n    print(decode(4))\n}\n",
        {"lamwebp/codec.lam": "func decode(value: int) -> int { return value * 2 }\n"},
    )
    print("PASS: nested submodule resolves")


def test_deep_nested_submodule_resolves() -> None:
    _assert_resolves(
        "from lamwebp.io.files import extension\n\nfunc main() {\n    print(extension())\n}\n",
        {"lamwebp/io/files.lam": "func extension() -> str { return \".webp\" }\n"},
    )
    print("PASS: deeply nested submodule resolves")


def test_nested_package_submodule_resolves() -> None:
    _assert_resolves(
        "from lamwebp.io.files import readWebp\n\nfunc main() {\n    print(readWebp(\"image.webp\"))\n}\n",
        {"lamwebp/io/files/__init__.lam": "func readWebp(path: str) -> str { return path }\n"},
    )
    print("PASS: nested package submodule resolves")


def test_root_package_and_submodule_resolve_together() -> None:
    _assert_resolves(
        "from lamwebp import version\nfrom lamwebp.codec import decode\n\nfunc main() {\n    print(version() + decode())\n}\n",
        {
            "lamwebp/__init__.lam": "func version() -> str { return \"v1\" }\n",
            "lamwebp/codec.lam": "func decode() -> str { return \"-codec\" }\n",
        },
    )
    print("PASS: root package and submodule resolve together")


def test_package_reexports_submodule_symbol() -> None:
    _assert_resolves(
        "from lamwebp import decode\n\nfunc main() {\n    print(decode())\n}\n",
        {
            "lamwebp/__init__.lam": "from lamwebp.codec import decode\n",
            "lamwebp/codec.lam": "func decode() -> str { return \"ok\" }\n",
        },
    )
    print("PASS: package root reexports submodule symbol")


def test_submodule_alias_resolves() -> None:
    _assert_resolves(
        "from lamwebp.codec import decode as readImage\n\nfunc main() {\n    print(readImage())\n}\n",
        {"lamwebp/codec.lam": "func decode() -> str { return \"ok\" }\n"},
    )
    print("PASS: submodule import alias resolves")


def test_submodule_transitive_import_resolves() -> None:
    _assert_resolves(
        "from lamwebp.codec.decoder import decode\n\nfunc main() {\n    print(decode())\n}\n",
        {
            "lamwebp/codec/decoder.lam": (
                "from lamwebp.io.files import suffix\n"
                "func decode() -> str { return \"image\" + suffix() }\n"
            ),
            "lamwebp/io/files.lam": "func suffix() -> str { return \".webp\" }\n",
        },
    )
    print("PASS: transitive submodule import resolves")


def test_nested_submodule_precedes_legacy_dotted_file() -> None:
    _assert_resolves(
        "from lamwebp.codec import decode\n\nfunc main() {\n    print(decode())\n}\n",
        {
            "lamwebp/codec.lam": "func decode() -> str { return \"nested\" }\n",
            "lamwebp.codec.lam": "func legacy() -> str { return \"legacy\" }\n",
        },
    )
    print("PASS: nested submodule precedes legacy dotted file")


def test_legacy_dotted_module_file_fallback() -> None:
    _assert_resolves(
        "from lamwebp.codec import decode\n\nfunc main() {\n    print(decode())\n}\n",
        {"lamwebp.codec.lam": "func decode() -> str { return \"legacy\" }\n"},
    )
    print("PASS: legacy dotted module file remains a fallback")


def test_missing_nested_submodule_lists_nested_paths() -> None:
    _assert_missing_module(
        "from lamwebp.io.files import readWebp\n\nfunc main() {\n    pass\n}\n",
        [
            "module `lamwebp.io.files` could not be found",
            "lamwebp/io/files.lam",
            "lamwebp/io/files/__init__.lam",
            "lamwebp.io.files.lam",
        ],
    )
    print("PASS: missing nested submodule lists nested search paths")


TAG_ALPHA = "func tag() -> str {\n    return \"alpha\"\n}\n"
TAG_BETA = "# beta\nfunc tag() -> str {\n    return \"beta\"\n}\n\nfunc betaName() -> str {\n    return \"beta\"\n}\n"
TAG_GAMMA = "func tag() -> str {\n    return \"gamma\"\n}\n\nfunc gammaName() -> str {\n    return \"gamma\"\n}\n"


def test_namespaced_library_functions() -> None:
    """Formerly a link clash: same-name functions in two libraries are now
    namespaced per module."""
    _assert_builds_and_prints(
        "from alpha import tag\nfrom beta import betaName\nimport beta as b\n\nfunc main() {\n    print(tag() + betaName() + b.tag())\n}\n",
        {"alpha.lam": TAG_ALPHA, "beta.lam": TAG_BETA},
        "alphabetabeta",
    )
    print("PASS: same-name library functions are namespaced")


def test_namespaced_user_class_and_library_class() -> None:
    """Formerly a link clash: a user class and a library class with the same
    name now coexist; the library one is reached through an alias."""
    _assert_builds_and_prints(
        "from settings import load, Config as SettingsConfig\n\nclass Config {\n    func init(self) {\n        self.name: str = \"app\"\n    }\n}\n\n"
        "func main() {\n    c: Config = Config()\n    s: SettingsConfig = SettingsConfig()\n    print(c.name + load() + s.path)\n}\n",
        {"settings.lam": "class Config {\n    func init(self) {\n        self.path: str = \"/etc\"\n    }\n}\n\nfunc load() -> str {\n    return Config().path\n}\n"},
        "app/etc/etc",
    )
    print("PASS: user class and library class with the same name are namespaced")


def test_link_clash_constructor_vs_function() -> None:
    _assert_link_clash(
        "from shapes import Thing\nfrom factory import newThing\n\nfunc main() {\n    print(Thing().size + newThing())\n}\n",
        {
            "shapes.lam": "class Thing {\n    func init(self) {\n        self.size: int = 1\n    }\n}\n",
            "factory.lam": "func newThing() -> int {\n    return 2\n}\n",
        },
        ["Go `func NewThing` is declared by modules `factory` and `shapes`"],
    )
    print("PASS: link clash between a constructor and a function")


def test_link_clash_raw_go_functions() -> None:
    raw = "go! {\n    import (\n        \"strings\"\n    )\n\n    func lamxHelper() string {\n        return strings.ToUpper(\"x\")\n    }\n}\n\nfunc {name}() -> str {\n    out: str = \"\"\n    go! {\n        out = lamxHelper()\n    }\n    return out\n}\n"
    _assert_link_clash(
        "from one import first\nfrom two import second\n\nfunc main() {\n    print(first() + second())\n}\n",
        {"one.lam": raw.replace("{name}", "first"), "two.lam": raw.replace("{name}", "second")},
        [
            "Go `func lamxHelper` is declared by modules `one` and `two`",
            ">>>    6 |     func lamxHelper() string {",
        ],
    )
    print("PASS: link clash between raw go! functions")


def test_namespaced_constants() -> None:
    _assert_builds_and_prints(
        "from one import first\nfrom two import second\n\nfunc main() {\n    print(first() + second())\n}\n",
        {
            "one.lam": "const Version: str = \"1\"\n\nfunc first() -> str {\n    return Version\n}\n",
            "two.lam": "const Version: str = \"2\"\n\nfunc second() -> str {\n    return Version\n}\n",
        },
        "12",
    )
    print("PASS: same-name module constants are namespaced")


def test_namespaced_private_functions() -> None:
    _assert_builds_and_prints(
        "from one import first\nfrom two import second\n\nfunc main() {\n    print(first() + second())\n}\n",
        {
            "one.lam": "private func helper() -> int {\n    return 1\n}\n\nfunc first() -> int {\n    return helper()\n}\n",
            "two.lam": "private func helper() -> int {\n    return 2\n}\n\nfunc second() -> int {\n    return helper()\n}\n",
        },
        "3",
    )
    print("PASS: same-name private functions are namespaced")


def test_namespaced_three_modules() -> None:
    _assert_builds_and_prints(
        "from alpha import tag\nfrom beta import betaName\nfrom gamma import gammaName\n\nfunc main() {\n    print(tag() + betaName() + gammaName())\n}\n",
        {"alpha.lam": TAG_ALPHA, "beta.lam": TAG_BETA, "gamma.lam": TAG_GAMMA},
        "alphabetagamma",
    )
    print("PASS: three same-name definitions are namespaced")


def test_namespaced_package_submodules() -> None:
    _assert_builds_and_prints(
        "from @acme/jwt import sign\nfrom @acme/sftp import serve\nimport @acme/sftp as sftp\n\nfunc main() {\n    print(sign() + serve() + sftp.tag())\n}\n",
        {
            "extlibs/@acme/jwt/__init__.lam": "from .core import sign\n",
            "extlibs/@acme/jwt/core.lam": "func sign() -> str {\n    return \"jwt\" + tag()\n}\n\nfunc tag() -> str {\n    return \"@acme/jwt\"\n}\n",
            "extlibs/@acme/sftp/__init__.lam": "func serve() -> str {\n    return \"sftp\"\n}\n\nfunc tag() -> str {\n    return \"@acme/sftp\"\n}\n",
        },
        "jwt@acme/jwtsftp@acme/sftp",
    )
    print("PASS: same-name functions in scoped package submodules are namespaced")


def test_link_same_method_names_build() -> None:
    _assert_builds_and_prints(
        "from one import A\nfrom two import B\n\nfunc main() {\n    print(A().tag() + B().tag())\n}\n",
        {
            "one.lam": "class A {\n    func init(self) {\n        self.v: int = 1\n    }\n    func tag(self) -> str {\n        return \"a\"\n    }\n}\n",
            "two.lam": "class B {\n    func init(self) {\n        self.v: int = 2\n    }\n    func tag(self) -> str {\n        return \"b\"\n    }\n}\n",
        },
        "ab",
    )
    print("PASS: identical method names on different classes still build")


def test_link_go_init_functions_build() -> None:
    raw = "go! {\n    var {var} = 0\n\n    func init() {\n        {var} = {value}\n    }\n}\n\nfunc {name}() -> int {\n    out: int = 0\n    go! {\n        out = {var}\n    }\n    return out\n}\n"
    _assert_builds_and_prints(
        "from one import first\nfrom two import second\n\nfunc main() {\n    print(first() + second())\n}\n",
        {
            "one.lam": raw.replace("{var}", "lamxOne").replace("{value}", "1").replace("{name}", "first"),
            "two.lam": raw.replace("{var}", "lamxTwo").replace("{value}", "2").replace("{name}", "second"),
        },
        "3",
    )
    print("PASS: go! init functions in several modules still build")


def test_repeated_imports_do_not_duplicate_signatures() -> None:
    """Regression: every ``from store import ...`` line re-registered the
    module's method signatures, so a single-variant method called with an
    ``any`` argument was reported as an ambiguous overload."""
    _assert_resolves(
        "from store import Client\nfrom store import Item\nfrom store import connectStore\n\n"
        "func upload(client: Client, stream: any) -> bool {\n    item: Item = Item()\n    _ = item\n"
        "    return client.putStream(\"k\", stream)\n}\n\nfunc main() {\n    print(upload(connectStore(), None))\n}\n",
        {"store.lam": "class Item {\n    func init(self) {\n        self.key: str = \"\"\n    }\n}\n\n"
                      "class Client {\n    func init(self) {\n        self.n: int = 0\n    }\n\n"
                      "    func putStream(self, key: str, stream: any) -> bool {\n        return key != \"\"\n    }\n}\n\n"
                      "func connectStore() -> Client {\n    return Client()\n}\n"},
    )
    print("PASS: repeated imports do not duplicate method signatures")


def test_imported_class_conversion_dunders() -> None:
    """``int(obj)`` / ``float(obj)`` / ``bool(obj)`` on a class imported from
    another module dispatch to its ``__int__`` / ``__float__`` / ``__bool__``,
    including through a subclass defined in the importing module."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "money.lam", (
            "class Money {\n    func init(self, cents: int) {\n        self.cents: int = cents\n    }\n\n"
            "    func __int__(self) -> int {\n        return self.cents\n    }\n\n"
            "    func __float__(self) -> float {\n        return float(self.cents) / 100.0\n    }\n\n"
            "    func __bool__(self) -> bool {\n        return self.cents > 0\n    }\n}\n"
        ))
        main = root / "main.lam"
        _write(main, (
            "from money import Money\n\nclass Tip(Money) {\n    func init(self, cents: int) {\n"
            "        base.init(cents)\n    }\n}\n\nfunc main() {\n    m: Money = Money(250)\n"
            "    print(int(m), float(m), bool(m), bool(Money(0)), int(Tip(40)))\n}\n"
        ))
        proc = _build(main)
        if proc.returncode != 0:
            raise AssertionError(f"build failed:\n{proc.stderr}")
        out = subprocess.run([str(root / "app")], capture_output=True, text=True, timeout=60).stdout.strip()
        if out != "250 2.5 true false 40":
            raise AssertionError(f"unexpected output: {out!r}")
    print("PASS: imported class conversion dunders")


def _build_and_run(files: dict[str, str], source: str) -> tuple[bool, str]:
    """Build ``main.lam`` next to ``files`` and run it; (built, output-or-error)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in files.items():
            _write(root / rel, text)
        main = root / "main.lam"
        _write(main, source)
        proc = _build(main)
        if proc.returncode != 0:
            return False, (proc.stderr or "") + (proc.stdout or "")
        run = subprocess.run([str(root / "app")], capture_output=True, text=True, timeout=60)
        return True, run.stdout.strip() + (run.stderr.strip() and "\n" + run.stderr.strip())


def _expect_output(files: dict[str, str], source: str, expected: str, label: str) -> None:
    built, out = _build_and_run(files, source)
    if not built:
        raise AssertionError(f"{label}: build failed:\n{out}")
    if out != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {out!r}")
    print(f"PASS: {label}")


def _expect_build_error(files: dict[str, str], source: str, expected: list[str], label: str) -> None:
    built, out = _build_and_run(files, source)
    if built:
        raise AssertionError(f"{label}: expected a compile error, got output {out!r}")
    missing = [text for text in expected if text not in out]
    if missing:
        raise AssertionError(f"{label}: missing {missing!r} in:\n{out[:1500]}")
    print(f"PASS: {label}")


_HELPERS = (
    "const Version: str = \"1.2\"\n\n"
    "class Box {\n    func init(self, v: int = 7) {\n        self.v: int = v\n    }\n\n"
    "    static func make() -> Box {\n        return Box(3)\n    }\n}\n\n"
    "func double(x: int) -> int {\n    return x * 2\n}\n\n"
    "func label(b: Box) -> str {\n    return f\"box{b.v}\"\n}\n"
)


def _tagger(name: str) -> str:
    return (
        f"const Name: str = \"{name}\"\n\n"
        f"func tag() -> str {{\n    return \"{name}:\" + Name\n}}\n\n"
        f"func parse(s: str) -> str {{\n    return \"{name}-str:\" + s\n}}\n\n"
        f"func parse(n: int) -> str {{\n    return f\"{name}-int:{{n}}\"\n}}\n\n"
        f"func describe() -> str {{\n    return tag() + \"/\" + parse(1)\n}}\n"
    )


def test_from_import_aliases() -> None:
    _expect_output(
        {"helpers.lam": _HELPERS},
        "from helpers import double as twice, Box as Crate, Version as V, label as describe\n\n"
        "func main() {\n    c: Crate = Crate(5)\n    other: Crate = Crate.make()\n"
        "    print(twice(4), c.v, other.v, V, describe(c))\n}\n",
        "8 5 3 1.2 box5",
        "from-import aliases for functions, classes, static methods and consts",
    )


def test_module_imports() -> None:
    _expect_output(
        {"helpers.lam": _HELPERS},
        "import helpers\nimport helpers as h\n\n"
        "func build(n: int) -> h.Box {\n    return h.Box(n)\n}\n\n"
        "func main() {\n    b: helpers.Box = helpers.Box()\n"
        "    print(helpers.double(2), h.double(5), b.v, build(9).v, h.Box.make().v, h.Version, h.label(b))\n}\n",
        "4 10 7 9 3 1.2 box7",
        "`import X` / `import X as Y` qualified access",
    )


def test_same_name_functions_in_two_modules() -> None:
    _expect_output(
        {"alpha.lam": _tagger("alpha"), "beta.lam": _tagger("beta")},
        "from alpha import tag, parse, Name\nimport beta as b\n\n"
        "func main() {\n    print(tag(), b.tag())\n    print(parse(\"x\"), b.parse(2))\n"
        "    print(Name, b.Name)\n}\n",
        "alpha:alpha beta:beta\nalpha-str:x beta-int:2\nalpha beta",
        "same-name functions, overloads and consts in two modules are isolated",
    )


def test_local_definition_with_aliased_import() -> None:
    _expect_output(
        {"alpha.lam": _tagger("alpha")},
        "from alpha import tag as alphaTag, Name as AlphaName\n\nconst Name: str = \"main\"\n\n"
        "func tag() -> str {\n    return \"main-tag\"\n}\n\n"
        "func main() {\n    print(tag(), alphaTag(), Name, AlphaName)\n}\n",
        "main-tag alpha:alpha main alpha",
        "local definition and aliased import of the same name",
    )


def test_transitive_same_name_functions() -> None:
    _expect_output(
        {
            "alpha.lam": _tagger("alpha"),
            "beta.lam": _tagger("beta"),
            "wrap_a.lam": "from alpha import describe\n\nfunc a() -> str {\n    return describe()\n}\n",
            "wrap_b.lam": "import beta\n\nfunc b() -> str {\n    return beta.describe()\n}\n",
        },
        "from wrap_a import a\nfrom wrap_b import b\n\nfunc main() {\n    print(a())\n    print(b())\n}\n",
        "alpha:alpha/alpha-int:1\nbeta:beta/beta-int:1",
        "modules never imported by main keep their own same-name functions",
    )


def test_raw_go_reference_to_renamed_function() -> None:
    raw = (
        "go! {\n    import \"strings\"\n}\n\n"
        "func tag() -> str {\n    return \"{kind}\"\n}\n\n"
        "func shout() -> str {\n    out: str = \"\"\n    go! {\n        out = strings.ToUpper(Tag())\n    }\n    return out\n}\n"
    )
    _expect_output(
        {"one.lam": raw.replace("{kind}", "one"), "two.lam": raw.replace("{kind}", "two")},
        "import one\nimport two\n\nfunc main() {\n    print(one.shout(), two.shout())\n}\n",
        "ONE TWO",
        "raw go! code calling its module's renamed function",
    )


def test_raw_go_lam_spelled_function_names() -> None:
    """Raw ``go!`` code (blocks and ``go!(...)`` expressions) may call a
    module function, or a function imported without an alias, by its Lam
    spelling (``tag()``); it lowers to the Go name, renamed module function
    included (``Tag__one``). It used to fail with ``undefined: tag``."""
    raw = (
        "go! {\n    import \"strings\"\n}\n\n"
        "func tag() -> str {\n    return \"{kind}\"\n}\n\n"
        "func shout() -> str {\n    out: str = \"\"\n    go! {\n        out = strings.ToUpper(tag())\n    }\n    return out\n}\n\n"
        "func whisper() -> str {\n    return go!(strings.ToLower(tag()) + \"~\")\n}\n"
    )
    _expect_output(
        {"one.lam": raw.replace("{kind}", "one"), "two.lam": raw.replace("{kind}", "two")},
        "from one import tag, whisper\nimport two\n\n"
        "func main() {\n    mine: str = go!(tag() + \"+\")\n    print(mine, whisper(), two.shout(), two.whisper())\n}\n",
        "one+ one~ TWO two~",
        "raw go! naming module and imported functions by their Lam spelling",
    )


def test_scoped_package_import_requires_alias() -> None:
    files = {
        "extlibs/@acme/one/__init__.lam": _tagger("one"),
        "extlibs/@acme/two/__init__.lam": _tagger("two"),
    }
    _expect_output(
        files,
        "import @acme/one as one\nfrom @acme/two import tag as twoTag\n\n"
        "func main() {\n    print(one.tag(), twoTag(), one.Name)\n}\n",
        "one:one two:two one",
        "`import @scope/pkg as alias` and aliased scoped from-import",
    )
    _expect_build_error(
        files,
        "import @acme/one\n\nfunc main() {\n    print(1)\n}\n",
        ["`import @acme/one` needs an alias", "import @acme/one as one"],
        "scoped package import without `as` is rejected",
    )


def test_local_definition_conflict_suggests_alias() -> None:
    _expect_build_error(
        {"alpha.lam": _tagger("alpha")},
        "from alpha import tag\n\nfunc tag() -> str {\n    return \"main\"\n}\n\nfunc main() {\n    print(tag())\n}\n",
        ["function `tag` conflicts with an imported binding", "from alpha import tag as"],
        "local definition clashing with a plain import suggests `as`",
    )


def test_namespaces_inside_fstrings() -> None:
    _expect_output(
        {"helpers.lam": _HELPERS, "alpha.lam": _tagger("alpha")},
        "from helpers import double as twice, Version as V\nimport alpha as a\n\n"
        "func tag() -> str {\n    return \"main\"\n}\n\n"
        "func main() {\n    print(f\"{twice(3)} {V} {a.tag()} {tag()} {{literal}} {a.Name}\")\n}\n",
        "6 1.2 alpha:alpha main {literal} alpha",
        "aliases and module members inside f-string placeholders",
    )


def _config_module(kind: str) -> str:
    return (
        "go! {\n    import \"strings\"\n}\n\n"
        "interface Named {\n    func name() -> str\n}\n\n"
        f"class Config {{\n    static count: int = 0\n\n    func init(self, label: str = \"{kind}\") {{\n"
        "        self.label: str = label\n        Config.count += 1\n    }\n\n"
        "    func name() -> str {\n        return self.label\n    }\n\n"
        f"    static func make() -> Config {{\n        return Config(\"{kind}-made\")\n    }}\n\n"
        "    func copy(self) -> Config {\n        return Config(self.label + \"+\")\n    }\n}\n\n"
        "func describe(n: Named) -> str {\n    return n.name()\n}\n\n"
        "func shout(c: Config) -> str {\n    out: str = \"\"\n    go! {\n"
        "        fresh := NewConfig(strings.ToUpper(c.Label))\n        out = fresh.Label\n    }\n    return out\n}\n"
    )


def test_namespaced_classes_in_two_modules() -> None:
    _expect_output(
        {"one.lam": _config_module("one"), "two.lam": _config_module("two")},
        "from one import Config, describe, shout\nimport two as t\n\n"
        "class Sub(t.Config) {\n    func init(self) {\n        base.init(\"sub\")\n    }\n}\n\n"
        "func pick(c: t.Config) -> str {\n    return c.label\n}\n\n"
        "func main() {\n    a: Config = Config()\n    b: t.Config = t.Config()\n    s: Sub = Sub()\n"
        "    print(a.label, b.label, Config.make().label, t.Config.make().label, a.copy().label, pick(b))\n"
        "    print(describe(a), t.describe(b), shout(a), t.shout(b), s.label, isinstance(b, t.Config), isinstance(a, t.Config))\n"
        "    print(Config.count, t.Config.count)\n}\n",
        "one two one-made two-made one+ two\none two ONE TWO sub true false\n4 4",
        "same-name classes, interfaces, statics and raw go! in two modules are isolated",
    )


def test_importer_raw_go_follows_renames() -> None:
    _expect_output(
        {"alpha.lam": _tagger("alpha"), "beta.lam": _tagger("beta"),
         "one.lam": _config_module("one"), "two.lam": _config_module("two")},
        "from alpha import tag\nfrom one import Config\nimport beta\nimport two\n\n"
        "func main() {\n    out: str = \"\"\n    go! {\n        c := NewConfig(\"raw\")\n        out = Tag() + \" \" + c.Label\n    }\n"
        "    print(out, beta.tag(), two.Config().label)\n}\n",
        "alpha:alpha raw beta:beta two",
        "raw go! in an importing module follows renamed functions and classes",
    )


def test_library_class_named_like_core_stdlib_class() -> None:
    """A library may define ``Error`` even though lamerrors' ``Error`` (used by
    compiler-generated code) is linked too: the library's class is renamed."""
    _expect_output(
        {"errs.lam": "class Error {\n    func init(self, code: int) {\n        self.code: int = code\n    }\n}\n\n"
                     "func failCode() -> int {\n    return Error(42).code\n}\n"},
        "from errs import failCode\nfrom lamerrors import Result, Error\n\n"
        "func risky() -> Result[int] {\n    return Result.Err(Error(\"Boom\", \"bad\"))\n}\n\n"
        "func main() {\n    r: Result[int] = risky()\n    print(failCode(), r.ok())\n}\n",
        "42 false",
        "library class named like a core stdlib class is namespaced",
    )


def test_main_class_named_like_core_stdlib_class_still_clashes() -> None:
    _assert_link_clash(
        "from lamerrors import Result\n\nclass Error {\n    func init(self) {\n        self.code: int = 1\n    }\n}\n\n"
        "func risky() -> Result[int] {\n    return Result.Ok(Error().code)\n}\n\nfunc main() {\n    print(risky().ok())\n}\n",
        {},
        ["Go `type Error` is declared by modules `main` and `lamerrors`", "help: rename one of the declarations"],
    )
    print("PASS: a main-module class named like lamerrors.Error is still a clash")


def test_imported_function_interface_parameter() -> None:
    """Regression: passing a class to an imported function whose parameter is
    an interface the caller never imported was rejected ("has type `Config`,
    expected `Named`")."""
    lib = (
        "interface Named {\n    func name() -> str\n}\n\n"
        "class Config {\n    func init(self) {\n        self.label: str = \"cfg\"\n    }\n\n"
        "    func name() -> str {\n        return self.label\n    }\n}\n\n"
        "class Holder {\n    func init(self) {\n        self.n: int = 0\n    }\n\n"
        "    func show(self, item: Named) -> str {\n        return \"[\" + item.name() + \"]\"\n    }\n}\n\n"
        "func describe(n: Named) -> str {\n    return n.name()\n}\n"
    )
    _expect_output(
        {"lib1.lam": lib},
        "from lib1 import Config, Holder, describe\nimport lib1 as l\n\n"
        "func main() {\n    print(describe(Config()), l.describe(l.Config()), Holder().show(Config()))\n}\n",
        "cfg cfg [cfg]",
        "imported function/method with an interface parameter accepts a conforming class",
    )


def test_namespaced_module_variables() -> None:
    """Same-name module-level variables in two modules are namespaced; each
    module reads and mutates its own, and ``module.var`` reaches it."""
    lib = (
        "counter: int = {start}\n\n"
        "func bump() -> int {{\n    counter += 1\n    return counter\n}}\n\n"
        "func current() -> int {{\n    return counter\n}}\n"
    )
    _assert_builds_and_prints(
        "import one\nimport two\n\nfunc main() {\n    _ = one.bump()\n    _ = one.bump()\n    _ = two.bump()\n"
        "    print(f\"{one.current()}-{two.current()}-{one.counter}-{two.counter}\")\n}\n",
        {"one.lam": lib.format(start=10), "two.lam": lib.format(start=100)},
        "12-101-12-101",
    )
    print("PASS: same-name module variables are namespaced")


def main() -> None:
    tests = [
        test_plain_import_missing_module,
        test_from_import_missing_module,
        test_missing_module_suggestion,
        test_package_init_resolves,
        test_from_import_missing_symbol,
        test_from_import_missing_symbol_suggestion,
        test_from_import_missing_symbol_alias,
        test_from_import_valid_symbols_resolve,
        test_nested_submodule_resolves,
        test_deep_nested_submodule_resolves,
        test_nested_package_submodule_resolves,
        test_root_package_and_submodule_resolve_together,
        test_package_reexports_submodule_symbol,
        test_submodule_alias_resolves,
        test_submodule_transitive_import_resolves,
        test_nested_submodule_precedes_legacy_dotted_file,
        test_legacy_dotted_module_file_fallback,
        test_missing_nested_submodule_lists_nested_paths,
        test_namespaced_library_functions,
        test_namespaced_user_class_and_library_class,
        test_link_clash_constructor_vs_function,
        test_link_clash_raw_go_functions,
        test_namespaced_constants,
        test_namespaced_private_functions,
        test_namespaced_three_modules,
        test_namespaced_package_submodules,
        test_link_same_method_names_build,
        test_link_go_init_functions_build,
        test_repeated_imports_do_not_duplicate_signatures,
        test_imported_class_conversion_dunders,
        test_from_import_aliases,
        test_module_imports,
        test_same_name_functions_in_two_modules,
        test_local_definition_with_aliased_import,
        test_transitive_same_name_functions,
        test_raw_go_reference_to_renamed_function,
        test_raw_go_lam_spelled_function_names,
        test_scoped_package_import_requires_alias,
        test_local_definition_conflict_suggests_alias,
        test_namespaced_module_variables,
        test_imported_function_interface_parameter,
        test_namespaced_classes_in_two_modules,
        test_importer_raw_go_follows_renames,
        test_library_class_named_like_core_stdlib_class,
        test_main_class_named_like_core_stdlib_class_still_clashes,
        test_namespaces_inside_fstrings,
    ]
    for test in tests:
        test()
    print(f"\nImport resolution results: {len(tests)} passed, 0 failed, {len(tests)} total")


if __name__ == "__main__":
    main()
