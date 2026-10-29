@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYTHON=%~dp0.venv\Scripts\python.exe"
) else (
  set "PYTHON=python"
)
rem Preview only. Full deletion requires an explicit separate command.
"%PYTHON%" -B -X utf8 "%~dp0cleanup.py" runtime
pause
