"""The gateway usage scheme: kept across saves, and only an explicit 'router'
scheme lets router saves rewrite the IP takeover."""
from copy import deepcopy
from pathlib import Path
import re

import pytest
import yaml

from backend.common.config import get_repository
from backend.common.linux_gateway import LinuxGatewayError, normalize_gateway_settings
from backend.common.linux_gateway_draft import (
    RESTORE_KEY, prepare_restore, prepare_settings, preview_draft, remember_original,
)
from backend.common.linux_gateway_scheme import effective_scheme, router_takeover, sync_router_takeover
from backend.converters.mihomo import generate_mihomo_config
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401
from backend.test_linux_gateway import snapshot

GATEWAY_PATH = '/api/custom-config/linux-gateway'
LIBRARY = [
    {'id': 'lib-ip', 'name': 'Telegram IP', 'behavior': 'ipcidr', 'source_type': 'content',
     'content': '91.108.4.0/22', 'enabled': True},
    {'id': 'lib-classical', 'name': 'Chat', 'behavior': 'classical', 'source_type': 'content',
     'content': 'DOMAIN,chat.test\nIP-CIDR,198.51.100.0/24,no-resolve', 'enabled': True},
    {'id': 'lib-domain', 'name': 'Sites', 'behavior': 'domain', 'source_type': 'content',
     'content': 'example.test', 'enabled': True},
    {'id': 'lib-mrs', 'name': 'Binary', 'behavior': 'ipcidr', 'source_type': 'url', 'format': 'mrs',
     'url': 'https://rules.test/ip.mrs', 'enabled': True},
    {'id': 'lib-off', 'name': 'Off', 'behavior': 'ipcidr', 'source_type': 'content',
     'content': '203.0.113.0/24', 'enabled': True},
]
RULES = [
    {'id': 'ip', 'itemType': 'ruleset', 'library_rule_id': 'lib-ip', 'policy': 'DIRECT', 'enabled': True},
    {'id': 'classical', 'itemType': 'ruleset', 'library_rule_id': 'lib-classical', 'policy': 'DIRECT', 'enabled': True},
    {'id': 'domain', 'itemType': 'ruleset', 'library_rule_id': 'lib-domain', 'policy': 'DIRECT', 'enabled': True},
    {'id': 'mrs', 'itemType': 'ruleset', 'library_rule_id': 'lib-mrs', 'policy': 'DIRECT', 'enabled': True},
    {'id': 'off', 'itemType': 'ruleset', 'library_rule_id': 'lib-off', 'policy': 'DIRECT', 'enabled': False},
]


def routed_config(routers, gateway=None):
    rules = deepcopy(RULES)
    library = {item['id']: item for item in LIBRARY}
    for rule in rules:
        rule.update({key: value for key, value in library[rule['library_rule_id']].items() if key != 'id'})
        rule['enabled'] = rule['id'] != 'off'
    return {'mihomo': {'custom_config': '', 'linux_gateway': gateway or {'enabled': False, 'rule_set_ids': []}},
            'rule_configs': rules, 'rule_library': deepcopy(LIBRARY), 'routers': {'items': routers}}


AGENTS = [{'id': 'm1', 'service_type': 'mihomo'}, {'id': 'm2', 'service_type': 'mihomo', 'profile_id': 'other'},
          {'id': 'dns', 'service_type': 'oxidns'}]


def test_scheme_is_validated_kept_and_never_added():
    old = {'enabled': True, 'rule_set_ids': ['a', 'a']}
    assert normalize_gateway_settings(old) == {'enabled': True, 'rule_set_ids': ['a']}
    assert normalize_gateway_settings({**old, 'scheme': None}) == {'enabled': True, 'rule_set_ids': ['a']}
    for scheme in ('router', 'gateway'):
        assert normalize_gateway_settings({**old, 'scheme': scheme})['scheme'] == scheme
    for scheme in ('', 'Router', 1, ['router']):
        with pytest.raises(LinuxGatewayError, match='使用方式无效'):
            normalize_gateway_settings({**old, 'scheme': scheme})


