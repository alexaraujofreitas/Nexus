#!/usr/bin/env bash
# ============================================================
# NEXUS TRADER — Production Deploy (Linux/macOS)
#
# Builds and launches the full stack:
#   postgres, redis, api, engine, frontend, cloudflare tunnel
#
# Usage:
#   cd /path/to/NexusTrader/web
#   bash deploy.sh
# ============================================================
set -euo pipefail

echo ""
echo "========================================"
echo " NexusTrader Production Deploy"
echo "========================================"
echo ""

# Check .env.prod exists
if [ ! -f .env.prod ]; then
    echo "ERROR: .env.prod not found."
    echo "Copy .env.prod and fill in CLOUDFLARE_TUNNEL_TOKEN."
    exit 1
fi

# Build and start all services
echo "[1/3] Building and starting all services..."
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build

echo ""
echo "[2/3] Waiting for services to become healthy..."
sleep 15

echo ""
echo "[3/3] Service status:"
docker compose -f docker-compose.prod.yml ps

echo ""
echo "========================================"
echo " Deploy complete."
echo " Frontend:  http://nexustrader:5173"
echo " Backend:   http://nexus-api:8000"
echo " Public:    https://www.kivikdg.com"
echo "========================================"
echo ""
echo "Tail logs with:"
echo "  docker compose -f docker-compose.prod.yml logs -f"
echo ""
