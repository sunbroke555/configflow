"""Dynamic chain candidate membership and independent strategy behavior."""
from copy import deepcopy
import json

import pytest
import yaml

from backend.converters.mihomo import generate_mihomo_config, get_mihomo_provider_downloads
from backend.utils.dialer_references import DialerReferenceError, validate_emitted
from backend.utils.provider_delivery import prepare_provider_bundle
from backend.test_proxy_chains import chain, node, profile


def dynamic_profile(kind='url-test'):
    config = profile({'id': 'exit', 'name': 'Exit', 'type': kind,
                      'subscriptions': ['feed'], 'regex': '^US-', 'url': 'https://probe.test',
                      'interval': 37, 'tolerance': 23, 'lazy': False,
                      **({'strategy': 'round-robin'} if kind == 'load-balance' else {})})
    config['proxy_groups'][0]['chain']['exit'] = {'type': 'group', 'id': 'exit'}
    config['subscriptions'] = [{'id': 'feed', 'name': 'Feed', 'url': 'https://feed.test'}]
    return config


def provider_text(names):
    return yaml.safe_dump({'proxies': [{'name': name, 'type': 'http', 'server': 'remote.test', 'port': 80}
                                      for name in names]})


@pytest.mark.parametrize('kind', ['select', 'url-test', 'fallback', 'load-balance'])
def test_group_exit_has_independent_strategy_and_live_provider_membership(kind, monkeypatch):
    config = dynamic_profile(kind)
    before = deepcopy(config)
    membership = ['US-one', 'HK-one']
    calls = []
    def fetch(*args):
        calls.append(args)
        return provider_text(membership), 'rendered_yaml'
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    main = yaml.safe_load(generate_mihomo_config(config))
    groups = {g['name']: g for g in main['proxy-groups']}
    original, independent = groups['Exit'], groups['Chain']
    assert independent['type'] == original['type'] == kind
    assert independent['use'] != original['use'] == ['Feed']
    assert original['filter'] == '^US-'
    assert 'filter' not in independent
    for field in ('url', 'interval', 'tolerance', 'lazy'):
        assert independent[field] == original[field]
    if kind == 'load-balance':
        assert independent['strategy'] == 'round-robin'
    private_name = independent['use'][0]
    private = main['proxy-providers'][private_name]
    assert private['filter'] == '^US-'
    assert private['override']['dialer-proxy'] == 'A'
    assert main['proxy-providers']['Feed'].get('override') is None
    assert config == before
    assert len(calls) == 1
    downloads = get_mihomo_provider_downloads(config, main_config=main)
    assert len(downloads) == 2
    assert len({item['local_path'] for item in downloads}) == 2
    assert len({item['url'] for item in downloads}) == 1
    membership[:] = ['US-new', 'US-extra', 'HK-new']
    refreshed = prepare_provider_bundle(config, main)
    assert len(calls) == 2
    assert all([p['name'] for p in yaml.safe_load(item['content'])['proxies']] == membership for item in refreshed)
    assert config == before


@pytest.mark.parametrize('membership', [[], ['HK-only'], ['US-same', 'US-same']])
def test_invalid_provider_refresh_fails_before_publication(membership, monkeypatch):
    config = dynamic_profile()
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        lambda *_: (provider_text(membership), 'rendered_yaml'))
    monkeypatch.setattr('backend.utils.subscription_cache.save_subscription_nodes',
                        lambda *_: pytest.fail('Invalid provider must never update cache'))
    with pytest.raises(DialerReferenceError):
        prepare_provider_bundle(config, main)


def test_provider_entry_cycle_checks_filtered_actual_membership(monkeypatch):
    config = dynamic_profile()
    config['proxy_groups'][0]['chain']['entry'] = {'type': 'group', 'id': 'entry'}
    config['proxy_groups'].append({'id': 'entry', 'name': 'Entry', 'type': 'select', 'subscriptions': ['other']})
    config['subscriptions'].append({'id': 'other', 'name': 'Other', 'url': 'https://other.test'})
    def fetch(id, _url):
        proxies = [{'name': 'US-remote', 'type': 'http', 'server': 'remote.test', 'port': 80}]
        if id == 'other':
            proxies[0]['dialer-proxy'] = 'Chain'
        return yaml.safe_dump({'proxies': proxies}), 'rendered_yaml'
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    with pytest.raises(DialerReferenceError, match='循环'):
        generate_mihomo_config(config)


