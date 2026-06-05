# Hermes Trading Engine - paper-trading dashboard (standalone container)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HTE_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Test-only deps (pytest). Installed on top of the runtime requirements so the
# suite runs inside the container with no manual `pip install`. Does not change
# runtime behavior — nothing in engine/ imports these.
COPY requirements-dev.txt /app/requirements-dev.txt
RUN pip install --no-cache-dir -r /app/requirements-dev.txt

COPY engine /app/engine
COPY web /app/web
COPY scripts /app/scripts
# Ship the test suite (+ conftest.py and tests/fixtures) at /app/tests so that
# `python -m pytest` and targeted `pytest tests -k "..."` work in the container.
# pytest.ini scopes collection to tests/ and keeps timeouts platform-safe.
COPY pytest.ini /app/pytest.ini
COPY tests /app/tests

RUN mkdir -p /data
EXPOSE 8800

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8800/api/health').status==200 else 1)"

CMD ["uvicorn", "engine.app:app", "--host", "0.0.0.0", "--port", "8800"]
