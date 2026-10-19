"""网站识别：来源可信度、Agent 路径、缓存与配置空间隔离。"""
import re
from datetime import datetime, timedelta

import pytest

from backend.agents.manager import AgentManager
from backend.test_region_check import env  # 共用已隔离的配置 / Agent fixture
from backend.utils import domain_discovery_service as service
from backend.utils.region_check import RegionStore, get_region_store
from backend.utils.website_identity import PAGE_MARKERS, known_service, page_identity, site_title


def test_known_service_requires_domain_boundary():
    assert known_service('api.openai.com')['name'] == 'ChatGPT'
    assert known_service('youtube.com')['name'] == 'YouTube'
    assert known_service('notopenai.com') is None
    assert known_service('openai.com.evil.test') is None


def test_page_metadata_attribute_order_entities_and_path_redaction():
    html = '''<TITLE>Example &amp; docs</TITLE>
      <meta content='Example &amp; Co' property='og:site_name'>
      <meta name="description" content="Documentation &amp; help">'''
    markers = {}
    for key, pattern in PAGE_MARKERS.items():
        match = re.search(pattern, html)
        if match:
            markers[key] = match.group(1) if match.lastindex else match.group(0)
    entry = page_identity({'status': 200, 'markers': markers,
                           'final_url': 'https://docs.example.com/private?token=secret'}, 'example.com', 'now')
    assert entry['name'] == 'Example & Co'
    assert entry['description'] == 'Documentation & help'
    assert entry['final_host'] == 'docs.example.com'
    assert 'secret' not in str(entry)


@pytest.mark.parametrize('title,name', [
    ('HHCLUB :: 登录 - Powered by NexusPHP', 'HHCLUB'),
    ('Login - GitHub', 'GitHub'),
    ('首页 - 某站 - Powered by Discuz!', '某站'),
    ('Cloud Monitoring as a Service | Datadog', 'Cloud Monitoring as a Service | Datadog'),
    ('Wi-Fi Home', 'Wi-Fi Home'),
    ('登录', '登录'),
])
def test_site_title_drops_page_names_and_powered_by(title, name):
    assert site_title(title) == name
    assert page_identity({'status': 200, 'markers': {'title': title}}, 'example.com', 'now')['name'] == name


@pytest.mark.parametrize('result,reason', [
    ({'error': 'timeout'}, '访问失败'),
    ({'status': 403, 'markers': {'title': 'Example'}}, 'HTTP 403'),
    ({'status': 200, 'markers': {'title': 'Example', 'challenge': 'cf-chl-'}}, '人机验证'),
    ({'status': 200, 'markers': {'title': '404 Not Found'}}, '错误页'),
    ({'status': 200, 'markers': {}}, '未找到'),
    (None, '未成功'),
])
def test_failure_is_not_a_website_name(result, reason):
    entry = page_identity(result, 'example.com', 'now')
    assert entry['name'] == '' and reason in entry['reason']


def _report(client, agent, auth):
    response = client.post(f"/api/agents/{agent['id']}/traffic-report", headers=auth, json={
        'schema': 1, 'status': 'running', 'items': [
            {'host': 'www.example.com', 'port': 443, 'rule': 'Match', 'policy': 'DIRECT', 'outlet': 'direct', 'conns': 3},
            {'host': 'api.example.com', 'port': 443, 'rule': 'Match', 'policy': 'DIRECT', 'outlet': 'direct', 'conns': 1},
        ]})
    assert response.status_code == 200


def test_identify_cache_note_and_clear(env, monkeypatch):
    client, repository, agent, auth, _ = env
    _report(client, agent, auth)
    calls = []

    def fetch(self, selected, payload):
        calls.append((selected, payload))
        pages = {'https://example.com/': {}, 'https://www.example.com/': {'title': 'Example docs'}}
        return {'results': [{'id': item['id'], 'status': 200, 'markers': pages[item['url']]}
                            for item in payload['requests']]}

    monkeypatch.setattr(AgentManager, 'region_check', fetch)
    assert client.put('/api/domain-discovery/website/note', json={'domain': 'example.com', 'note': '我的文档'}).status_code == 200
    job = service.start_website_job('default', ['example.com'], wait=True)
    assert job['kind'] == 'website' and job['status'] == 'done' and not job['errors']
    assert calls[0][0]['id'] == agent['id']
    # 主域名首页优先；流量最多的 www 子域与 www 候选重合，不重复请求
    assert [item['url'] for item in calls[0][1]['requests']] == ['https://example.com/', 'https://www.example.com/']
    assert calls[0][1]['target'] == 'DIRECT'
    website = client.get('/api/domain-discovery/domains').get_json()['items'][0]['website']
    assert website['page']['name'] == 'Example docs'
    assert website['page']['host'] == 'www.example.com'
    assert website['note'] == '我的文档'
    assert len(calls) == 1  # 列表只读缓存，不发请求
    client.post(f"/api/agents/{agent['id']}/domain-discovery/clear")
    assert get_region_store().load(agent['id'])['websites'] == {}
    assert repository.get_profile('default')['domain_discovery']['website_notes']['example.com'] == '我的文档'
    assert client.put('/api/domain-discovery/website/note', json={'domain': 'example.com', 'note': ''}).status_code == 200
    assert repository.get_profile('default')['domain_discovery']['website_notes'] == {}


