import copy
import logging

import pytest
from flask import Flask

from backend.agents.manager import AgentManager
from backend.common import config as config_module
from backend.common.agent_manager import init_agent_manager
from backend.common.auth import MAX_AUTH_TOKEN_LENGTH
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints


def _app(repository):
    config_module.set_repository(repository)
    init_agent_manager()
    app = Flask(__name__)
    register_blueprints(app)
    return app


def _registration_payload(**overrides):
    payload = {
        "name": "secure-agent",
        "host": "10.0.0.8",
        "port": 8080,
        "service_type": "mihomo",
        "version": "1.2.3",
    }
    payload.update(overrides)
    return payload


def test_manager_distinguishes_new_registration_from_authenticated_existing_registration(tmp_path):
    manager = AgentManager(ProfileRepository(tmp_path))

    created = manager.register_agent(_registration_payload())
    registered = manager.register_agent(
        _registration_payload(port=9090), existing_token=created["token"]
    )

    assert created["is_new"] is True
    assert created["token"]
    assert registered == {
        "id": created["id"],
        "status": "online",
        "is_new": False,
    }
    assert manager.get_agent_by_id(created["id"])["port"] == 9090


@pytest.mark.parametrize("provided_token", [None, "wrong-token", "令牌🔐"])
def test_manager_rejects_unauthenticated_existing_registration_without_mutation(
    tmp_path, provided_token
):
    repository = ProfileRepository(tmp_path)
    manager = AgentManager(repository)
    created = manager.register_agent(_registration_payload())
    before = copy.deepcopy(repository.get_system()["agents"])

    with pytest.raises(PermissionError):
        manager.register_agent(
            _registration_payload(port=9999, version="attacker"),
            existing_token=provided_token,
        )

    assert repository.get_system()["agents"] == before
    assert manager.get_agent_by_id(created["id"])["token"] == created["token"]


def test_first_registration_returns_token_and_persists_it(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_REGISTRATION_KEY", raising=False)
    repository = ProfileRepository(tmp_path)
    app = _app(repository)

    response = app.test_client().post("/api/agents/register", json=_registration_payload())

    assert response.status_code == 200
    body = response.get_json()
    assert set(body) == {"success", "id", "status", "is_new", "token"}
    assert body["success"] is True
    assert body["is_new"] is True
    assert body["token"]
    persisted = repository.get_system()["agents"][0]
    assert persisted["id"] == body["id"]
    assert persisted["token"] == body["token"]


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Bearer wrong-token",
        "Bearer wrong-token extra",
        "Bearer  wrong-token",
        "Bearer\twrong-token",
        "Bearer 令牌🔐",
        f"Bearer {'x' * (MAX_AUTH_TOKEN_LENGTH + 1)}",
    ],
    ids=["missing", "wrong", "trailing-field", "double-space", "tab", "unicode", "oversized"],
)
def test_duplicate_registration_requires_existing_bearer_and_does_not_mutate(
    tmp_path, monkeypatch, authorization
):
    monkeypatch.delenv("AGENT_REGISTRATION_KEY", raising=False)
    repository = ProfileRepository(tmp_path)
    app = _app(repository)
    client = app.test_client()
    created = client.post("/api/agents/register", json=_registration_payload()).get_json()
    before = copy.deepcopy(repository.get_system()["agents"])
    headers = {"Authorization": authorization} if authorization else {}

    response = client.post(
        "/api/agents/register",
        json=_registration_payload(port=9999, version="attacker"),
        headers=headers,
    )

    assert response.status_code == 401
    assert response.get_json() == {"success": False, "message": "Unauthorized"}
    assert repository.get_system()["agents"] == before
    assert repository.get_system()["agents"][0]["token"] == created["token"]


