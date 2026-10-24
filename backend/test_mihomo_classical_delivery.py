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
from backend.utils.scheduled_refresh import refresh_rule_source
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

    def scheduled(url):
        response = fetch(url)
        response.raise_for_status()
        return response.content
    # The scheduled task refreshes the cache; device pulls only read it.
    try:
        refresh_rule_source(rule, fetch=scheduled)
    except requests.RequestException:
        assert remote_state != 'fresh'
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
    assert parse_qs(parsed.query) == {'target': ['mihomo']}
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


@pytest.mark.parametrize('source', [SOURCE + '*.manual.test\n', 'payload: ["+.tmdb.org", "192.0.2.8", "*.manual.test"]\n'])
def test_classical_edit_check_keeps_original_content_without_conversion_proposal(delivery, source):
    client, repository, _ = delivery
    original = deepcopy(repository.export_all())
    preview = client.post('/api/rule-library/check-content', json={
        'behavior': 'classical', 'content': source})
    assert preview.status_code == 200
    assert preview.json['content'] == source
    assert preview.json['changes'] == []
    assert not preview.json['errors']
    assert preview.json['warnings'][0]['before'] == '*.manual.test'
    assert repository.export_all() == original


@pytest.mark.parametrize(('source', 'expected'), [
    (SOURCE, SOURCE + '+.added.test\n192.0.2.9\n'),
    ('payload: ["+.tmdb.org"]\n', 'payload: ["+.tmdb.org", "+.added.test", "192.0.2.9"]\n'),
])
def test_classical_append_preview_keeps_bare_original_and_appended_lines(delivery, source, expected):
    client, repository, _ = delivery
    original = deepcopy(repository.export_all())
    preview = client.post('/api/rule-library/check-content', json={
        'behavior': 'classical', 'content': source, 'append_lines': ['+.added.test', '192.0.2.9']})
    assert preview.status_code == 200
    assert preview.json['content'] == expected
    assert preview.json['changes'] == []
    assert not preview.json['errors']
    assert repository.export_all() == original


def _assert_raw_storage_and_mihomo_delivery(client, repository, rule, source, normalized):
    saved = next(item for item in repository.get_shared()['rule_library'] if item['id'] == rule['id'])
    assert saved['content'] == source
    cache = repository.shared_rules_dir() / rule_cache_filename(saved)
    assert cache.read_text() == source
    path = '/api/rules/local/' + rule['name']
    assert client.get(path).get_data(as_text=True) == source
    delivered = client.get(path, query_string={'target': 'mihomo'})
    assert delivered.status_code == 200
    if rule.get('format') == 'yaml':
        actual = delivered.get_data(as_text=True)
        assert yaml.safe_load(actual) == yaml.safe_load(normalized)
        assert not yaml.compose(actual).value[0][1].flow_style
        for line in source.splitlines():
            if line.startswith('#'):
                assert line in actual
    else:
        assert delivered.get_data(as_text=True) == normalized
    assert cache.read_text() == source
    assert next(item for item in repository.get_shared()['rule_library'] if item['id'] == rule['id'])['content'] == source


@pytest.mark.parametrize(('source', 'normalized', 'rule_format'), [
    (SOURCE, EXPECTED, 'text'),
    ('payload: ["+.tmdb.org", "192.0.2.8"]\n',
     'payload: ["DOMAIN-SUFFIX,tmdb.org", "IP-CIDR,192.0.2.8/32,no-resolve"]\n', 'yaml'),
])
def test_classical_create_update_and_draft_save_keep_raw_content(delivery, source, normalized, rule_format):
    client, repository, rule = delivery
    new_rule = {**rule, 'id': 'new', 'name': 'New', 'content': source, 'format': rule_format}
    assert client.post('/api/rule-library', json=new_rule).status_code == 200
    _assert_raw_storage_and_mihomo_delivery(client, repository, new_rule, source, normalized)

    updated = {**rule, 'content': source + '# put\n', 'format': rule_format}
    assert client.put('/api/rule-library/mixed', json=updated).status_code == 200
    _assert_raw_storage_and_mihomo_delivery(client, repository, updated, updated['content'], normalized + '# put\n')

    draft = client.get('/api/rule-library/mixed/draft-content').json
    assert draft['rule']['format'] == rule_format
    source += '# draft\n'
    assert client.put('/api/rule-library/mixed/draft-content', json={
        'revision': draft['revision'], 'content': source}).status_code == 200
    _assert_raw_storage_and_mihomo_delivery(client, repository, updated, source, normalized + '# draft\n')


