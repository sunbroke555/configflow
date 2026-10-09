"""Shared source hydration, independent generation, and restorable global backups."""
import copy
import json
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from flask import Flask

from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.converters.mihomo import generate_mihomo_config, get_mihomo_provider_downloads, get_mihomo_ruleset_downloads
from backend.converters.mosdns import generate_mosdns_config, get_mosdns_ruleset_downloads
from backend.converters.surge import generate_surge_config
from backend.routes import register_blueprints
from backend.utils.subscription_cache import save_subscription_nodes


@pytest.fixture
def generation_app(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'office', 'name': 'Office'})
    repository.save_shared({
        'subscriptions': [{'id': 'sub', 'name': 'Feed', 'url': '', 'enabled': True}],
        'nodes': [],
        'subscription_aggregations': [],
        'rule_library': [{'id': 'library', 'name': 'Shared Rules', 'source_type': 'content',
                          'behavior': 'domain', 'content': 'example.com', 'enabled': True}],
    })
    for profile_id, port in (('default', 7890), ('office', 7891)):
        repository.save_profile(profile_id, {
            'proxy_groups': [{'id': 'group', 'name': 'Proxy', 'type': 'select',
                              'manual_nodes': ['DIRECT'], 'subscriptions': ['sub']}],
            'rule_configs': [{'id': 'ruleset', 'itemType': 'ruleset', 'library_rule_id': 'library',
                              'policy': 'DIRECT', 'enabled': True},
                             {'id': 'inline', 'itemType': 'rule', 'rule_type': 'DOMAIN',
                              'value': 'local.example', 'policy': 'DIRECT', 'enabled': True}],
            'mihomo': {'custom_config': f'mixed-port: {port}\n'},
            'surge': {'custom_config': f'[General]\nhttp-listen = 127.0.0.1:{port}\n'},
            'mosdns': {'direct_rulesets': ['ruleset']},
        })
    repository.update_system_transaction(lambda system: system['system_config'].update({
        'server_domain': 'https://config.example', 'config_token': 'global+token&secret',
    }))
    config_store.set_repository(repository)
    save_subscription_nodes('sub', [{'id': 'cached', 'name': 'Cached', 'type': 'http',
                                     'server': 'example.test', 'port': 80, 'enabled': True}])
    app = Flask(__name__)
    app.config['TESTING'] = True
    register_blueprints(app)
    yield app, repository
    config_store.reset_config_context()


def test_explicit_provider_and_rule_urls_match_generated_mihomo(generation_app):
    _app, repository = generation_app
    for profile_id in ('default', 'office'):
        snapshot = repository.get_compat_config(profile_id)
        original = copy.deepcopy(snapshot)
        generated = yaml.safe_load(generate_mihomo_config(snapshot))
        providers = get_mihomo_provider_downloads(snapshot)
        rules = get_mihomo_ruleset_downloads(snapshot)
        assert len(providers) == len(rules) == 1
        for downloads, section in ((providers, 'proxy-providers'), (rules, 'rule-providers')):
            for item in downloads:
                assert urlsplit(item['url']).path.startswith(f'/api/profiles/{profile_id}/')
                assert item['url'] == generated[section][item['name']]['url']
                assert item['local_path'] == generated[section][item['name']]['path']
        assert parse_qs(urlsplit(providers[0]['url']).query)['token'] == ['global+token&secret']
        assert snapshot == original


def test_surge_and_mosdns_links_keep_explicit_profile(generation_app):
    _app, repository = generation_app
    snapshot = repository.get_compat_config('office')
    original = copy.deepcopy(snapshot)
    surge = generate_surge_config(snapshot)
    managed = surge.splitlines()[0].split()[1]
    assert urlsplit(managed).path == '/api/config/office/surge'
    assert parse_qs(urlsplit(managed).query)['token'] == ['global+token&secret']
    policy = next(line for line in surge.splitlines() if line.startswith('Proxy = '))
    assert '/api/profiles/office/subscriptions/sub/proxies' in policy
    assert 'format=surge' in policy
    rule = next(line for line in surge.splitlines() if line.startswith('RULE-SET,'))
    assert '/api/profiles/office/rule-library/content/library' in rule
    mosdns = generate_mosdns_config(snapshot)
    downloads = get_mosdns_ruleset_downloads(snapshot)
    assert len(downloads) == 1
    outer = urlsplit(downloads[0]['url'])
    assert outer.path == '/api/profiles/office/mosdns/rule-proxy'
    query = parse_qs(outer.query)
    assert query['token'] == [snapshot['system_config']['rule_proxy_token']]
    assert urlsplit(query['url'][0]).path == '/api/profiles/office/rule-library/content/library'
    assert downloads[0]['local_path'] in mosdns
    assert snapshot == original


