# syntax=docker/dockerfile:1.7

# ---- builder stage ----
FROM python:3.12-slim AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# uv for fast deps + reproducible installs
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

# Build the venv at /app (not /build) so the shebangs uv writes into the
# venv binaries — which hardcode the absolute path — match the runtime
# stage's path. Otherwise COPY-ing /build/.venv to /app/.venv leaves
# shebangs pointing at /build/.venv/bin/python which doesn't exist at
# runtime.
WORKDIR /app

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
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/ /app/src/
# Agent persona definitions (SOUL/IDENTITY/AGENTS/USER per agent + _shared/).
# Loaded at runtime by `agents.personas.load_persona` — resolved relative to
# settings.py via `Path(__file__).parent.parent.parent / "agents/workspaces"`.
# Migrated from the vault PVC mount to the image in stages 1-3 (PRs #71, #73,
# and this PR). Skills still live in vault — see `settings.skills_dir`.
COPY --chown=app:app agents/ /app/agents/

USER app
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import httpx; httpx.get('http://localhost:8765/healthz', timeout=2).raise_for_status()"

CMD ["uvicorn", "agents.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--workers", "1"]
