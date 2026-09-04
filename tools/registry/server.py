#!/usr/bin/env python3
"""Tiny Lam package registry — reference implementation.

Implements the three endpoints :class:`compiler.install_cli.Registry`
speaks, with zero runtime deps so the whole service is one ``python
server.py`` call (also shipped as a Docker image under
``tools/registry/Dockerfile`` for the ``lamc install`` workflow tests).

Layout on disk
--------------

``<data>/`` is the directory the server persists to (``./data`` by
default, ``/data`` inside the Docker image). The registry owns the
whole directory and mirrors the published tarballs + a one-shot
index JSON per package::

    data/
      index/
        lamwebp.json                  # plain name
        @alice__lamwebp.json          # scope__name alias
      tarballs/
        lamwebp-1.0.0.tar.gz
        lamwebp-1.1.0.tar.gz

``index/*.json`` contains ``{"name": str, "versions": [...]}`` —
exactly what the ``Registry.list_versions`` client expects to parse.

Endpoints
---------

``GET /api/v1/libraries/<name>``
    Read ``index/<alias>.json``. 404 if missing.

``GET /api/v1/libraries/<name>/<version>.tar.gz``
    Stream back the corresponding tarball.

``POST /api/v1/publish``
    Accept ``multipart/form-data`` with a single ``file`` field,
    validate that the tarball contains a ``lamlib.toml`` at its
    rooted subdir, then:

    1. Compute a sha256 of the tarball bytes.
    2. Append a new ``VersionInfo`` to the package's index JSON
       (or create it if this is the first release).
    3. Save the tarball under ``tarballs/``.

Validation server-side stays conservative — we only refuse outright
broken tarballs (no manifest, bad SemVer). The breaking-change
gate lives on the installer side because only the installer knows
the consumer's view of "breaking"; the registry side emits the
warning via response payload when it can detect one.
"""

from __future__ import annotations

import cgi
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional, Tuple


# ── Paths ────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("LAMC_REGISTRY_DATA", "./data")).resolve()
INDEX_DIR = DATA_DIR / "index"
TAR_DIR   = DATA_DIR / "tarballs"

# Upload limit — the reference implementation holds the whole tarball
# in memory while parsing the multipart body, so keep it modest.
MAX_UPLOAD = 20 * 1024 * 1024  # 20 MiB


# ── Name <-> alias helpers ──────────────────────────────────

_MODULE_NAME_RE = re.compile(
    r"^(@[A-Za-z0-9_][A-Za-z0-9_\-]*\/[A-Za-z_][A-Za-z0-9_\-]*|[A-Za-z_][A-Za-z0-9_]*)$")


def _alias(name: str) -> str:
    """Filesystem-safe alias: ``@alice/lamwebp`` → ``@alice__lamwebp``.
    Used as the JSON filename and the tarball basename prefix."""
    return name.replace("/", "__")


def _unalias(alias: str) -> str:
    """Inverse of :func:`_alias` for the few callers (debugging,
    index listing) that need the canonical name from a filename."""
    return alias.replace("__", "/")


# ── Index JSON IO ────────────────────────────────────────────

def _index_path(name: str) -> Path:
    return INDEX_DIR / f"{_alias(name)}.json"


def _load_index(name: str) -> Dict:
    p = _index_path(name)
    if not p.exists():
        return {"name": name, "versions": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"name": name, "versions": []}


def _save_index(name: str, idx: Dict) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    _index_path(name).write_text(
        json.dumps(idx, indent=2, sort_keys=False), encoding="utf-8")


# ── Tarball validation ──────────────────────────────────────

# The registry needs the manifest fields to build the index entry,
# so we parse the same TOML subset the compiler does. Import the
# shared module when running under the project checkout; fall back
# to a vendored copy inside the Docker image.
try:
    from compiler.manifest import Manifest, ManifestError
