"""Consumer-visible compilation contracts for profile-local named proxy chains."""
from copy import deepcopy

import pytest
import yaml

from backend.converters.mihomo import generate_mihomo_config, get_mihomo_provider_downloads
from backend.converters.surge import generate_surge_config
from backend.utils.dialer_references import (
    DialerReferenceError, dependency_graph, incoming_dialers, validate_dialers,
)
from backend.utils.provider_delivery import DeliverySnapshot
from backend.utils.proxy_chains import lower_proxy_chains


def node(id, **fields):
    return {'id': id, 'name': id.upper(), 'type': 'http', 'server': f'{id}.test', 'port': 80, **fields}


def chain(id='chain', entry=None, exit='b', **fields):
    return {'id': id, 'name': id.title(), 'type': 'chain',
            'chain': {'entry': entry or {'type': 'node', 'id': 'a'},
                      'exit': exit if isinstance(exit, dict) else {'type': 'node', 'id': exit}}, **fields}


def profile(*groups):
    return {'profile_id': 'default', 'nodes': [node('a'), node('b')],
            'proxy_groups': [chain(), *groups], 'rule_configs': []}


def render(config, **kwargs):
    return yaml.safe_load(generate_mihomo_config(config, **kwargs))


@pytest.mark.parametrize('ordering', ['include', 'precise', 'strategies_first', 'legacy'])
def test_direct_exit_and_named_chain_coexist_without_shared_mutation(ordering):
    chooser = {'id': 'chooser', 'name': 'Choose', 'type': 'select',
               'manual_nodes': ['b'], 'include_groups': ['chain']}
    expected = ['B', 'Chain']
    if ordering == 'precise':
        chooser['proxies_order'] = [{'type': 'node', 'id': 'b'}, {'type': 'strategy', 'id': 'chain'}]
    elif ordering == 'strategies_first':
        chooser['proxy_order'] = 'strategies_first'
        expected.reverse()
    elif ordering == 'legacy':
        chooser = {'id': 'chooser', 'name': 'Choose', 'type': 'select',
                   'source': 'strategy', 'proxies': ['chain']}
        expected = ['Chain']
    config = profile(chooser)
    config['rule_configs'] = [{'id': 'rule', 'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'Chain'}]
    before = deepcopy(config)
    main = render(config)
    proxies = {p['name']: p for p in main['proxies']}
    assert proxies['Chain']['server'] == 'b.test'
    assert proxies['Chain']['dialer-proxy'] == 'A'
    assert proxies['A']['server'] == 'a.test'
    if ordering != 'legacy':
        assert 'dialer-proxy' not in proxies['B']
    assert main['proxy-groups'][0]['proxies'] == expected
    assert main['rules'] == ['MATCH,Chain']
    assert all(g['type'] != 'chain' for g in main['proxy-groups'])
    assert config == before


def test_rule_only_chain_emits_without_automatic_selection_or_original_exit():
    config = profile()
    config['rule_configs'] = [{'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'Chain'}]
    main = render(config)
    assert {p['name'] for p in main['proxies']} == {'A', 'Chain'}
    assert main['proxy-groups'] == []
    assert main['rules'] == ['MATCH,Chain']


def test_stable_chain_references_follow_renamed_nodes_groups_and_chains():
    config = profile({'id': 'entry', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['a']},
                     chain('outer', {'type': 'group', 'id': 'chain'}),
                     {'id': 'chooser', 'name': 'Choose', 'type': 'select', 'include_groups': ['outer']})
    config['proxy_groups'][0]['chain']['entry'] = {'type': 'group', 'id': 'entry'}
    config['proxy_groups'][0]['name'] = 'Renamed chain'
    config['proxy_groups'][1]['name'] = 'Renamed entry'
    config['nodes'][0]['name'] = 'Renamed node'
    proxies = {p['name']: p for p in render(config)['proxies']}
    assert proxies['Renamed chain']['dialer-proxy'] == 'Renamed entry'
    assert proxies['Outer']['dialer-proxy'] == 'Renamed chain'
    assert 'Renamed node' in proxies


def test_lowering_ids_are_unique_and_nested_objects_are_independent():
    config = profile({'id': 'chooser', 'name': 'Choose', 'type': 'select',
                      'proxies_order': [{'type': 'strategy', 'id': 'chain'}]})
    config['nodes'].append(node('__chain__chain'))
    config['nodes'][1]['params'] = {'headers': {'User-Agent': 'shared'}}
    before = deepcopy(config)
    runtime = lower_proxy_chains(config)
    ids = [n['id'] for n in runtime['nodes']] + [g['id'] for g in runtime['proxy_groups']]
    assert len(ids) == len(set(ids))
    derived = next(n for n in runtime['nodes'] if n['name'] == 'Chain')
    assert runtime['proxy_groups'][0]['proxies_order'] == [{'type': 'node', 'id': derived['id']}]
    derived['params']['headers']['User-Agent'] = 'runtime'
    assert config == before
    assert runtime['nodes'][1]['params']['headers']['User-Agent'] == 'shared'
    assert not any(g['type'] == 'chain' for g in runtime['proxy_groups'])


def test_chain_entry_group_can_choose_original_direct_exit():
    config = profile({'id': 'entry', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['a', 'b']})
    config['proxy_groups'][0]['chain']['entry'] = {'type': 'group', 'id': 'entry'}
    proxies = {p['name']: p for p in render(config)['proxies']}
    assert proxies['Chain']['dialer-proxy'] == 'Entry'
    assert 'dialer-proxy' not in proxies['B']


def test_indirect_group_cycle_checks_every_branch():
    config = profile({'id': 'entry', 'name': 'Entry', 'type': 'select',
                      'manual_nodes': ['a'], 'include_groups': ['nested']},
                     {'id': 'nested', 'name': 'Nested', 'type': 'select', 'include_groups': ['chain']})
    config['proxy_groups'][0]['chain']['entry'] = {'type': 'group', 'id': 'entry'}
    with pytest.raises(DialerReferenceError):
        validate_dialers(config)
    with pytest.raises(DialerReferenceError):
        render(config)


@pytest.mark.parametrize('case', ['missing_exit', 'missing_entry', 'disabled_exit', 'disabled_entry',
                                  'duplicate_name', 'duplicate_id', 'reserved_name', 'self_entry',
                                  'subscription_exit', 'subscription_entry', 'malformed_entry',
                                  'malformed_chain'])
def test_invalid_chain_metadata_is_rejected_before_conversion(case, monkeypatch):
    config = profile()
    current = config['proxy_groups'][0]
    if case == 'missing_exit':
        current['chain']['exit']['id'] = 'missing'
    elif case == 'missing_entry':
        current['chain']['entry']['id'] = 'missing'
    elif case == 'disabled_exit':
        config['nodes'][1]['enabled'] = False
    elif case == 'disabled_entry':
        config['nodes'][0]['enabled'] = False
    elif case == 'duplicate_name':
        current['name'] = 'B'
    elif case == 'duplicate_id':
        config['proxy_groups'].append(chain(name='Other'))
    elif case == 'reserved_name':
        current['name'] = 'DIRECT'
    elif case == 'self_entry':
        current['chain']['entry']['id'] = 'b'
    elif case == 'subscription_exit':
        config['nodes'][1]['subscription_id'] = 'feed'
    elif case == 'subscription_entry':
        config['nodes'][0]['subscription_id'] = 'feed'
    elif case == 'malformed_entry':
        current['chain']['entry'] = None
    elif case == 'malformed_chain':
        current['chain'] = []
    def unexpected_conversion(_node):
        pytest.fail('Invalid stored metadata must not reach node conversion')
    monkeypatch.setattr('backend.converters.mihomo.convert_node_to_mihomo', unexpected_conversion)
    with pytest.raises(DialerReferenceError):
        render(config)


@pytest.mark.parametrize('field,value', [('subscriptions', ['feed']), ('aggregations', ['agg']),
                                        ('use', ['Feed']), ('follow_group', 'other')])
@pytest.mark.parametrize('nested', [False, True])
def test_chain_entry_accepts_dynamic_group_dependency_closure(field, value, nested, monkeypatch):
    dynamic = {'id': 'dynamic', 'name': 'Dynamic', 'type': 'select', 'manual_nodes': ['a'], field: value}
    config = profile(dynamic, {'id': 'other', 'name': 'Other', 'type': 'select', 'subscriptions': ['feed']})
    config['subscriptions'] = [{'id': 'feed', 'name': 'Feed', 'url': 'https://feed.test'}]
    config['subscription_aggregations'] = [{'id': 'agg', 'name': 'Agg', 'subscriptions': ['feed']}]
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        lambda *_: ('proxies: [{name: Remote, type: http, server: remote.test, port: 80}]', 'remote'))
    monkeypatch.setattr('backend.routes.aggregations.get_subscription_proxies_yaml',
                        lambda *_: ('proxies: [{name: Remote, type: http, server: remote.test, port: 80}]', 'remote'))
    target = 'dynamic'
    if nested:
        config['proxy_groups'].append({'id': 'static', 'name': 'Static', 'type': 'select',
                                       'include_groups': ['dynamic']})
        target = 'static'
    config['proxy_groups'][0]['chain']['entry'] = {'type': 'group', 'id': target}
    validate_dialers(config)
    main = render(config)
    assert next(p for p in main['proxies'] if p['name'] == 'Chain')['dialer-proxy'] == target.title()
    assert main['proxy-providers']


def test_manual_only_constraint_includes_nested_static_group_members():
    config = profile({'id': 'entry', 'name': 'Entry', 'type': 'select', 'manual_nodes': ['a']})
    config['proxy_groups'][0]['chain']['entry'] = {'type': 'group', 'id': 'entry'}
    config['nodes'][0]['subscription_id'] = 'feed'
    with pytest.raises(DialerReferenceError):
        validate_dialers(config)


def test_follow_chain_is_rejected_even_when_follower_is_disabled():
    config = profile({'id': 'follower', 'name': 'Follower', 'type': 'select',
                      'follow_group': 'chain', 'enabled': False})
    with pytest.raises(DialerReferenceError):
        validate_dialers(config)


@pytest.mark.parametrize('consumer', ['group', 'order', 'chain', 'rule'])
def test_disabled_chain_cannot_be_used_by_enabled_consumer(consumer):
    config = profile()
    config['proxy_groups'][0]['enabled'] = False
    if consumer == 'group':
        config['proxy_groups'].append({'id': 'g', 'name': 'Group', 'type': 'select', 'include_groups': ['chain']})
    elif consumer == 'order':
        config['proxy_groups'].append({'id': 'g', 'name': 'Group', 'type': 'select',
                                       'proxies_order': [{'type': 'strategy', 'id': 'chain'}]})
    elif consumer == 'chain':
        config['proxy_groups'].append(chain('outer', {'type': 'group', 'id': 'chain'}))
    else:
        config['rule_configs'] = [{'itemType': 'rule', 'rule_type': 'MATCH', 'policy': 'Chain'}]
    with pytest.raises(ValueError):
        render(config)


def test_disabled_chain_preserves_disabled_exit_metadata_without_emitting():
    config = profile()
    config['proxy_groups'][0]['enabled'] = False
    config['nodes'][1]['enabled'] = False
    validate_dialers(config)
    assert render(config)['proxies'] == []
    assert '[Proxy Group]' in generate_surge_config(config)


def test_logical_graph_and_incoming_references_protect_chain_and_shared_dependencies():
    config = profile(chain('outer', {'type': 'group', 'id': 'chain'}),
                     {'id': 'g', 'name': 'Group', 'type': 'select', 'enabled': False,
                      'proxies_order': [{'type': 'strategy', 'id': 'chain'}]})
    assert dependency_graph(config)[('group', 'chain')] == [('node', 'a')]
    assert set(incoming_dialers(config, 'group', 'chain')) == {('group', 'outer'), ('group', 'g')}
    assert ('group', 'chain') in incoming_dialers(config, 'node', 'b')
    assert ('group', 'chain') in incoming_dialers(config, 'node', 'a')


@pytest.mark.parametrize('failed_node', ['A', 'Chain'])
def test_conversion_failure_cannot_silently_remove_chain_topology(failed_node, monkeypatch):
    from backend.converters.mihomo import convert_node_to_mihomo
    monkeypatch.setattr('backend.converters.mihomo.convert_node_to_mihomo',
                        lambda n: None if n['name'] == failed_node else convert_node_to_mihomo(n))
    with pytest.raises(DialerReferenceError):
        render(profile())


def test_raw_dialer_cycle_through_named_chain_is_rejected():
    config = profile()
    config['nodes'][0]['params'] = {'dialer-proxy': 'Chain'}
    with pytest.raises(DialerReferenceError):
        validate_dialers(config)


def test_surge_rejects_enabled_named_chain_instead_of_exporting_fake_group():
    with pytest.raises(DialerReferenceError, match='Surge'):
        generate_surge_config(profile())


def test_provider_receives_original_catalog_and_never_automatic_chain_copy(monkeypatch):
    config = profile({'id': 'g', 'name': 'Group', 'type': 'select', 'aggregations': ['agg']})
    config['subscription_aggregations'] = [{'id': 'agg', 'name': 'Aggregate', 'nodes': ['b']}]
    before = deepcopy(config)
    calls = []
    def provider(aggregation, *, config, main_config, persist, subscription_fetch=None):
        calls.append(config)
        assert persist is False
        assert config == before
        assert {p['name'] for p in main_config['proxies']} == {'A', 'Chain'}
        return {'content': 'proxies: [{name: B, type: http, server: b.test, port: 80}]'}
    monkeypatch.setattr('backend.routes.aggregations.generate_aggregation_provider', provider)
    main = render(config)
    assert len(calls) == 1
    assert set(main['proxy-providers']) == {'Aggregate'}
    assert [p['name'] for p in get_mihomo_provider_downloads(config)] == ['Aggregate']
    assert config == before


def test_provider_cannot_shadow_named_chain_but_original_exit_overlap_remains_valid():
    config = profile({'id': 'g', 'name': 'Group', 'type': 'select', 'manual_nodes': ['b']})
    main = render(config)
    snapshot = DeliverySnapshot.capture('default', main, profile=config)
    config['proxy_groups'][0]['name'] = 'Changed later'
    with pytest.raises(DialerReferenceError):
        snapshot.validate([{'name': 'Chain', 'type': 'http', 'server': 'remote.test', 'port': 80}])
    snapshot.validate([{'name': 'B', 'type': 'http', 'server': 'b.test', 'port': 80}])
