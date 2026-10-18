"""Generate the canonical Shell installer for the Go Agent."""
import json
import posixpath
import re
import shlex
from pathlib import Path, PurePosixPath


INSTALL_GO_SCRIPT_TEMPLATE = (Path(__file__).parent / "scripts" / "install-go.sh").read_text(encoding="utf-8")
INSTALL_GO_AIO_SCRIPT_TEMPLATE = (Path(__file__).parent / "scripts" / "install-go-aio.sh").read_text(encoding="utf-8")
AGENT_CORE_INSTALL_INTERFACE = "# ConfigFlow Agent + core installer interface: 1"
_AGENT_INIT_DETECTION = '''if command -v rc-service >/dev/null 2>&1 && command -v rc-update >/dev/null 2>&1; then
    INIT_SYSTEM="openrc"
elif command -v systemctl >/dev/null 2>&1; then
    INIT_SYSTEM="systemd"
else
    echo "需要 systemd 或 OpenRC；自定义运行环境请显式配置停止、启动、状态命令。"
    exit 1
fi'''
_CORE_INIT_DETECTION = '''if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
    INIT_SYSTEM="systemd"
elif [ -x /sbin/openrc-run ] && command -v rc-service >/dev/null 2>&1 && command -v rc-update >/dev/null 2>&1; then
    INIT_SYSTEM="openrc"
else
    echo "需要正在使用的 systemd 或 OpenRC；容器请先配置 init 服务管理器。" >&2
    exit 1
fi'''


