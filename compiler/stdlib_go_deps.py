"""Pinned Go-module versions for the Lammergeier stdlib.

Every stdlib library that wraps a third-party Go module is listed
here together with the exact version the Lam toolchain was built
against. The compiler filters this table to the stdlib modules used by the
current build, merges those pins with project-side pins collected
from ``lamlib.toml`` / ``lamlib.lock.toml``, and injects the union
into the synthesised ``go.mod`` before ``go mod tidy`` runs.

Why we do this:

* Without pins, ``go mod tidy`` picks the newest tag at build time.
  A user who wrote their Lam code against, say, ``lamcron`` a year
  ago could get a silently upgraded ``github.com/robfig/cron/v3``
  whose API changes break the stdlib's own Go glue code. Pinning
  here makes the user's build reproducible and independent of when
  they click compile.
* Because pins feed MVS (Go's Minimum Version Selection), the user
  can still *upgrade* one of these modules by listing it in their
  own ``lamlib.toml`` — project pins win over stdlib pins (see
  ``_collect_go_pins`` in ``lammergeier.py``).
* Keeping the versions in Python rather than a TOML file means the
  table moves in lockstep with the stdlib source: a PR that adopts
  a new API bumps the map in the same commit.

Updating a pin
--------------

1. Check the upstream release notes for breaking changes.
2. Update the Go code in ``lib/<libname>.lam`` if needed.
3. Bump the version string below.
4. Run the regression suite (``python3 tests/tests/run_tests.py``);
   any stdlib lib that wraps the bumped module is exercised there.

Never widen a pin range (e.g. ``>=1.2,<2``) here — Go tooling can't
express ranges in ``go.mod`` and MVS would just pick the lower
bound anyway.
"""

from __future__ import annotations

from typing import Dict

# Module path → pinned version. Sorted by module path for diff
# stability. Keep this dict flat (no nested groups) so the merge
# in ``_collect_go_pins`` stays a single ``dict.update`` call.
STDLIB_GO_PINS: Dict[str, str] = {
    # ``lamenv`` — TOML + YAML config parsing.
    "github.com/BurntSushi/toml":           "v1.6.0",
    # ``lamdata`` — pandas-style DataFrame / Series.
    "github.com/go-gota/gota":              "v0.12.0",
    # ``lamdb`` — MySQL + Postgres drivers (blank-imported).
    "github.com/go-sql-driver/mysql":       "v1.10.0",
    # ``lamuuid`` — RFC 4122 UUIDs.
    "github.com/google/uuid":               "v1.6.0",
    # ``lamdb`` — Postgres driver (blank-imported).
    "github.com/lib/pq":                    "v1.12.3",
    # ``lamcron`` — cron scheduler.
    "github.com/robfig/cron/v3":            "v3.0.1",
    # ``lamschema`` — pure-Go XSD validation.
    "github.com/moolekkari/validatexml-go": "v0.1.1",
    # ``lamschema`` — JSON Schema (draft-07).
    "github.com/xeipuuv/gojsonschema":      "v1.2.0",
    # ``lamyaml``, ``lamenv`` — YAML codec.
    "gopkg.in/yaml.v3":                     "v3.0.1",
    # ``lamdb`` — SQLite driver. Pure-Go (no CGo / C toolchain),
    # which is what makes Lam binaries with SQLite support
    # cross-compilable from any platform. Blank-imported alongside
    # the MySQL and Postgres drivers in ``lib/lamdb.lam``.
    "modernc.org/sqlite":                   "v1.50.0",
}

STDLIB_GO_PIN_MODULES: Dict[str, tuple[str, ...]] = {
    "github.com/BurntSushi/toml":           ("lamenv",),
    "github.com/go-gota/gota":              ("lamdata",),
    "github.com/go-sql-driver/mysql":       ("lamdb",),
    "github.com/google/uuid":               ("lamuuid",),
    "github.com/lib/pq":                    ("lamdb",),
    "github.com/robfig/cron/v3":            ("lamcron",),
    "github.com/moolekkari/validatexml-go": ("lamschema",),
    "github.com/xeipuuv/gojsonschema":      ("lamschema",),
    "gopkg.in/yaml.v3":                     ("lamyaml", "lamenv", "lamschema"),
    "modernc.org/sqlite":                   ("lamdb",),
}
