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

    # 统计总目录数用于进度显示
    local dirs=("$storage_dir"/*/)
    local dir_total=${#dirs[@]}
    local dir_idx=0

    for ws_dir in "${dirs[@]}"; do
        ((dir_idx++)) || true
        printf '\r\033[K  \033[2m扫描中 [%d/%d] ...\033[0m' "$dir_idx" "$dir_total" > /dev/tty

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
    printf '\r\033[K' > /dev/tty
}

# ── URL percent-decode（完整 UTF-8 支持）──
url_decode() {
    python3 -c "import sys, urllib.parse; print(urllib.parse.unquote(sys.argv[1]))" "$1" 2>/dev/null \
        || echo "$1"  # fallback: 原样返回
}

# ── URL percent-encode（仅编码非 ASCII 和特殊字符）──
url_encode_path() {
    python3 - "$1" << 'PYEOF' 2>/dev/null || echo "$1"
import sys, urllib.parse
path = sys.argv[1]
print(urllib.parse.quote(path, safe="/:@!$&'()*+,;=-._~"))
PYEOF
}

# ── 对 folder URI 做人类可读化 ──
humanize_uri() {
    local uri="$1"
    # 完整解码 percent-encoding
    uri=$(url_decode "$uri")
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
    local lines_per_item=${3:-2}  # 每项占几行
    local page_size=${4:-0}  # 每页显示条数，0=自动检测
    local selected=0
    local offset=0  # 当前窗口起始 index

    # 自动检测终端高度，计算 page_size
    if [[ $page_size -eq 0 ]]; then
        local term_lines
        term_lines=$(tput lines 2>/dev/null) || term_lines=24
        # 预留 3 行给状态栏/提示等
        page_size=$(( (term_lines - 3) / lines_per_item ))
        ((page_size < 3)) && page_size=3
    fi

    # 非交互终端回退
    if [[ ! -t 0 ]]; then
        echo -e "${YELLOW}非交互终端，请输入编号:${RESET}" > /dev/tty
        read -r selected < /dev/tty
        echo "$selected"
        return
    fi

    # 实际窗口大小 = min(page_size, total)
    local window=$page_size
    ((window > total)) && window=$total

    tput civis 2>/dev/null > /dev/tty || true
    trap 'tput cnorm 2>/dev/null > /dev/tty; trap - RETURN' RETURN

    # 渲染固定行数的窗口
    _render_window() {
        for ((wi = 0; wi < window; wi++)); do
            local idx=$((offset + wi))
            if ((idx < total)); then
                $renderer "$idx" "$selected"
            else
                # 空行填充
                for ((li = 0; li < lines_per_item; li++)); do
                    printf '\r\033[K' > /dev/tty
                    echo "" > /dev/tty
                done
            fi
        done
        # 状态栏
        printf '\r\033[K' > /dev/tty
        if ((total > window)); then
            echo -e "  ${DIM}── $((selected + 1))/${total} ──${RESET}" > /dev/tty
        fi
    }

    _render_window

    local status_lines=0
    ((total > window)) && status_lines=1
    local total_render_lines=$(( window * lines_per_item + status_lines ))

    while true; do
        IFS= read -rsn1 key < /dev/tty
        local redraw=0
        case "$key" in
            $'\x1b')
                read -rsn1 -t 0.05 k2 < /dev/tty || true
                read -rsn1 -t 0.05 k3 < /dev/tty || true
                # Page Up/Down 有第 4 字节 ~
                local k4=""
                if [[ "$k3" == "5" || "$k3" == "6" ]]; then
                    read -rsn1 -t 0.05 k4 < /dev/tty || true
                fi
                case "$k2$k3$k4" in
                    "[A") # UP
                        if ((selected > 0)); then
                            ((selected--))
                            # 滚动窗口
                            ((selected < offset)) && offset=$selected
                            redraw=1
                        fi
                        ;;
                    "[B") # DOWN
                        if ((selected < total - 1)); then
                            ((selected++))
                            # 滚动窗口
                            ((selected >= offset + window)) && ((offset = selected - window + 1))
                            redraw=1
                        fi
                        ;;
                    "[5~") # Page Up
                        ((selected -= window))
                        ((selected < 0)) && selected=0
                        ((selected < offset)) && offset=$selected
                        redraw=1
                        ;;
                    "[6~") # Page Down
                        ((selected += window))
                        ((selected >= total)) && selected=$((total - 1))
                        ((selected >= offset + window)) && ((offset = selected - window + 1))
                        redraw=1
                        ;;
                esac
                ;;
            "")
                echo "" > /dev/tty
                echo "$selected"
                return
                ;;
        esac
        if ((redraw)); then
            # 清空多余输入（快速连按产生的残留按键）
            while read -rsn1 -t 0.01 _ < /dev/tty 2>/dev/null; do :; done
            for ((i = 0; i < total_render_lines; i++)); do tput cuu1 2>/dev/null > /dev/tty; done
            _render_window
        fi
    done
}

# ── workspace 列表渲染器 ──
_render_workspace_item() {
    local i=$1 selected=$2
    local display_folder="${FILTERED_DISPLAYS[$i]}"
    local mtime_human="${FILTERED_MTIMES[$i]%%|*}"
    local size="${FILTERED_SIZES[$i]}"

    if [[ $i -eq $selected ]]; then
        printf '\r\033[K  \033[0;36m\033[1m▸ [%d]\033[0m \033[1m%s\033[0m\n\r\033[K        \033[2m%s  ·  %s\033[0m\n' \
            "$((i + 1))" "$display_folder" "$size" "$mtime_human" > /dev/tty
    else
        printf '\r\033[K    \033[2m[%d]\033[0m %s\n\r\033[K        \033[2m%s  ·  %s\033[0m\n' \
            "$((i + 1))" "$display_folder" "$size" "$mtime_human" > /dev/tty
    fi
}

# ── 操作菜单渲染器 ──
ACTION_LABELS=()
ACTION_DESCS=()

_render_action_item() {
    local i=$1 selected=$2
    if [[ $i -eq $selected ]]; then
        printf '\r\033[K  \033[0;36m\033[1m▸ [%d]\033[0m \033[1m%s\033[0m  \033[2m%s\033[0m\n' \
            "$((i + 1))" "${ACTION_LABELS[$i]}" "${ACTION_DESCS[$i]}" > /dev/tty
    else
        printf '\r\033[K    \033[2m[%d]\033[0m %s  \033[2m%s\033[0m\n' \
            "$((i + 1))" "${ACTION_LABELS[$i]}" "${ACTION_DESCS[$i]}" > /dev/tty
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
    echo -e "${DIM}正在扫描 workspace 存储...${RESET}"
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

    # 3. 主循环：选择 workspace → 执行操作 → 回到选择
    while true; do

    echo -e "${BOLD}找到 ${total} 个 workspace 记录${RESET}"
    echo -ne "${YELLOW}输入关键词过滤（留空显示全部，输入 q 退出）:${RESET} "
    read -r filter_keyword

    if [[ "$filter_keyword" == "q" || "$filter_keyword" == "Q" ]]; then
        echo -e "${DIM}再见。${RESET}"
        exit 0
    fi

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
        echo ""
        continue
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

    # 预计算显示名称（单次 python3 调用批量解码，避免渲染时反复 fork）
    echo -e "${DIM}正在解析路径名称...${RESET}"
    FILTERED_DISPLAYS=()
    local _raw_folders=""
    for ((i = 0; i < ${#FILTERED_FOLDERS[@]}; i++)); do
        _raw_folders+="${FILTERED_FOLDERS[$i]}"$'\n'
    done
    local _decoded
    _decoded=$(printf '%s' "$_raw_folders" | python3 -c "
import sys, urllib.parse
for line in sys.stdin:
    uri = urllib.parse.unquote(line.rstrip('\n'))
    # strip scheme prefix
    if uri.startswith('vscode-remote://'):
        uri = uri[len('vscode-remote://'):]
    elif uri.startswith('file://'):
        uri = uri[len('file://'):]
    print(uri)
" 2>/dev/null) || _decoded=""
    if [[ -n "$_decoded" ]]; then
        while IFS= read -r line; do
            FILTERED_DISPLAYS+=("$line")
        done <<< "$_decoded"
    else
        # fallback: 原样显示
        for ((i = 0; i < ${#FILTERED_FOLDERS[@]}; i++)); do
            FILTERED_DISPLAYS+=("$(humanize_uri "${FILTERED_FOLDERS[$i]}")")
        done
    fi

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

    echo ""
    echo -e "${DIM}────────────────────────────────────────${RESET}"
    echo ""

    done  # end while true
}

# ── 迁移操作 ──
do_migrate() {
    local sel_hash="$1" sel_folder="$2" sel_ws_dir="$3"

    echo -e "${YELLOW}输入新的文件夹路径（仅修改部分即可）:${RESET}"
    echo -e "${DIM}当前 URI: ${sel_folder}${RESET}"
    echo ""

    # 智能提取可编辑部分（完整 URL decode）
    local editable_part
    if [[ "$sel_folder" == vscode-remote://* ]]; then
        editable_part=$(echo "$sel_folder" | sed 's|vscode-remote://[^/]*/||')
    elif [[ "$sel_folder" == file://* ]]; then
        editable_part=$(echo "$sel_folder" | sed 's|file://||')
    else
        editable_part="$sel_folder"
    fi
    editable_part=$(url_decode "$editable_part")

    echo -e "${CYAN}当前路径: ${RESET}${editable_part}"
    # 使用 \001/\002 包裹 ANSI 转义，让 readline 正确计算光标位置
    local prompt=$'\001\033[0;36m\002新路径:   \001\033[0m\002'
    read -r -e -i "$editable_part" -p "$prompt" new_path

    if [[ -z "$new_path" || "$new_path" == "$editable_part" ]]; then
        echo -e "${YELLOW}路径未变更。${RESET}"
        return
    fi

    # 重新编码回 URI（中文等非 ASCII 字符需要 percent-encode）
    local new_path_encoded
    new_path_encoded=$(url_encode_path "$new_path")
    local new_uri
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
    echo -ne "${YELLOW}确认删除? [y/N]:${RESET} "
    read -r confirm

    if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
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