def test_classical_compose_preserves_original_source_and_cache(delivery):
    client, repository, rule = delivery
    response = client.post('/api/rules/compose', json={
        'source': {'kind': 'new_library', 'library': {**rule, 'name': 'Composed'}},
        'rule': {'policy': 'DIRECT'}, 'dns': 'default',
    })
    assert response.status_code == 200, response.json
    created = response.json['data']['library']
    assert created['content'] == SOURCE
    _assert_raw_storage_and_mihomo_delivery(client, repository, created, SOURCE, EXPECTED)


def test_classical_malformed_payload_is_still_rejected_before_any_save(delivery):
    client, repository, rule = delivery
    original = deepcopy(repository.export_all())
    content = 'payload: [true]'
    preview = client.post('/api/rule-library/check-content', json={
        'behavior': 'classical', 'content': content})
    assert preview.status_code == 200
    assert preview.json['content'] == content and preview.json['errors']
    invalid = {**rule, 'content': content}
    assert client.post('/api/rule-library', json={**invalid, 'id': 'new', 'name': 'New'}).status_code == 400
    assert client.put('/api/rule-library/mixed', json=invalid).status_code == 400
    draft = client.get('/api/rule-library/mixed/draft-content').json
    assert client.put('/api/rule-library/mixed/draft-content', json={
        'revision': draft['revision'], 'content': content}).status_code == 400
    assert client.post('/api/rules/compose', json={
        'source': {'kind': 'new_library', 'library': {**invalid, 'name': 'Invalid'}},
        'rule': {'policy': 'DIRECT'}, 'dns': 'default',
    }).status_code == 400
    assert repository.export_all() == original


@pytest.mark.parametrize('behavior,payload', [
    ('classical', ['+.tmdb.org', '192.0.2.1']),
    ('domain', ['+.tmdb.org']), ('ipcidr', ['192.0.2.0/24']),
])
@pytest.mark.parametrize('declared', [None, '', 'yaml', 'text'])
def test_provider_format_infers_handwritten_yaml_only_when_unspecified(delivery, behavior, payload, declared):
    client, repository, rule = delivery
    source = yaml.safe_dump({'payload': payload}, sort_keys=False)
    rule.update(content=source, behavior=behavior)
    rule.pop('format', None)
    if declared is not None:
        rule['format'] = declared
    repository.save_shared({'rule_library': [rule]})
    before = deepcopy(repository.export_all())
    config = repository.get_compat_config('default')
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    expected = declared or 'yaml'
    assert main['rule-providers']['Mixed']['format'] == expected
    downloads = get_mihomo_ruleset_downloads(config)
    assert downloads[0]['local_path'].endswith('.list' if expected == 'text' else '.yaml')
    assert repository.export_all() == before
    assert client.get('/api/rules/local/Mixed').get_data(as_text=True) == source


@pytest.mark.parametrize('behavior,raw,expected', [
    ('classical', '+.tmdb.org', 'DOMAIN-SUFFIX,tmdb.org'),
    ('domain', '+.tmdb.org', '+.tmdb.org'),
    ('ipcidr', '192.0.2.0/24', '192.0.2.0/24'),
])
@pytest.mark.parametrize('source_type', ['content', 'url'])
def test_mihomo_flow_yaml_is_expanded_only_in_marked_response(delivery, monkeypatch, behavior, raw, expected, source_type):
    client, repository, rule = delivery
    source = '# original\npayload: ["' + raw + '"] # keep\n'
    rule.update(content=source, source_type=source_type, behavior=behavior, format='yaml', url='https://rules.test/payload.yaml')
    repository.save_shared({'rule_library': [rule]})
    response = requests.Response()
    response.status_code = 200
    response.encoding = 'utf-8'
    response._content = source.encode()
    monkeypatch.setattr('backend.routes.rules.request_rule', lambda *_a, **_k: response)
    config = repository.get_compat_config('default')
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    provider = main['rule-providers']['Mixed']
    assert parse_qs(urlsplit(provider['url']).query)['target'] == ['mihomo']
    assert get_mihomo_ruleset_downloads(config)[0]['url'] == provider['url']
    delivered = client.get('/api/rules/local/Mixed?target=mihomo').get_data(as_text=True)
    assert yaml.safe_load(delivered)['payload'] == [expected]
    assert not yaml.compose(delivered).value[0][1].flow_style
    assert '# original' in delivered and '# keep' in delivered
    for target in ('', '?target=oxidns', '?target=mosdns', '?target=surge'):
        assert client.get('/api/rules/local/Mixed' + target).get_data(as_text=True) == source
    assert repository.get_shared()['rule_library'][0]['content'] == source
    assert (repository.shared_rules_dir() / rule_cache_filename(rule)).read_text() == source
