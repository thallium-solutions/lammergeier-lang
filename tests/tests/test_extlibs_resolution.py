#!/usr/bin/env python3
"""Integration tests for the third-party library resolution path.

The compiler looks for ``.lam`` libraries in three layers:

    stdlib  →  extlibs  →  project

where ``extlibs`` itself is assembled from (in priority order)
``--extlibs`` CLI flags, the ``LAMC_EXTLIBS`` env var, a project-local
``extlibs/`` sibling directory, and the user-global
``~/.lammergeier/extlibs`` install path.

This test file exercises each entry point in isolation so regressions
in the resolution order surface with a clear signal.

Run with::

    python3 tests/tests/test_extlibs_resolution.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LAMC = [sys.executable, str(ROOT / "compiler" / "lammergeier.py")]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_lib(dirpath: Path, module_name: str, tag: str) -> Path:
    """Write a tiny library that exports a single ``tag()`` function.

    The function just returns the ``tag`` literal so the caller can
    observe which copy of the library was actually resolved and
    bundled into the final binary.
    """
    lib_path = dirpath / f"{module_name}.lam"
    _write(
        lib_path,
        f"func tag() -> str {{\n    return \"{tag}\"\n}}\n",
    )
    return lib_path


def _make_manifest(dirpath: Path, module_name: str, lamc_range: str) -> Path:
    manifest_path = dirpath / "lamlib.toml"
    _write(
        manifest_path,
        (
            "[library]\n"
            f'name = "{module_name}"\n'
            'version = "0.1.0"\n'
            "\n"
            "[compatibility]\n"
            f'lamc = "{lamc_range}"\n'
        ),
    )
    return manifest_path


def _make_main(dirpath: Path, module_name: str) -> Path:
    main_path = dirpath / "main.lam"
    _write(
        main_path,
        (
            f"from {module_name} import tag\n"
            "\n"
            "func main() {\n"
            "    print(tag())\n"
            "}\n"
        ),
    )
    return main_path


def _run(args: list[str], env_extra: dict[str, str] | None = None,
         cwd: Path | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("LAMC_EXTLIBS", None)  # Tests opt in explicitly.
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        LAMC + list(args),
        cwd=str(cwd or ROOT),
        env=env,
        capture_output=True,
        text=True,
    )


def _assert_output(proc: subprocess.CompletedProcess, expected: str,
                   label: str) -> None:
    if proc.returncode != 0:
        raise AssertionError(
            f"{label}: compile failed (rc={proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    got = proc.stdout.strip()
    if got != expected:
        raise AssertionError(
            f"{label}: expected {expected!r}, got {got!r}\nstderr: {proc.stderr}"
        )


def test_cli_flag_resolves_extlib() -> None:
    """``--extlibs DIR`` alone is enough to resolve a third-party module."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        extlibs_dir = tmp_path / "shared-libs"
        _make_lib(extlibs_dir, "demo_widget", "from-cli-flag")
        main = _make_main(tmp_path, "demo_widget")
        proc = _run(["--run", "--extlibs", str(extlibs_dir), str(main)])
        _assert_output(proc, "from-cli-flag", "cli flag")
    print("PASS: --extlibs flag resolves a third-party module")


def test_env_var_resolves_extlib() -> None:
    """``LAMC_EXTLIBS`` accepts colon-separated directories."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        _make_lib(b_dir, "demo_widget", "from-env-b")
        main = _make_main(tmp_path, "demo_widget")
        proc = _run(
            ["--run", str(main)],
            env_extra={"LAMC_EXTLIBS": f"{a_dir}{os.pathsep}{b_dir}"},
        )
        _assert_output(proc, "from-env-b", "LAMC_EXTLIBS env var")
    print("PASS: LAMC_EXTLIBS resolves a third-party module")


def test_project_extlibs_subdir_resolves() -> None:
    """A ``extlibs/`` sibling of the source file is auto-discovered."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_lib(tmp_path / "extlibs", "demo_widget", "from-project-extlibs")
        main = _make_main(tmp_path, "demo_widget")
        proc = _run(["--run", str(main)])
        _assert_output(proc, "from-project-extlibs", "project-local extlibs/")
    print("PASS: <project>/extlibs/ is auto-discovered")


