from copy import deepcopy
from types import SimpleNamespace

import pytest
import yaml
from flask import Flask

from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.common.setup_presets import builtin_rule_catalog, recommended_domain_rules
from backend.routes import register_blueprints
from backend.utils.subscription_cache import save_subscription_nodes

SESSION = 'a' * 32
NODE = {'id': 'node', 'name': 'Test node', 'type': 'ss', 'server': '192.0.2.1',
        'port': 443, 'cipher': 'aes-128-gcm', 'password': 'private'}
DOMAIN_RULE = {'id': 'work', 'name': '工作域名', 'behavior': 'domain', 'format': 'yaml',
               'source_type': 'content', 'content': 'payload:\n  - +.work.test', 'enabled': True}


@pytest.fixture
def setup_api(tmp_path):
    repository = ProfileRepository(tmp_path)
    previous = config_store._repository
    config_store.set_repository(repository)
    app = Flask(__name__)
    register_blueprints(app)
    yield app.test_client(), repository
    config_store.set_repository(previous)


def start(client, goal='router'):
    response = client.post('/api/setup', json={'id': SESSION, 'name': '新手配置', 'goal': goal})
    assert response.status_code == 200, response.json
    return response.json


def test_suggestion_is_read_only_and_creation_resumes_the_same_profile(setup_api):
    client, repository = setup_api
    before = repository.export_all()
    assert client.get('/api/setup').json['suggested']
    assert repository.export_all() == before
    first = start(client)
    token = repository.get_system()['system_config']['config_token']
    assert start(client)['profile_id'] == first['profile_id']
    assert repository.get_system()['system_config']['config_token'] == token
    assert len(repository.list_profiles()) == 2
    assert client.get('/api/setup').json['sessions'][0]['id'] == SESSION
    assert repository.get_profile('default') == before['profiles']['default']


def test_first_start_template_still_suggests_setup_until_a_device_exists(tmp_path):
    from backend.common.config import _get_initial_config, get_default_config
    repository = ProfileRepository(tmp_path, default_config_factory=get_default_config,
                                   initial_config_factory=_get_initial_config)
    assert repository.get_profile('default')['proxy_groups']
    previous = config_store._repository
    config_store.set_repository(repository)
    try:
        app = Flask(__name__)
        register_blueprints(app)
        client = app.test_client()
        assert client.get('/api/setup').json['suggested']
        repository.update_system_transaction(lambda system: system.update(agents=[
            {'id': 'mihomo', 'name': 'mihomo', 'host': '192.0.2.10', 'service_type': 'mihomo', 'profile_id': 'default'}]))
        assert not client.get('/api/setup').json['suggested']
    finally:
        config_store.set_repository(previous)


def test_subscriptions_without_a_link_are_not_offered_as_sources(tmp_path):
    from backend.common.config import _get_initial_config, get_default_config
    repository = ProfileRepository(tmp_path, default_config_factory=get_default_config,
                                   initial_config_factory=_get_initial_config)
    placeholder = repository.get_shared()['subscriptions'][0]['id']
    repository.update_shared_transaction(lambda shared: shared['subscriptions'].append(
        {'id': 'multi', 'name': 'Multi', 'url': 'https://a.test/sub\n\nhttps://b.test/sub', 'enabled': True}))
    repository.update_shared_transaction(lambda shared: shared['subscriptions'].append(
        {'id': 'noted', 'name': 'Noted', 'url': '订阅链接\nhttps://a.test/sub', 'enabled': True}))
    previous = config_store._repository
    config_store.set_repository(repository)
    try:
        app = Flask(__name__)
        register_blueprints(app)
        client = app.test_client()
        assert [item['id'] for item in client.get('/api/setup').json['subscriptions']] == ['multi']
        start(client)
        response = client.post(f'/api/setup/{SESSION}/source', json={'subscription_ids': [placeholder]})
        assert response.status_code == 400 and '订阅链接' in response.json['message']
        response = client.post(f'/api/setup/{SESSION}/source', json={'subscription_ids': ['multi']})
        assert response.status_code == 200, response.json
    finally:
        config_store.set_repository(previous)


def test_sources_are_idempotent_and_bad_subscription_cannot_complete(setup_api):
    client, repository = setup_api
    start(client)
    body = {'url': 'https://subscription.test/list?token=private'}
    for _ in range(2):
        response = client.post(f'/api/setup/{SESSION}/source', json=body)
        assert response.status_code == 200, response.json
    assert len(repository.get_shared()['subscriptions']) == 1
    response = client.post(f'/api/setup/{SESSION}/prepare', json={})
    assert response.status_code == 400
    assert '可用节点' in response.json['message']
    assert client.post(f'/api/setup/{SESSION}/finish', json={}).status_code == 409


@pytest.mark.parametrize('goal', ['router', 'gateway'])
def test_prepare_is_resumable_scoped_and_has_a_real_proxy_exit(setup_api, goal):
    client, repository = setup_api
    old = repository.get_profile('default')
    session = start(client, goal)
    repository.update_shared_transaction(lambda shared: shared['nodes'].append(deepcopy(NODE)))
    response = client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    assert response.status_code == 200
    for _ in range(2):
        response = client.post(f'/api/setup/{SESSION}/prepare', json={'template': 'basic'})
        assert response.status_code == 200, response.json
    profile = repository.get_profile(session['profile_id'])
    assert profile['_revision'] == 1
    groups = {group['name']: group for group in profile['proxy_groups']}
    # 'Test node' names no region, so only the all-node group carries it.
    assert groups['ALL']['manual_nodes'] == ['node'] and '🇭🇰 香港节点' not in groups
    assert profile['rule_configs'][-1]['policy'] == '🐟 漏网之鱼'
    assert yaml.safe_load(profile['mihomo']['custom_config'])['dns']['listen'] == '127.0.0.1:1053'
    assert repository.get_profile('default') == old
    response = client.post(f'/api/setup/{SESSION}/finish', json={})
    assert response.status_code == 409
    assert profile['oxidns']['remote_dns'] == '127.0.0.1:1053'
    assert profile['oxidns']['listen'] == '0.0.0.0:53'
    assert profile['oxidns']['local_dns'] == 'https://223.5.5.5/dns-query'
    assert client.get(f'/api/setup/{SESSION}').json['local_dns'] == profile['oxidns']['local_dns']


