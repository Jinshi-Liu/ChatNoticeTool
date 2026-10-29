@echo off
setlocal
set PYTHONDONTWRITEBYTECODE=1
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  set "PYTHON=%~dp0.venv\Scripts\pythonw.exe"
) else (
  set "PYTHON=pythonw"
)
start "" "%PYTHON%" -B -X utf8 "%~dp0todo_widget.py"
endlocal