def test_cli_flag_beats_env_var() -> None:
    """``--extlibs`` entries win over ``LAMC_EXTLIBS`` (same precedence
    as the docstring on ``compile_tpy.extlibs_paths``)."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cli_dir = tmp_path / "cli"
        env_dir = tmp_path / "env"
        _make_lib(cli_dir, "demo_widget", "cli-wins")
        _make_lib(env_dir, "demo_widget", "env-loses")
        main = _make_main(tmp_path, "demo_widget")
        proc = _run(
            ["--run", "--extlibs", str(cli_dir), str(main)],
            env_extra={"LAMC_EXTLIBS": str(env_dir)},
        )
        _assert_output(proc, "cli-wins", "--extlibs beats LAMC_EXTLIBS")
    print("PASS: --extlibs beats LAMC_EXTLIBS")


def test_extlibs_beats_project_local() -> None:
    """Extlibs takes priority over the source file's own directory
    (and its ``lib/`` subdir), per the stdlib → extlibs → project
    order."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # Same-name library in BOTH places; extlibs should win.
        _make_lib(tmp_path / "ext", "demo_widget", "extlibs-wins")
        _make_lib(tmp_path / "lib", "demo_widget", "project-loses")
        main = _make_main(tmp_path, "demo_widget")
        proc = _run(["--run", "--extlibs", str(tmp_path / "ext"), str(main)])
        _assert_output(proc, "extlibs-wins", "extlibs beats project lib/")
    print("PASS: extlibs beats project-local lib/")


