#!/bin/bash
# ============================================================
# NexusTrader Web — Production Deployment Script
#
# Usage:
#   chmod +x infra/scripts/deploy.sh
#   ./infra/scripts/deploy.sh
#
# Prerequisites:
#   - Docker + Docker Compose installed
#   - .env file configured (see .env.example)
#   - cloudflared installed and authenticated (for tunnel)
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
WEB_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "╔══════════════════════════════════════════════════╗"
echo "║       NexusTrader Web — Production Deploy        ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── Pre-flight checks ─────────────────────────────────────
echo "[1/6] Pre-flight checks..."

if ! command -v docker &> /dev/null; then
    echo "ERROR: docker not found. Install Docker first."
    exit 1
fi

if ! command -v docker compose &> /dev/null; then
    echo "ERROR: docker compose not found. Install Docker Compose v2."
    exit 1
fi

if [ ! -f "$WEB_DIR/.env" ]; then
    echo "WARNING: No .env file found. Using defaults (NOT suitable for production)."
    echo "  Copy .env.example to .env and configure before production deployment."
fi

# ── Build images ───────────────────────────────────────────
echo "[2/6] Building Docker images..."
cd "$WEB_DIR"
docker compose build --no-cache

# ── Start services ─────────────────────────────────────────
echo "[3/6] Starting services..."
docker compose up -d

# ── Wait for health ────────────────────────────────────────
echo "[4/6] Waiting for services to be healthy..."
MAX_WAIT=60
ELAPSED=0
while [ $ELAPSED -lt $MAX_WAIT ]; do
    if docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" 2>/dev/null; then
        echo "  API is healthy!"
        break
    fi
    echo "  Waiting... ($ELAPSED/${MAX_WAIT}s)"
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done

if [ $ELAPSED -ge $MAX_WAIT ]; then
    echo "ERROR: API did not become healthy within ${MAX_WAIT}s"
    echo "  Check logs: docker compose logs api"
    exit 1
fi

# ── Verify database ───────────────────────────────────────
echo "[5/6] Verifying database connection..."
docker compose exec -T postgres pg_isready -U "${POSTGRES_USER:-nexus}" -d "${POSTGRES_DB:-nexustrader}"

# ── Status report ──────────────────────────────────────────
echo "[6/6] Deployment status:"
echo ""
docker compose ps
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║  Deployment complete!                             ║"
echo "║                                                   ║"
echo "║  API:       http://localhost:8000                  ║"
echo "║  Health:    http://localhost:8000/health            ║"
echo "║  Frontend:  Build and serve via nginx (port 3000)  ║"
echo "║                                                   ║"
echo "║  Next steps:                                       ║"
echo "║  1. Run: cloudflared tunnel --config               ║"
echo "║     infra/cloudflared/config.yml run nexustrader   ║"
echo "║  2. Configure Cloudflare Access application        ║"
echo "║  3. Set NEXUS_CF_ENABLED=true in .env              ║"
echo "╚══════════════════════════════════════════════════╝"
