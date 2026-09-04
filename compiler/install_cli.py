"""``lamc install`` / ``uninstall`` / ``publish`` subcommands.

Matches ``docs/third_party_libraries.md``:

- **Source-only.** We always ship ``.lam`` source, never binaries.
  The installed tree stays grepable and any ``lamc`` that can parse
  the sources can use it — no compiler-version pinning on artefacts.
- **Registry = HTTP.** A Lam registry is a plain HTTP service
  exposing three JSON endpoints (see :class:`Registry`). Installers
  can also fetch straight from a git repo with no registry.
- **Install destination.** ``<cwd>/extlibs/`` by default —
  per-project, lockfiled, reproducible. ``--global`` opts into the
  legacy ``~/.lammergeier/extlibs/`` mode (no lockfile, no project
  manifest read — useful for one-off CLI installs). Both dirs are
  already on the compiler's extlibs search path.
- **Breaking-change gate.** Before replacing an installed version,
  :func:`compiler.apidiff.compare` runs against the previous
  install. If the SemVer bump is smaller than the detected delta
  the command refuses; ``--allow-breaking`` overrides.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from compiler import apidiff
from compiler.manifest import (
    Manifest,
    ManifestError,
    go_major,
    go_version_tuple,
    is_valid_module_name,
    is_valid_semver,
    satisfies,
)
from compiler.version import LAMC_VERSION


DEFAULT_REGISTRY = os.environ.get("LAMC_REGISTRY", "http://localhost:8765")
USER_EXTLIBS = Path.home() / ".lammergeier" / "extlibs"
LOCKFILE_NAME = "lamlib.lock.toml"


def _default_lamc_compat_range() -> str:
    major, minor, *_ = LAMC_VERSION.split(".")
    return f"^{major}.{minor}"


# ── Content-addressed cache ─────────────────────────────────────
#
# Two artefact kinds land in ``$LAMC_CACHE`` (default
# ``~/.lammergeier/cache``):
#
# - ``tarballs/<aa>/<sha>.tar.gz`` — registry tarballs, keyed by
#   their SHA-256. Two-character sharding keeps directory entry
#   counts sane.
# - ``git/<safe-url>.git/`` — bare clones, refreshed via
#   ``git fetch`` on subsequent installs. The bare layout means
#   no working tree to invalidate; users get a per-install
#   working tree by ``git clone <cache> <dst>`` (local protocol —
#   hardlinks, no network).
#
# Calls into the cache always go through the helpers below so a
# future ``lamc cache prune`` only needs to know about one set of
# paths.

def _cache_root() -> Path:
    """Return the cache root, honouring ``$LAMC_CACHE``."""
    override = os.environ.get("LAMC_CACHE")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".lammergeier" / "cache"


def _cache_tarball_path(sha256: str) -> Path:
    """Content-addressed location for a registry tarball.

    Two-char shard prefix keeps a single directory from filling up
    once a project has hundreds of pins."""
    return _cache_root() / "tarballs" / sha256[:2] / f"{sha256}.tar.gz"


_GIT_CACHE_KEY_RE = re.compile(r"[^A-Za-z0-9_.-]+")

def _cache_git_dir(url: str) -> Path:
    """Bare-clone directory for a git remote. Two clones of the
    same URL collapse to the same on-disk cache entry."""
    safe = _GIT_CACHE_KEY_RE.sub("_", url).strip("_")
    return _cache_root() / "git" / f"{safe}.git"


def _extlibs_dir(project: bool = True,
                 override: Optional[Path] = None) -> Path:
    """Where ``install`` writes libraries.

    ``project`` (the default) writes to ``<cwd>/extlibs/`` and is
    paired with reading + updating ``lamlib.toml`` /
    ``lamlib.lock.toml``. ``project=False`` (the ``--global`` flag
    at the CLI) writes to ``~/.lammergeier/extlibs/`` and skips all
    project-level state — the escape hatch for one-off installs."""
    if override is not None:
        return override
    if project:
        return Path.cwd() / "extlibs"
    return USER_EXTLIBS


class InstallError(Exception):
    """Surfaced to the CLI as a single-line error + non-zero exit."""


# ── Registry client ──────────────────────────────────────────

@dataclass
class VersionInfo:
    """One entry in a ``GET /api/v1/libraries/<name>`` listing."""
    version: str
    tarball: str    # absolute URL
    sha256:  str
    yanked:  bool = False


class Registry:
    """Minimal HTTP client for a Lam registry.

    Contract (three endpoints, all JSON except the tarball GET):

    ``GET /api/v1/libraries/<name>``
        ``{"name": str, "versions": [{"version","tarball","sha256"}]}``.
    ``GET /api/v1/libraries/<name>/<version>.tar.gz``
        Raw gzipped tarball.
    ``POST /api/v1/publish``
        ``multipart/form-data`` with a single ``file`` field.

    Scoped names (``@alice/lamwebp``) are URL-encoded once
    (``/api/v1/libraries/%40alice%2Flamwebp``).
    """

    def __init__(self, base_url: str, token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json",
             "User-Agent": "lamc-install/0.1"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def list_versions(self, name: str) -> List[VersionInfo]:
        """Sorted version list (descending) — callers pick ``[0]``
        for "latest compatible"."""
        encoded = urllib.parse.quote(name, safe="")
        req = urllib.request.Request(
            self._url(f"/api/v1/libraries/{encoded}"),
            headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise InstallError(f"library {name!r} not found in registry")
            raise InstallError(f"registry error: HTTP {e.code} {e.reason}")
        except urllib.error.URLError as e:
            raise InstallError(f"registry unreachable: {e.reason}")
        except TimeoutError:
            raise InstallError("registry request timed out")

        versions = [
            VersionInfo(
                version=v["version"],
                tarball=(v["tarball"] if v["tarball"].startswith("http")
                         else self._url(v["tarball"])),
                sha256=v.get("sha256", ""),
                yanked=bool(v.get("yanked", False)),
            )
            for v in data.get("versions", [])
        ]

        def _key(vi: VersionInfo):
            parts = vi.version.split("-", 1)[0].split(".")
            while len(parts) < 3:
                parts.append("0")
            try:
                return tuple(int(p) for p in parts[:3])
            except ValueError:
                return (0, 0, 0)

        return sorted(versions, key=_key, reverse=True)

    def download(self, info: VersionInfo, dst: Path,
                 offline: bool = False) -> Path:
        """Fetch ``info.tarball`` into ``dst`` and verify the sha256
        matches. Returns the tarball path.

        When ``info.sha256`` is set, the content-addressed cache is
        consulted first; a verified hit short-circuits the network
        call. On a cache miss with ``offline=True`` the call fails
        loudly rather than reaching for the network."""
        dst.mkdir(parents=True, exist_ok=True)
        basename = Path(urllib.parse.urlparse(info.tarball).path).name
        target = dst / basename

        cached = _cache_tarball_path(info.sha256) if info.sha256 else None

        # Cache hit — verify the bytes still match (a corrupt cache
        # would silently poison every install otherwise).
        if cached is not None and cached.exists():
            data = cached.read_bytes()
            actual = hashlib.sha256(data).hexdigest()
            if actual == info.sha256:
                target.write_bytes(data)
                return target
            # Drop the bad entry; we'll re-fetch (or fail in offline).
            try:
                cached.unlink()
            except OSError:
                pass

        if offline:
            raise InstallError(
                f"--offline: no cached tarball for {info.version!r} "
                f"(expected sha256={info.sha256[:12]}… at "
                f"{cached}).")

        req = urllib.request.Request(info.tarball, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
        except urllib.error.HTTPError as e:
            raise InstallError(f"download failed: HTTP {e.code}")
        except urllib.error.URLError as e:
            raise InstallError(f"download failed: {e.reason}")

        if info.sha256:
            actual = hashlib.sha256(data).hexdigest()
            if actual != info.sha256:
                raise InstallError(
                    f"sha256 mismatch for {info.version}: "
                    f"registry={info.sha256[:12]}… actual={actual[:12]}…")
            # Populate the cache for next time. Best-effort — a
            # full disk shouldn't fail the install we just succeeded
            # at downloading.
            if cached is not None:
                try:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(data)
                except OSError:
                    pass
        target.write_bytes(data)
        return target

    def publish(self, tarball: Path) -> Dict:
        """``POST /api/v1/publish`` — upload ``tarball`` as
        multipart form data. Returns the JSON response."""
        body, ctype = _multipart_encode("file", tarball.name,
                                        tarball.read_bytes())
        req = urllib.request.Request(
            self._url("/api/v1/publish"),
            data=body,
            headers={**self._headers(), "Content-Type": ctype},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                msg = e.read().decode("utf-8")
            except Exception:
                msg = e.reason
            raise InstallError(f"publish failed: HTTP {e.code} — {msg}")
        except urllib.error.URLError as e:
            raise InstallError(f"publish failed: {e.reason}")


def _multipart_encode(field: str, filename: str,
                      content: bytes) -> Tuple[bytes, str]:
    """Stdlib-only single-field multipart encoder — we refuse to
    pull in ``requests`` just for publish."""
    boundary = f"----lamcboundary{os.urandom(8).hex()}"
    lines = [
        f"--{boundary}".encode(),
        (f'Content-Disposition: form-data; name="{field}"; '
         f'filename="{filename}"').encode(),
        b"Content-Type: application/gzip",
        b"",
        content,
        f"--{boundary}--".encode(),
        b"",
    ]
    return b"\r\n".join(lines), f"multipart/form-data; boundary={boundary}"


# ── Git source adapter ──────────────────────────────────────

_GIT_SPEC_RE = re.compile(
    r"^(?P<url>(?:https?|git|ssh|file)://\S+?|git@[^:]+?:\S+?)"
    r"(?:@(?P<ref>[^@\s]+))?$"
)


def _looks_like_git(spec: str) -> bool:
    return bool(_GIT_SPEC_RE.match(spec)) or spec.endswith(".git")


def _clone_git(spec: str, dst: Path,
               offline: bool = False) -> Tuple[Path, str]:
    """Clone ``spec`` (``<url>[@<ref>]``) into ``dst``. Returns
    ``(<dst>, <ref-or-commit>)``.

    Backed by a bare clone in ``$LAMC_CACHE/git/`` so:

    - The first install of a repo populates the cache; subsequent
      installs do a cheap ``git fetch`` against the cache and a
      local clone from the cache into ``dst`` (hardlink-fast,
      zero network).
    - ``offline=True`` refuses any remote contact: a cache hit
      proceeds (without fetch); a cache miss raises.

    The ref is checked out in ``dst`` (not in the bare cache),
    so two installs pinning different refs of the same repo share
    the same object store but get independent working trees."""
    if shutil.which("git") is None:
        raise InstallError("`git` not found on PATH — cannot clone from git")

    m = _GIT_SPEC_RE.match(spec)
    if m:
        url, ref = m.group("url"), m.group("ref") or ""
    else:
        url, ref = spec, ""

    cache_dir = _cache_git_dir(url)

    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)

    try:
        # 1. Populate / refresh the bare cache.
        if cache_dir.exists():
            if not offline:
                # Best-effort fetch: a single failure (offline net,
                # rate-limit) shouldn't prevent install if the cache
                # already has the requested ref. Errors here are
                # downgraded to a warning and we proceed against
                # the existing object store.
                try:
                    subprocess.run(
                        ["git", "-C", str(cache_dir), "fetch",
                         "--quiet", "--tags", "--force", "origin"],
                        check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError as e:
                    print(
                        f"[lamc] cache fetch failed for {url} "
                        f"(continuing with stale cache): "
                        f"{e.stderr.strip() or e.stdout.strip()}",
                        file=sys.stderr)
        else:
            if offline:
                raise InstallError(
                    f"--offline: no git cache for {url!r} (expected at "
                    f"{cache_dir}).")
            cache_dir.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--quiet", "--bare", url, str(cache_dir)],
                check=True, capture_output=True, text=True)

        # 2. Clone from the cache to dst (local protocol — fast).
        subprocess.run(
            ["git", "clone", "--quiet", "--local",
             str(cache_dir), str(dst)],
            check=True, capture_output=True, text=True)

        # 3. Check out the requested ref (or stay on default HEAD).
        if ref:
            subprocess.run(
                ["git", "-C", str(dst), "checkout", "--quiet", ref],
                check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise InstallError(
            f"git clone failed: {e.stderr.strip() or e.stdout.strip()}")

    # Resolve the actual commit we ended up on so the lockfile
    # records something immutable.
    try:
        commit = subprocess.run(
            ["git", "-C", str(dst), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
    except subprocess.CalledProcessError:
        commit = ref or "HEAD"

    shutil.rmtree(dst / ".git", ignore_errors=True)
    return dst, commit


# ── Tarball helpers ──────────────────────────────────────────

# Every file extracted from a tarball must live entirely under
# ``into``. Without this guard a malicious registry could ship a
# tarball whose entries use ``../../../etc/passwd`` as the name and
# trick us into writing outside the install dir. We check each
# member's resolved destination stays under ``into`` and also veto
# symlinks that would point out of the tree after extraction.
def _safe_extract(tar: tarfile.TarFile, into: Path) -> None:
    """``tarfile.extractall`` with path-traversal guards."""
    into = into.resolve()
    for m in tar.getmembers():
        member_path = (into / m.name).resolve()
        if into not in member_path.parents and member_path != into:
            raise InstallError(f"tarball entry escapes install dir: {m.name!r}")
        if m.issym() or m.islnk():
            link = (member_path.parent / m.linkname).resolve()
            if into not in link.parents and link != into:
                raise InstallError(
                    f"tarball symlink escapes install dir: {m.name!r}")
    tar.extractall(into)


def _pack_directory(src: Path, name: str, version: str,
                    into: Path) -> Path:
    """Produce ``<name>-<version>.tar.gz`` under ``into``, rooted
    at ``<name>-<version>/`` inside the archive. Scoped names are
    flattened to ``scope__name`` for the tarball filename so it's
    shell-safe on every platform."""
    into.mkdir(parents=True, exist_ok=True)
    safe = name.replace("/", "__")
    if safe.startswith("@"):
        safe = safe[1:]
    archive = into / f"{safe}-{version}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(str(src), arcname=f"{safe}-{version}",
                filter=_tar_filter)
    return archive


def _tar_filter(ti: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    """Drop files we never want to publish (build artefacts, caches,
    editor state, local vendored deps)."""
    base = os.path.basename(ti.name)
    if base in {".git", ".venv", "__pycache__", ".pytest_cache",
                ".DS_Store", "build", "extlibs"}:
        return None
    if any(part.startswith(".") and part not in {".", "..",
                                                  ".gitignore",
                                                  ".editorconfig"}
           for part in ti.name.split("/")):
        return None
    if ti.name.endswith((".pyc", ".pyo", ".tar.gz")):
        return None
    return ti


# ── Install destination helpers ─────────────────────────────

def _lib_dir(extlibs: Path, name: str) -> Path:
    """Filesystem layout for an installed library. Scoped names
    keep their ``/`` so the resolver finds ``@alice/lamwebp``
    under ``extlibs/@alice/lamwebp/``."""
    return extlibs / name


def _installed_manifest(extlibs: Path, name: str) -> Optional[Manifest]:
    """Return the manifest of the currently-installed version, or
    ``None`` if the library isn't installed."""
    d = _lib_dir(extlibs, name)
    mf = d / "lamlib.toml"
    if mf.exists():
        try:
            return Manifest.load(mf)
        except ManifestError:
            return None
    return None