def generate_go_agent_install_script(
    server_url: str,
    agent_name: str,
    service_type: str,
    agent_port: int = 8080,
    agent_ip: str = "",
    config_path: str = None,
    restart_command: str = None,
    binary_download_url: str = "",
    service_manager: str = "",
    service_unit: str = "",
    service_binary: str = "",
    stop_command: str = "",
    start_command: str = "",
    status_command: str = "",
    health_url: str = "",
    health_dns_address: str = "",
    health_dns_name: str = "",
    deployment_health_timeout: int = 30,
    install_core: bool = False,
) -> str:
    """Generate an installer with explicit service lifecycle and deployment checks.

    Standard native installs detect systemd/OpenRC on the target. A custom
    restart command is not parsed to invent stop/start/status operations.
    """
    if service_type not in ("mihomo", "mosdns", "oxidns"):
        raise ValueError("Go Agent transactional deployment supports mihomo, mosdns and oxidns")
    if not isinstance(install_core, bool):
        raise ValueError("install_core 必须为 true 或 false")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_name):
        raise ValueError("Invalid agent name: use letters, numbers, hyphens or underscores")
    if not 1 <= int(agent_port) <= 65535:
        raise ValueError("Invalid Agent port")
    unit = service_unit or service_type
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", unit):
        raise ValueError("Invalid service unit")
    if unit.endswith(".service"):
        unit = unit[:-8]
    if service_manager not in ("", "systemd", "openrc", "command"):
        raise ValueError("Shell installs require systemd, openrc or explicit command lifecycle")
    standard_restart = not restart_command or restart_command in (
        f"systemctl restart {unit}", f"systemctl restart {unit}.service", f"rc-service {unit} restart",
    )
    if install_core:
        if service_type not in ("mihomo", "oxidns"):
            raise ValueError("仅单服务 Mihomo 或 OxiDNS Go Agent 支持同时安装核心")
        if (config_path not in (None, "", f"/etc/{service_type}/config.yaml") or
                unit != service_type or service_manager or not standard_restart or
                service_binary not in ("", service_type, f"/usr/local/bin/{service_type}") or
                any((stop_command, start_command, status_command))):
            raise ValueError("同时安装核心需要默认配置路径、服务名、核心路径和自动识别服务管理器，不能使用自定义启停命令")
        if isinstance(agent_port, bool) or not re.fullmatch(r"[0-9]+", str(agent_port)):
            raise ValueError("Invalid Agent port")
        if int(agent_port) == (1053 if service_type == "mihomo" else 53):
            raise ValueError("Agent 端口不能与所安装核心的 DNS 监听端口相同")
    if not service_manager:
        service_manager = "__CONFIGFLOW_INIT__" if standard_restart else "command"
    if service_manager == "command" and not all((stop_command, start_command, status_command)):
        raise ValueError("Custom lifecycle requires stop_command, start_command and status_command; a restart command alone cannot support rollback")
    cfg = {
        "server_url": server_url,
        "agent_name": agent_name,
        "agent_host": "0.0.0.0",
        "agent_port": int(agent_port),
        "agent_ip": agent_ip,
        "service_type": service_type,
        "deployment_method": "shell",
        "service_name": service_type,
        "config_path": config_path or f"/etc/{service_type}/config.yaml",
        "restart_command": "__CONFIGFLOW_RESTART__" if standard_restart else restart_command,
        "service_manager": service_manager,
        "service_unit": unit,
        "service_binary": service_binary or service_type,
        "stop_command": stop_command,
        "start_command": start_command,
        "status_command": status_command,
        "health_url": health_url,
        "health_dns_address": health_dns_address,
        "health_dns_name": health_dns_name,
        "deployment_health_timeout": int(deployment_health_timeout),
        "heartbeat_interval": 30,
        "agent_id": "__CONFIGFLOW_AGENT_ID__",
        "token": "__CONFIGFLOW_TOKEN__",
    }
    replacements = {
        "service_type": service_type,
        "service_unit": unit,
        "agent_name": agent_name,
        "config_json": json.dumps(cfg, ensure_ascii=True, indent=2),
        "binary_download_url_shell": shlex.quote(binary_download_url.rstrip("/")),
        "init_detection": _AGENT_INIT_DETECTION,
        "core_install_phase": "",
        "agent_cleanup_trap": 'trap \'rm -f "$BINARY_TMP"\' EXIT HUP INT TERM',
    }
    if install_core:
        from backend.agents.core_install_script import generate_core_install_script

        # Keep the core installer's variables, umask, traps and exit statements
        # inside a subshell. In particular, do not place this in an if/&&/||
        # condition: that would disable its set -e failure handling.
        replacements["core_install_phase"] = (
            f'echo "先安装或保留 {service_type} 核心，成功后继续安装 Agent。"\n'
            '(\n'
            f'set -- --core {service_type}\n'
            + generate_core_install_script().rstrip() + '\n)\n'
            'BINARY_TMP=""\n'
            'configflow_agent_cleanup() {\n'
            '    agent_status=$?\n'
            '    trap - 0 HUP INT TERM\n'
            '    if [ -n "${BINARY_TMP:-}" ]; then rm -f "$BINARY_TMP" || true; fi\n'
            '    if [ "$agent_status" -ne 0 ]; then\n'
            f'        echo "Agent 安装未完成；{service_type} 核心安装阶段已结束，已安装或原有的核心予以保留。修复上方 Agent 错误后可重新运行。" >&2\n'
            '    fi\n'
            '    exit "$agent_status"\n'
            '}\n'
            'trap configflow_agent_cleanup 0\n'
            "trap 'exit 129' HUP\n"
            "trap 'exit 130' INT\n"
            "trap 'exit 143' TERM\n"
            '# Existing cores are skipped before core dependency installation.\n'
            '# Check Agent prerequisites before downloading or writing its files.\n'
            'for agent_tool in awk cat chmod cp grep head mkdir mktemp mv rm sed; do\n'
            '    if ! command -v "$agent_tool" >/dev/null 2>&1; then\n'
            '        echo "Agent 安装缺少工具：${agent_tool}。请使用系统包管理器补齐后重新运行。" >&2\n'
            '        exit 1\n'
            '    fi\n'
            'done\n'
            'if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then\n'
            '    echo "Agent 安装需要 curl 或 wget，请使用系统包管理器安装后重新运行。" >&2\n'
            '    exit 1\n'
            'fi\n'
            f'CORE_BINARY={shlex.quote(service_binary or service_type)}\n'
            f'if [ ! -f /etc/{service_type}/config.yaml ] || [ ! -r /etc/{service_type}/config.yaml ] || ! command -v "$CORE_BINARY" >/dev/null 2>&1; then\n'
            f'    echo "{service_type} 核心缺少默认配置或可执行文件。请补齐 /etc/{service_type}/config.yaml 和核心程序；已有自定义部署请取消同时安装核心，并设置实际路径。" >&2\n'
            '    exit 1\n'
            'fi\n'
            'if [ "$INIT_SYSTEM" = systemd ]; then\n'
            f'    core_unit_state=$(systemctl show {service_type}.service --property=LoadState --value)\n'
            '    if [ "$core_unit_state" != loaded ]; then\n'
            f'        echo "{service_type} 默认服务未正确加载，请先修复 {service_type}.service；已有自定义服务请取消同时安装核心并填写实际服务名。" >&2\n'
            '        exit 1\n'
            '    fi\n'
            f'elif [ ! -x /etc/init.d/{service_type} ]; then\n'
            f'    echo "缺少可执行的 /etc/init.d/{service_type} 服务，请先补齐；已有自定义服务请取消同时安装核心并填写实际服务名。" >&2\n'
            '    exit 1\n'
            'fi\n'
        )
        replacements["init_detection"] = _CORE_INIT_DETECTION
        replacements["agent_cleanup_trap"] = "# The Agent cleanup trap above preserves the completed core phase."
    # Single pass: values containing braces must remain literal, especially JSON
    # commands and URLs. The config heredoc is quoted to disable shell expansion.
    script = re.sub(r"\{(" + "|".join(replacements) + r")\}", lambda match: replacements[match.group(1)], INSTALL_GO_SCRIPT_TEMPLATE)
    if install_core:
        script = script.replace("#!/bin/sh\n", f"#!/bin/sh\n{AGENT_CORE_INSTALL_INTERFACE}\n", 1)
    return script


