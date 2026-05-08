#!/usr/bin/env python3
"""Black-box test harness for the Lammergeier LSP.

Spawns ``compiler/lsp.py`` as a subprocess, talks JSON-RPC over its
stdio, and asserts on every supported request. Exits non-zero (and
prints a summary) on the first failure so it slots into the existing
CI pipeline.

Run with:

    python3 tests/lsp/test_lsp.py

The whole harness is intentionally pure-stdlib so it runs anywhere
the regular test suite does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
LSP_SCRIPT = PROJECT_ROOT / "compiler" / "lsp.py"


def _frame(msg: Dict[str, Any]) -> bytes:
    body = json.dumps(msg).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


class LspClient:
    """Minimal LSP client used only by these tests."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(LSP_SCRIPT)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
        self._next_id = 1

    def _send(self, msg: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(_frame(msg))
        self.proc.stdin.flush()

    def request(self, method: str, params: Optional[Dict[str, Any]] = None,
                 timeout: float = 8.0) -> Dict[str, Any]:
        msg_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": msg_id, "method": method,
                    "params": params or {}})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._recv(timeout=deadline - time.time())
            if msg is None:
                continue
            if msg.get("id") == msg_id:
                return msg
        raise TimeoutError(f"no response to {method} within {timeout}s")

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def _recv(self, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
        assert self.proc.stdout is not None
        # Read headers
        deadline = time.time() + timeout
        headers = b""
        while True:
            ch = self.proc.stdout.read(1)
            if not ch:
                return None
            headers += ch
            if headers.endswith(b"\r\n\r\n"):
                break
            if time.time() > deadline:
                return None
        # Parse Content-Length
        length = 0
        for line in headers.decode("ascii").split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":", 1)[1].strip())
        body = b""
        while len(body) < length:
            chunk = self.proc.stdout.read(length - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body.decode("utf-8"))

    def collect_notifications(self, method: str, n: int = 1,
                               timeout: float = 3.0) -> List[Dict[str, Any]]:
        """Read messages until ``n`` notifications of ``method`` arrive."""
        out: List[Dict[str, Any]] = []
        deadline = time.time() + timeout
        while len(out) < n and time.time() < deadline:
            msg = self._recv(timeout=deadline - time.time())
            if msg is None:
                break
            if msg.get("method") == method:
                out.append(msg)
        return out

    def shutdown(self) -> None:
        try:
            self.request("shutdown")
            self.notify("exit")
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


# ────────────────────────────────────────────────────────────
# Test cases
# ────────────────────────────────────────────────────────────

VALID_DOC = """class Greeter {
    func __init__(self, name: str) {
        self.name = name
    }

    func greet(self) -> str {
        return "hello " + self.name
    }

    static func global_(prefix: str) -> str {
        return prefix + ": world"
    }
}

func helper(x: int) -> int {
    return x * 2
}

func main() {
    g: Greeter = Greeter("ada")
    print(g.greet())
    print(helper(21))
}
"""

WARNING_DOC = """func main() {
    value: int = 1;
    print("hi");
}
"""

INVALID_DOC = """func main() {
    if oops_unbalanced_paren( {
        print("nope")
    }
}
"""

# Document that imports from the bundled stdlib so we can exercise
# cross-file hover / completion / go-to-definition. ``Dotenv``,
# ``Config``, and ``basicAuth`` are real public names in
# ``lib/lamenv.lam`` and ``lib/lamserver_plugins.lam``.
CROSS_DOC = """from lamenv import Dotenv, Config
from lamserver_plugins import basicAuth

func main() {
    env: Dotenv = Dotenv.parse(\"K=v\")
    print(env.get(\"K\"))
}
"""

USER_HELPER_DOC = """func double(x: int) -> int {
    return x * 2
}
"""

USER_MAIN_DOC = """from helper import double

func main() {
    print(double(21))
}
"""

REFERENCES_DOC = """func target(x: int) -> int {
    return x
}

func main() {
    a: int = target(1)
    b: int = target(a)
    print(b)
}
"""

GENERIC_DOC = """func identity[T](x: T) -> T {
    return x
}

class Box[T] {
    func get(self) -> T {
        return self.value
    }
}
"""

SIGNATURE_DOC = """func combine(left: str, right: str) -> str {
    return left + right
}

func main() {
    print(combine("a", "b"))
}
"""

