"""Every publication entry enforces gateway Agent support before any upload."""
from copy import deepcopy

import pytest

from backend.agents.deployment_bundle import build_deployment_bundle
from backend.agents.linux_gateway import MIN_AGENT_VERSION
from backend.routes.agents import _prepare_transactional_content
from backend.test_agent_deployments import env  # noqa: F401
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401


def config(enabled):
    return {'profile_id': 'default', 'system_config': {}, 'rule_configs': [], 'rule_library': [],
            'mihomo': {'custom_config': 'mode: rule\ndns: {enable: false}\n',
                       'linux_gateway': {'enabled': enabled, 'rule_set_ids': []}}}


@pytest.mark.parametrize('version', ['', '1.8.24-go', 'invalid'])
def test_shared_preparation_rejects_old_gateway_but_preserves_ordinary(version):
    agent = {'id': 'old', 'service_type': 'mihomo', 'version': version}
    with pytest.raises(ValueError, match=MIN_AGENT_VERSION):
        _prepare_transactional_content(agent, config(True))
    assert _prepare_transactional_content(agent, config(False))['bundle'].manifest['service_type'] == 'mihomo'


def test_supported_agent_can_prepare_gateway():
    prepared = _prepare_transactional_content(
        {'id': 'new', 'service_type': 'mihomo', 'version': MIN_AGENT_VERSION}, config(True))
    assert 'x-configflow-linux-gateway:' in prepared['config_content']


@pytest.mark.parametrize('enabled', [False, True])
def test_direct_bundle_upload_does_not_bypass_support_check(env, monkeypatch, enabled):
    _, manager, agent, snapshot = env
    calls = []
    def request(agent, method, path, **kwargs):
        calls.append(path)
        if path == 'capabilities':
            return {'deployment_protocols': [1], 'service_type': 'mihomo'}
        return {'success': True, 'status': 'ready'}
    monkeypatch.setattr(manager, '_deployment_request', request)
    marker = 'x-configflow-linux-gateway: {enabled: true, version: 1}\n' if enabled else ''
    bundle = build_deployment_bundle(agent, snapshot, 'mode: rule\n' + marker)
    result = manager.publish_deployment(agent['id'], bundle, activate=False)
    if enabled:
        assert result['status'] == 'failed' and MIN_AGENT_VERSION in result['message']
        assert calls == []
    else:
        assert result['status'] == 'ready' and calls == ['capabilities', 'deployments']


@pytest.mark.parametrize('enabled', [False, True])
def test_normal_publish_route_enforces_gateway_support(authenticated_app, monkeypatch, enabled):
    client, manager, registrations, admin, _ = authenticated_app
    agent_id = registrations[0]['id']
    def update(profile):
        profile.update(deepcopy(config(enabled)))
    manager.repository.update_profile_transaction('default', update)
    calls = []
    def request(agent, method, path, **kwargs):
        calls.append(path)
        if path == 'capabilities':
            return {'deployment_protocols': [1], 'service_type': 'mihomo'}
        return {'success': True, 'status': 'ready'}
    monkeypatch.setattr(manager, '_deployment_request', request)
    response = client.post(f'/api/agents/{agent_id}/push-config', headers=admin, json={'restart': False})
    result = response.get_json()
    if enabled:
        assert response.status_code >= 400 and MIN_AGENT_VERSION in result['message']
        assert calls == []
    else:
        assert response.status_code < 300 and result['status'] == 'ready'
        assert calls == ['capabilities', 'deployments']


def full_tun(custom_config=None, gateway=False):
    from backend.common.router_split import full_tun_base_yaml
    value = config(gateway)
    value['mihomo']['custom_config'] = (full_tun_base_yaml(value['mihomo']['custom_config'])
                                        if custom_config is None else custom_config)
    value['mihomo']['router_split'] = {'version': 1, 'takeover': 'full_tun', 'route_rule_set_ids': [], 'origin': 'setup'}
    return value


@pytest.fixture
def host(monkeypatch):
    """The Agent status a full TUN publish reads; no gateway record by default."""
    state = {'status': {'success': True, 'managed': {'active': False, 'healthy': False, 'preserves_forwarding': False}},
             'calls': []}
    def call(manager, agent_id, operation, method, profile_id, body=None):
        state['calls'].append((agent_id, operation, method, profile_id))
        if isinstance(state['status'], Exception):
            raise state['status']
        return deepcopy(state['status'])
    monkeypatch.setattr('backend.agents.linux_gateway.call', call)
    return state


