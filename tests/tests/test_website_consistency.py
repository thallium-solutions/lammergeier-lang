#!/usr/bin/env python3
"""Cross-check the deployed website against authoritative project documentation.

This is intentionally a repository contract rather than a browser snapshot.  It
executes the real website builder in an isolated tree, validates structured
package/module facts, resolves local links, and parses every Lam snippet that is
hard-coded into the homepage.
"""

from __future__ import annotations

import difflib
import html
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent.parent
APP = ROOT / "website" / "assets" / "js" / "app.js"

DOC_SOURCES = {
    "SYNTAX.md": ROOT / "docs" / "SYNTAX.md",
    "TRANSPILATION.md": ROOT / "docs" / "TRANSPILATION.md",
    "stdlib.md": ROOT / "docs" / "stdlib.md",
    "libraries.md": ROOT / "docs" / "libraries.md",
    "server_plugins.md": ROOT / "docs" / "server_plugins.md",
    "third_party_libraries.md": ROOT / "docs" / "third_party_libraries.md",
    "installation.md": ROOT / "docs" / "installation.md",
    "package_manager.md": ROOT / "docs" / "package_manager.md",
    "README.md": ROOT / "README.md",
    "CONTRIBUTING.md": ROOT / "CONTRIBUTING.md",
}

CONTENT_ROUTES = {
    "SYNTAX.md": "syntax",
    "TRANSPILATION.md": "transpilation",
    "stdlib.md": "stdlib",
    "libraries.md": "libraries",
    "server_plugins.md": "server_plugins",
    "third_party_libraries.md": "third_party",
    "installation.md": "installation",
    "package_manager.md": "package_manager",
    "README.md": "readme",
    "CONTRIBUTING.md": "contributing",
}


@dataclass(frozen=True)
class OfficialPackage:
    name: str
    repository: str
    exports: tuple[str, ...]
    version: str = "2.0.0"


# These values are the release contract shared by the independently versioned
# official repositories.  Changing a package release intentionally requires a
# one-line update here and corresponding catalog/documentation changes.
OFFICIAL_PACKAGES = (
    OfficialPackage("@lam/jwt", "lam-jwt", ("Jwt", "JwtKeySet"), "3.0.0"),
    OfficialPackage("@lam/memcached", "lam-memcached", ("Memcached",)),
    OfficialPackage("@lam/protobuf", "lam-protobuf", ("Pb",), "3.0.0"),
    OfficialPackage(
        "@lam/redis",
        "lam-redis",
        ("PubSub", "Redis", "RedisCommand", "RedisScanPage", "RedisZMember"),
    ),
    OfficialPackage("@lam/s3", "lam-s3", ("S3", "S3Client", "S3Config", "S3Object"), "2.1.0"),
    OfficialPackage(
        "@lam/sftp",
        "lam-sftp",
        ("SftpConfig", "SftpHandlers", "SftpRequest", "SftpServer"),
        "3.1.0",
    ),
    OfficialPackage(
        "@lam/ssh",
        "lam-ssh",
        ("SshClient", "SshCommandResult", "SshConfig", "SshKeyPair", "SshSession"),
        "3.0.0",
    ),
    OfficialPackage(
        "@lam/vulture",
        "lam-vulture",
        (
            "HttpError",
            "InjectResponse",
            "Request",
            "RequestBodyReader",
            "Response",
            "RouteOptions",
            "Server",
            "SseEmitter",
        ),
        "2.0.1",
    ),
    OfficialPackage(
        "@lam/vulture/std-plugin",
        "lam-vulture-std-plugin",
        ("CircuitBreaker", "compress", "helmet", "metrics", "rateLimit", "requestLog"),
        "2.0.2",
    ),
    OfficialPackage(
        "@lam/vulture/tus-plugin",
        "lam-vulture-tus-plugin",
        ("tusGc", "tusUploads"),
        "3.0.1",
    ),
    OfficialPackage(
        "@lam/vulture/ws-plugin",
        "lam-vulture-ws-plugin",
        ("WebSocket", "wsRoute", "wsUpgrade"),
        "2.0.1",
    ),
)


@dataclass(frozen=True)
class Failure:
    check: str
    path: str
    line: int
    message: str


class Report:
    def __init__(self) -> None:
        self.failures: list[Failure] = []

    def add(self, check: str, path: Path | str, line: int, message: str) -> None:
        if isinstance(path, Path):
            try:
                shown = path.relative_to(ROOT).as_posix()
            except ValueError:
                shown = path.as_posix()
        else:
            shown = path
        failure = Failure(check, shown, max(1, line), message)
        if failure not in self.failures:
            self.failures.append(failure)

    def finish(self) -> int:
        if not self.failures:
            print("PASS: website/documentation consistency checks")
            return 0
        print(f"FAIL: website/documentation consistency ({len(self.failures)} issue(s))")
        current = None
        for failure in sorted(
            self.failures, key=lambda item: (item.check, item.path, item.line, item.message)
        ):
            if failure.check != current:
                current = failure.check
                print(f"\n[{current}]")
            print(f"  {failure.path}:{failure.line}: {failure.message}")
        return 1


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _first_difference(expected: str, actual: str) -> tuple[int, str]:
    expected_lines = expected.splitlines()
    actual_lines = actual.splitlines()
    limit = min(len(expected_lines), len(actual_lines))
    line = next(
        (index + 1 for index in range(limit) if expected_lines[index] != actual_lines[index]),
        limit + 1,
    )
    diff = list(
        difflib.unified_diff(
            actual_lines,
            expected_lines,
            fromfile="committed",
            tofile="generated",
            n=1,
            lineterm="",
        )
    )
    detail = " | ".join(part.strip() for part in diff[2:8])
    return line, detail


