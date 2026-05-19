@echo off
setlocal
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0codex-provider.ps1" %*
exit /b %ERRORLEVEL%
