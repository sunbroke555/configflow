#!/bin/sh
# Standalone core installer. Never installs or registers a ConfigFlow Agent.
set -eu
umask 022

MIHOMO_VERSION=v1.19.32
OXIDNS_VERSION=v1.6.1
LOCK=/run/configflow-core-install.lock
STAGE=""
LOCKED=false
COMMITTED=false
CREATED_FILES=""
CREATED_DIRS=""
ENABLE_ATTEMPTED=""
START_ATTEMPTED=""
INIT_SYSTEM=""
TUN_ENABLED=false
TUN_REASON=""

fail() { printf '%s\n' "$*" >&2; exit 1; }
exists() { [ -e "$1" ] || [ -L "$1" ]; }
cleanup() {
    cleanup_status=$?
    trap - EXIT HUP INT TERM
    set +e
    if [ "$COMMITTED" != true ]; then
        rollback_safe=true
        for service in $START_ATTEMPTED; do
            if [ "$INIT_SYSTEM" = systemd ]; then systemctl stop "$service" || rollback_safe=false
            else rc-service "$service" stop || rollback_safe=false; fi
        done
        for service in $ENABLE_ATTEMPTED; do
            if [ "$INIT_SYSTEM" = systemd ]; then systemctl disable "$service" || rollback_safe=false
            else rc-update del "$service" default || rollback_safe=false; fi
        done
        if [ "$rollback_safe" = true ]; then
            for path in $CREATED_FILES; do rm -f "$path"; done
            # These directories were absent before this invocation. Cores may
            # create their own cache/runtime files after starting.
            for path in $CREATED_DIRS; do rm -rf "$path"; done
            if [ -n "$CREATED_FILES" ] && [ "$INIT_SYSTEM" = systemd ]; then systemctl daemon-reload; fi
        else
            printf '%s\n' '回滚未完成：核心停止或取消开机启动失败。为避免删除仍运行服务的文件，已保留本次核心、配置及服务文件。请先手动停止 mihomo/oxidns 并取消开机启动，再检查这些文件；本脚本不会自动覆盖残留安装。' >&2
        fi
        if [ "$cleanup_status" -ne 0 ]; then printf '%s\n' '安装失败；已尝试撤销本次创建和启动的核心，请同时检查上方回滚结果。' >&2; fi
    fi
    [ -z "$STAGE" ] || rm -rf "$STAGE"
    if [ "$LOCKED" = true ]; then rm -f "$LOCK/pid"; rmdir "$LOCK"; fi
    exit "$cleanup_status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

[ "$(id -u)" -eq 0 ] || fail '请使用 root 运行，例如 sudo sh install-cores.sh。'
[ "$(uname -s)" = Linux ] || fail '仅支持 Linux（systemd 或 OpenRC）。'
for dependency in awk cat chmod cp gzip mkdir mktemp mv rm rmdir sed sha256sum sleep tail tar; do
    command -v "$dependency" >/dev/null 2>&1 || fail "缺少依赖 ${dependency}。Debian/Ubuntu: apt-get install curl gzip tar coreutils gawk；Alpine: apk add curl gzip tar coreutils gawk。安装依赖后重试。"
done
if command -v curl >/dev/null 2>&1; then DOWNLOAD=curl
elif command -v wget >/dev/null 2>&1; then DOWNLOAD=wget
else fail '需要 curl 或 wget。Debian/Ubuntu: apt-get install curl；Alpine: apk add curl。'; fi

case "$(uname -m)" in
    x86_64|amd64)
        MIHOMO_ASSET=mihomo-linux-amd64-v1-v1.19.32.gz
        MIHOMO_SHA=306f81e723e60ce6b828899a6fe83e1d00e9ecefb2dc8d4d849312a5bc00efdc
        OXIDNS_ASSET=oxidns-x86_64-unknown-linux-musl.tar.gz
        OXIDNS_SHA=2ac8e103e03407a73c29da848f6ce2d524e751ce1da8a744aa9168fc1384e3b0 ;;
    aarch64|arm64)
        MIHOMO_ASSET=mihomo-linux-arm64-v1.19.32.gz
        MIHOMO_SHA=9dd862e28b46ff7d775f169cceebc28deccaa0a9e804237d421cd2571e0caba0
        OXIDNS_ASSET=oxidns-aarch64-unknown-linux-musl.tar.gz
        OXIDNS_SHA=d879d4b9199c5c911d296af4df8d9e974523fb98a6ee2418ae7274d152365e42 ;;
    armv7l|armv7)
        MIHOMO_ASSET=mihomo-linux-armv7-v1.19.32.gz
        MIHOMO_SHA=f622cbe903ba1a11d30e9071d1c5373929c675b854856ff3e7f977255a25bb66
        OXIDNS_ASSET=oxidns-armv7-unknown-linux-musleabihf.tar.gz
        OXIDNS_SHA=369739e626e2664131d91b7f2b9b4cf396475672ed3086d07358675f390e9786 ;;
    *) fail "不支持的架构 $(uname -m)，仅支持 amd64、arm64、armv7。" ;;
