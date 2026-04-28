# syntax=docker/dockerfile:1.7
#
# Two-stage build for the HESM optimizer Cloud Run service.
#
# Stage 1 ("builder") resolves and installs dependencies with uv into a
# self-contained .venv directory. Stage 2 copies only that .venv plus the
# application source into a slim runtime image. This keeps the final image
# small (~120 MB) and free of build tooling.

ARG PYTHON_VERSION=3.12
ARG UV_VERSION=0.4

# ---- Builder ---------------------------------------------------------------

FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# Install runtime deps first so they cache across source-only changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Now copy the source and install the project itself.
COPY src ./src
RUN uv sync --frozen --no-dev

# ---- Runtime ---------------------------------------------------------------

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

# Run as non-root for least privilege.
RUN groupadd --system app && useradd --system --gid app --no-create-home app

WORKDIR /app

# Copy the resolved venv and the application source from the builder.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

USER app

EXPOSE 8080

# Cloud Run sends SIGTERM on shutdown; uvicorn handles it gracefully.
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--no-access-log", "--proxy-headers", "--forwarded-allow-ips=*"]
