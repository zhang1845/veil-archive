# Build context: backend/. This entry point never boots the legacy DB or Redis.
FROM python:3.13-slim AS dependencies
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app/backend
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/backend/.venv/bin:$PATH" PORT=8080
WORKDIR /app/backend
COPY --from=dependencies /app/backend/.venv ./.venv
COPY src ./src
RUN chmod -R u=rwX,go=rX /app/backend/src \
    && groupadd --gid 10001 veil \
    && useradd --uid 10001 --gid veil --no-create-home veil
USER veil
EXPOSE 8080
CMD ["uvicorn", "src.cloudbase.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--no-proxy-headers", "--no-access-log"]