def test_nested_static_exit_copies_every_candidate_and_preserves_order():
    config = profile({'id': 'exit', 'name': 'Exit', 'type': 'fallback',
                      'proxies_order': [{'type': 'strategy', 'id': 'nested'}, {'type': 'node', 'id': 'b'}],
                      'url': 'https://probe.test', 'interval': 17},
                     {'id': 'nested', 'name': 'Nested', 'type': 'select', 'manual_nodes': ['c']})
    config['nodes'].append(node('c'))
    config['proxy_groups'][0]['chain']['exit'] = {'type': 'group', 'id': 'exit'}
    before = deepcopy(config)
    main = yaml.safe_load(generate_mihomo_config(config))
    groups = {g['name']: g for g in main['proxy-groups']}
    proxies = {p['name']: p for p in main['proxies']}
    derived = groups['Chain']['proxies']
    assert derived[0] in groups
    assert proxies[derived[1]]['server'] == 'b.test'
    assert proxies[derived[1]]['dialer-proxy'] == 'A'
    assert proxies[groups[derived[0]]['proxies'][0]]['dialer-proxy'] == 'A'
    assert groups['Exit']['proxies'] == ['Nested', 'B']
    assert 'dialer-proxy' not in proxies['B']
    assert 'dialer-proxy' not in proxies['C']
    assert config == before


def test_readable_candidates_remain_isolated_when_names_collide():
    config = profile(
        {'id': 'exit', 'name': 'Exit', 'type': 'select', 'manual_nodes': ['b']},
        {'id': 'existing', 'name': 'Chain_B', 'type': 'select', 'manual_nodes': ['b']},
        chain('other', {'type': 'node', 'id': 'c'}, exit={'type': 'group', 'id': 'exit'}),
    )
    config['nodes'].append(node('c'))
    config['proxy_groups'][0]['chain']['exit'] = {'type': 'group', 'id': 'exit'}
    before = deepcopy(config)
    main = yaml.safe_load(generate_mihomo_config(config))
    groups = {g['name']: g for g in main['proxy-groups']}
    proxies = {p['name']: p for p in main['proxies']}
    assert groups['Chain']['proxies'] == ['Chain_B (2)']
    assert groups['Other']['proxies'] == ['Other_B']
    assert proxies['Chain_B (2)']['dialer-proxy'] == 'A'
    assert proxies['Other_B']['dialer-proxy'] == 'C'
    assert groups['Chain_B']['proxies'] == ['B']
    assert 'dialer-proxy' not in proxies['B']
    assert config == before


@pytest.mark.parametrize('serialize', [json.dumps, yaml.safe_dump], ids=['json', 'yaml'])
def test_group_exit_accepts_proxy_string_without_outer_type(serialize):
    config = profile({'id': 'exit', 'name': 'Exit', 'type': 'select', 'manual_nodes': ['b']})
    config['proxy_groups'][0]['chain']['exit'] = {'type': 'group', 'id': 'exit'}
    proxy = {'name': 'Embedded', 'type': 'socks5', 'server': 'exit.test', 'port': 1080}
    config['nodes'][1] = {'id': 'b', 'name': 'B', 'proxy_string': serialize(proxy)}
    before = deepcopy(config)

    main = yaml.safe_load(generate_mihomo_config(config))
    groups = {g['name']: g for g in main['proxy-groups']}
    proxies = {p['name']: p for p in main['proxies']}
    derived, = groups['Chain']['proxies']
    assert groups['Exit']['proxies'] == ['B']
    assert proxies['B'] == {**proxy, 'name': 'B'}
    assert proxies[derived] == {**proxy, 'name': derived, 'dialer-proxy': 'A'}
    assert config == before


