@echo off
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
where pythonw >nul 2>nul
if errorlevel 1 (
    start "" pyw -m merchant_tray.launcher
) else (
    start "" pythonw -m merchant_tray.launcher
)
