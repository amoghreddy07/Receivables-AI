@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

echo ============================================
echo  ReceivablesAI - One-time setup
echo ============================================
echo.

REM --- Python -----------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo ERROR: python not found on PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo Found Python %PYVER%

REM --- Virtual environment -----------------------------------------------
if exist ".venv\Scripts\python.exe" (
    echo Virtual environment already exists at .venv\ — skipping creation.
) else (
    echo Creating virtual environment ...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        exit /b 1
    )
    echo Virtual environment created.
)

REM --- Dependencies ------------------------------------------------------
echo Installing backend dependencies ...
".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
".venv\Scripts\python.exe" -m pip install --quiet -e "backend[llm]"
if errorlevel 1 (
    echo ERROR: pip install failed.
    exit /b 1
)

echo.
echo ============================================
echo  Setup complete.
echo  Run start.bat to launch the dashboard.
echo ============================================