# ── Lockfile (``lamlib.lock.toml``) ──────────────────────────

def _load_lockfile(path: Path) -> Dict:
    """Parse the project's lockfile if present. Returns the raw
    dict so callers can manipulate it without a round-trip through
    the full manifest validator."""
    if not path.exists():
        return {"pins": {}}
    from compiler.manifest import _parse_toml  # shared toml
    try:
        tree = _parse_toml(path.read_text(encoding="utf-8"))
    except ManifestError as e:
        raise InstallError(f"malformed {path.name}: {e}")
    tree.setdefault("pins", {})
    return tree


def _write_lockfile(path: Path, pins: Dict[str, Dict],
                    go_pins: Optional[Dict[str, Dict]] = None) -> None:
    """Emit the lockfile. Deterministic: keys sorted, one library
    per section, so ``git diff`` on the file tells the truth.

    Schema v1 layout:

    - ``[meta] schema = 1`` at the top — versions the file so we
      can migrate later without breaking parsers.
    - One ``[pins.<name>]`` per resolved Lam library, with optional
      ``tree_sha256`` (for git/path sources) and ``requested_by``
      (a TOML array of trail strings, ``"root"`` for direct deps).
    - One ``[go_pins.<path>]`` per resolved Go module, also with a
      ``requested_by`` array.

    Migration: a v0 lockfile (no ``[meta]`` block, ``requested_by``
    as a single string for go pins, no ``tree_sha256``) loads fine
    via :func:`_load_lockfile`; the next write through this function
    silently upgrades it to v1. No user-facing tool needed."""
    lines = ["# Auto-generated by `lamc install` — do not edit by hand.",
             "# Commit this file so your teammates resolve the same",
             "# dependency set.", "",
             "[meta]",
             f"schema = {LOCKFILE_SCHEMA}",
             ""]
    for name in sorted(pins):
        entry = pins[name]
        lines.append(f"[pins.{_lock_key(name)}]")
        lines.append(f'name    = "{name}"')
        for key in ("version", "source", "sha256", "url", "ref",
                    "requested_ref", "tree_sha256"):
            if key in entry and entry[key]:
                lines.append(f'{key} = "{entry[key]}"')
        rby = entry.get("requested_by") or []
        if rby:
            normalised = sorted({_normalize_requestor(s) for s in rby})
            lines.append(f"requested_by = {_toml_str_array(normalised)}")
        lines.append("")
    if go_pins:
        for mod_path in sorted(go_pins):
            entry = go_pins[mod_path]
            lines.append(f"[go_pins.{_lock_key(mod_path)}]")
            lines.append(f'path    = "{mod_path}"')
            if "version" in entry and entry["version"]:
                lines.append(f'version = "{entry["version"]}"')
            rby = entry.get("requested_by")
            # v0 stored a scalar string; v1 stores a TOML array.
            # Accept both shapes on input, always emit array on output.
            if rby:
                if isinstance(rby, str):
                    rby = [rby]
                normalised = sorted({_normalize_requestor(s) for s in rby})
                lines.append(f"requested_by = {_toml_str_array(normalised)}")
            lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _lock_key(name: str) -> str:
    """Lockfile sections must be legal TOML keys. Scoped names
    (``@alice/lamwebp``) need a safe alias — we quote them. Plain
    names pass through unchanged."""
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        return name
    return f'"{name}"'


# ── Tree hashing (for git / path sources) ───────────────────

_TREE_HASH_SKIP = frozenset({".git", "__pycache__", ".DS_Store",
                             ".pytest_cache", ".mypy_cache",
                             "node_modules", "extlibs"})