@pytest.mark.parametrize('outer_type', [None, 'http'], ids=['missing', 'stale'])
def test_group_exit_excludes_converted_type_instead_of_outer_metadata(outer_type):
    config = profile({'id': 'exit', 'name': 'Exit', 'type': 'select',
                      'manual_nodes': ['b', 'c'], 'exclude-type': 'socks5'})
    config['proxy_groups'][0]['chain']['exit'] = {'type': 'group', 'id': 'exit'}
    config['nodes'][1] = {'id': 'b', 'name': 'B', 'proxy_string': json.dumps(
        {'type': 'socks5', 'server': 'excluded.test', 'port': 1080})}
    if outer_type is not None:
        config['nodes'][1]['type'] = outer_type
    config['nodes'].append(node('c'))
    before = deepcopy(config)

    main = yaml.safe_load(generate_mihomo_config(config))
    groups = {g['name']: g for g in main['proxy-groups']}
    proxies = {p['name']: p for p in main['proxies']}
    derived, = groups['Chain']['proxies']
    assert proxies[derived]['server'] == 'c.test'
    assert proxies[derived]['dialer-proxy'] == 'A'
    assert proxies['B']['type'] == 'socks5'
    assert 'dialer-proxy' not in proxies['B']
    assert groups['Exit']['exclude-type'] == 'socks5'
    assert config == before


def test_chain_as_exit_composes_all_hops_instead_of_dropping_inner_entry():
    config = profile(chain('inner', {'type': 'node', 'id': 'b'}, exit='c'))
    config['nodes'].append(node('c'))
    config['proxy_groups'][0]['chain']['exit'] = {'type': 'group', 'id': 'inner'}
    main = yaml.safe_load(generate_mihomo_config(config))
    proxies = {p['name']: p for p in main['proxies']}
    root = proxies['Chain']
    assert root['server'] == 'c.test'
    middle = proxies[root['dialer-proxy']]
    assert middle['server'] == 'b.test'
    assert middle['dialer-proxy'] == 'A'
    assert proxies['Inner']['dialer-proxy'] == 'B'


def test_disabled_group_exit_can_be_retained_without_emitting():
    config = dynamic_profile()
    config['proxy_groups'][0]['enabled'] = False
    config['proxy_groups'][1]['enabled'] = False
    main = yaml.safe_load(generate_mihomo_config(config))
    assert not main['proxies']
    assert 'Chain' not in {g['name'] for g in main['proxy-groups']}


def test_native_override_cycle_is_not_hidden_by_original_provider_graph():
    main = {'proxies': [], 'proxy-groups': [{'name': 'Entry', 'type': 'select', 'use': ['Private']}],
            'proxy-providers': {'Private': {'override': {'additional-prefix': 'private::', 'dialer-proxy': 'Entry'}}}}
    with pytest.raises(DialerReferenceError, match='循环'):
        validate_emitted(main, provider_proxies={'Private': [{'name': 'remote', 'type': 'http'}]}, require_providers=True)


@pytest.mark.parametrize('builtin', ['DIRECT', 'REJECT'])
def test_builtin_exit_candidates_are_rejected_but_entry_choices_remain_explicit(builtin):
    config = profile({'id': 'choice', 'name': 'Choice', 'type': 'select', 'manual_nodes': ['a', builtin]})
    config['proxy_groups'][0]['chain']['entry'] = {'type': 'group', 'id': 'choice'}
    assert yaml.safe_load(generate_mihomo_config(config))['proxy-groups'][0]['proxies'] == ['A', builtin]
    config['proxy_groups'][0]['chain'] = {'entry': {'type': 'node', 'id': 'a'},
                                         'exit': {'type': 'group', 'id': 'choice'}}
    with pytest.raises(DialerReferenceError):
        generate_mihomo_config(config)


