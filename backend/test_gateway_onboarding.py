"""Install preparation stays read-only until one explicit, scoped transaction."""
from copy import deepcopy

import pytest
import yaml
from flask import Flask

from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.common.gateway_onboarding import (
    DEFAULT_LOCAL_DNS, GatewayOnboardingError, assert_gateway_onboarding_ready,
    gateway_onboarding_state, prepare_gateway_onboarding,
)
from backend.common.oxidns_editor import prepare_editor
from backend.routes import register_blueprints


def snapshot():
    value = config_store.get_default_config()
    value.update(profile_id='home',
        nodes=[{'id': 'n', 'name': 'Node', 'type': 'ss', 'server': '192.0.2.1',
                'port': 443, 'cipher': 'aes-128-gcm', 'password': 'test'}],
        proxy_groups=[{'id': 'g', 'name': 'Proxy', 'type': 'select', 'manual_nodes': ['n']}],
        rule_configs=[{'id': 'ip', 'itemType': 'ruleset', 'library_rule_id': 'lib-ip',
                       'name': 'Proxy IP', 'behavior': 'ipcidr', 'policy': 'Proxy',
                       'source_type': 'content', 'content': '1.1.1.0/24', 'url': '/api/rules/Proxy%20IP'},
                      {'id': 'direct', 'itemType': 'ruleset', 'library_rule_id': 'lib-direct',
                       'name': 'Direct Domains', 'behavior': 'domain', 'policy': 'DIRECT',
                       'url': '/api/rules/Direct%20Domains'},
                      {'id': 'proxy', 'itemType': 'ruleset', 'library_rule_id': 'lib-proxy',
                       'name': 'Proxy Domains', 'behavior': 'domain', 'policy': 'Proxy',
                       'url': '/api/rules/Proxy%20Domains'},
                      {'id': 'match', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'Proxy'}],
        rule_library=[{'id': 'lib-ip', 'name': 'Proxy IP', 'behavior': 'ipcidr',
                       'source_type': 'content', 'content': '1.1.1.0/24'},
                      {'id': 'lib-direct', 'name': 'Direct Domains', 'behavior': 'domain',
                       'source_type': 'content', 'content': '+.direct.test'},
                      {'id': 'lib-proxy', 'name': 'Proxy Domains', 'behavior': 'domain',
                       'source_type': 'content', 'content': '+.proxy.test'}])
    # Real repositories provision this internal capability separately from the
    # subscription token; direct converter fixtures need the same prerequisite.
    value['system_config'].update(server_domain='https://config.example', config_token='subscription-token',
                                  rule_proxy_token='test-internal-rule-proxy-token')
    value['mihomo']['custom_config'] = 'mixed-port: 7890\nlog-level: warning\n'
    return value


def form(**overrides):
    return {'rule_set_ids': ['ip'], 'direct_rule_set_ids': ['direct'],
            'proxy_rule_set_ids': ['proxy'], 'local_dns': DEFAULT_LOCAL_DNS, **overrides}


def plugin(document, tag):
    return next(item for item in document['plugins'] if item['tag'] == tag)


def disable_native_fallback(settings):
    document = yaml.safe_load(settings['native_config'])
    document['plugins'] = [item for item in document['plugins']
                           if item['tag'] not in ('forward_fallback', 'fallback_dns', 'fallback')]
    plugin(document, 'proxy_dns_seq')['args'] = [{'exec': 'goto remote_dns'}]
    settings['native_config'] = yaml.safe_dump(document)


def test_preview_generates_both_configs_preserves_sources_and_unrelated_settings():
    config = snapshot()
    config['oxidns'].update(worker_threads=7, direct_rules=[], custom_hosts='full:router.test 192.168.1.1')
    config['oxidns']['api'].update(enabled=True, username='admin', password='private')
    before = deepcopy(config)
    candidate, report = prepare_gateway_onboarding(config, form())
    assert config == before
    assert report['ready'], report
    assert candidate['mihomo']['linux_gateway'] == {'enabled': True, 'rule_set_ids': ['ip']}
    assert candidate['nodes'] == before['nodes']
    assert candidate['proxy_groups'] == before['proxy_groups']
    assert candidate['rule_configs'] == before['rule_configs']
    assert candidate['rule_library'] == before['rule_library']
    assert candidate['mosdns'] == before['mosdns']
    assert candidate['oxidns']['worker_threads'] == 7
    assert candidate['oxidns']['api']['password'] == 'private'
    assert candidate['oxidns']['custom_hosts'] == before['oxidns']['custom_hosts']
    assert candidate['oxidns']['direct_rulesets'] == ['direct']
    assert candidate['oxidns']['proxy_rulesets'] == ['proxy']
    assert candidate['oxidns']['remote_dns'] == candidate['oxidns']['fallback_dns'] == '127.0.0.1:1053'
    assert candidate['oxidns']['default_forward'] == 'forward_remote'
    assert yaml.safe_load(candidate['mihomo']['custom_config'])['log-level'] == 'warning'
    assert assert_gateway_onboarding_ready(candidate)['ready']