except ImportError:  # pragma: no cover (container-only path)
    sys.path.insert(0, "/opt/lamc")
    from compiler.manifest import Manifest, ManifestError


def _inspect_tarball(data: bytes) -> Tuple[str, str, bytes]:
    """Extract ``(name, version, manifest_bytes)`` from an upload.

    Raises ``ValueError`` on malformed tarballs so the HTTP handler
    can translate each failure into a 4xx response. We deliberately
    don't persist the tarball here — validation runs fully in memory
    so a corrupt payload never lands on disk."""
    bio = io.BytesIO(data)
    try:
        with tarfile.open(fileobj=bio, mode="r:gz") as tar:
            # Find exactly one top-level directory.
            tops = set()
            for m in tar.getmembers():
                if "/" in m.name.rstrip("/"):
                    tops.add(m.name.split("/", 1)[0])
                else:
                    tops.add(m.name)
            if len(tops) != 1:
                raise ValueError(
                    f"tarball must have exactly one root directory "
                    f"(got {sorted(tops)})")
            root = next(iter(tops))

            mf_member = None
            for m in tar.getmembers():
                if m.name == f"{root}/lamlib.toml":
                    mf_member = m; break
            if mf_member is None:
                raise ValueError("tarball missing lamlib.toml at root")

            f = tar.extractfile(mf_member)
            if f is None:
                raise ValueError("lamlib.toml unreadable")
            mf_bytes = f.read()
    except tarfile.TarError as e:
        raise ValueError(f"not a valid .tar.gz: {e}")

    try:
        mf = Manifest.from_text(mf_bytes.decode("utf-8"))
    except ManifestError as e:
        raise ValueError(f"invalid lamlib.toml: {e}")

    return mf.name, mf.version, mf_bytes


# ── HTTP handler ─────────────────────────────────────────────

