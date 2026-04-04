@echo off
REM ============================================================
REM NEXUS TRADER — Production Deploy (Windows)
REM
REM Builds and launches the full stack:
REM   postgres, redis, api, engine, frontend, cloudflare tunnel
REM
REM Usage:
REM   cd C:\Users\alexa\NexusTrader\web
REM   deploy.bat
REM
REM Options:
REM   deploy.bat          Build and start all services
REM   deploy.bat stop     Stop all services
REM   deploy.bat restart  Stop then start all services
REM   deploy.bat logs     Tail all service logs
REM   deploy.bat status   Show service status
REM ============================================================

cd /d "%~dp0"

if "%1"=="stop" goto :stop
if "%1"=="restart" goto :restart
if "%1"=="logs" goto :logs
if "%1"=="status" goto :status
goto :start

:stop
echo.
echo  Stopping NexusTrader...
docker compose -f docker-compose.prod.yml --env-file .env.prod down
echo  Stopped.
goto :eof

:restart
echo.
echo  Restarting NexusTrader...
docker compose -f docker-compose.prod.yml --env-file .env.prod down
goto :start

:logs
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f
goto :eof

:status
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
goto :eof

:start
echo.
echo ========================================
echo  NexusTrader Production Deploy
echo ========================================
echo.

REM Check .env.prod exists
if not exist .env.prod (
    echo ERROR: .env.prod not found.
    echo Copy .env.prod.example and fill in your secrets.
    exit /b 1
)

REM Build and start all services
echo [1/3] Building and starting all services...
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build

if %ERRORLEVEL% NEQ 0 (
    echo ERROR: Docker compose failed.
    exit /b 1
)

echo.
echo [2/3] Waiting for services to become healthy...
timeout /t 15 /nobreak >nul

echo.
echo [3/3] Service status:
docker compose -f docker-compose.prod.yml --env-file .env.prod ps

echo.
echo ========================================
echo  Deploy complete.
echo  Local:   http://localhost:5173
echo  Public:  https://www.kivikdg.com
echo ========================================
echo.
echo Commands:
echo   deploy.bat stop      Stop all services
echo   deploy.bat restart   Restart all services
echo   deploy.bat logs      Tail logs
echo   deploy.bat status    Show status
echo.
