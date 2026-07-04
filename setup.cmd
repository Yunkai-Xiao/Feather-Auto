@echo off
setlocal

set "REPO_ROOT=%~dp0"
set "SETUP_SCRIPT=%REPO_ROOT%scripts\setup-windows.ps1"

if not exist "%SETUP_SCRIPT%" (
  echo Cannot find setup script:
  echo %SETUP_SCRIPT%
  echo.
  echo Make sure this file is still in the Feather Auto repo root.
  pause >nul
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SETUP_SCRIPT%" %*
if errorlevel 1 (
  echo.
  echo Setup failed. Press any key to close.
  pause >nul
  exit /b 1
)

echo.
echo Setup finished. Press any key to close.
pause >nul
