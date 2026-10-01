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
RUN python -m venv /opt/venv \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH"

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
RUN python -c "import kurigram, tgcrypto, httpx" \
    && pip install --no-cache-dir --no-deps -e . \
    && python -c "import arr_uploader.cli, arr_uploader.worker, arr_uploader.torbox" \
    && mkdir -p /srv/arr/state/uploader/inbox \
    && arr-uploader --help > /dev/null

# The uploader needs write access to the media library to remove a movie folder
# after a verified upload, and to its own state directory. Nothing else.
VOLUME ["/srv/arr/state/uploader"]

ENTRYPOINT ["arr-uploader"]
CMD ["worker"]