esac
if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    INIT_SYSTEM=systemd
    UNIT_DIR=/etc/systemd/system
elif [ -x /sbin/openrc-run ] && command -v rc-service >/dev/null 2>&1 && command -v rc-update >/dev/null 2>&1; then
    INIT_SYSTEM=openrc
    UNIT_DIR=/etc/init.d
else fail '需要正在使用的 systemd 或 OpenRC；容器请先配置 init 服务管理器。'; fi
for directory in /run /etc /usr/local /usr/local/bin "$UNIT_DIR"; do
    [ -d "$directory" ] && [ ! -L "$directory" ] || fail "安装目录缺失或为符号链接：${directory}。请先准备正常目录后重试。"
done
if ! mkdir "$LOCK" 2>/dev/null; then
    fail "另一个核心安装可能正在运行：${LOCK}。请等待完成；若为中断残留，确认其中 pid 对应进程已退出后删除此锁目录再重试。"
fi
LOCKED=true
printf '%s\n' "$$" > "$LOCK/pid"

unit_path() {
    if [ "$INIT_SYSTEM" = systemd ]; then printf '%s/%s.service' "$UNIT_DIR" "$1"; else printf '%s/%s' "$UNIT_DIR" "$1"; fi
}
receipt="configflow-standalone-cores-v1:$INIT_SYSTEM"
complete=true
for service in mihomo oxidns; do
    service_complete=true
    for path in "/usr/local/bin/$service" "/etc/$service/config.yaml" "/etc/$service/.configflow-core-install" "$(unit_path "$service")"; do
        if [ ! -f "$path" ] || [ -L "$path" ]; then service_complete=false; fi
    done
    [ -x "/usr/local/bin/$service" ] || service_complete=false
    if [ "$service_complete" = true ]; then
        expected_receipt=$(printf '%s\n' "$receipt"; sha256sum "/usr/local/bin/$service" "$(unit_path "$service")") || fail '无法核对已有安装记录，请检查核心文件权限。'
        [ "$(cat "/etc/$service/.configflow-core-install")" = "$expected_receipt" ] || service_complete=false
    fi
    [ "$service_complete" = true ] || complete=false
done
if [ "$complete" = true ]; then
    printf '%s\n' '检测到本脚本已完成的双核心安装，保留现有程序、配置与服务并跳过；不会升级或重启。需要面板管理时，请另行安装/接入 Agent。'
    COMMITTED=true
    exit 0
fi
for service in mihomo oxidns; do
    for path in "/usr/local/bin/$service" "/etc/$service" "/etc/systemd/system/$service.service" "/etc/systemd/system/$service.service.d" "/run/systemd/system/$service.service" "/usr/lib/systemd/system/$service.service" "/lib/systemd/system/$service.service" "/etc/init.d/$service" "/run/$service.pid"; do
        ! exists "$path" || fail "发现已有核心、配置或服务：${path}。为保留现有部署，本次未修改；请使用 Agent 接入，或自行备份并移除旧安装后重试。"
    done
    if command -v "$service" >/dev/null 2>&1; then fail "PATH 中已有 $service 核心，请使用 Agent 接入现有安装。"; fi
    if [ "$INIT_SYSTEM" = systemd ]; then
        load_state=$(systemctl show "$service.service" --property=LoadState --value) || fail "无法检查已有 $service 服务，请检查 systemd 是否正常运行。"
        [ "$load_state" = not-found ] || fail "已有 $service 服务（状态 ${load_state}），请使用 Agent 接入现有安装。"
    fi
