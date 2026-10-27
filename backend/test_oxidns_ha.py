"""Keepalived pairing and router behavior; publication orchestration is tested in test_oxidns_ha_publish."""
import copy

import pytest
import requests

from backend.agents import oxidns_ha
from backend.agents.manager import AgentDeploymentConflict, AgentManager
from backend.common import config as config_store
from backend.common.config_repository import ProfileRepository
from backend.routers import dns_guard
from backend.routers.dhcp_dns import dns_ha_settings
from backend.routers.release import execute
from backend.test_agent_upgrade_auth import authenticated_app  # noqa: F401 (pytest fixture)
from backend.test_router_dhcp_dns import DESIRED as DHCP_DESIRED, ROUTER as DHCP_ROUTER, MemoryDriver, plan_for
from backend.test_router_dns_guard import DESIRED as GUARD_DESIRED, NETWATCH, NAT, device, preview, publish, router

PRIMARY_HOST, SECONDARY_HOST, VIP = '192.168.1.20', '192.168.1.21', '192.168.1.53'


class Devices:
    """Both OxiDNS Agents' HA endpoints, recorded in call order."""

    def __init__(self):
        self.calls = []
        self.protocols = {}
        self.blockers = {}
        self.cidr = '192.168.1.0/24'
        self.fail_apply = set()
        self.fail_remove = set()
        self.runtime = {}
        self.managed = {}

    def capabilities(self, agent, method, suffix, **_):
        assert (method, suffix) == ('get', 'capabilities')
        self.calls.append(('capabilities', agent['host']))
        return {'success': True, 'oxidns_ha_protocols': self.protocols.get(agent['host'], [1])}

    def status(self, agent):
        self.calls.append(('status', agent['host']))
        return {'protocol': 1, 'managed': self.managed.get(agent['host'], {'active': True, 'pending': False}),
                'runtime': self.runtime.get(agent['host'], {'keepalived_running': True, 'role': 'backup',
                                                            'vip_present': False, 'healthy': True}),
                'error': ''}

    def preview(self, agent, spec):
        self.calls.append(('preview', agent['host'], copy.deepcopy(spec)))
        blockers = self.blockers.get(agent['host'], [])
        return {'ok': not blockers, 'blockers': blockers, 'interface': 'eth0', 'cidr': self.cidr}

    def apply(self, agent, spec):
        self.calls.append(('apply', agent['host'], copy.deepcopy(spec)))
        if agent['host'] in self.fail_apply or (agent['host'], spec['vip']) in self.fail_apply:
            raise RuntimeError('keepalived 启动失败')
        return {'protocol': 1}

    def remove(self, agent):
        self.calls.append(('remove', agent['host']))
        if agent['host'] in self.fail_remove:
            raise requests.ConnectionError('offline')
        return {'removed': True}

    def kinds(self):
        return [(call[0], call[1]) for call in self.calls]


@pytest.fixture
def pair(tmp_path, monkeypatch):
    from backend.agents import oxidns_ha_publish
    monkeypatch.setattr(oxidns_ha_publish, 'start_loop', lambda _: None)
    previous = config_store._repository
    repository = ProfileRepository(tmp_path)
    config_store.set_repository(repository)
    manager = AgentManager(repository)
    devices = Devices()
    monkeypatch.setattr(oxidns_ha, 'SETTLE_SECONDS', 0)
    monkeypatch.setattr(manager, '_deployment_request', devices.capabilities)
    for name in ('status', 'preview', 'apply', 'remove'):
        monkeypatch.setattr(manager, f'oxidns_ha_{name}', getattr(devices, name))
    primary = manager.register_agent({'name': 'DNS-A', 'host': PRIMARY_HOST, 'service_type': 'oxidns',
                                      'deployment_method': 'shell'})
    secondary = manager.register_agent({'name': 'DNS-B', 'host': SECONDARY_HOST, 'service_type': 'oxidns',
                                        'deployment_method': 'shell'})
    yield manager, devices, primary, secondary
    config_store.set_repository(previous)


def enable(manager, primary, secondary, **request):
    return oxidns_ha._configure_keepalived(manager, primary['id'], {'peer_id': secondary['id'], 'vip': VIP, **request})


