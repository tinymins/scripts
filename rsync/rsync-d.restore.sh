#!/bin/bash

# 定义源目录和目标目录
SOURCE_DIR="pve-truenas-emil:/mnt/storage/data/archives/RSYNC/ZYM-PC/D/"
DESTINATION_DIR="/mnt/d/"

# 定义忽略文件列表路径
IGNORE_LIST="/mnt/d/Apps/RSync/rsync-d.ignore.txt"

# 执行 rsync 命令
rsync -avz --delete --delete-excluded --progress --exclude-from="$IGNORE_LIST" "$SOURCE_DIR" "$DESTINATION_DIR" --dry-run
