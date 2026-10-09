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
    repository.save_system({"system_config": {"config_token": "mcp-admin-token", "server_domain": "https://cf.example.com"}})
    repository.save_shared({
        "subscriptions": [{"id": "sub-1", "name": "A"}, {"id": "sub-2", "name": "B"}],
        "rule_library": [
            {"id": "lib-1", "name": "inline", "source_type": "content", "content": "DOMAIN,a.com"},
            {"id": "lib-2", "name": "remote", "source_type": "url", "url": "https://example.com/x.list"},
        ],
    })
    repository.save_profile("default", {
            "proxy_groups": [
                {"id": "group-1", "name": "PROXY", "type": "select", "subscriptions": ["sub-1", "sub-2"]},
                {"id": "group-2", "name": "AUTO"},
                {"id": "group-3", "name": "Fallback"},
            ],
            "rule_configs": [
                {"id": "rule-1", "itemType": "rule", "rule_type": "DOMAIN", "value": "a.com", "policy": "PROXY"},
                {"id": "ruleset-1", "itemType": "ruleset", "library_rule_id": "lib-1", "policy": "DIRECT"},
            ],
        },
    )
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    yield app, repository


def _ids(repository, key):
    config = repository.get_shared() if key in {"subscriptions", "nodes", "rule_library"} else repository.get_profile("default")
    return [item["id"] for item in config.get(key, [])]




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


def test_reorder_does_not_persist_hydrated_rule_source(app_with_config):
    """列表中的来源字段不能被排序写回为独立配置覆盖。"""
    app, repository = app_with_config
    with app.app_context():
        tools.call_tool("reorder_items", {"collection": "rules", "ids": ["ruleset-1"]})
    stored = {item["id"]: item for item in repository.get_profile("default")["rule_configs"]}
    assert stored["ruleset-1"]["library_rule_id"] == "lib-1"
    assert "url" not in stored["ruleset-1"]
    assert repository.get_shared()["rule_library"][0]["content"] == "DOMAIN,a.com"


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
    group = {"id": "shared", "name": "Shared", "type": "select", "subscriptions": ["sub-1"]}
    repository.save_profile("alpha", {"proxy_groups": [group]})
    default = repository.get_profile("default")
    with app.app_context():
        exported = tools.call_tool(
            "manage_config_backup", {"action": "export", "scope": "profile", "profile_id": "alpha"}
        )
        assert exported["proxy_groups"] == [group]
        assert "resource_refs" not in exported
        assert "node_dialers" not in exported
        assert "subscriptions" not in exported
        tools.call_tool(
            "manage_config_backup",
            {
                "action": "import",
                "scope": "profile",
                "profile_id": "alpha",
                "data": {"proxy_groups": [{**group, "subscriptions": ["sub-2"]}]},
            },
        )
    assert repository.get_profile("alpha")["proxy_groups"] == [{**group, "subscriptions": ["sub-2"]}]
    assert repository.get_profile("default") == default
    assert _ids(repository, "subscriptions") == ["sub-1", "sub-2"]


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
    stored = repository.get_shared()["subscriptions"]
    assert all("cached_node_count" not in sub for sub in stored)


def test_reorder_keeps_urls_that_output_sanitizer_redacts(app_with_config):
    """含内部令牌的订阅 URL 在响应里会被脱敏，排序不能把脱敏结果写回配置"""
    app, repository = app_with_config
    repository.update_system_transaction(
        lambda system: system.setdefault("system_config", {}).update({"rule_proxy_token": "sekret-token"})
    )
    secret_url = "https://cf.example.com/api/rules/local/cn?token=sekret-token"
    repository.update_shared_transaction(
        lambda shared: shared["subscriptions"].__setitem__(0, {"id": "sub-1", "name": "A", "url": secret_url}),
    )
    with app.app_context():
        listed = tools.call_tool("list_subscriptions", {})
        assert listed[0]["url"] == "[REDACTED]", "前提：列表响应确实会脱敏"
        tools.call_tool("reorder_items", {"collection": "subscriptions", "ids": ["sub-2"]})
    stored = {sub["id"]: sub for sub in repository.get_shared()["subscriptions"]}
    assert stored["sub-1"]["url"] == secret_url


