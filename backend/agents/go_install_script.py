"""Generate the canonical Shell installer for the Go Agent."""
import json
import posixpath
import re
import shlex
from pathlib import Path, PurePosixPath


INSTALL_GO_SCRIPT_TEMPLATE = (Path(__file__).parent / "scripts" / "install-go.sh").read_text(encoding="utf-8")
INSTALL_GO_AIO_SCRIPT_TEMPLATE = (Path(__file__).parent / "scripts" / "install-go-aio.sh").read_text(encoding="utf-8")


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
) -> str:
    """Generate an installer with explicit service lifecycle and deployment checks.

    Standard native installs detect systemd/OpenRC on the target. A custom
    restart command is not parsed to invent stop/start/status operations.
    """
    if service_type not in ("mihomo", "mosdns", "oxidns"):
        raise ValueError("Go Agent transactional deployment supports mihomo, mosdns and oxidns")
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
    }
    # Single pass: values containing braces must remain literal, especially JSON
    # commands and URLs. The config heredoc is quoted to disable shell expansion.
    return re.sub(r"\{(" + "|".join(replacements) + r")\}", lambda match: replacements[match.group(1)], INSTALL_GO_SCRIPT_TEMPLATE)


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
) -> str:
    """Manage two isolated native Agents for already-installed native cores.

    The target script checks both cores and both deployment journals before
    stopping either Agent. It never installs, rewrites or restarts the cores.
    """
    if not isinstance(agent_name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", agent_name):
        raise ValueError("Invalid agent name: use letters, numbers, hyphens or underscores")
    ports = []
    for port in (mihomo_agent_port, oxidns_agent_port):
        if isinstance(port, bool) or not re.fullmatch(r"[0-9]+", str(port)) or not 1 <= int(port) <= 65535:
            raise ValueError("Invalid Agent port")
        ports.append(int(port))
    if ports[0] == ports[1]:
        raise ValueError("Mihomo and OxiDNS Agent ports must be different")
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
    return re.sub(r"\{(" + "|".join(replacements) + r")\}",
                  lambda match: replacements[match.group(1)], INSTALL_GO_AIO_SCRIPT_TEMPLATE)
