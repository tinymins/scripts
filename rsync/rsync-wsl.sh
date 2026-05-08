#!/bin/bash
# rsync-wsl.sh — 从任意一个 WSL 发行版内同步所有 WSL 的 home 目录到 NAS
# 用法: 在任意 WSL 中运行，或从 Windows: wsl -d <任一发行版> -- bash /mnt/d/Apps/Scripts/rsync/rsync-wsl.sh
# 也可以双击 rsync-wsl.bat 自动选择一个发行版来执行

DESTINATION_BASE="pve-truenas-emil:/mnt/storage/data/archives/RSYNC/ZYM-PC/WslHome"
IGNORE_LIST="/mnt/d/Apps/Scripts/rsync/rsync-wsl.ignore.txt"

# 要跳过的发行版
SKIP_DISTROS="docker-desktop docker-desktop-data"

# 获取所有 WSL 发行版
DISTROS=$(wsl.exe -l -q 2>/dev/null | tr -d '\r\0' | grep -v '^$')

if [ -z "$DISTROS" ]; then
    echo "未找到任何 WSL 发行版"
    exit 1
fi

echo "========================================"
echo " WSL Home 目录同步"
echo "========================================"

FAILED=0
SYNCED=0

for DISTRO in $DISTROS; do
    # 检查是否在跳过列表中
    if echo "$SKIP_DISTROS" | grep -qw "$DISTRO"; then
        echo ""
        echo "[$DISTRO] 跳过 (在排除列表中)"
        continue
    fi

    echo ""
    echo "========================================"
    echo "[$DISTRO] 开始同步..."
    echo "========================================"

    # 通过 /mnt/wsl 或 wsl 命令获取对方的 home 路径
    # 这里直接在目标发行版里执行 rsync
    HOME_DIR=$(wsl.exe -d "$DISTRO" -- sh -c 'echo $HOME' 2>/dev/null | tr -d '\r')
    if [ -z "$HOME_DIR" ]; then
        echo "[$DISTRO] 警告: 无法获取 HOME 目录，跳过"
        FAILED=$((FAILED + 1))
        continue
    fi

    # 检查该发行版是否安装了 rsync
    if ! wsl.exe -d "$DISTRO" -- which rsync > /dev/null 2>&1; then
        echo "[$DISTRO] 警告: rsync 未安装，跳过"
        FAILED=$((FAILED + 1))
        continue
    fi

    echo "[$DISTRO] 同步 $HOME_DIR/ -> $DESTINATION_BASE/$DISTRO/"

    wsl.exe -d "$DISTRO" -- rsync -avz --delete --delete-excluded --progress \
        --exclude-from="$IGNORE_LIST" \
        "$HOME_DIR/" "$DESTINATION_BASE/$DISTRO/"

    if [ $? -eq 0 ]; then
        echo "[$DISTRO] 同步完成"
        SYNCED=$((SYNCED + 1))
    else
        echo "[$DISTRO] 同步失败"
        FAILED=$((FAILED + 1))
    fi
done

echo ""
echo "========================================"
echo " 全部完成: 成功 $SYNCED, 失败 $FAILED"
echo "========================================"
