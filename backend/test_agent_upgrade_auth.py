"""Exercise upgrade authentication through the production app's request hooks."""
import copy
import json
import runpy
import time

import pytest
import requests

from backend.agents import upgrades
from backend.common import auth as auth_module
from backend.common import config as config_module
from backend.common.agent_manager import get_agent_manager
from backend.common.config_repository import ProfileRepository
from backend.routes import auth as auth_routes


@pytest.fixture
def authenticated_app(tmp_path, monkeypatch):
    # Run backend.app itself: blueprint-only tests omit the global JWT gate.
    for module in (auth_module, auth_routes):
        monkeypatch.setattr(module, "ADMIN_USERNAME", "upgrade-admin")
        monkeypatch.setattr(module, "ADMIN_PASSWORD", "upgrade-test-password")
    monkeypatch.setattr(config_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "app.log"))
    repository = ProfileRepository(tmp_path)
    repository.update_system_transaction(
        lambda system: system["system_config"].update(server_domain="http://localhost")
    )
    monkeypatch.setattr(config_module, "_repository", repository)
    config_module.reset_config_context()
    artifacts = tmp_path / "agents"
    artifacts.mkdir()
    for arch in upgrades.ARCHES:
        (artifacts / ("configflow-agent-" + arch)).write_bytes(b"\x7fELF" + arch.encode())
    monkeypatch.setenv("AGENTS_STATIC_DIR", str(artifacts))
    application = runpy.run_module("backend.app", run_name="upgrade_auth_test_app")
    client = application["app"].test_client()
    assert client.get("/api/auth/status").get_json() == {"authEnabled": True}
    login = client.post("/api/auth/login", json={
        "username": "upgrade-admin", "password": "upgrade-test-password",
    })
    assert login.status_code == 200
    admin = {"Authorization": "Bearer " + login.get_json()["token"]}
    manager = get_agent_manager()
    agents = [manager.register_agent({
        "name": name, "host": "127.0.0.1", "port": 1234,
        "service_type": "mihomo", "deployment_method": "shell", "version": "1.1.0-go",
    }) for name in ("upgrading", "other-agent")]
    yield client, manager, agents, admin, artifacts
    application["file_handler"].close()
    application["console_handler"].close()
    config_module.reset_config_context()


def seed_upgrade(manager, agent):
    state = {"update_id": "a" * 32, "agent_id": agent["id"], "protocol": 1,
             "target_version": upgrades.get_latest_version(), "previous_version": "1.1.0-go",
             "status": "downloading", "started_at": time.time(), "updated_at": time.time()}
    manager._update_agents(lambda agents: next(
        item for item in agents if item["id"] == agent["id"]
    ).update(latest_upgrade=state))
    return state


def agent_response(payload, status=200):
    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(payload).encode()
    response._content_consumed = True
    return response


@pytest.mark.parametrize("arch", upgrades.ARCHES)
def test_agent_binary_download_does_not_require_admin_jwt(authenticated_app, arch):
    client, _, _, _, artifacts = authenticated_app
    filename = "configflow-agent-" + arch
    response = client.get("/api/agents/download/" + filename)
    assert response.status_code == 200
    assert response.data == (artifacts / filename).read_bytes()
    assert response.mimetype == "application/octet-stream"
    assert "attachment" in response.headers["Content-Disposition"]


def test_public_download_keeps_filename_allowlist(authenticated_app):
    client, _, _, _, artifacts = authenticated_app
    (artifacts / "private-config.json").write_text("not a public binary")
    response = client.get("/api/agents/download/private-config.json")
    assert response.status_code == 404
    assert b"not a public binary" not in response.data


