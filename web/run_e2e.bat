@echo off
REM ============================================================
REM NexusTrader — Run Playwright E2E Tests in Docker (Windows)
REM
REM Usage: cd web && run_e2e.bat
REM ============================================================

echo === NexusTrader E2E Test Runner ===
echo.

cd /d "%~dp0"

echo [1/4] Cleaning up any previous E2E stack...
docker compose -f docker-compose.e2e.yml down -v 2>nul

echo [2/4] Building (no cache) and starting full stack...
docker compose -f docker-compose.e2e.yml build --no-cache
docker compose -f docker-compose.e2e.yml up --abort-on-container-exit
set EXIT_CODE=%ERRORLEVEL%

echo.
echo [3/4] Results:
if exist playwright-report (
    echo   HTML report: web\playwright-report\index.html
)
if exist test-results (
    echo   Screenshots/traces: web\test-results\
)

echo [4/4] Stopping stack...
docker compose -f docker-compose.e2e.yml down -v 2>nul

echo.
if %EXIT_CODE%==0 (
    echo === ALL E2E TESTS PASSED ===
) else (
    echo === E2E TESTS FAILED (exit code: %EXIT_CODE%) ===
)

exit /b %EXIT_CODE%