@pytest.mark.parametrize('existing_fallback', ['', 'https://1.1.1.1/dns-query'])
def test_existing_native_configuration_updates_safely_without_template_replacement(existing_fallback):
    config = snapshot()
    config['oxidns']['log']['level'] = 'debug'
    config['oxidns']['fallback_dns'] = existing_fallback
    settings = prepare_editor(config['oxidns'], config)
    document = yaml.safe_load(settings['native_config'])
    document['runtime']['worker_threads'] = 9
    document['runtime']['future'] = {'keep': False}
    settings['native_config'] = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    settings['additional_files'] = [{'path': 'notes.txt', 'content': 'keep me', 'encoding': 'text'}]
    config['oxidns'] = settings
    candidate, report = prepare_gateway_onboarding(config, form())
    assert report['ready'], report
    assert candidate['oxidns']['config_mode'] == 'native'
    output = yaml.safe_load(candidate['oxidns']['native_config'])
    assert output['runtime'] == document['runtime']
    assert output['log'] == document['log']
    assert candidate['oxidns']['additional_files'] == settings['additional_files']
    assert 'gateway_onboarding_native_backup' not in candidate['oxidns']
    assert plugin(output, 'forward_local')['args']['upstreams'] == [{'addr': DEFAULT_LOCAL_DNS}]
    assert plugin(output, 'forward_remote')['args']['upstreams'] == [{'addr': '127.0.0.1:1053'}]
    assert plugin(output, 'forward_fallback')['args']['upstreams'] == [{'addr': '127.0.0.1:1053'}]
    assert plugin(output, 'server_udp')['args']['listen'] == '0.0.0.0:53'
    assert plugin(output, 'server_tcp')['args']['listen'] == '0.0.0.0:53'
    assert assert_gateway_onboarding_ready(candidate)['ready']


def test_native_disabled_fallback_can_be_prepared_but_is_not_already_ready():
    config, report = prepare_gateway_onboarding(snapshot(), form())
    assert report['ready']
    config['oxidns']['fallback_dns'] = ''
    config['oxidns'] = prepare_editor(config['oxidns'], config)
    disable_native_fallback(config['oxidns'])
    before = deepcopy(config)
    state = gateway_onboarding_state(config, 1)
    assert state['can_prepare'], state
    with pytest.raises(GatewayOnboardingError, match='备用上游'):
        assert_gateway_onboarding_ready(config)
    assert config == before
    candidate, report = prepare_gateway_onboarding(config, form())
    assert report['ready'], report
    assert candidate['oxidns']['config_mode'] == 'native'
    assert assert_gateway_onboarding_ready(candidate)['ready']


def test_native_disabled_fallback_with_foreign_reference_still_blocks_projection():
    config = snapshot()
    config['oxidns'] = prepare_editor(config['oxidns'], config)
    disable_native_fallback(config['oxidns'])
    document = yaml.safe_load(config['oxidns']['native_config'])
    document['plugins'].extend([
        {'tag': 'forward_fallback', 'type': 'forward', 'args': {'upstreams': [{'addr': 'https://1.1.1.1/dns-query'}]}},
        {'tag': 'foreign', 'type': 'sequence', 'args': [{'exec': '$forward_fallback'}]},
    ])
    config['oxidns']['native_config'] = yaml.safe_dump(document)
    before = deepcopy(config)
    _, report = prepare_gateway_onboarding(config, form())
    assert not report['ready'] and report['template_conversion_available']
    assert config == before


@pytest.mark.parametrize('reference', ['$fallback', '${BACKUP_EXEC}', 'goto fallback_dns'])
def test_native_disabled_fallback_rejects_external_or_dynamic_edges(reference):
    config = snapshot()
    config['oxidns'] = prepare_editor(config['oxidns'], config)
    disable_native_fallback(config['oxidns'])
    document = yaml.safe_load(config['oxidns']['native_config'])
    document['plugins'].append({'tag': 'foreign', 'type': 'sequence', 'args': [{'exec': reference}]})
    config['oxidns']['native_config'] = yaml.safe_dump(document)
    before = deepcopy(config)
    assert not gateway_onboarding_state(config, 1)['can_prepare']
    _, report = prepare_gateway_onboarding(config, form())
    assert not report['ready'] and report['template_conversion_available']
    assert config == before


