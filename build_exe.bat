@echo off
rem Builds dist\phonemic\phonemic.exe (plus dist\phonemic-windows.zip) with PyInstaller.
rem The result runs on Windows PCs without Python installed.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)

echo Installing PyInstaller...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q pyinstaller
if errorlevel 1 goto :failed

echo Building phonemic.exe (takes a minute or two)...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --log-level WARN ^
    --windowed --name phonemic ^
    --icon "%~dp0phonemic\assets\phonemic.ico" ^
    --add-data "%~dp0phonemic\web;phonemic\web" ^
    --add-data "%~dp0phonemic\assets;phonemic\assets" ^
    --distpath "%~dp0dist" --workpath "%~dp0build\pyinstaller" --specpath "%~dp0build" ^
    "%~dp0scripts\phonemic_exe.py"
if errorlevel 1 goto :failed

echo Zipping...
powershell -NoProfile -Command "Compress-Archive -Force -Path '%~dp0dist\phonemic' -DestinationPath '%~dp0dist\phonemic-windows.zip'"
if errorlevel 1 goto :failed

echo.
echo Done: dist\phonemic\phonemic.exe  (share dist\phonemic-windows.zip)
pause
exit /b 0

:failed
echo.
echo Build failed - see the messages above.
pause
exit /b 1
