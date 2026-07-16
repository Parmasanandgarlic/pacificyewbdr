@echo off
title Pacific Yew · BDR Command Center
color 0A

echo.
echo   ====================================
echo     Pacific Yew · BDR Command Center
echo   ====================================
echo.

:: ── Resolve script directory ────────────────────────────────────────────────
set "APP_DIR=%~dp0"
cd /d "%APP_DIR%"

:: ── Check Python ────────────────────────────────────────────────────────────
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo         Download it from https://www.python.org/downloads/
    echo         Make sure to check "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo [OK] Python found.

:: ── Create venv if missing ──────────────────────────────────────────────────
if not exist "%APP_DIR%venv\Scripts\activate.bat" (
    echo [SETUP] Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
)

:: ── Activate venv ───────────────────────────────────────────────────────────
call "%APP_DIR%venv\Scripts\activate.bat"
echo [OK] Virtual environment activated.

:: ── Install / update deps ───────────────────────────────────────────────────
echo [DEPS] Installing dependencies (this may take a moment on first run)...
pip install -q -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)
echo [OK] Dependencies ready.

:: ── Check for .env ──────────────────────────────────────────────────────────
if not exist "%APP_DIR%.env" (
    echo.
    echo [WARNING] No .env file found!
    echo           Copy .env.example to .env and fill in your API keys.
    echo           The app will not work without valid credentials.
    echo.
    pause
    exit /b 1
)
echo [OK] .env file found.

:: ── Launch Streamlit ────────────────────────────────────────────────────────
echo.
echo   Starting Pacific Yew BDR Command Center...
echo   The app will open in your default browser.
echo   Press Ctrl+C in this window to stop the server.
echo.

streamlit run app.py ^
    --server.headless true ^
    --browser.gatherUsageStats false ^
    --server.address localhost ^
    --server.port 8501 ^
    --theme.base dark

:: ── Cleanup on exit ─────────────────────────────────────────────────────────
echo.
echo   Server stopped. Goodbye!
pause