done

check_ports() {
    proc_files=""
    for network in /proc/net/tcp /proc/net/udp /proc/net/tcp6 /proc/net/udp6; do
        if [ -r "$network" ]; then proc_files="$proc_files $network"; fi
    done
    [ -r /proc/net/tcp ] && [ -r /proc/net/udp ] || fail '无法读取 /proc/net/tcp 或 udp，不能检查 DNS 端口，请挂载容器 /proc 后重试。'
    # Any IPv4/IPv6 listener on these ports can conflict with a wildcard bind.
    if ! awk 'FNR > 1 {split($2, addr, ":"); port=toupper(addr[2]); if ((port == "0035" || port == "041D") && (FILENAME ~ /udp/ || $4 == "0A")) {print "已占用 DNS 端口：" $2 " (" FILENAME ")"; conflict=1}} END {exit conflict ? 1 : 0}' $proc_files; then
        fail '53 或 1053 端口被占用。请用 ss -lntup 检查并调整已有 DNS 服务的监听地址/端口，再重试；本脚本不会关闭系统 DNS 服务。'
    fi
}
check_ports

show_tun_disabled_guidance() {
    printf '%s\n' "TUN 初始配置保持关闭：$TUN_REASON" \
        '普通 Linux/虚拟机：先检查内核 TUN 驱动（modprobe tun），确认 /dev/net/tun 为 10:200 字符设备且可读写。' \
        'PVE LXC：在 PVE 宿主机执行 modprobe tun，检查 pct config <CTID> 的空闲 devN 槽，再用 pct set <CTID> --devN /dev/net/tun 添加设备（例如空闲的 dev0）；完整停止并启动容器后，在容器内复查。不要改为特权容器或放宽整个设备目录权限。' \
        '权限：保留容器的 CAP_NET_ADMIN；核对 Mihomo 服务的 CapabilityBoundingSet、DeviceAllow=/dev/net/tun rw 及设备隔离策略。若接口重名，确认已有接口的归属后更改 Mihomo 的 tun.device。' \
        '修复后：在面板的 Mihomo 基础配置中设置 tun.enable: true，保留 DNS 监听及 TUN DNS 劫持端口 1053，再保存并发布；未接入 Agent 时修改 /etc/mihomo/config.yaml 并重启 mihomo 服务。重复运行本安装脚本会保留已有配置，不会自动开启 TUN。'
}

