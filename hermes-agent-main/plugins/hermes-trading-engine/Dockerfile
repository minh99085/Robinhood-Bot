# Hermes Trading Engine - paper-trading dashboard (standalone container)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTE_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY engine /app/engine
COPY web /app/web
COPY scripts /app/scripts
COPY config /app/config

RUN mkdir -p /data
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/api/health').status==200 else 1)"

CMD ["uvicorn", "engine.app:app", "--host", "0.0.0.0", "--port", "8800"]
