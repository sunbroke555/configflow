"""DNS high availability is opt-in: without it every router output stays byte-for-byte the same.

These values were recorded before the feature existed. An Agent record that
never enabled it (or carries a disabled leftover) must keep producing them.
"""
import copy
import hashlib
import json

import pytest

from backend.routers import dns_guard
from backend.routers.dhcp_dns import dns_agent_settings
from backend.test_router_dhcp_dns import DESIRED as DHCP_DESIRED, DNS_AGENT, ROUTER as DHCP_ROUTER, MemoryDriver, plan_for
from backend.test_router_dns_guard import DESIRED as GUARD_DESIRED, device, preview, router as guard_router


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


DISABLED_LEFTOVERS = [{}, {'oxidns_ha': None}, {'oxidns_ha': {}}, {'oxidns_ha': {'enabled': False}},
                      {'oxidns_ha': {'enabled': 'true', 'vip': '10.0.0.9', 'peer_id': 'dns-2'}}]

GUARD_SPEC = {'agent': '10.0.0.2', 'interface': 'bridge', 'network': '10.0.0.0/24',
              'bypass': ['10.0.0.2/32', '10.0.0.4/32', '10.0.0.50/32'], 'block_dot': True, 'failover': True}
UP_SCRIPT = ('/ip firewall nat enable [find comment="cf-dnsguard:hijack"]; '
             ':foreach c in=[/ip firewall connection find where dst-port=53 and !dstnat and dst-address!=10.0.0.2 '
             'and src-address!=10.0.0.2 and src-address!=10.0.0.4 and src-address!=10.0.0.50] do={'
             ':do {/ip firewall connection remove $c} on-error={}}')
DOWN_SCRIPT = (':delay 15s; :if ([/tool netwatch get [find name="cf-dnsguard"] status] = "down") '
               'do={/ip firewall nat disable [find comment="cf-dnsguard:hijack"]; '
               ':foreach c in=[/ip firewall connection find where dst-port=53 and dstnat and '
               'reply-src-address=10.0.0.2] do={:do {/ip firewall connection remove $c} on-error={}}}')
RESOURCES_HASH = '43c524b01465a261b65c59d0489db26dfea5ad453f5759d92b0ba56c303cbec5'
GUARD_FINGERPRINT = '78a13339dbb09b14'
DHCP_FINGERPRINT = '6603e082a6bbbf58'
CONTROLS_DIGEST = 'aa1eba905a5ae0813aaedef09d734144834b958eead385c625e99ca2d61532f0'


def test_guard_spec_resources_and_netwatch_scripts_are_unchanged():
    _, driver = device()
    spec = dns_guard.desired_spec(guard_router()['dns_guard'], GUARD_DESIRED, driver.read_state())
    assert spec == GUARD_SPEC and list(spec) == list(GUARD_SPEC)
    items = dns_guard.resources(spec)
    watch = items[-1]['values']
    assert (watch['up-script'], watch['down-script']) == (UP_SCRIPT, DOWN_SCRIPT)
    assert _hash(items) == RESOURCES_HASH


def test_guard_plan_fingerprint_is_unchanged():
    _, driver = device()
    _, plan = preview(driver, guard_router())
    assert plan['fingerprint'] == GUARD_FINGERPRINT


@pytest.mark.parametrize('leftover', DISABLED_LEFTOVERS)
def test_dhcp_settings_and_plan_fingerprint_are_unchanged(leftover):
    settings = dns_agent_settings('dns-agent', {**DNS_AGENT, **leftover})
    assert settings == {'dns_agent_id': 'dns-agent', 'dns_server': '192.168.0.2', 'dns_error': ''}
    plan = plan_for(MemoryDriver(), DHCP_ROUTER, {**DHCP_DESIRED, **settings})
    assert plan['fingerprint'] == DHCP_FINGERPRINT