# Probe without creating an interface, changing routes, or writing sysctls.
# The new service runs as root; these checks describe the installer context.
# Service-manager policy may differ, so actual interface creation is checked
# again after startup instead of treating an active DNS process as TUN success.
check_tun_environment() {
    if ! exists /dev/net/tun; then
        TUN_REASON='/dev/net/tun 未提供。'
    elif [ ! -c /dev/net/tun ]; then
        TUN_REASON='/dev/net/tun 不是字符设备。'
    elif ! command -v stat >/dev/null 2>&1; then
        TUN_REASON='缺少 stat，无法确认 TUN 设备号；请安装 coreutils 后再检查。'
    elif [ "$(stat -L -c '%t:%T' /dev/net/tun 2>/dev/null || true)" != a:c8 ]; then
        TUN_REASON='无法确认 /dev/net/tun 是 Linux TUN 设备（主/次设备号 10:200）。'
    elif ! (exec 3<>/dev/net/tun) 2>/dev/null; then
        TUN_REASON='TUN 设备存在，但无法读写打开；设备映射、cgroup 或安全策略可能限制访问。'
    elif [ -e /sys/class/net/configflow-tun ]; then
        TUN_REASON='网络接口 configflow-tun 已存在；为避免接管其他接口，保留 TUN 关闭。'
    else
        # CAP_NET_ADMIN is bit 12. Inspect its hex digit without relying on
        # awk extensions or shell integer width (also supports 32-bit armv7).
        tun_capability=$(awk '
            $1 == "CapEff:" || $1 == "CapBnd:" {
                if (NF != 2 || $2 !~ /^[0-9a-fA-F]+$/ || length($2) > 16) {invalid=1; next}
                mask=tolower($2); digit=length(mask) >= 4 ? substr(mask, length(mask)-3, 1) : "0"
                seen[$1]++; allowed[$1]=(digit ~ /^[13579bdf]$/)
            }
            END {
                if (invalid || seen["CapEff:"] != 1 || seen["CapBnd:"] != 1) print "unknown"
                else if (!allowed["CapEff:"]) print "effective"
                else if (!allowed["CapBnd:"]) print "bounding"
                else print "available"
            }' /proc/self/status 2>/dev/null) || tun_capability=unknown
        case "$tun_capability" in
            available) TUN_ENABLED=true ;;
            effective) TUN_REASON='当前进程缺少有效 CAP_NET_ADMIN，无法确认可创建 TUN 和调整路由。' ;;
            bounding) TUN_REASON='当前进程的能力上限 CapBnd 不包含 CAP_NET_ADMIN。' ;;
            *) TUN_REASON='无法读取或解析当前进程的 CAP_NET_ADMIN，不能确认 TUN 权限。' ;;
        esac
    fi
    if [ "$TUN_ENABLED" = true ]; then
        printf '%s\n' 'TUN 环境检测通过，初始配置将启用 TUN（mixed、auto-route）。启动时会创建 configflow-tun 并自动调整本机路由；当前没有代理节点，流量按 DIRECT 直连。'
    else
        show_tun_disabled_guidance
    fi
    forwarding=$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || true)
    if [ "$forwarding" = 1 ]; then
        printf '%s\n' 'IPv4 转发：当前为 1；重启后的持久化状态尚未验证。'
    else
        if [ "$forwarding" = 0 ]; then forwarding_status='当前为 0'; else forwarding_status='状态无法读取'; fi
        printf '%s\n' "IPv4 转发：${forwarding_status}。这不影响本机 TUN 的选择；仅当此 Linux/LXC 承担旁路网关或路由转发时需要开启。" \
            '旁路网关修复：在该 Linux/LXC 内执行 sysctl -w net.ipv4.ip_forward=1，并把 net.ipv4.ip_forward=1 写入 /etc/sysctl.d/99-configflow-forward.conf；检查其他 sysctl 文件是否将其改回 0，重启后用 sysctl net.ipv4.ip_forward 验证。PVE 仅做二层网桥时不因此需要开启宿主机转发。'
    fi
}
check_tun_environment
STAGE=$(mktemp -d /tmp/configflow-cores.XXXXXX)
chmod 700 "$STAGE"
mkdir "$STAGE/mihomo" "$STAGE/oxidns"
download() {
    if [ "$DOWNLOAD" = curl ]; then
        curl --fail --location --connect-timeout 15 --max-time 300 --retry 2 --output "$2" "$1" || fail "下载失败：$1。请检查到 GitHub 的网络后重试。"
    else
        wget -T 60 -O "$2" "$1" || fail "下载失败：$1。请检查到 GitHub 的网络后重试。"
    fi
}
printf '%s\n' "下载官方 Mihomo $MIHOMO_VERSION 和 OxiDNS $OXIDNS_VERSION full，并校验固定 SHA256。"
download "https://github.com/MetaCubeX/mihomo/releases/download/$MIHOMO_VERSION/$MIHOMO_ASSET" "$STAGE/mihomo.gz"
download "https://github.com/svenshi/oxidns/releases/download/$OXIDNS_VERSION/$OXIDNS_ASSET" "$STAGE/oxidns.tar.gz"
printf '%s  %s\n' "$MIHOMO_SHA" "$STAGE/mihomo.gz" "$OXIDNS_SHA" "$STAGE/oxidns.tar.gz" > "$STAGE/download.sha256"
sha256sum -c "$STAGE/download.sha256" || fail '下载文件的 SHA256 不匹配，已停止安装；请重新下载，不要跳过校验。'
gzip -dc "$STAGE/mihomo.gz" > "$STAGE/mihomo-bin" || fail 'Mihomo 压缩包解压失败。'
# Only extract the verified core executable, never upstream sample configs/UI.
tar -xzf "$STAGE/oxidns.tar.gz" -C "$STAGE/oxidns" oxidns || fail 'OxiDNS 压缩包解压失败。'
[ -f "$STAGE/oxidns/oxidns" ] && [ ! -L "$STAGE/oxidns/oxidns" ] || fail 'OxiDNS 包内核心文件无效。'
chmod 755 "$STAGE/mihomo-bin" "$STAGE/oxidns/oxidns"
write_mihomo_bootstrap() {
cat > "$STAGE/mihomo/config.yaml" <<MIHOMO_CONFIG
# Direct bootstrap; TUN follows the installation environment probe.
port: 0
socks-port: 0
mixed-port: 0
redir-port: 0
tproxy-port: 0
mode: rule
log-level: info
ipv6: false
tun:
  enable: $TUN_ENABLED
  device: configflow-tun
  stack: mixed
  auto-route: true
  auto-detect-interface: true
  auto-redirect: false
  dns-hijack:
    - any:1053
    - tcp://any:1053
dns:
  enable: true
  listen: 0.0.0.0:1053
  ipv6: false
  enhanced-mode: normal
  nameserver:
    - 223.5.5.5
    - 119.29.29.29
proxies: []
proxy-groups: []
rules:
  - MATCH,DIRECT
MIHOMO_CONFIG
}
write_mihomo_bootstrap
cat > "$STAGE/oxidns/config.yaml" <<'OXIDNS_CONFIG'
log:
  level: info
