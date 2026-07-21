@echo off
REM Launch the ENHANCED build on port 8011 (deps already installed, Mock already primary).
cd /d "%~dp0"
set "PYEXE=python"
if exist ".venv\Scripts\python.exe" set "PYEXE=.venv\Scripts\python.exe"
set PORT=8011
echo Starting enhanced HR Assistant on http://localhost:8011/
"%PYEXE%" server.py
pause
