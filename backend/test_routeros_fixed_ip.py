"""Reservation safety: VLAN scope, explicit target, ownership and compensation."""
from copy import deepcopy
from urllib.parse import unquote, urlsplit

import pytest
import requests

from backend.routers import routeros_fixed_ip as fixed
from backend.routers.base import RouterError, RouterRollbackError
from backend.routers.routeros import DHCP_LEASES, DHCP_NETWORKS, DHCP_SERVERS, RouterOSDriver

IP = '192.168.95.50'
MAC = '02:00:00:95:00:50'
LEASE = {'.id': '*A', 'address': IP, 'mac-address': MAC, 'server': 'vlan-dhcp',
         'dynamic': 'true', 'disabled': 'false', 'status': 'bound', 'last-seen': '1s',
         'client-id': '1:2:0:0:95:0:50', 'comment': ''}


class Response:
    def __init__(self, data=None, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return deepcopy(self.data)


class REST:
    def __init__(self):
        self.tables = {
            DHCP_SERVERS: [{'.id': '*1', 'name': 'vlan-dhcp', 'interface': 'vlan95',
                            'disabled': 'false', 'invalid': 'false'}],
            DHCP_NETWORKS: [{'.id': '*2', 'address': '192.168.95.0/24',
                             'gateway': '192.168.95.1', 'dns-server': '192.168.95.1'}],
            '/ip/address': [{'.id': '*3', 'address': '192.168.95.1/24', 'interface': 'vlan95'}],
            DHCP_LEASES: [deepcopy(LEASE)],
        }
        self.writes = []
        self.fail = None
        self.fired = False
        self.next_id = 32

    @staticmethod
    def path(url):
        return unquote(urlsplit(url).path).removeprefix('/rest')

    def get(self, url, **kwargs):
        path = self.path(url)
        if path in self.tables:
            return Response(self.tables[path])
        assert path.startswith(DHCP_LEASES + '/')
        if self.fail == 'readback' and self.writes and not self.fired:
            self.fired = True
            raise requests.Timeout('injected readback fault')
        row = next((item for item in self.tables[DHCP_LEASES] if path.endswith('/' + item['.id'])), None)
        return Response(row, 200 if row else 404)

    def post(self, url, json, **kwargs):
        assert self.path(url) == DHCP_LEASES + '/make-static'
        assert set(json) == {'.id'}
        if self.fail == 'permission':
            return Response({'message': 'failure', 'detail': 'not enough permissions (9)'}, 403)
        self.writes.append(('POST', self.path(url), deepcopy(json)))
        row = next(row for row in self.tables[DHCP_LEASES] if row['.id'] == json['.id'])
        row['dynamic'] = 'false'
        if self.fail == 'lost_convert':
            raise requests.Timeout('reply lost after conversion')
        return Response([])

    def patch(self, url, json, **kwargs):
        assert set(json) == {'comment'}, 'Never write dynamic or independent DNS'
        row = next(row for row in self.tables[DHCP_LEASES] if self.path(url).endswith('/' + row['.id']))
        self.writes.append(('PATCH', self.path(url), deepcopy(json)))
        if self.fail == 'patch':
            return Response({'message': 'injected failure'}, 400)
        row.update(json)
        if self.fail == 'external_change':
            row['dhcp-option'] = 'custom-dns'
            raise requests.Timeout('another writer changed lease during failure')
        if self.fail == 'lost_patch':
            raise requests.Timeout('reply lost after comment')
        return Response(row)

    def put(self, url, json, **kwargs):
        assert self.path(url) == DHCP_LEASES
        assert 'dns-server' not in json and 'dynamic' not in json
        self.writes.append(('PUT', self.path(url), deepcopy(json)))
        self.next_id += 1
        row = {'.id': f'*{self.next_id:X}', 'dynamic': 'false', **json}
        self.tables[DHCP_LEASES].append(row)
        if self.fail == 'lost_add':
            raise requests.Timeout('reply lost after creation')
        if self.fail == 'wrong_add_id':
            return Response({'.id': '*A'})
        return Response(row)

    def delete(self, url, **kwargs):
        self.writes.append(('DELETE', self.path(url), None))
        self.tables[DHCP_LEASES][:] = [row for row in self.tables[DHCP_LEASES]
                                     if not self.path(url).endswith('/' + row['.id'])]
        if self.fail == 'lost_delete':
            raise requests.Timeout('delete reply lost')
        return Response({}, 204)

    def close(self):
        pass


def setup(dynamic=True):
    driver = RouterOSDriver('http://router.invalid', 'test', 'not-a-real-password')
    rest = REST()
    driver.session = rest
    if not dynamic:
        rest.tables[DHCP_LEASES] = []
    return driver, rest


def test_vlan_dynamic_conversion_preserves_id_server_and_client_identifier():
    driver, rest = setup()
    before = deepcopy(rest.tables)
    state = fixed.inspect(fixed.read(driver), IP, MAC.lower())
    assert state['eligible'] and state['dynamic'] and state['scope']['interface'] == 'vlan95'
    op = fixed.preview(driver, IP, MAC, '电脑')
    assert not rest.writes
    binding = fixed.apply(driver, op)
    assert binding['id'] == '*A' and binding['comment'].startswith('cf-client:电脑:')
    assert [(method, payload) for method, _, payload in rest.writes] == [
        ('POST', {'.id': '*A'}), ('PATCH', {'comment': binding['comment']})]
    row = rest.tables[DHCP_LEASES][0]
    assert row['client-id'] == LEASE['client-id'] and row['server'] == LEASE['server']
    assert rest.tables[DHCP_NETWORKS] == before[DHCP_NETWORKS]
    assert rest.tables[DHCP_SERVERS] == before[DHCP_SERVERS]
    fixed.rollback(driver, binding)
    assert rest.tables[DHCP_LEASES] == []  # next DHCP request can allocate dynamically


def test_new_reservation_uses_only_scoped_server_and_inherits_network_dns():
    driver, rest = setup(False)
    binding = fixed.apply(driver, fixed.preview(driver, IP, MAC, '电脑'))
    assert rest.writes[0] == ('PUT', DHCP_LEASES, {
        'address': IP, 'mac-address': MAC, 'server': 'vlan-dhcp',
        'comment': binding['comment'], 'disabled': 'false'})
    assert fixed.inspect(fixed.read(driver), IP, MAC)['fixed']
    assert fixed.inspect(fixed.read(driver), IP, MAC)['owned']
    fixed.delete(driver, fixed.preview_delete(driver, binding))
    assert rest.tables[DHCP_LEASES] == []


@pytest.mark.parametrize(('change', 'message'), [
    (lambda rest: rest.tables[DHCP_SERVERS][0].update(disabled='true'), 'DHCP 网段'),
    (lambda rest: rest.tables[DHCP_SERVERS][0].update(invalid='true'), 'DHCP 网段'),
    (lambda rest: rest.tables[DHCP_SERVERS][0].update(relay='192.168.95.2'), 'DHCP 网段'),
    (lambda rest: rest.tables[DHCP_NETWORKS].clear(), 'DHCP 网段'),
    (lambda rest: rest.tables[DHCP_LEASES][0].update(dynamic='false'), '已有静态租约'),
    (lambda rest: rest.tables[DHCP_LEASES][0].update(dynamic='false', address='192.168.95.60'), '已有静态租约'),
    (lambda rest: rest.tables[DHCP_LEASES][0].update(dynamic='false', **{'mac-address': '02:00:00:00:00:99'}), '其他 MAC'),
    (lambda rest: rest.tables[DHCP_LEASES][0].update(address='192.168.95.60'), '已变化'),
    (lambda rest: rest.tables[DHCP_LEASES][0].update(radius='true'), 'RADIUS'),
])
def test_conflicts_never_write(change, message):
    driver, rest = setup()
    change(rest)
    with pytest.raises(RouterError, match=message):
        fixed.preview(driver, IP, MAC, '电脑')
    assert rest.writes == []


@pytest.mark.parametrize('mac', ['', 'not-a-mac', 'FF:FF:FF:FF:FF:FF', '00:00:00:00:00:00'])
def test_missing_or_nonclient_mac_cannot_be_reserved(mac):
    driver, rest = setup()
    assert not fixed.inspect(fixed.read(driver), IP, mac)['eligible']
    assert not rest.writes


def test_outside_subnet_and_router_address_cannot_be_reserved():
    driver, rest = setup(False)
    assert 'DHCP 网段' in fixed.inspect(fixed.read(driver), '192.168.96.50', MAC)['reason']
    assert '路由器接口地址' in fixed.inspect(fixed.read(driver), '192.168.95.1', MAC)['reason']


def test_no_arbitrary_selection_among_multiple_servers():
    driver, rest = setup(False)
    rest.tables[DHCP_SERVERS].append({'.id': '*4', 'name': 'other', 'interface': 'other-vlan'})
    rest.tables['/ip/address'].append({'.id': '*5', 'address': '192.168.95.2/24', 'interface': 'other-vlan'})
    assert '多个 DHCP 服务' in fixed.inspect(fixed.read(driver), IP, MAC)['reason']


def test_expired_preview_rejects_change_but_allows_renewal_counters():
    driver, rest = setup()
    op = fixed.preview(driver, IP, MAC, '电脑')
    rest.tables[DHCP_LEASES][0].update({'last-seen': '15s', 'expires-after': '25m'})
    assert fixed.preview(driver, IP, MAC, '电脑') == op
    rest.tables[DHCP_LEASES][0]['comment'] = 'manual edit'
    with pytest.raises(RouterError, match='预览后发生变化'):
        fixed.apply(driver, op)
    assert not rest.writes


@pytest.mark.parametrize('failure', ['patch', 'lost_convert', 'lost_patch', 'readback'])
def test_failed_conversion_removes_only_its_static_reservation(failure):
    driver, rest = setup()
    other = {'.id': '*F', 'address': '192.168.95.70', 'mac-address': '02:00:00:95:00:70',
             'server': 'vlan-dhcp', 'dynamic': 'false', 'comment': 'manual'}
    rest.tables[DHCP_LEASES].append(deepcopy(other))
    op = fixed.preview(driver, IP, MAC, '电脑')
    rest.fail = failure
    with pytest.raises(RouterError):
        fixed.apply(driver, op)
    assert rest.tables[DHCP_LEASES] == [other]
    assert rest.writes[-1] == ('DELETE', DHCP_LEASES + '/*A', None)


def test_rollback_keeps_external_custom_dns_change_and_reports_failure():
    driver, rest = setup()
    op = fixed.preview(driver, IP, MAC, '电脑')
    rest.fail = 'external_change'
    with pytest.raises(RouterRollbackError, match='外部变化'):
        fixed.apply(driver, op)
    assert all(method != 'DELETE' for method, _, _ in rest.writes)
    assert rest.tables[DHCP_LEASES][0]['dhcp-option'] == 'custom-dns'


def test_permission_denial_explains_write_policy_and_performs_no_compensation():
    driver, rest = setup()
    op = fixed.preview(driver, IP, MAC, '电脑')
    rest.fail = 'permission'
    with pytest.raises(RouterError, match='read、write、api、rest-api'):
        fixed.apply(driver, op)
    assert not rest.writes
    assert rest.tables[DHCP_LEASES][0]['dynamic'] == 'true'


def test_unique_operation_marker_recovers_lost_add_reply_without_deleting_foreign_id():
    for failure in ('wrong_add_id', 'lost_add'):
        driver, rest = setup(False)
        rest.tables[DHCP_LEASES].append({'.id': '*A', 'address': '192.168.95.70',
                                       'mac-address': '02:00:00:95:00:70', 'dynamic': 'false'})
        op = fixed.preview(driver, IP, MAC, '电脑')
        rest.fail = failure
        with pytest.raises(RouterError):
            fixed.apply(driver, op)
        assert len(rest.tables[DHCP_LEASES]) == 1 and rest.tables[DHCP_LEASES][0]['.id'] == '*A'
        assert rest.writes[-1][0] == 'DELETE' and rest.writes[-1][1] != DHCP_LEASES + '/*A'


def test_delete_requires_prefix_exact_mac_ip_server_and_unchanged_preview():
    driver, rest = setup()
    binding = fixed.apply(driver, fixed.preview(driver, IP, MAC, '电脑'))
    for key, value in [('comment', 'manual'), ('ip', '192.168.95.99'),
                       ('mac', '02:00:00:95:00:99'), ('server', 'another-server')]:
        with pytest.raises(RouterError):
            fixed.preview_delete(driver, {**binding, key: value})
    op = fixed.preview_delete(driver, binding)
    rest.tables[DHCP_LEASES][0]['lease-time'] = '10m'
    with pytest.raises(RouterError, match='预览后发生变化'):
        fixed.delete(driver, op)
    assert all(method != 'DELETE' for method, _, _ in rest.writes)


def test_delete_receipt_restores_exact_settings_and_reports_new_id():
    driver, rest = setup()
    binding = fixed.apply(driver, fixed.preview(driver, IP, MAC, '电脑'))
    original = fixed._fields(rest.tables[DHCP_LEASES][0])
    receipt = fixed.delete(driver, fixed.preview_delete(driver, binding))
    restored = fixed.restore_deleted(driver, receipt)
    assert restored['id'] != binding['id']
    assert fixed._fields(rest.tables[DHCP_LEASES][0]) == original


def test_delete_reply_loss_is_resolved_by_id_readback():
    driver, rest = setup()
    binding = fixed.apply(driver, fixed.preview(driver, IP, MAC, '电脑'))
    op = fixed.preview_delete(driver, binding)
    rest.fail = 'lost_delete'
    fixed.delete(driver, op)
    assert not rest.tables[DHCP_LEASES]


def test_restore_deleted_never_overwrites_new_lease():
    driver, rest = setup()
    binding = fixed.apply(driver, fixed.preview(driver, IP, MAC, '电脑'))
    receipt = fixed.delete(driver, fixed.preview_delete(driver, binding))
    rest.tables[DHCP_LEASES].append({**LEASE, '.id': '*99'})
    before = len(rest.writes)
    with pytest.raises(RouterError, match='其他租约'):
        fixed.restore_deleted(driver, receipt)
    assert len(rest.writes) == before


def test_disabled_owned_reservation_is_not_presented_as_fixed_but_can_be_deleted():
    driver, rest = setup()
    rest.tables[DHCP_LEASES][0].update(dynamic='false', disabled='true', comment='cf-client:old')
    state = fixed.inspect(fixed.read(driver), IP, MAC)
    assert not state['fixed'] and not state['eligible'] and state['owned']
    assert state['binding']['id'] == '*A'
    fixed.preview_delete(driver, state['binding'])


def test_dynamic_evidence_survives_ineligible_scope():
    driver, rest = setup()
    rest.tables[DHCP_SERVERS][0]['disabled'] = 'true'
    state = fixed.inspect(fixed.read(driver), IP, MAC)
    assert state['dynamic'] and not state['eligible'] and 'DHCP 网段' in state['reason']


def test_failed_batch_deletion_can_restore_an_owned_disabled_lease():
    driver, rest = setup()
    rest.tables[DHCP_LEASES][0].update(dynamic='false', disabled='true', comment='cf-client:old')
    original = fixed._fields(rest.tables[DHCP_LEASES][0])
    binding = fixed.inspect(fixed.read(driver), IP, MAC)['binding']
    receipt = fixed.delete(driver, fixed.preview_delete(driver, binding))
    restored = fixed.restore_deleted(driver, receipt)
    assert restored['id'] != binding['id']
    assert fixed._fields(rest.tables[DHCP_LEASES][0]) == original


def test_lost_readback_after_new_reservation_compensates_its_proven_id():
    driver, rest = setup(False)
    op = fixed.preview(driver, IP, MAC, '电脑')
    rest.fail = 'readback'
    with pytest.raises(RouterError):
        fixed.apply(driver, op)
    assert rest.tables[DHCP_LEASES] == []
    assert [method for method, _, _ in rest.writes] == ['PUT', 'DELETE']
