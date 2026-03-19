#!/usr/bin/env nu
# compact-wsl.nu — 压缩 WSL2 VHDX（需要以管理员身份运行）

const SCRIPT_PATH = (path self)

def get_distros [] {
    let lxss_path = 'HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss'
    let subkeys = (^reg query $lxss_path
        | lines
        | where { |l| $l =~ '\\{' }
        | str trim)

    $subkeys | each { |key|
        let raw = (^reg query $key | lines | str trim | where { |l| $l != "" and not ($l | str starts-with "HKEY_") })
        let name = ($raw
            | where { |l| $l =~ '(?i)^DistributionName\s' }
            | first
            | parse --regex '^\S+\s+\S+\s+(?P<val>.+)$'
            | get val.0)
        let base = ($raw
            | where { |l| $l =~ '(?i)^BasePath\s' }
            | first
            | parse --regex '^\S+\s+\S+\s+(?P<val>.+)$'
            | get val.0
            | str replace --regex '^\\\\\?\\' '')
        let vhdx = ($base | path join "ext4.vhdx")
        if ($vhdx | path exists) {
            let size_bytes = (ls $vhdx | get 0.size | into int)
            { name: $name, vhdx: $vhdx, size: $size_bytes }
        }
    } | where { |r| $r != null }
}

def main [] {
    # 检查管理员权限
    let is_admin = (do { ^net session } | complete).exit_code == 0
    if not $is_admin {
        let nu_exe = $nu.current-exe
        let args = $"'\"($SCRIPT_PATH)\"'"
        ^powershell -NoProfile -Command $"Start-Process '($nu_exe)' -ArgumentList ($args) -Verb RunAs"
        return
    }

    try {
        run
    } catch { |e|
        print $"\n(ansi red)发生错误: ($e.msg)(ansi reset)"
    }
    input "按回车退出..."
}

def run [] {
    # 检查注册表中是否有 WSL 安装信息
    let lxss_path = 'HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss'
    let subkeys = (do { ^reg query $lxss_path } | complete)
    if $subkeys.exit_code != 0 {
        print $"(ansi red)未找到 WSL 安装信息(ansi reset)"
        return
    }

    loop {
        let distros = (get_distros)
        if ($distros | is-empty) {
            print $"(ansi red)未找到任何 WSL2 发行版 (ext4.vhdx)(ansi reset)"
            input "按回车退出..."
            return
        }

        # 显示列表
        print $"\n(ansi cyan)可用的 WSL2 发行版:(ansi reset)"
        $distros | enumerate | each { |it|
            let size_gb = ($it.item.size / 1_073_741_824 | math round --precision 2)
            print $"  [(($it.index + 1))] ($it.item.name) — ($size_gb) GB"
        }
        print "  [0] 全部压缩"
        print "  [q] 退出"
        print ""

        let choice = (input "请选择: ")
        if $choice in ["q" "Q"] { break }
        let idx = try { $choice | into int } catch { -1 }
        if $idx < 0 or $idx > ($distros | length) {
            print $"(ansi red)无效选择，请重新输入(ansi reset)"
            continue
        }

        let selected = if $idx == 0 { $distros } else { [($distros | get ($idx - 1))] }

        # 询问是否零填充
        print $"(ansi dark_gray)零填充空闲空间可以大幅提升压缩效果，但会额外写入一轮数据(ansi reset)"
        let zero_fill = (input "是否零填充? (y/N): ")

        # 对选中的发行版执行清理
        for distro in $selected {
            print $"\n(ansi cyan)[($distro.name)] 正在查看磁盘使用情况...(ansi reset)"
            ^wsl -d $distro.name -u root -- df -h /

            if $zero_fill in ["y" "Y"] {
                print $"(ansi yellow)[($distro.name)] 正在零填充空闲空间 \(可能需要几分钟\)...(ansi reset)"
                ^wsl -d $distro.name -u root -- bash -c "dd if=/dev/zero of=/tmp/.zero_fill bs=1M 2>/dev/null; rm -f /tmp/.zero_fill"
            }

            print $"(ansi yellow)[($distro.name)] 正在执行 fstrim...(ansi reset)"
            ^wsl -d $distro.name -u root fstrim /
        }

        # 关闭 WSL
        print $"\n(ansi yellow)正在关闭 WSL...(ansi reset)"
        ^wsl --shutdown

        # 等待所有 VHDX 文件解锁
        print $"(ansi yellow)等待 VHDX 文件释放...(ansi reset)"
        let timeout = 120
        mut elapsed = 0
        mut all_free = false
        while $elapsed < $timeout {
            $all_free = true
            for d in $selected {
                let ps_cmd = '[IO.File]::Open("' + $d.vhdx + '","Open","ReadWrite","None").Close()'
                let check = (do { ^powershell -NoProfile -Command $ps_cmd } | complete)
                if $check.exit_code != 0 {
                    $all_free = false
                    break
                }
            }
            if $all_free { break }
            sleep 3sec
            $elapsed = $elapsed + 3
            print $"(ansi dark_gray)  已等待 ($elapsed)s...(ansi reset)"
        }
        if not $all_free {
            print $"(ansi red)错误: VHDX 文件在 ($timeout)s 后仍被占用，请检查 Docker Desktop 等程序是否在运行(ansi reset)"
            continue
        }
        print $"(ansi green)VHDX 文件已就绪(ansi reset)"

        # 压缩每个 VHDX
        for distro in $selected {
            let size_before_gb = ($distro.size / 1_073_741_824 | math round --precision 2)
            print $"\n(ansi cyan)[($distro.name)] 压缩前: ($size_before_gb) GB(ansi reset)"

            let has_optimize = (do { ^powershell -NoProfile -Command "Get-Command Optimize-VHD -ErrorAction SilentlyContinue" } | complete).exit_code == 0
            if $has_optimize {
                ^powershell -NoProfile -Command $'Optimize-VHD -Path "($distro.vhdx)" -Mode Full'
            } else {
                print $"(ansi dark_gray)Optimize-VHD 不可用，使用 diskpart...(ansi reset)"
                $'select vdisk file="($distro.vhdx)"\nattach vdisk readonly\ncompact vdisk\ndetach vdisk\nexit\n' | ^diskpart
            }

            let size_after = (ls $distro.vhdx | get 0.size | into int)
            let size_after_gb = ($size_after / 1_073_741_824 | math round --precision 2)
            let saved = ($size_before_gb - $size_after_gb | math round --precision 2)
            print $"(ansi green)[($distro.name)] 压缩后: ($size_after_gb) GB \(节省 ($saved) GB\)(ansi reset)"
        }

        print $"\n(ansi green)本轮完成!(ansi reset)"
    }
}