class RegistryHandler(BaseHTTPRequestHandler):
    """Single-request handler — routing is done by hand because the
    whole API is three endpoints and we don't want to depend on
    a web framework. The server class uses ``ThreadingHTTPServer``
    so concurrent installs don't serialise on a single thread."""

    server_version = "lamcregistry/0.1"

    def log_message(self, fmt, *args):
        sys.stderr.write(
            f"[{time.strftime('%Y-%m-%dT%H:%M:%S')}] "
            f"{self.address_string()} - {fmt % args}\n")

    # ── GETs ─────────────────────────────────────────────

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if parts == ["health"]:
                return self._json(200, {"ok": True})
            if len(parts) >= 3 and parts[0] == "api" and parts[1] == "v1" and parts[2] == "libraries":
                if len(parts) == 4:
                    return self._serve_index(urllib.parse.unquote(parts[3]))
                if len(parts) == 5:
                    return self._serve_tarball(
                        urllib.parse.unquote(parts[3]), parts[4])
            self._json(404, {"error": "not found"})
        except Exception as e:  # pragma: no cover
            self._json(500, {"error": str(e)})

    def _serve_index(self, name: str):
        if not _MODULE_NAME_RE.match(name):
            return self._json(400, {"error": f"bad name: {name!r}"})
        p = _index_path(name)
        if not p.exists():
            return self._json(404, {"error": f"no package {name!r}"})
        body = p.read_bytes()
        self._raw(200, body, "application/json")

    def _serve_tarball(self, name: str, filename: str):
        if not _MODULE_NAME_RE.match(name):
            return self._json(400, {"error": f"bad name: {name!r}"})
        if not filename.endswith(".tar.gz") or "/" in filename:
            return self._json(400, {"error": "bad tarball name"})
        path = TAR_DIR / filename
        if not path.exists():
            return self._json(404, {"error": "tarball missing"})
        data = path.read_bytes()
        self._raw(200, data, "application/gzip")

    # ── POST /publish ───────────────────────────────────

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/api/v1/publish":
            return self._json(404, {"error": "not found"})

        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return self._json(
                400, {"error": "expected multipart/form-data"})

        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            return self._json(
                400, {"error": f"content-length must be 1..{MAX_UPLOAD}"})

        try:
            fs = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={"REQUEST_METHOD": "POST",
                         "CONTENT_TYPE": ctype,
                         "CONTENT_LENGTH": str(length)},
                keep_blank_values=True,
            )
        except Exception as e:
            return self._json(400, {"error": f"multipart parse: {e}"})

        if "file" not in fs:
            return self._json(400, {"error": "expected a 'file' field"})

        item = fs["file"]
        data = item.file.read()
        if len(data) > MAX_UPLOAD:
            return self._json(413, {"error": "payload too large"})

        try:
            name, version, _mf_bytes = _inspect_tarball(data)
        except ValueError as e:
            return self._json(400, {"error": str(e)})

        idx = _load_index(name)
        if any(v["version"] == version and not v.get("yanked")
               for v in idx["versions"]):
            return self._json(
                409, {"error": f"{name}@{version} already published "
                                "(versions are immutable)"})

        sha = hashlib.sha256(data).hexdigest()
        tar_name = f"{_alias(name)}-{version}.tar.gz"
        TAR_DIR.mkdir(parents=True, exist_ok=True)
        (TAR_DIR / tar_name).write_bytes(data)

        entry = {
            "version":  version,
            "tarball":  f"/api/v1/libraries/"
                         f"{urllib.parse.quote(name, safe='')}/{tar_name}",
            "sha256":   sha,
            "yanked":   False,
            "uploaded": int(time.time()),
        }
        idx["name"] = name
        idx["versions"].append(entry)
        _save_index(name, idx)

        self._json(200, {
            "ok": True,
            "name": name,
            "version": version,
            "sha256": sha,
            "size": len(data),
        })

    # ── Response helpers ────────────────────────────────

    def _json(self, code: int, body: dict):
        self._raw(code, json.dumps(body, indent=2).encode("utf-8"),
                  "application/json")

    def _raw(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# ── Entry point ──────────────────────────────────────────────

def _seed_if_empty() -> None:
    """If ``LAMC_REGISTRY_SEED`` points at a directory of
    ``*.tar.gz`` tarballs, import each one into the registry on
    boot. Lets the Dockerfile ship a starter catalogue for tests /
    demos without running the full publish dance."""
    seed = os.environ.get("LAMC_REGISTRY_SEED")
    if not seed or not Path(seed).is_dir():
        return
    for tar_path in sorted(Path(seed).glob("*.tar.gz")):
        try:
            data = tar_path.read_bytes()
            name, version, _ = _inspect_tarball(data)
        except ValueError:
            continue
        idx = _load_index(name)
        if any(v["version"] == version for v in idx["versions"]):
            continue
        sha = hashlib.sha256(data).hexdigest()
        tar_name = f"{_alias(name)}-{version}.tar.gz"
        TAR_DIR.mkdir(parents=True, exist_ok=True)
        (TAR_DIR / tar_name).write_bytes(data)
        idx["name"] = name
        idx["versions"].append({
            "version":  version,
            "tarball":  f"/api/v1/libraries/"
                         f"{urllib.parse.quote(name, safe='')}/{tar_name}",
            "sha256":   sha,
            "yanked":   False,
            "uploaded": int(time.time()),
        })
        _save_index(name, idx)
        sys.stderr.write(f"[seed] imported {name}@{version}\n")


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    TAR_DIR.mkdir(parents=True, exist_ok=True)
    _seed_if_empty()

    port = int(os.environ.get("LAMC_REGISTRY_PORT", "8765"))
    host = os.environ.get("LAMC_REGISTRY_HOST", "0.0.0.0")
    httpd = ThreadingHTTPServer((host, port), RegistryHandler)
    sys.stderr.write(
        f"[lamcregistry] listening on http://{host}:{port} "
        f"data={DATA_DIR}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[lamcregistry] shutting down\n")


if __name__ == "__main__":
    main()