RENAME_DOC = """func compute(value: int) -> int {
    total: int = value + 1
    return total
}

func main() {
    print(compute(41))
}
"""

FORMAT_DOC = """func main(){
value:int=1
if value>0{
print("x:y", value)# inline
}
}
"""

FORMAT_EXPECTED = """func main() {
    value: int = 1
    if value > 0 {
        print("x:y", value)  # inline
    }
}
"""


def assert_eq(label: str, actual, expected) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, got {actual!r}")


def assert_true(label: str, actual) -> None:
    if not actual:
        raise AssertionError(f"{label}: expected truthy, got {actual!r}")


def run_tests() -> int:
    client = LspClient()
    failures: List[str] = []

    try:
        # ── initialize ──────────────────────────────────────
        resp = client.request("initialize", {
            "processId": os.getpid(),
            "rootUri": f"file://{PROJECT_ROOT}",
            "capabilities": {},
        })
        caps = resp.get("result", {}).get("capabilities", {})
        try:
            assert_true("hoverProvider", caps.get("hoverProvider"))
            assert_true("definitionProvider", caps.get("definitionProvider"))
            assert_true("documentSymbolProvider", caps.get("documentSymbolProvider"))
            assert_true("signatureHelpProvider", isinstance(caps.get("signatureHelpProvider"), dict))
            assert_true("referencesProvider", caps.get("referencesProvider"))
            assert_true("renameProvider", isinstance(caps.get("renameProvider"), dict))
            assert_true("rename prepareProvider", caps.get("renameProvider", {}).get("prepareProvider"))
            assert_true("documentFormattingProvider", caps.get("documentFormattingProvider"))
            assert_true("completionProvider", isinstance(caps.get("completionProvider"), dict))
            print("PASS: initialize advertises capabilities")
        except AssertionError as e:
            failures.append(str(e))

        client.notify("initialized")

        # ── didOpen (valid doc) — expect empty diagnostics ──
        uri = "file:///tmp/lsp_test.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri, "languageId": "lammergeier",
                "version": 1, "text": VALID_DOC,
            },
        })
        notes = client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=4)
        try:
            assert_true("got publishDiagnostics on open", notes)
            params = notes[0].get("params", {})
            assert_eq("clean diagnostics for valid doc",
                      params.get("diagnostics"), [])
            print("PASS: clean document yields empty diagnostics")
        except AssertionError as e:
            failures.append(str(e))

        # ── didOpen valid dict destructuring syntax ─────────
        dict_uri = "file:///tmp/lsp_dict_destructure.lam"
        dict_doc = (PROJECT_ROOT / "tests" / "tests" / "cases" / "test_dict_destructure.lam").read_text(encoding="utf-8")
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": dict_uri, "languageId": "lammergeier",
                "version": 1, "text": dict_doc,
            },
        })
        notes = client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=4)
        try:
            assert_true("got dict destructure diagnostics notification", notes)
            params = notes[0].get("params", {})
            assert_eq("dict destructure has no diagnostics",
                      params.get("diagnostics"), [])
            print("PASS: dict destructuring syntax is accepted by LSP")
        except AssertionError as e:
            failures.append(str(e))

        # ── didOpen with semantic warning ───────────────────
        warn_uri = "file:///tmp/lsp_warning.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": warn_uri, "languageId": "lammergeier",
                "version": 1, "text": WARNING_DOC,
            },
        })
        notes = client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=4)
        try:
            assert_true("got warning diagnostics on open", notes)
            diags = notes[0].get("params", {}).get("diagnostics", [])
            assert_true("at least one semantic warning", diags)
            assert_eq("warning severity",
                      diags[0].get("severity"), 2)
            assert_true("warning message",
                        "unused local `value`" in diags[0].get("message", ""))
            print("PASS: semantic warnings publish as LSP warning diagnostics")
        except AssertionError as e:
            failures.append(str(e))

        references_uri = "file:///tmp/lsp_references.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": references_uri, "languageId": "lammergeier",
                "version": 1, "text": REFERENCES_DOC,
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        ref_lines = REFERENCES_DOC.splitlines()
        target_line = next(i for i, l in enumerate(ref_lines) if "func target" in l)
        target_col = ref_lines[target_line].index("target") + 1
        resp = client.request("textDocument/references", {
            "textDocument": {"uri": references_uri},
            "position": {"line": target_line, "character": target_col},
            "context": {"includeDeclaration": True},
        })
        refs = resp.get("result") or []
        try:
            ref_positions = {
                (loc["uri"], loc["range"]["start"]["line"], loc["range"]["start"]["character"])
                for loc in refs
            }
            expected = {
                (references_uri, target_line, ref_lines[target_line].index("target")),
                (references_uri, next(i for i, l in enumerate(ref_lines) if "target(1)" in l),
                 next(l.index("target") for l in ref_lines if "target(1)" in l)),
                (references_uri, next(i for i, l in enumerate(ref_lines) if "target(a)" in l),
                 next(l.index("target") for l in ref_lines if "target(a)" in l)),
            }
            assert_eq("same-file reference count", len(refs), 3)
            assert_eq("same-file reference locations", ref_positions, expected)
            print("PASS: references finds definition and two same-file usages")
        except AssertionError as e:
            failures.append(f"references same-file: {e}")

        rename_uri = "file:///tmp/lsp_rename.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": rename_uri, "languageId": "lammergeier",
                "version": 1, "text": RENAME_DOC,
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        rename_lines = RENAME_DOC.splitlines()
        local_line = next(i for i, l in enumerate(rename_lines) if "return total" in l)
        local_col = rename_lines[local_line].index("total") + 1
        resp = client.request("textDocument/prepareRename", {
            "textDocument": {"uri": rename_uri},
            "position": {"line": local_line, "character": local_col},
        })
        prep = resp.get("result") or {}
        resp = client.request("textDocument/rename", {
            "textDocument": {"uri": rename_uri},
            "position": {"line": local_line, "character": local_col},
            "newName": "answer",
        })
        edits = ((resp.get("result") or {}).get("changes") or {}).get(rename_uri, [])
        try:
            edit_positions = {
                (edit["range"]["start"]["line"], edit["range"]["start"]["character"], edit["newText"])
                for edit in edits
            }
            assert_eq("prepareRename placeholder for local", prep.get("placeholder"), "total")
            assert_eq("local rename edit count", len(edits), 2)
            assert_true("local rename edits declaration",
                        (1, rename_lines[1].index("total"), "answer") in edit_positions)
            assert_true("local rename edits use",
                        (local_line, rename_lines[local_line].index("total"), "answer") in edit_positions)
            print("PASS: rename edits a local variable in the current function")
        except AssertionError as e:
            failures.append(f"rename local: {e}")

        func_line = next(i for i, l in enumerate(rename_lines) if "func compute" in l)
        func_col = rename_lines[func_line].index("compute") + 1
        resp = client.request("textDocument/rename", {
            "textDocument": {"uri": rename_uri},
            "position": {"line": func_line, "character": func_col},
            "newName": "calculate",
        })
        edits = ((resp.get("result") or {}).get("changes") or {}).get(rename_uri, [])
        try:
            edit_positions = {
                (edit["range"]["start"]["line"], edit["range"]["start"]["character"], edit["newText"])
                for edit in edits
            }
            call_line = next(i for i, l in enumerate(rename_lines) if "compute(41)" in l)
            assert_eq("top-level rename edit count", len(edits), 2)
            assert_true("top-level rename edits declaration",
                        (func_line, rename_lines[func_line].index("compute"), "calculate") in edit_positions)
            assert_true("top-level rename edits call",
                        (call_line, rename_lines[call_line].index("compute"), "calculate") in edit_positions)
            print("PASS: rename edits a top-level function in one file")
        except AssertionError as e:
            failures.append(f"rename top-level: {e}")

        format_uri = "file:///tmp/lsp_format.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": format_uri, "languageId": "lammergeier",
                "version": 1, "text": FORMAT_DOC,
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        resp = client.request("textDocument/formatting", {
            "textDocument": {"uri": format_uri},
            "options": {"tabSize": 4, "insertSpaces": True},
        })
        edits = resp.get("result") or []
        try:
            assert_eq("formatting edit count", len(edits), 1)
            assert_eq("formatting edit text", edits[0].get("newText"), FORMAT_EXPECTED)
            assert_eq("formatting edit starts at document start",
                      edits[0].get("range", {}).get("start"), {"line": 0, "character": 0})
            print("PASS: LSP formatting returns a text edit")
        except AssertionError as e:
            failures.append(f"formatting: {e}")

        # ── documentSymbol ──────────────────────────────────
        resp = client.request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        })
        symbols = resp.get("result", [])
        try:
            names = {s["name"] for s in symbols}
            assert_true("Greeter symbol", "Greeter" in names)
            assert_true("helper symbol", "helper" in names)
            assert_true("main symbol", "main" in names)
            greeter = next(s for s in symbols if s["name"] == "Greeter")
            child_names = {c["name"] for c in greeter.get("children", [])}
            assert_true("greet method nested under Greeter",
                        "greet" in child_names)
            print("PASS: documentSymbol returns a class with nested methods")
        except (AssertionError, StopIteration) as e:
            failures.append(f"documentSymbol: {e}")

        generic_uri = "file:///tmp/lsp_generics.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": generic_uri, "languageId": "lammergeier",
                "version": 1, "text": GENERIC_DOC,
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        resp = client.request("textDocument/documentSymbol", {
            "textDocument": {"uri": generic_uri},
        })
        symbols = resp.get("result", [])
        try:
            names = {s["name"] for s in symbols}
            assert_true("generic function symbol", "identity" in names)
            assert_true("generic class symbol", "Box" in names)
            box = next(s for s in symbols if s["name"] == "Box")
            child_names = {c["name"] for c in box.get("children", [])}
            assert_true("generic class method symbol", "get" in child_names)
            print("PASS: documentSymbol includes generic function and class")
        except (AssertionError, StopIteration) as e:
            failures.append(f"generic documentSymbol: {e}")

        # ── hover on `helper` ───────────────────────────────
        # Find "helper" in the source.
        for line_idx, line in enumerate(VALID_DOC.splitlines()):
            col = line.find("helper(21)")
            if col >= 0:
                resp = client.request("textDocument/hover", {
                    "textDocument": {"uri": uri},
                    "position": {"line": line_idx, "character": col + 1},
                })
                try:
                    contents = (resp.get("result") or {}).get("contents") or {}
                    val = contents.get("value", "") if isinstance(contents, dict) else ""
                    assert_true("hover contains 'helper'", "helper" in val)
                    assert_true("hover shows type info", "int" in val)
                    print("PASS: hover returns signature for top-level function")
                except AssertionError as e:
                    failures.append(str(e))
                break

        signature_uri = "file:///tmp/lsp_signature.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": signature_uri, "languageId": "lammergeier",
                "version": 1, "text": SIGNATURE_DOC,
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        sig_lines = SIGNATURE_DOC.splitlines()
        sig_line = next(i for i, l in enumerate(sig_lines) if "combine(\"a\"" in l)
        first_param_char = sig_lines[sig_line].index('combine("a"') + len('combine(')
        resp = client.request("textDocument/signatureHelp", {
            "textDocument": {"uri": signature_uri},
            "position": {"line": sig_line, "character": first_param_char},
        })
        try:
            result = resp.get("result") or {}
            signatures = result.get("signatures") or []
            assert_true("signature help result", signatures)
            assert_true("signature label", "func combine(left: str, right: str) -> str" in signatures[0].get("label", ""))
            assert_eq("active parameter before comma", result.get("activeParameter"), 0)
            params = signatures[0].get("parameters") or []
            assert_eq("first parameter label", params[0].get("label"), "left: str")
            print("PASS: signatureHelp returns function signature inside a call")
        except (AssertionError, IndexError) as e:
            failures.append(f"signatureHelp: {e}")

        second_param_char = sig_lines[sig_line].index(', "b"') + len(', ')
        resp = client.request("textDocument/signatureHelp", {
            "textDocument": {"uri": signature_uri},
            "position": {"line": sig_line, "character": second_param_char},
        })
        try:
            result = resp.get("result") or {}
            signatures = result.get("signatures") or []
            assert_true("signature help result after comma", signatures)
            assert_eq("active parameter after comma", result.get("activeParameter"), 1)
            params = signatures[0].get("parameters") or []
            assert_eq("second parameter label", params[1].get("label"), "right: str")
            print("PASS: signatureHelp advances active parameter after comma")
        except (AssertionError, IndexError) as e:
            failures.append(f"signatureHelp active parameter: {e}")

        # ── completion at top level ─────────────────────────
        resp = client.request("textDocument/completion", {
            "textDocument": {"uri": uri},
            "position": {"line": 0, "character": 0},
        })
        items = (resp.get("result") or {}).get("items", [])
        try:
            labels = {it.get("label") for it in items}
            assert_true("completion has helper", "helper" in labels)
            assert_true("completion has Greeter", "Greeter" in labels)
            assert_true("completion has 'func' keyword", "func" in labels)
            print("PASS: completion lists user functions and classes")
        except AssertionError as e:
            failures.append(str(e))

        # ── completion lists variables + parameters (scope-aware) ──
        # Build a doc where the cursor sits inside a function body so
        # the LSP should surface the function's locals + params AND
        # the module-level variables.
        vars_uri = "file:///tmp/lsp_vars.lam"
        vars_doc = (
            "const PI: float = 3.14\n"                 # 0
            "topName: str = \"alice\"\n"              # 1
            "\n"                                        # 2
            "func greet(name: str) -> str {\n"         # 3
            "    counter: int = 0\n"                  # 4
            "    prefix: str = \"hi \"\n"             # 5
            "    \n"                                    # 6
            "    return prefix + name\n"                # 7
            "}\n"                                        # 8
        )
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": vars_uri, "languageId": "lammergeier",
                "version": 1, "text": vars_doc,
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        resp = client.request("textDocument/completion", {
            "textDocument": {"uri": vars_uri},
            "position": {"line": 6, "character": 4},  # blank line inside greet()
        })
        items = (resp.get("result") or {}).get("items", [])
        labels = {it.get("label") for it in items}
        try:
            assert_true("local 'counter' in completion",  "counter" in labels)
            assert_true("local 'prefix' in completion",  "prefix" in labels)
            assert_true("param 'name' in completion",     "name" in labels)
            assert_true("top-level const 'PI' in completion", "PI" in labels)
            assert_true("top-level var 'topName' in completion",
                         "topName" in labels)
            assert_true("top-level func 'greet' in completion",
                         "greet" in labels)
            print("PASS: completion lists variables, parameters, and top-level names")
        except AssertionError as e:
            failures.append(str(e))

        # ── completion after `Greeter.` shows static methods ──
        resp = client.request("textDocument/completion", {
            "textDocument": {"uri": uri},
            # End of line: insert imaginary ``Greeter.`` at top.
            "position": {"line": 0, "character": 0},
        })
        # Re-do with a doc that triggers the dot-completion code path.
        dotted_uri = "file:///tmp/lsp_dot.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": dotted_uri, "languageId": "lammergeier",
                "version": 1,
                "text": VALID_DOC + "\nGreeter.\n",
            },
        })
        # Drain the diagnostics notification before asking for completion.
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        last_line = (VALID_DOC + "\nGreeter.\n").splitlines().index("Greeter.")
        resp = client.request("textDocument/completion", {
            "textDocument": {"uri": dotted_uri},
            "position": {"line": last_line, "character": len("Greeter.")},
        })
        items = (resp.get("result") or {}).get("items", [])
        try:
            labels = {it.get("label") for it in items}
            assert_true("static method 'global_' in completion after Greeter.",
                        "global_" in labels)
            print("PASS: dot-completion lists class members")
        except AssertionError as e:
            failures.append(str(e))

        # ── definition jumps to declaration ─────────────────
        for line_idx, line in enumerate(VALID_DOC.splitlines()):
            col = line.find("helper(21)")
            if col >= 0:
                resp = client.request("textDocument/definition", {
                    "textDocument": {"uri": uri},
                    "position": {"line": line_idx, "character": col + 1},
                })
                loc = resp.get("result")
                try:
                    assert_true("definition non-null", loc)
                    assert_eq("definition URI", loc.get("uri"), uri)
                    # Helper is declared on a known line — find it.
                    helper_line = next(i for i, l in enumerate(VALID_DOC.splitlines())
                                       if "func helper(" in l)
                    assert_eq("definition line",
                              loc["range"]["start"]["line"], helper_line)
                    print("PASS: definition jumps to declaration line")
                except (AssertionError, StopIteration) as e:
                    failures.append(f"definition: {e}")
                break

        # ── cross-file resolution against the bundled stdlib ────
        cross_uri = "file:///tmp/lsp_cross.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": cross_uri, "languageId": "lammergeier",
                "version": 1, "text": CROSS_DOC,
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        cross_lines = CROSS_DOC.splitlines()

        # Hover on the ``Dotenv`` annotation: should return a markdown
        # block that mentions the originating module.
        dot_line = next(i for i, l in enumerate(cross_lines)
                         if "env: Dotenv" in l)
        dot_col = cross_lines[dot_line].index("Dotenv") + 1
        resp = client.request("textDocument/hover", {
            "textDocument": {"uri": cross_uri},
            "position": {"line": dot_line, "character": dot_col},
        })
        try:
            val = ((resp.get("result") or {}).get("contents") or {}).get("value", "")
            assert_true("hover mentions imported class", "Dotenv" in val)
            assert_true("hover annotates origin module", "lamenv" in val)
            print("PASS: hover resolves imported stdlib class")
        except AssertionError as e:
            failures.append(f"cross-file hover: {e}")

        # Hover on ``basicAuth`` (an imported function — not a class).
        ba_line = next(i for i, l in enumerate(cross_lines)
                        if "import basicAuth" in l)
        ba_col = cross_lines[ba_line].index("basicAuth") + 1
        resp = client.request("textDocument/hover", {
            "textDocument": {"uri": cross_uri},
            "position": {"line": ba_line, "character": ba_col},
        })
        try:
            val = ((resp.get("result") or {}).get("contents") or {}).get("value", "")
            assert_true("hover on imported func", "basicAuth" in val)
            assert_true("hover origin", "lamserver_plugins" in val)
            print("PASS: hover resolves imported stdlib function")
        except AssertionError as e:
            failures.append(f"cross-file hover (func): {e}")

        resp = client.request("textDocument/rename", {
            "textDocument": {"uri": cross_uri},
            "position": {"line": ba_line, "character": ba_col},
            "newName": "renamedAuth",
        })
        try:
            err = resp.get("error") or {}
            assert_true("stdlib rename rejected", err)
            assert_true("stdlib rename clear error",
                        "imported symbol `basicAuth`" in err.get("message", ""))
            print("PASS: rename rejects imported stdlib symbols clearly")
        except AssertionError as e:
            failures.append(f"stdlib rename rejection: {e}")

        # Top-level completion should expose every imported alias.
        resp = client.request("textDocument/completion", {
            "textDocument": {"uri": cross_uri},
            "position": {"line": 0, "character": 0},
        })
        items = (resp.get("result") or {}).get("items", [])
        labels = {it.get("label") for it in items}
        try:
            assert_true("Dotenv in top-level completion", "Dotenv" in labels)
            assert_true("Config in top-level completion", "Config" in labels)
            assert_true("basicAuth in top-level completion",
                         "basicAuth" in labels)
            print("PASS: completion lists imported stdlib aliases")
        except AssertionError as e:
            failures.append(f"cross-file completion (top): {e}")

        # ``from lamenv import |`` should suggest the module's exports.
        from_uri = "file:///tmp/lsp_from.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": from_uri, "languageId": "lammergeier",
                "version": 1, "text": "from lamenv import \n",
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        resp = client.request("textDocument/completion", {
            "textDocument": {"uri": from_uri},
            "position": {"line": 0, "character": len("from lamenv import ")},
        })
        items = (resp.get("result") or {}).get("items", [])
        labels = {it.get("label") for it in items}
        try:
            assert_true("Dotenv suggested in 'from lamenv import |'",
                         "Dotenv" in labels)
            assert_true("Config suggested in 'from lamenv import |'",
                         "Config" in labels)
            print("PASS: 'from <module> import' suggests module exports")
        except AssertionError as e:
            failures.append(f"cross-file completion (import list): {e}")

        # Dotted-member completion against an imported class.
        dot_uri = "file:///tmp/lsp_dotted.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": dot_uri, "languageId": "lammergeier",
                "version": 1,
                "text": "from lamenv import Dotenv\n\nDotenv.\n",
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        resp = client.request("textDocument/completion", {
            "textDocument": {"uri": dot_uri},
            "position": {"line": 2, "character": len("Dotenv.")},
        })
        items = (resp.get("result") or {}).get("items", [])
        labels = {it.get("label") for it in items}
        try:
            assert_true("Dotenv.parse in dot completion", "parse" in labels)
            print("PASS: dot-completion lists imported class methods")
        except AssertionError as e:
            failures.append(f"cross-file dot completion: {e}")

        # Go-to-definition on ``Dotenv`` should jump into lamenv.lam.
        resp = client.request("textDocument/definition", {
            "textDocument": {"uri": cross_uri},
            "position": {"line": dot_line, "character": dot_col},
        })
        loc = resp.get("result")
        try:
            assert_true("definition non-null", loc)
            target = loc.get("uri", "")
            assert_true("definition URI points into lib/lamenv.lam",
                         "lib/lamenv.lam" in target)
            print("PASS: go-to-definition jumps into the lib file")
        except AssertionError as e:
            failures.append(f"cross-file definition: {e}")

        # ── cross-file user module resolution ───────────────
        helper_uri = "file:///tmp/helper.lam"
        user_main_uri = "file:///tmp/user_main.lam"
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": helper_uri, "languageId": "lammergeier",
                "version": 1, "text": USER_HELPER_DOC,
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        client.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": user_main_uri, "languageId": "lammergeier",
                "version": 1, "text": USER_MAIN_DOC,
            },
        })
        client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=2)
        user_lines = USER_MAIN_DOC.splitlines()
        call_line = next(i for i, l in enumerate(user_lines) if "double(21)" in l)
        call_col = user_lines[call_line].index("double") + 1

        resp = client.request("textDocument/hover", {
            "textDocument": {"uri": user_main_uri},
            "position": {"line": call_line, "character": call_col},
        })
        try:
            val = ((resp.get("result") or {}).get("contents") or {}).get("value", "")
            assert_true("hover on user import shows signature",
                         "func double(x: int) -> int" in val)
            assert_true("hover on user import shows module", "helper" in val)
            print("PASS: hover resolves imported user module function")
        except AssertionError as e:
            failures.append(f"user-module hover: {e}")

        resp = client.request("textDocument/completion", {
            "textDocument": {"uri": user_main_uri},
            "position": {"line": 0, "character": 0},
        })
        labels = {it.get("label") for it in (resp.get("result") or {}).get("items", [])}
        try:
            assert_true("double in imported completion", "double" in labels)
            print("PASS: completion lists imported user module symbols")
        except AssertionError as e:
            failures.append(f"user-module completion: {e}")

        resp = client.request("textDocument/definition", {
            "textDocument": {"uri": user_main_uri},
            "position": {"line": call_line, "character": call_col},
        })
        loc = resp.get("result")
        try:
            assert_true("user definition non-null", loc)
            assert_eq("user definition URI", loc.get("uri"), helper_uri)
            assert_eq("user definition line", loc["range"]["start"]["line"], 0)
            print("PASS: go-to-definition jumps into user module")
        except AssertionError as e:
            failures.append(f"user-module definition: {e}")

        resp = client.request("textDocument/references", {
            "textDocument": {"uri": helper_uri},
            "position": {"line": 0, "character": USER_HELPER_DOC.splitlines()[0].index("double") + 1},
            "context": {"includeDeclaration": True},
        })
        refs = resp.get("result") or []
        try:
            ref_positions = {
                (loc["uri"], loc["range"]["start"]["line"], loc["range"]["start"]["character"])
                for loc in refs
            }
            assert_true("cross-file includes helper definition",
                        (helper_uri, 0, USER_HELPER_DOC.splitlines()[0].index("double")) in ref_positions)
            assert_true("cross-file includes import usage",
                        (user_main_uri, 0, USER_MAIN_DOC.splitlines()[0].index("double")) in ref_positions)
            assert_true("cross-file includes call usage",
                        (user_main_uri, call_line, USER_MAIN_DOC.splitlines()[call_line].index("double")) in ref_positions)
            print("PASS: references finds usage across two open workspace files")
        except AssertionError as e:
            failures.append(f"user-module references: {e}")

        # ── didChange with broken doc → diagnostics ─────────
        client.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": INVALID_DOC}],
        })
        notes = client.collect_notifications("textDocument/publishDiagnostics", n=1, timeout=3)
        try:
            assert_true("got publishDiagnostics on change", notes)
            diags = notes[0].get("params", {}).get("diagnostics", [])
            assert_true("at least one diagnostic for syntax error", diags)
            assert_eq("severity is Error",
                      diags[0].get("severity"), 1)
            print("PASS: invalid document produces a parse-error diagnostic")
        except AssertionError as e:
            failures.append(str(e))

    finally:
        client.shutdown()

    print()
    if failures:
        print(f"FAILED: {len(failures)} assertion(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("All LSP tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())
