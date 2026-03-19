# compact-wsl.ps1 — 压缩 WSL2 VHDX

# 检查管理员权限
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "请以管理员身份运行此脚本，或双击 compact-wsl.bat 自动提权" -ForegroundColor Red
    pause
    exit 1
}

# 从注册表获取 WSL 发行版列表
$lxssPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss"
if (-not (Test-Path $lxssPath)) {
    Write-Error "未找到 WSL 安装信息"
    exit 1
}

while ($true) {
    # 刷新发行版列表和大小
    $distros = Get-ChildItem $lxssPath | ForEach-Object {
        $name = (Get-ItemProperty $_.PSPath).DistributionName
        $basePath = (Get-ItemProperty $_.PSPath).BasePath
        if ($name -and $basePath) {
            $basePath = $basePath -replace '^\\\\\?\\', ''
            $vhdx = Join-Path $basePath "ext4.vhdx"
            if (Test-Path $vhdx) {
                [PSCustomObject]@{ Name = $name; VhdxPath = $vhdx }
            }
        }
    } | Where-Object { $_ -ne $null }

    if (-not $distros -or $distros.Count -eq 0) {
        Write-Host "未找到任何 WSL2 发行版 (ext4.vhdx)" -ForegroundColor Red
        pause
        exit 1
    }

    # 显示列表让用户选择
    Write-Host "`n可用的 WSL2 发行版:" -ForegroundColor Cyan
    for ($i = 0; $i -lt $distros.Count; $i++) {
        $size = [math]::Round((Get-Item $distros[$i].VhdxPath).Length / 1GB, 2)
        Write-Host "  [$($i + 1)] $($distros[$i].Name) — $size GB"
    }
    Write-Host "  [0] 全部压缩"
    Write-Host "  [q] 退出"
    Write-Host ""

    $choice = Read-Host "请选择"
    if ($choice -eq 'q' -or $choice -eq 'Q') { break }
    if ($choice -notmatch '^\d+$' -or [int]$choice -lt 0 -or [int]$choice -gt $distros.Count) {
        Write-Host "无效选择，请重新输入" -ForegroundColor Red
        continue
    }

    if ([int]$choice -eq 0) {
        $selected = $distros
    } else {
        $selected = @($distros[[int]$choice - 1])
    }

    # 询问是否零填充
    Write-Host "零填充空闲空间可以大幅提升压缩效果，但会额外写入一轮数据" -ForegroundColor DarkGray
    $zeroFill = Read-Host "是否零填充? (y/N)"

    # 对选中的发行版执行清理
    foreach ($distro in $selected) {
        Write-Host "`n[$($distro.Name)] 正在查看磁盘使用情况..." -ForegroundColor Cyan
        wsl -d $distro.Name -u root -- df -h /

        if ($zeroFill -eq 'y' -or $zeroFill -eq 'Y') {
            Write-Host "[$($distro.Name)] 正在零填充空闲空间 (可能需要几分钟)..." -ForegroundColor Yellow
            wsl -d $distro.Name -u root -- bash -c "dd if=/dev/zero of=/tmp/.zero_fill bs=1M 2>/dev/null; rm -f /tmp/.zero_fill"
        }

        Write-Host "[$($distro.Name)] 正在执行 fstrim..." -ForegroundColor Yellow
        wsl -d $distro.Name -u root fstrim /
    }

    # 关闭 WSL
    Write-Host "`n正在关闭 WSL..." -ForegroundColor Yellow
    wsl --shutdown

    # 等待所有 VHDX 文件解锁
    Write-Host "等待 VHDX 文件释放..." -ForegroundColor Yellow
    $timeout = 120
    $elapsed = 0
    $allFree = $false
    while ($elapsed -lt $timeout) {
        $allFree = $true
        foreach ($d in $selected) {
            try {
                $fs = [System.IO.File]::Open($d.VhdxPath, 'Open', 'ReadWrite', 'None')
                $fs.Close()
            } catch {
                $allFree = $false
                break
            }
        }
        if ($allFree) { break }
        Start-Sleep -Seconds 3
        $elapsed += 3
        Write-Host "  已等待 ${elapsed}s..." -ForegroundColor DarkGray
    }
    if (-not $allFree) {
        Write-Host "错误: VHDX 文件在 ${timeout}s 后仍被占用，请检查 Docker Desktop 等程序是否在运行" -ForegroundColor Red
        continue
    }
    Write-Host "VHDX 文件已就绪" -ForegroundColor Green

    # 压缩每个 VHDX
    foreach ($distro in $selected) {
        $vhdx = $distro.VhdxPath
        $fileItem = Get-Item $vhdx
        $sizeBefore = [math]::Round($fileItem.Length / 1GB, 2)

        # 检查 NTFS 压缩
        if ($fileItem.Attributes -band [System.IO.FileAttributes]::Compressed) {
            $sizeOnDisk = [math]::Round((New-Object -ComObject Scripting.FileSystemObject).GetFile($vhdx).Size / 1GB, 2)
            Write-Host "`n[$($distro.Name)] 已启用 NTFS 压缩 (逻辑 $sizeBefore GB, 实占 $sizeOnDisk GB), 跳过 Optimize-VHD" -ForegroundColor DarkYellow
            continue
        }

        Write-Host "`n[$($distro.Name)] 压缩前: $sizeBefore GB" -ForegroundColor Cyan

        if (Get-Command Optimize-VHD -ErrorAction SilentlyContinue) {
            Optimize-VHD -Path $vhdx -Mode Full
        } else {
            Write-Host "Optimize-VHD 不可用，使用 diskpart..." -ForegroundColor DarkYellow
            @"
select vdisk file="$vhdx"
attach vdisk readonly
compact vdisk
detach vdisk
exit
"@ | diskpart
        }

        $sizeAfter = [math]::Round((Get-Item $vhdx).Length / 1GB, 2)
        $saved = [math]::Round($sizeBefore - $sizeAfter, 2)
        Write-Host "[$($distro.Name)] 压缩后: $sizeAfter GB (节省 $saved GB)" -ForegroundColor Green
    }

    Write-Host "`n本轮完成!" -ForegroundColor Green
}
