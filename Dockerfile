# =============================================================================
# inkwake server — FastAPI, Pillow and uvicorn.
#
# Two stages, and the reason is not image size alone: the dependencies are
# installed into a virtualenv in the builder and only that venv is copied into
# a clean base, so pip, its wheel cache and every build artefact stay out of
# the runtime layer. Both stages MUST share the same base image — a venv is not
# relocatable, it hard-codes the interpreter path it was created against, and
# copying one onto a different Python produces an image whose `python` works
# and whose `uvicorn` dies on the first import.
#
# The base is named once, in an ARG, so a rebuild against a newer patch release
# is a single edit rather than a search across stages.
#
# A tag, not a digest. This image is built locally from the checkout and may go
# a year between rebuilds; for that a floating patch tag picking up the current
# security fixes beats a frozen layer nobody remembers to bump. The build was
# last verified against
#   python@sha256:7e3a6aca9d74f93cca21a91d86a8dad8c34749afd5b4a98ee481c9c47b9f5ed4
# (Debian 13, tzdata 2026b) — pass --build-arg PYTHON_IMAGE=... to reproduce it.
# =============================================================================
ARG PYTHON_IMAGE=python:3.13-slim

# --- build -------------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# requirements.txt alone first. A change to any source file must not invalidate
# this layer — resolving and downloading pillow, numpy and psycopg again on
# every code edit turns a five-second rebuild into a two-minute one.
COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt

# --- runtime -----------------------------------------------------------------
FROM ${PYTHON_IMAGE} AS runtime

# tzdata is named even though the current base already carries it (measured:
# 2026b-0+deb13u1 in python:3.13-slim), because app/config.py builds
# ZoneInfo("Europe/Vienna") at import time. If a future base ever drops the
# package, that is a ZoneInfoNotFoundError before a single route is registered
# — a container that restarts forever and never answers /healthz. Naming the
# dependency turns "the base happens to include it" into something this file
# guarantees, and the assertion below turns it into something the build checks.
#
# fonts-dejavu-core is the renderer's floor, and it only works because the
# names line up. app/render/layout.py opens
# `<repo>/assets/fonts/DejaVuSans.ttf` and `DejaVuSans-Bold.ttf` by exact name
# and raises if either is missing — it does not fall back to a system font and
# it does not go through fontconfig. The Debian package installs those two
# files under exactly those names in /usr/share/fonts/truetype/dejavu/, so the
# staging step below can put them where the renderer already looks. Installed
# unconditionally: making the package depend on the contents of assets/ would
# give two different images from the same Dockerfile depending on the state of
# the working tree.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends tzdata fonts-dejavu-core; \
    rm -rf /var/lib/apt/lists/*; \
    python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Vienna')"; \
    test -f /usr/share/fonts/truetype/dejavu/DejaVuSans.ttf

# Never root (the app parses untrusted-ish upstream bytes: JPEGs from a wildlife
# camera, calendar feeds, JSON from a weather API). A fixed numeric id rather than a name, so the
# ownership of the /data volume is predictable from outside the container.
RUN groupadd --gid 10001 inkwake \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin inkwake

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# The fonts, copied through a staging directory on purpose.
#
# `COPY assets/ ./assets/` fails the entire build when assets/ is absent from
# the context, and an assets/fonts that is still empty is not tracked by git —
# so it is absent exactly on the server, where nobody is watching the build
# output. A glob tolerates a missing match as long as *some* source matches,
# which is what requirements.txt is doing on that line: it is the anchor, not a
# second copy of the dependency list.
#
# The loop is the actual fallback. The repository's own faces win; whichever of
# the two the build did not receive is filled in from the Debian package under
# the same name, because the renderer raises on a missing file rather than
# substituting anything. Installing the package alone would not have helped —
# nothing in the render path ever looks in /usr/share/fonts.
COPY requirements.txt assets* /stage/
RUN set -eux; \
    mkdir -p /app/assets/fonts; \
    if [ -d /stage/fonts ]; then cp -a /stage/fonts/. /app/assets/fonts/; fi; \
    rm -rf /stage; \
    for f in DejaVuSans.ttf DejaVuSans-Bold.ttf; do \
      if [ ! -f "/app/assets/fonts/$f" ]; then \
        echo "assets/fonts/$f missing from the build context, using the packaged DejaVu"; \
        cp "/usr/share/fonts/truetype/dejavu/$f" "/app/assets/fonts/$f"; \
      fi; \
    done; \
    ls -1 /app/assets/fonts

COPY app ./app

# /data is created in the image, not left to the volume driver. Docker seeds a
# fresh named volume from the image's directory *including its ownership*; with
# no directory here the volume is created root-owned and uid 10001 cannot write
# the device registry or a single frame. The failure surfaces only on the first
# deploy to an empty volume, which is the worst possible moment.
RUN mkdir -p /data/frames \
 && chown -R 10001:10001 /data /app

# Byte-compile as root while /app is still writable. The runtime mounts the
# root filesystem read-only, so an interpreter that had to compile on every
# start would pay for it on every start and cache nothing.
RUN python -m compileall -q /app/app

USER 10001:10001
EXPOSE 8000

# Liveness only, and over loopback inside the container, so it says nothing
# about whether a reverse proxy or the device can reach the app — that is deliberate:
# this check restarts a wedged process, it does not diagnose the network.
# urllib rather than curl keeps the image free of an HTTP client that exists
# solely to answer this question.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"]

# One worker, and that is a correctness requirement rather than a resource
# decision: the source cache and the render cache live in process memory, and
# the current frame lives in module state. A second worker would keep a second
# cache, render a second frame, and hand two devices two different images while
# both raced for the registry's write lock. Three wakes a day do not need a pool.
#
# --forwarded-allow-ips is set because the peer uvicorn sees is the docker
# bridge gateway, never 127.0.0.1, so uvicorn's default trust list rejects the
# proxy's X-Forwarded-For and every request logs the gateway address. The
# compose file publishes the port on loopback by default, so the reverse proxy
# is the only path in. If you publish it on the LAN instead, a client there can
# set X-Forwarded-For to whatever it likes; it only affects the logged address.
#
# CMD, not ENTRYPOINT: `docker compose run app python -m app.something` has to
# stay possible for a one-off render without --entrypoint gymnastics.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-server-header"]
