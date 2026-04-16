@echo off
REM ============================================================
REM  KLH Listing Tool - one-time setup (Windows)
REM ============================================================
REM  Run this ONCE per machine from C:\KLH\klh-listing-tool\
REM  after `git clone`. It will:
REM    1. Create the Python venv
REM    2. Install dependencies
REM    3. Create %USERPROFILE%\.klh\ with config.yaml + empty .env
REM       (auto-detects OneDrive desktop redirect, no hardcoded user)
REM    4. Create C:\KLH\data\ working dirs
REM    5. Create ONE\ and TWO\ on the real desktop
REM
REM  After this finishes, open %USERPROFILE%\.klh\.env in Notepad
REM  and paste in the eBay API credentials (see INSTALL.md).
REM  Then double-click the desktop shortcut to launch.
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0.."

echo.
echo === Step 1/5: Python check ===
where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python not on PATH. Install Python 3.11+ from python.org
    echo        and tick "Add python.exe to PATH" during install.
    pause
    exit /b 1
)
python --version

echo.
echo === Step 2/5: Creating virtualenv ===
if exist .venv (
    echo venv already exists, skipping.
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: venv creation failed.
        pause
        exit /b 1
    )
)

echo.
echo === Step 3/5: Installing dependencies ===
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip --quiet
pip install -e .[dashboard]
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause
    exit /b 1
)

echo.
echo === Step 4/5: Creating config directory ===
set KLH_CFG=%USERPROFILE%\.klh
if not exist "%KLH_CFG%" mkdir "%KLH_CFG%"

REM ── Detect the real desktop ──────────────────────────────────────
REM OneDrive on Windows often redirects %USERPROFILE%\Desktop into
REM %USERPROFILE%\OneDrive\Desktop. We need the path the user actually
REM sees in Finder/Explorer so ONE\ and TWO\ land on the visible desktop.
if exist "%USERPROFILE%\OneDrive\Desktop" (
    set DESKTOP=%USERPROFILE%\OneDrive\Desktop
    echo Detected OneDrive-redirected desktop.
) else (
    set DESKTOP=%USERPROFILE%\Desktop
    echo Using standard desktop.
)

REM YAML wants forward slashes — convert backslashes for the config file.
set "DESKTOP_FWD=!DESKTOP:\=/!"

if exist "%KLH_CFG%\config.yaml" (
    echo config.yaml already exists at %KLH_CFG%\config.yaml - leaving alone.
) else (
    REM Copy template, then substitute __DESKTOP__ placeholder via
    REM PowerShell (batch's string ops can't do file-wide replace cleanly).
    copy /Y install\config.yaml.template "%KLH_CFG%\config.yaml" >nul
    powershell -NoProfile -Command "(Get-Content -Raw '%KLH_CFG%\config.yaml') -replace '__DESKTOP__', '%DESKTOP_FWD%' | Set-Content -NoNewline '%KLH_CFG%\config.yaml'"
    echo Wrote %KLH_CFG%\config.yaml with desktop = %DESKTOP%
)

if exist "%KLH_CFG%\.env" (
    echo .env already exists - leaving alone.
) else (
    copy /Y install\env.template "%KLH_CFG%\.env" >nul
    echo Wrote %KLH_CFG%\.env  [EDIT THIS FILE WITH API CREDS BEFORE FIRST LAUNCH]
)

echo.
echo === Step 5/5: Creating data directories ===
if not exist "C:\KLH\data" mkdir "C:\KLH\data"
if not exist "C:\KLH\data\normalized" mkdir "C:\KLH\data\normalized"
if not exist "C:\KLH\data\mockups" mkdir "C:\KLH\data\mockups"
if not exist "C:\KLH\data\listed" mkdir "C:\KLH\data\listed"
echo Created C:\KLH\data\ working dirs.

REM Create ONE\ and TWO\ on the real (possibly OneDrive-redirected) desktop.
if exist "%DESKTOP%" (
    if not exist "%DESKTOP%\ONE" mkdir "%DESKTOP%\ONE"
    if not exist "%DESKTOP%\TWO" mkdir "%DESKTOP%\TWO"
    echo Created ONE\ and TWO\ at %DESKTOP%
) else (
    echo WARNING: Could not find %DESKTOP% — create ONE\ and TWO\ by hand.
)

echo.
echo ============================================================
echo  SETUP COMPLETE.
echo.
echo  Next:
echo    1. Open %KLH_CFG%\.env in Notepad and paste in API creds
echo       (EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID)
echo    2. Copy %KLH_CFG%\tokens.json from the master machine
echo       (contains the OAuth refresh token)
echo    3. Double-click the "KLH Listing Tool" desktop shortcut
echo ============================================================
pause
endlocal