@pytest.mark.parametrize('leftover', DISABLED_LEFTOVERS)
def test_router_inspection_uses_the_single_agent_without_ha(monkeypatch, leftover):
    from backend.routes import routers as api
    driver = MemoryDriver()
    # OxiDNS selection also inspects the known LAN; this fixture has no RA service.
    nd_state = {'/ip/address': [{'.id': '*A', 'address': '192.168.0.1/24', 'interface': 'bridge'}],
                '/ipv6/nd': []}
    monkeypatch.setattr(driver, '_list', lambda path: copy.deepcopy(nd_state[path]), raising=False)
    agents = {'dns-agent': {**DNS_AGENT, **copy.deepcopy(leftover)},
              'gateway': {'id': 'gateway', 'host': '192.168.0.3', 'service_type': 'mihomo', 'status': 'offline'}}
    monkeypatch.setattr(api, '_agent', lambda agent_id: agents.get(agent_id) if agent_id else None)
    monkeypatch.setattr(api, 'compute_desired', lambda *args, **kwargs:
                        {key: value for key, value in DHCP_DESIRED.items() if not key.startswith('dns_')})
    _, desired, _, _ = api._inspect({}, {**DHCP_ROUTER, 'proxy_gateway_agent_id': 'gateway'}, driver)
    assert {key: value for key, value in desired.items() if key.startswith('dns')} == {
        'dns_agent_id': 'dns-agent', 'dns_server': '192.168.0.2', 'dns_error': ''}
    assert driver.dhcp_reads == 2  # Initial DHCP report, then the RA DNS scope check.


@pytest.mark.parametrize('leftover', DISABLED_LEFTOVERS)
def test_rule_sync_controls_digest_is_unchanged(monkeypatch, leftover):
    from backend.routers import rule_sync
    agents = {'oxidns': {'id': 'oxidns', 'host': '10.0.0.2', 'service_type': 'oxidns', 'profile_id': 'default',
                         'name': 'DNS', **copy.deepcopy(leftover)},
              'mihomo': {'id': 'mihomo', 'host': '10.0.0.4', 'service_type': 'mihomo', 'profile_id': 'default'},
              'dns-2': {'id': 'dns-2', 'host': '10.0.0.3', 'service_type': 'oxidns', 'profile_id': 'default'}}
    monkeypatch.setattr('backend.routes.routers._agent', lambda agent_id: agents.get(agent_id) if agent_id else None)
    monkeypatch.setattr(rule_sync, 'selected_sources', lambda config, item: {})
    item = {**guard_router(), 'proxy_gateway_agent_id': 'mihomo', 'dns_agent_id': 'oxidns'}
    assert rule_sync.controls({'oxidns': {'listen': '0.0.0.0:53'}}, item) == CONTROLS_DIGEST


def test_reregistration_keeps_the_same_record_fields(tmp_path, monkeypatch):
    from backend.agents.manager import AgentManager
    from backend.common import config as config_store
    from backend.common.config_repository import ProfileRepository
    previous = config_store._repository
    repository = ProfileRepository(tmp_path)
    config_store.set_repository(repository)
    try:
        manager = AgentManager(repository)
        payload = {'name': 'DNS', 'host': '10.0.0.2', 'port': 8080, 'service_type': 'oxidns', 'version': '2.0.0-go'}
        created = manager.register_agent(payload)
        fresh = set(manager.get_agent_by_id(created['id']))
        manager.register_agent(payload, existing_token=created['token'])
        again = set(manager.get_agent_by_id(created['id']))
    finally:
        config_store.set_repository(previous)
    assert fresh == {'id', 'name', 'host', 'port', 'token', 'service_type', 'profile_id', 'deployment_method',
                     'status', 'last_heartbeat', 'version', 'config_version', 'enabled', 'created_at'}
    assert again == fresh | {'updated_at', 'deployments', 'latest_deployment', 'latest_upgrade',
                             'domain_discovery_enabled'}
