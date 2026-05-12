@echo off
setlocal EnableDelayedExpansion

REM ========================================================================
REM KLH Dashboard one-click updater (Windows).
REM
REM What it does:
REM   1. cd into the klh-listing-tool repo
REM   2. git pull (gets latest code + extra_images)
REM   3. Rewrites the extra_images_dir line in ~/.klh/config.yaml so it
REM      points at the in-repo path on THIS PC
REM   4. Prints diagnostic output so we can SEE what's set + what's on disk
REM   5. Tells the user to close + reopen the dashboard
REM ========================================================================

echo.
echo ============================================================
echo   KLH Dashboard updater
echo ============================================================
echo.

set "REPO=%USERPROFILE%\Documents\klh-listing-tool"
set "CONFIG=%USERPROFILE%\.klh\config.yaml"
set "TARGET=%REPO%\extra_images"

echo Resolved paths:
echo   REPO    = %REPO%
echo   CONFIG  = %CONFIG%
echo   TARGET  = %TARGET%
echo.

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

echo --- BEFORE: current extra_images_dir line in config.yaml ---
findstr /R /C:"^[ ]*extra_images_dir:" "%CONFIG%"
echo.

echo Pulling latest from git...
pushd "%REPO%"
git pull --ff-only
if errorlevel 1 (
    echo.
    echo [WARN] git pull had a problem — continuing anyway.
)
echo.
echo Current git HEAD:
git log -1 --format="  %%h %%ad %%s" --date=short
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
echo --- AFTER: current extra_images_dir line in config.yaml ---
findstr /R /C:"^[ ]*extra_images_dir:" "%CONFIG%"
echo.

echo --- extras folder contents on disk ---
if exist "%TARGET%" (
    echo Folder exists: %TARGET%
    echo Subfolders:
    dir /B /AD "%TARGET%" 2>nul
    echo.
    if exist "%TARGET%\6x4 Photos" (
        echo Files in "6x4 Photos":
        dir /B "%TARGET%\6x4 Photos" 2>nul
    ) else (
        echo [WARN] "6x4 Photos" subfolder NOT present under %TARGET%
        echo        Try running this updater again, or check git pull errors above.
    )
) else (
    echo [ERROR] %TARGET% does not exist on disk.
    echo Git pull may have failed. Check the messages above.
)

echo.
echo ============================================================
echo   Done.
echo ============================================================
echo.
echo Next: close the KLH Dashboard window if it's open, then double-click
echo the KLH Dashboard shortcut on the desktop to start it again.
echo After it opens, check the version pill (top right of the dashboard) —
echo it should match the git HEAD shown above.
echo.
pause
endlocal
