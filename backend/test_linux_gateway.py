"""Gateway-only generation, stable runtime updates, isolation and atomic bundles."""
from copy import deepcopy
import io
import tarfile
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml
from flask import Flask

from backend.agents.deployment_bundle import (
    build_deployment_bundle, DeploymentPreparationError, materialize_rule,
)
from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.common.linux_gateway import (
    FAKE_PROVIDER, LinuxGatewayError, apply_linux_gateway, extract_gateway_cidrs,
    gateway_editor_state, gateway_provider_payload, gateway_provider_yaml,
    gateway_source_yaml, normalize_gateway_settings, provider_name, validate_gateway_selection,
)
from backend.converters.mihomo import generate_mihomo_config, get_mihomo_ruleset_downloads
from backend.routes import register_blueprints


def snapshot(*, enabled=True, selected=None):
    return {
        'profile_id': 'home',
        'system_config': {'server_domain': 'https://config.example', 'config_token': 'a+b&c'},
        'mihomo': {'custom_config': 'mixed-port: 7890\n',
                   'linux_gateway': {'enabled': enabled, 'rule_set_ids': ['r'] if selected is None else selected}},
        'rule_configs': [{'id': 'r', 'itemType': 'ruleset', 'name': 'Proxy IPs',
                          'library_rule_id': 'lib', 'behavior': 'ipcidr', 'policy': 'DIRECT', 'enabled': True}],
        'rule_library': [{'id': 'lib', 'name': 'Proxy IPs', 'behavior': 'ipcidr',
                          'source_type': 'content', 'content': '1.1.1.0/24\n8.8.8.8', 'enabled': True}],
    }


def generated(config):
    return yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))


def test_empty_stable_id_cache_survives_source_rename(tmp_path, monkeypatch):
    from backend.utils.rule_utils import rule_cache_filename

    config = snapshot()
    source = config['rule_library'][0]
    source.update(source_type='url', url='https://rules.example/ip.txt')
    source.pop('content')
    cache = tmp_path / rule_cache_filename(source)
    cache.parent.mkdir(parents=True)
    cache.write_text('')
    source['name'] = 'Renamed source'
    monkeypatch.setattr('backend.routes.rules.get_ruleset_content', lambda *args, **kwargs: '')
    monkeypatch.setattr('backend.utils.rule_utils.get_rules_dir', lambda: str(tmp_path))
    assert gateway_provider_payload(config, 'r') == []
    (tmp_path / rule_cache_filename(source)).unlink()
    with pytest.raises(LinuxGatewayError, match='无法读取'):
        gateway_provider_payload(config, 'r')


def test_disabled_preserves_generated_bytes_and_never_mutates_input():
    config = snapshot(enabled=False)
    config['mihomo']['custom_config'] = '''mixed-port: 7890
ipv6: true
dns: {listen: '0.0.0.0:53', enhanced-mode: redir-host}
tun: {enable: false, route-address: [10.0.0.0/8]}
'''
    original = deepcopy(config)
    with_setting = generate_mihomo_config(config, preflight_providers=False)
    without_setting = deepcopy(config)
    del without_setting['mihomo']['linux_gateway']
    assert with_setting == generate_mihomo_config(without_setting, preflight_providers=False)
    assert get_mihomo_ruleset_downloads(config) == get_mihomo_ruleset_downloads(without_setting)
    assert config == original
    doc = {'dns': {'listen': ':53'}, 'tun': {'enable': False}}
    before = deepcopy(doc)
    apply_linux_gateway(doc, config)
    assert doc == before


