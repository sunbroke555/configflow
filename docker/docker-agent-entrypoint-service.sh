#!/bin/sh
set -e

# ==============================================================================
# ConfigFlow Agent Docker Entrypoint (统一服务入口)
# ------------------------------------------------------------------------------
# 支持两种模式：
#   1. 单服务模式：设置 SERVICE_TYPE=mihomo|mosdns（mihomo/mosdns 镜像使用）
#   2. AIO 模式：设置 ENABLE_MIHOMO/ENABLE_MOSDNS（aio 镜像使用）
# ==============================================================================

SUPERVISORCTL_CMD="supervisorctl -c /etc/supervisor/supervisord.conf"
AGENT_DIR="/opt/configflow-agent"
mkdir -p "$AGENT_DIR"

# --- 创建默认 Mihomo 配置 ---
create_default_mihomo_config() {
    MIHOMO_CONFIG="/etc/mihomo/config.yaml"
    if [ ! -f "$MIHOMO_CONFIG" ]; then
        echo "Mihomo config not found, creating default config..."
        cat > "$MIHOMO_CONFIG" <<'EOFCONFIG'
# Mihomo 默认配置
# 该配置会被 ConfigFlow 自动更新

mixed-port: 7890
allow-lan: true
mode: rule
log-level: info
external-controller: 0.0.0.0:9090

dns:
  enable: true
  listen: 0.0.0.0:53
  enhanced-mode: fake-ip
  nameserver:
    - 223.5.5.5
    - 119.29.29.29

proxies: []

proxy-groups:
  - name: PROXY
    type: select
    proxies:
      - DIRECT

rules:
  - MATCH,PROXY
EOFCONFIG
        echo "Default mihomo config created at $MIHOMO_CONFIG"
    fi
}

# --- 创建默认 MosDNS 配置 ---
create_default_mosdns_config() {
    MOSDNS_CONFIG="/etc/mosdns/config.yaml"
    if [ ! -f "$MOSDNS_CONFIG" ]; then
        echo "MosDNS config not found, creating default config..."
        cat > "$MOSDNS_CONFIG" <<'EOFCONFIG'
# MosDNS 默认配置
# 该配置会被 ConfigFlow 自动更新

log:
  level: info
  file: ""

plugins:
  # 上游服务器
  - tag: forward_local
    type: forward
    args:
      concurrent: 2
      upstreams:
        - addr: 223.5.5.5
        - addr: 119.29.29.29

  # 执行序列
  - tag: main_sequence
    type: sequence
    args:
      - exec: $forward_local

  # UDP 服务器
  - tag: udp_server
    type: udp_server
    args:
      entry: main_sequence
      listen: ":53"

  # TCP 服务器
  - tag: tcp_server
    type: tcp_server
    args:
      entry: main_sequence
      listen: ":53"
EOFCONFIG
        echo "Default mosdns config created at $MOSDNS_CONFIG"
    fi
}

# --- 生成 Agent 配置文件 ---
generate_agent_config() {
    _service_type=$1
    _agent_name=$2
    _agent_port=$3
    _config_path=$4

    _config_file="${AGENT_DIR}/config-${_service_type}.json"
    _existing_agent_id=""
    _existing_token=""
    if [ -f "$_config_file" ]; then
        _existing_agent_id=$(sed -n 's/.*"agent_id"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9_.-]*\)".*/\1/p' "$_config_file" | head -n 1)
        _existing_token=$(sed -n 's/.*"token"[[:space:]]*:[[:space:]]*"\([A-Za-z0-9_-]*\)".*/\1/p' "$_config_file" | head -n 1)
    fi

    _identity_fields=""
    if [ -n "$_existing_agent_id" ] && [ -n "$_existing_token" ]; then
        _identity_fields=",
  \"agent_id\": \"${_existing_agent_id}\",
  \"token\": \"${_existing_token}\""
    fi

    _config_tmp="${_config_file}.tmp.$$"
    cat > "$_config_tmp" <<EOF
{
  "server_url": "${SERVER_URL}",
  "agent_name": "${_agent_name}",
  "agent_host": "${AGENT_HOST:-0.0.0.0}",
  "agent_port": ${_agent_port},
  "agent_ip": "${AGENT_IP:-}",
  "service_type": "${_service_type}",
  "service_name": "${_service_type}",
  "config_path": "${_config_path}",
  "restart_command": "${SUPERVISORCTL_CMD} restart ${_service_type}",
  "heartbeat_interval": ${HEARTBEAT_INTERVAL:-60}${_identity_fields}
}
EOF
    mv "$_config_tmp" "$_config_file"
    echo "Agent config generated at $_config_file"
}

