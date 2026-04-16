@echo off
REM ============================================================
REM  KLH Listing Tool - one-time setup (Windows)
REM ============================================================
REM  Run this ONCE per machine from C:\KLH\klh-listing-tool\
REM  after `git clone`. It will:
REM    1. Create the Python venv
REM    2. Install dependencies
REM    3. Create %USERPROFILE%\.klh\ with config.yaml + empty .env
REM    4. Create C:\KLH\data\ working dirs
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

if exist "%KLH_CFG%\config.yaml" (
    echo config.yaml already exists at %KLH_CFG%\config.yaml - leaving alone.
) else (
    copy /Y install\config.yaml.template "%KLH_CFG%\config.yaml" >nul
    echo Wrote %KLH_CFG%\config.yaml
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

REM ONE/ and TWO/ live on the user's desktop — that's where Nicky/Kim
REM drop scans. If %USERPROFILE%\Desktop doesn't resolve (OneDrive-
REM redirected desktops can surprise us), fall through silently and
REM leave it to the user to create by hand.
if exist "%USERPROFILE%\Desktop" (
    if not exist "%USERPROFILE%\Desktop\ONE" mkdir "%USERPROFILE%\Desktop\ONE"
    if not exist "%USERPROFILE%\Desktop\TWO" mkdir "%USERPROFILE%\Desktop\TWO"
    echo Created ONE\ and TWO\ on desktop.
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