def _tree_sha256(root: Path) -> str:
    """Deterministic SHA-256 of a directory tree.

    Hashes ``(relative-path ‖ NUL ‖ file-bytes ‖ NUL)`` for every
    regular file in lexicographic order. VCS metadata, build caches
    and the ``extlibs/`` install tree are skipped — those are not
    part of the source identity and would make the hash flap on
    every clone / install.

    Recorded in the lockfile as ``tree_sha256`` for ``git`` and
    ``path`` sources so ``lamc verify`` (and a future ``--frozen``
    re-check) can detect drift without re-fetching from upstream."""
    if not root.is_dir():
        raise InstallError(f"_tree_sha256: not a directory: {root}")
    h = hashlib.sha256()
    files: List[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(seg in _TREE_HASH_SKIP for seg in rel.parts):
            continue
        files.append(rel)
    files.sort()
    for rel in files:
        h.update(str(rel).encode("utf-8"))
        h.update(b"\x00")
        h.update((root / rel).read_bytes())
        h.update(b"\x00")
    return h.hexdigest()


# ── Lockfile v1 helpers ─────────────────────────────────────

LOCKFILE_SCHEMA = 1

def _normalize_requestor(s: str) -> str:
    """Map resolver-internal labels to user-friendly lockfile values.

    The resolver tags demands with ``<requested>`` (the explicit
    install target), ``<project>:<name>`` (a constraint from the
    project's own manifest), or ``<installed>:<name>@<ver>`` (a
    constraint contributed by an already-on-disk peer). For the
    lockfile we collapse the first two to ``"root"`` (the project
    or the explicit request — from the user's view, both are "I
    asked for this") and strip the ``<installed>:`` prefix so the
    list reads as a plain ``<name>@<ver>`` trail."""
    if s == "<requested>":
        return "root"
    if s.startswith("<project>:"):
        return "root"
    if s.startswith("<installed>:"):
        return s[len("<installed>:"):]
    return s


def _toml_str_array(values: List[str]) -> str:
    """Emit a TOML array of strings on a single line. Values must
    not contain a literal ``"`` — the resolver-produced strings
    don't, and we don't want to drag a full TOML escaper in for
    one corner case."""
    parts = [f'"{v}"' for v in values]
    return "[" + ", ".join(parts) + "]"


# ── Breaking-change gate ────────────────────────────────────

def _check_semver_gate(name: str,
                       old: Manifest,
                       new_dir: Path,
                       new_version: str,
                       allow_breaking: bool) -> None:
    """Compare the current-install surface to the freshly-fetched
    one and refuse to proceed if the SemVer bump lies about the
    actual change. ``--allow-breaking`` bypasses the refusal but
    still prints the delta for auditability."""
    old_dir = old.source_path.parent if old.source_path else None
    if not old_dir or not old_dir.is_dir():
        return
    old_surface = apidiff.surface_from_path(old_dir)
    new_surface = apidiff.surface_from_path(new_dir)
    changes = apidiff.compare(old_surface, new_surface)

    detected = apidiff.worst_severity(changes)
    claimed  = apidiff.expected_bump(old.version, new_version)
    rank = {"patch": 0, "feature": 1, "breaking": 2}

    if rank[detected] > rank[claimed]:
        banner = (f"[lamc] API drift detected for {name}: "
                  f"version bump is {claimed} but the code "
                  f"contains {detected} changes.\n"
                  f"  {old.version} → {new_version}")
        print(banner, file=sys.stderr)
        for c in changes:
            if c.severity == "breaking":
                print(f"  {c}", file=sys.stderr)
        if not allow_breaking:
            raise InstallError(
                "refusing to install a mis-labelled release; "
                "rerun with `--allow-breaking` to override")


# ── Install ──────────────────────────────────────────────────

@dataclass
class InstallPlan:
    """Resolved plan before we touch the install tree."""
    name:        str
    version:     str
    source:      str           # "registry" | "git" | "path"
    src_dir:     Path          # where the fetched sources live
    manifest:    Manifest
    sha256:      str = ""
    url:         str = ""
    ref:         str = ""
    requested_ref: str = ""
    # Deterministic hash of the fetched source tree. Filled in for
    # ``git`` and ``path`` sources where there's no registry tarball
    # whose ``sha256`` already provides integrity. Empty for
    # registry sources — ``sha256`` is authoritative there.
    tree_sha256: str = ""


def _parse_spec(spec: str) -> Tuple[str, str]:
    """Split ``<name>[@<version>]`` into ``(name, version)``. Does
    not validate either half — callers decide whether it's a
    registry spec, a git URL, or a local path."""
    if "@" in spec and not spec.startswith("@"):
        # ``name@ver`` — but watch out for ``@scope/name`` where the
        # ``@`` is a leading scope marker, not the version separator.
        head, _, tail = spec.rpartition("@")
        return head, tail
    if spec.startswith("@"):
        # ``@scope/name[@ver]``
        rest = spec[1:]
        if "@" in rest:
            scoped, ver = rest.rsplit("@", 1)
            return "@" + scoped, ver
        return spec, ""
    return spec, ""


def _apply_replace(spec: str,
                   project_mf: Optional[Manifest]) -> str:
    """Rewrite ``spec`` through the project's ``[replace]`` table
    if it has a matching override.

    The lookup key is the *library name* parsed out of ``spec`` —
    a ``name@version`` spec, a scoped ``@alice/lib`` spec, and a
    bare ``name`` all dispatch on the same key. When the project
    manifest declares a replacement, the override wins regardless
    of the original source kind: a registry spec can be redirected
    to a path, a path spec to a git URL, etc. Library-level
    ``[replace]`` blocks are ignored at install time so a
    transitive dep can't sneak in a rewrite of an unrelated lib
    behind the user's back; only the project's own manifest is
    consulted (matches Go's ``go.mod`` ``replace`` semantics).
    """
    if project_mf is None or not project_mf.replace:
        return spec
    # Don't rewrite raw paths or git URLs the user passed
    # explicitly — those are an opt-out from registry resolution
    # entirely and shouldn't be silently re-rewritten.
    if (spec.startswith(("./", "/", "../"))
            or _looks_like_git(spec)):
        return spec
    name, _ = _parse_spec(spec)
    rs = project_mf.replace.get(name)
    if rs is None:
        return spec
    base_dir = (project_mf.source_path.parent
                if project_mf.source_path else None)
    return rs.to_install_spec(base_dir)


def _resolve_plan(spec: str,
                  registry: Registry,
                  work: Path) -> InstallPlan:
    """Fetch sources for ``spec`` into ``work/<name>-<ver>`` and
    return the shape the installer uses to copy them to their
    final home. No network calls happen once this returns."""

    # Local path?
    if spec.startswith("./") or spec.startswith("/") or spec.startswith("../"):
        src = Path(spec).resolve()
        if not src.is_dir():
            raise InstallError(f"path source not a directory: {src}")
        mf = Manifest.load(src / "lamlib.toml")
        return InstallPlan(
            name=mf.name, version=mf.version, source="path",
            src_dir=src, manifest=mf,
            tree_sha256=_tree_sha256(src))

    # Git URL?
    if _looks_like_git(spec):
        m = _GIT_SPEC_RE.match(spec)
        url = (m.group("url") if m else spec)
        ref = (m.group("ref") if m else "") or ""
        clone_dst = work / "git-clone"
        _, commit = _clone_git(spec, clone_dst)
        mf_path = clone_dst / "lamlib.toml"
        if not mf_path.exists():
            raise InstallError(
                f"cloned repo {url} has no lamlib.toml — cannot install")
        mf = Manifest.load(mf_path)
        return InstallPlan(
            name=mf.name, version=mf.version, source="git",
            src_dir=clone_dst, manifest=mf,
            url=url, ref=commit, requested_ref=ref,
            tree_sha256=_tree_sha256(clone_dst))

    # Registry.
    name, version = _parse_spec(spec)
    versions = registry.list_versions(name)
    if not versions:
        raise InstallError(f"registry has no versions for {name!r}")

    chosen: Optional[VersionInfo] = None
    if not version:
        for v in versions:
            if not v.yanked:
                chosen = v; break
    else:
        # Exact or range match
        for v in versions:
            if v.yanked:
                continue
            if v.version == version or satisfies(v.version, version):
                chosen = v; break
    if chosen is None:
        raise InstallError(
            f"no version of {name} satisfies {version or '<latest>'} "
            f"(available: {', '.join(v.version for v in versions[:8])}…)")

    tarball_path = registry.download(chosen, work)
    extract_dir = work / "extract"
    extract_dir.mkdir(exist_ok=True)
    with tarfile.open(tarball_path, "r:gz") as tar:
        _safe_extract(tar, extract_dir)

    # The archive is rooted at ``<name>-<version>/`` — grab the
    # single subdir (whatever its name is, the registry may have
    # used a flattened ``scope__name`` alias).
    kids = [p for p in extract_dir.iterdir() if p.is_dir()]
    if len(kids) != 1:
        raise InstallError(
            f"tarball layout invalid: expected one root directory, "
            f"got {[k.name for k in kids]}")
    root = kids[0]
    mf = Manifest.load(root / "lamlib.toml")

    return InstallPlan(
        name=mf.name, version=mf.version, source="registry",
        src_dir=root, manifest=mf,
        sha256=chosen.sha256, url=chosen.tarball,
        # Also record the content hash of the extracted tree so
        # ``lamc verify`` can compare on-disk extlibs against the
        # lockfile without re-fetching from the registry.
        tree_sha256=_tree_sha256(root))


# ── Transitive dependency resolution ────────────────────────

class DependencyConflict(InstallError):
    """Raised when two libraries (or a library + the project) pin
    irreconcilable version ranges of the same dependency.

    Carries the offending ``name`` and a list of ``(constraint,
    requested_by)`` pairs so callers can render a useful error or
    a structured trace for tests / CI."""

    def __init__(self, name: str,
                 demands: List[Tuple[str, str]],
                 kind: str = "lammergeier") -> None:
        self.name = name
        self.demands = demands
        self.kind = kind
        lines = [f"version conflict for {kind} dependency {name!r}:"]
        for spec, who in demands:
            lines.append(f"  - {who} requires {spec!r}")
        super().__init__("\n".join(lines))


@dataclass
class ResolvedDep:
    """One node in a resolved dependency graph.

    ``requested_by`` is ordered breadth-first so a transitive trail
    like ``app → lamhttp → lamlog`` reads top-to-bottom in error
    messages."""
    name:         str
    version:      str
    plan:         InstallPlan
    requested_by: List[str] = field(default_factory=list)


def _project_manifest(cwd: Path) -> Optional[Manifest]:
    """Load the project's ``lamlib.toml`` if there is one.

    The installer treats the project manifest as a peer dep node:
    its ``[dependencies]`` add constraints to the resolver, its
    ``[go-deps]`` join the Go-module merge. Libraries that pin a
    major incompatible with the project's pin are rejected so the
    project's intent always wins (without us silently downgrading
    the lib's request)."""
    p = cwd / "lamlib.toml"
    if not p.exists():
        return None
    try:
        return Manifest.load(p)
    except ManifestError:
        # A malformed project manifest shouldn't poison every install
        # with conflict errors — surface it once at the top of the
        # CLI, then fall back to "no project constraints".
        return None


def _installed_manifests(extlibs: Path) -> List[Manifest]:
    """Return every ``lamlib.toml`` already on disk under
    ``extlibs/``.

    Used by the resolver to honour the *existing* set of constraints
    when installing a new library: e.g. if ``lamone@1.0.0`` is
    already installed and depends on ``lamshared ^1``, that demand
    must still hold when we later install ``lamtwo`` which would
    upgrade ``lamshared`` to ^2 — otherwise we'd silently break
    every consumer of the still-installed ``lamone``.

    Best-effort: malformed manifests are skipped, scoped libs are
    walked one extra level deep (``extlibs/@scope/<name>``)."""
    if not extlibs.exists() or not extlibs.is_dir():
        return []
    out: List[Manifest] = []
    for entry in extlibs.iterdir():
        if not entry.is_dir():
            continue
        # Scoped: walk one level into ``@scope/<name>``.
        if entry.name.startswith("@"):
            for child in entry.iterdir():
                mf_path = child / "lamlib.toml"
                if mf_path.exists():
                    try:
                        out.append(Manifest.load(mf_path))
                    except ManifestError:
                        continue
            continue
        mf_path = entry / "lamlib.toml"
        if mf_path.exists():
            try:
                out.append(Manifest.load(mf_path))
            except ManifestError:
                continue
    return out


def _ranges_intersect(a: str, b: str) -> bool:
    """Cheap "do these two ranges share at least one concrete
    SemVer version?" check.

    We don't have a full constraint solver — that'd be a
    spec-grade Pubgrub re-implementation. Instead we sample the
    smallest version in each range's tail and the largest version
    in each range's head, then take the union of "candidate"
    points and accept if any of them satisfies BOTH ranges. The
    candidate set covers every operator combination the manifest
    parser admits (``^``, ``~``, ``>=``, ``<``, exact, wildcard).

    Wildcards (``*`` / empty) trivially intersect with anything."""
    if not a.strip() or a.strip() in ("*", "any"): return True
    if not b.strip() or b.strip() in ("*", "any"): return True

    candidates: set[str] = set()
    for spec in (a, b):
        m = re.match(r"^[~^]?(\d+)(?:\.(\d+))?(?:\.(\d+))?", spec.strip())
        if m:
            mj = int(m.group(1) or 0)
            mn = int(m.group(2) or 0)
            pa = int(m.group(3) or 0)
            candidates.update({
                f"{mj}.{mn}.{pa}",
                f"{mj}.{mn}.{pa + 1}",
                f"{mj}.{mn + 1}.0",
                f"{mj + 1}.0.0",
            })
        for tok in re.findall(r"\d+\.\d+\.\d+", spec):
            candidates.add(tok)
    candidates.add("0.0.0")
    return any(satisfies(c, a) and satisfies(c, b) for c in candidates)


def _pick_version(name: str,
                  registry: Registry,
                  combined_spec: str,
                  demands: List[Tuple[str, str]]) -> "VersionInfo":
    """Choose the highest non-yanked registry version that
    satisfies *every* demand. Raises :class:`DependencyConflict`
    if no version does."""
    versions = registry.list_versions(name)
    if not versions:
        raise InstallError(f"registry has no versions for {name!r}")
    for v in versions:
        if v.yanked:
            continue
        if all(satisfies(v.version, spec) for spec, _ in demands):
            return v
    raise DependencyConflict(name, demands)


def _walk_dependencies(root_plan: InstallPlan,
                       registry: Registry,
                       work: Path,
                       project_mf: Optional[Manifest],
                       installed: Optional[List[Manifest]],
                       quiet: bool) -> Tuple[List[ResolvedDep],
                                             Dict[str, Tuple[str, str]]]:
    """Breadth-first walk over the transitive dependency graph
    rooted at ``root_plan``.

    Returns ``(install_order, go_deps)`` where:

    - ``install_order`` is a list of :class:`ResolvedDep` ordered
      so leaves come before the libraries that need them. This
      lets the caller install in one pass without a second
      topological sort.
    - ``go_deps`` is a flat ``{module: (version, requested_by)}``
      map of merged Go-module requirements across the whole tree.
      Conflicts (different majors) raise before we return.

    Project-level ``[dependencies]`` and ``[go-deps]`` are folded
    in as if the project were the implicit topmost node — so a lib
    that pins ``lamhttp ^2`` while the project already pins
    ``lamhttp ^1`` is reported as a project-vs-lib conflict, not
    silently re-resolved.

    ``installed`` is the list of already-on-disk manifests; their
    ``[dependencies]`` / ``[go-deps]`` join the demand set so a
    second install can't silently invalidate constraints from a
    first install (the classic "two libs share a dep at
    incompatible majors" scenario)."""
    # accumulated demands per Lam dep + per Go module
    lam_demands: Dict[str, List[Tuple[str, str]]] = {}
    go_demands:  Dict[str, List[Tuple[str, str]]] = {}

    if project_mf is not None:
        for n, ds in project_mf.dependencies.items():
            if ds.range:
                lam_demands.setdefault(n, []).append(
                    (ds.range, f"<project>:{project_mf.name}"))
        for path, ver in project_mf.go_deps.items():
            go_demands.setdefault(path, []).append(
                (ver, f"<project>:{project_mf.name}"))

    # Already-installed libs contribute their own constraints. Note
    # we DON'T add a ``=<installed-version>`` self-constraint for the
    # lib itself — the new install request is allowed to upgrade /
    # downgrade in-place, that's the SemVer gate's job. We only
    # honour their *transitive* demands so an upgrade can't break a
    # peer that's still on disk.
    for mf in (installed or []):
        if mf.name == root_plan.name:
            # The root we're about to (re)install — its demands come
            # from the freshly-resolved manifest below, not the old
            # version's manifest. Skip.
            continue
        for n, ds in mf.dependencies.items():
            if ds.range:
                lam_demands.setdefault(n, []).append(
                    (ds.range, f"<installed>:{mf.name}@{mf.version}"))
        for path, ver in mf.go_deps.items():
            go_demands.setdefault(path, []).append(
                (ver, f"<installed>:{mf.name}@{mf.version}"))

    # Start with the root manifest's own deps.
    resolved: Dict[str, ResolvedDep] = {}
    queue: List[Tuple[Manifest, str]] = [(root_plan.manifest, "<requested>")]
    install_order: List[ResolvedDep] = []
    seen_descended: set[str] = {root_plan.name}
    resolved[root_plan.name] = ResolvedDep(
        name=root_plan.name, version=root_plan.version,
        plan=root_plan, requested_by=["<requested>"])

    # Fold the root's own Go deps + Lam deps into the demand set
    # under the root's own name (so error messages stay precise).
    for path, ver in root_plan.manifest.go_deps.items():
        go_demands.setdefault(path, []).append(
            (ver, f"{root_plan.name}@{root_plan.version}"))
    for n, ds in root_plan.manifest.dependencies.items():
        if ds.range:
            lam_demands.setdefault(n, []).append(
                (ds.range, f"{root_plan.name}@{root_plan.version}"))
        elif (ds.path or ds.git) and root_plan.manifest.source_path is not None:
            spec = _dep_install_spec(n, ds, root_plan.manifest.source_path.parent)
            if spec:
                sub_plan = _resolve_plan(
                    _apply_replace(spec, project_mf), registry,
                    work=work / f"dep-{_safe_segment(n)}")
                seen_label = f"{root_plan.name}@{root_plan.version}"
                entry = ResolvedDep(
                    name=n, version=sub_plan.version, plan=sub_plan,
                    requested_by=[seen_label])
                resolved[n] = entry
                install_order.append(entry)
                seen_descended.add(n)
                queue.append((sub_plan.manifest,
                              f"{n}@{sub_plan.version}"))

    # Walk the queue: for each manifest, fetch its deps' manifests
    # (registry-side), enqueue them, accumulate demand lists.
    while queue:
        mf, requested_by = queue.pop(0)
        for n, ds in mf.dependencies.items():
            if ds.range:
                lam_demands.setdefault(n, []).append((ds.range, requested_by))
            # Path deps from non-root libs are not followed — by the
            # time a lib hits the registry it must declare portable
            # dependencies. Direct git deps are portable, so they are
            # followed below.
        for path, ver in mf.go_deps.items():
            go_demands.setdefault(path, []).append((ver, requested_by))

        for n, ds in mf.dependencies.items():
            if n in seen_descended:
                continue
            if ds.git:
                spec = _dep_install_spec(n, ds, mf.source_path.parent if mf.source_path else None)
                if not spec:
                    continue
                sub_plan = _resolve_plan(
                    _apply_replace(spec, project_mf), registry,
                    work=work / f"dep-{_safe_segment(n)}")
                seen_descended.add(n)
                entry = ResolvedDep(
                    name=n, version=sub_plan.version, plan=sub_plan,
                    requested_by=[requested_by])
                resolved[n] = entry
                install_order.append(entry)
                queue.append((sub_plan.manifest,
                              f"{n}@{sub_plan.version}"))
                continue
            if not ds.range:
                continue
            # Resolve the version against ALL accumulated demands so
            # far so we never pick a lib version we'll later have to
            # revoke. Because we walk breadth-first, every demand for
            # ``n`` we'll ever see is already in ``lam_demands[n]``
            # by the time we reach the deepest node — except for
            # demands introduced *by* the lib we're about to fetch.
            # Any such later demand has to be re-checked against the
            # already-pinned version below, and triggers a conflict
            # if no overlap exists.
            chosen = _pick_version(n, registry, ds.range,
                                   lam_demands[n])
            # Project-level ``[replace]`` overrides apply
            # transitively too: if the user replaces ``lamhttp``
            # with a local checkout, every lib that pulls in
            # ``lamhttp`` resolves to that checkout, matching
            # Go's ``go.mod replace`` semantics.
            sub_spec = _apply_replace(
                f"{n}@{chosen.version}", project_mf)
            sub_plan = _resolve_plan(
                sub_spec, registry,
                work=work / f"dep-{_safe_segment(n)}")
            seen_descended.add(n)
            entry = ResolvedDep(
                name=n, version=sub_plan.version, plan=sub_plan,
                requested_by=[d[1] for d in lam_demands[n]])
            resolved[n] = entry
            install_order.append(entry)
            queue.append((sub_plan.manifest,
                          f"{n}@{sub_plan.version}"))

    # ── Final consistency sweep ─────────────────────────────
    # By now ``resolved[name].version`` reflects the version we
    # picked when we first encountered the lib. A *later* demand
    # might require a version outside that pick — verify all of
    # them against what's actually installable.
    for n, demands in lam_demands.items():
        if n not in resolved:
            # Project-only dep (not pulled in by any lib) — skip
            # the resolution step but still validate that the
            # demand list is internally consistent so the install
            # CLI surfaces the conflict before launching a second
            # ``lamc install <project-dep>`` round-trip.
            for i in range(len(demands)):
                for j in range(i + 1, len(demands)):
                    if not _ranges_intersect(demands[i][0], demands[j][0]):
                        raise DependencyConflict(n, demands)
            continue
        chosen = resolved[n].version
        for spec, who in demands:
            if not satisfies(chosen, spec):
                raise DependencyConflict(n, demands)

    # Go-module conflicts — different majors of the same path are
    # genuinely irreconcilable (Go treats each major as a different
    # package). Within the same major, pick the highest-pinned
    # version (Go's MVS rule).
    go_resolved: Dict[str, Tuple[str, str]] = {}
    for path, demands in go_demands.items():
        majors = {go_major(v) for v, _ in demands}
        if len(majors) > 1:
            raise DependencyConflict(path, demands, kind="go")
        # Pick the highest pinned version (MVS).
        chosen = max(demands, key=lambda d: go_version_tuple(d[0]))
        go_resolved[path] = chosen

    if not quiet and (install_order or go_resolved):
        if install_order:
            print(f"[lamc] resolved {len(install_order)} transitive "
                  f"Lam dep(s): "
                  f"{', '.join(d.name + '@' + d.version for d in install_order)}",
                  file=sys.stderr)
        if go_resolved:
            print(f"[lamc] resolved {len(go_resolved)} Go module "
                  f"requirement(s): "
                  f"{', '.join(p + '@' + v[0] for p, v in go_resolved.items())}",
                  file=sys.stderr)

    return install_order, go_resolved


def _safe_segment(name: str) -> str:
    """Filesystem-safe directory name for a (possibly scoped) lib
    used as a per-dep work subdirectory under the install tempdir."""
    return name.replace("/", "__").lstrip("@")


def install_one(spec: str,
                project: bool = True,
                extlibs_override: Optional[Path] = None,
                registry: Optional[Registry] = None,
                allow_breaking: bool = False,
                force: bool = False,
                quiet: bool = False) -> InstallPlan:
    """Install a single library plus all its transitive Lam deps.

    Returns the root plan so callers (the CLI, tests, higher-level
    ``install --upgrade`` loops) can inspect what landed. Each
    transitive dep also lands on disk; conflicts surface as a
    :class:`DependencyConflict` before any install side-effects.

    ``project`` (the default) installs into ``<cwd>/extlibs/``,
    reads the project's ``lamlib.toml`` for constraint-merge, and
    writes ``lamlib.lock.toml``. Pass ``project=False`` (CLI:
    ``--global``) for the user-wide install path that skips all
    project state — useful for ad-hoc usage outside any project."""
    registry = registry or Registry(DEFAULT_REGISTRY)
    extlibs = _extlibs_dir(project, extlibs_override)
    project_mf = _project_manifest(Path.cwd()) if project else None
    installed = _installed_manifests(extlibs)

    with tempfile.TemporaryDirectory(prefix="lamc-install-") as tmp:
        work = Path(tmp)
        # Honour the project's ``[replace]`` directive so a top-
        # level install of ``lamwebp`` can be transparently
        # redirected to a local checkout without modifying
        # ``[dependencies]``.
        plan = _resolve_plan(
            _apply_replace(spec, project_mf), registry, work)

        # Walk the full transitive dep graph BEFORE we touch the
        # install tree. This keeps the on-disk state consistent —
        # a conflict mid-install would otherwise leave the user
        # with a half-installed lib.
        deps, go_resolved = _walk_dependencies(
            plan, registry, work, project_mf, installed, quiet)

        # Install dependencies first (leaves before the requesting
        # lib) so partial failure leaves the smallest mess.
        for dep in deps:
            _materialise_one(dep.plan, extlibs, allow_breaking,
                             force, quiet)

        # SemVer / breaking-change gate for the explicitly-requested
        # lib (the deps already passed their own gate inside
        # ``_materialise_one``).
        current = _installed_manifest(extlibs, plan.name)
        if current is not None and current.version == plan.version and not force:
            if not quiet:
                print(f"[lamc] {plan.name}@{plan.version} already installed")
        elif current is not None:
            _check_semver_gate(plan.name, current, plan.src_dir,
                               plan.version, allow_breaking)
            _materialise_one(plan, extlibs, allow_breaking, force,
                             quiet)
        else:
            _materialise_one(plan, extlibs, allow_breaking, force,
                             quiet)

    # Lockfile update when we're inside a project. We pin EVERY
    # node in the resolved graph + the merged Go-module set so the
    # install is byte-reproducible across machines.
    proj_lock = (Path.cwd() / LOCKFILE_NAME) if project else None
    if proj_lock is not None:
        lock = _load_lockfile(proj_lock)
        lock["pins"][plan.name] = {
            "version":      plan.version,
            "source":       plan.source,
            "sha256":       plan.sha256,
            "url":          plan.url,
            "ref":          plan.ref,
            "requested_ref": plan.requested_ref,
            "tree_sha256":  plan.tree_sha256,
            "requested_by": ["root"],
        }
        for dep in deps:
            lock["pins"][dep.name] = {
                "version":      dep.plan.version,
                "source":       dep.plan.source,
                "sha256":       dep.plan.sha256,
                "url":          dep.plan.url,
                "ref":          dep.plan.ref,
                "requested_ref": dep.plan.requested_ref,
                "tree_sha256":  dep.plan.tree_sha256,
                "requested_by": list(dep.requested_by),
            }
        # Go-module requirements live under their own table so the
        # compile-time go.mod synthesiser can read them out without
        # walking each lib's ``[go-deps]`` independently.
        if go_resolved:
            lock["go_pins"] = {
                path: {"version":      ver,
                       "requested_by": [who]}
                for path, (ver, who) in go_resolved.items()
            }
        elif "go_pins" in lock:
            # Empty go-deps removes the table entirely, so the
            # lockfile stays minimal when nobody uses Go modules.
            lock.pop("go_pins")
        _write_lockfile(proj_lock, lock["pins"],
                        go_pins=lock.get("go_pins"))

    return plan


def _materialise_one(plan: InstallPlan,
                     extlibs: Path,
                     allow_breaking: bool,
                     force: bool,
                     quiet: bool) -> None:
    """Copy ``plan.src_dir`` into the extlibs tree, applying the
    SemVer gate when an older version is already on disk. Split
    out from :func:`install_one` so the transitive walker can
    install dependencies one-by-one without re-running its own
    resolver per node."""
    current = _installed_manifest(extlibs, plan.name)
    if current is not None and current.version == plan.version and not force:
        if not quiet:
            print(f"[lamc] {plan.name}@{plan.version} already installed")
        return
    if current is not None:
        _check_semver_gate(plan.name, current, plan.src_dir,
                           plan.version, allow_breaking)
    dst = _lib_dir(extlibs, plan.name)
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(plan.src_dir, dst)
    if not quiet:
        print(f"[lamc] installed {plan.name}@{plan.version} "
              f"({plan.source}) → {dst}")


# ── Uninstall ────────────────────────────────────────────────

def uninstall_one(name: str, project: bool = True,
                  extlibs_override: Optional[Path] = None,
                  quiet: bool = False) -> bool:
    extlibs = _extlibs_dir(project, extlibs_override)
    dst = _lib_dir(extlibs, name)
    if not dst.exists():
        if not quiet:
            print(f"[lamc] {name} is not installed")
        return False
    shutil.rmtree(dst)
    # Clean up an empty scope directory so ``@alice`` doesn't linger.
    parent = dst.parent
    if parent != extlibs and parent.exists() and not any(parent.iterdir()):
        parent.rmdir()
    if not quiet:
        print(f"[lamc] removed {name}")

    proj_lock = Path.cwd() / LOCKFILE_NAME
    if proj_lock.exists():
        lock = _load_lockfile(proj_lock)
        if name in lock["pins"]:
            lock["pins"].pop(name, None)
            _write_lockfile(proj_lock, lock["pins"])
    return True


# ── Publish ──────────────────────────────────────────────────

def publish_one(src_dir: Path,
                registry: Optional[Registry] = None,
                quiet: bool = False) -> Dict:
    """Pack ``src_dir`` into a tarball and POST it to the registry.

    Runs the breaking-change detector against the *registry's*
    latest version so a publisher also can't accidentally ship a
    mis-labelled release — the same check the installer applies in
    reverse."""
    registry = registry or Registry(DEFAULT_REGISTRY)
    mf_path = src_dir / "lamlib.toml"
    if not mf_path.exists():
        raise InstallError(f"no lamlib.toml under {src_dir}")
    mf = Manifest.load(mf_path)

    # Pre-publish surface-drift warning. Non-fatal on publish;
    # registries can enforce strictly server-side if they want to.
    try:
        versions = registry.list_versions(mf.name)
    except InstallError:
        versions = []
    if versions:
        with tempfile.TemporaryDirectory(prefix="lamc-publish-") as tmp:
            prev = versions[0]
            prev_dir = Path(tmp) / "prev"
            tar_path = registry.download(prev, Path(tmp))
            prev_dir.mkdir()
            with tarfile.open(tar_path, "r:gz") as t:
                _safe_extract(t, prev_dir)
            kids = [p for p in prev_dir.iterdir() if p.is_dir()]
            if kids:
                old_surface = apidiff.surface_from_path(kids[0])
                new_surface = apidiff.surface_from_path(src_dir)
                changes = apidiff.compare(old_surface, new_surface)
                detected = apidiff.worst_severity(changes)
                claimed  = apidiff.expected_bump(prev.version, mf.version)
                rank = {"patch": 0, "feature": 1, "breaking": 2}
                if rank[detected] > rank[claimed] and not quiet:
                    print(f"[lamc] WARNING: {mf.name} bump is {claimed} but "
                          f"code shows {detected} changes", file=sys.stderr)
                    for c in changes:
                        if c.severity == "breaking":
                            print(f"   {c}", file=sys.stderr)

    with tempfile.TemporaryDirectory(prefix="lamc-publish-") as tmp:
        archive = _pack_directory(src_dir, mf.name, mf.version, Path(tmp))
        resp = registry.publish(archive)
        if not quiet:
            print(f"[lamc] published {mf.name}@{mf.version} to "
                  f"{registry.base_url} ({archive.stat().st_size} bytes)")
        return resp


# ── argparse entry points ───────────────────────────────────

def _add_common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--registry", default=DEFAULT_REGISTRY,
                   help=f"Registry base URL (default: {DEFAULT_REGISTRY})")
    # ``--global`` collides with Python's ``global`` keyword once
    # argparse synthesises an attribute name from the flag, so we
    # store it under ``install_global`` instead.
    p.add_argument("--global", dest="install_global",
                   action="store_true",
                   help="Install into ~/.lammergeier/extlibs/ (no "
                        "lockfile, no project-manifest read). "
                        "Default is per-project ./extlibs/ + "
                        "lamlib.lock.toml.")
    p.add_argument("--extlibs-dir", metavar="DIR", default=None,
                   help="Override install destination (tests use this).")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="Suppress status chatter.")