def test_selective_output_contains_fake_and_stable_tokenized_providers():
    config = snapshot()
    config['mihomo']['custom_config'] = '''dns: {fake-ip-range: 198.19.0.1/24, nameserver: [223.5.5.5]}
tun:
  mtu: 1400
  route-address: [198.18.0.0/16]
  inet4-route-address: [0.0.0.0/1]
  route-exclude-address-set: [old]
'''
    before = deepcopy(config)
    doc = generated(config)
    assert doc['tun']['route-address-set'] == [FAKE_PROVIDER, provider_name('r')]
    assert doc['tun']['mtu'] == 1400
    assert doc['tun']['auto-route'] is doc['tun']['auto-redirect'] is True
    assert doc['tun']['strict-route'] is False
    assert doc['tun']['dns-hijack'] == []
    assert 'route-address' not in doc['tun'] and 'inet4-route-address' not in doc['tun']
    assert doc['dns']['listen'] == '127.0.0.1:1053'
    assert doc['dns']['nameserver'] == ['223.5.5.5']
    assert doc['dns']['ipv6'] is doc['ipv6'] is False
    assert doc['rule-providers'][FAKE_PROVIDER]['payload'] == ['198.19.0.0/24']
    provider = doc['rule-providers'][provider_name('r')]
    assert urlsplit(provider['url']).path == '/api/profiles/home/linux-gateway/rules/r'
    assert parse_qs(urlsplit(provider['url']).query) == {'token': ['a+b&c']}
    assert provider['interval'] == 300
    assert '/linux-gateway/source/r?' in doc['rule-providers']['Proxy IPs']['url']
    assert doc['x-configflow-linux-gateway']['profile_id'] == 'home'
    assert doc['x-configflow-linux-gateway']['provider_prefix'] == 'cf_gateway_'
    assert config == before
    assert get_mihomo_ruleset_downloads(config) == [
        {'name': name, 'url': item['url'], 'local_path': item['path']}
        for name, item in doc['rule-providers'].items() if item['type'] == 'http']


def test_fake_only_and_disappeared_rules_never_make_capture_unrestricted():
    for selected in ([], ['deleted']):
        config = snapshot(selected=selected)
        doc = generated(config)
        assert doc['tun']['route-address-set'][0] == FAKE_PROVIDER
        assert doc['rule-providers'][FAKE_PROVIDER]['payload'] == ['198.18.0.0/16']
        assert gateway_provider_payload(config, 'deleted') == []
    state = gateway_editor_state(snapshot(selected=['deleted']))
    assert state['candidates'][-1]['available'] is False
    assert '已删除' in state['candidates'][-1]['reason']


def test_rename_updates_and_stops_using_latest_library_state():
    config = snapshot()
    first = generated(config)['rule-providers'][provider_name('r')]
    config['rule_library'][0].update(name='Renamed', content='9.9.9.0/24')
    config['rule_configs'][0]['name'] = 'Renamed'
    assert generated(config)['rule-providers'][provider_name('r')] == first
    assert gateway_provider_payload(config, 'r') == ['9.9.9.0/24']
    assert yaml.safe_load(gateway_source_yaml(config, 'r')) == {'payload': ['9.9.9.0/24']}
    for target in (config['rule_configs'][0], config['rule_library'][0]):
        target['enabled'] = False
        assert gateway_provider_payload(config, 'r') == []
        assert yaml.safe_load(gateway_source_yaml(config, 'r')) == {'payload': []}
        target['enabled'] = True
    config['mihomo']['linux_gateway']['enabled'] = False
    assert gateway_provider_payload(config, 'r') == []
    config['mihomo']['linux_gateway']['enabled'] = True
    config['rule_configs'] = []
    assert gateway_provider_payload(config, 'r') == []


def test_classical_only_extracts_unconditional_destination_ipv4():
    content = '''payload:
- IP-CIDR,1.1.1.7/24,no-resolve
- IP-CIDR6,2001:db8::/32,no-resolve
- SRC-IP-CIDR,10.0.0.0/8
- DOMAIN,example.test
- GEOIP,US
- AND,((NETWORK,UDP),(IP-CIDR,8.8.8.8/32))
- OR,((IP-CIDR,9.9.9.9/32),(DST-PORT,443))
'''
    assert extract_gateway_cidrs(content, 'classical') == ['1.1.1.0/24']
    assert extract_gateway_cidrs('payload: []', 'ipcidr') == []
    assert extract_gateway_cidrs('2001:db8::/32\n1.0.0.0/24', 'ipcidr') == ['1.0.0.0/24']


