@echo off
title Windows Defender 卸载工具
color 0A
echo ========================================
echo    Windows Defender 卸载工具
echo    仅适用于 Windows Server
echo ========================================
echo.

REM 检查管理员权限
net session >nul 2>&1
if %errorLevel% neq 0 (
    color 0C
    echo [错误] 需要管理员权限！
    echo.
    echo 请右键点击此文件，选择"以管理员身份运行"
    pause
    exit /b 1
)

echo [信息] 正在检查 Windows Defender 状态...
powershell.exe -ExecutionPolicy Bypass -Command "Get-WindowsFeature -Name Windows-Defender | Select-Object Name, InstallState"
echo.

echo [警告] 即将卸载 Windows Defender
echo 注意事项：
echo 1. 此操作仅适用于 Windows Server 2016/2019/2022
echo 2. 卸载后需要重启服务器
echo 3. 确保已安装其他防病毒软件
echo.

choice /C YN /M "确定要继续吗？"
if %errorlevel%==2 (
    echo 操作已取消
    pause
    exit /b 0
)

echo.
echo [执行] 正在卸载 Windows Defender...
powershell.exe -ExecutionPolicy Bypass -Command "Uninstall-WindowsFeature -Name Windows-Defender -Restart:$false"

if %errorlevel%==0 (
    color 0A
    echo.
    echo [成功] Windows Defender 已成功卸载！
    echo.
    echo [重要] 必须重启服务器才能完成卸载过程
    echo.
    choice /C YN /T 30 /D N /M "是否立即重启？（30秒后自动选择否）"
    if %errorlevel%==1 (
        echo 正在重启服务器...
        shutdown /r /t 10 /c "Windows Defender 卸载完成，正在重启..."
    ) else (
        echo 请记得手动重启服务器以完成卸载。
    )
) else (
    color 0C
    echo.
    echo [错误] 卸载失败，请检查错误信息
)

echo.
pause