def check_generated_content(report: Report) -> None:
    """Run the real builder twice outside the checkout and compare byte-for-byte."""
    build_script = ROOT / "website" / "build.sh"
    for destination, source in DOC_SOURCES.items():
        if not source.is_file():
            report.add("website build", source, 1, f"authoritative source for {destination} is missing")
    if not build_script.is_file():
        report.add("website build", build_script, 1, "website builder is missing")
        return

    with tempfile.TemporaryDirectory(prefix="lam-website-build-") as td:
        isolated = Path(td)
        (isolated / "website").mkdir()
        (isolated / "docs").mkdir()
        shutil.copy2(build_script, isolated / "website" / "build.sh")
        for destination, source in DOC_SOURCES.items():
            if not source.is_file():
                continue
            target = isolated / (source.relative_to(ROOT))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

        command = ["bash", str(isolated / "website" / "build.sh")]
        first = subprocess.run(
            command, cwd=isolated, capture_output=True, text=True, timeout=30
        )
        if first.returncode != 0:
            report.add(
                "website build",
                build_script,
                1,
                "isolated build failed: " + (first.stderr or first.stdout).strip(),
            )
            return
        generated_dir = isolated / "website" / "content"
        first_output = {path.name: path.read_bytes() for path in generated_dir.glob("*.md")}

        second = subprocess.run(
            command, cwd=isolated, capture_output=True, text=True, timeout=30
        )
        if second.returncode != 0:
            report.add(
                "website build",
                build_script,
                1,
                "second isolated build failed: " + (second.stderr or second.stdout).strip(),
            )
            return
        second_output = {path.name: path.read_bytes() for path in generated_dir.glob("*.md")}
        if first_output != second_output:
            report.add(
                "website build",
                build_script,
                18,
                "build.sh is not deterministic/idempotent across two consecutive runs",
            )

        expected_names = set(DOC_SOURCES)
        generated_names = set(second_output)
        for missing in sorted(expected_names - generated_names):
            report.add(
                "website build",
                build_script,
                41,
                f"builder did not generate website/content/{missing}",
            )
        for extra in sorted(generated_names - expected_names):
            report.add(
                "website build",
                build_script,
                41,
                f"builder generated unregistered content file {extra}",
            )

        committed_dir = ROOT / "website" / "content"
        committed_names = {path.name for path in committed_dir.glob("*.md")}
        for missing in sorted(generated_names - committed_names):
            report.add(
                "website build",
                committed_dir / missing,
                1,
                "generated document is not committed; run website/build.sh",
            )
        for stale in sorted(committed_names - generated_names):
            report.add(
                "website build",
                committed_dir / stale,
                1,
                "stale generated document has no authoritative build input",
            )
        for name in sorted(generated_names & committed_names):
            generated = second_output[name].decode("utf-8")
            committed_path = committed_dir / name
            committed = _read(committed_path)
            if generated != committed:
                line, detail = _first_difference(generated, committed)
                report.add(
                    "website build",
                    committed_path,
                    line,
                    f"does not equal deterministic build output; run website/build.sh ({detail})",
                )


DOC_ENTRY_RE = re.compile(
    r"\{\s*id:\s*\"(?P<id>[^\"]+)\"\s*,\s*"
    r"file:\s*\"(?P<file>[^\"]+)\"",
    re.DOTALL,
)


def parse_doc_entries(report: Report) -> dict[str, str]:
    text = _read(APP)
    routes: dict[str, str] = {}
    files: dict[str, str] = {}
    for match in DOC_ENTRY_RE.finditer(text):
        route = match.group("id")
        filename = match.group("file")
        line = _line_number(text, match.start())
        if route in routes:
            report.add("website routes", APP, line, f"duplicate DOCS route id {route!r}")
        if filename in files:
            report.add("website routes", APP, line, f"duplicate DOCS file {filename!r}")
        routes[route] = filename
        files[filename] = route
        target = ROOT / "website" / filename
        if not target.is_file():
            report.add("website routes", APP, line, f"DOCS file does not exist: {filename}")

    expected_files = {f"content/{name}" for name in DOC_SOURCES}
    actual_files = set(files)
    for missing in sorted(expected_files - actual_files):
        report.add("website routes", APP, 11, f"DOCS omits generated file {missing}")
    for extra in sorted(actual_files - expected_files):
        report.add("website routes", APP, 11, f"DOCS references unmanaged file {extra}")
    for filename, expected_route in CONTENT_ROUTES.items():
        actual_route = files.get(f"content/{filename}")
        if actual_route is not None and actual_route != expected_route:
            report.add(
                "website routes",
                APP,
                11,
                f"content/{filename} uses route {actual_route!r}; expected {expected_route!r}",
            )
    return routes