def test_authenticated_duplicate_registration_never_returns_token(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_REGISTRATION_KEY", raising=False)
    repository = ProfileRepository(tmp_path)
    app = _app(repository)
    client = app.test_client()
    created = client.post("/api/agents/register", json=_registration_payload()).get_json()

    response = client.post(
        "/api/agents/register",
        json=_registration_payload(port=9090),
        headers={"Authorization": f"Bearer {created['token']}"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "id": created["id"],
        "status": "online",
        "is_new": False,
    }
    assert "token" not in response.get_data(as_text=True)
    persisted = repository.get_system()["agents"][0]
    assert persisted["token"] == created["token"]
    assert persisted["port"] == 9090


def test_agent_list_and_item_never_expose_token(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_REGISTRATION_KEY", raising=False)
    monkeypatch.setenv("API_TOKEN", "admin-token")
    repository = ProfileRepository(tmp_path)
    app = _app(repository)
    client = app.test_client()
    created = client.post("/api/agents/register", json=_registration_payload()).get_json()
    auth = {"Authorization": "Bearer admin-token"}

    listed = client.get("/api/agents", headers=auth)
    item = client.get(f"/api/agents/{created['id']}", headers=auth)

    assert listed.status_code == 200
    assert item.status_code == 200
    assert "token" not in listed.get_data(as_text=True)
    assert "token" not in item.get_data(as_text=True)


def test_registration_key_does_not_bypass_existing_agent_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REGISTRATION_KEY", "valid-registration-key")
    repository = ProfileRepository(tmp_path)
    app = _app(repository)
    client = app.test_client()
    payload = _registration_payload(registration_key="valid-registration-key")
    created = client.post("/api/agents/register", json=payload).get_json()
    before = copy.deepcopy(repository.get_system()["agents"])

    response = client.post("/api/agents/register", json=payload)

    assert created["token"]
    assert response.status_code == 401
    assert repository.get_system()["agents"] == before


def test_host_matching_happens_after_real_client_ip_normalization(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_REGISTRATION_KEY", raising=False)
    repository = ProfileRepository(tmp_path)
    app = _app(repository)
    client = app.test_client()
    payload = _registration_payload(host="172.18.0.4")
    proxy_headers = {"X-Forwarded-For": "203.0.113.9, 172.18.0.1"}
    created = client.post(
        "/api/agents/register", json=payload, headers=proxy_headers
    ).get_json()

    rejected = client.post(
        "/api/agents/register", json=payload, headers=proxy_headers
    )
    accepted = client.post(
        "/api/agents/register",
        json=payload,
        headers={**proxy_headers, "Authorization": f"Bearer {created['token']}"},
    )

    assert repository.get_system()["agents"][0]["host"] == "203.0.113.9"
    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert "token" not in accepted.get_json()


def test_registration_logs_only_bounded_control_free_whitelisted_fields(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.delenv("AGENT_REGISTRATION_KEY", raising=False)
    repository = ProfileRepository(tmp_path)
    app = _app(repository)
    raw_secret = "raw-secret-8d2f"
    prefix_secret = "prefix-secret-4a6c"
    nested_secret = "nested-secret-1b9e"
    unicode_control_secret = "unicode-control-secret-3c7a"
    payload = _registration_payload(
        name="safe-agent-" + ("x" * 256) + prefix_secret,
        service_type="mihomo\r\n" + raw_secret,
        version="1.2.3\u2028" + unicode_control_secret,
        raw=raw_secret,
        prefix={"value": prefix_secret},
        nested={"authorization": {"token": nested_secret}},
    )

    with caplog.at_level(logging.INFO):
        response = app.test_client().post("/api/agents/register", json=payload)

    assert response.status_code == 200
    response_text = response.get_data(as_text=True)
    log_text = caplog.text
    for secret in (raw_secret, prefix_secret, nested_secret, unicode_control_secret):
        assert secret not in log_text
        assert secret not in response_text
    assert "safe-agent-" in log_text
    assert "mihomo" in log_text
    assert all("\r" not in record.getMessage() and "\n" not in record.getMessage() for record in caplog.records)
    assert "authorization" not in log_text


def test_duplicate_registration_attack_is_rejected_after_repository_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_REGISTRATION_KEY", raising=False)
    first_repository = ProfileRepository(tmp_path)
    first_client = _app(first_repository).test_client()
    created = first_client.post(
        "/api/agents/register", json=_registration_payload()
    ).get_json()

    restarted_repository = ProfileRepository(tmp_path)
    restarted_client = _app(restarted_repository).test_client()
    before = copy.deepcopy(restarted_repository.get_system()["agents"])
    attacked = restarted_client.post(
        "/api/agents/register",
        json=_registration_payload(port=9999, version="attacker"),
    )
    legitimate = restarted_client.post(
        "/api/agents/register",
        json=_registration_payload(port=9090),
        headers={"Authorization": f"Bearer {created['token']}"},
    )

    assert attacked.status_code == 401
    assert before[0]["port"] != 9999
    assert legitimate.status_code == 200
    assert legitimate.get_json()["id"] == created["id"]
    assert "token" not in legitimate.get_json()
    persisted = restarted_repository.get_system()["agents"][0]
    assert persisted["token"] == created["token"]
    assert persisted["port"] == 9090
