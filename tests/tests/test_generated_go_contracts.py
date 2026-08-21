#!/usr/bin/env python3
"""Generated-Go contracts for typed lowering regressions."""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
COMPILER = ROOT / "compiler" / "lammergeier.py"


def _emit_go(source: str, name: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        lam_file = Path(td) / f"{name}.lam"
        lam_file.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(COMPILER), str(lam_file), "--emit-go"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        return result.stdout


def _compile_go(go_source: str, name: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "go.mod").write_text(
            f"module lammergeier_contract_{name}\n\ngo 1.22\n",
            encoding="utf-8",
        )
        (root / "main.go").write_text(go_source, encoding="utf-8")
        formatted = subprocess.run(
            ["gofmt", "-w", "main.go"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert formatted.returncode == 0, formatted.stderr
        built = subprocess.run(
            ["go", "test", "./..."],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert built.returncode == 0, built.stderr + built.stdout


def _compile_and_run_project(main_source: str, libraries: dict[str, str]) -> str:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        main_file = root / "main.lam"
        main_file.write_text(textwrap.dedent(main_source).lstrip("\n"), encoding="utf-8")
        for relative, source in libraries.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        binary = root / "app"
        built = subprocess.run(
            [sys.executable, str(COMPILER), str(main_file), "-o", str(binary)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert built.returncode == 0, built.stderr + built.stdout
        run = subprocess.run(
            [str(binary)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert run.returncode == 0, run.stderr + run.stdout
        return run.stdout.strip()


def _expect_compile_error(main_source: str, libraries: dict[str, str], expected: list[str]) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        main_file = root / "main.lam"
        main_file.write_text(textwrap.dedent(main_source).lstrip("\n"), encoding="utf-8")
        for relative, source in libraries.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(COMPILER), str(main_file), "--emit-go"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        combined = result.stderr + result.stdout
        assert result.returncode != 0, combined
        missing = [text for text in expected if text not in combined]
        assert not missing, f"missing {missing!r}\n{combined}"


def test_typed_anonymous_call_argument_emits_concrete_slice() -> None:
    go = _emit_go(
        """
        func sum(values: list[int]) -> int {
            total: int = 0
            for value in values {
                total += value
            }
            return total
        }

        func main() {
            print(sum([4, 8, 15]))
        }
        """,
        "typed_anonymous_call_argument",
    )
    assert "[]interface{}" not in go
    assert re.search(r"Sum\(\[\]int\{4,\s*8,\s*15\}\)", go), go
    _compile_go(go, "typed_anonymous_call_argument")
    print("PASS: typed anonymous call argument emits concrete Go slice")


def test_contextual_question_mark_argument_is_unboxed_once() -> None:
    go = _emit_go(
        """
        from lamerrors import Result

        func parseInt(s: str) -> Result {
            return Result.Ok(7)
        }

        func wrap(s: str) -> Result {
            return Result.Ok(parseInt(s)?)
        }

        func main() {
            _ = wrap("7")
        }
        """,
        "question_mark_argument",
    )
    assert go.count("ParseInt(s)") == 1, go
    assert re.search(r"Result_Ok\(__q\d+\.Value\)", go), go
    assert not re.search(r"__q\d+\.Value\.\(\*Result\)", go), go
    print("PASS: contextual ? argument is unboxed once without Result cast")


def test_typed_class_field_passes_without_redundant_assertion() -> None:
    go = _emit_go(
        """
        class RetryPolicy {
            func init(self, maxAttempts: int) {
                self.maxAttempts: int = maxAttempts
            }
        }

        class JobRunner {
            func init(self, policy: RetryPolicy) {
                self.policy: RetryPolicy = policy
            }
        }

        class Service {
            func init(self) {
                self.retryPolicy: RetryPolicy = RetryPolicy(3)
            }

            func runner(self) -> JobRunner {
                return JobRunner(self.retryPolicy)
            }
        }

        func main() {
            svc: Service = Service()
            _ = svc.runner()
        }
        """,
        "typed_class_field",
    )
    assert "NewJobRunner(s.RetryPolicy)" in go, go
    assert "s.RetryPolicy.(*RetryPolicy)" not in go, go
    _compile_go(go, "typed_class_field")
    print("PASS: typed class fields pass to constructors without redundant assertions")


def test_inferred_imported_instance_uses_defaults_and_user_dispatch() -> None:
    output = _compile_and_run_project(
        """
        from formatter_pkg import Formatter

        func main() {
            formatter = Formatter("item:")
            clean: str = Formatter.clean(" value ")
            print(formatter.format(clean))
        }
        """,
        {
            "formatter_pkg/__init__.lam": """
                go! {
                    import "strings"
                }

                class Formatter {
                    func init(self, prefix: str) {
                        self.prefix: str = prefix
                    }

                    static func clean(text: str) -> str {
                        go! {
                            return strings.TrimSpace(text)
                        }
                    }

                    func format(self, text: str, upper: bool = false) -> str {
                        if upper {
                            go! {
                                return self.prefix + strings.ToUpper(text)
                            }
                        }
                        return self.prefix + text
                    }
                }
            """,
        },
    )
    assert output == "item:value", output
    print("PASS: inferred imported instances use defaults and user-method dispatch")


def test_imported_result_payload_infers_receiver_and_cast() -> None:
    output = _compile_and_run_project(
        """
        from lamerrors import Result
        from document_pkg import loadDocument

        func title() -> Result[str] {
            document = loadDocument()?
            return Result.Ok(document.render())
        }

        func main() {
            print(title().unwrap())
        }
        """,
        {
            "document_pkg/__init__.lam": """
                from lamerrors import Result

                class Document {
                    func init(self, title: str) {
                        self.title: str = title
                    }

                    func render(self) -> str {
                        return self.title
                    }
                }

                func loadDocument() -> Result[Document] {
                    return Result.Ok(Document("inferred"))
                }
            """,
        },
    )
    assert output == "inferred", output
    print("PASS: imported Result payloads infer receiver classes and Go casts")


def test_lammergeier_rejects_transitive_private_symbols() -> None:
    _expect_compile_error(
        """
        from visibility_helper import shown, Box

        func main() {
            value: str = ""
            go! {
                value = LAMMERGEIER.hidden()
            }
            _ = LAMMERGEIER.Box.make()
            print(shown())
            print(value)
        }
        """,
        {
            "visibility_helper.lam": """
                private func hidden() -> str {
                    return "hidden"
                }

                func shown() -> str {
                    return "shown"
                }

                class Box {
                    private static func make() -> Box {
                        return Box()
                    }
                }
            """,
        },
        [
            "`LAMMERGEIER.hidden` is private and not visible",
            "`LAMMERGEIER.Box.make` is private and not visible",
        ],
    )
    print("PASS: LAMMERGEIER rejects transitive private symbols")


def main() -> int:
    tests = [
        test_typed_anonymous_call_argument_emits_concrete_slice,
        test_contextual_question_mark_argument_is_unboxed_once,
        test_typed_class_field_passes_without_redundant_assertion,
        test_inferred_imported_instance_uses_defaults_and_user_dispatch,
        test_imported_result_payload_infers_receiver_and_cast,
        test_lammergeier_rejects_transitive_private_symbols,
    ]
    for test in tests:
        test()
    print(f"\ngenerated-Go contracts: {len(tests)} passed, 0 failed, {len(tests)} total")
    return 0


if __name__ == "__main__":
    sys.exit(main())
