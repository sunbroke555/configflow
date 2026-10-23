"""Gateway trials must be read-only; explicit saves/restores are atomic."""
from copy import deepcopy

import pytest
import yaml

from backend.common.linux_gateway_draft import RESTORE_KEY
from backend.converters.mihomo import generate_mihomo_config
from backend.test_linux_gateway import gateway_app  # noqa: F401
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401

PATH = '/api/profiles/home/custom-config/linux-gateway'
SETTINGS = {'enabled': False, 'rule_set_ids': []}


def preview(client, repository, settings=SETTINGS, *, restore=False):
    response = client.post(PATH + ('/restore/preview' if restore else '/preview'), json={
        'settings': settings, 'expected_config_revision': repository.config_revision('home'),
    })
    assert response.status_code == 200, response.json
    assert response.headers['Cache-Control'] == 'no-store'
    return response.json


def save(client, review, settings=SETTINGS, *, restore=False):
    return client.post(PATH + ('/restore' if restore else ''), json={
        'settings': settings, 'expected_config_revision': review['config_revision'],
        'preview_token': review['preview_token'],
    })


def test_trial_preview_never_writes_downloads_or_calls_agent(gateway_app, monkeypatch):
    app, repository = gateway_app
    client = app.test_client()
    def forbidden(*args, **kwargs):
        pytest.fail('preview must not access network or write configuration')
    monkeypatch.setattr('requests.sessions.Session.request', forbidden)
    monkeypatch.setattr(repository, 'update_profile_transaction', forbidden)
    original = repository.export_all()
    for settings in (SETTINGS, {'enabled': True, 'rule_set_ids': ['r']}):
        review = preview(client, repository, settings)
        assert review['action'] == 'save'
        assert review['restore_point'] is None
    assert repository.export_all() == original
    assert any(change['path'].startswith('Mihomo.tun') for change in preview(client, repository)['changes'])


