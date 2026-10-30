"""Automatic rule sync applies IPv6 rule-set destinations like IPv4, never the rest."""
import copy
from urllib.parse import unquote

import pytest
import yaml

from backend.routeros_test_helpers import print_rows
from backend.routers import rule_sync, rule_sync_store as store
from backend.test_ros_policy import GATEWAY, MemoryROS, ROUTES, _query_match
from backend.test_router_rule_sync import environment, publish  # noqa: F401

IPV6 = '/ipv6/route'
OLD = 'IP-CIDR,1.1.1.0/24\nIP-CIDR6,2001:b28:f23d::/48\nIP-CIDR6,2001:b28:f23f::/48\nDOMAIN-SUFFIX,example.com'
NEW = 'IP-CIDR,8.8.8.0/24\nIP-CIDR6,2001:b28:f23d::/48\nIP-CIDR6,2001:67c:4e8::/48\nDOMAIN-SUFFIX,example.com'
HOST = {'interface': 'eth0', 'addresses': [{'address': 'fe80::3/64', 'interface': 'eth0'}]}
GW = 'fe80::3%bridge'


class MemoryROS6(MemoryROS):
    def __init__(self):
        super().__init__()
        self.rows[IPV6] = []
        self.rows['/ipv6/address'] = [{'.id': '*A1', 'address': 'fdda::1/64', 'interface': 'bridge'}]
        self.rows['/ip/address'] = [{'.id': '*B1', 'address': '192.168.0.1/24', 'interface': 'bridge',
                                     'actual-interface': 'bridge'}]

    def _request(self, method, path, values=None):
        bare, _, query = unquote(path).partition('?')
        if bare == IPV6 + '/print':
            return copy.deepcopy(print_rows(self.rows[IPV6], values))
        if bare == IPV6 + '/remove':
            return self._remove(values, IPV6)
        if bare == IPV6 and query:
            key, _, value = query.partition('=')
            return [copy.deepcopy(row) for row in self.rows[IPV6] if _query_match(row, key, value)]
        result = super()._request(method, path, values)
        if method == 'PUT' and bare == IPV6:
            row = next(row for row in self.rows[IPV6] if row['.id'] == result['.id'])
            row.update({'active': 'true', 'dynamic': 'false', 'immediate-gw': row['gateway']})
            return copy.deepcopy(row)
        return result


@pytest.fixture
def ipv6_environment(environment, monkeypatch):  # noqa: F811
    repository, _ = environment
    driver = MemoryROS6()
    monkeypatch.setattr('backend.routes.routers._driver', lambda router: driver)
    monkeypatch.setattr('backend.routes.routers._agent', lambda agent_id: {
        'id': 'gw', 'host': GATEWAY, 'service_type': 'mihomo', 'profile_id': 'default', 'enabled': True}
        if agent_id else None)
    host = copy.deepcopy(HOST)
    monkeypatch.setattr('backend.routes.routers._ipv6_host', lambda config, agent: host)

    def seed(config):
        config['mihomo']['custom_config'] = yaml.safe_dump({'ipv6': True, 'tun': {'enable': True}})
        config['routers']['items'][0].update(ros_policy_rule_sets=[], ip_rule_sets=['remote'])
    repository.update_profile_transaction('default', seed)
    repository.write_shared_text('rules/Remote.list', OLD)
    return repository, driver, host


def owned(driver):
    return sorted(row['dst-address'] for row in driver.rows[IPV6] if row.get('comment') == 'cf-rule6:ros')


def saved(repository):
    return repository.get_profile('default')['routers']['items'][0].get('ipv6_routes_state')


