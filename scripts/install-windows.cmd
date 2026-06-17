@echo off
setlocal
set SCRIPT_DIR=%~dp0
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%setup-windows.ps1"
if errorlevel 1 (
  echo.
  echo Setup failed. Press any key to close.
  pause >nul
  exit /b 1
)
echo.
echo Setup finished. Press any key to close.
pause >nul