def test_scheme_never_reaches_generated_mihomo():
    config = snapshot()
    config['mihomo']['linux_gateway']['scheme'] = 'router'
    text = generate_mihomo_config(config, preflight_providers=False)
    assert 'scheme' not in text
    marker = yaml.safe_load(text)['x-configflow-linux-gateway']
    assert set(marker) == {'enabled', 'version', 'provider_prefix', 'profile_id'}


def test_effective_scheme_prefers_stored_and_infers_only_from_bound_mihomo():
    routers = [{'id': 'a', 'type': 'ikuai', 'proxy_gateway_agent_id': 'm1'}]
    assert effective_scheme(routed_config(routers), AGENTS, 'default', 'default') == {'scheme': 'router', 'inferred': True}
    gateway = {'enabled': False, 'rule_set_ids': [], 'scheme': 'gateway'}
    assert effective_scheme(routed_config(routers, gateway), AGENTS, 'default', 'default') == {
        'scheme': 'gateway', 'inferred': False}
    # Another profile's Mihomo, a DNS agent or no gateway do not count.
    for agent_id in ('m2', 'dns', '', 'missing'):
        config = routed_config([{**routers[0], 'proxy_gateway_agent_id': agent_id}])
        assert effective_scheme(config, AGENTS, 'default', 'default') == {'scheme': 'gateway', 'inferred': True}
    # An agent without a profile belongs to whichever profile is the default.
    assert effective_scheme(routed_config(routers), AGENTS, 'default', 'home')['scheme'] == 'gateway'


def test_router_takeover_unions_in_order_and_skips_unusable_sets():
    routers = [
        {'id': 'a', 'type': 'routeros', 'proxy_gateway_agent_id': 'm1',
         'ip_rule_sets': ['classical', 'ip', 'domain'], 'ros_policy_rule_sets': ['ip', 'mrs']},
        {'id': 'b', 'type': 'ikuai', 'proxy_gateway_agent_id': 'm1',
         'ip_rule_sets': ['ip', 'off', 'gone'], 'ros_policy_rule_sets': ['ignored']},
        {'id': 'c', 'type': 'ikuai', 'proxy_gateway_agent_id': 'm2', 'ip_rule_sets': ['ip-elsewhere']},
    ]
    result = router_takeover(routed_config(routers), AGENTS, 'default', 'default')
    # RouterOS legacy overlap ('ip' in both lists) is read as a policy set once.
    assert result['rule_set_ids'] == ['classical', 'ip']
    assert result['skipped'] == [
        {'id': 'domain', 'name': 'Sites', 'reason': '域名规则集不能用于 IP 接管'},
        {'id': 'mrs', 'name': 'Binary', 'reason': 'MRS 格式不支持'},
        {'id': 'off', 'name': 'Off', 'reason': '规则集已停用'},
        {'id': 'gone', 'name': 'gone', 'reason': '规则集不存在'},
    ]


def test_dialog_save_keeps_stored_scheme_and_router_scheme_mirrors_routers():
    routers = [{'id': 'a', 'type': 'ikuai', 'proxy_gateway_agent_id': 'm1', 'ip_rule_sets': ['ip', 'domain']}]
    config = routed_config(routers, {'enabled': False, 'rule_set_ids': [], 'scheme': 'gateway'})
    def takeover(current):
        return router_takeover(current, AGENTS, 'default', 'default')
    # Old clients omit the scheme: it is kept and their list is used as-is.
    for value in ({'enabled': False, 'rule_set_ids': ['classical']},
                  {'enabled': False, 'rule_set_ids': ['classical'], 'scheme': None}):
        saved = prepare_settings(config, value, takeover)['mihomo']['linux_gateway']
        assert saved == {'enabled': False, 'rule_set_ids': ['classical'], 'scheme': 'gateway'}
    # Choosing router ignores the client's list in favour of 上游路由.
    saved = prepare_settings(config, {'enabled': False, 'rule_set_ids': ['classical'], 'scheme': 'router'},
                             takeover)['mihomo']['linux_gateway']
    assert saved == {'enabled': False, 'rule_set_ids': ['ip'], 'scheme': 'router'}
    stored_router = routed_config(routers, saved)
    kept = prepare_settings(stored_router, {'enabled': False, 'rule_set_ids': []}, takeover)
    assert kept['mihomo']['linux_gateway'] == saved
    # Legacy profiles without a scheme stay without one.
    legacy = routed_config(routers)
    assert prepare_settings(legacy, {'enabled': False, 'rule_set_ids': ['ip']}, takeover)[
        'mihomo']['linux_gateway'] == {'enabled': False, 'rule_set_ids': ['ip']}