# --- 设置 supervisor autostart ---
set_supervisor_autostart() {
    _conf_file=$1
    _enabled=$2
    if [ -f "$_conf_file" ]; then
        if [ "$_enabled" = "true" ]; then
            sed -i 's/autostart=false/autostart=true/' "$_conf_file"
        else
            sed -i 's/autostart=true/autostart=false/' "$_conf_file"
        fi
    fi
}

# ==============================================================================
# 主逻辑
# ==============================================================================

echo "Starting ConfigFlow Agent..."

if [ -n "$SERVICE_TYPE" ]; then
    # ---- 单服务模式 ----
    echo "Mode: single service (${SERVICE_TYPE})"

    if [ "$SERVICE_TYPE" = "mihomo" ]; then
        create_default_mihomo_config
        generate_agent_config "mihomo" \
            "${AGENT_NAME:-mihomo-agent}" \
            "${AGENT_PORT:-8080}" \
            "${CONFIG_PATH:-/etc/mihomo/config.yaml}"
        set_supervisor_autostart /etc/supervisor/conf.d/agent-mihomo.conf true
        set_supervisor_autostart /etc/supervisor/conf.d/mihomo.conf true
        chmod -R 755 /etc/mihomo
    elif [ "$SERVICE_TYPE" = "mosdns" ]; then
        create_default_mosdns_config
        generate_agent_config "mosdns" \
            "${AGENT_NAME:-mosdns-agent}" \
            "${AGENT_PORT:-8080}" \
            "${CONFIG_PATH:-/etc/mosdns/config.yaml}"
        set_supervisor_autostart /etc/supervisor/conf.d/agent-mosdns.conf true
        set_supervisor_autostart /etc/supervisor/conf.d/mosdns.conf true
        chmod -R 775 /etc/mosdns
    fi
else
    # ---- AIO 模式 (aio 镜像) ----
    echo "Mode: AIO"

    # Mihomo
    create_default_mihomo_config
    if [ "${ENABLE_MIHOMO}" = "true" ]; then
        echo "Mihomo is enabled."
        generate_agent_config "mihomo" \
            "${AGENT_MIHOMO_NAME:-mihomo-agent}" \
            "${AGENT_MIHOMO_PORT:-8080}" \
            "/etc/mihomo/config.yaml"
        set_supervisor_autostart /etc/supervisor/conf.d/agent-mihomo.conf true
        set_supervisor_autostart /etc/supervisor/conf.d/mihomo.conf true
    else
        echo "Mihomo is disabled."
        set_supervisor_autostart /etc/supervisor/conf.d/agent-mihomo.conf false
        set_supervisor_autostart /etc/supervisor/conf.d/mihomo.conf false
    fi

    # MosDNS
    create_default_mosdns_config
    if [ "${ENABLE_MOSDNS}" = "true" ]; then
        echo "MosDNS is enabled."
        generate_agent_config "mosdns" \
            "${AGENT_MOSDNS_NAME:-mosdns-agent}" \
            "${AGENT_MOSDNS_PORT:-8081}" \
            "/etc/mosdns/config.yaml"
        set_supervisor_autostart /etc/supervisor/conf.d/agent-mosdns.conf true
        set_supervisor_autostart /etc/supervisor/conf.d/mosdns.conf true
    else
        echo "MosDNS is disabled."
        set_supervisor_autostart /etc/supervisor/conf.d/agent-mosdns.conf false
        set_supervisor_autostart /etc/supervisor/conf.d/mosdns.conf false
    fi

    chmod -R 755 /etc/mihomo /etc/mosdns
fi

chmod -R 755 /etc/supervisor

echo "Log directory status:"
ls -la /var/log/supervisor/ 2>/dev/null || true

echo "Executing supervisord..."
exec "$@"