def test_subscription_cache_and_manual_profile_changes_are_checked(setup_api):
    client, repository = setup_api
    session = start(client)
    client.post(f'/api/setup/{SESSION}/source', json={'url': 'https://subscription.test/list'})
    save_subscription_nodes('setup_sub_' + SESSION, [NODE])
    assert client.post(f'/api/setup/{SESSION}/prepare', json={}).status_code == 200
    repository.save_profile(session['profile_id'], {'mihomo': {'custom_config': 'mixed-port: 9999'}})
    before = repository.export_all()
    assert client.post(f'/api/setup/{SESSION}/prepare', json={}).status_code == 409
    assert repository.export_all() == before
    assert client.post(f'/api/setup/{SESSION}/finish', json={}).status_code == 409


def test_embedded_installation_and_router_writes_do_not_block_new_choices(setup_api):
    client, repository = setup_api
    session = start(client)
    profile_id = session['profile_id']
    repository.update_shared_transaction(lambda shared: shared['nodes'].append(deepcopy(NODE)))
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    assert client.post(f'/api/setup/{SESSION}/prepare', json={}).status_code == 200
    # Step 4 saves the installation settings before generating the command.
    state = client.get(f'/api/profiles/{profile_id}/gateway-onboarding').json
    response = client.post(f'/api/profiles/{profile_id}/gateway-onboarding',
                           json={**state['settings'], 'expected_config_revision': state['config_revision']})
    assert response.status_code == 200, response.json
    response = client.post('/api/routers', json=ROUTER_DRAFT, headers={'X-ConfigFlow-Profile': profile_id})
    assert response.status_code == 200, response.json
    response = client.post(f'/api/setup/{SESSION}/prepare', json={'traffic_fallback': 'direct'})
    assert response.status_code == 200, response.json
    assert repository.get_profile(profile_id)['rule_configs'][-1]['policy'] == 'DIRECT'
    repository.save_profile(profile_id, {'mihomo': {'custom_config': 'mixed-port: 9999'}})
    response = client.post(f'/api/setup/{SESSION}/prepare', json={'traffic_fallback': 'proxy'})
    assert response.status_code == 409
    assert '其他页面' in response.json['message']


def test_invalid_requests_cannot_skip_steps_or_target_other_profiles(setup_api):
    client, repository = setup_api
    start(client)
    before = repository.export_all()
    assert client.patch(f'/api/setup/{SESSION}', json={'step': 5}).status_code == 400
    assert client.patch(f'/api/setup/{SESSION}', json={'profile_id': 'default'}).status_code == 400
    assert client.patch(f'/api/setup/{SESSION}', json={'router_id': 'unrelated'}).status_code == 409
    assert client.post(f'/api/setup/{SESSION}/source', json={'url': 'file:///etc/passwd'}).status_code == 400
    assert client.post('/api/setup', json=[]).status_code == 400
    assert repository.export_all() == before


def test_setup_apis_require_administrator_authentication(setup_api, monkeypatch):
    client, _ = setup_api
    monkeypatch.setattr('backend.common.auth.is_auth_enabled', lambda: True)
    assert client.get('/api/setup').status_code == 401
    assert client.post('/api/setup', json={'id': SESSION, 'name': 'test', 'goal': 'router'}).status_code == 401


@pytest.fixture
def installed(setup_api, monkeypatch):
    client, repository = setup_api
    session = start(client)
    repository.update_shared_transaction(lambda shared: shared['nodes'].append(deepcopy(NODE)))
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    client.post(f'/api/setup/{SESSION}/prepare', json={})
    profile_id = session['profile_id']
    revision = repository.config_revision(profile_id)
    agents = {service: {'id': service, 'name': service, 'host': '192.0.2.10',
                       'profile_id': profile_id, 'service_type': service} for service in ('mihomo', 'oxidns')}
    repository.update_system_transaction(lambda system: system.update(agents=list(agents.values())))
    gateway = {'id': 'installation', 'phase': 'published', 'target_changed': False, 'agents': agents}
    def latest(*_):
        return {'session': {**gateway, 'config_changed': repository.config_revision(profile_id) != revision}}
    monkeypatch.setattr('backend.common.agent_manager.get_agent_manager', lambda: object())
    monkeypatch.setattr('backend.agents.gateway_installation.latest', latest)
    managed = {'active': True, 'healthy': True}
    monkeypatch.setattr('backend.agents.linux_gateway.call', lambda *_: {'managed': managed})
    return client, repository, profile_id, gateway, managed


ROUTER_DRAFT = {'name': 'Main', 'type': 'ikuai', 'endpoint': 'http://192.0.2.1',
                'username': 'admin', 'password': 'draft-secret'}