def test_preview_reports_a_scheme_only_change():
    config = snapshot()
    candidate = deepcopy(config)
    candidate['mihomo']['linux_gateway']['scheme'] = 'router'
    changes = preview_draft(config, candidate, 1, 'save')['changes']
    assert changes == [{'path': 'Linux 网关.使用方式', 'before': '未设置', 'after': '主路由分流'}]
    config['mihomo']['linux_gateway']['scheme'] = 'router'
    candidate['mihomo']['linux_gateway']['scheme'] = 'gateway'
    assert preview_draft(config, candidate, 1, 'save')['changes'] == [
        {'path': 'Linux 网关.使用方式', 'before': '主路由分流', 'after': 'Linux 网关'}]
    assert preview_draft(config, deepcopy(config), 1, 'save')['changes'] == []


def test_choosing_a_scheme_alone_takes_no_restore_point():
    config = snapshot()
    candidate = deepcopy(config)
    candidate['mihomo']['linux_gateway']['scheme'] = 'gateway'
    remember_original(config, candidate)
    assert RESTORE_KEY not in candidate['mihomo']
    candidate['mihomo']['linux_gateway']['rule_set_ids'] = []
    remember_original(config, candidate)
    assert RESTORE_KEY in candidate['mihomo']


def test_restore_undoes_a_switch_to_the_router_scheme():
    routers = [{'id': 'a', 'type': 'ikuai', 'proxy_gateway_agent_id': 'm1', 'ip_rule_sets': ['ip']}]
    def takeover(config):
        return router_takeover(config, AGENTS, 'default', 'default')
    original = routed_config(routers, {'enabled': True, 'rule_set_ids': ['classical'], 'scheme': 'gateway'})
    switched = prepare_settings(original, {'enabled': True, 'rule_set_ids': ['classical'], 'scheme': 'router'}, takeover)
    assert switched['mihomo']['linux_gateway'] == {'enabled': True, 'rule_set_ids': ['ip'], 'scheme': 'router'}
    remember_original(original, switched)
    restored = prepare_restore(switched, takeover)
    assert restored['mihomo']['linux_gateway'] == {'enabled': True, 'rule_set_ids': ['classical'], 'scheme': 'gateway'}
    assert {'path': 'Linux 网关.使用方式', 'before': '主路由分流', 'after': 'Linux 网关'} in \
        preview_draft(switched, restored, 1, 'restore')['changes']


@pytest.mark.parametrize('snapshot_scheme', [None, 'gateway', 'router'])
def test_restore_brings_back_the_snapshot_scheme(snapshot_scheme):
    routers = [{'id': 'a', 'type': 'ikuai', 'proxy_gateway_agent_id': 'm1', 'ip_rule_sets': ['ip']}]
    stored = {'enabled': False, 'rule_set_ids': ['classical']}
    if snapshot_scheme:
        stored['scheme'] = snapshot_scheme
    original = routed_config(routers, stored)
    current = deepcopy(original)
    current['mihomo']['linux_gateway'] = {'enabled': True, 'rule_set_ids': ['classical'], 'scheme': 'gateway'}
    remember_original(original, current)
    def takeover(config):
        return router_takeover(config, AGENTS, 'default', 'default')
    restored = prepare_restore(current, takeover)['mihomo']['linux_gateway']
    # A restored router scheme follows today's 上游路由; others come back verbatim.
    expected = {**stored, 'rule_set_ids': ['ip']} if snapshot_scheme == 'router' else stored
    assert restored == expected


