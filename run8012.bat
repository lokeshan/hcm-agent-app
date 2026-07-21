@echo off
cd /d "%~dp0"
set "PYEXE=python"
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
set PORT=8012
echo Starting fixed build on http://localhost:8012/
"%PYEXE%" server.py
pause
