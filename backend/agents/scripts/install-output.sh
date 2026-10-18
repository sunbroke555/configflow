# Shared by generated installers; nested core installs reuse the parent session.
cf_output_is_active() { [ "${CF_OUTPUT_ACTIVE:-false}" = true ]; }
cf_read_file() {
    while IFS= read -r cf_file_line || [ -n "$cf_file_line" ]; do
        printf '%s\n' "$cf_file_line" || return 1
    done < "$1"
}
cf_output_degrade() {
    CF_OUTPUT_ACTIVE=false
    exec 1>&3 2>&4
    printf '[提醒] 日志记录不可用，继续显示原始输出。已保存的日志：%s\n' "$CF_OUTPUT_LOG" >&4 || :
    return 0
}

cf_output_begin() {
    CF_OUTPUT_OWNER=false
    if [ "$CF_OUTPUT_REUSE" = true ]; then return 0; fi
    CF_OUTPUT_OWNER=true
    CF_OUTPUT_ACTIVE=false
    CF_OUTPUT_DIR=""
    CF_OUTPUT_LOG=""
    exec 3>&1 4>&2
    cf_saved_umask=$(umask)
    umask 077
    # A fresh private directory prevents overwriting or following an existing
    # log file. CONFIGFLOW_INSTALL_LOG_DIR optionally selects an existing parent.
    for cf_log_root in "${CONFIGFLOW_INSTALL_LOG_DIR:-/var/log}" "${TMPDIR:-/tmp}" /tmp; do
        [ -d "$cf_log_root" ] && [ -w "$cf_log_root" ] || continue
        CF_OUTPUT_DIR=$(mktemp -d "$cf_log_root/configflow-install.XXXXXX" 2>/dev/null) || continue
        CF_OUTPUT_LOG="$CF_OUTPUT_DIR/install.log"
        if printf '' > "$CF_OUTPUT_LOG"; then
            CF_OUTPUT_ACTIVE=true
            break
        fi
    done
    umask "$cf_saved_umask"
    if ! cf_output_is_active; then
        printf '%s\n' '[提醒] 无法创建详细日志，将直接显示原始输出。' >&4
    else
        printf '详细日志：%s\n' "$CF_OUTPUT_LOG" >&3
    fi
    return 0
}

cf_ui() {
    printf '%s\n' "$*" >&3 || :
    if cf_output_is_active; then printf '%s\n' "$*" >> "$CF_OUTPUT_LOG" || cf_output_degrade; fi
    return 0
}
cf_prompt() {
    printf '%s' "$*" >&3 || :
    if cf_output_is_active; then printf '%s' "$*" >> "$CF_OUTPUT_LOG" || cf_output_degrade; fi
    return 0
}
cf_fact() {
    case "$1" in ''|*[!a-zA-Z0-9_-]*) return 0 ;; esac
    if cf_output_is_active; then
        if printf '%s\n' "$2" > "$CF_OUTPUT_DIR/fact.$1"; then return 0; fi
        cf_output_degrade
    fi
    case "$1" in tun_verified|core_mihomo_new|core_oxidns_new) return 0 ;; esac
    cf_ui "$2"
}
cf_fact_read() {
    cf_output_is_active || return 0
    case "$1" in ''|*[!a-zA-Z0-9_-]*) return 0 ;; esac
    if [ -f "$CF_OUTPUT_DIR/fact.$1" ]; then cf_read_file "$CF_OUTPUT_DIR/fact.$1" || :; fi
    return 0
}
cf_note() {
    case "$1" in ''|*[!a-zA-Z0-9_-]*) return 0 ;; esac
    if cf_output_is_active; then
        if printf '%s\n' "$2" > "$CF_OUTPUT_DIR/note.$1"; then return 0; fi
        cf_output_degrade
    fi
    cf_ui "[提醒] $2"
}
cf_step() {
    cf_step_number=0
    if cf_output_is_active; then
        if [ -f "$CF_OUTPUT_DIR/step-number" ]; then read -r cf_step_number < "$CF_OUTPUT_DIR/step-number" || cf_step_number=0; fi
        case "$cf_step_number" in ''|*[!0-9]*) cf_step_number=0 ;; esac
        cf_step_number=$((cf_step_number + 1))
        printf '%s\n' "$cf_step_number" > "$CF_OUTPUT_DIR/step-number" || cf_output_degrade
        printf '%s\n' "$*" > "$CF_OUTPUT_DIR/current-step" || cf_output_degrade
        cf_ui "[$cf_step_number] $*……"
    else
        cf_ui "[进行] $*……"
    fi
}
cf_step_done() { cf_ui "[完成] $*"; }
cf_complete() {
    if cf_output_is_active; then
        if printf '%s\n' "$*" > "$CF_OUTPUT_DIR/result"; then return 0; fi
        cf_output_degrade
    fi
    cf_ui "$*"
}

