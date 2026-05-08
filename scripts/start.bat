@echo off
REM Single-command launcher for the reference checker on Windows.
setlocal enabledelayedexpansion

cd /d "%~dp0\.."

REM --- 1. Check Docker ---
where docker >nul 2>&1
if errorlevel 1 (
    echo.
    echo ERROR: Docker is not installed.
    echo.
    echo Install Docker Desktop:
    echo   https://www.docker.com/products/docker-desktop/
    echo.
    echo Then re-run this script.
    pause
    exit /b 1
)

docker info >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker is installed but the daemon isn't running.
    echo Start Docker Desktop, then re-run this script.
    pause
    exit /b 1
)

docker compose version >nul 2>&1
if errorlevel 1 (
    echo ERROR: 'docker compose' v2 is required. Update Docker Desktop.
    pause
    exit /b 1
)

REM --- 2. First-run config ---
if not exist ".env" (
    copy "env.example" ".env" >nul
    echo.
    echo A new .env file has been created at:
    echo    %CD%\.env
    echo.
    echo Please open it and set OPENALEX_EMAIL to your email address.
    echo ^(Any valid email works; this gives you faster rate limits.^)
    echo Then re-run start.bat.
    echo.
    notepad .env
    exit /b 0
)

REM --- 3. Start the stack ---
echo Starting GROBID + reference checker...
echo   ^(first run downloads ~2GB GROBID image; subsequent runs are fast^)
echo.
docker compose up -d --build
if errorlevel 1 (
    echo.
    echo ERROR: docker compose failed to start the stack.
    pause
    exit /b 1
)

REM --- 4. Wait for health ---
echo.
echo Waiting for GROBID to finish loading ^(Java + ML models, ~30-60s^)...
set GROBID_OK=0
for /l %%i in (1,1,60) do (
    curl -fsS http://localhost:8070/api/isalive >nul 2>&1
    if not errorlevel 1 (
        set GROBID_OK=1
        goto grobid_done
    )
    <nul set /p =.
    timeout /t 2 /nobreak >nul
)
:grobid_done
echo.
if %GROBID_OK%==1 echo   [ok] GROBID is up

set APP_OK=0
for /l %%i in (1,1,30) do (
    curl -fsS http://localhost:5000/api/health >nul 2>&1
    if not errorlevel 1 (
        set APP_OK=1
        goto app_done
    )
    <nul set /p =.
    timeout /t 1 /nobreak >nul
)
:app_done
echo.
if %APP_OK%==1 echo   [ok] Reference checker app is up

echo.
echo ================================================================
echo   Reference checker is running.
echo.
echo   Web UI:        http://localhost:5000
echo   Health check:  http://localhost:5000/api/health
echo.
echo   To stop:       docker compose down
echo   View logs:     docker compose logs -f
echo   Update:        docker compose pull ^&^& docker compose up -d --build
echo.
echo   For batch CLI mode on 600 PDFs, see README.md - "Batch mode".
echo ================================================================
echo.
pause
