@echo off
setlocal
set "REPO_ROOT=%~dp0.."
call "%REPO_ROOT%\setup.cmd" %*
exit /b %errorlevel%
