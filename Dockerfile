# Multi-stage build — keeps the final image lean.
#
# Stage 1: builder — installs dependencies into a venv
# Stage 2: runtime — copies the venv and app code, nothing else
#
# Runs the FastAPI server only.
# The voice pipeline (STT → Claude → TTS) runs inline per WebSocket connection —
# no separate worker process needed.

FROM mcr.microsoft.com/mirror/docker/library/python:3.11-slim AS builder

WORKDIR /build

# System deps needed to compile asyncpg + cryptography
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install into an explicit venv so Stage 2 can copy it cleanly
RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

COPY pyproject.toml .
# Install runtime deps only (no dev/test extras)
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

# ---------------------------------------------------------------------------

FROM mcr.microsoft.com/mirror/docker/library/python:3.11-slim AS runtime

WORKDIR /app

# Runtime system libs (asyncpg needs libpq at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy venv from builder
COPY --from=builder /venv /venv
ENV PATH="/venv/bin:$PATH"

# Copy application code
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY alembic.ini .

# Non-root user — defense in depth (HIPAA best practice)
RUN useradd --system --create-home --uid 1001 appuser && \
    chown -R appuser:appuser /app
USER appuser

# Healthcheck — used by Docker Compose and ECS health checks
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

EXPOSE 8000

# Default: run the FastAPI server
# Override in docker-compose.yml for the LiveKit worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