def test_legacy_agent_can_read_and_report_own_upgrade_then_admin_confirms_rollback(
    authenticated_app, monkeypatch
):
    client, manager, (agent, other), admin, _ = authenticated_app
    state = seed_upgrade(manager, agent)
    headers = {"Authorization": "Bearer " + agent["token"]}
    path = "/api/agents/" + agent["id"] + "/upgrade"
    # A bootstrap read must not recursively poll the not-yet-started Agent.
    monkeypatch.setattr(upgrades, "_request", lambda *a, **kw: pytest.fail("unexpected Agent call"))
    response = client.get(path, headers=headers)
    assert response.status_code == 200
    assert response.get_json()["update_id"] == state["update_id"]
    response = client.post(path + "/report", headers=headers, json={
        "update_id": state["update_id"], "status": "rolling_back", "error": "migration failed",
    })
    assert response.status_code == 200
    assert response.get_json()["status"] == "rolling_back"
    assert "latest_upgrade" not in manager.get_agent_by_id(other["id"])
    response = client.post("/api/agents/" + agent["id"] + "/heartbeat", headers=headers,
                           json={"version": "1.1.0-go"})
    assert response.status_code == 200
    response = client.get(path, headers=admin)
    assert response.status_code == 200
    assert response.get_json()["status"] == "rolled_back"
    for secret in (agent["token"], other["token"]):
        assert secret not in response.get_data(as_text=True)


@pytest.mark.parametrize("credential", ["missing", "wrong", "other", "malformed"])
@pytest.mark.parametrize("operation", ["read", "report"])
def test_upgrade_routes_reject_missing_or_foreign_agent_credentials(
    authenticated_app, credential, operation
):
    client, manager, (agent, other), _, _ = authenticated_app
    state = seed_upgrade(manager, agent)
    before = copy.deepcopy(manager.repository.get_system())
    headers = {
        "missing": {}, "wrong": {"Authorization": "Bearer wrong-token"},
        "other": {"Authorization": "Bearer " + other["token"]},
        "malformed": {"Authorization": "Bearer " + agent["token"] + " extra"},
    }[credential]
    path = "/api/agents/" + agent["id"] + "/upgrade"
    response = client.get(path, headers=headers) if operation == "read" else client.post(
        path + "/report", headers=headers,
        json={"update_id": state["update_id"], "status": "rolling_back"},
    )
    assert response.status_code == 401
    assert manager.repository.get_system() == before


def test_admin_jwt_cannot_impersonate_agent_rollback_report(authenticated_app):
    client, manager, (agent, _), admin, _ = authenticated_app
    state = seed_upgrade(manager, agent)
    response = client.post("/api/agents/" + agent["id"] + "/upgrade/report", headers=admin,
                           json={"update_id": state["update_id"], "status": "rolling_back"})
    assert response.status_code == 401
    assert manager.get_agent_by_id(agent["id"])["latest_upgrade"] == state


@pytest.mark.parametrize("payload", [
    {"update_id": "wrong-task", "status": "rolling_back"},
    {"update_id": "a" * 32, "status": "succeeded"},
])
def test_authenticated_report_still_requires_matching_legacy_task(authenticated_app, payload):
    client, manager, (agent, _), _, _ = authenticated_app
    state = seed_upgrade(manager, agent)
    response = client.post("/api/agents/" + agent["id"] + "/upgrade/report",
                           headers={"Authorization": "Bearer " + agent["token"]}, json=payload)
    assert response.status_code == 400
    assert manager.get_agent_by_id(agent["id"])["latest_upgrade"] == state


@pytest.mark.parametrize("credential", ["missing", "agent"])
@pytest.mark.parametrize(("method", "suffix"), [
    ("GET", ""), ("GET", "/{id}"), ("POST", "/{id}/update"),
    ("POST", "/{id}/push-config"), ("POST", "/{id}/restart"),
    ("POST", "/{id}/uninstall"), ("DELETE", "/{id}"),
    ("GET", "/{id}/upgrade-extra"), ("GET", "/download-extra/configflow-agent-linux-amd64"),
])
def test_upgrade_exemptions_do_not_open_agent_administration(
    authenticated_app, credential, method, suffix
):
    client, manager, (agent, _), _, _ = authenticated_app
    before = copy.deepcopy(manager.repository.get_system())
    headers = {} if credential == "missing" else {"Authorization": "Bearer " + agent["token"]}
    response = client.open("/api/agents" + suffix.format(id=agent["id"]),
                           method=method, headers=headers)
    assert response.status_code == 401
    assert manager.repository.get_system() == before


def test_admin_can_still_read_agents_and_upgrade_status(authenticated_app):
    client, manager, (agent, _), admin, _ = authenticated_app
    state = seed_upgrade(manager, agent)
    manager._update_agents(lambda agents: next(
        item for item in agents if item["id"] == agent["id"]
    )["latest_upgrade"].update(status="failed"))
    assert client.get("/api/agents", headers=admin).status_code == 200
    response = client.get("/api/agents/" + agent["id"] + "/upgrade", headers=admin)
    assert response.status_code == 200
    assert response.get_json()["update_id"] == state["update_id"]
    assert response.get_json()["status"] == "failed"


