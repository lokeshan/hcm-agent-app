@echo off
REM ============================================================
REM  HR Assistant platform - one-click launcher (Windows)
REM  Double-click this file to install deps and start the app.
REM ============================================================
cd /d "%~dp0"

REM Pick the Python launcher that exists on this machine
where python >nul 2>nul && (set PYEXE=python) || (set PYEXE=py)

echo Installing dependencies (first run only, may take a minute)...
%PYEXE% -m pip install -r requirements.txt
echo.
echo ============================================================
echo   HR Assistant is starting...
echo.
echo   End-User App  :  http://localhost:8000/
echo   Admin Console :  http://localhost:8000/admin
echo.
echo   Leave this window open. Press Ctrl+C to stop the app.
echo ============================================================
echo.
%PYEXE% server.py
pause