def test_project_local_fallback_still_works() -> None:
    """When a module is only available project-locally, the compiler
    still finds it through the project fallback layer."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _make_lib(tmp_path, "only_local", "project-only")
        main = _make_main(tmp_path, "only_local")
        proc = _run(["--run", str(main)])
        _assert_output(proc, "project-only", "project fallback")
    print("PASS: project-local libraries still resolve without extlibs")


def test_missing_extlib_fails_clearly() -> None:
    """A reference to a module that doesn't exist in any layer should
    fail the build; this keeps us from silently dropping unresolved
    imports."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        main = _make_main(tmp_path, "nope_not_here")
        proc = _run(["--run", str(main)])
        if proc.returncode == 0:
            raise AssertionError(
                "missing extlib: expected non-zero exit; got clean compile\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
    print("PASS: unknown module yields a build failure")


def test_extlib_in_unrelated_directory() -> None:
    """Real-world scenario: a user ships a custom library at an
    arbitrary absolute path (think ``/opt/shared-lams/…`` or a sibling
    repo) and points ``--extlibs`` / ``LAMC_EXTLIBS`` at it. The
    project directory and the library directory are *completely
    separate* — neither is a child of the other.
    """
    with tempfile.TemporaryDirectory() as project_tmp, \
         tempfile.TemporaryDirectory() as library_tmp:
        project_dir = Path(project_tmp) / "my_app" / "src"
        library_dir = Path(library_tmp) / "third_party" / "v1"
        project_dir.mkdir(parents=True, exist_ok=True)
        library_dir.mkdir(parents=True, exist_ok=True)

        _make_lib(library_dir, "greet_kit", "hello from elsewhere")
        main = _make_main(project_dir, "greet_kit")

        # ``--extlibs`` form.
        proc = _run(["--run", "--extlibs", str(library_dir), str(main)])
        _assert_output(proc, "hello from elsewhere",
                       "unrelated directory via --extlibs")

        # ``LAMC_EXTLIBS`` form — same result.
        proc = _run(
            ["--run", str(main)],
            env_extra={"LAMC_EXTLIBS": str(library_dir)},
        )
        _assert_output(proc, "hello from elsewhere",
                       "unrelated directory via LAMC_EXTLIBS")
    print("PASS: custom library in a completely separate directory resolves")


def test_extlib_with_stdlib_dependency() -> None:
    """A custom library can itself import from the stdlib — the
    compiler has to follow the extlib's own ``from lamXxx import …``
    statements and pull those in too. This covers the common case of
    shipping a helper library that depends on e.g. ``lamstrings``.
    """
    with tempfile.TemporaryDirectory() as project_tmp, \
         tempfile.TemporaryDirectory() as library_tmp:
        project_dir = Path(project_tmp)
        library_dir = Path(library_tmp)

        # Library uses stdlib internally.
        _write(
            library_dir / "greet_kit.lam",
            (
                "from lamstrings import Strings\n"
                "\n"
                "func shout(name: str) -> str {\n"
                "    return Strings.toUpper(\"hi \" + name)\n"
                "}\n"
            ),
        )
        _write(
            project_dir / "main.lam",
            (
                "from greet_kit import shout\n"
                "\n"
                "func main() {\n"
                "    print(shout(\"ada\"))\n"
                "}\n"
            ),
        )
        proc = _run([
            "--run", "--extlibs", str(library_dir),
            str(project_dir / "main.lam"),
        ])
        _assert_output(proc, "HI ADA",
                       "extlib transitively importing stdlib")
    print("PASS: extlib with stdlib dependency builds and runs")


def test_user_global_extlibs_fallback() -> None:
    """``~/.lammergeier/extlibs`` is the last-resort lookup before the
    build fails. Simulate it by pointing ``HOME`` at a sandbox so the
    real one is never touched. We forward the parent's ``PYTHONPATH``
    so the compiler still finds its own user-site dependencies (lark,
    etc.) even though ``HOME`` was redirected.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake_home = tmp_path / "fake-home"
        user_extlibs = fake_home / ".lammergeier" / "extlibs"
        _make_lib(user_extlibs, "user_global_widget", "from-user-home")

        project_dir = tmp_path / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        main = _make_main(project_dir, "user_global_widget")

        # Point Python at the real user site-packages so lark stays
        # importable after we redirect HOME.
        import site
        existing_pp = os.environ.get("PYTHONPATH", "")
        forwarded_pp = os.pathsep.join(
            p for p in (existing_pp, *site.getsitepackages(),
                        site.getusersitepackages()) if p
        )

        # Redirect Go's caches into the sandbox too — a fresh ``HOME``
        # otherwise traps the build trying to read ``~/.cache/go-build``.
        proc = _run(
            ["--run", str(main)],
            env_extra={
                "HOME": str(fake_home),
                "PYTHONPATH": forwarded_pp,
                "GOCACHE":   str(fake_home / ".cache" / "go-build"),
                "GOMODCACHE": str(fake_home / "go" / "pkg" / "mod"),
                "GOPATH":    str(fake_home / "go"),
            },
        )
        _assert_output(proc, "from-user-home", "user-global ~/.lammergeier/extlibs")
    print("PASS: ~/.lammergeier/extlibs fallback resolves custom libraries")


def test_multiple_extlibs_dirs_in_order() -> None:
    """When multiple ``--extlibs`` entries are given, earlier ones
    shadow later ones — mirrors PATH-style lookup semantics.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        first = tmp_path / "first"
        second = tmp_path / "second"
        _make_lib(first,  "demo_widget", "first-wins")
        _make_lib(second, "demo_widget", "second-loses")

        main = _make_main(tmp_path, "demo_widget")
        proc = _run([
            "--run",
            "--extlibs", str(first),
            "--extlibs", str(second),
            str(main),
        ])
        _assert_output(proc, "first-wins",
                       "leftmost --extlibs wins over later ones")
    print("PASS: leftmost --extlibs directory takes priority")


def test_incompatible_extlib_lamc_range_warns_without_blocking() -> None:
    """An installed library can declare a compiler compatibility range.
    Compile should warn on mismatches, but keep building so old projects
    are not locked out of using their dependencies.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        extlibs_dir = tmp_path / "ext"
        _make_lib(extlibs_dir, "future_widget", "future-ok")
        _make_manifest(extlibs_dir, "future_widget", ">=999.0.0")
        main = _make_main(tmp_path, "future_widget")

        proc = _run(["--run", "--extlibs", str(extlibs_dir), str(main)])
        _assert_output(proc, "future-ok", "incompatible compatibility.lamc warning")
        if "warning: library future_widget@0.1.0 declares compatibility.lamc" not in proc.stderr:
            raise AssertionError(
                "expected compatibility warning in stderr\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
    print("PASS: incompatible compatibility.lamc warns but does not block")


def main() -> int:
    tests = [
        test_cli_flag_resolves_extlib,
        test_env_var_resolves_extlib,
        test_project_extlibs_subdir_resolves,
        test_cli_flag_beats_env_var,
        test_extlibs_beats_project_local,
        test_project_local_fallback_still_works,
        test_missing_extlib_fails_clearly,
        test_extlib_in_unrelated_directory,
        test_extlib_with_stdlib_dependency,
        test_user_global_extlibs_fallback,
        test_multiple_extlibs_dirs_in_order,
        test_incompatible_extlib_lamc_range_warns_without_blocking,
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
        print(f"Extlibs resolution: {failures} of {len(tests)} tests failed")
        return 1
    print(f"Extlibs resolution: {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
