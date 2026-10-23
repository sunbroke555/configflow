"""Standalone, fixed-layout Mihomo + OxiDNS core installer."""
from pathlib import Path
from backend.agents.install_output import with_install_output

# The core phase describes forwarding before any Agent runs. When the combined
# installer enables and persists forwarding itself, its manual sysctl repair
# would contradict the Agent step, so that one block is replaced verbatim.
_CORE_FORWARDING_BLOCK = '''    forwarding=$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || true)
    if [ "$forwarding" = 1 ]; then
        printf '%s\\n' 'IPv4 转发：当前为 1；重启后的持久化状态尚未验证。'
        cf_note forwarding 'IPv4 转发：当前已开启；重启后是否保持尚未确认。'
    else
        if [ "$forwarding" = 0 ]; then forwarding_status='当前为 0'; else forwarding_status='状态无法读取'; fi
        printf '%s\\n' "IPv4 转发：${forwarding_status}。这不影响本机 TUN 的选择；仅当此 Linux/LXC 承担旁路网关或路由转发时需要开启。" \\
            '旁路网关修复：在该 Linux/LXC 内执行 sysctl -w net.ipv4.ip_forward=1，并把 net.ipv4.ip_forward=1 写入 /etc/sysctl.d/99-configflow-forward.conf；检查其他 sysctl 文件是否将其改回 0，重启后用 sysctl net.ipv4.ip_forward 验证。PVE 仅做二层网桥时不因此需要开启宿主机转发。'
        cf_note forwarding "IPv4 转发：${forwarding_status}，持久化未验证；作为旁路网关时需开启，修复步骤见详细日志。"
    fi
'''
# The Agent step writes the forwarding note after persisting, so none here.
_INSTALLER_FORWARDING_BLOCK = '''    forwarding=$(cat /proc/sys/net/ipv4/ip_forward 2>/dev/null || true)
    case "$forwarding" in
        1) printf '%s\\n' 'IPv4 转发：当前为 1，安装器将在 Agent 步骤持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）。' ;;
        0) printf '%s\\n' 'IPv4 转发：当前为 0，安装器将在 Agent 步骤开启并持久化（/etc/sysctl.d/99-configflow-aio-forward.conf）。' ;;
        *) printf '%s\\n' 'IPv4 转发：状态无法读取，安装器将在 Agent 步骤再次检查并开启。' ;;
    esac
'''


def generate_core_install_script(installer_forwarding: bool = False) -> str:
    """Return the core-only installer; no Agent identity or access token is used.

    ``installer_forwarding`` is for the core phase of an installer that enables
    and persists IPv4 forwarding itself; the default is install-cores.sh as is.
    """
    script = (Path(__file__).parent / "scripts" / "install-cores.sh").read_text(encoding="utf-8")
    if installer_forwarding:
        if script.count(_CORE_FORWARDING_BLOCK) != 1:
            raise RuntimeError("install-cores.sh forwarding block changed; review the installer-managed text")
        script = script.replace(_CORE_FORWARDING_BLOCK, _INSTALLER_FORWARDING_BLOCK, 1)
    return with_install_output(script)
