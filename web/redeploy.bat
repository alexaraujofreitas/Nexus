@echo off
REM ============================================================
REM NEXUS TRADER — Quick Redeploy
REM
REM Rebuilds and restarts the full Docker stack.
REM After this, both URLs serve the latest code:
REM   - http://localhost:5173   (local)
REM   - https://www.kivikdg.com (public via Cloudflare Tunnel)
REM
REM Usage:
REM   Double-click, or from terminal:
REM   cd C:\Users\alexa\NexusTrader\web
REM   redeploy.bat
REM ============================================================

cd /d "%~dp0"

echo.
echo ========================================
echo  NexusTrader — Rebuilding ^& Redeploying
echo ========================================
echo.

REM Check .env.prod exists
if not exist .env.prod (
    echo ERROR: .env.prod not found.
    exit /b 1
)

REM Stop running containers
echo [1/4] Stopping current containers...
docker compose -f docker-compose.prod.yml --env-file .env.prod down
echo.

REM Rebuild everything (--build forces fresh image builds)
echo [2/4] Building fresh images with latest code...
docker compose -f docker-compose.prod.yml --env-file .env.prod build --no-cache
echo.

REM Start all services
echo [3/4] Starting all services...
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
echo.

REM Wait and show status
echo [4/4] Waiting for services to stabilize...
timeout /t 15 /nobreak >nul
echo.

docker compose -f docker-compose.prod.yml --env-file .env.prod ps

echo.
echo ========================================
echo  Redeploy complete!
echo  Local:   http://localhost:5173
echo  Public:  https://www.kivikdg.com
echo ========================================
echo.
pause