@pytest.fixture
def routed(authenticated_app):
    client, _, agents, admin, _ = authenticated_app
    repository = get_repository()
    repository.save_shared({'rule_library': deepcopy(LIBRARY)})
    repository.update_system_transaction(lambda system: system['system_config'].update(config_token='token'))
    repository.update_profile_transaction('default', lambda config: config.update(rule_configs=deepcopy(RULES)))
    return client, admin, agents, repository


def set_gateway(repository, value):
    repository.update_profile_transaction('default', lambda config: config['mihomo'].update(linux_gateway=value))


def gateway(repository):
    return repository.get_profile('default')['mihomo'].get('linux_gateway')


def router_body(agent_id, ip_rule_sets, **extra):
    return {'name': 'Main', 'type': 'ikuai', 'endpoint': 'http://192.0.2.1', 'username': 'admin',
            'password': 'secret', 'proxy_gateway_agent_id': agent_id, 'ip_rule_sets': ip_rule_sets,
            'ros_policy_rule_sets': [], **extra}


@pytest.mark.parametrize('stored', [None, {'enabled': False, 'rule_set_ids': ['classical']},
                                    {'enabled': False, 'rule_set_ids': ['classical'], 'scheme': 'gateway'}])
def test_router_saves_never_touch_gateway_or_inferred_schemes(routed, stored):
    client, admin, agents, repository = routed
    if stored is not None:
        set_gateway(repository, stored)
    before = gateway(repository)
    revision = repository.config_revision('default')
    created = client.post('/api/routers', headers=admin, json=router_body(agents[0]['id'], ['ip']))
    assert created.status_code == 200, created.get_json()
    assert created.get_json()['linux_gateway_sync'] == {'synced': False, 'changed': False, 'skipped': []}
    assert gateway(repository) == before
    # The router save is the only revision bump; nothing else was written.
    assert repository.config_revision('default') == revision + 1


def test_explicit_router_scheme_syncs_on_create_update_and_delete(routed):
    client, admin, agents, repository = routed
    set_gateway(repository, {'enabled': False, 'rule_set_ids': ['classical'], 'scheme': 'router'})
    created = client.post('/api/routers', headers=admin, json=router_body(agents[0]['id'], ['ip', 'domain']))
    assert created.status_code == 200, created.get_json()
    assert created.get_json()['linux_gateway_sync'] == {
        'synced': True, 'changed': True,
        'skipped': [{'id': 'domain', 'name': 'Sites', 'reason': '域名规则集不能用于 IP 接管'}]}
    assert gateway(repository) == {'enabled': False, 'rule_set_ids': ['ip'], 'scheme': 'router'}
    path = '/api/routers/' + created.get_json()['router']['id']
    # An identical save is idempotent and does not bump the revision again.
    revision = repository.config_revision('default')
    same = client.put(path, headers=admin, json=router_body(agents[0]['id'], ['ip', 'domain']))
    assert same.get_json()['linux_gateway_sync']['changed'] is False
    assert repository.config_revision('default') == revision + 1
    updated = client.put(path, headers=admin, json=router_body(agents[0]['id'], ['classical', 'ip']))
    assert updated.get_json()['linux_gateway_sync'] == {'synced': True, 'changed': True, 'skipped': []}
    assert gateway(repository)['rule_set_ids'] == ['classical', 'ip']
    # Pointing the router elsewhere drops its sets from this profile.
    moved = client.put(path, headers=admin, json=router_body('', ['classical', 'ip']))
    assert moved.get_json()['linux_gateway_sync']['changed'] is True
    assert gateway(repository)['rule_set_ids'] == []
    client.put(path, headers=admin, json=router_body(agents[0]['id'], ['ip']))
    deleted = client.delete(path, headers=admin)
    assert deleted.status_code == 200, deleted.get_json()
    assert deleted.get_json()['linux_gateway_sync'] == {'synced': True, 'changed': True, 'skipped': []}
    assert gateway(repository) == {'enabled': False, 'rule_set_ids': [], 'scheme': 'router'}


