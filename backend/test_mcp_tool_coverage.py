"""MCP 工具覆盖度回归：排序、规则位置、规则仓库正文/缓存、profile 级备份等"""
import json

import pytest
from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.mcp_server import tools
from backend.routes import register_blueprints


@pytest.fixture
def app_with_config(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_profile(
        "default",
        {
            "system_config": {"config_token": "mcp-admin-token", "server_domain": "https://cf.example.com"},
            "subscriptions": [{"id": "sub-1", "name": "A"}, {"id": "sub-2", "name": "B"}],
            "proxy_groups": [
                {"id": "group-1", "name": "PROXY"},
                {"id": "group-2", "name": "AUTO"},
                {"id": "group-3", "name": "DIRECT"},
            ],
            "rule_configs": [
                {"id": "rule-1", "itemType": "rule", "rule_type": "DOMAIN", "value": "a.com", "policy": "PROXY"},
                {"id": "ruleset-1", "itemType": "ruleset", "name": "cn", "policy": "DIRECT", "url": "/api/rules/local/cn"},
            ],
            "rule_library": [
                {"id": "lib-1", "name": "inline", "source_type": "content", "content": "DOMAIN,a.com"},
                {"id": "lib-2", "name": "remote", "source_type": "url", "url": "https://example.com/x.list"},
            ],
        },
    )
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    yield app, repository


def _ids(repository, key):
    return [item["id"] for item in repository.get_profile("default").get(key, [])]


def test_new_tools_are_registered():
    names = {tool["name"] for tool in tools.list_tools()}
    assert {
        "reorder_items",
        "get_rule_library_content",
        "cache_rule_library",
        "get_agent_install_info",
        "convert_mosdns_rule",
        "manage_app_logs",
    } <= names


def test_reorder_moves_listed_items_to_top_and_bottom(app_with_config):
    app, repository = app_with_config
    with app.app_context():
        result = tools.call_tool("reorder_items", {"collection": "proxy_groups", "ids": ["group-3"]})
        assert result["order"] == ["group-3", "group-1", "group-2"]
        tools.call_tool(
            "reorder_items",
            {"collection": "proxy_groups", "ids": ["group-3", "group-1"], "position": "bottom"},
        )
    assert _ids(repository, "proxy_groups") == ["group-2", "group-3", "group-1"]


def test_reorder_supports_every_declared_collection(app_with_config):
    app, repository = app_with_config
    with app.app_context():
        tools.call_tool("reorder_items", {"collection": "subscriptions", "ids": ["sub-2"]})
        tools.call_tool("reorder_items", {"collection": "rule_library", "ids": ["lib-2"]})
        tools.call_tool("reorder_items", {"collection": "rules", "ids": ["ruleset-1"]})
    assert _ids(repository, "subscriptions") == ["sub-2", "sub-1"]
    assert _ids(repository, "rule_library") == ["lib-2", "lib-1"]
    assert _ids(repository, "rule_configs") == ["ruleset-1", "rule-1"]


def test_reorder_keeps_ruleset_url_relative(app_with_config):
    """列表接口会给规则集 URL 拼上 server_domain，回写时必须还原成相对路径"""
    app, repository = app_with_config
    with app.app_context():
        tools.call_tool("reorder_items", {"collection": "rules", "ids": ["ruleset-1"]})
    stored = {item["id"]: item for item in repository.get_profile("default")["rule_configs"]}
    assert stored["ruleset-1"]["url"] == "/api/rules/local/cn"


def test_reorder_rejects_unknown_id(app_with_config):
    app, _ = app_with_config
    with app.app_context():
        with pytest.raises(Exception) as excinfo:
            tools.call_tool("reorder_items", {"collection": "rules", "ids": ["nope"]})
    assert "nope" in str(excinfo.value)


def test_manage_rule_position_bottom_appends(app_with_config):
    app, repository = app_with_config
    with app.app_context():
        created = tools.call_tool(
            "manage_rule",
            {
                "action": "create",
                "position": "bottom",
                "data": {"itemType": "rule", "rule_type": "DOMAIN", "value": "z.com", "policy": "DIRECT"},
            },
        )
        new_id = created["item"]["id"]
    assert _ids(repository, "rule_configs")[-1] == new_id


def test_manage_rule_defaults_to_top(app_with_config):
    app, repository = app_with_config
    with app.app_context():
        created = tools.call_tool(
            "manage_rule",
            {
                "action": "create",
                "data": {"itemType": "rule", "rule_type": "DOMAIN", "value": "z.com", "policy": "DIRECT"},
            },
        )
    assert _ids(repository, "rule_configs")[0] == created["item"]["id"]


def test_rule_library_content_and_settings_section(app_with_config):
    app, _ = app_with_config
    with app.app_context():
        assert tools.call_tool("get_rule_library_content", {"id": "lib-1"})["content"] == "DOMAIN,a.com"
        tools.call_tool(
            "update_settings",
            {"section": "github_proxy", "data": {"proxy_domains": "https://gh.example.com/"}},
        )
        assert tools.call_tool("get_settings", {"section": "github_proxy"})["proxy_domains"] == "https://gh.example.com/"


def test_profile_scoped_backup_export_and_import(app_with_config):
    app, repository = app_with_config
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    repository.save_profile("alpha", {"subscriptions": [{"id": "alpha-sub"}]})
    with app.app_context():
        exported = tools.call_tool(
            "manage_config_backup", {"action": "export", "scope": "profile", "profile_id": "alpha"}
        )
        assert [sub["id"] for sub in exported["subscriptions"]] == ["alpha-sub"]
        tools.call_tool(
            "manage_config_backup",
            {
                "action": "import",
                "scope": "profile",
                "profile_id": "alpha",
                "data": {"subscriptions": [{"id": "imported-sub"}]},
            },
        )
    assert [sub["id"] for sub in repository.get_profile("alpha")["subscriptions"]] == ["imported-sub"]


def test_app_log_info_tool(app_with_config):
    app, _ = app_with_config
    with app.app_context():
        assert tools.call_tool("manage_app_logs", {"action": "info"})["success"] is True


def test_mosdns_rule_proxy_accepts_internal_mcp_call(app_with_config):
    """内部调用不该被规则代理的 token 校验挡掉：缺 url 应报 400 而不是 401"""
    app, _ = app_with_config
    with app.app_context():
        with pytest.raises(Exception) as excinfo:
            tools.call_tool("convert_mosdns_rule", {"url": ""})
    assert "401" not in str(excinfo.value) and "Unauthorized" not in str(excinfo.value)


def test_reorder_does_not_persist_derived_list_fields(app_with_config):
    """列表接口附加的缓存字段不该被排序写回配置"""
    app, repository = app_with_config
    with app.app_context():
        tools.call_tool("reorder_items", {"collection": "subscriptions", "ids": ["sub-2"]})
    stored = repository.get_profile("default")["subscriptions"]
    assert all("cached_node_count" not in sub for sub in stored)


def test_reorder_keeps_urls_that_output_sanitizer_redacts(app_with_config):
    """含内部令牌的订阅 URL 在响应里会被脱敏，排序不能把脱敏结果写回配置"""
    app, repository = app_with_config
    repository.update_system_transaction(
        lambda system: system.setdefault("system_config", {}).update({"rule_proxy_token": "sekret-token"})
    )
    secret_url = "https://cf.example.com/api/rules/local/cn?token=sekret-token"
    repository.update_profile_transaction(
        "default",
        lambda profile: profile["subscriptions"].__setitem__(0, {"id": "sub-1", "name": "A", "url": secret_url}),
    )
    with app.app_context():
        listed = tools.call_tool("list_subscriptions", {})
        assert listed[0]["url"] == "[REDACTED]", "前提：列表响应确实会脱敏"
        tools.call_tool("reorder_items", {"collection": "subscriptions", "ids": ["sub-2"]})
    stored = {sub["id"]: sub for sub in repository.get_profile("default")["subscriptions"]}
    assert stored["sub-1"]["url"] == secret_url


def test_rest_reorder_still_accepts_full_object_arrays(app_with_config):
    """旧格式（整份对象数组）保持兼容，前端不受影响"""
    app, repository = app_with_config
    from backend.mcp_server.invoker import call_api

    with app.app_context():
        call_api(
            "POST",
            "/api/proxy-groups/reorder",
            body={"groups": [{"id": "group-2", "name": "AUTO"}, {"id": "group-1", "name": "PROXY"}]},
        )
    assert _ids(repository, "proxy_groups") == ["group-2", "group-1"]
