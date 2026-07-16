@echo off
title Pacific Yew · Installer
color 0A

echo.
echo   ====================================
echo     Pacific Yew · BDR Installer
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

for /f "tokens=*" %%i in ('python --version 2^>^&1') do set PYVER=%%i
echo [OK] %PYVER% found.

:: ── Create venv ─────────────────────────────────────────────────────────────
echo.
echo [1/4] Setting up virtual environment...
if not exist "%APP_DIR%venv\Scripts\activate.bat" (
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
    echo       Virtual environment created.
) else (
    echo       Virtual environment already exists. Skipping.
)

:: ── Install deps ────────────────────────────────────────────────────────────
echo.
echo [2/4] Installing dependencies...
call "%APP_DIR%venv\Scripts\activate.bat"
pip install -q -r requirements.txt
if %errorlevel% neq 0 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)
echo       All dependencies installed.

:: ── Check .env ──────────────────────────────────────────────────────────────
echo.
echo [3/4] Checking configuration...
if not exist "%APP_DIR%.env" (
    echo.
    echo   [WARNING] No .env file found!
    echo   Copying .env.example to .env — you MUST fill in your API keys.
    if exist "%APP_DIR%.env.example" (
        copy "%APP_DIR%.env.example" "%APP_DIR%.env" >nul
        echo   Created .env from template. Edit it with your credentials.
    ) else (
        echo   No .env.example found either. Create .env manually.
    )
) else (
    echo       .env file found.
)

:: ── Create Desktop Shortcut ────────────────────────────────────────────────
echo.
echo [4/4] Creating desktop shortcut...

set "SHORTCUT_PATH=%USERPROFILE%\Desktop\Pacific Yew BDR.lnk"
set "TARGET_PATH=%APP_DIR%run.bat"
set "ICON_PATH=%APP_DIR%pacific_yew.ico"
set "WORKING_DIR=%APP_DIR%"

:: Use PowerShell to create a proper Windows shortcut
powershell -NoProfile -Command ^
    "$ws = New-Object -ComObject WScript.Shell; ^
     $sc = $ws.CreateShortcut('%SHORTCUT_PATH%'); ^
     $sc.TargetPath = '%TARGET_PATH%'; ^
     $sc.WorkingDirectory = '%WORKING_DIR%'; ^
     $sc.Description = 'Pacific Yew BDR Command Center'; ^
     if (Test-Path '%ICON_PATH%') { $sc.IconLocation = '%ICON_PATH%,0' }; ^
     $sc.Save()"

if %errorlevel% equ 0 (
    echo       Desktop shortcut created!
) else (
    echo       [WARNING] Could not create shortcut. You can run run.bat directly.
)

:: ── Done ────────────────────────────────────────────────────────────────────
echo.
echo   ====================================
echo     Installation Complete!
echo   ====================================
echo.
echo   To start the app:
echo     - Double-click "Pacific Yew BDR" on your Desktop
echo     - Or run "run.bat" from this folder
echo.
echo   The app will open in your default browser
echo   at http://localhost:8501
echo.
pause