def test_enabled_router_sync_validates_without_changing_enabled(routed):
    client, admin, agents, repository = routed
    set_gateway(repository, {'enabled': True, 'rule_set_ids': [], 'scheme': 'router'})
    created = client.post('/api/routers', headers=admin, json=router_body(agents[0]['id'], ['ip', 'classical']))
    assert created.get_json()['linux_gateway_sync']['changed'] is True
    assert gateway(repository) == {'enabled': True, 'rule_set_ids': ['ip', 'classical'], 'scheme': 'router'}
    # A profile-wide gateway problem is reported, never failing the router save.
    repository.update_profile_transaction('default', lambda config: config['mihomo'].update(
        custom_config='routing-mark: 100\n'))
    path = '/api/routers/' + created.get_json()['router']['id']
    updated = client.put(path, headers=admin, json=router_body(agents[0]['id'], ['ip']))
    assert updated.status_code == 200
    result = updated.get_json()['linux_gateway_sync']
    assert result['synced'] is False and result['changed'] is False and result['error']
    assert gateway(repository) == {'enabled': True, 'rule_set_ids': ['ip', 'classical'], 'scheme': 'router'}


def test_policy_workspace_save_syncs_router_scheme(routed):
    client, admin, agents, repository = routed
    set_gateway(repository, {'enabled': False, 'rule_set_ids': [], 'scheme': 'router'})
    body = {**router_body(agents[0]['id'], []), 'type': 'routeros'}
    created = client.post('/api/routers', headers=admin, json=body)
    path = '/api/routers/' + created.get_json()['router']['id'] + '/policy-settings'
    state = client.get(path, headers=admin).get_json()['workspace']
    saved = client.put(path, headers=admin, json={
        'revision': state['revision'], 'base_url': '', 'policies': state['sources']['policies'],
        'router': {'ip_rule_sets': ['classical'], 'ros_policy_rule_sets': ['ip', 'mrs'],
                   'proxy_gateway_agent_id': agents[0]['id']}})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()['linux_gateway_sync'] == {
        'synced': True, 'changed': True, 'skipped': [{'id': 'mrs', 'name': 'Binary', 'reason': 'MRS 格式不支持'}]}
    assert gateway(repository)['rule_set_ids'] == ['classical', 'ip']
    assert 'linux_gateway_sync' not in client.get(path, headers=admin).get_json()


def frontend_sync_fields():
    source = (Path(__file__).parents[1] / 'frontend/src/api/linuxGateway.ts').read_text(encoding='utf-8')
    body = re.search(r'interface LinuxGatewaySyncResult \{([^}]*)\}', source).group(1)
    return set(re.findall(r'(\w+)\??:', body))


def test_sync_failure_fields_match_the_frontend_type(routed, monkeypatch):
    # Each side tests its own field names; this pins the shared contract.
    client, admin, agents, repository = routed
    fields = frontend_sync_fields()
    feedback = (Path(__file__).parents[1] / 'frontend/src/lib/linuxGateway.ts').read_text(encoding='utf-8')
    assert 'sync?.error' in feedback
    set_gateway(repository, {'enabled': True, 'rule_set_ids': [], 'scheme': 'router'})
    repository.update_profile_transaction('default', lambda config: config['mihomo'].update(
        custom_config='routing-mark: 100\n'))
    invalid = client.post('/api/routers', headers=admin, json=router_body(agents[0]['id'], ['ip'])).get_json()
    assert invalid['linux_gateway_sync']['error'] and set(invalid['linux_gateway_sync']) <= fields
    def fail(*_args, **_kwargs):
        raise RuntimeError('sync broke')
    monkeypatch.setattr('backend.common.linux_gateway_scheme.sync_router_takeover', fail)
    raised = client.post('/api/routers', headers=admin, json=router_body(agents[0]['id'], ['ip'])).get_json()
    assert raised['linux_gateway_sync'] == {'synced': False, 'changed': False, 'skipped': [], 'error': 'sync broke'}
    assert set(raised['linux_gateway_sync']) <= fields