# ── Manifest writer (line-scanning, preserves comments) ───────────
#
# We have no full TOML round-tripper in-tree (deliberate — keeps
# the toolchain stdlib-only). Adding a dep to ``lamlib.toml`` after
# ``lamc install <spec>`` therefore goes through a focused line
# scanner that:
#
# 1. Locates the existing ``[dependencies]`` block (if any).
# 2. Either updates the matching ``<name> = ...`` line in place, or
#    appends one inside the section.
# 3. Adds a fresh section at end-of-file if no dependencies exist.
#
# Comments, blank lines, ordering of other sections, and whatever
# trailing data the file has are all preserved — we only ever
# touch the lines we own.

_MANIFEST_KEY_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")

def _toml_quote_key(name: str) -> str:
    if _MANIFEST_KEY_SAFE.match(name):
        return name
    return f'"{name}"'


def _toml_quote_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _find_section_bounds(lines: List[str],
                          section: str) -> Tuple[Optional[int], int]:
    """Return ``(start_idx, end_idx)`` for ``[section]`` in ``lines``.

    ``start_idx`` is the 0-indexed line containing the header,
    or None if the section is absent. ``end_idx`` is the line
    *after* the last line of the section's body (i.e. the first
    line not belonging to it). Sub-tables (``[section.sub]``) count
    as part of the parent so we don't accidentally leak across."""
    target = f"[{section}]"
    start: Optional[int] = None
    for i, line in enumerate(lines):
        if line.strip() == target:
            start = i
            break
    if start is None:
        return None, len(lines)
    sub_prefix = f"[{section}."
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if s.startswith("[") and s.endswith("]") and not s.startswith(sub_prefix):
            return start, j
    return start, len(lines)


