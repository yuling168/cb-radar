@echo off
setlocal
title CB Lifecycle Replay Updater

echo ======================================================
echo    CB Full Lifecycle Replay Updater (2019 ~ Present)
echo ======================================================
echo.

cd /d "%~dp0"
if exist "cb-radar\scripts\analyze_cb_lifecycle.py" (
    cd /d "%~dp0cb-radar"
)

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Please ensure Python is installed and in PATH.
    pause
    exit /b 1
)

python scripts\analyze_cb_lifecycle.py --min-date 2019-01-01
if errorlevel 1 (
    echo.
    echo [ERROR] Analysis failed. Please check the error message above.
    pause
    exit /b 1
)

echo.
echo ======================================================
echo [SUCCESS] Report updated successfully!
echo File: exports\cb_lifecycle_replay_since_20190101.csv
echo ======================================================
echo.

set /p OPEN_NOW="Open CSV report now? (Y/N, default Y): "
if "%OPEN_NOW%"=="" set OPEN_NOW=Y
if /i "%OPEN_NOW%"=="Y" (
    if exist "exports\cb_lifecycle_replay_since_20190101.csv" (
        start "" "exports\cb_lifecycle_replay_since_20190101.csv"
    )
)

echo.
echo Press any key to exit...
pause >nul
