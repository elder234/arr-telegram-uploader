FROM python:3.12-slim

# No ffmpeg: parts are streamed byte-for-byte, never transcoded. Keeping the
# image small also keeps the attack surface small.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install into a virtualenv under /opt so the compiler can be purged afterwards
# without touching the system Python. pip installs into /usr/local by default,
# which apt cannot clean selectively.
COPY requirements.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && python -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/python -m pip install --no-cache-dir -r requirements.txt \
    && /opt/venv/bin/python -c "import kurigram, tgcrypto, httpx; print('deps ok in venv')" \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

# Explicit, not just PATH: `python` alone resolves to the system interpreter in
# this image, so every later layer that imported a dependency without naming the
# venv was reading /usr/local and reporting the package missing while a working
# copy sat in /opt/venv. Verified with `which python` in the layer below.
ENV PATH="/opt/venv/bin:$PATH" \
    VIRTUAL_ENV=/opt/venv

COPY pyproject.toml ./
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/

# Assert the runtime dependencies are importable *at build time*.
#
# `arr-uploader --help` does not catch this: cli.py imports Telegram lazily, so
# the console script resolves and the image looks healthy while kurigram is
# absent. The worker then dies on startup in a restart loop. An import assertion
# turns that into a failed build.
RUN which python && python -c "import sys; print('interpreter:', sys.executable)" \
    && /opt/venv/bin/python -c "import kurigram, tgcrypto, httpx; print('deps ok')" \
    && pip install --no-cache-dir --no-deps -e . \
    && python -c "import arr_uploader.cli, arr_uploader.worker, arr_uploader.torbox" \
    && python -c "import kurigram; print('kurigram visible to the worker interpreter')" \
    && mkdir -p /srv/arr/state/uploader/inbox \
    && arr-uploader --help > /dev/null

# The uploader needs write access to the media library to remove a movie folder
# after a verified upload, and to its own state directory. Nothing else.
VOLUME ["/srv/arr/state/uploader"]

ENTRYPOINT ["arr-uploader"]
CMD ["worker"]