@pytest.mark.parametrize('enabled', [False, True])
def test_library_availability_is_dynamic_without_overwriting_profile_flags(generation_app, enabled):
    _app, repository = generation_app
    before = repository.get_profile('office')['rule_configs']
    repository.update_shared_transaction(lambda shared: shared['rule_library'][0].update(enabled=enabled))
    snapshot = repository.get_compat_config('office')
    mihomo = yaml.safe_load(generate_mihomo_config(snapshot))
    surge = generate_surge_config(snapshot)
    mosdns = generate_mosdns_config(snapshot)
    assert ('Shared Rules' in mihomo.get('rule-providers', {})) is enabled
    assert ('RULE-SET,Shared Rules,DIRECT' in mihomo['rules']) is enabled
    assert 'DOMAIN,local.example,DIRECT' in mihomo['rules']
    assert any(line.startswith('RULE-SET,') for line in surge.splitlines()) is enabled
    assert 'DOMAIN,local.example,DIRECT' in surge
    assert ('./rules/Shared Rules.txt' in mosdns) is enabled
    assert len(get_mihomo_ruleset_downloads(snapshot)) == int(enabled)
    assert len(get_mosdns_ruleset_downloads(snapshot)) == int(enabled)
    assert repository.get_profile('office')['rule_configs'] == before


def test_download_preview_and_public_fetch_share_selected_parameters(generation_app):
    app, repository = generation_app
    before = repository.export_all()
    client = app.test_client()
    for profile_id, port in (('default', 7890), ('office', 7891)):
        headers = {'X-ConfigFlow-Profile': profile_id}
        preview = client.post('/api/generate/mihomo/preview', json={}, headers=headers)
        download = client.post('/api/generate/mihomo', json={}, headers=headers)
        public = client.get(f'/api/config/{profile_id}/mihomo',
                            query_string={'token': 'global+token&secret'},
                            headers={'X-ConfigFlow-Profile': 'missing'})
        assert (preview.status_code, download.status_code, public.status_code) == (200, 200, 200)
        assert preview.json['content'] == download.get_data(as_text=True) == public.get_data(as_text=True)
        assert yaml.safe_load(public.data)['mixed-port'] == port
        surge = client.post('/api/generate/surge', json={}, headers=headers)
        assert surge.status_code == 200
        assert f'http-listen = 127.0.0.1:{port}' in surge.get_data(as_text=True)
        assert yaml.safe_load((repository.generated_dir(profile_id) / 'config.yaml').read_text())['mixed-port'] == port
        assert f'http-listen = 127.0.0.1:{port}' in (repository.generated_dir(profile_id) / 'config.conf').read_text()
        download.close()
        surge.close()
    assert repository.export_all() == before


def test_custom_parameters_update_only_selected_profile(generation_app):
    app, repository = generation_app
    shared = repository.get_shared()
    response = app.test_client().post('/api/custom-config/mihomo', json={'config': 'mixed-port: 9000\n'},
                                     headers={'X-ConfigFlow-Profile': 'office'})
    assert response.status_code == 200
    assert repository.get_profile('office')['mihomo']['custom_config'] == 'mixed-port: 9000\n'
    assert repository.get_profile('default')['mihomo']['custom_config'] == 'mixed-port: 7890\n'
    assert repository.get_shared() == shared


