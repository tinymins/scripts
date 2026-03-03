#!/bin/sh
# /etc/wg-reresolve.sh
# 自动检测 WireGuard peer 的 DDNS IP 变化，清空 DNS 缓存并重启接口
# 所有配置从 UCI 动态读取，无需硬编码
#
# 用法: 配合 cron 每 3 分钟运行一次
#   */3 * * * * /etc/wg-reresolve.sh >> /tmp/wg-reresolve.log 2>&1
# 也可以每 10 分钟运行一次，减少频率
#   */10 * * * * /etc/wg-reresolve.sh >/dev/null 2>&1

LOG_TAG="wg-reresolve"

log() {
    logger -t "$LOG_TAG" "$1"
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1"
}

# ---- 1. 从 UCI 动态发现所有 WireGuard 接口 ----
WG_IFACES=$(uci show network 2>/dev/null \
    | grep "\.proto='wireguard'" \
    | sed "s/network\.\(.*\)\.proto=.*/\1/")

if [ -z "$WG_IFACES" ]; then
    log "未找到任何 WireGuard 接口，退出"
    exit 0
fi

NEED_RESTART_IFACES=""

for IFACE in $WG_IFACES; do
    # ---- 2. 遍历该接口下所有有 endpoint_host 的 peer ----
    PEER_IDX=0
    while true; do
        SECTION="network.@wireguard_${IFACE}[${PEER_IDX}]"

        # 检查 section 是否存在
        uci -q get "${SECTION}" >/dev/null 2>&1 || break

        EP_HOST=$(uci -q get "${SECTION}.endpoint_host")
        PEER_IDX=$((PEER_IDX + 1))

        # 跳过没有 endpoint_host 的 peer（本地 peer）
        [ -z "$EP_HOST" ] && continue

        PUBKEY=$(uci -q get "${SECTION}.public_key" 2>/dev/null)
        DESC=$(uci -q get "${SECTION}.description" 2>/dev/null)

        # ---- 3. 绕过本地缓存解析真实 IP ----
        NEW_IP=$(dig @8.8.8.8 +short "$EP_HOST" A 2>/dev/null | grep -E '^[0-9]+\.' | head -1)
        if [ -z "$NEW_IP" ]; then
            NEW_IP=$(dig @1.1.1.1 +short "$EP_HOST" A 2>/dev/null | grep -E '^[0-9]+\.' | head -1)
        fi
        if [ -z "$NEW_IP" ]; then
            log "[$IFACE] 无法解析 $EP_HOST，跳过"
            continue
        fi

        # ---- 4. 获取当前 WireGuard 使用的 endpoint IP ----
        CURRENT_EP=$(wg show "$IFACE" endpoints 2>/dev/null \
            | grep "$PUBKEY" | awk '{print $2}')
        CURRENT_IP=$(echo "$CURRENT_EP" | cut -d: -f1)

        # ---- 5. 比较 IP ----
        if [ "$NEW_IP" = "$CURRENT_IP" ]; then
            log "[$IFACE] peer '${DESC:-$EP_HOST}' IP 未变化 ($CURRENT_IP)"
        else
            log "[$IFACE] peer '${DESC:-$EP_HOST}' IP 变化: $CURRENT_IP -> $NEW_IP"
            # 标记该接口需要重启（去重）
            echo "$NEED_RESTART_IFACES" | grep -q "$IFACE" || \
                NEED_RESTART_IFACES="$NEED_RESTART_IFACES $IFACE"
        fi
    done
done

# ---- 6. 如果有接口需要重启，先清 DNS 缓存再重启 ----
if [ -z "$NEED_RESTART_IFACES" ]; then
    exit 0
fi

log "清空 DNS 缓存: /etc/init.d/dnsmasq restart"
/etc/init.d/dnsmasq restart
sleep 2

for IFACE in $NEED_RESTART_IFACES; do
    log "重启接口: ifdown $IFACE && ifup $IFACE"
    ifdown "$IFACE"
    sleep 2
    ifup "$IFACE"
done

sleep 8

# ---- 7. 验证 ----
for IFACE in $NEED_RESTART_IFACES; do
    EP_INFO=$(wg show "$IFACE" endpoints 2>/dev/null | grep -v '(none)')
    log "[$IFACE] 重启后 endpoints: $EP_INFO"
done

exit 0
