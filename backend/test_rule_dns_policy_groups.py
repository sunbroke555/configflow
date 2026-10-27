"""DNS follow uses configured manual group defaults, never live device state."""
from copy import deepcopy
import json

import pytest

from backend.common.oxidns_settings import get_default_oxidns_settings
from backend.common.resource import get_backend_resource
from backend.common.rule_dns import apply_rule_dns, follow_route, policy_dns_routes, rule_dns_state


def group(identity, **fields):
    return {'id': identity, 'name': identity, 'type': 'select', 'enabled': True, **fields}


def config_with_groups(*groups):
    return {'proxy_groups': list(groups),
            'nodes': [{'id': 'node', 'name': 'Proxy node', 'enabled': True}],
            'subscriptions': [{'id': 'sub', 'name': 'Subscription', 'enabled': True}],
            'subscription_aggregations': [{'id': 'agg', 'name': 'Aggregation', 'enabled': True}],
            'rule_library': [], 'rule_configs': [], 'agents': [], 'mosdns': {},
            'oxidns': get_default_oxidns_settings()}


def rule(identity, policy):
    return {'id': identity, 'itemType': 'rule', 'rule_type': 'DOMAIN',
            'value': identity + '.test', 'policy': policy, 'enabled': True}


def test_template_groups_resolve_direct_first_services_and_proxy_regions():
    with open(get_backend_resource('config_template.json'), encoding='utf-8') as handle:
        config = json.load(handle)
    routes = policy_dns_routes(config)
    for name in ('🎯 全球直连', '🍎 苹果服务', '📈 网络测试', 'Ⓜ️ 微软云盘',
                 'Ⓜ️ 微软服务', '🛒 亚马逊', '🐟 漏网之鱼', '📺 哔哩哔哩', '🎬 国内媒体'):
        assert routes[name] == 'direct', name
    for name in ('✈️ 节点选择', '🤖 人工智能', '📲 Telegram', '🎮 游戏平台', '⌨️ GitHub'):
        assert routes[name] == 'proxy', name
    assert routes['🚫 拒绝访问'] is None


@pytest.mark.parametrize('fields,expected', [
    ({'manual_nodes': ['DIRECT', 'node']}, 'direct'),
    ({'manual_nodes': ['node', 'DIRECT']}, 'proxy'),
    ({'manual_nodes': ['REJECT', 'DIRECT']}, None),
    ({'manual_nodes': ['DIRECT', 'node'],
      'proxies_order': [{'type': 'node', 'id': 'node'}, {'type': 'node', 'id': 'DIRECT'}]}, 'proxy'),
    ({'manual_nodes': ['node'], 'include_groups': ['direct'], 'proxy_order': 'strategies_first'}, 'direct'),
    ({'manual_nodes': ['node'], 'include_groups': ['direct']}, 'proxy'),
    ({'source': 'node', 'proxies': ['DIRECT']}, 'direct'),
    ({'source': 'strategy', 'proxies': ['direct']}, 'direct'),
    ({'manual_nodes': ['DIRECT'], 'subscriptions': ['sub']}, 'direct'),
    ({'manual_nodes': ['DIRECT'], 'aggregations': ['agg'],
      'proxies_order': [{'type': 'aggregation', 'id': 'agg'}, {'type': 'node', 'id': 'DIRECT'}]}, 'direct'),
    ({'subscriptions': ['sub']}, 'proxy'),
    ({'manual_nodes': ['DIRECT'], 'subscriptions': ['sub'], 'regex': 'Proxy`DIRECT'}, 'proxy'),
    ({'manual_nodes': ['DIRECT'], 'exclude-filter': 'DIRECT'}, 'proxy'),
    ({'manual_nodes': ['DIRECT'], 'exclude-type': 'direct'}, 'proxy'),
])
def test_manual_group_defaults_match_managed_rendering(fields, expected):
    config = config_with_groups(group('direct', manual_nodes=['DIRECT']), group('selection', **fields))
    before = deepcopy(config)
    assert follow_route(rule('r', 'selection'), config) == expected
    assert config == before