def test_enable_applies_secondary_first_and_writes_both_records(pair):
    manager, devices, primary, secondary = pair
    devices.runtime[PRIMARY_HOST] = {'keepalived_running': True, 'role': 'master', 'vip_present': True, 'healthy': True}
    result = enable(manager, primary, secondary)
    assert devices.kinds() == [('capabilities', PRIMARY_HOST), ('capabilities', SECONDARY_HOST),
                               ('preview', PRIMARY_HOST), ('preview', SECONDARY_HOST),
                               ('apply', SECONDARY_HOST), ('apply', PRIMARY_HOST),
                               ('status', PRIMARY_HOST), ('status', SECONDARY_HOST)]
    applies = {call[1]: call[2] for call in devices.calls if call[0] == 'apply'}
    assert applies[PRIMARY_HOST] == {'safety_version': 1, 'vip': VIP, 'vrid': 53, 'priority': 150,
                                     'self_ip': PRIMARY_HOST, 'peer_ip': SECONDARY_HOST}
    assert applies[SECONDARY_HOST] == {'safety_version': 1, 'vip': VIP, 'vrid': 53, 'priority': 100,
                                       'self_ip': SECONDARY_HOST, 'peer_ip': PRIMARY_HOST}
    first, second = (manager.get_agent_by_id(item['id']) for item in (primary, secondary))
    group = first['oxidns_ha']['group_id']
    assert first['oxidns_ha'] == {'enabled': True, 'group_id': group, 'peer_id': secondary['id'], 'vip': VIP,
                                  'vrid': 53, 'role': 'primary'}
    assert second['oxidns_ha'] == {**first['oxidns_ha'], 'peer_id': primary['id'], 'role': 'secondary'}
    assert first['oxidns_ha_state']['status'] == second['oxidns_ha_state']['status'] == 'succeeded'
    assert result['health'] == 'ok' and result['config'] == first['oxidns_ha']
    assert [member['agent_id'] for member in result['members']] == [primary['id'], secondary['id']]
    assert result['routers'] == [] and result['warnings'] == []


def test_enable_reports_the_elected_holder_not_the_election(pair, monkeypatch):
    manager, devices, primary, secondary = pair
    monkeypatch.setattr(oxidns_ha, 'SETTLE_SECONDS', 10)
    monkeypatch.setattr(oxidns_ha.time, 'sleep', lambda _: devices.runtime.update({PRIMARY_HOST: {
        'keepalived_running': True, 'role': 'master', 'vip_present': True, 'healthy': True}}))
    result = enable(manager, primary, secondary)
    assert result['health'] == 'ok'
    assert devices.kinds().count(('status', PRIMARY_HOST)) == 2

@pytest.mark.parametrize(('vip', 'vrid', 'expected'), [('192.168.1.0', None, 53), ('192.168.1.255', None, 53),
                                                       ('192.168.1.7', None, 7), (VIP, 200, 200)])
def test_vrid_comes_from_the_vip_unless_given(pair, vip, vrid, expected):
    manager, devices, primary, secondary = pair
    devices.cidr = '192.168.0.0/23'
    request = {'vip': vip} if vrid is None else {'vip': vip, 'vrid': vrid}
    enable(manager, primary, secondary, **request)
    assert manager.get_agent_by_id(primary['id'])['oxidns_ha']['vrid'] == expected


@pytest.mark.parametrize(('change', 'message'), [
    ({'vip': PRIMARY_HOST}, '主机地址相同'), ({'vip': '224.0.0.1'}, '单播'), ({'vip': ' 192.168.1.53'}, '单播'),
    ({'vip': '127.0.0.1'}, '单播'), ({'vrid': True}, 'VRID'), ({'vrid': 0}, 'VRID'), ({'vrid': 256}, 'VRID'),
    ({'vrid': '53'}, 'VRID'), ({'priority': 200}, '参数无效'), ({'peer_id': ''}, '对端'), ({'peer_id': 'missing'}, '对端'),
])
def test_invalid_requests_never_reach_an_agent(pair, change, message):
    manager, devices, primary, secondary = pair
    with pytest.raises(oxidns_ha.HAError, match=message):
        enable(manager, primary, secondary, **change)
    assert devices.calls == []
    assert 'oxidns_ha' not in manager.get_agent_by_id(primary['id'])


@pytest.mark.parametrize(('edit', 'message'), [
    ({'service_type': 'mihomo'}, '不是 OxiDNS'),
    ({'profile_id': 'other'}, '配置空间'), ({'host': PRIMARY_HOST}, '不能相同'), ({'host': 'dns.local'}, 'IPv4'),
    ({'enabled': False}, '已停用'),
])
def test_peer_must_be_a_distinct_oxidns_on_the_same_profile(pair, edit, message):
    manager, devices, primary, secondary = pair
    if 'profile_id' in edit:
        manager.repository.create_profile({'id': edit['profile_id']})

    def update(agents):
        next(item for item in agents if item['id'] == secondary['id']).update(edit)
    manager._update_agents(update)
    with pytest.raises(oxidns_ha.HAError, match=message):
        enable(manager, primary, secondary)
    assert devices.calls == []


def test_self_pairing_and_other_groups_are_rejected(pair):
    manager, devices, primary, secondary = pair
    with pytest.raises(oxidns_ha.HAError, match='不能是本机'):
        oxidns_ha.configure(manager, primary['id'], {'peer_id': primary['id'], 'vip': VIP})
    third = manager.register_agent({'name': 'DNS-C', 'host': '192.168.1.22', 'service_type': 'oxidns'})
    enable(manager, primary, secondary)
    devices.calls.clear()
    with pytest.raises(oxidns_ha.HAError, match='已加入其他 DNS 高可用组'):
        oxidns_ha.configure(manager, third['id'], {'peer_id': secondary['id'], 'vip': '192.168.1.54'})
    with pytest.raises(oxidns_ha.HAError, match='另一组'):
        oxidns_ha.configure(manager, third['id'], {'peer_id': manager.register_agent(
            {'name': 'DNS-D', 'host': '192.168.1.23', 'service_type': 'oxidns'})['id'], 'vip': VIP})
    assert devices.calls == []


