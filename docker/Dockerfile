FROM python:3.12-slim

# ffmpeg is not needed: we stream raw parts, never transcode. Keeping the image
# small also keeps the attack surface small.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches independently of source changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir --no-deps -e . \
    && mkdir -p /srv/arr/state/uploader/inbox

# The uploader needs write access to the media library to remove a movie folder
# after a verified upload, and to its own state directory. Nothing else.
VOLUME ["/srv/arr/state/uploader"]

ENTRYPOINT ["arr-uploader"]
CMD ["worker"]