def test_nested_references_and_followed_source_type():
    config = config_with_groups(
        group('direct', manual_nodes=['DIRECT']),
        group('priority', include_groups=['direct']),
        group('service', follow_group='priority'),
        group('nested', include_groups=['service']),
        group('auto', type='url-test', manual_nodes=['DIRECT', 'node']),
        group('follow_auto', follow_group='auto'),
        group('inherits_manual', type='url-test', follow_group='priority'),
    )
    routes = policy_dns_routes(config)
    assert all(routes[name] == 'direct' for name in ('direct', 'priority', 'service', 'nested', 'inherits_manual'))
    assert routes['follow_auto'] == 'proxy'


@pytest.mark.parametrize('kind', ['url-test', 'fallback', 'load-balance', 'chain', 'unknown'])
def test_non_manual_groups_remain_proxy(kind):
    config = config_with_groups(group('automatic', type=kind, manual_nodes=['DIRECT', 'node']))
    assert follow_route(rule('r', 'automatic'), config) == 'proxy'


def test_invalid_and_cyclic_groups_never_guess_direct():
    config = config_with_groups(
        group('a', include_groups=['b']), group('b', include_groups=['a']), group('empty'),
        group('disabled', enabled=False, manual_nodes=['DIRECT']),
        group('missing_source', follow_group='missing'), group('disabled_source', follow_group='disabled'),
        group('direct', manual_nodes=['DIRECT']), group('follower', follow_group='direct'),
        group('invalid_follow_chain', follow_group='follower'),
    )
    routes = policy_dns_routes(config)
    assert all(routes[name] == 'proxy' for name in ('a', 'b', 'empty', 'disabled', 'missing_source',
                                                  'disabled_source', 'invalid_follow_chain'))
    assert follow_route(rule('r', 'missing'), config) == 'proxy'
    assert follow_route(rule('r', 'DIRECT'), config) == 'direct'
    assert follow_route(rule('r', 'REJECT'), config) is None
    assert follow_route({**rule('r', 'direct'), 'rule_type': 'MATCH'}, config) is None


def test_follow_assignment_and_group_order_change_only_move_followed_dns():
    before = config_with_groups(group('selected', manual_nodes=['DIRECT', 'node']))
    before['rule_configs'] = [rule('followed', 'selected'), rule('explicit', 'selected')]
    after = deepcopy(before)
    apply_rule_dns(before, after, {'followed': 'follow', 'explicit': 'direct'})
    assert after['oxidns']['direct_rules'] == ['followed', 'explicit']
    before = deepcopy(after)
    after['proxy_groups'][0]['manual_nodes'] = ['node', 'DIRECT']
    apply_rule_dns(before, after, {})
    assert after['oxidns']['direct_rules'] == ['explicit']
    assert after['oxidns']['proxy_rules'] == ['followed']
    assert after['oxidns']['rule_dns_follow'] == ['followed']
    state = rule_dns_state(after)
    assert state['engine']['policy_routes'] == {'selected': 'proxy'}
    assert state['rules']['followed'] == {'state': 'follow', 'route': 'proxy', 'follow_route': 'proxy'}
    assert state['rules']['explicit'] == {'state': 'explicit', 'route': 'direct'}


def test_legacy_follow_drift_is_read_only_and_reapplying_follow_repairs_it():
    before = config_with_groups(group('selected', manual_nodes=['DIRECT']))
    before['rule_configs'] = [rule('r', 'selected')]
    before['oxidns'].update(rule_dns_follow=['r'], proxy_rules=['r'])
    unchanged = deepcopy(before)
    state = rule_dns_state(before)
    assert state['rules']['r'] == {'state': 'follow_drift', 'route': 'proxy', 'follow_route': 'direct'}
    assert state['engine']['policy_routes'] == {'selected': 'direct'}
    assert before == unchanged
    after = deepcopy(before)
    apply_rule_dns(before, after, {'r': 'follow'})
    assert after['oxidns']['direct_rules'] == ['r'] and after['oxidns']['proxy_rules'] == []