def test_reconfiguring_the_same_pair_keeps_its_group(pair):
    manager, devices, primary, secondary = pair
    enable(manager, primary, secondary)
    group = manager.get_agent_by_id(primary['id'])['oxidns_ha']['group_id']
    # From the other card: that Agent becomes the primary.
    oxidns_ha._configure_keepalived(manager, secondary['id'], {'peer_id': primary['id'], 'vip': '192.168.1.54'})
    first, second = (manager.get_agent_by_id(item['id']) for item in (primary, secondary))
    assert first['oxidns_ha']['group_id'] == second['oxidns_ha']['group_id'] == group
    assert (first['oxidns_ha']['role'], second['oxidns_ha']['role']) == ('secondary', 'primary')
    assert first['oxidns_ha']['vip'] == '192.168.1.54'


def test_old_agents_must_be_upgraded_first(pair):
    manager, devices, primary, secondary = pair
    devices.protocols[SECONDARY_HOST] = []
    with pytest.raises(oxidns_ha.HAError, match='DNS-B 的 Agent 版本不支持 DNS 高可用，请先升级 Agent'):
        enable(manager, primary, secondary)
    assert [call[0] for call in devices.calls] == ['capabilities', 'capabilities']


def test_preview_blockers_from_both_devices_are_listed_together(pair):
    manager, devices, primary, secondary = pair
    devices.blockers = {PRIMARY_HOST: ['缺少 keepalived：apt install keepalived'],
                        SECONDARY_HOST: ['OxiDNS 只监听了具体地址，无法应答虚拟 IP']}
    with pytest.raises(oxidns_ha.HAError) as caught:
        enable(manager, primary, secondary)
    assert caught.value.details['blockers'] == ['DNS-A：缺少 keepalived：apt install keepalived',
                                                'DNS-B：OxiDNS 只监听了具体地址，无法应答虚拟 IP']
    assert 'apply' not in [call[0] for call in devices.calls]


def test_vip_outside_the_detected_network_is_blocked(pair):
    manager, devices, primary, secondary = pair
    devices.cidr = '192.168.1.0/27'
    with pytest.raises(oxidns_ha.HAError, match='不在两台 OxiDNS 所在的网段'):
        enable(manager, primary, secondary)


def test_failed_primary_rolls_back_the_secondary_and_leaves_no_record(pair):
    manager, devices, primary, secondary = pair
    devices.fail_apply.add(PRIMARY_HOST)
    with pytest.raises(oxidns_ha.HAError, match='DNS-A：keepalived 启动失败'):
        enable(manager, primary, secondary)
    assert devices.kinds()[-4:] == [('apply', SECONDARY_HOST), ('apply', PRIMARY_HOST),
                                    ('remove', PRIMARY_HOST), ('remove', SECONDARY_HOST)]
    for item in (primary, secondary):
        record = manager.get_agent_by_id(item['id'])
        assert 'oxidns_ha' not in record and 'oxidns_ha_state' not in record


def test_failed_reconfiguration_restores_the_previous_pair(pair):
    manager, devices, primary, secondary = pair
    enable(manager, primary, secondary)
    before = manager.get_agent_by_id(primary['id'])['oxidns_ha']
    devices.calls.clear()
    devices.fail_apply.add((PRIMARY_HOST, '192.168.1.54'))
    with pytest.raises(oxidns_ha.HAError) as caught:
        enable(manager, primary, secondary, vip='192.168.1.54')
    assert '回滚未完成' not in str(caught.value)
    restored = [call for call in devices.calls if call[0] == 'apply'][2:]
    assert [(call[1], call[2]['vip'], call[2]['priority']) for call in restored] == [
        (PRIMARY_HOST, VIP, 150), (SECONDARY_HOST, VIP, 100)]
    # Both devices are back on the old pair, so routers keep handing out its VIP.
    record = manager.get_agent_by_id(primary['id'])
    assert record['oxidns_ha'] == before and record['oxidns_ha_state']['status'] == 'succeeded'
    assert 'keepalived 启动失败' in record['oxidns_ha_state']['error']
    peer = manager.get_agent_by_id(secondary['id'])
    assert 'dns_ha' in dns_ha_settings(primary['id'], {**record, 'status': 'online'}, peer)


def test_reconfiguration_that_cannot_be_undone_marks_the_pair_failed(pair):
    manager, devices, primary, secondary = pair
    enable(manager, primary, secondary)
    devices.fail_apply.update({PRIMARY_HOST, SECONDARY_HOST})
    with pytest.raises(oxidns_ha.HAError, match='回滚未完成'):
        enable(manager, primary, secondary, vip='192.168.1.54')
    for item in (primary, secondary):
        assert manager.get_agent_by_id(item['id'])['oxidns_ha_state']['status'] == 'failed'