def test_early_router_settings_are_private_and_do_not_invalidate_installation(installed):
    client, repository, profile_id, _, _ = installed
    before = repository.export_all()
    response = client.post(f'/api/setup/{SESSION}/router-draft', json={'router': ROUTER_DRAFT})
    assert response.status_code == 200, response.json
    draft = response.json['router_draft']
    assert draft['has_password'] and 'password' not in draft
    assert response.json['step'] == response.json['max_step'] == 3
    assert repository.export_all() == before
    path = repository.data_dir / 'setup-sessions' / f'{SESSION}.json'
    assert path.stat().st_mode & 0o777 == 0o600
    assert 'draft-secret' in path.read_text()
    for response in (client.get(f'/api/setup/{SESSION}'), client.get('/api/setup'),
                     client.post('/api/setup', json={'id': SESSION, 'name': '新手配置', 'goal': 'router'}),
                     client.patch(f'/api/setup/{SESSION}', json={'step': 3})):
        assert response.status_code == 200
        assert 'draft-secret' not in response.get_data(as_text=True)
    response = client.post(f'/api/setup/{SESSION}/router-draft', json={'router': {**ROUTER_DRAFT, 'name': 'Renamed', 'password': ''}})
    assert response.json['router_draft']['id'] == draft['id']
    assert response.json['router_draft']['has_password']
    assert repository.export_all() == before
    assert client.post(f'/api/setup/{SESSION}/router-draft', json={'router': None}).status_code == 200
    assert 'router_draft' not in client.get(f'/api/setup/{SESSION}').json
    assert 'draft-secret' not in path.read_text()


def test_router_draft_attaches_only_after_healthy_installation_and_is_idempotent(installed):
    client, repository, profile_id, _, managed = installed
    response = client.post(f'/api/setup/{SESSION}/router-draft', json={'router': ROUTER_DRAFT})
    router_id = response.json['router_draft']['id']
    managed['healthy'] = False
    assert client.post(f'/api/setup/{SESSION}/check', json={}).status_code == 409
    assert repository.get_profile(profile_id)['routers']['items'] == []
    assert 'router_draft' in client.get(f'/api/setup/{SESSION}').json
    managed['healthy'] = True
    for _ in range(2):
        response = client.post(f'/api/setup/{SESSION}/check', json={})
        assert response.status_code == 200, response.json
        assert response.json['step'] == 4
        assert response.json['router_id'] == router_id
        assert 'router_draft' not in response.json
    routers = repository.get_profile(profile_id)['routers']['items']
    assert len(routers) == 1
    assert routers[0]['proxy_gateway_agent_id'] == 'mihomo'
    assert routers[0]['dns_agent_id'] == 'oxidns'
    assert routers[0]['password'] == 'draft-secret'
    assert client.post(f'/api/setup/{SESSION}/router-draft', json={'router': ROUTER_DRAFT}).status_code == 409


def test_router_draft_retries_after_interruption_between_profile_and_session_commits(installed, monkeypatch):
    client, repository, profile_id, _, _ = installed
    client.post(f'/api/setup/{SESSION}/router-draft', json={'router': ROUTER_DRAFT})
    from backend.common import setup_flow
    write = setup_flow.write
    def interrupted_write(repo, session):
        if session['step'] == 4:
            raise OSError('simulated interrupted session commit')
        return write(repo, session)
    monkeypatch.setattr(setup_flow, 'write', interrupted_write)
    assert client.post(f'/api/setup/{SESSION}/check', json={}).status_code == 500
    assert len(repository.get_profile(profile_id)['routers']['items']) == 1
    monkeypatch.setattr(setup_flow, 'write', write)
    response = client.post(f'/api/setup/{SESSION}/check', json={})
    assert response.status_code == 200, response.json
    assert 'router_draft' not in response.json
    assert len(repository.get_profile(profile_id)['routers']['items']) == 1


@pytest.mark.parametrize('change', [{'proxy_gateway_agent_id': 'other'}, {'dns_agent_id': 'other'},
                                   {'auto_rule_sync': True}, {'unknown': True}, {'endpoint': 'file:///etc/passwd'}])
def test_router_draft_rejects_invalid_or_premature_links(installed, change):
    client, repository, _, _, _ = installed
    before = repository.export_all()
    assert client.post(f'/api/setup/{SESSION}/router-draft', json={'router': {**ROUTER_DRAFT, **change}}).status_code == 400
    assert repository.export_all() == before
    assert 'router_draft' not in client.get(f'/api/setup/{SESSION}').json


def test_installation_check_requires_both_services_and_healthy_forwarding(installed):
    client, _, _, gateway, managed = installed
    managed['healthy'] = False
    assert client.post(f'/api/setup/{SESSION}/check', json={}).status_code == 409
    managed['healthy'] = True
    gateway['target_changed'] = True
    assert client.post(f'/api/setup/{SESSION}/check', json={}).status_code == 409
    gateway['target_changed'] = False
    response = client.post(f'/api/setup/{SESSION}/check', json={})
    assert response.status_code == 200, response.json
    assert response.json['step'] == 4
    assert response.json['route_instructions']['routes'][0]['cidr'] == '198.18.0.0/16'
    assert response.json['route_instructions']['dns'] == '192.0.2.10'


def test_manual_router_completion_requires_explicit_acknowledgements(installed):
    client, _, _, _, _ = installed
    assert client.post(f'/api/setup/{SESSION}/check', json={}).status_code == 200
    assert client.post(f'/api/setup/{SESSION}/finish', json={}).status_code == 409
    response = client.post(f'/api/setup/{SESSION}/finish', json={'router_mode': 'manual', 'ack_routes': True, 'ack_dns': True})
    assert response.status_code == 200, response.json
    assert response.json['router_verification'] == 'manual'
    assert response.json['completed']


