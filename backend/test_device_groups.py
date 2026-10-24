"""Shared membership/source semantics, independent of either engine compiler."""
from copy import deepcopy
import ipaddress
import random

import pytest

from backend.common.device_groups import (
    DeviceGroupsError, compile_groups, infrastructure_snapshot, lookup_ip,
    normalize_groups, normalize_sources, ownership_ranges, revision,
    source_networks, source_revision,
)
from backend.common.oxidns_device_policy import revision as legacy_revision
from backend.routers.ros_policy import smart_sources

DIRECT = 'dg_123456'
OTHER = 'dg_abcdef'


def group(identity=DIRECT, members=None, **fields):
    return {'id': identity, 'name': '设备组' if identity == DIRECT else '其他设备组',
            'members': members if members is not None else ['10.0.0.50'], **fields}


def config(*groups, default=()):
    return {'id': 'default', 'device_groups': {'version': 1, 'groups': [*groups,
        {'id': 'default', 'name': '默认组', 'members': list(default)}]}}


def contains(ranges, address):
    address = ipaddress.ip_address(address)
    return any(address.version == net.version and address in net for net in map(ipaddress.ip_network, ranges))


def test_absent_and_null_are_not_an_implicit_migration():
    assert normalize_groups(None) is None
    for cfg in ({}, {'device_groups': None}):
        compiled = compile_groups(cfg)
        assert not compiled['enabled'] and not compiled['groups'] and not compiled['effective']
    for malformed in ({}, [], {'version': True, 'groups': []}, {'version': 2, 'groups': []}):
        with pytest.raises(DeviceGroupsError):
            normalize_groups(malformed)


def test_normalization_is_pure_canonical_and_default_last():
    cfg = config(group(members=['::ffff:10.0.0.50/128', 'fd00::123/64'], mode='direct'))
    cfg['device_groups']['groups'].reverse()
    before = deepcopy(cfg)
    normalized = compile_groups(cfg)['groups']
    assert normalized[0] == group(members=['10.0.0.50', 'fd00::/64'], mode='direct', keep_blocking=False, dns='follow')
    assert normalized[-1]['id'] == 'default'
    assert cfg == before


@pytest.mark.parametrize('member', ['0.0.0.0/0', '::/0', '127.0.0.2', '127.0.0.0/8', '::1',
                                    '198.18.0.5', '198.18.0.0/15', '224.0.0.1', 'not-an-ip'])
def test_invalid_device_members(member):
    with pytest.raises(DeviceGroupsError):
        compile_groups(config(group(members=[member])))


def test_custom_fake_range_is_used_instead_of_the_default():
    cfg = config(group(members=['198.18.0.50']))
    cfg['mihomo'] = {'custom_config': 'dns:\n  fake-ip-range: 100.64.0.1/16\n'}
    assert compile_groups(cfg)['enabled']
    cfg['device_groups']['groups'][0]['members'] = ['100.64.0.50']
    with pytest.raises(DeviceGroupsError, match='fake-IP'):
        compile_groups(cfg)


@pytest.mark.parametrize('groups,default', [
    ([group(members=['10.0.0.50', '10.0.0.50/32'])], []),
    ([group(), group(OTHER, ['::ffff:10.0.0.50'])], []),
    ([group()], ['10.0.0.50/32']),
])
def test_equal_cidrs_cannot_compete_even_with_different_spelling(groups, default):
    with pytest.raises(DeviceGroupsError, match='重复'):
        compile_groups(config(*groups, default=default))


@pytest.mark.parametrize('groups,default,expected', [
    ([group(members=['10.0.0.0/25'])], ['10.0.0.0/26'], ['10.0.0.64/26']),
    ([group(members=['10.0.0.0/24', '10.0.0.0/26'])], ['10.0.0.0/25'], ['10.0.0.0/26', '10.0.0.128/25']),
    ([group(members=['10.0.0.0/24', 'fd00::/64']), group(OTHER, ['10.0.0.64/26', 'fd00::/65'])], [],
         ['10.0.0.0/26', '10.0.0.128/25', 'fd00::8000:0:0:0/65']),
])
def test_longest_prefix_and_same_group_reentry(groups, default, expected):
    cfg = config(*groups, default=default)
    assert compile_groups(cfg)['ownership'][DIRECT] == expected
    cfg['device_groups']['groups'].reverse()
    assert compile_groups(cfg)['ownership'][DIRECT] == expected