def test_first_enable_that_cannot_be_undone_leaves_a_cleanup_marker(pair):
    manager, devices, primary, secondary = pair
    devices.fail_apply.add(PRIMARY_HOST)
    devices.fail_remove.add(PRIMARY_HOST)
    with pytest.raises(oxidns_ha.HAError, match='回滚未完成：DNS-A 无法连接'):
        enable(manager, primary, secondary)
    for item in (primary, secondary):
        record = manager.get_agent_by_id(item['id'])
        assert 'oxidns_ha' not in record
        assert record['oxidns_ha_state']['status'] == 'failed'
        assert record['oxidns_ha_state']['members'] == [secondary['id'], primary['id']]
    # Routers still see an Agent without DNS HA.
    assert oxidns_ha.status(manager, primary['id']) == {'success': True, 'config': None}
    # Disabling from either record reaches both devices, then restores the record shape.
    devices.calls.clear()
    with pytest.raises(oxidns_ha.HAError, match='停用未完成'):
        oxidns_ha.disable(manager, secondary['id'])
    assert sorted(devices.kinds()) == [('remove', PRIMARY_HOST), ('remove', SECONDARY_HOST)]
    assert manager.get_agent_by_id(primary['id'])['oxidns_ha_state']['members'] == [secondary['id'], primary['id']]
    devices.fail_remove.clear()
    devices.calls.clear()
    oxidns_ha.disable(manager, secondary['id'])
    assert sorted(devices.kinds()) == [('remove', PRIMARY_HOST), ('remove', SECONDARY_HOST)]
    for item in (primary, secondary):
        record = manager.get_agent_by_id(item['id'])
        assert 'oxidns_ha' not in record and 'oxidns_ha_state' not in record


def test_cleanup_marker_never_touches_a_member_in_a_new_pair(pair):
    manager, devices, primary, secondary = pair
    devices.fail_apply.add(PRIMARY_HOST)
    devices.fail_remove.add(PRIMARY_HOST)
    with pytest.raises(oxidns_ha.HAError):
        enable(manager, primary, secondary)
    devices.fail_apply.clear()
    devices.fail_remove.clear()
    third = manager.register_agent({'name': 'DNS-C', 'host': '192.168.1.22', 'service_type': 'oxidns',
                                    'deployment_method': 'shell'})
    oxidns_ha._configure_keepalived(manager, secondary['id'], {'peer_id': third['id'], 'vip': VIP})
    devices.calls.clear()
    oxidns_ha.disable(manager, primary['id'])
    assert devices.kinds() == [('remove', PRIMARY_HOST)]
    assert 'oxidns_ha_state' not in manager.get_agent_by_id(primary['id'])
    assert oxidns_ha.enabled(manager.get_agent_by_id(secondary['id']))


def test_any_save_failure_undoes_both_devices(pair, monkeypatch):
    manager, devices, primary, secondary = pair
    original = manager._update_agents
    calls = []

    def broken(mutate):
        calls.append(mutate)
        if len(calls) == 1:
            raise OSError('disk full')
        return original(mutate)
    monkeypatch.setattr(manager, '_update_agents', broken)
    with pytest.raises(oxidns_ha.HAError, match='保存 DNS 高可用记录失败'):
        enable(manager, primary, secondary)
    assert devices.kinds()[-2:] == [('remove', PRIMARY_HOST), ('remove', SECONDARY_HOST)]
    assert len(calls) == 1
    # When the undo also fails, the marker still lets disable reach the devices.
    calls.clear()
    devices.fail_remove.add(SECONDARY_HOST)
    with pytest.raises(oxidns_ha.HAError, match='回滚未完成'):
        enable(manager, primary, secondary)
    assert set(manager.get_agent_by_id(primary['id'])['oxidns_ha_state']['members']) == {primary['id'], secondary['id']}


def test_loopback_mihomo_upstream_blocks_before_any_device_call(pair):
    manager, devices, primary, secondary = pair
    manager.repository.save_profile('default', {'oxidns': {'remote_dns': 'udp://127.0.0.1:1053'}})
    with pytest.raises(oxidns_ha.HAError, match='udp://127.0.0.1:1053 是本机回环地址.*mihomo 的局域网地址'):
        enable(manager, primary, secondary)
    assert devices.calls == []
    manager.repository.save_profile('default', {'oxidns': {'remote_dns': 'udp://192.168.1.4:1053'}})
    enable(manager, primary, secondary)


def test_same_host_dns_follow_blocks_and_unreadable_drafts_warn(pair, monkeypatch):
    manager, devices, primary, secondary = pair

    def bind(agents):
        next(item for item in agents if item['id'] == secondary['id'])['dns_follow'] = {'connection': 'auto'}
    manager._update_agents(bind)
    with pytest.raises(oxidns_ha.HAError, match='DNS-B 的 DNS 联动使用同机连接'):
        enable(manager, primary, secondary)

    def unbind(agents):
        next(item for item in agents if item['id'] == secondary['id']).pop('dns_follow')
    manager._update_agents(unbind)
    monkeypatch.setattr('backend.common.oxidns_editor.prepare_editor', lambda *args: 1 / 0)
    result = enable(manager, primary, secondary)
    assert any('无法解析 OxiDNS 配置' in warning for warning in result['warnings'])