plugins:
  - tag: forward
    type: forward
    args:
      upstreams:
        - addr: udp://127.0.0.1:1053
  - tag: main_sequence
    type: sequence
    args:
      - exec: $forward
      - exec: accept
  - tag: udp_server
    type: udp_server
    args:
      listen: 0.0.0.0:53
      entry: main_sequence
  - tag: tcp_server
    type: tcp_server
    args:
      listen: 0.0.0.0:53
      entry: main_sequence
OXIDNS_CONFIG
"$STAGE/mihomo-bin" -t -d "$STAGE/mihomo" -f "$STAGE/mihomo/config.yaml" || fail 'Mihomo 初始配置校验失败，未安装核心。'
"$STAGE/oxidns/oxidns" check -c "$STAGE/oxidns/config.yaml" -d "$STAGE/oxidns" || fail 'OxiDNS 初始配置校验失败，未安装核心。'
# Recheck after downloads/validation, before touching persistent core resources.
check_ports

write_new_file() {
    new_source=$1
    new_target=$2
    (set -C; : > "$new_target") || fail "目标已存在或无法创建：${new_target}；不覆盖现有文件。"
    CREATED_FILES="$new_target $CREATED_FILES"
    cp "$new_source" "$new_target" || fail "写入失败：$new_target"
}
for service in mihomo oxidns; do
    mkdir "/etc/$service" || fail "无法新建 /etc/${service}；请检查权限或并行操作。"
    CREATED_DIRS="/etc/$service $CREATED_DIRS"
    write_new_file "$STAGE/$service/config.yaml" "/etc/$service/config.yaml"
    chmod 600 "/etc/$service/config.yaml"
    if [ "$service" = mihomo ]; then binary="$STAGE/mihomo-bin"; else binary="$STAGE/oxidns/oxidns"; fi
    write_new_file "$binary" "/usr/local/bin/$service"
    chmod 755 "/usr/local/bin/$service"
    if [ "$service" = mihomo ]; then core_args='-d /etc/mihomo -f /etc/mihomo/config.yaml'; else core_args='start -c /etc/oxidns/config.yaml -d /etc/oxidns'; fi
    if [ "$INIT_SYSTEM" = systemd ]; then
        cat > "$STAGE/$service.unit" <<UNIT
[Unit]
Description=$service DNS core
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/etc/$service
ExecStart=/usr/local/bin/$service $core_args
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
    else
        cat > "$STAGE/$service.unit" <<UNIT
