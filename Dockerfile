# syntax=docker/dockerfile:1.7
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble@sha256:330d401ae23c905896a0c26c66c36fc971b621519e7b11b435a8328d7bf6e031

COPY --from=ghcr.io/astral-sh/uv:0.12.8@sha256:d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini gosu \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable --no-cache --python /usr/bin/python3

# Give the mounted directory to pwuser before dropping root privileges.
RUN <<'EOF'
set -eu
cat > /usr/local/bin/docket-entrypoint <<'SCRIPT'
#!/bin/sh
set -eu
umask 077
if [ "$(id -u)" = 0 ]; then
    mkdir -p "$DATA_DIR"
    chown pwuser:pwuser "$DATA_DIR"
    exec gosu pwuser "$@"
fi
exec "$@"
SCRIPT
chmod 755 /usr/local/bin/docket-entrypoint
EOF

ENTRYPOINT ["/usr/bin/tini", "-s", "--", "/usr/local/bin/docket-entrypoint"]
CMD ["docket", "worker"]