def test_remote_update_adds_and_removes_ipv6_destinations_with_readback(ipv6_environment):
    repository, driver, _ = ipv6_environment
    publish((repository, driver))
    assert owned(driver) == ['2001:b28:f23d::/48', '2001:b28:f23f::/48']
    assert len(saved(repository)['resources']) == 2
    kept = next(row for row in driver.rows[IPV6] if row['dst-address'] == '2001:b28:f23d::/48')
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'synced', result
    assert (result['added'], result['removed']) == (2, 2)
    assert owned(driver) == ['2001:67c:4e8::/48', '2001:b28:f23d::/48']
    assert kept in driver.rows[IPV6]  # unchanged destinations are not rewritten
    assert {row['gateway'] for row in driver.rows[IPV6]} == {GW}
    manifest = saved(repository)
    assert {entry['config']['dst-address'] for entry in manifest['resources']} == set(owned(driver))
    assert 'IPv6' not in result['message']
    # The next manual preview agrees with what the sync left behind.
    from backend.routes.routers import _inspect
    config = rule_sync.config_for('default')
    _, _, state, _ = _inspect(config, config['routers']['items'][0], driver)
    assert state.ipv6_routes_plan['ops'] == [] and state.ipv6_routes_plan['status'] == 'synced'


def test_failed_ipv6_write_rolls_back_ipv4_and_ipv6_and_keeps_ownership(ipv6_environment):
    repository, driver, _ = ipv6_environment
    publish((repository, driver))
    before = {path: copy.deepcopy(driver.rows[path]) for path in (ROUTES, IPV6)}
    manifest = copy.deepcopy(saved(repository))
    driver.fail = ('PUT', IPV6, driver.counts['PUT', IPV6] + 1)
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'paused', result
    assert sorted(row['dst-address'] for row in driver.rows[ROUTES]) == \
        sorted(row['dst-address'] for row in before[ROUTES])
    assert owned(driver) == ['2001:b28:f23d::/48', '2001:b28:f23f::/48']
    current = saved(repository)
    assert [entry['config'] for entry in current['resources']] == [entry['config'] for entry in manifest['resources']]
    assert {entry['id'] for entry in current['resources']} <= {row['.id'] for row in driver.rows[IPV6]}


def test_ipv6_enrolment_and_next_hop_changes_wait_for_a_manual_release_without_pausing(ipv6_environment):
    repository, driver, host = ipv6_environment
    repository.write_shared_text('rules/Remote.list', 'IP-CIDR,1.1.1.0/24\nDOMAIN-SUFFIX,example.com')
    publish((repository, driver))
    assert owned(driver) == [] and saved(repository) is None
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'synced', result
    assert '8.8.8.0/24' in [row['dst-address'] for row in driver.rows[ROUTES]]
    assert owned(driver) == [] and 'IPv6 规则集路由未自动更新' in result['message']
    assert '手动预览发布' in result['message']

    # After a manual enrolment, a new Mihomo next hop is still a manual change.
    publish((repository, driver))
    assert owned(driver) == ['2001:67c:4e8::/48', '2001:b28:f23d::/48']
    host['addresses'] = [{'address': 'fe80::9/64', 'interface': 'eth0'}]
    result = rule_sync.sync('default', 'ros', lambda *_: OLD)
    assert result['status'] == 'synced', result
    assert '下一跳' in result['message']
    assert {row['gateway'] for row in driver.rows[IPV6]} == {GW}
    assert owned(driver) == ['2001:67c:4e8::/48', '2001:b28:f23d::/48']


def test_unclear_ipv6_next_hop_is_skipped_by_the_sync_and_ipv4_still_updates(ipv6_environment):
    repository, driver, host = ipv6_environment
    publish((repository, driver))
    # A second link-local address alone is not unclear: the proven next hop is kept.
    host['addresses'].append({'address': 'fe80::4/64', 'interface': 'eth0'})
    from backend.routes.routers import _inspect
    config = rule_sync.config_for('default')
    assert _inspect(config, config['routers']['items'][0], driver)[2].ipv6_routes_plan['gateway'] == GW
    host.clear()
    host['error'] = 'Agent 离线'
    result = rule_sync.sync('default', 'ros', lambda *_: NEW)
    assert result['status'] == 'synced', result
    assert '8.8.8.0/24' in [row['dst-address'] for row in driver.rows[ROUTES]]
    assert owned(driver) == ['2001:b28:f23d::/48', '2001:b28:f23f::/48']
    assert 'IPv6 规则集路由未自动更新' in result['message']
    assert store.read('default', 'ros')['status'] == 'synced'
