@echo off
chcp 65001 >nul
setlocal

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy-docker.ps1" %*
set "exit_code=%ERRORLEVEL%"

echo.
if not "%exit_code%"=="0" (
    echo 部署失败，退出码为 %exit_code%。
)
pause
exit /b %exit_code%
