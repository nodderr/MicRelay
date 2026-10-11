@echo off
rem One-time setup for phonemic: Python environment, dependencies and shortcuts.
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.10+ from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run this again.
    pause
    exit /b 1
)
python -c "import sys; sys.exit(sys.version_info < (3, 10))"
if errorlevel 1 (
    echo phonemic needs Python 3.10 or newer. Found:
    python --version
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating the Python environment...
    python -m venv .venv
    if errorlevel 1 goto :failed
)

echo Installing dependencies (this can take a minute)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 goto :failed

echo Creating Desktop and Start Menu shortcuts...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\make_shortcut.ps1"
if errorlevel 1 goto :failed

echo.
echo Done! Double-click "phonemic" on your Desktop to start.
echo If no virtual cable is installed yet, get VB-Cable: https://vb-audio.com/Cable/
pause
exit /b 0

:failed
echo.
echo Setup failed - see the messages above.
pause
exit /b 1