def test_custom_native_routing_blocks_until_explicit_conversion_and_retains_backup():
    config = snapshot()
    native = 'plugins:\n- tag: mine\n  type: sequence\n  args:\n  - exec: reject 0\n- tag: custom_listener\n  type: udp_server\n  args: {listen: "127.0.0.1:5353", entry: mine}\n'
    config['oxidns'].update(config_mode='native', native_config=native,
                            additional_files=[{'path': 'private.txt', 'content': 'original', 'encoding': 'text'}])
    before = deepcopy(config)
    _, report = prepare_gateway_onboarding(config, form())
    assert not report['ready'] and report['template_conversion_available']
    assert config == before
    state = gateway_onboarding_state(config, 7)
    assert not state['can_prepare'] and state['template_conversion_available']
    candidate, report = prepare_gateway_onboarding(config, form(confirm_template_conversion=True))
    assert report['ready'], report
    assert candidate['oxidns']['config_mode'] == 'template'
    backup = candidate['oxidns']['gateway_onboarding_native_backup']
    assert backup['native_config'] == native
    assert backup['additional_files'] == before['oxidns']['additional_files']
    assert assert_gateway_onboarding_ready(candidate)['ready']


@pytest.mark.parametrize('local', ['223.5.5.5', 'udp://223.5.5.5', 'tcp://223.5.5.5',
    'https://dns.google/dns-query', 'https://127.0.0.1/dns-query', 'tls://0.0.0.0',
    'https://1.1.1.1:53/dns-query', 'https://1.1.1.1:1053/dns-query',
    'tls://[2606:4700:4700::1111]', 'https://user:pass@1.1.1.1/dns-query',
    '- addr: https://1.1.1.1/dns-query\n  socks5: 127.0.0.1:7890'])
def test_unsafe_direct_dns_is_rejected(local):
    with pytest.raises(GatewayOnboardingError):
        prepare_gateway_onboarding(snapshot(), form(local_dns=local))


@pytest.mark.parametrize('local', ['tls://223.5.5.5', 'https://1.1.1.1/dns-query\nhttps://223.5.5.5/dns-query',
    '- addr: tls://223.5.5.5\n  idle_timeout: 20'])
def test_ip_addressed_encrypted_dns_formats(local):
    _, report = prepare_gateway_onboarding(snapshot(), form(local_dns=local))
    assert report['ready'], report


@pytest.mark.parametrize('bad', [
    {'direct_rule_set_ids': ['direct'], 'proxy_rule_set_ids': ['direct']},
    {'rule_set_ids': 'ip'}, {'rule_set_ids': ['ip\nnext']}, {'confirm_template_conversion': 'true'},
    {'unknown': True}, {'expected_config_revision': True}])
def test_invalid_form_is_rejected_without_mutation(bad):
    config = snapshot()
    before = deepcopy(config)
    with pytest.raises(GatewayOnboardingError):
        prepare_gateway_onboarding(config, form(**bad))
    assert config == before


def test_domain_choices_are_profile_ids_renames_follow_latest_source_and_policies_are_not_guessed():
    config = snapshot()
    config['rule_library'][1]['name'] = 'Renamed Direct'
    state = gateway_onboarding_state(config, 5)
    direct = next(item for item in state['domain_candidates'] if item['id'] == 'direct')
    assert direct['name'] == 'Renamed Direct'
    assert direct['policy'] == 'DIRECT'
    _, invalid = prepare_gateway_onboarding(config, form(direct_rule_set_ids=['foreign']))
    assert not invalid['ready']
    _, ip_only = prepare_gateway_onboarding(config, form(direct_rule_set_ids=['ip']))
    assert not ip_only['ready']
    config['rule_configs'][1]['enabled'] = False
    _, disabled = prepare_gateway_onboarding(config, form())
    assert not disabled['ready']


