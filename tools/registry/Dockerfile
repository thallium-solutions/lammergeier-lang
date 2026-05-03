# Lam package registry — reference implementation.
#
# Builds a ~60 MB image that serves ``lamc install`` / ``lamc publish``
# requests on port 8765. The entry point is a plain ``python3
# server.py``; we vendor ``compiler/manifest.py`` into the image so
# the server can validate uploaded ``lamlib.toml`` files without
# depending on the full compiler checkout.
#
# Build:
#   docker build -t lamcregistry tools/registry
#
# Run:
#   docker run --rm -p 8765:8765 -v lamcdata:/data lamcregistry
#
# Seed with sample libraries at boot:
#   docker run --rm -p 8765:8765 \
#     -v lamcdata:/data \
#     -v "$(pwd)/tools/registry/seed:/seed:ro" \
#     -e LAMC_REGISTRY_SEED=/seed \
#     lamcregistry

FROM python:3.11-slim

# ``python:3.11-slim`` already has everything we need — no pip
# installs, no compiler. Just pull in the source files.
WORKDIR /opt/lamcregistry

# The server module imports ``compiler.manifest`` to validate uploaded
# manifests. Copy the minimal subset required instead of the whole
# compiler so the image stays small and the runtime footprint stays
# dependency-free.
COPY compiler/__init__.py         /opt/lamc/compiler/__init__.py
COPY compiler/manifest.py         /opt/lamc/compiler/manifest.py
COPY tools/registry/server.py     /opt/lamcregistry/server.py

ENV PYTHONPATH=/opt/lamc \
    LAMC_REGISTRY_DATA=/data \
    LAMC_REGISTRY_PORT=8765 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8765
VOLUME ["/data"]

# Healthcheck hits /health which is a no-auth JSON ``{"ok": true}``.
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
  CMD python3 -c "import urllib.request,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=2).status==200 else 1)"

CMD ["python3", "/opt/lamcregistry/server.py"]
