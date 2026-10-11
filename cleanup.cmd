@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Run with your Python: python cleanup.py runtime
  pause
  exit /b 1
)
rem Preview only. Full deletion requires an explicit separate command.
"%~dp0.venv\Scripts\python.exe" -B -X utf8 "%~dp0cleanup.py" runtime
pause