def test_infrastructure_affects_effective_ranges_not_ownership_or_sources():
    cfg = config(group(members=['10.0.0.0/25'], mode='direct'))
    cfg.update(routers={'items': [{'id': 'router', 'endpoint': 'http://10.0.0.1'}]},
               agents=[{'id': 'agent', 'service_type': 'mihomo', 'profile_id': 'default', 'host': '10.0.0.10'}],
               policy_routing_sources={'include': ['10.0.0.0/24'], 'exclude_groups': [DIRECT]})
    compiled = compile_groups(cfg)
    assert compiled['ownership'][DIRECT] == ['10.0.0.0/25']
    for address in ('10.0.0.1', '10.0.0.10'):
        assert not contains(compiled['effective'][DIRECT], address)
        lookup = lookup_ip(cfg, address, compiled=compiled)
        assert lookup['owner_id'] == DIRECT and lookup['group_id'] == 'default'
        assert lookup['infrastructure_excluded']
    assert source_networks(cfg, '10.0.0.10', compiled=compiled) == ['10.0.0.128/25']
    cfg['device_groups']['groups'][0]['members'] = ['10.0.0.10']
    with pytest.raises(DeviceGroupsError, match='自身地址'):
        compile_groups(cfg)


def test_infrastructure_filters_profiles_but_includes_linked_gateways_without_dns_lookup():
    cfg = {'id': 'p', 'routers': {'items': [
        {'id': 'r', 'endpoint': 'https://router.example', 'proxy_gateway_agent_id': 'linked'}]},
        'agents': [
        {'id': 'own', 'profile_id': 'p', 'host': 'fd00::10', 'service_type': 'oxidns'},
        {'id': 'elsewhere', 'profile_id': 'q', 'host': '10.0.0.20', 'service_type': 'mihomo'},
        {'id': 'linked', 'profile_id': 'q', 'host': '10.0.0.30', 'service_type': 'mihomo'}]}
    snapshot = infrastructure_snapshot(cfg, verified_addresses=['10.0.0.1'])
    assert {row['ip'] for row in snapshot['addresses']} == {'fd00::10', '10.0.0.30', '10.0.0.1'}
    assert snapshot['unresolved'][0]['value'] == 'router.example'
    before = snapshot['fingerprint']
    cfg['agents'][0].update(name='新名称', status='online', system_metrics={'cpu': 90})
    assert infrastructure_snapshot(cfg, verified_addresses=['10.0.0.1'])['fingerprint'] == before
    cfg['agents'][0]['profile_id'] = 'q'
    assert infrastructure_snapshot(cfg, verified_addresses=['10.0.0.1'])['fingerprint'] != before


