FROM python:3.12-slim

# No ffmpeg: parts are streamed byte-for-byte, never transcoded. Keeping the
# image small also keeps the attack surface small.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install into the image's own interpreter.
#
# A virtualenv under /opt was tried first, to keep the compiler purge from
# touching installed packages. That was solving a problem that does not exist:
# `apt-get purge build-essential` removes the compiler, not site-packages. The
# venv cost a layer of ambiguity between the installer and the importer -- pip
# would report kurigram installed into /opt/venv while a bare `python` read
# /usr/local and reported it missing. One interpreter, named everywhere.
COPY requirements.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/

# Assert the runtime dependencies are importable at build time.
#
# `arr-uploader --help` does not catch this: cli.py imports Telegram lazily, so
# the console script resolves and the image looks healthy while kurigram is
# absent. The worker then dies on startup in a restart loop. This is the check
# that would have caught it, and it costs nothing.
RUN python -c "import sys; print('interpreter:', sys.executable)" \
    && python -c "import kurigram, tgcrypto, httpx; print('deps ok')" \
    && pip install --no-cache-dir --no-deps -e . \
    && python -c "import arr_uploader.cli, arr_uploader.worker, arr_uploader.torbox" \
    && python -c "import kurigram; print('kurigram visible to the runtime interpreter')" \
    && mkdir -p /srv/arr/state/uploader/inbox \
    && arr-uploader --help > /dev/null

# The uploader needs write access to the media library to remove a movie folder
# after a verified upload, and to its own state directory. Nothing else.
VOLUME ["/srv/arr/state/uploader"]

ENTRYPOINT ["arr-uploader"]
CMD ["worker"]