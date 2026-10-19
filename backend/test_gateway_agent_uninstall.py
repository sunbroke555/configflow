"""Execute native uninstall menus without touching the host or losing gateway journals."""
from pathlib import Path
import subprocess

import pytest

from backend.test_agent_native_aio_install import harness as aio_harness
from backend.test_agent_transaction_install import native_install_harness


def uninstall_harness(tmp_path, manager, mode):
    if mode == "aio":
        script, env, _ = aio_harness(tmp_path, manager)
        directory = tmp_path / "agent/mihomo"
        agent_unit = "configflow-agent-mihomo"
    else:
        script, env, _, _, _, _, _ = native_install_harness(tmp_path, manager, "mihomo")
        directory = tmp_path / "agent"
        agent_unit = "configflow-agent"
    env["CONFIGFLOW_INSTALL_LOG_DIR"] = str(tmp_path)
    return script, env, directory, agent_unit


def run_uninstall(script, env):
    return subprocess.run(["sh", str(script)], input="2\n", env=env, text=True, capture_output=True)


@pytest.mark.parametrize("mode", ["single", "aio"])
@pytest.mark.parametrize("manager", ["systemd", "openrc"])
@pytest.mark.parametrize("journal_kind", ["active", "damaged", "dangling-link"])
def test_gateway_journal_blocks_uninstall_before_stopping_agents(tmp_path, manager, mode, journal_kind):
    script, env, directory, _ = uninstall_harness(tmp_path, manager, mode)
    journal = directory / "linux-gateway.json"
    if journal_kind == "dangling-link":
        journal.symlink_to(directory / "missing-journal")
    else:
        journal.write_text('{"active":true}' if journal_kind == "active" else "broken journal")
    before = {path: path.read_bytes() for path in (tmp_path / "agent").rglob("*")
              if path.is_file() and not path.is_symlink()}
    result = run_uninstall(script, env)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "网关恢复记录未清理" in result.stderr
    assert journal.exists() or journal.is_symlink()
    assert all(path.read_bytes() == data for path, data in before.items())
    events = Path(env["EVENTS"])
    assert not events.exists() or "stop" not in events.read_text()


@pytest.mark.parametrize("mode", ["single", "aio"])
@pytest.mark.parametrize("manager", ["systemd", "openrc"])
def test_gateway_created_during_stop_preserves_agents_and_restarts_them(tmp_path, manager, mode):
    script, env, directory, agent_unit = uninstall_harness(tmp_path, manager, mode)
    journal = directory / "linux-gateway.json"
    command = tmp_path / "commands" / ("systemctl" if manager == "systemd" else "rc-service")
    source = command.read_text()
    stop = f"stop {agent_unit}" if manager == "systemd" else f"{agent_unit} stop"
    source += f'\nif [ "$*" = "{stop}" ]; then printf active > "$GATEWAY_JOURNAL"; fi\n'
    command.write_text(source)
    env["GATEWAY_JOURNAL"] = str(journal)
    before = {path: path.read_bytes() for path in (tmp_path / "agent").rglob("*") if path.is_file()}
    result = run_uninstall(script, env)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "网关恢复记录" in result.stderr
    assert journal.read_text() == "active"
    assert all(path.read_bytes() == data for path, data in before.items())
    events = Path(env["EVENTS"]).read_text()
    start = f"start {agent_unit}" if manager == "systemd" else f"{agent_unit} start"
    assert stop in events and start in events
    assert f"disable {agent_unit}" not in events
    assert f"del {agent_unit}" not in events


@pytest.mark.parametrize("mode", ["single", "aio"])
@pytest.mark.parametrize("manager", ["systemd", "openrc"])
def test_ordinary_uninstall_without_gateway_journal_still_completes(tmp_path, manager, mode):
    script, env, directory, agent_unit = uninstall_harness(tmp_path, manager, mode)
    result = run_uninstall(script, env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not directory.exists()
    events = Path(env["EVENTS"]).read_text()
    removed = f"disable {agent_unit}" if manager == "systemd" else f"del {agent_unit}"
    assert removed in events
    if mode == "aio":
        assert not (tmp_path / "agent/oxidns").exists()
