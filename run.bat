@echo off
rem ELPCam launcher for Windows: creates .venv on first run, then starts the browser GUI.
rem Extra arguments are passed through, e.g.  run.bat --simulate   or   run.bat serve --port 9000
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || (echo Python 3.10+ is required: https://www.python.org/downloads/ & pause & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
)
".venv\Scripts\python.exe" -m elpcam %*
if errorlevel 1 pause
