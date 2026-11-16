FROM python:3.11-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.10.12 /uv /uvx /bin/

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN uv sync --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8080
CMD ["uvicorn", "qa.api:app", "--host", "0.0.0.0", "--port", "8080"]
