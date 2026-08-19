FROM python:3.12-slim

# Pinned rather than :latest — a build tool that moves on its own defeats the
# point of shipping a lockfile.
COPY --from=ghcr.io/astral-sh/uv:0.11.2 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# --frozen installs exactly what uv.lock pins and fails if the lock is stale,
# rather than silently resolving something new. The previous `pip install .`
# copied the lockfile in and then ignored it, so the image drifted with every
# rebuild — which is how it ended up one release away from mcp 2.0, where the
# FastMCP module this server is built on no longer exists.
RUN uv sync --frozen --no-dev --no-editable

ENV PATH="/app/.venv/bin:$PATH"

ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_TRANSPORT=streamable-http \
    JOPLIN_HOST=host.docker.internal \
    JOPLIN_PORT=41184

# Nothing here needs to write to the image or bind a privileged port.
RUN useradd --system --create-home --uid 10001 joplin \
    && chown -R joplin:joplin /app
USER joplin

EXPOSE 8000

# /healthz stays outside the auth gate specifically so this keeps working once
# MCP_OAUTH_ENABLED is turned on.
# Exec form; MCP_PORT is read at runtime inside Python (os.environ), not baked in.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('MCP_PORT','8000')+'/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "src.mcp.joplin_mcp"]
