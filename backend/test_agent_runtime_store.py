"""Heartbeats are recorded beside config.json and laid over the registered Agent."""
from datetime import datetime, timedelta
import os
import stat

from flask import Flask

from backend.common import config as config_module
from backend.common.agent_manager import init_agent_manager
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints

METRICS = {
    "cpu": {"usage_percent": 12.5, "core_count": 4},
    "memory": {"total": 1024, "used": 512, "available": 512, "used_percent": 50.0},
    "disk": {"total": 2048, "used": 1024, "free": 1024, "used_percent": 50.0},
    "network": {"bytes_sent": 10, "bytes_recv": 20, "speed_sent": 1, "speed_recv": 2},
}


def setup(tmp_path):
    repository = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    manager = init_agent_manager()
    agent = manager.register_agent({"name": "edge", "host": "192.0.2.10", "version": "1.0.0"})
    return repository, manager, agent


def stored(repository, agent_id):
    return next(item for item in repository.get_system()["agents"] if item["id"] == agent_id)


def test_heartbeat_leaves_config_json_alone_and_is_visible_through_the_manager(tmp_path):
    repository, manager, agent = setup(tmp_path)
    before, revision = (tmp_path / "config.json").read_bytes(), repository.get_system()["_revision"]

    assert manager.update_heartbeat(agent["id"], {"version": "2.0.0", "service_status": "running", "system_metrics": METRICS})

    assert (tmp_path / "config.json").read_bytes() == before
    assert repository.get_system()["_revision"] == revision
    for current in (manager.get_agent_by_id(agent["id"]), manager.get_all_agents()[0]):
        assert current["status"] == "online"
        assert current["version"] == "2.0.0" and current["service_status"] == "running"
        assert current["system_metrics"] == METRICS
        assert {key: current[key] for key in ("cpu", "memory", "disk", "network")} == {key: METRICS[key] for key in ("cpu", "memory", "disk", "network")}
    assert stat.S_IMODE(os.stat(tmp_path / "runtime.sqlite3").st_mode) == 0o600


def test_partial_heartbeats_keep_earlier_values_and_inherit_registered_state(tmp_path):
    repository, manager, agent = setup(tmp_path)
    repository.update_system_transaction(lambda system: system["agents"][0].update(service_status="running"))
    stamp = datetime.fromisoformat(manager.get_agent_by_id(agent["id"])["last_heartbeat"])

    assert manager.update_heartbeat(agent["id"], {})
    first = manager.get_agent_by_id(agent["id"])
    # The first heartbeat starts from what config.json already held.
    assert first["version"] == "1.0.0" and first["service_status"] == "running"
    assert datetime.fromisoformat(first["last_heartbeat"]) >= stamp

    assert manager.update_heartbeat(agent["id"], {"system_metrics": METRICS})
    assert manager.update_heartbeat(agent["id"], {"version": "2.0.0"})
    current = manager.get_agent_by_id(agent["id"])
    assert current["version"] == "2.0.0" and current["system_metrics"] == METRICS


def test_re_registration_wins_until_the_next_heartbeat(tmp_path):
    _, manager, agent = setup(tmp_path)
    assert manager.update_heartbeat(agent["id"], {"version": "2.0.0", "system_metrics": METRICS})

    manager.register_agent({"name": "edge", "host": "192.0.2.10", "version": "3.0.0"}, existing_token=agent["token"])
    current = manager.get_agent_by_id(agent["id"])
    assert current["version"] == "3.0.0" and "system_metrics" not in current

    # The next heartbeat builds on the registration, not on the older stored metrics.
    assert manager.update_heartbeat(agent["id"], {})
    current = manager.get_agent_by_id(agent["id"])
    assert current["version"] == "3.0.0" and "system_metrics" not in current


def test_config_version_stays_in_config_json_and_is_written_only_when_changed(tmp_path):
    repository, manager, agent = setup(tmp_path)
    revision = repository.get_system()["_revision"]
    assert manager.update_heartbeat(agent["id"], {"config_version": stored(repository, agent["id"])["config_version"]})
    assert repository.get_system()["_revision"] == revision
    assert manager.update_heartbeat(agent["id"], {"config_version": "cfg-2"})
    assert stored(repository, agent["id"])["config_version"] == "cfg-2"


