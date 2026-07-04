@echo off
setlocal

set "REPO_ROOT=%~dp0"
set "START_SCRIPT=%REPO_ROOT%scripts\start-feather.ps1"

if not exist "%START_SCRIPT%" (
  echo Cannot find launcher script:
  echo %START_SCRIPT%
  echo.
  echo Make sure this file is still in the Feather Auto repo root.
  pause >nul
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%START_SCRIPT%" %*
if errorlevel 1 (
  echo.
  echo Start failed. Run setup.cmd first, then try again.
  echo Press any key to close.
  pause >nul
  exit /b 1
)