def test_candidates_try_homepages_before_busiest_subdomain():
    assert service.website_candidates('example.com', {'host': 'api.example.com', 'port': 8443}) == [
        ('example.com', 443), ('www.example.com', 443), ('api.example.com', 8443)]
    assert service.website_candidates('example.com', {'host': 'example.com', 'port': 80}) == [
        ('example.com', 80), ('www.example.com', 443)]


def test_identify_preconditions_and_invalid_input(env):
    client, _, agent, auth, _ = env
    _report(client, agent, auth)
    for payload in ([], {'domains': []}, {'domains': ['https://example.com']}, {'domains': ['unobserved.test']}):
        assert client.post('/api/domain-discovery/website/identify', json=payload).status_code == 400
    for payload in ([], {'domain': '../bad', 'note': 'x'}, {'domain': 'example.com', 'note': 12},
                    {'domain': 'example.com', 'note': 'x' * 201}):
        assert client.put('/api/domain-discovery/website/note', json=payload).status_code == 400


def test_batch_identify_skips_unobserved_domains(env, monkeypatch):
    client, _, agent, auth, _ = env
    _report(client, agent, auth)
    monkeypatch.setattr(AgentManager, 'region_check', lambda self, selected, payload: {'results': [
        {'id': item['id'], 'status': 200, 'markers': {'title': 'Example'}} for item in payload['requests']]})
    job = service.start_website_job('default', ['example.com', 'unobserved.test'], wait=True)
    assert job['status'] == 'done'
    assert [error['host'] for error in job['errors']] == ['unobserved.test']
    too_many = {'domains': [f'd{i}.test' for i in range(service.WEBSITE_BATCH_LIMIT + 1)]}
    assert client.post('/api/domain-discovery/website/identify', json=too_many).status_code == 400


def test_cached_websites_and_notes_do_not_cross_profiles(env):
    client, repository, agent, auth, _ = env
    _report(client, agent, auth)
    client.put('/api/domain-discovery/website/note', json={'domain': 'example.com', 'note': 'private'})
    get_region_store().update(agent['id'], lambda data: data['websites'].update({
        'www.example.com': {'checked_at': datetime.now().isoformat(), 'name': 'private'}}))
    items = [{'domain': 'example.com', 'hosts': [{'host': 'www.example.com'}]}]
    service.enrich_websites(items, service.read_settings({}), [])
    assert items[0]['website'] == {'service': None, 'page': None, 'note': ''}


def test_cache_expiry_and_bound(tmp_path):
    store = RegionStore(tmp_path)
    now = datetime.now()
    old = (now - timedelta(days=31)).isoformat()
    store.update('a', lambda data: data['websites'].update({
        'old.test': {'checked_at': old},
        **{f'{i}.test': {'checked_at': now.isoformat()} for i in range(1001)},
    }), now=now)
    assert len(store.load('a')['websites']) == 1000
    assert 'old.test' not in store.load('a')['websites']


def test_website_identity_does_not_include_rules(env):
    client, repository, agent, auth, _ = env
    _report(client, agent, auth)
    repository.save_shared({'rule_library': [
        {'id': 'lib', 'name': '国外网站', 'source_type': 'content', 'behavior': 'domain', 'content': '+.example.com'}]})
    repository.update_profile_fields('default', {'rule_configs': [
        {'id': 'rule', 'itemType': 'ruleset', 'library_rule_id': 'lib', 'policy': 'DIRECT', 'enabled': True}]})
    website = client.get('/api/domain-discovery/domains?view=all').get_json()['items'][0]['website']
    assert website['service'] is None and website['page'] is None
    assert 'rules' not in website