def test_router_receipts_do_not_invalidate_core_checks_but_core_edits_do(installed, monkeypatch):
    client, repository, profile_id, _, _ = installed
    client.post(f'/api/setup/{SESSION}/check', json={})
    from backend.routers.store import normalize
    router = normalize({'name': 'Main', 'type': 'ikuai', 'endpoint': 'http://192.0.2.1',
                        'username': 'admin', 'password': 'private',
                        'proxy_gateway_agent_id': 'mihomo', 'dns_agent_id': 'oxidns'})
    repository.save_profile(profile_id, {'routers': {'items': [router]}})
    closed = []
    monkeypatch.setattr('backend.routes.routers._driver', lambda _: SimpleNamespace(close=lambda: closed.append(True)))
    monkeypatch.setattr('backend.routes.routers._inspect', lambda *_: (None, {}, {}, {'return_routing': {}}))
    plan = {'empty': False, 'blockers': []}
    monkeypatch.setattr('backend.routers.plan.build_plan', lambda *_: plan)
    body = {'router_mode': 'managed', 'router_id': router['id']}
    assert client.post(f'/api/setup/{SESSION}/finish', json=body).status_code == 409
    plan['empty'] = True
    response = client.post(f'/api/setup/{SESSION}/finish', json=body)
    assert response.status_code == 200, response.json
    assert response.json['router_verification'] == 'verified'
    assert len(closed) == 2
    repository.save_profile(profile_id, {'mihomo': {'custom_config': 'mixed-port: 9999'}})
    assert client.post(f'/api/setup/{SESSION}/finish', json=body).status_code == 409


@pytest.mark.parametrize('dns_fallback', ['mihomo', 'direct'])
@pytest.mark.parametrize('traffic_fallback', ['proxy', 'direct'])
def test_dns_and_connection_fallbacks_are_independent_through_installation(setup_api, dns_fallback, traffic_fallback):
    from backend.common.gateway_onboarding import gateway_onboarding_state, prepare_gateway_onboarding, assert_gateway_onboarding_ready
    from backend.converters.oxidns import generate_oxidns_config
    from backend.converters.mihomo import generate_mihomo_config
    client, repository = setup_api
    session = start(client)
    repository.update_shared_transaction(lambda shared: shared['nodes'].append(deepcopy(NODE)))
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    local_dns = 'https://223.6.6.6/dns-query\ntls://223.5.5.5:853'
    choices = {'dns_fallback': dns_fallback, 'traffic_fallback': traffic_fallback, 'local_dns': f'  {local_dns}\n',
               'domain_rules': {'direct': ['builtin:direct'], 'proxy': ['builtin:proxy']}}
    for _ in range(2):
        response = client.post(f'/api/setup/{SESSION}/prepare', json=choices)
        assert response.status_code == 200, response.json
        assert response.json['dns_fallback'] == dns_fallback
        assert response.json['traffic_fallback'] == traffic_fallback
        assert response.json['local_dns'] == local_dns
    assert client.get(f'/api/setup/{SESSION}').json['local_dns'] == local_dns
    config, revision = repository.get_publish_snapshot(session['profile_id'])
    assert repository.get_profile(session['profile_id'])['_revision'] == 1
    assert len(repository.get_shared()['rule_library']) == len(builtin_rule_catalog())
    expected_forward = 'forward_remote' if dns_fallback == 'mihomo' else 'forward_local'
    expected_policy = '🐟 漏网之鱼' if traffic_fallback == 'proxy' else 'DIRECT'
    assert config['oxidns']['default_forward'] == expected_forward
    assert config['oxidns']['local_dns'] == local_dns
    assert config['oxidns']['direct_rulesets'] == ['setup_builtin_direct']
    assert config['oxidns']['proxy_rulesets'] == ['setup_builtin_proxy']
    assert config['rule_configs'][-1]['policy'] == expected_policy
    state = gateway_onboarding_state(config, revision)
    assert state['settings']['default_forward'] == expected_forward
    assert state['settings']['local_dns'] == local_dns
    prepared, report = prepare_gateway_onboarding(config, state['settings'], base_url='https://config.test')
    assert report['ready'], report
    assert assert_gateway_onboarding_ready(prepared, base_url='https://config.test')['ready']
    assert prepared['oxidns']['default_forward'] == expected_forward
    assert prepared['oxidns']['local_dns'] == local_dns
    assert prepared['rule_configs'] == config['rule_configs']
    assert not any('MATCH' in warning for warning in report['warnings'])
    document = yaml.safe_load(generate_mihomo_config(prepared))
    assert document['rules'][-1] == 'MATCH,' + expected_policy
    assert 'RULE-SET,Proxy,✈️ 节点选择' in document['rules']
    dns_document = yaml.safe_load(generate_oxidns_config(prepared, 'https://config.test'))
    forward = next(plugin for plugin in dns_document['plugins'] if plugin['tag'] == 'forward_local')
    assert [upstream['addr'] for upstream in forward['args']['upstreams']] == local_dns.splitlines()
    main = next(plugin for plugin in dns_document['plugins'] if plugin['tag'] == 'sequence_main')
    assert main['args'][-1]['exec'] == ('goto proxy_dns_seq' if dns_fallback == 'mihomo' else 'goto china_dns')


def test_removed_proxy_mode_and_invalid_fallbacks_do_not_mutate_configuration(setup_api):
    client, repository = setup_api
    start(client)
    before = repository.export_all()
    for body in ({'template': 'proxy'}, {'dns_fallback': 'proxy'}, {'traffic_fallback': 'mihomo'}, {'dns_fallback': []}):
        response = client.post(f'/api/setup/{SESSION}/prepare', json=body)
        assert response.status_code == 400, response.json
    assert repository.export_all() == before


@pytest.mark.parametrize('local_dns', ['', '  ', None, [], '223.5.5.5',
                                     'https://127.0.0.1/dns-query', 'tls://223.5.5.5:53',
                                     'https://dns.example/dns-query'])
def test_invalid_direct_dns_does_not_change_profile_presets_or_session(setup_api, local_dns):
    client, repository = setup_api
    start(client)
    repository.update_shared_transaction(lambda shared: shared['nodes'].append(deepcopy(NODE)))
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    before = repository.export_all()
    session = client.get(f'/api/setup/{SESSION}').json
    response = client.post(f'/api/setup/{SESSION}/prepare', json={'local_dns': local_dns})
    assert response.status_code == 400, response.json
    assert '直连 DNS' in response.json['message']
    assert repository.export_all() == before
    assert client.get(f'/api/setup/{SESSION}').json == session