def _write_manifest_dep(mf_path: Path, name: str, value: str,
                        quiet: bool = False) -> None:
    """Add or update ``<name> = <value>`` under ``[dependencies]`` in
    ``mf_path``.

    ``value`` is the raw TOML right-hand side: a quoted string for
    version ranges (``"^1.2.0"``) or an inline table for path / git
    forms (``{ path = "../x" }``). The function never alters comments
    or other sections.
    """
    text = mf_path.read_text(encoding="utf-8")
    # Preserve the original line-ending style (mostly LF; rare CRLF).
    keep_trailing_nl = text.endswith("\n")
    lines = text.splitlines()

    quoted = _toml_quote_key(name)
    new_line = f"{quoted} = {value}"

    start, end = _find_section_bounds(lines, "dependencies")

    action: str
    if start is None:
        # No [dependencies] table yet. Append one at end-of-file with
        # one blank line of breathing room.
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("[dependencies]")
        lines.append(new_line)
        action = "added"
    else:
        # Existing section. Try to find a key whose name matches
        # (either as the bare identifier or as the quoted form).
        bare_re = re.compile(rf"^\s*{re.escape(name)}\s*=")
        quoted_re = re.compile(rf'^\s*"{re.escape(name)}"\s*=')
        replaced_at = None
        for i in range(start + 1, end):
            if bare_re.match(lines[i]) or quoted_re.match(lines[i]):
                lines[i] = new_line
                replaced_at = i
                break
        if replaced_at is None:
            # Insert just before the section's trailing blank lines so
            # the new entry sits adjacent to its peers.
            insert_at = end
            while (insert_at > start + 1
                   and not lines[insert_at - 1].strip()):
                insert_at -= 1
            lines.insert(insert_at, new_line)
            action = "added"
        else:
            action = "updated"

    out = "\n".join(lines)
    if keep_trailing_nl:
        out += "\n"
    mf_path.write_text(out, encoding="utf-8")

    if not quiet:
        print(f"[lamc] {action} {name} in {mf_path.name}")


def _format_dep_value(plan: InstallPlan, user_spec: str) -> Optional[str]:
    """Render the right-hand side of ``<name> = <value>`` for a
    successful install. Returns ``None`` for sources we don't yet
    record in the manifest (currently: git — the schema gains a
    ``git``/``ref`` form when the ``[replace]`` directive lands).
    """
    if plan.source == "registry":
        # Caret-pin to the installed version: the most permissive
        # range that stays SemVer-compatible. npm / Cargo default.
        return f'"^{plan.version}"'
    if plan.source == "path":
        # Preserve the user's literal spec when it's relative — the
        # other devs cloning the repo need the same string. Absolute
        # paths get recorded as-is; the user owns portability there.
        escaped = user_spec.replace('"', '\\"')
        return '{ path = "' + escaped + '" }'
    if plan.source == "git":
        m = _GIT_SPEC_RE.match(user_spec)
        url = m.group("url") if m else plan.url
        ref = (m.group("ref") if m else "") or plan.requested_ref
        parts = [f"git = {_toml_quote_value(url)}"]
        if ref:
            parts.append(f"ref = {_toml_quote_value(ref)}")
        return "{ " + ", ".join(parts) + " }"
    return None


def _maybe_write_manifest_entry(plan: InstallPlan, user_spec: str,
                                quiet: bool) -> None:
    """Best-effort: record a fresh dependency in the project's
    ``lamlib.toml`` so manifest + lockfile + on-disk tree stay in
    lockstep.

    Failures are downgraded to a warning so a manifest-write hiccup
    (read-only checkout, exotic line endings) doesn't undo a
    successful install. ``lamc tidy`` (Phase 3) repairs drift."""
    mf_path = Path.cwd() / "lamlib.toml"
    if not mf_path.exists():
        return
    value = _format_dep_value(plan, user_spec)
    if value is None:
        return
    try:
        _write_manifest_dep(mf_path, plan.name, value, quiet=quiet)
    except OSError as e:
        print(f"[lamc] warning: could not update {mf_path.name}: {e}",
              file=sys.stderr)


def _dep_install_spec(name: str, ds, base_dir: Path | None = None) -> str | None:
    if ds.range:
        return f"{name}@{ds.range}"
    if ds.path:
        local = Path(ds.path)
        if not local.is_absolute() and base_dir is not None:
            local = (base_dir / local).resolve()
        return str(local)
    if ds.git:
        return f"{ds.git}@{ds.ref}" if ds.ref else ds.git
    return None


def _specs_from_project_manifest() -> List[str]:
    """Translate the project's ``[dependencies]`` table into the
    install-spec strings the resolver consumes. Used by bare
    ``lamc install`` (no positional args) to install everything
    declared in ``lamlib.toml`` without re-reading the file at every
    layer."""
    mf_path = Path.cwd() / "lamlib.toml"
    if not mf_path.exists():
        raise InstallError(
            f"no {mf_path.name} in {Path.cwd()} — run from a project root "
            f"or pass an explicit spec.")
    mf = Manifest.load(mf_path)
    specs: List[str] = []
    for name, ds in mf.dependencies.items():
        spec = _dep_install_spec(name, ds, mf_path.parent)
        if spec:
            specs.append(spec)
    return specs


# ── Lockfile-driven install (--frozen / --offline) ──────────────────
#
# ``--frozen`` validates that ``lamlib.toml`` and ``lamlib.lock.toml``
# agree, then materialises every pin recorded in the lockfile —
# no resolver, no ``Registry.list_versions`` call, no manifest
# write-back. The lockfile is law.
#
# ``--offline`` propagates to ``Registry.download`` / ``_clone_git``,
# which fail loudly on cache miss. Because resolving range specs
# without the network is impossible (we'd need ``list_versions`` to
# pick a satisfying version), ``--offline`` implies ``--frozen``.
#
# Together they're the canonical Docker / CI invocation:
#
#     lamc install --frozen --offline
#
# which guarantees the build sees exactly the bytes the lockfile
# describes — no upstream drift, no surprise pulls.

def _plan_from_pin(name: str, pin: Dict, registry: Registry,
                   work: Path, offline: bool) -> InstallPlan:
    """Re-fetch the source tree pinned in the lockfile and return
    the install plan. No resolution — the pin's ``source`` /
    ``sha256`` / ``url`` / ``ref`` fields uniquely identify the
    bytes we want."""
    source = pin.get("source", "")
    version = pin.get("version", "")
    sha256 = pin.get("sha256", "")
    url = pin.get("url", "")
    ref = pin.get("ref", "")
    requested_ref = pin.get("requested_ref", "")

    if source == "registry":
        if not url:
            raise InstallError(
                f"frozen: pin '{name}' has no tarball url — lockfile "
                f"is missing data, run `lamc install` to refresh.")
        info = VersionInfo(version=version, tarball=url,
                           sha256=sha256)
        sub_work = work / _safe_segment(name)
        sub_work.mkdir(parents=True, exist_ok=True)
        tarball_path = registry.download(info, sub_work, offline=offline)
        extract_dir = sub_work / "extract"
        extract_dir.mkdir(exist_ok=True)
        with tarfile.open(tarball_path, "r:gz") as tar:
            _safe_extract(tar, extract_dir)
        kids = [p for p in extract_dir.iterdir() if p.is_dir()]
        if len(kids) != 1:
            raise InstallError(
                f"frozen: tarball for {name} extracted to {len(kids)} "
                f"root dirs (expected 1)")
        root = kids[0]
        mf = Manifest.load(root / "lamlib.toml")
        return InstallPlan(
            name=mf.name, version=mf.version, source="registry",
            src_dir=root, manifest=mf,
            sha256=sha256, url=url,
            tree_sha256=_tree_sha256(root))

    if source == "git":
        if not url:
            raise InstallError(
                f"frozen: pin '{name}' has no git url — lockfile "
                f"is missing data.")
        spec = f"{url}@{ref}" if ref else url
        clone_dst = work / f"git-{_safe_segment(name)}"
        _, commit = _clone_git(spec, clone_dst, offline=offline)
        mf_path = clone_dst / "lamlib.toml"
        if not mf_path.exists():
            raise InstallError(
                f"frozen: cloned repo for {name} has no lamlib.toml")
        mf = Manifest.load(mf_path)
        return InstallPlan(
            name=mf.name, version=mf.version, source="git",
            src_dir=clone_dst, manifest=mf,
            url=url, ref=commit, requested_ref=requested_ref,
            tree_sha256=_tree_sha256(clone_dst))

    if source == "path":
        # Path sources can't be fully reproduced from a lockfile
        # alone — the path is local. The on-disk install in
        # ``./extlibs/<name>/`` is already the truth in this case;
        # frozen mode just trusts it. If it's missing we have no
        # way to recreate it without re-running the user's original
        # ``lamc install ./...`` command.
        raise InstallError(
            f"frozen: pin '{name}' is a path source — not "
            f"reproducible from the lockfile alone. Install it "
            f"manually with `lamc install <path>` or replace it "
            f"with a registry / git source.")

    raise InstallError(
        f"frozen: pin '{name}' has unknown source kind {source!r}")


def _frozen_install(project: bool, override: Optional[Path],
                    registry: Registry, offline: bool,
                    quiet: bool) -> int:
    """Install every pin in ``lamlib.lock.toml`` after validating
    it against ``lamlib.toml``. The resolver is not run — the
    lockfile is authoritative. Returns the CLI exit code."""
    cwd = Path.cwd()
    mf_path = cwd / "lamlib.toml"
    lock_path = cwd / LOCKFILE_NAME

    if not mf_path.exists():
        print(f"[lamc] error: --frozen requires {mf_path.name} in {cwd}",
              file=sys.stderr)
        return 2
    if not lock_path.exists():
        print(f"[lamc] error: --frozen requires {lock_path.name} — "
              f"run `lamc install` first to generate one.",
              file=sys.stderr)
        return 2

    try:
        mf = Manifest.load(mf_path)
    except ManifestError as e:
        print(f"[lamc] error: {mf_path.name}: {e}", file=sys.stderr)
        return 2
    lock = _load_lockfile(lock_path)
    pins: Dict[str, Dict] = lock.get("pins") or {}

    # Drift check: every manifest dep must have a satisfying pin.
    drift: List[str] = []
    for name, ds in mf.dependencies.items():
        pin = pins.get(name)
        if pin is None:
            drift.append(
                f"  - {name}: declared in {mf_path.name} but absent "
                f"from {lock_path.name}")
            continue
        pin_version = pin.get("version", "")
        if ds.range and pin_version and not satisfies(pin_version, ds.range):
            drift.append(
                f"  - {name}: {mf_path.name} requires {ds.range!r}, "
                f"{lock_path.name} pins {pin_version!r}")
        if ds.git:
            pin_source = pin.get("source", "")
            pin_url = pin.get("url", "")
            pin_requested_ref = pin.get("requested_ref", "")
            if pin_source != "git":
                drift.append(
                    f"  - {name}: {mf_path.name} declares a git source, "
                    f"{lock_path.name} pins source {pin_source!r}")
            elif pin_url != ds.git:
                drift.append(
                    f"  - {name}: {mf_path.name} requires git {ds.git!r}, "
                    f"{lock_path.name} pins {pin_url!r}")
            elif ds.ref and pin_requested_ref and pin_requested_ref != ds.ref:
                drift.append(
                    f"  - {name}: {mf_path.name} requires git ref {ds.ref!r}, "
                    f"{lock_path.name} pins requested ref {pin_requested_ref!r}")
    if drift:
        print("[lamc] error: --frozen: lockfile and manifest drift:",
              file=sys.stderr)
        for line in drift:
            print(line, file=sys.stderr)
        print("  run `lamc install` (without --frozen) to refresh.",
              file=sys.stderr)
        return 1

    extlibs = _extlibs_dir(project, override)
    extlibs.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lamc-frozen-") as tmp:
        work = Path(tmp)
        for name in sorted(pins):
            try:
                plan = _plan_from_pin(name, pins[name], registry,
                                      work, offline)
            except InstallError as e:
                print(f"[lamc] error materialising {name}: {e}",
                      file=sys.stderr)
                return 1
            # ``allow_breaking=True``: the lockfile already encodes
            # the user's intent; the SemVer gate would only fire
            # because an older copy is on disk, which is exactly
            # what frozen mode is replacing.
            try:
                _materialise_one(plan, extlibs, allow_breaking=True,
                                 force=True, quiet=quiet)
            except InstallError as e:
                print(f"[lamc] error installing {name}: {e}",
                      file=sys.stderr)
                return 1

    if not quiet:
        print(f"[lamc] frozen install complete — {len(pins)} "
              f"pin(s) materialised from {lock_path.name}")
    return 0


