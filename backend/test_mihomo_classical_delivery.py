"""Only Mihomo delivery repairs classical sources; shared bytes stay original."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import yaml
from flask import Flask

from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.converters.mihomo import generate_mihomo_config, get_mihomo_ruleset_downloads
from backend.routes import register_blueprints
from backend.utils.rule_utils import rule_cache_filename, save_rule_to_local


SOURCE = '# keep\n+.tmdb.org\nexact.test\n192.0.2.8\n2001:db8::8\nDOMAIN,typed.test\n'
EXPECTED = ('# keep\nDOMAIN-SUFFIX,tmdb.org\nDOMAIN,exact.test\n'
            'IP-CIDR,192.0.2.8/32,no-resolve\nIP-CIDR6,2001:db8::8/128,no-resolve\nDOMAIN,typed.test\n')


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    repository = ProfileRepository(tmp_path)
    monkeypatch.setattr(config_store, '_repository', repository)
    config_store.reset_config_context()
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: False)
    rule = {'id': 'mixed', 'name': 'Mixed', 'source_type': 'content', 'content': SOURCE,
            'behavior': 'classical', 'format': 'text', 'enabled': True}
    repository.save_shared({'rule_library': [rule]})
    repository.save_profile('default', {'rule_configs': [
        {'id': 'ref', 'itemType': 'ruleset', 'library_rule_id': 'mixed', 'policy': 'DIRECT', 'enabled': True}]})
    app = Flask(__name__)
    register_blueprints(app)
    yield app.test_client(), repository, rule
    config_store.reset_config_context()


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('yaml_payload', [False, True])
def test_manual_source_is_normalized_only_in_mihomo_response(delivery, cached, yaml_payload):
    client, repository, rule = delivery
    source, expected = SOURCE, EXPECTED
    if yaml_payload:
        rule.update(content=yaml.safe_dump({'payload': SOURCE.splitlines()[1:]}), format='yaml')
        source = rule['content']
        repository.save_shared({'rule_library': [rule]})
    if cached:
        save_rule_to_local(rule)
    before = deepcopy(repository.export_all())
    response = client.get('/api/profiles/default/rules/local/Mixed?target=mihomo')
    assert response.status_code == 200
    if yaml_payload:
        assert yaml.safe_load(response.data)['payload'] == EXPECTED.splitlines()[1:]
    else:
        assert response.get_data(as_text=True) == expected
    assert int(response.headers['Content-Length']) == len(response.data)
    assert repository.export_all() == before
    assert (repository.shared_rules_dir() / rule_cache_filename(rule)).read_text() == source
    assert client.get('/api/rules/local/Mixed').get_data(as_text=True) == source
    assert client.get('/api/rule-library/content/mixed').get_data(as_text=True) == source


@pytest.mark.parametrize('remote_state', ['fresh', 'timeout', 'error'])
def test_url_source_and_fallback_keep_raw_cache(delivery, monkeypatch, remote_state):
    client, repository, rule = delivery
    rule.update(source_type='url', url='https://rules.test/mixed.list')
    rule.pop('content')
    repository.save_shared({'rule_library': [rule]})
    cache = repository.shared_rules_dir() / rule_cache_filename(rule)
    repository.write_shared_text('rules/' + rule_cache_filename(rule), SOURCE if remote_state != 'fresh' else 'old.test')
    calls = []

    def fetch(url, **kwargs):
        calls.append(url)
        if remote_state == 'timeout':
            raise requests.Timeout()
        response = requests.Response()
        response.status_code = 503 if remote_state == 'error' else 200
        response.encoding = 'utf-8'
        response._content = SOURCE.encode()
        return response

    monkeypatch.setattr('backend.routes.rules.request_rule', fetch)
    before = deepcopy(repository.export_all())
    response = client.get('/api/rules/local/Mixed?target=mihomo')
    assert response.status_code == 200
    assert response.get_data(as_text=True) == EXPECTED
    assert int(response.headers['Content-Length']) == len(response.data)
    assert calls == [rule['url']]
    assert cache.read_text() == SOURCE
    assert repository.export_all() == before


@pytest.mark.parametrize('target', ['', 'surge', 'loon', 'quantumult-x', 'sing-box', 'mosdns', 'oxidns'])
def test_other_clients_receive_original_classical_bytes(delivery, target):
    client, _, _ = delivery
    response = client.get('/api/rules/local/Mixed', query_string={'target': target})
    assert response.status_code == 200
    assert response.get_data(as_text=True) == SOURCE


@pytest.mark.parametrize('behavior,rule_format', [('domain', 'text'), ('ipcidr', 'text'), ('classical', 'mrs')])
def test_non_classical_or_binary_formats_are_unchanged(delivery, behavior, rule_format):
    client, repository, rule = delivery
    rule.update(behavior=behavior, format=rule_format)
    repository.save_shared({'rule_library': [rule]})
    response = client.get('/api/rules/local/Mixed?target=mihomo')
    assert response.status_code == 200
    assert response.get_data(as_text=True) == SOURCE


@pytest.mark.parametrize('behavior', ['classical', 'domain', 'ipcidr'])
def test_generated_provider_and_download_share_explicit_mihomo_target(delivery, behavior):
    _, repository, rule = delivery
    rule['behavior'] = behavior
    repository.save_shared({'rule_library': [rule]})
    config = repository.get_compat_config('default')
    config['mihomo'] = {'custom_config': 'mixed-port: 0\ndns: {enable: false}\n'}
    config['system_config']['server_domain'] = 'https://config.test/prefix'
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    provider = main['rule-providers']['Mixed']
    download = get_mihomo_ruleset_downloads(config)[0]
    assert provider['url'] == download['url']
    parsed = urlsplit(provider['url'])
    assert parsed.path == '/prefix/api/profiles/default/rules/local/Mixed'
    assert parse_qs(parsed.query) == ({'target': ['mihomo']} if behavior == 'classical' else {})
    assert provider['behavior'] == behavior


def test_dns_converters_keep_original_matching_semantics_after_mihomo_download(delivery):
    from backend.agents.deployment_bundle import convert_mosdns_rules
    from backend.converters.mosdns import convert_rule_content_for_mosdns

    client, repository, _ = delivery
    assert client.get('/api/rules/local/Mixed?target=mihomo').status_code == 200
    original = repository.get_shared()['rule_library'][0]['content']
    expected = 'domain:tmdb.org\nfull:exact.test\nfull:typed.test'
    assert convert_rule_content_for_mosdns(original, 'domain') == expected
    assert convert_mosdns_rules(original.encode()).decode() == expected


def test_classical_edit_preview_requires_explicit_conversion_before_save(delivery):
    client, repository, rule = delivery
    original = deepcopy(repository.export_all())
    preview = client.post('/api/rule-library/check-content', json={
        'behavior': 'classical', 'content': SOURCE + '*.manual.test\n'})
    assert preview.status_code == 200
    assert preview.json['content'] == EXPECTED + '*.manual.test\n'
    assert len(preview.json['changes']) == 4
    assert preview.json['warnings'][0]['before'] == '*.manual.test'
    assert repository.export_all() == original
    new_rule = {**rule, 'id': 'new', 'name': 'New'}
    assert client.post('/api/rule-library', json=new_rule).status_code == 400
    assert client.put('/api/rule-library/mixed', json={**rule, 'content': SOURCE + 'new.test'}).status_code == 400
    draft = client.get('/api/rule-library/mixed/draft-content').json
    assert draft['rule']['format'] == 'text'
    assert client.put('/api/rule-library/mixed/draft-content', json={
        'revision': draft['revision'], 'content': SOURCE + 'new.test'}).status_code == 400
    assert repository.export_all() == original
    new_rule['content'] = preview.json['content']
    assert client.post('/api/rule-library', json=new_rule).status_code == 200
    assert next(item for item in repository.get_shared()['rule_library'] if item['id'] == 'new')['content'] == new_rule['content']
    # Existing malformed sources can still be renamed/disabled without repair.
    assert client.put('/api/rule-library/mixed', json={**rule, 'enabled': False}).status_code == 200
    assert next(item for item in repository.get_shared()['rule_library'] if item['id'] == 'mixed')['content'] == SOURCE