@pytest.mark.parametrize('content,behavior', [
    ('0.0.0.0/0', 'ipcidr'), ('0.0.0.0/1\n128.0.0.0/1', 'ipcidr'),
    ('1.1.1.999/24', 'ipcidr'), ('1.1.1.0/33', 'ipcidr'),
    ('IP-CIDR,1.1.1.0/24,DIRECT', 'classical'), ('SRC-IP-CIDR,1.1.1.0/24', 'ipcidr'),
    ('payload: [1, 2]', 'ipcidr'), ('payload: [{IP: test}]', 'ipcidr'),
    ('\x00MRS', 'ipcidr'), ('payload: &a [*a]', 'ipcidr'),
])
def test_unsafe_or_malformed_rules_rejected(content, behavior):
    with pytest.raises(LinuxGatewayError):
        extract_gateway_cidrs(content, behavior)


@pytest.mark.parametrize('custom', [
    'routing-mark: 100', 'proxies: [{routing-mark: 100}]',
    'proxy-providers: {test: {override: {routing-mark: 100}}}',
    'dns: {fake-ip-range: 0.0.0.0/0}', 'dns: {fake-ip-range: "::/0"}',
    'dns: {fake-ip-range: 127.0.0.1/8}', 'dns: {fake-ip-range: invalid}',
    'tun: invalid',
])
def test_enabled_conflicts_rejected_without_altering_saved_base(custom):
    config = snapshot()
    config['mihomo']['custom_config'] = custom
    before = deepcopy(config)
    with pytest.raises(LinuxGatewayError):
        validate_gateway_selection(config, config['mihomo']['linux_gateway'])
    assert config == before


def test_candidate_types_missing_sources_and_validation():
    config = snapshot()
    config['rule_library'][0]['url'] = 'https://rules.test/ip.mrs?foo=1'
    state = gateway_editor_state(config)
    assert state['candidates'][0]['available'] is False
    assert 'MRS' in state['candidates'][0]['reason']
    assert gateway_provider_payload(config, 'r') == []
    config['mihomo']['linux_gateway']['rule_set_ids'] = []
    with pytest.raises(LinuxGatewayError, match='MRS'):
        validate_gateway_selection(config, {'enabled': True, 'rule_set_ids': ['r']})
    with pytest.raises(LinuxGatewayError, match='不存在'):
        validate_gateway_selection(config, {'enabled': True, 'rule_set_ids': ['other-profile']})
    for value in ({'enabled': 'false'}, {'rule_set_ids': 'r'}, {'rule_set_ids': [True]}, {'rule_set_ids': ['\x00']}):
        with pytest.raises(LinuxGatewayError):
            normalize_gateway_settings(value)


@pytest.fixture
def gateway_app(tmp_path):
    repository = ProfileRepository(tmp_path)
    repository.create_profile({'id': 'home', 'name': 'Home'})
    config = snapshot()
    repository.save_shared({'rule_library': config['rule_library']})
    for profile_id in ('home', 'default'):
        repository.save_profile(profile_id, {key: deepcopy(config[key]) for key in ('mihomo', 'rule_configs')})
    repository.update_system_transaction(lambda value: value['system_config'].update(config['system_config']))
    config_store.set_repository(repository)
    app = Flask(__name__)
    app.config['TESTING'] = True
    register_blueprints(app)
    yield app, repository
    config_store.reset_config_context()


