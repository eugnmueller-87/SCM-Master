# SCM Master backend image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Boot: migrate -> seed the demo dataset (idempotent) -> serve. Binds to the
# platform-provided $PORT when present (Railway/Render), else 8000. The demo
# seed is safe to re-run (it skips when the catalog is already populated).
CMD ["sh", "-c", "alembic upgrade head && python -m app.seed_demo || true; uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