def test_shared_routeros_cleanup_is_a_warning(pair):
    manager, devices, primary, secondary = pair
    manager.repository.save_profile('default', {'oxidns': {'ros_address_list': {
        'enabled': True, 'args': {'address': 'http://192.168.1.1', 'username': 'u', 'password': 'p'}}}})
    result = enable(manager, primary, secondary)
    assert any('停止时清理' in warning for warning in result['warnings'])


def test_enabled_pair_keeps_fields_on_reregistration_and_locks_identity(pair):
    manager, devices, primary, secondary = pair
    enable(manager, primary, secondary)
    before = copy.deepcopy(manager.get_agent_by_id(primary['id']))
    manager.register_agent({'name': 'DNS-A', 'host': PRIMARY_HOST, 'service_type': 'oxidns'},
                           existing_token=primary['token'])
    after = manager.get_agent_by_id(primary['id'])
    assert (after['oxidns_ha'], after['oxidns_ha_state']) == (before['oxidns_ha'], before['oxidns_ha_state'])
    with pytest.raises(AgentDeploymentConflict, match='请先停用 DNS 高可用'):
        manager.register_agent({'name': 'DNS-A', 'host': PRIMARY_HOST, 'service_type': 'mihomo'},
                               existing_token=primary['token'])
    for change in ({'host': '192.168.1.30'}, {'port': 9090}, {'enabled': False}, {'service_type': 'mihomo'},
                   {'profile_id': 'default', 'host': '192.168.1.31'}):
        with pytest.raises(AgentDeploymentConflict, match='请先停用 DNS 高可用'):
            manager.update_agent(primary['id'], change)
    assert manager.update_agent(primary['id'], {'name': 'DNS-A2', 'host': PRIMARY_HOST, 'port': after['port']})
    with pytest.raises(AgentDeploymentConflict, match='请先停用 DNS 高可用'):
        manager.delete_agent(secondary['id'])


def test_status_reads_no_device_until_enabled(pair):
    manager, devices, primary, _ = pair
    assert oxidns_ha.status(manager, primary['id']) == {'success': True, 'config': None}
    assert devices.calls == []


@pytest.mark.parametrize(('runtimes', 'health'), [
    ((True, False), 'ok'), ((True, True), 'split'), ((False, False), 'split'), ((None, True), 'degraded'),
    ((None, False), 'degraded'), ((None, None), 'down'),
])
def test_health_follows_who_holds_the_vip(pair, runtimes, health):
    manager, devices, primary, secondary = pair
    enable(manager, primary, secondary)
    for host, holds in zip((PRIMARY_HOST, SECONDARY_HOST), runtimes):
        devices.runtime[host] = None if holds is None else {
            'keepalived_running': True, 'role': 'master' if holds else 'backup', 'vip_present': holds, 'healthy': True}
    original = devices.status

    def status(agent):
        if devices.runtime[agent['host']] is None:
            raise requests.ConnectionError('offline')
        return original(agent)
    manager.oxidns_ha_status = status
    result = oxidns_ha.status(manager, primary['id'])
    assert result['health'] == health
    unreachable = [member for member in result['members'] if member['runtime'] is None]
    assert all('无法连接' in member['error'] for member in unreachable)


def test_unhealthy_member_degrades(pair):
    manager, devices, primary, secondary = pair
    devices.runtime[PRIMARY_HOST] = {'keepalived_running': True, 'role': 'master', 'vip_present': True, 'healthy': True}
    devices.runtime[SECONDARY_HOST] = {'keepalived_running': True, 'role': 'backup', 'vip_present': False,
                                       'healthy': False}
    assert enable(manager, primary, secondary)['health'] == 'degraded'


def test_disable_removes_both_and_restores_the_record_shape(pair):
    manager, devices, primary, secondary = pair
    shapes = {item['id']: set(manager.get_agent_by_id(item['id'])) for item in (primary, secondary)}
    enable(manager, primary, secondary)
    manager.repository.save_profile('default', {'routers': {'items': [
        {'id': 'ros', 'name': 'ROS', 'type': 'routeros', 'endpoint': 'https://192.168.1.1', 'dns_agent_id': secondary['id']},
        {'id': 'other', 'name': 'Other', 'type': 'routeros', 'endpoint': 'https://192.168.1.2', 'dns_agent_id': ''}]}})
    devices.calls.clear()
    result = oxidns_ha.disable(manager, primary['id'])
    assert devices.kinds() == [('remove', SECONDARY_HOST), ('remove', PRIMARY_HOST)]
    assert result == {'success': True, 'config': None, 'warnings': [],
                      'routers': [{'id': 'ros', 'name': 'ROS', 'profile_id': 'default'}]}
    for item in (primary, secondary):
        assert set(manager.get_agent_by_id(item['id'])) - {'updated_at'} == shapes[item['id']] - {'updated_at'}
    assert oxidns_ha.disable(manager, primary['id'])['config'] is None