def test_domain_choices_are_read_only_and_only_offer_enabled_supported_domain_rules(setup_api):
    client, repository = setup_api
    entries = [DOMAIN_RULE,
               {**DOMAIN_RULE, 'id': 'disabled', 'name': '停用', 'enabled': False},
               {**DOMAIN_RULE, 'id': 'ips', 'name': 'IP', 'behavior': 'ipcidr'},
               {**DOMAIN_RULE, 'id': 'mrs', 'name': 'MRS', 'format': 'mrs'},
               {'id': 'setup_dns_cn', 'name': '旧预设', 'url': 'https://example.test/old-cn', 'behavior': 'domain'},
               {'id': 'existing_direct', 'name': '已有直连规则', 'url': builtin_rule_catalog()['builtin:direct']['url'], 'behavior': 'classical'}]
    repository.update_shared_transaction(lambda shared: shared['rule_library'].extend(deepcopy(entries)))
    before = repository.export_all()
    options = client.get('/api/setup').json['domain_rules']
    assert [item['id'] for item in options if item['source'] != 'builtin'] == ['library:work']
    assert any(item['id'] == 'builtin:telegram' and item['behavior'] == 'classical' for item in options)
    assert not any(item['id'] in ('builtin:fakeip_filter', 'builtin:private_ip') for item in options)
    assert repository.export_all() == before


def test_old_two_preset_choices_use_template_sources_without_extra_rule_library(setup_api):
    client, repository = setup_api
    start(client)
    repository.update_shared_transaction(lambda shared: shared['nodes'].append(deepcopy(NODE)))
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    response = client.post(f'/api/setup/{SESSION}/prepare', json={'domain_rules': {
        'direct': ['preset:direct'], 'proxy': ['preset:proxy']}})
    assert response.status_code == 200, response.json
    assert response.json['domain_rules'] == {'direct': ['builtin:chinamax'], 'proxy': ['builtin:proxy']}
    assert len(repository.get_shared()['rule_library']) == len(builtin_rule_catalog())


@pytest.mark.parametrize('selections', [
    {'direct': ['library:work'], 'proxy': ['builtin:proxy']},
    {'direct': [], 'proxy': ['library:work', 'builtin:direct']},
    {'direct': [], 'proxy': []},
])
def test_selected_domain_rules_reach_both_core_configs_and_installer(setup_api, selections):
    from backend.common.gateway_onboarding import gateway_onboarding_state, prepare_gateway_onboarding
    from backend.converters.oxidns import generate_oxidns_config
    from backend.converters.mihomo import generate_mihomo_config
    client, repository = setup_api
    session = start(client)
    repository.update_shared_transaction(lambda shared: shared.update(
        nodes=[deepcopy(NODE)], rule_library=[deepcopy(DOMAIN_RULE)]))
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    before_rule = deepcopy(repository.get_shared()['rule_library'][0])
    for _ in range(2):
        response = client.post(f'/api/setup/{SESSION}/prepare', json={'domain_rules': selections})
        assert response.status_code == 200, response.json
        assert response.json['domain_rules'] == selections
    assert client.get(f'/api/setup/{SESSION}').json['domain_rules'] == selections
    snapshot, revision = repository.get_publish_snapshot(session['profile_id'])
    profile = repository.get_profile(session['profile_id'])
    assert profile['_revision'] == 1
    assert repository.get_shared()['rule_library'][0] == before_rule
    assert profile['oxidns']['rule_update']['enabled'] == bool(selections['direct'] or selections['proxy'])
    assert len(repository.get_shared()['rule_library']) == 1 + len(builtin_rule_catalog())
    rules = {item['id']: item for item in snapshot['rule_configs'] if item['itemType'] == 'ruleset'}
    for route in ('direct', 'proxy'):
        ids = snapshot['oxidns'][route + '_rulesets']
        assert len(ids) == len(selections[route])
        assert all(rules[identity]['policy'] == ('🎯 全球直连' if route == 'direct' else '✈️ 节点选择') for identity in ids)
    state = gateway_onboarding_state(snapshot, revision)
    candidate, report = prepare_gateway_onboarding(snapshot, state['settings'], base_url='https://config.test')
    assert report['ready'], report
    assert candidate['oxidns']['direct_rulesets'] == snapshot['oxidns']['direct_rulesets']
    assert candidate['oxidns']['proxy_rulesets'] == snapshot['oxidns']['proxy_rulesets']
    mihomo = yaml.safe_load(generate_mihomo_config(candidate, base_url='https://config.test'))
    assert len([rule for rule in mihomo['rules'] if rule.startswith('RULE-SET,')]) == len(rules)
    dns = yaml.safe_load(generate_oxidns_config(candidate, 'https://config.test'))
    assert any(plugin['tag'] == 'forward_local' for plugin in dns['plugins'])


@pytest.mark.parametrize('selections', [None, [], {}, {'direct': [], 'proxy': 'builtin:proxy'},
                                      {'direct': ['builtin:direct'], 'proxy': ['builtin:direct']},
                                      {'direct': ['library:missing'], 'proxy': []},
                                      {'direct': ['library:disabled'], 'proxy': []}])
def test_invalid_domain_selections_do_not_create_presets_or_change_drafts(setup_api, selections):
    client, repository = setup_api
    start(client)
    repository.update_shared_transaction(lambda shared: shared.update(
        nodes=[deepcopy(NODE)], rule_library=[{**DOMAIN_RULE, 'id': 'disabled', 'enabled': False}]))
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    before = repository.export_all()
    session = client.get(f'/api/setup/{SESSION}').json
    response = client.post(f'/api/setup/{SESSION}/prepare', json={'domain_rules': selections})
    assert response.status_code in (400, 409), response.json
    assert repository.export_all() == before
    assert client.get(f'/api/setup/{SESSION}').json == session


