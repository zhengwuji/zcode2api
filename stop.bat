@echo off
title ZCode2API Stop
cd /d "%~dp0"

echo ========================================================
echo               Stopping ZCode2API
echo ========================================================
echo.

set STOPPED=0

if exist "%~dp0zcode2api.pid" (
    set /p RUN_PID=<"%~dp0zcode2api.pid"
    if defined RUN_PID (
        echo [*] Stopping process PID %RUN_PID%...
        taskkill /F /T /PID %RUN_PID% >nul 2>&1
        if not errorlevel 1 set STOPPED=1
    )
    del /f /q "%~dp0zcode2api.pid" >nul 2>&1
)

if exist "%~dp0zcode2api.port" (
    set /p RUN_PORT=<"%~dp0zcode2api.port"
    del /f /q "%~dp0zcode2api.port" >nul 2>&1
) else (
    set RUN_PORT=3335
)

if defined RUN_PORT (
    echo [*] Checking port %RUN_PORT%...
    for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%RUN_PORT% " ^| findstr "LISTENING"') do (
        echo [*] Killing process on port %RUN_PORT%: PID %%a
        taskkill /F /T /PID %%a >nul 2>&1
        set STOPPED=1
    )
)

echo.
if "%STOPPED%"=="1" (
    echo [OK] ZCode2API has been stopped.
) else (
    echo [*] No running ZCode2API process found.
)
echo ========================================================
echo.
pause