def test_unreachable_member_needs_force_to_clear_records(pair):
    manager, devices, primary, secondary = pair
    enable(manager, primary, secondary)
    devices.fail_remove.add(SECONDARY_HOST)
    with pytest.raises(oxidns_ha.HAError, match='DNS-B 无法连接') as caught:
        oxidns_ha.disable(manager, primary['id'])
    assert caught.value.details == {'can_force': True}
    # Nothing was removed on DNS-B, so the pair keeps serving and routers keep the VIP.
    record = manager.get_agent_by_id(secondary['id'])
    assert record['oxidns_ha']['enabled'] is True and record['oxidns_ha_state']['status'] == 'succeeded'
    assert '停用未完成' in record['oxidns_ha_state']['error']
    first = manager.get_agent_by_id(primary['id'])
    assert dns_ha_settings(primary['id'], {**first, 'status': 'online'}, record)['dns_server'] == VIP
    result = oxidns_ha.disable(manager, secondary['id'], force=True)
    assert result['warnings'] and all('oxidns_ha' not in manager.get_agent_by_id(item['id'])
                                      for item in (primary, secondary))


def ha_agents(**overrides):
    group = {'enabled': True, 'group_id': 'g1', 'vip': '10.0.0.53', 'vrid': 53}
    agent = {'id': 'oxidns', 'host': '10.0.0.2', 'service_type': 'oxidns', 'status': 'online', 'profile_id': 'default',
             'oxidns_ha': {**group, 'peer_id': 'oxidns-2', 'role': 'primary'},
             'oxidns_ha_state': {'status': 'succeeded', 'error': '', 'updated_at': 1}}
    peer = {'id': 'oxidns-2', 'host': '10.0.0.3', 'service_type': 'oxidns', 'status': 'online', 'profile_id': 'default',
            'oxidns_ha': {**group, 'peer_id': 'oxidns', 'role': 'secondary'},
            'oxidns_ha_state': {'status': 'succeeded', 'error': '', 'updated_at': 1}}
    for key, value in overrides.items():
        target, field = key.split('__')
        record = agent if target == 'agent' else peer
        if field.startswith('ha_'):
            record['oxidns_ha'][field[3:]] = value
        else:
            record[field] = value
    return agent, peer


def test_dhcp_hands_out_the_vip_while_either_member_is_online():
    agent, peer = ha_agents(agent__status='offline')
    assert dns_ha_settings('oxidns', agent, peer) == {
        'dns_agent_id': 'oxidns', 'dns_server': '10.0.0.53', 'dns_error': '',
        'dns_ha': {'vip': '10.0.0.53', 'hosts': ['10.0.0.2', '10.0.0.3'], 'peer_id': 'oxidns-2'}}


@pytest.mark.parametrize(('overrides', 'message'), [
    ({'agent__status': 'offline', 'peer__status': 'offline'}, '均离线'),
    ({'peer__ha_group_id': 'g2'}, '配对不完整'), ({'peer__ha_peer_id': 'other'}, '配对不完整'),
    ({'peer__ha_vip': '10.0.0.54'}, '配对不完整'), ({'peer__ha_enabled': False}, '配对不完整'),
    ({'peer__service_type': 'mihomo'}, '配对不完整'),
    ({'peer__oxidns_ha_state': {'status': 'failed'}}, '尚未部署成功'),
    ({'agent__ha_vip': '127.0.0.1', 'peer__ha_vip': '127.0.0.1'}, 'IPv4'), ({'peer__host': 'dns.local'}, 'IPv4'),
])
def test_dhcp_rejects_an_incomplete_pair(overrides, message):
    agent, peer = ha_agents(**overrides)
    settings = dns_ha_settings('oxidns', agent, peer)
    assert message in settings['dns_error'] and not settings['dns_server'] and 'dns_ha' not in settings
    assert 'dns_ha' not in dns_ha_settings('oxidns', agent, None)


def test_router_inspection_switches_to_the_vip(monkeypatch):
    from backend.routes import routers as api
    agent, peer = ha_agents(agent__host='192.168.0.20', peer__host='192.168.0.21',
                            agent__ha_vip='192.168.0.53', peer__ha_vip='192.168.0.53')
    agents = {item['id']: item for item in (agent, peer)}
    driver = MemoryDriver()
    monkeypatch.setattr(api, '_agent', lambda agent_id: agents.get(agent_id) if agent_id else None)
    monkeypatch.setattr(api, 'compute_desired', lambda *args, **kwargs:
                        {key: value for key, value in DHCP_DESIRED.items() if not key.startswith('dns_')})
    _, desired, state, _ = api._inspect({}, {**DHCP_ROUTER, 'dns_agent_id': 'oxidns'}, driver)
    assert (desired['dns_server'], desired['dns_ha']['hosts']) == ('192.168.0.53', ['192.168.0.20', '192.168.0.21'])
    plan = plan_for(driver, {**DHCP_ROUTER, 'dns_agent_id': 'oxidns'}, desired)
    assert plan['stages'][-1]['ops'][0]['dns'] == ['192.168.0.53']