def test_builtin_library_comes_only_from_the_original_template(setup_api):
    import json
    from backend.common.resource import get_backend_resource
    catalog = builtin_rule_catalog()
    with open(get_backend_resource('config_template.json'), encoding='utf-8') as handle:
        template = json.load(handle)
    by_name = {item['name']: item for item in catalog.values()}
    assert len(catalog) == len(template['rule_library']) == 25
    assert by_name.keys() == {item['name'] for item in template['rule_library']}
    for item in template['rule_library']:
        field = 'content' if item.get('source_type') == 'content' else 'url'
        assert by_name[item['name']][field] == item[field]
    assert not {'OpenAI', 'Gemini', 'Claude', 'Copilot', 'SteamCN', 'Custom_Direct', 'Custom_Proxy', 'Custom_IP_Proxy'} & by_name.keys()
    template_library = {item['id']: item for item in template['rule_library']}
    assert list(by_name) == [template_library[rule['library_rule_id']]['name']
                             for rule in template['rule_configs'] if rule.get('itemType') == 'ruleset']
    client, repository = setup_api
    before = repository.export_all()
    index = client.get('/api/setup').json
    assert index['recommended_domain_rules'] == recommended_domain_rules()
    assert [len(index['recommended_domain_rules'][route]) for route in ('direct', 'proxy')] == [7, 18]
    assert repository.export_all() == before


def test_recommended_classical_library_is_ready_and_existing_sources_are_not_duplicated(setup_api):
    from backend.common.gateway_onboarding import gateway_onboarding_state, prepare_gateway_onboarding, assert_gateway_onboarding_ready
    from backend.converters.oxidns import get_oxidns_ruleset_downloads
    client, repository = setup_api
    existing = {'id': 'existing_telegram', 'name': 'Telegram', 'behavior': 'classical',
                'url': builtin_rule_catalog()['builtin:telegram']['url'].replace('/refs/heads/', '/')}
    custom = {**DOMAIN_RULE, 'id': 'personal', 'name': '我的自定义'}
    repository.update_shared_transaction(lambda shared: shared.update(nodes=[deepcopy(NODE)], rule_library=[existing, custom]))
    session = start(client)
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    for _ in range(2):
        response = client.post(f'/api/setup/{SESSION}/prepare', json={})
        assert response.status_code == 200, response.json
        assert response.json['domain_rules'] == recommended_domain_rules()
    shared = repository.get_shared()['rule_library']
    assert shared[:2] == [existing, custom]
    assert len(shared) == len(builtin_rule_catalog()) + 1
    profile = repository.get_profile(session['profile_id'])
    assert profile['_revision'] == 1
    assert any(rule.get('library_rule_id') == existing['id'] for rule in profile['rule_configs'])
    assert not any(rule.get('library_rule_id') == custom['id'] for rule in profile['rule_configs'])
    snapshot, revision = repository.get_publish_snapshot(session['profile_id'])
    assert [rule['name'] for rule in snapshot['rule_configs'] if rule['itemType'] == 'ruleset'] == [item['name'] for item in builtin_rule_catalog().values()]
    state = gateway_onboarding_state(snapshot, revision)
    candidate, report = prepare_gateway_onboarding(snapshot, state['settings'], base_url='https://config.test')
    assert report['ready'], report
    assert assert_gateway_onboarding_ready(candidate, base_url='https://config.test')['ready']
    downloads = get_oxidns_ruleset_downloads(candidate, 'https://config.test')
    assert any(item['local_path'].endswith('_domain.txt') for item in downloads)
    assert any(item['local_path'].endswith('_ip.txt') for item in downloads)
    assert len(downloads) > 30
    index = client.get('/api/setup').json
    telegram = next(item for item in index['domain_rules'] if item['id'] == 'builtin:telegram')
    assert 'library:existing_telegram' in telegram['aliases']
    assert not any(item['id'] == 'library:existing_telegram' for item in index['domain_rules'])
    # An older saved library identity is still accepted and resolves to the same source.
    result = client.post(f'/api/setup/{SESSION}/prepare', json={'domain_rules': {'direct': [], 'proxy': ['library:existing_telegram']}})
    assert result.status_code == 200, result.json
    assert result.json['domain_rules'] == {'direct': [], 'proxy': ['builtin:telegram']}


def test_disabled_existing_builtin_source_is_not_cloned_or_reenabled(setup_api):
    client, repository = setup_api
    telegram = {key: value for key, value in builtin_rule_catalog()['builtin:telegram'].items() if key not in ('default_route', '_template_order')}
    telegram.update(id='disabled_telegram', enabled=False)
    repository.update_shared_transaction(lambda shared: shared.update(nodes=[deepcopy(NODE)], rule_library=[deepcopy(telegram)]))
    session = start(client)
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    index = client.get('/api/setup').json
    assert 'builtin:telegram' not in index['recommended_domain_rules']['proxy']
    assert not any(item['id'] == 'builtin:telegram' for item in index['domain_rules'])
    response = client.post(f'/api/setup/{SESSION}/prepare', json={})
    assert response.status_code == 200, response.json
    shared = repository.get_shared()['rule_library']
    assert len(shared) == len(builtin_rule_catalog())
    assert shared[0] == telegram
    assert not any(rule.get('library_rule_id') == telegram['id'] for rule in repository.get_profile(session['profile_id'])['rule_configs'])


def test_original_first_start_library_is_reused_without_a_second_copy(tmp_path):
    from backend.common.setup_presets import ensure_builtin_library, domain_rule_options
    repository = ProfileRepository(tmp_path / 'first-start', default_config_factory=config_store.get_default_config,
                                   initial_config_factory=config_store._get_initial_config)
    before = repository.export_all()
    resolved = ensure_builtin_library(repository)
    assert len(resolved) == 25
    assert len(repository.get_shared()['rule_library']) == 25
    assert repository.export_all() == before
    assert all(item['source'] == 'builtin' for item in domain_rule_options(repository))


