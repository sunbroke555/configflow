"""Source-specific group filters stay dynamic in Surge and Loon exports."""
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from backend.converters.loon import generate_loon_config
from backend.converters.surge import convert_proxy_group_to_surge, get_proxy_group_nodes


@pytest.fixture
def config():
    return {
        'profile_id': 'home',
        'system_config': {'server_domain': 'https://config.test', 'config_token': 'secret /&?'},
        'nodes': [{'id': 'manual', 'name': '手动-排除', 'type': 'ss', 'server': 'node.test', 'port': 443,
                   'params': {'cipher': 'aes-128-gcm', 'password': 'pw'}}],
        'subscriptions': [{'id': 'sub', 'name': 'Feed'}],
        'subscription_aggregations': [{'id': 'agg', 'name': 'Aggregate'}],
        'proxy_groups': [
            {'id': 'reference', 'name': '引用-排除', 'type': 'select', 'manual_nodes': ['DIRECT']},
            {'id': 'mixed', 'name': '混合', 'type': 'select', 'manual_nodes': ['manual'],
             'include_groups': ['reference'], 'subscriptions': ['sub'], 'aggregations': ['agg'],
             'regex': '(?i)HK|港', 'exclude_regex': '排除|倍率 [2-9]|&,+',
             'aggregation_regex': 'JP|日本', 'aggregation_exclude_regex': '维护|到期'},
        ],
    }


def _section(output, section):
    return output.split(f'[{section}]\n', 1)[1].split('\n[', 1)[0].strip().splitlines()


def _policy_queries(line):
    urls = [part.split(',', 1)[0] for part in line.split('policy-path = ')[1:]]
    return {urlsplit(url).path: parse_qs(urlsplit(url).query) for url in urls}


def _remote_entries(output):
    entries = {}
    for line in _section(output, 'Remote Proxy'):
        name, settings = line.split(' = ', 1)
        url = urlsplit(settings.split(',', 1)[0])
        entries[name] = (url.path, parse_qs(url.query))
    return entries


def test_surge_filters_each_dynamic_source_without_filtering_explicit_members(config):
    group = config['proxy_groups'][1]
    original = deepcopy(config)
    line = convert_proxy_group_to_surge(group, config)

    assert line.startswith('混合 = select, 手动-排除, 引用-排除, ')
    queries = _policy_queries(line)
    assert queries['/api/profiles/home/subscriptions/sub/proxies'] == {
        'format': ['surge'], 'token': ['secret /&?'],
        'regex': [group['regex']], 'exclude_regex': [group['exclude_regex']],
    }
    assert queries['/api/profiles/home/aggregations/agg/provider'] == {
        'format': ['surge'], 'token': ['secret /&?'],
        'regex': [group['aggregation_regex']], 'exclude_regex': [group['aggregation_exclude_regex']],
    }
    assert 'policy-regex-filter' not in line
    assert config == original


def test_surge_follow_inherits_exclusion_only_for_both_sources(config):
    source = config['proxy_groups'][1]
    source['regex'] = source['aggregation_regex'] = ''
    follower = {'id': 'follow', 'name': '跟随', 'type': 'select', 'follow_group': 'mixed',
                'regex': 'wrong', 'exclude_regex': 'wrong', 'aggregation_regex': 'wrong',
                'aggregation_exclude_regex': 'wrong'}
    config['proxy_groups'].append(follower)
    line = convert_proxy_group_to_surge(follower, config)

    assert line.startswith('跟随 = select, 手动-排除, 引用-排除, ')
    queries = _policy_queries(line)
    assert queries['/api/profiles/home/subscriptions/sub/proxies']['exclude_regex'] == [source['exclude_regex']]
    assert queries['/api/profiles/home/aggregations/agg/provider']['exclude_regex'] == [source['aggregation_exclude_regex']]
    assert all('regex' not in query for query in queries.values())


def test_surge_legacy_members_filter_before_adding_explicit_nodes():
    nodes = [{'name': name, 'subscription_id': 'sub'} for name in ('HK 01', 'HK 到期', 'JP 01')]
    group = {'subscriptions': ['sub'], 'regex': 'HK', 'exclude_regex': '到期', 'proxies': ['HK 到期']}
    assert get_proxy_group_nodes(group, {'nodes': nodes}) == ['HK 01', 'HK 到期']


def test_loon_separates_subscription_and_aggregation_filters(config):
    original = deepcopy(config)
    output = generate_loon_config(config)
    entries = _remote_entries(output)
    assert entries == {
        'Feed': ('/api/profiles/home/subscriptions/sub/proxies', {
            'format': ['loon'], 'token': ['secret /&?'], 'exclude_regex': ['排除|倍率 [2-9]|&,+'],
        }),
        'Aggregate': ('/api/profiles/home/aggregations/agg/provider', {
            'format': ['loon'], 'token': ['secret /&?'], 'exclude_regex': ['维护|到期'],
        }),
    }
    assert _section(output, 'Remote Filter') == [
        '混合-筛选 = NameRegex,Feed,FilterKey="(?i)HK|港"',
        '混合-聚合筛选 = NameRegex,Aggregate,FilterKey="JP|日本"',
    ]
    assert '混合 = select,手动-排除,引用-排除,混合-筛选,混合-聚合筛选' in _section(output, 'Proxy Group')
    assert config == original


def test_loon_remote_reuse_respects_each_group_exclusion_and_follow_source(config):
    source = config['proxy_groups'][1]
    source['regex'] = source['aggregation_regex'] = ''
    config['proxy_groups'].extend([
        {'id': 'plain', 'name': '全部', 'type': 'select', 'subscriptions': ['sub']},
        {'id': 'different', 'name': '另一个筛选', 'type': 'select', 'subscriptions': ['sub'],
         'exclude_regex': '另一种排除'},
        {'id': 'follow', 'name': '跟随', 'type': 'select', 'follow_group': 'mixed',
         'exclude_regex': 'wrong', 'aggregation_exclude_regex': 'wrong',
         'regex': 'wrong', 'aggregation_regex': 'wrong'},
    ])
    output = generate_loon_config(config)
    entries = _remote_entries(output)
    assert len(entries) == 4
    by_exclusion = {
        query.get('exclude_regex', [''])[0]: name
        for name, (path, query) in entries.items() if '/subscriptions/' in path
    }
    assert set(by_exclusion) == {'', source['exclude_regex'], '另一种排除'}
    groups = _section(output, 'Proxy Group')
    assert f"全部 = select,{by_exclusion['']}" in groups
    assert f"另一个筛选 = select,{by_exclusion['另一种排除']}" in groups
    mixed = next(line.split(' = ', 1)[1] for line in groups if line.startswith('混合 = '))
    assert f'跟随 = {mixed}' in groups
    assert _section(output, 'Remote Filter') == []
