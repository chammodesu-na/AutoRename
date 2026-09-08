@echo off
REM ============================================================
REM AutoRename - build script
REM Run this INSIDE this folder, on Windows with Python 3.10+.
REM ============================================================

echo [1/3] Installing required packages...
pip install -r requirements.txt
if errorlevel 1 (
    echo Package install failed. Check your pip/Python installation.
    pause
    exit /b 1
)

echo [2/3] Cleaning previous build folders...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q AutoRename.spec 2>nul

echo [3/3] Building exe with PyInstaller...
python -m PyInstaller --noconfirm --onefile --windowed --name "AutoRename" --icon "app_icon.ico" tray_app.py

if errorlevel 1 (
    echo Build failed. Check the error message above.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo Build complete: dist\AutoRename.exe
echo Next step: open installer.iss with Inno Setup and compile it.
echo This will produce Output\AutoRenameSetup.exe
echo ============================================================
pause
