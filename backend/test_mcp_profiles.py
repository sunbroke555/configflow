import json

from flask import Flask, request

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.mcp_server import invoker, tools
from backend.routes import register_blueprints


def test_mcp_registers_profile_management_tools_and_profile_schema():
    definitions = {tool["name"]: tool for tool in tools.list_tools()}

    assert {"list_profiles", "get_profile", "manage_profile", "clone_profile", "bind_agent_profile"} <= definitions.keys()
    assert "profile_id" in definitions["list_subscriptions"]["inputSchema"]["properties"]
    assert "profile_id" in definitions["generate_config"]["inputSchema"]["properties"]


def test_mcp_profile_id_becomes_request_header_for_existing_tools(monkeypatch):
    app = Flask(__name__)

    @app.get("/api/subscriptions")
    def subscriptions():
        return {"profile": request.headers.get("X-ConfigFlow-Profile")}

    with app.app_context():
        result = tools.call_tool("list_subscriptions", {"profile_id": "alpha"})

    assert result["profile"] == "alpha"


def test_invoker_accepts_explicit_headers_and_profile_id(tmp_path):
    app = Flask(__name__)

    @app.get("/echo")
    def echo():
        return {"profile": request.headers.get("X-ConfigFlow-Profile"), "custom": request.headers.get("X-Custom")}

    with app.app_context():
        result = invoker.call_api(
            "GET",
            "/echo",
            profile_id="alpha",
            headers={"X-Custom": "yes"},
        )

    assert result == {"profile": "alpha", "custom": "yes"}


def test_mcp_can_manage_profiles_and_bind_agents(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"system_config": {"config_token": "mcp-admin-token"}})
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
    repository.save_profile("default", {"system_config": {"config_token": "mcp-admin-token"}})
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("default", {"subscriptions": [{"id": "default-sub"}]})
    repository.save_profile("alpha", {"subscriptions": [{"id": "alpha-sub"}]})
    repository.activate_profile("alpha")
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    from backend.mcp_server import mcp_bp
    app.register_blueprint(mcp_bp)

    response = app.test_client().post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {repository.get_system()['system_config']['config_token']}"
        },
        json={
            "jsonrpc": "2.0",
            "id": "legacy-list",
            "method": "tools/call",
            "params": {"name": "list_subscriptions", "arguments": {}},
        },
    )
    payload = json.loads(response.get_json()["result"]["content"][0]["text"])

    assert payload[0]["id"] == "default-sub"


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