def test_api_profile_isolation_auth_and_runtime_updates(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    headers = {'X-ConfigFlow-Profile': 'home'}
    before = repository.get_profile('default')
    state = client.get('/api/custom-config/linux-gateway', headers=headers)
    assert state.status_code == 200 and state.json['settings']['enabled'] is True
    response = client.post('/api/custom-config/linux-gateway', headers=headers,
                           json={'enabled': True, 'rule_set_ids': ['r']})
    assert response.status_code == 200, response.json
    assert response.json['publish_guard']['expected_profile_id'] == 'home'
    assert response.json['publish_guard']['expected_service_type'] == 'mihomo'
    assert repository.get_profile('default') == before
    url = '/api/profiles/home/linux-gateway/rules/r'
    assert client.get(url).status_code == 401
    assert client.get(url, query_string={'token': 'bad'}).status_code == 401
    def payload(path=url):
        response = client.get(path, query_string={'token': 'a+b&c'}, headers={'X-ConfigFlow-Profile': 'missing'})
        assert response.status_code == 200, response.get_data(as_text=True)
        assert response.headers['Cache-Control'] == 'no-store'
        return yaml.safe_load(response.get_data(as_text=True))['payload']
    assert payload() == ['1.1.1.0/24', '8.8.8.8/32']
    repository.update_shared_transaction(lambda shared: shared['rule_library'][0].update(name='Renamed', content='9.9.9.9/32'))
    assert payload() == ['9.9.9.9/32']
    assert payload('/api/profiles/home/linux-gateway/source/r') == ['9.9.9.9/32']
    repository.update_profile_transaction('home', lambda profile: profile['rule_configs'][0].update(enabled=False))
    assert payload() == []
    assert payload('/api/profiles/home/linux-gateway/source/r') == []
    assert payload('/api/profiles/default/linux-gateway/rules/r') == ['9.9.9.9/32']
    repository.update_shared_transaction(lambda shared: shared['rule_library'][0].update(enabled=False))
    assert payload('/api/profiles/default/linux-gateway/rules/r') == []


def test_api_rejects_invalid_input_atomically(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    before = repository.export_all()
    for value in (None, [], {'enabled': 'yes'}, {'enabled': True, 'rule_set_ids': ['foreign']}):
        response = client.post('/api/custom-config/linux-gateway', headers={'X-ConfigFlow-Profile': 'home'}, json=value)
        assert response.status_code == 400
        assert repository.export_all() == before


def test_bundle_freezes_both_provider_sources_without_self_http(gateway_app, monkeypatch):
    _app, repository = gateway_app
    config = repository.get_compat_config('home')
    original = deepcopy(config)
    content = generate_mihomo_config(config, preflight_providers=False)
    repository.update_shared_transaction(lambda shared: shared['rule_library'][0].update(content='9.9.9.9/32'))
    monkeypatch.setattr('backend.agents.deployment_bundle.request_rule', lambda *a, **kw: pytest.fail('self HTTP'))
    bundle = build_deployment_bundle({'id': 'agent', 'service_type': 'mihomo', 'profile_id': 'home'}, config, content)
    with tarfile.open(fileobj=io.BytesIO(bundle.archive), mode='r:gz') as archive:
        files = {item.name: archive.extractfile(item).read() for item in archive.getmembers()}
    assert yaml.safe_load(files[f'files/ruleset/{provider_name("r")}.yaml']) == {'payload': ['1.1.1.0/24', '8.8.8.8/32']}
    assert yaml.safe_load(files['files/ruleset/Proxy IPs.list']) == {'payload': ['1.1.1.0/24', '8.8.8.8/32']}
    main = yaml.safe_load(files['files/config.yaml'])
    assert main['rule-providers'][FAKE_PROVIDER]['payload'] == ['198.18.0.0/16']
    assert config == original
    with pytest.raises(DeploymentPreparationError, match='snapshot'):
        materialize_rule('https://config.example/api/profiles/default/linux-gateway/rules/r', config, '')


def test_bundle_rejects_routing_mark_hidden_in_subscription_provider(gateway_app):
    _app, repository = gateway_app
    config = repository.get_compat_config('home')
    with pytest.raises(DeploymentPreparationError, match='routing-mark'):
        build_deployment_bundle({'id': 'agent', 'service_type': 'mihomo', 'profile_id': 'home'}, config,
            'mode: rule', provider_downloads=[{'local_path': './providers/sub.yaml',
                'content': 'proxies: [{name: marked, type: direct, routing-mark: 123}]'}])


def test_original_source_comments_do_not_create_invalid_yaml_rules():
    config = snapshot()
    config['rule_library'][0]['content'] = '# comments\n1.1.1.0/24 # comment\n8.8.8.8\n'
    assert yaml.safe_load(gateway_source_yaml(config, 'r')) == {'payload': ['1.1.1.0/24', '8.8.8.8/32']}


def test_source_and_capture_share_single_ip_and_cidr_semantics():
    config = snapshot()
    config['rule_library'][0]['content'] = '8.8.8.8\n2001:db8::1\n9.9.9.7/24\n'
    assert gateway_provider_payload(config, 'r') == ['8.8.8.8/32', '9.9.9.0/24']
    assert yaml.safe_load(gateway_source_yaml(config, 'r'))['payload'] == [
        '8.8.8.8/32', '2001:db8::1/128', '9.9.9.0/24']
    config['rule_library'][0].update(behavior='classical', content='IP-CIDR,8.8.8.8,no-resolve\nIP-CIDR6,2001:db8::1\n')
    assert yaml.safe_load(gateway_source_yaml(config, 'r'))['payload'] == [
        'IP-CIDR,8.8.8.8/32,no-resolve', 'IP-CIDR6,2001:db8::1/128']
    for content in ('IP-CIDR6,8.8.8.8/32', 'IP-CIDR,2001:db8::/32'):
        with pytest.raises(LinuxGatewayError, match='版本一致'):
            extract_gateway_cidrs(content, 'classical')


def test_original_classical_source_preserves_non_ip_conditions():
    config = snapshot()
    config['rule_library'][0].update(behavior='classical', content='DOMAIN-REGEX,^x#y$\nIP-CIDR,1.1.1.0/24 # comment\n')
    assert yaml.safe_load(gateway_source_yaml(config, 'r')) == {'payload': ['DOMAIN-REGEX,^x#y$', 'IP-CIDR,1.1.1.0/24']}


def test_rule_mode_is_forced_only_while_gateway_is_enabled():
    config = snapshot()
    config['mihomo']['custom_config'] = 'mode: global\n'
    assert generated(config)['mode'] == 'rule'
    config['mihomo']['linux_gateway']['enabled'] = False
    assert generated(config)['mode'] == 'global'


@pytest.mark.parametrize('key,value', [('exclude-interface', ['eth0']), ('include-interface', ['eth1']),
                                     ('exclude-mac-address', ['00:11:22:33:44:55']), ('include-uid', [0])])
def test_gateway_rejects_old_tun_filters_without_altering_disabled_mode(key, value):
    config = snapshot()
    config['mihomo']['custom_config'] = yaml.safe_dump({'tun': {key: value}})
    with pytest.raises(LinuxGatewayError, match=key):
        generated(config)
    config['mihomo']['linux_gateway']['enabled'] = False
    assert generated(config)['tun'][key] == value


def test_disabling_is_always_possible_after_selected_source_changes_type():
    config = snapshot()
    config['rule_library'][0]['behavior'] = 'domain'
    assert validate_gateway_selection(config, {'enabled': False, 'rule_set_ids': ['r']}) == {'enabled': False, 'rule_set_ids': ['r']}
    assert gateway_provider_payload(config, 'r') == []
    # Existing unavailable references are editable/removable, never promoted.
    assert validate_gateway_selection(config, {'enabled': True, 'rule_set_ids': ['r']})['enabled'] is True


def test_multiple_huge_rule_sources_cannot_combine_into_full_capture():
    for content in ('0.0.0.0/1', '128.0.0.0/1', '128.0.0.0/2', '0.0.0.0/7'):
        with pytest.raises(LinuxGatewayError, match='/8'):
            extract_gateway_cidrs(content, 'ipcidr')


def test_runtime_tokens_work_with_production_global_auth_middleware(gateway_app, monkeypatch):
    from backend.common.auth import generate_token
    from backend.routes.auth import setup_before_request
    app, _repository = gateway_app
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: True)
    monkeypatch.setattr('backend.routes.auth.is_auth_enabled', lambda: True)
    setup_before_request(app)
    client = app.test_client()
    for prefix in ('/api/profiles/home', '/api/custom-config'):
        for resource in ('rules', 'source'):
            path = f'{prefix}/linux-gateway/{resource}/r'
            assert client.get(path).status_code == 401
            assert client.get(path, query_string={'token': 'a+b&c'}).status_code == 200
            assert client.get(path, query_string={'token': 'wrong'}).status_code == 401
    editor = '/api/custom-config/linux-gateway'
    assert client.get(editor, query_string={'token': 'a+b&c'}).status_code == 401
    assert client.get(editor, headers={'Authorization': 'Bearer ' + generate_token('admin')}).status_code == 200


def test_auth_enabled_requires_refresh_token_for_selected_real_ip_sets(monkeypatch):
    config = snapshot()
    config['system_config']['config_token'] = ''
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: True)
    with pytest.raises(LinuxGatewayError, match='订阅令牌'):
        generated(config)
    config['mihomo']['linux_gateway']['rule_set_ids'] = []
    assert generated(config)['tun']['route-address-set'] == [FAKE_PROVIDER]
