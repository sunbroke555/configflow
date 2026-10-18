"""新版界面依赖的后端接口：节点延迟、订阅健康与流量、规则命中定位、统计历史、配置修订号。"""

from flask import Flask

from backend.common import config as config_module
from backend.common.config_repository import ProfileRepository
from backend.routes import register_blueprints
from backend.utils import node_latency, subscription_health


def make_client(repository):
    config_module.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    return app.test_client()


def test_node_latency_preserves_full_proxy_config_and_results(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({"nodes": [
        {"id": "n1", "name": "香港 01", "proxy_string":
         '{"type":"trojan","server":"127.0.0.1","port":443,"password":"secret","sni":"test.example"}'},
        {"id": "n2", "name": "失联节点", "type": "ss", "server": "127.0.0.1", "port": 1,
         "params": {"cipher": "aes-128-gcm", "password": "secret2"}},
        {"id": "n3", "name": "停用节点", "enabled": False, "proxy_string": "ss://invalid"},
    ]})
    client = make_client(repository)
    monkeypatch.setattr(node_latency, '_probe_binary', lambda: '/unused/test-probe')

    def probe(proxies, options):
        assert options['url'].startswith('https://')
        assert options['unified_delay'] is True
        assert proxies[0]['password'] == 'secret'
        assert proxies[0]['sni'] == 'test.example'
        return {proxy['name']: {'latency': 88} if proxy['name'] == '["node","n1"]' else
                {'latency': None, 'error': '连接或请求超时', 'error_code': 'timeout'} for proxy in proxies}

    monkeypatch.setattr(node_latency, '_run_probe', probe)
    response = client.post('/api/nodes/latency', json={})
    body = response.get_json()
    assert response.status_code == 200
    assert set(body['results']) == {'["node","n1"]', '["node","n2"]'}
    assert body['results']['["node","n1"]']['latency'] == 88
    assert body['results']['["node","n2"]']['latency'] is None
    assert body['results']['["node","n2"]']['error_code'] == 'timeout'
    assert b'secret' not in response.data
    client.post('/api/nodes/latency', json={'names': ['香港 01']})
    stored = client.get('/api/nodes/latency').get_json()['results']
    assert len(stored['["node","n1"]']['history']) == 2
    assert len(stored['["node","n2"]']['history']) == 1


def test_node_latency_rejects_bad_names(tmp_path):
    client = make_client(ProfileRepository(tmp_path))
    assert client.post("/api/nodes/latency", json={"names": "all"}).status_code == 400


def test_node_latency_history_is_bounded(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({"nodes": [
        {"id": "n1", "name": "A", "enabled": True,
         "proxy_string": "{\"name\": \"A\", \"type\": \"ss\", \"server\": \"127.0.0.1\", \"port\": 1}"},
    ]})
    make_client(repository)
    monkeypatch.setattr(node_latency, '_probe_binary', lambda: '/unused/test-probe')
    monkeypatch.setattr(node_latency, '_run_probe', lambda proxies, options: {
        proxy['name']: {'latency': 42} for proxy in proxies
    })
    for _ in range(node_latency.HISTORY_SIZE + 5):
        node_latency.run_tests()
    assert len(node_latency.load_results()['["node","n1"]']["history"]) == node_latency.HISTORY_SIZE


def test_parse_userinfo():
    assert subscription_health.parse_userinfo(
        "upload=1024; download=2048; total=10737418240; expire=1798732800"
    ) == {"upload": 1024, "download": 2048, "total": 10737418240, "expire": 1798732800}
    assert subscription_health.parse_userinfo("upload=1; total=5") == {"upload": 1, "total": 5}
    assert subscription_health.parse_userinfo("") is None
    assert subscription_health.parse_userinfo("garbage") is None


def test_fetch_userinfo_skips_non_http():
    assert subscription_health.fetch_userinfo("file:///etc/passwd") is None
    assert subscription_health.fetch_userinfo(None) is None


def test_subscription_health_history_and_derived_fields(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({"subscriptions": [
        {"id": "s1", "name": "A", "url": "https://example.invalid/a", "enabled": True},
    ]})
    client = make_client(repository)

    for i in range(subscription_health.HISTORY_SIZE + 3):
        subscription_health.record_fetch("s1", ok=True, count=i)
    subscription_health.record_fetch("s1", ok=False, from_cache=True, count=9)
    subscription_health.record_fetch(
        "s1", ok=True, count=10,
        traffic={"upload": 1, "download": 2, "total": 100, "expire": 2000000000},
    )

    health = client.get("/api/subscriptions/health").get_json()
    item = health["items"][0]
    assert len(item["history"]) == subscription_health.HISTORY_SIZE
    assert item["history"][-2]["status"] == "cache"
    assert item["history"][-1]["status"] == "ok"
    assert item["traffic"]["total"] == 100

    listed = client.get("/api/subscriptions").get_json()[0]
    assert listed["traffic"]["download"] == 2
    assert listed["last_fetch"]["count"] == 10

    # 前端整对象回写时，派生字段不得落进配置文件
    client.put("/api/subscriptions/s1", json={**listed, "name": "B"})
    stored = repository.get_shared()["subscriptions"][0]
    assert stored["name"] == "B"
    for key in ("traffic", "last_fetch", "cached_node_count", "cached_updated_at", "traffic_at"):
        assert key not in stored

    # 删除订阅时清理健康记录
    client.delete("/api/subscriptions/s1")
    assert "s1" not in subscription_health.load_health()


def test_match_test_returns_rule_identity(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_profile("default", {"rule_configs": [
        {"id": "r1", "itemType": "rule", "rule_type": "DOMAIN-SUFFIX", "value": "google.com",
         "policy": "PROXY", "enabled": True},
        {"id": "r2", "itemType": "rule", "rule_type": "DOMAIN-SUFFIX", "value": "anthropic.com",
         "policy": "AI", "enabled": True},
    ]})
    client = make_client(repository)

    body = client.post("/api/rules/match-test", json={"query": "api.anthropic.com"}).get_json()

    assert body["matched"] is True
    assert body["rule_id"] == "r2"
    assert body["item_type"] == "rule"
    assert body["priority"] == 2


def test_config_revision_follows_profile_and_shared_changes(tmp_path):
    repository = ProfileRepository(tmp_path)
    first = repository.config_revision("default")

    repository.update_profile_fields("default", {"proxy_groups": []})
    after_profile = repository.config_revision("default")
    assert after_profile > first

    # 共享资源（订阅/节点）变化同样影响生成的配置
    repository.save_shared({"nodes": [{"id": "n1", "name": "A"}]})
    assert repository.config_revision("default") > after_profile
    listed = next(p for p in repository.list_profiles() if p["id"] == "default")
    assert listed["revision"] == repository.config_revision("default")


def test_stats_overview_returns_history_and_revision(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.save_shared({
        "subscriptions": [{"id": "s1", "name": "S", "url": "https://example.invalid/s"}],
        "nodes": [{"id": "n1", "name": "A"}, {"id": "n2", "name": "B"}],
    })
    client = make_client(repository)

    data = client.get("/api/stats/overview").get_json()["data"]
    assert data["nodes"]["total"] == 2
    assert data["nodes"]["history"][-1] == 2
    assert data["subscriptions"]["history"][-1] == 1
    assert data["profile"]["revision"] >= 1

    # 采样间隔内再次请求：不重复落盘，但末尾值跟随最新计数
    repository.save_shared({"nodes": [{"id": "n1", "name": "A"}]})
    again = client.get("/api/stats/overview").get_json()["data"]
    assert again["nodes"]["history"][-1] == 1
    assert len(again["nodes"]["history"]) == len(data["nodes"]["history"])
