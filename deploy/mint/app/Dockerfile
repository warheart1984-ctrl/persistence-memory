# The ledger service for the single-box deployment. Same code as the repo's Dockerfile, but:
#   - runs as an unprivileged user (uid 10001),
#   - writes only to /data (so the container can run with a read-only root filesystem),
#   - exposes no secrets (all configuration arrives at run time).
# Build context is the repository root (see docker-compose.yml).
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JARVIS_HOST=0.0.0.0

COPY pyproject.toml ./
COPY app ./app
COPY mcp_server ./mcp_server

RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin jarvis \
 && mkdir -p /data \
 && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
