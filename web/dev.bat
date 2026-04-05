@echo off
REM ============================================================
REM NEXUS TRADER — Local Development Launcher (Windows)
REM
REM Launches the full local stack WITHOUT Docker:
REM   1. Frontend  (Vite dev server at :5173)
REM   2. Engine    (Trading engine with auto-connect)
REM
REM Usage:
REM   cd C:\Users\alexa\NexusTrader\web
REM   dev.bat              Start frontend + engine
REM   dev.bat engine       Start engine only
REM   dev.bat frontend     Start frontend only
REM   dev.bat stop         Kill all NexusTrader processes
REM ============================================================

cd /d "%~dp0"
cd ..

if "%1"=="engine" goto :engine_only
if "%1"=="frontend" goto :frontend_only
if "%1"=="stop" goto :stop
goto :start_all

:stop
echo.
echo  Stopping NexusTrader local processes...
taskkill /f /fi "WINDOWTITLE eq NexusTrader*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq nexus-*" >nul 2>&1
echo  Done.
goto :eof

:engine_only
echo.
echo  Starting NexusTrader Engine...
echo.
python -m web.engine.main
goto :eof

:frontend_only
echo.
echo  Starting NexusTrader Frontend...
echo.
cd web\frontend
call npm run dev
goto :eof

:start_all
echo.
echo ========================================
echo  NexusTrader Local Dev Launcher
echo ========================================
echo.
echo  NOTE: Redis is not available locally.
echo  The engine runs standalone with SQLite.
echo  Web dashboard will show "Engine Disconnected"
echo  (this is expected without Redis).
echo.

echo [1/2] Starting Frontend (Vite :5173)...
start "NexusTrader Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

echo [2/2] Starting Engine...
echo.
echo ========================================
echo  Frontend:  http://localhost:5173
echo  Engine:    Running in this window
echo ========================================
echo.

cd /d %~dp0..
python -m web.engine.main
goto :eof
