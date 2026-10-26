"""Replay real iKuai 4.0.314 API shapes, then inject failures at mutation boundaries."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.routers import ikuai_fixed_ip as fixed
from backend.routers.base import RouterConflictError, RouterError, RouterRollbackError
from backend.routers.ikuai import IKuaiDriver, decode_value, encode_value

CAPTURE = json.loads((Path(__file__).parent / 'testdata/ikuai_fixed_ip_40314.json').read_text())['requests']
SERVER = CAPTURE[2]['response']['results']['data'][0]
LEASE = CAPTURE[3]['response']['results']['data'][0]
STATIC = CAPTURE[8]['response']['results']['static_data'][0]
IP, MAC = LEASE['ip_addr'], LEASE['mac'].upper()


class Device:
    def __init__(self, static=()):
        self.servers, self.leases, self.static = [deepcopy(SERVER)], [deepcopy(LEASE)], deepcopy(list(static))
        self.calls, self.mutations = [], []
        self.next_id, self.fault, self.after_reads = 2, '', 0
        self.denied = None
        self.hostname = ''
        self.driver = IKuaiDriver('https://192.0.2.1', 'test', 'test')
        self.driver._post = self.post

    def post(self, path, payload):
        if path == '/Action/login':
            return {'Result': 10000, 'code': 0}
        self.calls.append(deepcopy(payload))
        func, action, param = payload['func_name'], payload['action'], decode_value(payload['param'])
        if self.denied and action != 'show':
            return {'Result': 10000, 'code': self.denied, 'message': 'Access denied', 'Label': 'DHCP静态分配'}
        if action == 'show':
            if func == 'dhcp_server':
                rows, prefix = self.servers, ''
            elif func == 'dhcp_lease':
                rows, prefix = self.leases, ''
            elif func == 'dhcp_static':
                assert param['TYPE'] == 'static_total,static_data'
                rows, prefix = self.static, 'static_'
                if self.mutations and self.mutations[-1][0] == 'add':
                    self.after_reads += 1
                    if self.fault == 'readback' and self.after_reads == 2:
                        self.fault = ''
                        raise RouterError('injected readback failure')
            else:
                raise AssertionError(func)
            offset, limit = map(int, param['limit'].split(','))
            return {'code': 0, 'results': {prefix + 'total': len(rows),
                                         prefix + 'data': encode_value(deepcopy(rows[offset:offset+limit]))}}
        assert func == 'dhcp_static'
        self.mutations.append((action, deepcopy(param)))
        if action == 'add':
            assert param['dns1'] == param['dns2'] == ''
            assert 1 <= len(param['tagname']) <= 15
            self.static.append({**param, 'hostname': self.hostname, 'id': self.next_id, 'termname': '', 'ip_addr_int': 3232255332})
            self.next_id += 1
            self.after_reads = 0
            if self.fault == 'lost_add':
                self.fault = ''
                raise RouterError('injected lost add response')
            return {'code': 0, 'message': 'Success', 'rowid': self.next_id - 1}
        if action == 'del':
            assert isinstance(param['id'], str)
            self.static = [row for row in self.static if str(row['id']) != param['id']]
            if self.fault == 'lost_delete':
                self.fault = ''
                raise RouterError('injected lost delete response')
            return {'code': 0, 'message': 'Success'}
        raise AssertionError(action)


def plan(device):
    return fixed.preview(device.driver, IP, MAC, '测试电脑')


def test_real_capture_proves_empty_dns_and_static_view():
    assert CAPTURE[7]['response'] == {'code': 0, 'message': 'Success', 'rowid': 1}
    assert STATIC['dns1'] == STATIC['dns2'] == ''
    assert 'dns1' not in CAPTURE[9]['response']['results']['data'][0]
    assert CAPTURE[11]['request']['param'] == {'id': '1'}
    assert CAPTURE[12]['response']['results']['static_total'] == 0


def test_add_verify_delete_and_restore_with_inherited_dns():
    device = Device()
    op = plan(device)
    assert not device.mutations
    assert op['scope']['interface'] == 'lan1' and op['scope']['gateway'] == '192.168.77.1'
    binding = fixed.apply(device.driver, op)
    assert binding['mac'] == MAC and binding['id'] == '2'
    payload = device.mutations[0][1]
    assert payload['dns1'] == payload['dns2'] == '' and payload['comment'] == 'cf-client:测试电脑'
    state = fixed.inspect(fixed.read(device.driver), IP, MAC)
    assert state['fixed'] and state['owned'] and not state['eligible'] and state['binding']['id'] == '2'
    # The existing DHCP DNS feature must remain usable with this new allocation.
    assert not device.driver.read_dhcp_dns().entries[0].blockers
    deletion = fixed.preview_delete(device.driver, binding)
    receipt = fixed.delete(device.driver, deletion)
    assert device.static == []
    restored = fixed.restore_deleted(device.driver, receipt)
    assert restored['id'] == '3' and device.static[0]['dns1'] == device.static[0]['dns2'] == ''


@pytest.mark.parametrize('row,reason', [
    ({'mac': '02:00:00:00:00:99'}, '其他 MAC'),
    ({'ip_addr': '192.168.77.101'}, '已有静态'),
    ({'enabled': 'no'}, '已有静态'),
])
def test_conflicts_never_overwrite(row, reason):
    device = Device([{**STATIC, **row}])
    with pytest.raises(RouterError, match=reason):
        plan(device)
    assert not device.mutations


@pytest.mark.parametrize('server', [
    {'enabled': 'no'}, {'status': 0}, {'addr_pool': '192.168.78.10-192.168.78.100'},
    {'check_relay_only': 1}, {'gateway': IP}, {'netmask': 'invalid'},
])
def test_only_active_unambiguous_networks(server):
    device = Device()
    device.servers[0].update(server)
    with pytest.raises(RouterError, match='DHCP 网段'):
        plan(device)
    assert not device.mutations


def test_overlapping_servers_refuse_arbitrary_interface_selection():
    device = Device()
    device.servers.append({**SERVER, 'id': 2, 'interface': 'lan2'})
    with pytest.raises(RouterError, match='多个 DHCP'):
        plan(device)


@pytest.mark.parametrize('fault', ['lost_add', 'readback'])
def test_partial_add_failure_deletes_only_new_unique_tag(fault):
    foreign = {**STATIC, 'id': 1, 'ip_addr': '192.168.77.120', 'mac': '02:00:00:00:00:99', 'comment': 'manual'}
    device = Device([foreign])
    op = plan(device)
    device.fault = fault
    with pytest.raises(RouterError, match='injected'):
        fixed.apply(device.driver, op)
    assert device.static == [foreign]
    assert [item[0] for item in device.mutations] == ['add', 'del']


def test_successful_add_can_be_rolled_back_after_database_failure():
    device = Device()
    binding = fixed.apply(device.driver, plan(device))
    fixed.rollback(device.driver, binding)
    assert device.static == []


def test_lost_delete_reply_is_reconciled_by_readback():
    device = Device()
    binding = fixed.apply(device.driver, plan(device))
    op = fixed.preview_delete(device.driver, binding)
    device.fault = 'lost_delete'
    fixed.delete(device.driver, op)
    assert device.static == []


def test_foreign_or_modified_binding_is_never_deleted():
    device = Device([STATIC])
    binding = fixed.inspect(fixed.read(device.driver), IP, MAC)['binding']
    op = fixed.preview_delete(device.driver, binding)
    device.static[0]['dns1'] = '192.168.77.9'
    with pytest.raises(RouterError, match='变化'):
        fixed.delete(device.driver, op)
    device.static[0]['comment'] = 'manual'
    with pytest.raises(RouterError, match='不能删除'):
        fixed.preview_delete(device.driver, binding)
    assert not device.mutations


@pytest.mark.parametrize('code,reason', [(1001, '读取权限'), (1002, '修改权限'), (1003, '不提供'), (1008, '会话已失效')])
def test_explicit_permission_unsupported_and_session_messages(code, reason):
    device = Device()
    op = plan(device)
    device.denied = code
    with pytest.raises(RouterError, match=reason):
        fixed.apply(device.driver, op)
    assert device.static == [] and not device.mutations


def test_network_changed_after_preview_refuses_before_write():
    device = Device()
    op = plan(device)
    device.servers[0]['gateway'] = '192.168.77.254'
    with pytest.raises(RouterError, match='预览后'):
        fixed.apply(device.driver, op)
    assert not device.mutations


def test_truncated_static_list_fails_closed():
    device = Device()
    original = device.post
    def incomplete(path, body):
        if body.get('func_name') == 'dhcp_static':
            return {'code': 0, 'results': {'static_total': 1001, 'static_data': []}}
        return original(path, body)
    device.driver._post = incomplete
    with pytest.raises(RouterError, match='不完整'):
        plan(device)
    assert not device.mutations


def test_missing_mac_and_disabled_owned_binding_display():
    device = Device([{**STATIC, 'enabled': 'no'}])
    snapshot = fixed.read(device.driver)
    state = fixed.inspect(snapshot, IP, MAC)
    assert state['owned'] and not state['fixed'] and not state['eligible']
    assert 'MAC' in fixed.inspect(snapshot, IP, '')['reason']


def test_dhcp_hostname_is_runtime_and_never_blocks_verification_or_rollback():
    device = Device()
    device.hostname = 'cf-fixed-client'
    binding = fixed.apply(device.driver, plan(device))
    assert device.static[0]['hostname'] == 'cf-fixed-client'
    device.static[0]['hostname'] = 'renamed-by-client'
    fixed.rollback(device.driver, binding)
    assert device.static == []
    device.fault = 'lost_add'
    with pytest.raises(RouterError, match='injected'):
        fixed.apply(device.driver, plan(device))
    assert device.static == []