def test_same_group_entry_and_exit_are_independent_not_a_logical_cycle():
    config = profile({'id': 'choice', 'name': 'Choice', 'type': 'select', 'manual_nodes': ['a', 'b']})
    config['proxy_groups'][0]['chain'] = {'entry': {'type': 'group', 'id': 'choice'},
                                         'exit': {'type': 'group', 'id': 'choice'}}
    main = yaml.safe_load(generate_mihomo_config(config))
    groups = {g['name']: g for g in main['proxy-groups']}
    assert groups['Chain']['proxies'] != groups['Choice']['proxies']
    assert all(p['dialer-proxy'] == 'Choice' for p in main['proxies'] if p.get('dialer-proxy'))


def test_backtick_filters_preserve_global_provider_order_and_original_captures(monkeypatch):
    from backend.utils.dialer_references import provider_proxy_name, filter_provider_candidates
    config = dynamic_profile()
    config['proxy_groups'][1]['regex'] = '^(US)-`^HK-'
    config['proxy_groups'][1]['subscriptions'].append('second')
    config['subscriptions'].append({'id': 'second', 'name': 'Second', 'url': 'https://second.test'})
    memberships = {'feed': ['HK-first', 'US-first'], 'second': ['HK-second', 'US-second']}
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        lambda id, *_: (provider_text(memberships[id]), 'rendered_yaml'))
    main = yaml.safe_load(generate_mihomo_config(config))
    group = next(g for g in main['proxy-groups'] if g['name'] == 'Chain')
    candidates = []
    for name, raw in zip(group['use'], memberships.values()):
        provider = main['proxy-providers'][name]
        assert '`' not in provider['filter']
        for original in raw:
            transformed = provider_proxy_name({'name': original}, provider['override'])
            assert transformed.endswith(original)
            candidates.append({'name': transformed, 'original': original})
    ordered = filter_provider_candidates(candidates, group['filter'])
    assert [p['original'] for p in ordered] == ['US-first', 'US-second', 'HK-first', 'HK-second']


@pytest.mark.parametrize('chain_name, source_name, static_name', [
    ('Chain`名称', 'Feed', 'US-static'),
    ('Chain', 'Feed`来源', 'US-static'),
    ('Chain', 'Feed', 'US-`static'),
    ('Chain`名称', 'Feed`来源', 'US-`static'),
])
def test_backticks_in_names_preserve_mixed_candidate_bucket_order(chain_name, source_name, static_name):
    from backend.utils.dialer_references import provider_proxy_name, filter_provider_candidates
    config = dynamic_profile()
    config['proxy_groups'][0]['name'] = chain_name
    config['proxy_groups'][1].update(regex='^(US)-`^HK-', manual_nodes=['b', 'c', 'd'])
    config['nodes'][1]['name'] = static_name
    hk_static_name = 'HK-' + static_name.removeprefix('US-')
    config['nodes'].append(node('c', name=hk_static_name))
    excluded_static_name = 'DE-' + static_name.removeprefix('US-')
    config['nodes'].append(node('d', name=excluded_static_name))
    config['subscriptions'][0]['name'] = source_name
    config['proxy_groups'][1]['subscriptions'].append('second')
    config['subscriptions'].append({'id': 'second', 'name': 'Second', 'url': 'https://second.test'})
    before = deepcopy(config)
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    group = next(g for g in main['proxy-groups'] if g['name'] == chain_name)
    us_static, hk_static, excluded_static = group['proxies']
    candidates = [{'name': us_static, 'original': static_name},
                  {'name': hk_static, 'original': hk_static_name},
                  {'name': excluded_static, 'original': excluded_static_name}]
    memberships = [['HK-first', 'US-first'], ['HK-second', 'US-second']]
    for provider_name, raw in zip(group['use'], memberships):
        override = main['proxy-providers'][provider_name]['override']
        candidates.extend({'name': provider_proxy_name({'name': name}, override), 'original': name}
                          for name in raw)
    selected = filter_provider_candidates(candidates, group['filter'])
    assert [p['original'] for p in selected] == [
        static_name, 'US-first', 'US-second', hk_static_name, 'HK-first', 'HK-second']
    assert config == before