def test_dhcp_plan_fingerprint_tracks_the_pair():
    driver = MemoryDriver()
    driver.dhcp.entries[0].dns = [DHCP_DESIRED['dns_server']]
    single = plan_for(driver)
    ha = {'vip': DHCP_DESIRED['dns_server'], 'hosts': ['192.168.0.20', '192.168.0.21'], 'peer_id': 'peer'}
    paired = plan_for(driver, DHCP_ROUTER, {**DHCP_DESIRED, 'dns_ha': ha})
    moved = plan_for(driver, DHCP_ROUTER, {**DHCP_DESIRED, 'dns_ha': {**ha, 'hosts': ['192.168.0.20', '192.168.0.22']}})
    assert single['empty'] and paired['empty'] and moved['empty']
    assert len({single['fingerprint'], paired['fingerprint'], moved['fingerprint']}) == 3


GUARD_HA = {**GUARD_DESIRED, 'dns_server': '10.0.0.10',
            'dns_ha': {'vip': '10.0.0.10', 'hosts': ['10.0.0.8', '10.0.0.9'], 'peer_id': 'peer'},
            'proxy_gateway': '10.0.0.11'}


def test_guard_targets_the_vip_and_exempts_both_real_hosts():
    _, driver = device()
    settings = router(bypass=['10.0.0.12/30', '10.0.0.64/26'])['dns_guard']
    spec = dns_guard.desired_spec(settings, GUARD_HA, driver.read_state())
    assert spec == {'agent': '10.0.0.10', 'interface': 'bridge', 'network': '10.0.0.0/24',
                    'bypass': ['10.0.0.8/29', '10.0.0.64/26'], 'block_dot': True, 'failover': True,
                    'exempt': ['10.0.0.10', '10.0.0.8', '10.0.0.9', '10.0.0.11',
                               '10.0.0.12', '10.0.0.13', '10.0.0.14', '10.0.0.15']}
    items = {item['role']: item['values'] for item in dns_guard.resources(spec)}
    assert items['hijack-udp']['to-addresses'] == '10.0.0.10' and items['hijack-udp']['dst-address'] == '!10.0.0.10'
    assert items['hairpin-tcp']['dst-address'] == '10.0.0.10' and items['netwatch']['host'] == '10.0.0.10'
    # The /29 is too wide for the collapsed-list expansion; exempt still names every host.
    for host in ('10.0.0.8', '10.0.0.9', '10.0.0.11', '10.0.0.15'):
        assert f'src-address!={host}' in items['netwatch']['up-script']
    assert 'reply-src-address=10.0.0.10' in items['netwatch']['down-script']
    assert '10.0.0.64' not in items['netwatch']['up-script']


@pytest.mark.parametrize(('change', 'message'), [
    ({'hosts': ['10.0.0.8', '172.16.0.9']}, '172.16.0.9 必须与虚拟 IP 10.0.0.10 在同一直连网段'),
    ({'vip': '172.16.0.10'}, '直连网段'),
])
def test_guard_requires_the_pair_on_the_vip_network(change, message):
    _, driver = device()
    desired = {**GUARD_HA, 'dns_ha': {**GUARD_HA['dns_ha'], **change}}
    desired['dns_server'] = desired['dns_ha']['vip']
    _, plan = preview(driver, router(), desired)
    assert any(message in blocker for blocker in plan['blockers'])


@pytest.mark.parametrize('cidr', ['10.0.0.9/32', '10.0.0.10/32'])
def test_guard_checks_every_pair_address_against_proxy_networks(cidr):
    _, driver = device()
    desired = {**GUARD_HA, 'proxy_cidrs': [*GUARD_HA['proxy_cidrs'], {'cidr': cidr, 'sources': ['rule']}]}
    with pytest.raises(dns_guard.RouterError, match=cidr.split('/')[0] + ' 落在代理目的网段内'):
        dns_guard.desired_spec(router()['dns_guard'], desired, driver.read_state())


def test_guard_switches_to_the_pair_through_one_replacement():
    session, driver = device()
    _, single = publish(driver, router())
    desired = {**GUARD_HA, 'proxy_gateway': GUARD_DESIRED['proxy_gateway']}
    _, plan = preview(driver, single, desired)
    [op] = plan['stages'][-1]['ops']
    assert op['kind'] == 'dns_guard_replace' and op['spec']['exempt'] and not plan['blockers']
    assert execute(plan, driver, None)['ok']
    paired = {**single, 'dns_guard_state': driver.dns_guard_manifest}
    assert paired['dns_guard_state']['spec']['agent'] == '10.0.0.10'
    assert session.rows[NETWATCH][0]['host'] == '10.0.0.10'
    assert any(row.get('to-addresses') == '10.0.0.10' for row in session.rows[NAT])
    report, plan = preview(driver, paired, desired)
    assert report['dns_guard']['status'] == 'synced' and plan['empty']


def test_rule_sync_digest_changes_only_with_the_pair(monkeypatch):
    from backend.routers import rule_sync
    agent, peer = ha_agents()
    agents = {item['id']: item for item in (agent, peer)}
    monkeypatch.setattr('backend.routes.routers._agent', lambda agent_id: agents.get(agent_id) if agent_id else None)
    monkeypatch.setattr(rule_sync, 'selected_sources', lambda config, item: {})
    item = {'id': 'ros', 'type': 'routeros', 'dns_agent_id': 'oxidns'}
    paired = rule_sync.controls({}, item)
    peer['host'] = '10.0.0.4'
    moved = rule_sync.controls({}, item)
    agent.pop('oxidns_ha')
    single = rule_sync.controls({}, item)
    peer.pop('oxidns_ha')
    assert len({paired, moved, single}) == 3
    assert single == rule_sync.controls({}, item)


