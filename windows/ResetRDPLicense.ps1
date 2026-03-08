param(
    [switch]$Silent
)

# 要求管理员权限
if (-NOT ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")) {
    if ($Silent) {
        Write-Error "需要管理员权限！静默模式下无法自动提升，请以管理员身份运行。"
        exit 1
    }
    Write-Host "需要管理员权限！正在重新启动..." -ForegroundColor Red
    Start-Process PowerShell -Verb RunAs "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

Write-Host "===========================================" -ForegroundColor Cyan
Write-Host "    Windows Server RDP 宽限期重置工具" -ForegroundColor Cyan
Write-Host "===========================================" -ForegroundColor Cyan
Write-Host ""

# 停止服务
Write-Host "[1/4] 停止远程桌面服务..." -ForegroundColor Yellow
Stop-Service TermService -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

# 获取权限并删除
Write-Host "[2/4] 处理注册表权限..." -ForegroundColor Yellow

$regKey = "HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\RCM\GracePeriod"

function Remove-RegistryKeyWithPermission {
    param($Path)

    if (Test-Path $Path) {
        try {
            # 方法1：使用.NET获取权限
            $key = [Microsoft.Win32.Registry]::LocalMachine.OpenSubKey(
                "SYSTEM\CurrentControlSet\Control\Terminal Server\RCM\GracePeriod",
                [Microsoft.Win32.RegistryKeyPermissionCheck]::ReadWriteSubTree,
                [System.Security.AccessControl.RegistryRights]::FullControl
            )

            if ($key) {
                $acl = $key.GetAccessControl()
                $user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
                $rule = New-Object System.Security.AccessControl.RegistryAccessRule(
                    $user,
                    [System.Security.AccessControl.RegistryRights]::FullControl,
                    [System.Security.AccessControl.InheritanceFlags]::ContainerInherit,
                    [System.Security.AccessControl.PropagationFlags]::None,
                    [System.Security.AccessControl.AccessControlType]::Allow
                )
                $acl.SetAccessRule($rule)
                $acl.SetOwner([System.Security.Principal.NTAccount]$user)
                $key.SetAccessControl($acl)
                $key.Close()
            }

            # 删除键值
            Remove-Item $Path -Recurse -Force -ErrorAction Stop
            Write-Host "    [√] 注册表项已删除" -ForegroundColor Green
            return $true

        } catch {
            Write-Host "    [!] 方法1失败，尝试方法2..." -ForegroundColor Yellow

            # 方法2：使用PsExec以SYSTEM权限运行
            try {
                # 下载PsExec（如果需要）
                $psexecPath = "$env:TEMP\PsExec64.exe"
                if (-not (Test-Path $psexecPath)) {
                    Write-Host "    下载PsExec工具..." -ForegroundColor Gray
                    Invoke-WebRequest -Uri "https://live.sysinternals.com/PsExec64.exe" -OutFile $psexecPath -ErrorAction SilentlyContinue
                }

                if (Test-Path $psexecPath) {
                    & $psexecPath -accepteula -s -i powershell -Command "Remove-Item 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\RCM\GracePeriod' -Recurse -Force" 2>$null
                    Write-Host "    [√] 使用SYSTEM权限删除成功" -ForegroundColor Green
                    return $true
                }
            } catch {
                Write-Host "    [!] 方法2失败" -ForegroundColor Red
            }

            # 方法3：使用注册表命令
            Write-Host "    [!] 尝试方法3..." -ForegroundColor Yellow
            $regPath = "HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\Terminal Server\RCM\GracePeriod"

            # 获取所有权
            & takeown /f "C:\Windows\System32\config\SYSTEM" /a 2>$null
            & icacls "C:\Windows\System32\config\SYSTEM" /grant "Administrators:F" /q 2>$null

            # 删除注册表项
            & reg delete $regPath /f 2>$null

            if (-not (Test-Path $Path)) {
                Write-Host "    [√] 注册表项已删除" -ForegroundColor Green
                return $true
            }
        }
    } else {
        Write-Host "    [!] 注册表项不存在" -ForegroundColor Yellow
        return $true
    }

    return $false
}

# 执行删除
$result = Remove-RegistryKeyWithPermission -Path $regKey

if (-not $result) {
    Write-Host ""
    Write-Host "    [!] 自动删除失败，需要手动操作：" -ForegroundColor Red
    Write-Host "    1. 打开注册表编辑器 (regedit)" -ForegroundColor White
    Write-Host "    2. 导航到：HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\Terminal Server\RCM\GracePeriod" -ForegroundColor White
    Write-Host "    3. 右键点击GracePeriod，选择'权限'" -ForegroundColor White
    Write-Host "    4. 点击'高级'，更改所有者为当前用户" -ForegroundColor White
    Write-Host "    5. 勾选'替换子容器和对象的所有者'" -ForegroundColor White
    Write-Host "    6. 给当前用户完全控制权限" -ForegroundColor White
    Write-Host "    7. 删除GracePeriod下的所有内容" -ForegroundColor White
    Write-Host ""
    if (-not $Silent) {
        Write-Host "    按任意键打开注册表编辑器..." -ForegroundColor Yellow
        $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
        Start-Process regedit
    }
}

# 设置授权模式
Write-Host "[3/4] 设置授权模式..." -ForegroundColor Yellow
Set-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server\RCM\Licensing Core" -Name "LicensingMode" -Value 4 -Type DWord -Force
Write-Host "    [√] 已设置为'每用户'模式" -ForegroundColor Green

# 重启服务
Write-Host "[4/4] 重启远程桌面服务..." -ForegroundColor Yellow
Start-Service TermService
Write-Host "    [√] 服务已重启" -ForegroundColor Green

Write-Host ""
Write-Host "===========================================" -ForegroundColor Green
Write-Host "              操作完成！" -ForegroundColor Green
Write-Host "===========================================" -ForegroundColor Green
Write-Host ""
Write-Host "建议重启计算机以确保生效" -ForegroundColor Yellow
Write-Host ""
if (-not $Silent) {
    Write-Host "按任意键退出..."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}
