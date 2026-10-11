@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Create a virtual environment first: py -3 -m venv .venv
  pause
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" -B -X utf8 "%~dp0chatnotice.py" doctor
if errorlevel 1 (pause & exit /b 1)
"%~dp0.venv\Scripts\python.exe" -B -X utf8 "%~dp0chatnotice.py" status
pause