def test_rest_reorder_full_object_arrays_preserve_unlisted_groups(app_with_config):
    """排序不删除并发新增或未列出的策略组。"""
    app, repository = app_with_config
    from backend.mcp_server.invoker import call_api

    with app.app_context():
        call_api(
            "POST",
            "/api/proxy-groups/reorder",
            body={"groups": [{"id": "group-2", "name": "AUTO"}, {"id": "group-1", "name": "PROXY"}]},
        )
    assert _ids(repository, "proxy_groups") == ["group-2", "group-1", "group-3"]




def test_proxy_group_edit_round_trip_with_real_fields(app_with_config):
    """按工具描述里的字段名编辑策略组，改动必须真的落到配置里"""
    app, repository = app_with_config
    with app.app_context():
        created = tools.call_tool(
            "manage_proxy_group",
            {
                "action": "create",
                "data": {
                    "name": "AUTO-HK",
                    "type": "url-test",
                    "enabled": True,
                    "subscriptions": ["sub-1"],
                    "regex": "HK|香港",
                    "include_groups": ["group-1"],
                    "manual_nodes": ["DIRECT"],
                },
            },
        )
        group_id = created["item"]["id"]
        tools.call_tool(
            "manage_proxy_group",
            {"action": "update", "id": group_id, "data": {"regex": "SG|新加坡", "interval": 600}},
        )
    stored = {g["id"]: g for g in repository.get_profile("default")["proxy_groups"]}[group_id]
    assert stored["regex"] == "SG|新加坡"
    assert stored["include_groups"] == ["group-1"]
    assert stored["subscriptions"] == ["sub-1"]
    assert stored["interval"] == 600


def test_node_create_uses_proxy_string(app_with_config):
    app, repository = app_with_config
    with app.app_context():
        created = tools.call_tool(
            "manage_node",
            {"action": "create", "data": {"name": "HK-01", "proxy_string": "ss://abc@1.2.3.4:443", "enabled": True}},
        )
    stored = {n["id"]: n for n in repository.get_shared().get("nodes", [])}
    assert stored[created["item"]["id"]]["proxy_string"] == "ss://abc@1.2.3.4:443"


def test_named_chain_roundtrip_uses_proxy_group_tools_and_stays_profile_local(app_with_config):
    app, repository = app_with_config
    repository.create_profile({"id": "alpha", "name": "Alpha"})
    default = repository.get_profile("default")
    with app.app_context():
        node = tools.call_tool("manage_node", {
            "action": "create", "data": {"name": "Exit", "type": "http", "enabled": True,
            "proxy_string": '{"type":"http","server":"exit.test","port":80}'},
        })["item"]
        shared = repository.get_shared()
        entry = tools.call_tool("manage_proxy_group", {
            "action": "create", "profile_id": "alpha",
            "data": {"name": "Entry", "type": "select", "manual_nodes": ["DIRECT"]},
        })["item"]
        chain = {"entry": {"type": "group", "id": entry["id"]}, "exit": {"type": "node", "id": node["id"]}}
        created = tools.call_tool("manage_proxy_group", {
            "action": "create", "profile_id": "alpha",
            "data": {"name": "Via Exit", "type": "chain", "chain": chain},
        })["item"]
        tools.call_tool("manage_proxy_group", {
            "action": "update", "id": created["id"], "profile_id": "alpha",
            "data": {"name": "Renamed Chain"},
        })
        listed = tools.call_tool("list_proxy_groups", {"profile_id": "alpha"})
        stored = next(group for group in listed if group["id"] == created["id"])
        assert stored["name"] == "Renamed Chain"
        assert stored["type"] == "chain"
        assert stored["chain"] == chain
        assert next(group for group in repository.get_profile("alpha")["proxy_groups"]
                    if group["id"] == created["id"])["chain"] == chain
        assert repository.get_profile("default") == default
        assert repository.get_shared() == shared
        tools.call_tool("manage_proxy_group", {
            "action": "delete", "id": created["id"], "profile_id": "alpha",
        })
    assert [group["id"] for group in repository.get_profile("alpha")["proxy_groups"]] == [entry["id"]]
    assert repository.get_profile("default") == default
    assert repository.get_shared() == shared
