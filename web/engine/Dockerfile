# ============================================================
# NEXUS TRADER Web — Trading Engine Service Dockerfile
# ============================================================
FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc && \
    rm -rf /var/lib/apt/lists/*

# Python deps — shares the backend requirements plus core/ deps
COPY ../backend/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Copy engine source
COPY . /app/engine/

# Copy backend's core_patch and app.config for shared modules
COPY ../backend/core_patch /app/backend/core_patch/
COPY ../backend/app /app/backend/app/

ENV PYTHONPATH=/app/engine:/app/backend:/app/nexustrader
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "engine.main"]