def test_new_choices_after_installation_check_clear_stale_later_steps(installed):
    client, repository, profile_id, _, _ = installed
    response = client.post(f'/api/setup/{SESSION}/check', json={})
    assert response.status_code == 200, response.json
    assert response.json['max_step'] == 4 and response.json['route_instructions']
    retry = client.post(f'/api/setup/{SESSION}/prepare', json={})
    assert retry.status_code == 200 and retry.json['max_step'] == 4
    assert 'route_instructions' in retry.json
    changed = client.post(f'/api/setup/{SESSION}/prepare', json={'traffic_fallback': 'direct'})
    assert changed.status_code == 200, changed.json
    assert changed.json['max_step'] == 3 and changed.json['step'] == 3
    assert not {'installation', 'route_instructions', 'router_verification'} & set(changed.json)
    assert client.patch(f'/api/setup/{SESSION}', json={'step': 4}).status_code == 400


IP_RULE = {'id': 'chat', 'name': '聊天', 'behavior': 'classical', 'format': 'text', 'source_type': 'content',
           'content': 'DOMAIN-SUFFIX,chat.test\nIP-CIDR,198.51.100.0/24,no-resolve', 'enabled': True}
DIRECT_IP_RULE = {**IP_RULE, 'id': 'lan-chat', 'name': '内网聊天', 'content': 'IP-CIDR,203.0.113.0/24'}


def test_gateway_takes_over_only_ip_app_rule_sets(setup_api):
    from backend.common.gateway_onboarding import gateway_onboarding_state, prepare_gateway_onboarding
    from backend.converters.mihomo import generate_mihomo_config
    client, repository = setup_api
    session = start(client, 'gateway')
    repository.update_shared_transaction(lambda shared: shared.update(
        nodes=[deepcopy(NODE)], rule_library=[deepcopy(DOMAIN_RULE), deepcopy(IP_RULE), deepcopy(DIRECT_IP_RULE)]))
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    # Broad proxy lists (Apple, a library set with IPs) and direct sets stay
    # out: their ranges overlap direct sites that would then be proxied.
    selections = {'direct': ['library:lan-chat', 'builtin:direct'],
                  'proxy': ['builtin:telegram', 'builtin:apple', 'builtin:googlefcm', 'library:chat', 'library:work']}
    response = client.post(f'/api/setup/{SESSION}/prepare', json={'domain_rules': selections})
    assert response.status_code == 200, response.json
    snapshot, revision = repository.get_publish_snapshot(session['profile_id'])
    names = {item['id']: item['name'] for item in snapshot['rule_configs'] if item['itemType'] == 'ruleset'}
    selected = snapshot['mihomo']['linux_gateway']['rule_set_ids']
    assert sorted(names[identity] for identity in selected) == ['GoogleFCM', 'Telegram']
    assert snapshot['mihomo']['linux_gateway']['enabled'] is False
    # The embedded installer carries the selection into the enabled gateway.
    state = gateway_onboarding_state(snapshot, revision)
    assert state['settings']['rule_set_ids'] == selected
    candidate, report = prepare_gateway_onboarding(snapshot, state['settings'], base_url='https://config.test')
    assert report['ready'], report
    mihomo = yaml.safe_load(generate_mihomo_config(candidate, base_url='https://config.test'))
    assert len(mihomo['tun']['route-address-set']) == 3
    # Without either app rule set among the proxy rules nothing is taken over.
    selections = {'direct': [], 'proxy': ['builtin:apple', 'library:chat']}
    response = client.post(f'/api/setup/{SESSION}/prepare', json={'domain_rules': selections})
    assert response.status_code == 200, response.json
    assert repository.get_profile(session['profile_id'])['mihomo']['linux_gateway']['rule_set_ids'] == []


def test_router_setup_leaves_ip_takeover_to_the_router_settings(setup_api):
    client, repository = setup_api
    session = start(client)
    repository.update_shared_transaction(lambda shared: shared.update(nodes=[deepcopy(NODE)], rule_library=[deepcopy(IP_RULE)]))
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['node']})
    selections = {'direct': [], 'proxy': ['builtin:telegram', 'library:chat']}
    response = client.post(f'/api/setup/{SESSION}/prepare', json={'domain_rules': selections})
    assert response.status_code == 200, response.json
    assert 'linux_gateway' not in repository.get_profile(session['profile_id'])['mihomo']


def test_template_groups_follow_wizard_choices_and_drop_empty_regions():
    from backend.common.setup_presets import template_strategy
    rules = [('builtin:apple', 'proxy'), ('builtin:telegram', 'direct'), ('builtin:epic', 'proxy'),
             ('builtin:steam', 'direct'), ('builtin:bilibili', 'direct'), ('builtin:chinamax', 'proxy'),
             ('builtin:lan', 'direct'), ('library:work', 'proxy'), ('library:other', 'direct')]
    nodes = [{'name': '香港 01'}, {'name': '日本 01'}]
    groups, policies, final = template_strategy(rules, 'proxy', ['sub'], ['manual'], nodes,
                                                [{'id': 'manual', 'name': '新加坡 01'}])
    names = {group['name']: group for group in groups}
    ids = {group['id']: group['name'] for group in groups}
    assert policies == [
        '🍎 苹果服务',       # template direct-first; chosen proxy, so it now follows 优先代理
        '🎯 全球直连',       # template proxy-first; chosen direct
        '🎮 游戏平台',       # shared with Steam: the proxy rule keeps the group
        '🎯 全球直连',       # ...and the direct one leaves it
        '📺 哔哩哔哩',       # template direct-first and chosen direct
        '✈️ 节点选择',       # template routes ChinaMax direct; chosen proxy
        'DIRECT',           # template policy is DIRECT itself
        '✈️ 节点选择', '🎯 全球直连',  # sets without a template group
    ]
    assert final == '🐟 漏网之鱼'
    assert ids[names['🍎 苹果服务']['follow_group']] == '优先代理'
    assert ids[names['🐟 漏网之鱼']['follow_group']] == '优先代理'
    assert ids[names['📺 哔哩哔哩']['follow_group']] == '优先直连'
    assert ids[names['🎮 游戏平台']['follow_group']] == '香港优先策略'
    # Regions without matching nodes are gone, including from every group list.
    assert {'🇭🇰 香港节点', '🇯🇵 日本节点', '🇸🇬 新加坡节点'} <= set(names)
    assert not {'🇹🇼 台湾节点', '🇺🇸 美国节点', '🇰🇷 韩国节点', '🇺🇸 美国负载均衡'} & set(names)
    assert all(item in ids for group in groups for item in group.get('include_groups') or [])
    assert names['🇭🇰 香港节点']['subscriptions'] == ['sub'] and names['🇭🇰 香港节点']['manual_nodes'] == []
    assert names['🇸🇬 新加坡节点']['subscriptions'] == [] and names['🇸🇬 新加坡节点']['manual_nodes'] == ['manual']
    assert names['ALL']['subscriptions'] == ['sub'] and names['ALL']['manual_nodes'] == ['manual']
    _, _, final = template_strategy(rules, 'direct', ['sub'], [], nodes, [])
    assert final == 'DIRECT'


