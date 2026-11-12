# Build only from this sanitized source directory; never a NAS/media root.
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    HOME=/tmp \
    VM_NAS_MODE=1 \
    VM_DATA_DIR=/data \
    VM_MEDIA_ROOT=/media \
    VM_SCAN_INTERVAL=120

WORKDIR /app
COPY --chown=1000:1000 requirements-nas.txt LICENSE ./
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --yes --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --disable-pip-version-check -r requirements-nas.txt \
    && mkdir -p /data /media \
    && chown 1000:1000 /data /media

# Deliberately exclude configs, databases, git history, media, and test fixtures.
COPY --chown=1000:1000 app.py datasource.py library.py mapping.py nas_archive.py nas_server.py ./
COPY --chown=1000:1000 web/ ./web/

USER 1000:1000
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
    CMD ["python", "-c", "import json,urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8765/health',timeout=3)); raise SystemExit(0 if r.get('ok') else 1)"]

# The adapter fixes host/port/browser behavior and exactly one Uvicorn worker.
CMD ["python", "nas_server.py"]
