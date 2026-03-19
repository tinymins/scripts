#!/usr/bin/env nu
# ==========================================================
# WSL Mirror Mode Auto-Recovery Script (Nushell)
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
#   nu fix_wsl_mirror.nu
#
# ==========================================================

const SCRIPT_PATH = (path self)

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

def get_distros [] {
    ^wsl -l -q
        | decode utf-16le
        | lines
        | each { |it| $it | str trim }
        | where { |it| $it != "" }
}

def run [] {
    print $"(ansi cyan)========================================(ansi reset)"
    print $"(ansi cyan) WSL Mirror Mode Auto-Recovery(ansi reset)"
    print $"(ansi cyan)========================================(ansi reset)"

    let distros = (get_distros)
    if ($distros | is-empty) {
        print $"(ansi red)未找到任何 WSL 发行版(ansi reset)"
        return
    }

    # 显示列表让用户选择
    print $"\n(ansi cyan)可用的 WSL 发行版:(ansi reset)"
    $distros | enumerate | each { |it|
        print $"  [(($it.index + 1))] ($it.item)"
    }
    print ""

    let choice = (input "请选择发行版: ")
    let idx = try { ($choice | into int) - 1 } catch { -1 }
    if $idx < 0 or $idx >= ($distros | length) {
        print $"(ansi red)无效选择(ansi reset)"
        return
    }

    let distro = ($distros | get $idx)
    print $"\n已选择: (ansi green)($distro)(ansi reset)"

    mut attempt = 0

    loop {
        $attempt += 1
        print $"\n(ansi yellow)--- Attempt ($attempt) ---(ansi reset)"

        # 1. Shutdown WSL
        do { ^wsl --shutdown } | complete
        sleep 2sec

        # 2. Clean stale swap.vhdx files
        let pattern = ($env.TEMP | str replace --all '\' '/') + "/**/swap.vhdx"
        let swaps = try { glob $pattern } catch { [] }
        for s in $swaps {
            let parent = ($s | path dirname)
            do -i { rm -rf $parent }
        }

        # 3. Flush ARP cache
        do { ^netsh interface ip delete arpcache } | complete

        # 4. Every 3rd attempt: restart HNS
        if $attempt mod 3 == 0 {
            print $"  (ansi dark_gray)Restarting HNS...(ansi reset)"
            do { ^powershell -NoProfile -Command "Restart-Service hns -Force" } | complete
            sleep 3sec
        }

        # 5. Every 5th attempt: also restart winnat
        if $attempt mod 5 == 0 {
            print $"  (ansi dark_gray)Restarting WinNAT...(ansi reset)"
            do { ^net stop winnat } | complete
            do { ^net start winnat } | complete
            sleep 2sec
        }

        # 6. Start WSL and check for eth interface
        let result = (do { ^wsl -d $distro -- ip addr show } | complete)

        if ($result.stdout =~ 'eth\d+') {
            print $"  (ansi green)SUCCESS on attempt ($attempt)!(ansi reset)"
            print $result.stdout
            break
        } else {
            print $"  (ansi red)Failed \(0x8007054f\)(ansi reset)"
            sleep 3sec
        }
    }

    print $"\n(ansi cyan)========================================(ansi reset)"
    print $"(ansi green) WSL mirror mode is working! \(attempt ($attempt)\)(ansi reset)"
    print $"(ansi cyan)========================================(ansi reset)"
}