class Response:
    def __init__(self, status, body):
        self.status_code, self.body = status, body

    def json(self):
        if self.body is None:
            raise ValueError('no json')
        return self.body


@pytest.mark.parametrize(('status', 'body', 'expected'), [
    (404, None, 'Agent DNS 不支持 DNS 高可用，请升级'), (422, {'error': '缺少 keepalived'}, '缺少 keepalived'),
    (409, {'success': False, 'message': '无法证明归属'}, '无法证明归属'), (500, None, 'HTTP 500'),
    (200, {'success': False, 'message': 'busy'}, 'busy'),
])
def test_agent_errors_become_messages(tmp_path, monkeypatch, status, body, expected):
    manager = AgentManager(ProfileRepository(tmp_path))
    calls = []
    monkeypatch.setattr(requests, 'delete', lambda url, **kwargs: calls.append((url, kwargs)) or Response(status, body))
    with pytest.raises(RuntimeError, match=expected):
        manager.oxidns_ha_remove({'id': 'a', 'name': 'DNS', 'host': '192.168.1.20', 'port': 8080, 'token': 't'})
    assert calls[0][0] == 'http://192.168.1.20:8080/api/oxidns-ha'
    assert calls[0][1]['headers'] == {'Authorization': 'Bearer t'}


def test_agent_status_without_a_success_field_is_returned(tmp_path, monkeypatch):
    manager = AgentManager(ProfileRepository(tmp_path))
    body = {'protocol': 1, 'managed': None, 'runtime': {'vip_present': False}, 'error': ''}
    monkeypatch.setattr(requests, 'post', lambda url, **kwargs: Response(200, body))
    agent = {'id': 'a', 'host': '192.168.1.20', 'port': 8080, 'token': 't'}
    assert manager.oxidns_ha_preview(agent, {'vip': VIP}) == body


def test_http_api(authenticated_app, monkeypatch):  # noqa: F811
    from backend.agents import oxidns_ha_publish
    monkeypatch.setattr(oxidns_ha_publish, 'start_loop', lambda _: None)
    client, manager, _, admin, _ = authenticated_app
    devices = Devices()
    monkeypatch.setattr(AgentManager, '_deployment_request', lambda _self, *args, **kwargs: devices.capabilities(*args, **kwargs))
    for name in ('status', 'preview', 'apply', 'remove'):
        monkeypatch.setattr(AgentManager, f'oxidns_ha_{name}',
                            lambda _self, *args, name=name: getattr(devices, name)(*args))
    primary = manager.register_agent({'name': 'DNS-A', 'host': PRIMARY_HOST, 'service_type': 'oxidns'})
    secondary = manager.register_agent({'name': 'DNS-B', 'host': SECONDARY_HOST, 'service_type': 'oxidns'})
    path = f'/api/agents/{primary["id"]}/oxidns-ha'
    assert client.get(path).status_code == 401
    assert client.get(path, headers=admin).get_json() == {'success': True, 'config': None}
    devices.blockers[SECONDARY_HOST] = ['缺少 keepalived']
    rejected = client.put(path, headers=admin, json={'peer_id': secondary['id'], 'vip': VIP})
    assert rejected.status_code == 409 and rejected.get_json()['blockers'] == ['DNS-B：缺少 keepalived']
    devices.blockers.clear()
    response = client.put(path, headers=admin, json={'peer_id': secondary['id'], 'vip': VIP})
    started = response.get_json()
    assert response.status_code == 202
    assert started['config'] is None and started['publication']['status'] == 'preparing'
    assert not any(call[0] == 'apply' for call in devices.calls)
    # HTTP start is asynchronous. The dedicated publication tests drive both
    # real transaction journals; finish this reservation before testing HA CRUD.
    job = manager.get_agent_by_id(primary['id'])[oxidns_ha_publish.FIELD]
    oxidns_ha_publish._fail(manager, job, 'test reservation finished')
    enabled = enable(manager, primary, secondary)
    listed = next(item for item in client.get('/api/agents', headers=admin).get_json() if item['id'] == primary['id'])
    assert listed['oxidns_ha'] == enabled['config'] and 'token' not in listed
    blocked = client.delete(f'/api/agents/{secondary["id"]}', headers=admin)
    assert blocked.status_code == 409 and '请先停用 DNS 高可用' in blocked.get_json()['message']
    devices.fail_remove.add(SECONDARY_HOST)
    refused = client.delete(path, headers=admin)
    assert refused.status_code == 409 and refused.get_json()['can_force'] is True
    cleared = client.delete(path + '?force=1', headers=admin)
    assert cleared.status_code == 200 and cleared.get_json()['config'] is None
    assert 'oxidns_ha' not in manager.get_agent_by_id(secondary['id'])
