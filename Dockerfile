# SPHEREx PlanetX backend: FastAPI API, plus the survey worker when PLANETX_RUN_WORKER=1.
# Build for the host you deploy to (Render: linux/amd64).
FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM python:3.13-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first so this layer is cached until the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY backend ./backend
RUN uv sync --locked --no-dev --no-editable

FROM python:3.13-slim
RUN useradd --system --create-home --uid 10001 app
COPY --from=build /app/.venv /app/.venv
COPY deploy/entrypoint.sh /usr/local/bin/entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    PLANETX_CACHE_DIR=/data/cache \
    PLANETX_DB=/data/survey.sqlite
# Mount a persistent volume at /data. The entrypoint fixes its ownership and
# then drops root, so the app never runs as root.
RUN mkdir /data && chown app:app /data
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/health' % os.environ['PORT'], timeout=4)"
ENTRYPOINT ["entrypoint.sh"]
CMD ["sh", "-c", "exec uvicorn planetx.api.app:app --host 0.0.0.0 --port $PORT"]