@dataclass(frozen=True)
class RemovedRule:
    name: str
    pattern: re.Pattern[str]
    replacement: str


REMOVED_RULES = (
    RemovedRule(
        "bundled server module",
        re.compile(r"(?<![A-Za-z0-9_@/-])lamserver(?:_plugins|_ws|_tus)?(?![A-Za-z0-9_/-])"),
        "use the matching @lam/vulture package import",
    ),
    RemovedRule(
        "route *Opts API",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:routeOpts|getOpts|postOpts|putOpts|delOpts|"
            r"patchOpts|headOpts|optionsOpts|wsRouteOpts)(?![A-Za-z0-9_])"
        ),
        "use route/method/wsRoute overloads with RouteOptions",
    ),
    RemovedRule(
        "network recv API",
        re.compile(r"(?<![A-Za-z0-9_])(?:recvBytes|recv)(?![A-Za-z0-9_])"),
        "use receive",
    ),
    RemovedRule(
        "tryX Result alias",
        re.compile(r"(?<![A-Za-z0-9_])try[A-Z][A-Za-z0-9_]*(?![A-Za-z0-9_])"),
        "use the unsuffixed Result-returning API",
    ),
    RemovedRule(
        "Cli.getInt",
        re.compile(r"(?<![A-Za-z0-9_])Cli\.getInt(?![A-Za-z0-9_])"),
        "use the integer overload of Cli.getFlag",
    ),
    RemovedRule(
        "type-specific Data helper",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:mutateInts|mutateFloats|mutateStrings|mutateBools|"
            r"fromInts|fromFloats|fromStrings|fromBools)(?![A-Za-z0-9_])"
        ),
        "use overloaded DataFrame.mutate or generic Series.from",
    ),
)


@dataclass(frozen=True)
class Allowance:
    rule: str
    path: str
    heading: str
    line_pattern: re.Pattern[str]
    fence: str | None = None


# Historical names are permitted only in these labelled, exact contexts.  Add
# another narrow entry when a migration note genuinely needs to name an old API;
# do not exclude a whole file or a generic substring.
REMOVED_ALLOWLIST = (
    Allowance(
        "bundled server module",
        "docs/libraries.md",
        "Migrating the HTTP server from Lam 1.17",
        re.compile(
            r"^\| `from lamserver(?:_plugins|_tus|_ws)? import [^`]+` "
            r"\| `from @lam/vulture(?:/(?:std|tus|ws)-plugin)? import [^`]+` \|$"
        ),
    ),
    Allowance(
        "bundled server module",
        "docs/libraries.md",
        "Migrating legacy Vulture applications to v2",
        re.compile(
            r"^\| `from lamserver(?:_plugins|_tus|_ws)? import [^`]+` "
            r"\| `from @lam/vulture(?:/(?:std|tus|ws)-plugin)? import [^`]+` \|$"
        ),
    ),
    Allowance(
        "bundled server module",
        "CHANGELOG.md",
        "Removed",
        re.compile(
            r"^- Removed the compiler-bundled `lamserver`, `lamserver_plugins`, "
            r"`lamserver_ws`,$"
        ),
    ),
    Allowance(
        "bundled server module",
        "CHANGELOG.md",
        "Removed",
        re.compile(r"^  and `lamserver_tus` modules; applications now install their Vulture packages\.$"),
    ),
    Allowance(
        "bundled server module",
        "CHANGELOG.md",
        "Added",
        re.compile(r"^- Added optional request schemas and schema formats to the simple `lamserver`$"),
    ),
    Allowance(
        "bundled server module",
        "CHANGELOG.md",
        "Changed",
        re.compile(r"^- Unified `lamserver` request/response schema validation with the `lamschema`$"),
    ),
    Allowance(
        "bundled server module",
        "CHANGELOG.md",
        "Added",
        re.compile(r"^- `lamserver` lifecycle hooks, decorators, content-type parsers, versioned$"),
    ),
    Allowance(
        "bundled server module",
        "CHANGELOG.md",
        "Documentation",
        re.compile(r"^- Added the `lamserver` plugin authoring guide in `docs/server_plugins\.md`\.$"),
    ),
    Allowance(
        "bundled server module",
        "CHANGELOG.md",
        "Added",
        re.compile(r"^- Advanced `lamserver` features: query and cookie helpers, HTML responses,$"),
    ),
)


@dataclass(frozen=True)
class MarkdownLine:
    number: int
    text: str
    headings: tuple[str, ...]
    fence: str | None


def iter_markdown_lines(text: str) -> list[MarkdownLine]:
    headings: dict[int, str] = {}
    fence: str | None = None
    result: list[MarkdownLine] = []
    for number, line in enumerate(text.splitlines(), 1):
        fence_match = re.match(r"^\s*(```+|~~~+)\s*([^ ]*)", line)
        if fence_match:
            if fence is None:
                fence = fence_match.group(2).strip().lower() or "text"
                result.append(MarkdownLine(number, line, tuple(headings.values()), None))
            else:
                result.append(MarkdownLine(number, line, tuple(headings.values()), fence))
                fence = None
            continue
        if fence is None:
            heading_match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", line)
            if heading_match:
                level = len(heading_match.group(1))
                headings[level] = heading_match.group(2)
                headings = {key: value for key, value in headings.items() if key <= level}
        result.append(MarkdownLine(number, line, tuple(headings.values()), fence))
    return result


