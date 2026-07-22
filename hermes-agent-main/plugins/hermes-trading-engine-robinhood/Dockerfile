# Hermes Robinhood Agentic plugin — options + equities trading via Robinhood's Trading MCP.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RH_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY requirements-dev.txt /app/requirements-dev.txt
RUN pip install --no-cache-dir -r /app/requirements-dev.txt

COPY engine /app/engine
COPY scripts /app/scripts
COPY pytest.ini /app/pytest.ini
COPY tests /app/tests
COPY tools.py /app/tools.py
COPY plugin.yaml /app/plugin.yaml
COPY __init__.py /app/__init__.py

# Optional: mount Monte-Carlo-Sim at runtime via MONTE_CARLO_SIM_PATH
ENV MONTE_CARLO_SIM_PATH=/opt/Monte-Carlo-Sim \
    CHART_VISION_PROVIDER=mock \
    CHART_VISION_EXECUTION_MODE=recommendation_only

RUN mkdir -p /data
EXPOSE 8810

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8810/api/health').status==200 else 1)"

CMD ["uvicorn", "engine.app:app", "--host", "0.0.0.0", "--port", "8810"]