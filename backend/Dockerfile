# syntax=docker/dockerfile:1
#
# The API and the Celery workers share one image: they run the same code, and a single image means
# a worker can never be running a different build from the API that queued its work.
#
# The Playwright base image is used because the crawler's JavaScript-rendering fallback needs
# Chromium and its system libraries, which are tedious and fragile to assemble by hand.

# Noble (Ubuntu 24.04) ships Python 3.12. The older jammy tag is Python 3.10, which lacks
# `enum.StrEnum` — this codebase targets 3.11+.
FROM mcr.microsoft.com/playwright/python:v1.54.0-noble AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt itself changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only Chromium is needed; the other browsers would add ~700 MB for nothing.
RUN python -m playwright install chromium --with-deps

COPY . .

# Run as a non-root user. The Playwright image ships `pwuser` already configured for Chromium.
RUN chown -R pwuser:pwuser /app
USER pwuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Run schema migrations automatically on startup, then launch uvicorn respecting Render's $PORT
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers"]
