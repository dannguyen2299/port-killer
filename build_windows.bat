@echo off
setlocal
cd /d "%~dp0"

python -m pip install --user pyinstaller
if errorlevel 1 exit /b %errorlevel%

python -m PyInstaller --clean --onefile --name port-killer-windows --add-data "index.html;." app.py
if errorlevel 1 exit /b %errorlevel%

echo Built: %cd%\dist\port-killer-windows.exe
