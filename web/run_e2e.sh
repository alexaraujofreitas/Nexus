#!/bin/bash
# ============================================================
# NexusTrader — Run Playwright E2E Tests in Docker
#
# Usage: cd web && bash run_e2e.sh
# ============================================================
set -e

echo "=== NexusTrader E2E Test Runner ==="
echo ""

# Ensure we're in the web/ directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "[1/4] Cleaning up any previous E2E stack..."
docker compose -f docker-compose.e2e.yml down -v 2>/dev/null || true

echo "[2/4] Building and starting full stack (postgres + redis + api + frontend + playwright)..."
docker compose -f docker-compose.e2e.yml up --build --abort-on-container-exit 2>&1

EXIT_CODE=$?

echo ""
echo "[3/4] Collecting results..."
if [ -d "playwright-report" ]; then
    echo "  HTML report: web/playwright-report/index.html"
fi
if [ -d "test-results" ]; then
    echo "  Screenshots/traces: web/test-results/"
fi

echo "[4/4] Stopping stack..."
docker compose -f docker-compose.e2e.yml down -v 2>/dev/null || true

echo ""
if [ $EXIT_CODE -eq 0 ]; then
    echo "=== ALL E2E TESTS PASSED ==="
else
    echo "=== E2E TESTS FAILED (exit code: $EXIT_CODE) ==="
fi

exit $EXIT_CODE
