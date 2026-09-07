FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir "uv>=0.5,<1" \
    && useradd --create-home --uid 10001 fivecast

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --locked --no-dev
RUN chown -R fivecast:fivecast /app

USER fivecast
CMD ["uv", "run", "--no-sync", "python", "-m", "fivecast.cloud"]
