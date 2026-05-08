#!/usr/bin/env nu
# rsync-wsl.nu — 从 Windows 端通过 wsl 命令同步所有 WSL 发行版的 home 目录到 NAS
# 用法: nu rsync-wsl.nu [--dry-run]
# 替代原来的 rsync-wsl.bat + rsync-wsl.sh，不再有 WSL 内调 wsl.exe 的套娃

def main [--dry-run] {

let destination_base = "pve-truenas-emil:/mnt/storage/data/archives/RSYNC/ZYM-PC/WslHome"
# 根据脚本所在目录自动生成 ignore 文件的 WSL 路径
let script_dir = ($env.FILE_PWD | path join "rsync-wsl.ignore.txt")
let ignore_list = (^wsl wslpath -u ($script_dir | str replace --all '\' '/') | str trim)
let skip_distros = ["docker-desktop", "docker-desktop-data"]

# 获取所有 WSL 发行版 (wsl -l -q 在 Windows 上输出 UTF-16LE)
let distros = (^wsl -l -q
    | decode utf-16le
    | lines
    | each { |it| $it | str trim }
    | where { |it| $it != "" })

if ($distros | is-empty) {
    print "未找到任何 WSL 发行版"
    exit 1
}

print "========================================"
print " WSL Home 目录同步"
print "========================================"

mut failed = 0
mut synced = 0

for distro in $distros {
    if $distro in $skip_distros {
        print $"[($distro)] 跳过 - 在排除列表中"
        continue
    }

    print ""
    print "========================================"
    print $"[($distro)] 开始同步..."
    print "========================================"

    # 获取该发行版的 HOME 目录
    let home_result = (^wsl -d $distro -- sh -c 'echo $HOME' | complete)
    let home_dir = ($home_result.stdout | str trim)

    if ($home_dir | is-empty) {
        print $"[($distro)] 警告: 无法获取 HOME 目录，跳过"
        $failed += 1
        continue
    }

    # 检查该发行版是否安装了 rsync
    let rsync_check = (^wsl -d $distro -- which rsync | complete)
    if $rsync_check.exit_code != 0 {
        print $"[($distro)] 警告: rsync 未安装，跳过"
        $failed += 1
        continue
    }

    let src = $"($home_dir)/"
    let dst = $"($destination_base)/($distro)/"
    print $"[($distro)] 同步 ($src) -> ($dst)"

    if $dry_run {
        print $"[($distro)] [dry-run] wsl -d ($distro) -- rsync -avz --delete --delete-excluded --progress --exclude-from ($ignore_list) ($src) ($dst)"
        $synced += 1
        continue
    }

    # 执行 rsync，直接运行以实时显示进度（不捕获输出）
    ^wsl -d $distro -- rsync -avz --delete --delete-excluded --progress --exclude-from $ignore_list $src $dst

    if $env.LAST_EXIT_CODE == 0 {
        print $"[($distro)] 同步完成"
        $synced += 1
    } else {
        print $"[($distro)] 同步失败"
        $failed += 1
    }
}

print ""
print "========================================"
print $" 全部完成: 成功 ($synced), 失败 ($failed)"
print "========================================"

} # end main