def test_inline_mosdns_rules_need_no_network_and_keep_proxy_auth(generation_app, monkeypatch):
    app, repository = generation_app
    snapshot = repository.get_compat_config('office')
    download = get_mosdns_ruleset_downloads(snapshot)[0]
    monkeypatch.setattr('backend.routes.mosdns._fetch_remote_content',
                        lambda _url: pytest.fail('Inline content must never make a network request'))
    parsed = urlsplit(download['url'])
    client = app.test_client()
    response = client.get(parsed.path + '?' + parsed.query)
    assert response.status_code == 200
    assert response.get_data(as_text=True) == 'full:example.com'
    query = parse_qs(parsed.query)
    denied = client.get(parsed.path, query_string={'url': query['url'][0], 'token': 'wrong'})
    assert denied.status_code == 401


def test_global_backup_attachment_roundtrips_all_profiles_and_secrets(generation_app):
    app, repository = generation_app
    repository.update_system_transaction(lambda system: system.update({
        'backup': {'webdav': {'password': 'backup-secret'}},
        'agents': [{'id': 'agent', 'name': 'Agent', 'token': 'agent-secret', 'profile_id': 'office'}],
    }))
    client = app.test_client()
    exported = client.get('/api/config/export', headers={'X-ConfigFlow-Profile': 'missing'})
    assert exported.status_code == 200
    assert 'attachment' in exported.headers['Content-Disposition']
    document = json.loads(exported.data)
    assert document == repository.export_all()
    assert document['schema_version'] == 5
    assert set(document['profiles']) == {'default', 'office'}
    assert document['system']['agents'][0]['token'] == 'agent-secret'
    assert document['system']['backup']['webdav']['password'] == 'backup-secret'
    redacted = client.get('/api/config/export?desensitize=true')
    assert redacted.status_code == 200
    assert 'agent-secret' not in redacted.get_data(as_text=True)
    assert 'backup-secret' not in redacted.get_data(as_text=True)
    assert client.post('/api/config/reset', headers={'X-ConfigFlow-Profile': 'missing'}).status_code == 200
    assert [profile['id'] for profile in repository.list_profiles()] == ['default']
    reset_token = repository.get_system()['system_config']['rule_proxy_token']
    restored = client.post('/api/config/import', json=document, headers={'X-ConfigFlow-Profile': 'missing'})
    assert restored.status_code == 200
    restored_document = repository.export_all()
    restored_settings = restored_document['system']['system_config']
    assert reset_token in restored_settings.get('retired_rule_proxy_tokens', [])
    assert set(document['system']['system_config'].get('retired_rule_proxy_tokens', [])) <= set(
        restored_settings.get('retired_rule_proxy_tokens', []))
    # Token retirement is deliberately monotonic across reset/import for redaction.
    restored_settings['retired_rule_proxy_tokens'] = document['system']['system_config'].get(
        'retired_rule_proxy_tokens', [])
    document['system']['system_config'].setdefault('retired_rule_proxy_tokens', [])
    for key in ('shared', 'system'):
        assert {k: v for k, v in restored_document[key].items() if k != '_revision'} == {
            k: v for k, v in document[key].items() if k != '_revision'}
    for profile_id, profile in document['profiles'].items():
        assert {k: v for k, v in restored_document['profiles'][profile_id].items() if k != '_revision'} == {
            k: v for k, v in profile.items() if k != '_revision'}
    exported.close()
    redacted.close()


@pytest.mark.parametrize('endpoint,payload,field,value', [
    ('rulesets', {'direct_rulesets': ['ruleset']}, 'direct_rulesets', ['ruleset']),
    ('custom-matches', {'custom_matches': [{'matches': ['qname example.com'], 'exec': 'accept'}]},
     'custom_matches', [{'matches': ['qname example.com'], 'exec': 'accept'}]),
    ('dns-servers', {'local_dns': '223.5.5.5'}, 'local_dns', '223.5.5.5'),
    ('log-settings', {'log_level': 'warn'}, 'log_level', 'warn'),
    ('api-settings', {'api_address': ':9090'}, 'api_address', ':9090'),
    ('cache-settings', {'cache_size': 2048}, 'cache_size', 2048),
])
def test_mosdns_settings_persist_only_selected_profile(generation_app, endpoint, payload, field, value):
    app, repository = generation_app
    other = repository.get_profile('default')
    shared = repository.get_shared()
    response = app.test_client().post(f'/api/mosdns/{endpoint}', json=payload,
                                     headers={'X-ConfigFlow-Profile': 'office'})
    assert response.status_code == 200
    assert repository.get_profile('office')['mosdns'][field] == value
    assert repository.get_profile('default') == other
    assert repository.get_shared() == shared


