@echo off
rem Opens the phonemic window (runs setup first if needed).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" call "%~dp0setup.bat"
if not exist ".venv\Scripts\pythonw.exe" exit /b 1
start "" ".venv\Scripts\pythonw.exe" -m phonemic.gui