def test_proxy_precondition_rejects_empty_and_direct_only_groups_but_never_changes_rules():
    config = snapshot()
    for item in config['rule_configs']:
        item['policy'] = 'DIRECT'
    before = deepcopy(config)
    _, report = prepare_gateway_onboarding(config, form())
    assert not report['ready'] and '代理出口' in report['blockers'][0]
    assert config == before
    config['rule_configs'][-1]['policy'] = 'Proxy'
    config['nodes'] = []
    config['proxy_groups'][0]['manual_nodes'] = []
    config['proxy_groups'][0]['include_direct'] = True
    _, report = prepare_gateway_onboarding(config, form())
    assert not report['ready']


@pytest.mark.parametrize('upstream', ['127.0.0.1:53', 'tcp://localhost:53', 'system', 'udp://127.0.0.1:1053'])
def test_mihomo_dns_cycles_are_blocked(upstream):
    config = snapshot()
    config['mihomo']['custom_config'] = yaml.safe_dump({'dns': {'nameserver': [upstream]}})
    _, report = prepare_gateway_onboarding(config, form())
    assert not report['ready'] and '循环' in report['blockers'][0]


def test_saved_readiness_does_not_repair_or_trust_inactive_native_fields():
    config, report = prepare_gateway_onboarding(snapshot(), form())
    assert report['ready']
    config['oxidns'] = prepare_editor(config['oxidns'], config)
    document = yaml.safe_load(config['oxidns']['native_config'])
    plugin(document, 'forward_fallback')['args']['upstreams'] = [{'addr': 'https://223.5.5.5/dns-query'}]
    config['oxidns']['native_config'] = yaml.safe_dump(document)
    before = deepcopy(config)
    with pytest.raises(GatewayOnboardingError, match='备用上游'):
        assert_gateway_onboarding_ready(config)
    assert config == before


@pytest.fixture
def onboarding_app(tmp_path):
    previous = config_store._repository
    repository = ProfileRepository(tmp_path, default_config_factory=config_store.get_default_config)
    repository.create_profile({'id': 'home', 'name': 'Home'})
    config = snapshot()
    repository.save_system({'system_config': config['system_config']})
    repository.save_shared({'nodes': config['nodes'], 'rule_library': config['rule_library']})
    repository.save_profile('home', {'proxy_groups': config['proxy_groups'], 'rule_configs': config['rule_configs'],
                                     'mihomo': config['mihomo'], 'oxidns': config['oxidns']})
    config_store.set_repository(repository)
    app = Flask(__name__)
    app.config['TESTING'] = True
    register_blueprints(app)
    yield app.test_client(), repository
    config_store.set_repository(previous)


def test_scoped_preview_and_atomic_save_share_revision_isolate_profiles_and_shared_sources(onboarding_app):
    client, repository = onboarding_app
    before = repository.export_all()
    state = client.get('/api/profiles/home/gateway-onboarding')
    assert state.status_code == 200, state.get_json()
    assert state.get_json()['settings']['rule_set_ids'] == []
    assert not state.get_json()['gateway_enabled']
    body = form(expected_config_revision=state.get_json()['config_revision'])
    preview = client.post('/api/profiles/home/gateway-onboarding/preview', json=body)
    assert preview.status_code == 200 and preview.get_json()['ready'], preview.get_json()
    assert repository.export_all() == before
    assert preview.headers['Cache-Control'] == 'no-store'
    saved = client.post('/api/profiles/home/gateway-onboarding', json=body)
    assert saved.status_code == 200, saved.get_json()
    result = saved.get_json()
    assert result['config_revision'] == repository.config_revision('home')
    assert result['publish_guards'] == {kind: {'expected_profile_id': 'home', 'expected_service_type': kind,
        'expected_config_revision': result['config_revision']} for kind in ('mihomo', 'oxidns')}
    after = repository.export_all()
    assert after['profiles']['default'] == before['profiles']['default']
    assert after['shared'] == before['shared']
    assert after['system'] == before['system']
    assert after['profiles']['home']['mosdns'] == before['profiles']['home']['mosdns']
    assert after['profiles']['home']['rule_configs'] == before['profiles']['home']['rule_configs']
    assert after['profiles']['home']['_revision'] == before['profiles']['home']['_revision'] + 1
    assert assert_gateway_onboarding_ready(repository.get_compat_config('home'))['ready']


def test_rejected_save_never_partially_enables_gateway(onboarding_app):
    client, repository = onboarding_app
    before = repository.export_all()
    response = client.post('/api/profiles/home/gateway-onboarding', json=form(proxy_rule_set_ids=['foreign']))
    assert response.status_code == 400
    assert repository.export_all() == before
    assert client.post('/api/profiles/home/gateway-onboarding', json={'bad': 'body'}).status_code == 400
    assert client.post('/api/profiles/home/gateway-onboarding', data='x' * 262145).status_code == 400
    assert client.get('/api/profiles/missing/gateway-onboarding').status_code == 404