cf_environment_check() {
    if ! cf_output_is_active; then "$@"; return $?; fi
    if ! cf_environment_report=$(mktemp "$CF_OUTPUT_DIR/environment.XXXXXX"); then
        cf_output_degrade
        "$@"
        return $?
    fi
    # This is an external Agent process, never an installer shell function.
    if "$@" > "$cf_environment_report" 2>&1; then cf_environment_status=0;
    else cf_environment_status=$?; fi
    cf_read_file "$cf_environment_report" || cf_output_degrade
    [ "$cf_environment_status" -eq 0 ] || return "$cf_environment_status"
    cf_warning_number=0
    while IFS= read -r cf_environment_line; do
        case "$cf_environment_line" in
            '[提醒] TUN 实际启动：'*)
                if [ "$(cf_fact_read tun_verified)" != yes ]; then
                    cf_note tun_scope 'TUN：预检未验证核心实际创建接口；详细检查范围见日志。'
                fi ;;
            '[提醒] IPv4 转发持久化：'*)
                if [ ! -f "$CF_OUTPUT_DIR/note.forwarding" ]; then
                    cf_note forwarding 'IPv4 转发重启后是否保持尚未确认；承担网关转发时请检查持久化配置，处理方法见日志。'
                fi ;;
            '[提醒] '*)
                cf_warning_number=$((cf_warning_number + 1))
                cf_note "environment_$cf_warning_number" "${cf_environment_line#'[提醒] '}" ;;
        esac
    done < "$cf_environment_report"
    return 0
}

cf_deployment_check() {
    if ! cf_output_is_active; then "$@"; return $?; fi
    if ! cf_deployment_report=$(mktemp "$CF_OUTPUT_DIR/deployment.XXXXXX"); then
        cf_output_degrade
        "$@"
        return $?
    fi
    if "$@" > "$cf_deployment_report" 2>&1; then cf_deployment_status=0;
    else cf_deployment_status=$?; fi
    cf_read_file "$cf_deployment_report" || cf_output_degrade
    while IFS= read -r cf_deployment_line; do
        case "$cf_deployment_line" in
            *'Deployment check requires attention:'*)
                cf_note deployment '存在需要处理的发布记录；检查结果和恢复方法见详细日志。'
                break ;;
        esac
    done < "$cf_deployment_report"
    return "$cf_deployment_status"
}

cf_output_end() {
    [ "$CF_OUTPUT_OWNER" = true ] || return 0
    cf_output_is_active || return 0
    if [ "$1" -ne 0 ]; then
        cf_failed_step='安装准备'
        if [ -f "$CF_OUTPUT_DIR/current-step" ]; then IFS= read -r cf_failed_step < "$CF_OUTPUT_DIR/current-step"; fi
        printf '\n[失败] %s（退出码 %s）\n' "$cf_failed_step" "$1" >&4
        printf '%s\n' '最近的详细输出（含错误及回滚信息）：' >&4
        if command -v tail >/dev/null 2>&1; then tail -n 50 "$CF_OUTPUT_LOG" >&4;
        else cf_read_file "$CF_OUTPUT_LOG" >&4; fi
        printf '完整日志及处理提示：%s\n' "$CF_OUTPUT_LOG" >&4
        return 0
    fi
    if [ -f "$CF_OUTPUT_DIR/result" ]; then
        cf_ui ''
        cf_ui "$(cf_read_file "$CF_OUTPUT_DIR/result")"
        for cf_summary_file in "$CF_OUTPUT_DIR"/fact.*; do
            [ -f "$cf_summary_file" ] || continue
            case "$cf_summary_file" in */fact.tun_verified|*/fact.core_mihomo_new|*/fact.core_oxidns_new) continue ;; esac
            cf_ui "  $(cf_read_file "$cf_summary_file")"
        done
        cf_notes_heading=false
        for cf_summary_file in "$CF_OUTPUT_DIR"/note.*; do
            [ -f "$cf_summary_file" ] || continue
            if [ "$cf_notes_heading" = false ]; then cf_ui ''; cf_ui '需要留意'; cf_notes_heading=true; fi
            cf_ui "  $(cf_read_file "$cf_summary_file")"
        done
    fi
    cf_ui ''
    cf_ui "详细日志：$CF_OUTPUT_LOG"
    return 0
}
