# ============================================================
# NexusTrader Playwright E2E Test Runner
#
# Based on Microsoft's official Playwright image with Chromium
# pre-installed. Runs all E2E specs against the app stack.
#
# Build context: web/ directory
# ============================================================
FROM mcr.microsoft.com/playwright:v1.59.1-noble

WORKDIR /app

# Copy package files for dependency installation
COPY frontend/package.json frontend/package-lock.json ./

# Install dependencies (includes @playwright/test)
RUN npm ci

# Copy test files and config
COPY frontend/e2e ./e2e
COPY frontend/playwright.config.ts ./

# Default: run all tests with list + HTML reporters
CMD ["npx", "playwright", "test", "--reporter=list,html"]