#!/sbin/openrc-run
name="$service"
description="$service DNS core"
command="/usr/local/bin/$service"
command_args="$core_args"
command_background=true
pidfile="/run/$service.pid"
directory="/etc/$service"
output_log="/etc/$service/$service.log"
error_log="/etc/$service/$service.log"
depend() {
    need net
}
UNIT
    fi
    write_new_file "$STAGE/$service.unit" "$(unit_path "$service")"
    if [ "$INIT_SYSTEM" = openrc ]; then chmod 755 "$(unit_path "$service")"; fi
done
show_service_logs() {
    log_service=$1
    if [ "$INIT_SYSTEM" = systemd ]; then
        if command -v journalctl >/dev/null 2>&1; then journalctl -u "$log_service" -n 40 --no-pager >&2 || true; fi
    elif [ -f "/etc/$log_service/$log_service.log" ]; then
        tail -n 40 "/etc/$log_service/$log_service.log" >&2 || true
    fi
}
if [ "$INIT_SYSTEM" = systemd ]; then systemctl daemon-reload || fail 'systemd 加载新服务失败。'; fi
for service in mihomo oxidns; do
    ENABLE_ATTEMPTED="$service $ENABLE_ATTEMPTED"
    if [ "$INIT_SYSTEM" = systemd ]; then systemctl enable "$service" || fail "$service 设置开机启动失败。"
    else rc-update add "$service" default || fail "$service 设置开机启动失败。"; fi
    START_ATTEMPTED="$service $START_ATTEMPTED"
    if [ "$INIT_SYSTEM" = systemd ]; then systemctl start "$service" || { show_service_logs "$service"; fail "$service 启动失败，请查看上方错误。"; }
    else rc-service "$service" start || { show_service_logs "$service"; fail "$service 启动失败，请查看上方错误。"; }; fi
done
sleep 2
for service in mihomo oxidns; do
    if [ "$INIT_SYSTEM" = systemd ]; then systemctl is-active --quiet "$service" || { show_service_logs "$service"; fail "$service 启动后未保持运行，请查看服务日志。"; }
    else rc-service "$service" status || { show_service_logs "$service"; fail "$service 启动后未保持运行，请查看服务日志。"; }; fi