@pytest.mark.parametrize('regex, marker', [('^US-', ''), ('^(US)-`^HK-', '排序_1_')])
def test_readable_provider_names_survive_refresh_and_preserve_chain_isolation(regex, marker, monkeypatch):
    from backend.utils.dialer_references import provider_proxy_name
    config = dynamic_profile()
    config['proxy_groups'][1]['regex'] = regex
    config['proxy_groups'].extend([
        {'id': 'existing', 'name': 'Chain_Feed', 'type': 'select', 'manual_nodes': ['b']},
        chain('other', {'type': 'node', 'id': 'b'}, exit={'type': 'group', 'id': 'exit'}),
    ])
    membership = ['US-old', 'HK-old']
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        lambda *_: (provider_text(membership), 'rendered_yaml'))
    main = yaml.safe_load(generate_mihomo_config(config))
    groups = {g['name']: g for g in main['proxy-groups']}
    assert groups['Chain']['use'] == ['Chain_Feed (2)']
    assert groups['Other']['use'] == ['Other_Feed']
    for chain_name, dialer in [('Chain', 'A'), ('Other', 'B')]:
        provider_name, = groups[chain_name]['use']
        override = main['proxy-providers'][provider_name]['override']
        assert override['dialer-proxy'] == dialer
        assert provider_proxy_name({'name': 'US-old'}, override) == f'{provider_name}_{marker}US-old'
    membership[:] = ['US-new', 'HK-new']
    bundle = prepare_provider_bundle(config, main)
    payloads = {item['name']: yaml.safe_load(item['content'])['proxies'] for item in bundle}
    validate_emitted(main, provider_proxies=payloads, require_providers=True)
    for chain_name in ('Chain', 'Other'):
        provider_name, = groups[chain_name]['use']
        refreshed = payloads[provider_name][0]
        assert provider_proxy_name(refreshed, main['proxy-providers'][provider_name]['override']) == (
            f'{provider_name}_{marker}US-new')
    assert [p['name'] for p in payloads['Feed']] == membership


def test_provider_sorting_marker_collision_is_rejected_before_publication(monkeypatch):
    config = dynamic_profile()
    config['proxy_groups'][1]['regex'] = '.*`^HK-'
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        lambda *_: (provider_text(['排序_1_US-old']), 'rendered_yaml'))
    with pytest.raises(DialerReferenceError, match='排序标记冲突'):
        generate_mihomo_config(config)


def test_direct_and_aggregation_providers_share_one_subscription_materialization(monkeypatch):
    config = dynamic_profile()
    config['proxy_groups'].append({'id': 'aggregate', 'name': 'Aggregate group', 'type': 'select',
                                   'aggregations': ['agg']})
    config['subscription_aggregations'] = [{'id': 'agg', 'name': 'Aggregate', 'subscriptions': ['feed']}]
    calls = []

    def fetch(*args):
        calls.append(args)
        return provider_text(['US-one']), 'rendered_yaml'

    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml', fetch)
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    bundle = prepare_provider_bundle(config, main)
    assert calls == [('feed', 'https://feed.test')]
    assert {item['name'] for item in bundle}.issuperset({'Feed', 'Aggregate'})
    assert all(yaml.safe_load(item['content'])['proxies'][0]['name'] == 'US-one' for item in bundle)


def test_combined_prepared_source_is_reused_by_aggregation_without_fetch(monkeypatch):
    config = dynamic_profile()
    config['proxy_groups'][1].pop('subscriptions')
    config['proxy_groups'][1]['aggregations'] = ['agg']
    config['subscription_aggregations'] = [{'id': 'agg', 'name': 'Aggregate', 'subscriptions': ['feed']}]
    main = yaml.safe_load(generate_mihomo_config(config, preflight_providers=False))
    monkeypatch.setattr('backend.routes.subscriptions.get_subscription_proxies_yaml',
                        lambda *_: pytest.fail('Prepared subscription bytes must not be fetched again'))
    bundle = prepare_provider_bundle(config, main,
        prepared={'Feed': {'content': provider_text(['US-captured']), 'cache_updates': []}})
    assert all(yaml.safe_load(item['content'])['proxies'][0]['name'] == 'US-captured' for item in bundle)
