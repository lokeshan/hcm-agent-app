@echo off
REM ============================================================
REM  Verification launcher - runs the NEW platform on port 8010
REM  (so it won't collide with anything on 8000) using Mock data.
REM ============================================================
cd /d "%~dp0"
REM Pick a REAL Python: prefer the project venv, then the py launcher, then python.
set "PYEXE="
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
if not defined PYEXE (where py >nul 2>nul && set "PYEXE=py")
if not defined PYEXE set "PYEXE=python"
echo Using Python: %PYEXE%
set PORT=8010

echo Installing dependencies (first run only)...
%PYEXE% -m pip install -r requirements.txt

echo Setting Mock as the primary data source for this walkthrough...
%PYEXE% -c "import connectors; connectors.list_all(); connectors.set_primary('mock')"

echo.
echo ============================================================
echo   NEW platform starting on:
echo     End-User App  :  http://localhost:8010/
echo     Admin Console :  http://localhost:8010/admin
echo ============================================================
echo.
%PYEXE% server.py
pause
