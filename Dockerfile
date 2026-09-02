FROM python:3.12-slim

WORKDIR /app

# ffmpeg and ffprobe are both used; curl backs the HEALTHCHECK below.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY templates/ ./templates/
COPY static/ ./static/
# config.json is gitignored, so the image ships the example instead and lets
# environment variables (or a mounted file) override it at runtime.
COPY config_example.json ./config.json

# /data holds the database; /videos_parent is where libraries get mounted.
RUN mkdir -p /app/thumbnails /data && \
    useradd --create-home --uid 10001 stream && \
    chown -R stream:stream /app /data
USER stream

ENV THUMBNAIL_DIR=/app/thumbnails \
    DB_FILE=/data/video_db.json \
    PORT=6969

EXPOSE 6969

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

# --no-proxy-headers: forwarded-header policy lives in middleware.client_ip,
# which honours X-Forwarded-For only from configured TRUSTED_PROXIES.
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --no-proxy-headers"]
