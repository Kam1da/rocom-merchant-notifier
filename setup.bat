@echo off
cd /d "%~dp0"
echo ============================================
echo   Merchant Tray - Setup
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    set "PYTHON_CMD=py"
) else (
    set "PYTHON_CMD=python"
)

echo [1/3] Installing Python dependencies...
%PYTHON_CMD% -m pip install -r requirements.txt
if errorlevel 1 (
    echo Dependency install failed. Please check your Python setup.
    pause
    exit /b 1
)

echo [2/3] Checking runtime folders...
if not exist "data" mkdir "data"
if not exist "assets\icon.png" (
    echo Missing assets\icon.png. Please keep the assets folder with this project.
    pause
    exit /b 1
)

echo [3/3] Adding to startup...
powershell -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut('%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\MerchantTray.lnk');$s.TargetPath='%~dp0start.bat';$s.WorkingDirectory='%~dp0';$s.Save()"
echo        Done! Will auto-launch on next boot.

echo.
echo Setup complete! Double-click start.bat to launch.
echo.
pause