def test_unknown_agent_writes_nothing(tmp_path):
    repository, manager, _ = setup(tmp_path)
    before = (tmp_path / "config.json").read_bytes()
    assert not manager.update_heartbeat("missing", {"version": "2.0.0"})
    assert (tmp_path / "config.json").read_bytes() == before
    assert manager.runtime._rows() == {}


def test_deleted_agents_lose_their_state_and_stray_rows_are_never_shown(tmp_path):
    _, manager, agent = setup(tmp_path)
    assert manager.update_heartbeat(agent["id"], {"version": "2.0.0"})
    manager.runtime.record_heartbeat({"id": "stray"}, {"version": "9"})
    assert [item["id"] for item in manager.get_all_agents()] == [agent["id"]]
    assert manager.delete_agent(agent["id"])
    assert set(manager.runtime._rows()) == {"stray"}
    assert manager.get_all_agents() == []


def test_each_repository_keeps_its_own_runtime_database(tmp_path):
    _, first, agent = setup(tmp_path / "one")
    assert first.update_heartbeat(agent["id"], {"version": "2.0.0"})
    _, second, other = setup(tmp_path / "two")
    assert second.runtime._rows() == {}
    assert second.get_agent_by_id(other["id"])["version"] == "1.0.0"


def test_domain_discovery_toggle_returns_the_live_agent(tmp_path):
    _, manager, agent = setup(tmp_path)
    assert manager.update_heartbeat(agent["id"], {"version": "2.0.0", "system_metrics": METRICS})
    updated = manager.set_domain_discovery(agent["id"], True)
    assert updated["version"] == "2.0.0" and updated["system_metrics"] == METRICS and updated["status"] == "online"


def test_newer_heartbeat_in_config_json_wins_after_running_older_code(tmp_path):
    repository, manager, agent = setup(tmp_path)
    assert manager.update_heartbeat(agent["id"], {"version": "2.0.0"})
    later = (datetime.now() + timedelta(seconds=5)).isoformat()

    def older_code_heartbeat(system):
        for item in system["agents"]:
            item.update(last_heartbeat=later, version="1.5.0")
    repository.update_system_transaction(older_code_heartbeat)

    current = manager.get_agent_by_id(agent["id"])
    assert current["version"] == "1.5.0" and current["last_heartbeat"] == later


def test_stale_heartbeat_reads_as_offline(tmp_path):
    _, manager, agent = setup(tmp_path)
    assert manager.update_heartbeat(agent["id"], {})
    with manager.runtime._database() as connection:
        connection.execute("UPDATE agent_runtime SET heartbeat_at = heartbeat_at + 1, last_heartbeat = ?",
                           ((datetime.now() - timedelta(minutes=5)).isoformat(),))
    assert manager.get_all_agents()[0]["status"] == "offline"


def test_a_damaged_runtime_database_falls_back_and_is_rebuilt(tmp_path):
    _, manager, agent = setup(tmp_path)
    assert manager.update_heartbeat(agent["id"], {"version": "2.0.0"})
    (tmp_path / "runtime.sqlite3").write_bytes(b"not a database" * 100)
    assert manager.get_agent_by_id(agent["id"])["version"] == "1.0.0"
    assert manager.update_heartbeat(agent["id"], {"version": "2.1.0"})
    assert manager.get_agent_by_id(agent["id"])["version"] == "2.1.0"
    assert list(tmp_path.glob("runtime.sqlite3.corrupt-*"))


def test_heartbeat_endpoint_records_state_and_rejects_unknown_agents(tmp_path):
    repository, manager, agent = setup(tmp_path)
    app = Flask(__name__)
    register_blueprints(app)
    client = app.test_client()
    headers = {"Authorization": f"Bearer {agent['token']}"}
    assert client.post(f"/api/agents/{agent['id']}/heartbeat", headers=headers, json={"version": "2.0.0"}).status_code == 200
    assert manager.get_agent_by_id(agent["id"])["version"] == "2.0.0"
    assert stored(repository, agent["id"])["version"] == "1.0.0"
    assert client.post("/api/agents/missing/heartbeat", headers=headers, json={}).status_code in (401, 404)
