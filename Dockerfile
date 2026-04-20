# ── Stage 1: Build dependencies ──────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt


# ── Stage 2: Production image ─────────────────────────────────────────────────
FROM python:3.12-slim

# OpenCV runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN addgroup --system app && adduser --system --ingroup app app

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY --chown=app:app app/ app/

# Runtime data directory (will be mounted as a volume on Fly.io)
RUN mkdir -p /app/data/img_cache && chown -R app:app /app/data

ENV DATABASE_URL=sqlite:////app/data/mimir_assets.db

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request, os; urllib.request.urlopen('http://localhost:' + os.environ.get('PORT','8000') + '/api/stats')"

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