@pytest.mark.parametrize('version', ['1.8.25-go', '1.8.39-go', ''])
def test_full_tun_profile_needs_a_forwarding_agent_on_shell(host, version):
    from backend.agents.linux_gateway import FORWARDING_MIN_AGENT_VERSION
    agent = {'id': 'old', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': version}
    with pytest.raises(ValueError, match=FORWARDING_MIN_AGENT_VERSION):
        _prepare_transactional_content(agent, full_tun())
    # Docker never runs the host gateway, so an older image keeps publishing.
    docker = {**agent, 'deployment_method': 'docker'}
    prepared = _prepare_transactional_content(docker, full_tun())
    assert 'auto-redirect: true' in prepared['config_content']
    current = {**agent, 'version': FORWARDING_MIN_AGENT_VERSION}
    assert 'x-configflow-linux-gateway' not in _prepare_transactional_content(current, full_tun())['config_content']


def test_full_tun_profile_without_tun_is_never_published(host):
    from backend.common.router_split import GENERATED_TUN_MISSING
    agent = {'id': 'new', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.8.40-go'}
    with pytest.raises(ValueError) as error:
        _prepare_transactional_content(agent, full_tun('mode: rule\ndns: {enable: false}\n'))
    assert str(error.value) == GENERATED_TUN_MISSING


def test_full_tun_profile_with_the_gateway_enabled_cannot_generate(host):
    from backend.common.router_split import RouterSplitError
    agent = {'id': 'new', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.8.40-go'}
    with pytest.raises(RouterSplitError) as error:
        _prepare_transactional_content(agent, full_tun(gateway=True))
    assert error.value.code == 'ROUTER_SPLIT_FULL_TUN'
    invalid = full_tun()
    invalid['mihomo']['router_split']['version'] = 2
    with pytest.raises(RouterSplitError) as error:
        _prepare_transactional_content(agent, invalid)
    assert error.value.code == 'ROUTER_SPLIT_INVALID'


@pytest.mark.parametrize('method', ['shell', 'docker'])
def test_gateway_and_ordinary_profiles_keep_their_agent_requirements(host, method):
    agent = {'id': 'old', 'service_type': 'mihomo', 'deployment_method': method, 'version': MIN_AGENT_VERSION}
    # goal=gateway: the selective gateway still needs only the gateway version.
    assert 'x-configflow-linux-gateway:' in _prepare_transactional_content(agent, config(True))['config_content']
    ordinary = {**agent, 'version': '1.8.0-go'}
    assert _prepare_transactional_content(ordinary, config(False))['bundle'].manifest['service_type'] == 'mihomo'
    oxidns = {'id': 'dns', 'service_type': 'oxidns', 'deployment_method': method, 'version': '1.8.0-go'}
    assert _prepare_transactional_content(oxidns, full_tun())['bundle'].manifest['service_type'] == 'oxidns'


@pytest.mark.parametrize('managed', [
    {'active': True, 'healthy': True}, {'active': False, 'pending': True}, {'active': False, 'error': 'journal unreadable'},
])
def test_full_tun_publish_never_lets_an_old_gateway_record_turn_forwarding_off(host, managed):
    """A legacy host rebound to a full TUN profile: the deployed config has no
    gateway marker, so the Agent removes its record and, without the persisted
    AIO forwarding, writes ip_forward back to the journal's original 0."""
    from backend.agents.linux_gateway import FORWARDING_NOT_KEPT
    agent = {'id': 'rebound', 'profile_id': 'home', 'service_type': 'mihomo', 'deployment_method': 'shell',
             'version': '1.8.40-go'}
    host['status'] = {'success': True, 'managed': {**managed, 'preserves_forwarding': False}}
    with pytest.raises(ValueError) as error:
        _prepare_transactional_content(agent, full_tun())
    assert str(error.value) == FORWARDING_NOT_KEPT.format(action='发布完整 TUN 后，')
    assert host['calls'] == [('rebound', 'status', 'GET', 'home')]
    # Once the forwarding is persisted, removing the record keeps it on.
    host['status']['managed']['preserves_forwarding'] = True
    assert 'auto-redirect: true' in _prepare_transactional_content(agent, full_tun())['config_content']


def test_full_tun_publish_refuses_when_the_gateway_record_cannot_be_checked(host):
    from backend.agents.oxidns_monitor import MonitorError
    agent = {'id': 'm', 'profile_id': 'home', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.8.40-go'}
    host['status'] = MonitorError('AGENT_UNREACHABLE', 502, '暂时无法连接 Agent，请检查设备连接。')
    with pytest.raises(ValueError, match='无法确认设备上是否仍有旧版 Linux 网关记录.*暂时无法连接 Agent'):
        _prepare_transactional_content(agent, full_tun())


def test_status_is_only_read_for_a_full_tun_shell_mihomo(host):
    shell = {'id': 'm', 'profile_id': 'home', 'service_type': 'mihomo', 'deployment_method': 'shell', 'version': '1.8.40-go'}
    _prepare_transactional_content({**shell, 'deployment_method': 'docker'}, full_tun())
    _prepare_transactional_content(shell, config(False))
    _prepare_transactional_content({**shell, 'version': MIN_AGENT_VERSION}, config(True))
    _prepare_transactional_content({**shell, 'service_type': 'oxidns'}, full_tun())
    assert host['calls'] == []


def test_full_tun_publish_refuses_a_filtered_tun():
    """Lab v2-E2: tun.exclude-interface [eth0] let forwarded SYNs loop between router and Linux."""
    import yaml as pyyaml
    from backend.common.router_split import GENERATED_TUN_FILTERED, full_tun_base_yaml
    agent = {'id': 'new', 'service_type': 'mihomo', 'deployment_method': 'docker', 'version': '1.8.40-go'}
    base = pyyaml.safe_load(full_tun_base_yaml('mode: rule\ndns: {enable: false}\n'))
    base['tun'].update({'exclude-interface': ['eth0'], 'include-uid': [0]})
    with pytest.raises(ValueError) as error:
        _prepare_transactional_content(agent, full_tun(pyyaml.safe_dump(base)))
    assert str(error.value) == GENERATED_TUN_FILTERED.format(keys='exclude-interface、tun.include-uid')
    # An empty filter list filters nothing.
    base['tun'].update({'exclude-interface': [], 'include-uid': None})
    assert 'auto-redirect: true' in _prepare_transactional_content(agent, full_tun(pyyaml.safe_dump(base)))['config_content']


def old_split(*, gateway=False, routed=True, used=True):
    """An unmarked profile from the old router split wizard, its base YAML now a full TUN.

    ``used``: the editor changed the gateway once, which keeps a restore point.
    """
    from backend.common.linux_gateway_draft import RESTORE_KEY
    from backend.common.router_split import full_tun_base_yaml
    value = config(gateway)
    value['mihomo']['custom_config'] = full_tun_base_yaml(value['mihomo']['custom_config'])
    if routed:
        value['routers'] = {'items': [{'id': 'r1', 'proxy_gateway_agent_id': 'split'}]}
    if used:
        value['mihomo'][RESTORE_KEY] = {'version': 1, 'id': 'p', 'created_at': '2026-10-01T00:00:00+00:00',
                                        'mihomo': {}, 'oxidns': {}}
    return value


SPLIT_AGENT = {'id': 'split', 'profile_id': 'home', 'service_type': 'mihomo', 'deployment_method': 'shell',
               'version': '1.8.40-go'}


@pytest.fixture
def sessions(monkeypatch):
    value = []
    monkeypatch.setattr('backend.common.setup_flow.profile_sessions', lambda repository, profile_id: deepcopy(value))
    return value


@pytest.mark.parametrize('match', ['topology', 'installed', 'setup'])
def test_old_router_split_gateway_off_publish_keeps_forwarding(host, sessions, match):
    """Lab v2-H: publishing the gateway off made the Agent remove its record and set ip_forward back to 0."""
    from backend.agents.linux_gateway import PUBLISH_GATEWAY_OFF_NOT_KEPT
    agent, profile = SPLIT_AGENT, old_split(routed=match != 'setup', used=match == 'topology')
    if match == 'setup':
        # The old wizard always turned the gateway on.
        sessions.append({'id': 's', 'goal': 'router', 'completed': True})
    if match == 'installed':
        # The gateway installation wizard registered this Agent.
        agent = {**SPLIT_AGENT, 'gateway_installation_id': 'install-1'}
    host['status'] = {'success': True, 'managed': {'active': True, 'healthy': True, 'preserves_forwarding': False}}
    with pytest.raises(ValueError) as error:
        _prepare_transactional_content(agent, profile)
    assert str(error.value) == PUBLISH_GATEWAY_OFF_NOT_KEPT
    assert host['calls'] == [('split', 'status', 'GET', 'home')]
    # Persisted forwarding (ordinary All-in-One), or no record at all, keeps it on.
    host['status']['managed']['preserves_forwarding'] = True
    _prepare_transactional_content(agent, profile)
    # An Agent before 1.8.40-go cannot persist it, whatever it reports.
    with pytest.raises(ValueError, match='已停止发布'):
        _prepare_transactional_content({**agent, 'version': '1.8.39-go'}, profile)
    host['status']['managed'] = {'active': False, 'preserves_forwarding': False}
    _prepare_transactional_content(agent, profile)
    _prepare_transactional_content({**agent, 'version': '1.8.39-go'}, profile)


def test_old_router_split_gateway_off_publish_refuses_an_unknown_record(host, sessions):
    from backend.agents.oxidns_monitor import MonitorError
    host['status'] = MonitorError('AGENT_UNREACHABLE', 502, '暂时无法连接 Agent，请检查设备连接。')
    with pytest.raises(ValueError, match='无法确认设备上是否仍有旧版 Linux 网关记录.*暂时无法连接 Agent'):
        _prepare_transactional_content(SPLIT_AGENT, old_split())


def test_gateway_off_publish_reads_the_status_only_for_an_old_router_split(host, sessions):
    _prepare_transactional_content({**SPLIT_AGENT, 'deployment_method': 'docker'}, old_split())
    _prepare_transactional_content(SPLIT_AGENT, old_split(gateway=True))
    _prepare_transactional_content({**SPLIT_AGENT, 'profile_id': None}, old_split())
    _prepare_transactional_content({**SPLIT_AGENT, 'id': 'other'}, old_split())
    _prepare_transactional_content(SPLIT_AGENT, old_split(routed=False))
    sessions.append({'id': 's', 'goal': 'gateway'})
    _prepare_transactional_content(SPLIT_AGENT, old_split())
    sessions[:] = [{'id': 's', 'goal': 'router', 'abandoned': True}]
    _prepare_transactional_content(SPLIT_AGENT, old_split(routed=False))
    # A topology match whose gateway was never changed in the editor or installed.
    sessions.clear()
    _prepare_transactional_content(SPLIT_AGENT, old_split(used=False))
    assert host['calls'] == []


@pytest.mark.parametrize('version', ['1.8.20-go', '1.8.24-go', ''])
def test_gateway_off_publish_skips_agents_that_never_had_the_gateway(host, sessions, version):
    """Review: an ordinary routed profile on an Agent before 1.8.25-go could not publish at all."""
    from backend.agents.oxidns_monitor import MonitorError
    host['status'] = MonitorError('UPGRADE_REQUIRED', 409, 'no gateway API')
    old = {**SPLIT_AGENT, 'version': version}
    # Without a gateway API the host has no gateway record to remove.
    for profile in (old_split(), old_split(used=False)):
        _prepare_transactional_content(old, profile)
    ordinary = config(False)
    ordinary['routers'] = {'items': [{'id': 'r1', 'proxy_gateway_agent_id': 'split'}]}
    _prepare_transactional_content(old, ordinary)
    sessions.append({'id': 's', 'goal': 'router', 'completed': True})
    _prepare_transactional_content(old, old_split(routed=False, used=False))
    assert host['calls'] == []


def test_gateway_off_publish_never_reads_the_status_for_a_profile_that_never_used_the_gateway(host, sessions):
    """Review: an ordinary AIO profile with ConfigFlow-managed routes now waited on (or failed with) a status call."""
    from backend.agents.oxidns_monitor import MonitorError
    host['status'] = MonitorError('AGENT_UNREACHABLE', 502, '暂时无法连接 Agent，请检查设备连接。')
    ordinary = config(False)
    ordinary['routers'] = {'items': [{'id': 'r1', 'proxy_gateway_agent_id': 'split'}]}
    for version in ('1.8.30-go', '1.8.40-go'):
        _prepare_transactional_content({**SPLIT_AGENT, 'version': version}, ordinary)
        _prepare_transactional_content({**SPLIT_AGENT, 'version': version}, old_split(used=False))
    assert host['calls'] == []


def test_gateway_off_publish_treats_a_missing_gateway_endpoint_as_no_record(host, sessions):
    from backend.agents.oxidns_monitor import MonitorError
    # call() maps the Agent's 404 to UPGRADE_REQUIRED: it never had a gateway endpoint.
    host['status'] = MonitorError('UPGRADE_REQUIRED', 409, '设备尚未提供网关接口')
    _prepare_transactional_content(SPLIT_AGENT, old_split())
    assert host['calls'] == [('split', 'status', 'GET', 'home')]