def test_explicit_save_keeps_bases_and_first_snapshot_then_restores_exactly(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    repository.update_profile_transaction('home', lambda c: c.update(oxidns={
        'config_mode': 'native', 'native_config': 'plugins: []\n',
        'remote_dns': 'tls://dns.example', '_common_disabled': {'custom': 'kept'},
    }))
    original = repository.get_profile('home')
    other = repository.get_profile('default')
    result = save(client, preview(client, repository))
    assert result.status_code == 200, result.json
    current = repository.get_profile('home')
    point = current['mihomo'][RESTORE_KEY]
    assert point['mihomo'] == original['mihomo']
    assert point['oxidns'] == original['oxidns']
    assert current['mihomo']['custom_config'] == original['mihomo']['custom_config']
    assert current['oxidns'] == original['oxidns']
    assert result.json['publish_guard'] == {
        'expected_profile_id': 'home', 'expected_service_type': 'mihomo',
        'expected_config_revision': repository.config_revision('home'),
    }
    assert set(result.json['restore_point']) == {'id', 'created_at'}
    # More gateway saves retain the first baseline, even across re-enabling.
    enabled = {'enabled': True, 'rule_set_ids': []}
    assert save(client, preview(client, repository, enabled), enabled).status_code == 200
    assert repository.get_profile('home')['mihomo'][RESTORE_KEY] == point
    # Reveal intervening service edits before restoring them; unrelated profile
    # settings and shared resources are never rolled back.
    repository.update_profile_transaction('home', lambda c: (
        c['oxidns'].update(remote_dns='https://later.example/dns-query'),
        c['mihomo'].update(custom_config='mixed-port: 8888\n'),
    ) and None)
    review = preview(client, repository, restore=True)
    assert any(item['path'] == 'OxiDNS 草稿.remote_dns' for item in review['changes'])
    result = save(client, review, restore=True)
    assert result.status_code == 200, result.json
    restored = repository.get_profile('home')
    assert restored['mihomo'] == original['mihomo']
    assert restored['oxidns'] == original['oxidns']
    assert restored['rule_configs'] == original['rule_configs']
    assert result.json['restore_point'] is None
    assert repository.get_profile('default') == other


@pytest.mark.parametrize('kind', ['profile', 'shared', 'system'])
def test_intervening_edits_block_stale_save_atomically(gateway_app, kind):
    app, repository = gateway_app
    client = app.test_client()
    review = preview(client, repository)
    if kind == 'profile':
        repository.update_profile_transaction('home', lambda c: c['mihomo'].update(custom_config='mixed-port: 8888\n'))
    elif kind == 'shared':
        repository.update_shared_transaction(lambda c: c['rule_library'][0].update(name='Updated'))
    else:
        repository.update_system_transaction(lambda c: c['system_config'].update(server_domain='https://changed.example'))
    before = repository.export_all()
    result = save(client, review)
    assert result.status_code == 409, result.json
    assert repository.export_all() == before


def test_stale_restore_preview_and_cross_profile_preview_cannot_overwrite(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    review = preview(client, repository)
    copied = client.post('/api/profiles/default/custom-config/linux-gateway', json={
        'settings': SETTINGS, 'expected_config_revision': repository.config_revision('default'),
        'preview_token': review['preview_token'],
    })
    assert copied.status_code == 409
    assert save(client, review).status_code == 200
    review = preview(client, repository, restore=True)
    repository.update_profile_transaction('home', lambda c: c['oxidns'].update(remote_dns='8.8.8.8'))
    before = repository.export_all()
    assert save(client, review, restore=True).status_code == 409
    assert repository.export_all() == before


def test_mismatched_settings_missing_revision_and_missing_snapshot_are_rejected(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    before = repository.export_all()
    assert client.post(PATH + '/preview', json={'settings': SETTINGS}).status_code == 400
    assert client.post(PATH + '/restore/preview', json={
        'expected_config_revision': repository.config_revision('home')}).status_code == 400
    review = preview(client, repository)
    assert save(client, review, {'enabled': True, 'rule_set_ids': []}).status_code == 409
    for token in ('bad', '中文', None):
        review['preview_token'] = token
        assert save(client, review).status_code == 409
    assert repository.export_all() == before


def test_changed_public_origin_invalidates_preview(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    review = preview(client, repository)
    before = repository.export_all()
    response = client.post(PATH, base_url='https://another.example', json={
        'settings': SETTINGS, 'expected_config_revision': review['config_revision'],
        'preview_token': review['preview_token'],
    })
    assert response.status_code == 409
    assert repository.export_all() == before


def test_guard_is_rechecked_inside_commit_lock(gateway_app, monkeypatch):
    app, repository = gateway_app
    client = app.test_client()
    review = preview(client, repository)
    original_update = repository.update_profile_transaction
    def racing(profile_id, updater, **kwargs):
        original_update(profile_id, lambda c: c['mihomo'].update(custom_config='mixed-port: 9090\n'))
        return original_update(profile_id, updater, **kwargs)
    monkeypatch.setattr(repository, 'update_profile_transaction', racing)
    assert save(client, review).status_code == 409
    assert repository.get_profile('home')['mihomo']['custom_config'] == 'mixed-port: 9090\n'
    assert RESTORE_KEY not in repository.get_profile('home')['mihomo']


def test_runtime_backup_updates_do_not_invalidate_a_configuration_preview(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    review = preview(client, repository)
    repository.update_system_transaction(lambda c: c['backup'].update(last_backup='just now'))
    assert save(client, review).status_code == 200


def test_broken_snapshot_and_incompatible_target_are_atomic(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    repository.update_profile_transaction('home', lambda c: c['mihomo'].update({RESTORE_KEY: {'version': 1}}))
    before = repository.export_all()
    assert save(client, preview(client, repository)).status_code == 400
    assert repository.export_all() == before
    assert client.post(PATH + '/restore/preview', json={
        'expected_config_revision': repository.config_revision('home')}).status_code == 400
    repository.update_profile_transaction('home', lambda c: c['mihomo'].update(custom_config='routing-mark: 10\n'))
    before = repository.export_all()
    result = client.post(PATH + '/preview', json={
        'expected_config_revision': repository.config_revision('home'),
        'settings': {'enabled': True, 'rule_set_ids': ['r']},
    })
    assert result.status_code == 400
    assert 'routing-mark' in result.json['message']
    assert repository.export_all() == before


def test_restore_preview_hides_opaque_yaml_and_credentials(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    assert client.post(PATH, json=SETTINGS).status_code == 200
    repository.update_profile_transaction('home', lambda c: c['oxidns'].update(
        native_config='secret: opaque-native-secret', password='plaintext-secret',
        remote_dns='https://user:url-secret@dns.example/dns-query?token=query-secret'))
    review = preview(client, repository, restore=True)
    encoded = str(review['changes'])
    assert not any(secret in encoded for secret in ('opaque-native-secret', 'plaintext-secret', 'url-secret', 'query-secret'))
    assert any(item['path'] == 'OxiDNS 草稿.native_config' for item in review['changes'])


def test_legacy_save_snapshot_does_not_change_generated_yaml_and_share_export_omits_secrets(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    assert client.post(PATH, json=SETTINGS).status_code == 200
    config = repository.get_compat_config('home')
    without_snapshot = deepcopy(config)
    without_snapshot['mihomo'].pop(RESTORE_KEY)
    assert generate_mihomo_config(config, preflight_providers=False) == generate_mihomo_config(without_snapshot, preflight_providers=False)
    assert RESTORE_KEY in repository.export_all()['profiles']['home']['mihomo']
    assert RESTORE_KEY not in repository.export_all(desensitize=True)['profiles']['home']['mihomo']
    assert yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))['mixed-port'] == 7890


def test_new_draft_endpoints_require_panel_auth_not_agent_or_subscription_tokens(authenticated_app):
    client, manager, (agent, _), admin, _ = authenticated_app
    profile_id = manager.get_agent_by_id(agent['id'])['profile_id']
    path = f'/api/profiles/{profile_id}/custom-config/linux-gateway'
    for suffix in ('/preview', '/restore/preview', '/restore'):
        assert client.post(path + suffix, json={}).status_code == 401
        assert client.post(path + suffix, headers={'Authorization': 'Bearer ' + agent['token']}, json={}).status_code == 401
    response = client.get(path, headers=admin)
    assert response.status_code == 200, response.json
    review = client.post(path + '/preview', headers=admin, json={
        'settings': SETTINGS, 'expected_config_revision': response.json['config_revision'],
    })
    assert review.status_code == 200, review.json


# ---- router split guards

MARKER = {'version': 1, 'takeover': 'full_tun', 'route_rule_set_ids': ['r'], 'origin': 'setup'}
ROUTER = {'id': 'r1', 'name': 'Main', 'type': 'ikuai', 'endpoint': 'http://192.0.2.1', 'username': 'admin',
          'password': 'p', 'proxy_gateway_agent_id': 'mihomo-home'}


def make_mode(repository, mode):
    """Turn the gateway-enabled 'home' profile into one router split takeover."""
    from backend.common import setup_flow
    from backend.common.router_split import full_tun_base_yaml
    if mode == 'full_tun':
        repository.update_profile_transaction('home', lambda c: c['mihomo'].update(
            custom_config=full_tun_base_yaml(c['mihomo']['custom_config']), router_split=deepcopy(MARKER),
            linux_gateway={'enabled': False, 'rule_set_ids': []}))
    elif mode == 'invalid':
        repository.update_profile_transaction('home', lambda c: c['mihomo'].update(router_split={'version': 2}))
    elif mode == 'legacy_setup':
        setup_flow.write(repository, {'id': 'a' * 32, 'profile_id': 'home', 'name': 'Home', 'goal': 'router',
                                      'step': 5, 'max_step': 5, 'prepared': True, 'completed': True})
    elif mode == 'legacy_topology':
        repository.update_system_transaction(lambda system: system.update(agents=[
            {'id': 'mihomo-home', 'name': 'm', 'profile_id': 'home', 'service_type': 'mihomo'}]))
        repository.save_profile('home', {'routers': {'items': [deepcopy(ROUTER)]}})


def guarded(client, path, body):
    response = client.post(PATH + path, json=body)
    return response.status_code, (response.json or {}).get('code')


@pytest.mark.parametrize('mode', ['legacy_setup', 'legacy_topology'])
def test_legacy_router_split_keeps_the_gateway_on(gateway_app, mode):
    app, repository = gateway_app
    client = app.test_client()
    make_mode(repository, mode)
    state = client.get(PATH).json
    assert state['router_split']['mode'] == 'legacy'
    assert state['router_split']['source'] == mode.split('_')[1]
    assert set(state['router_split']) == {'mode', 'source', 'issues', 'route_rule_set_ids'}
    revision = repository.config_revision('home')
    before = repository.export_all()
    off = {'settings': SETTINGS, 'expected_config_revision': revision}
    assert guarded(client, '/preview', off) == (409, 'ROUTER_SPLIT_LEGACY')
    assert guarded(client, '', {**off, 'preview_token': '0' * 64}) == (409, 'ROUTER_SPLIT_LEGACY')
    # The unguarded request shape of old integrations is checked as well.
    assert guarded(client, '', SETTINGS) == (409, 'ROUTER_SPLIT_LEGACY')
    assert repository.export_all() == before
    # Changing what the gateway takes over is still allowed.
    changed = {'enabled': True, 'rule_set_ids': []}
    assert save(client, preview(client, repository, changed), changed).status_code == 200
    assert RESTORE_KEY in repository.get_profile('home')['mihomo']
    # That snapshot still forwards, so it can be restored.
    preview(client, repository, restore=True)
    # A snapshot from before the trial turns the gateway off.
    repository.update_profile_transaction('home', lambda c: c['mihomo'][RESTORE_KEY]['mihomo'].update(
        linux_gateway={'enabled': False, 'rule_set_ids': []}))
    restore = {'expected_config_revision': repository.config_revision('home')}
    assert guarded(client, '/restore/preview', restore) == (409, 'ROUTER_SPLIT_RESTORE_BLOCKED')
    response = client.post(PATH + '/restore', json={**restore, 'preview_token': '0' * 64})
    assert response.status_code == 409 and response.json == {
        'success': False, 'code': 'ROUTER_SPLIT_RESTORE_BLOCKED',
        'message': '恢复后 Linux 网关为关闭状态，主路由转来的连接会中断，已阻止。'}
    assert RESTORE_KEY in repository.get_profile('home')['mihomo']


def test_full_tun_router_split_never_enables_the_gateway(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    # A restore point taken earlier would bring back a profile without TUN.
    assert client.post(PATH, json={'enabled': True, 'rule_set_ids': []}).status_code == 200
    make_mode(repository, 'full_tun')
    state = client.get(PATH).json
    assert state['router_split'] == {'mode': 'full_tun', 'source': 'marker', 'issues': [],
                                     'route_rule_set_ids': ['r']}
    before = repository.export_all()
    revision = repository.config_revision('home')
    on = {'enabled': True, 'rule_set_ids': ['r']}
    assert guarded(client, '/preview', {'settings': on, 'expected_config_revision': revision}) == (409, 'ROUTER_SPLIT_FULL_TUN')
    assert guarded(client, '', on) == (409, 'ROUTER_SPLIT_FULL_TUN')
    restore = {'expected_config_revision': revision}
    assert guarded(client, '/restore/preview', restore) == (409, 'ROUTER_SPLIT_RESTORE_BLOCKED')
    response = client.post(PATH + '/restore', json={**restore, 'preview_token': '0' * 64})
    assert response.json['message'] == '主路由分流的配置空间不能恢复试用前快照：快照中没有完整 TUN，恢复会让分流中断。'
    assert repository.export_all() == before
    # Keeping the gateway off is an ordinary save.
    assert save(client, preview(client, repository)).status_code == 200


def test_invalid_marker_stops_every_gateway_change(gateway_app):
    app, repository = gateway_app
    client = app.test_client()
    make_mode(repository, 'invalid')
    assert client.get(PATH).json['router_split']['mode'] == 'invalid'
    revision = repository.config_revision('home')
    for settings in (SETTINGS, {'enabled': True, 'rule_set_ids': ['r']}):
        assert guarded(client, '/preview', {'settings': settings, 'expected_config_revision': revision}) == (
            409, 'ROUTER_SPLIT_INVALID')
    assert guarded(client, '/restore/preview', {'expected_config_revision': revision}) == (409, 'ROUTER_SPLIT_INVALID')


def test_router_split_guard_is_rechecked_inside_the_transaction(gateway_app, monkeypatch):
    app, repository = gateway_app
    client = app.test_client()
    on = {'enabled': True, 'rule_set_ids': ['r']}
    original_update = repository.update_profile_transaction
    def racing(profile_id, updater, **kwargs):
        # Another request turns the profile into a full TUN router split meanwhile.
        monkeypatch.setattr(repository, 'update_profile_transaction', original_update)
        make_mode(repository, 'full_tun')
        return original_update(profile_id, updater, **kwargs)
    monkeypatch.setattr(repository, 'update_profile_transaction', racing)
    # The unguarded request shape skips the revision check; the guard still runs on the locked profile.
    response = client.post(PATH, json=on)
    assert response.status_code == 409 and response.json['code'] == 'ROUTER_SPLIT_FULL_TUN'
    assert repository.get_profile('home')['mihomo']['linux_gateway']['enabled'] is False


def test_ordinary_gateway_reports_no_router_split(gateway_app):
    app, repository = gateway_app
    state = app.test_client().get(PATH).json
    assert state['router_split'] == {'mode': None, 'source': None, 'issues': [], 'route_rule_set_ids': []}


@pytest.fixture
def hosts(monkeypatch):
    """The bound Mihomo Agents and the gateway status each one reports."""
    state = {'agents': {'mihomo-home': {'id': 'mihomo-home', 'name': 'm', 'profile_id': 'home', 'service_type': 'mihomo',
                                        'deployment_method': 'shell', 'version': '1.8.40-go'}},
             'status': {'success': True, 'managed': {'active': True, 'healthy': True, 'preserves_forwarding': True}},
             'calls': []}

    class Manager:
        def get_agent_by_id(self, agent_id):
            return deepcopy(state['agents'].get(agent_id))

    def call(manager, agent_id, operation, method, profile_id, body=None):
        state['calls'].append((agent_id, operation, profile_id))
        if isinstance(state['status'], Exception):
            raise state['status']
        return deepcopy(state['status'])
    monkeypatch.setattr('backend.common.agent_manager.get_agent_manager', lambda: Manager())
    monkeypatch.setattr('backend.agents.linux_gateway.call', call)
    return state


def give_base_full_tun(repository):
    from backend.common.router_split import full_tun_base_yaml
    repository.update_profile_transaction('home', lambda c: c['mihomo'].update(
        custom_config=full_tun_base_yaml(c['mihomo'].get('custom_config') or '')))


def test_legacy_restore_to_a_base_yaml_with_its_own_full_tun_is_allowed(gateway_app, hosts):
    """Ordinary AIO with a full TUN base YAML trying the gateway: going back keeps
    the TUN, and its persisted forwarding stays when the Agent removes the record."""
    from backend.common.router_split import full_tun_base_yaml
    app, repository = gateway_app
    client = app.test_client()
    make_mode(repository, 'legacy_topology')
    changed = {'enabled': True, 'rule_set_ids': []}
    assert save(client, preview(client, repository, changed), changed).status_code == 200
    def snapshot_full_tun(config):
        snapshot = config['mihomo'][RESTORE_KEY]['mihomo']
        snapshot.update(custom_config=full_tun_base_yaml(snapshot.get('custom_config') or ''),
                        linux_gateway={'enabled': False, 'rule_set_ids': []})
    repository.update_profile_transaction('home', snapshot_full_tun)
    # Not persisted (an old router split host): the record's removal turns forwarding off.
    hosts['status']['managed']['preserves_forwarding'] = False
    restore = {'expected_config_revision': repository.config_revision('home')}
    assert guarded(client, '/restore/preview', restore) == (409, 'ROUTER_SPLIT_FORWARDING_NOT_PERSISTED')
    hosts['status']['managed']['preserves_forwarding'] = True
    review = preview(client, repository, restore=True)
    assert save(client, review, restore=True).status_code == 200
    assert repository.get_profile('home')['mihomo']['linux_gateway']['enabled'] is False


@pytest.mark.parametrize('mode', ['legacy_setup', 'legacy_topology'])
def test_legacy_split_whose_base_gained_a_full_tun_keeps_forwarding_when_turned_off(gateway_app, hosts, mode):
    """Lab v2-H: an old wizard host has no AIO forwarding persistence, so turning
    the gateway off and publishing set ip_forward back to 0 and broke UDP/ICMP."""
    from backend.agents.linux_gateway import GATEWAY_OFF_NOT_KEPT
    app, repository = gateway_app
    client = app.test_client()
    make_mode(repository, mode)
    repository.update_system_transaction(lambda system: system.update(agents=[deepcopy(hosts['agents']['mihomo-home'])]))
    give_base_full_tun(repository)
    assert client.get(PATH).json['router_split']['mode'] is None
    hosts['status']['managed']['preserves_forwarding'] = False
    before = repository.export_all()
    revision = repository.config_revision('home')
    off = {'settings': SETTINGS, 'expected_config_revision': revision}
    response = client.post(PATH + '/preview', json=off)
    assert response.status_code == 409 and response.json == {
        'success': False, 'code': 'ROUTER_SPLIT_FORWARDING_NOT_PERSISTED', 'message': GATEWAY_OFF_NOT_KEPT.format(name='m')}
    assert guarded(client, '', {**off, 'preview_token': '0' * 64}) == (409, 'ROUTER_SPLIT_FORWARDING_NOT_PERSISTED')
    assert guarded(client, '', SETTINGS) == (409, 'ROUTER_SPLIT_FORWARDING_NOT_PERSISTED')
    assert repository.export_all() == before
    assert {call[1:] for call in hosts['calls']} == {('status', 'home')}
    # Without a gateway record on the host nothing restores forwarding.
    hosts['status']['managed'] = {'active': False, 'healthy': False, 'preserves_forwarding': False}
    assert client.post(PATH + '/preview', json=off).status_code == 200
    # Persisted (as an ordinary All-in-One install has it): allowed.
    hosts['status']['managed'] = {'active': True, 'healthy': True, 'preserves_forwarding': True}
    hosts['calls'].clear()
    assert save(client, preview(client, repository)).status_code == 200
    assert repository.get_profile('home')['mihomo']['linux_gateway']['enabled'] is False
    # The status is read before the transaction, once per request.
    assert len(hosts['calls']) == 2


@pytest.mark.parametrize('change,message', [
    # A gateway record without persisted forwarding, on an Agent that cannot persist it.
    ({'version': '1.8.39-go'}, '请先将 Mihomo Agent 更新到 1.8.40-go 或更新版本'),
    ({'version': '1.8.25-go'}, '请先将 Mihomo Agent 更新到 1.8.40-go 或更新版本'),
    (None, '无法读取「m」的 IPv4 转发状态，已阻止关闭 Linux 网关：暂时无法连接 Agent，请检查设备连接。请确认设备在线后重试。'),
])
def test_legacy_full_tun_gateway_off_needs_a_current_reachable_agent(gateway_app, hosts, change, message):
    from backend.agents.oxidns_monitor import MonitorError
    app, repository = gateway_app
    client = app.test_client()
    make_mode(repository, 'legacy_topology')
    give_base_full_tun(repository)
    if change is None:
        hosts['status'] = MonitorError('AGENT_UNREACHABLE', 502, '暂时无法连接 Agent，请检查设备连接。')
    else:
        hosts['agents']['mihomo-home'].update(change)
    response = client.post(PATH + '/preview', json={'settings': SETTINGS,
                                                    'expected_config_revision': repository.config_revision('home')})
    assert response.status_code == 409 and response.json['code'] == 'ROUTER_SPLIT_FORWARDING_NOT_PERSISTED'
    assert message in response.json['message']
    # A Docker Mihomo never applies the gateway on its host.
    hosts['agents']['mihomo-home'].update(deployment_method='docker')
    assert save(client, preview(client, repository)).status_code == 200


@pytest.mark.parametrize('version,reads', [('1.8.25-go', True), ('1.8.39-go', True), ('1.8.20-go', False), ('', False)])
def test_legacy_full_tun_gateway_off_is_allowed_without_a_gateway_record(gateway_app, hosts, version, reads):
    """Review: a host that never applied the gateway keeps its forwarding, whatever its version."""
    from backend.agents.oxidns_monitor import MonitorError
    app, repository = gateway_app
    client = app.test_client()
    make_mode(repository, 'legacy_topology')
    give_base_full_tun(repository)
    hosts['agents']['mihomo-home']['version'] = version
    hosts['status'] = {'success': True, 'managed': {'active': False, 'healthy': False}}
    assert save(client, preview(client, repository)).status_code == 200
    assert bool(hosts['calls']) is reads
    # An Agent without the gateway endpoint (404) has no record either.
    hosts['calls'].clear()
    hosts['status'] = MonitorError('UPGRADE_REQUIRED', 409, '设备尚未提供网关接口')
    hosts['agents']['mihomo-home']['version'] = '1.8.30-go'
    repository.update_profile_transaction('home', lambda c: c['mihomo']['linux_gateway'].update(enabled=True))
    assert save(client, preview(client, repository)).status_code == 200


def test_legacy_topology_gateway_off_checks_only_the_routers_proxy_gateway(gateway_app, hosts):
    """Review: an offline or old secondary Mihomo that no router forwards to blocked the switch-off."""
    app, repository = gateway_app
    client = app.test_client()
    make_mode(repository, 'legacy_topology')
    second = {'id': 'mihomo-two', 'name': 'two', 'profile_id': 'home', 'service_type': 'mihomo',
              'deployment_method': 'shell', 'version': '1.8.20-go'}
    hosts['agents']['mihomo-two'] = second
    repository.update_system_transaction(lambda system: system['agents'].append(deepcopy(second)))
    give_base_full_tun(repository)
    hosts['status'] = {'success': True, 'managed': {'active': False, 'healthy': False}}
    assert save(client, preview(client, repository)).status_code == 200
    assert {call[0] for call in hosts['calls']} == {'mihomo-home'}


def test_legacy_split_with_a_full_tun_base_cannot_go_back_to_no_tun(gateway_app, hosts):
    """Restoring a snapshot without TUN while the current base has one is the old teardown."""
    from backend.common.router_split import LEGACY_RESTORE_BLOCKED
    app, repository = gateway_app
    client = app.test_client()
    make_mode(repository, 'legacy_topology')
    changed = {'enabled': True, 'rule_set_ids': []}
    assert save(client, preview(client, repository, changed), changed).status_code == 200
    give_base_full_tun(repository)
    repository.update_profile_transaction('home', lambda c: c['mihomo'][RESTORE_KEY]['mihomo'].update(
        linux_gateway={'enabled': False, 'rule_set_ids': []}))
    response = client.post(PATH + '/restore/preview', json={'expected_config_revision': repository.config_revision('home')})
    assert response.status_code == 409 and response.json['message'] == LEGACY_RESTORE_BLOCKED
    assert hosts['calls'] == []


def test_ordinary_profile_with_a_full_tun_base_turns_the_gateway_off_without_agent_calls(gateway_app, hosts):
    from backend.common import setup_flow
    app, repository = gateway_app
    client = app.test_client()
    make_mode(repository, 'legacy_topology')
    # A gateway-goal profile using an upstream router is no router split.
    setup_flow.write(repository, {'id': 'b' * 32, 'profile_id': 'home', 'name': 'Home', 'goal': 'gateway',
                                  'step': 5, 'max_step': 5, 'prepared': True, 'completed': True})
    give_base_full_tun(repository)
    hosts['status']['managed']['preserves_forwarding'] = False
    assert save(client, preview(client, repository)).status_code == 200
    assert hosts['calls'] == []


def test_full_tun_restore_message_matches_the_restore_point(gateway_app):
    from backend.common.router_split import FULL_TUN_NO_RESTORE_POINT, FULL_TUN_RESTORE_OVERWRITES, full_tun_base_yaml
    app, repository = gateway_app
    client = app.test_client()
    make_mode(repository, 'full_tun')
    restore = {'expected_config_revision': repository.config_revision('home')}
    response = client.post(PATH + '/restore/preview', json=restore)
    assert response.status_code == 409 and response.json == {
        'success': False, 'code': 'ROUTER_SPLIT_RESTORE_BLOCKED', 'message': FULL_TUN_NO_RESTORE_POINT}
    def full_tun_snapshot(config):
        config['mihomo'][RESTORE_KEY] = {'version': 1, 'id': 'p', 'created_at': '2026-10-23T00:00:00+00:00',
                                         'mihomo': {**deepcopy(config['mihomo']),
                                                    'custom_config': full_tun_base_yaml('')},
                                         'oxidns': {}}
    repository.update_profile_transaction('home', full_tun_snapshot)
    response = client.post(PATH + '/restore/preview', json={'expected_config_revision': repository.config_revision('home')})
    assert response.json['message'] == FULL_TUN_RESTORE_OVERWRITES


def test_editor_reads_setup_sessions_only_for_a_possible_legacy_split(gateway_app, monkeypatch):
    """Unrelated profiles must not wait for (or fail on) the setup session store."""
    app, repository = gateway_app
    client = app.test_client()
    def forbidden(*_args):
        raise AssertionError('setup sessions read')
    repository.update_profile_transaction('home', lambda c: c['mihomo'].update(linux_gateway={'enabled': False, 'rule_set_ids': []}))
    monkeypatch.setattr('backend.routes.linux_gateway.legacy_context', forbidden)
    monkeypatch.setattr('backend.common.router_split.legacy_context', forbidden)
    assert client.get(PATH).json['router_split']['mode'] is None
    assert save(client, preview(client, repository)).status_code == 200
    make_mode(repository, 'full_tun')
    assert client.get(PATH).json['router_split']['mode'] == 'full_tun'
    assert save(client, preview(client, repository)).status_code == 200


def test_gateway_enabled_meanwhile_without_a_context_asks_for_a_refresh(gateway_app, monkeypatch):
    app, repository = gateway_app
    client = app.test_client()
    repository.update_profile_transaction('home', lambda c: c['mihomo'].update(linux_gateway={'enabled': False, 'rule_set_ids': []}))
    original_update = repository.update_profile_transaction
    def racing(profile_id, updater, **kwargs):
        monkeypatch.setattr(repository, 'update_profile_transaction', original_update)
        original_update('home', lambda c: c['mihomo'].update(linux_gateway={'enabled': True, 'rule_set_ids': []}))
        return original_update(profile_id, updater, **kwargs)
    monkeypatch.setattr(repository, 'update_profile_transaction', racing)
    # The unguarded request shape skips the revision check that would catch this first.
    response = client.post(PATH, json=SETTINGS)
    assert response.status_code == 400 and '请刷新后重试' in response.json['message']
    assert repository.get_profile('home')['mihomo']['linux_gateway']['enabled'] is True