def _cmd_install(args) -> int:
    reg = Registry(args.registry, token=os.environ.get("LAMC_TOKEN"))
    override = Path(args.extlibs_dir).resolve() if args.extlibs_dir else None
    project = not args.install_global
    offline = bool(getattr(args, "offline", False))
    frozen = bool(getattr(args, "frozen", False)) or offline

    user_specs: List[str] = list(args.specs)

    # ``--frozen`` operates on the lockfile, not on user-supplied
    # specs. Combining the two is ambiguous — refuse rather than
    # silently picking one interpretation.
    if frozen and user_specs:
        print("[lamc] error: --frozen / --offline cannot be combined "
              "with positional specs (they read the lockfile).",
              file=sys.stderr)
        return 2
    if frozen and not project:
        print("[lamc] error: --frozen / --offline require project mode "
              "(remove --global).", file=sys.stderr)
        return 2

    if frozen:
        return _frozen_install(project, override, reg, offline,
                               args.quiet)

    bare = not user_specs

    if bare:
        # ``lamc install`` (no args) only makes sense in project mode
        # — we need a manifest to read.
        if not project:
            print("[lamc] error: bare `lamc install` reads the project's "
                  "lamlib.toml and is incompatible with --global.",
                  file=sys.stderr)
            return 2
        try:
            user_specs = _specs_from_project_manifest()
        except (InstallError, ManifestError) as e:
            print(f"[lamc] error: {e}", file=sys.stderr)
            return 2
        if not user_specs:
            if not args.quiet:
                print("[lamc] no dependencies declared in lamlib.toml — "
                      "nothing to install.")
            return 0

    failures = 0
    for spec in user_specs:
        try:
            plan = install_one(
                spec,
                project=project,
                extlibs_override=override,
                registry=reg,
                allow_breaking=args.allow_breaking,
                force=args.force,
                quiet=args.quiet,
            )
        except InstallError as e:
            print(f"[lamc] error installing {spec}: {e}", file=sys.stderr)
            failures += 1
            continue

        # Only sync the manifest when the user explicitly asked us to
        # add a dep (i.e. supplied a positional spec). Bare
        # ``lamc install`` is reading the manifest, not modifying it.
        if project and not bare:
            _maybe_write_manifest_entry(plan, spec, args.quiet)

    return 0 if failures == 0 else 1


def _cmd_uninstall(args) -> int:
    override = Path(args.extlibs_dir).resolve() if args.extlibs_dir else None
    for name in args.names:
        try:
            uninstall_one(
                name,
                project=not args.install_global,
                extlibs_override=override,
                quiet=args.quiet,
            )
        except InstallError as e:
            print(f"[lamc] error uninstalling {name}: {e}", file=sys.stderr)
            return 1
    return 0


# ── Phase 3: ``lamc tidy`` / ``lamc verify`` ────────────────
#
# Both verbs read the project's ``lamlib.toml`` + ``lamlib.lock.toml``
# and reconcile them against ground truth — ``tidy`` against the
# project's actual ``import`` graph, ``verify`` against the on-disk
# extlibs trees. Neither verb takes positional library arguments;
# they're project-scoped maintenance commands.
#
# We deliberately keep the import-scanning regex tolerant: a file
# that fails to parse mid-edit shouldn't break ``lamc tidy``.

_TIDY_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(?P<from_mod>@?[\w./@-]+)\s+import\b"
    r"|import\s+(?P<imp_mod>@?[\w./@-]+))",
    re.MULTILINE,
)

# Stdlib module names — ship with the compiler so they should
# never appear in a user's ``[dependencies]``. Kept here as a
# string set for tidy's own use; the canonical list lives in
# :mod:`compiler.semantic`. The tiny duplication avoids a heavy
# import for a verb-level helper.
_STDLIB_MODS = frozenset({
    "lammath", "lamstrings", "lamtime", "lamconv", "lamos",
    "lamre", "lamjson", "lamhttp", "lamrandom", "lamhash",
    "lampath", "lamsort", "lamstats", "lamsys", "lamenv",
    "lamlog", "lamcsv", "lamerr", "lamerrors", "lamfmt",
    "lamtest", "lamtypes", "lamio", "lamnet", "lamsecurity",
    "lamunicode", "lamurl", "lambase64", "lamcompress",
    "lamcrypto", "lamdatetime", "lamarray", "lamdata",
    "lamiter", "lamuuid", "lamcache", "lambytes",
    "lamtemplate", "lamratelimit", "lamretry", "lamexec",
    "lamset", "lamqueue", "lamstack", "lamheap", "lamdeque",
    "lamcollections", "lamfunctools", "lamitertools",
    "lamthreading", "lamasync", "lamconcurrency", "lamactor",
    "lamdb", "lammigrate", "lamredis", "lamemcached",
    "lamcron", "lamsmtp",
    "lamserver", "lamserver_ws", "lamserver_plugins",
    "lamserver_tus", "lamschema", "lamjwt", "lamprotobuf",
    "lamcli",
})


def _tidy_find_manifest(start: Path) -> Optional[Path]:
    """Walk upward from ``start`` looking for ``lamlib.toml``. Bound
    the search to six levels (matching the rest of the manifest-
    discovery helpers) so a script in ``/tmp`` doesn't pick up an
    unrelated grandparent's manifest.
    """
    here = start.resolve()
    for _ in range(6):
        cand = here / "lamlib.toml"
        if cand.exists():
            return cand
        if here.parent == here:
            break
        here = here.parent
    return None


