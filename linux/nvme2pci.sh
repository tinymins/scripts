#!/bin/bash

# 提示用户输入序列号
read -p "请输入NVMe设备的序列号: " target_serial < /dev/tty

# 遍历 /sys/class/nvme 下的所有 NVMe 设备
for nvme in /sys/class/nvme/nvme*; do
    # 获取设备的序列号并删除多余的空格
    serial=$(cat "$nvme/serial" | tr -d '\n' | xargs)

    # 如果序列号匹配
    if [ "$serial" == "$target_serial" ]; then
        # 获取设备的符号链接目标，也就是PCI地址相对路径
        symlink_target=$(readlink "$nvme")

        # 计算PCI地址
        pci_address=$(echo "$symlink_target" | awk -F'/' '{for(i=1;i<=NF;i++) if($i ~ /^0000:/) print $i}')

        echo "找到的 PCI 地址: $pci_address"
        exit 0
    fi
done

echo "没有找到匹配的设备，序列号: $target_serial"
exit 1
