"""Sandboxed CLI reporting checks; no host services or containers are changed."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from backend.agents.install_script import (
    generate_docker_agent_run,
    generate_lightweight_install_script,
)


ROOT = Path(__file__).parents[1]


def test_legacy_generator_preserves_template_markers_inside_configuration(tmp_path):
    path = "/etc/{cli_output}/{agent_shell_content}/{service_type}.yaml"
    command = "echo {cli_output} {agent_shell_content} {service_type}"
    url = "https://config.example/{cli_output}"
    script = generate_lightweight_install_script(
        url, "legacy-test", "mihomo", config_path=path, restart_command=command,
    )
    config_text = script.split("cat > config.json << EOF\n", 1)[1].split("\nEOF", 1)[0]
    config = json.loads(config_text)
    assert config["config_path"] == path
    assert config["restart_command"] == command
    assert config["server_url"] == url
    assert script.count("\ncf_init() {") == 1
    generated = tmp_path / "install.sh"
    generated.write_text(script)
    syntax = subprocess.run(["sh", "-n", str(generated)], text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr


def _write_command(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _legacy_harness(tmp_path, manager="systemd"):
    source = generate_lightweight_install_script(
        "https://config.example/?token=never-log-registration-token",
        "legacy-test", "mihomo",
    )
    for original, replacement in (
        ("/opt/configflow-agent", str(tmp_path / "agent")),
        ("/var/log", str(tmp_path / "logs")),
        ("/etc/", str(tmp_path / "etc") + "/"),
        ("/run/", str(tmp_path / "run") + "/"),
        ("/tmp/", str(tmp_path / "tmp") + "/"),
        ("/dev/tty", "/dev/stdin"),
    ):
        source = source.replace(original, replacement)
    script = tmp_path / "install.sh"
    script.write_text(source, encoding="utf-8")
    for directory in ("etc/systemd/system", "etc/init.d", "etc/logrotate.d", "run", "tmp", "logs", "bin"):
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    distro = "alpine" if manager == "openrc" else "debian"
    (tmp_path / "etc/os-release").write_text(f'ID={distro}\nVERSION_ID=1\nNAME="Test Linux"\n')
    if manager == "openrc":
        (tmp_path / "etc/alpine-release").write_text("3.22\n")
    commands = tmp_path / "bin"
    _write_command(commands, "id", "printf '0\\n'\n")
    _write_command(commands, "systemctl", '''
printf 'systemctl %s\n' "$*" >> "$EVENTS"
case "$1" in
    start) printf 'SERVICE_DETAIL systemd start\n'; [ "${FAIL_START:-0}" = 0 ] || exit 42 ;;
    status) printf 'SERVICE_DETAIL systemd status\n' ;;
esac
exit 0
''')
    _write_command(commands, "rc-service", '''
printf 'rc-service %s\n' "$*" >> "$EVENTS"
case "$2" in
    start) printf 'SERVICE_DETAIL openrc start\n'; [ "${FAIL_START:-0}" = 0 ] || exit 42 ;;
    status) printf 'started\nSERVICE_DETAIL openrc status\n' ;;
esac
exit 0
''')
    for name in ("rc-update", "pkill", "sleep", "curl", "nc", "socat", "logrotate"):
        _write_command(commands, name, f'printf "{name} %s\\n" "$*" >> "$EVENTS"\nexit 0\n')
    env = os.environ.copy()
    env.update(
        PATH=f"{commands}{os.pathsep}{env['PATH']}",
        EVENTS=str(tmp_path / "events"),
        CONFIGFLOW_LOG_DIR=str(tmp_path / "logs"),
    )
    for name in ("CONFIGFLOW_CLI_ACTIVE", "CONFIGFLOW_LOG_CURSOR", "CONFIGFLOW_LOG_FILE", "CONFIGFLOW_VERBOSE"):
        env.pop(name, None)
    return script, env


def _run(script, env, choice="1", verbose=False):
    args = ["sh", str(script)] + (["--verbose"] if verbose else [])
    return subprocess.run(args, input=choice + "\n", env=env, text=True, capture_output=True, timeout=15)


def _log(tmp_path):
    logs = list((tmp_path / "logs").glob("configflow-install.*/install.log"))
    assert len(logs) == 1
    assert logs[0].parent.stat().st_mode & 0o777 == 0o700
    assert logs[0].stat().st_mode & 0o777 == 0o600
    return logs[0], logs[0].read_text(encoding="utf-8")


@pytest.mark.parametrize("manager", ["systemd", "openrc"])
@pytest.mark.parametrize("verbose", [False, True])
def test_legacy_install_has_concise_output_full_log_and_valid_generated_files(tmp_path, manager, verbose):
    script, env = _legacy_harness(tmp_path, manager)
    syntax = subprocess.run(["sh", "-n", str(script)], text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr
    result = _run(script, env, verbose=verbose)
    assert result.returncode == 0, result.stderr
    screen = result.stdout + result.stderr
    log_path, logs = _log(tmp_path)
    assert "Shell Agent 安装完成" in screen
    assert str(log_path) in screen
    assert ("SERVICE_DETAIL" in screen) is verbose
    assert "SERVICE_DETAIL" in logs
    assert "never-log-registration-token" not in screen + logs
    assert "bad substitution" not in (screen + logs).lower()
    assert "\x1b[" not in screen
    config = json.loads((tmp_path / "agent/config.json").read_text())
    assert config["service_type"] == "mihomo"
    assert config["restart_command"] == "systemctl restart mihomo"
    events = (tmp_path / "events").read_text()
    assert not re.search(r"(?:systemctl|rc-service).*\b(?:mihomo|mosdns|oxidns)\b", events)
    if manager == "openrc":
        init = tmp_path / "etc/init.d/configflow-agent"
        syntax = subprocess.run(["sh", "-n", str(init)], text=True, capture_output=True)
        assert syntax.returncode == 0, syntax.stderr
        assert "depend() {" in init.read_text()


def test_legacy_uninstall_keeps_operation_log_and_only_controls_agent(tmp_path):
    script, env = _legacy_harness(tmp_path)
    (tmp_path / "agent").mkdir()
    (tmp_path / "agent/config.json").write_text('{"token":"never-log-registration-token"}')
    result = _run(script, env, choice="2")
    assert result.returncode == 0, result.stderr
    log_path, logs = _log(tmp_path)
    assert "Shell Agent 卸载完成" in result.stdout + result.stderr
    assert str(log_path) in result.stdout
    assert not (tmp_path / "agent").exists()
    assert "never-log-registration-token" not in logs
    events = (tmp_path / "events").read_text()
    assert "systemctl stop configflow-agent" in events
    assert not re.search(r"(?:systemctl|rc-service).*\b(?:mihomo|mosdns|oxidns)\b", events)


def test_legacy_failure_preserves_exit_code_and_shows_command_detail(tmp_path):
    script, env = _legacy_harness(tmp_path)
    env["FAIL_START"] = "1"
    result = _run(script, env)
    assert result.returncode == 42
    assert "SERVICE_DETAIL systemd start" in result.stdout + result.stderr
    assert "退出码 42" in result.stdout + result.stderr
    assert "Shell Agent 安装完成" not in result.stdout + result.stderr
    _, logs = _log(tmp_path)
    assert "SERVICE_DETAIL systemd start" in logs


@pytest.mark.parametrize("verbose,exit_code", [(False, 0), (True, 0), (False, 37)])
def test_docker_run_uses_same_reporting_and_preserves_docker_exit(tmp_path, verbose, exit_code):
    script = tmp_path / "docker-agent.sh"
    script.write_text(generate_docker_agent_run("https://config.example", "docker-test"))
    commands = tmp_path / "bin"
    commands.mkdir()
    _write_command(commands, "docker", f"printf 'DOCKER_PROGRESS container-id\\n'\nexit {exit_code}\n")
    env = os.environ.copy()
    env.update(PATH=f"{commands}{os.pathsep}{env['PATH']}", CONFIGFLOW_LOG_DIR=str(tmp_path / "logs"))
    for name in ("CONFIGFLOW_CLI_ACTIVE", "CONFIGFLOW_LOG_CURSOR", "CONFIGFLOW_LOG_FILE", "CONFIGFLOW_VERBOSE"):
        env.pop(name, None)
    syntax = subprocess.run(["sh", "-n", str(script)], text=True, capture_output=True)
    assert syntax.returncode == 0, syntax.stderr
    result = _run(script, env, verbose=verbose)
    assert result.returncode == exit_code
    screen = result.stdout + result.stderr
    assert ("DOCKER_PROGRESS" in screen) is (verbose or exit_code != 0)
    log_path, logs = _log(tmp_path)
    assert "DOCKER_PROGRESS" in logs
    assert str(log_path) in screen


def test_daemon_http_handler_does_not_initialize_cli_or_pollute_health_response(tmp_path):
    source = (ROOT / "backend/agents/scripts/agent.sh").read_text()
    assert "cf_init" not in source
    source = source.replace('LOG_FILE="/var/log/configflow-agent.log"', f'LOG_FILE="{tmp_path / "daemon.log"}"')
    script = tmp_path / "agent.sh"
    script.write_text(source)
    result = subprocess.run(
        ["sh", str(script), "__handle_request__"],
        input="GET /health HTTP/1.1\r\n\r\n", text=True, capture_output=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("HTTP/1.1 200 OK\n")
    assert result.stdout.endswith('{"status":"ok"}')
    assert "完整日志" not in result.stdout