@pytest.mark.parametrize('policy', ['GLOBAL', 'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'PASS-RULE', 'COMPATIBLE', '不存在', '', None])
def test_all_proxy_only_accepts_enabled_named_groups(policy):
    cfg = config(group(mode='proxy', policy=policy))
    cfg['proxy_groups'] = [{'id': 'p', 'name': '节点选择'}, {'id': 'off', 'name': '已停用', 'enabled': False}]
    if policy in {'GLOBAL', 'DIRECT', 'REJECT', 'REJECT-DROP', 'PASS', 'PASS-RULE', 'COMPATIBLE'}:
        cfg['proxy_groups'].append({'id': 'reserved', 'name': policy})
    with pytest.raises(DeviceGroupsError, match='已启用'):
        compile_groups(cfg)
    cfg['device_groups']['groups'][0]['policy'] = '节点选择'
    assert compile_groups(cfg)['groups'][0]['policy'] == '节点选择'
    cfg['device_groups']['groups'][0]['policy'] = '已停用'
    with pytest.raises(DeviceGroupsError):
        compile_groups(cfg)


def test_default_is_required_and_has_no_mode():
    cfg = config(group())
    cfg['device_groups']['groups'].pop()
    with pytest.raises(DeviceGroupsError, match='默认组'):
        compile_groups(cfg)
    cfg = config()
    cfg['device_groups']['groups'][0]['mode'] = 'direct'
    with pytest.raises(DeviceGroupsError, match='默认组'):
        compile_groups(cfg)


def test_member_limit_and_expanded_range_limit():
    members = [str(ipaddress.IPv4Address('10.0.0.0') + index) for index in range(1024)]
    assert len(normalize_groups(config(group(members=members))['device_groups'])['groups'][0]['members']) == 1024
    with pytest.raises(DeviceGroupsError, match='1024'):
        normalize_groups(config(group(members=members + ['10.0.4.0']))['device_groups'])
    exceptions = [str(ipaddress.IPv4Address('10.0.0.0') + index * 104729) for index in range(128)]
    # Use 128 well-separated exceptions inside /1; no loopback/fake-IP overlap.
    cfg = config(group(members=['10.0.0.0/8']), default=exceptions)
    import backend.common.device_groups as model
    previous = model.MAX_RANGES
    try:
        model.MAX_RANGES = 64
        with pytest.raises(DeviceGroupsError, match='64'):
            compile_groups(cfg)
    finally:
        model.MAX_RANGES = previous


def test_default_source_exclusion_uses_the_implicit_complement_clipped_to_include():
    cfg = config(group(members=['10.0.0.0/25']), default=['10.0.0.50'])
    compiled = compile_groups(cfg)
    assert 'default' not in compiled['ownership']
    default = ownership_ranges(compiled, ['default'], within=['10.0.0.0/24'])
    assert default == ['10.0.0.50/32', '10.0.0.128/25']
    cfg['policy_routing_sources'] = {'include': ['10.0.0.0/24'], 'exclude_groups': ['default']}
    sources = source_networks(cfg, '10.0.0.254')
    assert contains(sources, '10.0.0.20')
    assert not contains(sources, '10.0.0.50') and not contains(sources, '10.0.0.200')


def test_sources_are_independent_allow_zero_prefix_and_ignore_ipv6_on_routeros():
    cfg = {'policy_routing_sources': {'include': ['0.0.0.0/0', '::/0'], 'exclude_groups': []}}
    assert normalize_sources(cfg['policy_routing_sources']) == cfg['policy_routing_sources']
    sources = source_networks(cfg, '10.0.0.10')
    assert contains(sources, '8.8.8.8') and not contains(sources, '10.0.0.10')
    assert not contains(sources, 'fd00::1')
    assert source_networks({'policy_routing_sources': {'include': [], 'exclude_groups': []}}, '10.0.0.10') == []


def test_source_revision_preserves_legacy_and_ignores_dns_group_labels_and_modes():
    legacy = {'oxidns': {'device_policies': [{'ip': '10.0.0.0/24', 'mode': 'smart'}], 'local_dns': 'example'}}
    assert source_revision(legacy) == legacy_revision(legacy['oxidns'])
    assert source_networks(legacy, '10.0.0.10') == smart_sources(legacy['oxidns']['device_policies'], '10.0.0.10')
    cfg = config(group(members=['10.0.0.0/25'], mode='direct'))
    cfg['policy_routing_sources'] = {'include': ['10.0.0.0/24'], 'exclude_groups': [DIRECT]}
    before = source_revision(cfg)
    cfg['oxidns'] = {'local_dns': 'changed'}
    cfg['device_groups']['groups'][0].update(name='已改名', mode='follow')
    assert source_revision(cfg) == before
    cfg['device_groups']['groups'][0]['members'] = ['10.0.0.0/26']
    assert source_revision(cfg) != before


def test_revision_detects_related_agent_bindings_and_lookup_reports_longest_member():
    cfg = config(group(members=['10.0.0.0/24']), group(OTHER, ['10.0.0.50']))
    assert lookup_ip(cfg, '10.0.0.50')['group_id'] == OTHER
    assert lookup_ip(cfg, '10.0.0.51')['group_id'] == DIRECT
    assert lookup_ip(cfg, '10.1.0.1')['group_id'] == 'default'
    first = revision(cfg)
    cfg['agents'] = [{'id': 'a', 'service_type': 'oxidns', 'profile_id': 'default', 'host': '10.0.0.10'}]
    assert revision(cfg) != first
    with pytest.raises(DeviceGroupsError):
        lookup_ip(cfg, '10.0.0.0/24')


def test_random_legacy_migration_formula_preserves_exact_source_entries_and_order():
    rng = random.Random(20261024)
    for _ in range(100):
        choices = {}
        for _ in range(rng.randrange(1, 35)):
            network = ipaddress.IPv4Network((int(ipaddress.IPv4Address('10.0.0.0')) + rng.randrange(256),
                                            rng.randrange(24, 33)), strict=False)
            choices[str(network)] = rng.choice(['smart', 'direct'])
        rows = [{'ip': address, 'mode': mode} for address, mode in choices.items()]
        direct = [row['ip'] for row in rows if row['mode'] == 'direct']
        smart = [row['ip'] for row in rows if row['mode'] == 'smart']
        exceptions = [address for address in smart if any(ipaddress.ip_network(address).subnet_of(ipaddress.ip_network(net)) for net in direct)]
        cfg = config(group(members=direct), default=exceptions)
        cfg['policy_routing_sources'] = {'include': smart, 'exclude_groups': [DIRECT]}
        gateway = '10.0.0.' + str(rng.randrange(256))
        assert source_networks(cfg, gateway) == smart_sources(rows, gateway)