def generate_go_aio_install_script(
    server_url: str,
    agent_name: str,
    agent_ip: str = "",
    binary_download_url: str = "",
    mihomo_agent_port: int = 8080,
    oxidns_agent_port: int = 8082,
    mihomo_config_path: str = "/etc/mihomo/config.yaml",
    oxidns_config_path: str = "/etc/oxidns/config.yaml",
    mihomo_service_unit: str = "mihomo",
    oxidns_service_unit: str = "oxidns",
    mihomo_service_binary: str = "mihomo",
    oxidns_service_binary: str = "oxidns",
    install_core: bool = False,
) -> str:
    """Manage two isolated native Agents, optionally installing missing cores.

    Core installation is opt-in and uses the canonical installer in an isolated
    phase. Agent failures retain completed cores and restart previous Agents.
    """
    if not isinstance(install_core, bool):
        raise ValueError("install_core 必须为 true 或 false")
    if not isinstance(agent_name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", agent_name):
        raise ValueError("Invalid agent name: use letters, numbers, hyphens or underscores")
    ports = []
    for port in (mihomo_agent_port, oxidns_agent_port):
        if isinstance(port, bool) or not re.fullmatch(r"[0-9]+", str(port)) or not 1 <= int(port) <= 65535:
            raise ValueError("Invalid Agent port")
        ports.append(int(port))
    if ports[0] == ports[1]:
        raise ValueError("Mihomo and OxiDNS Agent ports must be different")
    if install_core and any(port in (53, 1053) for port in ports):
        raise ValueError("两个 Agent 端口均不能使用核心 DNS 监听端口 53 或 1053")
    def inside_agent_directory(value):
        # Collapse Linux absolute-path aliases before comparing components.
        normalized = PurePosixPath(posixpath.normpath("/" + value.lstrip("/")))
        agent_root = PurePosixPath("/opt/configflow-agent")
        return normalized == agent_root or agent_root in normalized.parents

    paths = []
    for value in (mihomo_config_path, oxidns_config_path):
        if (not isinstance(value, str) or not value.startswith("/") or
                any(ord(c) < 32 or ord(c) == 127 for c in value) or
                ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) == "/" or value.endswith("/")):
            raise ValueError("Core configuration paths must be absolute file paths without parent traversal")
        if inside_agent_directory(value):
            raise ValueError("Core configuration paths must be outside the Agent directory")
        paths.append(str(PurePosixPath("/" + value.lstrip("/"))))
    if paths[0] == paths[1]:
        raise ValueError("Mihomo and OxiDNS configuration paths must be different")
    if PurePosixPath(paths[0]).parent == PurePosixPath(paths[1]).parent:
        raise ValueError("Core configurations require separate directories for deployment journals")
    replacements = {"binary_download_url_shell": shlex.quote(binary_download_url.rstrip("/")),
                    "mihomo_agent_port": str(ports[0]), "oxidns_agent_port": str(ports[1])}
    for service, port, path, unit, binary in zip(
        ("mihomo", "oxidns"), ports, paths,
        (mihomo_service_unit, oxidns_service_unit),
        (mihomo_service_binary, oxidns_service_binary),
    ):
        if not isinstance(unit, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", unit):
            raise ValueError("Invalid service unit")
        unit = unit.removesuffix(".service")
        if not unit or unit.startswith("configflow-"):
            raise ValueError("Core service unit must not be a ConfigFlow Agent unit")
        if not isinstance(binary, str) or not binary or any(ord(c) < 32 or ord(c) == 127 for c in binary):
            raise ValueError("Invalid core service binary")
        if binary.startswith("/") and inside_agent_directory(binary):
            raise ValueError("Core service binaries must be outside the Agent directory")
        if install_core and (path != f"/etc/{service}/config.yaml" or unit != service or
                             binary not in (service, f"/usr/local/bin/{service}")):
            raise ValueError("同时安装两个核心需要默认配置路径、服务名和核心路径；自定义部署请取消同时安装核心")
        cfg = {
            "server_url": server_url, "agent_name": f"{agent_name}-{service}",
            "agent_host": "0.0.0.0", "agent_port": port, "agent_ip": agent_ip,
            "service_type": service, "service_name": service, "deployment_method": "shell",
            "native_aio": True, "agent_service_unit": f"configflow-agent-{service}",
            "config_path": path, "service_manager": "__CONFIGFLOW_INIT__", "service_unit": unit,
            "service_binary": binary, "restart_command": "__CONFIGFLOW_RESTART__",
            "heartbeat_interval": 30, "deployment_health_timeout": 30,
            "agent_id": "__CONFIGFLOW_AGENT_ID__", "token": "__CONFIGFLOW_TOKEN__",
        }
        replacements[f"{service}_config_json"] = json.dumps(cfg, ensure_ascii=True, indent=2)
        replacements[f"{service}_config_path_shell"] = shlex.quote(path)
        replacements[f"{service}_service_unit_shell"] = shlex.quote(unit)
        replacements[f"{service}_service_binary_shell"] = shlex.quote(binary)
    if mihomo_service_unit.removesuffix(".service") == oxidns_service_unit.removesuffix(".service"):
        raise ValueError("Mihomo and OxiDNS service units must be different")
    agent_arch_detection = '''case "$(uname -m)" in
    x86_64|amd64) AGENT_ARCH=linux-amd64 ;;
    aarch64|arm64) AGENT_ARCH=linux-arm64 ;;
    armv7l|armhf) AGENT_ARCH=linux-armv7 ;;
    *) fail "不支持的 CPU 架构" ;;
esac'''
    replacements.update({
        "init_detection": '''if command -v rc-service >/dev/null 2>&1 && command -v rc-update >/dev/null 2>&1; then
    INIT_SYSTEM=openrc
elif command -v systemctl >/dev/null 2>&1; then
    INIT_SYSTEM=systemd
else
    fail "需要 systemd 或 OpenRC 管理已安装的 Mihomo 和 OxiDNS。"
fi''',
        "installation_description": 'echo "此安装器只接管已安装的 Mihomo 和 OxiDNS，不安装核心、不改核心配置、不重启核心。"',
        "agent_arch_detection": agent_arch_detection,
        "core_phase_state": "",
        "core_cleanup_message": "",
        "core_install_phase": "",
    })
    if install_core:
        from backend.agents.core_install_script import generate_core_install_script

        replacements.update({
            "init_detection": _CORE_INIT_DETECTION,
            "installation_description": 'echo "选择安装时先补装缺少的 Mihomo 和 OxiDNS 核心，再安装两个独立 Agent；已有核心与配置保留。"',
            "agent_arch_detection": "",
            "core_phase_state": "CORE_PHASE_COMPLETED=false\n",
            "core_cleanup_message": '''        if [ "$CORE_PHASE_COMPLETED" = true ]; then
            echo "Agent 安装未完成；两个核心的安装阶段已结束，已安装或原有的核心予以保留。修复上方 Agent 错误后可重新运行。" >&2
        fi
''',
            # An unconditional subshell preserves set -e, unlike an if/&&/||
            # context, and isolates core variables, exit statements and traps.
            "core_install_phase": agent_arch_detection + '\n'
                + 'echo "先安装或保留两个核心，成功后继续安装两个 Agent。"\n'
                + '(\nset -- --core both\n' + generate_core_install_script().rstrip() + '\n)\n'
                + 'CORE_PHASE_COMPLETED=true\n'
                + '''# Existing cores can skip core dependency installation; check Agent tools.
for agent_tool in cat chmod cp dirname basename grep head mkdir mktemp mv pwd readlink rm sed; do
    command -v "$agent_tool" >/dev/null 2>&1 || fail "Agent 安装缺少工具：${agent_tool}。请使用系统包管理器补齐后重新运行。"
done
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    fail "Agent 安装需要 curl 或 wget，请使用系统包管理器安装后重新运行。"
fi
# Minimal systems may not yet have /opt, needed for physical path checks.
mkdir -p "$(dirname "$AGENT_ROOT")"
''',
        })
    script = re.sub(r"\{(" + "|".join(replacements) + r")\}",
                    lambda match: replacements[match.group(1)], INSTALL_GO_AIO_SCRIPT_TEMPLATE)
    if install_core:
        script = script.replace("#!/bin/sh\n", f"#!/bin/sh\n{AGENT_CORE_INSTALL_INTERFACE}\n", 1)
    return script