def test_stale_revision_blocks_all_writes(onboarding_app):
    client, repository = onboarding_app
    revision = repository.config_revision('home')
    repository.save_profile('home', {'mihomo': {'custom_config': 'log-level: debug'}})
    before = repository.export_all()
    response = client.post('/api/profiles/home/gateway-onboarding', json=form(expected_config_revision=revision))
    assert response.status_code == 409, response.get_json()
    assert repository.export_all() == before


def test_authenticated_gateway_rules_require_explicit_subscription_token(monkeypatch):
    config = snapshot()
    config['system_config'].pop('config_token')
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: True)
    _, result = prepare_gateway_onboarding(config, form())
    assert not result['ready'] and '订阅令牌' in result['blockers'][0]
    assert 'config_token' not in config['system_config']


def test_native_named_forward_nodes_must_be_reached_by_actual_listener_pipeline():
    config = snapshot()
    config['oxidns'] = prepare_editor(config['oxidns'], config)
    document = yaml.safe_load(config['oxidns']['native_config'])
    plugin(document, 'proxy_dns_seq')['args'] = [{'exec': '$forward_local'}, {'exec': 'accept'}]
    config['oxidns']['native_config'] = yaml.safe_dump(document)
    _, report = prepare_gateway_onboarding(config, form())
    assert not report['ready'] and report['template_conversion_available']
    assert 'proxy_dns_seq' in report['blockers'][0]
    candidate, report = prepare_gateway_onboarding(config, form(confirm_template_conversion=True))
    assert report['ready'], report
    assert assert_gateway_onboarding_ready(candidate)['ready']


def test_onboarding_production_auth_does_not_accept_agent_credentials(authenticated_app):
    client, manager, (agent, _), admin, _ = authenticated_app
    profile_id = manager.get_agent_by_id(agent['id'])['profile_id']
    url = f'/api/profiles/{profile_id}/gateway-onboarding'
    assert client.get(url).status_code == 401
    assert client.post(url + '/preview', json=form()).status_code == 401
    assert client.post(url, json=form()).status_code == 401
    agent_headers = {'Authorization': 'Bearer ' + agent['token']}
    assert client.get(url, headers=agent_headers).status_code == 401
    response = client.get(url, headers=admin)
    assert response.status_code == 200, response.get_json()
    assert response.headers['Cache-Control'] == 'no-store'


from backend.test_agent_upgrade_auth import authenticated_app


def test_native_conversion_backup_roundtrips_authenticated_editor_but_not_public_preview(onboarding_app):
    client, repository = onboarding_app
    config = repository.get_compat_config('home')
    token = config['system_config']['rule_proxy_token']
    original = {'config_mode': 'native', 'native_config': 'plugins: []\n',
                'additional_files': [{'path': 'private.txt', 'content': token, 'encoding': 'text'}],
                'note': 'https://config.test/rules?token=' + token}
    settings = deepcopy(config['oxidns'])
    settings['gateway_onboarding_native_backup'] = original
    repository.save_profile('home', {'oxidns': settings})
    response = client.get('/api/profiles/home/oxidns/settings')
    assert response.status_code == 200
    assert response.get_json()['gateway_onboarding_native_backup'] == original
    saved = client.post('/api/profiles/home/oxidns/settings', json=response.get_json())
    assert saved.status_code == 200
    assert repository.get_profile('home')['oxidns']['gateway_onboarding_native_backup'] == original
    assert saved.get_json()['settings']['gateway_onboarding_native_backup'] == original
    # This exception is limited to authenticated OxiDNS editor routes. Ordinary
    # metadata and preview responses still sanitize opaque saved capabilities.
    from backend.common.config_export import sanitize_external_payload
    assert token not in str(sanitize_external_payload({'settings': settings}))


def test_state_exposes_callback_address_and_warns_about_system_loopback():
    config = snapshot()
    config['system_config']['server_domain'] = 'http://127.0.0.1:5001'
    before = deepcopy(config)
    state = gateway_onboarding_state(config, 3)
    assert state['server_url'] == 'http://127.0.0.1:5001'
    assert any('系统设置' in warning and '回环地址' in warning for warning in state['warnings'])
    assert config == before


