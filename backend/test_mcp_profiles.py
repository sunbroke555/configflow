import json

from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.mcp_server import tools
from backend.routes import register_blueprints


def test_mcp_can_manage_profiles_and_bind_agents(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_system({"system_config": {"config_token": "mcp-admin-token"}})
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    from backend.mcp_server import mcp_bp
    app.register_blueprint(mcp_bp)
    client = app.test_client()
    auth_headers = {
        "Authorization": f"Bearer {repository.get_system()['system_config']['config_token']}"
    }

    def call(name, arguments):
        response = client.post(
            "/mcp",
            headers=auth_headers,
            json={
                "jsonrpc": "2.0",
                "id": name,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        )
        assert response.status_code == 200
        return json.loads(response.get_json()["result"]["content"][0]["text"])

    created = call("manage_profile", {"action": "create", "data": {"id": "alpha", "name": "Alpha"}})
    assert created["id"] == "alpha"
    assert created["profile_id"] == "default"
    assert call("get_profile", {"id": "alpha"})["id"] == "alpha"
    cloned = call("clone_profile", {"source_profile_id": "alpha", "data": {"id": "beta"}})
    assert cloned["id"] == "beta"

    repository.update_system_transaction(
        lambda system: system.update({
            "agents": [
                {
                    "id": "agent-1",
                    "name": "Agent",
                    "host": "127.0.0.1",
                    "port": 8080,
                    "token": "test-token",
                    "profile_id": "default",
                }
            ]
        }),
    )
    bound = call("bind_agent_profile", {"id": "agent-1", "profile_id": "alpha"})
    assert bound["profile_id"] == "alpha"


def test_mcp_without_profile_id_keeps_legacy_default_profile(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_system({"system_config": {"config_token": "mcp-admin-token"}})
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("default", {"proxy_groups": [{"id": "default-group", "name": "Default", "type": "select"}]})
    repository.save_profile("alpha", {"proxy_groups": [{"id": "alpha-group", "name": "Alpha", "type": "select"}]})
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    from backend.mcp_server import mcp_bp
    app.register_blueprint(mcp_bp)

    response = app.test_client().post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {repository.get_system()['system_config']['config_token']}",
            "X-ConfigFlow-Profile": "alpha",
        },
        json={
            "jsonrpc": "2.0",
            "id": "legacy-list",
            "method": "tools/call",
            "params": {"name": "list_proxy_groups", "arguments": {}},
        },
    )
    payload = json.loads(response.get_json()["result"]["content"][0]["text"])

    assert payload[0]["id"] == "default-group"


def test_mcp_rejects_internal_rule_proxy_token_when_anonymous_mode_is_enabled(tmp_path):
    repository = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    app = Flask(__name__)
    from backend.mcp_server import mcp_bp
    app.register_blueprint(mcp_bp)

    token = repository.get_system()["system_config"]["rule_proxy_token"]
    responses = [
        app.test_client().post(
            "/mcp",
            headers={"Authorization": f"Bearer {token}"},
            json={"jsonrpc": "2.0", "id": "blocked-header", "method": "tools/list"},
        ),
        app.test_client().post(
            "/mcp",
            query_string={"token": token},
            json={"jsonrpc": "2.0", "id": "blocked-query", "method": "tools/list"},
        ),
    ]

    assert [response.status_code for response in responses] == [401, 401]


def test_mcp_rejects_rule_proxy_token_before_equal_config_token(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    config_module.set_repository(repository)
    shared_token = repository.get_system()["system_config"]["rule_proxy_token"]
    monkeypatch.setattr("backend.mcp_server.auth._config_token", lambda: shared_token)
    monkeypatch.setattr(
        "backend.mcp_server.auth._internal_rule_proxy_tokens", lambda: {shared_token}
    )
    app = Flask(__name__)
    from backend.mcp_server import mcp_bp
    app.register_blueprint(mcp_bp)

    responses = [
        app.test_client().post(
            "/mcp",
            headers={"Authorization": f"Bearer {shared_token}"},
            json={"jsonrpc": "2.0", "id": "equal-header", "method": "tools/list"},
        ),
        app.test_client().post(
            "/mcp",
            query_string={"token": shared_token},
            json={"jsonrpc": "2.0", "id": "equal-query", "method": "tools/list"},
        ),
    ]

    assert [response.status_code for response in responses] == [401, 401]


def test_mcp_exposes_proxy_groups_without_resource_selection_or_node_dialer_tools(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_system({"system_config": {"config_token": "mcp-admin-token"}})
    config_module.set_repository(repository)
    app = Flask(__name__)
    from backend.mcp_server import mcp_bp
    app.register_blueprint(mcp_bp)
    response = app.test_client().post(
        "/mcp",
        headers={"Authorization": "Bearer mcp-admin-token"},
        json={"jsonrpc": "2.0", "id": "tools", "method": "tools/list"},
    )
    assert response.status_code == 200
    names = {tool["name"] for tool in response.get_json()["result"]["tools"]}
    assert {"list_proxy_groups", "manage_proxy_group"} <= names
    for name in ("get_profile_resources", "set_profile_resources",
                 "get_profile_node_dialers", "set_profile_node_dialers"):
        assert name not in names
        assert not tools.has_tool(name)
