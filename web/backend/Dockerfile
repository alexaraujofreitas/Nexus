# ============================================================
# NEXUS TRADER Web — API Service Dockerfile
# ============================================================
FROM python:3.11-slim

WORKDIR /app

# System deps for PostgreSQL client and build
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY . /app/backend/
WORKDIR /app/backend

# Mount NexusTrader core/ and config/ at runtime via volume
# The Dockerfile does NOT copy core/ — it's mounted from the host

ENV PYTHONPATH=/app/backend:/app/nexustrader
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