def test_state_keeps_actionable_warnings_and_attaches_dns_warning_to_its_field():
    from backend.common.linux_gateway import gateway_editor_state

    config = snapshot()
    config['oxidns']['local_dns'] = 'udp://192.168.1.1:53'
    config['mihomo']['linux_gateway'] = {'enabled': True, 'rule_set_ids': ['deleted-rule']}
    before = deepcopy(config)

    state = gateway_onboarding_state(config, 3)

    assert 'DoH / DoT' in state['field_warnings']['local_dns']
    assert any('deleted-rule' in warning for warning in state['warnings'])
    assert not any('Linux + nftables' in warning or 'DNS' in warning for warning in state['warnings'])
    # The existing gateway editor retains its usage guidance.
    assert any('Linux + nftables' in warning for warning in gateway_editor_state(config)['warnings'])
    assert config == before

    config['oxidns']['local_dns'] = DEFAULT_LOCAL_DNS
    assert gateway_onboarding_state(config, 3)['field_warnings'] == {}


@pytest.mark.parametrize('value', ['- addr: [broken', '- wrong: https://223.5.5.5/dns-query', '- addr: null'])
def test_bad_dns_yaml_returns_form_error_not_server_failure(onboarding_app, value):
    client, repository = onboarding_app
    before = repository.export_all()
    response = client.post('/api/profiles/home/gateway-onboarding/preview', json=form(local_dns=value))
    assert response.status_code == 400, response.get_json()
    assert repository.export_all() == before


@pytest.mark.parametrize('field', ['nameserver', 'fallback', 'default-nameserver',
    'proxy-server-nameserver', 'direct-nameserver', 'nameserver-policy', 'proxy-server-nameserver-policy'])
@pytest.mark.parametrize('upstream', ['192.168.1.2', '192.168.1.2:53', '192.168.1.2:1053',
    'udp://192.168.1.2', 'udp://192.168.1.2:1053', 'tcp://192.168.1.2',
    'tcp://192.168.1.2:1053', 'udp://192.168.1.2:53#DIRECT'])
def test_saved_readiness_blocks_linux_lan_dns_cycles_in_every_upstream_field(field, upstream):
    config, report = prepare_gateway_onboarding(snapshot(), form())
    assert report['ready']
    document = yaml.safe_load(config['mihomo']['custom_config'])
    document['dns'][field] = {'+.example.test': [upstream]} if field.endswith('nameserver-policy') else [upstream]
    config['mihomo']['custom_config'] = yaml.safe_dump(document)
    before = deepcopy(config)
    with pytest.raises(GatewayOnboardingError, match='解析循环'):
        assert_gateway_onboarding_ready(config, local_addresses=('192.168.1.2',))
    assert config == before


@pytest.mark.parametrize('upstream', ['https://192.168.1.2/dns-query', 'tls://192.168.1.2:853',
                                    '192.168.1.1:53'])
def test_saved_readiness_retains_other_local_encrypted_dns_and_other_lan_servers(upstream):
    config, _ = prepare_gateway_onboarding(snapshot(), form())
    document = yaml.safe_load(config['mihomo']['custom_config'])
    document['dns']['nameserver'] = [upstream]
    config['mihomo']['custom_config'] = yaml.safe_dump(document)
    assert assert_gateway_onboarding_ready(config, local_addresses=('192.168.1.2',))['ready']


def test_saved_readiness_without_host_address_keeps_previous_behavior():
    config, _ = prepare_gateway_onboarding(snapshot(), form())
    document = yaml.safe_load(config['mihomo']['custom_config'])
    document['dns']['nameserver'] = ['192.168.1.2:53']
    config['mihomo']['custom_config'] = yaml.safe_dump(document)
    assert assert_gateway_onboarding_ready(config)['ready']


@pytest.mark.parametrize('mode', ['template', 'native'])
def test_direct_dns_default_and_direct_match_are_preserved_independently(mode):
    config = snapshot()
    config['rule_configs'][-1]['policy'] = 'DIRECT'
    config['oxidns']['default_forward'] = 'forward_local'
    if mode == 'native':
        config['oxidns'] = prepare_editor(config['oxidns'], config)
    candidate, report = prepare_gateway_onboarding(config, form(default_forward='forward_local'))
    assert report['ready'], report
    assert candidate['rule_configs'][-1]['policy'] == 'DIRECT'
    assert gateway_onboarding_state(candidate, 3)['settings']['default_forward'] == 'forward_local'
    assert assert_gateway_onboarding_ready(candidate)['ready']
    assert not any('MATCH' in warning for warning in report['warnings'])