def test_sync_helper_ignores_absent_scheme_even_when_inferred_router(routed):
    _, _, agents, repository = routed
    repository.update_profile_transaction('default', lambda config: config.update(routers={'items': [
        {'id': 'r', 'name': 'Main', 'type': 'ikuai', 'endpoint': 'http://192.0.2.1', 'username': 'admin',
         'proxy_gateway_agent_id': agents[0]['id'], 'ip_rule_sets': ['ip']}]}))
    config = repository.get_compat_config('default')
    assert effective_scheme(config, agents, 'default', 'default')['inferred'] is True
    before = repository.export_all()
    assert sync_router_takeover(repository, 'default', agents) == {'synced': False, 'changed': False, 'skipped': []}
    assert repository.export_all() == before


def test_editor_state_reports_scheme_and_router_save_forces_takeover(routed):
    client, admin, agents, repository = routed
    client.post('/api/routers', headers=admin, json=router_body(agents[0]['id'], ['ip', 'domain']))
    state = client.get(GATEWAY_PATH, headers=admin).get_json()
    assert state['scheme'] is None
    assert state['effective_scheme'] == 'router' and state['scheme_inferred'] is True
    assert state['router_takeover'] == {
        'rule_set_ids': ['ip'], 'skipped': [{'id': 'domain', 'name': 'Sites', 'reason': '域名规则集不能用于 IP 接管'}]}
    settings = {'enabled': False, 'rule_set_ids': ['classical'], 'scheme': 'router'}
    review = client.post(GATEWAY_PATH + '/preview', headers=admin, json={
        'settings': settings, 'expected_config_revision': state['config_revision']}).get_json()
    assert {'path': 'Linux 网关.使用方式', 'before': '未设置', 'after': '主路由分流'} in review['changes']
    saved = client.post(GATEWAY_PATH, headers=admin, json={
        'settings': settings, 'expected_config_revision': review['config_revision'],
        'preview_token': review['preview_token']})
    assert saved.status_code == 200, saved.get_json()
    assert saved.get_json()['settings'] == {'enabled': False, 'rule_set_ids': ['ip'], 'scheme': 'router'}
    assert saved.get_json()['scheme'] == 'router' and saved.get_json()['scheme_inferred'] is False
    # Switching back to gateway uses the client's own list again.
    settings = {'enabled': False, 'rule_set_ids': ['classical'], 'scheme': 'gateway'}
    saved = client.post(GATEWAY_PATH, headers=admin, json={'settings': settings}).get_json()
    assert saved['settings'] == settings and saved['effective_scheme'] == 'gateway'
    invalid = client.post(GATEWAY_PATH, headers=admin, json={'settings': {**settings, 'scheme': 'both'}})
    assert invalid.status_code == 400 and invalid.get_json()['message'] == '使用方式无效'


def test_a_failing_wizard_bookkeeping_step_does_not_fail_the_router_save(routed, monkeypatch):
    client, admin, agents, repository = routed
    set_gateway(repository, {'enabled': False, 'rule_set_ids': [], 'scheme': 'router'})
    from backend.common import setup_flow
    def broken(*args, **kwargs):
        raise ValueError('unreadable setup session')
    monkeypatch.setattr(setup_flow, 'adopt_owned_write', broken)
    created = client.post('/api/routers', headers=admin, json=router_body(agents[0]['id'], ['ip']))
    assert created.status_code == 200, created.get_json()
    assert created.get_json()['linux_gateway_sync']['changed'] is True
    assert gateway(repository)['rule_set_ids'] == ['ip']
