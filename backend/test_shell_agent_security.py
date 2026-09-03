import json
import os
import subprocess
import threading
from pathlib import Path

from flask import Flask
from werkzeug.serving import make_server

from backend.common import config as config_module
from backend.common.agent_manager import init_agent_manager
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints


SCRIPT = Path(__file__).parent / "agents" / "scripts" / "agent.sh"
GO_INSTALL_SCRIPT = Path(__file__).parent / "agents" / "scripts" / "install-go.sh"
GO_CLIENT = Path(__file__).parent / "agents" / "go-agent" / "client.go"
DOCKER_ENTRYPOINT = Path(__file__).parents[1] / "docker" / "docker-agent-entrypoint-service.sh"


def test_shell_agent_heartbeat_sends_real_token_but_only_logs_redacted_token(tmp_path):
    source = SCRIPT.read_text(encoding="utf-8")
    functions = source.split("# 备份配置", 1)[0]
    log_file = tmp_path / "agent.log"
    functions = functions.replace('LOG_FILE="/var/log/configflow-agent.log"', f'LOG_FILE="{log_file.as_posix()}"')

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_args = tmp_path / "curl-args.txt"
    (fake_bin / "systemctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (fake_bin / "curl").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CURL_ARGS_FILE"\nprintf "{}\\n200"\n',
        encoding="utf-8",
    )
    os.chmod(fake_bin / "systemctl", 0o755)
    os.chmod(fake_bin / "curl", 0o755)

    token = "real-token-$[]-with-specials"
    harness = tmp_path / "heartbeat-probe.sh"
    harness.write_text(
        functions
        + f"\nSERVER_URL='https://config.test'\nAGENT_ID='agent-1'\nTOKEN='{token}'\nSERVICE_NAME='mosdns'\nsend_heartbeat\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["CURL_ARGS_FILE"] = str(curl_args)
    result = subprocess.run(["sh", str(harness)], env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    recorded_args = curl_args.read_text(encoding="utf-8")
    assert token in recorded_args
    assert ("*" * 3) not in recorded_args
    logs = log_file.read_text(encoding="utf-8")
    assert ("*" * 3) in logs
    assert token not in logs
    assert token[:10] not in logs
    assert "bad substitution" not in result.stderr.lower()


def test_shell_agent_reregistration_sends_bearer_and_preserves_token_when_response_omits_it(tmp_path):
    source = SCRIPT.read_text(encoding="utf-8")
    functions = source.split("# 备份配置", 1)[0]
    log_file = tmp_path / "agent.log"
    config_file = tmp_path / "config.json"
    functions = functions.replace('LOG_FILE="/var/log/configflow-agent.log"', f'LOG_FILE="{log_file.as_posix()}"')
    functions = functions.replace('CONFIG_FILE="/opt/configflow-agent/config.json"', f'CONFIG_FILE="{config_file.as_posix()}"')
    token = "existing-registration-token"
    config_file.write_text(
        '{\n  "agent_id": "agent-1",\n  "token": "existing-registration-token"\n}\n',
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_args = tmp_path / "curl-args.txt"
    (fake_bin / "curl").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" > "$CURL_ARGS_FILE"\nprintf \'{"success":true,"id":"agent-1","status":"online","is_new":false}\\n200\'\n',
        encoding="utf-8",
    )
    os.chmod(fake_bin / "curl", 0o755)
    harness = tmp_path / "register-probe.sh"
    harness.write_text(
        functions
        + "\nSERVER_URL='https://config.test'\nAGENT_NAME='probe'\nAGENT_IP='10.0.0.8'\nAGENT_PORT=8080\nSERVICE_TYPE='mihomo'\n"
        + f"AGENT_ID='agent-1'\nTOKEN='{token}'\nregister_to_server\nprintf '%s' \"$TOKEN\" > '{(tmp_path / 'token-after.txt').as_posix()}'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["CURL_ARGS_FILE"] = str(curl_args)

    result = subprocess.run(["sh", str(harness)], env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    recorded_args = curl_args.read_text(encoding="utf-8")
    assert f"Authorization: Bearer {token}" in recorded_args
    assert (tmp_path / "token-after.txt").read_text(encoding="utf-8") == token
    assert token in config_file.read_text(encoding="utf-8")
    logs = log_file.read_text(encoding="utf-8")
    assert token not in logs
    assert "Authorization: Bearer ***" in logs


def test_shell_agent_first_registration_saves_returned_token_without_logging_it(tmp_path):
    source = SCRIPT.read_text(encoding="utf-8")
    functions = source.split("# 备份配置", 1)[0]
    log_file = tmp_path / "agent.log"
    config_file = tmp_path / "config.json"
    functions = functions.replace('LOG_FILE="/var/log/configflow-agent.log"', f'LOG_FILE="{log_file.as_posix()}"')
    functions = functions.replace('CONFIG_FILE="/opt/configflow-agent/config.json"', f'CONFIG_FILE="{config_file.as_posix()}"')
    config_file.write_text('{\n  "agent_name": "probe"\n}\n', encoding="utf-8")
    issued_token = "newly-issued-shell-token"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_args = tmp_path / "curl-args.txt"
    (fake_bin / "curl").write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > "$CURL_ARGS_FILE"\nprintf \'{{"success":true,"id":"agent-new","token":"{issued_token}"}}\\n200\'\n',
        encoding="utf-8",
    )
    os.chmod(fake_bin / "curl", 0o755)
    harness = tmp_path / "first-register-probe.sh"
    harness.write_text(
        functions
        + "\nSERVER_URL='https://config.test'\nAGENT_NAME='probe'\nAGENT_IP='10.0.0.9'\nAGENT_PORT=8080\nSERVICE_TYPE='mihomo'\nAGENT_ID=''\nTOKEN=''\n"
        + f"register_to_server\nprintf '%s' \"$TOKEN\" > '{(tmp_path / 'token-after.txt').as_posix()}'\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["CURL_ARGS_FILE"] = str(curl_args)

    result = subprocess.run(["sh", str(harness)], env=env, text=True, capture_output=True)

    assert result.returncode == 0, result.stderr
    recorded_args = curl_args.read_text(encoding="utf-8")
    assert "Authorization:" not in recorded_args
    assert (tmp_path / "token-after.txt").read_text(encoding="utf-8") == issued_token
    assert issued_token in config_file.read_text(encoding="utf-8")
    assert issued_token not in log_file.read_text(encoding="utf-8")


def test_shell_registers_against_real_flask_and_heartbeats_with_persisted_token(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_REGISTRATION_KEY", raising=False)
    repository = ProfileRepository(tmp_path / "repository")
    config_module.set_repository(repository)
    init_agent_manager()
    app = Flask(__name__)
    register_blueprints(app)
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    source = SCRIPT.read_text(encoding="utf-8")
    functions = source.split("# 备份配置", 1)[0]
    log_file = tmp_path / "agent.log"
    config_file = tmp_path / "config.json"
    functions = functions.replace('LOG_FILE="/var/log/configflow-agent.log"', f'LOG_FILE="{log_file.as_posix()}"')
    functions = functions.replace('CONFIG_FILE="/opt/configflow-agent/config.json"', f'CONFIG_FILE="{config_file.as_posix()}"')
    config_file.write_text(
        json.dumps(
            {
                "server_url": f"http://127.0.0.1:{server.server_port}",
                "agent_name": "real-shell-agent",
                "agent_host": "127.0.0.1",
                "agent_port": 8080,
                "agent_ip": "10.0.0.77",
                "service_type": "mihomo",
                "service_name": "definitely-not-running",
                "config_path": str(tmp_path / "service.yaml"),
                "restart_command": "true",
                "heartbeat_interval": 30,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    harness = tmp_path / "real-flask-probe.sh"
    harness.write_text(
        functions + "\nload_config\nregister_to_server\nsend_heartbeat\n",
        encoding="utf-8",
    )

    try:
        first = subprocess.run(["sh", str(harness)], text=True, capture_output=True)
        assert first.returncode == 0, first.stderr
        first_config = json.loads(config_file.read_text(encoding="utf-8"))
        issued_token = first_config["token"]
        assert issued_token and issued_token != "[REDACTED]"
        assert repository.get_system()["agents"][0]["token"] == issued_token

        second = subprocess.run(["sh", str(harness)], text=True, capture_output=True)
        assert second.returncode == 0, second.stderr
        second_config = json.loads(config_file.read_text(encoding="utf-8"))
        assert second_config["token"] == issued_token
        persisted = repository.get_system()["agents"][0]
        assert persisted["token"] == issued_token
        assert persisted["status"] == "online"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_shell_agent_does_not_interpolate_urls_or_request_paths_into_logs():
    source = SCRIPT.read_text(encoding="utf-8")

    forbidden = [
        'log "注册URL: $register_url"',
        "'$heartbeat_url'",
        'log "收到HTTP请求: $method $path"',
        'log_error "规则集下载失败: $item_url"',
        'url=$item_url',
    ]
    for fragment in forbidden:
        assert fragment not in source


def test_shell_installer_does_not_log_binary_download_url():
    source = GO_INSTALL_SCRIPT.read_text(encoding="utf-8")

    assert 'URL: $BINARY_URL' not in source


def test_shell_restart_logs_never_include_restart_url_or_command():
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'log "执行重启命令: $RESTART_CMD"' not in source
    assert 'curl -s' in source
    assert 'eval "$RESTART_CMD"' in source


def test_shell_restart_runtime_logs_only_fixed_descriptions(tmp_path):
    source = SCRIPT.read_text(encoding="utf-8")
    functions = source.split("# HTTP 服务器", 1)[0]
    log_file = tmp_path / "agent.log"
    functions = functions.replace('LOG_FILE="/var/log/configflow-agent.log"', f'LOG_FILE="{log_file.as_posix()}"')
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "curl").write_text('#!/bin/sh\nprintf "{}\\n200\\n"\n', encoding="utf-8")
    os.chmod(fake_bin / "curl", 0o755)
    harness = tmp_path / "restart-probe.sh"
    harness.write_text(
        functions + "\n"
        "RESTART_CMD='https://restart.test/restart?token=url-secret#key=fragment-secret'\n"
        "restart_service\n"
        "RESTART_CMD='true # command-secret'\n"
        "restart_service\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    result = subprocess.run(["sh", str(harness)], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    logs = log_file.read_text(encoding="utf-8")
    for secret in ("url-secret", "fragment-secret", "command-secret", "restart.test"):
        assert secret not in logs
    assert "执行服务重启操作" in logs
    assert "检测到 URL，使用 curl 发送重启请求" in logs
    assert "检测到命令，直接执行" in logs


def test_shell_agent_has_portable_syntax():
    result = subprocess.run(["sh", "-n", str(SCRIPT)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_go_agent_heartbeat_sends_configured_bearer_token():
    source = GO_CLIENT.read_text(encoding="utf-8")
    assert 'req.Header.Set("Authorization", "Bearer "+c.Token)' in source


def test_docker_entrypoint_preserves_registered_agent_identity(tmp_path):
    source = DOCKER_ENTRYPOINT.read_text(encoding="utf-8")
    functions = source.split("# 主逻辑", 1)[0]
    functions = functions.replace(
        'AGENT_DIR="/opt/configflow-agent"',
        'AGENT_DIR="$TEST_AGENT_DIR"',
    )
    config_dir = tmp_path / "agent-state"
    config_dir.mkdir()
    config_file = config_dir / "config-mihomo.json"
    config_file.write_text(
        json.dumps({
            "server_url": "http://old-server",
            "agent_name": "old-name",
            "agent_id": "existing-agent-id",
            "token": "existing-agent-token",
        }),
        encoding="utf-8",
    )
    harness = tmp_path / "entrypoint-test.sh"
    harness.write_text(
        functions
        + "\nSERVER_URL='http://new-server'\n"
        + "HEARTBEAT_INTERVAL=5\n"
        + "generate_agent_config mihomo new-name 18080 /etc/mihomo/config.yaml\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["TEST_AGENT_DIR"] = str(config_dir)
    result = subprocess.run(["sh", str(harness)], env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    saved = json.loads(config_file.read_text(encoding="utf-8"))
    assert saved["agent_id"] == "existing-agent-id"
    assert saved["token"] == "existing-agent-token"
    assert saved["server_url"] == "http://new-server"
    assert saved["agent_name"] == "new-name"
