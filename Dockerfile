# Runs ONLY the HTML leg of this tool (psd_html + a FastAPI wrapper) -- PSD in, Word-safe HTML
# bundle out. The OFT leg (grammar/*.ps1) needs classic desktop Outlook via COM automation on
# Windows and cannot run in a Linux container; it is intentionally not part of this image.
#
# No auth / rate-limiting / upload-size limits by design (per request) -- do not expose this
# publicly as-is; put it behind something that adds at least an API key and a body-size cap.
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install the pinned base package first (better Docker layer caching: this layer only rebuilds
# when deps or package source change, not on every api/ edit).
COPY pyproject.toml requirements.lock ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps -e .

# API-only deps, layered on top -- fastapi/uvicorn are not in the base package's own dependency
# list on purpose, since the CLI/GUI/OFT paths never need a web server.
COPY api/requirements.txt ./api/requirements.txt
RUN pip install --no-cache-dir -r api/requirements.txt

COPY api ./api

# Render (and most PaaS Docker runners) inject PORT at runtime and require the process to bind
# 0.0.0.0:$PORT; ENV PORT here is only the fallback for `docker run` without -e PORT=... locally.
ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fs "http://localhost:${PORT}/health" || exit 1

# Shell form + exec so uvicorn becomes PID 1 (correct SIGTERM handling) while still expanding $PORT.
CMD ["sh", "-c", "exec uvicorn api.main:app --host 0.0.0.0 --port ${PORT}"]