def _is_removed_allowlisted(
    rule: RemovedRule, relative: str, context: MarkdownLine
) -> bool:
    for allowance in REMOVED_ALLOWLIST:
        if allowance.rule != rule.name or allowance.path != relative:
            continue
        if allowance.heading not in context.headings:
            continue
        if allowance.fence is not None and allowance.fence != context.fence:
            continue
        if allowance.line_pattern.fullmatch(context.text):
            return True
    return False


def _looks_like_removed_api_use(rule: RemovedRule, line: str, token: str) -> bool:
    """Reject API guidance without confusing similarly named user declarations."""
    if rule.name == "tryX Result alias":
        if re.search(rf"\bfunc\s+{re.escape(token)}\b", line):
            return False
        return bool(
            re.search(rf"\.{re.escape(token)}\b", line)
            or re.search(rf"`{re.escape(token)}`", line)
            or re.search(rf"\bimport\b.*\b{re.escape(token)}\b", line)
            or re.search(rf"\b{re.escape(token)}\s*\(", line)
        )
    if rule.name == "network recv API":
        return bool(
            re.search(rf"\.{re.escape(token)}\b", line)
            or re.search(rf"`{re.escape(token)}`", line)
            or re.search(rf"\bimport\b.*\b{re.escape(token)}\b", line)
        )
    return True


