# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Kolkata

WORKDIR /app

# Install runtime dependencies first (read straight from pyproject.toml) so that
# code-only changes reuse this cached layer.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt \
 && rm /tmp/requirements.txt

# Application code (owned by root, read-only for the runtime user).
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
COPY scripts ./scripts

# Unprivileged runtime user. Only data/ (SQLite DB, Kite session, exports) is writable.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p data/market data/exports \
 && chown -R appuser:appuser data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

# Exactly one worker: the market-data hub, paper sessions and backtest jobs live in
# process memory, and the database is SQLite. Migrations run automatically on start.
# Proxy headers are trusted because the container is only reachable through the nginx
# frontend, which sets X-Forwarded-For to the real client IP (used by the login lock-out).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "20", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
