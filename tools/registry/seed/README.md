# Seed libraries for the local registry

The files in this directory are imported on boot by the reference
registry (`tools/registry/server.py` / the Docker image) so tests
and demos never face an empty registry.

Layout
------

Each `.tar.gz` under this directory must be a library tarball rooted
at `<name>-<version>/`, containing at minimum:

```
<name>-<version>/
├── lamlib.toml
├── README.md
└── <name>.lam      (or <name>/__init__.lam)
```

See [`docs/third_party_libraries.md`](../../../docs/third_party_libraries.md)
§4 for the full published-layout spec.

Adding / refreshing seeds
-------------------------

Run `tools/registry/make_seed.sh` (or its cross-platform twin) to
regenerate the tarballs from the `src/` subdirectory. The script
just `tar -czf`'s each library directory — no magic.

The registry imports every `*.tar.gz` it finds here the first time
it boots against a fresh `/data` volume. Re-running the container
with the same volume is idempotent (duplicate versions are skipped).
