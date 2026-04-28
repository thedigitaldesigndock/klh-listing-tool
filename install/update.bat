@echo off
setlocal EnableDelayedExpansion

REM ========================================================================
REM KLH Dashboard one-click updater for Nicky's PC.
REM
REM What it does:
REM   1. cd into the klh-listing-tool repo
REM   2. git pull (gets latest code + extra_images)
REM   3. Rewrites the extra_images_dir line in ~/.klh/config.yaml so it
REM      points at the in-repo path on THIS PC
REM   4. Tells you to close + reopen the dashboard
REM
REM Run this any time something seems out of date — the dashboard's auto-pull
REM should keep things fresh on its own, but this is the manual safety net.
REM ========================================================================

echo.
echo ============================================================
echo   KLH Dashboard updater
echo ============================================================
echo.

set "REPO=%USERPROFILE%\Documents\klh-listing-tool"
set "CONFIG=%USERPROFILE%\.klh\config.yaml"
set "TARGET=%REPO%\extra_images"

if not exist "%REPO%\.git" (
    echo [ERROR] Repo not found at: %REPO%
    echo Make sure the dashboard is installed in Documents\klh-listing-tool
    pause
    exit /b 1
)

if not exist "%CONFIG%" (
    echo [ERROR] config.yaml missing at: %CONFIG%
    echo Re-run setup-credentials.bat first.
    pause
    exit /b 1
)

echo Pulling latest from git...
pushd "%REPO%"
git pull --ff-only
if errorlevel 1 (
    echo.
    echo [WARN] git pull had a problem — continuing anyway.
)
popd

echo.
echo Updating config.yaml extra_images_dir to:
echo    %TARGET%
echo.

REM Use PowerShell to rewrite the one line — handles spaces/escaping safely.
powershell -NoProfile -Command ^
  "$path = '%CONFIG%';" ^
  "$target = '%TARGET%';" ^
  "$content = Get-Content -Raw $path;" ^
  "$pattern = '(?m)^(\s*extra_images_dir:\s*).*$';" ^
  "if ($content -match $pattern) {" ^
    "$replacement = '${1}' + $target;" ^
    "$new = [regex]::Replace($content, $pattern, $replacement);" ^
    "Set-Content -Path $path -Value $new -NoNewline;" ^
    "Write-Host 'config.yaml updated.'" ^
  "} else {" ^
    "Write-Host '[WARN] extra_images_dir line not found in config.yaml.';" ^
    "Write-Host 'Add this line manually:';" ^
    "Write-Host \"  extra_images_dir: $target\"" ^
  "}"

echo.
echo ============================================================
echo   Done.
echo ============================================================
echo.
echo Next: close the KLH Dashboard window if it's open, then double-click
echo the KLH Dashboard shortcut on the desktop to start it again.
echo.
pause
endlocal
