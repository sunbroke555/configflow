import json

import pytest
import yaml
from flask import Flask

from backend.common.config_repository import ProfileRepository
from backend.common import config as config_module
from backend.routes import register_blueprints
from backend.routes.auth import setup_before_request


def make_app(repository, monkeypatch):
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    return app


def _rule(rule_id):
    return {"id": rule_id, "itemType": "rule", "rule_type": "DOMAIN", "value": f"{rule_id}.test", "policy": "DIRECT"}


def test_profile_context_prefers_route_over_query_and_header(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    for profile_id in ("alpha", "beta"):
        repository.create_profile({"id": profile_id, "name": profile_id})
        repository.save_profile(profile_id, {"rule_configs": [_rule(profile_id)]})
    app = make_app(repository, monkeypatch)

    response = app.test_client().get(
        "/api/profiles/beta/rules?profile=alpha",
        headers={"X-ConfigFlow-Profile": "missing"},
    )

    assert response.status_code == 200
    assert response.get_json()[0]["id"] == "beta"
    assert response.headers["X-ConfigFlow-Profile"] == "beta"


def test_query_precedes_header_and_selection_is_request_local(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("alpha", {"rule_configs": [_rule("alpha")]})
    client = make_app(repository, monkeypatch).test_client()

    selected = client.get("/api/rules?profile=alpha", headers={"X-ConfigFlow-Profile": "missing"})
    default = client.get("/api/rules")

    assert selected.status_code == 200
    assert selected.get_json()[0]["id"] == "alpha"
    assert default.get_json() == []
    assert default.headers["X-ConfigFlow-Profile"] == "default"


def test_profile_crud_clone_import_export_and_delete_rules(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({"subscriptions": [{"id": "sub-1", "name": "Shared", "url": "https://sub.test/list"}]})
    client = make_app(repository, monkeypatch).test_client()
    assert client.post("/api/profiles", json={"id": "alpha", "name": "Alpha"}).status_code == 201
    groups = [{"id": "shared", "name": "Shared", "type": "select", "subscriptions": ["sub-1"]}]
    repository.save_profile("alpha", {"proxy_groups": groups})

    assert client.post("/api/profiles/alpha/clone", json={"id": "beta", "name": "Beta"}).status_code == 201
    exported = client.get("/api/profiles/beta/export")
    assert exported.status_code == 200
    assert exported.get_json()["proxy_groups"] == groups
    assert "resource_refs" not in exported.get_json()
    assert "node_dialers" not in exported.get_json()
    assert "subscriptions" not in exported.get_json()

    imported = client.post("/api/profiles/beta/import", json={
        "proxy_groups": groups, "mihomo": {"custom_config": "port: 12345\n"},
    })
    assert imported.status_code == 200
    assert repository.get_profile("beta")["mihomo"]["custom_config"] == "port: 12345\n"
    assert repository.get_shared()["subscriptions"][0]["url"] == "https://sub.test/list"
    assert client.delete("/api/profiles/default").status_code == 409
    assert client.delete("/api/profiles/beta").status_code == 204
    assert not (tmp_path / "profiles" / "beta").exists()


@pytest.mark.parametrize("selection,status", [("../outside", 400), ("", 400), ("deleted-profile", 404)])
def test_invalid_profile_selection_never_falls_back(tmp_path, monkeypatch, selection, status):
    client = make_app(ProfileRepository(tmp_path), monkeypatch).test_client()
    response = client.get("/api/rules", headers={"X-ConfigFlow-Profile": selection})
    assert response.status_code == status
    query_response = client.get("/api/rules", query_string={"profile": selection}, headers={"X-ConfigFlow-Profile": "default"})
    assert query_response.status_code == status


def test_stale_profile_selection_does_not_block_global_operations(tmp_path, monkeypatch):
    client = make_app(ProfileRepository(tmp_path), monkeypatch).test_client()
    headers = {"X-ConfigFlow-Profile": "deleted-profile"}
    for path in ("/api/auth/status", "/api/profiles", "/api/subscriptions", "/api/nodes", "/api/rule-library", "/api/server-domain", "/api/config/export"):
        response = client.get(path, headers=headers, query_string={"profile": "../outside"})
        assert response.status_code == 200, (path, response.get_json())
        assert "X-ConfigFlow-Profile" not in response.headers
    assert client.get("/api/profiles/deleted-profile/proxy-groups", headers=headers).status_code == 404


def test_profile_config_and_generated_artifact_use_the_explicit_profile(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("alpha", {"mihomo": {"custom_config": "port: 12345\n"}})
    client = make_app(repository, monkeypatch).test_client()

    config_response = client.get("/api/config/alpha/mihomo", headers={"X-ConfigFlow-Profile": "missing"})
    generated_response = client.post("/api/profiles/alpha/generate/mihomo", json={})

    assert config_response.status_code == generated_response.status_code == 200
    assert yaml.safe_load(config_response.get_data())["port"] == 12345
    output = repository.generated_dir("alpha") / "config.yaml"
    assert yaml.safe_load(output.read_text(encoding="utf-8"))["port"] == 12345
    assert not (repository.generated_dir("default") / "config.yaml").exists()


def test_profile_import_does_not_overwrite_system_or_agents(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_system({"system_config": {"server_domain": "http://stable.test"}, "agents": [{"id": "agent-1", "profile_id": "default"}]})
    client = make_app(repository, monkeypatch).test_client()
    response = client.post("/api/profiles/alpha/import", json={
        "mihomo": {"custom_config": "port: 12345\n"},
        "system_config": {"server_domain": "http://attacker.test"},
        "agents": [{"id": "attacker"}],
    })

    assert response.status_code == 200
    assert repository.get_profile("alpha")["mihomo"]["custom_config"] == "port: 12345\n"
    system = repository.get_system()
    assert system["system_config"]["server_domain"] == "http://stable.test"
    assert system["agents"] == [{"id": "agent-1", "profile_id": "default"}]


def test_profile_config_url_is_public_when_config_token_is_valid(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_system({"system_config": {"config_token": "secret"}})
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: True)
    app = make_app(repository, monkeypatch)
    setup_before_request(app)
    assert app.test_client().get("/api/config/alpha/mihomo?token=secret").status_code == 200


def test_full_backup_contains_shared_data_and_all_profiles(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({"subscriptions": [{"id": "exported", "name": "Exported", "url": "https://sub.test"}]})
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    response = make_app(repository, monkeypatch).test_client().get("/api/config/export", headers={"X-ConfigFlow-Profile": "alpha"})
    assert response.status_code == 200
    exported = json.loads(response.get_data())
    assert exported["schema_version"] == 5
    assert exported["shared"]["subscriptions"][0]["id"] == "exported"
    assert set(exported["profiles"]) == {"default", "alpha"}


def test_legacy_config_url_defaults_to_default_profile(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("default", {"mihomo": {"custom_config": "port: 11111\n"}})
    repository.save_profile("alpha", {"mihomo": {"custom_config": "port: 22222\n"}})
    response = make_app(repository, monkeypatch).test_client().get("/api/config/mihomo")
    assert response.status_code == 200
    assert yaml.safe_load(response.get_data())["port"] == 11111


def test_load_config_uses_request_profile_context(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("alpha", {"mihomo": {"custom_config": "port: 22222\n"}})
    app = make_app(repository, monkeypatch)
    with app.test_request_context("/api/proxy-groups", headers={"X-ConfigFlow-Profile": "alpha"}):
        loaded = config_module.load_config()
    assert loaded["mihomo"]["custom_config"] == "port: 22222\n"


def test_each_request_reads_latest_shared_config(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({"subscriptions": [{"id": "before", "name": "Before", "url": "https://sub.test"}]})
    client = make_app(repository, monkeypatch).test_client()
    assert client.get("/api/subscriptions").get_json()[0]["id"] == "before"
    repository.save_shared({"subscriptions": [{"id": "after", "name": "After", "url": "https://sub.test"}]})
    assert client.get("/api/subscriptions").get_json()[0]["id"] == "after"


def test_incremental_config_transaction_uses_latest_disk_state(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    independent = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    config_module.get_config("default")
    independent.update_profile_transaction("default", lambda profile: profile["rule_configs"].append(_rule("independent")))
    config_module.update_config_transaction(lambda profile: profile["rule_configs"].append(_rule("route")), "default")
    assert repository.get_profile("default")["rule_configs"] == [_rule("independent"), _rule("route")]


def test_new_repository_keeps_existing_template_defaults(tmp_path, monkeypatch):
    template = tmp_path / "config_template.json"
    template.write_text(json.dumps({
        "subscriptions": [{"id": "template-sub", "name": "Template", "url": "https://sub.test"}],
        "mihomo": {"custom_config": "port: 12345\n"},
        "system_config": {"server_domain": "http://template.test"},
    }), encoding="utf-8")
    monkeypatch.setattr(config_module, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(config_module, "get_backend_resource", lambda _: str(template))
    monkeypatch.setattr(config_module, "_repository", None)
    repository = config_module.get_repository()
    assert repository.get_shared()["subscriptions"][0]["id"] == "template-sub"
    assert "resource_refs" not in repository.get_profile("default")
    assert repository.get_profile("default")["mihomo"]["custom_config"] == "port: 12345\n"
    assert "system_config" not in repository.get_profile("default")
    assert repository.get_system()["system_config"]["server_domain"] == "http://template.test"


@pytest.mark.parametrize("endpoint", ["resources", "node-dialers"])
@pytest.mark.parametrize("method", ["get", "put"])
def test_obsolete_profile_resource_and_node_dialer_routes_are_absent(tmp_path, monkeypatch, endpoint, method):
    repository = ProfileRepository(tmp_path)
    client = make_app(repository, monkeypatch).test_client()
    before = repository.export_all()
    response = getattr(client, method)(f"/api/profiles/default/{endpoint}", json={})
    assert response.status_code == 404
    assert repository.export_all() == before
