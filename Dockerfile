# Root-level Dockerfile for deployment (Railway/Render/Fly).
# Build context is the repo ROOT, so paths are backend/… and frontend/… — this
# works regardless of any "root directory" UI setting. (backend/Dockerfile is
# kept for local `docker build` from within backend/.)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install deps first for layer caching.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (backend) + the static operations UI (served at / by FastAPI).
COPY backend/ /app/
COPY frontend/ /frontend/

# Run as a non-root user.
RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Boot: migrate -> seed the demo dataset (idempotent) -> serve on $PORT.
CMD ["sh", "-c", "alembic upgrade head && python -m app.seed_demo || true; uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