def test_setup_profile_uses_template_groups_with_regions_from_the_nodes(setup_api):
    from backend.converters.mihomo import generate_mihomo_config
    client, repository = setup_api
    session = start(client, 'gateway')
    hong_kong = {**deepcopy(NODE), 'id': 'hk', 'name': '香港 01'}
    japan = {**deepcopy(NODE), 'id': 'jp', 'name': '日本 01', 'server': '192.0.2.2'}
    repository.update_shared_transaction(lambda shared: shared['nodes'].extend([hong_kong, japan]))
    client.post(f'/api/setup/{SESSION}/source', json={'node_ids': ['hk', 'jp']})
    response = client.post(f'/api/setup/{SESSION}/prepare', json={})
    assert response.status_code == 200, response.json
    snapshot, _ = repository.get_publish_snapshot(session['profile_id'])
    document = yaml.safe_load(generate_mihomo_config(snapshot, base_url='https://config.test', preflight_providers=False))
    emitted = {group['name']: group for group in document['proxy-groups']}
    assert emitted['🇭🇰 香港节点']['proxies'] == ['香港 01']
    assert emitted['🇯🇵 日本节点']['proxies'] == ['日本 01']
    assert '🇺🇸 美国节点' not in emitted and '🇺🇸 美国节点' not in emitted['✈️ 节点选择']['proxies']
    assert emitted['📲 Telegram']['proxies'][0] == '✈️ 节点选择'
    assert emitted['🍎 苹果服务']['proxies'][0] == '✈️ 节点选择'
    assert document['rules'][-1] == 'MATCH,🐟 漏网之鱼'
    assert emitted['🐟 漏网之鱼']['proxies'][0] == '✈️ 节点选择'


def test_finishing_can_make_the_setup_profile_the_default_and_names_leftovers(installed):
    client, repository, profile_id, _, _ = installed
    response = client.get(f'/api/setup/{SESSION}')
    assert response.json['default_profile'] == {'is_default': False, 'suggested': True,
                                                'current': {'id': 'default', 'name': '默认配置'}}
    assert client.post(f'/api/setup/{SESSION}/check', json={}).status_code == 200
    choices = {'router_mode': 'manual', 'ack_routes': True, 'ack_dns': True}
    assert client.post(f'/api/setup/{SESSION}/finish', json={**choices, 'make_default': 'yes'}).status_code == 400
    response = client.post(f'/api/setup/{SESSION}/finish', json={**choices, 'make_default': True})
    assert response.status_code == 200, response.json
    assert repository.default_profile_id() == profile_id
    summary = response.json['default_profile']
    assert summary['is_default'] and not summary['suggested']
    assert summary['leftovers'] == {'profile': {'id': 'default', 'name': '默认配置'}, 'subscriptions': []}
    # The template's sample subscription counts as a leftover only while
    # nothing but the old default uses it.
    sample = 'sub_1760688109177'
    repository.update_shared_transaction(lambda shared: shared['subscriptions'].append(
        {'id': sample, 'name': 'subscribe', 'url': 'https://example.invalid/sub', 'enabled': True}))
    repository.save_profile('default', {'proxy_groups': [{'id': 'g', 'name': 'G', 'type': 'select', 'subscriptions': [sample]}]})
    leftovers = client.get(f'/api/setup/{SESSION}').json['default_profile']['leftovers']
    assert leftovers['subscriptions'] == [{'id': sample, 'name': 'subscribe'}]
    repository.update_profile_transaction(profile_id, lambda profile: profile['proxy_groups'].append(
        {'id': 'uses-sample', 'name': 'Uses sample', 'type': 'select', 'subscriptions': [sample]}))
    assert client.get(f'/api/setup/{SESSION}').json['default_profile']['leftovers']['subscriptions'] == []


def test_default_is_not_suggested_while_devices_use_it(installed):
    client, repository, _, _, _ = installed
    repository.update_system_transaction(lambda system: system['agents'].append({'id': 'old', 'name': 'Old', 'profile_id': 'default'}))
    assert client.get(f'/api/setup/{SESSION}').json['default_profile']['suggested'] is False
    assert client.post(f'/api/setup/{SESSION}/check', json={}).status_code == 200
    response = client.post(f'/api/setup/{SESSION}/finish', json={'router_mode': 'manual', 'ack_routes': True, 'ack_dns': True})
    assert response.status_code == 200, response.json
    assert repository.default_profile_id() == 'default' and 'leftovers' not in response.json['default_profile']
