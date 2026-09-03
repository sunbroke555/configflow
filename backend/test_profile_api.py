import json

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


def test_profile_context_prefers_route_over_header_and_query(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.create_profile({"id": "beta", "name": "Beta"})
    repository.save_profile("alpha", {"subscriptions": [{"id": "alpha"}]})
    repository.save_profile("beta", {"subscriptions": [{"id": "beta"}]})
    app = make_app(repository, monkeypatch)
    client = app.test_client()

    response = client.get(
        "/api/profiles/beta/subscriptions?profile=alpha",
        headers={"X-ConfigFlow-Profile": "alpha"},
    )

    assert response.status_code == 200
    assert response.get_json()[0]["id"] == "beta"


def test_legacy_routes_use_active_profile_and_header_is_request_scoped(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("alpha", {"subscriptions": [{"id": "alpha"}]})
    repository.activate_profile("alpha")
    app = make_app(repository, monkeypatch)
    client = app.test_client()

    active_response = client.get("/api/subscriptions")
    explicit_response = client.get("/api/subscriptions", headers={"X-ConfigFlow-Profile": "default"})

    assert active_response.get_json()[0]["id"] == "alpha"
    assert explicit_response.get_json() == []
    assert repository.active_profile_id() == "alpha"


def test_profile_crud_clone_import_export_and_delete_rules(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    app = make_app(repository, monkeypatch)
    client = app.test_client()

    created = client.post("/api/profiles", json={"id": "alpha", "name": "Alpha"})
    assert created.status_code == 201
    repository.save_profile("alpha", {"subscriptions": [{"id": "sub-alpha"}]})

    cloned = client.post("/api/profiles/alpha/clone", json={"id": "beta", "name": "Beta"})
    assert cloned.status_code == 201
    assert repository.get_profile("beta")["subscriptions"] == [{"id": "sub-alpha"}]

    exported = client.get("/api/profiles/beta/export")
    assert exported.status_code == 200
    assert exported.get_json()["subscriptions"] == [{"id": "sub-alpha"}]

    imported = client.post(
        "/api/profiles/beta/import",
        json={"subscriptions": [{"id": "sub-imported"}]},
    )
    assert imported.status_code == 200
    assert repository.get_profile("beta")["subscriptions"] == [{"id": "sub-imported"}]

    assert client.delete("/api/profiles/default").status_code == 400
    assert client.delete("/api/profiles/beta").status_code == 204
    assert not (tmp_path / "profiles" / "beta").exists()


def test_invalid_profile_context_returns_not_found(tmp_path, monkeypatch):
    app = make_app(ProfileRepository(tmp_path), monkeypatch)

    response = app.test_client().get(
        "/api/subscriptions",
        headers={"X-ConfigFlow-Profile": "../outside"},
    )

    assert response.status_code == 400


def test_stale_profile_header_does_not_block_auth_or_profile_management(tmp_path, monkeypatch):
    app = make_app(ProfileRepository(tmp_path), monkeypatch)
    client = app.test_client()
    headers = {"X-ConfigFlow-Profile": "deleted-profile"}

    assert client.get("/api/auth/status", headers=headers).status_code == 200
    assert client.post(
        "/api/auth/login",
        headers=headers,
        json={"username": "nobody", "password": "wrong"},
    ).status_code != 404
    profiles_response = client.get("/api/profiles", headers=headers)
    assert profiles_response.status_code == 200
    assert profiles_response.get_json()[0]["id"] == "default"


def test_stale_profile_header_returns_clear_not_found_for_profile_resources(tmp_path, monkeypatch):
    app = make_app(ProfileRepository(tmp_path), monkeypatch)

    response = app.test_client().get(
        "/api/subscriptions",
        headers={"X-ConfigFlow-Profile": "deleted-profile"},
    )

    assert response.status_code == 404
    assert response.get_json()["message"] == "Profile not found: deleted-profile"


def test_profile_config_and_generate_aliases_are_explicit(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    monkeypatch.setattr(
        "backend.routes.generate.generate_mihomo_config",
        lambda data, base_url="": "alpha-config",
    )

    config_response = app.test_client().get("/api/config/alpha/mihomo")
    generated_response = app.test_client().post("/api/profiles/alpha/generate/mihomo", json={})

    assert config_response.status_code == 200
    assert config_response.get_data(as_text=True)
    assert generated_response.status_code == 200
    assert (repository.generated_dir("alpha") / "config.yaml").read_text(encoding="utf-8") == "alpha-config"


def test_profile_import_does_not_overwrite_system_or_agents(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile(
        "default",
        {
            "system_config": {"server_domain": "http://stable.test"},
        },
    )
    repository.update_system_transaction(
        lambda system: system.update({
            "agents": [{"id": "agent-1", "profile_id": "default"}],
        })
    )
    app = make_app(repository, monkeypatch)

    response = app.test_client().post(
        "/api/profiles/alpha/import",
        json={
            "subscriptions": [{"id": "imported"}],
            "system_config": {"server_domain": "http://attacker.test"},
            "agents": [{"id": "attacker"}],
        },
    )

    assert response.status_code == 200
    assert repository.get_profile("alpha")["subscriptions"] == [{"id": "imported"}]
    system = repository.get_system()
    assert system["system_config"]["server_domain"] == "http://stable.test"
    assert system["agents"] == [{"id": "agent-1", "profile_id": "default"}]


def test_profile_config_url_is_public_when_config_token_is_valid(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("default", {"system_config": {"config_token": "secret"}})
    config_module.set_repository(repository)
    monkeypatch.setattr("backend.routes.auth.is_auth_enabled", lambda: True)
    app = Flask(__name__)
    register_blueprints(app)
    setup_before_request(app)

    response = app.test_client().get("/api/config/alpha/mihomo?token=secret")

    assert response.status_code == 200


def test_legacy_config_export_reads_the_active_profile(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"subscriptions": [{"id": "exported"}]})
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)

    response = app.test_client().get("/api/config/export")

    assert response.status_code == 200
    assert response.get_json()["subscriptions"] == [{"id": "exported"}]


def test_legacy_config_url_defaults_to_default_profile(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("default", {"marker": "default"})
    repository.save_profile("alpha", {"marker": "alpha"})
    repository.activate_profile("alpha")
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    monkeypatch.setattr(
        "backend.routes.config.generate_mihomo_config",
        lambda data, base_url="": data["marker"],
    )

    response = app.test_client().get("/api/config/mihomo")

    assert response.status_code == 200
    assert response.get_data(as_text=True) == "default"


def test_load_config_uses_request_profile_context(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.create_profile({"id": "beta", "name": "Beta"})
    repository.save_profile("alpha", {"marker": "alpha"})
    repository.save_profile("beta", {"marker": "beta"})
    repository.activate_profile("beta")
    config_module.set_repository(repository)
    app = Flask(__name__)

    with app.test_request_context("/", headers={"X-ConfigFlow-Profile": "alpha"}):
        loaded = config_module.load_config()

    assert loaded["marker"] == "alpha"


def test_each_request_reads_latest_profile_config(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"subscriptions": [{"id": "before"}]})
    app = make_app(repository, monkeypatch)
    client = app.test_client()

    assert client.get("/api/subscriptions").get_json()[0]["id"] == "before"

    repository.save_profile("default", {"subscriptions": [{"id": "after"}]})

    assert client.get("/api/subscriptions").get_json()[0]["id"] == "after"


def test_incremental_config_transaction_uses_latest_disk_state(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    independent = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    config_module.get_config("default")  # Populate a deliberately stale compatibility snapshot.
    independent.update_profile_transaction(
        "default", lambda profile: profile["subscriptions"].append({"id": "independent"})
    )

    config_module.update_config_transaction(
        lambda profile: profile["subscriptions"].append({"id": "route"}),
        "default",
    )

    assert repository.get_profile("default")["subscriptions"] == [
        {"id": "independent"},
        {"id": "route"},
    ]


def test_profile_config_snapshot_is_cleared_after_request(tmp_path, monkeypatch):
    app = make_app(ProfileRepository(tmp_path), monkeypatch)

    app.test_client().get("/api/subscriptions")

    assert config_module._CONFIG_CACHE.get() is None


def test_desensitized_export_does_not_create_external_temp_file(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_profile(
        "default",
        {"subscriptions": [{"id": "sub-1", "url": "https://secret.example"}]},
    )
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)

    def fail_temp_file(*args, **kwargs):
        raise AssertionError("export must stay in memory")

    monkeypatch.setattr("tempfile.NamedTemporaryFile", fail_temp_file)
    response = app.test_client().get("/api/config/export?desensitize=true")

    assert response.status_code == 200
    assert json.loads(response.get_data())['subscriptions'][0]['url'] == '***已脱敏***'


def test_new_repository_keeps_existing_template_defaults(tmp_path, monkeypatch):
    template = tmp_path / 'config_template.json'
    template.write_text(
        json.dumps({
            'subscriptions': [{'id': 'template-sub'}],
            'marker': 'template',
            'system_config': {'server_domain': 'http://template.test'},
        }),
        encoding='utf-8',
    )
    data_dir = tmp_path / 'data'
    monkeypatch.setattr(config_module, 'DATA_DIR', str(data_dir))
    monkeypatch.setattr(config_module, 'get_backend_resource', lambda _: str(template))
    monkeypatch.setattr(config_module, '_repository', None)

    repository = config_module.get_repository()

    assert repository.get_profile('default')['subscriptions'] == [{'id': 'template-sub'}]
    assert repository.get_profile('default')['marker'] == 'template'
    assert 'system_config' not in repository.get_profile('default')
    assert repository.get_system()['system_config']['server_domain'] == 'http://template.test'
