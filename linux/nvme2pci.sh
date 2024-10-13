#!/bin/bash

# 提示用户输入序列号
read -p "请输入NVMe设备的序列号: " target_serial < /dev/tty

# 遍历 /sys/class/nvme 下的所有 NVMe 设备
for nvme in /sys/class/nvme/nvme*; do
    # 获取设备的序列号并删除多余的空格
    serial=$(cat "$nvme/serial" | tr -d '\n' | xargs)

    # 如果序列号匹配
    if [ "$serial" == "$target_serial" ]; then
        # 获取设备的型号
        model=$(cat "$nvme/model" | tr -d '\n' | xargs)

        # 获取设备的符号链接目标
        symlink_target=$(readlink "$nvme")

        # 提取完整的PCI地址路径，不包括 /devices 和 /nvme 部分
        pci_address=$(echo "$symlink_target" | sed -n 's|.*\(pci0000:.*\)|\1|p')

        echo "找到的设备型号: $model"
        echo "找到的 PCI 地址路径: $pci_address"
        exit 0
    fi
done

echo "没有找到匹配的设备，序列号: $target_serial"
exit 1
