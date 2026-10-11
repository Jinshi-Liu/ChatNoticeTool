@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" -B -X utf8 "%~dp0todo_widget.py"
) else (
  echo Create a virtual environment first: py -3 -m venv .venv
  echo Alternatively, run: python todo_widget.py
  pause
  exit /b 1
)
