# syntax=docker/dockerfile:1.7

# ---- builder stage ----
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# uv for fast deps + reproducible installs
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /build

# Copy metadata + lockfile first for layer caching. README.md is referenced
# by pyproject.toml (project.readme); hatchling reads it during build.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copy source and install the project
COPY src/ src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime stage ----
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# non-root user (uid 1000 matches vault PVC ownership convention)
RUN groupadd --gid 1000 app && \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash app

WORKDIR /app
COPY --from=builder --chown=app:app /build/.venv /app/.venv
COPY --chown=app:app src/ /app/src/

USER app
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import httpx; httpx.get('http://localhost:8765/healthz', timeout=2).raise_for_status()"

CMD ["uvicorn", "agents.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--workers", "1", \
     "--log-config", "/app/src/agents/logging.json"]