@pytest.mark.parametrize("protocol", [1, 2])
def test_admin_starts_upgrade_and_both_agent_protocols_can_download(
    authenticated_app, monkeypatch, protocol
):
    client, manager, (agent, _), admin, artifacts = authenticated_app
    submitted = []

    def agent_request(target, method, path, **kwargs):
        assert target["id"] == agent["id"]
        if method == "GET" and path == "/api/upgrade-info":
            return agent_response({"agent_id": agent["id"], "deployment_method": "shell",
                                   "architecture": "linux-amd64"}, 200 if protocol == 2 else 404)
        assert (method, path) == ("POST", "/api/update")
        submitted.append(kwargs["json"])
        return agent_response({}, 202 if protocol == 2 else 200)

    monkeypatch.setattr(upgrades, "_request", agent_request)
    response = client.post("/api/agents/" + agent["id"] + "/update", headers=admin, json={})
    assert response.status_code == 202
    state = response.get_json()
    assert state["status"] == "queued"  # The HTTP receipt does not prove a download started.
    assert state["protocol"] == protocol
    assert submitted[0]["update_id"] == state["update_id"]
    assert submitted[0]["version"] == upgrades.get_latest_version()
    if protocol == 2:
        assert submitted[0]["sha256"] == state["artifacts"]["linux-amd64"]["sha256"]
    download = client.get("/api/agents/download/configflow-agent-linux-amd64")
    assert download.status_code == 200
    assert download.data == (artifacts / "configflow-agent-linux-amd64").read_bytes()


@pytest.mark.parametrize("status", ["checking", "unknown", "rollback_failed", "succeeded"])
def test_reregister_preserves_upgrade_record_and_pending_operation_lock(authenticated_app, status):
    client, manager, (agent, _), admin, _ = authenticated_app
    state = seed_upgrade(manager, agent)
    state["status"] = status
    manager._update_agents(lambda agents: next(
        item for item in agents if item["id"] == agent["id"]
    ).update(latest_upgrade=copy.deepcopy(state)))
    headers = {"Authorization": "Bearer " + agent["token"]}
    response = client.post("/api/agents/register", headers=headers, json={
        "name": "upgrading", "host": "127.0.0.1", "port": 1234,
        "service_type": "mihomo", "deployment_method": "shell", "version": upgrades.get_latest_version(),
    })
    assert response.status_code == 200
    assert response.get_json()["id"] == agent["id"]
    registered = manager.get_agent_by_id(agent["id"])
    assert registered["latest_upgrade"] == state
    assert registered["token"] == agent["token"]
    assert registered["version"] == upgrades.get_latest_version()
    if status != "succeeded":
        # Re-registration cannot unlock a pending/unknown/failed-rollback task.
        assert client.post("/api/agents/" + agent["id"] + "/push-config", headers=admin,
                           json={"deployment_id": "b" * 32}).status_code == 409
        assert client.delete("/api/agents/" + agent["id"], headers=admin).status_code == 409
        assert manager.get_agent_by_id(agent["id"])["latest_upgrade"] == state


def test_reregistered_new_agent_can_confirm_preserved_upgrade(authenticated_app, monkeypatch):
    client, manager, (agent, _), admin, _ = authenticated_app
    state = seed_upgrade(manager, agent)
    response = client.post("/api/agents/register", headers={"Authorization": "Bearer " + agent["token"]},
                           json={"name": "upgrading", "host": "127.0.0.1", "port": 1234,
                                 "service_type": "mihomo", "deployment_method": "shell",
                                 "version": upgrades.get_latest_version()})
    assert response.status_code == 200
    monkeypatch.setattr(upgrades, "_request", lambda *a, **kw: agent_response({
        "agent_id": agent["id"], "version": state["target_version"], "migration_ready": True,
    }))
    response = client.get("/api/agents/" + agent["id"] + "/upgrade", headers=admin)
    assert response.status_code == 200
    assert response.get_json()["status"] == "succeeded"
    assert response.get_json()["update_id"] == state["update_id"]
    assert manager.get_agent_by_id(agent["id"])["latest_upgrade"]["status"] == "succeeded"
