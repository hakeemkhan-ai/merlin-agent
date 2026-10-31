FROM ghcr.io/astral-sh/uv:0.12.9 AS uv

FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:${PATH}"
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md .python-version ./
COPY src ./src
COPY alembic.ini ./
COPY migrations ./migrations
COPY deploy/worker_health.py /app/worker_health.py
RUN uv sync --locked --no-dev --no-editable && \
    useradd --create-home --uid 10001 merlin \
    && mkdir -p /data/artifacts /data/github-state && chown -R merlin:merlin /data

FROM base AS verification
RUN uv sync --locked --dev --no-editable
USER merlin

FROM base AS runtime
USER merlin
CMD ["uvicorn", "merlin.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
