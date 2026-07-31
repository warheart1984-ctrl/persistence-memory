FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JARVIS_ENV=production \
    JARVIS_HOST=0.0.0.0 \
    JARVIS_PORT=8001 \
    JARVIS_STORE_PATH=/data/jarvis-store.json

COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir -e .

RUN mkdir -p /data

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