def check_removed_guidance(report: Report) -> None:
    markdown_files = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", ROOT / "CHANGELOG.md"]
    markdown_files.extend(sorted((ROOT / "docs").glob("*.md")))
    for path in markdown_files:
        relative = path.relative_to(ROOT).as_posix()
        for context in iter_markdown_lines(_read(path)):
            for rule in REMOVED_RULES:
                for match in rule.pattern.finditer(context.text):
                    token = match.group(0)
                    if not _looks_like_removed_api_use(rule, context.text, token):
                        continue
                    if _is_removed_allowlisted(rule, relative, context):
                        continue
                    location = " / ".join(context.headings) or "document root"
                    report.add(
                        "removed API guidance",
                        path,
                        context.number,
                        f"{rule.name} {token!r} appears in current guidance ({location}); {rule.replacement}",
                    )

    # app.js is current homepage copy, never migration/history.  Exact identifier
    # boundaries avoid accidental matches in unrelated words and internal names.
    app_text = _read(APP)
    for number, line in enumerate(app_text.splitlines(), 1):
        for rule in REMOVED_RULES:
            for match in rule.pattern.finditer(line):
                token = match.group(0)
                if _looks_like_removed_api_use(rule, line, token):
                    report.add(
                        "homepage claims",
                        APP,
                        number,
                        f"homepage names removed {rule.name} {token!r}; {rule.replacement}",
                    )

    claim_patterns = (
        (
            re.compile(
                r"(?is)standard library.{0,240}(?:HTTP servers|Plugins).{0,300}"
                r"live under <code>lib/</code>"
            ),
            "HTTP servers/plugins are external @lam packages, not lib/ stdlib modules",
        ),
        (
            re.compile(
                r"(?is)(?:plugins|WebSockets|TUS uploads).{0,240}"
                r"ship as <code>lam\*</code> modules"
            ),
            "server/plugin capabilities must not be described as bundled lam* modules",
        ),
        (
            re.compile(r"(?is)(?:CORS|HttpError).{0,220}come from the stdlib"),
            "Vulture HTTP helpers come from external packages, not the stdlib",
        ),
    )
    for pattern, message in claim_patterns:
        for match in pattern.finditer(app_text):
            report.add("homepage claims", APP, _line_number(app_text, match.start()), message)


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _section_lines(text: str, heading: str) -> list[tuple[int, str]]:
    lines = text.splitlines()
    start = None
    level = None
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and re.sub(r"[`*_]", "", match.group(2)).strip() == heading:
            start = index + 1
            level = len(match.group(1))
            break
    if start is None or level is None:
        return []
    end = len(lines)
    for index in range(start, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return [(index + 1, lines[index]) for index in range(start, end)]


def _module_rows(text: str, heading: str) -> dict[str, tuple[int, list[str]]]:
    rows: dict[str, tuple[int, list[str]]] = {}
    for number, line in _section_lines(text, heading):
        if not line.lstrip().startswith("|"):
            continue
        cells = _split_table_row(line)
        if not cells:
            continue
        match = re.fullmatch(r"`(lam[A-Za-z0-9_]*)`", cells[0])
        if match:
            rows[match.group(1)] = (number, cells)
    return rows


def _tier_modules(text: str) -> tuple[int, set[str]]:
    modules: set[str] = set()
    first_line = 1
    for number, line in _section_lines(text, "Stdlib tiers"):
        if not line.lstrip().startswith("|"):
            continue
        found = re.findall(r"`(lam[A-Za-z0-9_]*)`", line)
        if found and first_line == 1:
            first_line = number
        modules.update(found)
    return first_line, modules


def _shim_import(source: str) -> str | None:
    if not re.search(r"(?im)^#\s*Compatibility module\b", source):
        return None
    imports = re.findall(
        r"(?m)^from\s+(lam[A-Za-z0-9_]*)\s+import\s+([A-Za-z0-9_, ]+)\s*$", source
    )
    if len(imports) != 1:
        return None
    body = re.sub(r"(?m)^\s*#.*$", "", source)
    if not re.search(r"(?s)func\s+main\(\)\s*\{\s*pass\s*\}\s*$", body):
        return None
    module, names = imports[0]
    return f"from {module} import {', '.join(part.strip() for part in names.split(','))}"


def _compare_module_table(
    report: Report,
    path: Path,
    heading: str,
    rows: dict[str, tuple[int, list[str]]],
    actual_modules: set[str],
    shim_imports: dict[str, str],
) -> None:
    shown_heading = f"{path.relative_to(ROOT).as_posix()} / {heading}"
    table_modules = set(rows)
    section = _section_lines(_read(path), heading)
    section_line = section[0][0] if section else 1
    for missing in sorted(actual_modules - table_modules):
        report.add(
            "stdlib module tables",
            path,
            section_line,
            f"{shown_heading} omits lib/{missing}.lam",
        )
    for extra in sorted(table_modules - actual_modules):
        line = rows[extra][0]
        report.add(
            "stdlib module tables",
            path,
            line,
            f"{shown_heading} lists {extra}, but lib/{extra}.lam does not exist",
        )
    for module in sorted(shim_imports):
        if module not in rows:
            continue
        line, cells = rows[module]
        row_text = " | ".join(cells)
        lowered = row_text.lower()
        if "compatibility" not in lowered or not ("re-export" in lowered or "import" in lowered):
            report.add(
                "stdlib module tables",
                path,
                line,
                f"{module} is a compatibility shim; label it as a compatibility re-export/import",
            )
    if "lamdatastructures" in rows:
        line, cells = rows["lamdatastructures"]
        if "canonical" not in " | ".join(cells).lower():
            report.add(
                "stdlib module tables",
                path,
                line,
                "lamdatastructures is the canonical owner and must be labelled canonical",
            )


def check_stdlib_modules(report: Report) -> None:
    lib_files = sorted((ROOT / "lib").glob("*.lam"))
    actual_modules = {path.stem for path in lib_files}
    shim_imports = {
        path.stem: import_line
        for path in lib_files
        if (import_line := _shim_import(_read(path))) is not None
    }
    for path in lib_files:
        source = _read(path)
        declares_shim = re.search(r"(?im)^#\s*Compatibility module\b", source) is not None
        if declares_shim and path.stem not in shim_imports:
            report.add(
                "stdlib module tables",
                path,
                1,
                "compatibility module must be a single explicit re-export plus an empty main",
            )

    stdlib_path = ROOT / "docs" / "stdlib.md"
    stdlib_text = _read(stdlib_path)
    public_rows = _module_rows(stdlib_text, "Public API index")
    if not public_rows:
        report.add(
            "stdlib module tables", stdlib_path, 1, "missing parseable Public API index module table"
        )
    else:
        _compare_module_table(
            report, stdlib_path, "Public API index", public_rows, actual_modules, shim_imports
        )
        for module, (line, cells) in public_rows.items():
            if len(cells) < 2:
                continue
            import_match = re.search(r"from\s+(lam[A-Za-z0-9_]*)\s+import\b", cells[1])
            if not import_match or import_match.group(1) != module:
                report.add(
                    "stdlib module tables",
                    stdlib_path,
                    line,
                    f"{module} row must show an import from {module}",
                )

    tier_line, tier_modules = _tier_modules(stdlib_text)
    for missing in sorted(actual_modules - tier_modules):
        report.add(
            "stdlib module tables",
            stdlib_path,
            tier_line,
            f"Stdlib tiers table omits lib/{missing}.lam",
        )
    for extra in sorted(tier_modules - actual_modules):
        report.add(
            "stdlib module tables",
            stdlib_path,
            tier_line,
            f"Stdlib tiers table lists {extra}, but lib/{extra}.lam does not exist",
        )

    syntax_path = ROOT / "docs" / "SYNTAX.md"
    syntax_rows = _module_rows(_read(syntax_path), "Standard Library")
    if not syntax_rows:
        report.add(
            "stdlib module tables", syntax_path, 1, "missing parseable Standard Library module table"
        )
    else:
        _compare_module_table(
            report, syntax_path, "Standard Library", syntax_rows, actual_modules, shim_imports
        )


def _catalog_rows(text: str) -> dict[str, tuple[int, list[str]]]:
    rows: dict[str, tuple[int, list[str]]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.lstrip().startswith("|"):
            continue
        cells = _split_table_row(line)
        if not cells:
            continue
        match = re.fullmatch(r"`(@lam/[A-Za-z0-9_/-]+)`", cells[0])
        if match:
            rows[match.group(1)] = (number, cells)
    return rows


def _non_migration_lines(text: str) -> list[MarkdownLine]:
    return [
        context
        for context in iter_markdown_lines(text)
        if not any(heading.lower().startswith("migrating") for heading in context.headings)
    ]


def check_official_packages(report: Report) -> None:
    path = ROOT / "docs" / "libraries.md"
    text = _read(path)
    rows = _catalog_rows(text)
    expected = {package.name for package in OFFICIAL_PACKAGES}
    for missing in sorted(expected - set(rows)):
        report.add("official packages", path, 12, f"official package table omits {missing}")
    for extra in sorted(set(rows) - expected):
        report.add(
            "official packages",
            path,
            rows[extra][0],
            f"unrecognised official package {extra}; update OFFICIAL_PACKAGES if intentional",
        )

    version_text = _read(ROOT / "compiler" / "version.py")
    version_match = re.search(r'LAMC_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"', version_text)
    if not version_match:
        report.add("official packages", ROOT / "compiler" / "version.py", 1, "cannot read LAMC_VERSION")
        compiler_target = None
    else:
        compiler_target = f"{version_match.group(1)}.{version_match.group(2)}"
        if not re.search(rf"\blamc`?\s+{re.escape(compiler_target)}\b", text):
            report.add(
                "official packages",
                path,
                5,
                f"catalog must state that official packages target lamc {compiler_target}",
            )

    all_lines = iter_markdown_lines(text)
    current_lines = _non_migration_lines(text)
    globally_declared_versions = {
        version
        for version in {package.version for package in OFFICIAL_PACKAGES}
        if re.search(
            rf"Every current official package[^.]*`{re.escape(version)}`",
            text,
            re.IGNORECASE | re.DOTALL,
        )
    }
    for package in OFFICIAL_PACKAGES:
        row = rows.get(package.name)
        row_text = ""
        if row is not None:
            line, cells = row
            row_text = " | ".join(cells)
            repository_url = f"https://github.com/thallium-solutions/{package.repository}"
            if repository_url not in row_text:
                report.add(
                    "official packages",
                    path,
                    line,
                    f"{package.name} row must link to {repository_url}",
                )
            missing_exports = [name for name in package.exports if not re.search(rf"\b{re.escape(name)}\b", row_text)]
            if missing_exports:
                report.add(
                    "official packages",
                    path,
                    line,
                    f"{package.name} row omits current exports: {', '.join(missing_exports)}",
                )

        spec_pattern = re.compile(
            rf"{re.escape(package.name)}@(?P<version>[~^]?\d+(?:\.\d+){{1,2}})"
        )
        specs = [(context, match.group("version")) for context in current_lines for match in spec_pattern.finditer(context.text)]
        version_is_documented = (
            package.version in globally_declared_versions
            or package.version in row_text
            or any(version == package.version for _, version in specs)
        )
        if not version_is_documented:
            report.add(
                "official packages",
                path,
                row[0] if row else 12,
                f"catalog does not associate {package.name} with current version {package.version}",
            )
        current_minor = ".".join(package.version.split(".")[:2])
        accepted_specs = {
            package.version,
            f"^{current_minor}",
            f"^{package.version}",
            f"~{current_minor}",
            f"~{package.version}",
        }
        for context, version in specs:
            if version not in accepted_specs:
                report.add(
                    "official packages",
                    path,
                    context.number,
                    f"current guidance uses {package.name}@{version}; expected {package.version}",
                )

        imports: list[tuple[MarkdownLine, set[str]]] = []
        import_pattern = re.compile(
            rf"from\s+{re.escape(package.name)}\s+import\s+([A-Za-z0-9_, ]+)"
        )
        for context in all_lines:
            for match in import_pattern.finditer(context.text):
                names = {name.strip() for name in match.group(1).split(",") if name.strip()}
                imports.append((context, names))
        covered = set().union(*(names for _, names in imports)) if imports else set()
        if not covered.intersection(package.exports):
            report.add(
                "official packages",
                path,
                row[0] if row else 12,
                f"catalog needs a current canonical import example for {package.name}",
            )

    if compiler_target is not None and compiler_target != "1.19":
        report.add(
            "official packages",
            ROOT / "compiler" / "version.py",
            3,
            "compiler release changed; verify official package versions and update this contract",
        )


INLINE_LINK_RE = re.compile(
    r"(?<![A-Za-z0-9_])!?\[[^\]]*\]\(\s*(?P<target><[^>]+>|[^\s)]+)"
    r"(?:\s+[\"'][^)]*[\"'])?\s*\)"
)
HTML_LINK_RE = re.compile(r"\b(?:href|src)\s*=\s*[\"'](?P<target>[^\"']+)[\"']")
OWN_SOURCE_RE = re.compile(
    r"^https://github\.com/thallium-solutions/lammergeier-lang/"
    r"(?:blob|tree)/(?:main|master)/(?P<path>[^?#]+)"
)


@dataclass(frozen=True)
class Link:
    path: Path
    line: int
    target: str


def _markdown_links(path: Path) -> list[Link]:
    links: list[Link] = []
    for context in iter_markdown_lines(_read(path)):
        if context.fence is not None:
            continue
        for pattern in (INLINE_LINK_RE, HTML_LINK_RE):
            for match in pattern.finditer(context.text):
                target = match.group("target").strip("<>")
                links.append(Link(path, context.number, html.unescape(target)))
    return links


def _html_links(path: Path) -> list[Link]:
    text = _read(path)
    return [
        Link(path, _line_number(text, match.start()), html.unescape(match.group("target")))
        for match in HTML_LINK_RE.finditer(text)
    ]


def _javascript_links(path: Path) -> list[Link]:
    links: list[Link] = []
    in_block_comment = False
    for number, line in enumerate(_read(path).splitlines(), 1):
        stripped = line.lstrip()
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            in_block_comment = "*/" not in stripped
            continue
        if stripped.startswith("//"):
            continue
        for match in HTML_LINK_RE.finditer(line):
            target = html.unescape(match.group("target"))
            if "${" not in target:
                links.append(Link(path, number, target))
    return links


def _heading_slug(raw: str) -> str:
    def escape_code(match: re.Match[str]) -> str:
        value = match.group(1)
        value = re.sub(r"&(?!(?:#\d{1,7}|#[Xx][0-9A-Fa-f]{1,6}|\w+);)", "&amp;", value)
        return value.replace("<", "&lt;").replace(">", "&gt;")

    text = re.sub(r"`([^`]*)`", escape_code, raw)
    text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    # Marked passes rendered inline HTML to app.js's heading callback. Literal
    # ampersands therefore arrive as ``&amp;`` while existing entities remain
    # entities. app.js decodes that rendered text exactly once before removing
    # punctuation, preventing the encoding artifact from becoming an ``amp``
    # path segment.
    text = re.sub(r"&(?!(?:#\d{1,7}|#[Xx][0-9A-Fa-f]{1,6}|\w+);)", "&amp;", text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    text = text.replace('"', "&quot;").replace("'", "&#39;")
    text = html.unescape(text)
    text = re.sub(r"[*_~]", "", text).lower()
    text = re.sub(r"[^A-Za-z0-9_\s-]", "", text).strip()
    return re.sub(r"\s", "-", text)


def _document_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    for context in iter_markdown_lines(_read(path)):
        if context.fence is not None:
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", context.text)
        if heading:
            anchors.add(_heading_slug(heading.group(1)))
        anchors.update(re.findall(r"\b(?:id|name)=[\"']([^\"']+)[\"']", context.text))
    return anchors


def _suggest(value: str, choices: set[str]) -> str:
    close = difflib.get_close_matches(value, sorted(choices), n=2, cutoff=0.45)
    return f"; closest: {', '.join(close)}" if close else ""


def _resolve_source_link(report: Report, link: Link) -> None:
    target = link.target
    own = OWN_SOURCE_RE.match(target)
    if own:
        source_path = ROOT / unquote(own.group("path")).rstrip("/")
        if not source_path.exists():
            report.add(
                "source links",
                link.path,
                link.line,
                f"repository source link does not resolve locally: {target}",
            )
        return
    parsed = urlsplit(target)
    if parsed.scheme or target.startswith("//") or target.startswith("#"):
        return
    if not parsed.path:
        return
    local = (link.path.parent / unquote(parsed.path)).resolve()
    try:
        local.relative_to(ROOT)
    except ValueError:
        report.add("source links", link.path, link.line, f"local link escapes repository: {target}")
        return
    if not local.exists():
        report.add("source links", link.path, link.line, f"local source link is missing: {target}")


def _resolve_website_link(
    report: Report,
    link: Link,
    routes: dict[str, str],
    anchors: dict[str, set[str]],
) -> None:
    target = link.target
    if target.startswith("#/"):
        route_and_query = target[1:]
        route_path, _, query_text = route_and_query.partition("?")
        if route_path == "/":
            return
        route_match = re.fullmatch(r"/docs/([A-Za-z0-9_-]+)", route_path)
        if not route_match:
            report.add("website links", link.path, link.line, f"unknown internal route: {target}")
            return
        route = route_match.group(1)
        if route not in routes:
            report.add("website links", link.path, link.line, f"route has no DOCS entry: {target}")
            return
        query = parse_qs(query_text)
        if "h" in query:
            requested = query["h"][0]
            choices = anchors.get(route, set())
            if requested not in choices:
                report.add(
                    "website links",
                    link.path,
                    link.line,
                    f"heading route does not resolve: {target}{_suggest(requested, choices)}",
                )
        return
    if target.startswith("#"):
        filename = link.path.name
        route = CONTENT_ROUTES.get(filename)
        if route is None:
            return
        requested = unquote(target[1:])
        choices = anchors.get(route, set())
        if requested and requested not in choices:
            report.add(
                "website links",
                link.path,
                link.line,
                f"in-document heading does not resolve in the SPA: {target}"
                f"{_suggest(requested, choices)}",
            )
        return

    own = OWN_SOURCE_RE.match(target)
    if own:
        source_path = ROOT / unquote(own.group("path")).rstrip("/")
        if not source_path.exists():
            report.add(
                "source links",
                link.path,
                link.line,
                f"repository source link does not resolve locally: {target}",
            )
        return
    parsed = urlsplit(target)
    if parsed.scheme or target.startswith("//"):
        return
    if not parsed.path:
        return
    raw_path = unquote(parsed.path)
    if raw_path.startswith("/"):
        local = ROOT / "website" / raw_path.lstrip("/")
    else:
        # Injected Markdown is rendered in website/index.html, so browser-relative
        # URLs resolve from the deploy root, not from website/content/.
        local = ROOT / "website" / raw_path
    local = local.resolve()
    website_root = (ROOT / "website").resolve()
    try:
        local.relative_to(website_root)
    except ValueError:
        report.add("website links", link.path, link.line, f"website link escapes deploy root: {target}")
        return
    if not local.exists():
        report.add("website links", link.path, link.line, f"website-local target is missing: {target}")


def check_links(report: Report, routes: dict[str, str]) -> None:
    anchors: dict[str, set[str]] = {}
    for route, filename in routes.items():
        content_path = ROOT / "website" / filename
        if content_path.is_file():
            anchors[route] = _document_anchors(content_path)

    source_paths = [ROOT / "README.md", ROOT / "CONTRIBUTING.md"]
    source_paths.extend(sorted((ROOT / "docs").glob("*.md")))
    for path in source_paths:
        for link in _markdown_links(path):
            _resolve_source_link(report, link)

    website_links: list[Link] = []
    for path in sorted((ROOT / "website" / "content").glob("*.md")):
        website_links.extend(_markdown_links(path))
    website_links.extend(_html_links(ROOT / "website" / "index.html"))
    website_links.extend(_javascript_links(APP))
    for link in website_links:
        _resolve_website_link(report, link, routes, anchors)


@dataclass(frozen=True)
class HomepageSnippet:
    name: str
    source: str
    line: int


def _template_literal(text: str, start: int) -> tuple[str, int]:
    if start >= len(text) or text[start] != "`":
        raise ValueError("template literal does not start with a backtick")
    index = start + 1
    chars: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "`" and (index == 0 or text[index - 1] != "\\"):
            return "".join(chars), index + 1
        chars.append(char)
        index += 1
    raise ValueError("unterminated template literal")


def _extract_homepage_snippets(report: Report) -> list[HomepageSnippet]:
    text = _read(APP)
    snippets: list[HomepageSnippet] = []
    hero_match = re.search(r"\bconst\s+heroSample\s*=\s*`", text)
    if not hero_match:
        report.add("homepage snippets", APP, 1, "cannot find heroSample template literal")
    else:
        start = hero_match.end() - 1
        try:
            source, _ = _template_literal(text, start)
            snippets.append(HomepageSnippet("heroSample", source, _line_number(text, start) + 1))
        except ValueError as exc:
            report.add("homepage snippets", APP, _line_number(text, start), str(exc))

    tour_start = text.find("const tourCards = [")
    tour_end = text.find("const renderTourCard", tour_start)
    if tour_start < 0 or tour_end < 0:
        report.add("homepage snippets", APP, 1, "cannot locate tourCards array")
    else:
        tour = text[tour_start:tour_end]
        headings = len(re.findall(r"\bheading\s*:\s*(?:`|\"|')", tour))
        code_matches = list(re.finditer(r"\bcode\s*:\s*`", tour))
        if headings != len(code_matches):
            report.add(
                "homepage snippets",
                APP,
                _line_number(text, tour_start),
                f"tour cards have {headings} headings but {len(code_matches)} extractable code snippets",
            )
        for index, match in enumerate(code_matches, 1):
            start = tour_start + match.end() - 1
            try:
                source, _ = _template_literal(text, start)
                snippets.append(
                    HomepageSnippet(f"tourCards[{index}]", source, _line_number(text, start) + 1)
                )
            except ValueError as exc:
                report.add("homepage snippets", APP, _line_number(text, start), str(exc))

    rendered = re.findall(
        r'<pre><code\s+class="language-(?:python|lam|lammergeier)">'
        r"(?P<body>.*?)</code></pre>",
        text,
        re.DOTALL,
    )
    expected_renderers = {"${escapeHtml(heroSample)}", "${escapeHtml(c.code)}"}
    actual_renderers = {body.strip() for body in rendered}
    for missing in sorted(expected_renderers - actual_renderers):
        report.add(
            "homepage snippets",
            APP,
            1,
            f"cannot prove extraction coverage for rendered Lam snippet expression {missing}",
        )
    for unknown in sorted(actual_renderers - expected_renderers):
        offset = text.find(unknown)
        report.add(
            "homepage snippets",
            APP,
            _line_number(text, max(0, offset)),
            f"rendered Lam snippet is not covered by the extractor: {unknown[:80]!r}",
        )
    return snippets


def check_homepage_snippets(report: Report) -> None:
    snippets = _extract_homepage_snippets(report)
    if not snippets:
        return
    sys.path.insert(0, str(ROOT))
    try:
        from compiler.lammergeier import create_parser, preprocess_for_parse
    except Exception as exc:  # pragma: no cover - environment-specific diagnostic
        report.add("homepage snippets", APP, 1, f"cannot import Lam parser: {exc}")
        return
    parser = create_parser()
    for snippet in snippets:
        try:
            prepared = preprocess_for_parse(snippet.source).source
            parser.parse(prepared if prepared.endswith("\n") else prepared + "\n")
        except Exception as exc:
            relative_line = getattr(exc, "line", 1) or 1
            report.add(
                "homepage snippets",
                APP,
                snippet.line + relative_line - 1,
                f"{snippet.name} is not valid Lam syntax: {exc}",
            )


def main() -> int:
    report = Report()
    check_generated_content(report)
    routes = parse_doc_entries(report)
    check_removed_guidance(report)
    check_stdlib_modules(report)
    check_official_packages(report)
    check_links(report, routes)
    check_homepage_snippets(report)
    return report.finish()


if __name__ == "__main__":
    sys.exit(main())
