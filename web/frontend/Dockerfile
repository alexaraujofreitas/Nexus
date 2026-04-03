# ============================================================
# NEXUS TRADER Web — Frontend Dockerfile (E2E)
#
# Runs the Vite dev server on port 5173 with proxy enabled
# so that /api and /ws requests are forwarded to the API service.
# Used by docker-compose.e2e.yml for Playwright E2E testing.
# ============================================================
FROM node:20-slim

WORKDIR /app

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    rm -rf /var/lib/apt/lists/*

# Copy package files first for layer caching
COPY package.json package-lock.json ./

# Install all dependencies
RUN npm ci

# Copy source code
COPY . .

EXPOSE 5173

HEALTHCHECK --interval=5s --timeout=5s --retries=10 \
    CMD curl -f http://localhost:5173/ || exit 1

# Use Vite dev server (supports proxy for /api and /ws)
CMD ["npx", "vite", "--host", "0.0.0.0", "--port", "5173"]
