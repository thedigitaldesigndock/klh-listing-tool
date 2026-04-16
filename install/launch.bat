@echo off
REM ============================================================
REM  KLH Listing Tool - launcher (Windows)
REM ============================================================
REM  Pulls the latest code from GitHub, starts the dashboard
REM  server, opens the browser. Close the cmd window to stop.
REM ============================================================

cd /d "%~dp0.."

echo Fetching latest version from GitHub...
git pull --quiet
if errorlevel 1 (
    echo WARNING: git pull failed - continuing with current local version.
    echo          Check your internet connection. Press any key to continue.
    pause >nul
)

REM Open browser after a brief delay so the server has time to bind.
REM We pass --no-browser to the server so it doesn't also open a tab —
REM otherwise we'd get two duplicate localhost tabs on every launch.
start "" /B cmd /c "timeout /t 3 >nul && start http://localhost:8765"

echo.
echo Starting dashboard on http://localhost:8765
echo (Close this window to stop the server.)
echo.

call .venv\Scripts\activate.bat
python -m dashboard.server --port 8765 --no-browser
