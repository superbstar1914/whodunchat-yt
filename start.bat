@echo off
REM WhoDunChat local launcher for Windows.
REM Double-click this file, or run it from a terminal: start.bat

chcp 65001 >nul
setlocal enabledelayedexpansion

cd /d "%~dp0"

echo ============================================
echo   WhoDunChat - Local Launcher
echo ============================================
echo.

REM ---- 1. Check Python is available ----
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python was not found on PATH.
    echo Please install Python 3.10+ from https://www.python.org/downloads/
    echo and make sure "Add python.exe to PATH" is checked during install.
    pause
    exit /b 1
)

python --version

REM ---- 2. Create venv if it doesn't exist yet ----
if not exist "venv\Scripts\activate.bat" (
    echo.
    echo [INFO] Creating virtual environment in .\venv ...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
)

REM ---- 3. Activate venv ----
call venv\Scripts\activate.bat

REM ---- 4. Install/update dependencies ----
echo.
echo [INFO] Installing dependencies (this can take a while the first time)...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERROR] Dependency install failed. See the error above.
    echo Common causes:
    echo   - No internet connection
    echo   - A package needs a newer/older Python version
    pause
    exit /b 1
)

REM ---- 5. Run logic tests (optional sanity check, non-blocking) ----
echo.
echo [INFO] Running local logic tests (no network required)...
set PYTHONPATH=%cd%
python tests\test_pipeline_logic.py
echo.

REM ---- 6. Start the server ----
echo [INFO] Starting server at http://localhost:8000
echo [INFO] Press CTRL+C to stop the server.
echo.
python -m uvicorn app.main:app --reload --port 8000

pause
