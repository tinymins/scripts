@echo off
:: compact-wsl.bat — 双击即可运行，自动提权并调用 compact-wsl.ps1

:: 检查管理员权限，没有则自动提权
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process '%~f0' -Verb RunAs"
    exit /b
)

:: 以 Bypass 策略运行同目录下的 ps1
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0compact-wsl.ps1"
pause