def test_agent_generator_supports_bound_surge_and_fallback_base_url(generation_app):
    from backend.agents.config_generator import generate_agent_config

    _app, repository = generation_app
    snapshot = repository.get_compat_config('office')
    snapshot['system_config']['server_domain'] = ''
    generated = generate_agent_config(snapshot, {'service_type': 'surge', 'profile_id': 'office'},
                                      base_url='https://agent-entry.example')
    assert generated['service_type'] == 'surge'
    assert 'https://agent-entry.example/api/config/office/surge' in generated['content']
    assert 'http-listen = 127.0.0.1:7891' in generated['content']


@pytest.mark.parametrize('service_type', ['mihomo', 'surge', 'mosdns'])
def test_agent_pull_uses_own_token_and_preserves_exact_bound_bytes(generation_app, monkeypatch, service_type):
    import hashlib
    from backend.agents.config_generator import generate_agent_config
    from backend.common import auth
    from backend.common.agent_manager import init_agent_manager
    from backend.routes.auth import setup_before_request

    app, repository = generation_app
    monkeypatch.setattr(auth, 'ADMIN_USERNAME', 'admin')
    monkeypatch.setattr(auth, 'ADMIN_PASSWORD', 'admin-password')
    setup_before_request(app)
    internal_token = repository.get_system()['system_config']['rule_proxy_token']
    internal_url = f'https://config.example/api/profiles/office/mosdns/rule-proxy?token={internal_token}'
    repository.update_profile_transaction('office', lambda profile: profile['mosdns'].update(
        custom_config=f'download-url: {internal_url}\n'))
    manager = init_agent_manager()
    bound = manager.register_agent({'name': service_type, 'host': '10.0.0.2',
                                    'profile_id': 'office', 'service_type': service_type})
    foreign = manager.register_agent({'name': 'foreign', 'host': '10.0.0.3'})
    agent = manager.get_agent_by_id(bound['id'])
    expected = generate_agent_config(repository.get_compat_config('office'), agent,
                                     base_url='http://localhost')
    client = app.test_client()
    before = repository.get_system()
    for token in (None, 'wrong-token', foreign['token']):
        query = {'profile': 'missing'}
        if token is not None:
            query['token'] = token
        response = client.get(f'/api/agents/{bound["id"]}/config', query_string=query,
                              headers={'X-ConfigFlow-Profile': '../invalid'})
        assert response.status_code == 401
        assert internal_token not in response.get_data(as_text=True)
    assert repository.get_system() == before
    response = client.get(f'/api/agents/{bound["id"]}/config',
                          query_string={'token': bound['token'], 'profile': 'default'},
                          headers={'X-ConfigFlow-Profile': 'missing'})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['profile_id'] == 'office'
    assert payload['content'] == expected['content']
    assert payload['md5'] == hashlib.md5(payload['content'].encode('utf-8')).hexdigest()
    assert payload['version'] == payload['md5'][:8]
    if service_type == 'mosdns':
        assert internal_url in payload['content']
    assert bound['token'] not in response.get_data(as_text=True)
    for token, status in ((foreign['token'], 401), (bound['token'], 200)):
        heartbeat = client.post(f'/api/agents/{bound["id"]}/heartbeat?profile=missing',
                                headers={'Authorization': f'Bearer {token}',
                                         'X-ConfigFlow-Profile': '../invalid'},
                                json={'version': 'bound-agent'})
        assert heartbeat.status_code == status
    assert manager.get_agent_by_id(bound['id'])['profile_id'] == 'office'
