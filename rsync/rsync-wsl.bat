@echo off
chcp 65001 >nul
:: rsync-wsl.bat — 双击即可同步所有 WSL 发行版的 home 目录到 NAS
:: 自动选择第一个可用的 WSL 发行版来执行 rsync-wsl.sh

wsl -- bash /mnt/d/Apps/Scripts/rsync/rsync-wsl.sh
pause