done
tun_interface_ready() {
    for tun_flag_file in /sys/class/net/configflow-tun/tun_flags /sys/class/net/configflow-tun/flags; do
        tun_flags=$(cat "$tun_flag_file" 2>/dev/null || true)
        # Bit 0 is IFF_TUN in tun_flags and IFF_UP in interface flags.
        printf '%s\n' "$tun_flags" | awk '/^0x[0-9a-fA-F]+$/ {digit=substr(tolower($0), length($0), 1); if (digit ~ /^[13579bdf]$/) ok=1} END {exit ok ? 0 : 1}' || return 1
    done
    if [ "$INIT_SYSTEM" = systemd ]; then
        tun_pid=$(systemctl show mihomo.service --property=MainPID --value 2>/dev/null) || return 1
    else
        tun_pid=$(cat /run/mihomo.pid 2>/dev/null) || return 1
    fi
    case "$tun_pid" in ''|0|*[!0-9]*) return 1 ;; esac
    # An existing interface alone is insufficient: require the new core's
    # open TUN descriptor to identify exactly our explicitly named device.
    for tun_fdinfo in /proc/"$tun_pid"/fdinfo/*; do
        [ -r "$tun_fdinfo" ] || continue
        if awk '$1 == "iff:" && $2 == "configflow-tun" && NF == 2 {found=1} END {exit found ? 0 : 1}' "$tun_fdinfo"; then return 0; fi
    done
    return 1
}
wait_for_tun_interface() {
    tun_wait=0
    until tun_interface_ready; do
        [ "$tun_wait" -lt 10 ] || return 1
        sleep 1
        tun_wait=$((tun_wait + 1))
    done
    return 0
}
if [ "$TUN_ENABLED" = true ]; then
    # Mihomo can remain active with DNS working after its TUN listener failed.
    # Allow slower machines to finish listener creation before falling back.
    if wait_for_tun_interface; then
        printf '%s\n' '已确认 Mihomo 创建并持有 configflow-tun TUN 接口；此检查不代表全部透明代理路由或代理节点已配置。'
    else
        show_service_logs mihomo
        printf '%s\n' '未能确认 Mihomo 实际创建并持有 TUN 接口；将关闭本次初始配置的 TUN，保留 DNS 服务安装。'
        if [ "$INIT_SYSTEM" = systemd ]; then systemctl stop mihomo || fail '关闭 TUN 前停止 Mihomo 失败，未改写运行配置；请手动检查服务。'
        else rc-service mihomo stop || fail '关闭 TUN 前停止 Mihomo 失败，未改写运行配置；请手动检查服务。'; fi
        TUN_ENABLED=false
        TUN_REASON='安装进程预检通过，但未能确认核心服务实际创建 TUN；服务管理器权限或容器设备策略可能与安装进程不同。'
        write_mihomo_bootstrap
        cp "$STAGE/mihomo/config.yaml" /etc/mihomo/config.yaml || fail '写入关闭 TUN 的初始配置失败。'
        if [ "$INIT_SYSTEM" = systemd ]; then systemctl start mihomo || { show_service_logs mihomo; fail '关闭 TUN 后 Mihomo 启动失败。'; }
        else rc-service mihomo start || { show_service_logs mihomo; fail '关闭 TUN 后 Mihomo 启动失败。'; }; fi
        sleep 2
        if [ "$INIT_SYSTEM" = systemd ]; then systemctl is-active --quiet mihomo || { show_service_logs mihomo; fail '关闭 TUN 后 Mihomo 未保持运行。'; }
        else rc-service mihomo status || { show_service_logs mihomo; fail '关闭 TUN 后 Mihomo 未保持运行。'; }; fi
        show_tun_disabled_guidance
    fi
fi
if command -v dig >/dev/null 2>&1; then
    for port in 1053 53; do
        for transport in +notcp +tcp; do
            result="$STAGE/dns-$port-${transport#+}.txt"
            dig @127.0.0.1 -p "$port" example.com A "$transport" +time=2 +tries=1 > "$result" 2>&1 || { cat "$result" >&2; fail "DNS 查询失败：$port ${transport}，已停止安装。"; }
            cat "$result"
            if ! awk '/status: NOERROR,/ {status=1} /ANSWER: [1-9][0-9]*,/ {answer=1} END {exit (status && answer) ? 0 : 1}' "$result"; then
                fail "DNS 查询未获得有效答案：$port ${transport}，请检查上游 223.5.5.5/119.29.29.29 是否可达。"
            fi
        done
    done
    printf '%s\n' '两套核心的 UDP/TCP DNS 查询均已通过。'
else
    printf '%s\n' '服务已启动，实际 DNS 解析尚未验证（未安装 dig）。可安装 dnsutils（Debian/Ubuntu）或 bind-tools（Alpine）后运行：' \
        'dig @127.0.0.1 -p 53 example.com; dig @127.0.0.1 -p 53 example.com +tcp' \
        'dig @127.0.0.1 -p 1053 example.com; dig @127.0.0.1 -p 1053 example.com +tcp'
fi
for service in mihomo oxidns; do
    printf '%s\n' "$receipt" > "$STAGE/receipt"
    sha256sum "/usr/local/bin/$service" "$(unit_path "$service")" >> "$STAGE/receipt" || fail '无法保存安装记录。'
    write_new_file "$STAGE/receipt" "/etc/$service/.configflow-core-install"
done
COMMITTED=true
printf '%s\n' 'Mihomo 与 OxiDNS 已分别安装并设置开机启动。OxiDNS 53 → Mihomo 1053 → 公共 DNS。' \
    '配置：/etc/mihomo/config.yaml、/etc/oxidns/config.yaml。当前没有代理节点，流量按 DIRECT 直连。' \
    '本脚本只安装两个核心；如需面板管理，请继续使用独立的 Agent 接入安装。'
printf '%s\n' "安装结果：tun.enable=${TUN_ENABLED}。首次从面板发布前，请检查 Mihomo 基础配置的 tun.enable 与本次检测结果一致；发布以面板配置为准。"
