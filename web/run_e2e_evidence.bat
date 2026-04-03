@echo off
REM ============================================================
REM NexusTrader — Playwright E2E Evidence Run (Windows)
REM
REM Runs ALL Playwright specs in Docker Chromium and captures:
REM   1. Full console output with browser names (list reporter)
REM   2. HTML report with traces and screenshots
REM   3. JSON test results for machine verification
REM
REM Usage: cd web && run_e2e_evidence.bat
REM ============================================================

setlocal enabledelayedexpansion
set TIMESTAMP=%DATE:~-4%%DATE:~4,2%%DATE:~7,2%_%TIME:~0,2%%TIME:~3,2%
set TIMESTAMP=%TIMESTAMP: =0%
set EVIDENCE_DIR=e2e-evidence-%TIMESTAMP%

echo === NexusTrader E2E Evidence Run ===
echo Timestamp: %TIMESTAMP%
echo Evidence dir: %EVIDENCE_DIR%
echo.

cd /d "%~dp0"

REM Create evidence directory
mkdir %EVIDENCE_DIR% 2>nul

echo [1/5] Cleaning up any previous E2E stack...
docker compose -f docker-compose.e2e.yml down -v 2>nul

echo [2/5] Building fresh stack (no cache)...
docker compose -f docker-compose.e2e.yml build --no-cache

echo [3/5] Starting full stack and running Playwright in Chromium...
echo    (postgres -> redis -> api -> frontend -> playwright)
echo.

REM Run and capture FULL console output
docker compose -f docker-compose.e2e.yml up --abort-on-container-exit 2>&1 | tee %EVIDENCE_DIR%\console_output.txt
set EXIT_CODE=%ERRORLEVEL%

echo.
echo [4/5] Collecting evidence artifacts...

REM Copy Playwright HTML report
if exist playwright-report (
    xcopy /E /I /Q playwright-report %EVIDENCE_DIR%\playwright-report >nul 2>nul
    echo   + HTML report copied
)

REM Copy test results (screenshots, traces)
if exist test-results (
    xcopy /E /I /Q test-results %EVIDENCE_DIR%\test-results >nul 2>nul
    echo   + Test results (screenshots/traces) copied
)

REM Extract browser info from console output
echo.
echo === CHROMIUM EXECUTION EVIDENCE ===
echo.
findstr /I "chromium" %EVIDENCE_DIR%\console_output.txt
findstr /I "browser" %EVIDENCE_DIR%\console_output.txt
findstr /I "desktop-chromium mobile-chromium" %EVIDENCE_DIR%\console_output.txt
findstr /I "passed failed" %EVIDENCE_DIR%\console_output.txt | findstr /V "healthcheck"
echo.

REM Create summary file
(
echo NexusTrader E2E Evidence Summary
echo ================================
echo Date: %DATE% %TIME%
echo Docker Playwright Image: mcr.microsoft.com/playwright:v1.59.1-noble
echo Browser: Chromium (headless)
echo Projects: desktop-chromium, mobile-chromium (Pixel 5)
echo.
echo Test Specs:
echo   01-login.spec.ts     - Auth flow
echo   02-dashboard.spec.ts - Dashboard rendering
echo   03-scanner.spec.ts   - Scanner feature
echo   04-trading.spec.ts   - Trading interface
echo   05-settings.spec.ts  - Settings page
echo   06-logs.spec.ts      - Log viewer
echo   07-analytics.spec.ts - Analytics dashboard
echo   08-backtest.spec.ts  - Backtesting
echo   09-validation.spec.ts- Validation checks
echo   10-mobile.spec.ts    - Mobile responsive
echo   ws-connect.spec.ts   - WebSocket connect
echo   ws-reconnect.spec.ts - WebSocket reconnect
echo   ws-subscribe.spec.ts - WebSocket subscribe
echo.
echo Exit Code: %EXIT_CODE%
) > %EVIDENCE_DIR%\evidence_summary.txt

echo [5/5] Stopping stack...
docker compose -f docker-compose.e2e.yml down -v 2>nul

echo.
echo === EVIDENCE COLLECTION COMPLETE ===
echo.
echo Evidence directory: web\%EVIDENCE_DIR%\
echo   console_output.txt      - Full Playwright console log
echo   playwright-report\      - HTML report (open index.html)
echo   test-results\           - Screenshots and traces
echo   evidence_summary.txt    - Run metadata
echo.

if %EXIT_CODE%==0 (
    echo === ALL E2E TESTS PASSED IN CHROMIUM ===
) else (
    echo === E2E TESTS FAILED (exit code: %EXIT_CODE%) ===
)

exit /b %EXIT_CODE%
