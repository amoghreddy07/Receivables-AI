@echo off
setlocal
set "ROOT=%~dp0"
cd /d "%ROOT%"

echo ============================================
echo  ReceivablesAI - Dashboard launcher
echo ============================================
echo.

REM --- Virtual environment -----------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Virtual environment not found at .venv\
    echo.
    echo Run setup.bat first to create it:
    echo   setup.bat
    echo.
    echo Or manually:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -e "backend[llm]"
    exit /b 1
)

REM --- Demo database -----------------------------------------------------
if not exist "data\demo.db" (
    echo ERROR: data\demo.db not found.
    echo.
    echo Seed the demo database first:
    echo   cd backend
    echo   ..\.venv\Scripts\python -m scripts.seed_demo
    echo.
    echo Then run start.bat again.
    exit /b 1
)

REM --- Environment -------------------------------------------------------
set "RA_DEMO_MODE=1"
set "RA_DB_URL=sqlite:///../data/demo.db"
set "RA_DEFAULT_MODE=supervised"
set "RA_LLM_MODE=rules"

echo Demo database : data\demo.db
echo Dashboard     : http://127.0.0.1:8000
echo.
echo Press Ctrl+C to stop.
echo.

cd /d "%ROOT%backend"
"%ROOT%.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
