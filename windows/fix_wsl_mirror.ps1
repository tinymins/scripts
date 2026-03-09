# ==========================================================
# WSL Mirror Mode Auto-Recovery Script
# ==========================================================
#
# 问题现象:
#   WSL2 使用 networkingMode=mirrored 时，重启后随机出现:
#   Error code: CreateInstance/CreateVm/ConfigureNetworking/0x8007054f
#   WSL 回退到 networkingMode None，VM 内只有 lo 接口，无网络。
#
# 根因:
#   WSL2 mirror 模式存在已知的竞态条件 (race condition) bug。
#   启动时 HNS 服务创建 FSE Switch → 绑定物理网卡 → VmSwitch 初始化端口
#   → WSL VM 配置网络，这些步骤之间缺乏可靠同步机制。
#   wslservice.exe 在前序步骤未就绪时就尝试配置网络，导致失败。
#   此外 swap.vhdx 文件锁定 (STATUS_SHARING_VIOLATION) 也可能触发该错误。
#
#   GitHub 有多个未关闭 issue 确认此问题:
#     https://github.com/microsoft/WSL/issues/13454 (81+ comments)
#     https://github.com/microsoft/WSL/issues/12351 (61+ comments)
#   Microsoft 截至 2026-03 尚未提供官方修复。
#
# 解决方式:
#   反复 shutdown → 清理 swap.vhdx → 刷 ARP → 定期重启 HNS/WinNAT
#   → 启动 WSL → 检测 eth 接口，直到竞态条件恰好通过。
#   实测通常 1-10 次内可成功。
#
# 用法:
#   以管理员身份运行:
#   powershell -ExecutionPolicy Bypass -File fix_wsl_mirror.ps1
#
# ==========================================================

# Auto-elevate to Administrator if not already elevated
if (-not ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

$maxAttempts = 20
$attempt = 0
$success = $false

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " WSL Mirror Mode Auto-Recovery" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

while (-not $success -and $attempt -lt $maxAttempts) {
    $attempt++
    Write-Host "`n--- Attempt $attempt / $maxAttempts ---" -ForegroundColor Yellow

    # 1. Shutdown WSL
    wsl --shutdown 2>$null
    Start-Sleep -Seconds 2

    # 2. Clean stale swap.vhdx files
    $swaps = Get-ChildItem "$env:TEMP" -Recurse -Filter "swap.vhdx" -ErrorAction SilentlyContinue
    if ($swaps) {
        foreach ($s in $swaps) {
            Remove-Item $s.Directory.FullName -Recurse -Force -ErrorAction SilentlyContinue
        }
    }

    # 3. Flush ARP cache
    netsh interface ip delete arpcache 2>$null | Out-Null

    # 4. Every 3rd attempt: restart HNS
    if ($attempt % 3 -eq 0) {
        Write-Host "  Restarting HNS..." -ForegroundColor Gray
        Restart-Service hns -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }

    # 5. Every 5th attempt: also restart winnat
    if ($attempt % 5 -eq 0) {
        Write-Host "  Restarting WinNAT..." -ForegroundColor Gray
        net stop winnat 2>$null | Out-Null
        net start winnat 2>$null | Out-Null
        Start-Sleep -Seconds 2
    }

    # 6. Start WSL and check
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "wsl"
    $psi.Arguments = '-d Ubuntu-22.04-WSL2 -- ip addr show'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::Unicode

    $proc = [System.Diagnostics.Process]::new()
    $proc.StartInfo = $psi
    $proc.Start() | Out-Null
    $stdout = $proc.StandardOutput.ReadToEnd()
    $stderr = $proc.StandardError.ReadToEnd()
    $proc.WaitForExit()

    if ($stdout -match 'eth\d+') {
        Write-Host "  SUCCESS on attempt $attempt!" -ForegroundColor Green
        Write-Host $stdout
        $success = $true
    } else {
        Write-Host "  Failed (0x8007054f)" -ForegroundColor Red
        Start-Sleep -Seconds 3
    }
}

Write-Host "`n========================================" -ForegroundColor Cyan
if ($success) {
    Write-Host " WSL mirror mode is working!" -ForegroundColor Green
} else {
    Write-Host " FAILED after $maxAttempts attempts." -ForegroundColor Red
    Write-Host " Try: netsh winsock reset && netsh int ip reset" -ForegroundColor Yellow
    Write-Host " Then reboot and run this script again." -ForegroundColor Yellow
}
Write-Host "========================================" -ForegroundColor Cyan
