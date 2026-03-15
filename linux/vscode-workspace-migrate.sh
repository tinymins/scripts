#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# vscode-workspace-migrate.sh
# VS Code Workspace Storage 迁移工具
# 当项目文件夹改名后，Copilot Chat 历史等 workspace 数据会丢失。
# 本脚本通过修改 workspace.json 中的 folder URI 实现无缝迁移。
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

# ── 颜色 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

# ── 检测 VS Code workspace storage 路径 ──
detect_storage_dir() {
    local candidates=()

    # WSL: Windows 端 AppData
    if grep -qi microsoft /proc/version 2>/dev/null; then
        local win_user
        # 尝试通过 cmd.exe 获取用户名
        win_user=$(cmd.exe /c "echo %USERNAME%" 2>/dev/null | tr -d '\r\n') || true
        if [[ -n "$win_user" ]]; then
            local win_path="/mnt/c/Users/${win_user}/AppData/Roaming/Code/User/workspaceStorage"
            [[ -d "$win_path" ]] && candidates+=("$win_path")
        fi
        # 备选：扫描 /mnt/c/Users
        if [[ ${#candidates[@]} -eq 0 ]]; then
            for d in /mnt/c/Users/*/AppData/Roaming/Code/User/workspaceStorage; do
                [[ -d "$d" ]] && candidates+=("$d")
            done
        fi
        # WSL server 端
        local wsl_path="$HOME/.vscode-server/data/User/workspaceStorage"
        [[ -d "$wsl_path" ]] && candidates+=("$wsl_path")
    fi

    # macOS
    local mac_path="$HOME/Library/Application Support/Code/User/workspaceStorage"
    [[ -d "$mac_path" ]] && candidates+=("$mac_path")

    # Linux native
    local linux_path="$HOME/.config/Code/User/workspaceStorage"
    [[ -d "$linux_path" ]] && candidates+=("$linux_path")

    # 去重输出
    printf '%s\n' "${candidates[@]}" | sort -u
}

# ── 扫描所有 workspace 记录 ──
scan_workspaces() {
    local storage_dir="$1"
    local idx=0

    WORKSPACE_HASHES=()
    WORKSPACE_FOLDERS=()
    WORKSPACE_SIZES=()
    WORKSPACE_STORAGE_DIRS=()
    WORKSPACE_MTIMES=()

    for ws_dir in "$storage_dir"/*/; do
        local ws_json="${ws_dir}workspace.json"
        [[ -f "$ws_json" ]] || continue

        local folder
        folder=$(grep -o '"folder"[[:space:]]*:[[:space:]]*"[^"]*"' "$ws_json" 2>/dev/null \
                 | head -1 \
                 | sed 's/"folder"[[:space:]]*:[[:space:]]*"//;s/"$//') || continue
        [[ -z "$folder" ]] && continue

        local hash
        hash=$(basename "$ws_dir")

        local size
        size=$(du -sh "$ws_dir" 2>/dev/null | cut -f1) || size="?"

        local mtime
        mtime=$(stat -c '%Y' "$ws_json" 2>/dev/null || stat -f '%m' "$ws_json" 2>/dev/null) || mtime=0
        local mtime_human
        mtime_human=$(date -d "@$mtime" '+%Y-%m-%d %H:%M' 2>/dev/null || date -r "$mtime" '+%Y-%m-%d %H:%M' 2>/dev/null) || mtime_human="unknown"

        WORKSPACE_HASHES+=("$hash")
        WORKSPACE_FOLDERS+=("$folder")
        WORKSPACE_SIZES+=("$size")
        WORKSPACE_STORAGE_DIRS+=("$storage_dir")
        WORKSPACE_MTIMES+=("$mtime_human|$mtime")
        ((idx++)) || true
    done
}

# ── 对 folder URI 做人类可读化 ──
humanize_uri() {
    local uri="$1"
    # 解码常见 percent-encoding
    uri=$(echo "$uri" | sed 's/%2B/+/g;s/%2b/+/g;s/%3A/:/g;s/%3a/:/g;s/%20/ /g')
    # 去掉 scheme 前缀，保留关键路径
    if [[ "$uri" == vscode-remote://* ]]; then
        echo "${uri#vscode-remote://}"
    elif [[ "$uri" == file://* ]]; then
        echo "${uri#file://}"
    else
        echo "$uri"
    fi
}

# ── 通用交互选择器（支持数字 / 上下键） ──
# 参数: item_count render_callback
# render_callback(index, is_selected) 由调用方提供
# 返回选中的 index（通过 stdout）
_interactive_select() {
    local total=$1
    local renderer=$2  # 渲染函数名
    local lines_per_item=${3:-2}  # 每项占几行，默认 2
    local selected=0

    # 非交互终端回退
    if [[ ! -t 0 ]]; then
        echo -e "${YELLOW}非交互终端，请输入编号:${RESET}" >&2
        read -r selected
        echo "$selected"
        return
    fi

    tput civis 2>/dev/null || true
    trap 'tput cnorm 2>/dev/null; trap - RETURN' RETURN

    _render() {
        for ((i = 0; i < total; i++)); do
            tput el 2>/dev/null
            $renderer "$i" "$selected"
        done
    }

    _render

    while true; do
        IFS= read -rsn1 key
        case "$key" in
            [0-9])
                local num="$key"
                if ((num >= 1 && num <= total)); then
                    selected=$((num - 1))
                    for ((i = 0; i < total * lines_per_item; i++)); do tput cuu1 2>/dev/null; done
                    _render
                    echo ""
                    echo "$selected"
                    return
                fi
                ;;
            $'\x1b')
                read -rsn1 -t 0.1 k2 || true
                read -rsn1 -t 0.1 k3 || true
                case "$k2$k3" in
                    "[A") ((selected > 0)) && ((selected--)) ;;
                    "[B") ((selected < total - 1)) && ((selected++)) ;;
                esac
                for ((i = 0; i < total * lines_per_item; i++)); do tput cuu1 2>/dev/null; done
                _render
                ;;
            "")
                echo ""
                echo "$selected"
                return
                ;;
        esac
    done
}

# ── workspace 列表渲染器 ──
_render_workspace_item() {
    local i=$1 selected=$2
    local display_folder
    display_folder=$(humanize_uri "${FILTERED_FOLDERS[$i]}")
    local mtime_human="${FILTERED_MTIMES[$i]%%|*}"
    local size="${FILTERED_SIZES[$i]}"

    if [[ $i -eq $selected ]]; then
        echo -e "  ${CYAN}${BOLD}▸ [$((i + 1))]${RESET} ${BOLD}${display_folder}${RESET}"
        echo -e "        ${DIM}${size}  ·  ${mtime_human}${RESET}"
    else
        echo -e "    ${DIM}[$((i + 1))]${RESET} ${display_folder}"
        echo -e "        ${DIM}${size}  ·  ${mtime_human}${RESET}"
    fi
}

# ── 操作菜单渲染器 ──
ACTION_LABELS=()
ACTION_DESCS=()

_render_action_item() {
    local i=$1 selected=$2
    if [[ $i -eq $selected ]]; then
        echo -e "  ${CYAN}${BOLD}▸ [$((i + 1))]${RESET} ${BOLD}${ACTION_LABELS[$i]}${RESET}  ${DIM}${ACTION_DESCS[$i]}${RESET}"
    else
        echo -e "    ${DIM}[$((i + 1))]${RESET} ${ACTION_LABELS[$i]}  ${DIM}${ACTION_DESCS[$i]}${RESET}"
    fi
}

# ── 主逻辑 ──
main() {
    echo -e "${BOLD}━━━ VS Code Workspace Storage 迁移工具 ━━━${RESET}"
    echo ""

    # 1. 检测 storage 目录
    local storage_dirs
    mapfile -t storage_dirs < <(detect_storage_dir)

    if [[ ${#storage_dirs[@]} -eq 0 ]]; then
        echo -e "${RED}✗ 未找到 VS Code workspace storage 目录${RESET}"
        exit 1
    fi

    echo -e "${DIM}检测到 storage 目录:${RESET}"
    for sd in "${storage_dirs[@]}"; do
        echo -e "  ${DIM}$sd${RESET}"
    done
    echo ""

    # 保存为全局变量，供 do_migrate / do_delete 使用
    ALL_STORAGE_DIRS_UNIQUE=("${storage_dirs[@]}")

    # 2. 扫描所有 workspace
    ALL_HASHES=()
    ALL_FOLDERS=()
    ALL_SIZES=()
    ALL_STORAGE_DIRS=()
    ALL_MTIMES=()

    for sd in "${storage_dirs[@]}"; do
        scan_workspaces "$sd"
        ALL_HASHES+=("${WORKSPACE_HASHES[@]}")
        ALL_FOLDERS+=("${WORKSPACE_FOLDERS[@]}")
        ALL_SIZES+=("${WORKSPACE_SIZES[@]}")
        ALL_STORAGE_DIRS+=("${WORKSPACE_STORAGE_DIRS[@]}")
        ALL_MTIMES+=("${WORKSPACE_MTIMES[@]}")
    done

    local total=${#ALL_HASHES[@]}
    if [[ $total -eq 0 ]]; then
        echo -e "${RED}✗ 未找到任何 workspace 记录${RESET}"
        exit 1
    fi

    # 3. 可选：关键词过滤
    echo -e "${BOLD}找到 ${total} 个 workspace 记录${RESET}"
    echo -ne "${YELLOW}输入关键词过滤（留空显示全部）:${RESET} "
    read -r filter_keyword

    FILTERED_HASHES=()
    FILTERED_FOLDERS=()
    FILTERED_SIZES=()
    FILTERED_STORAGE_DIRS=()
    FILTERED_MTIMES=()

    for ((i = 0; i < total; i++)); do
        if [[ -z "$filter_keyword" ]] || echo "${ALL_FOLDERS[$i]}" | grep -qi "$filter_keyword"; then
            FILTERED_HASHES+=("${ALL_HASHES[$i]}")
            FILTERED_FOLDERS+=("${ALL_FOLDERS[$i]}")
            FILTERED_SIZES+=("${ALL_SIZES[$i]}")
            FILTERED_STORAGE_DIRS+=("${ALL_STORAGE_DIRS[$i]}")
            FILTERED_MTIMES+=("${ALL_MTIMES[$i]}")
        fi
    done

    local filtered_total=${#FILTERED_HASHES[@]}
    if [[ $filtered_total -eq 0 ]]; then
        echo -e "${RED}✗ 没有匹配的 workspace${RESET}"
        exit 1
    fi

    # 按 mtime 降序排序（最近使用的在前）
    local sorted_indices
    sorted_indices=$(for ((i = 0; i < filtered_total; i++)); do
        echo "$i ${FILTERED_MTIMES[$i]##*|}"
    done | sort -k2 -rn | awk '{print $1}')

    local TEMP_HASHES=() TEMP_FOLDERS=() TEMP_SIZES=() TEMP_STORAGE_DIRS=() TEMP_MTIMES=()
    for idx in $sorted_indices; do
        TEMP_HASHES+=("${FILTERED_HASHES[$idx]}")
        TEMP_FOLDERS+=("${FILTERED_FOLDERS[$idx]}")
        TEMP_SIZES+=("${FILTERED_SIZES[$idx]}")
        TEMP_STORAGE_DIRS+=("${FILTERED_STORAGE_DIRS[$idx]}")
        TEMP_MTIMES+=("${FILTERED_MTIMES[$idx]}")
    done
    FILTERED_HASHES=("${TEMP_HASHES[@]}")
    FILTERED_FOLDERS=("${TEMP_FOLDERS[@]}")
    FILTERED_SIZES=("${TEMP_SIZES[@]}")
    FILTERED_STORAGE_DIRS=("${TEMP_STORAGE_DIRS[@]}")
    FILTERED_MTIMES=("${TEMP_MTIMES[@]}")

    echo ""
    echo -e "${BOLD}选择 workspace（↑↓ 移动，Enter 确认，或输入数字）:${RESET}"
    echo ""

    local choice
    choice=$(_interactive_select "$filtered_total" _render_workspace_item 2)

    # 恢复光标
    tput cnorm 2>/dev/null || true

    local sel_hash="${FILTERED_HASHES[$choice]}"
    local sel_folder="${FILTERED_FOLDERS[$choice]}"
    local sel_storage="${FILTERED_STORAGE_DIRS[$choice]}"
    local sel_ws_dir="${sel_storage}/${sel_hash}"

    echo -e "${GREEN}已选择:${RESET}"
    echo -e "  ${BOLD}$(humanize_uri "$sel_folder")${RESET}"
    echo -e "  ${DIM}storage: ${sel_ws_dir}${RESET}"
    echo ""

    # ── 操作选择 ──
    ACTION_LABELS=("迁移到新路径" "删除存储数据")
    ACTION_DESCS=("— 项目改名后保留 Chat 历史等数据" "— 清理已废弃的 workspace 缓存")

    echo -e "${BOLD}选择操作:${RESET}"
    echo ""

    local action_choice
    action_choice=$(_interactive_select 2 _render_action_item 1)
    tput cnorm 2>/dev/null || true
    echo ""

    case "$action_choice" in
        0) do_migrate "$sel_hash" "$sel_folder" "$sel_ws_dir" ;;
        1) do_delete "$sel_hash" "$sel_folder" "$sel_ws_dir" ;;
    esac
}

# ── 迁移操作 ──
do_migrate() {
    local sel_hash="$1" sel_folder="$2" sel_ws_dir="$3"

    echo -e "${YELLOW}输入新的文件夹路径（仅修改部分即可）:${RESET}"
    echo -e "${DIM}当前 URI: ${sel_folder}${RESET}"
    echo ""

    # 智能提取可编辑部分
    local editable_part
    if [[ "$sel_folder" == vscode-remote://* ]]; then
        # 提取实际路径部分（authority 后的路径）
        editable_part=$(echo "$sel_folder" | sed 's|vscode-remote://[^/]*/||')
    elif [[ "$sel_folder" == file://* ]]; then
        editable_part=$(echo "$sel_folder" | sed 's|file://||')
    else
        editable_part="$sel_folder"
    fi
    editable_part=$(echo "$editable_part" | sed 's/%2B/+/g;s/%2b/+/g;s/%3A/:/g;s/%3a/:/g;s/%20/ /g')

    echo -ne "${CYAN}当前路径: ${RESET}${editable_part}"
    echo ""
    echo -ne "${CYAN}新路径:   ${RESET}"
    read -r -e -i "$editable_part" new_path

    if [[ -z "$new_path" || "$new_path" == "$editable_part" ]]; then
        echo -e "${YELLOW}路径未变更，退出。${RESET}"
        exit 0
    fi

    # 重新编码回 URI
    local new_uri
    # 还原 percent-encoding（保守：仅恢复空格和冒号）
    new_path_encoded=$(echo "$new_path" | sed 's/ /%20/g')
    if [[ "$sel_folder" == vscode-remote://* ]]; then
        local authority
        authority=$(echo "$sel_folder" | sed 's|vscode-remote://\([^/]*\)/.*|\1|')
        new_uri="vscode-remote://${authority}/${new_path_encoded}"
    elif [[ "$sel_folder" == file://* ]]; then
        new_uri="file://${new_path_encoded}"
    else
        new_uri="$new_path_encoded"
    fi

    echo ""
    echo -e "${BOLD}变更预览:${RESET}"
    echo -e "  ${RED}- ${sel_folder}${RESET}"
    echo -e "  ${GREEN}+ ${new_uri}${RESET}"
    echo ""

    # 查找所有相关 storage 目录（Windows 端 + WSL 端可能有同 hash 的目录）
    local related_dirs=("$sel_ws_dir")
    for sd in "${ALL_STORAGE_DIRS_UNIQUE[@]}"; do
        local candidate="${sd}/${sel_hash}"
        [[ -d "$candidate" && "$candidate" != "$sel_ws_dir" ]] && related_dirs+=("$candidate")
    done

    if [[ ${#related_dirs[@]} -gt 1 ]]; then
        echo -e "${CYAN}检测到多个关联 storage 目录（将同步修改）:${RESET}"
        for rd in "${related_dirs[@]}"; do
            echo -e "  ${DIM}${rd}${RESET}"
        done
        echo ""
    fi

    echo -ne "${YELLOW}确认执行迁移? [y/N]:${RESET} "
    read -r confirm
    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
        echo -e "${DIM}已取消。${RESET}"
        return
    fi

    # 执行迁移
    for ws_dir in "${related_dirs[@]}"; do
        local ws_json="${ws_dir}/workspace.json"
        if [[ -f "$ws_json" ]]; then
            cp "$ws_json" "${ws_json}.bak"
            echo -e "${DIM}  备份: ${ws_json}.bak${RESET}"

            local escaped_old
            escaped_old=$(printf '%s\n' "$sel_folder" | sed 's/[&/\]/\\&/g')
            local escaped_new
            escaped_new=$(printf '%s\n' "$new_uri" | sed 's/[&/\]/\\&/g')

            sed -i "s|${escaped_old}|${escaped_new}|g" "$ws_json"
            echo -e "${GREEN}  ✓ 已更新: ${ws_json}${RESET}"
        fi
    done

    echo ""
    echo -e "${GREEN}${BOLD}✓ 迁移完成！${RESET}"
    echo ""
    echo -e "${DIM}后续步骤:${RESET}"
    echo -e "  1. 重命名项目文件夹到新路径"
    echo -e "  2. 用 VS Code 打开新路径"
    echo -e "  3. Copilot Chat 历史将自动恢复"
    echo ""
    echo -e "${DIM}如需回滚，恢复 workspace.json.bak 即可。${RESET}"
}

# ── 删除操作 ──
do_delete() {
    local sel_hash="$1" sel_folder="$2" sel_ws_dir="$3"

    # 查找所有相关 storage 目录
    local related_dirs=("$sel_ws_dir")
    for sd in "${ALL_STORAGE_DIRS_UNIQUE[@]}"; do
        local candidate="${sd}/${sel_hash}"
        [[ -d "$candidate" && "$candidate" != "$sel_ws_dir" ]] && related_dirs+=("$candidate")
    done

    echo -e "${BOLD}即将删除以下 workspace 存储数据:${RESET}"
    echo ""
    local total_size=0
    for rd in "${related_dirs[@]}"; do
        local size
        size=$(du -sh "$rd" 2>/dev/null | cut -f1) || size="?"
        echo -e "  ${RED}✗${RESET} ${rd}  ${DIM}(${size})${RESET}"
    done
    local combined_size
    combined_size=$(du -shc "${related_dirs[@]}" 2>/dev/null | tail -1 | cut -f1) || combined_size="?"
    echo ""
    echo -e "  ${BOLD}总计: ${combined_size}${RESET}"
    echo ""
    echo -e "${RED}${BOLD}⚠ 此操作不可恢复！Chat 历史、索引缓存等数据将被永久删除。${RESET}"
    echo -ne "${YELLOW}确认删除? 输入 'delete' 确认:${RESET} "
    read -r confirm

    if [[ "$confirm" != "delete" ]]; then
        echo -e "${DIM}已取消。${RESET}"
        return
    fi

    for rd in "${related_dirs[@]}"; do
        rm -rf "$rd"
        echo -e "${GREEN}  ✓ 已删除: ${rd}${RESET}"
    done

    echo ""
    echo -e "${GREEN}${BOLD}✓ 清理完成！已释放 ${combined_size} 空间。${RESET}"
}

main "$@"
