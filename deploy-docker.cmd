@echo off
chcp 65001 >nul
setlocal EnableExtensions

rem 产品需求：此入口必须默认启用局域网访问；仅在显式传入 -LocalOnly 时改为仅本机访问。
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy-docker.ps1" -Lan %*
set "exit_code=%ERRORLEVEL%"

echo.
if not "%exit_code%"=="0" (
    echo 部署失败，退出码为 %exit_code%。
)
pause
exit /b %exit_code%
