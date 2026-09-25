@echo off
title ZCode2API
cd /d "%~dp0"

if not exist "%~dp0venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found.
    pause
    exit /b 1
)

echo ========================================================
echo               ZCode2API Server
echo  Admin UI : http://127.0.0.1:3335/admin
echo  Password : zcode
echo  Press Ctrl+C or run stop.bat to stop
echo ========================================================
echo.

"%~dp0venv\Scripts\python.exe" "%~dp0main.py" serve %*

echo.
echo Service stopped.
pause