def _tidy_scan_imports(project_root: Path) -> Dict[str, Path]:
    """Return ``{module_name: first_seen_path}`` for every ``from X
    import …`` / ``import X`` line in the project's ``.lam`` files.
    Skips ``extlibs/`` and the usual auto-generated directories so we
    never count a third-party lib's own imports against the user's
    manifest.
    """
    skip = {"extlibs", ".git", "build", "__pycache__", "node_modules"}
    seen: Dict[str, Path] = {}
    for path in project_root.rglob("*"):
        if path.is_dir() or path.suffix != ".lam":
            continue
        rel_parts = path.relative_to(project_root).parts
        if any(part in skip for part in rel_parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in _TIDY_IMPORT_RE.finditer(text):
            mod = m.group("from_mod") or m.group("imp_mod")
            if not mod:
                continue
            seen.setdefault(mod, path)
            if "." in mod and not mod.startswith("@"):
                seen.setdefault(mod.split(".", 1)[0], path)
    return seen


def _remove_manifest_dep(mf_path: Path, name: str,
                         quiet: bool = False) -> bool:
    """Delete ``<name>`` from the ``[dependencies]`` table of
    ``mf_path``. Returns ``True`` if a line was actually removed.

    Mirrors :func:`_write_manifest_dep`'s line-based editing so we
    preserve every other section, comment, and blank line. Quoted
    keys (``"@scope/name"``) and bare keys are both handled.
    """
    text = mf_path.read_text(encoding="utf-8")
    keep_trailing_nl = text.endswith("\n")
    lines = text.splitlines()

    start, end = _find_section_bounds(lines, "dependencies")
    if start is None:
        return False

    bare_re = re.compile(rf"^\s*{re.escape(name)}\s*=")
    quoted_re = re.compile(rf'^\s*"{re.escape(name)}"\s*=')
    removed = False
    new_lines: List[str] = []
    for i, line in enumerate(lines):
        if start < i < end and (bare_re.match(line) or quoted_re.match(line)):
            removed = True
            continue
        new_lines.append(line)

    if not removed:
        return False

    out = "\n".join(new_lines)
    if keep_trailing_nl:
        out += "\n"
    mf_path.write_text(out, encoding="utf-8")
    if not quiet:
        print(f"[lamc] removed {name} from {mf_path.name}")
    return True


def _installed_version(extlibs_root: Path, name: str) -> Optional[str]:
    """Return the version string from ``extlibs/<name>/lamlib.toml``
    if the library is currently installed, else ``None``. Used by
    ``tidy`` to fill in a sensible caret-pin for a missing entry."""
    cand = extlibs_root / name / "lamlib.toml"
    if not cand.exists():
        return None
    try:
        mf = Manifest.load(cand)
    except (ManifestError, OSError):
        return None
    return mf.version or None


def _cmd_tidy(args) -> int:
    """Sync ``lamlib.toml`` ``[dependencies]`` with the project's
    actual import graph.

    Logic, matching ``go mod tidy`` in spirit:

    1. Scan every ``.lam`` under the project root (``extlibs/`` and
       friends pruned) for ``from X import …`` / ``import X``.
    2. Drop stdlib hits — those don't need a manifest entry.
    3. Compare the residual set against ``[dependencies]``:
       - ``unused`` = declared but not imported anywhere.
       - ``missing`` = imported but not declared.
    4. Apply (or, with ``--check``, just report) the diff:
       - Unused entries are deleted.
       - Missing entries are added with a caret pin derived from
         ``extlibs/<name>/lamlib.toml`` (the install on disk is the
         source of truth — ``tidy`` doesn't reach the network).
         Names that aren't installed yet are left for the user to
         resolve with ``lamc install <name>``; we still report them
         so the diff is honest.
    5. After mutation, re-run ``--frozen`` install to refresh the
       lockfile so ``lamlib.toml`` and ``lamlib.lock.toml`` agree.

    ``--check`` exits non-zero with a diff plan if any change is
    needed. The CI knob: ``lamc tidy --check`` in pre-commit.
    """
    cwd = Path.cwd()
    mf_path = _tidy_find_manifest(cwd)
    if mf_path is None:
        print("[lamc] tidy: no lamlib.toml found in cwd or any parent.",
              file=sys.stderr)
        return 2

    project_root = mf_path.parent
    try:
        mf = Manifest.load(mf_path)
    except ManifestError as e:
        print(f"[lamc] tidy: {mf_path.name} is malformed: {e}",
              file=sys.stderr)
        return 2

    declared = set(mf.dependencies.keys())
    imports = set(_tidy_scan_imports(project_root).keys())
    used = {m for m in imports if m not in _STDLIB_MODS}

    unused = sorted(declared - used)
    missing = sorted(used - declared)
    extlibs_root = project_root / "extlibs"

    # Bucket missing entries by whether we can pin them from the
    # current install. Ones we can't pin are still reported but not
    # auto-added — the user has to ``lamc install <name>`` first.
    auto_addable: List[Tuple[str, str]] = []   # (name, "^x.y.z")
    needs_install: List[str] = []
    for name in missing:
        ver = _installed_version(extlibs_root, name)
        if ver:
            auto_addable.append((name, f'"^{ver}"'))
        else:
            needs_install.append(name)

    if not unused and not auto_addable and not needs_install:
        if not args.quiet:
            print(f"[lamc] tidy: {mf_path.name} is already in sync "
                  f"with the project's imports.")
        return 0

    # Render the plan.
    if unused:
        print("would remove (declared but never imported):")
        for n in unused:
            print(f"  - {n}")
    if auto_addable:
        print("would add (imported, version inferred from extlibs/):")
        for n, v in auto_addable:
            print(f"  + {n} = {v}")
    if needs_install:
        print("imported but not installed (run `lamc install <name>` first):")
        for n in needs_install:
            print(f"  ? {n}")

    if args.check:
        return 0 if not (unused or auto_addable or needs_install) else 1

    if needs_install:
        print(f"[lamc] tidy: refusing to mutate {mf_path.name} while "
              f"some imports aren't installed. Run `lamc install` for "
              f"those names first, then re-run `lamc tidy`.",
              file=sys.stderr)
        return 1

    # Apply the diff.
    for name in unused:
        _remove_manifest_dep(mf_path, name, quiet=args.quiet)
    for name, value in auto_addable:
        _write_manifest_dep(mf_path, name, value, quiet=args.quiet)

    # Refresh the lockfile so it matches the new manifest. We use
    # the same ``--frozen``-equivalent path: read the manifest,
    # install everything declared, write the lockfile. Any drift
    # detection happens via the SemVer / API-diff gate as usual.
    #
    # ``install_one`` and ``_specs_from_project_manifest`` both
    # resolve paths against ``Path.cwd()``, so we chdir to the
    # project root for the duration. Without this, running
    # ``lamc tidy`` from a subdirectory would find the manifest via
    # the upward walk but then fail to refresh because the install
    # primitives would look for ``lamlib.toml`` next to the cwd.
    if not args.quiet:
        print("[lamc] tidy: refreshing lockfile…")
    reg = Registry(DEFAULT_REGISTRY, token=os.environ.get("LAMC_TOKEN"))
    failures = 0
    saved_cwd = Path.cwd()
    try:
        os.chdir(project_root)
        try:
            for spec in _specs_from_project_manifest():
                try:
                    install_one(spec, registry=reg, quiet=args.quiet)
                except InstallError as e:
                    print(f"[lamc] tidy: could not refresh {spec}: {e}",
                          file=sys.stderr)
                    failures += 1
        except (InstallError, ManifestError) as e:
            print(f"[lamc] tidy: lockfile refresh failed: {e}",
                  file=sys.stderr)
            return 1
    finally:
        os.chdir(saved_cwd)
    return 0 if failures == 0 else 1


def _verify_pin(name: str, pin: Dict, extlibs_root: Path) -> Tuple[bool, str]:
    """Compare the on-disk install of ``name`` to the lockfile pin.

    Returns ``(ok, detail)``. ``ok=False`` either means the library
    isn't installed at all or its current ``tree_sha256`` differs
    from the lockfile's record. ``detail`` is a one-line human-
    readable summary suitable for printing.
    """
    src = extlibs_root / name
    if not src.is_dir():
        return False, f"missing on disk (expected at {src})"
    expected = pin.get("tree_sha256") or ""
    if not expected:
        # Older v0 / v1 lockfiles for registry pins didn't carry
        # tree_sha256. We fall back to a soft "present" check.
        return True, "present (no tree_sha256 recorded — re-run `lamc install` to populate)"
    actual = _tree_sha256(src)
    if actual != expected:
        return False, (f"tree drifted: expected {expected[:12]}…, "
                       f"got {actual[:12]}…")
    return True, f"ok ({actual[:12]}…)"


def _cmd_verify(args) -> int:
    """Re-hash every installed extlib and compare against the
    lockfile. Surfaces supply-chain integrity violations: tampering
    with installed source, partial installs, lockfile/disk drift
    after a manual edit.

    Exit codes: ``0`` clean, ``1`` any mismatch, ``2`` setup error
    (no lockfile, malformed lockfile, etc.). The latter is intended
    to be distinguishable in CI from a real integrity failure.
    """
    cwd = Path.cwd()
    mf_path = _tidy_find_manifest(cwd)
    if mf_path is None:
        print("[lamc] verify: no lamlib.toml found in cwd or any parent.",
              file=sys.stderr)
        return 2
    project_root = mf_path.parent
    lock_path = project_root / LOCKFILE_NAME
    if not lock_path.exists():
        print(f"[lamc] verify: no {LOCKFILE_NAME} found at {project_root}.",
              file=sys.stderr)
        return 2

    lock = _load_lockfile(lock_path)
    pins: Dict[str, Dict] = lock.get("pins") or {}
    if not pins:
        if not args.quiet:
            print(f"[lamc] verify: {LOCKFILE_NAME} has no [pins.*] table — "
                  f"nothing to check.")
        return 0

    extlibs_root = project_root / "extlibs"
    failures: List[Tuple[str, str]] = []
    for name in sorted(pins):
        ok, detail = _verify_pin(name, pins[name], extlibs_root)
        if not args.quiet:
            marker = "✓" if ok else "✗"
            print(f"  {marker} {name:<30} {detail}")
        if not ok:
            failures.append((name, detail))

    if failures:
        print(f"[lamc] verify: {len(failures)} integrity failure"
              f"{'s' if len(failures) != 1 else ''}:",
              file=sys.stderr)
        for name, detail in failures:
            print(f"  - {name}: {detail}", file=sys.stderr)
        print("  → run `lamc install --frozen` to repair from the lockfile, "
              "or `lamc install` to re-resolve.", file=sys.stderr)
        return 1
    if not args.quiet:
        print(f"[lamc] verify: all {len(pins)} pin"
              f"{'s' if len(pins) != 1 else ''} match the lockfile.")
    return 0


# ── Phase 4.2: ``lamc init`` ────────────────────────────────
#
# Scaffolds a fresh project in the current directory. The verb is
# deliberately flag-driven (no interactive prompts) so it composes
# cleanly with shell scripts and Dockerfiles. Defaults are picked
# to make the zero-flag form ``lamc init`` produce something
# you can immediately ``lamc main.lam --run`` against.

_INIT_BIN_TEMPLATE = """\
func main() {
    print("hello, lammergeier!");
}
"""

# Library scaffolding: a single function whose return value
# carries the version string so the canonical "is this lib
# wired up correctly?" smoke-test is just ``Lib.tag()``.
_INIT_LIB_TEMPLATE = """\
# {name} — {version}

func tag() -> str {{
    return "{name}@{version}";
}}
"""

_INIT_MANIFEST_TEMPLATE = """\
[library]
name    = "{name}"
version = "{version}"
license = "{license}"

[compatibility]
# Range of ``lamc`` versions this project has been tested
# against. Caret matches SemVer.
lamc = "{lamc_compat}"

[dependencies]
# Add third-party libraries here:
# lamwebp = "^1.2"
# Or run ``lamc install lamwebp@^1.2`` and let the installer
# write the entry for you.
"""

_INIT_GITIGNORE = """\
# Lammergeier build artefacts
/build/
*.lamc-cache/

# Project-local installed libraries — uncomment to commit them
# (Go-style "vendor everything"). Keeping them out of git is the
# default; the lockfile alone is enough to reproduce the build.
/extlibs/
"""


def _safe_default_name(cwd: Path) -> str:
    """Return a legal module name based on the current directory.

    ``Path.name`` may be empty (root) or contain hyphens / digits
    that the module-name validator rejects. Preserve the directory's
    letter case, replacing only characters that cannot appear in a Lam
    identifier. Fall back to ``"myproj"`` when nothing usable comes
    back so the no-flag invocation always produces a valid manifest.
    """
    raw = cwd.name.replace("-", "_")
    candidate = "".join(c for c in raw if c.isalnum() or c == "_").lstrip("_")
    if candidate and is_valid_module_name(candidate):
        return candidate
    return "myproj"


def _cmd_init(args) -> int:
    """Scaffold a fresh Lammergeier project in the current
    directory.

    Generates ``lamlib.toml``, an entry-point file (``main.lam``
    for ``--bin``, ``<name>.lam`` for ``--lib``), and a
    ``.gitignore``. The verb is non-destructive by default —
    ``--force`` is required to overwrite any existing file.

    Flags:
      ``--name NAME``       project module name (default: cwd's
                            base name, sanitised to a legal
                            identifier).
      ``--version VER``     SemVer string (default ``0.1.0``).
      ``--scope @SCOPE``    optional scope; the final name becomes
                            ``@scope/name``.
      ``--license SPDX``    license identifier (default ``MIT``).
      ``--bin`` / ``--lib`` shape selector. Default ``--bin``.
      ``--force``           overwrite existing files.
      ``-q``                suppress per-file status lines.
    """
    cwd = Path.cwd()
    name = (args.name or _safe_default_name(cwd)).strip()
    if not is_valid_module_name(name) or name.startswith("@"):
        print(f"[lamc] init: '{name}' is not a legal module name "
              f"(use snake_case, camelCase, PascalCase, or "
              f"SCREAMING_SNAKE_CASE, starting with a letter).",
              file=sys.stderr)
        return 2

    scope = (args.scope or "").strip()
    if scope:
        if not scope.startswith("@") or "/" in scope[1:]:
            print(f"[lamc] init: --scope must look like '@alice' "
                  f"(got {scope!r}).", file=sys.stderr)
            return 2
        full_name = f"{scope}/{name}"
        if not is_valid_module_name(full_name):
            print(f"[lamc] init: scoped module name {full_name!r} contains "
                  f"unsupported characters.", file=sys.stderr)
            return 2
    else:
        full_name = name

    version = (args.version or "0.1.0").strip()
    if not is_valid_semver(version):
        print(f"[lamc] init: --version {version!r} is not a valid "
              f"SemVer string (e.g. '0.1.0' or '1.2.3-rc.1').",
              file=sys.stderr)
        return 2

    licence = (args.license or "MIT").strip()

    if args.bin and args.lib:
        print("[lamc] init: --bin and --lib are mutually exclusive.",
              file=sys.stderr)
        return 2
    is_lib = bool(args.lib)
    # ``--bin`` is the default; nothing to flag explicitly.

    # Plan the files we'd create. Keep the plan declarative so the
    # ``--force`` / refusal logic stays simple.
    plan: List[Tuple[Path, str]] = []
    plan.append((cwd / "lamlib.toml", _INIT_MANIFEST_TEMPLATE.format(
        name=full_name,
        version=version,
        license=licence,
        lamc_compat=_default_lamc_compat_range(),
    )))
    if is_lib:
        plan.append((cwd / f"{name}.lam",
                     _INIT_LIB_TEMPLATE.format(name=full_name, version=version)))
    else:
        plan.append((cwd / "main.lam", _INIT_BIN_TEMPLATE))
    plan.append((cwd / ".gitignore", _INIT_GITIGNORE))

    # Refuse on collisions unless --force.
    collisions = [p for p, _ in plan if p.exists()]
    if collisions and not args.force:
        print("[lamc] init: refusing to overwrite existing files:",
              file=sys.stderr)
        for p in collisions:
            print(f"  - {p.relative_to(cwd) if p.is_relative_to(cwd) else p}",
                  file=sys.stderr)
        print("  (pass --force to overwrite)", file=sys.stderr)
        return 1

    for path, body in plan:
        path.write_text(body, encoding="utf-8")
        if not args.quiet:
            rel = path.relative_to(cwd) if path.is_relative_to(cwd) else path
            verb = "wrote" if path in collisions else "created"
            print(f"[lamc] {verb} {rel}")

    if not args.quiet:
        kind = "library" if is_lib else "executable"
        print(f"[lamc] init: scaffolded {kind} '{full_name}' v{version} "
              f"in {cwd}")
        print(f"  → next: run `lamc "
              f"{name + '.lam' if is_lib else 'main.lam --run'}`")
    return 0


# ── Phase 4.3: ``lamc list`` / ``tree`` / ``why`` ───────────
#
# Read-only introspection commands powered by ``lamlib.lock.toml``.
# All three operate purely on the lockfile + manifest — no
# registry round-trips, no on-disk extlib walks.
#
# The lockfile already records ``requested_by`` for every pin
# (a TOML array of ``"root"`` / ``"<name>@<version>"`` trail
# strings), so the relationships needed to render a tree or
# answer a why-query are entirely local data.


def _load_project_pins() -> Tuple[Path, Path, Dict[str, Dict]]:
    """Locate ``lamlib.toml`` + ``lamlib.lock.toml`` in cwd or any
    ancestor and return ``(manifest_path, lock_path, pins_dict)``.

    Raises :class:`InstallError` if either file is missing — the
    introspection verbs all need both to render anything useful.
    """
    here = Path.cwd().resolve()
    mf_path: Optional[Path] = None
    for _ in range(6):
        cand = here / "lamlib.toml"
        if cand.exists():
            mf_path = cand
            break
        if here.parent == here:
            break
        here = here.parent
    if mf_path is None:
        raise InstallError("no lamlib.toml found in cwd or any parent.")
    lock_path = mf_path.parent / LOCKFILE_NAME
    if not lock_path.exists():
        raise InstallError(
            f"no {LOCKFILE_NAME} at {mf_path.parent} — run "
            f"`lamc install` first to populate the lockfile.")
    lock = _load_lockfile(lock_path)
    pins: Dict[str, Dict] = lock.get("pins") or {}
    return mf_path, lock_path, pins


def _children_of(parent_label: str, pins: Dict[str, Dict]) -> List[str]:
    """Names of pins whose ``requested_by`` contains ``parent_label``.

    ``parent_label`` is either ``"root"`` or ``"<name>@<version>"``.
    The lockfile schema v1 stores ``requested_by`` as a TOML array
    of strings; v0 (which loaded under the migration shim as a
    bare string) is also tolerated for go-pins-style lockfiles.
    """
    out: List[str] = []
    for name, pin in pins.items():
        rb = pin.get("requested_by")
        # Accept both list (v1) and string (legacy) forms.
        if isinstance(rb, str):
            rb = [rb]
        elif rb is None:
            rb = []
        if parent_label in rb:
            out.append(name)
    return sorted(out)


def _pin_label(name: str, pins: Dict[str, Dict]) -> str:
    pin = pins.get(name) or {}
    ver = pin.get("version") or "?"
    return f"{name}@{ver}"


def _cmd_list(args) -> int:
    """Print every installed Lam dep, sorted, one per line.

    Output shape: ``<name>@<version>  [<source>]`` so the user
    can grep / awk it cleanly. Go pins land below under a header
    so the two layers don't get confused.
    """
    try:
        _, _, pins = _load_project_pins()
    except InstallError as e:
        print(f"[lamc] list: {e}", file=sys.stderr)
        return 2

    if not pins and not args.quiet:
        print("[lamc] list: no Lam deps in the lockfile.")
    for name in sorted(pins):
        pin = pins[name]
        ver = pin.get("version") or "?"
        src = pin.get("source") or "?"
        print(f"{name}@{ver}  [{src}]")

    # Go-module pins, if any. We render them under a clearly
    # marked header so the user can tell the layers apart.
    try:
        mf_path, lock_path, _ = _load_project_pins()
    except InstallError:
        return 0
    lock = _load_lockfile(lock_path)
    go_pins: Dict[str, Dict] = lock.get("go_pins") or {}
    if go_pins:
        print()
        print("# Go modules")
        for path in sorted(go_pins):
            ver = (go_pins[path].get("version") or "?")
            print(f"{path}  {ver}")
    return 0


def _render_tree(parent_label: str, pins: Dict[str, Dict],
                 prefix: str = "", visited: Optional[set] = None) -> None:
    """Recursively render the dep tree below ``parent_label``.

    ``visited`` guards against the (rare but legal) cycle case
    where a library transitively requests an ancestor — the
    resolver would normally collapse that, but the protective
    guard keeps the renderer honest if it ever happens.
    """
    if visited is None:
        visited = set()
    children = _children_of(parent_label, pins)
    for i, name in enumerate(children):
        is_last = i == len(children) - 1
        connector = "└── " if is_last else "├── "
        label = _pin_label(name, pins)
        print(f"{prefix}{connector}{label}")
        if name in visited:
            # Cycle break — show the recurrence but don't recurse.
            print(f"{prefix}{'    ' if is_last else '│   '}└── (cycle)")
            continue
        visited.add(name)
        _render_tree(label, pins,
                     prefix + ("    " if is_last else "│   "),
                     visited)


def _cmd_tree(args) -> int:
    """Print the dependency tree starting at the project root.

    Edges follow ``requested_by``: a pin lives under whichever
    ancestor first asked for it. When two libraries request the
    same pin transitively, it shows up under both — that's not a
    bug, it's a faithful render of the constraint graph.
    """
    try:
        mf_path, _, pins = _load_project_pins()
    except InstallError as e:
        print(f"[lamc] tree: {e}", file=sys.stderr)
        return 2

    try:
        mf = Manifest.load(mf_path)
        root_label = f"{mf.name}@{mf.version}"
    except (ManifestError, OSError):
        root_label = "<project>"

    print(root_label)
    _render_tree("root", pins)
    return 0


def _cmd_why(args) -> int:
    """Explain why a given pin exists.

    Walks ``requested_by`` upward until every chain ends at
    ``"root"`` (the project itself) or a pin we've already seen.
    Each chain is printed as a separate line so a pin that's
    requested by multiple ancestors shows every path.
    """
    try:
        _, _, pins = _load_project_pins()
    except InstallError as e:
        print(f"[lamc] why: {e}", file=sys.stderr)
        return 2

    target = args.name
    if target not in pins:
        print(f"[lamc] why: '{target}' is not in the lockfile. "
              f"Did you mean one of: "
              f"{', '.join(sorted(pins)[:5])}…?",
              file=sys.stderr)
        return 1

    label = _pin_label(target, pins)
    print(label)

    def walk(current: str, indent: int) -> None:
        pin = pins.get(current.split("@", 1)[0]) or {}
        rb = pin.get("requested_by")
        if isinstance(rb, str):
            rb = [rb]
        elif rb is None:
            rb = []
        if not rb:
            print(f"{'  ' * indent}└── (no requester recorded)")
            return
        for parent in rb:
            print(f"{'  ' * indent}└── requested by {parent}")
            if parent == "root":
                continue
            # Recurse: the parent is itself a pin; show what asked for it.
            walk(parent, indent + 1)

    walk(label, 1)
    return 0


def _cmd_publish(args) -> int:
    reg = Registry(args.registry, token=os.environ.get("LAMC_TOKEN"))
    try:
        publish_one(Path(args.path).resolve(), registry=reg,
                    quiet=args.quiet)
    except InstallError as e:
        print(f"[lamc] error: {e}", file=sys.stderr)
        return 1
    return 0


def _find_manifest_from(start: Path) -> Optional[Path]:
    """Walk upward from ``start`` looking for ``lamlib.toml``.

    Bound the walk so commands run from temporary directories do not
    accidentally pick up an unrelated manifest far above the project.
    """
    here = start.resolve()
    if here.is_file():
        here = here.parent
    for _ in range(6):
        cand = here / "lamlib.toml"
        if cand.exists():
            return cand
        if here.parent == here:
            break
        here = here.parent
    return None


def _cmd_lib_run(args) -> int:
    """Run a command from ``lamlib.toml``'s ``[scripts]`` table."""
    start = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    mf_path = _find_manifest_from(start)
    if mf_path is None:
        print("[lamc] lib run: no lamlib.toml found in cwd or any parent.",
              file=sys.stderr)
        return 2
    try:
        mf = Manifest.load(mf_path)
    except ManifestError as e:
        loc = f":{e.lineno}" if e.lineno else ""
        print(f"[lamc] lib run: {mf_path}{loc}: {e}", file=sys.stderr)
        return 2

    scripts = mf.scripts
    if args.list:
        if not scripts:
            print("[lamc] lib run: no scripts declared.")
            return 0
        for name in sorted(scripts):
            print(f"{name}\t{scripts[name]}")
        return 0

    if not args.script:
        print("[lamc] lib run: missing script name.", file=sys.stderr)
        print("usage: lamc lib run <script> [--cwd DIR] [--dry-run]",
              file=sys.stderr)
        return 2

    command = scripts.get(args.script)
    if command is None:
        print(f"[lamc] lib run: script {args.script!r} is not declared.",
              file=sys.stderr)
        if scripts:
            print("available scripts: " + ", ".join(sorted(scripts)),
                  file=sys.stderr)
        return 2
    if not command.strip():
        print(f"[lamc] lib run: script {args.script!r} is empty.",
              file=sys.stderr)
        return 2

    root = mf_path.parent
    if args.dry_run:
        print(command)
        return 0

    if not args.quiet:
        print(f"[lamc] running {args.script}: {command}", file=sys.stderr)
    try:
        proc = subprocess.run(command, cwd=str(root), shell=True)
    except OSError as e:
        print(f"[lamc] lib run: failed to start {args.script!r}: {e}",
              file=sys.stderr)
        return 1
    return proc.returncode


def main(argv: List[str]) -> int:
    """Entry point for ``lamc install`` / ``lamc uninstall`` /
    ``lamc publish``. Called by ``compiler.lammergeier.main`` once
    it has peeled the verb off ``sys.argv``."""
    if not argv:
        print(__doc__.splitlines()[0])
        return 2

    verb, rest = argv[0], argv[1:]

    if verb == "install":
        ap = argparse.ArgumentParser(prog="lamc install",
            description="Install one or more Lammergeier libraries. "
                        "With no arguments, reads the project's "
                        "lamlib.toml and installs every entry under "
                        "[dependencies].")
        _add_common_flags(ap)
        ap.add_argument("specs", nargs="*",
            help="Library spec: ``name[@version]``, a git URL, or "
                 "a local path. Example: lamwebp@1.2.0. Omit to "
                 "install everything declared in lamlib.toml.")
        ap.add_argument("--force", action="store_true",
            help="Reinstall even when the same version is already "
                 "present.")
        ap.add_argument("--allow-breaking", action="store_true",
            help="Bypass the SemVer / API-diff gate.")
        ap.add_argument("--frozen", action="store_true",
            help="Lockfile-driven install: validate that "
                 "lamlib.toml and lamlib.lock.toml agree, then "
                 "materialise every pin exactly as recorded. "
                 "Ignores positional specs and refuses on drift.")
        ap.add_argument("--offline", action="store_true",
            help="Refuse all network access — cache hits proceed, "
                 "misses fail. Implies --frozen.")
        return _cmd_install(ap.parse_args(rest))

    if verb == "uninstall":
        ap = argparse.ArgumentParser(prog="lamc uninstall",
            description="Remove an installed Lam library.")
        _add_common_flags(ap)
        ap.add_argument("names", nargs="+")
        return _cmd_uninstall(ap.parse_args(rest))

    if verb == "publish":
        ap = argparse.ArgumentParser(prog="lamc publish",
            description="Publish a local library tree to a registry.")
        ap.add_argument("path", nargs="?", default=".",
            help="Library root (directory containing lamlib.toml).")
        ap.add_argument("--registry", default=DEFAULT_REGISTRY)
        ap.add_argument("-q", "--quiet", action="store_true")
        return _cmd_publish(ap.parse_args(rest))

    if verb == "lib":
        if not rest or rest[0] in ("-h", "--help"):
            print("usage: lamc lib <subcommand> <args...>\n\n"
                  "Subcommands:\n"
                  "  run    Run a command declared in lamlib.toml [scripts].")
            return 0 if rest and rest[0] in ("-h", "--help") else 2
        subverb, subrest = rest[0], rest[1:]
        if subverb == "run":
            ap = argparse.ArgumentParser(prog="lamc lib run",
                description="Run a command declared in the nearest "
                            "lamlib.toml [scripts] table.")
            ap.add_argument("script", nargs="?",
                help="Script name from lamlib.toml [scripts].")
            ap.add_argument("--cwd",
                help="Directory to start manifest discovery from. "
                     "The script still runs from the manifest directory.")
            ap.add_argument("--list", action="store_true",
                help="List available scripts instead of running one.")
            ap.add_argument("--dry-run", action="store_true",
                help="Print the resolved command without executing it.")
            ap.add_argument("-q", "--quiet", action="store_true",
                help="Suppress the '[lamc] running ...' line.")
            return _cmd_lib_run(ap.parse_args(subrest))
        print(f"[lamc] unknown lib subcommand: {subverb}", file=sys.stderr)
        return 2

    if verb == "tidy":
        ap = argparse.ArgumentParser(prog="lamc tidy",
            description="Sync lamlib.toml [dependencies] with the "
                        "project's actual import graph: drop unused "
                        "entries, add missing imports (using versions "
                        "from extlibs/), and refresh the lockfile.")
        ap.add_argument("--check", action="store_true",
            help="Don't mutate anything. Print the diff plan and "
                 "exit non-zero if changes would be needed (CI mode).")
        ap.add_argument("-q", "--quiet", action="store_true")
        return _cmd_tidy(ap.parse_args(rest))

    if verb == "verify":
        ap = argparse.ArgumentParser(prog="lamc verify",
            description="Re-hash every installed extlib and compare "
                        "against lamlib.lock.toml. Catches supply-chain "
                        "tampering, partial installs, and lockfile/disk "
                        "drift.")
        ap.add_argument("-q", "--quiet", action="store_true")
        return _cmd_verify(ap.parse_args(rest))

    if verb == "list":
        ap = argparse.ArgumentParser(prog="lamc list",
            description="Print every Lam dep recorded in "
                        "lamlib.lock.toml, plus any go_pins. "
                        "Lockfile-only — no network.")
        ap.add_argument("-q", "--quiet", action="store_true")
        return _cmd_list(ap.parse_args(rest))

    if verb == "tree":
        ap = argparse.ArgumentParser(prog="lamc tree",
            description="Render the dependency tree from "
                        "lamlib.lock.toml's requested_by relationships.")
        ap.add_argument("-q", "--quiet", action="store_true")
        return _cmd_tree(ap.parse_args(rest))

    if verb == "why":
        ap = argparse.ArgumentParser(prog="lamc why",
            description="Explain why a given pin is in the lockfile by "
                        "walking its requested_by chain back to the "
                        "project root.")
        ap.add_argument("name",
            help="Library name to explain (e.g. lamhttp).")
        return _cmd_why(ap.parse_args(rest))

    if verb == "init":
        ap = argparse.ArgumentParser(prog="lamc init",
            description="Scaffold a fresh Lammergeier project in the "
                        "current directory. Writes lamlib.toml, an "
                        "entry-point .lam, and a .gitignore.")
        ap.add_argument("--name",
            help="Project module name (snake_case). Default: a "
                 "sanitised form of the current directory's name.")
        ap.add_argument("--version", default="0.1.0",
            help="SemVer version string (default 0.1.0).")
        ap.add_argument("--scope",
            help="Optional scope (must start with '@'). Result becomes "
                 "'@scope/name' in the manifest.")
        ap.add_argument("--license", default="MIT",
            help="SPDX license identifier (default MIT).")
        ap.add_argument("--bin", action="store_true",
            help="Scaffold an executable with `main.lam` "
                 "(default if neither --bin nor --lib is given).")
        ap.add_argument("--lib", action="store_true",
            help="Scaffold a library with `<name>.lam` instead of "
                 "main.lam.")
        ap.add_argument("--force", action="store_true",
            help="Overwrite existing files in the current directory.")
        ap.add_argument("-q", "--quiet", action="store_true")
        return _cmd_init(ap.parse_args(rest))

    print(f"[lamc] unknown subcommand: {verb}", file=sys.stderr)
    return 2
