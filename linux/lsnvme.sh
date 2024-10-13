#!/bin/bash

first_entry=true

# 遍历 /sys/class/nvme 下的所有 NVMe 设备
for nvme in /sys/class/nvme/nvme*; do
    # 如果不是第一个条目，输出双换行
    if [ "$first_entry" = false ]; then
        echo
        echo
    fi
    
    # 标记处理过第一个设备
    first_entry=false

    # 获取设备的序列号并删除多余的空格
    serial=$(cat "$nvme/serial" | tr -d '\n' | xargs)

    # 获取设备的型号
    model=$(cat "$nvme/model" | tr -d '\n' | xargs)

    # 获取设备的符号链接目标
    symlink_target=$(readlink "$nvme")

    # 提取完整的PCI地址路径，不包括 /devices 和 /nvme 部分
    pci_address=$(echo "$symlink_target" | sed -n 's|.*\(pci0000:.*\)|\1|p')

    # 输出设备信息
    echo "设备型号: $model"
    echo "设备序列号: $serial"
    echo "PCI 地址路径: $pci_address"
done

exit 0
