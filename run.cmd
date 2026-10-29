@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
if exist "%~dp0.venv\Scripts\python.exe" (
  set "PYTHON=%~dp0.venv\Scripts\python.exe"
) else (
  set "PYTHON=python"
)
"%PYTHON%" -B -X utf8 "%~dp0agent_setup.py" inspect
if errorlevel 1 exit /b 1
pause
